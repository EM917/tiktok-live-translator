"""识别证据音频（app/asr_evidence.py、app/pipeline_evidence.py，C2）。

钉住：默认开、只存四类段（被标记的、命中违禁词的——含警示关闭时被抑制的、桌面上点了重译的、
每 50 段 1 段对照）；环形缓冲找不回的只计数；写盘和清理在这一场自己的证据线程里（单线程、先交的
先做）、不挡事件循环，收尾放掉线程但不丢排着的写盘，进程退出（os._exit）前没写完的再等最多 2 秒、
事件循环关了之后才写完也不在终端刷 traceback；报警广播在前、证据在后，广播中途被停止也照存；
自己结束的场次收尾时被停止，session_end 仍由这一场自己写、记真正的原因；写失败只计数并打一行
中文警告；1024 MB / 6000 个文件 / 14 天的上限从最旧的整场删起、当前这场永远不删；磁盘剩不到 1 GB
就不存；证据只在 logs/ 底下（不在 web/，.gitignore 盖住）；「存储」那一行看得到、删得掉，界面文字
成对；中英两种界面下审计和终端逐字节相同（G8）。全程不联网、不起 ffmpeg、不加载模型。
"""
import ast
import asyncio
import concurrent.futures
import json
import os
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

import app.audio
from app import asr_evidence, diskspace, i18n, window_close
from app.asr_evidence import EvidenceRecorder, FrameRing, load_config, prune
from app.audio import FRAME_BYTES
from app.audit import AuditLog
from tests.helpers import run
from tests.test_asr_signals_pipeline import (ROOM, LargeV3, install_source, make_pipeline, result,
                                             rows)

ROOT = Path(__file__).resolve().parent.parent
LOUD = (np.ones(FRAME_BYTES // 2, dtype=np.int16) * 3000).tobytes()
PAUSE_META = {"cut": "pause", "frames": 30, "speech_frames": 30, "backend": "mlx",
              "model": "large-v3"}


def frame(i):
    """每帧的内容不同（帧号写进前 4 个字节），切出来的音频能核对是哪几帧。"""
    return int(i).to_bytes(4, "little") + LOUD[4:]


# ---- 设置 --------------------------------------------------------------------------------

def test_evidence_is_on_by_default_with_the_agreed_caps():
    assert load_config({}, {}) == {"on": True, "max_mb": 1024, "max_files": 6000, "days": 14,
                                   "ring_sec": 600, "control_every": 50}


@pytest.mark.parametrize("settings, env", [({"asr_evidence": False}, {}),
                                           ({}, {"TLT_ASR_EVIDENCE": "0"}),
                                           ({"asr_evidence": True}, {"TLT_ASR_SAFEGUARDS": "off"})])
def test_evidence_can_be_switched_off(settings, env):
    assert load_config(settings, env)["on"] is False


def test_bad_settings_are_ignored_with_one_warning(capsys):
    asr_evidence._said.clear()
    cfg = load_config({"asr_evidence": "yes", "asr_evidence_max_mb": -5,
                       "asr_evidence_days": True, "asr_evidence_control_every": 0}, {})
    cfg2 = load_config({"asr_evidence": "yes"}, {})
    assert cfg["on"] is True and cfg["max_mb"] == 1024 and cfg["days"] == 14
    assert cfg["control_every"] == 0 and cfg2["on"] is True
    out = capsys.readouterr().out
    assert out.count("asr_evidence 的值") == 1 and "asr_evidence_max_mb" in out
    assert all(line.startswith("[警告]") for line in out.splitlines())


def test_the_ring_length_has_an_upper_bound(capsys):
    """缓冲长度写成毫秒（600000）这种笔误不能让内存缓冲涨到十几 GB：超过 30 分钟提示一次、当没写。"""
    asr_evidence._said.clear()
    assert load_config({"asr_evidence_ring_sec": 600000}, {})["ring_sec"] == 600
    assert load_config({"asr_evidence_ring_sec": asr_evidence.RING_SEC_MAX}, {})["ring_sec"] == 1800
    out = capsys.readouterr().out
    assert out.strip() == ("[警告] settings.json 的 asr_evidence_ring_sec 的值 600000 不是 1 到 1800 "
                           "之间的整数，已忽略")
    # 最长的缓冲也装不下比段表更早的段：段表约 25 分钟，缓冲上限 30 分钟
    assert asr_evidence.RING_SEC_MAX * asr_evidence.BYTES_PER_SEC < 64 * 1024 * 1024


def test_constants_match_the_audio_source():
    assert asr_evidence.SAMPLE_RATE == app.audio.SAMPLE_RATE


# ---- 环形缓冲 ----------------------------------------------------------------------------

def test_the_ring_keeps_the_last_bytes_and_refuses_what_it_dropped():
    ring = FrameRing(10 * FRAME_BYTES)
    for i in range(25):
        ring.append(frame(i))
    assert ring.total == 25 * FRAME_BYTES and ring.start == 15 * FRAME_BYTES
    got = ring.slice(20 * FRAME_BYTES, 22 * FRAME_BYTES)
    assert got == frame(20) + frame(21)
    assert ring.slice(14 * FRAME_BYTES, 16 * FRAME_BYTES) is None      # 前半已经被挤掉
    assert ring.slice(24 * FRAME_BYTES, 30 * FRAME_BYTES) == frame(24)   # 越过末尾的截掉
    assert ring.slice(20 * FRAME_BYTES + 3, 20 * FRAME_BYTES + 7) == frame(20)[3:7]


# ---- 管线：存什么 --------------------------------------------------------------------------

def _session(monkeypatch, tmp_path, terms=(), settings=None):
    p, server = make_pipeline(monkeypatch, tmp_path, terms=terms)

    async def no_strong():                 # 重译不去探本机 Ollama，更不加载模型
        return None

    monkeypatch.setattr(p, "_strong_translator", no_strong)
    if settings:
        (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._new_session_state(p.audit)
    p._session_state = sess
    return p, server, sess


async def _feed(p, sess, n_frames, segments):
    """模拟拉流：先往缓冲里喂 n_frames 帧，再按 segments [(seq 结果, 结束帧, 帧数), …] 出段。"""
    recorder = p._evidence_round(sess)
    for i in range(n_frames):
        recorder.feed(frame(i))
    for res, end_frame, length in segments:
        meta = dict(PAUSE_META, round=recorder.round, pcm_end=end_frame * FRAME_BYTES,
                    pcm_len=length * FRAME_BYTES)
        await p._emit_original(res, audio_end_ts=1000.0 + end_frame / 10, asr_ms=900,
                               segment_ms=length * 100.0, asr_meta=meta, sess=sess)
    await recorder.drain()
    return recorder


def _index(recorder):
    path = recorder.dir / "index.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_flagged_segment_is_saved_with_its_context(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)

    async def go():
        return await _feed(p, sess, 200, [(result("hola a todos"), 150, 40),
                                          (result("cura todo", lp=-1.3), 190, 30)])

    recorder = run(go())
    index = _index(recorder)
    assert [r["seq"] for r in index] == [2] and index[0]["reasons"] == ["flagged"]
    rec = index[0]
    assert rec["wav"] == "seq-000002.wav" and rec["seg_ms"] == 3000
    assert rec["seg_offset_ms"] == rec["ctx_before_ms"] == 2000
    assert rec["ctx_after_ms"] == 1000
    assert rec["asr"]["flags"] == ["low_conf"] and rec["text"] == "cura todo"
    assert rec["audit"] == p.audit.path.name and rec["model"] == "large-v3"
    with wave.open(str(recorder.dir / rec["wav"])) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        pcm = w.readframes(w.getnframes())
    # 本段是第 160–189 帧，前 2 秒从第 140 帧起，后 1 秒到第 199 帧
    assert len(pcm) == 60 * FRAME_BYTES
    assert pcm[:FRAME_BYTES] == frame(140) and pcm[-FRAME_BYTES:] == frame(199)
    assert recorder.dir.parent == p.audit.path.parent / "asr-evidence"
    assert recorder.dir.name == p.audit.path.stem
    # 字幕消息不因证据多出任何东西
    assert all("asr_flag" not in c and "evidence" not in c for c in server.of_type("caption"))


def test_banned_term_hits_are_saved_even_when_alerts_are_off(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path, terms=["cura el cancer"])
    p.alerts_enabled = False

    async def go():
        return await _feed(p, sess, 100, [(result("este producto cura el cancer"), 90, 30)])

    recorder = run(go())
    assert server.of_type("alert") == []                       # 警示关着：不广播
    assert rows(p.audit.path, "alert")[0]["suppressed"] == "alerts_off"
    index = _index(recorder)
    assert index[0]["reasons"] == ["banned_term"] and index[0]["hits"] == ["cura el cancer"]


def test_one_unflagged_segment_in_fifty_is_kept_as_a_control(monkeypatch, tmp_path):
    p, _, sess = _session(monkeypatch, tmp_path)
    segments = [(result("frase numero {} de la tienda".format(i)), 20 + i * 3, 3)
                for i in range(1, 101)]

    async def go():
        return await _feed(p, sess, 330, segments)

    recorder = run(go())
    assert sorted((r["seq"], r["reasons"]) for r in _index(recorder)) == [(50, ["control"]),
                                                                          (100, ["control"])]


def test_controls_can_be_turned_off(monkeypatch, tmp_path):
    p, _, sess = _session(monkeypatch, tmp_path, settings={"asr_evidence_control_every": 0})
    segments = [(result("frase numero {} de la tienda".format(i)), 20 + i * 3, 3)
                for i in range(1, 51)]
    recorder = run(_feed(p, sess, 200, segments))
    assert _index(recorder) == []


def test_a_flagged_segment_on_a_control_seq_is_just_flagged(monkeypatch, tmp_path):
    p, _, sess = _session(monkeypatch, tmp_path, settings={"asr_evidence_control_every": 1})
    recorder = run(_feed(p, sess, 100, [(result("x y", lp=-1.0), 50, 20)]))
    assert _index(recorder)[0]["reasons"] == ["flagged"]


def test_a_manual_retranslate_saves_the_segment_from_the_ring(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)

    async def go():
        recorder = await _feed(p, sess, 200, [(result("ponen la oficina en orden"), 100, 40),
                                              (result("y luego otra cosa", lp=-1.2), 150, 40)])
        await p.retranslate(1, trigger="manual")       # 没有强模型：重译被拒，证据照存
        await p.retranslate(2, trigger="manual")       # 已经存过：只补一行索引，不再写 wav
        await p.retranslate(1, trigger="viewer")       # 手机点的不算
        await recorder.drain()
        return recorder

    recorder = run(go())
    # 证据线程先交的先写：索引行的先后就是交出去的先后
    lines = _index(recorder)
    assert [(r["seq"], r["reasons"][0]) for r in lines] == [(2, "flagged"), (1, "retranslate"),
                                                            (2, "retranslate")]
    index = {(r["seq"], r["reasons"][0]): r for r in lines}
    again, first = index[(2, "retranslate")], index[(2, "flagged")]
    # 补的那一行和第一行说的是同一份 wav 里的同一刀：离线复听按哪一行切都切对
    for key in ("wav", "seg_offset_ms", "seg_ms", "ctx_before_ms", "ctx_after_ms"):
        assert again[key] == first[key], key
    assert (first["seg_offset_ms"], first["ctx_after_ms"]) == (2000, 1000)
    assert sorted(f.name for f in recorder.dir.glob("*.wav")) == ["seq-000001.wav",
                                                                   "seq-000002.wav"]
    s = recorder.summary()
    assert s["saved"] == 2 and s["index_only"] == 1
    assert s["by_reason"] == {"flagged": 1, "retranslate": 2}


def test_a_retranslate_outside_the_ring_is_only_counted(monkeypatch, tmp_path):
    p, _, sess = _session(monkeypatch, tmp_path, settings={"asr_evidence_ring_sec": 5})

    async def go():
        recorder = await _feed(p, sess, 300, [(result("hace mucho rato"), 40, 30),
                                              (result("ahora mismo"), 290, 30)])
        await p.retranslate(1, trigger="manual")       # 第 10–39 帧早就被挤出 5 秒的缓冲
        await p.retranslate(77, trigger="manual")      # 这一场没有这一段
        await recorder.drain()
        return recorder

    recorder = run(go())
    assert _index(recorder) == []
    assert recorder.summary()["skipped_old"] == 2


def test_a_new_round_cannot_reach_the_previous_rounds_audio(monkeypatch, tmp_path):
    p, _, sess = _session(monkeypatch, tmp_path)

    async def go():
        recorder = await _feed(p, sess, 100, [(result("antes del corte"), 80, 30)])
        p._evidence_round(sess)                        # 断流重连：新的一轮、新的缓冲
        await p.retranslate(1, trigger="manual")
        await recorder.drain()
        return recorder

    recorder = run(go())
    assert recorder.round == 2 and recorder.summary()["skipped_old"] == 1


def test_a_retranslate_of_a_clip_already_on_disk_is_kept_after_a_reconnect_or_at_the_cap(
        monkeypatch, tmp_path):
    """wav 已经存过的段，补一行索引用不着缓冲里的音频：重连换了一轮、这一场到了上限都照补。
    手动重译是通顺错句唯一的人工信号，丢在有重连的场次上，「被标记的段被重译的比例」就偏了。"""
    p, _, sess = _session(monkeypatch, tmp_path)

    async def go():
        recorder = await _feed(p, sess, 200, [(result("antes del corte", lp=-1.2), 80, 30),
                                              (result("y otra mas", lp=-1.2), 150, 30)])
        p._evidence_round(sess)                        # 断流重连：新的一轮
        await p.retranslate(1, trigger="manual")
        recorder._full = True                          # 这一场到了自己的上限
        await p.retranslate(2, trigger="manual")
        await recorder.drain()
        return recorder

    recorder = run(go())
    index = {(r["seq"], r["reasons"][0]): r for r in _index(recorder)}
    assert sorted(index) == [(1, "flagged"), (1, "retranslate"), (2, "flagged"), (2, "retranslate")]
    for seq in (1, 2):
        assert index[(seq, "retranslate")]["wav"] == "seq-{:06d}.wav".format(seq)
        assert (index[(seq, "retranslate")]["seg_offset_ms"]
                == index[(seq, "flagged")]["seg_offset_ms"])
    s = recorder.summary()
    assert (s["skipped_old"], s["skipped_cap"], s["index_only"], s["saved"]) == (0, 0, 2, 2)


def test_the_loop_never_waits_for_a_prune_in_progress(monkeypatch, tmp_path):
    """清理在线程池里 os.walk、rmtree，可能要好几百毫秒。事件循环上的计数（跳过、找不到）和收尾的
    summary 不能排在它后面等——以前共用一把锁，实测把整个事件循环卡了 455 ms。"""
    p, _, sess = _session(monkeypatch, tmp_path)
    started, release = threading.Event(), threading.Event()
    real = asr_evidence.prune

    def slow_prune(*a, **k):
        started.set()
        release.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(asr_evidence, "prune", slow_prune)

    async def go():
        loop = asyncio.get_running_loop()
        recorder = p._evidence_round(sess)             # 开场清理：在线程池里卡住
        assert await loop.run_in_executor(None, started.wait, 5)
        t0 = time.monotonic()
        assert recorder.save(77, ["retranslate"], loop) is False      # 这一场没有这一段：只计数
        recorder.save(1, ["flagged"], loop)
        s = recorder.summary()
        spent = time.monotonic() - t0
        release.set()
        await recorder.drain()
        return spent, s

    spent, s = run(go())
    assert spent < 0.1, spent
    assert s["skipped_old"] == 2 and s["pending"] == 0      # 卡着的是清理，不是一份证据


def test_a_prune_in_progress_is_not_a_pending_clip_but_drain_still_waits_for_it(monkeypatch, tmp_path):
    """summary 的 pending 是还没写完的证据份数：开场清理卡着时不算进去，排在它后面的那份 wav 才算。
    以前清理也算一份，很快结束的一场碰上旧证据多、清理慢，session_end 就多报一份没写完。收尾的 drain
    照样等清理做完（session_end 的 pruned_dirs 要算上它）。"""
    p, _, sess = _session(monkeypatch, tmp_path)
    started, release = threading.Event(), threading.Event()
    real = asr_evidence.prune

    def slow_prune(*a, **k):
        started.set()
        release.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(asr_evidence, "prune", slow_prune)

    async def go():
        loop = asyncio.get_running_loop()
        recorder = p._evidence_round(sess)             # 开场清理：卡在证据线程里
        assert await loop.run_in_executor(None, started.wait, 5)
        only_prune = recorder.summary()["pending"]
        for i in range(200):
            recorder.feed(frame(i))
        await _flagged(p, sess, recorder, (100,))      # 一份 wav 排在清理后面
        with_clip = recorder.summary()["pending"]
        left = await recorder.drain(0.05)              # 清理还卡着：drain 等的是这两件
        release.set()
        await recorder.drain()
        return recorder, only_prune, with_clip, left

    recorder, only_prune, with_clip, left = run(go())
    assert (only_prune, with_clip, left) == (0, 1, 2)
    s = recorder.summary()
    assert (s["saved"], s["pending"]) == (1, 0)


def test_a_failed_wav_is_not_referenced_and_leaves_no_tmp(monkeypatch, tmp_path, capsys):
    """wav 写到一半失败：.tmp 删掉；在它失败之前就排上的那行重译索引不指向它（wav 记 null）；
    之后再点重译，从缓冲里重新存一份，而不是补一行指向不存在的文件。"""
    p, _, sess = _session(monkeypatch, tmp_path)
    entered, gate = threading.Event(), threading.Event()
    real = asr_evidence._write_wav

    def broken(path, pcm):
        asr_evidence._tmp_of(path).write_bytes(b"RIFF half")        # 写了一半
        entered.set()
        gate.wait(5)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(asr_evidence, "_write_wav", broken)

    async def go():
        loop = asyncio.get_running_loop()
        recorder = p._evidence_round(sess)
        await recorder.drain()
        for i in range(200):
            recorder.feed(frame(i))
        meta = dict(PAUSE_META, round=1, pcm_end=150 * FRAME_BYTES, pcm_len=30 * FRAME_BYTES)
        await p._emit_original(result("x", lp=-2.0), 100.0, 900, segment_ms=3000, asr_meta=meta,
                               sess=sess)
        # wav 还卡在证据线程里时点了重译：只排上一行索引，排在 wav 后面
        assert await loop.run_in_executor(None, entered.wait, 5)
        recorder.save(1, ["retranslate"], loop)
        gate.set()
        await recorder.drain()
        # 写失败那一刻就要删掉 .tmp：后面那次重新存会写同名的 .tmp 再改名，等到那时才看就看不出来
        left = sorted(f.name for f in recorder.dir.iterdir())
        monkeypatch.setattr(asr_evidence, "_write_wav", real)        # 磁盘又能写了
        recorder.save(1, ["retranslate"], loop)
        await recorder.drain()
        return recorder, left

    recorder, left = run(go())
    assert left == ["index.jsonl"]
    lines = _index(recorder)
    assert [(r["reasons"], r["wav"]) for r in lines] == [(["retranslate"], None),
                                                          (["retranslate"], "seq-000001.wav")]
    assert lines[0]["seg_offset_ms"] is None and lines[1]["seg_offset_ms"] == 2000
    assert [f.name for f in recorder.dir.iterdir() if f.suffix != ".jsonl"] == ["seq-000001.wav"]
    s = recorder.summary()
    assert (s["failed"], s["saved"], s["index_only"]) == (1, 1, 1)
    assert capsys.readouterr().out.count("[警告] 识别证据音频没存下来") == 1


def test_the_first_retranslate_after_a_failed_wav_saves_it_again(monkeypatch, tmp_path):
    """wav 写失败、失败已经落定之后才点重译：第一次点就从缓冲里重新存一份 wav。写失败那一刻就要把它从
    「已存」里拿掉——只靠补索引时查文件在不在兜底，这一下只会补一行 wav 为 null 的索引，要再点一次才
    存得下音频（审查的变异 M10b：删掉失败分支里那一句，别的测试全绿）。"""
    p, _, sess = _session(monkeypatch, tmp_path)
    real = asr_evidence._write_wav

    def broken(path, pcm):
        asr_evidence._tmp_of(path).write_bytes(b"RIFF half")
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(asr_evidence, "_write_wav", broken)

    async def go():
        recorder = await _feed(p, sess, 200, [(result("x", lp=-2.0), 150, 30)])   # 等到写失败
        failed = recorder.summary()["failed"]
        monkeypatch.setattr(asr_evidence, "_write_wav", real)                    # 磁盘又能写了
        await p.retranslate(1, trigger="manual")
        await recorder.drain()
        return recorder, failed

    recorder, failed = run(go())
    assert failed == 1
    assert [(r["reasons"], r["wav"]) for r in _index(recorder)] == [(["retranslate"],
                                                                      "seq-000001.wav")]
    assert sorted(f.name for f in recorder.dir.iterdir()) == ["index.jsonl", "seq-000001.wav"]
    s = recorder.summary()
    assert (s["failed"], s["saved"], s["index_only"]) == (1, 1, 0)


def test_switched_off_means_no_ring_and_no_files(monkeypatch, tmp_path):
    monkeypatch.setenv("TLT_ASR_EVIDENCE", "0")
    p, _, sess = _session(monkeypatch, tmp_path)

    async def go():
        assert p._evidence_round(sess) is None
        await p._emit_original(result("x", lp=-2.0), 100.0, 900, segment_ms=3000,
                               asr_meta=PAUSE_META, sess=sess)
        await p.retranslate(1, trigger="manual")

    run(go())
    assert not (tmp_path / "logs" / "asr-evidence").exists()
    assert p._evidence_summary(sess) == {"on": False}


# ---- 写盘：不挡循环、出错只计数 ----------------------------------------------------------------

def test_writes_happen_off_the_event_loop(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    gate = threading.Event()
    seen = {}
    real = asr_evidence._write_wav

    def slow_write(path, pcm):
        seen["thread"] = threading.current_thread()
        gate.wait(5)
        real(path, pcm)

    monkeypatch.setattr(asr_evidence, "_write_wav", slow_write)

    async def go():
        recorder = p._evidence_round(sess)
        for i in range(100):
            recorder.feed(frame(i))
        meta = dict(PAUSE_META, round=1, pcm_end=90 * FRAME_BYTES, pcm_len=30 * FRAME_BYTES)
        t0 = time.monotonic()
        await p._emit_original(result("x", lp=-2.0), 100.0, 900, segment_ms=3000, asr_meta=meta,
                               sess=sess)
        spent = time.monotonic() - t0
        await asyncio.sleep(0.05)
        assert not (recorder.dir / "seq-000001.wav").exists()      # 写盘还卡着，字幕早就出去了
        gate.set()
        await recorder.drain()
        return recorder, spent

    recorder, spent = run(go())
    assert spent < 1.0 and len(server.of_type("caption")) == 1
    assert seen["thread"] is not threading.main_thread()
    assert (recorder.dir / "seq-000001.wav").exists()


def test_a_failing_write_is_counted_and_warned_once(monkeypatch, tmp_path, capsys):
    p, server, sess = _session(monkeypatch, tmp_path, terms=["cura el cancer"])

    def broken(path, pcm):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(asr_evidence, "_write_wav", broken)
    capsys.readouterr()

    async def go():
        return await _feed(p, sess, 200, [(result("cura el cancer"), 60, 30),
                                          (result("otra", lp=-1.5), 120, 30),
                                          (result("y otra", lp=-1.5), 180, 30)])

    recorder = run(go())
    s = recorder.summary()
    assert s["failed"] == 3 and s["saved"] == 0
    out = capsys.readouterr().out
    assert out.count("[警告] 识别证据音频没存下来") == 1
    # 检测、报警、字幕照旧
    assert len(server.of_type("alert")) == 1 and len(server.of_type("caption")) == 3
    assert len(rows(p.audit.path, "segment")) == 3


def test_low_disk_space_skips_saving(monkeypatch, tmp_path):
    p, _, sess = _session(monkeypatch, tmp_path)

    class Usage:
        free = 500 * 1024 * 1024

    monkeypatch.setattr(asr_evidence.shutil, "disk_usage", lambda path: Usage())
    recorder = run(_feed(p, sess, 100, [(result("x", lp=-2.0), 90, 30)]))
    assert recorder.summary()["skipped_disk"] == 1
    # 先查空间再建目录：不存的时候不留一个空的场次目录
    assert not recorder.dir.exists()


def test_a_clip_skipped_for_low_disk_is_saved_by_a_later_retranslate(monkeypatch, tmp_path):
    """磁盘不够跳过的那份 wav 不算「已经存过」：空间回来以后点重译，从缓冲里把它存下来，而不是只补一行
    没有音频的索引。"""
    p, _, sess = _session(monkeypatch, tmp_path)

    class Usage:
        free = 500 * 1024 * 1024

    monkeypatch.setattr(asr_evidence.shutil, "disk_usage", lambda path: Usage())

    async def go():
        recorder = await _feed(p, sess, 200, [(result("x", lp=-2.0), 150, 30)])
        Usage.free = 50 * 1024 ** 3                    # 空间回来了
        await p.retranslate(1, trigger="manual")
        await recorder.drain()
        return recorder

    recorder = run(go())
    assert [(r["reasons"], r["wav"]) for r in _index(recorder)] == [(["retranslate"],
                                                                     "seq-000001.wav")]
    assert (recorder.dir / "seq-000001.wav").exists()
    s = recorder.summary()
    assert (s["skipped_disk"], s["saved"], s["index_only"]) == (1, 1, 0)


def test_a_clip_skipped_at_the_cap_is_not_turned_into_an_index_only_line(monkeypatch, tmp_path):
    """到了这一场上限跳过的 wav 同样不算「已经存过」：之后的重译按上限跳过、只计数，不补一行看着像
    「wav 存过」的索引。"""
    p, _, sess = _session(monkeypatch, tmp_path, settings={"asr_evidence_max_files": 2})

    async def go():
        recorder = await _feed(p, sess, 200, [(result("x1", lp=-2.0), 100, 30),
                                              (result("x2", lp=-2.0), 150, 30)])
        await p.retranslate(2, trigger="manual")
        await recorder.drain()
        return recorder

    recorder = run(go())
    assert [(r["seq"], r["reasons"]) for r in _index(recorder)] == [(1, ["flagged"])]
    s = recorder.summary()
    assert (s["saved"], s["skipped_cap"], s["index_only"]) == (1, 2, 0)


def _trace_evidence_io(monkeypatch):
    """记下证据的每件活在哪个线程上、按什么顺序做：(线程名, seq, 原因)，清理记成 (线程名, None, "prune")。"""
    seen = []
    real_write, real_prune = EvidenceRecorder._write, EvidenceRecorder._prune_locked_safe

    def write(self, record, pcm, entry):
        seen.append((threading.current_thread().name, record["seq"], record["reasons"][0]))
        return real_write(self, record, pcm, entry)

    def prune_(self):
        seen.append((threading.current_thread().name, None, "prune"))
        return real_prune(self)

    monkeypatch.setattr(EvidenceRecorder, "_write", write)
    monkeypatch.setattr(EvidenceRecorder, "_prune_locked_safe", prune_)
    return seen


async def _flagged(p, sess, recorder, seq_end_frames):
    for end in seq_end_frames:
        meta = dict(PAUSE_META, round=recorder.round, pcm_end=end * FRAME_BYTES,
                    pcm_len=30 * FRAME_BYTES)
        await p._emit_original(result("x{}".format(end), lp=-2.0), 100.0, 900, segment_ms=3000,
                               asr_meta=meta, sess=sess)


def test_evidence_io_runs_in_order_on_its_own_single_thread(monkeypatch, tmp_path):
    """写盘和清理都在这一场自己的证据线程里，按交出去的顺序做：不在默认线程池、不在识别的线程池。
    以前在默认线程池里靠一把锁排队，锁不保证先来先得：锁被开场清理占着时交的几件活，放开以后谁先
    拿到锁不一定，排在 wav 后面的重译索引会先写。"""
    p, _, sess = _session(monkeypatch, tmp_path)
    seen = _trace_evidence_io(monkeypatch)

    async def go():
        recorder = p._evidence_round(sess)             # 第一件活：开场清理
        recorder._io_lock.acquire()                    # 让它卡在清理上，后面交的全都排着
        try:
            for i in range(200):
                recorder.feed(frame(i))
            await _flagged(p, sess, recorder, (100, 150))
            await p.retranslate(2, trigger="manual")
            await p.retranslate(1, trigger="manual")
            await asyncio.sleep(0.05)
        finally:
            recorder._io_lock.release()
        await recorder.drain()
        return recorder

    recorder = run(go())
    assert [(seq, why) for _, seq, why in seen] == [(None, "prune"), (1, "flagged"), (2, "flagged"),
                                                    (2, "retranslate"), (1, "retranslate")]
    names = {name for name, _, _ in seen}
    assert len(names) == 1 and names.pop().startswith(asr_evidence.THREAD_NAME + "_")
    assert [(r["seq"], r["wav"]) for r in _index(recorder)] == [
        (1, "seq-000001.wav"), (2, "seq-000002.wav"), (2, "seq-000002.wav"), (1, "seq-000001.wav")]


@pytest.mark.parametrize("disk_ok", [True, False])
def test_a_retranslate_queued_behind_its_wav_sees_how_the_wav_ended(monkeypatch, tmp_path,
                                                                     disk_ok):
    """证据线程正忙时先交了 wav、又点了重译：那行索引一定在 wav 有了结局之后才写。wav 写成了就指向它
    （同一刀）；因为磁盘不够跳过了就记 null——不会指向一个不存在的文件，也不会把写成了的说成没有。"""
    p, _, sess = _session(monkeypatch, tmp_path)
    if not disk_ok:
        monkeypatch.setattr(asr_evidence, "_free_bytes", lambda path: 0)

    async def go():
        recorder = p._evidence_round(sess)
        recorder._io_lock.acquire()                    # 开场清理还没完
        try:
            for i in range(200):
                recorder.feed(frame(i))
            await _flagged(p, sess, recorder, (150,))
            await p.retranslate(1, trigger="manual")
            await asyncio.sleep(0.02)
        finally:
            recorder._io_lock.release()
        await recorder.drain()
        return recorder

    recorder = run(go())
    lines = _index(recorder)
    if disk_ok:
        assert [(r["reasons"][0], r["wav"]) for r in lines] == [("flagged", "seq-000001.wav"),
                                                                ("retranslate", "seq-000001.wav")]
        assert lines[0]["seg_offset_ms"] == lines[1]["seg_offset_ms"] == 2000
        assert (recorder.dir / "seq-000001.wav").exists()
    else:
        assert [(r["reasons"][0], r["wav"], r["seg_offset_ms"]) for r in lines] == [
            ("retranslate", None, None)]
        assert not (recorder.dir / "seq-000001.wav").exists()
        assert recorder.summary()["skipped_disk"] == 1


def test_a_clip_deleted_by_hand_is_not_referenced_and_can_be_saved_again(monkeypatch, tmp_path):
    """wav 存过、后来不在了（有人在访达里删了）：补的索引行不指向它，也忘掉它，再点重译从缓冲里重新存。"""
    p, _, sess = _session(monkeypatch, tmp_path)

    async def go():
        recorder = await _feed(p, sess, 200, [(result("x", lp=-2.0), 150, 30)])
        (recorder.dir / "seq-000001.wav").unlink()
        await p.retranslate(1, trigger="manual")
        await recorder.drain()
        await p.retranslate(1, trigger="manual")
        await recorder.drain()
        return recorder

    recorder = run(go())
    assert [(r["reasons"][0], r["wav"]) for r in _index(recorder)] == [
        ("flagged", "seq-000001.wav"), ("retranslate", None), ("retranslate", "seq-000001.wav")]
    assert (recorder.dir / "seq-000001.wav").exists()


def test_closing_keeps_queued_writes_and_lets_the_thread_go(monkeypatch, tmp_path):
    """收尾（close）不取消已经排上的写盘、不在事件循环上等；写完证据线程自己退出，之后不再收新活。"""
    p, _, sess = _session(monkeypatch, tmp_path)
    gate = threading.Event()
    seen = {}
    real = asr_evidence._write_wav

    def stuck(path, pcm):
        seen["thread"] = threading.current_thread()
        gate.wait(5)
        real(path, pcm)

    monkeypatch.setattr(asr_evidence, "_write_wav", stuck)

    async def go():
        loop = asyncio.get_running_loop()
        recorder = p._evidence_round(sess)
        await recorder.drain()                         # 开场清理先做完
        for i in range(200):
            recorder.feed(frame(i))
        await _flagged(p, sess, recorder, (100, 150))  # 一份卡在证据线程里写，一份排在它后面
        t0 = time.monotonic()
        summary = p._evidence_summary(sess)            # 收尾：取计数、close
        spent = time.monotonic() - t0
        late = recorder._submit(loop, time.sleep, 0)
        gate.set()
        await recorder.drain()
        return recorder, summary, spent, late

    recorder, summary, spent, late = run(go())
    assert spent < 0.1 and summary["pending"] == 2 and late is None
    assert sorted(f.name for f in recorder.dir.glob("*.wav")) == ["seq-000001.wav",
                                                                   "seq-000002.wav"]
    seen["thread"].join(2)
    assert not seen["thread"].is_alive()


# ---- 进程退出 -----------------------------------------------------------------------------------

def _stuck_when_the_loop_closes(monkeypatch, tmp_path):
    """一场收尾（drain 等不完、取计数、close）时还有一份 wav 卡在证据线程里，然后事件循环关掉
    （asyncio.run 返回）——浏览器模式按 Ctrl-C、走到 os._exit 之前就是这样。返回 (recorder, gate)，
    gate.set() 放那份写下去。_inflight 换成这条测试自己的：别的测试留下的活不算进来。"""
    monkeypatch.setattr(asr_evidence, "_inflight", set())
    p, _, sess = _session(monkeypatch, tmp_path)
    gate = threading.Event()
    real = asr_evidence._write_wav

    def stuck(path, pcm):
        gate.wait(5)
        real(path, pcm)

    monkeypatch.setattr(asr_evidence, "_write_wav", stuck)

    async def go():
        recorder = p._evidence_round(sess)
        await recorder.drain()                         # 开场清理先做完
        for i in range(200):
            recorder.feed(frame(i))
        await _flagged(p, sess, recorder, (150,))
        left = await recorder.drain(0.05)              # 收尾的 drain（Pipeline._evidence_drain）等不完
        return recorder, left, p._evidence_summary(sess)

    recorder, left, summary = run(go())
    assert left == 1 and summary["pending"] == 1
    assert not (recorder.dir / "seq-000001.wav").exists()
    return recorder, gate


def test_writes_still_queued_when_the_loop_closes_land_before_the_process_exits(monkeypatch,
                                                                                 tmp_path):
    """os._exit 不等任何线程，asyncio.run 收尾也只等默认线程池：收尾的 drain 没等完的那份，退出前由
    flush_evidence 再等一会儿（main.py 的 Ctrl-C 和后台异常退出、关窗口的 FinishOnce 都调它）。以前
    Ctrl-C 那条路上排着的 wav 和索引行跟着进程一起没了。"""
    recorder, gate = _stuck_when_the_loop_closes(monkeypatch, tmp_path)
    threading.Timer(0.2, gate.set).start()
    t0 = time.monotonic()
    assert window_close.flush_evidence() == 0
    assert time.monotonic() - t0 < asr_evidence.EXIT_FLUSH_SEC
    assert (recorder.dir / "seq-000001.wav").exists()
    assert [r["reasons"] for r in _index(recorder)] == [["flagged"]]
    # 从 _inflight 拿掉是做完之后的回调，在证据线程里、flush 返回之后才跑：等线程退出再看
    recorder._pool.shutdown(wait=True)
    assert asr_evidence._inflight == set()


def test_the_exit_flush_is_bounded_and_says_how_many_it_left(monkeypatch, tmp_path, capsys):
    """磁盘一直卡着也只多等 EXIT_FLUSH_SEC：关窗口本来就最多等 5 秒停止流程。没等到的在终端说一句（中文），
    不悄悄少；调用方给再长的时限也截到上限。"""
    recorder, gate = _stuck_when_the_loop_closes(monkeypatch, tmp_path)
    monkeypatch.setattr(asr_evidence, "EXIT_FLUSH_SEC", 0.2)
    capsys.readouterr()
    try:
        t0 = time.monotonic()
        left = asr_evidence.flush_pending(30)
        spent = time.monotonic() - t0
    finally:
        gate.set()
    assert left == 1 and 0.15 < spent < 1.5
    assert capsys.readouterr().out == "[警告] 退出时还有 1 份识别证据音频没写完，不再等\n"
    recorder._pool.shutdown(wait=True)                 # 不再等不等于取消：放行之后照样写完
    assert (recorder.dir / "seq-000001.wav").exists() and asr_evidence._inflight == set()


def test_a_write_that_finishes_after_the_loop_closed_prints_no_traceback(monkeypatch, tmp_path,
                                                                         caplog, capfd):
    """事件循环关了之后才写完：Python 3.9（CI 和最低支持版本）上 run_in_executor 交的活这时会往终端刷
    一段英文 traceback（exception calling callback … Event loop is closed），新一些的 asyncio 才会先看
    循环关没关。终端只许有中文行。"""
    recorder, gate = _stuck_when_the_loop_closes(monkeypatch, tmp_path)
    capfd.readouterr()
    caplog.clear()
    gate.set()
    assert asr_evidence.flush_pending(5) == 0
    # 做完之后的回调在证据线程里、flush 返回之后才跑：等线程退出，回调一定跑过了
    recorder._pool.shutdown(wait=True)
    assert (recorder.dir / "seq-000001.wav").exists()
    assert [r.getMessage() for r in caplog.records] == []
    out, err = capfd.readouterr()
    assert out == "" and err == ""


def test_handing_back_to_a_loop_that_closes_right_then_is_swallowed(monkeypatch):
    """查的时候循环还开着、交的那一下刚好关掉：call_soon_threadsafe 抛 RuntimeError，接住；已经关了就不交。"""
    monkeypatch.setattr(asr_evidence, "_inflight", set())
    calls = []

    class Closing:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, *a):
            calls.append(a)
            raise RuntimeError("Event loop is closed")

    job = concurrent.futures.Future()
    asr_evidence._inflight.add(job)
    asr_evidence._hand_back(Closing(), None, job)
    assert len(calls) == 1 and asr_evidence._inflight == set()
    closed = asyncio.new_event_loop()
    closed.close()
    asr_evidence._hand_back(closed, None, job)             # 关了：不交，也不抛


def test_closing_the_window_flushes_the_evidence_before_the_process_exits(monkeypatch):
    """关窗口（正常路径和浮窗的兜底都走 FinishOnce）：停止流程收尾之后、os._exit 之前等一会儿证据。"""
    calls = []
    monkeypatch.setattr(window_close, "flush_evidence", lambda: calls.append("flush"))
    once = window_close.FinishOnce(lambda: calls.append("finish"),
                                   exit_fn=lambda code: calls.append(("exit", code)))
    once.finish()
    assert calls == ["finish", "flush", ("exit", 0)]


def test_the_exit_flush_neither_imports_the_evidence_module_nor_blocks_the_exit(monkeypatch):
    """没加载过证据模块就没有要等的，也不为了退出去 import 它（会连带 numpy）；等的时候出任何事——
    包括又按了一次 Ctrl-C——都不拦住退出。"""
    class SecondCtrlC(BaseException):
        """替身 KeyboardInterrupt（也不是 Exception）：真抛 KeyboardInterrupt，漏出来会把整个 pytest 停掉。"""

    def interrupted(*_a, **_k):
        raise SecondCtrlC()

    monkeypatch.setattr(asr_evidence, "flush_pending", interrupted)
    assert window_close.flush_evidence() == 0
    # 两处都拿掉：from . import 会先拿包上的属性，只删 sys.modules 看不出它 import 了没有
    monkeypatch.delitem(sys.modules, "app.asr_evidence")
    monkeypatch.delattr(app, "asr_evidence")
    assert window_close.flush_evidence() == 0
    assert "app.asr_evidence" not in sys.modules and not hasattr(app, "asr_evidence")


def test_every_os_exit_in_main_flushes_the_evidence_first():
    """main.py 不能 import（会 execv），读语法树：每一处 os._exit 之前，同一段语句里先调了
    flush_evidence()。关窗口那条路的 os._exit 在 FinishOnce 里（上上一条测试）。"""
    def calls(stmt, name):
        return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                and name in (getattr(stmt.value.func, "attr", None),
                             getattr(stmt.value.func, "id", None)))

    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    exits = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if not isinstance(stmts, list):
                continue
            for i, stmt in enumerate(stmts):
                if calls(stmt, "_exit"):
                    exits.append(any(calls(prev, "flush_evidence") for prev in stmts[:i]))
    assert len(exits) >= 3 and all(exits), exits


# ---- 报警与证据 ---------------------------------------------------------------------------------

def test_the_alert_goes_out_before_the_clip_is_cut(monkeypatch, tmp_path):
    """报警广播在前、证据在后：切 PCM、建索引行不排在检测延迟的路上。"""
    p, server, sess = _session(monkeypatch, tmp_path, terms=["cura el cancer"])
    real = p._evidence_after_segment

    def traced(*a, **k):
        server.messages.append({"type": "evidence_cut"})
        return real(*a, **k)

    monkeypatch.setattr(p, "_evidence_after_segment", traced)
    recorder = run(_feed(p, sess, 100, [(result("este producto cura el cancer"), 90, 30)]))
    assert [m["type"] for m in server.messages
            if m["type"] in ("alert", "evidence_cut", "caption")] == ["alert", "evidence_cut",
                                                                      "caption"]
    assert _index(recorder)[0]["reasons"] == ["banned_term"]


def test_a_stop_during_a_slow_alert_broadcast_still_saves_the_clip(monkeypatch, tmp_path):
    """报警广播要等页面收（一个不读的页面最多 2 秒）。这时点了停止，取消落在广播里：命中违禁词的这一段
    照样存下来——以前证据排在广播之后、跟着取消一起丢了。"""
    p, server, sess = _session(monkeypatch, tmp_path, terms=["cura el cancer"])

    async def go():
        sending = asyncio.Event()
        real_broadcast = server.broadcast

        async def stuck_broadcast(msg):
            if msg.get("type") == "alert":
                sending.set()
                await asyncio.Event().wait()           # 页面不读
            await real_broadcast(msg)

        server.broadcast = stuck_broadcast
        recorder = p._evidence_round(sess)
        await recorder.drain()
        for i in range(200):
            recorder.feed(frame(i))
        meta = dict(PAUSE_META, round=recorder.round, pcm_end=150 * FRAME_BYTES,
                    pcm_len=30 * FRAME_BYTES)
        task = asyncio.ensure_future(p._emit_original(
            result("dice que cura el cancer"), 1000.0, 900, segment_ms=3000, asr_meta=meta,
            sess=sess))
        await asyncio.wait_for(sending.wait(), 5)
        task.cancel()                                  # _stop_locked 就是这么停的
        await asyncio.wait({task})
        await recorder.drain()
        return recorder, task

    recorder, task = run(go())
    assert task.cancelled()
    assert len(rows(p.audit.path, "alert")) == 1
    assert [(r["seq"], r["reasons"]) for r in _index(recorder)] == [(1, ["banned_term"])]
    assert (recorder.dir / "seq-000001.wav").exists()


# ---- 上限 -----------------------------------------------------------------------------------

def _old_session(base, name, n_files, size, age_days=0):
    d = base / name
    d.mkdir(parents=True)
    t = time.time() - age_days * 86400
    for i in range(n_files):
        f = d / "seq-{:06d}.wav".format(i)
        f.write_bytes(b"\0" * size)
        os.utime(str(f), (t, t))
    return d


def test_prune_deletes_the_oldest_sessions_and_never_the_current_one(tmp_path):
    base = tmp_path / "asr-evidence"
    a = _old_session(base, "session-20261001-100000", 3, 1000)
    b = _old_session(base, "session-20261002-100000", 3, 1000)
    cur = _old_session(base, "session-20261003-100000", 5, 1000)
    (base / "notes.txt").write_text("keep", encoding="utf-8")       # 不是一场的目录：不碰
    removed, freed, total_b, total_f = prune(base, cur.name, max_bytes=9000, max_files=100, days=14)
    assert removed == 1 and freed == 3000 and not a.exists() and b.exists() and cur.exists()
    assert (total_b, total_f) == (8000, 8)
    removed, *_ = prune(base, cur.name, max_bytes=1, max_files=100, days=14)
    assert removed == 1 and not b.exists() and cur.exists()           # 当前这场超了也不删
    assert (base / "notes.txt").exists()


def test_prune_by_file_count_and_by_age(tmp_path):
    base = tmp_path / "asr-evidence"
    old = _old_session(base, "session-20260901-100000", 1, 10, age_days=15)
    mid = _old_session(base, "session-20261001-100000", 4, 10)
    cur = _old_session(base, "session-20261003-100000", 2, 10)
    removed, *_ = prune(base, cur.name, max_bytes=10 ** 9, max_files=5, days=14)
    assert removed == 2 and not old.exists() and not mid.exists() and cur.exists()


def test_prune_does_not_follow_symlinks(tmp_path):
    base = tmp_path / "asr-evidence"
    base.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.wav").write_bytes(b"\0" * 100)
    try:
        (base / "session-20260101-000000").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器建不了符号链接")
    prune(base, "session-x", max_bytes=0, max_files=0, days=0)
    assert (outside / "keep.wav").exists()


def test_empty_session_dirs_expire_by_their_own_time_and_are_not_listed(tmp_path):
    """空目录没有文件可看：按目录自己的修改时间过期（以前记成 0、永远不清），「存储」里也不列一行「0 段」。"""
    logs = tmp_path / "logs"
    base = logs / "asr-evidence"
    old, fresh = base / "session-20260901-100000", base / "session-20261015-100000"
    old.mkdir(parents=True)
    fresh.mkdir()
    t = time.time() - 40 * 86400
    os.utime(str(old), (t, t))
    assert [it for it in diskspace.inventory(hf_dir=tmp_path / "none", log_dir=logs)
            if it["kind"] == "evidence"] == []
    removed, *_ = prune(base, "session-x", max_bytes=10 ** 9, max_files=100, days=14)
    assert removed == 1 and not old.exists() and fresh.exists()


def test_the_current_session_stops_saving_at_its_own_cap(monkeypatch, tmp_path):
    p, _, sess = _session(monkeypatch, tmp_path, settings={"asr_evidence_max_files": 2})
    segments = [(result("x{}".format(i), lp=-2.0), 20 + i * 20, 10) for i in range(5)]
    recorder = run(_feed(p, sess, 140, segments))
    s = recorder.summary()
    # 一个 wav + 索引文件 = 2 个文件，第二个 wav 就超了；之后的直接跳过
    assert s["saved"] == 1 and s["skipped_cap"] == 4
    assert len(list(recorder.dir.glob("*.wav"))) == 1


def test_a_new_session_prunes_old_ones_at_start(monkeypatch, tmp_path):
    base = tmp_path / "logs" / "asr-evidence"
    stale = _old_session(base, "session-20200101-000000", 2, 100, age_days=30)
    p, _, sess = _session(monkeypatch, tmp_path)

    async def go():
        recorder = p._evidence_round(sess)
        await recorder.drain()

    run(go())
    assert not stale.exists()


@pytest.mark.parametrize("settings, env", [({"asr_evidence": False}, {}),
                                           ({}, {"TLT_ASR_EVIDENCE": "0"}),
                                           ({}, {"TLT_ASR_SAFEGUARDS": "off"})])
def test_retention_still_runs_when_saving_is_off(monkeypatch, tmp_path, settings, env):
    """关掉证据只是不再存：已经存下的混音照样按 14 天 / 1024 MB 清（开场时），文档和「存储」那一行
    都是这么写的。为了隐私关掉证据的人会当真。"""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    base = tmp_path / "logs" / "asr-evidence"
    stale = _old_session(base, "session-20200101-000000", 2, 100, age_days=30)
    recent = _old_session(base, "session-20261015-100000", 2, 100, age_days=1)
    p, _, sess = _session(monkeypatch, tmp_path, settings=settings)

    async def go():
        assert p._evidence_round(sess) is None

    run(go())                     # asyncio.run 收尾时等默认线程池跑完
    assert not stale.exists() and recent.exists()
    assert not (base / p.audit.path.stem).exists()      # 关着就不建这一场的目录


# ---- 位置与隐私 ------------------------------------------------------------------------------

def test_evidence_lives_under_logs_and_is_ignored_by_git():
    from tests.test_gitignore_secrets import _git_ignores
    recorder = EvidenceRecorder(ROOT / "logs" / "session-20261016-200000.jsonl")
    rel = recorder.dir.relative_to(ROOT)
    assert rel.parts[:2] == ("logs", "asr-evidence")
    assert "web" not in rel.parts
    assert _git_ignores(str(rel / "seq-000001.wav"))
    assert _git_ignores(str(rel / "index.jsonl"))


def test_the_index_is_strict_json_without_nan(monkeypatch, tmp_path):
    p, _, sess = _session(monkeypatch, tmp_path)
    r = result("!x", lp=float("nan"))
    r.windows = [{"start": 0.0, "end": 2.0, "lp": float("nan"), "text": "!x"}]
    recorder = run(_feed(p, sess, 100, [(r, 90, 30)]))
    line = (recorder.dir / "index.jsonl").read_text(encoding="utf-8").splitlines()[0]
    rec = json.loads(line, parse_constant=lambda c: pytest.fail("non-finite " + c))
    assert rec["windows"][0]["lp"] is None and rec["asr"]["lp"] is None


# ---- 会话收尾 -------------------------------------------------------------------------------

def test_session_end_counts_the_evidence(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    install_source(monkeypatch, [LOUD] * 95)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._new_session_state(p.audit)

    async def go():
        loop = asyncio.get_running_loop()
        await p._stream_session("http://cdn/s.flv", LargeV3([result("hola"), result("si")]),
                                None, loop, sess=sess)
        await sess["evidence"].drain()

    run(go())
    end = p._asr_session_end(sess, {"reason": "offline"})
    ev = end["asr_risk"]["evidence"]
    assert ev["on"] is True and ev["saved"] == 1 and ev["by_reason"] == {"flagged": 1}
    assert ev["max_mb"] == 1024 and ev["control_every"] == 50
    assert sess["evidence"].ring is None                              # 环形缓冲已经放掉
    p.audit.close(**end)
    assert rows(p.audit.path, "session_end")[0]["asr_risk"]["evidence"]["saved"] == 1


def test_session_end_waits_briefly_for_writes_in_flight(monkeypatch, tmp_path):
    """停止前最后一秒标记的段：session_end 先等一会儿在途的写盘，计数和目录才对得上。走真的
    _run_stream_inner 收尾那一段（拉流用剧本代替）。"""
    from tests.test_pipeline_transcript import scripted_session
    from tests.test_resilience_stream import audit_rows
    from tests.test_resilience_stream import make_pipeline as stream_pipeline
    p, _ = stream_pipeline(monkeypatch, tmp_path)
    real = asr_evidence._write_wav

    def slow_write(path, pcm):
        time.sleep(0.3)
        real(path, pcm)

    monkeypatch.setattr(asr_evidence, "_write_wav", slow_write)

    async def body():
        sess = p._session_state
        recorder = p._evidence_round(sess)
        for i in range(100):
            recorder.feed(frame(i))
        meta = dict(PAUSE_META, round=recorder.round, pcm_end=90 * FRAME_BYTES,
                    pcm_len=30 * FRAME_BYTES)
        await p._emit_original(result("x", lp=-2.0), time.time(), 900, segment_ms=3000,
                               asr_meta=meta, sess=sess)

    scripted_session(monkeypatch, p, body)
    ends = [r for r in audit_rows(tmp_path) if r.get("type") == "session_end"]
    ev = ends[0]["asr_risk"]["evidence"]
    assert (ev["saved"], ev["pending"], ev["by_reason"]) == (1, 0, {"flagged": 1})


def test_a_drain_that_times_out_cancels_nothing_and_says_how_many_are_left(monkeypatch, tmp_path):
    p, _, sess = _session(monkeypatch, tmp_path)
    gate = threading.Event()
    real = asr_evidence._write_wav

    def stuck(path, pcm):
        gate.wait(5)
        real(path, pcm)

    monkeypatch.setattr(asr_evidence, "_write_wav", stuck)
    monkeypatch.setattr(p, "EVIDENCE_DRAIN_SEC", 0.05)

    async def go():
        recorder = p._evidence_round(sess)
        await recorder.drain()
        for i in range(100):
            recorder.feed(frame(i))
        meta = dict(PAUSE_META, round=1, pcm_end=90 * FRAME_BYTES, pcm_len=30 * FRAME_BYTES)
        await p._emit_original(result("x", lp=-2.0), 100.0, 900, segment_ms=3000, asr_meta=meta,
                               sess=sess)
        t0 = time.monotonic()
        await p._evidence_drain(sess)
        waited = time.monotonic() - t0
        end = p._asr_session_end(sess, {"reason": "user_stop"})
        gate.set()
        await recorder.drain()                         # 没被取消：照样写完
        return recorder, end, waited

    recorder, end, waited = run(go())
    assert waited < 1.0
    assert end["asr_risk"]["evidence"]["pending"] == 1
    assert end["asr_risk"]["evidence"]["saved"] == 0
    assert (recorder.dir / "seq-000001.wav").exists() and recorder.summary()["saved"] == 1


def _stuck_stream(monkeypatch, tmp_path, live):
    """走真的 _run_stream_inner 和 stop_stream：解析一次成功，「拉流」里出一段被标记的段，它的 wav 卡在
    证据线程里（gate 放开才写）。live=False：随后房间已结束（下播，这一场自己结束）；live=True：拉流一直
    挂着，等着被停止。返回 (pipeline, gate, state, 真的 asyncio.sleep)：state["emitted"] 在那一段出完后为真；
    测试用的管线把 asyncio.sleep 换成了 sleep(0)。"""
    from app import resolver
    from app.resolver import ResolveError
    from tests.test_resilience_stream import make_pipeline as stream_pipeline
    from tests.test_resilience_stream import scripted_resolve
    real_sleep = asyncio.sleep
    p, _ = stream_pipeline(monkeypatch, tmp_path)
    gate = threading.Event()
    state = {"emitted": False}
    real = asr_evidence._write_wav

    def stuck(path, pcm):
        gate.wait(10)
        real(path, pcm)

    monkeypatch.setattr(asr_evidence, "_write_wav", stuck)

    async def fake_session(media, *a, **k):
        # 第一轮出那一段；之后的轮次一帧都没有（V2 有声之后先用原地址连一轮，没声音再去查，查到「已结束」；
        # V1 第二次解析就收到）
        if state["emitted"]:
            return False, 0.0
        sess = p._session_state
        recorder = p._evidence_round(sess)
        for i in range(100):
            recorder.feed(frame(i))
        meta = dict(PAUSE_META, round=recorder.round, pcm_end=90 * FRAME_BYTES,
                    pcm_len=30 * FRAME_BYTES)
        await p._emit_original(result("x", lp=-2.0), time.time(), 900, segment_ms=3000,
                               asr_meta=meta, sess=sess)
        state["emitted"] = True
        if live:
            await asyncio.Event().wait()
        return True, 60.0

    async def probe(url):
        return 4, ""

    scripted_resolve(monkeypatch, ["http://cdn/a.flv", ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    return p, gate, state, real_sleep


def test_a_stop_while_a_finished_session_drains_keeps_its_own_session_end(monkeypatch, tmp_path):
    """这一场已经自己结束（下播），收尾正在等在途的证据写盘，中控这时点了停止（换主播、关窗口同理）：
    session_end 只有一条、由这一场自己写，原因还是 offline，识别风险和证据计数都在；停止不用等那份
    卡住的写盘，任务最后照样是「被取消」。以前取消从等待里冒出去，这一场的 _end_session 没跑，
    _stop_locked 替它写了一条 reason=user_stop、没有 asr_risk 的 session_end（CLAUDE.md 第七条）。"""
    from tests.test_resilience_stream import audit_rows
    p, gate, _, _ = _stuck_stream(monkeypatch, tmp_path, live=False)
    real_drain = p._evidence_drain

    async def go():
        draining = asyncio.Event()

        async def traced(sess):
            draining.set()
            return await real_drain(sess)

        monkeypatch.setattr(p, "_evidence_drain", traced)
        task = p._stream_task = asyncio.ensure_future(p._run_stream_inner(ROOM))
        await asyncio.wait_for(draining.wait(), 5)
        assert not task.done()
        t0 = time.monotonic()
        await p.stop_stream()
        spent = time.monotonic() - t0
        recorder = p._session_state["evidence"]
        gate.set()
        await recorder.drain()
        return task, spent, recorder

    task, spent, recorder = run(go())
    ends = [r for r in audit_rows(tmp_path) if r.get("type") == "session_end"]
    assert len(ends) == 1 and ends[0]["reason"] == "offline"
    ev = ends[0]["asr_risk"]["evidence"]
    assert (ev["saved"], ev["pending"]) == (0, 1)
    assert task.cancelled()
    assert spent < 1.0, spent
    assert (recorder.dir / "seq-000001.wav").exists()          # 没被取消：照样写完


def test_a_stuck_evidence_write_cannot_hold_the_stop_past_its_grace(monkeypatch, tmp_path):
    """直播中点停止，偏偏有一份证据写盘卡住了：收尾最多等 EVIDENCE_DRAIN_SEC 就写 session_end，在
    _stop_locked 的 STOP_GRACE_SEC 之内。等得比它长，_stop_locked 就撒手替这一场写 session_end，识别
    风险和证据的计数整个没了。用的是真的两个常数（大约要等 1 秒）。"""
    from tests.test_resilience_stream import audit_rows
    p, gate, state, real_sleep = _stuck_stream(monkeypatch, tmp_path, live=True)
    assert p.EVIDENCE_DRAIN_SEC < p.STOP_GRACE_SEC

    async def go():
        task = p._stream_task = asyncio.ensure_future(p._run_stream_inner(ROOM))
        for _ in range(500):
            if state["emitted"]:
                break
            await real_sleep(0.01)
        assert state["emitted"] and not task.done()
        t0 = time.monotonic()
        await p.stop_stream()
        spent = time.monotonic() - t0
        recorder = p._session_state["evidence"]
        gate.set()
        await recorder.drain()
        return task, spent

    task, spent = run(go())
    ends = [r for r in audit_rows(tmp_path) if r.get("type") == "session_end"]
    assert len(ends) == 1 and ends[0]["reason"] == "user_stop"
    assert ends[0]["asr_risk"]["evidence"]["pending"] == 1
    assert task.cancelled()
    assert spent < p.STOP_GRACE_SEC, spent


# ---- 「存储」那一行 ---------------------------------------------------------------------------

def _evidence_tree(tmp_path):
    logs = tmp_path / "logs"
    base = logs / "asr-evidence"
    _old_session(base, "session-20261014-100000", 3, 1000)
    _old_session(base, "session-20261015-100000", 2, 1000)
    current = logs / "session-20261016-100000.jsonl"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text("{}\n", encoding="utf-8")
    _old_session(base, current.stem, 4, 1000)
    return logs, current


def test_the_storage_row_lists_the_clips_except_this_session(tmp_path):
    logs, current = _evidence_tree(tmp_path)
    items = diskspace.inventory(hf_dir=tmp_path / "none", log_dir=logs, current_log=current)
    ev = [it for it in items if it["kind"] == "evidence"]
    assert len(ev) == 1 and ev[0]["id"] == "evidence:all" and ev[0]["role"] == "app"
    assert ev[0]["size"] == 5000
    assert ev[0]["label"] == "识别音频片段（5 段，不含本场）"
    assert "只存在这台电脑上" in ev[0]["note"] and "1024 MB" in ev[0]["note"]
    assert "14 天" in ev[0]["note"]


def test_the_storage_row_reads_in_english(tmp_path):
    logs, current = _evidence_tree(tmp_path)
    with i18n.use(i18n.EN):
        items = diskspace.inventory(hf_dir=tmp_path / "none", log_dir=logs, current_log=current)
        ev = [it for it in items if it["kind"] == "evidence"][0]
        label, note = i18n.render(ev["label"], i18n.EN), i18n.render(ev["note"], i18n.EN)
    assert label == "Recognition audio clips (5 clips, not including this session)"
    assert note.startswith("Mixed stream audio from flagged segments") and "1024 MB" in note
    assert not i18n.CJK.search(label + note)
    one = tmp_path / "one" / "logs"
    _old_session(one / "asr-evidence", "session-20261014-100000", 1, 10)
    single = [it for it in diskspace.inventory(hf_dir=tmp_path / "none", log_dir=one)
              if it["kind"] == "evidence"][0]
    assert i18n.render(single["label"], i18n.EN) == (
        "Recognition audio clips (1 clip, not including this session)")


def test_deleting_the_clips_keeps_this_session_and_everything_else(tmp_path):
    logs, current = _evidence_tree(tmp_path)
    base = logs / "asr-evidence"
    freed, done, failed = run(diskspace.delete(["evidence:all"], hf_dir=tmp_path / "none",
                                               log_dir=logs, current_log=current))
    assert done == ["evidence:all"] and failed == [] and freed == 5000
    assert [d.name for d in asr_evidence.session_dirs(base)] == [current.stem]
    assert current.exists()                                           # 审计日志不碰


def test_the_list_hint_promises_a_redownload_only_for_models(tmp_path):
    """识别音频片段和审计日志也标「本程序」，删了就没了：列表下面那句「删了会自动重下」只能说模型，
    否则和这一行自己的说明（删了不影响程序）打架。"""
    import re
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<span id="disk-hint"[^>]*data-en="([^"]*)">([^<]*)</span>', html)
    en, zh = m.group(1), m.group(2)
    assert "标「本程序」的模型删了会在下次需要时自动重下" in zh
    assert "Models marked This app download again when needed." in en
    assert "Items marked This app download again" not in en
    logs, current = _evidence_tree(tmp_path)
    notes = {}
    for lang in (i18n.ZH, i18n.EN):
        with i18n.use(lang):
            items = diskspace.inventory(hf_dir=tmp_path / "none", log_dir=logs, current_log=current)
            notes[lang] = [i18n.render(it["note"], lang) for it in items
                           if it["kind"] == "evidence"][0]
        assert not re.search(r"重下|重新下载|download", notes[lang]), notes[lang]
    assert notes[i18n.ZH].endswith("删了不影响程序")
    assert notes[i18n.EN].endswith("Deleting them doesn’t affect the app.")


def test_no_clips_means_no_row(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    assert [it for it in diskspace.inventory(hf_dir=tmp_path / "none", log_dir=logs)
            if it["kind"] == "evidence"] == []


# ---- G8 -------------------------------------------------------------------------------------

# ms 是报警翻译记录的耗时（alert_translation，四舍五入到 0.1 毫秒）：线程池里写证据的线程抢 GIL 时
# 会在 0.0 和 0.1 之间跳，和界面语言无关
TIME_KEYS = ("at", "ts", "started_at", "ended_at", "e2e_ms", "asr_ms", "ms")


def _g8(lang, monkeypatch, tmp_path, capsys, fail):
    d = tmp_path / lang
    d.mkdir()
    i18n._bad_once.clear()
    capsys.readouterr()
    if fail:
        def broken(path, pcm):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(asr_evidence, "_write_wav", broken)
    with i18n.use(lang):
        p, _, sess = _session(monkeypatch, d, terms=["cura el cancer"])
        recorder = run(_feed(p, sess, 200, [(result("cura el cancer"), 60, 30),
                                            (result("hola a todos los presentes"), 120, 30),
                                            (result("otra", lp=-1.5), 180, 30)]))
        end = p._asr_session_end(sess, {"reason": "user_stop"})
        p.audit.close(**end)
    out = capsys.readouterr().out
    audit = [json.dumps({k: v for k, v in json.loads(ln).items() if k not in TIME_KEYS},
                        ensure_ascii=False)
             for ln in p.audit.path.read_text(encoding="utf-8").splitlines()]
    index = sorted((json.dumps({k: v for k, v in r.items() if k not in ("at", "audit")},
                               ensure_ascii=False) for r in _index(recorder)))
    return audit, out, index


@pytest.mark.parametrize("fail", [False, True])
def test_g8_evidence_audit_index_and_terminal_do_not_depend_on_the_ui_language(
        monkeypatch, tmp_path, capsys, fail):
    asr_evidence._said.clear()
    zh = _g8(i18n.ZH, monkeypatch, tmp_path, capsys, fail)
    asr_evidence._said.clear()
    en = _g8(i18n.EN, monkeypatch, tmp_path, capsys, fail)
    assert zh == en
    assert ('"evidence": {' in "\n".join(zh[0])) is True
    if fail:
        assert "[警告] 识别证据音频没存下来" in zh[1]
