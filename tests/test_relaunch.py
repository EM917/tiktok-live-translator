"""换进 .venv 的那一跳（2026-10-21 专家审查 I11、I9），在 app/relaunch.py。

I11 Windows：CRT 的 execv 是「起新进程、自己立刻以 0 退出」，Start.bat 的「出错就 pause」永远
轮不到。replace_process 在 Windows 上起子进程、等它、带着它的退出码退出；POSIX 上照旧 exec。
钉住：os.name 是 nt 时走子进程并传递返回码（含 32 位无符号的退出码、拿不到退出码）、Ctrl-C
不杀子进程；POSIX 上不起子进程。一键更新的重启仍然是 execv（要放开端口）。

I9 macOS：第一跳开窗口时直接换进 .app 壳里的 python（main.py 在 ensure_env 末尾调
macbundle.relaunch_inside_bundle，和 main() 开头是同一个函数），省掉 .venv/bin/python 那一跳；不该进壳、
壳没备好或起不来就照旧进 .venv/bin/python。「完全磁盘访问权限」认的两个文件不变：启动解释器
（TLT_LAUNCH_PYTHON，第一跳就记下了）和最终进程的解释器（壳里的 python）。

测试里绝不真的 exec、不真的起子进程。Ctrl-C 那条要让替身抛真的 KeyboardInterrupt（replace_process 接的
就是它），所以 _windows_run 把它就地接住、改报这一条失败：回归时只红这一条，不会让整场 pytest 以退出码 2
中断、后面的测试全不跑。
"""
import ast
import os
import sys
from pathlib import Path

import pytest

from app import browser_login, macbundle, relaunch

REPO = Path(__file__).resolve().parent.parent
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="壳靠符号链接；功能仅 macOS 生效")


class FakeProc:
    def __init__(self, codes):
        self.codes = list(codes)
        self.waits = 0

    def wait(self):
        self.waits += 1
        item = self.codes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _windows_run(monkeypatch, codes, argv=None, env=None):
    """os.name 打桩成 nt 跑一次 replace_process；只包住这一个调用。"""
    calls = {"popen": [], "exit": [], "execv": []}
    proc = FakeProc(codes)

    def popen(args, env=None):
        calls["popen"].append((list(args), env))
        return proc

    with monkeypatch.context() as m:
        m.setattr(relaunch.os, "name", "nt")
        try:
            relaunch.replace_process(
                argv or [r"C:\Users\Zhang San\app\.venv\Scripts\python.exe",
                         r"C:\Users\Zhang San\app\main.py", "--port", "8799"],
                env=env, popen=popen, exit_=calls["exit"].append,
                execv=lambda *a: calls["execv"].append(a))
        except KeyboardInterrupt:
            # 替身抛的 Ctrl-C 漏出了 replace_process：只让这一条红。不接住的话 pytest 把它当成
            # 用户按了 Ctrl-C，整场以退出码 2 中断
            pytest.fail("replace_process 把 Ctrl-C 抛了出来，没有接着等子进程（等了 {} 次）"
                        .format(proc.waits), pytrace=False)
    return calls, proc


# ---- I11 replace_process -----------------------------------------------------------------------

def test_windows_waits_for_the_child_and_exits_with_its_code(monkeypatch):
    calls, proc = _windows_run(monkeypatch, [3])
    # 子进程拿到的是原样的参数列表：subprocess 自己按 Windows 规则加引号，不能再套一层 exec_args
    assert calls["popen"] == [([r"C:\Users\Zhang San\app\.venv\Scripts\python.exe",
                                r"C:\Users\Zhang San\app\main.py", "--port", "8799"], None)]
    assert calls["exit"] == [3] and calls["execv"] == []


def test_windows_passes_a_clean_exit_through(monkeypatch):
    calls, _ = _windows_run(monkeypatch, [0], env={"A": "1"})
    assert calls["exit"] == [0]
    assert calls["popen"][0][1] == {"A": "1"}


def test_ctrl_c_in_the_parent_keeps_waiting_for_the_child(monkeypatch):
    """控制台把 Ctrl-C 同时送给父子进程：子进程自己收尾（存证据再退出），父进程不能像
    subprocess.call 那样把它杀掉，只接着等，最后带着它的退出码退出。"""
    calls, proc = _windows_run(monkeypatch, [KeyboardInterrupt(), KeyboardInterrupt(), 0])
    assert proc.waits == 3 and calls["exit"] == [0]


@pytest.mark.parametrize("code,expect", [
    (0xC000013A, 0xC000013A - (1 << 32)),     # 按 Ctrl-C 关掉的退出码：32 位无符号
    (0xFFFFFFFF, -1),
    (1, 1),
    (None, 1),
])
def test_windows_exit_codes_fit_os_exit(monkeypatch, code, expect):
    calls, _ = _windows_run(monkeypatch, [code])
    assert calls["exit"] == [expect]
    assert expect & 0xFFFFFFFF == (code if code is not None else 1) & 0xFFFFFFFF


def test_windows_start_failure_is_raised_to_the_caller(monkeypatch):
    def popen(args, env=None):
        raise OSError("not found")

    exits = []
    with monkeypatch.context() as m:
        m.setattr(relaunch.os, "name", "nt")
        with pytest.raises(OSError):
            relaunch.replace_process(["py.exe", "main.py"], popen=popen, exit_=exits.append)
    assert exits == []


def test_buffered_output_is_written_out_before_the_process_is_replaced(monkeypatch):
    """exec 不替 Python 冲缓冲：stdout 接到管道或文件时，exec 之前打的 [初始化] 几行会丢。"""
    import io

    class Buffered(io.StringIO):
        flushed = 0

        def flush(self):
            Buffered.flushed += 1

    out, err = Buffered(), Buffered()
    monkeypatch.setattr(relaunch.sys, "stdout", out)
    monkeypatch.setattr(relaunch.sys, "stderr", err)
    relaunch.flush_std()
    assert Buffered.flushed == 2
    monkeypatch.setattr(relaunch.sys, "stdout", None)            # pythonw：没有 stdout 也不出错
    relaunch.flush_std()


def test_posix_execs_in_place_and_never_spawns(monkeypatch):
    seen = []

    def popen(*a, **k):
        raise AssertionError("POSIX 上不该起子进程")

    relaunch.replace_process(["/p/.venv/bin/python", "/p/main.py", "--x"], os_name="posix",
                             execv=lambda exe, argv: seen.append(("execv", exe, argv)),
                             execve=lambda exe, argv, env: seen.append(("execve", exe, argv, env)),
                             popen=popen, exit_=lambda c: seen.append(("exit", c)))
    relaunch.replace_process(["/p/app/python", "/p/main.py"], env={"TLT_IN_BUNDLE": "1"},
                             os_name="posix",
                             execv=lambda exe, argv: seen.append(("execv", exe, argv)),
                             execve=lambda exe, argv, env: seen.append(("execve", exe, argv, env)),
                             popen=popen, exit_=lambda c: seen.append(("exit", c)))
    assert seen == [("execv", "/p/.venv/bin/python", ["/p/.venv/bin/python", "/p/main.py", "--x"]),
                    ("execve", "/p/app/python", ["/p/app/python", "/p/main.py"],
                     {"TLT_IN_BUNDLE": "1"})]


# ---- I9 第一跳直接进 .app 壳 -------------------------------------------------------------------

def _first_hop(monkeypatch, tmp_path):
    """一个真的项目形状：.venv/bin/python 链到「发行版的 python3.13」；当前进程装成第一跳
    （启动器选的那个解释器，不在壳里、没有防循环变量、不在 pytest 里）。"""
    base = tmp_path / "anaconda3" / "bin"
    base.mkdir(parents=True)
    (base / "python3.13").write_bytes(b"python")
    root = tmp_path / "project"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").symlink_to(base / "python3.13")
    (root / ".venv" / "pyvenv.cfg").write_text("home = {}\nversion = 3.13.5\n".format(base),
                                               encoding="utf-8")
    monkeypatch.delenv(macbundle.ENV_GUARD, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(macbundle.sys, "platform", "darwin")
    monkeypatch.setattr(macbundle.sys, "executable", str(base / "python3.13"))
    return root, base / "python3.13"


@posix_only
def test_the_first_hop_lands_in_the_bundle_with_the_same_identity(monkeypatch, tmp_path):
    """计划审查 Q8 第 2 条：跳转链改了之后，「完全磁盘访问权限」的步骤列出的文件不能变——启动解释器
    （第一跳在任何 exec 之前记下）和最终进程的解释器（壳里的 python，链回 .venv/bin/python 链到的
    那个文件）。第一跳直接进壳，exec 的目标、参数、环境要和以前第二跳在 main() 里进壳时一样；推迟安装
    的记号（I7）也要跟着进去。零请求，真的建壳，假的 execve。"""
    root, base_python = _first_hop(monkeypatch, tmp_path)
    # 第一跳顶上 remember_launch_python 记下的样子（直接改 os.environ 会漏到别的测试里，经 monkeypatch 设）
    launch = macbundle.remember_launch_python({}, str(base_python))
    monkeypatch.setenv(macbundle.LAUNCH_PYTHON_ENV, launch)
    monkeypatch.setenv("TLT_INSTALL_DEFERRED", "4242")
    seen = []
    ok = macbundle.relaunch_inside_bundle(root, ["main.py", "--port", "8799"],
                                          execve=lambda path, argv, env: seen.append((path, argv, env)))
    assert ok and len(seen) == 1
    path, argv, env = seen[0]
    assert path == str(root / macbundle.APP_DIR_NAME / "Contents" / "MacOS" / "python")
    assert argv == [path, str(root / "main.py"), "--port", "8799"]
    assert env[macbundle.ENV_GUARD] == "1"
    assert env[macbundle.LAUNCH_PYTHON_ENV] == launch == os.path.realpath(str(base_python))
    assert env["TLT_INSTALL_DEFERRED"] == "4242"
    assert macbundle.inside_bundle(path)
    assert browser_login.fda_targets(env, executable=path) == [os.path.realpath(str(base_python))]


@posix_only
def test_without_a_window_the_first_hop_enters_the_venv_python_as_before(monkeypatch, tmp_path):
    root, _base = _first_hop(monkeypatch, tmp_path)
    seen = []
    for flag in ("--browser", "--no-open"):
        assert macbundle.relaunch_inside_bundle(
            root, ["main.py", flag], execve=lambda *a: seen.append(a)) is False
    assert seen == []


# ---- 接线：main.py 怎么换进 .venv；一键更新重启仍是 execv ------------------------------------

def _ensure_env():
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ensure_env")


def _stmt_index(body, text):
    hits = [i for i, s in enumerate(body) if text in ast.unparse(s)]
    assert len(hits) == 1, (text, hits)
    return hits[0]


def test_ensure_env_tries_the_bundle_then_waits_on_windows_then_execs_on_posix():
    """第一跳最后一步的顺序：macOS 先试直接进壳（I9，进去了就不会回来）；Windows 起子进程等它（I11，
    不会回来）；剩下的 POSIX 照旧 execv 进 .venv/bin/python 跑 main.py。"""
    ensure = _ensure_env()
    block = next(n for n in ast.walk(ensure) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "not in_project_venv"
                 and "exec_args" in ast.unparse(n))
    body = block.body
    bundle = _stmt_index(body, "relaunch_inside_bundle(ROOT)")
    assert ast.unparse(body[bundle].test) == "sys.platform == 'darwin'"
    argv = _stmt_index(body, "argv = [str(vpy), str(ROOT / 'main.py')] + sys.argv[1:]")
    windows = _stmt_index(body, "replace_process(argv)")
    assert ast.unparse(body[windows].test) == "os.name == 'nt'"
    posix = _stmt_index(body, "os.execv(str(vpy), exec_args(argv))")
    assert bundle < argv < windows < posix == len(body) - 1
    # 进壳之前先放锁（exec 不会执行 finally）、把开着的提示框交给最终进程（不关：换进去之后还要等后端才开窗口，
    # 见 tests/test_bootstrap_launch.py）、冲掉输出缓冲，推迟安装的记号也先记下
    assert _stmt_index(body, "lock.unlink()") < bundle
    assert _stmt_index(body, "_hand_off_dialogs()") < bundle
    assert _stmt_index(body, "mark_install_deferred()") < bundle
    flushes = [i for i, s in enumerate(body) if ast.unparse(s) == "flush_std()"]
    assert len(flushes) == 2 and flushes[0] < bundle
    # 进壳没成功时 relaunch_inside_bundle 打的那行「未能以应用身份重新启动」在第一次冲缓冲之后：
    # execv 紧前面再冲一次，stdout 是管道时它才不会丢（Windows 那支 replace_process 自己冲）
    assert flushes[1] == posix - 1


def test_the_update_restart_still_execs_in_place():
    """一键更新的重启要放开端口：Windows 上也不能留着旧进程等新进程。"""
    src = (REPO / "app" / "updater.py").read_text(encoding="utf-8")
    assert "os.execv(sys.executable, exec_args(" in src
    assert "replace_process" not in src
