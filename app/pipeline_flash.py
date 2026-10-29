# i18n: done
"""顶栏「闪购」：查看本机闪购助手的状态，推给控制页；点「打开闪购助手」时执行 open -a；闪购助手 0.8.0 起，
浮层里的「开始」「停止」「继续」由这里替人按下闪购助手面板上的那个大按钮（POST /start、/stop、/resume）。

只在 macOS、装了闪购助手时才有：main.py 在第一次 hello 之前调 attach_flash_sale，装了才往
server.config 里写 flash_sale 这个键、起轮询；没装就连这个键都没有，页面据此不画按钮（同浮窗的
float_window）。是否安装只在启动时判一次。不在 Pipeline.__init__ 里检测：测试大量构造 Pipeline，
开发机上可能真装着闪购助手，放在那里本机和 CI 的结果就会不一样，轮询还会真的去连本机的 9334。
演示模式（--demo）不调 attach_flash_sale：不查、不发，flash_control 指令也什么都不做。

隔离：
- 手机同看收不到：flash_sale、flash_control_result 不在 app/viewer.py 的 ALLOW 里，另外显式写进 DENY；
  replay_snapshot 只挑固定几个键。手机也发不进 flash_control（_handle_inbound 只认 retranslate）；
- 浮窗收不到：不在 app/server.py 的 FLOAT_TYPES / FLOAT_CONFIG_KEYS 里；浮窗页的入站一律丢弃；
- 不借报警、incident、status、顶部提示条来提醒（CLAUDE.md 第六条）：提醒只是顶栏按钮自己变色；
- 不碰 TikTok。控制只替人按开始、停止、继续：不新建闪购、不改价、不加商品，第一次的确认框照旧在专用 Chrome 里点。
不写审计、不写 session 日志、不加 settings 键。终端只在状态跳变、发控制指令时打一行（中文），不打印商品标题、
记录正文，也绝不打印控制密钥。

控制（_flash_control）谁能发：本机控制页，以及没带 Origin 的本机进程（curl、本地脚本；app/server.py 把它们
也当控制面）。后者只能发停止：开始、继续回 local_only（app/server.py CONTROL_SOURCE，直播助手平台轮廓 §5.6）。
「开始」还要点两下、两下都到后端（step=arm、step=fire，见 _flash_take_arm）。这两条防的是误触和「只发一条消息的
脚本」，不是安全边界：所以闪购控制的安全边界仍是「连得上本机 127.0.0.1 的控制端口」，不是密钥文件的 600 权限。同一用户的
进程本来就能直接读那个文件、自己去 POST 9334，借这条路不多得什么；多出来的只是连得上回环、却读不到这个用户
~/Library 的进程（别的账户、带网络权限的沙盒应用），在单用户的中控电脑上可以接受（没带 Origin 的连接现在
本来就能发 disk_delete、apply_update）。剩下一个契约本身的限制：有进程占了 9334、照着契约回 /status，
就拿得到密钥（本机回环上的明文 HTTP，客户端没法认证服务端）。这里能做的是看过 /status 的回话像闪购助手才读密钥、
才发（不然 no_status）：开始、继续看最近一次轮询（连上并读懂了、schema 是 1、page 是契约里的代码，见
StatusTracker.control_refusal）；停止不看上一次读得怎么样，发之前当场读一次，连得上、schema 是 1 就发（见
flash_sale.stop_precheck、_flash_stop_run：停止在出事时也要发得出去）。挡得住碰巧在 9334 上回 JSON 的别的服务，
挡不住存心冒充的进程。
"""
import asyncio
import time
from urllib.parse import urlsplit

from . import flash_sale
from .i18n import L
from .server import CONTROL_SOURCE

# 终端行里的动作名（终端永远是中文）
_CTL_NAMES = {"start": "开始", "stop": "停止", "resume": "继续"}   # i18n: terminal
# 闪购助手没有接受以外的几种结果在终端怎么说（页面拿到的是同一个代码，文案在 web/flash.js）
_CTL_OUTCOMES = {"no_status": "没有发出", "no_token": "没有发出", "unreachable": "连不上",
                 "no_answer": "没有回话", "bad_reply": "回话读不出", "failed": "出错",
                 "local_only": "没有发出", "not_confirmed": "没有发出"}   # i18n: terminal
_CTL_REFUSED = "没有接受"   # i18n: terminal


class FlashSaleMixin:
    def _init_flash_sale(self):
        self._flash_app = None          # 检测到的 .app 路径；None = 没装（或不是 macOS），整个功能不存在
        self._flash_task = None         # 轮询任务
        self._flash_base = None         # StatusTracker 最近交出来的状态（还没叠上控制指令的进度）
        self._flash_last = None         # 上次发布的 dict：没变就不广播（一次广播最坏被卡死的页面拖 2 秒）
        self._flash_conn_prev = None    # 上一次的 conn：终端行只在跳变时打
        self._flash_why_prev = None     # 上一次 bad_response 的原因：原因变了也算跳变
        self._flash_tracker = flash_sale.StatusTracker()
        self._flash_boost_until = None  # 点「打开」open 返回 0、或控制指令被接受之后快查到哪一刻（单调时钟）
        self._flash_wake_ev = None      # 唤醒轮询；首次使用时才建（Python 3.9 构造 Event 就绑事件循环）
        self._flash_pub_lock = None     # (事件循环, asyncio.Lock)：轮询和控制两条路发布状态时排队，见 _flash_refresh
        self._flash_opening = False     # 防连点：上一次 open 还没返回
        # 控制指令的进度：None，或 {"action", "phase", "until"}。phase 是 sending（在途）、waiting（已接受、
        # 等它生效，until 是等到哪一刻，单调时钟），或 retrying（只有停止：上一次没被接受，隔 3 秒再试，见
        # _flash_stop_run）；waiting 时看到过生效、还在等页面稳下来，另带 "seen": True（见 _flash_check_control）。
        # 发布时只带 action 和 phase（见 _flash_compose）
        self._flash_ctl = None
        self._flash_logged = set()      # 已经打过的异常种类、schema：同一种只打一次
        # 「开始」的第一下（{"id", "at"}，at 是单调时钟）：控制页发 step=arm 时记下，step=fire 来时取走（只用一次），
        # 停止也把它清掉。见 _flash_take_arm
        self._flash_arm = None
        # 可注入（同 _brand_dir_opener）：测试换成假的，绝不碰本机真的 9334、绝不读本机真的控制密钥，
        # 也不真的执行 open。地址和密钥位置取的是此刻模块里的值（tests/conftest.py 已换掉）
        self._flash_fetch = flash_sale.fetch_status
        self._flash_url = flash_sale.STATUS_URL
        self._flash_post = flash_sale.post_control
        self._flash_ctl_base = flash_sale.CONTROL_BASE
        self._flash_read_token = flash_sale.read_control_token
        self._flash_exec = None         # None = asyncio.create_subprocess_exec（调用时取）
        self._flash_mono = time.monotonic   # 陈旧、卡住、等生效都按单调时钟计（合盖睡眠时它不走，见 StatusTracker）
        self._flash_retry_sleep = asyncio.sleep   # 停止两次尝试之间的等待（测试换成拨钟的假 sleep）

    # ---- 启动、停止 ----

    async def attach_flash_sale(self, platform=None, candidates=None):
        """main.py 在第一次 hello 之前调。装了才写 config 键、起轮询；任何异常都吞掉，不挡启动。"""
        try:
            app = flash_sale.find_app(platform=platform, candidates=candidates)
            if app is None:
                return
            self._flash_app = app
            state = flash_sale.empty_state("checking")
            self._flash_base = state
            self._flash_last = state
            self._flash_conn_prev = "checking"
            # 发布闸（flash_sale.START_ENABLED）：页面据此画不画「开始」「继续」。和 flash_sale 一起，装了才有
            self.server.config["flash_start_enabled"] = flash_sale.START_ENABLED is True
            self.server.config["flash_sale"] = state
            print("[信息] 闪购助手：已装，开始查看 {} 的状态".format(self._flash_where()))
            self._start_flash_watch()
        except Exception as exc:
            print("[警告] 闪购助手：启动时出错（{}），这一次不显示闪购按钮".format(type(exc).__name__))
            self._flash_app = None
            self.server.config.pop("flash_sale", None)
            self.server.config.pop("flash_start_enabled", None)

    def _start_flash_watch(self):
        if self._flash_task is not None:
            return
        self._flash_task = self._spawn(self._flash_watch())

    def stop_flash_watch(self):
        """退出时（main.py 的收尾、关窗口的 close_session）调；也给测试用。可重入，绝不抛。
        只停轮询。在途的控制请求（_flash_control_run）不取消，让它发完：POST 最多 4 秒（连接 2 秒、读 2 秒）。
        尤其是「停止」：半路取消了，不知道闪购助手收到没有，还不如让它发完；停止的重试（_flash_stop_run）也不取消。
        进程等不等得到它看收尾的时限，结果广播不到页面也没关系（页面已经关了）。"""
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
        base = self._flash_base = self._flash_tracker.update(kind, value, self._flash_mono())
        event = self._flash_check_control(base)
        await self._flash_refresh()
        self._flash_log_transition(base)
        if event is not None:
            await self._flash_control_event(*event)

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
        """睡 sec 秒，或者被 _flash_wake 叫醒（点了「打开」open 返回 0、控制指令被接受）。"""
        ev = self._flash_wake_event()
        try:
            await asyncio.wait_for(ev.wait(), sec)
        except asyncio.TimeoutError:
            pass
        finally:
            ev.clear()

    def _flash_wake(self):
        self._flash_wake_event().set()

    # ---- 发布 ----

    def _flash_compose(self):
        """最近一次查到的状态叠上此刻控制指令的进度：control 换成 {"action", "phase"}（不带 until：它不变，
        但下发了也没用，页面不自己计时），attention 跟着重算（开始、继续被接受后等生效的那 30 秒里，有几种情况
        不算要人处理，见 flash_sale.attention）。没有指令时就是 base 本身。每次一个新 dict，不原地改 base。"""
        base = self._flash_base
        if base is None:
            return None
        ctl = self._flash_ctl
        shown = None if ctl is None else {"action": ctl["action"], "phase": ctl["phase"]}
        if base.get("control") == shown:
            return base
        state = dict(base, control=shown)
        state["attention"] = flash_sale.attention(state)
        return state

    def _flash_lock(self):
        loop = asyncio.get_running_loop()
        held = self._flash_pub_lock
        if held is None or held[0] is not loop:
            held = self._flash_pub_lock = (loop, asyncio.Lock())
        return held[1]

    async def _flash_refresh(self):
        """把此刻的状态（_flash_compose）发出去，没变就不发。轮询和控制两条路都走这里，在一把锁里排队：
        合成和广播之间有 await，不排队的话先合成的那份可能后到页面——页面停在旧的控制进度上，之后合成出来的
        又和「上次发的」相等，不会再发。在锁里才合成，所以排在后面的那一次发的一定是最新的。"""
        async with self._flash_lock():
            state = self._flash_compose()
            if state is None:
                return False
            return await self._flash_publish(state)

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

    def _flash_log_control(self, action, error, detail=None, tries=1, retrying=False):
        """终端一行中文：「闪购助手：中控请求开始 → 已接受 / 没有接受（代码）」，没发出、连不上、没回话另有说法。
        停止再试过的（tries > 1）末尾带「第 n 次」「一共试了 n 次」；retrying：停止第一次没被接受、接下来要再试。
        不打密钥、不打商品标题：detail 只会是读密钥的原因代码、没发的原因（schema、page，或停止当场读 /status 时的
        原因代码，见 _flash_control、flash_sale.stop_precheck），或者异常的类型名。"""
        name = _CTL_NAMES.get(action, action)
        if error is None:
            more = "" if tries <= 1 else "（第 {} 次）".format(tries)   # i18n: terminal
            print("[信息] 闪购助手：中控请求{} → 已接受{}".format(name, more))
            return
        code = error if detail is None else "{}：{}".format(error, detail)   # i18n: terminal
        if retrying:
            more = "，{} 秒内每 {} 秒再试".format(flash_sale.STOP_RETRY_SEC, flash_sale.STOP_RETRY_GAP_SEC)   # i18n: terminal
        else:
            more = "" if tries <= 1 else "，一共试了 {} 次".format(tries)   # i18n: terminal
        print("[警告] 闪购助手：中控请求{} → {}（{}）{}".format(name, _CTL_OUTCOMES.get(error, _CTL_REFUSED), code, more))

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

    # ---- 开始、停止、继续 ----

    def _flash_control(self, msg):
        """控制页的 flash_control 指令（handle_control 调，同步，立刻返回）：{"type": "flash_control",
        "action": "start" | "stop" | "resume"}，别的键一概不看。谁能发、安全边界在哪，见文件头。
        - 没装（演示模式也是这样）、动作不在白名单里：什么都不做（页面上本来没有这样的按钮）；
        - 开始、继续，发布闸关着（flash_sale.START_ENABLED）：什么都不做，终端一行（页面上这时没有这两颗按钮）。
          停止不受闸管；
        - 开始、继续来自没带 Origin 的本机进程（CONTROL_SOURCE 是 local）：不发，结果 error=local_only。停止照发；
        - 上一个指令还在途（phase=sending）：什么都不做。页面那边按钮是禁用的（发布的状态里 control.phase 是
          sending），连点、两个控制页同时点，都只发一个；
        - 开始：{"step": "arm", "id": 编号} 只记下第一下（_flash_take_arm），不回话；要等同一个编号的 {"step": "fire"}
          在 START_GUARD_SEC 到 START_CONFIRM_SEC 秒之间到，才往下走。不是这样的（没带 step、没有 arm、编号对不上、
          太快、太晚）不发，结果 error=not_confirmed，终端带是哪一种。继续点一下就发；
        - 停止：不看上一次轮询读得怎么样，放后台当场读一次 /status 再发，没被接受的按 flash_sale.STOP_RETRY_CODES
          再试（_flash_stop_run）。已经在等着再试（phase=retrying）时再点停止：什么都不做；
        - 开始、继续：停止正在等着再试（phase=retrying）的话，那些还没发的重试作废（直播助手平台轮廓 §5.5：你又点了
          开始）。最近一次 /status 不是连上并读懂了（连不上、读不出、上一跳没查到、还没查过），或者读懂了却不像闪购助手的
          回话（schema 不是 1、page 不是契约里的代码，见 StatusTracker.control_refusal）：不读密钥、不发，结果
          error=no_status，终端在后一种时另带原因（schema、page）。在等生效的上一个指令照旧等着；
        - 其余放后台发（_flash_control_run），不在这里 await：不挡同一页面后面的消息。"""
        if self._flash_app is None:
            return None
        action = msg.get("action")
        if not isinstance(action, str) or action not in flash_sale.CONTROL_ACTIONS:
            return None
        if action != "stop" and flash_sale.START_ENABLED is not True:
            print("[信息] 闪购助手：中控请求{} → 没有发出（开始、继续还没开放）".format(_CTL_NAMES[action]))
            return None
        if action != "stop" and CONTROL_SOURCE.get() != "page":
            self._spawn(self._flash_control_refused(action, "local_only"))
            return None
        ctl = self._flash_ctl
        if ctl is not None and ctl["phase"] == "sending":
            return None
        if action == "start":
            if msg.get("step") == "arm":
                self._flash_arm_start(msg.get("id"))
                return None
            problem = self._flash_take_arm(msg)
            if problem is not None:
                self._spawn(self._flash_control_refused(action, "not_confirmed", problem))
                return None
        if action == "stop":
            self._flash_arm = None          # 停下了：之前点的第一下作废
            if ctl is not None and ctl["phase"] == "retrying":
                return None
            self._flash_ctl = {"action": "stop", "phase": "sending", "until": None}
            self._spawn(self._flash_stop_run(ctl))
            return None
        if ctl is not None and ctl["phase"] == "retrying":
            # 停止还在等着再试：作废。_flash_stop_run 醒来看到进度换了人，就不再发
            print("[信息] 闪购助手：中控请求{}，停止的重试作废".format(_CTL_NAMES.get(action, action)))
            self._flash_ctl = ctl = None
        refusal = self._flash_tracker.control_refusal()
        if refusal is not None:
            detail = None if refusal == "not_ok" else refusal
            self._spawn(self._flash_control_refused(action, "no_status", detail))
            return None
        self._flash_ctl = {"action": action, "phase": "sending", "until": None}
        self._spawn(self._flash_control_run(action, ctl))
        return None

    def _flash_arm_start(self, arm_id):
        """「开始」的第一下：记下编号和此刻（单调时钟）。编号不是 1–64 个字母、数字、_、- 的不记（之后的 fire 就是 no_arm）。
        再点一次第一下（这一页或别的控制页）就换成新的这一个。"""
        if isinstance(arm_id, str) and flash_sale.ARM_ID.fullmatch(arm_id):
            self._flash_arm = {"id": arm_id, "at": self._flash_mono()}

    def _flash_take_arm(self, msg):
        """「开始」的第二下（step=fire）对不对得上第一下：None = 对得上；否则是原因（终端用）：no_step（没带 step=fire，
        比如旧页面、脚本只发一条）、no_arm（前面没有 arm，或已经用掉、被停止清掉了）、other_arm（编号不是最近那次 arm 的）、
        too_soon（离 arm 不到 START_GUARD_SEC 秒：双击一次）、too_late（满 START_CONFIRM_SEC 秒）。
        不管对不对得上，arm 都用掉了（只认一次）：要开始就再点两下。"""
        arm, self._flash_arm = self._flash_arm, None
        if msg.get("step") != "fire":
            return "no_step"
        if arm is None:
            return "no_arm"
        if msg.get("id") != arm["id"]:
            return "other_arm"
        gap = self._flash_mono() - arm["at"]
        if gap < flash_sale.START_GUARD_SEC:
            return "too_soon"
        if gap >= flash_sale.START_CONFIRM_SEC:
            return "too_late"
        return None

    async def _flash_control_refused(self, action, error, detail=None):
        self._flash_log_control(action, error, detail)
        try:
            await self._flash_refresh()   # 停止的重试刚作废时，页面上的「正在重试…」要收掉；别的时候状态没变，不发
        except Exception:
            pass
        await self._flash_send_result(action, error)

    async def _flash_stop_run(self, prev):
        """停止（_flash_control 已经把进度记成在途）：当场读一次 /status、读密钥、发 POST /stop（_flash_stop_attempt），
        没被接受、而且是「过一会儿就能收」的那几种（flash_sale.stop_should_retry：连不上、发出了没回话、闪购助手回
        busy、loading、no_response；当场读 /status 连不上、没读懂也算），隔 STOP_RETRY_GAP_SEC 秒再试，直到收到别的结果，
        或者从第一次算起再试一次就要超过 STOP_RETRY_SEC 秒。awaiting_approval、captcha 这类要人去专用 Chrome 里处理的，
        不再试（页面那句告诉人去哪儿点）。
        等着再试的那几秒，进度是 {"action": "stop", "phase": "retrying"}（页面写「正在重试…」）；这时有人点了开始、继续，
        _flash_control 把进度换掉，这里醒来看到换了人就不再发、不发结果（作废那一行终端已经打过）。每一次尝试都重新读
        /status 和密钥。接受了以后和别的指令一样等生效（phase=waiting）；最后只发一条结果，再试过的带 tries（试了几次）。
        退出时不取消（同 _flash_control_run）；真被取消了（事件循环收摊），进度还是这里的就换回发之前的那一份。"""
        started = self._flash_mono()
        tries = 0
        mine = self._flash_ctl
        finished = False
        try:
            while True:
                tries += 1
                attempt = asyncio.ensure_future(self._flash_stop_attempt())
                try:
                    await self._flash_refresh()   # 同 _flash_control_run：先发出去，再把 phase=sending 发布出去
                except Exception:
                    pass
                error, detail, retry = await attempt
                if self._flash_ctl is not mine:
                    finished = True
                    return                        # 在途时进度不会换人（_flash_control 都丢掉）；保险
                spent = self._flash_mono() - started
                if error is None or not retry or spent + flash_sale.STOP_RETRY_GAP_SEC > flash_sale.STOP_RETRY_SEC:
                    break
                mine = self._flash_ctl = {"action": "stop", "phase": "retrying", "until": None}
                if tries == 1:
                    self._flash_log_control("stop", error, detail, retrying=True)
                try:
                    await self._flash_refresh()
                except Exception:
                    pass
                await self._flash_retry_sleep(flash_sale.STOP_RETRY_GAP_SEC)
                if self._flash_ctl is not mine:
                    finished = True
                    return                        # 作废了：点了开始、继续
                mine = self._flash_ctl = {"action": "stop", "phase": "sending", "until": None}
            finished = True
        finally:
            if not finished and self._flash_ctl is mine:
                self._flash_ctl = prev
        if error is None:
            self._flash_ctl = {"action": "stop", "phase": "waiting",
                               "until": self._flash_mono() + flash_sale.CONTROL_WAIT_SEC}
        else:
            self._flash_ctl = prev
        self._flash_log_control("stop", error, detail, tries=tries)
        if error is None:
            self._flash_boost_until = self._flash_mono() + flash_sale.BOOST_SEC
            self._flash_wake()
        try:
            await self._flash_refresh()
        except Exception:
            pass
        await self._flash_send_result("stop", error, tries)

    async def _flash_stop_attempt(self):
        """停止的一次尝试 → (error, detail, 要不要再试)。error 为 None 是接受了。当场读一次 /status
        （flash_sale.stop_precheck：连得上、schema 是 1 才往下走），再读密钥、发 POST /stop。密钥只在这里的一个
        局部变量里待到交给 _flash_post（同 _flash_control_run）。后端自己出错：failed，不再试。"""
        try:
            kind, value = await self._flash_fetch(self._flash_url)
            pre = flash_sale.stop_precheck(kind, value)
            if pre is not None:
                return pre
            token, why = self._flash_read_token()
            if token is None:
                return "no_token", why, False
            post = self._flash_post("stop", token, self._flash_ctl_base)
            token = None
            error = await post
            return error, None, flash_sale.stop_should_retry(error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return "failed", type(exc).__name__, False

    async def _flash_control_run(self, action, prev):
        """开始、继续（停止走 _flash_stop_run）：读密钥 → 发 POST → 收结果，结果用 flash_control_result 广播给控制页。
        不自动重发（直播助手平台轮廓 §9.2 第 7 条：只有停止再试）。
        - 读不到密钥：不发，error=no_token（终端带原因代码，例如 missing）；
        - 发出去以后同时把 phase=sending 发布出去（别的控制页也把按钮禁用），不等广播再发：广播最坏被卡住的页面
          拖 2 秒，「停止」不该跟着晚；
        - 接受了（200）：phase 换成 waiting，CONTROL_WAIT_SEC 秒里每 3 秒查一次 /status、每一跳看它生效没有、
          开始和继续还看页面稳下来没有（_flash_check_control），并马上查一次；
        - 没接受、没发出、出错：控制进度换回发之前的那一份（在等生效的上一个指令照旧等着）。
        密钥只在这里的一个局部变量里待到交给 _flash_post：不打印、不广播、不进 config；出错时终端只打异常的
        类型名（aiohttp 的 ClientResponseError 的 repr 里带着 Authorization）。退出时不取消，见 stop_flash_watch。"""
        sending = self._flash_ctl
        error, detail, finished = None, None, False
        try:
            token, why = self._flash_read_token()
            if token is None:
                error, detail = "no_token", why
            else:
                post = asyncio.ensure_future(self._flash_post(action, token, self._flash_ctl_base))
                token = None
                try:
                    await self._flash_refresh()
                except Exception:
                    pass                  # 发布失败不挡指令：下一跳轮询会再发
                error = await post
            finished = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error, detail, finished = "failed", type(exc).__name__, True
        finally:
            if self._flash_ctl is sending:
                if finished and error is None:
                    self._flash_ctl = {"action": action, "phase": "waiting",
                                       "until": self._flash_mono() + flash_sale.CONTROL_WAIT_SEC}
                else:
                    self._flash_ctl = prev
        self._flash_log_control(action, error, detail)
        if error is None:
            self._flash_boost_until = self._flash_mono() + flash_sale.BOOST_SEC
            self._flash_wake()
        try:
            await self._flash_refresh()
        except Exception:
            pass
        await self._flash_send_result(action, error)

    def _flash_check_control(self, base):
        """每一跳查完、发布之前调，看等生效的那个指令：
        - 第一次看到它生效（flash_sale.control_done）：返回 ("done", 动作)，记下 seen（之后不再报）；
        - 生效过了、页面也稳下来了（flash_sale.control_settled）：收掉。开始、继续生效的时候页面多半还在刷新
          （running 先变 true，列表回来之前是 no_list）：这时不收，phase 照旧是 waiting，安静窗口接着算
          （flash_sale.attention）；停止生效就收；
        - 等满了 CONTROL_WAIT_SEC：收掉。一直没看到生效的返回 ("not_seen", 动作)，看到过的不再报。
        返回 None、("done", 动作) 或 ("not_seen", 动作)，由调用方打终端行、发结果。
        指令还在途（phase=sending）时不碰：它还没有「等到哪一刻」，回来以后自己定接下来是什么。"""
        ctl = self._flash_ctl
        if ctl is None or ctl["phase"] != "waiting":
            return None
        action, event = ctl["action"], None
        if not ctl.get("seen") and flash_sale.control_done(action, base):
            ctl = self._flash_ctl = dict(ctl, seen=True)
            event = "done", action
        if ctl.get("seen") and flash_sale.control_settled(action, base):
            self._flash_ctl = None
        elif self._flash_mono() >= ctl["until"]:
            self._flash_ctl = None
            if not ctl.get("seen"):
                event = "not_seen", action
        return event

    async def _flash_control_event(self, kind, action):
        name = _CTL_NAMES.get(action, action)
        if kind == "done":
            print("[信息] 闪购助手：中控请求{} → 已生效".format(name))
            return
        # 只写看到的：这么久没看到它生效，不猜为什么（第八条）
        print("[警告] 闪购助手：中控请求{} → {} 秒内没看到生效".format(name, flash_sale.CONTROL_WAIT_SEC))
        await self._flash_send_result(action, "not_seen")

    async def _flash_send_result(self, action, error, tries=1):
        """{type: flash_control_result, action, ok, error} 广播给控制页（手机 DENY、浮窗不收）。只带代码，
        文案在前端；error 见 flash_sale.CONTROL_OWN_ERRORS。停止再试过的另带 tries（一共试了几次，2 起），页面据此
        补一句。广播出错不往外抛。"""
        msg = {"type": "flash_control_result", "action": action, "ok": error is None, "error": error}
        if tries > 1:
            msg["tries"] = tries
        try:
            await self.server.broadcast(msg)
        except Exception as exc:
            self._flash_log_once("result:" + type(exc).__name__,
                                 "[警告] 闪购助手：发控制结果出错（{}）".format(type(exc).__name__))
