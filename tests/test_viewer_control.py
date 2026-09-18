"""Pipeline 这一侧：开/关/换链接、状态载荷、文案闸、空闲期审计、地址变化。

两条会真出事的性质在这里钉：
  * 端口没打开时**保持关闭并报观察**——观众端口一漂移，贴在墙上的二维码就静默失效；
  * 空闲期（还没开播、没有 audit）的同看事件不许丢——它们正是「那段时间谁看得到
    字幕和报警」的唯一证据。
"""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app import settings as settings_mod
from app import viewer as viewer_mod
from app import viewer_share as viewer_share_mod
from app.pipeline import Pipeline

try:                      # app/qr.py 是另一份改动提供的；缺了也不该让这个文件整体报错
    from app import qr as real_qr
except Exception:         # pragma: no cover
    real_qr = None

# A8：闸只拦「给不透明失败贴原因」的词和因果断言句式。
# 「防火墙」「Wi-Fi」这类名词是可观察事实 + 可做的事，不拦。
BANNED_WORDS = ("年龄", "限流", "封禁", "被墙", "拦截", "AP 隔离",
                "是因为", "应该是", "多半", "可能是", "导致")


class StubServer:
    port = 8765

    def __init__(self):
        self.config = {}
        self.messages = []
        self.viewer = None
        self.history = []
        self.alerts = []
        self.comments = []

    async def status(self, state, detail="", command=None):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)

    def of_type(self, mtype):
        return [m for m in self.messages if m.get("type") == mtype]


class FakeHub:
    def __init__(self, ports, token, fail=None, start_delay=0.0):
        self._ports = list(ports)
        self.token = token
        self.fail = fail
        self.start_delay = start_delay
        self.running = False
        self.count = 0
        self.stopped = []

    @property
    def port(self):
        return self._ports[0] if self._ports else None

    async def start(self):
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        if self.fail is not None:
            raise self.fail
        self.running = True

    async def stop(self, reason="off"):
        self.stopped.append(reason)
        self.running = False

    def rotate(self, token):
        self.token = token

    def url(self, ip):
        return viewer_mod.viewer_url(ip, self.port, self.token) if ip else None

    def state(self, ip_info=None, qr_rows=None, ip_changed=None):
        info = ip_info or {}
        ips = list(info.get("all") or [])
        url = self.url(info.get("ip")) if self.running else None
        return {"on": self.running, "port": self.port, "url": url,
                "ip": info.get("ip"), "ips": ips,
                "ambiguous": bool(info.get("ambiguous")), "viewers": self.count,
                "max_viewers": viewer_mod.MAX_VIEWERS, "qr_rows": qr_rows,
                "note": viewer_mod.share_note(
                    self.running, port=self.port, url=url, ip=info.get("ip"),
                    ips=ips, viewers=self.count, qr_ok=qr_rows is not None,
                    ip_changed=ip_changed, ports=self._ports)}


class FakeAudit:
    def __init__(self):
        self.records = []

    def viewer_share(self, state, port=None, viewers=None, reason=None):
        self.records.append(("viewer_share", state, port, viewers, reason))

    def viewer_connected(self, ip, count):
        self.records.append(("viewer_connected", ip, count))

    def viewer_disconnected(self, ip, count, reason):
        self.records.append(("viewer_disconnected", ip, count, reason))

    def viewer_auth_failed(self, ip, why, suppressed=0):
        self.records.append(("viewer_auth_failed", ip, why, suppressed))


class FakeQR:
    """app/qr.py 由另一份改动提供（见规格 §9 的 API）。这里注入一个替身，
    免得这个文件的断言依赖那个模块存不存在。"""

    def __init__(self, boom=None):
        self.boom = boom

    def encode(self, data, **kwargs):
        if self.boom is not None:
            raise self.boom
        return [[1, 0], [0, 1]]

    def rows(self, matrix, quiet=4):
        return ["0011", "1100"]


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5,
        context=False, asr_temperature=None, glossary=None, backend="auto",
        model=None, device="auto", compute_type="auto", denoise="off",
        banned_terms=None)
    server = StubServer()
    pipeline = Pipeline(args, server)
    made = []
    opts = {"fail": None, "start_delay": 0.0,
            "ip": {"ip": "192.168.1.23", "all": ["192.168.1.23"],
                   "source": "route", "ambiguous": False}}

    def make(ports, token):
        hub = FakeHub(ports, token, fail=opts["fail"],
                      start_delay=opts["start_delay"])
        made.append(hub)
        return hub

    monkeypatch.setattr(pipeline, "_make_viewer_hub", make)

    async def fake_ip():
        return opts["ip"]

    monkeypatch.setattr(pipeline, "_viewer_ip_info", fake_ip)
    monkeypatch.setattr(pipeline, "_start_viewer_ip_watch", lambda: None)
    install_qr(monkeypatch, FakeQR())
    return SimpleNamespace(pipeline=pipeline, server=server, made=made, opts=opts,
                           path=settings_mod.SETTINGS_FILE, monkeypatch=monkeypatch)


def install_qr(monkeypatch, module):
    import app

    monkeypatch.setitem(sys.modules, "app.qr", module)
    monkeypatch.setattr(app, "qr", module, raising=False)


def last_viewer(setup):
    published = setup.server.of_type("viewer")
    assert published, "没有发布过 viewer 状态"
    return published[-1]


def saved(setup):
    if not setup.path.exists():
        return {}
    return json.loads(setup.path.read_text(encoding="utf-8"))


# ---- 打开 / 端口没打开 ----
def test_opening_publishes_a_complete_state(setup):
    asyncio.run(setup.pipeline._set_viewer_share(True))
    state = last_viewer(setup)
    assert state["on"] is True
    assert state["url"].startswith("http://192.168.1.23:8766/#k=")
    assert state["qr_rows"] == ["0011", "1100"]
    assert setup.server.config["viewer"]["on"] is True
    assert setup.server.viewer is setup.made[-1]
    assert "扫下面的二维码" in state["note"]


def test_a_busy_port_stays_off_and_reports_what_the_system_said(setup):
    """观众端口刻意不漂移：漂了，已经发出去的二维码会静默失效。
    文案里只有观察（系统真的返回了什么）+ 能做的事。"""
    setup.opts["fail"] = OSError("[Errno 48] Address already in use")
    audit = FakeAudit()
    setup.pipeline.audit = audit
    asyncio.run(setup.pipeline._set_viewer_share(True))
    state = last_viewer(setup)
    assert state["on"] is False
    assert state["url"] is None
    assert "8766" in state["note"] and "8770" in state["note"]
    assert "Address already in use" in state["note"]
    assert setup.server.viewer is None
    assert saved(setup)["viewer_share_enabled"] is False
    assert ("viewer_share", "off", 8766, 0, "port_busy") in audit.records


def test_no_lan_address_still_opens_but_says_there_is_no_qr(setup):
    setup.opts["ip"] = {"ip": None, "all": [], "source": None, "ambiguous": False}
    asyncio.run(setup.pipeline._set_viewer_share(True))
    state = last_viewer(setup)
    assert state["on"] is True
    assert state["url"] is None
    assert state["qr_rows"] is None
    assert "没读到本机的局域网地址" in state["note"]
    assert "127.0.0.1" in state["note"]        # 能自己确认服务本身在跑


def test_several_addresses_are_all_offered(setup):
    setup.opts["ip"] = {"ip": "10.0.0.7", "all": ["10.0.0.7", "192.168.1.23"],
                        "source": "route", "ambiguous": True}
    asyncio.run(setup.pipeline._set_viewer_share(True))
    state = last_viewer(setup)
    assert state["ips"] == ["10.0.0.7", "192.168.1.23"]
    assert state["ambiguous"] is True
    assert "本机有多个网络地址" in state["note"]
    assert "192.168.1.23" in state["note"]


def test_picking_another_address_redraws_the_link(setup):
    """A10：程序分不出哪个地址手机能到——让中控点着试，比让程序猜靠谱。"""
    setup.opts["ip"] = {"ip": "10.0.0.7", "all": ["10.0.0.7", "192.168.1.23"],
                        "source": "route", "ambiguous": True}
    asyncio.run(setup.pipeline._set_viewer_share(True))
    asyncio.run(setup.pipeline._pick_viewer_ip("192.168.1.23"))
    assert last_viewer(setup)["url"].startswith("http://192.168.1.23:8766/#k=")
    # 不在候选里的地址一律不认：界面传什么都不能直接拼进链接
    asyncio.run(setup.pipeline._pick_viewer_ip("8.8.8.8"))
    assert last_viewer(setup)["url"].startswith("http://10.0.0.7:8766/#k=")


def test_a_broken_qr_encoder_does_not_take_the_share_down(setup):
    install_qr(setup.monkeypatch, FakeQR(boom=ValueError("超出容量")))
    asyncio.run(setup.pipeline._set_viewer_share(True))
    state = last_viewer(setup)
    assert state["on"] is True
    assert state["url"] is not None
    assert state["qr_rows"] is None
    assert "二维码没能生成" in state["note"]


def test_a_qr_module_that_is_not_what_we_expect_does_not_take_the_share_down(setup):
    """整段生成包在 try/except 里，而且捕的是 Exception 而不是某几个类型：
    二维码是锦上添花，同看本身不是。缺方法、改了签名、抛任何东西都只是没有码。"""
    install_qr(setup.monkeypatch, SimpleNamespace())          # 连 encode 都没有
    asyncio.run(setup.pipeline._set_viewer_share(True))
    state = last_viewer(setup)
    assert state["on"] is True
    assert state["url"] is not None
    assert state["qr_rows"] is None
    assert "二维码没能生成" in state["note"]


def test_the_real_qr_module_produces_rows_for_a_viewer_link(setup):
    """走真的 app/qr.py 一遍：接口对不上（改了参数名、换了返回类型）要在这里红。"""
    if real_qr is None:
        pytest.skip("app/qr.py 还没落地（由另一份改动提供）")
    install_qr(setup.monkeypatch, real_qr)
    asyncio.run(setup.pipeline._set_viewer_share(True))
    state = last_viewer(setup)
    rows_out = state["qr_rows"]
    assert isinstance(rows_out, list) and rows_out
    assert all(isinstance(r, str) and set(r) <= {"0", "1"} for r in rows_out)
    assert len({len(r) for r in rows_out}) == 1
    assert len(rows_out) == len(rows_out[0])                  # 方的


# ---- 文案闸 ----
def test_every_published_note_passes_the_rule_eight_gate(setup):
    """规则八：只写观察和能做的事，不给不透明失败贴原因标签。"""
    cases = [
        lambda: None,
        lambda: setup.opts.update(ip={"ip": None, "all": [], "source": None,
                                      "ambiguous": False}),
        lambda: setup.opts.update(ip={"ip": "10.0.0.7",
                                      "all": ["10.0.0.7", "192.168.1.23"],
                                      "source": "route", "ambiguous": True}),
        lambda: setup.opts.update(fail=OSError("[Errno 48] Address already in use")),
    ]
    notes = []
    for prepare in cases:
        prepare()
        asyncio.run(setup.pipeline._set_viewer_share(True))
        notes.append(last_viewer(setup)["note"])
        asyncio.run(setup.pipeline._set_viewer_share(False))
        notes.append(last_viewer(setup)["note"])
    notes.append(viewer_mod.NOTE_ROTATE_CONFIRM.format(n=3))
    notes.append(viewer_mod.share_note(True, port=8766, url="http://x/#k=y",
                                       ip="1.2.3.4", ips=["1.2.3.4"], viewers=1,
                                       qr_ok=False))
    notes.append(viewer_mod.share_note(True, port=8766, url="http://x/#k=y",
                                       ip="1.2.3.4", ips=["1.2.3.4"], viewers=1,
                                       ip_changed=("1.1.1.1", "1.2.3.4")))
    assert len(notes) >= 8
    for note in notes:
        for word in BANNED_WORDS:
            assert word not in note, (word, note)


def test_the_operator_texts_say_what_can_be_done(setup):
    """A8：能做的事要写出来——系统会弹什么框、Wi-Fi 要连哪一个、微信里怎么办。"""
    asyncio.run(setup.pipeline._set_viewer_share(True))
    note = last_viewer(setup)["note"]
    assert "请选允许" in note
    assert "同一个 Wi-Fi" in note
    assert "用浏览器打开" in note
    assert "当密码看待" in note              # 链接就是钥匙，必须说


# ---- 空闲期审计 ----
def test_events_from_the_idle_period_are_written_when_a_session_starts(setup):
    """还没开播时打开同看：事件先攒着，开播后按原顺序补写。
    没有这一步，「开播前就有人在看」这件事在合规证据里会整段不见。"""
    pipeline = setup.pipeline
    assert pipeline.audit is None
    pipeline._viewer_audit("viewer_share", state="on", port=8766, viewers=0,
                           reason="operator")
    pipeline._viewer_audit("viewer_connected", ip="192.168.1.50", count=1)
    pipeline._viewer_audit("viewer_auth_failed", ip="192.168.1.77",
                           why="bad_token", suppressed=0)
    assert len(pipeline._viewer_audit_pending) == 3
    audit = FakeAudit()
    pipeline.audit = audit
    pipeline._flush_viewer_audit()
    assert [r[0] for r in audit.records] == ["viewer_share", "viewer_connected",
                                            "viewer_auth_failed"]
    assert not pipeline._viewer_audit_pending
    pipeline._flush_viewer_audit()             # 再调一次不该重复写
    assert len(audit.records) == 3


def test_the_pending_queue_does_not_grow_without_bound(setup):
    pipeline = setup.pipeline
    for i in range(500):
        pipeline._viewer_audit("viewer_connected", ip="10.0.0.1", count=i)
    assert len(pipeline._viewer_audit_pending) == pipeline_mod.VIEWER_AUDIT_PENDING_MAX
    assert pipeline_mod.VIEWER_AUDIT_PENDING_MAX == viewer_mod.VIEWER_AUDIT_PENDING_MAX


def test_events_go_straight_to_the_audit_when_there_is_one(setup):
    audit = FakeAudit()
    setup.pipeline.audit = audit
    setup.pipeline._viewer_audit("viewer_connected", ip="10.0.0.9", count=2)
    assert audit.records == [("viewer_connected", "10.0.0.9", 2)]
    assert not setup.pipeline._viewer_audit_pending


def test_an_audit_that_refuses_to_write_does_not_break_the_share(setup):
    class Broken:
        def viewer_connected(self, ip, count):
            raise OSError("磁盘满了")

    setup.pipeline.audit = Broken()
    setup.pipeline._viewer_audit("viewer_connected", ip="10.0.0.9", count=1)


# ---- 启动与退出 ----
def test_restore_never_raises(setup, monkeypatch):
    """启动路径不能被同看带崩。"""
    def boom(*a, **k):
        raise RuntimeError("settings 读坏了")

    # restore_viewer_share 现在实现在 app/viewer_share.py（ViewerShareMixin），
    # 它自己的 load_settings 绑定在那个模块的命名空间里，补丁要打在那儿——
    # 打 pipeline_mod.load_settings 不会影响已经搬走的这份绑定
    monkeypatch.setattr(viewer_share_mod, "load_settings", boom)
    asyncio.run(setup.pipeline.restore_viewer_share())
    assert setup.server.viewer is None


def test_shutdown_stops_the_listener_without_flipping_the_setting(setup):
    """退出时把 0.0.0.0 上的端口收掉，但不动开关——下次启动还要照中控的意思恢复。"""
    asyncio.run(setup.pipeline._set_viewer_share(True))
    assert saved(setup)["viewer_share_enabled"] is True
    asyncio.run(setup.pipeline.stop_viewer_share("shutdown"))
    assert setup.server.viewer is None
    assert setup.made[-1].stopped == ["shutdown"]
    assert saved(setup)["viewer_share_enabled"] is True


def test_shutdown_never_raises_even_when_stopping_hangs(setup, monkeypatch):
    asyncio.run(setup.pipeline._set_viewer_share(True))
    hub = setup.made[-1]

    async def never(reason="off"):
        await asyncio.sleep(30)

    hub.stop = never
    # 同上：stop_viewer_share 也搬进了 app/viewer_share.py，这个常量要在那个
    # 模块里打补丁才会被它读到（pipeline_mod 上的只是重新导出的一份副本）
    monkeypatch.setattr(viewer_share_mod, "VIEWER_STOP_TIMEOUT_SEC", 0.05)
    asyncio.run(setup.pipeline.stop_viewer_share("shutdown"))


# ---- A3：串行 ----
def test_concurrent_commands_settle_on_the_last_one(setup):
    def run_all(*flags):
        async def scenario():
            await asyncio.gather(*[setup.pipeline._set_viewer_share(f)
                                   for f in flags])
        asyncio.run(scenario())

    run_all(True, False, True)
    assert setup.server.viewer is not None
    assert setup.server.viewer.running is True
    asyncio.run(setup.pipeline._set_viewer_share(False))

    setup.made.clear()
    run_all(True, True, False)
    assert setup.server.viewer is None
    # 没留下任何还在监听的替身
    assert all(not h.running for h in setup.made)


def test_a_close_arriving_during_a_slow_open_does_not_leak_a_listener(setup):
    """A3：start() 里有 await，期间中控又点了关闭——不许留下一个在监听的端口。"""
    setup.opts["start_delay"] = 0.05

    async def scenario():
        opening = asyncio.ensure_future(setup.pipeline._set_viewer_share(True))
        await asyncio.sleep(0)
        closing = asyncio.ensure_future(setup.pipeline._set_viewer_share(False))
        await asyncio.gather(opening, closing)

    asyncio.run(scenario())
    assert setup.server.viewer is None
    assert all(not h.running for h in setup.made)
    assert setup.made and setup.made[-1].stopped


# ---- A9：地址会变 ----
def test_a_changed_address_gets_republished_with_a_warning(setup, monkeypatch):
    monkeypatch.setattr(viewer_mod, "IP_REFRESH_SEC", 0.01)
    asyncio.run(setup.pipeline._set_viewer_share(True))

    async def scenario():
        setup.opts["ip"] = {"ip": "192.168.5.9", "all": ["192.168.5.9"],
                            "source": "route", "ambiguous": False}
        watch = asyncio.ensure_future(setup.pipeline._viewer_ip_watch())
        await asyncio.sleep(0.08)
        watch.cancel()
        try:
            await watch
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    state = last_viewer(setup)
    assert state["ip"] == "192.168.5.9"
    assert "本机地址已从 192.168.1.23 变为 192.168.5.9" in state["note"]
    assert "重新扫码" in state["note"]


def test_the_address_watch_stops_itself_once_sharing_is_off(setup, monkeypatch):
    monkeypatch.setattr(viewer_mod, "IP_REFRESH_SEC", 0.01)

    async def scenario():
        watch = asyncio.ensure_future(setup.pipeline._viewer_ip_watch())
        await asyncio.sleep(0.05)
        return watch.done()

    assert asyncio.run(scenario()) is True


# ---- handle_control 的三个分支 ----
def test_handle_control_routes_the_three_viewer_messages(setup, monkeypatch):
    seen = []

    async def record(name, *a):
        seen.append((name,) + a)

    monkeypatch.setattr(setup.pipeline, "_set_viewer_share",
                        lambda on: record("share", on))
    monkeypatch.setattr(setup.pipeline, "_rotate_viewer_token",
                        lambda: record("rotate"))
    monkeypatch.setattr(setup.pipeline, "_pick_viewer_ip",
                        lambda ip: record("pick", ip))

    async def scenario():
        for msg in ({"type": "viewer_share", "on": True},
                    {"type": "viewer_share"},
                    {"type": "viewer_rotate"},
                    {"type": "viewer_pick_ip", "ip": "192.168.1.5"}):
            result = setup.pipeline.handle_control(msg)
            assert asyncio.iscoroutine(result), msg
            await result

    asyncio.run(scenario())
    assert seen == [("share", True), ("share", False), ("rotate",),
                    ("pick", "192.168.1.5")]


def test_handle_control_still_ignores_anything_unknown(setup):
    assert setup.pipeline.handle_control({"type": "viewer_whatever"}) is None
