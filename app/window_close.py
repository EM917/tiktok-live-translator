# i18n: done
"""关窗口 = 停止违禁词监听。正在监听时关窗口要先问一句，关了要留下原因。

放在 app/ 里是为了能测：main.py 导入时会 execv 进虚拟环境，测试不能 import 它。
main.py 的 run_with_window 只负责把这里的几个函数接到 pywebview 上。
"""
import asyncio
import inspect
import os
import sys
import threading

from . import i18n
from .i18n import L

CLOSE_LOCALIZATION = {
    "global.quitConfirmation": L("正在监听直播，关闭窗口会停止违禁词监听。确定关闭？",
                                 "Close the window? This stops monitoring the live stream, "
                                 "including banned-term detection."),
    "global.quit": L("关闭", "Close"),
    "global.cancel": L("取消", "Cancel"),
    # 页面里 JS confirm() 的确认键：cocoa 取 global.ok / global.cancel。以前表里没有这一项，
    # 按钮是 pywebview 自带的「OK」配我们的「取消」，中文界面上混排；补上之后两个按钮都是
    # 中文（spec §8.2 标明的中文可见修正）。第二个实例的窗口也因此开始传 localization
    # （confirm_localization_kwargs），它的两个按钮从「OK / Cancel」变成「好 / 取消」
    "global.ok": L("好", "OK"),
}
STOP_TIMEOUT_SEC = 5.0     # 窗口关了之后最多等停止流程这么久，然后进程退出
AUDIT_BUDGET_SEC = 3.5     # 停止流程到这时还没走完，先把审计收尾：session_end 不能等


def close_localization(lang=None):
    """关窗确认框（和 cocoa 下 JS confirm() 的按钮）用的文字，按 lang 渲染（缺省是当前界面
    语言）。交给 pywebview 的一律是普通 str：PyObjC / pythonnet 收到 str 子类的行为没验证过。
    运行中换语言由 app/window_lang.py 拿它原地更新 window.localization（spec §8.2）。"""
    return {key: i18n.text(value, lang) for key, value in CLOSE_LOCALIZATION.items()}


def localization_kwargs(fn, lang=None):
    """fn（create_window 或 start）认 localization 参数才传。requirements 只要求
    pywebview>=5：多传一个老版本不认的参数会抛 TypeError，窗口就退回浏览器了。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if "localization" not in params:
        return {}
    return {"localization": close_localization(lang)}


def confirm_localization_kwargs(fn, lang=None):
    """第二个实例的窗口用（双击第二次时开的那个）：它不 guard_close，localization 只给页面里
    JS confirm() 的两个按钮——cocoa 取 global.ok / global.cancel。今天这个窗口没传，按钮是
    pywebview 自带的「OK / Cancel」。

    表里有了 global.ok 才传（M11 补上它，spec §8.2 标明的中文可见修正），两个按钮一起换成
    「好 / 取消」。在那之前传，只会换掉 Cancel，变成「OK / 取消」混排；而且要是拿闸来开关，
    这处中文可见的变化就落在开闸（Z1）上了。与闸无关：英文界面上 pywebview 自带的就是英文。"""
    if "global.ok" not in CLOSE_LOCALIZATION:
        return {}
    return localization_kwargs(fn, lang)


def stream_active(pipeline):
    fn = getattr(pipeline, "_stream_active", None)
    try:
        return bool(fn()) if callable(fn) else False
    except Exception:
        return False


def guard_close(window, pipeline):
    """关窗前那一刻才决定要不要确认：正在监听（含连接中）就问，待机时直接关，不打扰。

    挂在 window.events.closing 上。pywebview 在同一次关闭里先触发 closing、再读
    confirm_close（cocoa / winforms / gtk / qt 都是这个顺序），所以这里设的值当场
    生效；Cmd+Q 也走这条路（applicationShouldTerminate → should_close）。
    处理函数必须返回 None：返回 False 会让 pywebview 直接取消这次关闭。"""
    events = getattr(window, "events", None)
    closing = getattr(events, "closing", None)
    if closing is None:
        return False

    def on_closing():
        try:
            window.confirm_close = stream_active(pipeline)
        except Exception:
            pass

    try:
        closing += on_closing
    except Exception:
        return False
    return True


async def close_session(pipeline, audit_budget=AUDIT_BUDGET_SEC, total=STOP_TIMEOUT_SEC,
                        reason="window_closed", fields=None):
    """窗口已经关了：留下原因、停管线；停得慢就先把审计收尾。在事件循环上跑，
    返回停止流程是否在时限内走完。

    reason / fields 是写进 session_end 的结束原因和附带的观察。关窗口和 ⌘Q 是 window_closed（先写一条
    window_closed 记录）；收到退出信号（app/exit_signals.py）是 signal，fields 带 {"signal": "SIGTERM"}
    这样的信号名，不写 window_closed 记录——窗口没有人关。"""
    fields = dict(fields or {})
    audit = getattr(pipeline, "audit", None)
    if audit is not None and reason == "window_closed" and stream_active(pipeline):
        audit.window_closed()
    pipeline._stop_reason = reason
    # 附带的观察跟着结束原因走：被取消的那一场自己收尾时、_stop_locked 代写时都并进 session_end
    pipeline._stop_fields = fields
    # 手机同看的监听面要随窗口一起关掉：0.0.0.0 上的端口不该比进程活得久。
    # 和停止流程并行起，最后只等一小会儿——它自己带 2 秒预算，且收尾不能为它让路
    share = None
    stopper = getattr(pipeline, "stop_viewer_share", None)
    if callable(stopper):
        try:
            share = asyncio.ensure_future(stopper("shutdown"))
        except Exception:
            share = None
    stop = asyncio.ensure_future(pipeline.stop_stream(quiet=True))
    done, _ = await asyncio.wait({stop}, timeout=audit_budget)
    if stop not in done:
        # 识别线程、音频源或弹幕子进程一时退不掉：进程等不到停止流程走完就会退出，先让
        # session_end 落盘。这一场的识别风险、证据和弹幕计数由审计自己的 end_fields 补上
        # （管线开场时挂的）。close 可重入，停止流程稍后再关是空操作
        audit = getattr(pipeline, "audit", None)
        if audit is not None:
            # 这一场在关窗之前已经在自己收尾（下播、预算用尽……）：原因照它自己记下的写（专家审查 B5）
            end_reason, end_fields = reason, fields
            ending_of = getattr(pipeline, "_ending_of", None)
            ending = ending_of(audit) if callable(ending_of) else None
            if ending is not None:
                end_reason, end_fields = ending[0] or reason, dict(ending[1])
            audit.close(reason=end_reason, **end_fields)
        rest = total - audit_budget - 0.2
        if rest > 0:
            done, _ = await asyncio.wait({stop}, timeout=rest)
    if stop in done and not stop.cancelled():
        stop.exception()             # 取走异常，别在退出时刷一屏「never retrieved」
    if share is not None:
        done2, _ = await asyncio.wait({share}, timeout=0.5)
        if share in done2 and not share.cancelled():
            share.exception()        # 同上；没走完也不再等，进程马上就退出了
    return stop in done


def stop_after_close(loop, pipeline, timeout=STOP_TIMEOUT_SEC, audit_budget=AUDIT_BUDGET_SEC,
                     reason="window_closed", fields=None):
    """窗口线程里调用：webview.start() 返回（窗口关了）之后、os._exit 之前。也给 ⌘Q（hook_app_terminate）和
    退出信号（reason="signal"，见 FinishOnce.finish_signal）用：那两条路上 start() 不返回。"""
    if loop is None or pipeline is None:
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(
            close_session(pipeline, audit_budget, timeout, reason=reason, fields=fields), loop)
        return future.result(timeout=timeout)
    except Exception:
        return False


def flush_evidence():
    """os._exit 之前调（关窗口的 FinishOnce，和 main.py 里另外几处 os._exit）：还没写完的识别证据音频
    最多再等 2 秒（app/asr_evidence.flush_pending）。证据在每一场自己的证据线程里写，os._exit 不等任何
    线程，不等就跟着进程没了。返回没等到的份数。

    这个进程没加载过证据模块就没有要等的，也不为了退出去 import 它（会连带 import numpy）。退出路上
    任何异常——包括等的时候又按了一次 Ctrl-C——都不许拦住退出，一概吞掉。"""
    module = sys.modules.get("app.asr_evidence")
    if module is None:
        return 0
    try:
        return module.flush_pending()
    except BaseException:
        return 0


class FinishOnce:
    """关窗之后的收尾（finish，即 stop_after_close）加退出进程，只走一遍。

    两条路会走到这里：webview.start() 返回之后的主线程（正常路径，finish()），和浮窗的兜底定时器
    （app/float_window.follow_main_window：主窗口关了 10 秒 GUI 循环还没退出，finish_stuck()）。
    另有两条 start() 不返回的路（专家审查 B5）：⌘Q 在主线程的 willTerminate 通知里调 finish()
    （hook_app_terminate），退出信号在 exit_signals 的读线程里调 finish_signal()。
    start() 恰好在兜底触发前几秒才返回时，两边会各跑一遍 close_session；第二遍要是赶在第一遍停掉
    直播之前开始，审计里会多一条 window_closed。所以先到的那条路收尾并退出进程：主线程后到就
    等着（先到的那条会退出进程），兜底后到直接返回。"""

    def __init__(self, finish, exit_fn=None):
        self._lock = threading.Lock()
        self._finish = finish
        self._exit = exit_fn or os._exit

    def finish(self):
        """主线程：start() 返回之后调。兜底已经在收尾了就在这里等它退出进程。"""
        self._lock.acquire()
        self._run()

    def finish_stuck(self):
        """兜底定时器调：主线程已经在收尾了就什么都不做，返回 False。"""
        if not self._lock.acquire(blocking=False):
            return False
        # 终端留一行：事后分得清这次关窗走的是正常路径还是兜底
        print("[警告] 主窗口已关闭，界面循环迟迟没有退出：走兜底收尾，随后直接退出")
        self._run()
        return True

    def finish_signal(self, name):
        """收到退出信号（app/exit_signals.py 的读线程调）：收尾时 session_end 记 reason=signal 和信号名。
        别的路已经在收尾（关了窗口、⌘Q）就什么都不做，返回 False——那条路会退出进程。
        finish 要认 reason、fields 两个关键字参数（main.py 传的是转给 stop_after_close 的 lambda）。"""
        if not self._lock.acquire(blocking=False):
            return False
        self._run(reason="signal", fields={"signal": str(name)})
        return True

    def _run(self, **kwargs):
        try:
            self._finish(**kwargs)
        finally:
            # 停止流程收尾时只等了证据写盘 1 秒（Pipeline._evidence_drain）：退出前再给没写完的一点时间
            flush_evidence()
            self._exit(0)


def hook_app_terminate(callback, center=None, note_name=None, queue=None):
    """⌘Q、程序坞里的「退出」：pywebview 的 cocoa 后端里 NSApp terminate: 在 applicationShouldTerminate 同意
    之后直接 exit()，webview.start() 不返回，start() 之后的收尾（FinishOnce.finish）一步都走不到——2026-10-23
    用真 pywebview 6.2.1 实测：closing 事件之后进程以 0 退出，atexit 都没跑，这一场的审计没有 session_end。
    以前这里的注释和 audit.py 都以为 ⌘Q 会写 session_end（专家审查 B5 的复核也这么说）。

    挂在 NSApplicationWillTerminateNotification 上：它在 exit() 之前、在主线程上发出（实测在里面同步等
    1.5 秒、再 os._exit 都没问题）。callback 在那里同步收尾并退出进程（main.py 给的是 finish.finish）。
    只在 macOS 上有；拿不到 AppKit 就什么都不做。返回挂没挂上。center / note_name / queue 只给测试注入。"""
    if center is None:
        if sys.platform != "darwin":
            return False
        try:
            from AppKit import NSApplicationWillTerminateNotification as note_name
            from Foundation import NSNotificationCenter, NSOperationQueue
            center = NSNotificationCenter.defaultCenter()
            queue = NSOperationQueue.mainQueue()
        except Exception:
            return False

    def on_terminate(_note):
        callback()

    try:
        token = center.addObserverForName_object_queue_usingBlock_(note_name, None, queue, on_terminate)
    except Exception:
        return False
    _TERMINATE_HOOKS.append((token, on_terminate))      # 别让 GC 把观察者收走
    return True


_TERMINATE_HOOKS = []
