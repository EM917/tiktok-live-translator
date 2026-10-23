"""换进 .venv 的那一跳（2026-10-21 专家审查 I11），在 app/relaunch.py。

I11 Windows：CRT 的 execv 是「起新进程、自己立刻以 0 退出」，Start.bat 的「出错就 pause」永远
轮不到。replace_process 在 Windows 上起子进程、等它、带着它的退出码退出；POSIX 上照旧 exec。
钉住：os.name 是 nt 时走子进程并传递返回码（含 32 位无符号的退出码、拿不到退出码）、Ctrl-C
不杀子进程；POSIX 上不起子进程。一键更新的重启仍然是 execv（要放开端口）。

测试里绝不真的 exec、不真的起子进程。
"""
import ast
from pathlib import Path

import pytest

from app import relaunch

REPO = Path(__file__).resolve().parent.parent


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
        relaunch.replace_process(
            argv or [r"C:\Users\Zhang San\app\.venv\Scripts\python.exe",
                     r"C:\Users\Zhang San\app\main.py", "--port", "8799"],
            env=env, popen=popen, exit_=calls["exit"].append,
            execv=lambda *a: calls["execv"].append(a))
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


# ---- 接线：main.py 怎么换进 .venv；一键更新重启仍是 execv ------------------------------------

def _ensure_env():
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ensure_env")


def _stmt_index(body, text):
    hits = [i for i, s in enumerate(body) if text in ast.unparse(s)]
    assert len(hits) == 1, (text, hits)
    return hits[0]


def test_ensure_env_waits_on_windows_then_execs_on_posix():
    """第一跳最后一步：Windows 起子进程等它（I11，不会回来）；POSIX 照旧 execv 进 .venv/bin/python
    跑 main.py。"""
    ensure = _ensure_env()
    block = next(n for n in ast.walk(ensure) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "not in_project_venv"
                 and "exec_args" in ast.unparse(n))
    body = block.body
    argv = _stmt_index(body, "argv = [str(vpy), str(ROOT / 'main.py')] + sys.argv[1:]")
    windows = _stmt_index(body, "replace_process(argv)")
    assert ast.unparse(body[windows].test) == "os.name == 'nt'"
    posix = _stmt_index(body, "os.execv(str(vpy), exec_args(argv))")
    assert argv < windows < posix == len(body) - 1
    # 换进程之前先放锁、收提示框（exec 不会执行 finally）、冲掉输出缓冲
    assert _stmt_index(body, "lock.unlink()") < argv
    assert _stmt_index(body, "_close_info_dialog()") < argv
    assert _stmt_index(body, "flush_std()") < argv


def test_the_update_restart_still_execs_in_place():
    """一键更新的重启要放开端口：Windows 上也不能留着旧进程等新进程。"""
    src = (REPO / "app" / "updater.py").read_text(encoding="utf-8")
    assert "os.execv(sys.executable, exec_args(" in src
    assert "replace_process" not in src
