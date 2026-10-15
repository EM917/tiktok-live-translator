"""中控页回放的深度，以及「清空」到底清什么。

两件事原来都是假的：
  1. 页面最多留 300 张字幕卡片，服务端却只缓存 100 条——刷新或重连后 hello 回放只有 100 条，
     刚才还能往上翻的 200 条没了；
  2. 「清空字幕」「清空弹幕」只删页面上的节点，缓存原样留着，刷新后全回来了。

这里钉的是：中控页回放的深度等于页面自己留的条数（从 web/app.js 读出来比，两边不会悄悄漂开）、
手机回放的深度一个字没变、清空会清服务端缓存、警报不受影响。
"""
import asyncio
import re
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import viewer
from app.server import (
    COMMENTS_MAX,
    HISTORY_BREAK_ROOM,
    HISTORY_MAX,
    CaptionServer,
    captions_tail,
)

APP_JS = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")


def _caption(i):
    return {"type": "caption", "id": i, "ts": 1000.0 + i, "original": "uno %d" % i}


def _comment(i):
    return {"type": "comment", "id": "c%d" % i, "user": "ana", "text": "hola %d" % i,
            "ts": 1000.0 + i, "state": "same"}


def _alert(i):
    return {"type": "alert", "alert_id": i, "term": "cura", "tier": "exact", "ts": 1000.0 + i}


def _fill(server, captions=0, comments=0, alerts=0, breaks_at=()):
    async def go():
        for i in range(alerts):
            await server.broadcast(_alert(i))
        for i in range(captions):
            if i in breaks_at:
                await server.broadcast({"type": "session_break", "ts": 1000.0 + i, "streamer": "luna_demo"})
            await server.broadcast(_caption(i))
        for i in range(comments):
            await server.broadcast(_comment(i))

    asyncio.run(go())


def _types(messages):
    return [m["type"] for m in messages]


# ---- 页面的上限和服务端的深度对得上 ----

def test_the_replay_depth_is_what_the_page_itself_keeps():
    """HISTORY_MAX / COMMENTS_MAX 抄的是 app.js 里的数。改一边不改另一边，这条红。"""
    history = re.search(r"var maxHistory = (\d+);", APP_JS)
    comments = re.search(r"var maxComments = (\d+);", APP_JS)
    assert history and comments, "app.js 里找不到 maxHistory / maxComments 的定义"
    assert HISTORY_MAX == int(history.group(1))
    assert COMMENTS_MAX == int(comments.group(1))


def test_the_comment_panel_trims_with_the_named_constant_not_a_bare_number():
    """弹幕栏的裁剪都用 maxComments：留一个裸 100 的话，改常量时它会被漏掉。"""
    body = APP_JS[APP_JS.index("function renderComment("):APP_JS.index("function updateComment(")]
    assert "maxComments" in body
    assert not re.search(r"\b100\b", re.sub(r"//[^\n]*", "", body))


# ---- 中控页的回放：300 条字幕 ----

def test_desktop_replay_carries_as_many_captions_as_the_page_keeps():
    server = CaptionServer(port=0)
    _fill(server, captions=HISTORY_MAX + 120, comments=COMMENTS_MAX + 40)
    out = server.replay_messages()
    captions = [m for m in out if m["type"] == "caption"]
    comments = [m for m in out if m["type"] == "comment"]
    assert len(captions) == HISTORY_MAX
    assert [m["id"] for m in captions] == list(range(120, HISTORY_MAX + 120))     # 最近的 300 条，顺序不变
    assert len(comments) == COMMENTS_MAX
    assert all(m["replay"] is True for m in out)
    assert [m for m in captions if m.get("restore")] == [captions[-1]]               # 只有最后一条恢复大字幕


def test_session_breaks_do_not_take_a_slot_from_the_300_captions():
    """页面只数 .cap 卡片，分隔线不占名额；换主播换得勤，回放的字幕也还是满 300 条。"""
    server = CaptionServer(port=0)
    breaks = tuple(range(100, 300, 10))                                               # 20 个分隔，都落在回放窗口里
    _fill(server, captions=HISTORY_MAX + 60, breaks_at=breaks)
    assert len(server.history) == HISTORY_MAX + HISTORY_BREAK_ROOM                    # deque 自己有上界
    out = server.replay_messages()
    assert sum(m["type"] == "caption" for m in out) == HISTORY_MAX
    assert sum(m["type"] == "session_break" for m in out) == 20
    # 分隔线还夹在它该在的两条字幕之间
    ids = [m["id"] if m["type"] == "caption" else "|" for m in out]
    assert ids.index("|") > 0 and ids[-1] != "|"


def test_captions_tail_cuts_by_captions_and_keeps_the_breaks_between():
    items = [_caption(0), {"type": "session_break"}, _caption(1), _caption(2), {"type": "session_break"},
             _caption(3)]
    assert _types(captions_tail(items, 2)) == ["caption", "session_break", "caption"]
    assert [m.get("id") for m in captions_tail(items, 2)] == [2, None, 3]
    assert captions_tail(items, 99) == items
    assert captions_tail(items, 0) == []
    assert captions_tail([], 5) == []
    assert captions_tail([{"type": "session_break"}], 5) == [{"type": "session_break"}]


def test_replay_order_is_alerts_then_captions_then_comments():
    server = CaptionServer(port=0)
    _fill(server, captions=3, comments=2, alerts=2)
    assert _types(server.replay_messages()) == ["alert"] * 2 + ["caption"] * 3 + ["comment"] * 2


def test_replay_does_not_mark_the_cached_messages():
    server = CaptionServer(port=0)
    _fill(server, captions=2, comments=1, alerts=1)
    server.replay_messages()
    assert all("replay" not in m and "restore" not in m
               for m in list(server.history) + list(server.comments) + list(server.alerts))


# ---- 手机的回放：一个字没变 ----

def test_phone_replay_keeps_exactly_its_old_caps_while_the_desktop_goes_deeper():
    server = CaptionServer(port=0)
    _fill(server, captions=HISTORY_MAX + 50, comments=COMMENTS_MAX + 50, alerts=50)
    snapshot = viewer.replay_snapshot(server, viewers=1)
    captions = [m for m in snapshot if m["type"] == "caption"]
    comments = [m for m in snapshot if m["type"] == "comment"]
    assert len(captions) == viewer.PHONE_HISTORY_MAX == 100
    assert len(comments) == viewer.PHONE_COMMENTS_MAX == 100
    # 手机拿的是最近的 100 条，和以前 deque(maxlen=100) 留下的那一段完全一样
    assert [m["id"] for m in captions] == list(range(HISTORY_MAX + 50 - 100, HISTORY_MAX + 50))
    assert captions[-1].get("restore") is True
    assert sum(m["type"] == "alert" for m in snapshot) == 50
    assert len(snapshot) <= viewer.REPLAY_MAX < viewer.QUEUE_MAX
    # 中控页同一时刻拿到的更深
    assert sum(m["type"] == "caption" for m in server.replay_messages()) == HISTORY_MAX


def test_phone_replay_counts_session_breaks_in_its_100_as_before():
    """手机的 100 数的是 history 里的条目（含分隔线）——和过去 deque(maxlen=100) 的口径一致。"""
    server = CaptionServer(port=0)
    _fill(server, captions=150, breaks_at=(100, 120))
    snapshot = viewer.replay_snapshot(server)
    shown = [m for m in snapshot if m["type"] in ("caption", "session_break")]
    assert len(shown) == viewer.PHONE_HISTORY_MAX
    assert [m["type"] for m in shown].count("session_break") == 2


def test_replay_max_is_still_the_sum_of_the_old_parts():
    assert viewer.REPLAY_MAX == 1 + 1 + 1 + 20 + 50 + viewer.PHONE_HISTORY_MAX + viewer.PHONE_COMMENTS_MAX == 273


# ---- 清空：服务端缓存也要清 ----

def test_clear_captions_empties_the_cache_and_nothing_else():
    server = CaptionServer(port=0)
    _fill(server, captions=5, comments=3, alerts=2, breaks_at=(2,))
    assert asyncio.run(server.clear_history("captions")) is True
    assert len(server.history) == 0
    assert len(server.comments) == 3 and len(server.alerts) == 2
    assert _types(server.replay_messages()) == ["alert"] * 2 + ["comment"] * 3        # 刷新后字幕不再回来


def test_clear_comments_empties_the_cache_and_nothing_else():
    server = CaptionServer(port=0)
    _fill(server, captions=5, comments=3, alerts=2)
    assert asyncio.run(server.clear_history("comments")) is True
    assert len(server.comments) == 0
    assert len(server.history) == 5 and len(server.alerts) == 2


def test_clearing_never_touches_the_alerts():
    """违禁词警报跨刷新留存是合规要求：没看到就等于漏报。两种清空都不许动它。"""
    server = CaptionServer(port=0)
    _fill(server, captions=4, comments=4, alerts=7)
    before = [dict(a) for a in server.alerts]
    asyncio.run(server.clear_history("captions"))
    asyncio.run(server.clear_history("comments"))
    assert [dict(a) for a in server.alerts] == before
    assert [m["alert_id"] for m in server.replay_messages()] == list(range(7))


def test_an_unknown_kind_clears_nothing_and_says_so():
    server = CaptionServer(port=0)
    _fill(server, captions=2, comments=2, alerts=1)
    for kind in (None, "", "alerts", "all", 5, ["captions"]):
        assert asyncio.run(server.clear_history(kind)) is False
    assert (len(server.history), len(server.comments), len(server.alerts)) == (2, 2, 1)


def test_the_phone_replays_nothing_older_after_a_clear():
    """清空之后新连上或重连的手机不再回放被清掉的行（用户决定：这是预期）。"""
    server = CaptionServer(port=0)
    _fill(server, captions=30, comments=30)
    asyncio.run(server.clear_history("captions"))
    asyncio.run(server.clear_history("comments"))
    snapshot = viewer.replay_snapshot(server)
    assert not [m for m in snapshot if m["type"] in ("caption", "comment")]


def test_captions_after_a_clear_start_a_fresh_history():
    server = CaptionServer(port=0)
    _fill(server, captions=10)
    asyncio.run(server.clear_history("captions"))
    asyncio.run(server.broadcast(_caption(99)))
    out = server.replay_messages()
    assert [m["id"] for m in out] == [99] and out[0]["restore"] is True


def test_a_late_translation_for_a_cleared_caption_is_harmless():
    server = CaptionServer(port=0)
    _fill(server, captions=2)
    asyncio.run(server.clear_history("captions"))
    asyncio.run(server.broadcast({"type": "caption_update", "id": 1, "translated": "你好"}))
    assert len(server.history) == 0


def test_the_cleared_notice_never_reaches_a_phone():
    """history_cleared 只给本机页面：手机的白名单默认拒绝，没有这一种。"""
    assert viewer.filter_payload({"type": "history_cleared", "kind": "captions"}) is None
    assert "history_cleared" not in viewer.ALLOW


# ---- 端到端：真的走一遍 WebSocket ----

async def _open(server):
    app = web.Application()
    app.router.add_get("/ws", server._ws)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _drain(ws, until_idle=0.15):
    got = []
    while True:
        try:
            got.append(await ws.receive_json(timeout=until_idle))
        except asyncio.TimeoutError:
            return got


def test_a_reconnecting_page_gets_300_captions_and_100_comments():
    async def scenario():
        server = CaptionServer(port=0)
        for i in range(HISTORY_MAX + 80):
            await server.broadcast(_caption(i))
        for i in range(COMMENTS_MAX + 30):
            await server.broadcast(_comment(i))
        client = await _open(server)
        try:
            ws = await client.ws_connect("/ws")
            got = await _drain(ws)
            await ws.close()
            return got
        finally:
            await client.close()

    got = asyncio.run(scenario())
    assert got[0]["type"] == "hello"
    assert sum(m["type"] == "caption" for m in got) == HISTORY_MAX
    assert sum(m["type"] == "comment" for m in got) == COMMENTS_MAX
    assert all(m["replay"] is True for m in got[1:])


def test_clear_history_over_the_socket_empties_the_cache_and_tells_the_other_pages():
    async def scenario():
        server = CaptionServer(port=0)
        for i in range(5):
            await server.broadcast(_caption(i))
        for i in range(4):
            await server.broadcast(_comment(i))
        await server.broadcast(_alert(1))
        client = await _open(server)
        try:
            clicker = await client.ws_connect("/ws")
            other = await client.ws_connect("/ws")
            await _drain(clicker)
            await _drain(other)
            await clicker.send_json({"type": "clear_history", "kind": "captions"})
            await clicker.send_json({"type": "clear_history", "kind": "nonsense"})        # 认不出：忽略，不断连
            await clicker.send_json({"type": "clear_history"})
            seen_by_other = await _drain(other)
            seen_by_clicker = await _drain(clicker)
            await clicker.send_json({"type": "clear_history", "kind": "comments"})
            await _drain(other)
            fresh = await client.ws_connect("/ws")                                        # 刷新后新连上的页面
            after_reload = await _drain(fresh)
            await fresh.close()
            await clicker.close()
            await other.close()
            return seen_by_other, seen_by_clicker, after_reload, server
        finally:
            await client.close()

    seen_by_other, seen_by_clicker, after_reload, server = asyncio.run(scenario())
    assert [m for m in seen_by_other if m["type"] == "history_cleared"] == [
        {"type": "history_cleared", "kind": "captions"}]
    assert [m for m in seen_by_clicker if m["type"] == "history_cleared"] == [
        {"type": "history_cleared", "kind": "captions"}]
    assert _types(after_reload) == ["hello", "alert"]                                     # 字幕、弹幕都不回来，警报还在
    assert after_reload[1]["alert_id"] == 1 and after_reload[1]["replay"] is True
    assert len(server.history) == 0 and len(server.comments) == 0 and len(server.alerts) == 1


def test_clear_history_is_not_passed_on_to_the_pipeline():
    """缓存归服务端管：这条消息在 _ws 里就处理完了，不进 on_control（Pipeline 不认识它）。"""
    seen = []

    async def scenario():
        server = CaptionServer(port=0)
        server.on_control = lambda data: seen.append(data["type"])
        client = await _open(server)
        try:
            ws = await client.ws_connect("/ws")
            await _drain(ws)
            await ws.send_json({"type": "clear_history", "kind": "captions"})
            await ws.send_json({"type": "stop"})
            await _drain(ws)
            await ws.close()
        finally:
            await client.close()

    asyncio.run(scenario())
    assert seen == ["stop"]
