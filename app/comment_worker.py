"""弹幕抓取子进程——本项目唯一允许 `import TikTokLive` 的地方。

TikTokLive 7.x 要求 Python 3.10+，而主进程/测试承诺 3.9+，所以它只能是
可选依赖、隔在一个独立子进程里：装不上、连不上、被限流，都只影响这一个
子进程的退出码，绝不会拖垮字幕/检测/审计那条主链路（父进程见
app/comment_source.py 的 CommentSource._supervise）。

用法：
    python -m app.comment_worker <unique_id> [--session-id X] [--tt-target-idc Y]

登录态不走命令行：父进程起子进程后往 stdin 写一行 JSON（登录时
{"session_id": ..., "tt_target_idc": ...}，匿名时 {}），见 _read_handshake。命令行参数
在 `ps` 里谁都看得见，sessionid 等于登录凭据。--session-id / --tt-target-idc 只留给人手动
调试（终端里跑时 stdin 是终端，不读首行）。

协议：stdout 逐行 JSON、每行立刻 flush，父进程按行解析：
    {"event": "status", "state": ..., "detail": "...", "room_id": ...}
    {"event": "comments", "items": [{"id", "user", "text", 有值才带的账号字段}, ...]}
    {"event": "field_stats", "n": 50, "has": {"id": 50, ...},  每 50 条和断开时一行，增量
     "level_has": {...}, "level_hist": {...}, "level_kinds": {...}}   等级/徽章候选，见 app/comment_levels.py
stderr 只留 TikTokLive 库自己的日志，父进程会读走它（不读会堵住管道）。

退出码：0 正常断开；3 未开播；4 签名服务限流/报错；5 需要登录态；
6 找不到主播；7 被 TikTok 拦截；1 其它异常。每种情况退出前都先写一条
status 行，父进程据此决定退避多久、要不要带登录态重试、还是直接放弃
（见 comment_source.py）。
"""
import argparse
import asyncio
import json
import os
import signal
import sys
import threading

from .comment_levels import LevelStats
from .stdio import utf8_stdio

_PENDING = set()     # 信号处理里起的 disconnect 任务，保引用


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _status(state, detail="", room_id=None, http_status=None, handshake_msg=None):
    obj = {"event": "status", "state": state, "detail": detail}
    if room_id is not None:
        obj["room_id"] = room_id
    if http_status is not None:
        # 状态码单独给：7.0.1 起报错文本里不再有「HTTP 400」字样，父进程别去抠英文
        obj["http_status"] = http_status
    if handshake_msg:
        # 服务端写在 Handshake-Msg 头里的原话：7.0.0 的报错文本不带它，只能从头里取
        obj["handshake_msg"] = handshake_msg
    _emit(obj)


class FieldStats:
    """匿名连接下弹幕作者的各个字段到底有没有值（规格 §3.2）：只数「非空」，
    不记内容。每 FLUSH_EVERY 条和断开时 take() 一次，吐的是上次以来的增量，
    父进程负责累加（子进程每次重连都是新进程）。

    「非空」逐字段定义，不用 `is not None`：betterproto 没设的 int 是 0、
    字符串是 ''，子消息没设时也可能是个默认实例。"""

    FLUSH_EVERY = 50
    KEYS = ("id", "id_str", "sec_uid", "display_id", "identity", "identity_any", "mention",
            "follower", "mutual", "gifter", "subscriber", "fans_club")
    _IDENTITY_FLAGS = ("is_anchor", "is_moderator_of_anchor", "is_follower_of_anchor",
                       "is_mutual_following_with_anchor", "is_gift_giver_of_anchor",
                       "is_subscriber_of_anchor")

    def __init__(self):
        self.levels = LevelStats()      # 等级 / 徽章候选（只数不显示，见 app/comment_levels.py）
        self._reset()

    def _reset(self):
        self.n = 0
        self.has = dict.fromkeys(self.KEYS, 0)

    def observe(self, ev):
        from .comment_source import (_clean_str, _digits, _mentions_someone, _positive_int,
                                     fans_club_level)
        user = getattr(ev, "user", None)
        identity = getattr(ev, "user_identity", None)
        flags = [getattr(identity, f, None) is True for f in self._IDENTITY_FLAGS]
        hits = {
            "id": _positive_int(getattr(user, "id", None)),
            "id_str": _digits(getattr(user, "id_str", None)) is not None,
            "sec_uid": _clean_str(getattr(user, "sec_uid", None), 1) is not None,
            "display_id": _clean_str(getattr(user, "display_id", None), 1) is not None,
            # 主播本人、房管或管理员：这几种人以后不判定、不自动加入关注名单
            "identity": flags[0] or flags[1]
                        or getattr(getattr(user, "user_attr", None), "is_admin", None) is True,
            # 任一身份标记为真：全场都是 0 就说明这条连接根本没带 user_identity
            "identity_any": any(flags),
            "mention": _mentions_someone(ev),
            # 和主播的关系（弹幕栏要显示「粉丝」标签），以及粉丝团等级（先量一场再决定显不显示）
            "follower": getattr(identity, "is_follower_of_anchor", None) is True,
            "mutual": getattr(identity, "is_mutual_following_with_anchor", None) is True,
            "gifter": getattr(identity, "is_gift_giver_of_anchor", None) is True,
            "subscriber": getattr(identity, "is_subscriber_of_anchor", None) is True,
            "fans_club": fans_club_level(user) > 0,
        }
        self.n += 1
        for k, hit in hits.items():
            if hit:
                self.has[k] += 1
        self.levels.observe(ev)         # 自己兜异常：候选字段读坏了也不影响上面这些计数

    def take(self):
        """上次 take() 以来的增量行；一条都没数过返回 None。"""
        if not self.n:
            return None
        line = {"event": "field_stats", "n": self.n, "has": dict(self.has)}
        line.update(self.levels.take())
        self._reset()
        return line


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="python -m app.comment_worker")
    p.add_argument("unique_id")
    # 只给手动调试用：父进程从不传它们，登录态走 stdin 首行（见 _read_handshake）
    p.add_argument("--session-id", default=None)
    p.add_argument("--tt-target-idc", default=None)
    return p.parse_args(argv)


HANDSHAKE_TIMEOUT_SEC = 5.0
HANDSHAKE_MAX_BYTES = 4096      # 正常一行不到 200 字节；超长就不再读，按匿名处理
_PARENT_GONE = object()


def _read_handshake(fd=None, timeout=HANDSHAKE_TIMEOUT_SEC, read=os.read):
    """读父进程写进 stdin 的第一行（登录态），返回 dict；父进程已经没了返回 _PARENT_GONE。

    - 按字节读到第一个 \\n 为止，一个字节都不多读：后面的内容留给 _watch_parent，它靠 stdin
      的 EOF 判断父进程还在不在。所以必须在起看门狗之前调用（main 里就是这个顺序）。
    - timeout 秒还没读完整行就按匿名连（返回 {}）：父进程总会立刻写这一行，等不到说明哪里不对，
      但不能因此卡住不连。读线程是 daemon，留在后台不挡退出。
    - 整行没读完就到 EOF（父进程写到一半就退出、或者根本没写就退出）：没人会读我们的输出了，
      返回 _PARENT_GONE，调用方自己退出。
    - 不是 JSON 对象、超长：按匿名连。
    - stdin 是终端（人手动跑）或者根本没有 stdin：不读，返回 {}，用命令行参数。

    出错的几种情况往 stderr 写一行中文（父进程把 stderr 打进终端）；首行的内容一个字都不打出来：
    里面是登录凭据。"""
    if fd is None:
        try:
            fd = sys.stdin.fileno()
        except (AttributeError, OSError, ValueError):
            return {}
    try:
        if os.isatty(fd):
            return {}
    except OSError:
        return {}
    result = {}
    done = threading.Event()

    def _reader():
        buf = bytearray()
        try:
            while len(buf) <= HANDSHAKE_MAX_BYTES:
                byte = read(fd, 1)
                if not byte:
                    result["eof"] = True
                    break
                if byte == b"\n":
                    result["line"] = bytes(buf)
                    break
                buf += byte
        except Exception:
            result["eof"] = True              # stdin 读不了：和读到 EOF 一样，父进程那头已经断了
        done.set()

    threading.Thread(target=_reader, name="parent-handshake", daemon=True).start()
    if not done.wait(timeout):
        _note("{} 秒内没收到父进程的首行，按匿名连接".format(int(timeout)))  # i18n: terminal
        return {}
    if "line" not in result:
        if result.get("eof"):
            return _PARENT_GONE
        _note("父进程的首行太长，按匿名连接")  # i18n: terminal
        return {}
    try:
        obj = json.loads(result["line"].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        obj = None
    if not isinstance(obj, dict):
        _note("父进程的首行不是 JSON 对象，按匿名连接")  # i18n: terminal
        return {}
    return obj


def _note(text):
    """往 stderr 写一行给终端看的说明（父进程读 stderr、加「[弹幕]」前缀打出来）。"""
    try:
        print(text, file=sys.stderr, flush=True)
    except Exception:
        pass


def _apply_handshake(args, handshake):
    """首行里带了 sessionid 就用它（覆盖命令行上手动给的）；没带就保留命令行参数。"""
    session_id = handshake.get("session_id")
    if isinstance(session_id, str) and session_id:
        args.session_id = session_id
        idc = handshake.get("tt_target_idc")
        args.tt_target_idc = idc if isinstance(idc, str) and idc else None
    return args


def _classify_exc(exc):
    """异常 -> (status_state, exit_code)。分类依据见规格表：
    未开播/需要登录态/被拦截/签名服务/找不到主播/其它。

    **判断顺序有讲究，别按可读性重排**：TikTokLive 里
    AuthenticatedWebSocketConnectionError 和 SignatureRateLimitError 都是
    SignAPIError 的**子类**。父类那条如果排在前面，就会把「需要登录态」
    整条吞掉——2026-09-05 实测到的就是这个：TikTok 明说要登录，我们却退成
    4 号「签名服务繁忙」，白等 600 秒，而 comment_source 里那条「取浏览器
    登录态重试」的分支永远走不到。
    """
    from TikTokLive.client import errors as tt_errors
    from TikTokLive.client.errors import (
        AgeRestrictedError,
        AuthenticatedWebSocketConnectionError,
        SignAPIError,
        SignatureRateLimitError,
        UserNotFoundError,
        UserOfflineError,
        WebsocketURLMissingError,
    )
    # 7.x 中途才加进来的异常：老版本里取不到就退化成「永不匹配」，
    # 而不是让整个 _classify_exc 在 ImportError 上炸掉。
    blocked_cls = getattr(tt_errors, "WebcastBlocked200Error", ())

    if isinstance(exc, UserOfflineError):
        return "offline", 3
    # 子类必须排在 SignAPIError 前面，理由见上面的 docstring。
    # AgeRestrictedError 在这条链路上其实够不到（_run 调的是无参 connect()，
    # fetch_room_info 默认 False），留着是为了防止以后改用带 room_info 的连法
    # 时忘了分类——别因为「看着是死代码」就删。
    if isinstance(exc, (AuthenticatedWebSocketConnectionError,
                        WebsocketURLMissingError, AgeRestrictedError)):
        return "login_required", 5
    # 握手被拒、状态码又不是 200：评论服务明确拒绝了这次连接。单独归一类，
    # 父进程据此先去找组件更新、再拉长重试间隔，而不是当普通错误每分钟重连。
    # 必须排在 blocked 前面：TikTokLive 7.0.1 起把 400 也包成 WebcastBlockedError。
    # 2026-09-14 实录：7.0.0 遇到签名服务的备用线路，每次都是 HTTP 400。
    status = _handshake_status(exc)
    if status is not None and status != 200:
        return "rejected", 8
    # TikTok 明确回「你被判定为机器人」。这时候最不该做的就是立刻重连：
    # 原先它没进分类表，落到 1 号走 2 秒退避，等于顶着人家的风控反复敲门。
    if blocked_cls and isinstance(exc, blocked_cls):
        return "blocked", 7
    if isinstance(exc, (SignatureRateLimitError, SignAPIError)):
        return "error", 4
    if isinstance(exc, UserNotFoundError):
        return "not_found", 6
    return "error", 1


def _handshake_info(exc):
    """WebSocket 握手被拒时的 (HTTP 状态码, 服务端给的 Handshake-Msg)；不是握手被拒返回 (None, None)。

    TikTokLive 7.0.0 直接抛 websockets 的 InvalidStatusCode（带 .status_code、.headers）；
    7.0.1 起包成 WebcastBlockedError 再抛，原异常挂在 __cause__ 上；新版 websockets
    的 InvalidStatus 把状态码和头放在 .response 上。按类名认、沿异常链找，
    不 import websockets——不同版本里这些类的位置不一样。"""
    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ in ("InvalidStatusCode", "InvalidStatus"):
            response = getattr(cur, "response", None)
            code = getattr(cur, "status_code", None)
            if code is None:
                code = getattr(response, "status_code", None)
            headers = getattr(cur, "headers", None)
            if headers is None:
                headers = getattr(response, "headers", None)
            msg = None
            try:
                msg = headers.get("Handshake-Msg") if headers is not None else None
            except Exception:
                msg = None
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = None
            return code, (str(msg)[:200] if msg else None)
        cur = cur.__cause__ or cur.__context__
    return None, None


def _handshake_status(exc):
    """握手被拒时的 HTTP 状态码；不是握手被拒返回 None。"""
    return _handshake_info(exc)[0]


def _handshake_fields(exc):
    """状态行里要带的握手字段（只放有值的）。"""
    code, msg = _handshake_info(exc)
    fields = {}
    if code is not None:
        fields["http_status"] = code
    if msg:
        fields["handshake_msg"] = msg
    return fields


def _detail_of(exc):
    return "{}: {}".format(type(exc).__name__, str(exc)[:200])


async def _watch_parent(on_gone, read=None, getppid=os.getppid, grace=5.0, exit_fn=os._exit):
    """父进程没了就自己退出——否则父进程被强杀（kill -9、崩溃、Windows 上被
    任务管理器结束）时，这个子进程会带着 WebSocket 一直挂着：实录一次测试
    结束后 `comment_worker itskainb` 独自活了好几分钟。

    两个信号取先到的：① stdin 到 EOF——父进程用管道接着我们的 stdin，写完
    首行（登录态，main 已经用 _read_handshake 读走）就不再写入，它一死操作
    系统就关管道，各平台都可靠；② ppid 变了（Unix 上孤儿会被过继给 1 号
    进程）——stdin 不可读的环境靠它兜底。
    on_gone 负责优雅断开（起 disconnect 任务）；grace 秒后不管断没断都硬退，
    别让一个「优雅」的收尾变成又一种挂着不走。
    """
    loop = asyncio.get_running_loop()
    if read is None:
        def read():
            # 用原始 fd 而不是 sys.stdin.buffer：解释器退出时若有 daemon 线程还
            # 握着 BufferedReader 的锁，CPython 会直接 Fatal error 收尾（实测退出
            # 码从 6 变成 -6），父进程就读不到真正的退出码了。os.read 不持锁。
            fd = sys.stdin.fileno()
            while os.read(fd, 4096):
                pass
            return b""

    async def stdin_eof():
        # 用 daemon 线程而不是 run_in_executor：executor 线程会让 asyncio.run()
        # 在退出时等它——而父进程活着时这个 read() 永远不返回，子进程正常
        # 断开后就会卡在收尾、永远退不出去（父进程也就永远等不到退出码）。
        # daemon 线程不挡解释器退出。
        fut = loop.create_future()

        def _reader():
            try:
                read()
            except Exception:
                return                         # stdin 用不了：把机会留给 ppid 那路
            try:
                loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(None))
            except RuntimeError:
                pass                           # 事件循环已经关了：进程本来就在退出

        threading.Thread(target=_reader, name="parent-stdin-watch", daemon=True).start()
        await fut

    async def ppid_changed():
        parent = getppid()
        while getppid() == parent:
            await asyncio.sleep(2)

    waiters = [asyncio.ensure_future(stdin_eof()), asyncio.ensure_future(ppid_changed())]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
    try:
        on_gone()
    except Exception:
        pass
    await asyncio.sleep(grace)
    exit_fn(0)


async def _run(args):
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import CommentEvent, ConnectEvent, DisconnectEvent

    from .comment_source import event_to_item

    stats = FieldStats()

    def _flush_stats():
        line = stats.take()
        if line is not None:
            _emit(line)

    unique_id = args.unique_id if str(args.unique_id).startswith("@") \
        else "@" + str(args.unique_id)
    client = TikTokLiveClient(unique_id=unique_id)
    if args.session_id:
        client.web.set_session(args.session_id, args.tt_target_idc)

    @client.on(ConnectEvent)
    async def _on_connect(_ev):
        _status("connected", room_id=getattr(client, "room_id", None))

    @client.on(DisconnectEvent)
    async def _on_disconnect(_ev):
        _flush_stats()
        _status("disconnected")

    @client.on(CommentEvent)
    async def _on_comment(ev):
        item = event_to_item(ev)
        if item is not None:
            _emit({"event": "comments", "items": [item]})
        try:                    # 计数只是附带的，出错也不能挡弹幕
            stats.observe(ev)
            if stats.n >= stats.FLUSH_EVERY:
                _flush_stats()
        except Exception:
            pass

    def _handle_signal():
        # 尽力优雅断开；disconnect() 本身可能耗时，起个任务不阻塞信号处理器。
        # 就算断开失败/超时，进程也会在 finally 里正常退出。
        _PENDING.add(asyncio.ensure_future(client.disconnect()))   # 保引用，别被 GC 收走

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, RuntimeError):
            pass   # 不支持注册信号处理器的平台：尽力而为，不是硬需求

    # 父进程死了就跟着退（见 _watch_parent）；正常路径里父进程 terminate 我们
    # 时走的是上面的信号处理器，这条只兜「父进程来不及 terminate」的情况
    watchdog = asyncio.ensure_future(_watch_parent(_handle_signal))
    watchdog.add_done_callback(lambda _t: None)

    _status("connecting")
    try:
        is_live = await client.is_live()
    except Exception as exc:
        state, code = _classify_exc(exc)
        _status(state, _detail_of(exc), **_handshake_fields(exc))
        return code
    if not is_live:
        _status("offline")
        return 3

    try:
        await client.connect()
    except Exception as exc:
        _flush_stats()
        state, code = _classify_exc(exc)
        _status(state, _detail_of(exc), **_handshake_fields(exc))
        return code

    _flush_stats()
    # connect() 正常返回（对方停播/主动断开）：无论 DisconnectEvent 是否已经
    # 报过一次，这里都兜底再报一次，确保父进程一定能看到「已断开」。
    _status("disconnected")
    return 0


def _utf8_stdio():
    """父进程按 UTF-8 解析每一行。Windows 上管道的默认编码是 ANSI 代码页
    （cp936/cp1252）：带 emoji 的昵称直接 UnicodeEncodeError 丢掉这条，
    "niño" 编成 \xf1 让父进程 json.loads 失败——西语弹幕几乎条条带重音，
    等于弹幕面板在 Windows 上只剩纯 ASCII 的少数评论。

    注意这里和 main.py 的 harden_stdio() 是两种不同的处理，不能互换：主进程的输出
    是给人看的终端日志，要保留平台代码页；这里的 stdout 是给父进程 json.loads 的
    字节流，必须 UTF-8（见 app/stdio.py）。"""
    utf8_stdio()


def main(argv=None):
    _utf8_stdio()
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    # 先读 stdin 首行（登录态），再起任何东西：看门狗（_watch_parent）也读 stdin，起早了会把
    # 这一行当成普通输入读掉。最多等 HANDSHAKE_TIMEOUT_SEC 秒
    handshake = _read_handshake()
    if handshake is _PARENT_GONE:
        # 父进程在写完首行之前就没了：没有人会读我们的输出，也不该再去连 TikTok
        os._exit(0)
    _apply_handshake(args, handshake)
    try:
        code = asyncio.run(_run(args))
    except Exception as exc:
        _status("error", _detail_of(exc))
        code = 1
    # os._exit 而不是 sys.exit：跳过解释器收尾。看门狗的 daemon 线程还阻塞在
    # stdin 上，正常收尾会去等/锁它，退出码就不再是我们决定的那个。stdout
    # 每行都已 flush（_emit），这里再刷一次只是保险。
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(code)


if __name__ == "__main__":
    main()
