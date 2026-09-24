"""停止必须立刻生效，哪怕识别调用还卡在线程里。

真实故障：音频积压 20 秒时点「停止」，界面像死了一样。原因是
`run_in_executor` 里的识别调用取消不掉——线程一开跑只能等它自己结束，
而 _stop_locked 是无限期 await 那个任务。
"""
import asyncio

import pytest


async def _drain_background_tasks():
    """兜底清理：_stop_comment_source 的 shield 会把「不再等」的那份 stop()
    协程留在后台按自己的节奏收尾（这正是它该有的行为）。测试跑完就该把它
    收掉，不然事件循环随 run_until_complete 返回而关闭时，这些还在跑的任务
    会被当成「未完成就被销毁」打一条无关紧要但吵人的警告。"""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
              and not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


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


def test_stop_gives_up_on_a_comment_source_that_will_not_stop():
    """前提修复：换主播是「先停旧场再连新场」，旧场的弹幕来源如果卡在
    stop() 里不返回（读代码得出的前提缺口，见 app/comment_source.py），
    也不能让 _stream_lock 一直攥在手里——不然下一次 start_stream() 会卡住，
    界面也永远停不到 idle。用假时钟等价物（把超时调到几十毫秒）而不是真等
    默认的 10 秒。"""
    async def scenario():
        p = make_pipeline()
        p.COMMENT_STOP_TIMEOUT_SEC = 0.05

        class HangingCommentSource:
            comments_received = 0

            async def stop(self):
                await asyncio.sleep(30)     # 模拟永不返回

        p.comment_source = HangingCommentSource()

        async def polite():
            await asyncio.sleep(30)

        p._stream_task = asyncio.ensure_future(polite())
        await asyncio.sleep(0)
        await asyncio.wait_for(p.stop_stream(), timeout=1)
        await _drain_background_tasks()
        return p

    p = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())
    assert p.server.statuses[-1][0] == "idle"


def test_start_stream_does_not_block_on_the_lock_after_a_stuck_comment_source():
    """停止本身没卡住还不够——它必须真的放开 _stream_lock，紧接着而来的
    start_stream()（中控换主播时最常见的操作）才不会卡在拿锁这一步上。"""
    async def scenario():
        p = make_pipeline()
        p.COMMENT_STOP_TIMEOUT_SEC = 0.05

        class HangingCommentSource:
            comments_received = 0

            async def stop(self):
                await asyncio.sleep(30)

        p.comment_source = HangingCommentSource()

        async def polite():
            await asyncio.sleep(30)

        p._stream_task = asyncio.ensure_future(polite())
        await asyncio.sleep(0)
        await asyncio.wait_for(p.stop_stream(), timeout=1)
        # start_stream() 的第一步就是 `async with self._lock()`：能在很短时间内
        # 拿到同一把锁，就说明 stop_stream() 确实已经放开了它，不是靠运气没撞上。
        await asyncio.wait_for(p._lock().acquire(), timeout=0.5)
        p._lock().release()
        await _drain_background_tasks()
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
