# i18n: done
"""用 ffmpeg 从直播流中抽取音频，输出 16 kHz 单声道 16-bit PCM 帧。"""
import asyncio
import time
from collections import deque

import numpy as np

from .ffmpeg_bin import filter_path, find_ffmpeg
from .i18n import L

SAMPLE_RATE = 16000
FRAME_SEC = 0.1
FRAME_BYTES = int(SAMPLE_RATE * FRAME_SEC) * 2  # 0.1 秒 @ 16 kHz mono s16le
BYTES_PER_SEC = SAMPLE_RATE * 2


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
      * 收尾是一个独立任务（保住引用），每一步都有上限：SIGTERM 后等 TERM_GRACE，SIGKILL 后等
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
    # 同样的守护给 ffmpeg 自己：http 读超时（微秒），到点它会走 -reconnect
    RW_TIMEOUT_US = "15000000"
    # StreamReader 的缓冲上限。缓冲超过它的两倍 asyncio 才暂停读管道。默认 64 KiB（暂停线 128 KiB，
    # 约 4 秒音频）正是 10-18 那种卡法的门槛；放到 4 MiB（暂停线 8 MiB，约 4 分钟音频）是便宜的第二道
    # 防线：探测阶段最多攒 90 秒流时间（折成 PCM 约 2.9 MB），收尾窗口里吐出的更少。stderr 的
    # readline 上限也跟着变大（一行很长的报错不会再让读 stderr 的任务提前退出）
    STREAM_LIMIT = 4 * 1024 * 1024
    READ_BYTES = 64 * 1024
    # 收尾每一步的上限（秒）。复现里攒下的音频 SIGTERM 后 0.09 秒内就吐完；10-18 有 34 次走到 kill。
    # EXIT_GRACE 只在 stdout 已经 EOF 时用：等进程自己退出（通常几毫秒），到点还在才走 SIGTERM
    EXIT_GRACE = 1.0
    TERM_GRACE = 5.0
    KILL_GRACE = 3.0
    CLOSE_GRACE = 2.0
    # 调用方被取消时（停止、换主播）的上限：合计 1.5 秒，短于 Pipeline.STOP_GRACE_SEC
    URGENT_EXIT_GRACE = 0.2
    URGENT_TERM_GRACE = 0.3
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
        self._stderr_task = None

    def command(self, ffmpeg):
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
                    self._stderr_tail.append(text)
        except Exception:
            pass

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
                if not await self._await_exit(wait_task, self.TERM_GRACE, self.URGENT_TERM_GRACE):
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
