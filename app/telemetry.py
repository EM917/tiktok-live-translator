"""延迟与丢弃统计。

字幕延迟从 1.8s 变成 5s 时，光看一个 P95 无法定位是谁慢了——所以分段记：
  asr_ms        音频段结束 → 识别完成
  translate_ms  识别完成   → 翻译完成
  e2e_ms        音频段结束 → 字幕（原文）出现在界面上
配合两个队列深度和三个计数器，就能区分「VAD 等太久 / ASR 慢 / ASR 积压 /
翻译慢 / 翻译积压」这几种完全不同的情况。
"""
from collections import deque

WINDOW = 200      # 只保留最近 200 条样本，长时间运行内存恒定


def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = int(round((pct / 100.0) * (len(sorted_values) - 1)))
    return sorted_values[idx]


class Telemetry:
    def __init__(self, window=WINDOW):
        self.asr_ms = deque(maxlen=window)
        self.translate_ms = deque(maxlen=window)
        self.e2e_ms = deque(maxlen=window)
        # 片段时长：用户看到一句话的真实等待 ≈ 片段时长 + e2e。
        # 调切段参数（下一批）必须先有这个基线，否则改完不知道改善了多少。
        self.segment_ms = deque(maxlen=window)
        # 检测延迟（最坏情况）：违禁词若说在片段最开头，从说出口到报警要多久。
        # 这跟字幕延迟是两个数——同一套系统，词在片段头尾能差出整整一个片段，
        # 而合规上该被考核的是最坏值，不是平均值。
        self.detect_ms = deque(maxlen=window)
        self.audio_segments_total = 0
        self.audio_segments_dropped = 0
        self.translation_jobs_dropped = 0
        # 识别耗时超过片段时长的调用：Whisper 在音乐/噪声段上会复读跑飞，
        # 实测能让 4.5 秒的片段解码 39 秒——那正是队列溢出丢音频的前兆
        self.asr_overruns = 0
        self.asr_queue_depth = 0
        self.translation_queue_depth = 0

    def reset(self):
        """每场直播重新计数。统计条上的「丢音频 7」如果来自三小时前的另一个
        房间，运维就无法判断当前这场到底有没有丢词——而这正是本工具的 KPI。"""
        self.asr_ms.clear()
        self.translate_ms.clear()
        self.e2e_ms.clear()
        self.segment_ms.clear()
        self.detect_ms.clear()
        self.audio_segments_total = 0
        self.audio_segments_dropped = 0
        self.translation_jobs_dropped = 0
        self.asr_overruns = 0
        self.asr_queue_depth = 0
        self.translation_queue_depth = 0

    def record_asr(self, asr_ms, e2e_ms, segment_ms=None):
        self.audio_segments_total += 1
        self.asr_ms.append(asr_ms)
        self.e2e_ms.append(e2e_ms)
        if segment_ms:
            self.segment_ms.append(segment_ms)
            # 最坏情况 = 片段时长（词在最开头）+ 从片段结束到检测完成
            self.detect_ms.append(segment_ms + e2e_ms)

    def record_translation(self, translate_ms):
        self.translate_ms.append(translate_ms)

    def note_overrun(self):
        self.asr_overruns += 1

    def drop_audio(self):
        self.audio_segments_dropped += 1

    def drop_translation(self):
        self.translation_jobs_dropped += 1

    def snapshot(self):
        def stats(samples):
            values = sorted(samples)
            return {"p50": _percentile(values, 50), "p95": _percentile(values, 95),
                    "n": len(values)}

        return {
            "asr": stats(self.asr_ms),
            "translate": stats(self.translate_ms),
            "e2e": stats(self.e2e_ms),
            "segment": stats(self.segment_ms),
            "detect_worst": stats(self.detect_ms),
            "audio_segments_total": self.audio_segments_total,
            "audio_segments_dropped": self.audio_segments_dropped,
            "translation_jobs_dropped": self.translation_jobs_dropped,
            "asr_overruns": self.asr_overruns,
            "asr_queue_depth": self.asr_queue_depth,
            "translation_queue_depth": self.translation_queue_depth,
        }
