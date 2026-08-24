"""音频积压策略：晚报警 >> 永不报警。

旧策略是「队列满 3 段就丢最旧的」，有两个问题：
  1. 段数上限对不同切段配置含义不同（9 秒片段 = 27 秒缓冲，4 秒 = 12 秒），
     调切段时会连带偷偷改掉抗 stall 能力；
  2. 识别一慢就永久丢音频 = 永久漏词，与本工具的头号 KPI 直接冲突。
现在改成按音频秒数预算，硬上限前一律留着。
"""
from app.pipeline import (
    AUDIO_BACKLOG_DEGRADED_SEC,
    AUDIO_BACKLOG_HARD_SEC,
    AUDIO_BACKLOG_WARN_SEC,
    Pipeline,
)
from app.telemetry import Telemetry


def test_thresholds_are_ordered():
    assert AUDIO_BACKLOG_WARN_SEC < AUDIO_BACKLOG_DEGRADED_SEC < AUDIO_BACKLOG_HARD_SEC


def test_hard_limit_is_generous():
    """60 秒 16kHz 单声道 PCM 不到 2MB——内存不该是丢词的理由。"""
    bytes_needed = AUDIO_BACKLOG_HARD_SEC * 16000 * 2
    assert bytes_needed < 4 * 1024 * 1024
    assert AUDIO_BACKLOG_HARD_SEC >= 30      # 至少扛得住一次 20-30 秒的识别跑飞


def test_health_levels():
    assert Pipeline._health_level(0.0) == "ok"
    assert Pipeline._health_level(AUDIO_BACKLOG_WARN_SEC - 0.1) == "ok"
    assert Pipeline._health_level(AUDIO_BACKLOG_WARN_SEC) == "lagging"
    assert Pipeline._health_level(AUDIO_BACKLOG_DEGRADED_SEC) == "degraded"
    assert Pipeline._health_level(120.0) == "degraded"


def test_backlog_tracked_with_peak():
    t = Telemetry()
    t.set_backlog(12.0)
    t.set_backlog(31.5)
    t.set_backlog(2.0)                        # 追上来了
    snap = t.snapshot()
    assert snap["audio_backlog_sec"] == 2.0   # 当前值
    assert snap["max_backlog_sec"] == 31.5    # 峰值仍然可见（soak 用）


def test_backlog_reset_between_sessions():
    t = Telemetry()
    t.set_backlog(40.0)
    t.reset()
    assert t.snapshot()["max_backlog_sec"] == 0.0
