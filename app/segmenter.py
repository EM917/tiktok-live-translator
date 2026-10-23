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
        # 缓冲里的帧数、其中达到 silence_rms 的帧数：下面本来就在逐帧比 RMS，顺手数一下。
        # 识别风险分（app/asr_risk.py）要「这一段有声多久」和「是怎么切的」：9 秒硬切里有声很久
        # 却只识别出几个词，是 10-14 录音里套话顶替整段真话的那类段（8/8）。只记、不改切段
        self._frames = 0
        self._speech_frames = 0
        # 最近一次切出段时的情况 {"cut": "max"|"pause"|"flush", "frames", "speech_frames"}；
        # 还没切出过是 None。feed / flush 的返回值不变，调用方要用时自己来读
        self.last_cut = None

    def _cut_info(self, cut):
        return {"cut": cut, "frames": self._frames, "speech_frames": self._speech_frames}

    def feed(self, frame):
        """喂入一帧 PCM，返回 0 个或多个切好的段（bytes 列表）。"""
        arr = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
        self._buf += frame
        self._frames += 1
        if rms >= self.silence_rms:
            self._has_speech = True
            self._speech_frames += 1
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
                # 两个条件同一帧成立时记 max：段长已经到了上限，停顿只是碰巧
                self.last_cut = self._cut_info("max" if hit_max else "pause")
            self._reset()
        return out

    def flush(self):
        """流结束时把缓冲里剩下的语音吐出来。

        没有这一步，最后一段话会被直接丢弃：切段只在「攒够 9 秒」或「说完停顿
        0.7 秒」时触发，而直播断流/结束时这两个条件通常都没满足。对短音频来说
        更极端——可能一条字幕都出不来。
        """
        out = []
        duration = len(self._buf) / 2 / SAMPLE_RATE
        if self._has_speech and duration >= 0.5:   # 太短的残片没有识别价值
            out.append(bytes(self._buf))
            self.last_cut = self._cut_info("flush")
        self._reset()
        return out

    def buffered_sec(self):
        """缓冲里还没切出来的音频有几秒（不管有没有语音）。一轮出错收尾、没走到 flush 时，
        Pipeline 把它算进 stream_break 的 discarded_sec（专家审查 B4）。只读，不动缓冲。"""
        return len(self._buf) / 2 / SAMPLE_RATE

    def _reset(self):
        self._buf = bytearray()
        self._trailing_silence = 0.0
        self._has_speech = False
        self._frames = 0
        self._speech_frames = 0
