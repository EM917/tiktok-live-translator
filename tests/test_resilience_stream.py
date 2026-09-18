"""拉流这一侧的韧性：电脑休眠、房间状态不是 2/4、断网、断流证据、音频到达率、直连地址。

每个用例对应 resilience-confirmed.json 里一条已核实的缺口（1/15/21/23 是同一件事：
休眠与 App Nap）。全部用桩件驱动：不联网、不起 ffmpeg / caffeinate / 浏览器、不加载模型。
"""
import asyncio
import json
import socket
import time
from types import SimpleNamespace

import pytest

import app.asr
import app.audio
from app import pipeline as pipeline_mod
from app import power, resolver
from app.audio import FRAME_BYTES, AudioFlowMeter, frame_rms
from app.audit import AuditLog
from app.pipeline import Pipeline
from app.redact import strip_query
from app.resolver import ResolveError
from app.server import CaptionServer
from tests.helpers import run

ROOM = "https://www.tiktok.com/@bellaallnatural/live"
ROOM2 = "https://www.tiktok.com/@elisa/live"
DIRECT = "https://pull-flv-l1.tiktokcdn.com/stage/stream-1.flv"
SIGNED = DIRECT + "?expire=1790000000&sign=SECRETSIGN"
SILENT = b"\x00" * FRAME_BYTES
# CLAUDE.md 第八条：界面文字、日志只写观察和能做的事，不贴原因标签
CAUSE_LABELS = ("年龄", "限流", "封禁", "多半", "暂停", "断线", "被挡")


class RecordingServer(CaptionServer):
    """真的 CaptionServer（持续提示要落进 config），外加把每条消息记下来。"""

    def __init__(self):
        super().__init__(port=0)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(msg)
        await super().broadcast(msg)

    @property
    def statuses(self):
        return [(m["state"], m.get("detail", "")) for m in self.messages
                if m.get("type") == "status"]

    def incidents(self, key):
        return [m for m in self.messages if m.get("type") == "incident" and m.get("id") == key]


def make_pipeline(monkeypatch, tmp_path):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False,
    )
    server = RecordingServer()
    p = Pipeline(args, server)

    async def noop(*a, **k):
        return None

    p.run_selfcheck = noop
    p.ensure_local_translator = noop
    monkeypatch.setattr(app.asr, "create_transcriber", lambda **kw: object())
    real_sleep = asyncio.sleep
    monkeypatch.setattr(pipeline_mod.asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    return p, server


def rows_of(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def audit_rows(tmp_path):
    rows = []
    for f in sorted((tmp_path / "logs").glob("session-*.jsonl")):
        rows.extend(rows_of(f))
    return rows


def of_type(rows, kind):
    return [r for r in rows if r.get("type") == kind]


def scripted_resolve(monkeypatch, script):
    """resolve_stream_url 的桩：按剧本依次返回地址或抛出 ResolveError。"""
    calls = []
    items = iter(script)

    async def fake(url, cookies=None, cookies_browser="auto", trace=None):
        calls.append(url)
        item = next(items)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(resolver, "resolve_stream_url", fake)
    return calls


def assert_no_cause_labels(texts):
    for text in texts:
        for label in CAUSE_LABELS:
            assert label not in text, (label, text)


# ---- 凭证不进日志 --------------------------------------------------------------

def test_strip_query_removes_signed_parts_everywhere():
    text = "Error opening input {}: Server returned 403 | again {}".format(SIGNED, SIGNED)
    out = strip_query(text)
    assert "SECRETSIGN" not in out and "expire=" not in out
    assert out.count("pull-flv-l1.tiktokcdn.com/stage/stream-1.flv") == 2
    assert strip_query(None) == ""
    assert strip_query("x" * 50, 10) == "x" * 10


# ---- 防空闲睡眠（1/15/21/23）--------------------------------------------------------

class FakeProc:
    def __init__(self):
        self.rc = None
        self.terminated = False

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated = True
        self.rc = -15

    def wait(self, timeout=None):
        return self.rc

    def kill(self):
        self.rc = -9


class FakeProcessInfo:
    def __init__(self):
        self.begun, self.ended = [], []

    def beginActivityWithOptions_reason_(self, options, reason):
        self.begun.append((options, reason))
        return "activity-token"

    def endActivity_(self, token):
        self.ended.append(token)


def test_mac_guard_holds_idle_sleep_and_app_nap_and_releases_both_once():
    spawned, proc, info = [], FakeProc(), FakeProcessInfo()

    def popen(cmd, **kw):
        spawned.append(cmd)
        return proc

    guard = power.SleepGuard("直播合规监听中").acquire(
        platform="darwin", popen=popen, pid=4242, process_info=lambda: info)
    # -i 只挡空闲睡眠（不挡屏幕变暗，也挡不住合盖）；-w 让它随本程序退出
    assert spawned == [["/usr/bin/caffeinate", "-i", "-w", "4242"]]
    assert guard.held == ["caffeinate -i", "app_nap_opt_out"]
    options = info.begun[0][0]
    assert options & (1 << 20) == 0            # activity 本身不挡空闲睡眠，只为不被 App Nap
    guard.release()
    guard.release()                            # 可重入
    assert proc.terminated and info.ended == ["activity-token"]


def test_mac_guard_degrades_quietly_when_nothing_is_available():
    def popen(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    def no_foundation():
        raise ImportError("Foundation")

    guard = power.SleepGuard().acquire(platform="darwin", popen=popen,
                                       process_info=no_foundation)
    assert guard.held == []
    guard.release()


def test_windows_guard_sets_and_clears_the_thread_execution_state():
    calls = []

    class Kernel32:
        def SetThreadExecutionState(self, flags):
            calls.append(flags)
            return 0x80000000

    guard = power.SleepGuard().acquire(platform="win32", kernel32=Kernel32)
    assert guard.held == ["SetThreadExecutionState"]
    guard.release()
    assert calls == [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED, power.ES_CONTINUOUS]


def test_other_platforms_hold_nothing_and_tests_never_touch_the_system():
    assert power.SleepGuard().acquire(platform="linux").held == []
    guard = power.hold()                       # pytest 下什么也不拿
    assert guard.held == [] and guard._proc is None


class FakeGuard:
    def __init__(self):
        self.held = ["fake-guard"]
        self.owner = None
        self.released = 0

    def release(self):
        self.released += 1


class FakeAudit:
    def __init__(self):
        self.closed = []

    def close(self, reason=None, **fields):
        self.closed.append(reason)


def test_start_stream_guard_belongs_to_its_task_and_every_stop_releases_it(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    guards = []

    def hold(reason=""):
        guards.append(FakeGuard())
        return guards[-1]

    monkeypatch.setattr(power, "hold", hold)

    async def run_stream(url):
        await asyncio.Event().wait()

    monkeypatch.setattr(p, "_run_stream", run_stream)

    async def scenario():
        await p.start_stream(ROOM)
        first = guards[0]
        assert first.owner is p._stream_task and p._sleep_guard is first
        await p.start_stream(ROOM2)                    # 换直播间
        assert first.released == 1
        assert guards[1].owner is p._stream_task
        await p.handle_control({"type": "stop"})
        assert guards[1].released == 1 and p._sleep_guard is None

    run(scenario())


def test_a_late_old_task_cannot_release_the_new_sessions_guard():
    p = Pipeline.__new__(Pipeline)
    p.server = RecordingServer()
    p._stats_task = None

    async def scenario():
        new_task = asyncio.ensure_future(asyncio.Event().wait())
        guard = FakeGuard()
        guard.owner = new_task
        p._sleep_guard = guard
        old_audit, new_audit = FakeAudit(), FakeAudit()
        p.audit = new_audit
        await p._end_session(old_audit, reason="cancelled")      # 旧任务晚到的收尾
        assert guard.released == 0 and old_audit.closed == ["cancelled"]
        p.audit = old_audit       # 哪怕看起来还是「当前会话」，别的任务开的断言也不归它放
        await p._end_session(old_audit)
        new_task.cancel()
        return guard

    guard = run(scenario())
    assert guard.released == 0 and p._sleep_guard is guard


def test_natural_end_releases_the_guard_and_the_audit_says_what_was_held(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    guard = FakeGuard()
    monkeypatch.setattr(power, "hold", lambda reason="": guard)
    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=4)])

    async def one_round(media, *a, **k):
        return True, 60.0

    monkeypatch.setattr(p, "_stream_session", one_round)

    async def scenario():
        await p.start_stream(ROOM)
        await p._stream_task

    run(scenario())
    assert guard.released == 1
    rows = audit_rows(tmp_path)
    assert of_type(rows, "session_start")[0]["sleep_guard"] == ["fake-guard"]
    end = of_type(rows, "session_end")[0]
    assert end["reason"] == "offline" and end["status"] == 4


def test_a_failure_while_setting_up_the_session_releases_the_guard_and_closes_the_audit(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    guard = FakeGuard()
    monkeypatch.setattr(power, "hold", lambda reason="": guard)

    async def broken(*a, **k):
        raise RuntimeError("watchlist broke")

    monkeypatch.setattr(p, "_publish_watchlist", broken)     # 审计已建好、统计循环已起之后才出错

    async def scenario():
        await p.start_stream(ROOM)
        await p._stream_task

    run(scenario())
    assert guard.released == 1 and p._sleep_guard is None
    assert p._stats_task is None and p.audit is None
    assert server.statuses[-1][0] == "error" and "内部错误" in server.statuses[-1][1]
    rows = audit_rows(tmp_path)
    err = of_type(rows, "internal_error")
    assert len(err) == 1 and err[0]["message"] == "watchlist broke"
    assert rows[-1]["type"] == "session_end" and rows[-1]["reason"] == "internal_error"


def test_a_failure_before_the_audit_exists_still_releases_the_guard(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    guard = FakeGuard()
    monkeypatch.setattr(power, "hold", lambda reason="": guard)

    def unreadable(path=None):
        raise OSError("banned_terms.txt unreadable")

    monkeypatch.setattr(pipeline_mod, "load_detector", unreadable)

    async def scenario():
        await p.start_stream(ROOM)
        await p._stream_task

    run(scenario())
    assert guard.released == 1 and p._sleep_guard is None
    assert server.statuses[-1][0] == "error"
    assert audit_rows(tmp_path) == []


# ---- 时钟对账：电脑休眠 / 程序没在运行（1/15/21/23）------------------------------------

def _gap_pipeline(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    return p, server, sess


T0 = 1790000000.0


def test_clock_gap_with_a_stopped_monotonic_clock_is_recorded_and_stays_on_screen(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)
    sess["clock"] = (T0, 500.0)
    # macOS 合盖 23 分钟：墙钟走了 10 + 1380 秒，单调时钟只走了 10 秒
    run(p._check_clock_gap(10, now=(T0 + 1390.0, 510.0)))
    gap = of_type(rows_of(p.audit.path), "clock_gap")[0]
    assert gap["clocks_diverged"] is True
    assert gap["gap_sec"] == 1380.0 and gap["wall_sec"] == 1390.0 and gap["mono_sec"] == 10.0
    assert gap["from"] and gap["to"]
    text = server.config["incidents"]["session:clock_gap"]["text"]
    assert "电脑休眠或挂起了约 23 分钟" in text and "没有监听" in text
    assert "接电源" in text
    assert sess["gap"] is not None and sess["gap"]["diverged"] is True
    assert_no_cause_labels([text])


def test_clock_gap_on_a_clock_that_keeps_counting_only_says_the_program_did_not_run(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)
    sess["clock"] = (T0, 500.0)
    # Windows 休眠或事件循环卡住：两个钟一起多走了 60 秒
    run(p._check_clock_gap(10, now=(T0 + 70.0, 570.0)))
    gap = of_type(rows_of(p.audit.path), "clock_gap")[0]
    assert gap["clocks_diverged"] is False and gap["gap_sec"] == 60.0
    text = server.config["incidents"]["session:clock_gap"]["text"]
    assert "程序约 60 秒没有运行" in text and "休眠或挂起了" not in text
    assert sess["gap"]["diverged"] is False


def test_a_normal_tick_or_someone_elses_session_records_nothing(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)
    sess["clock"] = (T0, 500.0)
    run(p._check_clock_gap(10, now=(T0 + 12.0, 512.0)))
    p._session_state = p._new_session_state(object())       # 不是当前这份审计的状态
    run(p._check_clock_gap(10, now=(T0 + 5000.0, 520.0)))
    assert of_type(rows_of(p.audit.path), "clock_gap") == []
    assert "session:clock_gap" not in (server.config.get("incidents") or {})


def test_after_a_sleep_a_source_with_no_fresh_audio_is_dropped_at_once(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)
    stops = []

    class Source:
        async def stop(self):
            stops.append(1)

    async def scenario():
        sess["clock"] = (T0, 500.0)
        sess["source"], sess["last_frame_wall"] = Source(), T0
        await p._check_clock_gap(10, now=(T0 + 610.0, 510.0))
        await asyncio.sleep(0)
        # 事件循环卡住（两个钟一致）时不断开：数据还在管道里排着
        sess["gap_stop"] = False
        sess["source"], sess["last_frame_wall"] = Source(), T0 + 610.0
        await p._check_clock_gap(10, now=(T0 + 700.0, 600.0))
        await asyncio.sleep(0)

    run(scenario())
    assert stops == [1] and sess["gap_stop"] is False


def test_the_extra_clock_check_only_trusts_a_reading_from_a_running_stats_loop(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)

    async def scenario():
        sess["clock"] = (time.time() - 5000.0, time.monotonic() - 5000.0)
        p._stats_task = None                      # 统计循环没在跑：上一次读数是旧的，对不出结果
        await p._recheck_clock(sess)
        assert sess["gap"] is None
        p._stats_task = asyncio.ensure_future(asyncio.Event().wait())
        await p._recheck_clock(p._new_session_state(p.audit))    # 别的会话状态：不碰
        assert sess["gap"] is None
        await p._recheck_clock(sess)
        p._stats_task.cancel()

    run(scenario())
    gaps = of_type(rows_of(p.audit.path), "clock_gap")
    assert len(gaps) == 1 and gaps[0]["clocks_diverged"] is False


def test_stats_loop_calls_the_clock_check_every_tick(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    ticks = []

    async def check(interval, now=None):
        ticks.append(interval)
        if len(ticks) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(p, "_check_clock_gap", check)
    with pytest.raises(asyncio.CancelledError):
        run(p._stats_loop(interval=10))
    assert ticks == [10, 10, 10]


# ---- 「已结束」判定：只认状态 4；休眠之后先复查（1、5）------------------------------------

def test_offline_right_after_a_clock_gap_is_rechecked_once_before_ending(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions, probes = [], []

    async def fake_session(media, *a, sess=None, **k):
        sessions.append(media)
        if len(sessions) == 1:     # 这一轮里电脑睡过（单调时钟停住了）
            sess["gap"] = {"from": T0, "to": T0 + 1390, "sec": 1380.0, "diverged": True}
        return True, 60.0

    async def probe(url):
        probes.append(url)
        return 2, ""

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=4),
                                   "http://cdn/b.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    assert sessions == ["http://cdn/a.flv", "http://cdn/b.flv"]   # 复查说在播：接着监听
    assert probes == [ROOM]                                        # 只多问了一次房间接口
    rechecks = [d for _, d in server.statuses if "再查一次" in d]
    assert rechecks == ["电脑刚从休眠或挂起中恢复，TikTok 接口返回房间状态 4（已结束）；"
                        "30 秒后再查一次再下结论…"]
    assert server.statuses[-1][0] == "ended"                       # 第二次（没有休眠）才收手
    rows = audit_rows(tmp_path)
    waits = of_type(rows, "host_wait")
    assert [(w["trigger"], w["status"], w["outcome"]) for w in waits] == [("clock_gap", 2, "live")]
    end = of_type(rows, "session_end")[0]
    assert end["reason"] == "offline" and end["status"] == 4


def test_offline_confirmed_by_the_recheck_ends_the_session(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def fake_session(media, *a, sess=None, **k):
        sess["gap"] = {"from": T0, "to": T0 + 70, "sec": 60.0}
        return True, 60.0

    async def probe(url):
        return 4, ""

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    assert server.statuses[-1][0] == "ended" and "直播已结束" in server.statuses[-1][1]
    # 两个钟一起走多了（Windows 休眠或事件循环卡住）：不说休眠
    rechecks = [d for _, d in server.statuses if "再查一次" in d]
    assert rechecks == ["程序刚才约 60 秒没有运行，TikTok 接口返回房间状态 4（已结束）；"
                        "30 秒后再查一次再下结论…"]
    rows = audit_rows(tmp_path)
    assert [w["outcome"] for w in of_type(rows, "host_wait")] == ["ended"]
    assert of_type(rows, "session_end")[0]["reason"] == "offline"


def test_an_ended_verdict_that_beats_the_first_stats_tick_after_wake_is_still_rechecked(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def not_ticked_yet(interval=10):
        await asyncio.Event().wait()     # 醒来后统计循环的计时器还没到点，解析先回来了

    monkeypatch.setattr(p, "_stats_loop", not_ticked_yet)
    sessions, probes = [], []

    async def fake_session(media, *a, sess=None, **k):
        sessions.append(media)
        if len(sessions) == 1:           # 这一轮里合盖 23 分钟：墙钟走了，单调时钟没走
            wall, mono = sess["clock"]
            sess["clock"] = (wall - 1380.0, mono)
        return True, 60.0

    async def probe(url):
        probes.append(url)
        return 2, ""

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=4),
                                   "http://cdn/b.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    assert probes == [ROOM] and sessions == ["http://cdn/a.flv", "http://cdn/b.flv"]
    rows = audit_rows(tmp_path)
    gaps = of_type(rows, "clock_gap")
    assert len(gaps) == 1 and gaps[0]["clocks_diverged"] is True
    assert [(w["trigger"], w["outcome"]) for w in of_type(rows, "host_wait")] == [("clock_gap", "live")]
    assert any(d.startswith("电脑刚从休眠或挂起中恢复") for _, d in server.statuses)
    assert "session:clock_gap" in server.config["incidents"]      # 已经发生过的事留在屏幕上
    assert of_type(rows, "session_end")[0]["reason"] == "offline"


def test_an_ended_answer_while_waiting_for_the_room_right_after_a_gap_is_rechecked(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []
    answers = iter([(4, ""), (2, "")])

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return True, 60.0

    async def probe(url):
        status, why = next(answers)
        if status == 4:      # 等房间的这一分钟里电脑睡过，统计循环已经记下了跳变
            p._session_state["gap"] = {"from": T0, "to": T0 + 700, "sec": 640.0, "diverged": True}
        return status, why

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=3),
                                   "http://cdn/b.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    assert sessions == ["http://cdn/a.flv", "http://cdn/b.flv"]
    rows = audit_rows(tmp_path)
    assert [(w["trigger"], w["status"], w["outcome"]) for w in of_type(rows, "host_wait")] == [
        ("status", 3, "started"), ("clock_gap", 2, "live"), ("status", 2, "live")]
    assert of_type(rows, "session_end")[0]["reason"] == "offline"


def test_status_4_without_a_gap_ends_immediately_without_extra_requests(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def fake_session(media, *a, **k):
        return True, 60.0

    async def probe(url):
        raise AssertionError("不该再问房间接口")

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    assert server.statuses[-1][0] == "ended"


def test_a_room_status_other_than_4_waits_and_resumes_when_live_again(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []
    answers = iter([(3, ""), (3, ""), (2, "")])

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return True, 60.0

    async def probe(url):
        return next(answers)

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=3),
                                   "http://cdn/b.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    assert sessions == ["http://cdn/a.flv", "http://cdn/b.flv"]
    waiting = [d for s, d in server.statuses if "复查" in d]
    assert waiting and "TikTok 接口返回房间状态 3（不是在播状态），每 60 秒复查一次，最多 10 分钟" in waiting[0]
    assert_no_cause_labels([d for _, d in server.statuses])
    waits = of_type(audit_rows(tmp_path), "host_wait")
    assert [(w["status"], w["outcome"], w["waited_sec"]) for w in waits] == [
        (3, "started", 0), (2, "live", 180)]


def test_waiting_for_the_room_is_bounded_and_the_timeout_is_the_recorded_reason(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    probes = []

    async def fake_session(media, *a, **k):
        return True, 60.0

    async def probe(url):
        probes.append(url)
        return 3, "http=200"

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=3)])
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    assert len(probes) == 10                    # 10 分钟、每分钟一次
    assert server.statuses[-1][0] == "ended" and "监听已停止" in server.statuses[-1][1]
    assert_no_cause_labels([d for _, d in server.statuses])
    rows = audit_rows(tmp_path)
    assert of_type(rows, "host_wait")[-1]["outcome"] == "timeout"
    end = of_type(rows, "session_end")[0]
    assert end["reason"] == "host_wait_timeout" and end["status"] == 3 and end["waited_sec"] == 600


def test_first_start_on_a_room_that_is_not_live_still_fails_fast(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def probe(url):
        raise AssertionError("首次开播不等")

    scripted_resolve(monkeypatch, [ResolveError("没开播", kind="offline", status=3)])
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    run(p._run_stream_inner(ROOM))
    assert server.statuses[-1][0] == "error"
    end = of_type(audit_rows(tmp_path), "session_end")[0]
    assert end["reason"] == "resolve_error" and end["kind"] == "offline"


# ---- 解析层：原始状态值与每层的 why（5、8）-------------------------------------------------

class _Resp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, status=None, exc=None):
        self.status, self.exc = status, exc

    def get(self, url, headers=None):
        if self.exc is not None:
            raise self.exc
        return _Resp(self.status)


def _fake_yt_dlp_module(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))


def test_the_raw_room_status_travels_with_the_offline_verdict(monkeypatch):
    async def fake_json(session, url, limit=None, headers=None):
        return {"statusCode": 0, "data": {"user": {"roomId": "123", "status": 3}}}

    monkeypatch.setattr(resolver, "_get_json", fake_json)
    trace = []
    with pytest.raises(ResolveError) as exc:
        run(resolver.resolve_stream_url(ROOM, cookies_browser="none", trace=trace))
    assert exc.value.kind == "offline" and exc.value.status == 3
    assert (trace[0]["layer"], trace[0]["outcome"]) == ("官方接口", "offline")
    assert trace[0]["status"] == 3 and trace[0]["field"] == "user.status"
    assert resolver._LAYER_NOTE.get() is None          # 笔记不漏到后面的调用上


def test_webkit_and_page_offline_verdicts_carry_their_status(monkeypatch):
    async def api(url, cookies_browser="auto"):
        return None, False

    async def webkit(url, timeout):
        return {"offline": True, "status": 3}

    monkeypatch.setattr(resolver, "_resolve_via_api", api)
    monkeypatch.setattr(resolver, "_webkit_available", lambda: True)
    monkeypatch.setattr(resolver, "_run_webkit_fetch", webkit)
    with pytest.raises(ResolveError) as exc:
        run(resolver.resolve_stream_url(ROOM, cookies_browser="none"))
    assert exc.value.kind == "offline" and exc.value.status == 3

    page = ('<script id="SIGI_STATE" type="application/json">'
            + json.dumps({"LiveRoom": {"liveRoomUserInfo": {"liveRoom": {"status": 4}}}})
            + "</script>")

    async def fetch_page(url, browser=None):
        return resolver._parse_live_page(page)

    async def ytdlp(url, cookies=None, browser=None, timeout=45):
        return 1, "", "ERROR: x"

    _fake_yt_dlp_module(monkeypatch)
    monkeypatch.setattr(resolver, "_webkit_available", lambda: False)
    monkeypatch.setattr(resolver, "_run_ytdlp", ytdlp)
    monkeypatch.setattr(resolver, "_resolve_from_page", fetch_page)
    trace = []
    with pytest.raises(ResolveError) as exc:
        run(resolver.resolve_stream_url(ROOM, cookies_browser="none", trace=trace))
    assert exc.value.status == 4
    assert trace[-1]["layer"] == "直播页兜底" and trace[-1]["field"] == "liveRoom.status"


def test_room_api_failures_leave_codes_not_guesses(monkeypatch):
    async def status_with(session):
        note = resolver._fresh_note()
        got = await resolver._room_status(session, "x")
        return got, resolver._layer_note(note)

    got, why = run(status_with(_Session(status=403)))
    assert got == (None, None) and why == {"why": "http=403"}
    got, why = run(status_with(_Session(exc=ConnectionResetError("boom"))))
    assert why == {"why": "ConnectionResetError"}

    async def coded(session, url, limit=None, headers=None):
        return {"statusCode": 10202, "data": {}}

    monkeypatch.setattr(resolver, "_get_json", coded)
    got, why = run(status_with(None))
    assert why == {"why": "statusCode=10202"}

    async def roomless(session, url, limit=None, headers=None):
        return {"statusCode": 0, "data": {"user": {}}}

    monkeypatch.setattr(resolver, "_get_json", roomless)
    got, why = run(status_with(None))
    assert why == {"why": "no_roomId"}


def test_every_failed_layer_keeps_a_short_why_without_signatures(monkeypatch):
    _fake_yt_dlp_module(monkeypatch)

    async def api(url, cookies_browser="auto"):
        resolver._note("http=403")
        return None, False

    async def webkit(url, timeout):
        return {"error": "timeout (25s), last=" + json.dumps({"url": None, "err": SIGNED})}

    async def ytdlp(url, cookies=None, browser=None, timeout=45):
        return 1, "", "WARNING: x\nERROR: [TikTok] Unable to download {}: HTTP Error 403".format(SIGNED)

    async def page(url, browser=None):
        resolver._note("ClientConnectorError")
        return None, False

    monkeypatch.setattr(resolver, "_resolve_via_api", api)
    monkeypatch.setattr(resolver, "_webkit_available", lambda: True)
    monkeypatch.setattr(resolver, "_run_webkit_fetch", webkit)
    monkeypatch.setattr(resolver, "_run_ytdlp", ytdlp)
    monkeypatch.setattr(resolver, "_resolve_from_page", page)
    trace = []
    with pytest.raises(ResolveError):
        run(resolver.resolve_stream_url(ROOM, cookies_browser="none", trace=trace))
    layer = {r["layer"]: r for r in trace}
    assert layer["官方接口"]["why"] == "http=403"
    assert layer["WebKit"]["why"].startswith("timeout (25s)")
    assert "HTTP Error 403" in layer["yt-dlp匿名"]["why"]
    assert layer["直播页兜底"]["why"] == "ClientConnectorError"
    blob = json.dumps(trace, ensure_ascii=False)
    assert "SECRETSIGN" not in blob
    assert all(len(r.get("why", "")) <= 120 for r in trace)


def test_failed_resolve_records_keep_the_message_and_reconnect_number(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    class Audit:
        def __init__(self):
            self.records = []

        def resolve(self, record):
            self.records.append(record)

    p.audit = Audit()
    scripted_resolve(monkeypatch, [ResolveError("拉不到 {}".format(SIGNED), kind="unknown")])
    with pytest.raises(ResolveError):
        run(p._resolve_media(ROOM, reconnect=3))
    rec = p.audit.records[0]
    assert rec["reconnect"] == 3 and rec["ok"] is False
    assert "拉不到" in rec["message"] and "SECRETSIGN" not in rec["message"]


# ---- 断网：不解析、不耗预算、恢复后自动继续（6）----------------------------------------------

def test_a_network_outage_pauses_resolving_without_spending_the_budget(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    probes = []

    async def reachable():
        probes.append(1)
        return (False, "gaierror") if len(probes) <= 12 else (True, "")

    sessions = []

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return True, 60.0

    resolves = scripted_resolve(monkeypatch, [
        "http://cdn/a.flv", "http://cdn/b.flv",
        ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    # 断网 11 次重探（远超 5 次重连预算）期间一次解析都没发，网络回来后接着监听
    assert len(resolves) == 3 and sessions == ["http://cdn/a.flv", "http://cdn/b.flv"]
    assert server.statuses[-1][0] == "ended"
    down_texts = [d for _, d in server.statuses if "本机连不上 www.tiktok.com" in d]
    assert down_texts and "网络恢复后自动重连" in down_texts[0]
    levels = [m["level"] for m in server.incidents("session:network")]
    assert levels[0] == "error" and levels[-1] == "clear"
    assert "session:network" not in server.config.get("incidents", {})
    rows = audit_rows(tmp_path)
    downs, ups = of_type(rows, "network_down"), of_type(rows, "network_up")
    assert len(downs) == 1 and downs[0]["why"] == "gaierror" and downs[0]["since"]
    assert len(ups) == 1 and ups[0]["down_sec"] >= 0


def test_a_network_that_never_returns_gives_up_at_the_ceiling(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.NETWORK_GIVE_UP_SEC = 60.0
    probes = []

    async def unreachable():
        probes.append(1)
        return False, "timeout"

    async def fake_session(media, *a, **k):
        return True, 60.0

    scripted_resolve(monkeypatch, ["http://cdn/a.flv"])
    monkeypatch.setattr(resolver, "tiktok_reachable", unreachable)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    assert len(probes) == 4                       # 首探 + 60 秒里每 20 秒一次
    state, detail = server.statuses[-1]
    assert state == "error" and "本机连不上 www.tiktok.com 已超过 1 分钟" in detail
    assert of_type(audit_rows(tmp_path), "session_end")[0]["reason"] == "network_down"


ONGOING = ("session:network", "session:audio_rate", "session:quiet_audio")


def test_stopping_during_an_outage_takes_down_the_banners_that_promise_more_listening(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    calls = []

    async def scenario():
        hang = asyncio.Event()

        async def reachable():
            calls.append(1)
            if len(calls) == 1:
                return False, "timeout"
            await hang.wait()
            return True, ""

        async def fake_session(media, *a, sess=None, **k):
            await p._on_audio_events(sess, [("low", {"audio_sec": 30.0, "wall_sec": 60.0}),
                                            ("quiet", {"audio_sec": 121.0})])
            await p._incident("session:clock_gap", "warn", "电脑休眠或挂起了约 23 分钟")
            return True, 60.0

        scripted_resolve(monkeypatch, ["http://cdn/a.flv"])
        monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
        monkeypatch.setattr(p, "_stream_session", fake_session)
        await p.start_stream(ROOM)
        for _ in range(2000):
            await asyncio.sleep(0)
            if len(calls) >= 2:
                break
        before = set(server.config.get("incidents") or {})
        await p.handle_control({"type": "stop"})
        return before

    before = run(scenario())
    assert set(ONGOING) <= before
    left = server.config.get("incidents") or {}
    assert server.statuses[-1][0] == "idle"
    assert not set(ONGOING) & set(left)
    assert "session:clock_gap" in left            # 已经发生过的事留给中控看


def test_a_session_that_ends_by_itself_takes_down_the_ongoing_banners(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def fake_session(media, *a, sess=None, **k):
        await p._on_audio_events(sess, [("low", {"audio_sec": 30.0, "wall_sec": 60.0}),
                                        ("quiet", {"audio_sec": 121.0})])
        return True, 60.0

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    assert server.incidents("session:quiet_audio")[0]["level"] == "warn"
    assert not set(ONGOING) & set(server.config.get("incidents") or {})


def test_stop_takes_the_ongoing_banners_down_even_when_the_old_task_is_slow_to_finish(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.STOP_GRACE_SEC = 0.2

    async def scenario():
        release, entered = asyncio.Event(), asyncio.Event()

        async def stuck_stream(url):
            await p._incident("session:quiet_audio", "warn", "已收到 121 秒直播音频……程序继续监听")
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()       # 识别线程一时停不下来：宽限期内走不到收尾

        monkeypatch.setattr(p, "_run_stream", stuck_stream)
        await p.start_stream(ROOM)
        await entered.wait()
        task = p._stream_task
        await p.handle_control({"type": "stop"})
        left = set(server.config.get("incidents") or {})
        release.set()
        await task
        return left

    assert "session:quiet_audio" not in run(scenario())


def test_tcp_probe_works_against_a_local_listener_only():
    async def scenario():
        srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        ok = await resolver._tcp_probe("127.0.0.1", port, 2.0)
        srv.close()
        await srv.wait_closed()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        bad = await resolver._tcp_probe("127.0.0.1", free, 2.0)
        return ok, bad

    ok, bad = run(scenario())
    assert ok == (True, "") and bad[0] is False


def test_probes_never_leave_the_machine_under_pytest():
    assert run(resolver.tiktok_reachable()) == (True, "not_probed_in_tests")
    assert run(resolver.probe_room_status(ROOM)) == (None, "not_probed_in_tests")


# ---- 断流证据与结束原因（7、22）----------------------------------------------------------

def source_factory(frames=40, frame=SILENT, stalled=False, returncode=0, tail="", hang=False):
    class Source:
        def __init__(self, media, denoise_model=None):
            self.stalled = False
            self.proc = SimpleNamespace(returncode=None)

        async def frames(self):
            for _ in range(frames):
                yield frame
            if hang:
                await asyncio.Event().wait()
            self.stalled = stalled

        def stderr_tail(self):
            return tail

        async def stop(self):
            self.proc.returncode = returncode

    return Source


class Silence:
    def transcribe(self, pcm):
        return SimpleNamespace(text="", raw_text="", language="es", rejected=[])


def _round(p, sess):
    async def scenario():
        loop = asyncio.get_running_loop()
        return await p._stream_session("http://cdn/s.flv", Silence(), None, "note", loop, sess=sess)

    return run(scenario())


def test_each_round_leaves_a_break_record_and_an_audio_summary(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", source_factory(
        frames=40, tail="[flv] Error reading {}".format(SIGNED), returncode=0))
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._new_session_state(audit)
    sess["reconnect_no"] = 2
    got_audio, secs = _round(p, sess)
    audit.close()
    rows = rows_of(audit.path)
    brk = of_type(rows, "stream_break")[0]
    assert (brk["reason"], brk["returncode"], brk["got_audio"]) == ("eof", 0, True)
    assert brk["audio_sec"] == 4.0 and brk["reconnect_no"] == 2
    assert "tiktokcdn.com/stage/stream-1.flv" in brk["stderr_tail"]
    assert "SECRETSIGN" not in json.dumps(rows, ensure_ascii=False)
    summary = of_type(rows, "stream_audio")[-1]
    assert summary["final"] is True and summary["audio_sec"] == 4.0 and summary["segments_cut"] == 0
    assert sess["deaf_since"] is not None and sess["source"] is None


def test_a_stalled_round_is_recorded_as_a_stall(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(app.audio, "FFmpegAudioSource",
                        source_factory(frames=10, stalled=True, returncode=-15))
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    _round(p, p._new_session_state(audit))
    audit.close()
    brk = of_type(rows_of(audit.path), "stream_break")[0]
    assert brk["reason"] == "stall" and brk["returncode"] == -15


def test_a_cancelled_round_is_recorded_as_cancelled(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", source_factory(frames=5, hang=True))
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._new_session_state(audit)

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            p._stream_session("http://cdn/s.flv", Silence(), None, "note", loop, sess=sess))
        for _ in range(50):
            await asyncio.sleep(0)
            if sess["last_frame_wall"] is not None:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    audit.close()
    brk = of_type(rows_of(audit.path), "stream_break")[0]
    assert brk["reason"] == "cancelled" and brk["got_audio"] is True


def test_the_next_round_records_how_long_nothing_was_heard(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", source_factory(frames=10))
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._new_session_state(audit)
    _round(p, sess)
    assert of_type(rows_of(audit.path), "stream_resumed") == []    # 第一轮不算「恢复」
    sess["deaf_since"] = time.time() - 42.0
    sess["reconnect_no"] = 1
    sess["gap"] = {"sec": 100.0}
    _round(p, sess)
    audit.close()
    resumed = of_type(rows_of(audit.path), "stream_resumed")
    assert len(resumed) == 1 and resumed[0]["deaf_sec"] >= 41.5 and resumed[0]["reconnect_no"] == 1
    assert sess["gap"] is None                     # 收到音频之后，之前的时钟跳变不再影响判定


def test_reconnect_resolves_are_numbered_and_budget_exhaustion_is_the_end_reason(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def empty_round(media, *a, **k):
        return False, 0.0

    scripted_resolve(monkeypatch, ["http://cdn/dead.flv"] * 6)
    monkeypatch.setattr(p, "_stream_session", empty_round)
    run(p._run_stream_inner(ROOM))
    rows = audit_rows(tmp_path)
    assert [r.get("reconnect") for r in of_type(rows, "resolve")] == [None, 1, 2, 3, 4, 5]
    end = of_type(rows, "session_end")[0]
    assert end["reason"] == "reconnect_exhausted" and end["silent"] == 6 and end["budget"] == 5


def test_model_load_failure_is_the_recorded_end_reason(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    def broken(**kw):
        raise OSError("model cache unreadable")

    monkeypatch.setattr(app.asr, "create_transcriber", broken)
    scripted_resolve(monkeypatch, ["http://cdn/a.flv"])
    run(p._run_stream_inner(ROOM))
    assert server.statuses[-1][0] == "error"
    assert of_type(audit_rows(tmp_path), "session_end")[0]["reason"] == "model_load_failed"


def test_an_internal_error_lands_in_the_audit_before_it_is_closed(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    scripted_resolve(monkeypatch, ["http://cdn/a.flv"])

    async def boom():
        raise RuntimeError("denoise exploded on {}".format(SIGNED))

    monkeypatch.setattr(p, "_ensure_denoise_model", boom)
    run(p._run_stream(ROOM))
    assert server.statuses[-1][0] == "error" and "内部错误" in server.statuses[-1][1]
    rows = audit_rows(tmp_path)
    err = of_type(rows, "internal_error")[0]
    assert err["exc_type"] == "RuntimeError" and "denoise exploded" in err["message"]
    assert "RuntimeError" in err["traceback"] and len(err["traceback"]) <= 2000
    assert "SECRETSIGN" not in json.dumps(rows, ensure_ascii=False)
    assert rows[-1]["type"] == "session_end" and rows[-1]["reason"] == "internal_error"


def test_room_switch_and_user_stop_are_the_recorded_end_reasons(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        entered = asyncio.Event()

        async def endless_round(media, *a, **k):
            entered.set()
            await asyncio.Event().wait()

        async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
            return "http://cdn/a.flv"

        monkeypatch.setattr(resolver, "resolve_stream_url", fake_resolve)
        monkeypatch.setattr(p, "_stream_session", endless_round)
        await p.start_stream(ROOM)
        await asyncio.wait_for(entered.wait(), 10)
        entered.clear()
        await p.start_stream(ROOM2)                    # 换直播间
        await asyncio.wait_for(entered.wait(), 10)
        await p.handle_control({"type": "stop"})       # 中控点「停止」

    run(scenario())
    ends = of_type(audit_rows(tmp_path), "session_end")
    assert sorted(e["reason"] for e in ends) == ["new_session", "user_stop"]
    assert p._stop_reason is None


def test_session_end_carries_the_reason_and_core_fields_win(tmp_path):
    log = AuditLog(room_url=ROOM, log_dir=tmp_path)
    log.close(reason="reconnect_exhausted", silent=6, budget=5, type="oops", ended_at="never")
    rec = rows_of(log.path)[-1]
    assert rec["type"] == "session_end" and rec["ended_at"] != "never"
    assert (rec["reason"], rec["silent"], rec["budget"]) == ("reconnect_exhausted", 6, 5)
    plain = AuditLog(room_url=ROOM, log_dir=tmp_path / "b")
    plain.close()
    assert "reason" not in rows_of(plain.path)[-1]


# ---- 音频到达率与音量门限（9、28）--------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def feed_for(meter, clock, audio_sec, over_sec, frame=SILENT):
    n = int(round(audio_sec / 0.1))
    step = over_sec / n
    events = []
    for _ in range(n):
        clock.t += step
        events.extend(meter.feed(frame))
    return events


def feed_window(meter, clock, rate, frame=SILENT):
    """按「每秒收到 rate 秒音频」的速度喂，直到一个 60 秒窗口结束。"""
    step = 0.1 / rate
    events = []
    for _ in range(5000):
        clock.t += step
        ev = meter.feed(frame)
        events.extend(ev)
        if any(kind == "heartbeat" for kind, _ in ev):
            return events
    raise AssertionError("窗口没有结束")


def kinds(events):
    return [kind for kind, _ in events]


def test_frame_rms_uses_the_same_scale_as_the_segmenter_gate():
    import numpy as np

    loud = (np.ones(FRAME_BYTES // 2) * 1000).astype(np.int16).tobytes()
    quiet = (np.ones(FRAME_BYTES // 2) * 180).astype(np.int16).tobytes()   # 约 -45 dBFS
    assert abs(frame_rms(loud) - 1000.0) < 1e-3
    assert frame_rms(quiet) < 300.0 and frame_rms(b"") == 0.0 and frame_rms(b"\x01") == 0.0


def test_meter_ignores_the_warmup_burst_and_beats_once_a_minute():
    clock = Clock()
    meter = AudioFlowMeter(clock=clock)
    assert "heartbeat" not in kinds(feed_for(meter, clock, 30, 14))    # CDN 开播先灌缓存
    events = feed_for(meter, clock, 70, 70)
    beats = [data for kind, data in events if kind == "heartbeat"]
    assert len(beats) == 1
    assert 59.0 <= beats[0]["audio_sec"] <= 61.0 and 60.0 <= beats[0]["wall_sec"] <= 60.5
    assert set(beats[0]) == {"audio_sec", "wall_sec", "speech_frames", "segments"}


def test_meter_flags_low_audio_after_two_windows_and_clears_with_hysteresis():
    clock = Clock()
    meter = AudioFlowMeter(clock=clock)
    feed_for(meter, clock, 16, 16)
    assert "low" not in kinds(feed_window(meter, clock, 0.5))         # 一个窗口不算
    assert "low" in kinds(feed_window(meter, clock, 0.5)) and meter.low
    assert "low" in kinds(feed_window(meter, clock, 0.5))             # 持续期间数字跟着更新
    events = feed_window(meter, clock, 0.75)                           # 回差区间：不闪
    assert "recovered" not in kinds(events) and "low" not in kinds(events) and meter.low
    assert "recovered" in kinds(feed_window(meter, clock, 1.0)) and not meter.low


def test_meter_summarises_every_five_minutes():
    clock = Clock()
    meter = AudioFlowMeter(clock=clock)
    events = feed_for(meter, clock, 310, 310)
    summaries = [data for kind, data in events if kind == "summary"]
    assert len(summaries) == 1
    assert summaries[0]["segments_cut"] == 0 and 299.0 <= summaries[0]["audio_sec"] <= 301.0


def test_meter_reports_two_minutes_below_the_gate_across_rounds_and_clears_on_a_cut():
    clock = Clock()
    meter = AudioFlowMeter(clock=clock)
    assert "quiet" not in kinds(feed_for(meter, clock, 100, 100))
    meter.start_round()                                   # 断流重连：计数跨轮累计
    events = feed_for(meter, clock, 21, 21)
    quiet = [data for kind, data in events if kind == "quiet"]
    assert len(quiet) == 1 and quiet[0]["audio_sec"] >= 120.0 and meter.quiet
    assert meter.feed(SILENT, segments=1) == [("speech", {})]
    assert not meter.quiet and meter.cut(0) == []


def test_meter_events_become_audit_rows_and_persistent_notices(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._new_session_state(audit)
    run(p._on_audio_events(sess, [
        ("heartbeat", {"audio_sec": 30.0, "wall_sec": 60.0, "speech_frames": 12, "segments": 1}),
        ("low", {"audio_sec": 30.0, "wall_sec": 60.0}),
        ("quiet", {"audio_sec": 121.0}),
    ]))
    incidents = server.config["incidents"]
    assert incidents["session:audio_rate"]["text"] == "过去 1 分钟只收到 30 秒直播音频，缺的部分没有经过检测"
    assert incidents["session:quiet_audio"]["text"].startswith("已收到 121 秒直播音频，但音量一直低于识别门限")
    run(p._on_audio_events(sess, [("recovered", {"audio_sec": 58.0, "wall_sec": 60.0}),
                                  ("speech", {})]))
    assert "session:audio_rate" not in server.config["incidents"]
    assert "session:quiet_audio" not in server.config["incidents"]
    audit.close()
    beat = of_type(rows_of(audit.path), "audio_heartbeat")[0]
    assert beat == dict(beat, audio_sec=30.0, wall_sec=60.0, speech_frames=12, segments=1)


def test_a_real_round_of_loud_audio_counts_segments_and_peak(monkeypatch, tmp_path):
    import numpy as np

    p, server = make_pipeline(monkeypatch, tmp_path)
    loud = (np.ones(FRAME_BYTES // 2) * 2000).astype(np.int16).tobytes()
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", source_factory(frames=100, frame=loud))
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    _round(p, p._new_session_state(audit))
    audit.close()
    summary = of_type(rows_of(audit.path), "stream_audio")[-1]
    assert summary["segments_cut"] >= 1 and summary["peak_rms"] == 2000.0


# ---- 直连地址：看门狗断开 ≠ 直播结束（10）------------------------------------------------

def test_a_direct_address_that_still_serves_data_is_reconnected(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions, probes = [], []

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return (True, 60.0) if len(sessions) == 1 else (False, 0.0)

    async def still_serving(url, timeout=8):
        probes.append(url)
        return True

    async def passthrough(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    async def reachable():
        raise AssertionError("直连地址不探 www.tiktok.com")

    monkeypatch.setattr(resolver, "resolve_stream_url", passthrough)
    monkeypatch.setattr(resolver, "_media_url_works", still_serving)
    monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(DIRECT))
    assert probes == [DIRECT]
    assert len(sessions) == 3                    # 重连了；之后连续拉不到，原有的 1 次预算照样兜底
    assert "ended" not in [state for state, _ in server.statuses]    # 不再宣布「直播流已结束」
    assert server.statuses[-1][0] == "error"
    assert of_type(audit_rows(tmp_path), "session_end")[0]["reason"] == "reconnect_exhausted"


def test_a_direct_address_that_stopped_serving_ends_with_a_plain_statement(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def fake_session(media, *a, **k):
        return True, 60.0

    async def dead(url, timeout=8):
        return False

    async def passthrough(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    monkeypatch.setattr(resolver, "resolve_stream_url", passthrough)
    monkeypatch.setattr(resolver, "_media_url_works", dead)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(DIRECT))
    state, detail = server.statuses[-1]
    assert state == "ended" and detail.startswith("这个直连地址已经拉不到数据，监听已停止")
    assert not [d for _, d in server.statuses if "再试" in d]      # 没有时钟跳变：探一次就下结论
    end = of_type(audit_rows(tmp_path), "session_end")[0]
    assert end["reason"] == "stream_ended" and end["probes"] == 1


def test_a_direct_address_that_fails_right_after_a_gap_is_tried_again_before_ending(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions, probes = [], []
    answers = iter([False, False, True])

    async def fake_session(media, *a, sess=None, **k):
        sessions.append(media)
        if len(sessions) == 1:     # 醒来时时钟对账断开了这一轮，网络还没连上
            sess["gap"] = {"from": T0, "to": T0 + 1390, "sec": 1380.0, "diverged": True}
            return True, 60.0
        return False, 0.0

    async def flaky(url, timeout=8):
        probes.append(url)
        return next(answers)

    async def passthrough(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    monkeypatch.setattr(resolver, "resolve_stream_url", passthrough)
    monkeypatch.setattr(resolver, "_media_url_works", flaky)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(DIRECT))
    assert len(probes) == 3 and len(sessions) == 3      # 第三次探到数据：重连（之后的空轮照旧由预算兜底）
    retries = [d for _, d in server.statuses if "再试" in d]
    assert retries == ["电脑刚从休眠或挂起中恢复，这个直连地址暂时拉不到数据；2 秒后再试（第 1/4 次）…",
                       "电脑刚从休眠或挂起中恢复，这个直连地址暂时拉不到数据；4 秒后再试（第 2/4 次）…"]
    assert "ended" not in [state for state, _ in server.statuses]
    assert_no_cause_labels(retries)
    assert of_type(audit_rows(tmp_path), "session_end")[0]["reason"] == "reconnect_exhausted"


def test_a_direct_address_still_dead_after_the_retries_ends_and_says_how_often_it_was_tried(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    probes = []

    async def not_ticked_yet(interval=10):
        await asyncio.Event().wait()     # 跳变只能靠探活之前补对的那一次时钟发现

    monkeypatch.setattr(p, "_stats_loop", not_ticked_yet)

    async def fake_session(media, *a, sess=None, **k):
        wall, mono = sess["clock"]      # 两个钟一起多走了 60 秒（Windows 休眠或事件循环卡住）
        sess["clock"] = (wall - 70.0, mono - 70.0)
        return True, 60.0

    async def dead(url, timeout=8):
        probes.append(url)
        return False

    async def passthrough(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    monkeypatch.setattr(resolver, "resolve_stream_url", passthrough)
    monkeypatch.setattr(resolver, "_media_url_works", dead)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(DIRECT))
    assert len(probes) == 5
    retries = [d for _, d in server.statuses if "再试" in d]
    assert len(retries) == 4 and retries[-1].startswith("程序刚才约 60 秒没有运行，这个直连地址暂时拉不到数据；16 秒后再试")
    assert all("休眠" not in d for d in retries)
    assert server.statuses[-1][0] == "ended"
    rows = audit_rows(tmp_path)
    assert [g["clocks_diverged"] for g in of_type(rows, "clock_gap")] == [False]
    end = of_type(rows, "session_end")[0]
    assert end["reason"] == "stream_ended" and end["probes"] == 5
