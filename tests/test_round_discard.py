"""停止、换主播、更新暂停时丢掉的已到达音频要记下秒数；收尾超时自愈不丢切段器余量（专家审查 B4）。

以前（补核实验 exp_stop_discards_backlog / exp_heal_drops_buffer）：到达 24 秒后停止，审计里只有 2 段，
积压的 15 秒没了，session_end 也没提；自愈只取消 reader，切段器里还没切出来的那一截（最多 9 秒）不再
送去识别。段数能从 segments_cut 减 segment 和 asr_failed 算出来，缺的是秒数和切段器里那一截。现在：

  * 取消时：识别队列里没处理的段、取消那一刻正在识别线程里跑的那一段（审查 Q11）、切段器里攒着的那一截，
    写进 stream_break 的 discarded_segments / discarded_sec，每轮只算一次；session_end 汇总本场；
  * 自愈（只取消 reader）：切段器余量照样切出来送去识别，discarded 为 0，段数比以前多一段；
  * 没丢的时候 stream_break / session_end 和以前逐字节相同（新键只在有丢时出现）。

全部用桩件：不联网、不起 ffmpeg、不加载模型。主播名是虚构的。
"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import app.audio
import app.segmenter
from app import pipeline as pipeline_mod
from app.audio import FRAME_BYTES, FRAME_SEC, SAMPLE_RATE
from app.audit import AuditLog
from app.pipeline import Pipeline
from app.segmenter import SilenceSegmenter
from app.server import CaptionServer
from tests.helpers import run

ROOM = "https://www.tiktok.com/@ficticia_corte/live"
LOUD = (np.full(int(SAMPLE_RATE * FRAME_SEC), 3000, dtype=np.int16)).tobytes()   # 远高于门限 300
assert len(LOUD) == FRAME_BYTES


def make_pipeline(monkeypatch, tmp_path):
    from app import settings
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False)
    p = Pipeline(args, CaptionServer(port=0))
    audit = p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(audit)
    # 和 _run_stream_inner 一样挂上这一场的汇总：谁来关这份审计，session_end 都带着它
    audit.end_fields = lambda: p._session_end_extras(audit, sess)
    return p, sess


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def of_type(records, kind):
    return [r for r in records if r.get("type") == kind]


def live_source(n_frames, fed, stop_after=False):
    """先吐 n_frames 帧响亮的音频（实时到达的直播音频），然后挂着（直播还在）。stop_after：吐完就进入
    收尾（stopping）再挂着——收尾卡住，等自愈来取消 reader。"""
    class Source:
        def __init__(self, media, denoise_model=None):
            self.stopping = False
            self.on_stage = None

        async def frames(self):
            for _ in range(n_frames):
                fed["frames"] += 1
                yield LOUD
                await asyncio.sleep(0)
            if stop_after:
                self.stopping = True
            await asyncio.sleep(3600)

        def stderr_tail(self):
            return ""

        def force_close(self):
            return False

        async def stop(self, urgent=False):
            pass

    return Source


class BlockedASR:
    """第一段卡在识别线程里（等 release），之后的段都很快。按顺序记下识别过的段长（秒）。"""
    backend = "fake"

    def __init__(self, block_first=True):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.block_first = block_first
        self.seconds = []

    def transcribe(self, pcm):
        self.entered.set()
        if self.block_first and not self.seconds:
            self.release.wait(10)
        self.seconds.append(len(pcm) / 2.0 / SAMPLE_RATE)
        return SimpleNamespace(text="hola", raw_text="hola", language="es", rejected=[])


def test_the_segmenter_reports_what_it_has_not_cut_yet():
    seg = SilenceSegmenter()
    assert seg.buffered_sec() == 0.0
    for _ in range(12):
        assert seg.feed(LOUD) == []
    assert seg.buffered_sec() == pytest.approx(1.2)
    assert seg.flush() and seg.buffered_sec() == 0.0
    for _ in range(95):                                 # 9 秒硬切一刀，剩 0.5 秒
        seg.feed(LOUD)
    assert seg.buffered_sec() == pytest.approx(0.5)


def test_a_stop_after_24_seconds_records_the_backlog_the_buffer_and_the_segment_in_asr(
        monkeypatch, tmp_path):
    """到达 24 秒后停止：9 秒硬切两刀，第一段卡在识别线程里，第二段在队列里，切段器里还有 6 秒。三样都
    算进 discarded（共 3 段、24 秒），一段都没进审计的 segment；session_end 汇总本场。"""
    p, sess = make_pipeline(monkeypatch, tmp_path)
    fed = {"frames": 0}
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", live_source(240, fed))
    asr = BlockedASR()

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            p._stream_session("http://cdn/s.flv", asr, None, loop, sess=sess))
        while fed["frames"] < 240 or not asr.entered.is_set():
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        run(scenario())
    finally:
        asr.release.set()
    path = p.audit.path
    p.audit.close(reason="user_stop")
    records = rows(path)
    assert of_type(records, "segment") == []
    brk = of_type(records, "stream_break")
    assert len(brk) == 1 and brk[0]["reason"] == "cancelled"
    assert brk[0]["discarded_segments"] == 3
    assert brk[0]["discarded_sec"] == pytest.approx(24.0)
    audio = [r for r in of_type(records, "stream_audio") if r["final"]][0]
    assert audio["segments_cut"] == 3                    # 切段器那一截也切成了一段：段数对得上
    end = of_type(records, "session_end")[0]
    assert end["reason"] == "user_stop"
    assert end["discarded_segments"] == 3 and end["discarded_sec"] == pytest.approx(24.0)
    assert sess["discarded"] == {"segments": 3, "sec": pytest.approx(24.0)}


def test_the_segment_in_the_asr_thread_counts_even_when_the_queue_is_empty(monkeypatch, tmp_path):
    """审查 Q11：队列空着、切段器也空，只有识别线程里那一段——它的结果没人要了，照样算一段。"""
    p, sess = make_pipeline(monkeypatch, tmp_path)
    fed = {"frames": 0}
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", live_source(90, fed))  # 正好 9 秒一刀
    asr = BlockedASR()

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            p._stream_session("http://cdn/s.flv", asr, None, loop, sess=sess))
        while not asr.entered.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        run(scenario())
    finally:
        asr.release.set()
    brk = of_type(rows(p.audit.path), "stream_break")[0]
    assert (brk["discarded_segments"], brk["discarded_sec"]) == (1, 9.0)


def test_the_heal_path_sends_the_buffer_to_asr_instead_of_dropping_it(monkeypatch, tmp_path):
    """收尾超时自愈（_heal_stopping）只取消 reader：切段器里那 1.2 秒照样识别，段数多一段，discarded 为 0。
    （exp_heal_drops_buffer 的反面：以前 8 段切出、8 段识别，1.2 秒没了。）"""
    p, sess = make_pipeline(monkeypatch, tmp_path)
    fed = {"frames": 0}
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", live_source(192, fed, stop_after=True))
    asr = BlockedASR(block_first=False)

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            p._stream_session("http://cdn/s.flv", asr, None, loop, sess=sess))
        while fed["frames"] < 192 or len(asr.seconds) < 2:
            await asyncio.sleep(0.005)
        rnd = sess["round"]
        heal = p._heal_stopping(sess, {"break_no": rnd["no"] - 1})
        got = await asyncio.wait_for(task, 5)
        return heal, got

    heal, got = run(scenario())
    assert heal["reader_cancelled"] is True
    assert got[0] is True                               # 这一轮照常结束，重连循环接着走
    assert asr.seconds == [9.0, 9.0, pytest.approx(1.2)]
    records = rows(p.audit.path)
    assert len(of_type(records, "segment")) == 3
    brk = of_type(records, "stream_break")[0]
    assert "discarded_segments" not in brk and "discarded_sec" not in brk
    assert sess["discarded"] is None


def test_an_error_mid_stream_counts_the_unflushed_buffer(monkeypatch, tmp_path):
    """reader 出错（不是取消）时走不到 flush：切段器里那一截按秒数算进 discarded（不成段）。"""
    p, sess = make_pipeline(monkeypatch, tmp_path)

    class Broken:
        def __init__(self, media, denoise_model=None):
            self.stopping = False

        async def frames(self):
            for _ in range(15):
                yield LOUD
            raise RuntimeError("管道坏了")

        def stderr_tail(self):
            return ""

        async def stop(self, urgent=False):
            pass

    monkeypatch.setattr(app.audio, "FFmpegAudioSource", Broken)

    async def scenario():
        loop = asyncio.get_running_loop()
        with pytest.raises(RuntimeError):
            await p._stream_session("http://cdn/s.flv", BlockedASR(block_first=False), None,
                                    loop, sess=sess)

    run(scenario())
    brk = of_type(rows(p.audit.path), "stream_break")[0]
    assert brk["reason"] == "error"
    assert brk["discarded_segments"] == 0 and brk["discarded_sec"] == pytest.approx(1.5)


def test_a_natural_end_discards_nothing_and_adds_no_keys(monkeypatch, tmp_path):
    """正常断流：识别把队列做完、切段器 flush 过，stream_break 不多键，sess 里不记。"""
    p, sess = make_pipeline(monkeypatch, tmp_path)

    class Eof:
        def __init__(self, media, denoise_model=None):
            self.stopping = False

        async def frames(self):
            for _ in range(100):
                yield LOUD

        def stderr_tail(self):
            return ""

        async def stop(self, urgent=False):
            pass

    monkeypatch.setattr(app.audio, "FFmpegAudioSource", Eof)

    async def scenario():
        loop = asyncio.get_running_loop()
        return await p._stream_session("http://cdn/s.flv", BlockedASR(block_first=False), None,
                                       loop, sess=sess)

    assert run(scenario())[0] is True
    path = p.audit.path
    p.audit.close(reason="offline")
    records = rows(path)
    assert len(of_type(records, "segment")) == 2         # 9 秒一刀 + flush 出来的 1 秒
    brk = of_type(records, "stream_break")[0]
    assert "discarded_segments" not in brk and "discarded_sec" not in brk
    assert "discarded_sec" not in of_type(records, "session_end")[0]
    assert sess["discarded"] is None


# ---- 字节对比：没丢的时候和以前逐字节相同 ---------------------------------------------------------

class FrozenDatetime:
    """把审计记录里的时刻钉死，好逐字节比。"""
    @staticmethod
    def now():
        from datetime import datetime
        return datetime(2026, 10, 23, 12, 0, 0, 123000)


def _break_line(monkeypatch, tmp_path, name, discarded):
    import app.audit as audit_mod
    monkeypatch.setattr(audit_mod, "datetime", FrozenDatetime)
    p, sess = make_pipeline(monkeypatch, tmp_path / name)
    source = SimpleNamespace(proc=SimpleNamespace(returncode=0), stalled=False,
                             stderr_tail=lambda: "")
    sess["round"] = {"no": 1, "source": source, "reader": None, "t0": time.monotonic()}
    kwargs = {} if discarded == "omitted" else {"discarded": discarded}
    p._record_stream_break(sess, source, "eof", None, True, 12.0, None, **kwargs)
    lines = p.audit.path.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if '"type": "stream_break"' in line][0], sess


def test_a_stream_break_without_discards_is_byte_identical(monkeypatch, tmp_path):
    old, _ = _break_line(monkeypatch, tmp_path, "a", "omitted")       # 以前的调用方式
    none, _ = _break_line(monkeypatch, tmp_path, "b", None)
    zero, sess = _break_line(monkeypatch, tmp_path, "c", (0, 0.0))
    assert old == none == zero
    assert sess["discarded"] is None


def test_a_stream_break_with_discards_only_appends_the_two_keys(monkeypatch, tmp_path):
    old, _ = _break_line(monkeypatch, tmp_path, "a", "omitted")
    new, sess = _break_line(monkeypatch, tmp_path, "b", (2, 13.04))
    assert new.startswith(old[:-1] + ", ")
    assert new.endswith('"discarded_segments": 2, "discarded_sec": 13.0}')
    assert sess["discarded"] == {"segments": 2, "sec": pytest.approx(13.04)}


def test_session_end_sums_the_rounds_and_stays_unchanged_without_discards(monkeypatch, tmp_path):
    import app.audit as audit_mod
    monkeypatch.setattr(audit_mod, "datetime", FrozenDatetime)
    lines = {}
    for name, per_round in (("clean", [None, None]), ("lossy", [(1, 9.0), (2, 4.25)])):
        p, sess = make_pipeline(monkeypatch, tmp_path / name)
        audit = p.audit
        source = SimpleNamespace(proc=SimpleNamespace(returncode=0), stalled=False,
                                 stderr_tail=lambda: "")
        for discarded in per_round:
            sess["round"] = {"no": sess["break_no"] + 1, "source": source, "reader": None,
                             "t0": time.monotonic()}
            p._record_stream_break(sess, source, "cancelled", None, True, 5.0, None,
                                   discarded=discarded)
        path = audit.path
        audit.close(reason="user_stop")
        lines[name] = [line for line in path.read_text(encoding="utf-8").splitlines()
                       if '"type": "session_end"' in line][0]
    clean, lossy = json.loads(lines["clean"]), json.loads(lines["lossy"])
    assert "discarded_sec" not in clean and "discarded_segments" not in clean
    assert lossy["discarded_segments"] == 3 and lossy["discarded_sec"] == 13.2
    assert lines["lossy"].startswith(lines["clean"][:-1] + ", ")
