"""浮窗和管线的接线（app/pipeline_float.py）：开 / 关 / 监听结束就关 / 浏览器模式不可用。

宿主是替身（只记 request_*），不开任何窗口。真正的窗口宿主在 tests/test_float_window.py。
"""
import asyncio
import threading

from app.pipeline import Pipeline
from app.server import CaptionServer


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


# ---- 监听结束就关 -------------------------------------------------------------------------

def test_every_status_that_is_not_live_or_connecting_closes_the_float():
    host = FakeHost(open_=True)
    p = bare_pipeline(host=host)
    for state in ("idle", "ended", "error"):
        host.calls.clear()
        p._float_on_status({"type": "status", "state": state})
        assert host.calls == ["close"], state
    for state in ("live", "connecting"):
        host.calls.clear()
        p._float_on_status({"type": "status", "state": state})
        assert host.calls == [], state


def test_the_hook_is_wired_to_the_real_status_broadcast():
    host = FakeHost(open_=True)
    p = bare_pipeline(host=host)

    async def go():
        await p.server.status("live", "")
        await p.server.status("connecting", "")
        await p.server.status("idle", "正在停止…")
        await p.server.status("ended", "")
    asyncio.run(go())
    assert host.calls == ["close", "close"]


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
