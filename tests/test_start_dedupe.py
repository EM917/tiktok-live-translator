"""页面看门狗重发的 start 不再被服务端处理两遍。

专家审查 F7（2026-10-21，10-22 补核部分成立）：页面发出「开始」后 8 秒没收到 connecting 回执，就在原连接上
把同一个 start 再发一次（web/app.js armStartWatchdog，这次不改）。服务端照单全收：同一个房间处理两遍，
多一条「连接中」、多一次 config 广播，两次处理之间恰好让出事件循环时还可能多出一场极短的会话。
主要来源（桌面重译在消息循环里同步等强模型，B6）已经去掉；这里是兜底：内容相同、20 秒内、当前会话
就是这个房间而且正在连接或监听，就只重发一次当前状态（页面据此清掉在途的 start），然后不理。
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app.pipeline import Pipeline
from app.server import CaptionServer

ROOM = "https://www.tiktok.com/@ficticia_cuatro/live"
OTHER = "https://www.tiktok.com/@ficticia_cinco/live"


class RecordingServer(CaptionServer):
    def __init__(self):
        super().__init__(port=0)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(msg)
        await super().broadcast(msg)

    @property
    def statuses(self):
        return [m["state"] for m in self.messages if m.get("type") == "status"]


@pytest.fixture
def pl(monkeypatch, tmp_path):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False,
        cookies_browser="none")
    p = Pipeline(args, RecordingServer())
    p.started = []

    async def fake_start_stream(url, media=None):
        # 真的 start_stream 会设 room_url、建任务；这里只记下开了几场，不拉流
        p.started.append(url)
        p.server.config["room_url"] = url

    p.start_stream = fake_start_stream
    return p


def payload(url=ROOM, **extra):
    msg = {"type": "start", "url": url, "source": "es", "alerts": False, "brand": ""}
    msg.update(extra)
    return msg


async def handle(p, msg):
    result = p.handle_control(msg)
    if asyncio.iscoroutine(result):
        await result


def test_the_watchdog_resend_opens_one_session_and_gets_the_status_again(pl):
    async def scenario():
        await handle(pl, payload())
        before = list(pl.server.statuses)
        await handle(pl, payload())                  # 8 秒后看门狗补发的同一条
        return before

    before = asyncio.run(scenario())
    assert pl.started == [ROOM]                      # 只开了一场
    assert before == ["connecting"]
    assert pl.server.statuses == ["connecting", "connecting"]   # 第二次只重发一次当前状态


def test_a_resend_while_live_gets_the_live_status(pl):
    async def scenario():
        await handle(pl, payload())
        await pl.server.status("live", "")           # 生产里直播中的状态就是空 detail
        await handle(pl, payload())

    asyncio.run(scenario())
    assert pl.started == [ROOM]
    assert pl.server.statuses == ["connecting", "live", "live"]
    assert pl.server.messages[-1]["detail"] == ""


def test_switching_streamers_is_never_swallowed(pl):
    """换主播（url 不同）照常处理。"""
    async def scenario():
        await handle(pl, payload())
        await handle(pl, payload(OTHER))

    asyncio.run(scenario())
    assert pl.started == [ROOM, OTHER]


def test_the_same_room_after_20_seconds_is_a_new_start(pl):
    async def scenario():
        await handle(pl, payload())
        key, at = pl._last_start
        pl._last_start = (key, at - Pipeline.START_DEDUPE_SEC - 1)
        await handle(pl, payload())

    asyncio.run(scenario())
    assert pl.started == [ROOM, ROOM]


def test_after_stop_the_same_room_starts_again(pl):
    """停止之后状态是 idle：同一个房间再点开始，照常处理。"""
    async def scenario():
        await handle(pl, payload())
        await pl.server.status("idle", "")
        await handle(pl, payload())

    asyncio.run(scenario())
    assert pl.started == [ROOM, ROOM]


def test_a_failed_start_can_be_retried_at_once(pl):
    """上一次开始出错（状态 error）：马上再点同一个房间，照常处理。"""
    async def scenario():
        await handle(pl, payload())
        await pl.server.status("error", "")
        await handle(pl, payload())

    asyncio.run(scenario())
    assert pl.started == [ROOM, ROOM]


@pytest.mark.parametrize("change", [{"alerts": True}, {"brand": "marca_ficticia"},
                                    {"source": "auto"}, {"media": "https://cdn.example.invalid/a.flv"}])
def test_a_different_payload_for_the_same_room_is_processed(pl, change):
    """只认完全相同的 payload。"""
    async def scenario():
        await handle(pl, payload())
        await handle(pl, payload(**change))

    asyncio.run(scenario())
    assert pl.started == [ROOM, ROOM]


def test_a_resend_does_not_touch_settings_or_brand(pl, monkeypatch):
    """被当成重发的那一条什么都不改：不再写设置、不再广播 config。"""
    saved = []

    async def scenario():
        await handle(pl, payload())
        monkeypatch.setattr(pl, "_save_setting", lambda key, value: saved.append(key))
        n = len(pl.server.messages)
        await handle(pl, payload())
        return pl.server.messages[n:]

    after = asyncio.run(scenario())
    assert saved == []
    assert [m["type"] for m in after] == ["status"]


def test_the_window_is_measured_from_the_accepted_start(pl):
    """记下的是被接受的那一次 start 的单调时钟时间。"""
    async def scenario():
        t0 = time.monotonic()
        await handle(pl, payload())
        return t0

    t0 = asyncio.run(scenario())
    key, at = pl._last_start
    assert key[0] == ROOM and at >= t0


# ---- 只在真正开始处理之后才记；处理途中出错就撤掉（专家审查 F7 复核）----

def test_nothing_is_remembered_until_the_start_actually_runs(pl):
    """handle_control 交回协程那一刻还没记：server 真去 await 了才算被接受。"""
    result = pl.handle_control(payload())
    assert getattr(pl, "_last_start", None) is None
    asyncio.run(result)
    assert pl._last_start[0][0] == ROOM


def test_a_start_that_raises_does_not_swallow_the_retry(pl):
    """start_stream 在停旧场时抛了异常：状态停在 connecting，room_url 还是这个房间（上一场就是它）。
    马上再点同一个房间要照常处理，不能被当成重发吞掉。"""
    attempts = []
    real = pl.start_stream

    async def flaky_start(url, media=None):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("停旧场时出错")
        await real(url, media=media)

    pl.start_stream = flaky_start
    pl.server.config["room_url"] = ROOM              # 上一场就是这个房间

    async def scenario():
        with pytest.raises(OSError):
            await handle(pl, payload())
        state = pl.server.config["status"]["state"]
        remembered = getattr(pl, "_last_start", None)
        await handle(pl, payload())                  # 中控马上再点一次
        return state, remembered

    state, remembered = asyncio.run(scenario())
    assert state == "connecting"                     # 正是会被误判成重发的那种状态
    assert remembered is None                        # 出错的那一次撤掉了
    assert attempts == [ROOM, ROOM] and pl.started == [ROOM]   # 第二次照常处理
    assert pl._last_start[0][0] == ROOM              # 这次成功的记下了：之后的重发照样去重


def test_a_cancelled_start_is_forgotten_too(pl):
    """处理途中被取消（页面断开，server 那边等着的协程被取消）：同样撤掉，重开不会被吞。"""
    async def scenario():
        # 闸在协程里建：Python 3.9 的 asyncio.Event() 构造时就绑当前事件循环（CI 的 3.9 任务，PR #123）
        gate = asyncio.Event()

        async def stuck_start(url, media=None):
            pl.server.config["room_url"] = url
            await gate.wait()

        pl.start_stream = stuck_start
        task = asyncio.ensure_future(handle(pl, payload()))
        for _ in range(100):
            if getattr(pl, "_last_start", None) is not None:
                break
            await asyncio.sleep(0.01)
        during = pl._last_start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return during

    during = asyncio.run(scenario())
    assert during is not None and during[0][0] == ROOM
    assert pl._last_start is None


def test_a_failure_does_not_clear_a_newer_accepted_start(pl):
    """只撤自己那一条：处理途中别的页面的 start 已经被接受、记成更新的那条时，不碰它。"""
    calls = []

    async def scenario():
        gate = asyncio.Event()                       # 同上：在协程里建

        async def first_slow_then_fail(url, media=None):
            calls.append(url)
            if len(calls) == 1:
                await gate.wait()
                raise OSError("出错")
            pl.server.config["room_url"] = url

        pl.start_stream = first_slow_then_fail
        first = asyncio.ensure_future(handle(pl, payload()))
        await asyncio.sleep(0.01)
        await handle(pl, payload(OTHER))             # 另一个页面换了主播，被接受
        newer = pl._last_start
        gate.set()
        with pytest.raises(OSError):
            await first
        return newer

    newer = asyncio.run(scenario())
    assert pl._last_start is newer and newer[0][0] == OTHER
