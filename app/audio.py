"""用 ffmpeg 从直播流中抽取音频，输出 16 kHz 单声道 16-bit PCM 帧。"""
import asyncio
from collections import deque

from .ffmpeg_bin import find_ffmpeg

SAMPLE_RATE = 16000
FRAME_SEC = 0.1
FRAME_BYTES = int(SAMPLE_RATE * FRAME_SEC) * 2  # 0.1 秒 @ 16 kHz mono s16le


class FFmpegAudioSource:
    def __init__(self, media_url, denoise_model=None):
        self.media_url = media_url
        self.denoise_model = denoise_model  # RNNoise 模型路径；None = 不降噪
        self.proc = None
        self._stderr_tail = deque(maxlen=8)
        self._stderr_task = None

    async def frames(self):
        """异步生成固定长度（0.1 秒）的 PCM 帧，流结束后返回。"""
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("缺少音频组件 ffmpeg——请关闭程序后重新打开，会自动补装")
        cmd = [ffmpeg, "-nostdin", "-loglevel", "error"]
        if self.media_url.startswith("http"):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        cmd += ["-i", self.media_url, "-vn", "-ac", "1"]
        if self.denoise_model:
            # 高通滤掉低频轰鸣，再用 RNNoise 神经降噪抑制背景音乐/噪声、保留人声
            # （arnndn 内部以 48 kHz 工作，ffmpeg 会自动插入重采样）
            cmd += ["-af", "highpass=f=70,arnndn=m={}".format(self.denoise_model)]
        cmd += ["-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        buf = bytearray()
        try:
            while True:
                chunk = await self.proc.stdout.read(FRAME_BYTES)
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
