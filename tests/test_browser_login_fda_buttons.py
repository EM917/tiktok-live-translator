"""「完全磁盘访问权限」那几步旁边的按钮：open_fda_settings、reveal_python、reveal_terminal
（app/browser_login.py fda_open_argvs、fda_button_lines；app/pipeline.py _open_fda）。

2026-10-29 新 Mac 上，用户照着黄条的文字做不下来：是我们替他执行了
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    open -R <解释器的真实路径>
他把访达里选中的那个文件拖进列表才成功；从 Start.command 启动的还要把「终端」也加上。这几个按钮做的就是这几条
open，只打开系统界面，程序绝不自己改系统设置。这里钉：
  * argv 写死，页面传来的任何路径或参数都进不去；选中的解释器就是 sys.executable 解析符号链接以后的那个，
    和黄条上列的是同一份；
  * 只在 macOS 上有：别的平台持续提示、自检那一行都不带 fda，硬发过来什么都不执行，只回一句无害的话；
  * open 没成时把退出码照实告诉用户（第八条：只报观察）；
  * 手机同看、浮窗发不进来；手机也看不到 fda；
  * 文字按按钮写：第 1 步配「打开完全磁盘访问设置」，第 2 步配「在访达里选中 Python」，从终端启动时「终端」那一句配
    「在访达里选中终端」；按钮跟在哪一行后面（fda）就是文字里整整一行。

这里全是假的：假的 exec（不真跑 open，不会在跑测试的电脑上弹出系统设置或访达）、假的 probe。
"""
import asyncio
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import browser_login as bl
from app import i18n, resolver, selfcheck
from app.macbundle import LAUNCH_PYTHON_ENV
from app.server import CaptionServer
from tests.helpers import run
from tests.i18n_rules import EN_CAUSAL, EN_LABELS, ZH_CAUSAL, ZH_LABELS
from tests.test_browser_login_recheck import FakeProbe, _drain, _with_client, wire
from tests.test_login_first import INCIDENT
from tests.test_viewer_hub import FakeWS, StubServer, auth, make_hub, reasons, run_handshake, text

ROOT = Path(__file__).resolve().parent.parent
PY = "/opt/demo/bin/python3.13"
URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
TERMINAL = "/System/Applications/Utilities/Terminal.app"
TYPES = ("open_fda_settings", "reveal_python", "reveal_terminal")


class FakeExec:
    """假的 asyncio.create_subprocess_exec：记下 argv，按给的退出码返回；raises 就抛，hang 就一直不返回。"""

    def __init__(self, rc=0, raises=None, hang=False):
        self.rc, self.raises, self.hang = rc, raises, hang
        self.calls = []

    async def __call__(self, *argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.raises is not None:
            raise self.raises

        async def wait():
            if self.hang:
                await asyncio.sleep(30)
            return self.rc(argv) if callable(self.rc) else self.rc
        return SimpleNamespace(wait=wait)


def darwin_pipeline(monkeypatch, tmp_path, exe, platform="darwin"):
    p, server = wire(monkeypatch, tmp_path, FakeProbe({"safari": bl.BLOCKED, "chrome": bl.OK}))
    monkeypatch.setattr(sys, "platform", platform)
    p._fda_exec = exe
    return p, server


def click(p, *msgs):
    async def go():
        results = [p.handle_control(msg) for msg in msgs]
        for result in results:
            if result is not None:
                await result
        await asyncio.gather(*list(p._bg_tasks))
    run(go())


def notices(server):
    return [m["text"] for m in server.messages if m.get("type") == "notice"]


def out_lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line]


def blocked_layers():
    return [{"layer": resolver.LOGIN_LAYER, "outcome": "no_login", "browser": None,
             "login": {"safari": bl.BLOCKED}}]


# ---- argv 写死 -------------------------------------------------------------------------------

def test_each_kind_has_a_fixed_open_command_and_only_opens_system_ui():
    assert bl.FDA_OPEN_TYPES == TYPES
    assert bl.fda_open_argvs("open_fda_settings") == [["open", URL]]
    assert bl.fda_open_argvs("reveal_python", targets=[PY]) == [["open", "-R", PY]]
    assert bl.fda_open_argvs("reveal_terminal") == [["open", "-R", TERMINAL]]
    assert bl.TERMINAL_APP == TERMINAL and bl.FDA_SETTINGS_URL == URL
    for bad in ("open_flash_helper", "", None, "reveal_python ", "../reveal_python"):
        assert bl.fda_open_argvs(bad) == []
    # 只打开系统界面：每一条都是 open 本身，没有别的程序（tccutil、sqlite3、defaults……），程序绝不自己改系统设置
    for kind in TYPES:
        for argv in bl.fda_open_argvs(kind, targets=[PY]):
            assert argv[0] == "open" and len(argv) <= 3 and argv[1] in ("-R", URL)


def test_reveal_python_selects_the_resolved_interpreter_the_notice_lists(monkeypatch, tmp_path):
    """sys.executable 往往是 .venv/bin/python 这种符号链接：选中的是解析以后的那个文件，和黄条里给的路径一样。"""
    real = tmp_path / "Python.framework" / "bin" / "python3.13"
    real.parent.mkdir(parents=True)
    real.write_text("")
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    try:
        (venv / "python").symlink_to(real)
    except OSError:                                    # Windows 跑器没有建符号链接的权限时
        pytest.skip("这台机器上建不了符号链接")
    monkeypatch.setattr(sys, "executable", str(venv / "python"))
    monkeypatch.delenv(LAUNCH_PYTHON_ENV, raising=False)
    resolved = os.path.realpath(str(venv / "python"))
    assert bl.fda_open_argvs("reveal_python") == [["open", "-R", resolved]]
    for steps in (bl.fda_steps(buttons=True, terminal=False), bl.fda_steps(terminal=False)):
        assert "「{}」".format(resolved) in str(steps).split("\n")
    # 启动用的解释器不是同一个时，黄条列两个路径：两个都选中，次序和黄条一样
    launch = tmp_path / "launch" / "python3.14"
    launch.parent.mkdir()
    launch.write_text("")
    monkeypatch.setenv(LAUNCH_PYTHON_ENV, str(launch))
    targets = bl.fda_targets()
    assert targets == [os.path.realpath(str(launch)), resolved]
    assert bl.fda_open_argvs("reveal_python") == [["open", "-R", t] for t in targets]


@pytest.mark.parametrize("kind,expected", [
    ("open_fda_settings", [["open", URL]]),
    ("reveal_python", [["open", "-R", PY]]),
    ("reveal_terminal", [["open", "-R", TERMINAL]]),
])
def test_the_page_cannot_put_anything_into_argv(monkeypatch, tmp_path, kind, expected, capsys):
    exe = FakeExec(rc=0)
    p, server = darwin_pipeline(monkeypatch, tmp_path, exe)
    click(p, {"type": kind, "argv": ["rm", "-rf", "/"], "path": "/bin/sh", "url": "file:///etc/passwd",
              "target": "/Applications/Calculator.app", "args": ["-a", "Terminal"]})
    assert [argv for argv, _kw in exe.calls] == expected
    assert all(kw == {"stdout": asyncio.subprocess.DEVNULL, "stderr": asyncio.subprocess.DEVNULL}
               for _argv, kw in exe.calls)
    assert notices(server) == []                     # 成功不发提示：设置那一页、访达自己会到前面来
    assert not p._fda_opening
    assert "[信息] 完全磁盘访问权限：执行 {}".format(" ".join(expected[0])) in out_lines(capsys)


def test_two_interpreters_get_one_reveal_each_and_one_failure_does_not_stop_the_other(monkeypatch, tmp_path):
    second = "/opt/demo/launch/python3.14"
    exe = FakeExec(rc=lambda argv: 1 if argv[-1] == second else 0)
    p, server = darwin_pipeline(monkeypatch, tmp_path, exe)
    # 在 wire 之后换：wire 自己把 fda_targets 换成只有一个路径
    monkeypatch.setattr(bl, "fda_targets", lambda *a, **k: [second, PY])
    click(p, {"type": "reveal_python"})
    assert [argv for argv, _kw in exe.calls] == [["open", "-R", second], ["open", "-R", PY]]
    assert notices(server) == [bl.fda_open_failed("reveal_python", 1, second)]


# ---- open 没成：照实说退出码 ------------------------------------------------------------------

@pytest.mark.parametrize("kind,zh,en", [
    ("open_fda_settings",
     "没能打开「完全磁盘访问权限」那一页（open 返回 1）。可以自己打开「系统设置」→「隐私与安全性」→「完全磁盘访问权限」。",
     "Couldn’t open the Full Disk Access page (open returned 1). You can open System Settings > Privacy & "
     "Security > Full Disk Access yourself."),
    ("reveal_python",
     "没能在访达里选中「{}」（open 返回 1）。可以在「完全磁盘访问权限」的列表下面点「+」，按 ⌘⇧G，粘贴这个路径后回车、"
     "点「打开」。".format(PY),
     "Couldn’t show “{}” in Finder (open returned 1). You can click + under the Full Disk Access list, press "
     "⌘⇧G, paste this path, press Return, and click Open.".format(PY)),
    ("reveal_terminal",
     "没能在访达里选中「{}」（open 返回 1）。可以在「完全磁盘访问权限」的列表下面点「+」，按 ⌘⇧G，粘贴这个路径后回车、"
     "点「打开」。".format(TERMINAL),
     "Couldn’t show “{}” in Finder (open returned 1). You can click + under the Full Disk Access list, press "
     "⌘⇧G, paste this path, press Return, and click Open.".format(TERMINAL)),
])
def test_a_non_zero_exit_code_is_told_as_seen(monkeypatch, tmp_path, capsys, kind, zh, en):
    p, server = darwin_pipeline(monkeypatch, tmp_path, FakeExec(rc=1))
    sent = len(server.messages)
    click(p, {"type": kind})
    (note,) = notices(server)
    assert note == zh
    assert i18n.render(note, i18n.EN) == en
    assert out_lines(capsys)[-1] == "[警告] 完全磁盘访问权限：open 返回 1，没有打开"
    assert not p._fda_opening
    # 只是一句 notice：不挂持续提示、不改状态
    assert [m["type"] for m in server.messages[sent:]] == ["notice"]


@pytest.mark.parametrize("exe_kwargs,line", [
    ({"raises": FileNotFoundError(2, "No such file")}, "[警告] 完全磁盘访问权限：open 没能执行（FileNotFoundError）"),
    ({"hang": True}, "[警告] 完全磁盘访问权限：open 等了 0.05 秒还没返回"),
])
def test_open_that_fails_or_hangs_says_so_without_an_exit_code(monkeypatch, tmp_path, capsys, exe_kwargs, line):
    p, server = darwin_pipeline(monkeypatch, tmp_path, FakeExec(**exe_kwargs))
    monkeypatch.setattr(type(p), "FDA_OPEN_TIMEOUT_SEC", 0.05)
    click(p, {"type": "open_fda_settings"})
    (note,) = notices(server)
    assert note == bl.fda_open_failed("open_fda_settings")
    assert "open 返回" not in note and "open returned" not in i18n.render(note, i18n.EN)
    assert out_lines(capsys)[-1] == line
    assert not p._fda_opening


def test_a_double_click_runs_open_only_once(monkeypatch, tmp_path):
    gate = {}
    calls = []

    async def slow_exec(*argv, **kwargs):
        calls.append(argv)

        async def wait():
            await gate["ev"].wait()
            return 0
        return SimpleNamespace(wait=wait)

    p, _server = darwin_pipeline(monkeypatch, tmp_path, slow_exec)

    async def go():
        gate["ev"] = asyncio.Event()
        assert p.handle_control({"type": "open_fda_settings"}) is None      # 立刻返回，不在消息循环里等
        await asyncio.sleep(0.01)
        assert p.handle_control({"type": "open_fda_settings"}) is None
        assert len(p._bg_tasks) == 1
        assert p.handle_control({"type": "reveal_python"}) is None          # 别的按钮不受影响
        assert len(p._bg_tasks) == 2
        gate["ev"].set()
        await asyncio.gather(*list(p._bg_tasks))
        assert len(calls) == 2 and not p._fda_opening
        assert p.handle_control({"type": "open_fda_settings"}) is None      # 返回之后再点照常
        await asyncio.gather(*list(p._bg_tasks))
        assert len(calls) == 3

    run(go())


# ---- 只在 macOS 上有 ----------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("kind", TYPES)
def test_off_macos_nothing_runs_and_one_harmless_line_comes_back(monkeypatch, tmp_path, capsys, platform, kind):
    exe = FakeExec(rc=0)
    p, server = darwin_pipeline(monkeypatch, tmp_path, exe, platform=platform)
    click(p, {"type": kind})
    assert exe.calls == [] and not p._bg_tasks
    assert notices(server) == [bl.FDA_NOT_HERE]
    assert i18n.render(bl.FDA_NOT_HERE, i18n.EN) == "This button is only for macOS. Nothing was opened on this computer."
    assert out_lines(capsys)[-1] == "[信息] 完全磁盘访问权限：本机不是 macOS，{} 什么都没执行".format(kind)


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_the_blocked_notice_carries_the_buttons_on_macos_only(monkeypatch, tmp_path, platform):
    p, server = darwin_pipeline(monkeypatch, tmp_path, FakeExec(), platform=platform)
    run(p._sync_login_incident(blocked_layers()))
    msg = [m for m in server.messages if m.get("id") == INCIDENT][-1]
    stored = server.config["incidents"][INCIDENT]
    on_mac = platform == "darwin"
    assert msg["recheck"] is True
    assert msg["text"] == bl.no_login_notice({"safari": bl.BLOCKED}, buttons=on_mac)
    if on_mac:
        assert msg["fda"] == bl.fda_button_lines() and set(msg["fda"]) == {"settings", "python"}
        assert stored["fda"] == msg["fda"]                  # 刷新的页面照样画得出来
    else:
        assert "fda" not in msg and "fda" not in stored
        assert "⌘⇧G" in msg["text"]                          # 没有按钮的写法
    # 同样的观察再来一次：不重复推
    sent = len(server.messages)
    run(p._sync_login_incident(blocked_layers()))
    assert len(server.messages) == sent


def test_the_sign_in_kind_and_other_incidents_carry_no_buttons(monkeypatch, tmp_path):
    p, server = darwin_pipeline(monkeypatch, tmp_path, FakeExec())
    layers = [{"layer": resolver.LOGIN_LAYER, "outcome": "no_login", "browser": None,
               "login": {"safari": bl.NOT_LOGGED_IN}}]
    run(p._sync_login_incident(layers))
    assert "fda" not in server.messages[-1] and "fda" not in server.config["incidents"][INCIDENT]
    run(p._incident("session:clock_gap", "warn", "x"))
    assert set(server.messages[-1]) == {"type", "id", "level", "text", "ts"}


def test_the_buttons_go_away_when_the_recheck_turns_the_notice_into_the_sign_in_kind(monkeypatch, tmp_path):
    p, server = wire(monkeypatch, tmp_path, FakeProbe({"safari": bl.NOT_LOGGED_IN, "chrome": bl.OK}))
    p._fda_exec = FakeExec()
    run(p._sync_login_incident(blocked_layers()))
    assert "fda" in server.config["incidents"][INCIDENT]
    run(p._recheck_browser_login())
    assert "fda" not in server.config["incidents"][INCIDENT]
    assert "fda" not in [m for m in server.messages if m.get("id") == INCIDENT][-1]


@pytest.mark.parametrize("terminal", [False, True])
def test_the_terminal_button_only_when_started_from_a_terminal(monkeypatch, tmp_path, terminal):
    p, server = darwin_pipeline(monkeypatch, tmp_path, FakeExec())
    if terminal:
        monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    run(p._sync_login_incident(blocked_layers()))
    fda = server.config["incidents"][INCIDENT]["fda"]
    assert ("terminal" in fda) is terminal
    text = server.config["incidents"][INCIDENT]["text"]
    assert ("在访达里选中终端" in text) is terminal


# ---- 按钮跟在哪一行后面：文字里整整一行 -------------------------------------------------------------

@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("targets", [[PY], ["/opt/demo/launch/python3.14", "/Users/Jane Doe/miniconda3/bin/python3.13"]])
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_each_anchor_is_a_whole_line_of_the_steps(monkeypatch, terminal, targets, lang):
    """页面只拿 fda 给的字整行比对（web/app.js drawFdaLines）：每一项都得是持续提示、自检那一行里整整一行，恰好一次。"""
    monkeypatch.setattr(bl, "fda_targets", lambda *a, **k: list(targets))
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    if terminal:
        monkeypatch.setenv("TERM", "xterm-256color")
    anchors = bl.fda_button_lines()
    assert list(anchors) == (["settings", "python", "terminal"] if terminal else ["settings", "python"])
    notice = i18n.render(bl.no_login_notice({"safari": bl.BLOCKED}, buttons=True), lang)
    row_fix = i18n.render(bl.steps_text({"safari": bl.BLOCKED, "chrome": bl.NOT_LOGGED_IN}, buttons=True), lang)
    for where in (notice, row_fix):
        lines = [line.strip() for line in where.split("\n")]
        for key, line in anchors.items():
            assert lines.count(i18n.render(line, lang)) == 1, (key, lang, where)
    # 第 1 步配「打开完全磁盘访问设置」，第 2 步配「在访达里选中 Python」，「终端」那一句配「在访达里选中终端」
    for key, line in anchors.items():
        assert i18n.render(bl.FDA_BUTTON_LABELS[key], lang) in i18n.render(line, lang)
    assert i18n.render(anchors["settings"], lang).startswith("1. ")
    assert i18n.render(anchors["python"], lang).startswith("2. ")


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("n", [1, 2])
def test_the_button_steps_read_cleanly_in_both_languages(monkeypatch, terminal, n):
    """带按钮的写法：⌘⇧G 挪去 README / docs/faq 和失败时那一句；每个路径仍单独一行；只写看到的和能做的。"""
    targets = [PY, "/Users/Jane Doe/miniconda3/bin/python3.14"][:n]
    steps = bl.fda_steps(targets, terminal=terminal, buttons=True)
    zh, en = str(steps), i18n.render(steps, i18n.EN)
    for arm in (zh, en):
        assert "⌘⇧G" not in arm
        lines = arm.split("\n")
        for path in targets:
            assert lines.count("「{}」".format(path) if arm is zh else "“{}”".format(path)) == 1
        assert all(line == line.strip() and line for line in lines), lines
    assert not i18n.CJK.search(en) and "  " not in en
    for pattern, arm in ((EN_LABELS, en), (EN_CAUSAL, en), (ZH_LABELS, zh), (ZH_CAUSAL, zh)):
        assert not pattern.search(arm), (pattern.search(arm).group(), arm)
    assert ("Files to add:" in en) is (n == 2) and ("File to add:" in en) is (n == 1)
    assert ("Start.command" in zh) is terminal
    # 权限范围那句一字不动、仍是最后一行
    assert zh.split("\n")[-1] == str(bl.fda_steps(targets, terminal=terminal)).split("\n")[-1]


def test_the_failure_and_off_macos_lines_stay_within_rule_8():
    for note in [bl.FDA_NOT_HERE] + [bl.fda_open_failed(kind, rc, PY) for kind in TYPES for rc in (None, 1, 255)]:
        zh, en = str(note), i18n.render(note, i18n.EN)
        assert not i18n.CJK.search(en) and "  " not in en
        for pattern, arm in ((EN_LABELS, en), (EN_CAUSAL, en), (ZH_LABELS, zh), (ZH_CAUSAL, zh)):
            assert not pattern.search(arm), (pattern.search(arm).group(), arm)


def test_without_buttons_the_steps_are_unchanged():
    """解析失败的横幅里没有按钮：照旧是 ⌘⇧G 的写法（buttons 默认关着）。"""
    assert bl.fda_steps([PY], terminal=False) == bl.fda_steps([PY], terminal=False, buttons=False)
    assert "⌘⇧G" in bl.fda_steps([PY], terminal=False)
    assert "在访达里选中" not in bl.fda_steps([PY], terminal=True)


# ---- 自检「浏览器登录态」那一行 ---------------------------------------------------------------------

def test_the_selfcheck_row_carries_the_buttons_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(resolver, "_installed_browsers", lambda: ("safari", "chrome"))
    monkeypatch.setattr(bl, "probe", lambda browser: bl.BLOCKED if browser == "safari" else bl.NOT_LOGGED_IN)
    monkeypatch.setattr(bl, "fda_targets", lambda *a, **k: [PY])
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    observed, row = run(selfcheck.browser_login_state())
    assert row["recheck"] is True and row["fda"] == bl.fda_button_lines()
    assert bl.fda_steps(buttons=True) in row["fix"]
    lines = [line.strip() for line in str(row["fix"]).split("\n")]
    assert all(lines.count(str(line)) == 1 for line in row["fda"].values())
    # 别的浏览器读到了登录、登录优先那一步要读的 Safari 被拒（login-5，#163）：这一行是「提醒」，走同一段，也带按钮；
    # Safari 读得到只是没登录：不带；直接调（不给 buttons）也不带
    assert selfcheck._browser_login_row({"safari": bl.BLOCKED, "chrome": bl.OK}, buttons=True)["fda"] \
        == bl.fda_button_lines()
    assert "fda" not in selfcheck._browser_login_row({"safari": bl.NOT_LOGGED_IN}, buttons=True)
    assert "fda" not in selfcheck._browser_login_row(observed)
    # 不是 macOS：这一行「本机不适用」，什么都不带
    monkeypatch.setattr(sys, "platform", "win32")
    _observed, other = run(selfcheck.browser_login_state())
    assert "fda" not in other and "recheck" not in other


# ---- 只有本机页面发得出来 ------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [{"type": t} for t in TYPES] + [{"type": t, "id": 7} for t in TYPES])
def test_a_phone_cannot_send_them(bad):
    """手机同看的入站只认恰好 {"type":"retranslate","id":…}（app/viewer.py _handle_inbound）。"""
    calls = []
    server = StubServer()
    server.on_control = lambda msg: calls.append(msg)
    hub = make_hub(server, on_action=lambda *a: calls.append(a))
    ws = FakeWS(first=auth(), incoming=[text(bad)])
    run_handshake(hub, ws)
    assert calls == []
    assert reasons(hub) == ["closed"]


def test_the_phone_never_sees_the_anchors():
    from app.viewer import filter_payload

    msg = {"type": "incident", "id": INCIDENT, "level": "warn", "text": "x", "ts": 1.0, "recheck": True,
           "fda": {"settings": "1. x", "python": "2. y"}}
    out = filter_payload(msg)
    assert "fda" not in out and "recheck" not in out


def test_the_phone_never_sees_the_path_in_the_button_steps(monkeypatch):
    """带按钮的写法里路径照旧单独一行、括着：手机上中文和派生的英文都不带路径的任何一截（同 test_viewer_hub）。"""
    from app.viewer import filter_payload

    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    spaced = "/Users/Jane Doe/miniconda3/bin/python3.13"
    text = bl.fda_steps([spaced], terminal=True, buttons=True)
    out = filter_payload({"type": "incident", "id": INCIDENT, "level": "warn", "text": text, "ts": 1.0,
                          "fda": bl.fda_button_lines([spaced], terminal=True)})
    assert "fda" not in out
    for field in ("text", "text_en"):
        for piece in ("Jane", "Doe", "miniconda3"):
            assert piece not in out[field], (field, out[field])


def test_only_the_control_page_can_send_them_not_the_floating_window():
    """浮窗页（/ws?role=float）只读：它发什么都不理（app/server.py _ws）。"""
    server = CaptionServer(port=0)
    got = []
    server.on_control = lambda msg: got.append(msg)

    async def body(client):
        float_ws = await client.ws_connect("/ws?role=float")
        await _drain(float_ws)
        for kind in TYPES:
            await float_ws.send_json({"type": kind})
        await asyncio.sleep(0.1)
        assert got == []
        control = await client.ws_connect("/ws")
        await _drain(control)
        await control.send_json({"type": "reveal_python"})
        await asyncio.sleep(0.1)
        await control.close()
        await float_ws.close()

    run(_with_client(server, body))
    assert got == [{"type": "reveal_python"}]


# ---- 页面和后端是同一套名字、同一张表 -----------------------------------------------------------------

def test_the_page_uses_the_same_labels_and_types():
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for key, label in bl.FDA_BUTTON_LABELS.items():
        assert 'L("{}", "{}")'.format(str(label), label.en) in source, key
    table = dict(re.findall(r'\{ key: "(\w+)", type: "(\w+)" \}', source))
    assert table == {"settings": "open_fda_settings", "python": "reveal_python", "terminal": "reveal_terminal"}
    assert set(table.values()) == set(bl.FDA_OPEN_TYPES)


# ---- G8：终端在两种界面语言下逐字节相同 ---------------------------------------------------------------

def test_terminal_output_is_identical_in_both_ui_languages(monkeypatch, tmp_path, capsys):
    def one_run(lang):
        p, server = darwin_pipeline(monkeypatch, tmp_path / lang, FakeExec(rc=1))
        capsys.readouterr()                          # 建管线时打的几行带着各自的临时目录，不比
        sent = len(server.messages)
        with i18n.use(lang):
            click(p, *({"type": t} for t in TYPES))
        return capsys.readouterr().out.encode("utf-8"), [m["type"] for m in server.messages[sent:]]

    (tmp_path / "zh").mkdir()
    (tmp_path / "en").mkdir()
    zh_out, zh_types = one_run("zh")
    en_out, en_types = one_run("en")
    assert zh_out == en_out and zh_out and zh_types == en_types
