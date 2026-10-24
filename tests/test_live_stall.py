"""拉流卡住要能被发现（2026-10-19，改进清单第 1 条；设计 asr-bench/live-stability/design.md §2.1–2.2）。

  * 阶段计时：每一段等待有预算，超了写一条 stage_overrun（每进一次阶段最多一条）；收尾超时自愈一次——
    只关 transport、只取消这一轮的 reader，不取消整场；
  * 「已 N 分钟没收到直播音频」：从本场第一次起 ffmpeg 起算，触发看单调时钟、显示看墙钟，本机连不上
    TikTok 的提示挂着时不出，第一帧到了就撤；
  * 时钟跳变时按单调时钟计的起点往后挪，醒来不一起误报；
  * 统计循环里新加的检查抛错，循环照常跳；
  * 一轮自然结束后、后台还在等翻译时点停止，停止不被吞；
  * 收尾时 ffmpeg 吐出的音频照样送去识别，但不算「收到了直播音频」；
  * 以前卡死、现在晚几秒才结束的旧任务，碰不到新一场的翻译队列和提示；
  * _stop_locked 替旧任务代写 session_end 时也带上这一场的汇总。

全部用桩件：不联网、不起 ffmpeg、不加载模型。
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

import app.asr
import app.audio
import app.segmenter
from app import i18n, resolver
from app import pipeline as pipeline_mod
from app.audio import FRAME_BYTES
from app.audit import AuditLog
from app.pipeline import Pipeline
from app.stream_watch import OTHER, StageClock, StreamWatch
from tests import i18n_rules as rules
from tests.helpers import poll_until, run
from tests.test_resilience_stream import (CAUSE_LABELS, ROOM, RecordingServer, audit_rows,
                                          make_pipeline, of_type, rows_of)

EN = i18n.EN
T0 = 1790000000.0


@pytest.fixture(autouse=True)
def _no_hardware_probe(monkeypatch):
    """走 _run_session 的用例不探本机硬件：真的 recommend() 在 Apple Silicon 上会 import mlx_whisper，把真的
    mlx.core 留在进程里，排在后面的 tests/test_mlx_cache_limit.py（不许真的 mlx.core）就有 9 条红了。"""
    import app.hwdetect
    monkeypatch.setattr(app.hwdetect, "recommend", lambda backend=None, device=None: {
        "backend": "ct2", "model": "small", "device": "cpu", "compute_type": "int8", "note": ""})


def _session(monkeypatch, tmp_path, clock=None):
    """clock：用例按合成时间线喂（单调 1000 秒起、墙钟 T0 起）时传进来，这一场的观察状态就读这只钟——
    阶段起点、场末汇总这些按「现在」算的地方和喂进去的时刻在同一条时间线上。不传就是真钟（跑真的一轮
    拉流的用例：reader 给每帧盖的是真的 time.monotonic()）。真钟配合成时刻，算出来的数取决于机器开机
    多久（单调时钟从开机算起）：刚开机的机器上每一跳都会多写一条兜底阶段的 stage_overrun，开机久的不写。
    见 tests/test_coverage.py 的 _session。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    if clock is not None:
        monkeypatch.setattr(p, "_new_stream_watch", lambda: StreamWatch(
            p.STAGE_OTHER_SEC, mono=clock, wall=lambda: T0 + clock.t - 1000.0))
    sess = p._session_state = p._new_session_state(p.audit)
    return p, server, sess


# ---- 计数本身（app/stream_watch.py）------------------------------------------------------

class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def test_a_stage_overrun_is_reported_once_per_entry_and_leaving_falls_back_to_other():
    clock = Clock()
    stages = StageClock(60.0, mono=clock)
    assert stages.name == OTHER
    with stages.during("backoff", 12.0, break_no=3):
        clock.t += 12.5
        over = stages.overrun()
        assert over == {"stage": "backoff", "sec": 12.5, "budget_sec": 12.0, "break_no": 3}
        clock.t += 100
        assert stages.overrun() is None          # 这一次进来只报一次
    assert stages.name == OTHER and stages.overrun() is None
    stages.enter("streaming", None)              # 不设预算的阶段永不报
    clock.t += 10_000
    assert stages.overrun() is None
    assert stages.overruns == 1


def test_silence_counts_from_the_first_ffmpeg_not_from_lookup_or_model_load():
    """模型加载在第一次解析之后、第一次拉流之前，首次使用要下载几 GB：那段时间不算「没收到音频」。"""
    mono, wall = Clock(), Clock(T0)
    watch = StreamWatch(60.0, mono=mono, wall=wall)
    mono.t += 900
    wall.t += 900
    assert watch.silent_for() is None and watch.silent_wall() is None
    watch.note_spawn()
    mono.t += 30
    wall.t += 30
    assert watch.silent_for() == 30 and watch.silent_wall() == 30
    watch.note_audio(0.1, mono.t, wall.t)
    mono.t += 5
    assert watch.silent_for() == 5


def test_a_shift_moves_every_monotonic_start_but_not_the_wall_clock():
    mono, wall = Clock(), Clock(T0)
    watch = StreamWatch(60.0, mono=mono, wall=wall)
    watch.note_spawn()
    watch.stage.enter("backoff", 12.0)
    mono.t += 600                                 # Windows 睡了 10 分钟：单调时钟照走
    wall.t += 600
    watch.shift(590)
    assert watch.silent_for() == pytest.approx(10)
    assert watch.stage.overrun() is None
    assert watch.silent_wall() == pytest.approx(600)     # 给中控看的是墙钟：睡了多久就说多久


# ---- 「已 N 分钟没收到直播音频」-----------------------------------------------------------

def _armed(sess, mono0=1000.0):
    watch = sess["watch"]
    watch.first_spawn = (mono0, T0)
    return watch


def _tick(p, clock, mono, wall):
    """统计循环在（单调 mono、墙钟 wall）这一刻跳一下：这一场的钟也走到 mono。"""
    clock.t = mono
    return p._stream_watch_tick(now=(mono, wall))


def test_no_audio_appears_after_two_minutes_with_the_exact_words_and_one_audit_row(monkeypatch, tmp_path):
    clock = Clock(1000.0)
    p, server, sess = _session(monkeypatch, tmp_path, clock)
    _armed(sess)
    run(_tick(p, clock, 1000.0 + 119, T0 + 119))
    assert "session:no_audio" not in (server.config.get("incidents") or {})
    run(_tick(p, clock, 1000.0 + 125, T0 + 125))
    text = server.config["incidents"]["session:no_audio"]["text"]
    assert str(text) == ("已 2 分钟没收到直播音频，这段时间没有经过检测。程序还在自动重试，"
                         "可以在 TikTok 上看一眼直播是否还在进行。")
    assert i18n.render(text, EN) == ("No stream audio for 2 min. Nothing in that time was checked. "
                                     "The app keeps retrying. You can check on TikTok whether the "
                                     "stream is still live.")
    run(_tick(p, clock, 1000.0 + 190, T0 + 190))
    assert "3 分钟" in str(server.config["incidents"]["session:no_audio"]["text"])
    rows = of_type(rows_of(p.audit.path), "no_audio")
    assert len(rows) == 1 and rows[0]["sec"] == 125.0 and rows[0]["stage"] == OTHER
    assert rows[0]["break_no"] == 0


def test_no_audio_shows_wall_clock_minutes_after_the_lid_was_closed(monkeypatch, tmp_path):
    """macOS 合盖 30 分钟：单调时钟只走了 2 分钟多，界面要说 30 分钟，和时钟跳变那条对得上。"""
    clock = Clock(1000.0)
    p, server, sess = _session(monkeypatch, tmp_path, clock)
    _armed(sess)
    run(_tick(p, clock, 1000.0 + 125, T0 + 1800))
    assert str(server.config["incidents"]["session:no_audio"]["text"]).startswith("已 30 分钟没收到")


def test_no_audio_stays_away_while_the_network_notice_says_it_already(monkeypatch, tmp_path):
    clock = Clock(1000.0)
    p, server, sess = _session(monkeypatch, tmp_path, clock)
    _armed(sess)
    run(p._incident("session:network", "error", "x"))
    run(_tick(p, clock, 1000.0 + 300, T0 + 300))
    assert "session:no_audio" not in server.config["incidents"]


def test_the_first_frame_takes_the_notice_down_and_the_resume_row_says_so(monkeypatch, tmp_path):
    clock = Clock(1000.0)
    p, server, sess = _session(monkeypatch, tmp_path, clock)
    _armed(sess)

    async def scenario():
        await _tick(p, clock, 1000.0 + 130, T0 + 130)
        sess["deaf_since"] = time.time() - 130
        p._note_stream_resumed(sess, time.time())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    run(scenario())
    assert "session:no_audio" not in server.config["incidents"]
    resumed = of_type(rows_of(p.audit.path), "stream_resumed")
    assert resumed and resumed[0]["no_audio_notice"] is True
    assert sess["watch"].max_deaf_sec >= 129


def test_stopping_takes_the_no_audio_notice_down():
    assert "session:no_audio" in Pipeline.ONGOING_INCIDENTS


@pytest.mark.parametrize("tail", [0.0, 36.0])
def test_the_no_audio_words_name_no_cause(tail):
    text = Pipeline._no_audio_text(SimpleNamespace(_duration_text=Pipeline._duration_text), 600, tail)
    zh, en = str(text), i18n.render(text, EN)
    for label in CAUSE_LABELS:
        assert label not in zh
    assert not rules.ZH_LABELS.search(zh) and not rules.ZH_CAUSAL.search(zh)
    assert not rules.EN_LABELS.search(en) and not rules.EN_CAUSAL.search(en)


def test_no_audio_does_not_say_nothing_was_checked_when_late_tail_audio_was(monkeypatch, tmp_path):
    """10-18 那种流：每轮收尾时 ffmpeg 吐出约 6 秒、送去识别和违禁词检测。这段时间不算实时收到直播音频，
    但不能说「这段时间没有经过检测」；收到一帧实时音频后，晚到的那几秒从头算。"""
    clock = Clock(1000.0)
    p, server, sess = _session(monkeypatch, tmp_path, clock)
    watch = _armed(sess)
    for k in range(60):                                  # 一轮收尾吐出 6 秒（60 帧）
        watch.note_tail(0.1, 1030.0 + k * 0.01)
    run(_tick(p, clock, 1000.0 + 125, T0 + 125))
    text = server.config["incidents"]["session:no_audio"]["text"]
    assert str(text) == ("已 2 分钟没有实时收到直播音频，期间只有晚到的 6 秒经过了检测。程序还在自动重试，"
                         "可以在 TikTok 上看一眼直播是否还在进行。")
    assert i18n.render(text, EN) == ("No stream audio in real time for 2 min. Only 6 sec arrived in "
                                     "that time, late, and it was checked. The app keeps retrying. You "
                                     "can check on TikTok whether the stream is still live.")
    assert "没有经过检测" not in str(text)
    assert "Nothing in that time was checked" not in i18n.render(text, EN)
    watch.note_audio(0.1, 1130.0, T0 + 130)
    assert watch.tail_since_audio == 0.0


# ---- 阶段超时 -----------------------------------------------------------------------------

def test_a_stage_over_its_budget_leaves_one_audit_row(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    watch = sess["watch"]
    sess["break_no"] = 4
    p._stage(sess, "backoff", 12.0)
    t = watch.stage.entered
    run(p._stream_watch_tick(now=(t + 13, T0)))
    run(p._stream_watch_tick(now=(t + 30, T0)))
    rows = of_type(rows_of(p.audit.path), "stage_overrun")
    assert len(rows) == 1
    assert (rows[0]["stage"], rows[0]["budget_sec"], rows[0]["break_no"]) == ("backoff", 12.0, 4)


def test_a_slow_stop_is_healed_by_closing_the_transport_and_cancelling_only_the_reader(monkeypatch,
                                                                                       tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    closed = []

    class Source:
        proc = SimpleNamespace(pid=4242, returncode=-9)
        _transport = None

        def force_close(self):
            closed.append(1)
            return True

    async def scenario():
        reader = asyncio.ensure_future(asyncio.Event().wait())
        whole = asyncio.ensure_future(asyncio.Event().wait())     # 整场的任务：不许碰
        sess["round"] = {"no": 1, "source": Source(), "reader": reader}
        p._stage(sess, "stopping", 15.0)
        t = sess["watch"].stage.entered
        await p._stream_watch_tick(now=(t + 16, T0))
        await asyncio.sleep(0)
        out = reader.cancelled(), whole.done()
        whole.cancel()
        return out

    reader_cancelled, whole_done = run(scenario())
    assert closed == [1] and reader_cancelled and not whole_done
    row = of_type(rows_of(p.audit.path), "stage_overrun")[0]
    assert row["stage"] == "stopping" and row["pid"] == 4242 and row["returncode"] == -9
    assert row["self_heal"] == {"done": True, "transport_closed": True, "reader_cancelled": True}


def test_a_heal_for_a_round_that_already_changed_does_nothing(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    p._stage(sess, "stopping", 15.0)
    sess["round"] = {"no": 2, "source": None, "reader": None}     # 这一轮已经换了
    t = sess["watch"].stage.entered
    run(p._stream_watch_tick(now=(t + 16, T0)))
    row = of_type(rows_of(p.audit.path), "stage_overrun")[0]
    assert row["self_heal"] == {"done": False, "why": "round_changed"}


def test_a_clock_gap_moves_the_stage_and_silence_clocks(monkeypatch, tmp_path):
    """Windows 睡眠或事件循环被卡住：两个钟一起多走了 600 秒。阶段计时和无声计时往后挪，醒来不误报。"""
    p, server, sess = _session(monkeypatch, tmp_path)
    watch = _armed(sess, mono0=500.0)
    p._stage(sess, "backoff", 12.0)
    watch.stage.entered = entered = 505.0          # 和这条用例的单调时钟对齐：跳变之前进的阶段
    sess["clock"] = (T0, 500.0)
    run(p._check_clock_gap(10, now=(T0 + 610.0, 1110.0)))
    assert watch.stage.entered == pytest.approx(entered + 600)
    assert watch.first_spawn[0] == pytest.approx(1100.0)
    # macOS 合盖：单调时钟没走，不挪
    sess["clock"] = (T0 + 610.0, 1110.0)
    run(p._check_clock_gap(10, now=(T0 + 2000.0, 1120.0)))
    assert watch.first_spawn[0] == pytest.approx(1100.0)


def test_a_frame_heard_after_the_wake_is_not_moved_into_the_future(monkeypatch, tmp_path):
    """两个钟一起多走了 600 秒（事件循环卡住、Windows 睡眠）。醒来时 reader 比统计循环先跑：几帧缓冲的音频
    已经按醒来之后的单调时钟记下了。对账往后挪的只该是跳变之前的起点——挪过头的话无声计时是负数，
    之后真没声音了，「已 N 分钟没收到」要晚一整段跳变才出。"""
    p, server, sess = _session(monkeypatch, tmp_path)
    watch = sess["watch"]
    watch.first_spawn = (900.0, T0 - 100)
    sess["clock"] = (T0, 1000.0)                         # 上一跳统计循环
    watch.note_audio(0.1, 1610.0, T0 + 610)              # 醒来那一刻 reader 先收到一帧
    watch.stage.enter("stopping", 12.0)
    watch.stage.entered = 1609.0                         # 醒来之后才进的阶段
    run(p._check_clock_gap(10, now=(T0 + 610, 1610.0)))
    assert watch.last_audio[0] == pytest.approx(1610.0)
    assert watch.stage.entered <= 1610.0
    assert watch.first_spawn[0] == pytest.approx(1500.0)  # 跳变之前的起点照样挪 600 秒
    assert watch.silent_for(1610.0 + 300) == pytest.approx(300.0)
    run(p._stream_watch_tick(now=(1610.0 + 300, T0 + 910)))
    assert "session:no_audio" in server.config["incidents"]


class SlowComments:
    """弹幕停下要等一会儿（子进程收尾最多十来秒）：_end_session 卡在这里时统计循环还在跳。
    要在事件循环里建：3.9 的 asyncio.Event() 构造时就去取事件循环（前面的用例跑过 asyncio.run 之后，
    循环外取不到会抛 RuntimeError；取到了也是另一个循环）。生产代码里的这类对象都是协程里才建。"""
    comments_received = 0

    def __init__(self):
        asyncio.get_running_loop()       # 循环外建：每个版本上都当场抛，不只 3.9
        self.gate = asyncio.Event()

    async def stop(self):
        await self.gate.wait()


@pytest.mark.parametrize("kind", ["no_audio", "coverage"])
def test_a_natural_end_does_not_bring_back_the_notices_it_took_down(monkeypatch, tmp_path, kind):
    """直播自然结束（状态 4、等房间超时、断网放弃……）：_end_session 先撤持续提示，再等弹幕停下，最后才取消
    统计循环。中间统计循环跳一下，不能把「已 N 分钟没收到直播音频」或覆盖率那条又挂回去——它们带
    session: 前缀，会一直挂到下一场开始，和「监听已停止」摆在一起。"""
    clock = Clock(1000.0)
    p, server, sess = _session(monkeypatch, tmp_path, clock)
    watch = sess["watch"]
    if kind == "no_audio":
        key, mono = "session:no_audio", 1125.0
        _armed(sess)
        ticks = [mono]
    else:
        key, mono = "session:audio_rate", 1260.0
        watch.first_spawn = (1000.0, T0)
        watch.note_audio(60.0, 1000.0, T0)
        watch.note_audio(1.0, 1240.0, T0 + 240)          # 刚有过声音：不出无声那条
        ticks = [1250.0, mono]

    async def go():
        p.comment_source = SlowComments()
        for t in ticks:
            await _tick(p, clock, t, T0 + t - 1000.0)
        before = key in (server.config.get("incidents") or {})
        end = asyncio.ensure_future(p._end_session(p.audit, reason="offline"))
        for _ in range(20):
            await asyncio.sleep(0)
        cleared = key not in (server.config.get("incidents") or {})
        await _tick(p, clock, mono + 10, T0 + mono - 990.0)   # 等弹幕停下时跳了一下
        p.comment_source.gate.set()
        await end
        return before, cleared

    before, cleared = run(go())
    assert before and cleared
    assert key not in (server.config.get("incidents") or {})
    assert p.audit is None


def test_a_failing_check_does_not_stop_the_stats_loop(monkeypatch, tmp_path):
    """统计循环抛任何异常都会整个退出，而它是唯一能发现卡住的地方：新检查各自兜住。"""
    p, server, sess = _session(monkeypatch, tmp_path)
    snaps = []

    async def boom(*a, **k):
        raise RuntimeError("坏了")

    real_snapshot = p.telemetry.snapshot

    def snapshot():
        snaps.append(1)
        if len(snaps) == 4:
            raise asyncio.CancelledError
        return real_snapshot()

    monkeypatch.setattr(p, "_check_stage_overrun", boom)
    monkeypatch.setattr(p, "_check_no_audio", boom)
    monkeypatch.setattr(p.telemetry, "snapshot", snapshot)
    with pytest.raises(asyncio.CancelledError):
        run(p._stats_loop(interval=0))
    assert len(snaps) == 4


# ---- 一轮拉流里的接线 ------------------------------------------------------------------------

class Silence:
    def transcribe(self, pcm):
        return SimpleNamespace(text="", raw_text="", language="es", rejected=[])


class Words:
    def transcribe(self, pcm):
        return SimpleNamespace(text="hola", raw_text="hola", language="es", rejected=[])


def scripted_source(live=0, tail=0, facts=None):
    """live 帧直播音频，然后进入收尾（stopping）再吐 tail 帧。facts 给 stop_facts()。"""
    class Source:
        STALL_SEC = 20.0

        def __init__(self, media, denoise_model=None):
            self.stalled = False
            self.stopping = False
            self.tail_bytes = 0
            self.proc = SimpleNamespace(returncode=None)
            self.on_stage = None

        async def frames(self):
            if self.on_stage:
                self.on_stage("spawned")
            for _ in range(live):
                yield b"\x00" * FRAME_BYTES
            self.stopping = True
            if self.on_stage:
                self.on_stage("stopping")
            for _ in range(tail):
                self.tail_bytes += FRAME_BYTES
                yield b"\x01" * FRAME_BYTES

        def stderr_tail(self):
            return ""

        def stop_facts(self):
            return dict(facts or {}, tail_bytes=self.tail_bytes)

        async def stop(self, urgent=False):
            self.proc.returncode = -9

    return Source


class EveryTenFrames:
    def __init__(self, *a, **k):
        self.n = 0
        self.silence_rms = 300.0

    def feed(self, frame):
        self.n += 1
        return [frame * 10] if self.n % 10 == 0 else []

    def flush(self):
        return []


def _round(p, sess, transcriber):
    async def scenario():
        loop = asyncio.get_running_loop()
        return await p._stream_session("http://cdn/s.flv", transcriber, None, loop, sess=sess)
    return run(scenario())


def test_tail_audio_is_transcribed_but_is_not_live_audio(monkeypatch, tmp_path):
    """收尾时 ffmpeg 吐出的音频：送去识别（审计里那段带 tail），但这一轮仍算「一帧直播音频都没有」——
    不宣布直播中、不写 stream_resumed、音频秒数是 0，退避和无声计数不被它清掉。"""
    p, server, sess = _session(monkeypatch, tmp_path)
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", scripted_source(live=0, tail=20))
    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", EveryTenFrames)
    sess["deaf_since"] = time.time() - 30
    got_audio, secs = _round(p, sess, Words())
    assert (got_audio, secs) == (False, 0.0)
    assert ("live", "") not in server.statuses
    rows = rows_of(p.audit.path)
    assert of_type(rows, "stream_resumed") == []
    segs = of_type(rows, "segment")
    assert len(segs) == 2 and all(s.get("tail") is True for s in segs)
    brk = of_type(rows, "stream_break")[0]
    assert brk["got_audio"] is False and brk["tail_bytes"] == 20 * FRAME_BYTES
    assert sess["watch"].last_audio is None
    assert sess["watch"].tail_bytes == 20 * FRAME_BYTES
    assert sess["watch"].tail_since_audio == pytest.approx(20 * app.audio.FRAME_SEC)   # 提示里分开说


def test_live_frames_stay_unmarked_and_move_the_stages(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    monkeypatch.setattr(app.audio, "FFmpegAudioSource",
                        scripted_source(live=20, facts={"stop_path": "kill", "stop_ms": 5003}))
    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", EveryTenFrames)
    seen = []
    real_enter = sess["watch"].stage.enter

    def enter(name, budget, **extra):
        seen.append(name)
        real_enter(name, budget, **extra)

    sess["watch"].stage.enter = enter
    got_audio, secs = _round(p, sess, Words())
    assert got_audio and secs == pytest.approx(2.0)
    rows = rows_of(p.audit.path)
    assert all("tail" not in s for s in of_type(rows, "segment"))
    brk = of_type(rows, "stream_break")[0]
    assert (brk["break_no"], brk["stop_path"], brk["stop_ms"]) == (1, "kill", 5003)
    assert seen == ["spawn", "first_frame", "streaming", "stopping", "drain", OTHER]
    assert sess["watch"].audio_sec == pytest.approx(2.0)
    assert sess["break_no"] == 1 and sess["round"] is None


def test_an_old_round_finishing_late_leaves_the_new_sessions_queue_alone(monkeypatch, tmp_path):
    """以前卡死的旧任务永远不结束；修好之后它晚几秒才走完收尾，第一次真的会跑进新一场。"""
    p, server, sess = _session(monkeypatch, tmp_path)

    class Source(scripted_source(live=5)):
        async def stop(self, urgent=False):
            p._trans_queue = marker            # 收尾这几秒里，新一场已经开始了
            p.telemetry.translation_queue_depth = 3

    marker = object()
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", Source)
    _round(p, sess, Silence())
    assert p._trans_queue is marker
    assert p.telemetry.translation_queue_depth == 3


def _live_session(monkeypatch, tmp_path, transcriber, v2):
    """走 start_stream 的整场桩件：每轮 20 帧直播音频（两段字幕）后读到流尾，流地址一直查得到，每条翻译要 3 秒。
    返回 SimpleNamespace(p, server, resolves, translating, stopped)。

    stopped 里放进东西之后，桩件再被调到（拉流、查流地址）就抛错。停止被吞掉时直播任务会一直连下去，asyncio.run
    收尾时取消它也会被吞，测试就卡到 pytest-timeout（CI 上连同后面的用例一起结束）；这样它以出错结束，测试在
    断言上红，不卡死。用例在 scenario 的 finally 里放：等不到等待点、提前失败时也要放。"""
    from app import reconnect_policy, settings

    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", v2)
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False)
    server = RecordingServer()
    p = Pipeline(args, server)

    async def noop(*a, **k):
        return None

    p.run_selfcheck = noop
    p.ensure_local_translator = noop
    p._login_source = noop
    monkeypatch.setattr(app.asr, "create_transcriber", lambda **kw: transcriber)
    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", EveryTenFrames)
    stopped = []

    class Source(scripted_source(live=20)):
        async def frames(self):
            if stopped:
                raise RuntimeError("点了停止之后还在拉流")
            async for frame in super().frames():
                yield frame

    monkeypatch.setattr(app.audio, "FFmpegAudioSource", Source)
    resolves = []

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        resolves.append(url)
        if stopped:
            raise RuntimeError("点了停止之后还在查流地址")
        return "http://cdn/s.flv"

    async def reachable(*a, **k):
        return True, ""

    monkeypatch.setattr(resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
    orig_emit = p._emit_original
    translating = []

    async def emit(result, *a, **k):
        await orig_emit(result, *a, **k)
        return {"id": p._counter, "text": "hola"}

    async def slow_translate(job):
        translating.append(job["id"])
        await asyncio.sleep(3)

    p._emit_original = emit
    p._translate_and_update = slow_translate
    return SimpleNamespace(p=p, server=server, resolves=resolves, translating=translating,
                           stopped=stopped)


def _stage_seen(p, seen):
    """此刻这一场的阶段名；顺手记进 seen（去掉连续重复），等不到时写进失败信息。"""
    sess = getattr(p, "_session_state", None)
    watch = sess.get("watch") if isinstance(sess, dict) else None
    stage = watch.stage.name if watch is not None else None
    if not seen or seen[-1] != stage:
        seen.append(stage)
    return stage


def _pending_drains(p):
    sess = getattr(p, "_session_state", None)
    if not isinstance(sess, dict):
        return []
    return [t for t in sess.get("translation_drains", ()) if not t.done()]


async def _stop_and_look(live, task, drains=()):
    """点停止。stop_stream 一返回就在协程里看任务和后台排空（asyncio.run 退出时会把没结束的任务全部取消，放到
    run() 外面看什么都钉不住），再等 0.5 秒，看停止之后还查没查流地址、界面改没改回「直播中」。"""
    before = (len(live.resolves), len(live.server.statuses))
    t0 = time.monotonic()
    await live.p.stop_stream()
    took = time.monotonic() - t0
    got = {"took": took, "done": task.done(), "drains": len(drains),
           "drains_left": [t for t in drains if not t.done()]}
    await asyncio.sleep(0.5)
    got["resolves_after"] = len(live.resolves) - before[0]
    got["statuses_after"] = [state for state, _ in live.server.statuses[before[1]:]]
    return got


def _assert_the_stop_took(got, tmp_path):
    assert got["done"] and got["took"] < 1.0
    assert got["drains_left"] == []                         # 后台排空跟着停止取消了，没有等它跑完
    assert got["resolves_after"] == 0                       # 停止之后不再查流地址
    after = got["statuses_after"]
    assert "live" not in after and after[-1] == "idle"      # 也不再把界面改回「直播中」
    ends = of_type(audit_rows(tmp_path), "session_end")
    assert [e["reason"] for e in ends] == ["user_stop"]


def test_the_stop_during_the_translation_wait_is_not_swallowed(monkeypatch, tmp_path):
    """一轮自然结束后，在途翻译还要再跑一会儿。以前 run_workers 在这一轮里最多等 5 秒让它跑完，那 5 秒里点
    「停止」，取消被 except (TimeoutError, CancelledError) 接住，重连循环又去查流地址、把界面改回「直播中」。

    #131（专家审查 B3a）之后这一轮不再等翻译：识别排空做完就返回，等翻译的事交给后台任务（登记在
    sess["translation_drains"]），重连循环接着退避、再用原来的流地址连下一轮。drain 阶段只剩识别排空那
    几毫秒（本机实测每轮中位数约 2 毫秒），以前「等到 drain 阶段再停」的写法要靠 10 毫秒一次的轮询碰巧
    落进去，CI 上碰不上就一直转到 pytest-timeout。现在等的是新的等待点：后台排空还在等在途翻译、重连
    循环在退避里（V2：有声一轮之后先退避 2 秒，所以钉在 V2 上）。这时点停止：1 秒内生效、后台排空跟着
    取消、不再查流地址、不把界面改回「直播中」、session_end 记 user_stop。等不到这个等待点就直接失败，
    不卡死。"""
    live = _live_session(monkeypatch, tmp_path, Words(), v2=True)
    p, seen = live.p, []

    def at_the_wait():
        stage = _stage_seen(p, seen)
        return bool(live.translating) and stage == "backoff" and bool(_pending_drains(p))

    def where():
        return "翻译已开始 {} 条、后台排空 {} 个、看到的阶段 {}".format(
            len(live.translating), len(_pending_drains(p)), seen[-12:])

    async def scenario():
        try:
            await p.start_stream(ROOM)
            task = p._stream_task
            # 第一轮读到流尾、识别做完，这一轮已经返回：翻译还在后台排空里跑，重连循环在退避里等
            await poll_until(at_the_wait, 10.0, where)
            await asyncio.sleep(0.2)
            assert at_the_wait(), where()                 # 还在这个等待点里（退避 2 秒、翻译 3 秒）
            return await _stop_and_look(live, task, _pending_drains(p))
        finally:
            live.stopped.append(True)

    got = run(scenario())
    assert got["drains"] >= 1
    _assert_the_stop_took(got, tmp_path)


def test_stop_writes_the_sessions_summary_when_the_old_task_is_slow(monkeypatch, tmp_path):
    """旧任务过了宽限期（识别线程停不下来）：_stop_locked 代写 session_end，也要带上这一场的汇总。"""
    p, server, sess = _session(monkeypatch, tmp_path)
    monkeypatch.setattr(Pipeline, "STOP_GRACE_SEC", 0.05)
    sess["break_no"] = 7
    sess["watch"].audio_sec = 12.5

    async def scenario():
        release = asyncio.Event()

        async def stubborn():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
            await release.wait()             # 识别线程停不下来：取消之后还在等

        task = p._stream_task = asyncio.ensure_future(stubborn())
        await asyncio.sleep(0)
        await p.stop_stream()
        release.set()
        await task

    run(scenario())
    end = of_type(audit_rows(tmp_path), "session_end")[0]
    assert end["reason"] == "user_stop"
    assert end["breaks"] == 7 and end["audio_sec"] == 12.5 and "monitored_sec" in end
    assert "asr_risk" in end


def test_session_records_carry_the_stop_limits_and_the_summary(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    rounds = iter([(True, 60.0)])

    async def one_round(media, *a, sess=None, **k):
        sess["break_no"] += 1
        return next(rounds, (False, 0.0))     # 播了一分钟，之后一帧都没有：去查，查到已结束

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        if not getattr(fake_resolve, "done", False):
            fake_resolve.done = True
            return "http://cdn/s.flv"
        raise resolver.ResolveError("没开播", kind="offline", status=4)

    async def reachable(*a, **k):
        return True, ""

    monkeypatch.setattr(resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
    monkeypatch.setattr(p, "_stream_session", one_round)
    run(p._run_stream_inner(ROOM))
    rows = audit_rows(tmp_path)
    start = of_type(rows, "session_start")[0]
    assert start["stop_limits"]["term"] == app.audio.FFmpegAudioSource.TERM_GRACE
    assert start["stop_limits"]["stream_limit"] >= 4 * 1024 * 1024
    end = of_type(rows, "session_end")[0]
    assert end["reason"] == "offline"
    for key in ("breaks", "audio_sec", "monitored_sec", "max_deaf_sec", "stage_overruns",
                "no_audio_notices", "tail_sec_total"):
        assert key in end, key
    assert end["breaks"] == 2                   # 有声一轮、用原地址又连一轮（V2），然后查到已结束
    assert start["reconnect_policy"] == "v2" and start["lookup_bucket"] == [6, 180.0]
    assert end["lookups"] == 2 and end["reused"] == 1
    assert end["requests"]["lookups"] == 2 and end["requests"]["room_rechecks"] == 0


def test_the_lookup_budget_is_the_sum_of_the_layer_timeouts():
    """lookup 的预算按一次 resolve_stream_url 算，由各层超时相加得出——不是历史上最长的那一次
    （37.6 秒、57.8 秒是单次尝试的耗时）。层超时改了，预算跟着变。"""
    one = resolver.lookup_worst_case_sec(1)
    two = resolver.lookup_worst_case_sec(2)
    assert one > 57.8 and two > one
    assert resolver.lookup_worst_case_sec(0) >= (resolver.LOGIN_GAP_MAX_WAIT_SEC
                                                 + resolver.WEBKIT_TIMEOUT_SEC
                                                 + resolver.YTDLP_TIMEOUT_SEC)
