"""用 ffmpeg 从直播流中抽取音频，输出 16 kHz 单声道 16-bit PCM 帧。"""
import asyncio
from collections import deque

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
