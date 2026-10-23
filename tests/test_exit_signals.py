"""收到退出信号先把审计收尾（专家审查 B5，app/exit_signals.py）。

测试不 import main（main.py 模块级会 execv 进 .venv）：信号的分发、两种安装方式、换掉 PyObjC 的 mach 中断、
SIGHUP 之后把标准流换成 /dev/null，都在 app/exit_signals.py 里测。真 pywebview 上的实测记录见模块说明。
信号只用 SIGUSR1/SIGUSR2：测试进程绝不给自己发 SIGTERM、SIGINT、SIGHUP。
"""
import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import exit_signals as es
from app import window_close

ROOT = Path(__file__).resolve().parent.parent
POSIX = os.name != "nt"


def test_posix_handles_hup_term_and_int_and_windows_only_int():
    full = SimpleNamespace(SIGHUP=1, SIGTERM=15, SIGINT=2)
    assert es.handled_signals(platform="darwin", signal_mod=full) == [1, 15, 2]
    assert es.handled_signals(platform="linux", signal_mod=full) == [1, 15, 2]
    assert es.handled_signals(platform="win32", signal_mod=full) == [2]
    only_int = SimpleNamespace(SIGINT=2)                 # 这个平台的 signal 模块里没有 SIGHUP / SIGTERM
    assert es.handled_signals(platform="linux", signal_mod=only_int) == [2]


def test_signal_names_are_the_english_codes():
    assert es.signal_name(signal.SIGINT) == "SIGINT"
    assert es.signal_name(signal.SIGTERM) == "SIGTERM"
    assert es.signal_name(250) == "SIG250"


def test_dispatch_calls_first_once_then_repeat():
    seen = []
    d = es.Dispatch(lambda n: seen.append(("first", n)), lambda n: seen.append(("again", n)))
    d(signal.SIGTERM)
    d(signal.SIGINT)
    d(signal.SIGINT)
    assert seen == [("first", "SIGTERM"), ("again", "SIGINT"), ("again", "SIGINT")]
    assert d.first == "SIGTERM" and d.count == 3


def test_a_callback_that_raises_or_exits_never_escapes():
    def boom(name):
        raise SystemExit(3)

    d = es.Dispatch(boom, None)
    d(signal.SIGTERM)                                    # 不抛
    d(signal.SIGTERM)                                    # 没有 on_repeat：什么都不做
    assert d.errors == ["SystemExit"]


def test_dispatch_is_first_only_once_across_threads():
    firsts = []
    d = es.Dispatch(firsts.append, None)
    threads = [threading.Thread(target=d, args=(signal.SIGTERM,)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert firsts == ["SIGTERM"] and d.count == 20


# ---- 浏览器和命令行模式：事件循环在主线程 ----

class FakeLoop:
    def __init__(self, supported=True):
        self.supported = supported
        self.added = []
        self.soon = []

    def add_signal_handler(self, sig, callback, *args):
        if not self.supported:
            raise NotImplementedError
        self.added.append((sig, callback, args))

    def call_soon_threadsafe(self, callback, *args):
        self.soon.append((callback, args))


class FakeSignal:
    SIGINT, SIGTERM, SIGHUP = 2, 15, 1
    SIG_DFL = 0

    def __init__(self):
        self.handlers = {}
        self.wakeup = -1

    def signal(self, sig, handler):
        old = self.handlers.get(sig, self.SIG_DFL)
        self.handlers[sig] = handler
        return old

    def set_wakeup_fd(self, fd, warn_on_full_buffer=True):
        old, self.wakeup = self.wakeup, fd
        return old


def test_loop_handlers_use_add_signal_handler():
    loop, d = FakeLoop(), es.Dispatch(lambda n: None)
    assert es.install_loop_handlers(loop, d, signals=[15, 1]) == [15, 1]
    assert loop.added == [(15, d, (15,)), (1, d, (1,))]


def test_without_add_signal_handler_sigint_goes_through_call_soon_threadsafe():
    """Windows 的事件循环没有 add_signal_handler：退回 signal.signal，处理函数把信号交回事件循环。"""
    loop, sig, d = FakeLoop(supported=False), FakeSignal(), es.Dispatch(lambda n: None)
    assert es.install_loop_handlers(loop, d, signals=[2], signal_mod=sig) == [2]
    sig.handlers[2](2, None)
    assert loop.soon == [(d, (2,))]


@pytest.mark.skipif(not POSIX, reason="SIGUSR1 只在 POSIX 上有")
def test_a_real_signal_reaches_the_dispatch_through_the_loop():
    got = []

    async def scenario():
        loop = asyncio.get_running_loop()
        d = es.Dispatch(got.append)
        es.install_loop_handlers(loop, d, signals=[signal.SIGUSR1])
        try:
            os.kill(os.getpid(), signal.SIGUSR1)
            for _ in range(100):
                if got:
                    break
                await asyncio.sleep(0.01)
        finally:
            loop.remove_signal_handler(signal.SIGUSR1)

    asyncio.run(scenario())
    assert got == ["SIGUSR1"]


# ---- 窗口模式：C 层 → socketpair → 读线程 ----

@pytest.mark.skipif(not POSIX, reason="SIGUSR1 只在 POSIX 上有")
def test_the_bridge_delivers_a_real_signal_exactly_once_and_uninstalls_cleanly():
    got = []
    d = es.Dispatch(got.append, lambda n: got.append("again:" + n))
    bridge = es.WakeupBridge(d, signals=[signal.SIGUSR2])
    old_fd = signal.set_wakeup_fd(-1)
    signal.set_wakeup_fd(old_fd)
    assert bridge.install()
    try:
        assert bridge.reader_alive()
        os.kill(os.getpid(), signal.SIGUSR2)
        deadline = time.monotonic() + 2.0
        while not got and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)                     # Python 层的处理函数这时也跑过了：读线程活着，它不再交一次
    finally:
        bridge.uninstall()
    assert got == ["SIGUSR2"]
    assert signal.getsignal(signal.SIGUSR2) == signal.SIG_DFL
    assert signal.set_wakeup_fd(old_fd) == old_fd       # wakeup fd 恢复成装之前的
    assert not bridge.reader_alive()


def test_the_bridge_puts_back_what_it_changed_when_a_step_fails():
    class Refuses(FakeSignal):
        def set_wakeup_fd(self, fd, warn_on_full_buffer=True):
            raise ValueError("set_wakeup_fd only works in main thread")

    sig = Refuses()
    sig.handlers = {2: "before", 15: "before"}
    bridge = es.WakeupBridge(es.Dispatch(lambda n: None), signals=[2, 15], signal_mod=sig)
    assert bridge.install() is False
    assert sig.handlers == {2: "before", 15: "before"}
    assert not bridge.reader_alive()


def test_the_python_level_handler_only_dispatches_when_the_reader_is_gone():
    got = []
    bridge = es.WakeupBridge(es.Dispatch(got.append), signals=[], signal_mod=FakeSignal())
    bridge._python_level(signal.SIGTERM, None)          # 没装（读线程不在）：自己交
    assert got == ["SIGTERM"]
    bridge._reader = SimpleNamespace(is_alive=lambda: True)
    bridge._python_level(signal.SIGTERM, None)          # 读线程在：它会读到 C 层写的那个字节，这里不再交
    assert got == ["SIGTERM"]


def test_cocoa_mach_interrupt_is_replaced_with_a_no_op():
    calls = []
    helper = SimpleNamespace(installMachInterrupt=lambda: calls.append("mach"))
    assert es.keep_sigint_from_cocoa(helper) is True
    helper.installMachInterrupt()
    assert calls == []
    assert es.keep_sigint_from_cocoa(SimpleNamespace()) is False


def test_silencing_stdio_after_a_hangup_sends_output_to_devnull():
    code = ("import sys; sys.path.insert(0, {root!r}); from app import exit_signals as es; "
            "print('before', flush=True); ok = es.silence_stdio(); print('after', flush=True); "
            "sys.stderr.write('err\\n'); sys.exit(0 if ok else 3)").format(root=str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert out.stdout == "before\n" and out.stderr == ""


# ---- 和关窗那套收尾接上：FinishOnce.finish_signal ----

def test_finish_signal_runs_the_shutdown_with_reason_signal_and_exits():
    calls = []

    class Exited(Exception):
        pass

    def exit_fn(code):
        calls.append(("exit", code))
        raise Exited

    once = window_close.FinishOnce(lambda **kw: calls.append(("finish", kw)), exit_fn=exit_fn)
    with pytest.raises(Exited):
        once.finish_signal("SIGTERM")
    assert calls == [("finish", {"reason": "signal", "fields": {"signal": "SIGTERM"}}), ("exit", 0)]
    assert once.finish_signal("SIGINT") is False                # 已经在收尾：不来第二遍
    assert once.finish_stuck() is False


def test_finish_signal_steps_aside_when_the_window_path_is_already_finishing():
    calls = []
    once = window_close.FinishOnce(lambda **kw: calls.append(kw), exit_fn=lambda code: None)
    once._lock.acquire()                                        # 主线程正在收尾
    assert once.finish_signal("SIGHUP") is False
    assert calls == []


def test_the_module_holds_no_chinese_literals():
    import ast
    import re

    tree = ast.parse(open(es.__file__, encoding="utf-8").read())
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)}
    cjk = re.compile(r"[一-鿿]")
    offenders = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                 and isinstance(n.value, str) and id(n) not in docs and cjk.search(n.value)]
    assert offenders == []


# ---- ⌘Q：NSApp terminate: 直接 exit()，在 willTerminate 通知里收尾（window_close.hook_app_terminate）----

class FakeCenter:
    def __init__(self):
        self.added = []

    def addObserverForName_object_queue_usingBlock_(self, name, obj, queue, block):
        self.added.append((name, obj, queue))
        self.block = block
        return "token"


def test_cmd_q_runs_the_shutdown_from_the_will_terminate_notification():
    center, calls = FakeCenter(), []
    assert window_close.hook_app_terminate(lambda: calls.append("finish"), center=center,
                                           note_name="WillTerminate", queue="main")
    assert center.added == [("WillTerminate", None, "main")]
    center.block(object())
    assert calls == ["finish"]


def test_the_terminate_hook_is_mac_only(monkeypatch):
    monkeypatch.setattr(window_close.sys, "platform", "win32")
    assert window_close.hook_app_terminate(lambda: None) is False


# ---- main.py 的接线（不能 import main，读语法树）----

def _main_func(name):
    import ast

    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    return ast, next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == name)


def _calls(ast, func, name):
    return [n for n in ast.walk(func) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)]


def test_window_mode_installs_the_signal_bridge_before_the_backend_and_hooks_cmd_q_before_start():
    ast, func = _main_func("run_with_window")
    (install,) = _calls(ast, func, "_install_window_signals")
    (thread,) = [n for n in _calls(ast, func, "Thread")]
    (hook,) = _calls(ast, func, "hook_app_terminate")
    starts = [n for n in _calls(ast, func, "start") if getattr(n.func.value, "id", None) == "webview"]
    main_start = max(starts, key=lambda n: n.lineno)         # 主窗口那一次（另一个是已有实例的窗口）
    assert install.lineno < thread.lineno                    # 起后台服务之前就装好
    assert hook.lineno < main_start.lineno
    assert isinstance(hook.args[0], ast.Attribute) and (hook.args[0].value.id, hook.args[0].attr) == \
        ("finish", "finish")


def test_the_window_bridge_hands_signals_to_finish_signal_and_keeps_sigint_from_cocoa():
    ast, func = _main_func("_install_window_signals")
    assert _calls(ast, func, "keep_sigint_from_cocoa")
    assert _calls(ast, func, "finish_signal")
    assert _calls(ast, func, "WakeupBridge")


def test_cli_and_browser_mode_install_loop_handlers_and_shut_down_through_close_session():
    ast, func = _main_func("main_async")
    assert _calls(ast, func, "install_loop_handlers")
    assert _calls(ast, func, "_shutdown_on_signal")
    assert _calls(ast, func, "_recover_unclosed_audits")
    ast, shut = _main_func("_shutdown_on_signal")
    (close,) = _calls(ast, shut, "close_session")
    assert {kw.arg: getattr(kw.value, "value", None) for kw in close.keywords}["reason"] == "signal"
    assert _calls(ast, shut, "flush_evidence") and _calls(ast, shut, "_exit")
