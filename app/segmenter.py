"""把连续 PCM 帧切成适合送给 whisper 的语音段。

策略：能量 VAD（RMS 阈值）+ 时长约束——
  * 缓冲达到 max_seg_sec 无条件切段（直播常带背景音乐，纯靠静音切不动）；
  * 缓冲超过 min_seg_sec 且结尾出现足够长的安静间隙时提前切段；
  * 整段都没检测到语音时直接丢弃，不浪费识别算力。
"""
import numpy as np

from .audio import FRAME_SEC, SAMPLE_RATE


class SilenceSegmenter:
    def __init__(self, silence_rms=300.0, min_seg_sec=2.5, max_seg_sec=9.0,
                 trailing_silence_sec=0.7):
        self.silence_rms = silence_rms
        self.min_seg_sec = min_seg_sec
        self.max_seg_sec = max_seg_sec
        self.trailing_silence_sec = trailing_silence_sec
        self._buf = bytearray()
        self._trailing_silence = 0.0
        self._has_speech = False

    def feed(self, frame):
        """喂入一帧 PCM，返回 0 个或多个切好的段（bytes 列表）。"""
        arr = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
        self._buf += frame
        if rms >= self.silence_rms:
            self._has_speech = True
            self._trailing_silence = 0.0
        else:
            self._trailing_silence += FRAME_SEC

        duration = len(self._buf) / 2 / SAMPLE_RATE
        out = []
        hit_max = duration >= self.max_seg_sec
        hit_pause = (self._has_speech and duration >= self.min_seg_sec
                     and self._trailing_silence >= self.trailing_silence_sec)
        if hit_max or hit_pause:
            if self._has_speech:
                out.append(bytes(self._buf))
            self._reset()
        return out

    def _reset(self):
        self._buf = bytearray()
        self._trailing_silence = 0.0
        self._has_speech = False
