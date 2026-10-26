"""一键更新重启时传给新进程的参数（专家审查 F8 (c)，计划审查 Q10）。

浏览器模式下以前每次重启都再开一个新标签页，点更新的那个旧标签页也重连上来，新旧两个控制页同时连着。
现在只在已经带 --browser 时追加 --no-open：新进程不开页面，旧标签页发现 build 变了自己重新载入
（tests/stale-tab.test.mjs）。窗口模式不追加——那里 --no-open 的意思是「不开窗口、不进 .app」
（app/macbundle.py NO_WINDOW_FLAGS），追加了用户更新完就看不到窗口。

不 import main（它在模块级就会 execv），不连网络，不真起 git、pip、execv（借 test_resilience_update 的替身）。
"""
import sys

import pytest

from app import macbundle
from app import settings as settings_mod
from app import updater as updater_mod
from app.updater import relaunch_argv
from tests.helpers import run
from tests.test_resilience_update import RecordingServer, make_updater


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(updater_mod, "ROOT", tmp_path)
    return tmp_path


@pytest.mark.parametrize("argv,want", [
    (["--browser"], ["--browser", "--no-open"]),
    (["--browser", "--port", "8770"], ["--browser", "--port", "8770", "--no-open"]),
    (["--browser", "--no-open"], ["--browser", "--no-open"]),          # 不重复加
    ([], []),                                                           # 窗口模式：照原样
    (["--port", "8770", "--translator", "google"], ["--port", "8770", "--translator", "google"]),
    (["--no-open"], ["--no-open"]),
])
def test_only_browser_mode_gets_no_open(argv, want):
    before = list(argv)
    assert relaunch_argv(argv) == want
    assert argv == before, "不改调用方的列表（那是 sys.argv）"


def test_window_mode_keeps_the_flags_that_would_skip_the_window_out():
    """窗口模式重启后要照常进 .app、开窗口：relaunch_argv 不能凭空带出任何一个「不开窗口」的参数。"""
    assert "--no-open" in macbundle.NO_WINDOW_FLAGS
    for argv in ([], ["--port", "8770"], ["--demo"]):
        assert not set(relaunch_argv(argv)) & set(macbundle.NO_WINDOW_FLAGS)


@pytest.mark.parametrize("argv,want_no_open", [
    (["--browser"], True),
    ([], False),
])
def test_the_update_restart_execs_with_the_relaunch_argv(monkeypatch, isolated, argv, want_no_open):
    """走一遍真的一键更新流程（git、pip、execv 是替身）：浏览器模式重启的参数里有 --no-open，窗口模式没有。"""
    server, events = RecordingServer(), []
    u, _pip, execs = make_updater(monkeypatch, server, events)
    monkeypatch.setattr(sys, "argv", ["main.py"] + argv)

    run(u.apply())

    assert len(execs) == 1
    assert execs[0][1].endswith("main.py")
    assert execs[0][2:] == argv + (["--no-open"] if want_no_open else [])
