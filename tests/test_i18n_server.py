"""英文界面的服务端出口（spec §2.2、§2.4、§7）：桌面广播、hello 与回放、桌面首页、手机页与
手机消息的派生英文字段，以及设置里换界面语言那一支。

两件事贯穿全文件：
1. 闸关着（且没有一次性覆盖）时，一切与改造前逐字节相同——广播发出去的就是原消息本身，
   首页和手机页还是文件本身，手机消息一个字段都不多。
2. 缓存（config / alerts / history）里存的是原消息（界面文字是 Bi），只在发往界面的那一刻
   按「此刻」的语言渲染，所以切换语言后重连的页面拿到的是新语言。

这里的中文夹具都写成 L()：这批提交里生产代码还没有迁移，句子由测试造；数据全用 ASCII。
"""
import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web

from app import i18n, viewer
from app.i18n import CJK, L
from app.server import WEB_DIR, CaptionServer
from app.viewer import ViewerHub, replay_snapshot
from tests.helpers import run

SLEPT = L("电脑休眠过 {} 秒", "The computer was asleep for {} sec").format(30)
CONNECTING = L("正在连接…", "Connecting…")
WHY = L("超时", "Timed out")


class FakeClient:
    """本机页面替身：记下 send_json 收到的对象本身（不是副本），好判断是不是同一个。"""

    def __init__(self):
        self.got = []

    async def send_json(self, msg):
        self.got.append(msg)


class FakeHub:
    def __init__(self):
        self.calls = []

    def fanout(self, msg):
        self.calls.append(msg)


def make_server():
    server = CaptionServer(port=0)
    client = FakeClient()
    server.clients.add(client)
    server._transports[client] = None
    server.viewer = FakeHub()
    return server, client


async def _broadcast_samples(server):
    await server.status("connecting", CONNECTING)
    await server.broadcast({"type": "incident", "id": "session:sleep", "level": "warn",
                            "text": SLEPT, "ts": 1.0})
    await server.broadcast({"type": "alert", "alert_id": 7, "term": "colageno", "why": WHY,
                            "context": "hola"})
    await server.broadcast({"type": "caption", "id": 3, "original": "hola", "why": WHY})
    await server.broadcast({"type": "comment", "id": "c1", "user": "ana", "text": "hola"})


# ---- 桌面广播 ---------------------------------------------------------------------------

def test_in_chinese_the_desktop_gets_the_very_same_message_object():
    """中文时 render 原样返回：发给页面的就是原消息本身，没有复制、没有改写。"""
    server, client = make_server()
    msg = {"type": "incident", "id": "session:sleep", "level": "warn", "text": SLEPT, "ts": 1.0}
    run(server.broadcast(msg))
    assert client.got == [msg] and client.got[0] is msg
    assert server.viewer.calls[0] is msg


def test_in_english_the_desktop_gets_english_but_caches_and_the_phone_keep_the_pair():
    server, client = make_server()
    with i18n.use("en"):
        run(_broadcast_samples(server))
    status, incident, alert, caption, comment = client.got
    assert status["detail"] == "Connecting…"
    assert incident["text"] == "The computer was asleep for 30 sec"
    assert alert["why"] == "Timed out" and caption["why"] == "Timed out"
    assert comment == {"type": "comment", "id": "c1", "user": "ana", "text": "hola"}
    for got in client.got:
        assert not [v for v in got.values() if isinstance(v, i18n.Bi)]
    # 缓存里存的是原消息：hello 时按那时的语言现渲染
    cached = server.config["incidents"]["session:sleep"]["text"]
    assert isinstance(cached, i18n.Bi) and cached == "电脑休眠过 30 秒"
    assert isinstance(server.config["status"]["detail"], i18n.Bi)
    assert isinstance(server.alerts[-1]["why"], i18n.Bi)
    assert isinstance(server.history[-1]["why"], i18n.Bi)
    # 手机拿的也是原消息（带 Bi），由 filter_payload 自己派生英文
    assert [m["type"] for m in server.viewer.calls] == [m["type"] for m in client.got]
    assert server.viewer.calls[0]["detail"] is CONNECTING
    assert server.viewer.calls[1]["text"] is SLEPT


def test_hello_and_every_replay_follow_the_language_at_connect_time():
    """同一份缓存：中文时连上来拿到中文，换成英文后重连拿到英文（spec §2.4）。"""
    from aiohttp.test_utils import TestClient, TestServer

    async def connect(server):
        app = web.Application()
        app.router.add_get("/ws", server._ws)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            got = [await ws.receive_json(timeout=5) for _ in range(4)]
            await ws.close()
            return got
        finally:
            await client.close()

    async def scenario():
        server = CaptionServer(port=0)
        await _broadcast_samples(server)          # 没有页面连着：只进缓存
        zh = await connect(server)
        with i18n.use("en"):
            en = await connect(server)
        return zh, en

    zh, en = asyncio.run(scenario())
    hello, alert, caption, comment = en
    assert hello["type"] == "hello"
    assert hello["config"]["status"]["detail"] == "Connecting…"
    assert hello["config"]["incidents"]["session:sleep"]["text"] == \
        "The computer was asleep for 30 sec"
    assert alert["why"] == "Timed out" and alert["replay"] is True
    assert caption["why"] == "Timed out" and caption["restore"] is True
    assert comment["user"] == "ana" and comment["replay"] is True
    assert zh[0]["config"]["status"]["detail"] == "正在连接…"
    assert zh[0]["config"]["incidents"]["session:sleep"]["text"] == "电脑休眠过 30 秒"
    assert zh[1]["why"] == "超时" and zh[2]["why"] == "超时"
    # 中英两次的数据字段一模一样，只有界面文字不同
    assert zh[3] == en[3]


# ---- 桌面首页 ---------------------------------------------------------------------------

def test_the_desktop_page_is_the_file_itself_in_chinese():
    resp = run(CaptionServer(port=0)._index(None))
    assert isinstance(resp, web.FileResponse)
    assert resp._path == WEB_DIR / "index.html"


def test_the_desktop_page_in_english_only_changes_the_html_tag():
    with i18n.use("en"):
        resp = run(CaptionServer(port=0)._index(None))
    assert not isinstance(resp, web.FileResponse)
    assert resp.content_type == "text/html" and resp.charset == "utf-8"
    body = resp.text
    original = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    # 只换 <html> 开标签，别的一个字节不动；桌面页不加 data-i18n（那是手机页的闸标记）
    assert body == original.replace('<html lang="zh-CN">', '<html lang="en">', 1)
    assert body.count('<html lang="en">') == 1 and "data-i18n" not in body


# ---- 手机页 -----------------------------------------------------------------------------

def _phone_hub():
    return ViewerHub(SimpleNamespace(config={}, history=[], alerts=[], comments=[]),
                     ports=[0], token="t" * viewer.TOKEN_LEN, bind_host="127.0.0.1",
                     web_dir=WEB_DIR, log=lambda *a: None)


def _page(hub, accept=None):
    headers = {} if accept is None else {"Accept-Language": accept}
    return run(hub.page(SimpleNamespace(match_info={}, headers=headers, transport=None)))


def test_with_the_gate_closed_the_phone_page_is_the_file_itself():
    """闸关着：不看 Accept-Language，手机页与改造前逐字节相同，也没有 data-i18n 标记——
    viewer.js 见不到标记就不读本机的语言选择、不显示切换（spec §7.1）。"""
    assert not i18n.enabled()
    for accept in (None, "en-US,en;q=0.9", "zh-CN"):
        resp = _page(_phone_hub(), accept)
        assert isinstance(resp, web.FileResponse)


@pytest.mark.parametrize("accept,tag", [
    ("en-US,en;q=0.9", '<html lang="en" data-i18n="on">'),
    ("zh-CN,zh;q=0.9,en;q=0.8", '<html lang="zh-CN" data-i18n="on">'),
    ("fr;q=0.4,zh-TW;q=0.9", '<html lang="zh-CN" data-i18n="on">'),
    (None, '<html lang="zh-CN" data-i18n="on">'),
])
def test_when_enabled_the_phone_page_follows_the_phones_language(accept, tag):
    """双语机制生效时（闸开着或一次性覆盖）按手机自己的语言定，与桌面用的语言无关。"""
    with i18n.use("zh"):                          # 桌面是中文，手机照样按自己的语言
        resp = _page(_phone_hub(), accept)
    assert resp.content_type == "text/html" and resp.charset == "utf-8"
    original = (WEB_DIR / "viewer.html").read_text(encoding="utf-8")
    assert resp.text == original.replace('<html lang="zh-CN">', tag, 1)


def test_the_phone_page_keeps_its_security_headers_when_rendered():
    hub = _phone_hub()
    guard = list(hub.build_app().middlewares)[0]
    request = SimpleNamespace(match_info={}, headers={"Accept-Language": "en"}, transport=None)
    with i18n.use("en"):
        resp = run(guard(request, hub.page))
    assert resp.headers["Content-Security-Policy"] == viewer.CSP
    assert "unsafe-inline" not in resp.headers["Content-Security-Policy"]


# ---- 手机消息 ---------------------------------------------------------------------------

def test_the_phone_replay_carries_both_languages_only_when_enabled():
    server = CaptionServer(port=0)
    run(_broadcast_samples(server))

    def by_type(items):
        return {m["type"]: m for m in items}

    closed = by_type(replay_snapshot(server))
    assert "text_en" not in closed["incident"] and "why_en" not in closed["alert"]
    assert closed["incident"]["text"] == "电脑休眠过 30 秒"
    with i18n.use("en"):
        opened = by_type(replay_snapshot(server))
    assert opened["incident"]["text"] == "电脑休眠过 30 秒"
    assert opened["incident"]["text_en"] == "The computer was asleep for 30 sec"
    assert opened["alert"]["why_en"] == "Timed out"
    assert opened["caption"]["why_en"] == "Timed out"
    assert "detail" not in opened["status"]           # status 的文字本来就不给手机


def test_live_fanout_shares_one_payload_with_both_languages():
    """所有手机共享同一份载荷，各按自己的语言挑；派生字段每条消息只算一次。"""
    async def scenario():
        hub = _phone_hub()
        first = hub.new_viewer(SimpleNamespace(), None, "192.168.1.5")
        second = hub.new_viewer(SimpleNamespace(), None, "192.168.1.6")
        hub.register(first)
        hub.register(second)
        for v in (first, second):
            while not v.queue.empty():
                v.queue.get_nowait()                  # 回放那一串
        with i18n.use("en"):
            hub.fanout({"type": "incident", "id": "x", "level": "warn", "text": SLEPT})
        return first.queue.get_nowait(), second.queue.get_nowait()

    a, b = asyncio.run(scenario())
    assert a is b
    assert a["text"] == "电脑休眠过 30 秒" and a["text_en"] == "The computer was asleep for 30 sec"


# ---- 设置里换界面语言 ---------------------------------------------------------------------

class RecordingServer:
    def __init__(self):
        self.config = {}
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(msg)


def _pipeline(monkeypatch):
    from app.pipeline import Pipeline

    p = Pipeline.__new__(Pipeline)
    p.server = RecordingServer()
    saved = {}
    monkeypatch.setattr(p, "_save_setting", lambda k, v: saved.__setitem__(k, v), raising=False)
    return p, saved


def test_switching_the_ui_language_is_ignored_while_the_gate_is_closed(monkeypatch, capsys):
    p, saved = _pipeline(monkeypatch)
    assert p.handle_control({"type": "set_ui_lang", "value": "en"}) is None
    assert saved == {} and p.server.sent == [] and i18n.current() == i18n.ZH
    assert capsys.readouterr().out == ""


def test_switching_the_ui_language_saves_it_and_broadcasts_the_language_fields(monkeypatch, capsys):
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    p, saved = _pipeline(monkeypatch)
    for bad in ("fr", "", None, ["en"], {"x": 1}):
        assert p.handle_control({"type": "set_ui_lang", "value": bad}) is None
    assert saved == {} and p.server.sent == []
    run(p.handle_control({"type": "set_ui_lang", "value": "en"}))
    assert saved == {"ui_lang": "en"} and i18n.current() == i18n.EN
    (msg,) = p.server.sent
    assert msg == dict(i18n.config_info(), type="config")
    assert msg["ui_lang"] == "en" and msg["ui_lang_setting"] == "en"
    assert msg["ui_lang_available"] is True and msg["ui_lang_locked"] is False
    assert capsys.readouterr().out == "[信息] 界面语言：en → en\n"     # 终端永远是中文


# ---- G9 运行时网 ------------------------------------------------------------------------

PRODUCER = "/nowhere/app/producer.py"        # 不在 tests/ 下：算生产代码发出的消息


def _producer(name, src):
    """造一个「生产代码」里的函数：文件名不在 tests/ 下，运行时网就按生产消息查。"""
    scope = {}
    exec(compile(src, PRODUCER, "exec"), scope)
    return scope[name]


@pytest.fixture
def net(monkeypatch):
    hits = []
    monkeypatch.setattr(i18n, "NET_HITS", hits)
    monkeypatch.setattr(i18n, "NET", "report")
    return hits


def test_the_net_reports_plain_chinese_on_ui_fields_from_production_code(net):
    send = _producer("send", "async def send(server, msg):\n    await server.broadcast(msg)\n")
    server = CaptionServer(port=0)
    run(send(server, {"type": "status", "state": "idle", "detail": "正在停止…"}))
    run(send(server, {"type": "status", "state": "idle", "detail": L("正在停止…", "Stopping…")}))
    run(send(server, {"type": "status", "state": "idle", "detail": "Stopping (ASCII)"}))
    run(send(server, {"type": "caption", "id": 1, "original": "中文原文是数据", "why": ""}))
    run(send(server, {"type": "brand_new_thing", "text": "x"}))
    assert [h[:4] for h in net] == [("plain", "status", "detail", "正在停止…"),
                                    ("unregistered", "brand_new_thing", "", "")]
    assert net[0][4].startswith("tests/test_i18n_server.py::")      # 记下用例名


def test_messages_the_test_feeds_directly_are_skipped(net):
    """夹具直接喂给 broadcast / fanout 的中文不算：从调用者往上找，第一个不是广播管道、
    不是事件循环的帧在 tests/ 下。测试里包在 broadcast 外面的 RecordingServer 也算管道。"""
    class Wrapped(CaptionServer):
        async def broadcast(self, msg):
            await super().broadcast(msg)

    server = CaptionServer(port=0)
    run(server.broadcast({"type": "incident", "id": "a", "text": "电脑休眠过"}))
    run(server.status("idle", "本次没有自动更新"))
    asyncio.run(Wrapped(port=0).broadcast({"type": "notice", "text": "夹具"}))
    _phone_hub().fanout({"type": "incident", "id": "a", "text": "电脑休眠过"})
    assert net == []
    # 同一个包装，由生产代码发出：照查
    send = _producer("send", "async def send(server, msg):\n    await server.broadcast(msg)\n")
    run(send(Wrapped(port=0), {"type": "notice", "text": "漏写了 L"}))
    assert [h[:4] for h in net] == [("plain", "notice", "text", "漏写了 L")]


def test_the_net_is_off_in_production(net, monkeypatch):
    monkeypatch.setattr(i18n, "NET", None)
    send = _producer("send", "async def send(server, msg):\n    await server.broadcast(msg)\n")
    run(send(CaptionServer(port=0), {"type": "status", "detail": "正在停止…"}))
    assert net == []


def test_ui_values_walks_nested_config_paths():
    msg = {"type": "config",
           "incidents": {"a": {"text": "一"}, "b": {"text": L("二", "two")}},
           "selfcheck": {"checks": [{"name": "甲", "detail": "乙", "fix": ""}]},
           "viewer": {"note": "丙", "qr_rows": ["##"]}, "target_lang": "zh-CN"}
    got = sorted((path, str(value)) for path, value in i18n.ui_values(msg))
    assert got == [("incidents.*.text", "一"), ("incidents.*.text", "二"),
                   ("selfcheck.checks[].detail", "乙"), ("selfcheck.checks[].fix", ""),
                   ("selfcheck.checks[].name", "甲"), ("viewer.note", "丙")]
    assert list(i18n.ui_values({"type": "stats", "x": "中"})) == []
    assert list(i18n.ui_values("not a dict")) == []


def test_every_message_type_the_desktop_handles_is_registered_once():
    """web/app.js 的 switch 认得的每一种消息（hello 只在握手时发，不经 broadcast）
    都登记在 UI_FIELDS 或 DATA_ONLY 里，且只登记在一处。"""
    from tests.test_viewer_filter import KNOWN_TYPES

    handled = (set(KNOWN_TYPES) | {"alert_mode"}) - {"hello"}
    assert handled <= set(i18n.UI_FIELDS) | i18n.DATA_ONLY
    assert not set(i18n.UI_FIELDS) & i18n.DATA_ONLY
    for mtype, paths in i18n.UI_FIELDS.items():
        assert paths and all(isinstance(p, str) and p for p in paths), mtype


def test_english_desktop_messages_have_no_chinese_on_registered_fields():
    """G7 的形状（M 系列按这个补自己的句子）：英文模式下渲染后的登记字段里没有 CJK。"""
    server, client = make_server()
    with i18n.use("en"):
        run(_broadcast_samples(server))
    for msg in client.got:
        for path, value in i18n.ui_values(msg):
            assert not (isinstance(value, str) and CJK.search(value)), (msg["type"], path, value)


# ---- G9 strict：按用例判（tests/conftest.py 的 pytest_runtest_call） ----------------------------

def _conftest():
    """pytest 已经载入的那个 tests/conftest.py（同一个 _I18N_FIXTURE_TESTS），不另 import 一份。"""
    import sys
    from pathlib import Path

    here = (Path(__file__).resolve().parent / "conftest.py")
    return next(m for m in list(sys.modules.values())
                if getattr(m, "__file__", None) and Path(m.__file__).resolve() == here)


class _Item:
    def __init__(self, nodeid):
        self.nodeid = nodeid


def _after_call(item, error=None):
    """把 conftest 的 hook wrapper 当生成器推一遍：error 为 None 时模拟用例本身通过，否则模拟
    用例本身抛了 error。交回 wrapper 最后给出的结果。"""
    gen = _conftest().pytest_runtest_call(item)
    next(gen)
    try:
        if error is None:
            gen.send("passed")
        else:
            gen.throw(error)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("wrapper 没有在 yield 之后结束")


NODE = "tests/test_x.py::test_y[a]"
PLAIN = ("plain", "status", "detail", "正在停止…", NODE + " (call)")


@pytest.fixture
def strict(monkeypatch):
    hits = []
    monkeypatch.setattr(i18n, "NET_HITS", hits)
    monkeypatch.setattr(i18n, "NET", "strict")
    return hits


def test_strict_fails_the_test_that_sent_plain_chinese(strict):
    strict.append(PLAIN)
    strict.append(("unregistered", "brand_new_thing", "", "", NODE + " (setup)"))
    with pytest.raises(pytest.fail.Exception) as caught:
        _after_call(_Item(NODE))
    message = str(caught.value)
    assert "status.detail  「正在停止…」" in message and "没登记的消息类型 brand_new_thing" in message
    assert "i18n_fixture" in message                      # 告诉人另一条出路


def test_strict_leaves_other_tests_and_marked_tests_alone(strict, monkeypatch):
    strict.append(PLAIN)
    assert _after_call(_Item("tests/test_x.py::test_other")) == "passed"
    monkeypatch.setattr(_conftest(), "_I18N_FIXTURE_TESTS", {NODE})
    assert _after_call(_Item(NODE)) == "passed"


def test_strict_does_not_judge_a_test_that_switched_the_net_itself(strict, monkeypatch):
    """tests/test_i18n_server.py 的 net 夹具把 NET 换成 report、故意造违例：不按 strict 判。"""
    strict.append(PLAIN)
    monkeypatch.setattr(i18n, "NET", "report")
    assert _after_call(_Item(NODE)) == "passed"


def test_a_test_that_failed_on_its_own_keeps_its_own_error(strict):
    strict.append(PLAIN)
    with pytest.raises(KeyError):
        _after_call(_Item(NODE), KeyError("boom"))
