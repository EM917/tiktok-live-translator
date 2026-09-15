"""用 ffmpeg 从直播流中抽取音频，输出 16 kHz 单声道 16-bit PCM 帧。"""
import asyncio
import time
from collections import deque

import numpy as np

from .ffmpeg_bin import filter_path, find_ffmpeg

SAMPLE_RATE = 16000
FRAME_SEC = 0.1
FRAME_BYTES = int(SAMPLE_RATE * FRAME_SEC) * 2  # 0.1 秒 @ 16 kHz mono s16le


class FFmpegAudioSource:
    # 连续多少秒一个字节都没收到就判定为「静默停滞」。换 Wi-Fi、NAT 超时、运营商
    # 掐断时常见的不是 EOF 而是半开连接：ffmpeg 的 -reconnect 只在读到错误/EOF
    # 时触发，内核 TCP 重传超时要十几分钟——这期间 read 永不返回、界面还显示
    # 「直播中」、主播说的每个违禁词都没被听到。对合规监听器这是最贵的静默。
    STALL_SEC = 20.0
    # 同样的守护给 ffmpeg 自己：http 读超时（微秒），到点它会走 -reconnect
    RW_TIMEOUT_US = "15000000"

    def __init__(self, media_url, denoise_model=None):
        self.media_url = media_url
        self.denoise_model = denoise_model  # RNNoise 模型路径；None = 不降噪
        self.proc = None
        self.stalled = False                # 本次是因静默停滞被我们主动断开的
        self._stderr_tail = deque(maxlen=8)
        self._stderr_task = None

    async def frames(self):
        """异步生成固定长度（0.1 秒）的 PCM 帧，流结束后返回。"""
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("缺少音频组件 ffmpeg——请关闭程序后重新打开，会自动补装")
        cmd = [ffmpeg, "-nostdin", "-loglevel", "error"]
        if self.media_url.startswith("http"):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
                    "-rw_timeout", self.RW_TIMEOUT_US]
        cmd += ["-i", self.media_url, "-vn", "-ac", "1"]
        if self.denoise_model:
            # 高通滤掉低频轰鸣，再用 RNNoise 神经降噪抑制背景音乐/噪声、保留人声
            # （arnndn 内部以 48 kHz 工作，ffmpeg 会自动插入重采样）
            cmd += ["-af", "highpass=f=70,arnndn=m={}".format(filter_path(self.denoise_model))]
        cmd += ["-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        buf = bytearray()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(self.proc.stdout.read(FRAME_BYTES),
                                                   timeout=self.STALL_SEC)
                except asyncio.TimeoutError:
                    self.stalled = True
                    print("[警告] 直播流 {:.0f} 秒没有任何数据（连接可能已半开），"
                          "主动断开交给自动重连".format(self.STALL_SEC))
                    break
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= FRAME_BYTES:
                    yield bytes(buf[:FRAME_BYTES])
                    del buf[:FRAME_BYTES]
        finally:
            await self.stop()

    async def _drain_stderr(self):
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    self._stderr_tail.append(text)
        except Exception:
            pass

    def stderr_tail(self):
        return " | ".join(self._stderr_tail)

    async def stop(self):
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()


def frame_rms(frame):
    """一帧 s16le PCM 的 RMS，和 SilenceSegmenter 的识别门限同一个量纲。"""
    usable = len(frame) // 2 * 2
    if not usable:
        return 0.0
    arr = np.frombuffer(frame[:usable], dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(arr * arr)))


class AudioFlowMeter:
    """「这场直播实际收到了多少音频」的计数器：一场一个，每轮拉流开头调 start_round()。

    stall 看门狗只管「20 秒一个字节都没有」。网络劣化时 ffmpeg 可能 30 秒只吐 2 秒音频，
    静音或音量太低的流也一直有字节——这些时候界面照样显示「直播中」，却没有几段话
    进了检测。这里只计数、不做决定，feed() 返回的事件由调用方写审计或上界面：

      heartbeat      每 WINDOW_SEC（单调时钟）一个窗口：收到的音频秒数、经过秒数、超过
                     识别门限的帧数、切出的段数。每轮开头 WARMUP_SEC 不计——CDN 开播先
                     灌一段缓存，比例会虚高。单调时钟在电脑休眠时不走，睡着的时间不会被
                     算成「没收到音频」（那由 pipeline 的 clock_gap 负责）。
      low            连续 LOW_WINDOWS 个窗口收到的不到 LOW_RATIO；处于 low 期间每个窗口
                     再报一次（数字跟着更新）。
      recovered      某个窗口回到 RECOVER_RATIO 以上（和 LOW_RATIO 之间留回差，免得来回闪）。
      summary        每 SUMMARY_SEC 一次本轮累计，崩溃了也留得下记录。
      quiet / speech 累计收到 QUIET_AUDIO_SEC 秒音频却一段都没切出来（音量一直低于门限）
                     时报 quiet；之后切出第一段时报 speech。这个计数跨轮累计。
    """
    WARMUP_SEC = 15.0
    WINDOW_SEC = 60.0
    LOW_RATIO = 0.7
    RECOVER_RATIO = 0.8
    LOW_WINDOWS = 2
    SUMMARY_SEC = 300.0
    QUIET_AUDIO_SEC = 120.0

    def __init__(self, speech_rms=300.0, clock=time.monotonic):
        self.speech_rms = speech_rms
        self._clock = clock
        self.low = False
        self.quiet = False
        self.since_cut = 0.0
        self.start_round()

    def start_round(self):
        self.audio_sec = 0.0
        self.segments = 0
        self.speech_frames = 0
        self.peak_rms = 0.0
        self.low = False
        self._t0 = None
        self._summary_at = None
        self._win = None                  # [开始时刻, 音频秒, 门限以上帧, 段数]；热身期为 None
        self._low_windows = 0

    def summary(self):
        return {"audio_sec": round(self.audio_sec, 1), "segments_cut": self.segments,
                "peak_rms": round(self.peak_rms, 1)}

    def cut(self, segments):
        """记下新切出的语音段。feed() 已经包含这一步；流结束时 flush 出来的段走这里。"""
        if segments <= 0:
            return []
        self.segments += segments
        self.since_cut = 0.0
        if self.quiet:
            self.quiet = False
            return [("speech", {})]
        return []

    def feed(self, frame, segments=0):
        """记一帧（以及这一帧让切段器切出的段数），返回事件列表，通常为空。"""
        now = self._clock()
        rms = frame_rms(frame)
        sec = len(frame) / 2.0 / SAMPLE_RATE
        speech = rms >= self.speech_rms
        self.audio_sec += sec
        if speech:
            self.speech_frames += 1
        if rms > self.peak_rms:
            self.peak_rms = rms
        events = self.cut(segments)
        if not segments:
            self.since_cut += sec
            if not self.quiet and self.since_cut >= self.QUIET_AUDIO_SEC:
                self.quiet = True
                events.append(("quiet", {"audio_sec": round(self.since_cut, 1)}))
        if self._t0 is None:
            self._t0 = self._summary_at = now
        if now - self._summary_at >= self.SUMMARY_SEC:
            self._summary_at = now
            events.append(("summary", self.summary()))
        if self._win is None:
            if now - self._t0 >= self.WARMUP_SEC:
                self._win = [now, 0.0, 0, 0]
            return events
        win = self._win
        win[1] += sec
        win[2] += 1 if speech else 0
        win[3] += segments
        if now - win[0] >= self.WINDOW_SEC:
            events.extend(self._close_window(now))
        return events

    def _close_window(self, now):
        start, audio, speech, segments = self._win
        self._win = [now, 0.0, 0, 0]
        elapsed = now - start
        info = {"audio_sec": round(audio, 1), "wall_sec": round(elapsed, 1)}
        events = [("heartbeat", dict(info, speech_frames=speech, segments=segments))]
        ratio = audio / elapsed if elapsed > 0 else 1.0
        self._low_windows = self._low_windows + 1 if ratio < self.LOW_RATIO else 0
        if self._low_windows >= self.LOW_WINDOWS:
            self.low = True
            events.append(("low", info))
        elif self.low and ratio >= self.RECOVER_RATIO:
            self.low = False
            events.append(("recovered", info))
        return events
