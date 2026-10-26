"""一键更新之后的存活确认与自动退回（专家审查 I6；用户 2026-10-22 定 D10「自动退回更新前的版本」）。

钉住的问题：一键更新合并完新代码就直接 execv，新版本要是一启动就崩，暂停中的直播不会恢复，也回不到
旧版。现在 execv 之前留记号；新版本真的跑了两次、两次本机页面都没连上，第三次打开就 git reset --keep 回到
更新前的提交、弹窗说明、重新 exec（用户 2026-10-26 按 D10 原话定；#145 第 1、2 轮是「跑一次没连上、第二次
就退回」）。页面连上之前强退一次、崩一次都不会被误退。计划审查 P5 的两种误触发也钉在这里：同时启动两个
进程不退回；没在监听时正常启动一次、页面一连上就清掉记号。

不 import main（它在模块级跑 ensure_env，会把 pytest 进程 execv 掉）：main.py 里只剩调用的位置，用语法树
核对；逻辑在 app/update_guard.py。git 有替身（状态机）也有真的（临时仓库里真的退回一次）。
"""
import ast
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import i18n
from app import settings as settings_mod
from app import update_guard
from app.server import CaptionServer
from tests.helpers import run

REPO = Path(__file__).resolve().parent.parent
FROM, TO = "f" * 40, "e" * 40


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """设置文件、锁文件都在临时目录；模块里「本进程计过数、拿着锁」的状态每个用例从零开始。"""
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(update_guard, "_lock_path", lambda root: tmp_path / "guard.lock")
    update_guard.release_lock()
    monkeypatch.setitem(update_guard._state, "armed", None)
    yield tmp_path
    update_guard.release_lock()


def new_process():
    """模拟上一个进程没了（崩了、被强退）：它拿着的锁由系统放掉，「计过数」的状态也跟着没了。"""
    update_guard.release_lock()
    update_guard._state["armed"] = None


def fake_git(head=TO, dirty="", reset_code=0, fail=None):
    calls = []

    def git(root, *args):
        calls.append(args)
        if fail == args[0]:
            return None, ""
        if args[0] == "rev-parse":
            return 0, head + "\n"
        if args[0] == "status":
            return 0, dirty
        if args[0] == "reset":
            return reset_code, ""
        raise AssertionError(args)

    git.calls = calls
    return git


def guard():
    return settings_mod.load_settings().get(update_guard.KEY)


def runs_without_the_page(isolated, times=2):
    """新版本跑了 times 次、每次页面都没连上就没了（崩了、被强退）。"""
    for _ in range(times):
        assert update_guard.on_boot(isolated, git=fake_git()).kind == "counted"
        new_process()


# ---- 状态机 ----------------------------------------------------------------------------------

def test_a_normal_restart_counts_once_and_the_page_clears_the_marker(isolated):
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0", now=100.0)
    assert guard()["boots"] == 0 and guard()["from"] == FROM and guard()["to"] == TO

    git = fake_git()
    outcome = update_guard.on_boot(isolated, git=git)
    assert outcome.kind == "counted" and not outcome.rolled_back and git.calls == []
    assert guard()["boots"] == 1

    assert update_guard._state["lock"] is not None                  # 等页面期间拿着锁
    assert update_guard.mark_ready() is True
    assert guard() is None
    assert update_guard._state["lock"] is None                      # 就绪了就放锁
    assert update_guard.mark_ready() is False                     # 只清一次


def test_one_run_without_the_page_does_not_roll_back(isolated, capsys):
    """D10 原话（用户 2026-10-26）：新版本只跑了一次、页面连上之前崩了或被强退——下一次照常跑新版本，
    不碰 git；那一次页面连上了就清记号，以后再也不退。"""
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    runs_without_the_page(isolated, times=1)
    assert guard()["boots"] == 1

    git = fake_git()
    outcome = update_guard.on_boot(isolated, git=git)               # 第 2 次：照常跑新版本
    assert outcome.kind == "counted" and not outcome.rolled_back and git.calls == []
    assert guard()["boots"] == 2
    out = capsys.readouterr().out
    assert "第 2 次启动" in out and "下次打开会自动退回 v1.0.0" in out   # 终端说清楚下一次会怎样

    assert update_guard.mark_ready() is True and guard() is None   # 这一次页面连上了
    for _ in range(3):
        new_process()
        assert update_guard.on_boot(isolated, git=git).kind == "none"
    assert git.calls == []


@pytest.mark.parametrize("ready_on", [1, 2])
def test_a_page_in_either_run_clears_the_marker(isolated, ready_on):
    """两次里任何一次页面连上了，记号就清掉：之后不管强退、崩几次都不退回。"""
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    git = fake_git()
    for run_no in (1, 2):
        assert update_guard.on_boot(isolated, git=git).kind == "counted"
        if run_no == ready_on:
            assert update_guard.mark_ready() is True
            break
        new_process()
    assert guard() is None
    for _ in range(3):
        new_process()
        assert update_guard.on_boot(isolated, git=git).kind == "none"
    assert git.calls == []


def test_two_runs_without_the_page_then_the_third_launch_rolls_back(isolated, capsys):
    settings_mod.save_setting("resume_after_update", {"url": "https://x.test/@fake_host/live",
                                                      "at": 100.0, "to_version": "1.1.0"})
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0", now=100.0)
    assert update_guard.on_boot(isolated, git=fake_git()).kind == "counted"
    # 新版本启动时读掉了「重启后接着监听」的记号（读到即删），然后在页面连上之前崩了
    settings_mod.save_setting("resume_after_update", None)
    new_process()
    assert update_guard.on_boot(isolated, git=fake_git()).kind == "counted"   # 第 2 次：照常跑新版本
    settings_mod.save_setting("resume_after_update", None)                    # 又读掉了、又崩了
    new_process()
    capsys.readouterr()

    git = fake_git()
    outcome = update_guard.on_boot(isolated, git=git)                         # 第 3 次：退回

    assert outcome.rolled_back
    assert git.calls == [("rev-parse", "HEAD"), ("status", "--porcelain", "--untracked-files=no"),
                         ("reset", "--keep", FROM)]
    assert guard() is None
    # 暂停中的监听要接得上：记号还回去，过没过期由旧版本按原来的 at 判断
    assert settings_mod.load_settings()["resume_after_update"]["url"] == "https://x.test/@fake_host/live"
    assert settings_mod.load_settings()["resume_after_update"]["at"] == 100.0
    assert outcome.message == ("一键更新到 v1.1.0 之后，新版本启动了两次，字幕界面都没有连上。这次启动已"
                               "自动退回更新前的 v1.0.0，程序会重新打开。")
    english = i18n.text(outcome.message, "en")
    assert english.startswith("After the update to v1.1.0, the new version started twice and the caption "
                              "window didn’t connect either time.")
    assert "went back to v1.0.0" in english and not i18n.CJK.search(english)
    out = capsys.readouterr().out
    assert "已退回更新前的 v1.0.0" in out                                       # 终端是中文
    assert out.index("正在退回更新前的 v1.0.0") < out.index("已退回更新前的 v1.0.0")  # reset 之前先说一声


def test_the_second_run_gets_back_a_resume_marker_the_first_run_used_up(isolated, capsys):
    """第 1 次启动读掉了「重启后接着监听」的记号（读到即删）、页面没连上就没了：第 2 次照常跑新版本，记号要
    还回去，暂停中的监听这一次就接得上（不然要等第 3 次退回才还，那时往往过了 5 分钟）。还回去的是原样，
    不带 rolled_back_from，at 不变（过没过期照旧由 pipeline 判断）。"""
    marker = {"url": "https://x.test/@fake_host/live", "at": 100.0, "to_version": "1.1.0",
              "from": FROM, "to": TO}
    settings_mod.save_setting("resume_after_update", marker)
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0", now=100.0)
    assert update_guard.on_boot(isolated, git=fake_git()).kind == "counted"
    settings_mod.save_setting("resume_after_update", None)
    new_process()
    capsys.readouterr()

    assert update_guard.on_boot(isolated, git=fake_git()).kind == "counted"
    assert settings_mod.load_settings()["resume_after_update"] == marker
    assert "已把记号还回去" in capsys.readouterr().out


def test_the_first_run_never_hands_out_the_resume_copy(isolated):
    """第 1 次启动（一键更新自己的 execv）不碰「重启后接着监听」的记号：没有就是没有（更新期间中控点过开始/
    停止，pipeline 就不留记号），不能从副本里变出来。"""
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    record = dict(guard(), resume={"url": "https://x.test/@fake_host/live", "at": 100.0})
    settings_mod.save_setting(update_guard.KEY, record)
    assert update_guard.on_boot(isolated, git=fake_git()).kind == "counted"
    assert settings_mod.load_settings().get("resume_after_update") is None


def test_a_resume_marker_still_present_is_left_as_it_is(isolated):
    """新版本两次都在读记号之前就崩了：记号还在，第 2 次和退回时都原样留着，不拿副本覆盖（副本的 at 故意
    和记号不同，看得出是哪一份）。"""
    settings_mod.save_setting("resume_after_update", {"url": "https://x.test/@fake_host/live", "at": 100.0})
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0", now=100.0)
    assert guard()["resume"]["at"] == 100.0
    settings_mod.save_setting("resume_after_update", {"url": "https://x.test/@fake_host/live", "at": 200.0})
    runs_without_the_page(isolated)
    assert settings_mod.load_settings()["resume_after_update"]["at"] == 200.0
    assert update_guard.on_boot(isolated, git=fake_git()).rolled_back
    assert settings_mod.load_settings()["resume_after_update"]["at"] == 200.0


@pytest.mark.parametrize("broken", ["disarm", "print", "message"])
def test_once_git_has_rolled_back_a_later_error_still_reports_the_rollback(isolated, monkeypatch, broken):
    """git reset --keep 成功之后，收尾的哪一步出错（清记号写不了设置、终端断了 print 抛 OSError、拼对话框文字
    出错）都照样交回「已退回」：异常漏出去会被 main.py 的兜底吃掉，内存里的新代码接着跑在已经退回的旧文件上
    （第 2 轮审查）。"""
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    runs_without_the_page(isolated)

    def boom(*_a, **_k):
        raise OSError("broken")

    if broken == "disarm":
        monkeypatch.setattr(update_guard, "disarm", boom)
    elif broken == "print":
        monkeypatch.setattr(update_guard, "print", boom, raising=False)
    else:
        monkeypatch.setattr(update_guard, "rollback_message", boom)
    git = fake_git()
    outcome = update_guard.on_boot(isolated, git=git)
    assert outcome.rolled_back and ("reset", "--keep", FROM) in git.calls
    if broken == "message":
        assert outcome.message is None
    else:
        assert outcome.message.startswith("一键更新到 v1.1.0 之后")
    assert update_guard._state["lock"] is not None                  # 退回了：锁留给 main.py 重新 exec 前放


def test_main_reexecs_even_when_the_rollback_dialog_fails():
    """弹窗那一步（先 print）出错也不能挡住重新 exec；没有对话框文字就不弹。Windows 的系统消息框（用户
    2026-10-26 定 Windows 也弹窗）跟在 _info_dialog 后面、自己一个 try：打印抛了错（终端没了），消息框照样弹。"""
    func = _main_funcs()["_check_update_guard"]
    tries = [n for n in func.body if isinstance(n, ast.Try)]

    def guarded(name):
        found = [i for i, n in enumerate(tries) if any(_calls(s, name) for s in n.body)]
        assert len(found) == 1, name
        node = tries[found[0]]
        assert ast.unparse(node.handlers[0].type) == "Exception"
        assert "if outcome.message" in ast.unparse(node)
        return found[0], node

    dialog_at, dialog = guarded("_info_dialog")
    notice_at, notice = guarded("windows_notice")
    assert dialog_at < notice_at and not _calls(dialog, "windows_notice")
    assert "update_guard.windows_notice(outcome.message)" in ast.unparse(notice)
    src = ast.unparse(func)
    assert src.index("windows_notice") < src.index("release_lock") < src.index("execv")


def test_only_a_lock_held_elsewhere_skips_the_count():
    """拿锁失败时，只有「被别的进程拿着」才算别的实例（这次启动不计数）；文件系统不支持锁之类的错当作没有
    这道保险、照常计数。以前 Windows 上任何错都算别的实例，就永远不会退回（第 2 轮审查）。"""
    import errno
    held = update_guard._held_elsewhere
    for code in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
        assert held(code, windows=False)
    for code in (errno.EACCES, errno.EDEADLK):
        assert held(code, windows=True)
    for windows in (False, True):
        assert not held(errno.ENOLCK, windows) and not held(errno.EIO, windows)
        assert not held(None, windows)


@pytest.mark.parametrize("git,kind", [
    (fake_git(dirty=" M app/server.py\n"), "dirty"),            # 程序文件夹被手动改过：不退
    (fake_git(head="c" * 40), "moved"),                         # 已经不是更新到的那个提交：不退
    (fake_git(reset_code=1), "reset-failed"),
    (fake_git(fail="rev-parse"), "git-failed"),
    (fake_git(fail="status"), "git-failed"),
])
def test_no_rollback_when_the_folder_is_not_what_the_update_left(isolated, git, kind):
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    runs_without_the_page(isolated)
    outcome = update_guard.on_boot(isolated, git=git)
    assert outcome.kind == kind and not outcome.rolled_back
    assert ("reset", "--keep", FROM) not in git.calls or kind == "reset-failed"
    assert guard() is None                                       # 记号作废，不会每次启动都再试
    assert update_guard._state["lock"] is None and update_guard._state["armed"] is None


def test_a_force_quit_after_the_page_connected_does_not_count(isolated):
    """D10「强行退出不算」：页面连上过，记号已经清了；之后不管怎么退出、再开几次都不退回。"""
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    update_guard.on_boot(isolated, git=fake_git())
    update_guard.mark_ready()
    for _ in range(3):
        new_process()
        assert update_guard.on_boot(isolated, git=fake_git()).kind == "none"


def test_no_marker_means_nothing_happens(isolated):
    git = fake_git()
    assert update_guard.on_boot(isolated, git=git).kind == "none" and git.calls == []
    assert update_guard.mark_ready() is False


def test_a_broken_marker_is_dropped(isolated):
    settings_mod.save_setting(update_guard.KEY, {"from": FROM})
    assert update_guard.on_boot(isolated, git=fake_git()).kind == "invalid"
    assert guard() is None


@pytest.mark.parametrize("field, value", [
    ("from", "--hard"), ("from", "-" + "f" * 39), ("from", "f" * 39), ("from", "F" * 40),
    ("from", "HEAD~1"), ("from", 123), ("to", "e" * 41), ("to", "e" * 40 + "\n")])
def test_a_marker_whose_commits_are_not_full_hashes_never_reaches_git(isolated, field, value):
    """from 会原样交给 git reset --keep：不是完整提交号的（比如以 - 开头，git 会当成选项）整个记号作废，
    一条 git 都不跑（第 1 轮审查）。"""
    record = {"from": FROM, "to": TO, "from_version": "1.0.0", "to_version": "1.1.0", "at": 1.0,
              "boots": 1, "resume": None}
    record[field] = value
    settings_mod.save_setting(update_guard.KEY, record)
    git = fake_git()
    assert update_guard.on_boot(isolated, git=git).kind == "invalid"
    assert git.calls == [] and guard() is None


def test_a_sha256_repository_hash_is_accepted(isolated):
    update_guard.arm("f" * 64, "e" * 64, "1.0.0", "1.1.0")
    assert update_guard.on_boot(isolated, git=fake_git(head="e" * 64)).kind == "counted"


def test_mark_ready_only_clears_the_marker_this_process_counted(isolated):
    """一键更新 execv 失败时旧进程还在跑、页面重连：它没计过数，不能替新版本清记号。"""
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    assert update_guard.mark_ready() is False and guard()["boots"] == 0


def test_a_port_probe_that_sees_another_instance_does_not_count(isolated):
    """--no-open、--url 不做单实例检查：调用方补的那次探测看到端口上有本程序，就不计数。"""
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    update_guard.on_boot(isolated, git=fake_git())
    new_process()
    outcome = update_guard.on_boot(isolated, others_running=lambda: True, git=fake_git())
    assert outcome.kind == "other-instance" and guard()["boots"] == 1


# ---- 两个进程同时启动：靠进程锁，第二个不计数（计划审查 P5） ------------------------------------

HOLDER = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app import settings, update_guard
settings.SETTINGS_FILE = Path(sys.argv[2])
update_guard._lock_path = lambda root: Path(sys.argv[3])
print("RESULT", update_guard.on_boot(sys.argv[1], git=lambda root, *a: (0, "")).kind, flush=True)
if sys.stdin.readline().strip() == "ready":
    print("RESULT", update_guard.mark_ready(), flush=True)
sys.stdin.readline()
"""


def _result(proc):
    """子进程的下一行 RESULT（中间那些是它打的终端行）。"""
    for _ in range(20):
        line = proc.stdout.readline()
        if line.startswith("RESULT "):
            return line.split(" ", 1)[1].strip()
        assert line, "子进程提前退出了"
    raise AssertionError("没等到 RESULT")


def test_two_processes_started_together_do_not_roll_back(isolated):
    """一键更新重启之后（第 1 次启动，还没等页面连上）又双击了一次：单实例检查看不见还没起服务的
    第一个进程，第二个进程拿不到锁，就不计数——不会把一个好好的新版本退回去。"""
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(REPO), str(isolated / "settings.json"),
         str(isolated / "guard.lock")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert _result(holder) == "counted"                        # 第一个进程：第 1 次启动
        assert guard()["boots"] == 1
        git = fake_git()
        outcome = update_guard.on_boot(isolated, git=git)          # 第二个进程同时起来
        assert outcome.kind == "other-instance" and git.calls == []
        assert guard()["boots"] == 1
        holder.stdin.write("ready\n")
        holder.stdin.flush()
        assert _result(holder) == "True"                           # 第一个进程的页面连上了
        assert guard() is None
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


def test_a_double_launch_during_the_second_run_does_not_count_either(isolated):
    """第 2 次启动（新版本第一次没连上、中控再打开）时又双击了一次：第二个进程照样不计数，boots 停在 2。
    第 2 次那个进程随后页面没连上就没了，下一次真正的启动才是第 3 次、才退回。"""
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    runs_without_the_page(isolated, times=1)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(REPO), str(isolated / "settings.json"),
         str(isolated / "guard.lock")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert _result(holder) == "counted"                        # 第 2 次启动
        assert guard()["boots"] == 2
        git = fake_git()
        assert update_guard.on_boot(isolated, git=git).kind == "other-instance"
        assert git.calls == [] and guard()["boots"] == 2
        holder.stdin.write("quit\n")                              # 页面没连上，进程没了
        holder.stdin.flush()
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    new_process()
    assert update_guard.on_boot(isolated, git=fake_git()).rolled_back


# ---- 「就绪」= 本机页面第一次连上（app/server.py） ----------------------------------------------

def _page_scenario(isolated, connect):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        server = CaptionServer(port=0)
        app = web.Application()
        app.router.add_get("/ws", server._ws)
        client = TestClient(TestServer(app))
        await client.start_server()
        server.port = client.port
        try:
            await connect(client, server)
        finally:
            await client.close()

    run(scenario())


def test_the_first_local_page_clears_the_marker_without_monitoring(isolated):
    """没在监听时正常启动一次：页面一连上就清掉记号（不挂在「重启后接着监听」之后——那一步没在监听时
    根本走不到）。没带 Origin 的本地脚本、浮窗页不算页面。"""
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    update_guard.on_boot(isolated, git=fake_git())

    async def connect(client, server):
        for extra in ({}, {"params": {"role": "float"},
                           "headers": {"Origin": "http://127.0.0.1:{}".format(server.port)}}):
            ws = await client.ws_connect("/ws", **extra)
            assert (await ws.receive_json())["type"] == "hello"
            await asyncio.sleep(0.05)
            await ws.close()
            assert guard() is not None, extra
        ws = await client.ws_connect("/ws", headers={"Origin": "http://127.0.0.1:{}".format(server.port)})
        assert (await ws.receive_json())["type"] == "hello"
        for _ in range(100):
            if guard() is None:
                break
            await asyncio.sleep(0.01)
        await ws.close()
        assert server._page_seen is True

    _page_scenario(isolated, connect)
    assert guard() is None


def test_a_failing_mark_ready_does_not_break_the_page(isolated, monkeypatch):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(update_guard, "mark_ready", boom)

    async def connect(client, server):
        ws = await client.ws_connect("/ws", headers={"Origin": "http://127.0.0.1:{}".format(server.port)})
        assert (await ws.receive_json())["type"] == "hello"
        await ws.send_json({"type": "noop"})
        await asyncio.sleep(0.05)
        assert not ws.closed
        await ws.close()

    _page_scenario(isolated, connect)


# ---- 真的 git：临时仓库里退回一次 ----------------------------------------------------------------

def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_rollback_with_real_git_restores_the_previous_commit(isolated, monkeypatch):
    empty = isolated / "gitconfig"
    empty.write_text("", encoding="utf-8")
    for key, value in {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    repo = isolated / "app-folder"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (repo / "main.py").write_text("print('old')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "old")
    old = _git(repo, "rev-parse", "HEAD")
    (repo / "VERSION").write_text("1.1.0\n", encoding="utf-8")
    (repo / "main.py").write_text("raise SystemExit('crash on start')\n", encoding="utf-8")
    (repo / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "new")
    new = _git(repo, "rev-parse", "HEAD")
    (repo / "settings.json.bak").write_text("{}", encoding="utf-8")   # 未跟踪的杂物不算「改过」

    update_guard.arm(old, new, "1.0.0", "1.1.0")
    for _ in range(2):                                             # 新版本跑了两次，页面都没连上
        assert update_guard.on_boot(repo).kind == "counted"
        new_process()
    assert _git(repo, "rev-parse", "HEAD") == new
    outcome = update_guard.on_boot(repo)

    assert outcome.rolled_back
    assert _git(repo, "rev-parse", "HEAD") == old
    assert _git(repo, "rev-parse", "main") == old                  # 分支跟着回去，以后照常快进
    assert (repo / "main.py").read_text(encoding="utf-8") == "print('old')\n"
    assert not (repo / "new_module.py").exists()
    assert (repo / "settings.json.bak").exists()                   # 未跟踪的文件不动
    assert guard() is None


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_real_git_with_a_modified_file_is_left_alone(isolated, monkeypatch):
    empty = isolated / "gitconfig"
    empty.write_text("", encoding="utf-8")
    for key, value in {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    repo = isolated / "app-folder"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "main.py").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "old")
    old = _git(repo, "rev-parse", "HEAD")
    (repo / "main.py").write_text("new\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "new")
    new = _git(repo, "rev-parse", "HEAD")
    (repo / "main.py").write_text("hand edit\n", encoding="utf-8")

    update_guard.arm(old, new, "1.0.0", "1.1.0")
    for _ in range(2):
        assert update_guard.on_boot(repo).kind == "counted"
        new_process()
    assert update_guard.on_boot(repo).kind == "dirty"
    assert _git(repo, "rev-parse", "HEAD") == new
    assert (repo / "main.py").read_text(encoding="utf-8") == "hand edit\n"


# ---- git reset --keep 不设时限（#145 审查：超时被杀在半路，会留下半退回的工作区和 index.lock） ----------

def test_only_the_read_only_git_calls_have_a_time_limit(isolated, monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append((argv[1], kw.get("timeout")))
        out = {"rev-parse": TO + "\n", "status": ""}.get(argv[1], "")
        return subprocess.CompletedProcess(argv, 0, out.encode(), b"")

    monkeypatch.setattr(update_guard.subprocess, "run", fake_run)
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    runs_without_the_page(isolated)
    assert update_guard.on_boot(isolated).rolled_back                # 用模块自己的 _git
    assert seen == [("rev-parse", update_guard.GIT_TIMEOUT_SEC), ("status", update_guard.GIT_TIMEOUT_SEC),
                    ("reset", None)]


@pytest.mark.skipif(shutil.which("git") is None or sys.platform == "win32",
                    reason="需要 git 和 /bin/sh（替身 git 是 sh 脚本）")
def test_a_slow_reset_is_waited_for_not_killed(isolated, monkeypatch):
    """真的 git，但 reset 比只读命令的时限慢：照样等它做完——退回成功、HEAD 回到更新前、不留 index.lock。
    以前同样受 30 秒时限管，时限一到 subprocess 就杀进程（这里把时限缩到 3 秒、reset 慢 4 秒来模拟；
    rev-parse、status 也经过 sh 替身、受这 3 秒管，留足余量，机器很忙时也不会误超时）。"""
    empty = isolated / "gitconfig"
    empty.write_text("", encoding="utf-8")
    for key, value in {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    repo = isolated / "app-folder"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "main.py").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "old")
    old = _git(repo, "rev-parse", "HEAD")
    (repo / "main.py").write_text("new\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "new")
    new = _git(repo, "rev-parse", "HEAD")

    shim = isolated / "bin"
    shim.mkdir()
    (shim / "git").write_text('#!/bin/sh\nif [ "$1" = reset ]; then sleep 4; fi\nexec "{}" "$@"\n'.format(
        shutil.which("git")), encoding="utf-8")
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", "{}{}{}".format(shim, os.pathsep, os.environ["PATH"]))
    monkeypatch.setattr(update_guard, "GIT_TIMEOUT_SEC", 3)

    update_guard.arm(old, new, "1.0.0", "1.1.0")
    for _ in range(2):
        assert update_guard.on_boot(repo).kind == "counted"
        new_process()
    outcome = update_guard.on_boot(repo)

    assert outcome.rolled_back
    assert _git(repo, "rev-parse", "HEAD") == old
    assert (repo / "main.py").read_text(encoding="utf-8") == "old\n"
    assert not (repo / ".git" / "index.lock").exists()
    assert _git(repo, "status", "--porcelain") == ""


# ---- Windows 也弹窗（用户 2026-10-26 定）：系统自带的消息框，替身 ctypes，不真弹 ---------------------

class FakeUser32:
    def __init__(self, fail=False, gate=None):
        self.calls, self.fail, self.gate = [], fail, gate

    def MessageBoxW(self, hwnd, text, title, flags):
        self.calls.append((hwnd, text, title, flags))
        if self.gate is not None:
            self.gate.wait(5)                     # 真的 MessageBoxW 要等人点「确定」才返回
        if self.fail:
            raise OSError("no desktop")
        return 1


def fake_windll(**kw):
    from types import SimpleNamespace
    return SimpleNamespace(user32=FakeUser32(**kw))


def _now(fn):
    fn()


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_windows_shows_the_rollback_in_a_system_message_box(lang):
    msg = update_guard.rollback_message("1.0.0", "1.1.0")
    windll = fake_windll()
    with i18n.use(lang):
        assert update_guard.windows_notice(msg, os_name="nt", environ={}, windll=windll, start=_now) is True
    [(hwnd, text, title, flags)] = windll.user32.calls
    assert hwnd is None and type(text) is str and type(title) is str     # 交给 ctypes 的是普通 str
    assert text == i18n.text(msg, lang) and title == i18n.text(i18n.APP_NAME, lang)
    assert ("went back to v1.0.0" in text) if lang == "en" else ("已自动退回更新前的 v1.0.0" in text)
    assert flags == update_guard.MB_ICONINFORMATION | update_guard.MB_SETFOREGROUND | update_guard.MB_TOPMOST


def test_the_windows_message_box_does_not_hold_up_the_relaunch():
    """MessageBoxW 等人点才返回：放在守护线程里弹，windows_notice 马上返回，旧版本照常马上起来。"""
    import threading
    gate = threading.Event()
    windll = fake_windll(gate=gate)
    try:
        assert update_guard.windows_notice("已退回", os_name="nt", environ={}, windll=windll) is True
        for _ in range(200):
            if windll.user32.calls:
                break
            gate.wait(0.01)
        assert len(windll.user32.calls) == 1                       # 已经弹出来、还没人点
        box = next(t for t in threading.enumerate() if t.name == "update-rollback-notice")
        assert box.daemon and box.is_alive()
    finally:
        gate.set()


def test_the_windows_message_box_uses_ctypes_windll_by_default(monkeypatch):
    from types import SimpleNamespace
    windll = fake_windll()
    monkeypatch.setitem(sys.modules, "ctypes", SimpleNamespace(windll=windll))
    assert update_guard.windows_notice("已退回", os_name="nt", environ={}, start=_now) is True
    assert [c[1] for c in windll.user32.calls] == ["已退回"]


@pytest.mark.parametrize("case", ["not-windows", "ci", "empty", "no-windll", "box-fails", "no-thread"])
def test_no_windows_message_box_is_quietly_left_to_the_printed_line(monkeypatch, case):
    """Mac / Linux 不走这里（Mac 照旧由 _info_dialog 弹）；CI 不弹；没有 windll、MessageBoxW 出错、起不了线程
    都不抛——main.py 已经打印过那句话。"""
    from types import SimpleNamespace
    windll = fake_windll(fail=case == "box-fails")
    kw = {"os_name": "nt", "environ": {}, "windll": windll, "start": _now}
    if case == "not-windows":
        kw["os_name"] = "posix"
    elif case == "ci":
        kw["environ"] = {"CI": "1"}
    elif case == "no-windll":
        kw["windll"] = None
        monkeypatch.setitem(sys.modules, "ctypes", SimpleNamespace())     # 没有 windll（非 Windows 的 ctypes）
    elif case == "no-thread":
        def no_thread(_fn):
            raise RuntimeError("can't start new thread")
        kw["start"] = no_thread
    message = "" if case == "empty" else "已退回"
    shown = update_guard.windows_notice(message, **kw)
    assert shown is (case == "box-fails")                          # 交给了系统、系统那边出错：吞掉
    if case != "box-fails":
        assert windll.user32.calls == []


# ---- 一键更新留记号（app/updater.py）、恢复记号带上提交号（app/pipeline.py） -----------------------

def test_the_update_arms_the_guard_and_the_resume_marker_names_both_commits(monkeypatch, isolated):
    from app import updater as updater_mod
    from tests.test_resilience_update import TARGET, RecordingServer, make_live_pipeline, make_updater
    monkeypatch.setattr(updater_mod, "ROOT", isolated)
    server, events = RecordingServer(), []
    u, _pip, execs = make_updater(monkeypatch, server, events)
    p, _state = make_live_pipeline(server, events, isolated)
    p.updater = u

    run(p._apply_update())

    assert execs
    marker = settings_mod.load_settings()["resume_after_update"]
    assert marker["from"] == "b" * 40 and marker["to"] == TARGET
    armed = guard()
    assert (armed["from"], armed["to"], armed["boots"]) == ("b" * 40, TARGET, 0)
    assert (armed["from_version"], armed["to_version"]) == ("1.0.0", "9.9.9")
    assert armed["resume"] == marker                               # 抄了一份，退回时还得回去


def test_an_update_without_a_known_head_restarts_without_the_guard(monkeypatch, isolated, capsys):
    from app import updater as updater_mod
    from tests.test_resilience_update import RecordingServer, make_updater
    monkeypatch.setattr(updater_mod, "ROOT", isolated)
    server, events = RecordingServer(), []
    u, _pip, execs = make_updater(monkeypatch, server, events,
                                  git_overrides={"rev-parse:HEAD": (1, "", "")})
    run(u.apply())
    assert execs and guard() is None
    assert "不会自动退回" in capsys.readouterr().out


def test_after_a_rollback_the_resume_says_so_and_the_audit_reason_differs(isolated, monkeypatch):
    """退回之后旧版本接着听：状态行不能说「已更新到」，session_start 的 resumed_after 记 update_rolled_back。"""
    from app.pipeline import Pipeline
    from app import provenance
    from tests.test_resilience_update import RecordingServer
    monkeypatch.setattr(provenance, "app_version", lambda: "1.0.0")
    settings_mod.save_setting("resume_after_update", {"url": "https://www.tiktok.com/@fake_host/live",
                                                      "at": 1000.0, "to_version": "1.1.0"})
    update_guard.arm(FROM, TO, "1.0.0", "1.1.0")
    runs_without_the_page(isolated)
    assert update_guard.on_boot(isolated, git=fake_git()).rolled_back
    marker = settings_mod.load_settings()["resume_after_update"]
    assert marker["rolled_back_from"] == "1.1.0" and marker["at"] == 1000.0

    p = Pipeline.__new__(Pipeline)
    p.server = RecordingServer()
    starts = []

    async def start_stream(url, media=None):
        starts.append(url)

    p.start_stream = start_stream
    assert run(p.resume_after_update(now=1030.0)) is True
    assert starts == ["https://www.tiktok.com/@fake_host/live"]
    assert p._resume_reason == "update_rolled_back"
    detail = p.server.statuses()[-1][1]
    assert detail == "一键更新到 v1.1.0 之后字幕界面一直没有连上，已退回 v1.0.0，正在自动恢复监听 @fake_host…"
    assert i18n.text(detail, "en") == ("After the update to v1.1.0, the caption window never connected. "
                                       "The app went back to v1.0.0. Resuming monitoring @fake_host…")


# ---- main.py 的调用位置（不 import main，读语法树） ------------------------------------------------

def _main_funcs():
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)]


def test_main_checks_the_guard_only_after_the_single_instance_check():
    """计划审查 P5：在单实例检查之前计数，双击两次就会把好好的新版本退回去。窗口模式在 run_with_window
    的检查（已有实例就 return）之后、起后台服务之前；浏览器模式在 main() 的检查之后；--no-open、--url
    不做单实例检查，传一个探端口的 others_running 进去。"""
    funcs = _main_funcs()
    window = funcs["run_with_window"]
    body = window.body
    probe = next(i for i, s in enumerate(body) if _calls(s, "_existing_instance_url"))
    early_return = next(i for i, s in enumerate(body) if isinstance(s, ast.If)
                        and ast.unparse(s.test) == "existing"
                        and any(isinstance(x, ast.Return) for x in s.body))
    check = next(i for i, s in enumerate(body) if _calls(s, "_check_update_guard"))
    backend = next(i for i, s in enumerate(body) if isinstance(s, ast.Assign)
                   and _calls(s, "Thread"))
    assert probe < early_return < check < backend

    main = funcs["main"]
    calls = _calls(main, "_check_update_guard")
    assert len(calls) == 2
    plain = next(c for c in calls if not c.args)
    probed = next(c for c in calls if c.args)
    assert "_existing_instance_url" in ast.unparse(probed.args[0])
    # 不带探测的那一处跟在浏览器模式的单实例检查之后（同一个 if 分支里、检查后面）
    branch = next(n for n in ast.walk(main) if isinstance(n, ast.If)
                  and "args.no_open" in ast.unparse(n.test) and "args.url" in ast.unparse(n.test))
    order = [i for i, s in enumerate(branch.body) if _calls(s, "_existing_instance_url")]
    at = next(i for i, s in enumerate(branch.body) if plain in ast.walk(s))
    assert order and order[0] < at
    assert any(probed in ast.walk(s) for s in branch.orelse)


def test_main_reexecs_main_py_after_a_rollback_and_shows_the_message():
    func = _main_funcs()["_check_update_guard"]
    src = ast.unparse(func)
    assert "update_guard.on_boot(ROOT, others_running=others_running)" in src
    assert "_info_dialog(outcome.message)" in src                  # 不阻塞的系统提示框
    assert "main.py" in src and _calls(func, "execv") and _calls(func, "replace_process")
    assert src.index("_info_dialog") < src.index("release_lock") < src.index("execv")


def test_a_failed_reexec_after_a_rollback_says_so_and_exits():
    """git reset 已经成功、重新 exec 却抛了异常：不能带着 traceback 从 main() 崩出去，更不能让内存里的新代码
    接着跑在旧文件上——打一行中文、弹窗说再开一次、退出（第 1 轮审查）。"""
    func = _main_funcs()["_check_update_guard"]
    tries = [n for n in ast.walk(func) if isinstance(n, ast.Try)
             and any(_calls(s, "execv") for s in n.body)]
    assert len(tries) == 1
    body = tries[0].body
    assert any(_calls(s, "replace_process") for s in body)            # Windows 那条也在里面
    handler = tries[0].handlers[0]
    assert ast.unparse(handler.type) == "Exception"
    src = ast.unparse(handler)
    assert "print(" in src and "[错误]" in src
    assert _calls(handler, "_fail_alert") and "L(" in src
    assert isinstance(handler.body[-1], ast.Raise) and "SystemExit(1)" in ast.unparse(handler.body[-1])


def test_no_open_mode_counts_as_ready_once_the_service_is_up():
    """--no-open 不开页面：「页面连上」可能永远等不到，服务起来了就算就绪，否则下一次启动会把好好的
    新版本退回去。"""
    body = _main_funcs()["main_async"]
    src = ast.unparse(body)
    assert "update_guard.mark_ready()" in src
    ready_if = next(n for n in ast.walk(body) if isinstance(n, ast.If)
                    and "args.no_open" in ast.unparse(n.test) and _calls(n, "mark_ready"))
    assert "server is not None" in ast.unparse(ready_if.test)


def test_the_guard_lock_lives_outside_the_program_folder(tmp_path):
    path = update_guard._lock_path(tmp_path)
    assert tmp_path not in path.parents and path.name.startswith("tlt-update-guard-")
    assert update_guard._lock_path(tmp_path) == path and update_guard._lock_path(tmp_path / "x") != path


def test_a_held_lock_in_this_process_is_kept(isolated):
    assert update_guard._take_lock(isolated) is True
    fd = update_guard._state["lock"]
    assert update_guard._take_lock(isolated) is True and update_guard._state["lock"] == fd
    update_guard.release_lock()
    assert update_guard._state["lock"] is None


def test_rollback_message_takes_versions_with_or_without_the_v():
    msg = update_guard.rollback_message("v0.18.0", "0.19.0")
    assert msg == ("一键更新到 v0.19.0 之后，新版本启动了两次，字幕界面都没有连上。这次启动已自动退回"
                   "更新前的 v0.18.0，程序会重新打开。")
    assert "v0.19.0" in i18n.text(msg, "en") and "vv" not in i18n.text(msg, "en")
