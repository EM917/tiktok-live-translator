"""拉流收尾每一步都有上限（app/audio.py FFmpegAudioSource，2026-10-19）。

10-18 那场 91 分钟的静默卡住，在 wait() 要等管道断开的解释器上（3.12、3.13.5 这类没带 gh-119710 修复的版本）的机制（asr-bench/live-stability/design.md §1.3）：
ffmpeg 停在探测阶段、把音频攒在手里；看门狗到点后程序不再读 stdout、发 SIGTERM；ffmpeg 一次写出攒下的
音频，超过 StreamReader 缓冲上限两倍，asyncio 就暂停读这根管道；kill 之后暂停的管道读不到 EOF，
Process.wait() 永远不返回。这里用一个几行 Python 的假 ffmpeg（tests/fixtures/fake_ffmpeg.py）造出
同样的管道行为，真起子进程、真走 asyncio 的管道，不联网、不要 ffmpeg。

改之前，probe_flush 140000 那条在 3.12 和 3.13.5 上会永久卡住（pytest-timeout 兜底变红，不会挂住 CI）；
3.9、3.14 和带上 gh-119710 的 3.13.x（CI 的 3.13.16）进程一退出 wait() 就返回，本来就不卡——修法不能
指望解释器版本，所以这些用例在每个版本上都断言同一件事：收尾有上限、收尾时吐出的字节记得清。走到哪一步
随解释器而定的那条（kill 还是 kill+close），用能力探测（wait_waits_for_a_paused_pipe）而不是版本号来钉。
SIGTERM 的用例 Windows 上跳过（那里 terminate 就是直接结束进程，没有「收到信号再吐数据」这回事）。

自然结束（stdout 先 EOF）的用例每个平台都跑：Windows 上事件循环收到「进程已退出」总比 EOF 晚，以前
那里每次自然结束都记成 terminate。delayed_exit_notice 把这个先后在任何平台上造出来。

2026-10-22（专家审查 M2、M8）：POSIX 上第一个 SIGTERM 之后 TERM2_DELAY 秒还没退就补发一个（stop_path 记
terminate2），到 kill 的时刻不变；stderr 里「Will reconnect」的次数随 stop_facts 进 stream_break。文件末尾两条
用本机真 ffmpeg 对只监听 127.0.0.1 的假 CDN（tests/fake_flv_cdn.py），没有 ffmpeg 就跳过。
"""
import asyncio
import functools
import sys
import time
from pathlib import Path

import pytest

from app import audio
from app.audio import FRAME_BYTES, FFmpegAudioSource
from tests.helpers import poll_until

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_ffmpeg.py"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32",
                                reason="Windows 的 terminate 直接结束进程，没有 SIGTERM 处理")
# 正常收尾的最坏耗时：EXIT 1（只在 stdout 已 EOF 时）+ TERM 5 + KILL 3 + CLOSE 2，外加起进程、读 stderr 的零头
BOUND = (FFmpegAudioSource.EXIT_GRACE + FFmpegAudioSource.TERM_GRACE + FFmpegAudioSource.KILL_GRACE
         + 2 * FFmpegAudioSource.CLOSE_GRACE + 3.0)


@pytest.fixture
def fake(monkeypatch):
    """造一个音频源，命令换成假 ffmpeg；看门狗调到 1 秒。"""
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(FFmpegAudioSource, "STALL_SEC", 1.0)
    # 命令换成了假 ffmpeg：别为它去探测 PATH 上的真 ffmpeg（M8 的探测，见 tests/test_ffmpeg_command.py）
    monkeypatch.setattr(audio, "_probed", {"ffmpeg": {"tagged": False, "version": None}})

    def make(mode, *args):
        src = FFmpegAudioSource("file:///dev/null")
        src.command = lambda _ff: [sys.executable, "-I", str(FAKE), mode] + [str(a) for a in args]
        return src
    return make


def run(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


@functools.lru_cache(maxsize=None)
def wait_waits_for_a_paused_pipe():
    """能力探测，不看版本号：先登记 wait()，再让子进程退出，而它的 stdout 管道暂停着读（读不到 EOF、
    管道一直「连着」）——这个解释器的 Process.wait() 还返回吗？True = 不返回（3.12、3.13.5：要等所有
    管道断开，正是 10-18 的卡法）；False = 进程一退出就返回（3.9、3.14、带 gh-119710 的 3.13.x）。
    本机五个解释器各跑 8 次结果一致（asr-bench/live-stability/ci-fix/probe_paused_pipe.py）。"""
    async def probe():
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.subprocess_exec(
            lambda: asyncio.subprocess.SubprocessStreamProtocol(limit=2 ** 16, loop=loop),
            sys.executable, "-I", "-c", "import sys; sys.stdin.read()",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        proc = asyncio.subprocess.Process(transport, protocol, loop)
        wait_task = asyncio.ensure_future(proc.wait())
        try:
            transport.get_pipe_transport(1).pause_reading()
            await asyncio.sleep(0)               # wait() 先登记上——和收尾时先 wait、后 kill 的先后一样
            proc.stdin.close()                   # 子进程读到 stdin 的 EOF 才退出
            deadline = loop.time() + 10.0
            while proc.returncode is None:       # 事件循环已经收到「进程已退出」
                assert loop.time() < deadline, "探测用的子进程没有退出"
                await asyncio.sleep(0.01)
            done, _ = await asyncio.wait({wait_task}, timeout=0.5)
            return not done
        finally:
            transport.close()
            await asyncio.wait({wait_task}, timeout=2.0)
            if not wait_task.done():
                wait_task.cancel()
    return run(probe(), timeout=30)


def delayed_exit_notice(src, delay):
    """把事件循环收到「进程已退出」的通知往后推 delay 秒，并记下发出的 terminate / kill。Windows 上这个先后
    是常态：进程先关句柄（stdout 的 EOF 先到），句柄关完进程对象才 signaled，线程池里的等待再投进 IOCP。
    三个平台的事件循环都是在通知到的那一刻才去取 transport._process_exited，所以换掉实例上的它就够了。"""
    sent = []
    real_spawn = src._spawn

    def recording(name, real):
        def send():
            sent.append(name)
            real()
        return send

    async def spawn(cmd):
        proc = await real_spawn(cmd)
        transport = src._transport
        if delay:
            loop = asyncio.get_running_loop()
            real_exited = transport._process_exited
            transport._process_exited = lambda rc: loop.call_later(delay, real_exited, rc)
        for name in ("terminate", "kill"):
            setattr(proc, name, recording(name, getattr(proc, name)))
        return proc

    src._spawn = spawn
    return sent


async def consume(src, limit=None):
    """读到流结束；返回 (总帧数, 收尾时吐出的帧数)。"""
    n = tail = 0
    async for _ in src.frames():
        n += 1
        if src.stopping:
            tail += 1
        if limit and n >= limit:
            break
    return n, tail


def pipes_closed(src):
    transport = src._transport
    return all(transport.get_pipe_transport(fd) is None or transport.get_pipe_transport(fd).is_closing()
               for fd in (1, 2))


@POSIX_ONLY
def test_a_tail_bigger_than_the_pause_line_no_longer_hangs_the_stop(fake):
    """10-18 的形状：探测阶段攒下 140,000 字节（超过默认 64 KiB 上限的两倍），SIGTERM 后一次写出、
    之后不退。看门狗到点后照样读，吐出来的音频从 frames() 交出去（stopping 之后的帧），收尾有上限。"""
    src = fake("probe_flush", 140000)

    async def go():
        t0 = time.monotonic()
        n, tail = await consume(src)
        await src.stop()
        return n, tail, time.monotonic() - t0

    n, tail, sec = run(go())
    assert sec < src.STALL_SEC + BOUND
    assert src.stalled is True and src.stopping is True
    facts = src.stop_facts()
    assert facts["stop_path"] in ("kill", "kill+close")
    assert facts["tail_bytes"] == 140000 and facts["tail_sec"] == pytest.approx(140000 / 32000, abs=0.01)
    assert "tail_dropped_bytes" not in facts               # 都交出去了，没有在循环外读到、只能丢的
    assert n == tail == 140000 // FRAME_BYTES
    assert src.proc.returncode is not None
    assert pipes_closed(src)


@POSIX_ONLY
def test_a_tail_under_the_pause_line_still_returns(fake):
    src = fake("probe_flush", 131000)

    async def go():
        await consume(src)
        await src.stop()

    run(go())
    assert src.stop_facts()["tail_bytes"] == 131000


@POSIX_ONLY
def test_when_nobody_reads_the_paused_pipe_is_closed_and_stop_is_bounded(fake, monkeypatch):
    """消费方停在 yield 上不动（reader 在等一次广播）、外面调 stop()：没人读 stdout，子进程不理 SIGTERM
    照写不误，管道暂停（这里把缓冲上限调小，暂停线就是 128 KiB，4 秒写满）。kill 之后，在 wait() 要等管道断开的解释器上
    （wait_waits_for_a_paused_pipe 探测为真，如 3.12、3.13.5）等不到——收尾最后一步 transport.close() 把它收住；
    带了 gh-119710 修复的解释器（如 3.13.16、3.14）kill 之后就返回，停在 kill。"""
    monkeypatch.setattr(FFmpegAudioSource, "STREAM_LIMIT", 64 * 1024)
    src = fake("stream")

    async def go():
        agen = src.frames()
        await agen.__anext__()                   # 第一帧到手，生成器停在 yield 上
        t0 = time.monotonic()
        await src.stop()                         # 时钟对账那一类：外面要它停
        sec = time.monotonic() - t0
        await agen.aclose()                      # 读的那一方后来才收场
        return sec

    sec = run(go())
    assert sec < BOUND
    path = src.stop_facts()["stop_path"]
    if wait_waits_for_a_paused_pipe():
        assert path == "kill+close"              # 正是 10-18 的那一步：不关管道就永远等下去
    else:
        assert path == "kill"                    # 这个解释器 kill 之后 wait() 自己就返回了
    assert src.proc.returncode is not None
    assert pipes_closed(src)


@POSIX_ONLY
def test_the_paused_pipe_probe_sees_the_hang_on_the_interpreters_that_have_it():
    """探测本身也要可信：它说「会卡」的解释器上，kill 之后的 wait() 确实不返回——直接照 10-18 的形状
    （先登记 wait、管道暂停、kill）造一次，2 秒内看 wait() 回没回来。"""
    async def go():
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.subprocess_exec(
            lambda: asyncio.subprocess.SubprocessStreamProtocol(limit=2 ** 16, loop=loop),
            sys.executable, "-I", "-c", "import time; time.sleep(60)",
            stdin=None, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        proc = asyncio.subprocess.Process(transport, protocol, loop)
        wait_task = asyncio.ensure_future(proc.wait())
        try:
            transport.get_pipe_transport(1).pause_reading()
            await asyncio.sleep(0)
            proc.kill()
            done, _ = await asyncio.wait({wait_task}, timeout=2.0)
            return bool(done), proc.returncode
        finally:
            transport.close()
            await asyncio.wait({wait_task}, timeout=2.0)

    returned, rc = run(go())
    assert rc is not None                        # 两种解释器上进程都确实退了
    assert returned is (not wait_waits_for_a_paused_pipe())


@POSIX_ONLY
def test_a_consumer_that_stops_early_leaves_no_child_and_counts_what_it_dropped(fake):
    """消费方读了 10 帧就收掉生成器（aclose）；子进程不理 SIGTERM、照写不误。收尾有上限，
    循环外读到的只能计数后丢弃——记在 tail_dropped_bytes 里。"""
    src = fake("stream")

    async def go():
        t0 = time.monotonic()
        n, _ = await consume(src, limit=10)
        await src.stop()
        return n, time.monotonic() - t0

    n, sec = run(go())
    assert n == 10 and sec < BOUND
    facts = src.stop_facts()
    assert facts["stop_path"] in ("kill", "kill+close")
    assert facts["tail_dropped_bytes"] == facts["tail_bytes"] > 0
    assert src.proc.returncode is not None


@POSIX_ONLY
def test_two_concurrent_stops_share_one_set_of_signals(fake):
    """时钟对账 spawn 的 stop() 和 _stream_session finally 里的 stop() 同时来：只起一个收尾任务，两边
    等同一个结果。以前收尾跑在调用方自己的任务里，第一个调用方被取消后第二个直接返回，ffmpeg 还活着。"""
    src = fake("stream")
    calls = []

    async def go():
        got = []

        async def reader():
            async for _ in src.frames():
                got.append(1)
        task = asyncio.ensure_future(reader())
        await poll_until(lambda: len(got) >= 3, 10.0,
                         lambda: "收到 3 帧（已收到 {} 帧）".format(len(got)), interval=0.02)
        real_terminate = src.proc.terminate

        def terminate():
            calls.append("term")
            real_terminate()
        src.proc.terminate = terminate
        first = asyncio.ensure_future(src.stop())
        second = asyncio.ensure_future(src.stop())
        await asyncio.sleep(0.2)
        first.cancel()                           # 第一个调用方被取消：收尾照样做完
        await asyncio.gather(first, return_exceptions=True)
        await second
        await task
        return src._closer

    closer = run(go())
    # 一个收尾任务、一组信号：假进程不理 SIGTERM，所以 0.5 秒后补发了第二个（M2，2026-10-22；以前这里是
    # ["term"]，只发一个）。两个调用方各起一组的话会是四个
    assert calls == ["term", "term"]
    assert closer.done() and src.proc.returncode is not None
    assert src.stop_facts()["stop_path"] in ("kill", "kill+close")


@POSIX_ONLY
def test_a_cancelled_reader_takes_the_short_path(fake):
    """停止 / 换主播：reader 被取消，_stream_session 的 finally 用 urgent 收尾。整条取消路径要短于
    Pipeline.STOP_GRACE_SEC（3 秒），session_end 才由这一场自己写。"""
    from app.pipeline import Pipeline

    src = fake("stream")

    async def go():
        got = []

        async def reader():
            async for _ in src.frames():
                got.append(1)
                await asyncio.sleep(0.2)           # 像在等一次广播
        task = asyncio.ensure_future(reader())
        await poll_until(lambda: len(got) >= 2, 10.0,
                         lambda: "收到 2 帧（已收到 {} 帧）".format(len(got)), interval=0.02)
        t0 = time.monotonic()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await src.stop(urgent=True)
        return time.monotonic() - t0

    sec = run(go())
    assert sec < Pipeline.STOP_GRACE_SEC - 0.5
    facts = src.stop_facts()
    assert facts["stop_urgent"] is True and facts["stop_path"] in ("kill", "kill+close")


def test_urgent_limits_fit_inside_the_stop_grace():
    from app.pipeline import Pipeline

    s = FFmpegAudioSource
    assert s.URGENT_TERM_GRACE <= 0.5
    assert (s.URGENT_EXIT_GRACE + s.URGENT_TERM_GRACE + s.URGENT_KILL_GRACE + s.URGENT_CLOSE_GRACE
            < Pipeline.STOP_GRACE_SEC - 1.0)
    # 第二个 SIGTERM 落在 TERM 那一步里面：到 kill 的时刻和以前一样，整条收尾的上限不变（M2）
    assert 0 < s.TERM2_DELAY < s.TERM_GRACE and 0 < s.URGENT_TERM2_DELAY < s.URGENT_TERM_GRACE


def test_a_process_that_exits_on_its_own_is_just_reaped(fake):
    src = fake("eof", 32000)
    sent = delayed_exit_notice(src, 0)

    async def go():
        n, tail = await consume(src)
        await src.stop()
        return n, tail

    n, tail = run(go())
    assert (n, tail) == (10, 0)
    assert src.stop_facts()["stop_path"] == "exited"
    assert src.stop_facts()["tail_bytes"] == 0
    assert sent == [] and src.proc.returncode == 0


def test_a_process_that_dies_at_once_ends_the_round_cleanly(fake):
    src = fake("exit_now")
    sent = delayed_exit_notice(src, 0)
    assert run(consume(src)) == (0, 0)
    assert src.proc.returncode == 1
    assert src.stop_facts()["stop_path"] == "exited"
    assert sent == []


@pytest.mark.parametrize("mode,args,frames,rc", [("eof", (32000,), 10, 0), ("exit_now", (), 0, 1)])
def test_an_exit_notice_that_comes_after_the_eof_is_still_a_natural_end(fake, mode, args, frames, rc):
    """Windows 上的常态（CI windows-latest 3.13 以前两条都记成 terminate）：stdout 的 EOF 到了，事件循环
    还没收到「进程已退出」。不发信号，等它退出：stop_path 是 exited、退出码是它自己的，一轮照常结束。"""
    src = fake(mode, *args)
    sent = delayed_exit_notice(src, 0.3)

    async def go():
        n, tail = await consume(src)
        await src.stop()
        return n, tail

    assert run(go()) == (frames, 0)
    facts = src.stop_facts()
    assert facts["stop_path"] == "exited"
    assert sent == []
    assert src.proc.returncode == rc
    assert 250 <= facts["stop_ms"] < FFmpegAudioSource.EXIT_GRACE * 1000


def test_a_process_that_closes_stdout_but_stays_is_terminated_after_the_exit_grace(fake):
    """stdout 关了、进程却不退：等满 EXIT_GRACE 才发 SIGTERM（Windows 上 terminate），收尾照样有上限。"""
    src = fake("eof_linger", 32000)
    sent = delayed_exit_notice(src, 0)

    async def go():
        t0 = time.monotonic()
        n, _ = await consume(src)
        await src.stop()
        return n, time.monotonic() - t0

    n, sec = run(go())
    assert n == 10 and sec < BOUND
    facts = src.stop_facts()
    assert facts["stop_path"] == "terminate" and sent == ["terminate"]
    assert facts["stop_ms"] >= FFmpegAudioSource.EXIT_GRACE * 1000 - 50
    assert src.proc.returncode is not None


@POSIX_ONLY
def test_a_process_that_obeys_sigterm_stops_at_the_first_step(fake):
    src = fake("term_exit")

    async def go():
        await consume(src, limit=5)
        await src.stop()

    run(go())
    assert src.stop_facts()["stop_path"] == "terminate"


def test_missing_ffmpeg_is_unchanged(monkeypatch):
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: None)
    src = FFmpegAudioSource("http://example.invalid/x.flv")
    with pytest.raises(RuntimeError):
        run(consume(src))
    assert src.proc is None
    run(src.stop())                 # 没起过进程：什么都不做
    assert src.stop_facts() == {}


@POSIX_ONLY
def test_an_overlong_stderr_line_does_not_stop_stderr_reading(fake):
    """stderr 一行 70,000 字节、不带换行：以前读 stderr 的任务在这一行上退出，之后没人读 stderr。
    现在丢掉这一行接着读，收尾照样有上限。"""
    src = fake("long_stderr", 70000)

    async def go():
        t0 = time.monotonic()
        await consume(src)
        await src.stop()
        return time.monotonic() - t0

    sec = run(go())
    assert sec < src.STALL_SEC + BOUND
    assert "Packet mismatch" in src.stderr_tail()


def test_the_stdout_buffer_limit_is_raised():
    """便宜的第二道防线：暂停线从 128 KiB 变成 8 MiB（探测阶段最多攒 90 秒，折成 PCM 约 2.9 MB）。"""
    assert FFmpegAudioSource.STREAM_LIMIT >= 4 * 1024 * 1024


# ---- 整条流水线：照 10-18 排一串轮次 --------------------------------------------------------

@POSIX_ONLY
def test_a_10_18_like_sequence_writes_every_break_and_keeps_going(monkeypatch, tmp_path):
    """真的 _run_stream_inner → _run_session → _stream_session → FFmpegAudioSource，起的是假 ffmpeg：
    有声 6 秒 → 原地址一帧都没有、马上退出 → 查一次、有声 2 秒 → 原地址停在「探测阶段」、收尾吐出
    140,000 字节 → 查到已结束。改之前最后那一轮在没带 gh-119710 修复的 3.12/3.13 上永久卡住（不写 stream_break、不重连）；
    现在每一轮都有 stream_break，吐出的音频照样送去识别（段带 tail），这一场以「已结束」收尾。"""
    from types import SimpleNamespace

    import app.asr
    from app import reconnect_policy, resolver
    from app.resolver import ResolveError
    from tests.test_resilience_stream import ROOM, audit_rows, make_pipeline, of_type

    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", True)
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(app.asr, "create_transcriber", lambda **kw: SimpleNamespace(
        transcribe=lambda pcm: SimpleNamespace(text="hola", raw_text="hola", language="es",
                                               rejected=[])))
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(audio, "_probed", {"ffmpeg": {"tagged": False, "version": None}})
    monkeypatch.setattr(FFmpegAudioSource, "STALL_SEC", 1.0)
    script = [("eof", 192000), ("exit_now", 0), ("eof", 64000), ("probe_flush", 140000)]
    spawned = []

    def command(self, _ff):
        mode, n = script[min(len(spawned), len(script) - 1)]
        spawned.append(mode)
        return [sys.executable, "-I", str(FAKE), mode, str(n)]

    monkeypatch.setattr(FFmpegAudioSource, "command", command)
    lookups = []

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        lookups.append(url)
        if len(spawned) >= len(script):
            raise ResolveError("没开播", kind="offline", status=4)
        return "http://127.0.0.1:9/live/r{}.flv".format(len(lookups))

    async def reachable(*a, **k):
        return True, ""

    monkeypatch.setattr(resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
    t0 = time.monotonic()
    run(p._run_stream_inner(ROOM), timeout=90)
    assert time.monotonic() - t0 < 4 * BOUND
    rows = audit_rows(tmp_path)
    breaks = of_type(rows, "stream_break")
    assert [b["break_no"] for b in breaks] == [1, 2, 3, 4]
    assert [b["got_audio"] for b in breaks] == [True, False, True, False]
    assert [b["media_reused"] for b in breaks] == [False, True, False, True]
    last = breaks[-1]
    assert last["reason"] == "stall" and last["tail_bytes"] == 140000
    assert last["stop_path"] in ("kill", "kill+close")
    assert any(seg.get("tail") for seg in of_type(rows, "segment"))
    plans = [r["mode"] for r in of_type(rows, "reconnect_plan")]
    assert plans[:3] == ["reuse", "lookup", "reuse"]
    assert of_type(rows, "session_end")[0]["reason"] == "offline"
    assert spawned == ["eof", "exit_now", "eof", "probe_flush"]



# ---- M2：收尾补发第二个 SIGTERM（2026-10-22）----------------------------------------------------------
# ffmpeg 开始转码之后，要收到第二个信号才会打断阻塞中的读；单个 SIGTERM 不理，以前每次异常停流都固定多等
# 5 秒才被强杀（现网 28 次非用户停流全部走到 kill）。下面的假进程照这个样子写：第一个 SIGTERM 吐出攒着的
# N1 字节、接着卡住；第二个 SIGTERM 再吐 N2 字节后退出（退出码 255，和真 ffmpeg 一样）。

TWO_SIGNAL_FAKE = r"""
import os, signal, sys, time
n1, n2 = int(sys.argv[1]), int(sys.argv[2])
terms = []
def write_all(data):
    view = memoryview(data)
    while view:
        k = os.write(1, view[:65536])
        view = view[k:]
def on_term(_sig, _frame):
    terms.append(1)
    if len(terms) == 1:
        write_all(b"\x02" * n1)
    else:
        write_all(b"\x04" * n2)
        os._exit(255)
signal.signal(signal.SIGTERM, on_term)
for _ in range(5):
    write_all(b"\x01" * 3200)
    time.sleep(0.1)
while True:
    time.sleep(3600)
"""
N1, N2 = 20 * FRAME_BYTES, 10 * FRAME_BYTES
# 计时断言的余量（CI 高负载下也要稳）：补发前的等待只会被负载拉长、不会变短，所以下限照旧；上限都留 3 倍以上。
# 补发两个 SIGTERM 时收尾实测约 0.5 秒（下面的假进程 502–508 ms，真 ffmpeg 532 / 540 ms），上限放到 TERM_GRACE
# 的一半（2.5 秒）；只发一个 SIGTERM 时要等满 TERM_GRACE（5 秒）才 kill，照样超过它
WELL_BEFORE_KILL_MS = FFmpegAudioSource.TERM_GRACE * 1000 / 2


def two_signal_source(fake):
    src = fake("stream")
    src.command = lambda _ff: [sys.executable, "-I", "-c", TWO_SIGNAL_FAKE, str(N1), str(N2)]
    return src


def record_signals(src):
    """记下发出的 terminate / kill 和时刻（monotonic）。"""
    sent = []
    real_spawn = src._spawn

    async def spawn(cmd):
        proc = await real_spawn(cmd)
        for name in ("terminate", "kill"):
            real = getattr(proc, name)

            def send(name=name, real=real):
                sent.append((name, time.monotonic()))
                real()
            setattr(proc, name, send)
        return proc

    src._spawn = spawn
    return sent


@POSIX_ONLY
def test_a_second_sigterm_half_a_second_later_stops_it_and_keeps_the_tail(fake):
    """看门狗到点后发 SIGTERM，0.5 秒还没退就补一个；补发之后 frames() 接着读，两次吐出的音频全部交出去
    （CLAUDE.md 第六条），收尾不到 1 秒（以前要等满 TERM_GRACE 5 秒再 kill，第二次吐的那截也拿不到）。"""
    src = two_signal_source(fake)
    sent = record_signals(src)

    async def go():
        n, tail = await consume(src)
        await src.stop()
        return n, tail

    n, tail = run(go())
    assert [name for name, _t in sent] == ["terminate", "terminate"]
    gap = sent[1][1] - sent[0][1]
    assert FFmpegAudioSource.TERM2_DELAY - 0.05 <= gap < 3 * FFmpegAudioSource.TERM2_DELAY
    facts = src.stop_facts()
    assert facts["stop_path"] == "terminate2" and facts["stop_ms"] < WELL_BEFORE_KILL_MS
    assert facts["tail_bytes"] == N1 + N2 and "tail_dropped_bytes" not in facts
    assert tail == (N1 + N2) // FRAME_BYTES and n == 5 + tail
    assert src.proc.returncode == 255


@POSIX_ONLY
def test_without_the_second_signal_it_waits_for_kill_as_before(fake, monkeypatch):
    """Windows 上不补发（terminate 就是 TerminateProcess）。用 SECOND_SIGTERM=False 在 POSIX 上走这条：只有
    一个 SIGTERM，到 TERM_GRACE 才 kill，第二截音频没有——也就是改动前的样子。"""
    monkeypatch.setattr(audio, "SECOND_SIGTERM", False)
    monkeypatch.setattr(FFmpegAudioSource, "TERM_GRACE", 1.0)
    src = two_signal_source(fake)
    sent = record_signals(src)

    async def go():
        await consume(src)
        await src.stop()

    run(go())
    assert [name for name, _t in sent] == ["terminate", "kill"]
    assert sent[1][1] - sent[0][1] >= 1.0 - 0.05
    facts = src.stop_facts()
    assert facts["stop_path"] == "kill" and facts["tail_bytes"] == N1


@POSIX_ONLY
def test_the_urgent_path_sends_the_second_signal_sooner(fake, monkeypatch):
    """停止 / 换主播（reader 被取消）：第二个 SIGTERM 在 URGENT_TERM2_DELAY（0.15 秒）发，比正常路径的
    TERM2_DELAY（0.5 秒）早，整条路径仍在 STOP_GRACE 之内；取消路径上读到的音频交不出去，照样计数
    （tail_dropped_bytes），不静默丢。

    URGENT_TERM_GRACE 在这里放宽到 1.5 秒：真值 0.3 秒时，补发之后只剩 0.15 秒等假进程退出，CI 高负载下 kill 会
    抢在它前面。到 kill 的时刻是 URGENT_TERM_GRACE、和补发无关（两个上限的关系见
    test_urgent_limits_fit_inside_the_stop_grace）；只发一个 SIGTERM 时这里仍是 terminate、kill，照样红。"""
    from app.pipeline import Pipeline

    monkeypatch.setattr(FFmpegAudioSource, "URGENT_TERM_GRACE", 1.5)
    src = two_signal_source(fake)
    sent = record_signals(src)

    async def go():
        got = []

        async def reader():
            async for _ in src.frames():
                got.append(1)
        task = asyncio.ensure_future(reader())
        await poll_until(lambda: len(got) >= 2, 10.0,
                         lambda: "收到 2 帧（已收到 {} 帧）".format(len(got)), interval=0.02)
        t0 = time.monotonic()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await src.stop(urgent=True)
        return time.monotonic() - t0

    sec = run(go())
    assert sec < Pipeline.STOP_GRACE_SEC - 0.5
    assert [name for name, _t in sent] == ["terminate", "terminate"]
    gap = sent[1][1] - sent[0][1]
    # 下限不受负载影响；上限是正常路径的补发时刻，约为 URGENT_TERM2_DELAY 的 3.3 倍
    assert FFmpegAudioSource.URGENT_TERM2_DELAY - 0.05 <= gap < FFmpegAudioSource.TERM2_DELAY
    facts = src.stop_facts()
    assert facts["stop_path"] == "terminate2" and facts["stop_urgent"] is True
    # 取消之后读到的都算收尾吐出的：两次信号吐的 N1 + N2，加上取消时还在路上的几块
    assert facts["tail_dropped_bytes"] == facts["tail_bytes"] >= N1 + N2


@POSIX_ONLY
def test_a_process_that_already_exited_gets_no_second_signal(fake, monkeypatch):
    """进程已经退了、只是 wait() 还没返回（暂停的管道没关）：不补发，stop_path 不记 terminate2，往下照旧。"""
    for name, sec in (("TERM_GRACE", 1.0), ("KILL_GRACE", 0.3), ("CLOSE_GRACE", 0.3)):
        monkeypatch.setattr(FFmpegAudioSource, name, sec)
    src = fake("term_exit")                      # 收到 SIGTERM 立刻退出
    sent = record_signals(src)
    recording_spawn = src._spawn

    async def spawn(cmd):
        proc = await recording_spawn(cmd)
        # 进程退了也不返回的 wait()：3.12、3.13.5 上管道没关时就是这样（见文件头）
        proc.wait = lambda: asyncio.get_running_loop().create_future()
        return proc

    src._spawn = spawn

    async def go():
        await consume(src, limit=5)
        await src.stop()

    run(go())
    assert src.proc.returncode is not None
    assert [name for name, _t in sent] == ["terminate", "kill"]
    assert "terminate2" not in src.stop_facts()["stop_path"]


# ---- M8：stderr 里的「Will reconnect」计数，尾巴只收 error 级 -------------------------------------------

STDERR_FAKE = r"""
import os, sys
for line in sys.argv[1:]:
    os.write(2, (line + "\n").encode())
os.write(1, b"\x03" * 32000)
"""


def stderr_source(fake, lines):
    src = fake("eof")
    src.command = lambda _ff: [sys.executable, "-I", "-c", STDERR_FAKE] + list(lines)
    return src


def old_stop_facts(src):
    """9fa26b6 的 stop_facts() 原样：没有内部重连时新的输出必须和它逐字节相同。"""
    info = src.stop_info
    out = {"stop_path": info.get("path"), "stop_ms": info.get("stop_ms"),
           "tail_bytes": src.tail_bytes,
           "tail_sec": round(src.tail_bytes / float(audio.BYTES_PER_SEC), 2)}
    if src.tail_dropped_bytes:
        out["tail_dropped_bytes"] = src.tail_dropped_bytes
    if info.get("urgent"):
        out["stop_urgent"] = True
    return out


def test_stop_facts_carry_ffmpeg_reconnects_only_when_there_were_some(fake):
    import json

    tagged = ["[http @ 0x1] [error] Stream ends prematurely at 5, should be 18446744073709551615",
              "[http @ 0x1] [warning] Will reconnect at 5 in 0 second(s), error=End of file.",
              "    Last message repeated 1 times",
              "[aac @ 0x2] [warning] Queue input is backward in time",
              "[in#0/hls @ 0x3] [error] Error during demuxing: Input/output error"]
    src = stderr_source(fake, tagged)

    async def go(s):
        await consume(s)
        await s.stop()

    run(go(src))
    facts = src.stop_facts()
    assert facts["ffmpeg_reconnects"] == 2
    assert list(facts)[-1] == "ffmpeg_reconnects"             # 新键追加在最后，前面不动
    assert src.stderr_tail() == ("[http @ 0x1] Stream ends prematurely at 5, should be "
                                 "18446744073709551615 | [in#0/hls @ 0x3] Error during demuxing: "
                                 "Input/output error")
    quiet = stderr_source(fake, [x.replace(" [error]", "") for x in tagged if "[warning]" not in x
                                 and "repeated" not in x])
    run(go(quiet))
    assert quiet.ffmpeg_reconnects == 0
    assert json.dumps(quiet.stop_facts()) == json.dumps(old_stop_facts(quiet))
    assert quiet.stderr_tail() == src.stderr_tail()


def test_a_stderr_reader_cut_off_after_its_grace_still_writes_the_pending_repeat(monkeypatch):
    """stderr 过了 STDERR_GRACE 还没读完（比如管道被别的进程拿着）：取消读 stderr 的任务之后，等它收住再返回，
    还没写出的「Last message repeated」照样进尾巴——调用方紧接着读 stderr_tail（Pipeline._record_stream_break）
    不会少这一行。以前只 cancel 不等，这时尾巴里还没有这一行。"""
    from types import SimpleNamespace

    monkeypatch.setattr(FFmpegAudioSource, "STDERR_GRACE", 0.1)
    lines = [b"[http @ 0x1] [error] Stream ends prematurely at 5\n",
             b"[http @ 0x1] [warning] Will reconnect at 5 in 0 second(s)\n",
             b"[http @ 0x1] [error] Stream ends prematurely at 5\n"]

    class Stderr:
        async def readline(self):
            if lines:
                return lines.pop(0)
            await asyncio.sleep(3600)             # 一直不给 EOF

    src = FFmpegAudioSource("file:///dev/null")
    src.proc = SimpleNamespace(stderr=Stderr())

    async def go():
        src._stderr_task = asyncio.ensure_future(src._drain_stderr())
        await src._finish_stderr()
        return src.stderr_tail(), src._stderr_task.cancelled()

    tail, cancelled = run(go())
    assert cancelled is True
    assert tail == "[http @ 0x1] Stream ends prematurely at 5 | Last message repeated 1 times"
    assert src.ffmpeg_reconnects == 1


# ---- 真 ffmpeg 对假 CDN（只监听 127.0.0.1，tests/fake_flv_cdn.py）：读卡住时收尾远早于 kill ----------------

FFMPEG = audio.find_ffmpeg()
NEEDS_FFMPEG = pytest.mark.skipif(FFMPEG is None or sys.platform == "win32",
                                  reason="要本机的 ffmpeg 和 POSIX 信号")


@pytest.fixture(scope="module")
def cdn(tmp_path_factory):
    if FFMPEG is None or sys.platform == "win32":
        pytest.skip("要本机的 ffmpeg 和 POSIX 信号")
    from tests import fake_flv_cdn

    d = tmp_path_factory.mktemp("flv")
    av = fake_flv_cdn.make_flv(FFMPEG, d / "av.flv", 12, video=True).read_bytes()
    aonly = fake_flv_cdn.make_flv(FFMPEG, d / "aonly_claims.flv", 12, video=False,
                                  claims_video=True).read_bytes()
    with fake_flv_cdn.FakeCDN({"live/av.flv": av, "live/aonly_claims.flv": aonly}) as server:
        yield server


@pytest.fixture
def real(monkeypatch):
    """真 ffmpeg：看门狗调到 1.5 秒；不继承开发机上的 TLT_FFMPEG_LEGACY；重连显式用 V2（V1 时 FLV 回到
    ffmpeg 自己重连，见 audio.flv_exits_on_drop）。"""
    from app import reconnect_policy

    monkeypatch.delenv("TLT_FFMPEG_LEGACY", raising=False)
    monkeypatch.setattr(audio, "FFMPEG_LEGACY", False)
    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", True)
    monkeypatch.setattr(audio, "_probed", {})
    monkeypatch.setattr(FFmpegAudioSource, "STALL_SEC", 1.5)


async def consume_and_stop(src):
    n, tail = await consume(src)
    await src.stop()
    return n, tail


@NEEDS_FFMPEG
def test_a_stalled_cdn_read_stops_well_before_the_kill(cdn, real):
    """M2：ffmpeg 已经在转码、CDN 不再发数据但连接开着（半开）。看门狗到点后收尾，补发的第二个 SIGTERM
    打断阻塞的读。本批实测 stop_ms 532 / 540 ms（brew 9.0.1 / imageio 7.1）；改动前这里固定走到 kill，
    5003 / 5090 ms。上限 WELL_BEFORE_KILL_MS（2.5 秒）。"""
    src = FFmpegAudioSource(cdn.url("live/av.flv", burst=2, stall=3))
    n, _tail = run(consume_and_stop(src))
    facts = src.stop_facts()
    assert src.stalled is True and n >= 25                  # 收到了 3 秒左右的音频才卡住
    assert facts["stop_path"] == "terminate2"
    assert facts["stop_ms"] < WELL_BEFORE_KILL_MS


@NEEDS_FFMPEG
def test_audio_held_in_the_probe_stage_comes_out_with_either_signal_count(cdn, real, monkeypatch):
    """M2 + 第六条：流头说有视频、其实只有音频，CDN 发了 2 秒（不到 -analyzeduration 的 3 秒）就卡住。
    ffmpeg 还在探测、一帧没出；SIGTERM 让它把攒着的约 2 秒吐出来。补发第二个之后吐出的不比只发一个少，
    收尾却不用等到 kill。"""
    def round_(second):
        monkeypatch.setattr(audio, "SECOND_SIGTERM", second)
        monkeypatch.setattr(FFmpegAudioSource, "TERM_GRACE", 5.0 if second else 1.5)
        src = FFmpegAudioSource(cdn.url("live/aonly_claims.flv", burst=2, stall=2))
        n, tail = run(consume_and_stop(src))
        return src, n, tail

    two, n2, tail2 = round_(True)
    one, n1, tail1 = round_(False)
    assert n2 == tail2 and n1 == tail1                      # 收尾之前一帧都没有：全是收尾时吐出的
    assert two.stop_facts()["tail_sec"] >= 1.5
    assert two.tail_bytes >= one.tail_bytes
    assert two.stop_facts()["stop_ms"] < WELL_BEFORE_KILL_MS   # 实测 512 / 517 ms；只发一个是 5002 / 5004
    assert one.stop_facts()["stop_path"] == "kill"
