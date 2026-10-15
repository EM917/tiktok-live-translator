"""重连的页面在回放期间，别处发生的广播不能漏。

hello 之后的回放是一条一条 await 着发的，而页面要回放完才加入 self.clients。回放的这几毫秒里，
别处的广播（新字幕、新弹幕、新违禁词警报）进了服务端缓存，却没有任何路径能到这个页面——
快照早就拍完了，广播又够不着它。对合规监听器，漏的那一条可能就是警报。

修法：快照和「开始替这个页面攒广播」在同一个同步步骤里（和 app/viewer.py 的 ViewerHub.register 同理），
回放完把攒的补发，补的时候又有新的就再补一轮，直到检查那一刻是空的，然后在没有 await 的一步里入册。

这里的办法是把服务端 WebSocketResponse.send_json 换成「发完一条就让出，并在第 n 条之后
从里面广播」——和真实情况一样，广播发生在回放的两次 await 之间。
"""
import asyncio

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from app.server import CaptionServer


def _caption(i):
    return {"type": "caption", "id": i, "ts": 1000.0 + i, "original": "uno %d" % i}


def _comment(i):
    return {"type": "comment", "id": "c%d" % i, "user": "ana", "text": "hola %d" % i,
            "ts": 1000.0 + i, "state": "same"}


def _alert(i):
    return {"type": "alert", "alert_id": i, "term": "cura", "tier": "exact", "ts": 1000.0 + i}


# 缓存里先放 3 条警报、5 条字幕、3 条弹幕：hello 是第 1 条，警报 2-4，字幕 5-9，弹幕 10-12
ALERTS, CAPTIONS, COMMENTS = 3, 5, 3
REPLAY_SENDS = 1 + ALERTS + CAPTIONS + COMMENTS

LATE = [
    {"type": "alert", "alert_id": 900, "term": "LATE", "tier": "exact", "ts": 2000.0},
    _comment(901),
    _caption(902),
    {"type": "status", "state": "idle", "detail": "LATE", "ts": 2000.0},
]


def _server():
    server = CaptionServer(port=0)

    async def fill():
        for i in range(ALERTS):
            await server.broadcast(_alert(i))
        for i in range(CAPTIONS):
            await server.broadcast(_caption(i))
        for i in range(COMMENTS):
            await server.broadcast(_comment(i))

    asyncio.run(fill())
    return server


def _plan_sends(monkeypatch, server, plan):
    """服务端每发完一条（让出一次事件循环）就数一下；数到 plan 里的 n，就从这里广播 plan[n]。

    plan[n] 里的项可以是消息（dict），也可以是 async 可调用（比如 server.clear_history）。"""
    original = web.WebSocketResponse.send_json
    sent = {"n": 0}

    async def send_json(self, data, *args, **kwargs):
        result = await original(self, data, *args, **kwargs)
        await asyncio.sleep(0)
        sent["n"] += 1
        for item in plan.get(sent["n"], ()):
            if callable(item):
                await item()
            else:
                await server.broadcast(item)
        return result

    monkeypatch.setattr(web.WebSocketResponse, "send_json", send_json)


async def _open(server):
    app = web.Application()
    app.router.add_get("/ws", server._ws)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _drain(ws, until_idle=0.2):
    got = []
    while True:
        try:
            got.append(await ws.receive_json(timeout=until_idle))
        except asyncio.TimeoutError:
            return got


def _reconnect(server):
    """连一次，读到安静为止；返回收到的全部消息，以及读完时它是不是已入册的普通客户端。"""
    async def scenario():
        client = await _open(server)
        try:
            ws = await client.ws_connect("/ws")
            got = await _drain(ws)
            registered = len(server.clients) == 1 and not _replaying(server)
            await ws.close()
            return got, registered
        finally:
            await client.close()

    return asyncio.run(scenario())


def _replaying(server):
    """还在回放中的页面登记（修法之前的服务端没有这个字段，当作空——让测试红在「漏了广播」上）。"""
    return getattr(server, "_replaying", None) or {}


def _key(m):
    return (m["type"], m.get("alert_id", m.get("id", m.get("detail"))))


def _live(got):
    """收到的实时消息（没有 replay 标记的，hello 除外），按到达顺序。"""
    return [m for m in got if m["type"] != "hello" and not m.get("replay")]


def _replayed(got, kind):
    return [m for m in got if m.get("replay") and m["type"] == kind]


@pytest.mark.parametrize("n", range(1, REPLAY_SENDS + 1))
def test_a_broadcast_during_any_step_of_the_replay_reaches_the_page(monkeypatch, n):
    """第 n 条回放发完之后（含 hello 刚发完、最后一条回放刚发完这两个端点）来的广播，一条都不丢、不重。"""
    server = _server()
    _plan_sends(monkeypatch, server, {n: LATE})

    got, registered = _reconnect(server)

    assert got[0]["type"] == "hello"
    assert [_key(m) for m in _live(got)] == [_key(m) for m in LATE]       # 按发生顺序，各一次
    assert len(_replayed(got, "alert")) == ALERTS
    assert len(_replayed(got, "caption")) == CAPTIONS
    assert len(_replayed(got, "comment")) == COMMENTS
    assert registered                                                      # 之后是个正常的实时客户端


def test_late_messages_come_after_the_whole_replay_and_are_not_marked_as_replay(monkeypatch):
    """补发的是真正的新消息：排在回放后面，不带 replay——页面当成实时处理（新警报照常提醒）。"""
    server = _server()
    _plan_sends(monkeypatch, server, {2: LATE})

    got, _ = _reconnect(server)

    first_live = next(i for i, m in enumerate(got) if i and not m.get("replay"))
    assert first_live == REPLAY_SENDS
    assert all(m.get("replay") is True for m in got[1:first_live])
    assert all("replay" not in m for m in got[first_live:])


def test_a_broadcast_that_lands_while_the_backlog_is_being_flushed_is_sent_too(monkeypatch):
    """补发的时候又来了新的：再补一轮，不能在第一轮补完就入册。"""
    server = _server()
    late_a, late_b = LATE[0], LATE[1]
    # 第 REPLAY_SENDS 条后来 late_a；补发 late_a 就是第 REPLAY_SENDS+1 条，那条发完时又来 late_b
    _plan_sends(monkeypatch, server, {REPLAY_SENDS: [late_a], REPLAY_SENDS + 1: [late_b]})

    got, registered = _reconnect(server)

    assert [_key(m) for m in _live(got)] == [_key(late_a), _key(late_b)]
    assert registered


def test_an_alert_that_lands_mid_replay_is_still_cached_for_the_next_reload(monkeypatch):
    """补发不替代缓存：这个页面再刷新一次，照样从缓存里拿到那条警报。"""
    server = _server()
    _plan_sends(monkeypatch, server, {3: [LATE[0]]})

    _reconnect(server)

    assert [a["alert_id"] for a in server.alerts] == [0, 1, 2, 900]


def test_a_translation_patch_for_an_already_snapshotted_item_reaches_the_page(monkeypatch):
    """回放里那条的副本在快照时就拷好了，晚到的译文补丁只会改缓存——页面得靠补发才拿得到。"""
    server = _server()
    patches = [
        {"type": "caption_update", "id": 2, "translated": "dos"},
        {"type": "comment_update", "id": "c1", "translated": "hola uno", "state": "ok"},
        {"type": "alert_update", "alert_id": 1, "translated": "curar"},
    ]
    _plan_sends(monkeypatch, server, {2: patches})

    got, _ = _reconnect(server)

    assert [m["type"] for m in _live(got)] == ["caption_update", "comment_update", "alert_update"]
    # 回放里那几条仍是快照时的样子（没有译文），所以补丁不是多余的
    assert "translated" not in next(m for m in _replayed(got, "caption") if m["id"] == 2)


def test_a_clear_that_lands_mid_replay_reaches_the_page(monkeypatch):
    """另一个页面在这个页面回放时点了「清空字幕」：回放的旧字幕之后要收到 history_cleared，
    不然这个页面重连完，清掉的行又全摆在那里。"""
    server = _server()
    _plan_sends(monkeypatch, server, {6: [lambda: server.clear_history("captions")]})

    got, _ = _reconnect(server)

    assert _live(got) == [{"type": "history_cleared", "kind": "captions"}]
    assert len(_replayed(got, "caption")) == CAPTIONS        # 旧的先摆上，清空通知在它们后面
    assert len(server.history) == 0


def test_a_broadcast_after_the_page_joined_arrives_exactly_once(monkeypatch):
    """入册之后走正常广播，backlog 已经清空、不再收：不会既补发又广播，收两遍。"""
    server = _server()
    _plan_sends(monkeypatch, server, {2: [LATE[0]]})

    async def scenario():
        client = await _open(server)
        try:
            ws = await client.ws_connect("/ws")
            first = await _drain(ws)
            assert not _replaying(server) and len(server.clients) == 1
            await server.broadcast(_comment(950))
            second = await _drain(ws)
            await ws.close()
            return first, second
        finally:
            await client.close()

    first, second = asyncio.run(scenario())
    assert [_key(m) for m in _live(first)] == [("alert", 900)]
    assert [_key(m) for m in second] == [("comment", "c950")]


def test_a_page_that_stalls_in_the_replay_is_dropped_when_its_backlog_hits_the_cap(monkeypatch):
    """连上了却不往下走的页面：它回放期间每条广播都得替它留着，不设上限就是泄漏。
    到顶就断开（abort）并忘掉它的队列——页面自己会重连，hello 之后拿完整回放。"""
    server = _server()
    server.MAX_REPLAY_BACKLOG = 2
    burst = [_comment(910), _comment(911), _comment(912), _comment(913)]
    _plan_sends(monkeypatch, server, {2: burst})

    async def scenario():
        client = await _open(server)
        try:
            ws = await client.ws_connect("/ws")
            kinds = []
            while True:
                try:
                    msg = await ws.receive(timeout=1.0)
                except asyncio.TimeoutError:
                    break
                kinds.append(msg.type)
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED,
                                WSMsgType.ERROR):
                    break
            await asyncio.sleep(0.1)
            return kinds
        finally:
            await client.close()

    kinds = asyncio.run(scenario())
    assert not server.clients and not _replaying(server) and not server._transports
    assert kinds and kinds[-1] in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED,
                                   WSMsgType.ERROR)
    # 断开的只是这个卡住的页面：缓存一条没少，下一个页面照常拿到完整回放
    assert [c["id"] for c in server.comments][-4:] == ["c910", "c911", "c912", "c913"]
    monkeypatch.undo()
    got, registered = _reconnect(server)
    assert len(_replayed(got, "comment")) == COMMENTS + len(burst)
    assert registered


def test_a_server_without_the_replay_registry_still_broadcasts():
    """个别测试（以及旧的构造方式）用 __new__ 造服务端，没有 _replaying：broadcast 不能因此崩。"""
    server = CaptionServer.__new__(CaptionServer)
    server.clients = set()
    server.history = []
    server.alerts = []
    server.config = {}

    asyncio.run(server.broadcast({"type": "status", "state": "idle", "detail": ""}))
    assert server.config["status"]["state"] == "idle"
