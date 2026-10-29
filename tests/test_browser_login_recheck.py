"""「我加好了，再查一次」：recheck_browser_login（app/pipeline.py _recheck_browser_login）。

2026-10-29 新 Mac 上，中控照着「完全磁盘访问权限」那几步做完以后，在点「开始翻译」之前没有任何地方告诉他成没成：
程序只在启动、开场的整轮自检和下一次解析时才重新看权限，持续提示只在内存里，程序一重开就没了。这个指令让程序
当场看一次：只跑 browser_login.probe（不解密、不碰钥匙串），只替换自检「浏览器登录态」那一行，按结果撤掉或改写
「没有读到 TikTok 登录」那条提示，发一句 notice，终端打一行代码。

这里全是假的：假的 probe、假的「装了哪些浏览器」。不读真实浏览器数据、不连网、不 import main。
"""
import asyncio
import json
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import browser_login as bl
from app import i18n, resolver, selfcheck
from app.server import CaptionServer
from tests.helpers import run
from tests.test_login_first import INCIDENT, make_pipeline
from tests.test_viewer_hub import FakeWS, StubServer, auth, make_hub, reasons, run_handshake, text


class SelfcheckAudit:
    def __init__(self):
        self.records = []

    def resolve(self, record):
        pass

    def selfcheck(self, checks, summary, full=True):
        self.records.append(([c["name"] for c in checks], [c["level"] for c in checks], full))


class FakeProbe:
    """按顺序交出各次重查时每个浏览器的代码。"""

    def __init__(self, *rounds):
        self.rounds, self.calls = list(rounds), []

    def __call__(self, browser):
        self.calls.append(browser)
        return self.rounds[0][browser]

    def next_round(self):
        self.rounds.pop(0)


def wire(monkeypatch, tmp_path, probe, installed=("safari", "chrome")):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(resolver, "_installed_browsers", lambda: tuple(installed))
    monkeypatch.setattr(bl, "probe", probe)
    monkeypatch.setattr(bl, "fda_targets", lambda *a, **k: ["/opt/demo/bin/python3.13"])
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    p, server, _audit = make_pipeline(monkeypatch, tmp_path, stub_audit=False)
    p.audit = SelfcheckAudit()
    return p, server


def other_rows():
    return [selfcheck._check(selfcheck.NAMES["asr"], selfcheck.OK, "large-v3"),
            selfcheck._check(selfcheck.NAMES["audit"], selfcheck.OK, "logs/")]


def publish_startup_check(p, observed):
    """开播前那轮自检已经发布过：语音识别、浏览器登录态、审计日志三行。"""
    rows = other_rows()
    rows.insert(1, selfcheck._browser_login_row(observed))
    run(p._publish_selfcheck(rows))
    return rows


def raise_blocked_notice(p):
    """「没有读到 TikTok 登录」那条已经挂着（上一次解析时 Safari 被系统拒绝读取）。"""
    layers = [{"layer": resolver.LOGIN_LAYER, "outcome": "no_login", "browser": None,
               "login": {"safari": bl.BLOCKED}}]
    run(p._sync_login_incident(layers, {"safari": bl.BLOCKED, "chrome": bl.OK}))
    shown = p._incident_shown(INCIDENT)
    assert shown and shown.get("recheck") is True
    return shown


def notices(server):
    return [m["text"] for m in server.messages if m.get("type") == "notice"]


def incident_messages(server):
    return [m for m in server.messages if m.get("id") == INCIDENT]


def test_the_blocked_notice_carries_the_recheck_flag_and_the_others_do_not(monkeypatch, tmp_path):
    p, server = wire(monkeypatch, tmp_path, FakeProbe({"safari": bl.BLOCKED, "chrome": bl.OK}))
    raise_blocked_notice(p)
    assert incident_messages(server)[-1]["recheck"] is True
    # 读得到、没登录的那种：不带（按钮说的是「我加好了」，这时要做的是登录）
    layers = [{"layer": resolver.LOGIN_LAYER, "outcome": "no_login", "browser": None,
               "login": {"safari": bl.NOT_LOGGED_IN}}]
    run(p._sync_login_incident(layers))
    last = incident_messages(server)[-1]
    assert "recheck" not in last and "recheck" not in p._incident_shown(INCIDENT)
    # 别的持续提示和以前逐字节相同：不带这个键
    run(p._incident("session:clock_gap", "warn", "x"))
    assert set(server.messages[-1]) == {"type", "id", "level", "text", "ts"}
    assert set(server.config["incidents"]["session:clock_gap"]) == {"id", "level", "text", "since", "ts"}


def test_still_refused_then_readable_with_a_sign_in(monkeypatch, tmp_path, capsys):
    """假的 probe 先说 blocked、再说 ok：第一次提示不动、只说「仍然拒绝」；第二次撤掉提示、只替换自检那一行。"""
    probe = FakeProbe({"safari": bl.BLOCKED, "chrome": bl.NOT_LOGGED_IN},
                      {"safari": bl.OK, "chrome": bl.NOT_LOGGED_IN})
    p, server = wire(monkeypatch, tmp_path, probe)
    before = publish_startup_check(p, {"safari": bl.BLOCKED, "chrome": bl.NOT_LOGGED_IN})
    assert before[1]["level"] == selfcheck.WARN and before[1]["recheck"] is True
    shown = raise_blocked_notice(p)
    p.audit.records.clear()
    sent_before = len(server.messages)

    run(p._recheck_browser_login())
    assert probe.calls == ["safari", "chrome"]
    out = capsys.readouterr().out
    assert "[信息] 浏览器登录态重查：safari=blocked_by_system chrome=not_logged_in" in out
    # 提示不动；notice 只写看到的和能做的一步
    assert p._incident_shown(INCIDENT)["text"] == shown["text"]
    assert not any(m.get("id") == INCIDENT for m in server.messages[sent_before:])
    assert notices(server) == [bl.recheck_notice({"safari": bl.BLOCKED})]
    assert notices(server)[-1].startswith("系统仍然拒绝读取 Safari 的数据。按 ⌘Q 完全退出本程序，再打开它。")
    # 自检那一行重算了，等级没变：审计里没有新记录；按钮还在
    checks = server.config["selfcheck"]["checks"]
    assert [c["name"] for c in checks] == [c["name"] for c in before]
    assert checks[1]["recheck"] is True
    assert p.audit.records == []

    probe.next_round()
    run(p._recheck_browser_login())
    assert "[信息] 浏览器登录态重查：safari=ok chrome=not_logged_in" in capsys.readouterr().out
    assert p._incident_shown(INCIDENT) is None
    assert incident_messages(server)[-1]["level"] == "clear"
    assert notices(server)[-1] == "现在能读取 Safari 的数据了，读到了 TikTok 登录。"
    assert i18n.render(notices(server)[-1], i18n.EN) == "The app can read Safari data now and found a TikTok sign-in."
    after = server.config["selfcheck"]["checks"]
    # 只替换了这一行：别的行是原来那几个对象
    assert after[0] is before[0] and after[2] is before[2]
    assert after[1]["name"] == selfcheck.NAMES["browser_login"] and after[1]["level"] == selfcheck.OK
    assert after[1]["detail"] == "Safari：读到了 TikTok 登录；Chrome：能读取，没有 TikTok 登录 cookie（sessionid 等）"
    assert "recheck" not in after[1]
    assert server.config["selfcheck"]["summary"] == selfcheck.summarize(after)
    # 审计照 _audit_selfcheck：只记等级变了的那一行
    assert p.audit.records == [([selfcheck.NAMES["browser_login"]], [selfcheck.OK], False)]


def test_the_row_carries_the_button_only_with_the_fda_steps():
    blocked = selfcheck._browser_login_row({"safari": bl.BLOCKED, "chrome": bl.NOT_LOGGED_IN})
    assert blocked["level"] == selfcheck.WARN and blocked["recheck"] is True
    assert bl.fda_steps() in blocked["fix"]
    # 别的浏览器读到了登录：这一行是「通过」、不带步骤，也不带按钮
    assert "recheck" not in selfcheck._browser_login_row({"safari": bl.BLOCKED, "chrome": bl.OK})
    assert "recheck" not in selfcheck._browser_login_row({"safari": bl.NOT_LOGGED_IN})
    assert "recheck" not in selfcheck._browser_login_row({})


def test_readable_without_a_sign_in_turns_the_notice_into_the_sign_in_kind(monkeypatch, tmp_path):
    probe = FakeProbe({"safari": bl.NOT_LOGGED_IN, "chrome": bl.OK})
    p, server = wire(monkeypatch, tmp_path, probe)
    raise_blocked_notice(p)
    run(p._recheck_browser_login())
    shown = p._incident_shown(INCIDENT)
    # 那条提示换成读得到、没登录的那种：叫人去 Safari 里登录，不再带按钮
    assert shown["text"] == bl.no_login_notice({"safari": bl.NOT_LOGGED_IN})
    assert bl.SAFARI_LOGIN_STEPS in shown["text"] and "recheck" not in shown
    # 上一次解析里「后面几步借用了 Chrome」那一句不接过来：那次解析里 Safari 是系统拒绝读取，「Safari 读得到、没登录」
    # 是刚才重查看到的。拼在一起就成了「同一次解析里」从来没出现过的组合
    assert "同一次解析里" not in shown["text"]
    assert "same lookup" not in i18n.render(shown["text"], i18n.EN)
    assert notices(server)[-1] == "现在能读取 Safari 的数据了，里面没有 TikTok 登录。请在 Safari 里登录 TikTok。"


def test_auto_rechecks_only_speak_when_the_result_changes(monkeypatch, tmp_path):
    """页面在窗口重新拿到焦点时自动发的（auto）：结果和上一次一样就不再发 notice，免得每次切回窗口都弹一句。
    手点的每次都说。"""
    probe = FakeProbe({"safari": bl.BLOCKED, "chrome": bl.OK}, {"safari": bl.OK, "chrome": bl.OK})
    p, server = wire(monkeypatch, tmp_path, probe)
    raise_blocked_notice(p)
    run(p._recheck_browser_login(auto=True))
    run(p._recheck_browser_login(auto=True))
    assert len(notices(server)) == 1
    run(p._recheck_browser_login())
    assert len(notices(server)) == 2
    probe.next_round()
    run(p._recheck_browser_login(auto=True))
    assert len(notices(server)) == 3 and p._incident_shown(INCIDENT) is None


def test_the_control_runs_in_the_background_and_only_one_at_a_time(monkeypatch, tmp_path):
    probe = FakeProbe({"safari": bl.BLOCKED, "chrome": bl.OK})
    p, server = wire(monkeypatch, tmp_path, probe)

    async def go():
        assert p.handle_control({"type": "recheck_browser_login"}) is None     # 立刻返回，不在消息循环里等
        p._login_rechecking = True                                             # 上一次还没跑完：这次丢掉
        await p._recheck_browser_login()
        assert probe.calls == []
        p._login_rechecking = False
        await asyncio.gather(*list(p._bg_tasks))

    run(go())
    assert probe.calls == ["safari", "chrome"] and len(notices(server)) == 1
    assert p._login_rechecking is False


def test_a_failed_probe_is_reported_without_raising(monkeypatch, tmp_path, capsys):
    def broken(browser):
        raise RuntimeError("boom")

    monkeypatch.setattr(selfcheck, "browser_login_state", None)        # 调到它就 TypeError
    p, server = wire(monkeypatch, tmp_path, broken)
    run(p._recheck_browser_login())
    assert "[警告] 浏览器登录态重查失败: TypeError" in capsys.readouterr().out
    assert p._login_rechecking is False and notices(server) == []


@pytest.mark.parametrize("first", [
    {"safari": bl.BLOCKED}, {"safari": bl.OK}, {"safari": bl.NOT_LOGGED_IN}, {"safari": bl.NO_TIKTOK},
    {"safari": bl.READABLE}, {"safari": bl.NO_DATA}, {"chrome": bl.KEYCHAIN_WAIT}, {},
])
@pytest.mark.parametrize("terminal", [False, True])
def test_the_recheck_notice_only_states_what_was_seen(first, terminal):
    from tests.i18n_rules import EN_CAUSAL, EN_LABELS, ZH_CAUSAL, ZH_LABELS

    note = bl.recheck_notice(first, terminal=terminal)
    zh, en = str(note), i18n.render(note, i18n.EN)
    assert zh and en and not i18n.CJK.search(en)
    for pattern, arm in ((EN_LABELS, en), (EN_CAUSAL, en), (ZH_LABELS, zh), (ZH_CAUSAL, zh)):
        assert not pattern.search(arm), (pattern.search(arm).group(), arm)
    # 「里面没有 TikTok 登录」只在 probe 看得出没有登录 cookie 时说；看不出（READABLE）、没找到库（NO_DATA）不这么说
    assert ("里面没有 TikTok 登录" in zh) == (set(first.values()) <= {bl.NOT_LOGGED_IN, bl.NO_TIKTOK} and bool(first))
    if bl.BLOCKED in first.values():
        assert bl.quit_step(terminal) in note and ("Start.command" in zh) is terminal


# ---- 只有本机页面发得出来 ------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"type": "recheck_browser_login"},
    {"type": "recheck_browser_login", "auto": True},
    {"type": "recheck_browser_login", "id": 7},           # 凑成 {type, id} 的形状也不行：类型不是 retranslate
])
def test_a_phone_cannot_send_it(bad):
    """手机同看的入站只认恰好 {"type":"retranslate","id":…}（app/viewer.py _handle_inbound）。"""
    calls = []
    server = StubServer()
    server.on_control = lambda msg: calls.append(msg)
    hub = make_hub(server, on_action=lambda *a: calls.append(a))
    ws = FakeWS(first=auth(), incoming=[text(bad)])
    run_handshake(hub, ws)
    assert calls == []
    assert reasons(hub) == ["closed"]


def test_the_phone_never_sees_the_flag():
    from app.viewer import filter_payload

    msg = {"type": "incident", "id": INCIDENT, "level": "warn", "text": "x", "ts": 1.0, "recheck": True}
    assert "recheck" not in json.dumps(filter_payload(msg))


async def _with_client(server, body):
    app = web.Application()
    app.router.add_get("/ws", server._ws)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        return await body(client)
    finally:
        await client.close()


async def _drain(ws, wait=0.2):
    while True:
        try:
            await ws.receive_json(timeout=wait)
        except (asyncio.TimeoutError, TypeError, ValueError):
            return


def test_only_the_control_page_can_send_it_not_the_floating_window():
    """浮窗页（/ws?role=float）只读：它发什么都不理（app/server.py _ws）。"""
    server = CaptionServer(port=0)
    got = []
    server.on_control = lambda msg: got.append(msg)

    async def body(client):
        float_ws = await client.ws_connect("/ws?role=float")
        await _drain(float_ws)
        await float_ws.send_json({"type": "recheck_browser_login"})
        await asyncio.sleep(0.1)
        assert got == []
        control = await client.ws_connect("/ws")
        await _drain(control)
        await control.send_json({"type": "recheck_browser_login"})
        await asyncio.sleep(0.1)
        await control.close()
        await float_ws.close()

    run(_with_client(server, body))
    assert got == [{"type": "recheck_browser_login"}]
