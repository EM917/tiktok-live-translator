"""手机同看唯一的真 socket 集成测试。

只绑 127.0.0.1 + 端口 0（内核分配）：不占任何固定端口，也不对外监听。别的用例
一律直接调 handler——这一条存在的意义是证明那些替身没有骗我们：真的 aiohttp
握手、真的 WebSocket 帧、真的关闭码。

最重要的断言是 `on_control` 零调用：观众面上不存在通往控制面的路。
"""
import asyncio

import aiohttp
import pytest
from aiohttp import WSMsgType

from app import viewer as viewer_mod
from app.server import CaptionServer
from app.viewer import ViewerHub

TIMEOUT = 10.0


@pytest.fixture
def web_dir(tmp_path):
    for name in viewer_mod.VIEWER_FILES:
        (tmp_path / name).write_text("stand-in " + name, encoding="utf-8")
    return tmp_path


def run(coro):
    return asyncio.run(coro)


async def recv(ws):
    return await asyncio.wait_for(ws.receive_json(), TIMEOUT)


class Harness:
    def __init__(self, web_dir):
        self.web_dir = web_dir
        self.control_calls = []

    async def __aenter__(self):
        # 控制面不 start()：这个用例要的只是它的留存与 broadcast 逻辑
        self.server = CaptionServer(port=0)
        self.server.on_control = self.control_calls.append
        self.server.config["status"] = {"state": "live", "detail": "监听 /Users/x",
                                        "command": "git pull"}
        self.server.config["comment_backend"] = "connected"
        self.server.history.append({"type": "caption", "id": 1, "original": "uno",
                                    "translated": "一", "asr_ms": 900})
        self.server.alerts.append({"type": "alert", "alert_id": 1, "term": "colágeno",
                                   "tier": "exact", "context": "…colágeno…",
                                   "streamer": "bella", "session": "s1",
                                   "ui_clients": 2})
        self.token = viewer_mod.new_token()
        self.hub = ViewerHub(self.server, ports=[0], token=self.token,
                             bind_host="127.0.0.1", web_dir=self.web_dir,
                             log=lambda *a: None)
        await self.hub.start()
        self.server.viewer = self.hub
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        await self.session.close()
        await self.hub.stop("off")

    @property
    def base(self):
        return "http://127.0.0.1:{}".format(self.hub.port)

    def connect(self):
        # 不传 timeout：aiohttp 3.11 起这个参数只收 ClientWSTimeout，
        # 等待一律用外层的 asyncio.wait_for
        return self.session.ws_connect(self.base + "/vws")


def test_a_phone_authenticates_gets_the_replay_and_cannot_control_anything(web_dir):
    async def scenario():
        async with Harness(web_dir) as h:
            async with h.connect() as ws:
                await ws.send_json({"type": "auth", "k": h.token})
                hello = await recv(ws)
                assert hello["type"] == "viewer_hello"
                assert hello["read_only"] is True
                assert hello["viewers"] == 1
                status = await recv(ws)
                assert status == {"type": "status", "state": "live"}
                source = await recv(ws)
                assert source == {"type": "comment_source", "backend": "live"}
                alert = await recv(ws)
                assert alert["type"] == "alert" and alert["replay"] is True
                for gone in ("streamer", "session", "ui_clients"):
                    assert gone not in alert
                caption = await recv(ws)
                assert caption["type"] == "caption" and caption["id"] == 1
                assert "asr_ms" not in caption
                assert caption["restore"] is True

                # 控制形状的消息：只计数然后丢弃，绝不转交 on_control
                await ws.send_json({"type": "start",
                                    "url": "http://tiktok.com/@a/live"})
                await ws.send_json({"type": "stop"})
                await asyncio.sleep(0.1)
                assert h.control_calls == []

                # 实时广播真的穿过 fanout 到了手机上
                await h.server.broadcast({"type": "caption", "id": 2,
                                          "original": "dos", "asr_ms": 800})
                live = await recv(ws)
                assert live["id"] == 2 and "asr_ms" not in live

                # 含 token 的 viewer 载荷永远到不了手机：下一条收到的是字幕，不是它
                await h.server.broadcast({"type": "viewer", "on": True,
                                          "url": h.base + "/#k=" + h.token})
                await h.server.broadcast({"type": "caption", "id": 3,
                                          "original": "tres"})
                after = await recv(ws)
                assert after["id"] == 3
            assert h.hub.count == 0        # 断开后出册

    run(scenario())


def test_a_wrong_token_is_closed_with_4401(web_dir):
    async def scenario():
        async with Harness(web_dir) as h:
            async with h.connect() as ws:
                await ws.send_json({"type": "auth", "k": "x" * viewer_mod.TOKEN_LEN})
                denied = await recv(ws)
                assert denied == {"type": "viewer_denied", "reason": "token"}
                msg = await asyncio.wait_for(ws.receive(), TIMEOUT)
                assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
                assert ws.close_code == 4401
            assert h.hub.count == 0

    run(scenario())


def test_the_phone_page_and_its_assets_are_served_with_the_headers(web_dir):
    async def scenario():
        async with Harness(web_dir) as h:
            async with h.session.get(h.base + "/", timeout=TIMEOUT) as resp:
                assert resp.status == 200
                assert "unsafe-inline" not in resp.headers["Content-Security-Policy"]
                assert resp.headers["X-Content-Type-Options"] == "nosniff"
                assert resp.headers["Referrer-Policy"] == "no-referrer"
            async with h.session.get(h.base + "/v/viewer.css", timeout=TIMEOUT) as r:
                assert r.status == 200
            for path in ("/v/app.js", "/v/index.html", "/v/../settings.json",
                         "/static/app.js", "/ws", "/v/"):
                async with h.session.get(h.base + path, timeout=TIMEOUT) as r:
                    assert r.status == 404, path

    run(scenario())


def test_stopping_the_share_closes_the_port(web_dir):
    async def scenario():
        async with Harness(web_dir) as h:
            base = h.base
            await h.hub.stop("off")
            assert h.hub.running is False
            with pytest.raises(aiohttp.ClientError):
                async with h.session.get(base + "/", timeout=TIMEOUT):
                    pass
            # 幂等：再关一次什么都不做（__aexit__ 还会关第三次）
            await h.hub.stop("off")

    run(scenario())


def test_a_second_phone_gets_its_own_replay(web_dir):
    """每条连接的回放压进它自己的队列，不会互相插队。"""
    async def scenario():
        async with Harness(web_dir) as h:
            async with h.connect() as one:
                await one.send_json({"type": "auth", "k": h.token})
                assert (await recv(one))["viewers"] == 1
                async with h.connect() as two:
                    await two.send_json({"type": "auth", "k": h.token})
                    hello = await recv(two)
                    assert hello["viewers"] == 2
                    await h.server.broadcast({"type": "caption", "id": 9,
                                              "original": "nueve"})
                    # one 还在回放里，两边最终都要收到这条实时字幕
                    seen = []
                    for ws in (one, two):
                        while True:
                            msg = await recv(ws)
                            if msg.get("id") == 9:
                                seen.append(msg["type"])
                                break
                    assert seen == ["caption", "caption"]
                    assert h.hub.count == 2

    run(scenario())
