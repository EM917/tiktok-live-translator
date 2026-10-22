# i18n: done
"""用 ffmpeg 从直播流中抽取音频，输出 16 kHz 单声道 16-bit PCM 帧。"""
import asyncio
import os
import re
import subprocess
import sys
import time
import urllib.parse
from collections import deque

import numpy as np

from .ffmpeg_bin import filter_path, find_ffmpeg
from .i18n import L

SAMPLE_RATE = 16000
FRAME_SEC = 0.1
FRAME_BYTES = int(SAMPLE_RATE * FRAME_SEC) * 2  # 0.1 秒 @ 16 kHz mono s16le
BYTES_PER_SEC = SAMPLE_RATE * 2

# ---- 拉流 ffmpeg 的参数（2026-10-22，专家审查 M1、M3、M8）----
# 应急退回改动前的参数不改代码：启动时设 TLT_FFMPEG_LEGACY=1，这一次运行的拉流命令逐字节回到原样
# （FLV 也开 -reconnect_streamed 1、不设 -analyzeduration、-loglevel error，也不探测），和
# TLT_RECONNECT_V2=0 一个路数。测试要测哪一边就显式 monkeypatch 这一项或环境变量
FFMPEG_LEGACY = False
# 只有音频的流头却声称有视频时，ffmpeg 对 FLV 默认最多分析 90 秒流时间才开始出音频（显式设了就不走这个
# 特例）。3 秒：实测这种假流首帧 1.2 秒，正常音视频流加不加都是约 0.95 秒（asr-bench media-asr e7）
ANALYZE_US = "3000000"
# 带级别标签的日志：warning 级也打出来（才看得到「Will reconnect」），每行带 [error] / [warning]。
# 前面的「+」不能省：不带「+」是绝对写法，会把 ffmpeg 默认开着的「重复行折叠」一起关掉（实测 brew 9.0.1、
# imageio 7.1：level+warning 下同一行重复 42 次就打 42 行，+level+warning 照旧是一行加
# 「Last message repeated 41 times」），stderr 尾巴就和 -loglevel error 时对不上了
LOGLEVEL_TAGGED = "+level+warning"
LOGLEVEL_PLAIN = "error"
# 探测 ffmpeg 认不认 LOGLEVEL_TAGGED：-version 正常几十毫秒；放在拉流「起 ffmpeg」阶段的 10 秒预算之内
PROBE_TIMEOUT_SEC = 5.0
# 收尾时补发第二个 SIGTERM（M2）。Windows 的 terminate 是 TerminateProcess，直接结束进程，不用补
SECOND_SIGTERM = sys.platform != "win32"

_probed = {}            # ffmpeg 路径 -> {"tagged": 认不认带级别的日志, "version": -version 的第一行}


def ffmpeg_legacy(env=None):
    """这次运行的拉流 ffmpeg 用不用改动前的参数（TLT_FFMPEG_LEGACY=1）。"""
    env = os.environ if env is None else env
    flag = str(env.get("TLT_FFMPEG_LEGACY", "")).strip().lower()
    if flag in ("1", "on", "true", "yes"):
        return True
    if flag in ("0", "off", "false", "no"):
        return False
    return bool(FFMPEG_LEGACY)


def is_hls(url):
    """地址的 path 以 .m3u8 结尾（HLS）。不看 query：签名参数里也可能出现这几个字。"""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return False
    return path.lower().endswith(".m3u8")


def _run_version(ffmpeg, loglevel=None):
    """跑一次 ffmpeg -version，返回 (退出码, 第一行)；起不来或超时是 (None, None)。"""
    cmd = [ffmpeg, "-hide_banner"]
    if loglevel:
        cmd += ["-loglevel", loglevel]
    cmd.append("-version")
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=PROBE_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    lines = r.stdout.decode(errors="replace").splitlines() if r.returncode == 0 else []
    return r.returncode, (lines[0].strip()[:200] or None) if lines else None


def probe_ffmpeg(ffmpeg):
    """探测这份 ffmpeg（按路径缓存，一次运行只跑一次）：认不认 -loglevel +level+warning，以及
    -version 的第一行。程序优先用系统自带的 ffmpeg，版本不受控：老版本不认级别标志，带上它就整场拉不到流，
    所以先探测、认才用。同步、会起子进程，在事件循环里要放进线程（FFmpegAudioSource.frames 就这么做）。"""
    got = _probed.get(ffmpeg)
    if got is not None:
        return got
    rc, version = _run_version(ffmpeg, LOGLEVEL_TAGGED)
    tagged = rc == 0
    if not tagged:
        _, version = _run_version(ffmpeg)
        print("[信息] 这份 ffmpeg 不认 -loglevel {}，拉流日志照旧只记 error 级".format(
            LOGLEVEL_TAGGED))
    got = {"tagged": tagged, "version": version}
    _probed[ffmpeg] = got
    return got


def ffmpeg_version(ffmpeg=None):
    """ffmpeg -version 的第一行（如「ffmpeg version 7.1 Copyright …」），拿不到是 None。和探测共用缓存；
    第一次调用会起子进程（几十毫秒），在事件循环里调用请放进线程。"""
    ffmpeg = ffmpeg or find_ffmpeg()
    if ffmpeg is None:
        return None
    return probe_ffmpeg(ffmpeg)["version"]


# ffmpeg 带级别标签的一行：标签在上下文前缀之后，如「[http @ 0x…] [warning] Will reconnect at …」、
# 「[error] Error opening input file …」（实测 brew 9.0.1、imageio 7.1；Windows 上指针没有 0x，所以前缀只认方括号）
_LEVEL_TAG = re.compile(
    r"^((?:\[[^\[\]]*\] )*?)\[(panic|fatal|error|warning|info|verbose|debug|trace)\] ")
_REPEATED = re.compile(r"^Last message repeated (\d+) times$")
_TAIL_LEVELS = frozenset(("panic", "fatal", "error"))


class StderrLines:
    """把 ffmpeg 的 stderr 逐行分拣成「尾巴」和计数（M8）。

    尾巴（stderr_tail，进 stream_break、界面，pipeline_stream._round_facts 在里面找 HTTP 4xx）只收 error 级
    及以上的行，去掉级别标签后和 -loglevel error 时 ffmpeg 自己打的逐字节相同：
      * 没带标签的行（ffmpeg 不认级别标志、TLT_FFMPEG_LEGACY=1，或者一条标签都还没见过）原样收，和以前一样；
      * 带标签之后，warning 级的行不进尾巴；含「Will reconnect」的只计数 reconnects（ffmpeg 内部重连了几次）；
      * 「Last message repeated N times」和一条消息里换行出来的后半截没有标签，归上一条消息的级别；
      * -loglevel error 时中间隔着的 warning 根本不打，同一条报错前后两次会被 ffmpeg 折成「Last message
        repeated」；现在 warning 也打了，ffmpeg 就不折了——这里照样折回去，流结束时把没写出的那句补上
        （ffmpeg 正常退出时也会补；被 kill 时它来不及补，这里仍补上——已知和以前不一样的只有这一处）。"""

    def __init__(self, tail):
        self.tail = tail
        self.reconnects = 0
        self._tagged = False        # 见过带标签的行：之后按级别分拣
        self._level = None          # 上一条消息的级别
        self._reconnect = False     # 上一条消息是「Will reconnect」
        self._last = None           # 上一条收进尾巴的消息
        self._pending = 0           # 它之后又重复了几次、还没写出「Last message repeated」

    def feed(self, text):
        """喂一行（已经 strip、非空）。"""
        m = _LEVEL_TAG.match(text)
        if m is None and not self._tagged:
            self.tail.append(text)
            return
        if m is None:
            rep = _REPEATED.match(text)
            if rep is not None:
                n = int(rep.group(1))
                if self._reconnect:
                    self.reconnects += n
                if self._level in _TAIL_LEVELS:
                    self._pending += n
                return
        else:
            self._tagged = True
            self._level = m.group(2)
            text = (m.group(1) + text[m.end():]).strip()
            self._reconnect = "Will reconnect" in text
            if self._reconnect:
                self.reconnects += 1
        if self._level not in _TAIL_LEVELS or not text:
            return
        if text == self._last:
            self._pending += 1
            return
        self._flush()
        self.tail.append(text)
        self._last = text

    def _flush(self):
        if self._pending:
            self.tail.append("Last message repeated {} times".format(self._pending))  # i18n: data
            self._pending = 0

    def close(self):
        """stderr 读完了：补上还没写出的「Last message repeated」。"""
        self._flush()


class FFmpegAudioSource:
    """一轮拉流：起一个 ffmpeg，把它 stdout 上的 PCM 切成 0.1 秒一帧交出去，结束时把它收干净。

    收尾的规矩（2026-10-19 复现了 10-18 那 91 分钟的卡住，见 asr-bench/live-stability/design.md）：

      * stdout 只有 frames() 一个读者。asyncio 的 StreamReader 不许两个协程同时 read()（会抛
        RuntimeError），所以 stop() 从不读，只发信号、等收尾任务做完。
      * 收尾期间 frames() 接着读，直到 EOF 或截止时刻。以前看门狗到点就不再读 stdout：ffmpeg 停在
        「探测输入流」阶段时把收到的音频攒在自己手里，收到 SIGTERM 才一次写出来；写出的量超过
        StreamReader 缓冲上限的两倍，asyncio 就暂停读这根管道——进程被 kill 之后，暂停的管道永远读不到
        EOF，而 Python 3.12 和 3.13.5（本机 .venv）的 Process.wait() 要等所有管道关闭才返回，于是永远
        不返回：不写断流、不重连，界面一直是「正在连接直播音频流…」。（3.9、3.14 和后来带上 gh-119710
        的 3.13.x——CI 上的 3.13.16——进程一退出 wait() 就返回；收尾不靠这一点。）
      * 收尾是一个独立任务（保住引用），每一步都有上限：SIGTERM 后等 TERM_GRACE（POSIX 上 TERM2_DELAY
        秒还没退就补发一次 SIGTERM），SIGKILL 后等
        KILL_GRACE，还没完就 transport.close()（关掉所有管道、唤醒等 wait() 的人，不靠解释器版本），
        再等 CLOSE_GRACE。调用方被取消（停止、换主播）时宽限缩到 URGENT_*，整条取消路径要短于
        Pipeline.STOP_GRACE_SEC（3 秒），session_end 才由这一场自己写。
      * stdout 先读到了 EOF（ffmpeg 自己收工）时，先等它退出、最多 EXIT_GRACE，退了就不发信号：
        事件循环知道「进程已退出」可能比 EOF 晚。Windows 上通常晚（CI 两次观察都是这样；进程先关句柄，句柄关完进程对象才
        signaled，线程池里的等待再把它投进 IOCP），以前每次自然结束都记成 terminate，还对已退出的
        进程调一次 TerminateProcess；POSIX 上是偶发的先后竞争。
      * 收尾时 ffmpeg 吐出来的音频照样从 frames() 交出去（stopping 为 True 之后的帧），由调用方决定
        怎么用；离开循环之后才读到的（调用方提前收掉了生成器）只能计数后丢弃。记在 stop_info 里：
        CLAUDE.md 第六条「音频绝不静默丢弃」——丢之前至少要记下丢了多少。
    """
    # 连续多少秒一个字节都没收到就判定为「静默停滞」。换 Wi-Fi、NAT 超时、运营商
    # 掐断时常见的不是 EOF 而是半开连接：ffmpeg 的 -reconnect 只在读到错误/EOF
    # 时触发，内核 TCP 重传超时要十几分钟——这期间 read 永不返回、界面还显示
    # 「直播中」、主播说的每个违禁词都没被听到。对合规监听器这是最贵的静默。
    STALL_SEC = 20.0
    # 同样的守护给 ffmpeg 自己：http 读超时（微秒）。HLS 到点会走 -reconnect；FLV 关了内部重连
    # （见 command()），半开连接靠上面的 20 秒看门狗（asr-bench media-asr e10c）
    RW_TIMEOUT_US = "15000000"
    # StreamReader 的缓冲上限。缓冲超过它的两倍 asyncio 才暂停读管道。默认 64 KiB（暂停线 128 KiB，
    # 约 4 秒音频）正是 10-18 那种卡法的门槛；放到 4 MiB（暂停线 8 MiB，约 4 分钟音频）是便宜的第二道
    # 防线：以前没设 -analyzeduration 时，探测阶段最多攒 90 秒流时间（折成 PCM 约 2.9 MB）；FLV 现在设成
    # 3 秒（ANALYZE_US），TLT_FFMPEG_LEGACY=1 和 HLS 还是老样子，收尾窗口里吐出的更少。stderr 的
    # readline 上限也跟着变大（一行很长的报错不会再让读 stderr 的任务提前退出）
    STREAM_LIMIT = 4 * 1024 * 1024
    READ_BYTES = 64 * 1024
    # 收尾每一步的上限（秒）。复现里攒下的音频 SIGTERM 后 0.09 秒内就吐完；10-18 有 34 次走到 kill。
    # EXIT_GRACE 只在 stdout 已经 EOF 时用：等进程自己退出（通常几毫秒），到点还在才走 SIGTERM
    EXIT_GRACE = 1.0
    TERM_GRACE = 5.0
    # POSIX 上 SIGTERM 之后过这么久还没退，就再发一次 SIGTERM（M2）：ffmpeg 开始转码之后，要第二个信号才
    # 打断阻塞中的读，单个 SIGTERM 等满 TERM_GRACE 也不退（现网 28 次非用户停流全部走到 kill，p50 5010 ms）。
    # 两个信号 0.34–0.52 秒退出，收到信号时攒着的音频照样吐出来（media-asr e1、e1b）。从第一个信号起算，
    # 到 kill 还是 TERM_GRACE，整条收尾的上限不变
    TERM2_DELAY = 0.5
    KILL_GRACE = 3.0
    CLOSE_GRACE = 2.0
    # 调用方被取消时（停止、换主播）的上限：合计 1.5 秒，短于 Pipeline.STOP_GRACE_SEC
    URGENT_EXIT_GRACE = 0.2
    URGENT_TERM_GRACE = 0.3
    URGENT_TERM2_DELAY = 0.15
    URGENT_KILL_GRACE = 0.7
    URGENT_CLOSE_GRACE = 0.3
    STDERR_GRACE = 2.0
    # 收尾期间每次读最多等这么久：收尾任务做完、管道里又读不出东西，就不再等（真 ffmpeg 这时已经
    # 给了 EOF；这条是给不会给 EOF 的情形兜底，比如测试里的假进程）
    TAIL_POLL_SEC = 0.5

    def __init__(self, media_url, denoise_model=None):
        self.media_url = media_url
        self.denoise_model = denoise_model  # RNNoise 模型路径；None = 不降噪
        self.proc = None
        self.stalled = False                # 本次是因静默停滞被我们主动断开的
        self.stopping = False               # 已经开始收尾：之后 frames() 交出的帧是收尾时吐出的音频
        self._eof = False                   # 收尾之前 stdout 就读到了 EOF（ffmpeg 自己收工）
        # 收尾的事实，随 stream_break 进审计：path 走到了哪一步（exited / terminate / kill /
        # kill+close / close）、stop_ms、tail_bytes（开始收尾之后 ffmpeg 又吐出的字节数）、
        # tail_dropped_bytes（其中离开循环之后才读到、没能交出去的）。收尾任务做完才有
        self.stop_info = None
        self.tail_bytes = 0
        self.tail_dropped_bytes = 0
        # 阶段钩子（Pipeline 的阶段计时用）：起好进程时 "spawned"，开始收尾时 "stopping"。
        # 没设就不调；钩子出错不影响拉流
        self.on_stage = None
        self._transport = None
        self._closer = None
        self._urgent = None                 # 取消路径的信号（事件循环里才建，见 _urgent_future）
        self._stop_t0 = None
        self._stderr_tail = deque(maxlen=8)
        # stderr 分拣（M8）：尾巴只收 error 级，「Will reconnect」只计数，见 StderrLines
        self._stderr_lines = StderrLines(self._stderr_tail)
        self._stderr_task = None

    @property
    def ffmpeg_reconnects(self):
        """这一轮 ffmpeg 内部重连了几次（stderr 里「Will reconnect」的次数）。"""
        return self._stderr_lines.reconnects

    def command(self, ffmpeg):
        """拉流命令。TLT_FFMPEG_LEGACY=1 时和 2026-10-22 之前逐字节相同。

        * FLV（以及别的 http 渐进流，地址不一定以 .flv 结尾）关掉 ffmpeg 的内部重连（-reconnect_streamed 0，
          M1）：直播 CDN 断开后，内部重连用同一个地址连上，拿到的是从新的 FLV 头开始的一段流，ffmpeg 把它当成
          约 5 MB 的数据块跳读过去，这期间一帧音频都不出——2.1 Mbps 下聋 15–17 秒（不写 stream_break），
          250 kbps 下要等 20 秒看门狗加强杀，约 27 秒。关掉之后断开就退出，交给程序自己的重连，整段空档约
          2 秒（media-asr runs/e5b，6 次一致）。-reconnect 1 和 -rw_timeout 保留。
        * FLV 设 -analyzeduration（M3，见 ANALYZE_US）。
        * HLS（path 以 .m3u8 结尾）维持原参数：上面的跳读只在 FLV 上量过，HLS 不动。
        * 探测过、认带级别的日志时用 LOGLEVEL_TAGGED（M8）；没探测过或不认就是 -loglevel error。"""
        legacy = ffmpeg_legacy()
        probed = _probed.get(ffmpeg) or {}
        tagged = not legacy and probed.get("tagged") is True
        cmd = [ffmpeg, "-nostdin", "-loglevel", LOGLEVEL_TAGGED if tagged else LOGLEVEL_PLAIN]
        if self.media_url.startswith("http"):
            progressive = not legacy and not is_hls(self.media_url)
            if progressive:
                # 输入选项，放在 -reconnect 那一组前面：-rw_timeout 的值仍然紧挨着 -i（tests/test_bug_sweep.py 钉着）
                cmd += ["-analyzeduration", ANALYZE_US]
            cmd += ["-reconnect", "1", "-reconnect_streamed", "0" if progressive else "1",
                    "-reconnect_delay_max", "10", "-rw_timeout", self.RW_TIMEOUT_US]
        cmd += ["-i", self.media_url, "-vn", "-ac", "1"]
        if self.denoise_model:
            # 高通滤掉低频轰鸣，再用 RNNoise 神经降噪抑制背景音乐/噪声、保留人声
            # （arnndn 内部以 48 kHz 工作，ffmpeg 会自动插入重采样）
            cmd += ["-af", "highpass=f=70,arnndn=m={}".format(filter_path(self.denoise_model))]
        cmd += ["-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
        return cmd

    async def _spawn(self, cmd):
        """起 ffmpeg，返回 asyncio 的 Process，并留下它的 SubprocessTransport（收尾要 close() 它）。
        用 loop.subprocess_exec 而不是 create_subprocess_exec：后者把 transport 藏成私有属性。
        单独成一个方法，测试换掉它就不会去真起 ffmpeg。"""
        loop = asyncio.get_running_loop()
        limit = self.STREAM_LIMIT
        transport, protocol = await loop.subprocess_exec(
            lambda: asyncio.subprocess.SubprocessStreamProtocol(limit=limit, loop=loop),
            *cmd, stdin=None, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._transport = transport
        return asyncio.subprocess.Process(transport, protocol, loop)

    def _stage(self, name):
        hook = self.on_stage
        if hook is None:
            return
        try:
            hook(name)
        except Exception:
            pass

    async def frames(self):
        """异步生成固定长度（0.1 秒）的 PCM 帧，流结束后返回。stopping 为 True 之后交出的帧是
        收尾时 ffmpeg 吐出来的音频。"""
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError(L("缺少音频组件 ffmpeg——请关闭程序后重新打开，会自动补装",
                                 "Audio (ffmpeg) is missing. Quit and reopen the app to "
                                 "install it automatically."))
        if not ffmpeg_legacy() and ffmpeg not in _probed:
            # 第一次起这份 ffmpeg 之前探测一次（M8），之后读缓存。放进线程：不卡事件循环
            await asyncio.get_running_loop().run_in_executor(None, probe_ffmpeg, ffmpeg)
        self.proc = await self._spawn(self.command(ffmpeg))
        self._stage("spawned")
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        loop = asyncio.get_running_loop()
        buf = bytearray()
        done = cancelled = False      # done：读到了 EOF，或收尾已经做完、管道里也读不出东西了
        deadline = None
        try:
            while True:
                if self.stopping:
                    # 收尾中：接着读到 EOF。收尾任务最后一步会关管道，EOF 一定会来；截止时刻只是保险
                    if deadline is None:
                        deadline = loop.time() + self._tail_window()
                    timeout = min(self.TAIL_POLL_SEC, deadline - loop.time())
                    if timeout <= 0:
                        done = True
                        break
                else:
                    timeout = self.STALL_SEC
                try:
                    chunk = await asyncio.wait_for(self.proc.stdout.read(self.READ_BYTES),
                                                   timeout=timeout)
                except asyncio.TimeoutError:
                    if self.stopping:
                        if self._closer is not None and self._closer.done():
                            done = True
                            break
                        continue
                    self.stalled = True
                    print("[警告] 直播流 {:.0f} 秒没有任何数据（连接可能已半开），"
                          "主动断开交给自动重连".format(self.STALL_SEC))
                    # 不 break：发出收尾信号，接着读。ffmpeg 收到 SIGTERM 常会把攒着的音频一次写出来
                    self.request_stop()
                    continue
                if not chunk:
                    if not self.stopping:
                        self._eof = True
                    done = True
                    break
                if self.stopping:
                    self.tail_bytes += len(chunk)
                buf += chunk
                while len(buf) >= FRAME_BYTES:
                    yield bytes(buf[:FRAME_BYTES])
                    del buf[:FRAME_BYTES]
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            await self._finish_frames(done, cancelled)

    def _tail_window(self):
        """收尾期间最多再读多久：整条收尾路径的上限再加 1 秒。"""
        if self._urgent is not None and self._urgent.done():
            return self.URGENT_TERM_GRACE + self.URGENT_KILL_GRACE + self.URGENT_CLOSE_GRACE + 1.0
        return self.TERM_GRACE + self.KILL_GRACE + self.CLOSE_GRACE + 1.0

    async def _finish_frames(self, done, cancelled):
        """frames() 的收尾：调用方提前收掉了生成器（aclose、break）或被取消时，发出收尾信号并把
        stdout 读空（这里不能再交帧：计数后丢弃）；然后等收尾任务做完。"""
        if self.proc is None:
            return
        try:
            if not done:
                self.request_stop(urgent=cancelled)
                await self._drain_rest()
            await self.stop(urgent=cancelled)
        except asyncio.CancelledError:
            self.request_stop(urgent=True)
            raise

    async def _drain_rest(self):
        loop = asyncio.get_running_loop()
        end = loop.time() + self._tail_window()
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                return
            try:
                chunk = await asyncio.wait_for(self.proc.stdout.read(self.READ_BYTES),
                                               timeout=min(self.TAIL_POLL_SEC, remaining))
            except asyncio.TimeoutError:
                if self._closer is not None and self._closer.done():
                    return
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            if not chunk:
                return
            self.tail_bytes += len(chunk)
            self.tail_dropped_bytes += len(chunk)

    async def _drain_stderr(self):
        try:
            while True:
                try:
                    line = await self.proc.stderr.readline()
                except ValueError:
                    continue          # 一行超过缓冲上限：这一行丢掉，接着读，不让 stderr 管道没人读
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    self._stderr_lines.feed(text)
        except Exception:
            pass
        finally:
            self._stderr_lines.close()

    def stderr_tail(self):
        return " | ".join(self._stderr_tail)  # i18n: data

    def _urgent_future(self):
        if self._urgent is None:
            self._urgent = asyncio.get_running_loop().create_future()
        return self._urgent

    def request_stop(self, urgent=False):
        """开始收尾（只起一次）：同步、不等。返回收尾任务；进程还没起来时返回 None。
        urgent=True 把还没走完的各步宽限缩到 URGENT_*（调用方被取消时用）。"""
        if self.proc is None:
            return None
        if urgent:
            fut = self._urgent_future()
            if not fut.done():
                fut.set_result(True)
        if self._closer is None:
            self.stopping = True
            self._stop_t0 = time.monotonic()
            self._urgent_future()
            self._stage("stopping")
            # 独立任务、保住引用：发起收尾的那个调用方被取消，收尾照样做完（以前收尾跑在调用方
            # 自己的任务里，第一个调用方被取消后第二个直接返回，ffmpeg 还活着）
            self._closer = asyncio.ensure_future(self._close_out())
        return self._closer

    async def stop(self, urgent=False):
        """收尾并等它做完。可以并发调、可以调多次：只有一组信号，大家等同一个收尾任务。
        调用方被取消时把收尾升级成取消路径（宽限缩短），再把取消抛出去。"""
        closer = self.request_stop(urgent=urgent)
        try:
            if closer is not None:
                await asyncio.shield(closer)
            await self._finish_stderr()
        except asyncio.CancelledError:
            self.request_stop(urgent=True)
            raise

    async def _finish_stderr(self):
        task = self._stderr_task
        if task is None or task.done():
            return
        # asyncio.wait 到点不抛、也不吞外层的取消（以前 except (TimeoutError, CancelledError) 两个一起接）
        done, _ = await asyncio.wait({task}, timeout=self.STDERR_GRACE)
        if not done:
            task.cancel()

    def force_close(self):
        """关掉 ffmpeg 的 transport（所有管道 + 进程还在就 kill）。可重复调；收尾任务最后一步也是它。
        Pipeline 的阶段计时发现收尾超时时用来自愈。"""
        transport = self._transport
        if transport is None:
            return False
        try:
            transport.close()
        except Exception:
            return False
        return True

    async def _await_exit(self, wait_task, grace, urgent_grace):
        """等进程被回收（Process.wait 返回），最多 grace 秒；期间转成取消路径就只再等 urgent_grace。"""
        loop = asyncio.get_running_loop()
        urgent = self._urgent_future()
        deadline = loop.time() + (urgent_grace if urgent.done() else grace)
        while not wait_task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            waiters = {wait_task} if urgent.done() else {wait_task, urgent}
            await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if urgent.done():
                deadline = min(deadline, loop.time() + urgent_grace)
        return True

    async def _await_term(self, proc, wait_task, info):
        """第一个 SIGTERM 之后等进程退出，到 TERM_GRACE（取消路径 URGENT_TERM_GRACE）为止，返回退没退。

        POSIX 上过了 TERM2_DELAY（取消路径 URGENT_TERM2_DELAY）还没退，就补发一次 SIGTERM（M2），stop_path
        记 terminate2。以前只发一个：ffmpeg 已经开始转码、又卡在读网络上时不理它，每次异常停流都固定多等
        5 秒才被强杀。补发之后 frames() 照样接着读 stdout，ffmpeg 收到信号时吐出的音频照样交出去送识别
        （CLAUDE.md 第六条）。进程已经退了、只是 wait() 还没返回（管道没关）时不补发，往下照旧。
        Windows 不补发：那里 terminate 直接结束进程。"""
        if not SECOND_SIGTERM:
            return await self._await_exit(wait_task, self.TERM_GRACE, self.URGENT_TERM_GRACE)
        if await self._await_exit(wait_task, self.TERM2_DELAY, self.URGENT_TERM2_DELAY):
            return True
        if proc.returncode is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
            info["path"] = "terminate2"
        return await self._await_exit(wait_task, self.TERM_GRACE - self.TERM2_DELAY,
                                      self.URGENT_TERM_GRACE - self.URGENT_TERM2_DELAY)

    async def _close_out(self):
        proc = self.proc
        info = {"path": "exited", "urgent": False}
        wait_task = asyncio.ensure_future(proc.wait())
        try:
            if proc.returncode is None and self._eof:
                # stdout 已经 EOF：进程多半正在自己退出，只是事件循环还没收到退出通知（Windows 上
                # 通常这样）。先等它，退了就不发信号，stop_path 照实记 exited；到点还在才往下走 SIGTERM
                await self._await_exit(wait_task, self.EXIT_GRACE, self.URGENT_EXIT_GRACE)
            if proc.returncode is None:
                try:
                    proc.terminate()
                except (ProcessLookupError, OSError):
                    pass
                info["path"] = "terminate"
                if not await self._await_term(proc, wait_task, info):
                    try:
                        proc.kill()
                    except (ProcessLookupError, OSError):
                        pass
                    info["path"] = "kill"
                    if not await self._await_exit(wait_task, self.KILL_GRACE,
                                                  self.URGENT_KILL_GRACE):
                        info["path"] = "kill+close"
                        self.force_close()
                        await self._await_exit(wait_task, self.CLOSE_GRACE,
                                               self.URGENT_CLOSE_GRACE)
            # 进程已经退出、wait() 却还没返回（有根管道没关）：关掉。没人读、暂停了的管道就是这样
            if not wait_task.done() and not await self._await_exit(
                    wait_task, self.CLOSE_GRACE, self.URGENT_CLOSE_GRACE):
                info["path"] = "close" if info["path"] == "exited" else info["path"] + "+close"
                self.force_close()
                await self._await_exit(wait_task, self.CLOSE_GRACE, self.URGENT_CLOSE_GRACE)
        except Exception as exc:
            print("[警告] 收尾 ffmpeg 出错：{}".format(type(exc).__name__))
        finally:
            if not wait_task.done():
                wait_task.cancel()
            urgent = self._urgent
            info["urgent"] = bool(urgent is not None and urgent.done())
            info["stop_ms"] = int(round((time.monotonic() - self._stop_t0) * 1000))
            self.stop_info = info
        return info

    def stop_facts(self):
        """写进 stream_break 的收尾事实（没收尾过是空字典）。tail_* 读的是此刻的计数。"""
        info = self.stop_info
        if info is None:
            return {}
        out = {"stop_path": info.get("path"), "stop_ms": info.get("stop_ms"),
               "tail_bytes": self.tail_bytes,
               "tail_sec": round(self.tail_bytes / float(BYTES_PER_SEC), 2)}
        if self.tail_dropped_bytes:
            out["tail_dropped_bytes"] = self.tail_dropped_bytes
        if info.get("urgent"):
            out["stop_urgent"] = True
        # ffmpeg 这一轮内部重连了几次（M8）。和上面两项一样只在非零时写：没有重连时 stream_break 和以前
        # 逐字节相同。FLV 关了内部重连之后基本恒为 0，主要看 HLS；FLV 断流的次数看 reason=eof 的 stream_break
        if self.ffmpeg_reconnects:
            out["ffmpeg_reconnects"] = self.ffmpeg_reconnects
        return out


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
