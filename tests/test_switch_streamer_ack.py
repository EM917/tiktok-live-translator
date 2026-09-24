"""切换主播时的「开始」回执文案（app/pipeline.py 的 _start_with_ack）。

停掉旧管线可能要好几秒（等 ffmpeg 退出），期间界面不该像没反应——但「换到
另一个主播」和「同一个主播换了个地址/这是第一次开播」是两种处境：前者要多
说一句「正在切换」，不然中控会把这几秒的等待当成程序卡死。
"""
import asyncio

from app.pipeline import Pipeline


class FakeServer:
    def __init__(self, room_url=None):
        self.config = {"room_url": room_url} if room_url else {}
        self.statuses = []

    async def status(self, state, detail=""):
        self.statuses.append((state, detail))


def make_pipeline(room_url=None, stream_task=None):
    """Pipeline.__new__ 造半成品实例，只装 _start_with_ack 用得到的几样；
    start_stream 换成记录调用的假函数，不真的起管线。"""
    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer(room_url)
    p._stream_task = stream_task
    started = []

    async def fake_start_stream(url, media=None):
        started.append((url, media))

    p.start_stream = fake_start_stream
    p.started = started
    return p


async def _live_task():
    async def polite():
        await asyncio.sleep(30)

    task = asyncio.ensure_future(polite())
    await asyncio.sleep(0)     # 让它先跑到 sleep(30)，_stream_task.done() 才是 False
    return task


async def _finish(task):
    """测试收尾：真的取消并等它结束，不留后台任务。"""
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


def test_first_ever_start_uses_the_default_ack():
    async def scenario():
        p = make_pipeline(room_url=None, stream_task=None)
        await p._start_with_ack("https://www.tiktok.com/@bella/live")
        return p

    p = asyncio.run(scenario())
    assert p.server.statuses == [("connecting", "已收到指令，正在连接…")]
    assert p.started == [("https://www.tiktok.com/@bella/live", None)]


def test_same_streamer_new_address_uses_the_default_ack():
    """同一个主播换了个地址（比如从直播间链接换成带参数的分享链接）：不算切换主播。"""
    async def scenario():
        task = await _live_task()
        p = make_pipeline(room_url="https://www.tiktok.com/@bella/live", stream_task=task)
        await p._start_with_ack("https://www.tiktok.com/@bella/live?enter=share")
        await _finish(task)
        return p

    p = asyncio.run(scenario())
    assert p.server.statuses == [("connecting", "已收到指令，正在连接…")]


def test_switching_to_a_different_streamer_names_both_in_the_ack():
    async def scenario():
        task = await _live_task()
        p = make_pipeline(room_url="https://www.tiktok.com/@bella/live", stream_task=task)
        await p._start_with_ack("https://www.tiktok.com/@elisa/live")
        await _finish(task)
        return p

    p = asyncio.run(scenario())
    assert p.server.statuses == [
        ("connecting", "正在切换到 @elisa：先停止 @bella 的监听，再连接新的直播间…")]
    assert p.started == [("https://www.tiktok.com/@elisa/live", None)]


def test_no_current_session_does_not_claim_a_switch_even_with_a_stale_room_url():
    """没有正在进行的会话（_stream_task 是 None）时不该说「正在切换」——
    就算 config 里还留着上一场的 room_url。"""
    async def scenario():
        p = make_pipeline(room_url="https://www.tiktok.com/@bella/live", stream_task=None)
        await p._start_with_ack("https://www.tiktok.com/@elisa/live")
        return p

    p = asyncio.run(scenario())
    assert p.server.statuses == [("connecting", "已收到指令，正在连接…")]


def test_a_finished_stream_task_does_not_claim_a_switch():
    """_stream_task 存在但已经跑完（上一场早就自然结束了）：不算「正在进行」。"""
    async def scenario():
        async def already_done():
            return None

        task = asyncio.ensure_future(already_done())
        await task
        p = make_pipeline(room_url="https://www.tiktok.com/@bella/live", stream_task=task)
        await p._start_with_ack("https://www.tiktok.com/@elisa/live")
        return p

    p = asyncio.run(scenario())
    assert p.server.statuses == [("connecting", "已收到指令，正在连接…")]


def test_direct_stream_url_without_a_handle_never_claims_a_switch():
    """直接流地址（.flv 之类）解析不出主播名——不管是当前的还是新的，
    都不该编出一个「正在切换」的说法。"""
    async def scenario():
        task = await _live_task()
        p = make_pipeline(room_url="https://pull-flv.tiktokcdn.com/stream.flv",
                          stream_task=task)
        await p._start_with_ack("https://www.tiktok.com/@elisa/live")
        await _finish(task)
        return p

    p = asyncio.run(scenario())
    assert p.server.statuses == [("connecting", "已收到指令，正在连接…")]
