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


# ---- 统计循环的一跳出错：跳过这一跳，循环接着跑（专家审查 B1 途径二）----

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
    assert [r["exc_type"] for r in errors] == ["RuntimeError"]      # 同一种这一场只记一次
    assert "NO-LEAK" not in p.audit.path.read_text(encoding="utf-8")   # 只写类型名
    out = capsys.readouterr().out
    assert out.count("统计循环这一跳出错（RuntimeError）") == 1
    assert "NO-LEAK" not in out


def test_each_kind_of_tick_error_is_recorded_once_per_session(monkeypatch, tmp_path):
    p, _server = _stats_pipeline(monkeypatch, tmp_path)
    p._stats_tick_failed(RuntimeError("a"))
    p._stats_tick_failed(RuntimeError("b"))
    p._stats_tick_failed(KeyError("c"))
    first = p.audit
    first.close(reason="new_session")
    p.audit = AuditLog(room_url=ROOM_B, log_dir=tmp_path / "logs2")   # 下一场：同一种再记一次
    p._stats_tick_failed(RuntimeError("d"))
    p.audit.close(reason="user_stop")
    kinds = lambda audit: [r["exc_type"] for r in _audit_rows(audit)    # noqa: E731
                           if r["type"] == "stats_tick_error"]
    assert kinds(first) == ["RuntimeError", "KeyError"]
    assert kinds(p.audit) == ["RuntimeError"]
