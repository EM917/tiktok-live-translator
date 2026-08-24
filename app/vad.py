"""语音活动检测（VAD）：把纯音乐/噪声段挡在识别之前。

为什么需要：切段器用的是能量阈值（RMS），而背景音乐的能量一点不比人声低，
所以纯 BGM 段照样被当成「有人说话」送进 Whisper。后果有三个：
  1. Whisper 在没有人声的音频上会吐训练数据里的套话（"¡Suscríbete al canal!"），
     实测一场直播里 14% 的字幕是这种幻觉；
  2. 白白消耗算力——音乐段还特别慢（模型反复重试）；
  3. 幻觉文本会污染违禁词检测的上下文窗口。

这里用 faster-whisper 自带的 Silero VAD（已是依赖，不引入新包）。它只回答
「这段音频里有没有人声」——**不改变切段边界，也不碰有人说话的段**，
所以不会影响翻译质量：被丢掉的本来就没有话。
"""
import numpy as np

_UNAVAILABLE = False
SAMPLE_RATE = 16000


def speech_ratio(pcm, threshold=0.5):
    """返回这段音频里被判定为人声的比例（0.0–1.0）。

    VAD 不可用时返回 1.0（视为全是人声，即放行）——它是优化项，
    拿不到不能让整条链路失灵。
    """
    global _UNAVAILABLE
    if _UNAVAILABLE:
        return 1.0
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if audio.size < SAMPLE_RATE // 2:      # 太短，不值得判
        return 1.0
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        opts = VadOptions(threshold=threshold, min_speech_duration_ms=200,
                          min_silence_duration_ms=300)
        spans = get_speech_timestamps(audio, opts, sampling_rate=SAMPLE_RATE)
        voiced = sum(sp["end"] - sp["start"] for sp in spans)
        return voiced / audio.size if audio.size else 1.0
    except Exception as exc:
        _UNAVAILABLE = True
        print("[信息] VAD 不可用（{}），纯音乐段将不再被拦截".format(exc))
        return 1.0


def has_speech(pcm, min_ratio=0.06, threshold=0.5):
    """这段音频里是否有值得识别的人声。

    min_ratio 取得很低（6%）是刻意的：本工具漏一句话的代价远高于多跑一次识别，
    所以只拦「几乎完全没有人声」的段——9 秒音频里只要有半秒说话就放行。
    """
    return speech_ratio(pcm, threshold=threshold) >= min_ratio
