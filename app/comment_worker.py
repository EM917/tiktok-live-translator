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
import signal
import sys


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
    sys.exit(code)


if __name__ == "__main__":
    main()
