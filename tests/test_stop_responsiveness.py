"""停止必须立刻生效，哪怕识别调用还卡在线程里。

真实故障：音频积压 20 秒时点「停止」，界面像死了一样。原因是
`run_in_executor` 里的识别调用取消不掉——线程一开跑只能等它自己结束，
而 _stop_locked 是无限期 await 那个任务。
"""
import asyncio

import pytest


class FakeServer:
    def __init__(self):
        self.statuses = []
        self.config = {}

    async def status(self, state, detail=""):
        self.statuses.append((state, detail))

    async def broadcast(self, msg):
        pass


def make_pipeline():
    from app.pipeline import Pipeline

    p = Pipeline.__new__(Pipeline)          # 不跑 __init__，只测停止逻辑
    p.server = FakeServer()
    p._stream_task = None
    p._stats_task = None
    p._selfcheck_task = None
    p._asr_pool = None
    p.audit = None
    p._stream_lock = None
    return p


def test_stop_gives_up_on_a_task_that_will_not_cancel():
    """识别线程停不下来时，停止也必须在几秒内让界面回到待机。"""
    async def scenario():
        p = make_pipeline()
        p.STOP_GRACE_SEC = 0.2

        started = asyncio.Event()

        async def stubborn():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(30)      # 模拟取消不掉的执行器调用
                raise

        orphan = asyncio.ensure_future(stubborn())
        p._stream_task = orphan
        await started.wait()
        await asyncio.wait_for(p._stop_locked(), timeout=3)
        # 撒手不管的那条协程本来就会留在后台自行收尾，这里只是别让它污染测试输出
        orphan.cancel()
        try:
            await asyncio.wait_for(orphan, 0.1)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        return p

    p = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())
    states = [s for s, _ in p.server.statuses]
    assert states[-1] == "idle"
    # 而且要先给一句「正在停止」，别让人点了以为没反应
    assert p.server.statuses[0] == ("idle", "正在停止…")


def test_stop_is_fast_when_the_task_cancels_normally():
    async def scenario():
        p = make_pipeline()

        async def polite():
            await asyncio.sleep(30)

        p._stream_task = asyncio.ensure_future(polite())
        await asyncio.sleep(0)
        await asyncio.wait_for(p._stop_locked(), timeout=1)
        return p

    p = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())
    assert p.server.statuses[-1][0] == "idle"


@pytest.mark.parametrize("quiet", [True, False])
def test_quiet_stop_does_not_announce(quiet):
    """换直播间时会先静默停掉旧的，那时不该闪一下「已停止」。"""
    async def scenario():
        p = make_pipeline()

        async def polite():
            await asyncio.sleep(30)

        p._stream_task = asyncio.ensure_future(polite())
        await asyncio.sleep(0)
        await p._stop_locked(quiet=quiet)
        return p

    p = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())
    assert (len(p.server.statuses) == 0) is quiet
