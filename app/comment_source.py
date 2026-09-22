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
import inspect
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# event_to_item 在拿不到 msg_id 时用它编号——模块级、跨调用递增，
# 保证同一进程生命周期里两条弹幕不会撞 id。
_t_counter = 0


def _http_note(http_status):
    """「（HTTP 400）」这样的状态码说明；没有状态码返回空串。"""
    try:
        return "（HTTP {}）".format(int(http_status)) if http_status else ""
    except (TypeError, ValueError):
        return ""


def _installed_tiktoklive():
    """已安装的 TikTokLive 版本（只读包元数据，父进程不 import 这个库）。"""
    from .updater import tiktoklive_version
    return tiktoklive_version()


def _note_anonymous_request():
    """告诉 resolver「本进程刚向 TikTok 发过匿名请求」。弹幕这条链路不能因为它出任何问题。"""
    try:
        from .resolver import note_anonymous_request
        note_anonymous_request()
    except Exception:
        pass


def _accepts_raw(fn):
    """on_state 回调收不收第三个参数 raw（只进审计的原始报错）。老的两参回调照常能用。"""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 3 or any(p.name == "raw" for p in params)


# 更新检查的结果 -> 面板上说的话。只写发生了什么，不猜原因
_FRESHEN_NOTES = {
    "no-update": "弹幕组件已是可用的最新版本",
    "pip-failed": "弹幕组件更新检查没成功",
    "cooldown": "近几个小时已检查过弹幕组件更新",
    "recently-failed": "弹幕组件更新检查刚失败过",
}


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
        # 带上进程 id：兜底计数每次子进程重启都从 1 起，光靠 "t1…t300" 会在
        # 重连后被父进程的 id 去重当成重复弹幕整批丢掉
        _t_counter += 1
        cid = "t{}-{}".format(os.getpid(), _t_counter)
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
    # 被 TikTok 风控拦下时的退避。比签名错误还要长：那是「服务忙」，
    # 这是「你被当成机器人了」，越急着重连越坐实。
    BLOCKED_WAIT_SEC = 900.0
    # 评论服务拒绝握手（HTTP 400 等）之后多久重试。找过组件更新还是被拒，
    # 马上重连多半还是拒——别再每分钟烧一次签名额度
    REJECTED_WAIT_SEC = 180.0
    MAX_CONNECTS_PER_HOUR = 30
    STOP_GRACE_SEC = 3.0
    # 「一小时」本身也做成常量：额度限流测试要能把这个窗口也调短，
    # 否则触发限流后要真的等接近一小时才能看到窗口滑出、恢复连接。
    HOUR_WINDOW_SEC = 3600.0
    # 缺库/Python 版本不够时，多久重查一次 worker_available()
    PROVISION_POLL_SEC = 30.0
    # 评论 WebSocket 能「半开」：还在 connected、没有 DisconnectEvent、也没有任何
    # 弹幕事件——2026-09-21 实录：一场直播报了 connected 之后哑了 4 小时零弹幕，
    # HEALTHY_SEC 只在退避判定里用一次就完了，从没人再检查「连上之后有没有事」。
    # 看门狗每 SILENCE_CHECK_SEC 秒查一次，超过 SILENCE_RESTART_SEC 没收到任何
    # 弹幕就优雅重连。阈值定这么长：每次重连都要烧一次匿名请求 + 一次经 Euler
    # Stream 签名的握手（免费额度按天算），15 分钟是能接受的下限，不是随手挑的。
    SILENCE_RESTART_SEC = 15 * 60
    SILENCE_CHECK_SEC = 30
    # 时钟做成可替换的：每小时连接上限、健康判定都按它算。测试注入一个手动
    # 拨动的假时钟，窗口逻辑就不再依赖真实时间——2026-09-07 Windows CI 上
    # test_hourly_connect_cap 反复偶发失败，那台跑器的 time.time() 精度是
    # 15.6 毫秒，而测试把窗口压到了 50 毫秒。
    _clock = staticmethod(time.time)

    def __init__(self, on_items, on_state, cookies_browser="auto", root=ROOT):
        """on_items: async fn(items: list[dict])。
        on_state: async fn(state: str, detail: str)。
        cookies_browser: 借用浏览器登录态时试哪个/哪些浏览器，语义同
        resolver.py 的同名参数（"auto"/"none"/具体浏览器名）。
        """
        self._on_items = on_items
        self._on_state = on_state
        self._on_state_takes_raw = _accepts_raw(on_state)
        self._cookies_browser = cookies_browser
        self._root = root
        self.on_provision = None        # Pipeline 可选注入：updater.ensure_tiktoklive
        self.on_stale = None            # Pipeline 可选注入：updater.freshen_tiktoklive
        self.state = "idle"
        self.detail = ""
        self._unique_id = None
        self._task = None
        self._restart_task = None       # 换主播时在途的 _restart 任务（保引用）
        self._proc = None
        self._last_state = None         # 子进程最后一条 status 的 state，退出码不可信时的依据
        self._last_detail = ""          # 同一条 status 的 detail（原始报错），换成中文说明时用
        self._last_http = None          # 同一条 status 带的握手 HTTP 状态码（被拒时才有）
        self._last_handshake = None     # 同一条 status 带的服务端 Handshake-Msg 原话
        self._connect_times = []        # 最近一小时内的连接尝试时间戳，额度限流用
        # start()/stop() 每次调用都自增的世代号：_restart() 里 await self.stop()
        # 会让出事件循环，这段时间内如果有另一次 start()/stop() 插进来，_restart()
        # 结尾那次 self._launch() 就不该再执行——否则会为一个已经不该活着的
        # 会话起一个没人跟踪的子进程（见 stop() 期间的竞态记录）。
        self._epoch = 0
        self._connected_at = None       # 看门狗读的：这次连接建立的 _clock() 时间
        self._last_item_at = None       # 看门狗读的：最近一次收到弹幕批次的 _clock() 时间
        self._silent_restart = False    # 上一次子进程退出是不是看门狗触发的优雅重连
        self._last_silent_restart_at = None  # 上次静默重连的 _clock() 时间，同一窗口最多一次
        self.comments_received = 0      # 本场（start()~stop()）收到的弹幕条数，写进 session_end

    # ---- 对外接口 ----
    def start(self, unique_id):
        """幂等：同一 unique_id 已在跑就不动；换了主播先停旧的再起新的。"""
        self._epoch += 1
        epoch = self._epoch
        if self._unique_id == unique_id and self._task is not None and not self._task.done():
            return
        if self._task is not None and not self._task.done():
            # 保住引用：不保引用的任务可能被 GC 收走，换主播就静默失败
            self._restart_task = asyncio.ensure_future(self._restart(unique_id, epoch))
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
        self.comments_received = 0      # 新的一场，从零重新数（跨这一场内的重连都累加）
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
            self._connect_times.append(self._clock())
            self._last_state = None
            self._last_detail = ""
            self._last_http = None
            self._last_handshake = None
            # 记下这次子进程用的组件版本：被拒时若已经换了新版本（启动时的升级刚落地、
            # 上一次被取消的 pip 刚跑完），立刻重连，不去白查一次、更不白等几分钟
            spawned_version = _installed_tiktoklive()
            returncode, healthy = await self._run_once(unique_id, extra)
            if returncode not in (0, 3, 4, 5, 6, 7, 8):
                # 退出码不在约定表里（被信号打死、解释器收尾出错……）：子进程
                # 退出前写的最后一条 status 才是它真正想说的话——「主播不存在」
                # 就该停，而不是当成普通失败去烧签名额度重试
                returncode = {"not_found": 6, "login_required": 5, "offline": 3,
                              "blocked": 7, "rejected": 8,
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
            elif returncode in (7, 8):                    # 握手被拒（8：HTTP 400 等；7：回 200 不升级）
                if returncode == 8:
                    prefix = "评论服务拒绝了连接{}".format(_http_note(self._last_http))
                    wait = self.REJECTED_WAIT_SEC
                else:
                    prefix = "TikTok 暂时拒绝了评论连接"
                    wait = self.BLOCKED_WAIT_SEC
                # 服务端给的原始原因只进审计，不上面板
                raw = "{} http_status={}{}".format(
                    self._last_detail, self._last_http,
                    " handshake_msg={}".format(self._last_handshake) if self._last_handshake else "")
                now_version = _installed_tiktoklive()
                if now_version and spawned_version and now_version != spawned_version:
                    await self._set_state(
                        "connecting", "弹幕组件已更新到 {}，正在重新连接…".format(now_version), raw=raw)
                    backoff = self.BACKOFF_MIN_SEC
                    continue
                outcome = await self._try_freshen(prefix, raw)
                if outcome == "upgraded":
                    backoff = self.BACKOFF_MIN_SEC
                    continue
                note = _FRESHEN_NOTES.get(outcome)
                await self._set_state(
                    "error", "{}{}，{} 分钟后自动重试".format(
                        prefix, "；" + note if note else "", max(1, int(round(wait / 60)))),
                    raw=raw)
                await asyncio.sleep(wait)
            else:
                backoff = min(backoff * 2, self.BACKOFF_MAX_SEC)
                if self._last_state == "error" and self._last_detail:
                    # 原始报错是英文异常，中控看不懂；先说程序在做什么，原文留在括号里备查
                    await self._set_state(
                        "error", "评论连接出错，稍后自动重试（{}）".format(self._last_detail[:120]))
                await asyncio.sleep(backoff)

    async def _try_freshen(self, prefix, raw=""):
        """评论服务拒绝连接后问一次弹幕组件有没有更新（updater.freshen_tiktoklive）。
        返回 outcome 字符串；"upgraded" 表示已升级，调用方立刻重连。没注入、出错也不抛。"""
        stale = getattr(self, "on_stale", None)
        if stale is None:
            return "unavailable"

        async def announce():
            # 只有真的要跑 pip 时 updater 才会调这个：面板说「正在检查」时确实在检查
            await self._set_state("error", "{}，正在检查弹幕组件有没有更新…".format(prefix), raw=raw)

        try:
            result = stale("comment-rejected", announce)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            print("[警告] 检查弹幕组件更新失败: {}".format(exc))
            return "error"
        if not isinstance(result, dict):
            return "unavailable"
        outcome = result.get("outcome") or "unavailable"
        if outcome == "upgraded":
            await self._set_state(
                "connecting", "弹幕组件已从 {} 更新到 {}，正在重新连接…".format(
                    result.get("before"), result.get("after")), raw=raw)
        return outcome

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
        now = self._clock()
        self._connect_times = [t for t in self._connect_times
                               if now - t < self.HOUR_WINDOW_SEC]
        if len(self._connect_times) < self.MAX_CONNECTS_PER_HOUR:
            return
        await self._set_state("error", "连接尝试过多，暂停到下一小时")
        wait = max(0.0, self.HOUR_WINDOW_SEC - (now - min(self._connect_times)))
        await asyncio.sleep(wait)
        now = self._clock()
        self._connect_times = [t for t in self._connect_times
                               if now - t < self.HOUR_WINDOW_SEC]

    COOKIE_READ_TIMEOUT_SEC = 20.0   # 与 resolver.BROWSER_ATTEMPT_TIMEOUT 同量级

    async def _fetch_session_cookies(self):
        """读浏览器登录态要有上限：macOS 上 Chrome 的 cookie 库受钥匙串保护，
        授权对话框弹在别处没人点，线程会一直等下去，监督协程跟着卡死在这里、
        面板永远「未连接」。超时按「没拿到」处理，走需要登录的提示。"""
        loop = asyncio.get_running_loop()
        await self._set_state("connecting", "正在读取浏览器登录态…")
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, session_cookies, self._cookies_browser),
                timeout=self.COOKIE_READ_TIMEOUT_SEC)
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
        watchdog_task = asyncio.ensure_future(self._silence_watchdog(proc))
        connected_at = None
        self._connected_at = None
        self._last_item_at = None
        # 不带 sessionid 的子进程一启动就匿名请求 TikTok（TikTokLive 先抓
        # https://www.tiktok.com/@主播/live，再问是否在播），连上评论 WebSocket 之后才不再发。
        # 解析流地址那边「带登录抓直播页之前和上一次匿名请求隔开 8 秒」的规则要看得见这些
        # 请求（resolver.note_anonymous_request）：启动时记一次，连上或没连上就退出时再记一次。
        anonymous = "--session-id" not in extra_args
        if anonymous:
            _note_anonymous_request()
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
                    self._last_detail = obj.get("detail") or ""
                    self._last_http = obj.get("http_status")
                    self._last_handshake = obj.get("handshake_msg")
                    if state == "connected":
                        if anonymous and connected_at is None:
                            _note_anonymous_request()
                        connected_at = self._clock()
                        self._connected_at = connected_at
                    if state in ("rejected", "blocked"):
                        # 原始英文报错不直接上面板：子进程退出后 _supervise 换成
                        # 中文说明，并先去找组件更新
                        continue
                    await self._set_state(state, obj.get("detail") or "")
                elif kind == "comments":
                    items = obj.get("items")
                    if isinstance(items, list):
                        self._last_item_at = self._clock()
                        self.comments_received += len(items)
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
            if anonymous and connected_at is None:
                _note_anonymous_request()       # 没连上就结束了：它的匿名请求最晚发到这一刻
            stderr_task.cancel()
            watchdog_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._proc = None
        healthy = connected_at is not None and (self._clock() - connected_at) >= self.HEALTHY_SEC
        if self._silent_restart:
            # 看门狗刚优雅重连过这一次退出：无论连了多久，都当健康退出——
            # 立刻按 BACKOFF_MIN_SEC 重连，不进指数退避，也不算一次拒绝/出错
            # （returncode 本就是 0，这里只是不依赖 HEALTHY_SEC 的偶然满足）。
            healthy = True
            self._silent_restart = False
        return returncode, healthy

    async def _silence_watchdog(self, proc):
        """连着但哑了太久就优雅重连。评论 WebSocket 会「半开」：状态一直是
        connected，没有 DisconnectEvent，也没有任何弹幕事件，什么都不会主动
        告诉我们它已经死了（2026-09-21 实录：报了 connected 之后哑了 4 小时
        零弹幕，直到手动 SIGTERM 才在 30 秒内重连、第一秒就来了三条）。

        每 SILENCE_CHECK_SEC 秒查一次；只在当前状态是 connected 时才可能触发，
        且同一个 SILENCE_RESTART_SEC 窗口最多重连一次——重连要烧一次匿名请求
        加一次经 Euler Stream 签名的握手，免费额度按天算，看门狗自己不能把它
        烧光。异常只记日志，绝不冒泡到 _run_once（弹幕这条锦上添花的链路不能
        因为看门狗自己的 bug 被带死）。"""
        try:
            while True:
                await asyncio.sleep(self.SILENCE_CHECK_SEC)
                if self.state != "connected" or self._connected_at is None:
                    continue
                now = self._clock()
                silent_since = max(self._connected_at, self._last_item_at or 0)
                if now - silent_since < self.SILENCE_RESTART_SEC:
                    continue
                if self._last_silent_restart_at is not None and \
                        now - self._last_silent_restart_at < self.SILENCE_RESTART_SEC:
                    continue
                self._last_silent_restart_at = now
                self._silent_restart = True
                minutes = max(1, int(round((now - silent_since) / 60)))
                await self._set_state(
                    "silent_restart",
                    "连接着但 {} 分钟没有收到弹幕，已重连评论流".format(minutes))
                await self._terminate(proc)
                return   # 这次子进程的看门狗任务到此为止，_run_once 的 finally 会把它收掉
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[警告] 弹幕静默看门狗异常: {}".format(exc))

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
        while True:
            try:
                line = await stream.readline()
            except asyncio.CancelledError:
                raise
            except ValueError:
                # 单行超过 StreamReader 的 64KiB 上限（依赖库把整个响应体打进
                # stderr 就会这样）。readline 已经把缓冲清掉，接着读就行；
                # 这里要是退出循环，子进程再写满管道就会卡死在 write 上，
                # 父进程永远等在 stdout.readline——正是上面那段事故的样子。
                continue
            except Exception:
                return
            if not line:
                break
            try:
                text = line.decode(errors="replace").rstrip()
            except Exception:
                continue
            if text:
                print("[弹幕] {}".format(text))

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

    async def _set_state(self, state, detail="", raw=""):
        """raw：只写进审计的原始报错（服务端给的拒绝原因等），不广播到面板。"""
        self.state = state
        self.detail = detail
        try:
            if raw and getattr(self, "_on_state_takes_raw", False):
                await self._on_state(state, detail, raw)
            else:
                await self._on_state(state, detail)
        except Exception as exc:
            print("[警告] 弹幕来源状态回调失败: {}".format(exc))
