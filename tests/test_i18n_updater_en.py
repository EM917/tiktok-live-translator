"""一键更新、检查更新、组件提示的英文（spec §12.1 G7/G8，提交 M8）。

真的 Updater（git、pip、execv、GitHub 都换成替身，见 tests/test_resilience_update.py），把每一条
出口走一遍，看交给界面的状态行和一次性提示在英文下是不是干净的英文：没有中文、没有贴标签的词、
两句之间有空格，按钮名写成 Update Now / Start、终端写成 Terminal。中文那一路由原有测试钉着
（它们一条没改），这里另外钉住终端输出与界面语言无关。
"""
import re

import pytest

from app import i18n
from app import settings as settings_mod
from app import updater as updater_mod
from app.i18n import CJK, Bi
from app.updater import Updater
from tests.helpers import run
from tests.i18n_rules import EN_LABELS
from tests.test_resilience_update import TARGET, RecordingServer, fake_github, make_updater

# 句号（或右括号）后面紧跟着下一句的大写字母 = 两句英文粘在了一起
_GLUED = re.compile(r"\.(?=[A-Z])|\)(?=[A-Za-z])")


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """同 tests/test_resilience_update.py 的 isolated：设置文件和项目目录都在临时目录里。"""
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(updater_mod, "ROOT", tmp_path)
    return tmp_path


def assert_clean_english(text, where):
    assert isinstance(text, str) and not isinstance(text, Bi), (where, type(text))
    assert text.strip() and not CJK.search(text), (where, text)
    hit = EN_LABELS.search(text)
    assert hit is None, (where, hit and hit.group(), text)
    assert "  " not in text and not _GLUED.search(text), (where, text)


def ui_texts(server):
    """状态行和一次性提示，按发出的顺序，渲染成英文。"""
    out = []
    for msg in server.sent:
        if msg.get("type") == "status" and msg.get("detail"):
            out.append(("status", i18n.render(msg["detail"], "en")))
        elif msg.get("type") == "notice":
            out.append(("notice", i18n.render(msg["text"], "en")))
    return out


def last_english(server, kind):
    texts = [t for k, t in ui_texts(server) if k == kind]
    assert texts, kind
    for text in texts:
        assert_clean_english(text, kind)
    return texts[-1]


# ---- 没在监听：状态行 ------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,pip_code,expect", [
    ({}, None, "Couldn’t install the components the new version needs (pip result: timed out, "
               "details in logs/"),
    ({}, 1, "(pip result: 1, details in logs/"),
    ({"fetch": (None, "", "timeout")}, 0,
     "The update didn’t finish (couldn’t get the new version from GitHub (didn’t finish "
     "downloading in 90 sec)). Running the line below in Terminal usually fixes this:"),
    ({"rev-parse": (1, "", "fatal: no upstream configured for branch 'main'")}, 0,
     "The update didn’t finish (couldn’t find the remote branch this copy tracks (fatal: no "
     "upstream configured for branch 'main'))."),
    ({"rev-parse:HEAD": (0, TARGET + "\n", "")}, 0,
     "The app wasn’t updated (the remote branch this copy tracks has nothing newer). You can "
     "download the new version from GitHub: http://x"),
    ({"show:VERSION": (0, "1.0.0\n", "")}, 0,
     "(the remote branch this copy tracks has v1.0.0, which isn’t newer than the current v1.0.0)"),
    ({"show:VERSION": (1, "", "")}, 0,
     "(couldn’t read the version number on the remote branch (git returned 1))"),
    ({"show:requirements.txt": (1, "", "")}, 0,
     "(couldn’t read the new version’s component list (git returned 1))"),
    ({"merge-base": (1, "", "")}, 0,
     "(the local code can’t fast-forward to the new version (git returned 1))"),
    ({"merge": (1, "", "fatal: Not possible to fast-forward, aborting.")}, 0,
     "Couldn’t merge the new version’s code (fatal: Not possible to fast-forward, aborting.). "
     "Keeping the current version, v1.0.0. To update manually, run the line below in Terminal:"),
    ({}, 0, "Update complete. Restarting…"),
])
def test_idle_update_outcomes_in_english(monkeypatch, isolated, overrides, pip_code, expect):
    server, events = RecordingServer(), []
    u, _pip, _execs = make_updater(monkeypatch, server, events, git_overrides=overrides,
                                   pip_code=pip_code)
    run(u.apply())
    assert expect in last_english(server, "status")


def test_the_precheck_refusals_in_english(monkeypatch, isolated):
    server, events = RecordingServer(), []
    u, _pip, _execs = make_updater(monkeypatch, server, events)
    u.latest = {"can_auto": False, "url": "http://x"}
    run(u.apply())
    assert last_english(server, "status") == (
        "This copy was installed from a ZIP file and can’t update itself. Download the new "
        "version from GitHub: http://x")

    server, events = RecordingServer(), []
    u, _pip, _execs = make_updater(monkeypatch, server, events,
                                   git_overrides={"status": (127, "", "not found")})
    run(u.apply())
    assert last_english(server, "status").startswith(
        "git isn’t available on this computer, so the app can’t update itself. Download the new "
        "version’s ZIP file from GitHub, or install git and run:")

    server, events = RecordingServer(), []
    u, _pip, _execs = make_updater(
        monkeypatch, server, events,
        git_overrides={"status": (0, " M app/pipeline.py\n M web/app.js\n", "")})
    run(u.apply())
    assert last_english(server, "status").startswith(
        "The update didn’t start. These app files were changed, and updating would overwrite "
        "them: app/pipeline.py, web/app.js. If you didn’t change them on purpose, run the line "
        "below in Terminal")


def test_restart_failure_in_english(monkeypatch, isolated):
    server, events = RecordingServer(), []
    u, _pip, _execs = make_updater(monkeypatch, server, events)

    def broken_execv(exe, argv):
        raise OSError("exec format error")

    monkeypatch.setattr(updater_mod.os, "execv", broken_execv)
    run(u.apply())
    assert last_english(server, "status") == (
        "The update was downloaded, but the app couldn’t restart itself (exec format error). "
        "Quit the app and open it again")


# ---- 在监听：一次性提示，监听照常 --------------------------------------------------------------

def _live():
    return True


@pytest.mark.parametrize("overrides,expect", [
    ({"status": (0, " M app/pipeline.py\n", "")},
     "The update didn’t start. These app files were changed: app/pipeline.py. Monitoring "
     "continues. After the stream, click Update Now again to see what to do."),
    ({"fetch": (None, "", "timeout")},
     "The update didn’t finish: couldn’t get the new version from GitHub (didn’t finish "
     "downloading in 90 sec). Monitoring continues. After the stream, you can click Update Now "
     "again."),
    ({"rev-parse:HEAD": (0, TARGET + "\n", "")},
     "The update didn’t finish: the remote branch this copy tracks has nothing newer. "
     "Monitoring continues."),
    ({}, "Monitoring started again during the update, so v9.9.9 wasn’t merged. After the "
         "stream, you can click Update Now again. Monitoring continues."),
])
def test_live_update_notices_in_english(monkeypatch, isolated, overrides, expect):
    server, events = RecordingServer(), []
    u, _pip, execs = make_updater(monkeypatch, server, events, git_overrides=overrides)
    run(u.apply(live=_live))
    assert last_english(server, "notice") == expect
    assert execs == []


def test_waiting_for_the_pip_lock_in_english(monkeypatch, isolated):
    server, events = RecordingServer(), []
    u, _pip, _execs = make_updater(monkeypatch, server, events)

    async def go():
        lock = u._get_pip_lock()
        await lock.acquire()
        monkeypatch.setattr(updater_mod, "UPDATE_LOCK_WAIT_SEC", 0.01)
        try:
            await u.apply(live=_live)
        finally:
            lock.release()

    run(go())
    assert last_english(server, "notice").startswith(
        "The update didn’t finish: another component was still installing in the background "
        "after 0 sec. Click Update Now after it finishes. Monitoring continues.")


# ---- 检查更新、页脚、GPU 组件 ------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,expect", [
    ({"status": 403}, "Couldn’t check for updates (GitHub returned HTTP 403)."),
    ({"exc": OSError("unreachable")}, "Couldn’t check for updates (couldn’t reach GitHub)."),
    ({"status": 200}, "v1.0.0 is the latest version."),
])
def test_manual_update_check_in_english(monkeypatch, isolated, kwargs, expect):
    monkeypatch.setattr(updater_mod, "local_version", lambda: "1.0.0")
    server = RecordingServer()
    fake_github(monkeypatch, **kwargs)
    run(Updater(server).check_and_notify(delay=0, manual=True))
    assert last_english(server, "notice") == expect


@pytest.mark.parametrize("stored,zh,en", [
    ("HTTP 403", "HTTP 403", "HTTP 403"),
    ("超时", "超时", "timed out"),
    ("ClientConnectorError", "ClientConnectorError", "ClientConnectorError"),
    (None, "未知", "unknown"),
])
def test_footer_note_in_english(stored, zh, en):
    now, day = 1_800_000_000.0, 86400
    note = updater_mod.update_check_note(
        {"update_check_error": {"at": now, "since": now - 15 * day, "status_or_exc": stored}})
    assert note == "已连续 15 天没能连上更新服务器（最近一次：{}）".format(zh)
    text = i18n.render(note, "en")
    assert text == "Couldn’t reach the update server for 15 days in a row (last result: {})".format(en)
    assert_clean_english(text, stored)


def test_footer_note_reads_the_stored_reason_as_data(isolated):
    """settings 里存的原因只是数据：读回来的值不管是什么类型都不能让页脚出错。"""
    assert updater_mod._ui_check_error("超时") == "超时"
    assert updater_mod._ui_check_error(["x"]) == ["x"] and updater_mod._ui_check_error(None) is None
    now, day = 1_800_000_000.0, 86400
    note = updater_mod.update_check_note(
        {"update_check_error": {"at": now, "since": now - 15 * day, "status_or_exc": ["x"]}})
    assert note == "已连续 15 天没能连上更新服务器（最近一次：['x']）"


def test_the_gpu_component_note_in_english():
    text = i18n.render(updater_mod.MLX_READY_TEXT, "en")
    assert_clean_english(text, "MLX_READY_TEXT")
    assert "reopen the app" in text and "3 GB" in text


# ---- 终端与界面语言无关 ------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,pip_code", [({}, None), ({"merge": (1, "", "fatal: x")}, 0)])
def test_the_terminal_says_the_same_in_both_languages(monkeypatch, isolated, capsys, overrides,
                                                      pip_code):
    """「终端、日志、审计永远是中文」：同一次失败的更新在中文、英文界面下各跑一遍，终端输出
    逐字节相同，界面上一个是中文、一个是英文。"""
    got = {}
    for lang in (i18n.ZH, i18n.EN):
        settings_mod.save_setting("update_notice_at", 0)
        capsys.readouterr()
        server, events = RecordingServer(), []
        u, _pip, _execs = make_updater(monkeypatch, server, events, git_overrides=overrides,
                                       pip_code=pip_code)
        with i18n.use(lang):
            run(u.apply())
        got[lang] = (capsys.readouterr().out, server.statuses()[-1][1])
    assert got[i18n.ZH][0] == got[i18n.EN][0] and "[警告]" in got[i18n.ZH][0]
    assert got[i18n.ZH][1] == got[i18n.EN][1]                 # 缓存里存的都是同一个 Bi
    assert CJK.search(i18n.render(got[i18n.ZH][1], "zh"))
    assert not CJK.search(i18n.render(got[i18n.EN][1], "en"))
