"""弹幕抓取子进程——本项目唯一允许 `import TikTokLive` 的地方。

TikTokLive 7.x 要求 Python 3.10+，而主进程/测试承诺 3.9+，所以它只能是
可选依赖、隔在一个独立子进程里：装不上、连不上、被限流，都只影响这一个
子进程的退出码，绝不会拖垮字幕/检测/审计那条主链路（父进程见
app/comment_source.py 的 CommentSource._supervise）。

用法：
    python -m app.comment_worker <unique_id> [--session-id X] [--tt-target-idc Y]

协议：stdout 逐行 JSON、每行立刻 flush，父进程按行解析：
    {"event": "status", "state": ..., "detail": "...", "room_id": ...}
    {"event": "comments", "items": [{"id", "user", "text"}, ...]}
stderr 只留 TikTokLive 库自己的日志，父进程会读走它（不读会堵住管道）。

退出码：0 正常断开；3 未开播；4 签名服务限流/报错；5 需要登录态；
6 找不到主播；1 其它异常。每种情况退出前都先写一条 status 行，父进程
据此决定退避多久、要不要带登录态重试、还是直接放弃（见 comment_source.py）。
"""
import argparse
import asyncio
import json
import os
import signal
import sys
import threading


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _status(state, detail="", room_id=None):
    obj = {"event": "status", "state": state, "detail": detail}
    if room_id is not None:
        obj["room_id"] = room_id
    _emit(obj)


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="python -m app.comment_worker")
    p.add_argument("unique_id")
    p.add_argument("--session-id", default=None)
    p.add_argument("--tt-target-idc", default=None)
    return p.parse_args(argv)


def _classify_exc(exc):
    """异常 -> (status_state, exit_code)。分类依据见规格表：
    未开播/签名服务/需要登录态/找不到主播/其它。"""
    from TikTokLive.client.errors import (
        AgeRestrictedError,
        AuthenticatedWebSocketConnectionError,
        SignAPIError,
        SignatureRateLimitError,
        UserNotFoundError,
        UserOfflineError,
        WebsocketURLMissingError,
    )

    if isinstance(exc, UserOfflineError):
        return "offline", 3
    if isinstance(exc, (SignatureRateLimitError, SignAPIError)):
        return "error", 4
    if isinstance(exc, (AuthenticatedWebSocketConnectionError,
                        WebsocketURLMissingError, AgeRestrictedError)):
        return "login_required", 5
    if isinstance(exc, UserNotFoundError):
        return "not_found", 6
    return "error", 1


def _detail_of(exc):
    return "{}: {}".format(type(exc).__name__, str(exc)[:200])


async def _watch_parent(on_gone, read=None, getppid=os.getppid, grace=5.0, exit_fn=os._exit):
    """父进程没了就自己退出——否则父进程被强杀（kill -9、崩溃、Windows 上被
    任务管理器结束）时，这个子进程会带着 WebSocket 一直挂着：实录一次测试
    结束后 `comment_worker itskainb` 独自活了好几分钟。

    两个信号取先到的：① stdin 到 EOF——父进程用管道接着我们的 stdin、从不
    写入，它一死操作系统就关管道，各平台都可靠；② ppid 变了（Unix 上孤儿
    会被过继给 1 号进程）——stdin 不可读的环境靠它兜底。
    on_gone 负责优雅断开（起 disconnect 任务）；grace 秒后不管断没断都硬退，
    别让一个「优雅」的收尾变成又一种挂着不走。
    """
    loop = asyncio.get_event_loop()
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
        _status("disconnected")

    @client.on(CommentEvent)
    async def _on_comment(ev):
        item = event_to_item(ev)
        if item is not None:
            _emit({"event": "comments", "items": [item]})

    def _handle_signal():
        # 尽力优雅断开；disconnect() 本身可能耗时，起个任务不阻塞信号处理器。
        # 就算断开失败/超时，进程也会在 finally 里正常退出。
        asyncio.ensure_future(client.disconnect())

    loop = asyncio.get_event_loop()
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
        _status(state, _detail_of(exc))
        return code
    if not is_live:
        _status("offline")
        return 3

    try:
        await client.connect()
    except Exception as exc:
        state, code = _classify_exc(exc)
        _status(state, _detail_of(exc))
        return code

    # connect() 正常返回（对方停播/主动断开）：无论 DisconnectEvent 是否已经
    # 报过一次，这里都兜底再报一次，确保父进程一定能看到「已断开」。
    _status("disconnected")
    return 0


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
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
