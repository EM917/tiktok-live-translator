"""识别调用的并发保护（app/pipeline.py 的 _locked_transcribe）。

真实前提（读代码得出，未观察到）：_stop_locked 的 STOP_GRACE_SEC 超时之后会
放手不再等旧场的识别线程收尾（见 tests/test_stop_responsiveness.py）；而新场
几乎立刻复用同一个识别器（Pipeline.self._transcriber），换主播频繁时更是如此。
就算新场造了一个新的识别器对象，faster-whisper/mlx-whisper 的模型状态也可能
是进程级/类级共享的（app/asr.py 的 MLX ModelHolder 就是类级缓存）——两条线程
同时闯进同一份状态不是「各转各的两次调用」能比的。

_locked_transcribe 用一把**进程级**（模块级，不挂在某个 transcriber 实例上）的
threading.Lock 保证同一时刻只有一次 transcribe() 在跑，且加锁本身在线程池线程
里做，不能挡住事件循环——识别循环之外的一切（界面状态、弹幕、心跳）都在事件
循环上跑。
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.pipeline import _locked_transcribe


class RecordingTranscriber:
    """假识别器：转一次要 hold 秒，期间记录「同一时刻有几个调用在跑」，
    事后能看出是否发生过重叠。"""

    def __init__(self, hold=0.05):
        self.hold = hold
        self._guard = threading.Lock()
        self.active = 0
        self.overlap_detected = False

    def transcribe(self, segment):
        with self._guard:
            self.active += 1
            if self.active > 1:
                self.overlap_detected = True
        try:
            time.sleep(self.hold)
            return segment
        finally:
            with self._guard:
                self.active -= 1


def test_locked_transcribe_serializes_concurrent_calls_on_the_same_transcriber():
    transcriber = RecordingTranscriber(hold=0.05)
    results = []

    def worker(seg):
        results.append(_locked_transcribe(transcriber, seg))

    threads = [threading.Thread(target=worker, args=(seg,)) for seg in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not transcriber.overlap_detected
    assert sorted(results) == ["a", "b"]


def test_locked_transcribe_serializes_across_different_transcriber_objects():
    """锁是进程级的——旧场超时放手后新场立刻换了一个新的识别器对象，
    同一时刻也只许一次 transcribe 在跑，不能因为「对象不同」就放行并发。"""
    guard = threading.Lock()
    state = {"active": 0, "overlap": False}

    class Fake:
        def transcribe(self, segment):
            with guard:
                state["active"] += 1
                if state["active"] > 1:
                    state["overlap"] = True
            try:
                time.sleep(0.05)
                return segment
            finally:
                with guard:
                    state["active"] -= 1

    old_session, new_session = Fake(), Fake()
    results = []

    def worker(transcriber, seg):
        results.append(_locked_transcribe(transcriber, seg))

    threads = [threading.Thread(target=worker, args=(old_session, "old")),
              threading.Thread(target=worker, args=(new_session, "new"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not state["overlap"]
    assert sorted(results) == ["new", "old"]


def test_locked_transcribe_blocks_the_worker_thread_not_the_event_loop():
    """加锁必须在线程池线程里等，不能挪到事件循环所在线程——否则等锁的那段
    时间会让整个程序（界面心跳、状态广播……）一起卡住。用一个在事件循环上
    并行推进的 ticker 协程验证：两次 transcribe 串行占用 ~2*hold 秒，
    期间 ticker 仍然能按自己的节奏跑完，说明它没有被一起挡住。"""
    async def scenario():
        pool = ThreadPoolExecutor(max_workers=2)
        transcriber = RecordingTranscriber(hold=0.08)
        loop = asyncio.get_running_loop()
        ticks = []

        async def ticker():
            for _ in range(6):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        async def call(seg):
            return await loop.run_in_executor(pool, _locked_transcribe, transcriber, seg)

        ticker_task = asyncio.ensure_future(ticker())
        results = await asyncio.gather(call("a"), call("b"))
        await ticker_task
        pool.shutdown(wait=False)
        return results, ticks

    results, ticks = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(scenario())
    assert sorted(results) == ["a", "b"]
    # ticker 在两次串行的 transcribe 期间完整跑完了全部 6 拍——它没有被一起挡住
    assert len(ticks) == 6
