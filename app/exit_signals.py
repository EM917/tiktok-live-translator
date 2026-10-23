"""收到退出信号时，先把这一场的审计收尾再退出（专家审查 B5，2026-10-21）。

以前 main.py 只接 KeyboardInterrupt：关掉 Start.command 的终端窗口（SIGHUP）、kill（SIGTERM）直接按
默认动作结束进程，窗口模式下的 Ctrl-C 也绕过收尾——这一场的审计没有 session_end。现在：
  * 浏览器和命令行模式（asyncio 跑在主线程）：install_loop_handlers 装 Python 层的处理函数，交回事件循环；
    调用方取消主任务，在主任务里停管线（session_end 的 reason=signal），然后退出。事件循环被卡住、这一步
    跑不到时，Watchdog 在 LOOP_DEADLINE_SEC 秒后直接退出；
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
调用方据此不再等、直接退出。窗口模式下，读线程把第一次的收尾交给新线程、自己接着读（WakeupBridge），
收尾途中的第二个信号才读得到。

这个模块不含中文字面量、自己不打印：要打印的中文交给调用方（main.py）。测试不 import main（CLAUDE.md 的
「测试里别 import main」），逻辑都在这里测。
"""
import os
import signal
import socket
import sys
import threading
import time

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
        self.claim(signum)[1]()

    def claim(self, signum):
        """只认账、不调回调：在锁里数一次、定下这是不是第一次，返回 (是不是第一次, run)。run() 才调回调。
        给 WakeupBridge 用：第一次的 run 交给新线程跑，读线程接着读下一个信号——先后在这里就定了，
        不会因为新线程晚起一步，让第二个信号抢成「第一次」。"""
        name = signal_name(signum)
        with self._lock:
            self.count += 1
            first = self.first is None
            if first:
                self.first = name
        callback = self.on_first if first else self.on_repeat
        return first, (lambda: self._run(callback, name))

    def _run(self, callback, name):
        if callback is None:
            return
        try:
            callback(name)
        except BaseException as exc:      # 包括 SystemExit：退出由调用方用 os._exit 做
            self.errors.append(type(exc).__name__)


LOOP_DEADLINE_SEC = 10.0   # 浏览器和命令行模式：收到第一个信号这么久还没退出，看门狗直接退出（见 Watchdog）


def install_loop_handlers(loop, dispatch, signals=None, signal_mod=signal, watchdog=None):
    """浏览器和命令行模式（事件循环在主线程）：每个信号装一个 Python 层的处理函数，先给看门狗上弦
    （watchdog.arm，见 Watchdog），再用 call_soon_threadsafe 把信号交回事件循环里的 dispatch(编号)。
    返回装上的信号编号。

    以前用 loop.add_signal_handler：回调只在事件循环转起来时才跑，事件循环被同步代码卡住时一直不跑——
    SIGTERM、SIGHUP 以前按默认动作直接结束进程，接住之后反倒只剩 kill -9（第 1 轮审查）。Python 层的处理函数
    在主线程两条字节码之间就执行（卡在 sleep、锁、select 里也会被信号打断后先执行它），看门狗当场开始计时，
    不靠事件循环。主线程卡在一段不回 Python 的 C 代码里时，处理函数也要等它回来（这一点不如默认动作）。"""
    handler = _threadsafe(loop, dispatch, watchdog)
    installed = []
    for sig in (handled_signals(signal_mod=signal_mod) if signals is None else signals):
        try:
            signal_mod.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):
            continue
        installed.append(sig)
    return installed


def _threadsafe(loop, dispatch, watchdog=None):
    def handler(signum, frame):
        if watchdog is not None:
            watchdog.arm()
        try:
            loop.call_soon_threadsafe(dispatch, signum)
        except RuntimeError:              # 循环已经关了：直接交给 dispatch
            dispatch(signum)
    return handler


class Watchdog:
    """浏览器和命令行模式的兜底：收到第一个退出信号起计时，seconds 秒后进程还在，就在自己的线程里调
    on_expire（main.py：等证据写盘、os._exit）。收尾本身有上限（停管线最多约 5.3 秒、证据最多 2 秒），走得完
    就轮不到它；它管的是事件循环被卡住、取消主任务这一步根本跑不到的时候。没写完结尾的那一场，下次启动补一条
    （app/audit_recover.py）。

    start() 在主线程、装处理函数之前调：起一个阻塞读 socketpair 的 daemon 线程。arm() 在信号处理函数里调，
    只往 socketpair 写一个字节——不拿锁、不起线程：处理函数可能正好打断了拿着某把锁（比如起线程用的那把）的
    主线程，在里面再去拿就是死锁。"""

    def __init__(self, seconds, on_expire, socketpair=socket.socketpair, sleep=time.sleep):
        self.seconds = seconds
        self.on_expire = on_expire
        self._socketpair = socketpair
        self._sleep = sleep
        self._wsock = None
        self.fired = False

    def start(self):
        """起看门狗线程；起不来返回 False（只是少了这道保险，信号照样接）。"""
        try:
            rsock, wsock = self._socketpair()
            wsock.setblocking(False)
        except (OSError, ValueError):
            return False
        try:
            threading.Thread(target=self._wait, args=(rsock,), name="exit-signals-watchdog",
                             daemon=True).start()
        except RuntimeError:
            for s in (rsock, wsock):
                s.close()
            return False
        self._wsock = wsock
        return True

    def arm(self):
        wsock = self._wsock
        if wsock is None:
            return
        try:
            wsock.send(b"\0")
        except OSError:                   # 缓冲满了（已经上过弦）或者已经关了
            pass

    def _wait(self, rsock):
        while True:
            try:
                data = rsock.recv(1)
            except InterruptedError:
                continue
            except OSError:
                return
            if not data:
                return
            break
        self._sleep(self.seconds)
        self.fired = True
        try:
            self.on_expire()
        except BaseException:             # 包括 SystemExit：退出由调用方用 os._exit 做
            pass


class WakeupBridge:
    """窗口模式：把信号从 C 层直接送到一个后台线程，不等主线程回到 Python 代码（见模块说明）。

    install() 必须在主线程调（signal.signal、set_wakeup_fd 只许主线程用）。读线程是 daemon 线程，
    读到一个字节就交一次 dispatch。Python 层的处理函数只在读线程不在了的时候才自己调 dispatch——
    两条路都送的话，同一个信号会被算成两次（第二次就成了「又收到一次」）。

    第一次的回调（收尾：停管线最多 5 秒、等证据写盘最多 2 秒）放进一个新的 daemon 线程跑，读线程不等它、
    接着读：收尾途中再来的信号照样读得到、交给 on_repeat（main.py：不再等收尾，直接退出）。以前读线程自己
    同步跑收尾，收尾那几秒里第二个信号的字节没人读，Python 层的处理函数又看读线程活着、什么都不做——
    第二次 Ctrl-C 不起作用（第 1 轮审查用 exp/review-r1/second_signal.py 实测）。之后的回调还在读线程上跑：
    on_repeat 本身就是退出。"""

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
                self._deliver(signum)

    def _deliver(self, signum):
        """交一个信号：第一次的回调放进新线程（见类说明），之后的在读线程上直接跑。dispatch 不是 Dispatch
        （没有 claim）就照旧直接调。新线程起不来（线程数到顶）也照旧在这里跑：收尾总要有人做。"""
        claim = getattr(self.dispatch, "claim", None)
        if claim is None:
            self.dispatch(signum)
            return
        first, run = claim(signum)
        if first:
            try:
                threading.Thread(target=run, name="exit-signals-first", daemon=True).start()
                return
            except RuntimeError:
                pass
        run()

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
