"""一键更新重启时传给新进程的参数：和原来一模一样，不追加 --no-open（专家审查 F8 (c)，用户决定 D10）。

F8 (c) 起先打算在浏览器模式（带 --browser）的重启里追加 --no-open，让新进程不再另开一个标签页、只靠旧标签页
发现 build 变了自己重新载入（web/app.js reloadForBuild，tests/stale-tab.test.mjs）。第 1 轮审查指出这会削弱
一键更新后的启动检查（app/update_guard.py，D10）：main.py 带 --no-open 时，控制服务的端口一绑上就
update_guard.mark_ready()，在 Pipeline 初始化等启动步骤之前；不带时要等一个本机页面真的连上、拿完 hello 和回放
（app/server.py）才清记号。追加了，新版本只要在绑端口之后崩，记号每次都被清掉，再也不会自动退回——正是 D10 要防的
「新版本起不来」。所以重启参数照原样，浏览器模式更新后仍会多开一个新标签页（旧标签页也会刷新成新 JS）。
「只剩一个标签页」要先改 main.py（--no-open 等页面连上或启动走完再清记号），顺延给能改 main.py 的批次。

窗口模式本来就不能追加：那里 --no-open 的意思是「不开窗口、不进 .app」（app/macbundle.py NO_WINDOW_FLAGS，
计划审查 Q10），追加了用户更新完就看不到窗口。

不 import main（它在模块级就会 execv），不连网络，不真起 git、pip、execv（借 test_resilience_update 的替身）。
"""
import re
import sys
from pathlib import Path

import pytest

from app import macbundle
from app import settings as settings_mod
from app import updater as updater_mod
from tests.helpers import run
from tests.test_resilience_update import RecordingServer, make_updater

MAIN_PY = Path(__file__).resolve().parent.parent / "main.py"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(updater_mod, "ROOT", tmp_path)
    return tmp_path


@pytest.mark.parametrize("argv", [
    ["--browser"],                                   # 浏览器模式：不追加 --no-open（D10）
    ["--browser", "--port", "8770"],
    [],                                              # 窗口模式：照原样，照常开窗口
    ["--port", "8770", "--translator", "google"],
    ["--browser", "--no-open"],                      # 本来就带的照原样传下去
])
def test_the_update_restart_execs_with_the_same_arguments(monkeypatch, isolated, argv):
    """走一遍真的一键更新流程（git、pip、execv 是替身）：新进程拿到的参数和这一次启动时一样，一个不多。"""
    server, events = RecordingServer(), []
    u, _pip, execs = make_updater(monkeypatch, server, events)
    monkeypatch.setattr(sys, "argv", ["main.py"] + argv)

    run(u.apply())

    assert len(execs) == 1
    assert execs[0][1].endswith("main.py")
    assert execs[0][2:] == argv, "重启参数被改了：{} → {}".format(argv, execs[0][2:])
    if "--no-open" not in argv:
        assert "--no-open" not in execs[0], "带 --no-open 时端口一绑上就清掉一键更新后的启动检查记号（D10）"


@pytest.mark.parametrize("argv", [[], ["--port", "8770"], ["--demo"]])
def test_a_window_mode_restart_never_gains_a_flag_that_skips_the_window(monkeypatch, isolated, argv):
    """窗口模式重启后要照常进 .app、开窗口：重启参数里不能凭空多出任何一个「不开窗口」的参数。"""
    assert "--no-open" in macbundle.NO_WINDOW_FLAGS
    server, events = RecordingServer(), []
    u, _pip, execs = make_updater(monkeypatch, server, events)
    monkeypatch.setattr(sys, "argv", ["main.py"] + argv)

    run(u.apply())

    assert len(execs) == 1
    assert not set(execs[0][2:]) & set(macbundle.NO_WINDOW_FLAGS)


def test_main_still_clears_the_update_check_as_soon_as_a_no_open_server_binds():
    """绊线：上面不追加 --no-open 的理由，是 main.py 在 --no-open 时、Pipeline 初始化之前就 mark_ready。
    这条红了说明 main.py 改了这个顺序（比如改成等页面连上或启动走完再清）——那时可以回头再看 F8 (c) 的
    「更新后只剩一个标签页」（浏览器模式重启追加 --no-open）。只读 main.py 的文本，不 import 它。"""
    src = MAIN_PY.read_text(encoding="utf-8")
    block = re.search(r"if server is not None and args\.no_open:\n(?:[ \t]+.*\n)+?[ \t]+update_guard\.mark_ready\(\)", src)
    assert block, "main.py 里找不到 --no-open 时的 mark_ready"
    pipeline_at = src.find("Pipeline(args, server)")
    assert pipeline_at > 0
    assert block.start() < pipeline_at, "--no-open 的 mark_ready 已经挪到 Pipeline 之后：重新评估 F8 (c)"
