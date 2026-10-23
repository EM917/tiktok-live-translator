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


# ---- 同一次退出的回声不算「又收到」（第 2 轮审查）----

def _recording_dispatch(**kw):
    seen = []
    d = es.Dispatch(lambda n: seen.append(("first", n)), lambda n: seen.append(("again", n)),
                    on_echo=lambda n: seen.append(("echo", n)), **kw)
    return d, seen


def test_two_hangups_a_millisecond_apart_are_one_exit():
    """关 Start.command 的终端窗口、登录 shell 是 zsh：进程收到两个 SIGHUP，相隔 0.1～12 毫秒。第二个以前走
    on_repeat（os._exit(1)），第一次的收尾还没开始写。终端都关了，之后的 SIGHUP 再晚也不算又收到。"""
    d, seen = _recording_dispatch()
    d(signal.SIGHUP, at=100.0)
    d(signal.SIGHUP, at=100.001)
    d(signal.SIGHUP, at=105.0)
    assert seen == [("first", "SIGHUP"), ("echo", "SIGHUP"), ("echo", "SIGHUP")]
    assert d.first == "SIGHUP" and d.count == 3 and d.echoes == ["SIGHUP", "SIGHUP"]
    d(signal.SIGTERM, at=105.1)                         # 别的信号照旧是「又收到」
    assert seen[-1] == ("again", "SIGTERM")


def test_a_hangup_after_another_first_signal_is_an_echo_too():
    """先按了 Ctrl-C，收尾途中又关了终端窗口：SIGHUP 不打断收尾（main.py 的 on_echo 把标准流换成 /dev/null）。"""
    d, seen = _recording_dispatch()
    d(signal.SIGINT, at=10.0)
    d(signal.SIGHUP, at=11.0)
    assert seen == [("first", "SIGINT"), ("echo", "SIGHUP")]


def test_the_same_signal_within_the_gap_is_an_echo_but_half_a_second_later_it_ends_the_process():
    """同名、相隔不到 REPEAT_GAP_SEC（0.3 秒）：同一个来源一下发了两个。0.5 秒后再来一次（第 1 轮审查钉的「收尾途中
    再按一次 Ctrl-C」）仍然是又收到。不同名的，隔得再近也是又收到。"""
    assert 0.0 < es.REPEAT_GAP_SEC < 0.5
    d, seen = _recording_dispatch()
    d(signal.SIGINT, at=0.0)
    d(signal.SIGINT, at=0.1)
    d(signal.SIGINT, at=0.5)
    assert seen == [("first", "SIGINT"), ("echo", "SIGINT"), ("again", "SIGINT")]
    d, seen = _recording_dispatch()
    d(signal.SIGTERM, at=0.0)
    d(signal.SIGINT, at=0.001)
    assert seen == [("first", "SIGTERM"), ("again", "SIGINT")]


def test_without_an_explicit_time_the_dispatch_reads_its_clock():
    now = [50.0]
    d, seen = _recording_dispatch(clock=lambda: now[0])
    d(signal.SIGTERM)
    now[0] = 50.2
    d(signal.SIGTERM)
    now[0] = 50.6
    d(signal.SIGTERM)
    assert seen == [("first", "SIGTERM"), ("echo", "SIGTERM"), ("again", "SIGTERM")]
    assert d.first_at == 50.0


def test_two_hangups_through_the_bridge_let_the_first_shutdown_finish():
    """窗口模式的读线程：第一个 SIGHUP 的收尾在自己的线程里跑（这里慢到 0.2 秒），第二个 1 毫秒后读到——
    on_repeat 一次都不调，第一次的收尾走完。"""
    done = threading.Event()
    seen = []

    def on_first(name):
        seen.append(("first", name))
        time.sleep(0.2)
        seen.append(("first-done", name))
        done.set()

    d = es.Dispatch(on_first, lambda n: seen.append(("again", n)), on_echo=lambda n: seen.append(("echo", n)))
    bridge = es.WakeupBridge(d, signals=[], signal_mod=FakeSignal())
    bridge._deliver(signal.SIGHUP)
    time.sleep(0.001)
    bridge._deliver(signal.SIGHUP)
    assert done.wait(2.0)
    assert ("again", "SIGHUP") not in seen
    assert sorted(seen) == [("echo", "SIGHUP"), ("first", "SIGHUP"), ("first-done", "SIGHUP")]


_HANGUP_CHILD = """
import asyncio, os, signal, sys, time
sys.path.insert(0, {root!r})
from app import exit_signals as es

MODE, OUT = sys.argv[1], sys.argv[2]


def report(text, code):
    with open(OUT + ".tmp", "w") as fh:
        fh.write(text)
    os.replace(OUT + ".tmp", OUT)
    os._exit(code)


def on_repeat(name):
    report("repeat " + name, 1)


if MODE == "window":
    def on_first(name):
        time.sleep(0.3)                              # 收尾要一点时间：回声在这期间到
        report("first-done " + name, 0)

    bridge = es.WakeupBridge(es.Dispatch(on_first, on_repeat, on_echo=lambda n: None))
    assert bridge.install()
    with open(OUT + ".ready", "w") as fh:
        fh.write("ready")
    print("ready", flush=True)
    time.sleep(10)                                   # 主线程停在这里（近似原生界面循环）
    report("timeout", 2)
else:
    async def main():
        loop = asyncio.get_running_loop()

        def on_first(name):
            loop.call_later(0.3, report, "first-done " + name, 0)

        es.install_loop_handlers(loop, es.Dispatch(on_first, on_repeat, on_echo=lambda n: None))
        with open(OUT + ".ready", "w") as fh:
            fh.write("ready")
        print("ready", flush=True)
        await asyncio.sleep(10)
        report("timeout", 2)

    asyncio.run(main())
"""


@pytest.mark.skipif(not POSIX, reason="SIGHUP 只在 POSIX 上有")
@pytest.mark.parametrize("mode", ["window", "cli"])
def test_a_child_sent_two_hangups_a_millisecond_apart_finishes_its_shutdown(tmp_path, mode):
    """子进程里走真的信号：测试进程给它（不是给自己）连发两个 SIGHUP，相隔 1 毫秒。第一次的收尾走完、退出码 0；
    以前第二个走 on_repeat，退出码 1。"""
    child = tmp_path / "child.py"
    child.write_text(_HANGUP_CHILD.format(root=str(ROOT)), encoding="utf-8")
    out = tmp_path / "out.txt"
    proc = subprocess.Popen([sys.executable, str(child), mode, str(out)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(signal.SIGHUP)
        time.sleep(0.001)
        proc.send_signal(signal.SIGHUP)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert out.read_text() == "first-done SIGHUP"


_ZSH_DRIVER = """
import os, pty, select, shlex, signal, sys, time

zsh, launcher, child, mode, out = sys.argv[1:6]
pid, master = pty.fork()
if pid == 0:
    os.execv(zsh, ["-zsh", "-f", "-i"])              # 登录 shell（Terminal 开 .command 文件就是这样）


def drain(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            try:
                if not os.read(master, 4096):
                    return
            except OSError:
                return


drain(0.8)
# 和 Terminal 打开 Start.command 一样：在登录 shell 里敲「脚本路径 ; exit;」，脚本再 exec 成 python
line = " ".join(shlex.quote(a) for a in (launcher, sys.executable, child, mode, out)) + " ; exit;\\n"
os.write(master, line.encode())
deadline = time.monotonic() + 20
while not os.path.exists(out + ".ready") and time.monotonic() < deadline:
    drain(0.05)
drain(0.5)
os.close(master)                                     # 关终端窗口
deadline = time.monotonic() + 20
while not os.path.exists(out) and time.monotonic() < deadline:
    time.sleep(0.05)
try:
    os.kill(pid, signal.SIGKILL)
except OSError:
    pass
try:
    os.waitpid(pid, 0)
except OSError:
    pass
"""


@pytest.mark.skipif(not POSIX or not os.path.exists("/bin/zsh"), reason="要 POSIX 和 /bin/zsh")
@pytest.mark.parametrize("mode", ["window", "cli"])
def test_closing_a_zsh_terminal_lets_the_first_shutdown_finish(tmp_path, mode):
    """第 2 轮审查的实测场景：pty 里起登录 zsh，跑一个 exec python 的 bash 脚本（同 Start.command），再关掉 pty
    主端。python 这时是 zsh 的前台任务，不是会话首进程：zsh 转发一个 SIGHUP，zsh 退出时内核再发一个。
    第一次的收尾要走完（以前第二个 SIGHUP 走 on_repeat，6 次里 6 次没走完）。驱动跑在自己的子进程里：
    pytest 进程有别的线程，不在这里 fork。"""
    child = tmp_path / "child.py"
    child.write_text(_HANGUP_CHILD.format(root=str(ROOT)), encoding="utf-8")
    launcher = tmp_path / "start.sh"
    launcher.write_text('#!/bin/bash\nexec "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    driver = tmp_path / "driver.py"
    driver.write_text(_ZSH_DRIVER, encoding="utf-8")
    out = tmp_path / "out.txt"
    subprocess.run([sys.executable, str(driver), "/bin/zsh", str(launcher), str(child), mode, str(out)],
                   capture_output=True, timeout=60)
    assert (tmp_path / "out.txt.ready").exists(), "zsh 没跑起子进程"
    assert out.read_text() == "first-done SIGHUP"


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


def test_loop_handlers_are_python_level_handlers_that_hand_the_signal_back_to_the_loop():
    """不再用 add_signal_handler（事件循环被卡住时回调不跑，第 1 轮审查）：Python 层的处理函数先给看门狗上弦，
    再经 call_soon_threadsafe 把信号交回事件循环。"""
    loop, sig, d = FakeLoop(), FakeSignal(), es.Dispatch(lambda n: None)
    armed = []
    dog = SimpleNamespace(arm=lambda: armed.append(1))
    assert es.install_loop_handlers(loop, d, signals=[15, 1], signal_mod=sig, watchdog=dog) == [15, 1]
    assert loop.added == []
    before = time.monotonic()
    sig.handlers[15](15, None)
    # 第 2 轮审查起，信号到达的时刻跟着一起交过去（分回声和「又收到」，见 Dispatch）：以前这里是 (d, (15,))
    ((callback, (signum, at)),) = loop.soon
    assert armed == [1] and callback is d and signum == 15
    assert before <= at <= time.monotonic()
    assert es.install_loop_handlers(FakeLoop(), d, signals=[2], signal_mod=sig) == [2]     # 没有看门狗也照装
    sig.handlers[2](2, None)


def test_a_plain_callable_handed_to_the_loop_gets_only_the_signal_number():
    loop, sig, got = FakeLoop(), FakeSignal(), []
    es.install_loop_handlers(loop, got.append, signals=[15], signal_mod=sig)
    sig.handlers[15](15, None)
    assert loop.soon == [(got.append, (15,))]


def test_a_signal_after_the_loop_closed_goes_straight_to_the_dispatch():
    class Closed(FakeLoop):
        def call_soon_threadsafe(self, callback, *args):
            raise RuntimeError("Event loop is closed")

    got, sig = [], FakeSignal()
    es.install_loop_handlers(Closed(), es.Dispatch(got.append), signals=[15], signal_mod=sig)
    sig.handlers[15](15, None)
    assert got == ["SIGTERM"]


@pytest.mark.skipif(not POSIX, reason="SIGUSR1 只在 POSIX 上有")
def test_a_real_signal_reaches_the_dispatch_through_the_loop():
    got = []

    async def scenario():
        loop = asyncio.get_running_loop()
        d = es.Dispatch(got.append)
        old = signal.getsignal(signal.SIGUSR1)
        es.install_loop_handlers(loop, d, signals=[signal.SIGUSR1])
        try:
            os.kill(os.getpid(), signal.SIGUSR1)
            for _ in range(100):
                if got:
                    break
                await asyncio.sleep(0.01)
        finally:
            signal.signal(signal.SIGUSR1, old)

    asyncio.run(scenario())
    assert got == ["SIGUSR1"]


@pytest.mark.skipif(not POSIX, reason="SIGUSR1 只在 POSIX 上有")
def test_the_watchdog_fires_while_the_loop_is_stuck_in_synchronous_code():
    """事件循环被同步代码卡住：dispatch 要等它转起来才跑，看门狗不等——收到信号起 seconds 秒就调 on_expire。"""
    got, expired = [], threading.Event()

    async def scenario():
        loop = asyncio.get_running_loop()
        dog = es.Watchdog(0.2, expired.set)
        assert dog.start()
        old = signal.getsignal(signal.SIGUSR1)
        es.install_loop_handlers(loop, es.Dispatch(got.append), signals=[signal.SIGUSR1], watchdog=dog)
        try:
            os.kill(os.getpid(), signal.SIGUSR1)
            stuck_until = time.monotonic() + 1.0
            while time.monotonic() < stuck_until and not expired.is_set():
                time.sleep(0.01)                     # 卡住事件循环：不 await
            during = (expired.is_set(), list(got))
            await asyncio.sleep(0.05)                # 事件循环转起来，dispatch 才跑
        finally:
            signal.signal(signal.SIGUSR1, old)
        return during, dog

    (fired, got_while_stuck), dog = asyncio.run(scenario())
    assert fired and got_while_stuck == []
    assert got == ["SIGUSR1"] and dog.fired


@pytest.mark.skipif(not POSIX, reason="子进程收 SIGTERM 只在 POSIX 上这么测")
def test_sigterm_still_ends_a_process_whose_loop_is_stuck():
    """审查点名的退步：接住 SIGTERM 之后，事件循环被卡住时进程不退。子进程里事件循环卡在同步的 sleep 上，
    测试进程给它（不是给自己）发 SIGTERM：看门狗到点退出，退出码是 on_expire 给的 1。"""
    code = """
import asyncio, os, signal, sys, time
sys.path.insert(0, {root!r})
from app import exit_signals as es

async def main():
    dog = es.Watchdog(0.3, lambda: os._exit(1))
    dog.start()
    es.install_loop_handlers(asyncio.get_running_loop(), es.Dispatch(lambda n: os._exit(0)), watchdog=dog)
    print("ready", flush=True)
    time.sleep(20)                                   # 事件循环卡住：dispatch 跑不到
    os._exit(2)

asyncio.run(main())
""".format(root=str(ROOT))
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 1
        assert time.monotonic() - started < 5.0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_the_watchdog_only_runs_once_and_never_without_a_signal():
    calls = []
    dog = es.Watchdog(0.05, lambda: calls.append("expire"))
    assert dog.start()
    time.sleep(0.15)
    assert calls == []                               # 没收到信号：不计时
    dog.arm()
    dog.arm()                                        # 第二个信号：不重新计时、不再调一次
    deadline = time.monotonic() + 2.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.15)
    assert calls == ["expire"]


def test_an_unstarted_watchdog_ignores_arm():
    dog = es.Watchdog(0.01, lambda: None)
    dog.arm()                                        # 没起来（start 失败）：上弦什么都不做、不抛
    assert dog.fired is False


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


@pytest.mark.skipif(not POSIX, reason="SIGUSR1 只在 POSIX 上有")
def test_a_second_real_signal_reaches_on_repeat_while_the_first_shutdown_is_still_running():
    """第 1 轮审查：读线程以前同步跑第一次的收尾（最多 5 + 2 秒），这期间第二个信号的字节没人读——窗口模式下
    第二次 Ctrl-C 不起作用。现在第一次的收尾在自己的线程里，第二个信号照样交到 on_repeat。"""
    release = threading.Event()
    first_running = threading.Event()
    seen = []

    def on_first(name):
        seen.append(("first", name))
        first_running.set()
        release.wait(5.0)                    # 模拟 stop_after_close 在等管线停下
        seen.append(("first-done", name))

    def on_repeat(name):
        seen.append(("again", name))

    bridge = es.WakeupBridge(es.Dispatch(on_first, on_repeat), signals=[signal.SIGUSR1, signal.SIGUSR2])
    old_fd = signal.set_wakeup_fd(-1)
    signal.set_wakeup_fd(old_fd)
    assert bridge.install()
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        assert first_running.wait(2.0)
        os.kill(os.getpid(), signal.SIGUSR2)
        deadline = time.monotonic() + 2.0
        while ("again", "SIGUSR2") not in seen and time.monotonic() < deadline:
            time.sleep(0.01)
        during = list(seen)
    finally:
        release.set()
        bridge.uninstall()
    assert during == [("first", "SIGUSR1"), ("again", "SIGUSR2")]     # 第一次的收尾还没走完
    assert bridge.dispatch.first == "SIGUSR1" and bridge.dispatch.count == 2


def test_two_signals_read_together_keep_their_order():
    """两个字节一次 recv 读到：第一次在读线程上就认了账，新线程晚起一步也不会让第二个抢成「第一次」。"""
    release = threading.Event()
    seen = []

    def on_first(name):
        release.wait(2.0)
        seen.append(("first", name))

    d = es.Dispatch(on_first, lambda n: seen.append(("again", n)))
    bridge = es.WakeupBridge(d, signals=[], signal_mod=FakeSignal())
    bridge._deliver(signal.SIGTERM)
    bridge._deliver(signal.SIGINT)           # 读线程不等第一次的收尾
    assert seen == [("again", "SIGINT")]
    release.set()
    deadline = time.monotonic() + 2.0
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [("again", "SIGINT"), ("first", "SIGTERM")] and d.first == "SIGTERM"


def test_a_plain_callable_dispatch_is_still_called_directly():
    got = []
    bridge = es.WakeupBridge(got.append, signals=[], signal_mod=FakeSignal())
    bridge._deliver(signal.SIGTERM)
    assert got == [signal.SIGTERM]


@pytest.mark.skipif(not POSIX, reason="SIGUSR1 只在 POSIX 上有")
def test_a_second_signal_ends_the_process_before_the_first_shutdown_finishes():
    """和第 1 轮审查的实验同一个场景，在子进程里走到 os._exit：主线程停在 sleep 里
    （近似原生界面循环），第一次的收尾要 3 秒，0.5 秒后再来一个信号 → 以 on_repeat 的退出码 1 结束，不等那 3 秒。"""
    code = """
import os, signal, sys, threading, time
sys.path.insert(0, {root!r})
from app import exit_signals as es

def on_first(name):
    time.sleep(3.0)
    os._exit(0)

def on_repeat(name):
    os._exit(1)

bridge = es.WakeupBridge(es.Dispatch(on_first, on_repeat), signals=[signal.SIGUSR1])
assert bridge.install()

def sender():
    time.sleep(0.2); os.kill(os.getpid(), signal.SIGUSR1)
    time.sleep(0.5); os.kill(os.getpid(), signal.SIGUSR1)

threading.Thread(target=sender, daemon=True).start()
time.sleep(10)
os._exit(2)
""".format(root=str(ROOT))
    started = time.monotonic()
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    elapsed = time.monotonic() - started
    assert out.returncode == 1, out.stderr
    assert elapsed < 3.0


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


def _passes_the_echo_handler(dispatch):
    return {kw.arg: getattr(kw.value, "id", None) for kw in dispatch.keywords}.get("on_echo") == \
        "_echo_exit_signal"


def test_both_modes_hand_echoes_to_their_own_handler_that_only_silences_stdio_after_a_hangup():
    """第 2 轮审查：同一次退出的回声（zsh 下关终端窗口的第二个 SIGHUP）不走 _exit_again。"""
    ast, window = _main_func("_install_window_signals")
    ast, cli = _main_func("main_async")
    for func in (window, cli):
        (dispatch,) = _calls(ast, func, "Dispatch")
        assert getattr(dispatch.args[1], "id", None) == "_exit_again"
        assert _passes_the_echo_handler(dispatch)
    ast, echo = _main_func("_echo_exit_signal")
    assert _calls(ast, echo, "silence_stdio") and not _calls(ast, echo, "_exit")
    assert not _calls(ast, echo, "print") and not _calls(ast, echo, "_say")


def test_the_window_bridge_hands_signals_to_finish_signal_and_keeps_sigint_from_cocoa():
    ast, func = _main_func("_install_window_signals")
    assert _calls(ast, func, "finish_signal")
    assert _calls(ast, func, "WakeupBridge")
    # mach 中断只在桥装上之后才换（第 1 轮审查）：装不上时换掉它，Ctrl-C 在 Cocoa 主循环里就彻底没反应
    (keep,) = _calls(ast, func, "keep_sigint_from_cocoa")
    (gate,) = [n for n in ast.walk(func) if isinstance(n, ast.If) and isinstance(n.test, ast.Call)
               and getattr(n.test.func, "attr", None) == "install"]
    assert any(keep is n for stmt in gate.body for n in ast.walk(stmt))


def test_cli_and_browser_mode_install_loop_handlers_and_shut_down_through_close_session():
    ast, func = _main_func("main_async")
    (install,) = _calls(ast, func, "install_loop_handlers")
    assert "watchdog" in {kw.arg for kw in install.keywords}          # 事件循环卡住时的兜底（第 1 轮审查）
    (dog,) = _calls(ast, func, "Watchdog")
    assert getattr(dog.args[1], "id", None) == "_exit_stuck"
    (start,) = [n for n in _calls(ast, func, "start") if getattr(n.func.value, "id", None) == "watchdog"]
    assert start.lineno < install.lineno                               # 线程在装处理函数之前起好
    ast, stuck = _main_func("_exit_stuck")
    assert _calls(ast, stuck, "flush_evidence") and _calls(ast, stuck, "_exit")
    ast, func = _main_func("main_async")
    assert _calls(ast, func, "_shutdown_on_signal")
    assert _calls(ast, func, "_recover_unclosed_audits")
    ast, shut = _main_func("_shutdown_on_signal")
    (close,) = _calls(ast, shut, "close_session")
    assert {kw.arg: getattr(kw.value, "value", None) for kw in close.keywords}["reason"] == "signal"
    assert _calls(ast, shut, "flush_evidence") and _calls(ast, shut, "_exit")
