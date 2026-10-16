"""字幕翻译队列深度（telemetry.translation_queue_depth）要跟着出队走。

事故（2026-10-16 审阅）：深度只在入队时写，translation_worker 取走任务后它一直停在 1，
直到下一段才变。弹幕翻译靠 _subtitle_translation_busy（排队或在途）让路，于是几乎永远
「忙」，每一批弹幕都白等满 IDLE_WAIT_MAX_SEC（3 秒），整场弹幕约 1.2 条/秒封顶。
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app.pipeline import Pipeline
from tests.helpers import run


class StubServer:
    def __init__(self):
        self.config = {}
        self.broadcasts = []

    async def status(self, state, detail="", command=None):
        pass

    async def broadcast(self, msg):
        self.broadcasts.append(msg)


class Translator:
    """记下每次被叫到那一刻的「深度」与「真实队列长度」。弹幕也走它，所以带 name。"""

    name = "hymt2"      # 弹幕只用常驻本地模型翻（app/comments.py COMMENT_ENGINES）

    def __init__(self, p):
        self.p = p
        self.calls = []                     # 字幕与弹幕的原文，按调用顺序
        self.depths = []                    # (telemetry 里的深度, 真实 qsize)
        self.comment_at = None

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls.append(text)
        if text.startswith("comentario"):
            self.comment_at = time.monotonic()
        else:
            self.depths.append((self.p.telemetry.translation_queue_depth,
                                self.p._trans_queue.qsize()))
        return "[zh] " + text


def make_pipeline(monkeypatch, tmp_path):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", tmp_path / "banned_terms.txt")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source=None,
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto",
        model=None, device="auto", compute_type="auto", denoise="off",
    )
    server = StubServer()
    return Pipeline(args, server), server


def fake_audio(monkeypatch, frames, hang=False):
    """一帧切一段的假音频。hang：送完不收尾，模拟直播还在继续（好让测试在中途取消）。"""
    import app.audio
    import app.segmenter
    from app.audio import FRAME_BYTES

    class Source:
        def __init__(self, media, denoise_model=None):
            pass

        async def frames(self):
            for _ in range(frames):
                yield b"\x00" * FRAME_BYTES
            if hang:
                await asyncio.sleep(3600)

        def stderr_tail(self):
            return ""

        async def stop(self):
            pass

    class OneSegmentPerFrame:
        def __init__(self, *a, **k):
            pass

        def feed(self, frame):
            return [frame]

        def flush(self):
            return []

    monkeypatch.setattr(app.audio, "FFmpegAudioSource", Source)
    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", OneSegmentPerFrame)


class Transcriber:
    def __init__(self):
        self.n = 0

    def transcribe(self, pcm):
        self.n += 1
        text = "hola que tal numero %d" % self.n
        return SimpleNamespace(text=text, raw_text=text, language="es", rejected=[])


async def stream(p):
    loop = asyncio.get_running_loop()
    return await p._stream_session("http://cdn/s.flv", Transcriber(), None, loop)


def test_depth_follows_the_queue_when_the_worker_takes_a_job(monkeypatch, tmp_path):
    """worker 一取走任务，telemetry 里的深度就是此刻真实的排队数，不是入队时的旧值。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    tr = Translator(p)
    p.translator = tr
    fake_audio(monkeypatch, frames=4)

    async def scenario():
        return await stream(p)

    got_audio, _secs = run(scenario())
    assert got_audio is True
    assert len(tr.depths) >= 1
    for depth, real in tr.depths:
        assert depth == real           # 修之前最后一条是 1 对 0：深度停在入队那一刻


def test_a_single_dequeued_job_leaves_depth_zero(monkeypatch, tmp_path):
    """最小形态：只有一段，worker 取走它以后队列是空的，深度必须是 0。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    tr = Translator(p)
    p.translator = tr
    fake_audio(monkeypatch, frames=1)

    run(stream(p))
    assert tr.depths == [(0, 0)]


def test_depth_and_busy_are_clear_once_the_session_ends(monkeypatch, tmp_path):
    """整场收尾后没有任何字幕排队或在途：深度为 0、busy 为假——弹幕不该再让路。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.translator = Translator(p)
    fake_audio(monkeypatch, frames=3)

    run(stream(p))
    assert p.telemetry.translation_queue_depth == 0
    assert p._subtitle_busy == 0
    assert p._subtitle_translation_busy() is False


def test_cancelled_session_drains_depth_to_zero(monkeypatch, tmp_path):
    """停止/换房间时队列里的积压被丢掉，深度跟着归零——不留给下一场开头的弹幕。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    fake_audio(monkeypatch, frames=8, hang=True)

    class Stuck:
        name = "hymt2"

        def __init__(self):
            self.started = None

        async def translate(self, text, target, source="auto", glossary=None):
            self.started.set()
            await asyncio.sleep(3600)      # 第一条卡住，后面的在队里积压

    async def scenario():
        tr = Stuck()
        tr.started = asyncio.Event()      # 在 scenario 里创建，绑当前循环
        p.translator = tr
        task = asyncio.ensure_future(stream(p))
        await asyncio.wait_for(tr.started.wait(), 5)
        for _ in range(100):               # 等积压攒起来
            if p._trans_queue is not None and p._trans_queue.qsize() >= 2:
                break
            await asyncio.sleep(0.02)
        assert p.telemetry.translation_queue_depth >= 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert p.telemetry.translation_queue_depth == 0


def test_a_comment_batch_is_not_held_for_the_idle_wait_when_no_subtitle_is_pending(
        monkeypatch, tmp_path):
    """端到端：字幕都译完了，弹幕这一批不该再原地等 3 秒。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    tr = Translator(p)
    p.translator = tr
    fake_audio(monkeypatch, frames=3)

    async def scenario():
        await stream(p)
        assert p.telemetry.translation_queue_depth == 0
        t0 = time.monotonic()
        await p.comments.accept({"type": "viewer_comments", "items": [
            {"id": "c1", "user": "a", "text": "comentario uno"},
        ]})
        for _ in range(300):
            if tr.comment_at is not None:
                break
            await asyncio.sleep(0.01)
        assert tr.comment_at is not None
        return tr.comment_at - t0

    waited = run(scenario())
    # BATCH_WINDOW_SEC（0.3）以外不该再有等待；修之前这里是 0.3 + 3.0
    assert waited < p.comments.IDLE_WAIT_MAX_SEC * 0.5


def test_comments_still_yield_while_a_subtitle_is_really_in_flight(monkeypatch, tmp_path):
    """让路本身不能被修没了：字幕真在翻（_subtitle_busy 非零）时，弹幕仍然等。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.telemetry.translation_queue_depth = 0
    p._subtitle_busy = 1
    assert p._subtitle_translation_busy() is True
    p._subtitle_busy = 0
    p.telemetry.translation_queue_depth = 2          # 还有字幕在排队
    assert p._subtitle_translation_busy() is True
    p.telemetry.translation_queue_depth = 0
    assert p._subtitle_translation_busy() is False
