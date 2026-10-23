"""收到退出信号时，先把这一场的审计收尾再退出（专家审查 B5，2026-10-21）。

以前 main.py 只接 KeyboardInterrupt：关掉 Start.command 的终端窗口（SIGHUP）、kill（SIGTERM）直接按
默认动作结束进程，窗口模式下的 Ctrl-C 也绕过收尾——这一场的审计没有 session_end。现在：
  * 浏览器和命令行模式（asyncio 跑在主线程）：install_loop_handlers 用 loop.add_signal_handler 接住，
    调用方取消主任务，在主任务里停管线（session_end 的 reason=signal），然后退出；
  * 窗口模式（主线程跑 pywebview 的 Cocoa 主循环、asyncio 在后台线程）：Python 层的信号处理函数要等主线程
    回到 Python 代码才执行，Cocoa 主循环里可能一直不执行——2026-10-23 用真 pywebview 6.2.1 实测，
    SIGTERM、SIGHUP 发过去，Python 层的处理函数一次都没跑。所以用 WakeupBridge：主线程装一个空的 Python
    处理函数（这样 C 层的处理函数才会接管），再用 signal.set_wakeup_fd 让 C 层在收到信号的那一刻把信号
    编号写进一根 socketpair；一个后台线程阻塞读它，读到就交给调用方的收尾（停管线、写 session_end、
    等证据写盘，最后 os._exit）。实测 SIGTERM、SIGHUP 都在 1 毫秒内读到；
  * pywebview 的 cocoa 后端在 webview.start() 里调 PyObjCTools.AppHelper.installMachInterrupt()，它在
    C 层把 SIGINT 换成自己的处理函数（经 mach 端口调 NSApp terminate:）：Ctrl-C 走的是「退出程序」那条路，
    直播中还会弹「确定关闭？」，确认后进程直接 exit()，审计一个字都不补。keep_sigint_from_cocoa 把它换成
    空函数，SIGINT 留给 WakeupBridge（实测换掉之后 Ctrl-C 同样 1 毫秒内读到）；
  * Windows 只接 Ctrl-C（SIGINT）：那边没有 SIGHUP，SIGTERM 也不是这样送到进程的。

同一个进程只收尾一次：Dispatch 认第一次收到的信号；收尾途中再收到（比如又按了一次 Ctrl-C）交给 on_repeat，
调用方据此不再等、直接退出。

这个模块不含中文字面量、自己不打印：要打印的中文交给调用方（main.py）。测试不 import main（CLAUDE.md 的
「测试里别 import main」），逻辑都在这里测。
"""
import os
import signal
import socket
import sys
import threading

POSIX_SIGNALS = ("SIGHUP", "SIGTERM", "SIGINT")
WINDOWS_SIGNALS = ("SIGINT",)


def handled_signals(platform=None, signal_mod=signal):
    """这个平台要接住的信号（signal 模块里的编号）。Windows 只有 SIGINT。"""
    plat = sys.platform if platform is None else platform
    names = WINDOWS_SIGNALS if plat == "win32" else POSIX_SIGNALS
    return [getattr(signal_mod, name) for name in names if hasattr(signal_mod, name)]


def signal_name(signum):
    """SIGTERM 这样的名字（写进 session_end 的 signal 字段，英文代码）。认不出的写 SIG<编号>。"""
    try:
        return signal.Signals(signum).name
    except (ValueError, TypeError):
        return "SIG{}".format(signum)


class Dispatch:
    """把收到的信号交给调用方：第一次调 on_first(名字)，之后每一次调 on_repeat(名字)。线程安全。
    回调里抛的异常吞掉（收尾的路上不许再出新的岔子），记在 errors 里给测试看。"""

    def __init__(self, on_first, on_repeat=None):
        self._lock = threading.Lock()
        self.on_first = on_first
        self.on_repeat = on_repeat
        self.first = None
        self.count = 0
        self.errors = []

    def __call__(self, signum):
        name = signal_name(signum)
        with self._lock:
            self.count += 1
            first = self.first is None
            if first:
                self.first = name
        callback = self.on_first if first else self.on_repeat
        if callback is None:
            return
        try:
            callback(name)
        except BaseException as exc:      # 包括 SystemExit：退出由调用方用 os._exit 做
            self.errors.append(type(exc).__name__)


def install_loop_handlers(loop, dispatch, signals=None, signal_mod=signal):
    """浏览器和命令行模式（事件循环在主线程）：每个信号用 loop.add_signal_handler 交给 dispatch(编号)。
    add_signal_handler 不可用（Windows 的事件循环没有它）就退回 signal.signal，处理函数里用
    call_soon_threadsafe 回到事件循环。返回装上的信号编号。"""
    installed = []
    for sig in (handled_signals(signal_mod=signal_mod) if signals is None else signals):
        try:
            loop.add_signal_handler(sig, dispatch, sig)
        except (NotImplementedError, RuntimeError, ValueError, OSError):
            try:
                signal_mod.signal(sig, _threadsafe(loop, dispatch))
            except (ValueError, OSError, RuntimeError):
                continue
        installed.append(sig)
    return installed


def _threadsafe(loop, dispatch):
    def handler(signum, frame):
        try:
            loop.call_soon_threadsafe(dispatch, signum)
        except RuntimeError:              # 循环已经关了：直接交给 dispatch
            dispatch(signum)
    return handler


class WakeupBridge:
    """窗口模式：把信号从 C 层直接送到一个后台线程，不等主线程回到 Python 代码（见模块说明）。

    install() 必须在主线程调（signal.signal、set_wakeup_fd 只许主线程用）。读线程是 daemon 线程，
    读到一个字节就调一次 dispatch(编号)。Python 层的处理函数只在读线程不在了的时候才自己调 dispatch——
    两条路都送的话，同一个信号会被算成两次（第二次就成了「又收到一次」）。"""

    def __init__(self, dispatch, signals=None, signal_mod=signal, socketpair=socket.socketpair):
        self.dispatch = dispatch
        self.signals = signals
        self._signal = signal_mod
        self._socketpair = socketpair
        self._reader = None
        self._rsock = self._wsock = None
        self.installed = []
        self.old_wakeup_fd = None

    def install(self):
        """装好返回 True；任何一步失败都把已经改的恢复原样、返回 False（信号保持原来的默认动作）。"""
        sigs = handled_signals(signal_mod=self._signal) if self.signals is None else self.signals
        try:
            rsock, wsock = self._socketpair()
            wsock.setblocking(False)
        except (OSError, ValueError):
            return False
        olds = []
        try:
            for sig in sigs:
                olds.append((sig, self._signal.signal(sig, self._python_level)))
            self.old_wakeup_fd = self._signal.set_wakeup_fd(wsock.fileno(), warn_on_full_buffer=False)
        except (OSError, ValueError, RuntimeError):
            for sig, old in olds:
                try:
                    self._signal.signal(sig, old)
                except (OSError, ValueError, RuntimeError):
                    pass
            for s in (rsock, wsock):
                s.close()
            return False
        self._rsock, self._wsock = rsock, wsock
        self.installed = [sig for sig, _ in olds]
        self._reader = threading.Thread(target=self._read_loop, name="exit-signals", daemon=True)
        self._reader.start()
        return True

    def _read_loop(self):
        while True:
            try:
                data = self._rsock.recv(64)
            except InterruptedError:
                continue
            except OSError:
                return
            if not data:
                return
            for signum in data:
                self.dispatch(signum)

    def reader_alive(self):
        return self._reader is not None and self._reader.is_alive()

    def _python_level(self, signum, frame):
        if not self.reader_alive():
            self.dispatch(signum)

    def uninstall(self):
        """测试用：恢复 wakeup fd，关掉 socketpair（读线程随之结束）。信号处理函数恢复成默认。"""
        try:
            self._signal.set_wakeup_fd(-1 if self.old_wakeup_fd is None else self.old_wakeup_fd)
        except (OSError, ValueError, RuntimeError):
            pass
        for sig in self.installed:
            try:
                self._signal.signal(sig, self._signal.SIG_DFL)
            except (OSError, ValueError, RuntimeError):
                pass
        for s in (self._wsock, self._rsock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        if self._reader is not None:
            self._reader.join(timeout=1.0)


def keep_sigint_from_cocoa(apphelper=None):
    """不让 pywebview 的 cocoa 后端把 SIGINT 换成 PyObjC 的 mach 中断（见模块说明）：把
    AppHelper.installMachInterrupt 换成空函数。pywebview 是在 webview.start() 里按模块属性调它的，所以要在
    start() 之前换。只在 macOS 上做；PyObjC 不在（别的平台、没装 pywebview）就什么都不做。返回换没换。"""
    if apphelper is None:
        if sys.platform != "darwin":
            return False
        try:
            from PyObjCTools import AppHelper as apphelper
        except Exception:
            return False
    if not hasattr(apphelper, "installMachInterrupt"):
        return False
    apphelper.installMachInterrupt = _skip_mach_interrupt
    return True


def _skip_mach_interrupt(*_args, **_kwargs):
    return None


def silence_stdio(devnull=os.devnull):
    """SIGHUP（终端窗口关了）之后，标准输出和标准错误指向的终端已经没了：再写就是 EIO，收尾路上任何一行
    print 都可能抛 OSError，把停管线、写 session_end 打断。把 1、2 号描述符换成 /dev/null。
    只在收到 SIGHUP 时调；失败就算了（返回 False）。"""
    try:
        fd = os.open(devnull, os.O_WRONLY)
    except OSError:
        return False
    ok = True
    try:
        for stream, target in ((sys.stdout, 1), (sys.stderr, 2)):
            try:
                if stream is not None:
                    stream.flush()
            except Exception:
                pass
            try:
                os.dup2(fd, target)
            except OSError:
                ok = False
    finally:
        os.close(fd)
    return ok
