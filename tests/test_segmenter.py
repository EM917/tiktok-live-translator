"""segmenter：能量 VAD 切段状态机。flush 的「别丢最后一段」正是 v0.7.1 修的
bug（531b916），用测试钉住防止回归。"""
import numpy as np

from app.audio import FRAME_BYTES
from app.segmenter import SilenceSegmenter

SAMPLES_PER_FRAME = FRAME_BYTES // 2   # 0.1 秒 @ 16 kHz mono s16le


def loud_frame(amplitude=3000):
    return (np.ones(SAMPLES_PER_FRAME, dtype=np.int16) * amplitude).tobytes()


def silent_frame():
    return np.zeros(SAMPLES_PER_FRAME, dtype=np.int16).tobytes()


def test_max_duration_forces_cut():
    seg = SilenceSegmenter()
    out = []
    for _ in range(95):                      # 9.5 秒连续语音
        out += seg.feed(loud_frame())
    assert len(out) == 1                     # 攒满 9 秒强切一次
    assert len(out[0]) == 90 * FRAME_BYTES


def test_trailing_pause_cuts_early():
    seg = SilenceSegmenter()
    out = []
    for _ in range(26):                      # 2.6 秒语音（过了 2.5 秒下限）
        out += seg.feed(loud_frame())
    for _ in range(7):                       # 0.7 秒停顿
        out += seg.feed(silent_frame())
    assert len(out) == 1                     # 停顿达标即提前切段
    assert len(out[0]) == 33 * FRAME_BYTES   # 段里包含语音+停顿全部缓冲


def test_pure_silence_never_emits():
    seg = SilenceSegmenter()
    out = []
    for _ in range(200):                     # 20 秒纯静音
        out += seg.feed(silent_frame())
    assert out == []                         # 无语音的段直接丢弃，不浪费识别
    assert seg.flush() == []


def test_flush_keeps_final_speech():
    seg = SilenceSegmenter()
    for _ in range(10):                      # 1 秒语音后流断开
        assert seg.feed(loud_frame()) == []
    out = seg.flush()
    assert len(out) == 1                     # 最后一段话必须吐出来
    assert len(out[0]) == 10 * FRAME_BYTES


def test_flush_drops_tiny_residue():
    seg = SilenceSegmenter()
    for _ in range(4):                       # 0.4 秒残片，低于 0.5 秒门槛
        seg.feed(loud_frame())
    assert seg.flush() == []


def test_flush_resets_state():
    seg = SilenceSegmenter()
    for _ in range(10):
        seg.feed(loud_frame())
    seg.flush()
    assert seg.flush() == []                 # 再 flush 不重复吐段


# ---- last_cut：这一刀是怎么切的、有声多久（给识别风险分，见 app/asr_risk.py） ----------------

def test_last_cut_records_a_hard_cut_and_its_voiced_frames():
    seg = SilenceSegmenter()
    assert seg.last_cut is None              # 还没切出过
    out = []
    for i in range(90):                      # 9 秒：每 3 帧里 1 帧安静
        out += seg.feed(silent_frame() if i % 3 == 2 else loud_frame())
    assert len(out) == 1 and len(out[0]) == 90 * FRAME_BYTES   # 返回值和以前一样
    assert seg.last_cut == {"cut": "max", "frames": 90, "speech_frames": 60}


def test_last_cut_records_a_pause_cut():
    seg = SilenceSegmenter()
    out = []
    for _ in range(26):
        out += seg.feed(loud_frame())
    for _ in range(7):
        out += seg.feed(silent_frame())
    assert len(out) == 1
    assert seg.last_cut == {"cut": "pause", "frames": 33, "speech_frames": 26}


def test_last_cut_records_a_flush_and_counts_restart_after_each_cut():
    seg = SilenceSegmenter()
    for _ in range(95):                      # 9 秒硬切一次，剩 0.5 秒
        seg.feed(loud_frame())
    assert seg.last_cut["cut"] == "max"
    out = seg.flush()
    assert len(out) == 1 and len(out[0]) == 5 * FRAME_BYTES
    assert seg.last_cut == {"cut": "flush", "frames": 5, "speech_frames": 5}


def test_a_discarded_silent_buffer_does_not_touch_last_cut():
    seg = SilenceSegmenter()
    for _ in range(26):
        seg.feed(loud_frame())
    for _ in range(7):
        seg.feed(silent_frame())
    before = dict(seg.last_cut)
    for _ in range(120):                     # 12 秒静音：整段丢掉，不算切出一段
        assert seg.feed(silent_frame()) == []
    assert seg.flush() == []
    assert seg.last_cut == before
