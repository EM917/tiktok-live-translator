"""浮窗页的连接（/ws?role=float）：服务端怎么把它和中控页分开。

浮窗只显示：它收得少（不收报警）、发不了指令、不算「显示报警的界面页面」。最后这条是合规证据：
Pipeline._alert_stamp 把页面数写进每条报警的审计，浮窗数进去会让「有页面接收了这条报警」变成假话。
"""
import asyncio
import re

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from app.pipeline import Pipeline
from app import i18n
from app.server import (FLOAT_CONFIG_KEYS, FLOAT_REPLAY_CAPTIONS, FLOAT_REPLAY_COMMENTS, FLOAT_TYPES,
                        WEB_DIR, CaptionServer)


def caption(i, **extra):
    return dict({"type": "caption", "id": i, "ts": 1000.0 + i, "original": "uno %d" % i,
                 "translated": None, "translate_state": "pending"}, **extra)


def comment(i):
    return {"type": "comment", "id": "c%d" % i, "user": "ana", "text": "hola %d" % i,
            "ts": 1000.0 + i, "translated": None, "state": "pending"}


def alert(i):
    return {"type": "alert", "alert_id": i, "term": "cura", "tier": "exact", "ts": 1000.0 + i}


async def with_client(server, body):
    app = web.Application()
    app.router.add_get("/ws", server._ws)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        return await body(client)
    finally:
        await client.close()


async def read_until_quiet(ws, wait=0.15):
    got = []
    while True:
        try:
            msg = await ws.receive(timeout=wait)
        except asyncio.TimeoutError:
            return got
        if msg.type != WSMsgType.TEXT:
            return got
        got.append(msg.json())


def seeded_server():
    server = CaptionServer(port=0)
    server.config.update({"caption_size": 40, "target_lang": "zh-CN", "version": "9.9.9",
                          "engine": {"secret": "x"}, "viewer": {"url": "http://192.168.1.2:8766/#k=abc"},
                          "guard_state": {"enabled": True}, "guard_marked": {"u1": {"keys": ["u1"]}}})

    async def fill():
        await server.broadcast({"type": "status", "state": "live", "detail": ""})
        for i in range(5):
            await server.broadcast(alert(i))
        for i in range(8):
            await server.broadcast(caption(i))
        await server.broadcast({"type": "session_break", "ts": 1.0, "streamer": "x"})
        for i in range(30):
            await server.broadcast(comment(i))
    asyncio.run(fill())
    return server


# ---- hello 和回放 -------------------------------------------------------------------------

def test_the_float_hello_carries_only_what_the_float_page_uses():
    server = seeded_server()

    async def body(client):
        ws = await client.ws_connect("/ws?role=float")
        first = await ws.receive_json()
        await ws.close()
        return first

    hello = asyncio.run(with_client(server, body))
    assert hello["type"] == "hello"
    assert set(hello["config"]) <= set(FLOAT_CONFIG_KEYS)
    assert hello["config"]["caption_size"] == 40 and hello["config"]["status"]["state"] == "live"
    for leaked in ("engine", "viewer", "version"):
        assert leaked not in hello["config"]


def test_the_float_replay_is_short_and_has_no_alerts():
    server = seeded_server()

    async def body(client):
        ws = await client.ws_connect("/ws?role=float")
        got = await read_until_quiet(ws)
        await ws.close()
        return got

    got = asyncio.run(with_client(server, body))
    types = [m["type"] for m in got[1:]]
    assert "alert" not in types and "alert_update" not in types
    caps = [m for m in got if m["type"] == "caption"]
    assert [c["id"] for c in caps] == [5, 6, 7] and len(caps) == FLOAT_REPLAY_CAPTIONS
    assert all(c["replay"] for c in caps)
    assert [c.get("restore") for c in caps] == [None, None, True]
    comments = [m for m in got if m["type"] == "comment"]
    assert len(comments) == FLOAT_REPLAY_COMMENTS and comments[-1]["id"] == "c29"
    assert all(c["replay"] for c in comments)


def test_the_control_page_still_gets_everything():
    server = seeded_server()

    async def body(client):
        ws = await client.ws_connect("/ws")
        got = await read_until_quiet(ws)
        await ws.close()
        return got

    got = asyncio.run(with_client(server, body))
    assert [m["type"] for m in got].count("alert") == 5
    assert "engine" in got[0]["config"] and "viewer" in got[0]["config"]


def test_any_other_role_value_is_a_normal_control_page():
    server = seeded_server()

    async def body(client):
        results = {}
        for role in ("control", "admin", "FLOAT", ""):
            ws = await client.ws_connect("/ws?role=" + role)
            results[role] = await read_until_quiet(ws)
            await ws.close()
        return results

    results = asyncio.run(with_client(server, body))
    for role, got in results.items():
        assert "engine" in got[0]["config"], role


# ---- 广播 ---------------------------------------------------------------------------------

def test_live_broadcasts_reach_the_float_page_only_when_it_uses_them():
    server = CaptionServer(port=0)
    server.config["caption_size"] = 30

    async def body(client):
        ws = await client.ws_connect("/ws?role=float")
        await read_until_quiet(ws)                      # hello
        sent = [caption(1), {"type": "caption_update", "id": 1, "translated": "hola", "translate_state": "ok"},
                comment(1), {"type": "comment_update", "id": "c1", "translated": "x", "state": "ok"},
                {"type": "comment_guard", "id": "c1", "guard": {"lvl": "alert"}},
                {"type": "guard_mark", "key": "u1", "entry": {"keys": ["u1"]}},
                {"type": "session_break", "ts": 2.0, "streamer": "y"},
                {"type": "status", "state": "idle", "detail": ""},
                {"type": "history_cleared", "kind": "captions"},
                {"type": "config", "caption_size": 44},
                # 下面这些浮窗不收
                alert(1), {"type": "alert_update", "alert_id": 1, "context_zh": "x"},
                {"type": "incident", "id": "i", "level": "warn", "text": "x"},
                {"type": "health", "level": "ok"}, {"type": "notice", "text": "hi"},
                {"type": "stats", "x": 1}, {"type": "guard_event", "ev": {"id": "e"}},
                {"type": "viewer", "url": "http://x/#k=1"}, {"type": "engine", "engine": "auto"},
                {"type": "selfcheck"}, {"type": "disk"}, {"type": "watchlist"},
                {"type": "comment_source", "backend": "connected"},
                {"type": "config", "engine": {"secret": 1}, "version": "1"}]
        for msg in sent:
            await server.broadcast(msg)
        return await read_until_quiet(ws)

    got = asyncio.run(with_client(server, body))
    assert [m["type"] for m in got] == ["caption", "caption_update", "comment", "comment_update",
                                        "comment_guard", "guard_mark", "session_break", "status",
                                        "history_cleared", "config"]
    assert got[-1] == {"type": "config", "caption_size": 44}       # 只留浮窗用得上的键


def test_every_type_the_float_page_handles_is_forwarded_and_nothing_else():
    """web/float.js 的 floatReduce 里 case 了哪些类型，和 FLOAT_TYPES 一一对应：
    服务端多转发一种，页面白收；页面多认一种，服务端不转发它就永远等不到。一边改了另一边没改就红。"""
    src = (WEB_DIR / "float.js").read_text(encoding="utf-8")
    body = src.split("function floatReduce(", 1)[1].split("\nfunction ", 1)[0]
    handled = set(re.findall(r'case "([a-z_]+)":', body))
    assert handled == set(FLOAT_TYPES) | {"hello"}
    # hello / config 里页面读的键，也和服务端留的键一一对应
    keys = set(re.findall(r'"([a-z_]+)" in cfg', src))
    assert keys <= set(FLOAT_CONFIG_KEYS), keys
    assert set(FLOAT_CONFIG_KEYS) - keys <= {"status", "target_lang"}, keys     # status 单独读；目标语言页面暂不用


def test_float_clients_see_no_alerts_during_a_replay_backlog():
    """回放期间攒的广播也要过同一道滤网。"""
    server = CaptionServer(port=0)
    fl, ctl = object(), object()
    server._float_ws.add(fl)
    server._replaying[fl] = []
    server._replaying[ctl] = []

    async def go():
        await server.broadcast(alert(1))
        await server.broadcast(caption(1))
        await server.broadcast({"type": "config", "engine": {"x": 1}})
    asyncio.run(go())
    assert [m["type"] for m in server._replaying[fl]] == ["caption"]
    assert [m["type"] for m in server._replaying[ctl]] == ["alert", "caption", "config"]


# ---- 只读 ---------------------------------------------------------------------------------

def test_a_float_page_cannot_send_anything():
    server = seeded_server()
    calls = []
    server.on_control = lambda data: calls.append(data)

    async def body(client):
        ws = await client.ws_connect("/ws?role=float")
        await read_until_quiet(ws)
        for msg in ({"type": "clear_history", "kind": "captions"}, {"type": "start", "url": "https://x"},
                    {"type": "stop"}, {"type": "float_window", "on": False},
                    {"type": "set_target", "value": "en"}):
            await ws.send_json(msg)
        await ws.send_str("not json")
        await asyncio.sleep(0.1)
        await ws.close()
        # 同一台服务器上，中控页的 clear_history 照常生效
        control = await client.ws_connect("/ws")
        await read_until_quiet(control)
        await control.send_json({"type": "clear_history", "kind": "captions"})
        await asyncio.sleep(0.1)
        await control.close()

    asyncio.run(with_client(server, body))
    assert calls == []                                     # on_control 一条都没收到
    assert len(server.history) == 0                        # 是中控页那条清掉的，不是浮窗


def test_a_float_page_that_clears_nothing_leaves_the_history():
    server = seeded_server()
    before = len(server.history)

    async def body(client):
        ws = await client.ws_connect("/ws?role=float")
        await read_until_quiet(ws)
        await ws.send_json({"type": "clear_history", "kind": "captions"})
        await asyncio.sleep(0.1)
        await ws.close()

    asyncio.run(with_client(server, body))
    assert len(server.history) == before


# ---- 不算「显示报警的页面」 ---------------------------------------------------------------

def test_the_float_page_is_not_counted_as_a_page_that_shows_alerts():
    server = CaptionServer(port=0)
    control, fl = object(), object()
    server.clients.update({control, fl})
    server._float_ws.add(fl)
    assert len(server.clients) == 2 and server.ui_client_count() == 1
    server.clients.discard(control)
    assert server.ui_client_count() == 0


def test_an_alert_audit_stamp_does_not_count_the_float_page():
    server = CaptionServer(port=0)
    control, fl = object(), object()
    server.clients.update({control, fl})
    server._float_ws.add(fl)
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.server = server
    assert pipeline._alert_stamp()["ui_clients"] == 1
    server.clients.discard(control)                  # 中控页断线、只剩浮窗：审计不能说有页面在接收
    assert pipeline._alert_stamp()["ui_clients"] == 0


def test_the_stamp_still_works_with_a_bare_server_that_only_has_a_client_list():
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.server = type("S", (), {"clients": [1, 2, 3]})()
    assert pipeline._alert_stamp()["ui_clients"] == 3
    pipeline.server = None
    assert pipeline._alert_stamp()["ui_clients"] == 0


def test_a_stuck_float_page_is_dropped_without_an_audit_record(capsys):
    server = CaptionServer(port=0)
    dropped = []
    server.on_client_dropped = lambda reason, buffered: dropped.append(reason)
    fl, ctl = object(), object()
    server.clients.update({fl, ctl})
    server._float_ws.add(fl)
    server._drop_client(fl, None, "send_timeout", None)
    assert dropped == [] and fl not in server.clients and fl not in server._float_ws
    assert "[警告] 浮窗页收不下消息（send_timeout）" in capsys.readouterr().out
    server._drop_client(ctl, None, "send_timeout", None)         # 中控页照旧走审计
    assert dropped == ["send_timeout"]


def test_the_dropped_page_message_counts_only_ui_pages(capsys):
    pipeline = Pipeline.__new__(Pipeline)
    server = CaptionServer(port=0)
    control, fl = object(), object()
    server.clients.update({control, fl})
    server._float_ws.add(fl)
    pipeline.server = server
    pipeline.audit = None
    pipeline.on_ui_client_dropped("send_timeout")
    assert "还连着 1 个页面" in capsys.readouterr().out


# ---- 状态钩子 -----------------------------------------------------------------------------

def test_the_status_hook_sees_every_status_and_nothing_else():
    server = CaptionServer(port=0)
    seen = []
    server.on_status = seen.append

    async def go():
        await server.broadcast({"type": "status", "state": "live", "detail": ""})
        await server.broadcast(caption(1))
        await server.status("idle", "")
    asyncio.run(go())
    assert [m["state"] for m in seen] == ["live", "idle"]


def test_a_failing_status_hook_never_breaks_the_broadcast(capsys):
    server = CaptionServer(port=0)

    def boom(msg):
        raise ValueError("hook bug")
    server.on_status = boom
    asyncio.run(server.status("ended", "x"))
    assert server.config["status"]["state"] == "ended"
    assert "[警告] 处理状态钩子失败: hook bug" in capsys.readouterr().out


def test_a_bare_server_without_the_hook_attribute_still_broadcasts():
    server = CaptionServer.__new__(CaptionServer)
    server.config, server.clients = {}, set()
    server.history = server.alerts = server.comments = []
    asyncio.run(server.broadcast({"type": "status", "state": "idle"}))
    assert server.config["status"]["state"] == "idle"


# ---- 页面本身 -----------------------------------------------------------------------------

async def fetch_float(server, lang):
    app = web.Application()
    app.router.add_get("/float", server._float_page)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        with i18n.use(lang):
            resp = await client.get("/float")
            return resp.status, resp.headers.get("Content-Type", ""), await resp.text()
    finally:
        await client.close()


def test_the_float_page_is_served_in_the_current_interface_language():
    server = CaptionServer(port=0)
    status, ctype, html = asyncio.run(fetch_float(server, "zh"))
    assert status == 200 and ctype.startswith("text/html")
    assert html == (WEB_DIR / "float.html").read_text(encoding="utf-8")          # 中文：端文件本身，字节不变
    assert '<html lang="zh-CN">' in html

    status, ctype, html = asyncio.run(fetch_float(server, "en"))
    assert status == 200 and ctype.startswith("text/html")
    assert '<html lang="en">' in html and '<html lang="zh-CN">' not in html
    assert html.replace('<html lang="en">', '<html lang="zh-CN">') == \
        (WEB_DIR / "float.html").read_text(encoding="utf-8")                      # 只换了 <html> 开标签


def test_the_float_page_is_not_mistaken_for_the_main_interface():
    """双击第二次时 main.py 靠 <meta name="tlt-app"> 认主界面（app/instance.py）；浮窗页没有它。"""
    html = (WEB_DIR / "float.html").read_text(encoding="utf-8")
    assert "tlt-app" not in html
    assert "tlt-app" in (WEB_DIR / "index.html").read_text(encoding="utf-8")


def test_the_server_registers_the_float_route():
    async def go():
        server = CaptionServer(port=0)
        await server.start()
        try:
            routes = {r.resource.canonical for r in server._runner.app.router.routes()}
        finally:
            await server._runner.cleanup()
        return routes
    assert "/float" in asyncio.run(go())
