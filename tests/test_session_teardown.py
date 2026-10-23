"""晚到的旧流任务收尾，不得碰下一场的东西。

实录事故（2026-08-31）：识别一段要几十秒时，点「开始」后旧任务在 3 秒
宽限内取消不掉、被放手自行收尾；等它终于走到 finally，self.audit 已经是
新会话的了——一关，新会话 102 条字幕的合规证据全部静默丢失（audit 文件
只剩 544 字节的头）。stats 循环同理会被砍，健康提示随之失踪。
"""
import asyncio

from app.pipeline import Pipeline


class FakeAudit:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeServer:
    config = {}

    async def broadcast(self, msg):
        pass


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _pipeline():
    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p._stats_task = None
    return p


def test_late_finalizer_closes_only_its_own_audit():
    p = _pipeline()
    old_audit, new_audit = FakeAudit(), FakeAudit()
    p.audit = new_audit                        # 新会话已经开始
    _run(p._end_session(old_audit))            # 旧任务晚到的收尾
    assert old_audit.closed                    # 自己的关了
    assert not new_audit.closed                # 新会话的动都没动
    assert p.audit is new_audit


def test_late_finalizer_leaves_the_new_stats_loop_alone():
    async def scenario():
        p = _pipeline()
        old_audit = FakeAudit()
        p.audit = FakeAudit()                  # 已是新会话
        p._stats_task = asyncio.ensure_future(asyncio.sleep(30))
        stats = p._stats_task
        await p._end_session(old_audit)
        alive = not stats.cancelled()
        stats.cancel()
        return alive

    assert _run(scenario())                    # 新会话的 stats 循环还活着


def test_current_session_teardown_still_cleans_everything():
    async def scenario():
        p = _pipeline()
        mine = FakeAudit()
        p.audit = mine                         # 我就是当前会话
        p._stats_task = asyncio.ensure_future(asyncio.sleep(30))
        stats = p._stats_task
        await p._end_session(mine)
        await asyncio.sleep(0)                 # 让取消真正传播到任务
        return mine.closed, p.audit, stats.cancelled() or stats.done()

    closed, audit, stats_gone = _run(scenario())
    assert closed and audit is None and stats_gone


# ---- 收尾等到一半才过期（专家审查 B1）----
#
# 上面三条只测了「进入收尾时就已经过期」。真实的竞态是：旧场进入 _end_session 时还是当前会话，接着
# await 停弹幕——弹幕子进程还在连接阶段时不理 SIGTERM，要等 3 秒后 SIGKILL。这期间换主播的
# _stop_locked 过了宽限期、把旧场收掉，新一场开起来、建好自己的统计循环；旧场停完弹幕回来，以前拿
# 入口处算的 still_current 去取消 self._stats_task——取消的是新一场的，看门狗整场跟着没了。

import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app import pipeline as pipeline_mod  # noqa: E402
from app.audit import AuditLog  # noqa: E402
from app.server import CaptionServer  # noqa: E402

ROOM_A = "https://www.tiktok.com/@ficticia_uno/live"
ROOM_B = "https://www.tiktok.com/@ficticia_dos/live"


class RecordingServer(CaptionServer):
    """真的 CaptionServer（状态和持续提示落进 config），外加把每条消息记下来。"""

    def __init__(self):
        super().__init__(port=0)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(msg)
        await super().broadcast(msg)


def _real_pipeline(monkeypatch, tmp_path):
    """真的 Pipeline、真的 start_stream / _stop_locked / _run_stream_inner / _begin_session / _end_session；
    只换掉拉流那一轮（_run_session）和弹幕子进程的 stop()。不联网、不起 ffmpeg、不读浏览器登录。"""
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False,
        cookies_browser="none")
    server = RecordingServer()
    p = Pipeline(args, server)

    async def noop(*a, **k):
        return None

    p.run_selfcheck = noop
    p.ensure_local_translator = noop
    p._heal_local_engine = noop
    p._provision_then_check = noop
    p._login_source = noop
    p._hold_sleep_guard = lambda *a, **k: None
    p._release_sleep_guard = lambda *a, **k: None
    return p, server


def test_teardown_that_goes_stale_halfway_leaves_the_new_stats_loop(monkeypatch, tmp_path):
    p, server = _real_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(Pipeline, "STOP_GRACE_SEC", 0.3)

    async def scenario():
        gate = asyncio.Event()
        stops = []
        armed = {"on": False}

        async def slow_stop():
            # 换主播之后的第一次（旧场自己的 _end_session）：弹幕子进程还在连接阶段，不理 SIGTERM，要等
            # SIGKILL。之后的（_stop_locked 兜底）立刻返回——CommentSource.stop() 第一行就把 _task 置空了。
            # 换主播之前的（start_stream 开场先停一遍）不算
            if not armed["on"]:
                return
            stops.append(1)
            if len(stops) == 1:
                await gate.wait()

        p.comment_source.stop = slow_stop

        async def live_round(url):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)       # 紧急停 ffmpeg 那一下
                raise

        p._run_session = live_round
        await p.start_stream(ROOM_A)
        for _ in range(200):                    # 等 A 开场、建好统计循环
            if p._stats_task is not None:
                break
            await asyncio.sleep(0.01)
        stats_a = p._stats_task
        assert stats_a is not None and not stats_a.done()
        task_a = p._stream_task

        armed["on"] = True
        await p.start_stream(ROOM_B)            # 换主播：旧场卡在停弹幕，宽限期到了就放手
        for _ in range(300):                    # 等 B 开场、建好自己的统计循环
            if p._stats_task is not None and p._stats_task is not stats_a:
                break
            await asyncio.sleep(0.01)
        stats_b = p._stats_task
        assert stats_b is not None and stats_b is not stats_a
        assert stats_a.cancelled() or stats_a.done()

        assert len(stops) >= 2                  # 旧场还卡在停弹幕，_stop_locked 那一次已经返回
        assert not task_a.done()                # 旧场的收尾还卡着
        gate.set()                              # 旧场的弹幕终于停下，接着收尾
        await asyncio.wait([task_a], timeout=5.0)
        assert task_a.done()                    # 旧场收尾走完了
        await asyncio.sleep(0.05)
        alive = not stats_b.done() and p._stats_task is stats_b
        await p.stop_stream(quiet=True)
        await asyncio.sleep(0)
        return alive, stats_b.done()

    alive, done_after_stop = asyncio.run(scenario())
    assert alive                                # 新一场的统计循环还活着（以前被旧场收尾取消了）
    assert done_after_stop                      # 新一场自己停止时照常收掉

    logs = sorted((tmp_path / "logs").rglob("session-*.jsonl"))
    ends = [json.loads(line) for f in logs for line in f.read_text(encoding="utf-8").splitlines()
            if '"session_end"' in line]
    assert len(ends) == 2                       # 两场各一条 session_end，旧场没碰新场的审计


def test_own_stats_task_is_the_only_one_cancelled():
    """sess 里记着本场自己的统计循环：收尾只取消它；self._stats_task 已经指向下一场的，不碰。"""
    async def scenario():
        p = _pipeline()
        gate = asyncio.Event()

        class SlowComments:
            async def stop(self):
                await gate.wait()

        p.comment_source = SlowComments()
        mine = FakeAudit()
        p.audit = mine
        own = asyncio.ensure_future(asyncio.sleep(30))
        p._stats_task = own
        sess = {"audit": mine, "stats_task": own}
        ending = asyncio.ensure_future(p._end_session(mine, sess=sess))
        await asyncio.sleep(0.01)               # 收尾卡在停弹幕
        # 这期间：_stop_locked 收掉旧场（取消它的统计循环、清空审计），下一场开起来
        own.cancel()
        p._stats_task = None
        p.audit = new_audit = FakeAudit()
        nxt = asyncio.ensure_future(asyncio.sleep(30))
        p._stats_task = nxt
        gate.set()
        await ending
        alive = not nxt.done() and p._stats_task is nxt
        nxt.cancel()
        return alive, mine.closed, new_audit.closed

    alive, mine_closed, new_closed = _run(scenario())
    assert alive and mine_closed and not new_closed


def test_current_session_with_sess_cancels_its_stats_and_clears_the_slot():
    async def scenario():
        p = _pipeline()
        mine = FakeAudit()
        p.audit = mine
        own = asyncio.ensure_future(asyncio.sleep(30))
        p._stats_task = own
        await p._end_session(mine, sess={"audit": mine, "stats_task": own})
        await asyncio.sleep(0)
        return own.cancelled(), p._stats_task, p.audit

    cancelled, slot, audit = _run(scenario())
    assert cancelled and slot is None and audit is None


# ---- 统计循环的某一步出错：只跳过这一步，同一跳的其余各步和下一跳照常（专家审查 B1 途径二）----

def _stats_pipeline(monkeypatch, tmp_path):
    p, server = _real_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=ROOM_A, log_dir=tmp_path / "logs")
    p._session_state = p._new_session_state(p.audit)
    return p, server


def _audit_rows(audit):
    return [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()]


def test_a_tick_that_raises_is_skipped_and_the_loop_keeps_running(monkeypatch, tmp_path, capsys):
    p, server = _stats_pipeline(monkeypatch, tmp_path)
    failures = {"left": 4}
    real_gap = p._check_clock_gap

    async def flaky_gap(interval, now=None):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise RuntimeError("https://pull-flv.example.invalid/s.flv?sign=NO-LEAK")
        return await real_gap(interval)

    p._check_clock_gap = flaky_gap

    async def scenario():
        loop_task = asyncio.ensure_future(p._stats_loop(interval=0.01))
        for _ in range(300):
            if failures["left"] == 0 and any(m.get("type") == "stats" for m in server.messages):
                break
            await asyncio.sleep(0.01)
        running = not loop_task.done()
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        return running, loop_task.cancelled()

    running, cancelled = asyncio.run(scenario())
    assert running                              # 连着四跳出错，循环还在
    assert cancelled                            # 取消照样往外抛
    assert any(m.get("type") == "stats" for m in server.messages)   # 之后的跳照常推统计
    p.audit.close(reason="user_stop")
    errors = [r for r in _audit_rows(p.audit) if r["type"] == "stats_tick_error"]
    assert [(r["step"], r["exc_type"]) for r in errors] == [("clock_gap", "RuntimeError")]  # 这一场只记一次
    assert "NO-LEAK" not in p.audit.path.read_text(encoding="utf-8")   # 只写步骤代码和类型名
    out = capsys.readouterr().out
    assert out.count("统计循环的 clock_gap 这一步出错（RuntimeError）") == 1
    assert "NO-LEAK" not in out


def test_each_kind_of_tick_error_is_recorded_once_per_session(monkeypatch, tmp_path):
    p, _server = _stats_pipeline(monkeypatch, tmp_path)
    p._stats_tick_failed(RuntimeError("a"), "clock_gap")
    p._stats_tick_failed(RuntimeError("b"), "clock_gap")
    p._stats_tick_failed(KeyError("c"), "clock_gap")
    p._stats_tick_failed(RuntimeError("e"), "audit_health")      # 另一步的同一种异常：单独一条
    first = p.audit
    first.close(reason="new_session")
    p.audit = AuditLog(room_url=ROOM_B, log_dir=tmp_path / "logs2")   # 下一场：同一种再记一次
    p._stats_tick_failed(RuntimeError("d"), "clock_gap")
    p.audit.close(reason="user_stop")
    kinds = lambda audit: [(r["step"], r["exc_type"]) for r in _audit_rows(audit)    # noqa: E731
                           if r["type"] == "stats_tick_error"]
    assert kinds(first) == [("clock_gap", "RuntimeError"), ("clock_gap", "KeyError"),
                            ("audit_health", "RuntimeError")]
    assert kinds(p.audit) == [("clock_gap", "RuntimeError")]


# ---- 某一步每跳都抛：其余各步照样每跳运行（专家审查 B1 复核：以前按「一跳」兜，排在后面的整场跑不到）----

import pytest  # noqa: E402

# 统计循环每一跳依次调的方法（步骤代码 → 方法名）；health 只在等级变了时才调，单独测
STEPS = {
    "clock_gap": "_check_clock_gap",
    "stream_watch": "_stream_watch_tick",
    "snapshot": "_stats_snapshot",
    "coverage": "_coverage_stats",
    "broadcast": "_stats_broadcast",
    "detection": "_watch_detection",
    "audit_health": "_check_audit_health",
    "asr_memory": "_asr_memory_tick",
    "guard": "_guard_tick",
}
NEEDS_SNAPSHOT = {"coverage", "broadcast", "detection"}
TICKS = 5


def _counted_steps(p, failing, health="ok"):
    """把每一步换成只计次数的替身；failing 那一步每跳都抛 RuntimeError。第一步（clock_gap）在第 TICKS+1
    跳开头抛 CancelledError 让循环停下：前 TICKS 跳每一步被调的次数正好就是它跑了几跳。停在第一步而不是
    最后一步：哪一步被挡住都能停下来、断言马上红，不会一直转到超时。"""
    calls = {name: 0 for name in STEPS}
    calls["health"] = 0
    real_snapshot = p._stats_snapshot

    def make(name):
        def stand_in(*args):
            if name == "clock_gap" and calls[name] == TICKS:
                raise asyncio.CancelledError
            calls[name] += 1
            if name == failing:
                raise RuntimeError("坏了 https://pull-flv.example.invalid/s.flv?sign=NO-LEAK")
            if name == "snapshot":
                snap = real_snapshot()
                snap["health"] = health
                return snap
            if name == "coverage":
                return {}
            return None
        return stand_in

    for name, attr in STEPS.items():
        setattr(p, attr, make(name))
    p._announce_health = make("health")
    return calls


def _run_loop(p):
    async def scenario():
        await p._stats_loop(interval=0)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())


@pytest.mark.parametrize("failing", sorted(set(STEPS) - {"snapshot"}))
def test_a_step_that_raises_every_tick_does_not_hold_up_the_others(monkeypatch, tmp_path, capsys,
                                                                   failing):
    p, _server = _stats_pipeline(monkeypatch, tmp_path)
    calls = _counted_steps(p, failing)
    _run_loop(p)
    assert {name: n for name, n in calls.items() if name != "health"} == {name: TICKS for name in STEPS}
    assert calls["health"] == 0                     # 等级一直是 ok，不发提示
    p.audit.close(reason="user_stop")
    errors = [(r["step"], r["exc_type"]) for r in _audit_rows(p.audit) if r["type"] == "stats_tick_error"]
    assert errors == [(failing, "RuntimeError")]    # 每跳都抛，这一场只记一条
    assert "NO-LEAK" not in p.audit.path.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert out.count("统计循环的 {} 这一步出错（RuntimeError）".format(failing)) == 1
    assert "NO-LEAK" not in out


def test_the_audit_health_check_runs_every_tick_while_the_clock_check_keeps_failing(monkeypatch, tmp_path):
    """复核里点名的那一种：时钟检查每跳都抛，审计健康检查照样每跳都跑（它排在后面）。"""
    p, _server = _stats_pipeline(monkeypatch, tmp_path)
    calls = _counted_steps(p, "clock_gap")
    _run_loop(p)
    assert calls["clock_gap"] == calls["audit_health"] == calls["asr_memory"] == calls["guard"] == TICKS
    p.audit.close(reason="user_stop")


def test_without_a_snapshot_only_the_steps_that_need_it_are_skipped(monkeypatch, tmp_path):
    """统计快照本身出错：要用它的几步（覆盖率、广播、识别卡住检查）跟着跳过，其余各步照样每跳运行。"""
    p, _server = _stats_pipeline(monkeypatch, tmp_path)
    calls = _counted_steps(p, "snapshot")
    _run_loop(p)
    for name in STEPS:
        assert calls[name] == (0 if name in NEEDS_SNAPSHOT else TICKS), name
    p.audit.close(reason="user_stop")
    errors = [(r["step"], r["exc_type"]) for r in _audit_rows(p.audit) if r["type"] == "stats_tick_error"]
    assert errors == [("snapshot", "RuntimeError")]


def test_a_health_notice_that_fails_is_retried_on_the_next_tick(monkeypatch, tmp_path):
    """健康提示没发出去就不算换过档：每跳重发，其余各步照常；发出去了就只发一次。"""
    p, _server = _stats_pipeline(monkeypatch, tmp_path)
    calls = _counted_steps(p, "health", health="warn")
    _run_loop(p)
    assert calls["health"] == TICKS
    assert all(calls[name] == TICKS for name in STEPS)
    p.audit.close(reason="user_stop")
    errors = [(r["step"], r["exc_type"]) for r in _audit_rows(p.audit) if r["type"] == "stats_tick_error"]
    assert errors == [("health", "RuntimeError")]

    (tmp_path / "ok").mkdir()
    p2, _server2 = _stats_pipeline(monkeypatch, tmp_path / "ok")
    calls2 = _counted_steps(p2, None, health="warn")
    _run_loop(p2)
    assert calls2["health"] == 1
    p2.audit.close(reason="user_stop")


# ---- 自然收尾途中点「开始」（专家审查 B5，reverify/backend/b1/out-natural.txt）----
#
# 下播之后这一场自己进 _end_session 收尾；收尾途中中控点了下一个房间。以前 _stop_locked 照样取消它，取消落在
# 收尾的某个 await 上（等弹幕停下最常见），session_end 改由 _stop_locked 代写：原因成了 new_session，status 和
# 弹幕计数也没了。下播后 0.02、0.10 秒点开始都中，0.40 秒点就对（那时收尾已经写完）。

def _natural_end_pipeline(monkeypatch, tmp_path, comment_stop_sec):
    p, server = _real_pipeline(monkeypatch, tmp_path)
    marks = []
    armed = {"on": False}

    async def comment_stop():
        # 下播之后的第一次（这一场自己的 _end_session）：弹幕子进程收到 SIGTERM 后退出要这么久。之后的
        # （_stop_locked 兜底、下一场停止）立刻返回——CommentSource.stop() 第一行就把 _task 置空了
        if armed["on"]:
            marks.append("comment_stop")
            if marks.count("comment_stop") == 1:
                await asyncio.sleep(comment_stop_sec)

    p.comment_source.stop = comment_stop

    async def round_that_ends(url):
        if "ficticia_uno" in url:
            p._comment_counts_audit = p.audit           # 这一场起过弹幕，收到 7 条
            p.comment_source.comments_received = 7
            await asyncio.sleep(0.05)
            p._session_state["end"] = {"reason": "offline", "status": 4}
            armed["on"] = True
            marks.append("returned")
            return                                      # 主播下播：这一轮自己结束
        await asyncio.Event().wait()

    p._run_session = round_that_ends
    return p, server, marks


async def _until_session_of(p, room):
    """等下一场开好审计（_begin_session 写完 session_start）再往下走：太早停掉，那一场连审计文件都没有。"""
    for _ in range(400):
        if p.audit is not None and p.server.config.get("room_url") == room \
                and isinstance(p._session_state, dict) and p._session_state.get("audit") is p.audit:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("下一场没有开起来")


def _session_ends(tmp_path):
    out = []
    for f in sorted((tmp_path / "logs").rglob("session-*.jsonl")):
        rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]
        start = rows[0]
        out.append((start.get("room_url"), [r for r in rows if r["type"] == "session_end"]))
    return out


@pytest.mark.parametrize("click_after", [0.02, 0.10])
def test_a_start_clicked_while_the_last_session_ends_by_itself_keeps_offline(monkeypatch, tmp_path,
                                                                             click_after):
    p, server, marks = _natural_end_pipeline(monkeypatch, tmp_path, comment_stop_sec=0.25)

    async def scenario():
        await p.start_stream(ROOM_A)
        for _ in range(300):
            if "returned" in marks:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(click_after)
        await p.start_stream(ROOM_B)
        await _until_session_of(p, ROOM_B)
        await p.stop_stream(quiet=True)

    asyncio.run(scenario())
    ends = dict((room, rows) for room, rows in _session_ends(tmp_path))
    (end_a,) = ends[ROOM_A]
    assert end_a["reason"] == "offline"                 # 以前是 new_session
    assert end_a["status"] == 4                         # 下播时房间接口给的状态
    assert end_a["comments_received"] == 7              # 弹幕计数也在
    assert "recovered" not in end_a
    (end_b,) = ends[ROOM_B]
    assert end_b["reason"] == "stopped"


def test_a_session_that_ends_by_itself_too_slowly_is_closed_with_its_own_reason(monkeypatch, tmp_path,
                                                                                capsys):
    """收尾比 STOP_GRACE_SEC 还慢（弹幕子进程还在连接阶段，要等 SIGKILL）：_stop_locked 代写，原因和附带的
    观察照这一场自己记下的。"""
    p, server, marks = _natural_end_pipeline(monkeypatch, tmp_path, comment_stop_sec=2.0)
    monkeypatch.setattr(Pipeline, "STOP_GRACE_SEC", 0.3)

    async def scenario():
        await p.start_stream(ROOM_A)
        for _ in range(300):
            if "comment_stop" in marks:
                break
            await asyncio.sleep(0.005)
        await p.start_stream(ROOM_B)
        await _until_session_of(p, ROOM_B)
        await p.stop_stream(quiet=True)

    asyncio.run(scenario())
    ends = dict((room, rows) for room, rows in _session_ends(tmp_path))
    (end_a,) = ends[ROOM_A]
    assert end_a["reason"] == "offline" and end_a["status"] == 4
    assert end_a["comments_received"] == 7
    assert "上一场正在自己收尾" in capsys.readouterr().out
    assert p._ending is None


def test_a_user_stop_during_a_natural_ending_also_keeps_offline(monkeypatch, tmp_path):
    p, server, marks = _natural_end_pipeline(monkeypatch, tmp_path, comment_stop_sec=0.25)

    async def scenario():
        await p.start_stream(ROOM_A)
        for _ in range(300):
            if "comment_stop" in marks:
                break
            await asyncio.sleep(0.005)
        await p.stop_stream()                           # 中控点「停止」

    asyncio.run(scenario())
    (end_a,) = dict(_session_ends(tmp_path))[ROOM_A]
    assert end_a["reason"] == "offline"


def test_a_live_session_is_still_cancelled_and_records_the_stop_reason(monkeypatch, tmp_path):
    """没在自己收尾的一场照旧：取消、记停止的原因（_ending 只认正在收尾的那一场）。"""
    p, server = _real_pipeline(monkeypatch, tmp_path)

    async def live(url):
        await asyncio.Event().wait()

    p._run_session = live

    async def scenario():
        await p.start_stream(ROOM_A)
        await _until_session_of(p, ROOM_A)
        await p.stop_stream()

    asyncio.run(scenario())
    (end_a,) = dict(_session_ends(tmp_path))[ROOM_A]
    assert end_a["reason"] == "user_stop"


# ---- 收到退出信号：reason=signal，带信号名（app/exit_signals.py → window_close.close_session）----

def test_a_signal_stop_records_reason_signal_and_the_signal_name(monkeypatch, tmp_path):
    from app import window_close

    p, server = _real_pipeline(monkeypatch, tmp_path)

    async def live(url):
        await asyncio.Event().wait()

    p._run_session = live

    async def scenario():
        await p.start_stream(ROOM_A)
        await _until_session_of(p, ROOM_A)
        return await window_close.close_session(p, reason="signal", fields={"signal": "SIGTERM"})

    assert asyncio.run(scenario())
    rows = [json.loads(line) for f in (tmp_path / "logs").rglob("session-*.jsonl")
            for line in f.read_text(encoding="utf-8").splitlines()]
    assert not [r for r in rows if r["type"] == "window_closed"]     # 窗口没有人关
    (end,) = [r for r in rows if r["type"] == "session_end"]
    assert end["reason"] == "signal" and end["signal"] == "SIGTERM"
    assert list(end)[:4] == ["type", "ended_at", "reason", "signal"]
    assert p._stop_reason is None and p._stop_fields is None


def test_the_window_close_fallback_writes_signal_too_when_the_stop_is_slow(monkeypatch, tmp_path):
    from app import window_close

    p, server = _real_pipeline(monkeypatch, tmp_path)

    async def live(url):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(1.0)                # 音频源收尾慢
            raise

    p._run_session = live
    monkeypatch.setattr(Pipeline, "STOP_GRACE_SEC", 3.0)

    async def scenario():
        await p.start_stream(ROOM_A)
        await _until_session_of(p, ROOM_A)
        audit = p.audit
        await window_close.close_session(p, audit_budget=0.2, total=4.0, reason="signal",
                                         fields={"signal": "SIGHUP"})
        return audit

    audit = asyncio.run(scenario())
    rows = [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()]
    (end,) = [r for r in rows if r["type"] == "session_end"]
    assert end["reason"] == "signal" and end["signal"] == "SIGHUP"


def test_the_window_close_fallback_keeps_a_natural_ending_reason(monkeypatch, tmp_path):
    """关窗口时这一场已经在自己收尾（下播）、又收得慢：兜底那一条照这一场自己的原因写，不写 window_closed。"""
    from app import window_close

    p, server, marks = _natural_end_pipeline(monkeypatch, tmp_path, comment_stop_sec=1.5)

    async def scenario():
        await p.start_stream(ROOM_A)
        for _ in range(300):
            if "comment_stop" in marks:
                break
            await asyncio.sleep(0.005)
        audit = p.audit
        await window_close.close_session(p, audit_budget=0.2, total=4.0)
        return audit

    audit = asyncio.run(scenario())
    rows = [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()]
    (end,) = [r for r in rows if r["type"] == "session_end"]
    assert end["reason"] == "offline" and end["status"] == 4
