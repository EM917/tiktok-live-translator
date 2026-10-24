"""浮窗和管线的接线（app/pipeline_float.py）：开 / 关 / 停止就关、自己停了留下结束卡片 / 浏览器模式不可用。

宿主是替身（只记 request_*），不开任何窗口。真正的窗口宿主在 tests/test_float_window.py；
结束卡片要用的那几处宿主改动（js_api、focus_main、主窗口存在宿主上）和 Dock 提醒
（app/window_attention.request_attention）也在这个文件末尾，同样只用替身，不碰 AppKit。
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app import float_window as fw
from app import window_attention
from app.pipeline import Pipeline
from app.server import CaptionServer


# 真的那个，下面直接测它（autouse 的替身换掉的是模块上的名字）
REAL_REQUEST_ATTENTION = window_attention.request_attention


@pytest.fixture(autouse=True)
def attention(monkeypatch):
    """Dock 提醒一律换成记账的替身：真的调到 AppKit 会给 pytest 进程建一个 NSApp（Dock 里冒出一个图标）。"""
    calls = []
    monkeypatch.setattr(window_attention, "request_attention", lambda *a, **k: calls.append(1) or True)
    return calls


class FakeHost:
    def __init__(self, open_=False):
        self.open = open_
        self.calls = []
        self.on_change = None

    def is_open(self):
        return self.open

    def request_open(self):
        self.calls.append("open")

    def request_close(self):
        self.calls.append("close")


def bare_pipeline(active=True, host=None, loop=None):
    server = CaptionServer(port=0)
    p = Pipeline.__new__(Pipeline)
    p.server = server
    p._stream_task = type("T", (), {"done": lambda self: not active})()
    p._init_float_window()
    if host is not None:
        p.attach_float_host(host, loop)
    return p


# ---- 浏览器 / 命令行模式 ------------------------------------------------------------------

def test_without_a_host_the_feature_does_not_exist():
    p = bare_pipeline()
    assert "float_window" not in p.server.config          # 页面据此不画顶栏按钮
    assert p.server.on_status is None
    assert p.handle_control({"type": "float_window", "on": True}) is None     # 忽略，不报错
    assert p.handle_control({"type": "float_window", "on": False}) is None


def test_a_half_built_pipeline_is_fine_too():
    p = Pipeline.__new__(Pipeline)                        # 个别测试绕过 __init__
    assert p._set_float_window(True) is None
    p._float_on_status({"state": "idle"})
    p._float_changed(True)


# ---- 挂上宿主 -----------------------------------------------------------------------------

def test_attaching_a_host_makes_hello_say_the_feature_is_available():
    host = FakeHost()
    p = bare_pipeline(host=host)
    assert p.server.config["float_window"] == {"available": True, "open": False}
    assert host.on_change == p._float_changed
    assert p.server.on_status == p._float_on_status


def test_detaching_removes_every_trace():
    host = FakeHost()
    p = bare_pipeline(host=host)
    p.detach_float_host()                                  # 窗口没开成、退回浏览器
    assert "float_window" not in p.server.config and p.server.on_status is None
    assert p.handle_control({"type": "float_window", "on": True}) is None
    assert host.calls == []


# ---- 开 / 关 ------------------------------------------------------------------------------

def test_the_toggle_asks_the_host_and_never_blocks_the_loop():
    host = FakeHost()
    p = bare_pipeline(host=host)
    assert p.handle_control({"type": "float_window", "on": True}) is None   # 不返回协程：不占消息循环
    assert p.handle_control({"type": "float_window", "on": False}) is None
    assert host.calls == ["open", "close"]


def test_a_stale_click_while_idle_does_not_open_an_empty_window():
    host = FakeHost()
    p = bare_pipeline(active=False, host=host)
    p.handle_control({"type": "float_window", "on": True})
    assert host.calls == []
    p.handle_control({"type": "float_window", "on": False})   # 关永远允许
    assert host.calls == ["close"]


def test_a_malformed_message_means_off():
    host = FakeHost()
    p = bare_pipeline(host=host)
    p.handle_control({"type": "float_window"})
    p.handle_control({"type": "float_window", "on": "no"})     # 字符串 "no" 为真值：按开处理
    assert host.calls == ["close", "open"]


# ---- 停止就关；自己停了留下结束卡片（专家审查 U3） -------------------------------------------
# 以前这一节钉的是「不是 live / connecting 就关」：ended、error 也关，浮窗没有任何说明就消失了，主窗口以外
# 没有提醒（U3）。现在只看 state：idle 关窗；ended / error 留着窗口（页面换成结束卡片，见 tests/float.test.mjs），
# 从监听中停下来的那一下 Dock 跳一次；live / connecting 不动。

def test_idle_closes_the_float_but_ended_and_error_keep_it_for_the_end_card(attention):
    host = FakeHost(open_=True)
    p = bare_pipeline(host=host)
    p._float_on_status({"type": "status", "state": "idle"})
    assert host.calls == ["close"]
    for state in ("ended", "error"):
        host.calls.clear()
        p._float_on_status({"type": "status", "state": "live"})
        p._float_on_status({"type": "status", "state": state})
        assert host.calls == [], state                       # 不关：页面要把结束卡片画出来
    for state in ("live", "connecting"):
        host.calls.clear()
        p._float_on_status({"type": "status", "state": state})
        assert host.calls == [], state
    assert len(attention) == 2                               # ended、error 各一次


def test_a_state_the_hook_does_not_know_still_closes_the_float():
    host = FakeHost(open_=True)
    p = bare_pipeline(host=host)
    p._float_on_status({"type": "status", "state": "something_new"})
    assert host.calls == ["close"]                            # 认不出的照旧关，不留一个说不清的窗口


def test_the_dock_bounces_only_when_monitoring_stops_on_its_own(attention):
    host = FakeHost(open_=True)
    p = bare_pipeline(host=host)
    for state in ("connecting", "live", "connecting", "live"):    # 开播、断流重连、回来：都不提醒
        p._float_on_status({"state": state})
    assert attention == []
    p._float_on_status({"state": "ended"})
    assert len(attention) == 1
    p._float_on_status({"state": "ended"})                    # 同一场接连两条结束状态：只跳一次
    p._float_on_status({"state": "error"})
    assert len(attention) == 1
    p._float_on_status({"state": "connecting"})               # 换主播或重新开始：下一场停了再提醒
    p._float_on_status({"state": "error"})
    assert len(attention) == 2


def test_an_error_from_ready_does_not_bounce_the_dock(attention):
    host = FakeHost(open_=True)
    p = bare_pipeline(host=host)
    p._float_on_status({"state": "idle"})
    p._float_on_status({"state": "error"})                    # 待机时点开始、地址不合格：人就在主窗口里
    assert attention == [] and host.calls == ["close"]


def test_the_dock_bounces_even_when_the_float_is_not_open(attention):
    host = FakeHost(open_=False)
    p = bare_pipeline(host=host)
    p._float_on_status({"state": "live"})
    p._float_on_status({"state": "ended"})
    assert len(attention) == 1                                # 主窗口以外总得有个地方看得出来
    assert host.calls == []


def test_without_a_host_nothing_bounces(attention):
    p = bare_pipeline()
    p._float_on_status({"state": "live"})
    p._float_on_status({"state": "ended"})
    assert attention == []                                    # 浏览器 / 命令行模式：没有 Dock 图标可跳


def test_the_hook_is_wired_to_the_real_status_broadcast(attention):
    host = FakeHost(open_=True)
    p = bare_pipeline(host=host)

    async def go():
        await p.server.status("live", "")
        await p.server.status("connecting", "")
        await p.server.status("idle", "正在停止…")
        await p.server.status("ended", "")
    asyncio.run(go())
    assert host.calls == ["close"]                            # 只有 idle 关；ended 留着窗口
    assert attention == []                                    # idle 之后的 ended 不是「从监听中停下来」

    host2 = FakeHost(open_=True)
    p2 = bare_pipeline(host=host2)

    async def ended():
        await p2.server.status("connecting", "")
        await p2.server.status("live", "")
        await p2.server.status("ended", "上一场字幕在下方。")
    asyncio.run(ended())
    assert host2.calls == [] and len(attention) == 1


def test_a_user_stop_sends_two_idles_and_the_float_only_closes(attention):
    """修复计划审查 P3：_stop_locked 先把 _stop_reason 清空、最后才发停完的那条 idle。读 _stop_reason 的话
    第二条 idle 会被当成意外结束；只看 state 的话两条都只是关窗，不提醒、不翻卡片。走的是真的 _stop_locked。"""
    host = FakeHost(open_=True)
    p = bare_pipeline(host=host)
    p._stats_task = None
    p._asr_pool = None
    p.audit = None
    sent = []
    real_broadcast = p.server.broadcast

    async def capture(msg):
        sent.append(dict(msg))
        await real_broadcast(msg)
    p.server.broadcast = capture

    async def go():
        await p.server.status("live", "")

        async def stream():
            await asyncio.sleep(30)
        p._stream_task = asyncio.ensure_future(stream())
        await asyncio.sleep(0)
        p._stop_reason = "user_stop"                         # 同 handle_control 的 stop 分支
        await asyncio.wait_for(p._stop_locked(), timeout=3)
    asyncio.run(go())
    states = [(m["state"], m["detail"]) for m in sent if m.get("type") == "status"]
    assert [s for s, _ in states] == ["live", "idle", "idle"], states
    assert states[1][1] == "正在停止…" and states[2][1] == ""
    assert getattr(p, "_stop_reason", None) is None           # 第二条 idle 发出时它已经清空了
    assert host.calls == ["close", "close"]                   # 两条都只是关窗（真宿主第二次什么都不做）
    assert attention == []


# ---- 状态回到页面 -------------------------------------------------------------------------

def test_a_change_from_a_gui_thread_is_published_on_the_loop():
    host = FakeHost()
    sent = []

    async def go():
        loop = asyncio.get_running_loop()
        p = bare_pipeline(host=host, loop=loop)

        async def capture(msg):
            sent.append(msg)
        p.server.broadcast = capture
        host.open = True
        worker = threading.Thread(target=p._float_changed, args=(True, None))
        worker.start()                                     # 窗口事件线程 / 工作线程
        worker.join()
        await asyncio.sleep(0.05)
        return p

    p = asyncio.run(go())
    assert sent == [{"type": "config", "float_window": {"available": True, "open": True}}]
    assert p.server.config["float_window"] == {"available": True, "open": True}


def test_a_failed_open_also_says_so_in_a_notice():
    host = FakeHost()
    sent = []

    async def go():
        p = bare_pipeline(host=host, loop=asyncio.get_running_loop())

        async def capture(msg):
            sent.append(msg)
        p.server.broadcast = capture
        from app.float_window import OPEN_FAILED
        p._float_changed(False, OPEN_FAILED)
        await asyncio.sleep(0.05)
        return OPEN_FAILED

    error = asyncio.run(go())
    assert [m["type"] for m in sent] == ["config", "notice"]
    assert sent[0]["float_window"]["open"] is False
    assert sent[1]["text"] == error and error.en == "Couldn’t open the Floating Window."
    # 话里只有观察，没有原因（CLAUDE.md 第八条）
    assert not any(w in error.en.lower() for w in ("because", "probably", "likely", "due to"))


def test_a_closed_loop_does_not_make_a_gui_thread_raise():
    host = FakeHost()
    loop = asyncio.new_event_loop()
    p = bare_pipeline(host=host, loop=loop)
    loop.close()
    p._float_changed(True)                                 # 退出过程中的迟到事件：安静地丢掉


def test_a_broadcast_failure_is_reported_not_raised(capsys):
    host = FakeHost()

    async def go():
        p = bare_pipeline(host=host, loop=asyncio.get_running_loop())

        async def boom(msg):
            raise RuntimeError("socket gone")
        p.server.broadcast = boom
        await p._publish_float()
    asyncio.run(go())
    assert "[警告] 发布浮窗状态失败：socket gone" in capsys.readouterr().out


# ---- 只显示：不碰别的 ---------------------------------------------------------------------

def test_the_float_message_touches_no_audit_and_no_other_control_path():
    host = FakeHost()
    p = bare_pipeline(host=host)
    p.audit = type("A", (), {"__getattr__": lambda self, name: (_ for _ in ()).throw(
        AssertionError("浮窗不该碰审计：" + name))})()
    p.handle_control({"type": "float_window", "on": True})
    p.handle_control({"type": "float_window", "on": False})
    p._float_on_status({"state": "ended"})


# ---- Dock 提醒（app/window_attention.request_attention） -----------------------------------------

class FakeApp:
    def __init__(self):
        self.requests = []

    def requestUserAttention_(self, kind):
        self.requests.append(kind)
        return 7


def test_request_attention_hands_one_informational_bounce_to_the_main_thread():
    app, queued = FakeApp(), []
    assert REAL_REQUEST_ATTENTION(platform="darwin", app=app, call_after=queued.append) is True
    assert app.requests == []                                 # 不在调用方的线程上碰 AppKit
    (job,) = queued
    job()                                                     # 主线程上跑
    assert app.requests == [window_attention.NS_INFORMATIONAL_REQUEST] == [10]   # 只跳一下，不是一直跳


def test_request_attention_is_a_no_op_off_macos():
    app, queued = FakeApp(), []
    for platform in ("win32", "linux"):
        assert REAL_REQUEST_ATTENTION(platform=platform, app=app, call_after=queued.append) is False
    assert queued == [] and app.requests == []


def test_request_attention_never_raises(capsys):
    def broken(fn):
        raise RuntimeError("no run loop")
    assert REAL_REQUEST_ATTENTION(platform="darwin", app=FakeApp(), call_after=broken) is False
    assert "[警告] Dock 图标提醒没有生效：no run loop" in capsys.readouterr().out


def test_the_alert_title_path_does_not_go_through_the_dock_bounce(monkeypatch):
    """报警那一路（窗口标题）行为不变：不经过 request_attention。"""
    hits = []
    monkeypatch.setattr(window_attention, "request_attention", lambda *a, **k: hits.append(1))

    class Win:
        titles = []

        def set_title(self, text):
            self.titles.append(text)
    attention = window_attention.WindowAttention(Win(), base="T")
    assert attention.set_attention(2, "p", 1) is True
    assert hits == []


# ---- 「回到主窗口」：js_api、focus_main、主窗口存在宿主上 ----------------------------------------

class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self):
        for fn in list(self.handlers):
            fn()

    def wait(self, timeout=None):
        return True


class FakeMainWindow:
    def __init__(self, miniaturized=False, fail=False):
        self.calls = []
        self.fail = fail
        self.events = SimpleNamespace(closed=FakeEvent())
        self.native = SimpleNamespace(isMiniaturized=lambda: miniaturized)

    def restore(self):
        self.calls.append("restore")

    def show(self):
        if self.fail:
            raise RuntimeError("window is gone")
        self.calls.append("show")


def float_host(platform="darwin", create=None, call_after=None):
    """call_after 是「交给主线程」的替身（真的是 AppHelper.callAfter）；不传就当场执行。"""
    made = []


    def create_window(title, url, **kwargs):
        window = SimpleNamespace(title=title, url=url, kwargs=kwargs,
                                 events=SimpleNamespace(moved=FakeEvent(), resized=FakeEvent(),
                                                        closed=FakeEvent(), shown=FakeEvent()),
                                 native=None, destroy=lambda: None)
        made.append(window)
        return window
    host = fw.FloatWindowHost(create or create_window, "http://127.0.0.1:8765/", platform=platform,
                              screens=lambda: [(0, 0, 1440, 900)], load=lambda: {}, save=lambda k, v: None,
                              spawn=lambda fn: fn(), mac_tweaks=None, frame_reader=None,
                              call_after=call_after or (lambda fn: fn()))
    return host, made


def test_the_float_window_gets_a_js_api_with_only_focus_main():
    host, made = float_host()
    host.request_open()
    (window,) = made
    api = window.kwargs["js_api"]
    assert isinstance(api, fw.FloatApi)
    # pywebview 把 js_api 上不以下划线开头的属性都挂到页面上：只该有这一个
    assert [n for n in dir(api) if not n.startswith("_")] == ["focus_main"]


def test_an_old_pywebview_without_js_api_just_gets_no_button():
    def old_create(title, url, width=None, height=None, min_size=None, resizable=None, on_top=None,
                   focus=None, transparent=None, background_color=None):
        return SimpleNamespace(events=SimpleNamespace(), native=None, destroy=lambda: None)
    host, _made = float_host(create=old_create)
    host.request_open()                                       # 多传一个不认的参数会抛 TypeError：这里没抛
    assert host.is_open()


def test_follow_main_window_keeps_the_main_window_on_the_host_and_forgets_it_on_close():
    host, _made = float_host()
    main = FakeMainWindow()
    assert host.main_window is None and host.focus_main() is False
    assert fw.follow_main_window(main, host) is True
    assert host.main_window is main
    assert fw.FloatApi(host).focus_main() is True             # 页面里的 pywebview.api.focus_main()
    assert main.calls == ["show"]
    main.events.closed.fire()
    assert host.main_window is None and host.focus_main() is False


def test_focus_main_restores_a_minimized_window_on_macos_only():
    host, _made = float_host(platform="darwin")
    host.main_window = FakeMainWindow(miniaturized=True)
    assert host.focus_main() is True
    assert host.main_window.calls == ["restore", "show"]
    host.main_window = FakeMainWindow(miniaturized=False)
    host.focus_main()
    assert host.main_window.calls == ["show"]                 # 没缩进 Dock：不调 restore
    host, _made = float_host(platform="win32")
    host.main_window = FakeMainWindow(miniaturized=True)
    host.focus_main()
    assert host.main_window.calls == ["show"]                 # winforms 的 restore 会把最大化的窗口改回普通大小


def test_focus_main_reads_appkit_only_on_the_main_thread():
    """JS 桥的工作线程上不碰 AppKit：isMiniaturized、restore、show 一起交给主线程（AppHelper.callAfter）。"""
    queued = []
    host, _made = float_host(platform="darwin", call_after=queued.append)
    reads = []
    main = FakeMainWindow()
    main.native = SimpleNamespace(isMiniaturized=lambda: reads.append(threading.current_thread()) or True)
    host.main_window = main
    bridge = threading.Thread(target=lambda: reads.append(("returned", host.focus_main())))
    bridge.start()                                            # pywebview 每次 JS 调用新开一个线程
    bridge.join()
    assert reads == [("returned", True)]                      # 桥线程上只是排进主线程的队列
    assert main.calls == [] and len(queued) == 1
    queued.pop()()                                            # 主线程（这里是跑测试的线程）执行
    assert reads[1:] == [threading.current_thread()]
    assert main.calls == ["restore", "show"]


def test_focus_main_off_macos_does_not_queue_anything():
    def no_queue(fn):
        raise AssertionError("别的平台不该交给 AppKit 的主线程")
    host, _made = float_host(platform="win32", call_after=no_queue)
    host.main_window = FakeMainWindow(miniaturized=True)
    assert host.focus_main() is True
    assert host.main_window.calls == ["show"]


def test_focus_main_never_raises_into_the_js_bridge(capsys):
    # macOS：出错发生在主线程上那一步——不往 AppKit 的事件循环里抛，只打一行警告；桥上返回的是「交出去了」
    host, _made = float_host()
    host.main_window = FakeMainWindow(fail=True)
    assert host.focus_main() is True
    assert "[警告] 没能把主窗口提到前面：window is gone" in capsys.readouterr().out
    # 交不出去（拿不到 AppHelper 之类）：桥上返回 False
    host, _made = float_host(call_after=lambda fn: (_ for _ in ()).throw(RuntimeError("no AppHelper")))
    host.main_window = FakeMainWindow()
    assert host.focus_main() is False
    assert "[警告] 没能把主窗口提到前面：no AppHelper" in capsys.readouterr().out
    # 别的平台：show 直接在桥线程上调（pywebview 自己切到界面线程），出错返回 False
    host, _made = float_host(platform="win32")
    host.main_window = FakeMainWindow(fail=True)
    assert host.focus_main() is False
    assert "[警告] 没能把主窗口提到前面：window is gone" in capsys.readouterr().out
    assert fw.bring_to_front(None) is False


def test_focus_main_does_not_touch_the_float_window_or_the_pipeline():
    host, made = float_host()
    host.request_open()
    host.main_window = FakeMainWindow()
    changes = []
    host.on_change = lambda *a: changes.append(a)
    host.focus_main()
    assert host.is_open() and changes == []                   # 浮窗留在原处，不广播任何状态

