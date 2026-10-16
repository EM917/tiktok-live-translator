"""翻译跑到一半被取消时，那张字幕卡片要有个结局，不能永远停在「翻译中…」。

事故（2026-10-16 审阅）：停止 / 换主播 / 断流收尾超时会取消 translation_worker。被取消时它
手上那一条早已出队，收尾时的排空（_drop_job）看不见它；_translate_and_update_inner 又只接
Exception，CancelledError 直接穿过、不发布译文。结果 server.history 里那条一直是 pending，
卡片永远写着「翻译中…」——刷新页面、手机重连回放也一样。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app.pipeline import Pipeline, QUALITY_STRONG
from app.server import CaptionServer
from tests.helpers import run


def make_pipeline(monkeypatch, tmp_path, server=None):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", tmp_path / "banned_terms.txt")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source=None,
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto",
        model=None, device="auto", compute_type="auto", denoise="off",
    )
    server = server if server is not None else CaptionServer()
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


class Stuck:
    """翻译卡住：进来就报到，然后一直等。"""

    name = "hymt2"

    def __init__(self):
        self.started = asyncio.Event()
        self.calls = 0

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls += 1
        self.started.set()
        await asyncio.sleep(3600)


class Fast:
    """回的是纯中文：把原文夹在里面会被判成「回的不是译文」。"""

    name = "hymt2"

    async def translate(self, text, target, source="auto", glossary=None):
        return "大家好，今天有优惠"


def stream(p):
    loop = asyncio.get_running_loop()
    return p._stream_session("http://cdn/s.flv", Transcriber(), None, loop)


async def settle(times=5):
    """让 _spawn 出去的广播任务跑完。"""
    for _ in range(times):
        await asyncio.sleep(0)


def states(server):
    return {item["id"]: item.get("translate_state") for item in server.history
            if item.get("type") == "caption"}


def test_cancelling_during_a_translation_resolves_the_caption(monkeypatch, tmp_path):
    """停止落在翻译进行中：历史里不能留 pending，卡片拿到和「积压跳过」同一个结局。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    tr = Stuck()
    p.translator = tr
    fake_audio(monkeypatch, frames=1, hang=True)

    async def scenario():
        task = asyncio.ensure_future(stream(p))
        await asyncio.wait_for(tr.started.wait(), 5)
        assert states(server) == {1: "pending"}        # 这一条已出队、正在翻
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settle()

    run(scenario())
    assert states(server) == {1: "dropped"}
    item = next(i for i in server.history if i.get("type") == "caption")
    assert item["translated"] is None


def test_cancelling_also_resolves_the_queued_jobs_behind_it(monkeypatch, tmp_path):
    """在途的和排在后面的都要有结局（后者原本就有，这里钉住两者一起不留 pending）。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    tr = Stuck()
    p.translator = tr
    fake_audio(monkeypatch, frames=3, hang=True)

    async def scenario():
        task = asyncio.ensure_future(stream(p))
        await asyncio.wait_for(tr.started.wait(), 5)
        for _ in range(200):                           # 等 3 段都出了字幕
            if len(states(server)) == 3:
                break
            await asyncio.sleep(0.01)
        assert len(states(server)) == 3
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settle()

    run(scenario())
    assert set(states(server).values()) == {"dropped"}
    assert len(states(server)) == 3


def test_the_resolution_is_booked_to_the_session_that_ended(monkeypatch, tmp_path):
    """收尾是同步落在这一场里的：_stream_session 抛出 CancelledError 的那一刻计数已经记了，
    下一场 reset 之后不会再被晚到的任务加一。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    tr = Stuck()
    p.translator = tr
    fake_audio(monkeypatch, frames=1, hang=True)

    async def scenario():
        task = asyncio.ensure_future(stream(p))
        await asyncio.wait_for(tr.started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        booked = p.telemetry.translation_jobs_dropped    # 一个 await 都没让出去
        p.telemetry.reset()                              # 下一场开始
        await settle(10)
        return booked

    booked = run(scenario())
    assert booked == 1
    assert p.telemetry.translation_jobs_dropped == 0


def test_a_translation_that_is_being_published_is_not_overwritten(monkeypatch, tmp_path):
    """取消落在译文广播中途：译文已经记进历史、正在发给页面，这时再补一条「已跳过」
    会把刚上屏的译文盖掉。"""
    gate = {}

    class SlowPublishServer(CaptionServer):
        async def broadcast(self, msg):
            await super().broadcast(msg)                 # 先记进历史，和真实顺序一致
            if msg.get("type") == "caption_update" and msg.get("translate_state") == "ok":
                gate["publishing"].set()
                await asyncio.sleep(3600)                # 发给页面的过程卡在这里

    p, server = make_pipeline(monkeypatch, tmp_path, server=SlowPublishServer())
    p.translator = Fast()
    fake_audio(monkeypatch, frames=1, hang=True)

    async def scenario():
        gate["publishing"] = asyncio.Event()
        task = asyncio.ensure_future(stream(p))
        await asyncio.wait_for(gate["publishing"].wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settle()

    run(scenario())
    assert states(server) == {1: "ok"}                   # 没有被盖成 dropped
    item = next(i for i in server.history if i.get("type") == "caption")
    assert item["translated"] == "大家好，今天有优惠"


def test_a_translation_still_running_when_the_drain_times_out_is_resolved(monkeypatch, tmp_path):
    """流自然结束时只给在途翻译 TRANSLATION_DRAIN_SEC 秒；到点取消它，卡片也要有结局。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.TRANSLATION_DRAIN_SEC = 0.05
    tr = Stuck()
    p.translator = tr
    fake_audio(monkeypatch, frames=1)

    async def scenario():
        got_audio, _secs = await stream(p)
        await settle()
        return got_audio

    assert run(scenario()) is True
    assert tr.calls == 1
    assert states(server) == {1: "dropped"}


def test_finished_translations_are_left_alone(monkeypatch, tmp_path):
    """没有被打断的翻译照常是 ok，收尾不会误伤。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.translator = Fast()
    fake_audio(monkeypatch, frames=3)

    async def scenario():
        await stream(p)
        await settle()

    run(scenario())
    assert set(states(server).values()) == {"ok"}
    assert len(states(server)) == 3
    assert p.telemetry.translation_jobs_dropped == 0


def test_a_strong_retranslation_survives_the_cancel_of_the_fast_job(monkeypatch, tmp_path):
    """快译卡住时中控点了「重译」，强模型的译文已经上屏；这时停止/换房间取消了快译 worker，
    收掉在途那一条不能把已上屏的强译擦成「已跳过」。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    tr = Stuck()
    p.translator = tr
    fake_audio(monkeypatch, frames=1, hang=True)

    async def scenario():
        task = asyncio.ensure_future(stream(p))
        await asyncio.wait_for(tr.started.wait(), 5)
        assert states(server) == {1: "pending"}
        await p._publish_translation(1, "强模型译文", True, 10.0, QUALITY_STRONG, "zh-CN")
        assert states(server) == {1: "ok"}
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settle()

    run(scenario())
    assert states(server) == {1: "ok"}                   # 没有被盖成 dropped
    item = next(i for i in server.history if i.get("type") == "caption")
    assert item["translated"] == "强模型译文"
    assert p.telemetry.translation_jobs_dropped == 1     # 快译这一条确实没跑完，计数照记


def test_the_queued_drain_does_not_wipe_a_strong_retranslation_either(monkeypatch, tmp_path):
    """排在后面的任务被收掉时同理：卡片上已经有强模型的译文就留着，没有的才标「已跳过」。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    tr = Stuck()
    p.translator = tr
    fake_audio(monkeypatch, frames=3, hang=True)

    async def scenario():
        task = asyncio.ensure_future(stream(p))
        await asyncio.wait_for(tr.started.wait(), 5)
        for _ in range(200):                           # 等 3 段都出了字幕
            if len(states(server)) == 3:
                break
            await asyncio.sleep(0.01)
        assert len(states(server)) == 3
        await p._publish_translation(2, "第二句的强译", True, 10.0, QUALITY_STRONG, "zh-CN")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settle()

    run(scenario())
    assert states(server) == {1: "dropped", 2: "ok", 3: "dropped"}
    second = next(i for i in server.history if i.get("type") == "caption" and i["id"] == 2)
    assert second["translated"] == "第二句的强译"
