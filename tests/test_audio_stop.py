"""拉流收尾每一步都有上限（app/audio.py FFmpegAudioSource，2026-10-19）。

10-18 那场 91 分钟的静默卡住，在 Python 3.12/3.13 上的机制（asr-bench/live-stability/design.md §1.3）：
ffmpeg 停在探测阶段、把音频攒在手里；看门狗到点后程序不再读 stdout、发 SIGTERM；ffmpeg 一次写出攒下的
音频，超过 StreamReader 缓冲上限两倍，asyncio 就暂停读这根管道；kill 之后暂停的管道读不到 EOF，
Process.wait() 永远不返回。这里用一个几行 Python 的假 ffmpeg（tests/fixtures/fake_ffmpeg.py）造出
同样的管道行为，真起子进程、真走 asyncio 的管道，不联网、不要 ffmpeg。

改之前，probe_flush 140000 那条在 3.12/3.13 上会永久卡住（pytest-timeout 兜底变红，不会挂住 CI）；
3.9 和 3.14 的 wait() 不等管道，本来就不卡——修法不能指望解释器版本，所以这些用例在每个版本上都断言
同一件事：收尾有上限、收尾时吐出的字节记得清。SIGTERM 的用例 Windows 上跳过（那里 terminate 就是
直接结束进程，没有「收到信号再吐数据」这回事）。
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

from app import audio
from app.audio import FRAME_BYTES, FFmpegAudioSource

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_ffmpeg.py"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32",
                                reason="Windows 的 terminate 直接结束进程，没有 SIGTERM 处理")
# 正常收尾的最坏耗时：TERM 5 + KILL 3 + CLOSE 2，外加起进程、读 stderr 的零头
BOUND = FFmpegAudioSource.TERM_GRACE + FFmpegAudioSource.KILL_GRACE + 2 * FFmpegAudioSource.CLOSE_GRACE + 3.0


@pytest.fixture
def fake(monkeypatch):
    """造一个音频源，命令换成假 ffmpeg；看门狗调到 1 秒。"""
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(FFmpegAudioSource, "STALL_SEC", 1.0)

    def make(mode, *args):
        src = FFmpegAudioSource("file:///dev/null")
        src.command = lambda _ff: [sys.executable, "-I", str(FAKE), mode] + [str(a) for a in args]
        return src
    return make


def run(coro, timeout=60):
    return asyncio.run(asyncio.wait_for(coro, timeout))


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
    照写不误，管道暂停（这里把缓冲上限调小，暂停线就是 128 KiB，4 秒写满）。kill 之后 3.12/3.13 的
    wait() 等不到管道断开——收尾最后一步 transport.close() 把它收住。"""
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
    assert path in ("kill", "kill+close")
    if sys.version_info[:2] in ((3, 12), (3, 13)):
        assert path == "kill+close"              # 正是 10-18 的那一步：不关管道就永远等下去
    assert src.proc.returncode is not None
    assert pipes_closed(src)


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
        while len(got) < 3:
            await asyncio.sleep(0.02)
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
    assert calls == ["term"]
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
        while len(got) < 2:
            await asyncio.sleep(0.02)
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
    assert s.URGENT_TERM_GRACE + s.URGENT_KILL_GRACE + s.URGENT_CLOSE_GRACE < Pipeline.STOP_GRACE_SEC - 1.0


def test_a_process_that_exits_on_its_own_is_just_reaped(fake):
    src = fake("eof", 32000)

    async def go():
        n, tail = await consume(src)
        await src.stop()
        return n, tail

    n, tail = run(go())
    assert (n, tail) == (10, 0)
    assert src.stop_facts()["stop_path"] == "exited"
    assert src.stop_facts()["tail_bytes"] == 0


def test_a_process_that_dies_at_once_ends_the_round_cleanly(fake):
    src = fake("exit_now")
    assert run(consume(src)) == (0, 0)
    assert src.proc.returncode == 1
    assert src.stop_facts()["stop_path"] == "exited"


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
