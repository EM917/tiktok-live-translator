"""ViewerHub：分发、背压、注册顺序、鉴权、轮换。

这个文件里最重要的一条是「fanout 是同步的」（test_fanout_is_synchronous）。
业务前提：漏报警的代价远高于误报，而识别循环 await 的正是报警广播——观众侧
只要有一个 await，一台卡住的手机就变成识别延迟，也就变成漏报。所以宁可断开
手机（它会重连并拿到完整回放），绝不让它进关键路径。
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import WSMsgType

from app import viewer as viewer_mod
from app.viewer import ViewerHub

TOKEN = "t" * viewer_mod.TOKEN_LEN


@pytest.fixture
def loop():
    """给同步测试一个可用的事件循环：Python 3.9 的 asyncio.Queue() 构造时会去
    取当前循环。这个 fixture 只是让 Queue 建得出来，测试本身不跑循环。"""
    made = asyncio.new_event_loop()
    asyncio.set_event_loop(made)
    try:
        yield made
    finally:
        asyncio.set_event_loop(None)
        made.close()


class FakeWS:
    """观众 WebSocket 替身。incoming 是鉴权之后要「发进来」的消息脚本。"""

    def __init__(self, incoming=(), first=None, hang=False, send_delay=0.0,
                 linger=0.0):
        self.sent = []
        self.closed = None
        self.prepared = False
        self._incoming = list(incoming)
        self._first = first
        self._hang = hang
        self._send_delay = send_delay
        self._linger = linger

    async def prepare(self, request):
        self.prepared = True

    async def receive(self):
        if self._hang:
            await asyncio.sleep(30)
        if self._first is not None:
            first, self._first = self._first, None
            return first
        if self._incoming:
            return self._incoming.pop(0)
        return SimpleNamespace(type=WSMsgType.CLOSED, data=None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            if self._linger:
                await asyncio.sleep(self._linger)
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def send_json(self, payload):
        if self._send_delay:
            await asyncio.sleep(self._send_delay)
        self.sent.append(payload)

    async def close(self, code=1000, message=b""):
        self.closed = code


class FakeTransport:
    def __init__(self, peer="192.168.1.99"):
        self.aborts = 0
        self.closes = 0
        self.peer = peer

    def abort(self):
        self.aborts += 1

    def close(self):
        self.closes += 1

    def is_closing(self):
        return bool(self.aborts or self.closes)

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return (self.peer, 51515)
        return default


class StubServer:
    port = 8765

    def __init__(self, **config):
        self.config = dict(config)
        self.history = []
        self.alerts = []
        self.comments = []
        self.on_control = None


def text(payload):
    return SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(payload))


def auth(token=TOKEN):
    return text({"type": "auth", "k": token})


def make_hub(server=None, token=TOKEN, ports=(0,), **kwargs):
    """测试里的 hub 一律绑 127.0.0.1 + 端口 0：不占任何固定端口，也不对外监听。"""
    events = []
    hub = ViewerHub(server or StubServer(), ports=list(ports), token=token,
                    bind_host="127.0.0.1",
                    audit_hook=lambda event, **f: events.append((event, f)),
                    log=lambda *a: None, **kwargs)
    hub.events = events
    return hub


def reasons(hub, event="viewer_disconnected"):
    return [f.get("reason") for name, f in hub.events if name == event]


def add(hub, ws=None, ip="192.168.1.9"):
    v = hub.new_viewer(ws or FakeWS(), FakeTransport(), ip)
    hub.register(v)
    return v


# ---- 分发 ----
def test_fanout_is_synchronous(loop):
    """非 async 函数里注册两个观众、直接调 fanout，队列里立刻就有东西。
    这一条钉死的是「broadcast 不会为了观众而 await」。"""
    hub = make_hub()
    one, two = add(hub), add(hub)
    before = one.queue.qsize()
    hub.fanout({"type": "caption", "id": 1, "original": "hola"})
    assert one.queue.qsize() == before + 1
    assert two.queue.qsize() == before + 1
    assert hub.fanout({"type": "caption", "id": 2}) is None


def test_fanout_drops_only_the_viewer_that_cannot_keep_up(loop, monkeypatch):
    """一个观众落后 512 条 → 只丢它，另一个照常收；用 abort 不用 close。"""
    monkeypatch.setattr(viewer_mod, "QUEUE_MAX", 2)
    hub = make_hub()
    slow, ok = add(hub), add(hub)
    hub.fanout({"type": "caption", "id": 1})      # 队列满（1 条回放 + 这条）
    hub.fanout({"type": "caption", "id": 2})      # 放不下 → slow 和 ok 都满了
    assert reasons(hub) == ["queue_full", "queue_full"]
    assert slow.transport.aborts == 1
    assert slow.transport.closes == 0             # close 对不读的页面永远等不完
    assert ok.transport.aborts == 1
    assert hub.count == 0
    hub.fanout({"type": "caption", "id": 3})      # 没人了也不许抛


def test_fanout_ignores_messages_outside_the_whitelist(loop):
    hub = make_hub()
    v = add(hub)
    before = v.queue.qsize()
    hub.fanout({"type": "viewer", "url": "http://192.168.1.9:8766/#k=SECRET"})
    hub.fanout({"type": "config", "target_lang": "zh-CN"})
    hub.fanout({"type": "selfcheck", "items": []})
    assert v.queue.qsize() == before


def test_writer_drops_a_viewer_that_stops_reading(monkeypatch):
    monkeypatch.setattr(viewer_mod, "SEND_TIMEOUT_SEC", 0.01)

    async def scenario():
        hub = make_hub()
        ws = FakeWS(send_delay=5.0)
        v = add(hub, ws)
        v.writer = asyncio.ensure_future(hub._writer(v))
        await asyncio.sleep(0.1)
        return hub, v

    hub, v = asyncio.run(scenario())
    assert reasons(hub) == ["send_timeout"]
    assert v.transport.aborts == 1
    assert hub.count == 0


# ---- 注册与回放 ----
def test_register_pushes_the_whole_replay_in_one_synchronous_block(loop):
    """顺序：viewer_hello → status → comment_source → incidents → alerts →
    captions（最后一条带 restore）→ comments。register 全程无 await，所以
    之后来的实时消息只能排在回放后面——不存在「回放插进实时中间」的竞态。"""
    server = StubServer(status={"state": "live", "detail": "监听 /Users/x"},
                        comment_backend="connected",
                        incidents={"disk": {"id": "disk", "level": "warn",
                                            "text": "空间不足", "ts": 1.0}})
    server.alerts.append({"type": "alert", "alert_id": 1, "term": "colágeno",
                          "tier": "exact", "context": "…"})
    server.history.append({"type": "caption", "id": 1, "original": "uno"})
    server.history.append({"type": "caption", "id": 2, "original": "dos"})
    server.comments.append({"type": "comment", "id": "c1", "user": "ana",
                            "text": "hola"})
    hub = make_hub(server)
    v = add(hub)
    hub.fanout({"type": "caption", "id": 3, "original": "tres"})   # 实时的一条
    got = []
    while not v.queue.empty():
        got.append(v.queue.get_nowait())
    assert [m["type"] for m in got] == [
        "viewer_hello", "status", "comment_source", "incident", "alert",
        "caption", "caption", "comment", "caption"]
    assert got[0]["read_only"] is True
    assert got[0]["max_viewers"] == viewer_mod.MAX_VIEWERS
    assert "detail" not in got[1]                 # status 的自由文本没跟过来
    assert got[2]["backend"] == "live"
    assert got[4]["replay"] is True
    assert got[5]["replay"] is True and got[5]["id"] == 1
    assert got[6].get("restore") is True          # 最后一条历史字幕恢复大字幕
    assert got[7]["replay"] is True and got[7]["user"] == "ana"
    assert got[8]["id"] == 3 and "replay" not in got[8]   # 实时的排在最后


def test_register_does_not_blow_up_when_the_replay_exceeds_the_queue(loop, monkeypatch):
    """回放放不下不该拦住入册：实时报警比历史字幕重要。"""
    monkeypatch.setattr(viewer_mod, "QUEUE_MAX", 3)
    server = StubServer()
    for i in range(50):
        server.history.append({"type": "caption", "id": i, "original": "x"})
    hub = make_hub(server)
    v = add(hub)
    assert hub.count == 1
    assert v.queue.qsize() == 3


def test_replay_snapshot_stays_under_the_queue_limit():
    """回放上限 273 条要一直小于 QUEUE_MAX，否则每个新观众一进来就被判定落后。"""
    server = StubServer(status={"state": "live"}, comment_backend="idle",
                        incidents={str(i): {"id": str(i), "level": "warn",
                                            "text": "x", "ts": float(i)}
                                   for i in range(20)})
    server.alerts = [{"type": "alert", "alert_id": i} for i in range(50)]
    server.history = [{"type": "caption", "id": i} for i in range(100)]
    server.comments = [{"type": "comment", "id": i} for i in range(100)]
    snapshot = viewer_mod.replay_snapshot(server, viewers=1, share_since=1.0)
    assert len(snapshot) == viewer_mod.REPLAY_MAX
    assert viewer_mod.REPLAY_MAX < viewer_mod.QUEUE_MAX


def test_replay_snapshot_carries_the_alert_mode_when_config_has_it():
    """晚进来的手机要在下一次事件之前就知道这场报不报警，不能靠猜。"""
    server = StubServer(alerts_enabled=True)
    snapshot = viewer_mod.replay_snapshot(server)
    modes = [m for m in snapshot if m["type"] == "alert_mode"]
    assert modes == [{"type": "alert_mode", "on": True}]

    server = StubServer(alerts_enabled=False)
    snapshot = viewer_mod.replay_snapshot(server)
    modes = [m for m in snapshot if m["type"] == "alert_mode"]
    assert modes == [{"type": "alert_mode", "on": False}]


def test_replay_snapshot_omits_the_alert_mode_when_config_lacks_the_key():
    """没有这一键的都是老会话——按「未知」处理，不当「关闭」擅自下结论。"""
    server = StubServer()
    snapshot = viewer_mod.replay_snapshot(server)
    assert not any(m["type"] == "alert_mode" for m in snapshot)


# ---- 握手 ----
def run_handshake(hub, ws, ip="192.168.1.99"):
    request = SimpleNamespace(transport=FakeTransport(), match_info={},
                              headers={})
    hub._make_ws = lambda: ws
    return asyncio.run(_drive(hub, request))


async def _drive(hub, request):
    result = await hub.vws(request)
    await asyncio.sleep(0.05)     # 让写任务把队列里的东西发出去
    return result


def test_correct_token_gets_registered_and_gets_the_replay():
    hub = make_hub()
    ws = FakeWS(first=auth(), linger=0.05)
    run_handshake(hub, ws)
    assert [name for name, _ in hub.events] == ["viewer_connected",
                                                "viewer_disconnected"]
    assert hub.events[0][1] == {"ip": "192.168.1.99", "count": 1}
    assert reasons(hub) == ["closed"]
    assert ws.sent and ws.sent[0]["type"] == "viewer_hello"


@pytest.mark.parametrize("first,why", [
    (text({"type": "auth", "k": "x" * 43}), "bad_token"),
    (text({"type": "auth"}), "bad_token"),
    (text({"type": "start", "url": "http://x"}), "missing"),
    (SimpleNamespace(type=WSMsgType.TEXT, data="not json at all"), "not_json"),
    (text([1, 2, 3]), "not_json"),
    (SimpleNamespace(type=WSMsgType.BINARY, data=b"\x00"), "bad_type"),
])
def test_a_bad_first_message_is_refused_with_4401(first, why):
    hub = make_hub()
    ws = FakeWS(first=first)
    run_handshake(hub, ws)
    assert ws.sent == [{"type": "viewer_denied", "reason": "token"}]
    assert ws.closed == 4401
    assert hub.events == [("viewer_auth_failed",
                           {"ip": "192.168.1.99", "why": why, "suppressed": 0})]
    assert hub.count == 0


def test_no_first_message_times_out_with_4408():
    hub = make_hub(auth_timeout=0.01)
    ws = FakeWS(hang=True)
    run_handshake(hub, ws)
    assert ws.closed == 4408
    assert [f["why"] for name, f in hub.events if name == "viewer_auth_failed"] \
        == ["timeout"]


def test_a_client_that_leaves_before_authenticating_is_not_an_auth_failure():
    hub = make_hub()
    ws = FakeWS(first=SimpleNamespace(type=WSMsgType.CLOSE, data=None))
    run_handshake(hub, ws)
    assert hub.events == []
    assert ws.sent == []


def test_the_thirteenth_viewer_is_told_the_room_is_full():
    hub = make_hub()

    async def scenario():
        for _ in range(viewer_mod.MAX_VIEWERS):
            add(hub)
        ws = FakeWS(first=auth())
        hub._make_ws = lambda: ws
        await hub.vws(SimpleNamespace(transport=FakeTransport(), match_info={},
                                      headers={}))
        return ws

    ws = asyncio.run(scenario())
    assert ws.sent == [{"type": "viewer_denied", "reason": "full"}]
    assert ws.closed == 4429
    assert "cap" in reasons(hub)
    assert hub.count == viewer_mod.MAX_VIEWERS


def test_inbound_messages_never_reach_on_control():
    """手机端一条都不该发。就算发了，也只计数然后丢弃——绝不解析、绝不转交。

    包含品牌词表这两条本机专属的控制消息：refresh_brands（重新扫描 brands/
    目录）、open_brands_dir（打开词表文件夹）都只该在本机控制面可用，手机
    同看没有理由触发它们——道理和 start/stop/set_engine 完全一样，不需要
    在 ALLOW/DENY 表之外再加一条专门的黑名单，_handle_inbound 本就只认
    「恰好 {type: retranslate, id: …}」这一种形状。"""
    server = StubServer()
    calls = []
    server.on_control = lambda msg: calls.append(msg)
    hub = make_hub(server)
    ws = FakeWS(first=auth(), incoming=[
        text({"type": "start", "url": "http://tiktok.com/@a/live"}),
        text({"type": "stop"}),
        text({"type": "set_engine", "engine": "deepl", "api_key": "k"}),
        text({"type": "refresh_brands"}),
        text({"type": "open_brands_dir"}),
        # 弹幕留意（用户决定 8）：关注名单只在中控电脑上改，手机发这些一律进不来
        text({"type": "guard_mark", "key": "7234567890123456789", "on": True, "nick": "Ana",
              "handle": "ana_demo", "uid_src": "id"}),
        text({"type": "guard_set", "enabled": True}),
        text({"type": "guard_dismiss", "ev_id": "g1"}),
        text({"type": "guard_clear_auto"}),
        text({"type": "guard_reenable"}),
        text({"type": "open_guard_dir"}),
        # 弹幕重译（2026-10-14）：只有本机页面能点，手机发这个一律进不来
        text({"type": "retranslate_comment", "id": "7234567890123456789"}),
        # 字幕记录（含观众弹幕和昵称）：导出和打开文件夹只有中控本机能点
        text({"type": "export_transcript"}),
        text({"type": "open_transcripts_dir"}),
        # 提示条上的 ×（2026-10-21）：关掉时钟跳变那条只有中控本机能点，手机上没有 ×，发来也进不来
        text({"type": "dismiss_incident", "id": "session:clock_gap", "ts": 1790000000.0}),
    ])
    run_handshake(hub, ws)
    assert calls == []


def test_inbound_flood_gets_the_viewer_aborted():
    """滑动窗口而不是绝对上限：窗口内一次性发太多帧（不管是不是能解析）
    照样断——junk 帧也计数，坏页面换个 id 绕不过这道闸。"""
    hub = make_hub()
    flood = [text({"type": "noise", "n": i})
             for i in range(viewer_mod.INBOUND_BURST + 1)]
    ws = FakeWS(first=auth(), incoming=flood)
    run_handshake(hub, ws)
    assert "inbound_flood" in reasons(hub)


def test_a_lone_retranslate_frame_does_not_trip_the_flood_cap():
    calls = []
    hub = make_hub(on_action=lambda a, p, ip: calls.append((a, p, ip)))
    ws = FakeWS(first=auth(), incoming=[text({"type": "retranslate", "id": 7})])
    run_handshake(hub, ws)
    assert calls == [("retranslate", {"id": 7}, "192.168.1.99")]
    assert "inbound_flood" not in reasons(hub)


@pytest.mark.parametrize("bad", [
    {"type": "retranslate"},                       # 缺 id
    {"type": "retranslate", "id": "abc"},           # id 不是数字
    {"type": "retranslate", "id": "1.5"},           # 不是纯数字字符串
    {"type": "retranslate", "id": True},            # bool 是 int 的子类，要挡掉
    {"type": "retranslate", "id": -1},              # 负数：字幕序号不该是负的
    {"type": "retranslate", "id": 1, "extra": 2},   # 多一个键就不是「恰好」了
    {"type": "start", "url": "http://tiktok.com"},  # 完全不相关的类型
    {"type": "retranslate", "id": None},
    "not a dict at all",
    {"type": "refresh_brands"},                     # 本机控制面专属：手机不该碰
    {"type": "open_brands_dir"},                    # 同上
    {"type": "guard_mark", "key": "100", "on": True, "nick": "Ana", "handle": "ana_demo", "uid_src": "id"},
    {"type": "guard_mark", "id": 7},                # 凑成 {type, id} 的形状也不行：类型不是 retranslate
    {"type": "guard_set", "enabled": True},
    {"type": "guard_dismiss", "ev_id": "g1"},
    {"type": "guard_clear_auto"},
    {"type": "guard_reenable"},
    {"type": "open_guard_dir"},
    {"type": "retranslate_comment", "id": "c7"},    # 恰好 {type, id} 的形状也不行：类型不是 retranslate
    {"type": "retranslate_comment", "id": 7},
    {"type": "export_transcript"},                  # 字幕记录：只有本机控制面能导出、能打开文件夹
    {"type": "open_transcripts_dir"},
    {"type": "dismiss_incident", "id": "session:clock_gap", "ts": 1790000000.0},   # 提示条的 ×：只有本机
])
def test_malformed_or_irrelevant_inbound_is_silently_ignored(bad):
    """既不解析出动作，也不回消息、不断连接——坏页面和探测本就不该有回音。"""
    calls = []
    hub = make_hub(on_action=lambda *a: calls.append(a))
    ws = FakeWS(first=auth(), incoming=[text(bad)])
    run_handshake(hub, ws)
    assert calls == []
    assert reasons(hub) == ["closed"]


def test_a_non_text_frame_is_ignored_too():
    calls = []
    hub = make_hub(on_action=lambda *a: calls.append(a))
    ws = FakeWS(first=auth(), incoming=[
        SimpleNamespace(type=WSMsgType.BINARY, data=b"\x00"),
        SimpleNamespace(type=WSMsgType.TEXT, data="{not json"),
    ])
    run_handshake(hub, ws)
    assert calls == []
    assert reasons(hub) == ["closed"]


def test_a_numeric_string_id_is_accepted_like_an_int():
    calls = []
    hub = make_hub(on_action=lambda a, p, ip: calls.append(p["id"]))
    ws = FakeWS(first=auth(), incoming=[text({"type": "retranslate", "id": "42"})])
    run_handshake(hub, ws)
    assert calls == [42]


def test_a_second_rapid_click_is_swallowed_by_the_per_viewer_gap():
    """ACTION_GAP_SEC 之内的第二次点击：静默吞掉，不发送任何东西，不断开。"""
    calls = []
    hub = make_hub(on_action=lambda a, p, ip: calls.append(p["id"]))
    ws = FakeWS(first=auth(), incoming=[
        text({"type": "retranslate", "id": 1}),
        text({"type": "retranslate", "id": 2}),
    ])
    run_handshake(hub, ws)
    assert calls == [1]
    assert reasons(hub) == ["closed"]      # 没有因为超限而被断开


def test_the_per_minute_cap_ignores_further_actions_once_hit():
    """每次都满足单独的冷却间隔，只测每分钟的总量上限。"""
    clock = {"t": 0.0}
    calls = []

    def on_action(action, payload, ip):
        calls.append(payload["id"])
        clock["t"] += viewer_mod.ACTION_GAP_SEC

    hub = make_hub(now=lambda: clock["t"], on_action=on_action)
    messages = [text({"type": "retranslate", "id": i})
                for i in range(viewer_mod.ACTIONS_PER_MINUTE + 1)]
    ws = FakeWS(first=auth(), incoming=messages)
    run_handshake(hub, ws)
    assert calls == list(range(viewer_mod.ACTIONS_PER_MINUTE))


def test_on_action_raising_does_not_break_the_read_loop():
    """同步回调抛异常：吞掉、记日志，绝不炸掉正在读 socket 的循环，也不断连接。"""
    seen = []

    def broken(action, payload, ip):
        seen.append(payload["id"])
        raise RuntimeError("boom")

    hub = make_hub(on_action=broken)
    ws = FakeWS(first=auth(), incoming=[
        text({"type": "retranslate", "id": 1}),
        text({"type": "retranslate", "id": 2}),
    ])
    run_handshake(hub, ws)
    assert seen == [1]                     # 第二条在冷却内本来就会被吞掉
    assert reasons(hub) == ["closed"]       # 正常收尾，不是被断开的
    assert "inbound_flood" not in reasons(hub)


def test_a_coroutine_returned_by_on_action_is_scheduled_not_awaited_inline():
    """回调可能返回协程（真正等强模型的部分）：hub 用 ensure_future 调度它，
    绝不在读循环里 await——那样一台手机的点击就会拖慢识别循环。"""
    ran = []

    async def slow_work():
        await asyncio.sleep(0.01)
        ran.append("done")

    def on_action(action, payload, ip):
        return slow_work()

    async def scenario():
        hub = make_hub(on_action=on_action)
        ws = FakeWS(first=auth(), incoming=[text({"type": "retranslate", "id": 5})])
        request = SimpleNamespace(transport=FakeTransport(), match_info={},
                                  headers={})
        hub._make_ws = lambda: ws
        await hub.vws(request)
        assert hub._action_tasks        # 调度进去了，不是被丢在原地当协程对象
        await asyncio.sleep(0.05)
        return ran

    result = asyncio.run(scenario())
    assert result == ["done"]


def test_pending_connections_are_capped_before_the_upgrade():
    """A1：未鉴权的连接也要有上限，而且要在 prepare 之前就挡住。"""
    from aiohttp import web

    hub = make_hub()
    hub._pending = viewer_mod.MAX_PENDING
    ws = FakeWS(first=auth())
    hub._make_ws = lambda: ws

    async def scenario():
        with pytest.raises(web.HTTPServiceUnavailable):
            await hub.vws(SimpleNamespace(transport=FakeTransport(),
                                          match_info={}, headers={}))

    asyncio.run(scenario())
    assert ws.prepared is False           # 根本没升级成 WebSocket
    assert hub._pending == viewer_mod.MAX_PENDING   # 没占额度，也没多还一个


def test_the_pending_slot_comes_back_after_an_auth_timeout():
    hub = make_hub(auth_timeout=0.01)
    run_handshake(hub, FakeWS(hang=True))
    assert hub._pending == 0


def test_auth_failures_from_one_ip_are_rate_limited():
    """扫端口的人不该能把审计文件刷成几千行同样的记录，但压掉了多少必须记下来。"""
    clock = {"t": 0.0}
    hub = make_hub(now=lambda: clock["t"])
    for _ in range(20):
        hub._auth_failed("10.0.0.5", "bad_token")
    clock["t"] += viewer_mod.AUTH_FAIL_AUDIT_WINDOW_SEC + 1
    hub._auth_failed("10.0.0.5", "bad_token")
    records = [f for name, f in hub.events if name == "viewer_auth_failed"]
    assert len(records) == 2
    assert sum(r["suppressed"] for r in records) == 19
    # token 绝不进审计：只记「第一条消息哪里不对」
    assert all(TOKEN not in str(r) for r in records)
    assert all(r["why"] == "bad_token" for r in records)


def test_auth_failure_tracking_does_not_grow_without_bound():
    clock = {"t": 0.0}
    hub = make_hub(now=lambda: clock["t"])
    for i in range(viewer_mod.AUTH_FAIL_TRACK_MAX + 50):
        clock["t"] += 1
        hub._auth_failed("10.0.{}.{}".format(i // 256, i % 256), "bad_token")
    assert len(hub._auth_fail) <= viewer_mod.AUTH_FAIL_TRACK_MAX


# ---- 轮换与关闭 ----
def test_rotate_kicks_everyone_and_invalidates_the_old_token():
    async def scenario():
        hub = make_hub()
        ws = FakeWS()
        v = add(hub, ws)
        v.writer = asyncio.ensure_future(hub._writer(v))
        hub.rotate("n" * viewer_mod.TOKEN_LEN)
        await asyncio.sleep(0.05)
        return hub, ws

    hub, ws = asyncio.run(scenario())
    assert ws.closed == 4403
    assert hub.count == 0
    assert "kicked" in reasons(hub)
    assert hub._check_auth(auth(TOKEN)) == "bad_token"
    assert hub._check_auth(auth("n" * viewer_mod.TOKEN_LEN)) is None


def test_stop_is_idempotent_and_fanout_stays_quiet_afterwards():
    async def scenario():
        hub = make_hub()
        await hub.start()
        assert hub.running
        v = add(hub)
        v.writer = asyncio.ensure_future(hub._writer(v))
        await hub.stop("off")
        await hub.stop("off")            # 幂等
        await asyncio.sleep(0.05)
        assert hub.running is False
        hub.fanout({"type": "caption", "id": 1})
        return hub, v

    hub, v = asyncio.run(scenario())
    assert hub.count == 0
    assert "kicked" in reasons(hub)


def test_stop_does_not_wait_on_a_phone_that_stopped_reading(monkeypatch):
    """「关闭同看」是中控的动作，不许等任何一台手机：关闭帧发不出去就 abort。
    不这么做的话 runner.cleanup() 会等还在跑的 handler（aiohttp 默认 60 秒），
    界面上那个按钮就卡住了。"""
    monkeypatch.setattr(viewer_mod, "KICK_GRACE_SEC", 0.02)

    class Stuck(FakeWS):
        async def close(self, code=1000, message=b""):
            await asyncio.sleep(30)

    async def scenario():
        hub = make_hub()
        await hub.start()
        ws = Stuck()
        v = add(hub, ws)
        v.writer = asyncio.ensure_future(hub._writer(v))
        await asyncio.sleep(0.02)          # 让写任务把回放发完，卡在 close 上
        await hub.stop("off")
        return hub, v, ws

    hub, v, ws = asyncio.run(scenario())
    assert ws.closed is None               # 关闭帧没发出去
    assert v.transport.aborts >= 1         # 但连接一定断了
    assert hub.count == 0


def test_rotate_aborts_a_connection_that_cannot_be_closed_cleanly(monkeypatch):
    monkeypatch.setattr(viewer_mod, "KICK_GRACE_SEC", 0.02)

    class Stuck(FakeWS):
        async def close(self, code=1000, message=b""):
            await asyncio.sleep(30)

    async def scenario():
        hub = make_hub()
        ws = Stuck()
        v = add(hub, ws)
        v.writer = asyncio.ensure_future(hub._writer(v))
        hub.rotate("n" * viewer_mod.TOKEN_LEN)
        await asyncio.sleep(0.08)
        return v

    v = asyncio.run(scenario())
    assert v.transport.aborts >= 1


def test_rotate_outside_a_loop_still_disconnects(loop):
    """同步路径（没有在跑的事件循环）时当场断，不留一个永远不死的连接。"""
    hub = make_hub()
    v = add(hub)
    hub.rotate("n" * viewer_mod.TOKEN_LEN)
    assert v.transport.aborts == 1
    assert hub.count == 0


def test_start_walks_the_candidate_ports_and_reports_the_last_error():
    """占住一个端口，再让候选列表以它开头：第一个失败要接着试下一个，
    全部失败才把系统的原话抛给调用方（由它报观察并保持关闭）。
    用内核分配的端口而不是 1–3：特权端口这条界线在 Windows 上不成立。"""
    async def scenario():
        held = make_hub()
        await held.start()
        taken = held.port
        try:
            walking = make_hub(ports=(taken, 0))
            await walking.start()
            assert walking.running is True
            assert walking.port not in (None, taken)
            await walking.stop("off")

            doomed = make_hub(ports=(taken,))
            with pytest.raises(OSError):
                await doomed.start()
            assert doomed.running is False
            assert doomed.port == taken       # 报观察时说得出是哪个端口
        finally:
            await held.stop("off")

    asyncio.run(scenario())


def test_a_viewer_message_never_reaches_a_phone_through_the_real_broadcast():
    """端到端：真的 CaptionServer + 真的 hub。viewer 载荷里带着含 token 的 URL。"""
    from app.server import CaptionServer

    async def scenario():
        server = CaptionServer(port=0)
        hub = make_hub(server)
        server.viewer = hub
        v = add(hub)
        drained = v.queue.qsize()
        await server.broadcast({"type": "viewer", "on": True,
                                "url": "http://192.168.1.9:8766/#k=SECRET"})
        assert v.queue.qsize() == drained
        await server.broadcast({"type": "caption", "id": 1, "original": "hola"})
        assert v.queue.qsize() == drained + 1
        return v

    v = asyncio.run(scenario())
    items = []
    while not v.queue.empty():
        items.append(v.queue.get_nowait())
    assert not any("SECRET" in json.dumps(m, ensure_ascii=False) for m in items)


# ---- TCP 这一层的闸（专家审查 S1）----
# 审查实测：一台设备对同看端口挂 150 条连上却一个字节都不发的 TCP 连接，aiohttp 等第一个请求
# 没有期限，程序的 open/spawn/socket 全部 EMFILE、审计 0 条。修法是 _GuardedProtocol：同一个 IP
# 最多 MAX_CONN_PER_IP 条，连上 FIRST_BYTE_TIMEOUT_SEC 秒还没有首字节就断开。计数在 connection_lost
# 里扣（全程代理，审查 Q13）——下面先用替身钉住代理本身，再用真 socket 走一遍。
REPO = Path(__file__).resolve().parent.parent


class GuardTransport(FakeTransport):
    def __init__(self, peer="10.0.0.7"):
        super().__init__(peer)
        self.proto = None

    def abort(self):
        first = not self.is_closing()
        super().abort()
        if first and self.proto is not None:
            self.proto.connection_lost(None)      # 真 transport 会在下一轮循环里调


class RecordingInner:
    """aiohttp RequestHandler 的替身：只记下收到了哪些回调。"""

    def __init__(self, log, eof_result=None, lost_raises=False):
        self.log = log
        self.eof_result = eof_result
        self.lost_raises = lost_raises

    def connection_made(self, transport):
        self.log.append(("made", transport))

    def data_received(self, data):
        self.log.append(("data", data))

    def eof_received(self):
        self.log.append(("eof",))
        return self.eof_result

    def pause_writing(self):
        self.log.append(("pause",))

    def resume_writing(self):
        self.log.append(("resume",))

    def connection_lost(self, exc):
        self.log.append(("lost", exc))
        if self.lost_raises:
            raise RuntimeError("aiohttp blew up")


def guarded(gate, factory, peer="10.0.0.7"):
    transport = GuardTransport(peer)
    proto = viewer_mod._GuardedProtocol(factory, gate)
    transport.proto = proto
    proto.connection_made(transport)
    return proto, transport


def test_the_guard_forwards_every_callback_and_releases_the_count_in_connection_lost():
    """六个回调一个不落地转给 aiohttp 的协议，eof_received 的返回值原样交回（它决定半关还是
    整条关）。计数在 connection_lost 里扣——不是在数据开始流动、也不是在升级 WebSocket 时。"""
    gate = viewer_mod._ConnGate()
    log = []

    async def scenario():
        proto, transport = guarded(gate, lambda: RecordingInner(log, eof_result=True))
        assert gate.by_ip == {"10.0.0.7": 1} and gate.total == 1
        proto.data_received(b"GET / HTTP/1.1\r\n")
        proto.pause_writing()
        proto.resume_writing()
        proto.data_received(b"\x81\x05hello")         # 升级之后的帧走的是同一个回调
        assert proto.eof_received() is True
        assert gate.total == 1                          # 数据流动不扣
        exc = ConnectionResetError("peer went away")
        proto.connection_lost(exc)
        assert gate.by_ip == {} and gate.total == 0 and not gate.live
        return transport

    transport = asyncio.run(scenario())
    assert log == [("made", transport), ("data", b"GET / HTTP/1.1\r\n"), ("pause",), ("resume",),
                   ("data", b"\x81\x05hello"), ("eof",), ("lost", log[-1][1])]
    assert isinstance(log[-1][1], ConnectionResetError)
    assert transport.aborts == 0


def test_connections_beyond_the_per_ip_cap_are_cut_before_aiohttp_sees_them():
    """第 17 条：立即断开，内层协议根本不造（aiohttp 的连接表里不会多一条）。别的 IP 不受影响；
    断掉一条之后同一个 IP 又能进来一条。"""
    gate = viewer_mod._ConnGate()
    made = []

    def factory():
        made.append(1)
        return RecordingInner([])

    async def scenario():
        kept = [guarded(gate, factory) for _ in range(viewer_mod.MAX_CONN_PER_IP)]
        _, extra = guarded(gate, factory)
        assert extra.aborts == 1
        assert len(made) == viewer_mod.MAX_CONN_PER_IP
        assert gate.by_ip == {"10.0.0.7": viewer_mod.MAX_CONN_PER_IP}
        assert gate.rejected == 1
        _, other = guarded(gate, factory, peer="10.0.0.8")
        assert other.aborts == 0 and gate.by_ip["10.0.0.8"] == 1
        kept[0][0].connection_lost(None)
        _, again = guarded(gate, factory)
        assert again.aborts == 0
        assert gate.by_ip["10.0.0.7"] == viewer_mod.MAX_CONN_PER_IP
        for proto, _ in kept[1:]:
            proto.connection_lost(None)

    asyncio.run(scenario())


def test_the_count_is_released_even_when_aiohttps_connection_lost_raises():
    gate = viewer_mod._ConnGate()

    async def scenario():
        proto, _ = guarded(gate, lambda: RecordingInner([], lost_raises=True))
        with pytest.raises(RuntimeError):
            proto.connection_lost(None)
        assert gate.total == 0 and not gate.live

    asyncio.run(scenario())


def test_a_connection_that_never_sends_a_byte_is_cut_after_the_first_byte_timeout(monkeypatch):
    monkeypatch.setattr(viewer_mod, "FIRST_BYTE_TIMEOUT_SEC", 0.05)
    gate = viewer_mod._ConnGate()

    async def scenario():
        silent, silent_tr = guarded(gate, lambda: RecordingInner([]))
        talker, talker_tr = guarded(gate, lambda: RecordingInner([]), peer="10.0.0.9")
        talker.data_received(b"G")                    # 一个字节就够：期限只管「一声不吭」
        await asyncio.sleep(0.2)
        assert silent_tr.aborts == 1
        assert talker_tr.aborts == 0
        assert gate.idle_closed == 1
        assert gate.by_ip == {"10.0.0.9": 1}
        talker.connection_lost(None)

    asyncio.run(scenario())


def test_the_defaults_match_the_plan():
    assert viewer_mod.MAX_CONN_PER_IP == 16
    assert viewer_mod.FIRST_BYTE_TIMEOUT_SEC == 5.0


def test_a_flood_of_rejections_leaves_one_terminal_line_per_window():
    clock = {"t": 0.0}
    lines = []
    gate = viewer_mod._ConnGate(log=lines.append, now=lambda: clock["t"])
    for _ in range(viewer_mod.MAX_CONN_PER_IP + 50):
        gate.admit("10.0.0.5")
    assert gate.rejected == 50
    assert len(lines) == 1 and lines[0].startswith("[同看]") and "10.0.0.5" in lines[0]
    clock["t"] += viewer_mod.AUTH_FAIL_AUDIT_WINDOW_SEC + 1
    gate.admit("10.0.0.5")
    assert len(lines) == 2


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("条件在 {} 秒内没有成立".format(timeout))
        await asyncio.sleep(0.02)


async def _eof_within(reader, timeout):
    """对端关了（读到 EOF 或被重置）返回 True；期限内还开着返回 False。"""
    try:
        data = await asyncio.wait_for(reader.read(1), timeout)
    except asyncio.TimeoutError:
        return False
    except (ConnectionError, OSError):
        return True
    return data == b""


def test_real_idle_sockets_beyond_the_cap_are_cut_at_once_and_the_rest_after_the_timeout(
        monkeypatch):
    """真 socket、真 hub（只绑 127.0.0.1）：超过上限的立即断开，剩下的到期一起断开，计数归零。"""
    monkeypatch.setattr(viewer_mod, "FIRST_BYTE_TIMEOUT_SEC", 2.0)
    extra = 8

    async def scenario():
        hub = make_hub()
        await hub.start()
        pairs = []
        try:
            pairs = await asyncio.gather(*[asyncio.open_connection("127.0.0.1", hub.port)
                                           for _ in range(viewer_mod.MAX_CONN_PER_IP + extra)])
            await _until(lambda: hub._gate.rejected == extra)
            assert hub.open_connections == viewer_mod.MAX_CONN_PER_IP
            closed = await asyncio.gather(*[_eof_within(r, 0.3) for r, _ in pairs])
            assert sum(closed) == extra                 # 多出来的那几条已经被断开
            await _until(lambda: hub.open_connections == 0)
            assert hub._gate.idle_closed == viewer_mod.MAX_CONN_PER_IP
            assert hub._gate.by_ip == {}
            closed = await asyncio.gather(*[_eof_within(r, 1.0) for r, _ in pairs])
            assert all(closed)
        finally:
            for _, writer in pairs:
                writer.close()
            await hub.stop("off")

    asyncio.run(scenario())


def test_a_keep_alive_connection_that_already_sent_a_request_outlives_the_first_byte_timeout(
        monkeypatch):
    """首字节期限只管「一声不吭」的连接。手机浏览器发完请求会把连接留着复用（keep-alive），
    这种连接过了期限也不该被断。"""
    monkeypatch.setattr(viewer_mod, "FIRST_BYTE_TIMEOUT_SEC", 0.3)

    async def scenario():
        hub = make_hub()
        await hub.start()
        writer = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", hub.port)
            writer.write(b"GET /v/viewer.css HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), 5.0)
            assert status.startswith(b"HTTP/1.1 200")
            await asyncio.sleep(viewer_mod.FIRST_BYTE_TIMEOUT_SEC + 0.5)
            assert hub.open_connections == 1
            assert hub._gate.idle_closed == 0
        finally:
            if writer is not None:
                writer.close()
            await hub.stop("off")
        await _until(lambda: hub.open_connections == 0)

    asyncio.run(scenario())


def test_a_real_phone_flows_through_the_guard_and_the_count_returns_to_zero():
    """WebSocket 升级之后的数据路径（审查 Q13）：手机页、鉴权、回放、实时字幕、手机发「重译」、
    断开——全程经过代理协议，断开后按 IP 的计数归零。aiohttp 换版本时这一条最先红。"""
    actions = []

    async def scenario():
        server = StubServer(status={"state": "live"})
        hub = make_hub(server, on_action=lambda a, p, ip: actions.append((p["id"], ip)))
        await hub.start()
        base = "http://127.0.0.1:{}".format(hub.port)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(base + "/v/viewer.css") as resp:
                    assert resp.status == 200
                    await resp.read()
                async with session.ws_connect(base + "/vws") as ws:
                    await ws.send_json({"type": "auth", "k": TOKEN})
                    hello = await asyncio.wait_for(ws.receive_json(), 5.0)
                    assert hello["type"] == "viewer_hello"
                    await _until(lambda: hub.count == 1)
                    hub.fanout({"type": "caption", "id": 9, "original": "hola"})
                    while True:
                        msg = await asyncio.wait_for(ws.receive_json(), 5.0)
                        if msg.get("type") == "caption" and msg.get("id") == 9:
                            break
                    await ws.send_json({"type": "retranslate", "id": 9})
                    await _until(lambda: actions == [(9, "127.0.0.1")])
                    assert 1 <= hub.open_connections <= 2     # /vws + 也许还留着的 keep-alive
                await _until(lambda: hub.count == 0)
            await _until(lambda: hub.open_connections == 0)
            assert hub._gate.by_ip == {}
        finally:
            await hub.stop("off")

    asyncio.run(scenario())
    assert actions == [(9, "127.0.0.1")]


def test_stop_cuts_connections_that_never_sent_anything(monkeypatch):
    """关同看时，一声不吭的连接不能留在端口上等期限。"""
    async def scenario():
        hub = make_hub()
        await hub.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", hub.port)
        try:
            await _until(lambda: hub.open_connections == 1)
            started = time.monotonic()
            await hub.stop("off")
            assert time.monotonic() - started < 3.0
            assert await _eof_within(reader, 2.0)
            await _until(lambda: hub.open_connections == 0)
        finally:
            writer.close()

    asyncio.run(scenario())


def test_stop_cuts_what_runner_cleanup_left_behind():
    """stop() 末尾的兜底（_gate.abort_all）：aiohttp 的 runner.cleanup() 自己会关它连接表里的每一条，
    这里让它什么都不做，模拟它出错或漏下的情况。空连接照样要在关同看时断掉，不等 5 秒的首字节期限。"""
    async def scenario():
        hub = make_hub()
        await hub.start()
        runner = hub._runner           # AppRunner 有 __slots__，方法换不掉：换掉 hub 手里的这一个

        async def leaves_everything():
            return None

        hub._runner = SimpleNamespace(cleanup=leaves_everything)
        reader, writer = await asyncio.open_connection("127.0.0.1", hub.port)
        try:
            await _until(lambda: hub.open_connections == 1)
            await hub.stop("off")
            assert viewer_mod.FIRST_BYTE_TIMEOUT_SEC > 2.0     # 下面 2 秒内断开的不是首字节期限
            assert await _eof_within(reader, 2.0)
            await _until(lambda: hub.open_connections == 0)
        finally:
            writer.close()
            await runner.cleanup()

    asyncio.run(scenario())


# 子进程：把软上限压到 256（本机从 Finder 启动的 .app 拿到的就是这个数），起真 hub（只绑 127.0.0.1），
# 按 stdin 的指令报告「现在还能不能开文件、开 socket、起子进程」。攻击方是另一个进程（它自己的上限
# 抬高，不让它成为瓶颈），挂的空连接比 256 还多——修之前服务端会先把自己的名额耗光。
_SERVER = r'''
import asyncio, json, resource, socket, subprocess, sys, tempfile
soft, idle, token = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
from app import viewer
viewer.FIRST_BYTE_TIMEOUT_SEC = idle

class FS:
    config = {"status": {"state": "live"}}; alerts = []; history = []; comments = []

def probe():
    out = {}
    try:
        tempfile.TemporaryFile().close(); out["open"] = "ok"
    except OSError as e:
        out["open"] = "ERR%d" % e.errno
    try:
        socket.socket().close(); out["socket"] = "ok"
    except OSError as e:
        out["socket"] = "ERR%d" % e.errno
    try:
        subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=30); out["spawn"] = "ok"
    except OSError as e:
        out["spawn"] = "ERR%d" % e.errno
    return out

async def main():
    hub = viewer.ViewerHub(FS(), ports=[0], token=token, bind_host="127.0.0.1",
                           log=lambda *a: None)
    await hub.start()
    print(json.dumps({"port": hub.port}), flush=True)
    loop = asyncio.get_running_loop()
    while True:
        line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
        if line != "state":
            break
        print(json.dumps(dict(probe(), conns=hub.open_connections, rejected=hub._gate.rejected,
                              idle_closed=hub._gate.idle_closed, viewers=hub.count)), flush=True)
    await hub.stop("off")

asyncio.run(main())
'''

_ATTACKER = r'''
import resource, socket, sys
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
want = 4096 if hard == resource.RLIM_INFINITY or hard < 0 else min(4096, hard)
if soft != resource.RLIM_INFINITY and soft < want:
    resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
port, n = int(sys.argv[1]), int(sys.argv[2])
socks = []
for _ in range(n):
    try:
        socks.append(socket.create_connection(("127.0.0.1", port), timeout=3))
    except OSError:
        break
print(len(socks), flush=True)
sys.stdin.readline()
closed = 0
for s in socks:
    s.setblocking(False)
    try:
        if s.recv(1) == b"":
            closed += 1
    except BlockingIOError:
        pass
    except OSError:
        closed += 1
print(closed, flush=True)
'''


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 没有 RLIMIT_NOFILE")
def test_idle_sockets_cannot_exhaust_file_handles_under_a_256_limit():
    soft, idle, attack = 256, 2.0, 300
    server = subprocess.Popen([sys.executable, "-c", _SERVER, str(soft), str(idle), TOKEN],
                              cwd=str(REPO), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True)
    attacker = None

    def ask():
        server.stdin.write("state\n")
        server.stdin.flush()
        return json.loads(server.stdout.readline())

    try:
        port = json.loads(server.stdout.readline())["port"]
        attacker = subprocess.Popen([sys.executable, "-c", _ATTACKER, str(port), str(attack)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        holding = int(attacker.stdout.readline())
        if holding <= soft:
            pytest.skip("攻击方只开出了 {} 条连接，没超过服务端的上限 {}，这台机器上测不出来".format(
                holding, soft))
        state = ask()
        assert state["conns"] <= viewer_mod.MAX_CONN_PER_IP
        assert (state["open"], state["socket"], state["spawn"]) == ("ok", "ok", "ok"), state
        # 到期之后轮询，不固定睡 idle+1 秒：服务端是另一个进程，慢跑器上它的计时器可能晚一点才轮到
        time.sleep(idle)
        deadline = time.monotonic() + 10.0
        while True:
            state = ask()
            if (state["conns"] == 0
                    and state["idle_closed"] == viewer_mod.MAX_CONN_PER_IP
                    and state["rejected"] == holding - viewer_mod.MAX_CONN_PER_IP) \
                    or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        assert state["conns"] == 0
        assert state["idle_closed"] == viewer_mod.MAX_CONN_PER_IP
        assert state["rejected"] == holding - viewer_mod.MAX_CONN_PER_IP
        assert (state["open"], state["socket"], state["spawn"]) == ("ok", "ok", "ok"), state
        attacker.stdin.write("report\n")
        attacker.stdin.flush()
        assert int(attacker.stdout.readline()) == holding     # 一条不剩，全被断开了

        async def phone():
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect("http://127.0.0.1:{}/vws".format(port)) as ws:
                    await ws.send_json({"type": "auth", "k": TOKEN})
                    return await asyncio.wait_for(ws.receive_json(), 5.0)

        assert asyncio.run(phone())["type"] == "viewer_hello"   # 合法手机照样连得上
    finally:
        for proc in (attacker, server):
            if proc is None:
                continue
            try:
                proc.stdin.close()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
                proc.wait(timeout=10)


# ---- 未鉴权的 /vws 占位（专家审查 S6）----
def test_the_auth_timeout_is_two_seconds():
    """原来 5 秒：一个 IP 挂 32 个不发 auth 的连接、到期重开，合法手机 20 次只进来 2 次。"""
    assert viewer_mod.AUTH_TIMEOUT_SEC == 2.0
    assert make_hub().auth_timeout == 2.0
    assert viewer_mod.MAX_PENDING_PER_IP == 4
    # 单 IP 的 TCP 上限（S1）要容得下单 IP 未鉴权上限加一台手机正常的那几条（浏览器最多并行 6 条）
    assert viewer_mod.MAX_CONN_PER_IP > viewer_mod.MAX_PENDING_PER_IP + 6


def test_one_address_squatting_32_unauthenticated_sockets_cannot_lock_out_another_phone():
    """同一个 IP 开 32 个不发 auth 的连接：前 4 个占着位，其余直接 503、不占全局名额；
    另一个 IP 带正确口令照样进来。占位的人走了之后计数归零。"""
    from aiohttp import web

    async def scenario():
        hub = make_hub()
        outcomes = []

        async def squat():
            try:
                await hub.vws(SimpleNamespace(transport=FakeTransport("10.0.0.66"),
                                              match_info={}, headers={}))
                outcomes.append("held")
            except web.HTTPServiceUnavailable:
                outcomes.append("503")

        hub._make_ws = lambda: FakeWS(hang=True)
        tasks = [asyncio.ensure_future(squat()) for _ in range(32)]
        await _until(lambda: len(outcomes) >= 32 - viewer_mod.MAX_PENDING_PER_IP, timeout=2.0)
        assert outcomes.count("503") == 32 - viewer_mod.MAX_PENDING_PER_IP
        assert hub._pending == viewer_mod.MAX_PENDING_PER_IP      # 503 的那些没占全局名额
        assert hub._unauth == {"10.0.0.66": viewer_mod.MAX_PENDING_PER_IP}

        phone = FakeWS(first=auth(), linger=0.05)
        hub._make_ws = lambda: phone
        await hub.vws(SimpleNamespace(transport=FakeTransport("10.0.0.20"), match_info={},
                                      headers={}))
        assert [f["ip"] for name, f in hub.events if name == "viewer_connected"] \
            == ["10.0.0.20"]
        assert phone.sent and phone.sent[0]["type"] == "viewer_hello"
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert hub._pending == 0
        assert hub._unauth == {}

    asyncio.run(scenario())


def test_the_per_address_slot_is_given_back_after_auth_and_after_a_refusal():
    hub = make_hub()
    run_handshake(hub, FakeWS(first=auth(), linger=0.01))
    assert hub._unauth == {}
    run_handshake(hub, FakeWS(first=auth("x" * viewer_mod.TOKEN_LEN)))
    assert hub._unauth == {}
    hub = make_hub(auth_timeout=0.01)
    run_handshake(hub, FakeWS(hang=True))
    assert hub._unauth == {} and hub._pending == 0


def test_an_authenticated_phone_does_not_hold_its_address_s_unauthenticated_slot():
    """已经在看的手机不占它这个 IP 的未鉴权名额：同一个 NAT 后面的几台手机陆续连进来，
    在看的人再多也不会把后来的挡在 503 外面。

    「陆续」要在测试里做出来：上一台鉴权入册了才连下一台。同一时刻还没发 auth 的超过
    MAX_PENDING_PER_IP 条，本来就该 503（上面占位那条钉着）。原来一次起 6 条、固定等 0.1 秒，
    3.12 起能过只是因为 wait_for 里的 receive 不挂起就不让出，6 条碰巧排成了先后；3.9 的
    wait_for 总要让出一次，6 条同时处在未鉴权，后 2 条 503（PR #122 的 CI）。"""
    async def scenario():
        hub = make_hub()
        release = asyncio.Event()

        class WatchingWS(FakeWS):
            async def __anext__(self):
                await release.wait()           # 一直在看，测试放手才走
                raise StopAsyncIteration

        sockets = [WatchingWS(first=auth()) for _ in range(viewer_mod.MAX_PENDING_PER_IP + 2)]
        made = iter(sockets)
        hub._make_ws = lambda: next(made)
        tasks = []
        for watching in range(1, len(sockets) + 1):
            task = asyncio.ensure_future(hub.vws(SimpleNamespace(
                transport=FakeTransport("10.0.0.30"), match_info={}, headers={})))
            tasks.append(task)
            await _until(lambda n=watching, t=task: hub.count == n or t.done(), timeout=2.0)
            assert not task.done(), task.exception()       # 被 503 挡掉的话 task 已经结束
            assert hub.count == watching
            assert hub._unauth == {}
        release.set()
        await asyncio.gather(*tasks)
        assert hub.count == 0 and hub._unauth == {} and hub._pending == 0

    asyncio.run(scenario())


def test_a_real_socket_that_never_authenticates_is_closed_after_about_two_seconds():
    async def scenario():
        hub = make_hub()
        await hub.start()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                        "http://127.0.0.1:{}/vws".format(hub.port)) as ws:
                    started = time.monotonic()
                    while True:
                        msg = await asyncio.wait_for(ws.receive(), 10.0)
                        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                            break
                    elapsed = time.monotonic() - started
            assert ws.close_code == 4408
            assert 1.5 <= elapsed < 4.0, elapsed
        finally:
            await hub.stop("off")

    asyncio.run(scenario())


# ---- 所有观众共用的「重译」令牌桶（专家审查 S7）----
def test_phones_taking_turns_to_reconnect_share_one_bucket():
    """每台手机自己的限额挂在连接上、重连就清零；共用的桶挂在 hub 上：8 台手机（或 1 台
    重连 8 次）同一时刻各点一次，只放进 ACTION_BUCKET_BURST 次；之后每 3 秒补 1 次。"""
    clock = {"t": 0.0}
    calls = []
    hub = make_hub(now=lambda: clock["t"], on_action=lambda a, p, ip: calls.append(p["id"]))
    for i in range(8):
        run_handshake(hub, FakeWS(first=auth(), incoming=[text({"type": "retranslate", "id": i})]))
    assert calls == list(range(viewer_mod.ACTION_BUCKET_BURST))
    clock["t"] += 60.0 / viewer_mod.ACTION_BUCKET_PER_MINUTE      # 3 秒：补 1 个
    for i in range(10, 13):
        run_handshake(hub, FakeWS(first=auth(), incoming=[text({"type": "retranslate", "id": i})]))
    assert calls == list(range(viewer_mod.ACTION_BUCKET_BURST)) + [10]
    clock["t"] += 3600.0                                            # 歇很久也只攒满 BURST 个
    for i in range(20, 30):
        run_handshake(hub, FakeWS(first=auth(), incoming=[text({"type": "retranslate", "id": i})]))
    assert calls[-viewer_mod.ACTION_BUCKET_BURST:] == list(range(20, 20 + viewer_mod.ACTION_BUCKET_BURST))
    assert len(calls) == 2 * viewer_mod.ACTION_BUCKET_BURST + 1


def test_the_shared_bucket_caps_a_sustained_rate_at_twenty_a_minute(loop):
    """持续点：每台手机各自守着 3 秒间隔、每分钟 10 次，三台手机合起来一分钟也只放进 20 次左右
    （开头突发的 5 次 + 每 3 秒 1 次）。"""
    clock = {"t": 0.0}
    calls = []
    hub = make_hub(now=lambda: clock["t"], on_action=lambda a, p, ip: calls.append(clock["t"]))
    phones = [hub.new_viewer(FakeWS(), FakeTransport(), "10.0.0.{}".format(i)) for i in range(3)]
    for step in range(60):                    # 60 秒，每秒一台手机点一次
        clock["t"] = float(step)
        hub._handle_inbound(phones[step % 3], text({"type": "retranslate", "id": step}))
    assert len(calls) <= viewer_mod.ACTION_BUCKET_BURST + viewer_mod.ACTION_BUCKET_PER_MINUTE
    assert len(calls) >= viewer_mod.ACTION_BUCKET_PER_MINUTE


def test_a_click_swallowed_by_a_phones_own_cooldown_does_not_spend_a_shared_token(loop):
    clock = {"t": 0.0}
    calls = []
    hub = make_hub(now=lambda: clock["t"], on_action=lambda a, p, ip: calls.append(p["id"]))
    fast = hub.new_viewer(FakeWS(), FakeTransport(), "10.0.0.1")
    for i in range(6):                       # 同一台手机 0 秒内连点 6 下：只有第一下算
        hub._handle_inbound(fast, text({"type": "retranslate", "id": i}))
    others = [hub.new_viewer(FakeWS(), FakeTransport(), "10.0.0.{}".format(i + 2))
              for i in range(viewer_mod.ACTION_BUCKET_BURST)]
    for i, phone in enumerate(others):
        hub._handle_inbound(phone, text({"type": "retranslate", "id": 100 + i}))
    # 桶里 5 个：快手机用掉 1 个，另外 4 台各 1 个，第 5 台被桶挡住
    assert calls == [0, 100, 101, 102, 103]
    # 被桶挡住的那台不该因此进冷却：3 秒后桶补了 1 个，它能点
    clock["t"] += viewer_mod.ACTION_GAP_SEC
    hub._handle_inbound(others[-1], text({"type": "retranslate", "id": 200}))
    assert calls[-1] == 200
