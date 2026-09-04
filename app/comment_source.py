"""弹幕后端抓取——父进程侧（不 import TikTokLive，见 app/comment_worker.py）。

这一条链路彻底独立于字幕/检测/审计：子进程连不上、断线、被限流，全部
只影响弹幕面板本身，绝不能让主链路等它、更不能让它的异常冒泡出去。
子进程是唯一 import TikTokLive 的地方（该库要求 Python 3.10+，本项目
承诺 3.9+，所以只能是可选依赖，隔在子进程里）；这里只负责起停它、
读它吐出来的 JSON 行、按退出码决定退避/重试/放弃。

额度保护：TikTokLive 的 WebSocket 签名经第三方 Euler Stream 服务，免费
额度 2500 次/天，每次 connect() 都要签一次——所以退避、限流、停播即停
都不是可有可无的细节，是这条免费通道能不能一直用下去的前提。
"""
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# event_to_item 在拿不到 msg_id 时用它编号——模块级、跨调用递增，
# 保证同一进程生命周期里两条弹幕不会撞 id。
_t_counter = 0


def worker_available():
    """子进程是否具备运行条件：Python >= 3.10 且已装 TikTokLive。"""
    if sys.version_info < (3, 10):
        return False
    try:
        return importlib.util.find_spec("TikTokLive") is not None
    except Exception:
        # find_spec 在极少数损坏的安装/路径下会抛异常而不是返回 None，
        # 弹幕这种锦上添花的功能不该因为这个把主进程带崩
        return False


def event_to_item(ev):
    """把 TikTokLive 的 CommentEvent（鸭子类型，纯属性访问）转成
    `{"id", "user", "text"}`；没有可显示文本时返回 None。

    刻意用 getattr 而不是直接属性访问：这样传一个 SimpleNamespace 伪造的
    假事件进来也能测，不用真的装 TikTokLive。
    """
    global _t_counter
    text = getattr(ev, "comment", None)
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    # 防御性截断：这一行会被 json.dumps 后整行写进子进程 stdout，父进程用
    # asyncio.StreamReader.readline() 按行读取（默认 64KiB 上限），一条异常
    # 长的弹幕（网络数据，长度不受本地控制）能把一行撑爆导致 readline()
    # 抛异常。CommentTranslator.MAX_TEXT=300 已经说明这个量级对翻译够用，
    # 这里留更宽松的上限只是兜底，不影响正常弹幕。
    text = text[:1000]
    # 显示名优先：中控看的是「Toñita 🇭🇳」这种昵称，不是 user7381 这种账号 id
    user_obj = getattr(ev, "user", None)
    user = (getattr(user_obj, "nickname", None)
            or getattr(user_obj, "unique_id", None) or "")
    common = getattr(ev, "common", None)
    msg_id = getattr(common, "msg_id", None)
    if msg_id:                      # 有且非 0/空——0 和缺失都算「没有」
        cid = str(msg_id)
    else:
        _t_counter += 1
        cid = "t{}".format(_t_counter)
    return {"id": cid, "user": str(user), "text": text}


def session_cookies(cookies_browser):
    """借用浏览器登录态取 (sessionid, tt-target-idc)；任何异常/无结果返回 (None, None)。

    阻塞 IO（起子进程读浏览器 cookie 数据库）——调用方必须放到
    run_in_executor 里跑，不能在事件循环里直接 await 这个同步函数。
    """
    if cookies_browser == "none":
        return (None, None)
    try:
        from .resolver import _browser_order

        import yt_dlp.cookies as ytd_cookies

        for browser in _browser_order(cookies_browser):
            try:
                jar = ytd_cookies.extract_cookies_from_browser(browser)
            except Exception:
                continue
            sessionid = None
            tt_target_idc = None
            for cookie in jar:
                domain = getattr(cookie, "domain", "") or ""
                if "tiktok.com" not in domain:
                    continue
                if cookie.name == "sessionid":
                    sessionid = cookie.value
                elif cookie.name == "tt-target-idc":
                    tt_target_idc = cookie.value
            if sessionid:
                return (sessionid, tt_target_idc)
    except Exception:
        pass
    return (None, None)


class CommentSource:
    """管理 `python -m app.comment_worker` 子进程：起停、读输出、按退出码
    退避重连，把结果转发给 Pipeline（on_items / on_state）。

    所有数字常量都放成类属性，测试把它们调到 0.01~0.05 秒就能在几十毫秒
    内跑完整套退避/限流场景，不用真的等分钟级的时间。
    """

    BACKOFF_MIN_SEC = 2.0
    BACKOFF_MAX_SEC = 60.0
    HEALTHY_SEC = 60.0
    OFFLINE_RETRY_SEC = 30.0
    SIGN_ERROR_WAIT_SEC = 600.0
    MAX_CONNECTS_PER_HOUR = 30
    STOP_GRACE_SEC = 3.0
    # 「一小时」本身也做成常量：额度限流测试要能把这个窗口也调短，
    # 否则触发限流后要真的等接近一小时才能看到窗口滑出、恢复连接。
    HOUR_WINDOW_SEC = 3600.0
    # 缺库/Python 版本不够时，多久重查一次 worker_available()
    PROVISION_POLL_SEC = 30.0

    def __init__(self, on_items, on_state, cookies_browser="auto", root=ROOT):
        """on_items: async fn(items: list[dict])。
        on_state: async fn(state: str, detail: str)。
        cookies_browser: 借用浏览器登录态时试哪个/哪些浏览器，语义同
        resolver.py 的同名参数（"auto"/"none"/具体浏览器名）。
        """
        self._on_items = on_items
        self._on_state = on_state
        self._cookies_browser = cookies_browser
        self._root = root
        self.on_provision = None        # Pipeline 可选注入：updater.ensure_tiktoklive
        self.state = "idle"
        self.detail = ""
        self._unique_id = None
        self._task = None
        self._proc = None
        self._last_state = None         # 子进程最后一条 status 的 state，退出码不可信时的依据
        self._connect_times = []        # 最近一小时内的连接尝试时间戳，额度限流用
        # start()/stop() 每次调用都自增的世代号：_restart() 里 await self.stop()
        # 会让出事件循环，这段时间内如果有另一次 start()/stop() 插进来，_restart()
        # 结尾那次 self._launch() 就不该再执行——否则会为一个已经不该活着的
        # 会话起一个没人跟踪的子进程（见 stop() 期间的竞态记录）。
        self._epoch = 0

    # ---- 对外接口 ----
    def start(self, unique_id):
        """幂等：同一 unique_id 已在跑就不动；换了主播先停旧的再起新的。"""
        self._epoch += 1
        epoch = self._epoch
        if self._unique_id == unique_id and self._task is not None and not self._task.done():
            return
        if self._task is not None and not self._task.done():
            asyncio.ensure_future(self._restart(unique_id, epoch))
            return
        self._launch(unique_id)

    async def _restart(self, unique_id, epoch):
        # _external=False：这是本次 start() 请求自己发起的停旧步骤，不能
        # 让它自己把 epoch 再往前推一格，否则下面的校验永远判自己过期。
        await self.stop(_external=False)
        if epoch != self._epoch:
            # stop() 让出事件循环的这段时间里，又有别的 start()/stop()（外部
            # 调用）插进来过（比如新会话没等这次重启完成就自己结束了）——
            # 这次重启已经过期，不该再为一个不再相关的目标起子进程。
            return
        self._launch(unique_id)

    def _launch(self, unique_id):
        self._unique_id = unique_id
        self._task = asyncio.ensure_future(self._supervise(unique_id))

    async def stop(self, _external=True):
        """取消监督协程（连带其正在跑的子进程），发 state idle。

        `_external` 仅供 `_restart()` 内部调用时传 False：那次 stop 是
        本次 start() 请求自己的一部分，不算「又来了一次新的外部意图」。
        """
        if _external:
            self._epoch += 1
        task = self._task
        self._task = None
        self._unique_id = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print("[警告] 弹幕监督任务异常退出: {}".format(exc))
        # 等旧子进程收尾的这几秒里，下一场可能已经 start() 了：那就别再把
        # 界面刷回「未连接」——新会话的 connecting/connected 才是当前事实
        if self._task is None:
            await self._set_state("idle", "")

    # ---- 监督协程：起子进程 -> 读输出 -> 按退出码决定下一步 ----
    async def _supervise(self, unique_id):
        backoff = self.BACKOFF_MIN_SEC
        tried_login = False
        session_id = None
        tt_target_idc = None
        while True:
            if not worker_available():
                await self._await_provisioned()
                continue
            await self._enforce_hourly_limit()
            extra = []
            if session_id:
                extra += ["--session-id", session_id]
            if tt_target_idc:
                extra += ["--tt-target-idc", tt_target_idc]
            self._connect_times.append(time.time())
            self._last_state = None
            returncode, healthy = await self._run_once(unique_id, extra)
            if returncode not in (0, 3, 4, 5, 6):
                # 退出码不在约定表里（被信号打死、解释器收尾出错……）：子进程
                # 退出前写的最后一条 status 才是它真正想说的话——「主播不存在」
                # 就该停，而不是当成普通失败去烧签名额度重试
                returncode = {"not_found": 6, "login_required": 5, "offline": 3,
                              "disconnected": 0}.get(self._last_state, returncode)
            if returncode == 0:
                backoff = self.BACKOFF_MIN_SEC if healthy \
                    else min(backoff * 2, self.BACKOFF_MAX_SEC)
                await asyncio.sleep(backoff)
            elif returncode == 3:                       # UserOfflineError
                await self._set_state("offline", "主播未开播")
                await asyncio.sleep(self.OFFLINE_RETRY_SEC)
            elif returncode == 4:                        # 签名服务限流/报错
                await self._set_state("error", "评论签名服务繁忙，稍后重试")
                await asyncio.sleep(self.SIGN_ERROR_WAIT_SEC)
            elif returncode == 5:                        # 需要登录态
                if not tried_login:
                    tried_login = True
                    sid, idc = await self._fetch_session_cookies()
                    if sid:
                        session_id, tt_target_idc = sid, idc
                        continue                          # 立即带登录态重起，不退避
                await self._set_state(
                    "unavailable",
                    "TikTok 要求登录才能读取评论，浏览器里登录 TikTok 后重新开始")
                return
            elif returncode == 6:                         # UserNotFoundError
                await self._set_state("unavailable", "找不到该主播")
                return
            else:
                backoff = min(backoff * 2, self.BACKOFF_MAX_SEC)
                await asyncio.sleep(backoff)

    async def _await_provisioned(self):
        """Python 版本不够，或 TikTokLive 还没装：报告状态，必要时触发安装，
        每 PROVISION_POLL_SEC 秒重查一次，装好/满足条件了才回到调用方继续。
        """
        if sys.version_info < (3, 10):
            detail = "弹幕需要 Python 3.10+（当前 {}.{}）".format(
                sys.version_info[0], sys.version_info[1])
            await self._set_state("unavailable", detail)
        else:
            await self._set_state("unavailable", "正在安装弹幕组件 TikTokLive…")
            provision = getattr(self, "on_provision", None)
            if provision is not None:
                try:
                    result = provision()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    print("[警告] 触发弹幕组件安装失败: {}".format(exc))
            if not worker_available():
                # 装完了会立刻走出下面的循环；没装上（离线、pip 失败、一小时内
                # 已试过）就别让界面一直停在「正在安装」——那是在撒谎
                await self._set_state(
                    "unavailable",
                    "弹幕组件 TikTokLive 未装上，稍后自动重试；"
                    "也可手动执行 pip install TikTokLive 后重新开始")
        while not worker_available():
            await asyncio.sleep(self.PROVISION_POLL_SEC)

    async def _enforce_hourly_limit(self):
        """每小时最多 MAX_CONNECTS_PER_HOUR 次连接尝试——保护 Euler Stream
        的免费签名额度。超过就等最早那次尝试滑出窗口。"""
        now = time.time()
        self._connect_times = [t for t in self._connect_times
                               if now - t < self.HOUR_WINDOW_SEC]
        if len(self._connect_times) < self.MAX_CONNECTS_PER_HOUR:
            return
        await self._set_state("error", "连接尝试过多，暂停到下一小时")
        wait = max(0.0, self.HOUR_WINDOW_SEC - (now - min(self._connect_times)))
        await asyncio.sleep(wait)
        now = time.time()
        self._connect_times = [t for t in self._connect_times
                               if now - t < self.HOUR_WINDOW_SEC]

    async def _fetch_session_cookies(self):
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, session_cookies, self._cookies_browser)
        except Exception:
            return (None, None)

    # ---- 单次子进程生命周期：起 -> 读 stdout/stderr -> 等退出 ----
    async def _run_once(self, unique_id, extra_args):
        # _spawn 本身也可能抛异常（fd 耗尽等）——放在 try 外面的话异常会直接
        # 冒到 _supervise 的 while True 循环，把整条监督协程带死（该协程是
        # fire-and-forget 起的，没人会看到这个异常，弹幕来源从此静默失联）。
        try:
            proc = await self._spawn([unique_id] + list(extra_args))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[警告] 弹幕子进程启动失败: {}".format(exc))
            return (-1, False)
        self._proc = proc
        stderr_task = asyncio.ensure_future(self._drain_stderr(proc))
        connected_at = None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                obj = self._parse_line(line)
                if obj is None:
                    continue
                kind = obj.get("event")
                if kind == "status":
                    state = obj.get("state") or "error"
                    self._last_state = state
                    if state == "connected":
                        connected_at = time.time()
                    await self._set_state(state, obj.get("detail") or "")
                elif kind == "comments":
                    items = obj.get("items")
                    if isinstance(items, list):
                        await self._safe_on_items(items)
            returncode = await proc.wait()
        except asyncio.CancelledError:
            await self._terminate(proc)
            raise
        except Exception as exc:
            # 例如某一行 JSON 超过 StreamReader 的行缓冲上限（弹幕文本异常长）
            # 时 readline() 会抛 ValueError——同样不能让它冒泡杀死监督协程，
            # 且子进程必须先 terminate，否则会变成没人再读 stdout、写满管道
            # 缓冲区后卡死的孤儿进程（stop() 也找不回它，见事故记录）。
            print("[警告] 弹幕子进程读取异常: {}".format(exc))
            await self._terminate(proc)
            return (-1, False)
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._proc = None
        healthy = connected_at is not None and (time.time() - connected_at) >= self.HEALTHY_SEC
        return returncode, healthy

    async def _spawn(self, args):
        """单独成方法，方便测试 monkeypatch 掉、返回一个假子进程。"""
        # stdin 也接管道，但从不写：父进程一死操作系统就关掉它，子进程读到
        # EOF 就自行退出（见 comment_worker._watch_parent）。没有这根管道，
        # 父进程被强杀时子进程会带着 WebSocket 一直挂着。
        return await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.comment_worker", *args,
            cwd=str(self._root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _terminate(self, proc):
        """stop() 取消监督协程时用：尽力优雅退出，超时就强杀，绝不残留子进程。"""
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.STOP_GRACE_SEC)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        except Exception:
            pass

    async def _drain_stderr(self, proc):
        """必须把 stderr 读干净——不读的话子进程写满管道缓冲区就会卡死，
        表现为「弹幕来源一直卡在 connecting」，一点也不像 stderr 的锅。"""
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                try:
                    text = line.decode(errors="replace").rstrip()
                except Exception:
                    continue
                if text:
                    print("[弹幕] {}".format(text))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    @staticmethod
    def _parse_line(line):
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            return None
        return obj if isinstance(obj, dict) else None

    async def _safe_on_items(self, items):
        try:
            await self._on_items(items)
        except Exception as exc:
            print("[警告] 弹幕转发失败: {}".format(exc))

    async def _set_state(self, state, detail=""):
        self.state = state
        self.detail = detail
        try:
            await self._on_state(state, detail)
        except Exception as exc:
            print("[警告] 弹幕来源状态回调失败: {}".format(exc))
