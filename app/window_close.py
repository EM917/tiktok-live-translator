"""关窗口 = 停止违禁词监听。正在监听时关窗口要先问一句，关了要留下原因。

放在 app/ 里是为了能测：main.py 导入时会 execv 进虚拟环境，测试不能 import 它。
main.py 的 run_with_window 只负责把这里的几个函数接到 pywebview 上。
"""
import asyncio
import inspect

from . import i18n

CLOSE_LOCALIZATION = {
    "global.quitConfirmation": "正在监听直播，关闭窗口会停止违禁词监听。确定关闭？",
    "global.quit": "关闭",
    "global.cancel": "取消",
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


async def close_session(pipeline, audit_budget=AUDIT_BUDGET_SEC, total=STOP_TIMEOUT_SEC):
    """窗口已经关了：留下原因、停管线；停得慢就先把审计收尾。在事件循环上跑，
    返回停止流程是否在时限内走完。"""
    audit = getattr(pipeline, "audit", None)
    if audit is not None and stream_active(pipeline):
        audit.window_closed()
    pipeline._stop_reason = "window_closed"
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
        # 识别线程或弹幕子进程一时退不掉：进程等不到停止流程走完就会退出，先让
        # session_end 落盘。close 可重入，停止流程稍后再关是空操作
        audit = getattr(pipeline, "audit", None)
        if audit is not None:
            audit.close(reason="window_closed")
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


def stop_after_close(loop, pipeline, timeout=STOP_TIMEOUT_SEC, audit_budget=AUDIT_BUDGET_SEC):
    """窗口线程里调用：webview.start() 返回（窗口关了）之后、os._exit 之前。"""
    if loop is None or pipeline is None:
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(
            close_session(pipeline, audit_budget, timeout), loop)
        return future.result(timeout=timeout)
    except Exception:
        return False
