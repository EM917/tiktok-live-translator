"""ViewerHub：分发、背压、注册顺序、鉴权、轮换。

这个文件里最重要的一条是「fanout 是同步的」（test_fanout_is_synchronous）。
业务前提：漏报警的代价远高于误报，而识别循环 await 的正是报警广播——观众侧
只要有一个 await，一台卡住的手机就变成识别延迟，也就变成漏报。所以宁可断开
手机（它会重连并拿到完整回放），绝不让它进关键路径。
"""
import asyncio
import json
from types import SimpleNamespace

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
    def __init__(self):
        self.aborts = 0
        self.closes = 0

    def abort(self):
        self.aborts += 1

    def close(self):
        self.closes += 1

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return ("192.168.1.99", 51515)
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
