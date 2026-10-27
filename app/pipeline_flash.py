# i18n: done
"""顶栏「闪购」：查看本机闪购助手的状态（只读），推给控制页；点「打开闪购助手」时执行 open -a。

只在 macOS、装了闪购助手时才有：main.py 在第一次 hello 之前调 attach_flash_sale，装了才往
server.config 里写 flash_sale 这个键、起轮询；没装就连这个键都没有，页面据此不画按钮（同浮窗的
float_window）。是否安装只在启动时判一次。不在 Pipeline.__init__ 里检测：测试大量构造 Pipeline，
开发机上可能真装着闪购助手，放在那里本机和 CI 的结果就会不一样，轮询还会真的去连本机的 9334。

隔离：
- 手机同看收不到：flash_sale 不在 app/viewer.py 的 ALLOW 里，另外显式写进 DENY；replay_snapshot 只挑固定几个键；
- 浮窗收不到：不在 app/server.py 的 FLOAT_TYPES / FLOAT_CONFIG_KEYS 里；
- 不借报警、incident、status、顶部提示条来提醒（CLAUDE.md 第六条）：提醒只是顶栏按钮自己变色；
- 不控制闪购助手，也不碰 TikTok：只发 GET /status，再就是 open -a。
不写审计、不写 session 日志、不加 settings 键。终端只在状态跳变时打一行（中文），不打印商品标题和记录正文。
"""
import asyncio
import time
from urllib.parse import urlsplit

from . import flash_sale
from .i18n import L


class FlashSaleMixin:
    def _init_flash_sale(self):
        self._flash_app = None          # 检测到的 .app 路径；None = 没装（或不是 macOS），整个功能不存在
        self._flash_task = None         # 轮询任务
        self._flash_last = None         # 上次发布的 dict：没变就不广播（一次广播最坏被卡死的页面拖 2 秒）
        self._flash_conn_prev = None    # 上一次的 conn：终端行只在跳变时打
        self._flash_why_prev = None     # 上一次 bad_response 的原因：原因变了也算跳变
        self._flash_tracker = flash_sale.StatusTracker()
        self._flash_boost_until = None  # 点「打开」、open 返回 0 之后快查到哪一刻（单调时钟）
        self._flash_wake_ev = None      # 唤醒轮询；首次使用时才建（Python 3.9 构造 Event 就绑事件循环）
        self._flash_opening = False     # 防连点：上一次 open 还没返回
        self._flash_logged = set()      # 已经打过的异常种类、schema：同一种只打一次
        # 可注入（同 _brand_dir_opener）：测试换成假的，绝不碰本机真的 9334，也不真的执行 open
        self._flash_fetch = flash_sale.fetch_status
        self._flash_url = flash_sale.STATUS_URL
        self._flash_exec = None         # None = asyncio.create_subprocess_exec（调用时取）
        self._flash_mono = time.monotonic   # 陈旧、卡住都按单调时钟计（合盖睡眠时它不走，见 StatusTracker）

    # ---- 启动、停止 ----

    async def attach_flash_sale(self, platform=None, candidates=None):
        """main.py 在第一次 hello 之前调。装了才写 config 键、起轮询；任何异常都吞掉，不挡启动。"""
        try:
            app = flash_sale.find_app(platform=platform, candidates=candidates)
            if app is None:
                return
            self._flash_app = app
            state = flash_sale.empty_state("checking")
            self._flash_last = state
            self._flash_conn_prev = "checking"
            self.server.config["flash_sale"] = state
            print("[信息] 闪购助手：已装，开始查看 {} 的状态".format(self._flash_where()))
            self._start_flash_watch()
        except Exception as exc:
            print("[警告] 闪购助手：启动时出错（{}），这一次不显示闪购按钮".format(type(exc).__name__))
            self._flash_app = None
            self.server.config.pop("flash_sale", None)

    def _start_flash_watch(self):
        if self._flash_task is not None:
            return
        self._flash_task = self._spawn(self._flash_watch())

    def stop_flash_watch(self):
        """退出时（main.py 的收尾、关窗口的 close_session）调；也给测试用。可重入，绝不抛。"""
        task = getattr(self, "_flash_task", None)
        self._flash_task = None
        if task is not None:
            task.cancel()

    # ---- 轮询 ----

    async def _flash_watch(self):
        """第一次不等，直接查。每一跳单独兜异常：一个 except 包住整个循环，出一次错轮询就永久停了
        （app/viewer_share.py _viewer_ip_watch 是反例，app/pipeline.py _stats_loop 那段注释讲了教训）。"""
        while True:
            try:
                await self._flash_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._flash_log_once("tick:" + type(exc).__name__,
                                     "[警告] 闪购助手：查状态出错（{}），下一次照常".format(type(exc).__name__))
            await self._flash_sleep(self._flash_interval())

    async def _flash_tick(self):
        kind, value = await self._flash_fetch(self._flash_url)
        state = self._flash_tracker.update(kind, value, self._flash_mono())
        await self._flash_publish(state)
        self._flash_log_transition(state)

    def _flash_interval(self):
        boost = self._flash_boost_until
        if boost is not None and self._flash_mono() < boost:
            return flash_sale.POLL_OK_SEC
        return flash_sale.POLL_OK_SEC if self._flash_conn_prev == "ok" else flash_sale.POLL_DOWN_SEC

    def _flash_wake_event(self):
        if self._flash_wake_ev is None:
            self._flash_wake_ev = asyncio.Event()
        return self._flash_wake_ev

    async def _flash_sleep(self, sec):
        """睡 sec 秒，或者被 _flash_wake 叫醒（点了「打开」、open 返回 0）。"""
        ev = self._flash_wake_event()
        try:
            await asyncio.wait_for(ev.wait(), sec)
        except asyncio.TimeoutError:
            pass
        finally:
            ev.clear()

    def _flash_wake(self):
        self._flash_wake_event().set()

    async def _flash_publish(self, state):
        """和上次发布的比，没变就不发。不下发 updated_at（它每 2 秒变一次，下发了去重就失效），
        所以只有真正的变化才广播。先写 config（hello 整份带上，重连自动补发），再广播；每次一个新 dict，
        不原地改：hello 和缓存共用这个对象。"""
        if state == self._flash_last:
            return False
        self.server.config["flash_sale"] = state
        self._flash_last = None         # 广播半路出错：下一跳再发一次
        await self.server.broadcast(dict(state, type="flash_sale"))
        self._flash_last = state
        return True

    # ---- 终端 ----

    def _flash_where(self):
        try:
            return urlsplit(self._flash_url).netloc or str(self._flash_url)
        except (TypeError, ValueError):
            return str(self._flash_url)

    def _flash_log_once(self, key, line):
        if key in self._flash_logged:
            return
        self._flash_logged.add(key)
        print(line)

    def _flash_log_transition(self, state):
        conn = state.get("conn")
        prev, self._flash_conn_prev = self._flash_conn_prev, conn
        why = self._flash_tracker.why if conn == "bad_response" else None
        prev_why, self._flash_why_prev = self._flash_why_prev, why
        if conn == "ok":
            if prev != "ok":
                print("[信息] 闪购助手：已连上 {}（版本 {}）".format(
                    self._flash_where(), state.get("app_version") or "?"))
            schema = self._flash_tracker.schema
            if schema != 1:
                self._flash_log_once("schema:{}".format(schema),
                                     "[信息] 闪购助手：/status 的 schema 是 {}，按 1 尽量读".format(schema))
        elif conn == "unreachable":
            if prev != "unreachable":
                print("[信息] 闪购助手：连不上 {}，之后每 {} 秒查一次".format(
                    self._flash_where(), self._flash_interval()))
        elif conn == "bad_response":
            # 进入读不出、或原因变了才打一行（同连不上：一直这样就不刷屏）。no_answer 是连得上、没回话：
            # 只写看到的，不说它卡住了
            if prev != "bad_response" or prev_why != why:
                what = "没有回话" if why == "no_answer" else "返回的内容读不出"   # i18n: terminal
                print("[警告] 闪购助手：{} {}（{}）".format(self._flash_url, what, why))

    # ---- 「打开闪购助手」 ----

    def _open_flash_helper(self):
        """控制页的 open_flash_helper 指令（handle_control 调，同步）。没装、或上一次还没返回就什么都不做。
        放后台跑，不在这里 await：免得挡住同一页面后面的「停止」（同 retranslate、apply_update）。
        手机同看发不进来（app/viewer.py _handle_inbound 只认 retranslate），浮窗页的入站一律丢弃。"""
        if self._flash_app is None or self._flash_opening:
            return None
        self._flash_opening = True
        self._spawn(self._flash_open_run())
        return None

    async def _flash_open_run(self):
        """open -a <完整路径>，看返回码。0：之后 BOOST_SEC 秒里每 3 秒查一次，并马上查一次（状态自己会变，
        不发提示）。非 0、超时、出错：终端一行，再发一条 notice（where=flash，页面写在闪购浮层里）。
        只写观察到的事（返回码），不猜原因（CLAUDE.md 第八条）。"""
        rc = None
        try:
            argv = flash_sale.open_argv(self._flash_app)
            print("[信息] 闪购助手：执行 open -a {}".format(self._flash_app))
            run = self._flash_exec or asyncio.create_subprocess_exec
            try:
                proc = await run(*argv, stdout=asyncio.subprocess.DEVNULL,
                                 stderr=asyncio.subprocess.DEVNULL)
                rc = await asyncio.wait_for(proc.wait(), flash_sale.OPEN_TIMEOUT_SEC)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                print("[警告] 闪购助手：open 等了 {} 秒还没返回".format(flash_sale.OPEN_TIMEOUT_SEC))
            except Exception as exc:
                print("[警告] 闪购助手：open 没能执行（{}）".format(type(exc).__name__))
            if rc == 0:
                self._flash_boost_until = self._flash_mono() + flash_sale.BOOST_SEC
                self._flash_wake()
                return
            if rc is None:
                text = L("没能打开闪购助手。", "Couldn’t open Flash Sale Helper.")
            else:
                print("[警告] 闪购助手：open 返回 {}，没有打开".format(rc))
                text = L("没能打开闪购助手（open 返回 {}）。",
                         "Couldn’t open Flash Sale Helper (open returned {}).").format(rc)
            await self.server.broadcast({"type": "notice", "where": "flash", "text": text})
        finally:
            self._flash_opening = False
