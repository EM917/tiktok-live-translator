"""识别证据音频（app/asr_evidence.py、app/pipeline_evidence.py，C2）。

钉住：默认开、只存四类段（被标记的、命中违禁词的——含警示关闭时被抑制的、桌面上点了重译的、
每 50 段 1 段对照）；环形缓冲找不回的只计数；写盘在线程池里、不挡事件循环；写失败只计数并打一行
中文警告；300 MB / 2000 个文件 / 14 天的上限从最旧的整场删起、当前这场永远不删；磁盘剩不到 1 GB
就不存；证据只在 logs/ 底下（不在 web/，.gitignore 盖住）；「存储」那一行看得到、删得掉，界面文字
成对；中英两种界面下审计和终端逐字节相同（G8）。全程不联网、不起 ffmpeg、不加载模型。
"""
import asyncio
import json
import os
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

import app.audio
from app import asr_evidence, diskspace, i18n
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
    assert load_config({}, {}) == {"on": True, "max_mb": 300, "max_files": 2000, "days": 14,
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
    assert cfg["on"] is True and cfg["max_mb"] == 300 and cfg["days"] == 14
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
    # 写盘在默认线程池里，几行索引的先后不保证：按 (seq, 原因) 对
    index = {(r["seq"], r["reasons"][0]): r for r in _index(recorder)}
    assert sorted(index) == [(1, "retranslate"), (2, "flagged"), (2, "retranslate")]
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
    assert s["skipped_old"] == 2 and s["pending"] == 1


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
        # wav 还卡在写盘里（拿着写盘锁）时点了重译：只排上一行索引，排在 wav 后面
        assert await loop.run_in_executor(None, entered.wait, 5)
        recorder.save(1, ["retranslate"], loop)
        gate.set()
        await recorder.drain()
        monkeypatch.setattr(asr_evidence, "_write_wav", real)        # 磁盘又能写了
        recorder.save(1, ["retranslate"], loop)
        await recorder.drain()
        return recorder

    recorder = run(go())
    lines = _index(recorder)
    assert [(r["reasons"], r["wav"]) for r in lines] == [(["retranslate"], None),
                                                          (["retranslate"], "seq-000001.wav")]
    assert lines[0]["seg_offset_ms"] is None and lines[1]["seg_offset_ms"] == 2000
    assert [f.name for f in recorder.dir.iterdir() if f.suffix != ".jsonl"] == ["seq-000001.wav"]
    s = recorder.summary()
    assert (s["failed"], s["saved"], s["index_only"]) == (1, 1, 1)
    assert capsys.readouterr().out.count("[警告] 识别证据音频没存下来") == 1


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
    """关掉证据只是不再存：已经存下的混音照样按 14 天 / 300 MB 清（开场时），文档和「存储」那一行
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
    assert ev["max_mb"] == 300 and ev["control_every"] == 50
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
    assert "只存在这台电脑上" in ev[0]["note"] and "300 MB" in ev[0]["note"]
    assert "14 天" in ev[0]["note"]


def test_the_storage_row_reads_in_english(tmp_path):
    logs, current = _evidence_tree(tmp_path)
    with i18n.use(i18n.EN):
        items = diskspace.inventory(hf_dir=tmp_path / "none", log_dir=logs, current_log=current)
        ev = [it for it in items if it["kind"] == "evidence"][0]
        label, note = i18n.render(ev["label"], i18n.EN), i18n.render(ev["note"], i18n.EN)
    assert label == "Recognition audio clips (5 clips, not including this session)"
    assert note.startswith("Mixed stream audio from flagged segments") and "300 MB" in note
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
