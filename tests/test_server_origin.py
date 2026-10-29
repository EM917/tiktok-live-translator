"""server._classify_origin：WS 来源判定（control / 拒绝）。
浏览器里任何网页都能对 ws://127.0.0.1 发起连接，这里是唯一防线。
曾有第三级 "view"（Chrome 插件以 TikTok 页面身份只收字幕）——插件撤掉后
（2026-09-07）连同这一级一起删了：没有合法客户端需要以别的来源连进来。"""
from types import SimpleNamespace

from app.server import CaptionServer


def classify(origin, port=8765):
    server = CaptionServer(port=port)
    request = SimpleNamespace(headers={} if origin is None else {"Origin": origin})
    return server._classify_origin(request)


def test_no_origin_is_control():
    # 非浏览器客户端（curl、本地脚本、应用窗口）没有 Origin 头。仍是控制面，只有一处例外：闪购的开始、继续这类
    # 会让工具对观众做事的指令，本机脚本发不了（回 local_only，停止照发；app/server.py CONTROL_SOURCE，
    # tests/test_pipeline_flash.py::test_the_server_tells_a_page_from_a_local_script）
    assert classify(None) == "control"


def test_own_page_is_control():
    assert classify("http://127.0.0.1:8765") == "control"
    assert classify("http://localhost:8765") == "control"


def test_other_local_port_is_rejected():
    # 本机跑的其他网页（别人的开发服务器）不该能碰本程序——以前给「只看」权，
    # 现在没有只看的客户端了，直接拒绝
    assert classify("http://127.0.0.1:3000") is None
    assert classify("http://localhost") is None       # 默认 80 端口 ≠ 8765


def test_port_follows_server():
    assert classify("http://127.0.0.1:9000", port=9000) == "control"
    assert classify("http://127.0.0.1:8765", port=9000) is None


def test_tiktok_pages_are_rejected():
    # 插件撤掉后 tiktok.com 上的脚本没有任何理由连进来
    assert classify("https://www.tiktok.com") is None
    assert classify("https://live.tiktok.com") is None


def test_lookalike_and_http_tiktok_rejected():
    assert classify("https://evil-tiktok.com") is None
    assert classify("https://tiktok.com.evil.com") is None
    assert classify("http://www.tiktok.com") is None
    assert classify("https://nottiktok.com") is None


def test_arbitrary_sites_rejected():
    assert classify("https://example.com") is None


# ---- 端到端：判定结果在 _ws 里怎么落地 ----
#
# 上面只测判定函数本身。这条约定曾由 test_comments.py 里一个 view 来源的
# 测试顺带覆盖；插件撤掉、那个测试删掉后，「control 连接的指令到得了
# on_control、被拒来源根本握不上手」就没有任何端到端测试钉着了——补上。

def test_ws_control_dispatch_and_rejection_end_to_end():
    import asyncio

    from aiohttp import web
    from aiohttp.client_exceptions import WSServerHandshakeError
    from aiohttp.test_utils import TestClient, TestServer

    control_calls = []

    async def scenario():
        server = CaptionServer(port=8765)
        server.on_control = lambda data: control_calls.append(data)
        app = web.Application()
        app.router.add_get("/ws", server._ws)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # 无 Origin：非浏览器客户端 / 应用窗口 —— 指令要到 on_control
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "start", "url": "https://x"})
            await ws.send_str("[1, 2]")            # 非对象 JSON：忽略，不能断连
            await ws.send_json({"type": "stop"})
            await asyncio.sleep(0.05)
            await ws.close()
            # tiktok.com 来源：以前是只读的 view 一级，现在直接拒绝握手
            rejected = None
            try:
                await client.ws_connect("/ws", headers={"Origin": "https://www.tiktok.com"})
            except WSServerHandshakeError as exc:
                rejected = exc.status
            return rejected
        finally:
            await client.close()

    status = asyncio.run(scenario())
    assert [c["type"] for c in control_calls] == ["start", "stop"]
    assert status == 403


# ---- 无 Origin 的连接拿不到同看口令（专家审查 S8 第 3 点）----
#
# 无 Origin 照旧给控制权（上面的 test_no_origin_is_control；tools/readme_shots.py 一类本地脚本靠它），
# 但 hello 以前把 config["viewer"] 整份发下去：同看链接 http://地址:端口/#k=口令、编码了同一条链接的
# 二维码、卡片说明里又抄了一遍链接。任何连得上 127.0.0.1 的本地进程都能拿走这把钥匙。浏览器和应用
# 窗口发起 WebSocket 一定带 Origin，中控页照旧拿到完整链接和二维码。

TOKEN = "Ab3_" + "x" * 39            # 43 个 URL 安全字符，和 secrets.token_urlsafe(32) 一样长
LAN_IP = "192.168.1.23"
OWN_ORIGIN = {"Origin": "http://127.0.0.1:8765"}


def _share_state():
    """同看打开、有一个局域网地址：ViewerHub.state() 的真载荷（真链接、真二维码、真说明文字）。"""
    from app import qr, viewer

    hub = viewer.ViewerHub(None, ports=[8766], token=TOKEN)
    hub._running = True
    url = hub.url(LAN_IP)
    return hub.state(ip_info={"ip": LAN_IP, "all": [LAN_IP], "ambiguous": False},
                     qr_rows=qr.rows(qr.encode(url.encode("utf-8"))))


def _without_secret(obj):
    import json
    return TOKEN not in json.dumps(obj, ensure_ascii=False) and "#k=" not in json.dumps(obj)


def test_share_state_fixture_really_carries_the_key():
    state = _share_state()
    assert state["url"] == "http://{}:8766/#k={}".format(LAN_IP, TOKEN)
    assert state["qr_rows"] and TOKEN in state["note"]


def test_without_share_key_strips_the_link_the_qr_and_the_note():
    from app.server import without_share_key

    state = _share_state()
    msg = dict(state, type="viewer")
    out = without_share_key(msg)
    assert _without_secret(out)
    assert out["url"] == "http://{}:8766/".format(LAN_IP)       # 地址和端口还在，钥匙没了
    assert out["qr_rows"] is None
    assert "http://{}:8766/".format(LAN_IP) in out["note"]
    assert out["viewers"] == state["viewers"] and out["on"] is True
    # 不改传进来的对象：中文时 i18n.render 原样返回 server.config 本身
    assert msg["url"].endswith("#k=" + TOKEN) and msg["qr_rows"] == state["qr_rows"]
    # config 形状（hello 的 config、config 广播）同样认
    cfg = {"type": "config", "viewer": state, "target_lang": "zh-CN"}
    out_cfg = without_share_key(cfg)
    assert _without_secret(out_cfg) and out_cfg["target_lang"] == "zh-CN"
    assert cfg["viewer"] is state


def test_without_share_key_keeps_the_note_bilingual():
    """中文界面时说明文字还是 Bi（没渲染）：两臂都去掉钥匙，英文不丢。"""
    from app import i18n
    from app.server import without_share_key

    out = without_share_key({"type": "viewer", **_share_state()})
    assert isinstance(out["note"], i18n.Bi)
    assert TOKEN not in out["note"] and TOKEN not in out["note"].en
    assert "http://{}:8766/".format(LAN_IP) in out["note"].en


def test_without_share_key_leaves_everything_else_alone():
    from app.server import without_share_key

    for msg in ({"type": "status", "state": "live", "detail": ""},
                {"type": "caption", "id": 1, "original": "hola"},
                {"type": "viewer", "on": False, "url": None, "qr_rows": None, "note": "x"},
                {"type": "config", "viewer": {"on": False, "url": None}},
                {"type": "config", "target_lang": "en"}):
        assert without_share_key(msg) is msg       # 同一个对象：不复制、不改写


def _hello_and_live(lang="zh"):
    """起真的 _ws：一个无 Origin、一个带本机 Origin 的连接，各取 hello，再广播一条 viewer。"""
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from app import i18n

    async def first(ws, kind):
        for _ in range(20):
            msg = await ws.receive_json(timeout=5)
            if msg.get("type") == kind:
                return msg
        raise AssertionError("没收到 " + kind)

    async def scenario():
        server = CaptionServer(port=8765)
        state = _share_state()
        server.config["viewer"] = state
        app = web.Application()
        app.router.add_get("/ws", server._ws)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            bare = await client.ws_connect("/ws")
            page = await client.ws_connect("/ws", headers=OWN_ORIGIN)
            got = {"bare_hello": await first(bare, "hello"), "page_hello": await first(page, "hello")}
            for _ in range(100):                          # 两边都回放完、入册了再广播
                if len(server.clients) == 2:
                    break
                await asyncio.sleep(0.01)
            await server.broadcast(dict(state, type="viewer", viewers=1))
            got["bare_live"] = await first(bare, "viewer")
            got["page_live"] = await first(page, "viewer")
            await bare.close()
            await page.close()
            await asyncio.sleep(0.05)
            got["keyless_left"] = len(server._keyless_ws)
            return got
        finally:
            await client.close()

    with i18n.use(lang):
        return asyncio.run(scenario())


def test_a_client_without_origin_never_receives_the_share_key():
    got = _hello_and_live()
    bare_hello, bare_live = got["bare_hello"], got["bare_live"]
    assert _without_secret(bare_hello) and _without_secret(bare_live)
    assert bare_hello["config"]["viewer"]["url"] == "http://{}:8766/".format(LAN_IP)
    assert bare_hello["config"]["viewer"]["qr_rows"] is None
    assert bare_live["viewers"] == 1 and bare_live["qr_rows"] is None
    assert got["keyless_left"] == 0                       # 断开后从名单里摘掉


def test_the_own_page_still_gets_the_full_link_and_qr():
    got = _hello_and_live()
    for key in ("page_hello", "page_live"):
        state = got[key]["config"]["viewer"] if key == "page_hello" else got[key]
        assert state["url"] == "http://{}:8766/#k={}".format(LAN_IP, TOKEN), key
        assert state["qr_rows"], key
        assert TOKEN in state["note"], key


def test_english_interface_strips_the_key_too():
    got = _hello_and_live("en")
    assert _without_secret(got["bare_hello"]) and _without_secret(got["bare_live"])
    note = got["bare_hello"]["config"]["viewer"]["note"]
    assert note.startswith("On. On a phone connected to the same Wi-Fi")
    assert TOKEN in got["page_hello"]["config"]["viewer"]["note"]
