"""CaptionServer.broadcast 交给手机同看的那一段。

这是整个功能的 KPI 隔离点。识别循环 await 的就是报警广播：broadcast 里多一个
await，一台收不下消息的手机就变成识别延迟，而识别延迟变成音频积压，音频积压
变成漏报——对一个合规监听器，漏报是唯一不能接受的失败。所以这里钉三件事：
fanout 恰好被调一次、**不被 await**、它出什么事都不影响本机界面和既有留存。
"""
import asyncio

import pytest

from app.server import CaptionServer


class NotAwaitable:
    """被 await 就炸。fanout 是普通函数，broadcast 里写成 `await hub.fanout(msg)`
    会静默「可行」（返回 None 也能 await 失败得很晚），这个替身让它当场红。"""

    def __await__(self):
        raise AssertionError("broadcast 不许 await 手机同看的 fanout")


class FakeHub:
    def __init__(self, boom=False):
        self.calls = []
        self.boom = boom

    def fanout(self, msg):
        self.calls.append(msg)
        if self.boom:
            raise RuntimeError("观众侧炸了")
        return NotAwaitable()


class FakeClient:
    """本机控制页面替身。send_json 里刻意多让几轮循环过去。"""

    def __init__(self, turns=3):
        self.got = []
        self.turns = turns
        self.hub_calls_when_first_sent = None

    async def send_json(self, msg):
        for _ in range(self.turns):
            await asyncio.sleep(0)
        self.got.append(msg)


def make_server(hub=None, client=None):
    server = CaptionServer(port=0)
    if hub is not None:
        server.viewer = hub
    if client is not None:
        server.clients.add(client)
        server._transports[client] = None
    return server


def test_a_new_server_has_no_viewer_face():
    """默认关闭 = 默认不存在第二个监听面。"""
    assert CaptionServer(port=0).viewer is None


def test_every_broadcast_hands_the_message_over_exactly_once():
    hub = FakeHub()
    server = make_server(hub)

    async def scenario():
        await server.broadcast({"type": "caption", "id": 1, "original": "hola"})
        await server.broadcast({"type": "alert", "alert_id": 1, "term": "x"})
        await server.status("live", "直播中")

    asyncio.run(scenario())
    assert [m["type"] for m in hub.calls] == ["caption", "alert", "status"]


def test_a_failing_fanout_never_reaches_the_local_ui():
    hub = FakeHub(boom=True)
    client = FakeClient()
    server = make_server(hub, client)

    async def scenario():
        await server.broadcast({"type": "caption", "id": 1, "original": "hola"})

    asyncio.run(scenario())
    assert len(hub.calls) == 1
    assert client.got == [{"type": "caption", "id": 1, "original": "hola"}]


def test_an_old_server_object_without_the_attribute_still_broadcasts():
    """升级路径与测试替身：没有 viewer 属性的对象不能让广播崩掉。"""
    server = make_server()
    del server.viewer
    assert not hasattr(server, "viewer")

    async def scenario():
        await server.broadcast({"type": "caption", "id": 1})

    asyncio.run(scenario())
    assert len(server.history) == 1


def test_the_phone_is_served_before_the_local_pages():
    """刻意的顺序：本机页面每个最多拖 2 秒，放在它们后面的话，一个卡死的
    本机页面会把手机端一起拖慢。"""
    order = []

    class RecordingHub:
        def fanout(self, msg):
            order.append("hub")

    class RecordingClient(FakeClient):
        async def send_json(self, msg):
            for _ in range(5):
                await asyncio.sleep(0)
            order.append("client")

    server = make_server(RecordingHub(), RecordingClient())

    async def scenario():
        await server.broadcast({"type": "caption", "id": 1})

    asyncio.run(scenario())
    assert order == ["hub", "client"]


@pytest.mark.parametrize("msg,attr", [
    ({"type": "caption", "id": 1, "original": "hola"}, "history"),
    ({"type": "alert", "alert_id": 1, "term": "x"}, "alerts"),
    ({"type": "comment", "id": "c1", "user": "a", "text": "hi"}, "comments"),
])
def test_the_hook_does_not_disturb_the_existing_retention(msg, attr):
    """留存逻辑是刷新后还能看到报警的唯一依据，hook 不许动它。"""
    hub = FakeHub()
    server = make_server(hub)

    async def scenario():
        await server.broadcast(msg)

    asyncio.run(scenario())
    assert len(getattr(server, attr)) == 1
    assert len(hub.calls) == 1


def test_config_and_status_still_land_in_config():
    hub = FakeHub()
    server = make_server(hub)

    async def scenario():
        await server.broadcast({"type": "status", "state": "live", "detail": "d",
                                "command": "c"})
        await server.broadcast({"type": "config", "target_lang": "zh-TW"})

    asyncio.run(scenario())
    assert server.config["status"]["command"] == "c"
    assert server.config["target_lang"] == "zh-TW"
    assert len(hub.calls) == 2
