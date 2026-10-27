"""不管是哪条路径关的审计，session_end 都带这一场的汇总（2026-10-19，live-stability session-end 调查）。

10-19 那场直播中点停止：音频源收尾比 Pipeline.STOP_GRACE_SEC 慢（生产里 ffmpeg 收到 SIGTERM 不退，
FFmpegAudioSource.stop() 要等满 5 秒才 kill），_stop_locked 撒手、替这一场写了 session_end，只有
ended_at 和 reason——没有 asr_risk、证据计数、弹幕计数。09-28 以来 7 场 user_stop 里有 5 场是这样。

写 session_end 的三条路径：这一场自己收尾（_run_stream_inner → _end_session）、_stop_locked 替过了宽限期的
旧任务代写、关窗口的兜底（window_close.close_session 直接 audit.close）。三条都要带这一场的 asr_risk（含证据计数）
和弹幕计数，在关审计的那一刻算，只读这一份审计自己的状态（CLAUDE.md 第七条，my_audit）：晚到的旧任务
不读、不写下一场。调用方显式传的字段优先；汇总算不出来不拦 session_end。

弹幕计数只在弹幕是这一场自己起的时才带（Pipeline._comment_counts_audit）：CommentSource 的计数只在 start()
时清零，stop() 不清，没起弹幕的一场（直接流地址、--no-comments、登录优先下解析返回前就结束）读到的是上一场
的数。09-28 174313 那场 resolve_error、0 段、弹幕 connecting->idle，session_end 记了上一场的 329。

弹幕留意的 comment_guard 只由这一场自己的 _end_session 写：守卫只许出现在规格 I1 列出的几处
（tests/test_comment_guard_pipeline.py），代写和关窗口兜底的那条没有这一栏。

走真的 _run_stream_inner / _run_session / _stream_session / stop_stream；只换掉解析、识别器、弹幕来源、
音频源（frames() 的 finally 里 await stop()，和 FFmpegAudioSource 一样）。不联网、不起 ffmpeg、不加载模型。
"""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import app.asr
import app.audio
from app import pipeline as pipeline_mod
from app import resolver, window_close
from app.asr import ASRResult
from app.audio import FRAME_BYTES
from app.audit import AuditLog
from app.pipeline import Pipeline
from tests.test_resilience_stream import RecordingServer, audit_rows, of_type, rows_of

ROOM = "https://www.tiktok.com/@slowstop/live"
LOUD = (np.ones(FRAME_BYTES // 2, dtype=np.int16) * 3000).tobytes()
QUIET = b"\x00" * FRAME_BYTES
GRACE = 0.3          # 停止宽限（生产 3 秒）；慢的音频源收尾要 0.6 秒，是宽限的两倍（生产是 5 秒对 3 秒）


class Transcriber:
    backend = "mlx"
    model_size = "large-v3"

    def __init__(self):
        self.n = 0

    def transcribe(self, pcm):
        self.n += 1
        r = ASRResult(text="hola chicas", language="es", raw_text="hola chicas", rejected=[])
        # 一半的段 lp 低：asr_risk.flagged 不是 0，比得出「数对了」
        r.signals = {"lp": -1.0 if self.n % 2 else -0.3, "nsp": 0.01, "cr": 1.2, "tok": 5,
                     "t": 0.0, "calls": 1, "win": 1}
        return r


class Comments:
    """弹幕来源的桩。和 CommentSource 一样，计数只在 start() 时清零重数，stop() 不清。每次 start() 这一场就
    「收到」counts 里的下一个数，前后两场的数不同，读串了看得出来。"""

    def __init__(self, counts=(3,)):
        self.counts = list(counts)
        self.started = []
        self.comments_received = 0
        self.comment_fields = {"n": 0, "has": {}}

    def start(self, unique_id):
        self.started.append(unique_id)
        n = self.counts.pop(0) if self.counts else 0
        self.comments_received = n
        self.comment_fields = {"n": n, "has": {"id": n}}

    async def stop(self):
        pass


def slow_source(stop_sec):
    class SlowStopSource:
        """frames() 一直出声（10 帧有声 + 6 帧静音，切得出段）；被取消时 finally 里 await stop()，
        stop() 要 stop_sec 秒。不认 urgent：走的是 _stream_session 的普通 stop() 分支。"""

        def __init__(self, media, denoise_model=None):
            self.stalled = False

        async def frames(self):
            n = 0
            try:
                while True:
                    yield LOUD if n % 16 < 10 else QUIET
                    n += 1
                    await asyncio.sleep(0.002)
            finally:
                await self.stop()

        async def stop(self):
            await asyncio.sleep(stop_sec)

        def stderr_tail(self):
            return ""

    return SlowStopSource


def make(monkeypatch, tmp_path, stop_sec, grace=GRACE, counts=(3,)):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=True)
    p = Pipeline(args, RecordingServer())

    async def noop(*a, **k):
        return None

    p.run_selfcheck = noop
    p.ensure_local_translator = noop
    p.comment_source = Comments(counts)
    monkeypatch.setattr(app.asr, "create_transcriber", lambda **kw: Transcriber())

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        return "http://cdn/a.flv"

    async def reachable(*a, **k):
        return True, ""

    async def probe(url):
        return 2, ""

    async def no_login(*a, **k):
        return None

    monkeypatch.setattr(resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(resolver, "login_source_with_budget", no_login)
    monkeypatch.setattr(Pipeline, "STOP_GRACE_SEC", grace)
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", slow_source(stop_sec))
    return p


async def started(p, segments=3, url=ROOM):
    """开一场、等它出 segments 段。p._counter 跨场累加，按开场前的值算。"""
    before = p._counter
    task = p._stream_task = asyncio.ensure_future(p._run_stream_inner(url))
    t0 = time.monotonic()
    while p._counter < before + segments and time.monotonic() - t0 < 10:
        await asyncio.sleep(0.01)
    assert p._counter >= before + segments
    return task


def assert_carries_the_sessions_counts(rows, reason):
    segments = of_type(rows, "segment")
    ends = of_type(rows, "session_end")
    assert len(ends) == 1, ends
    end = ends[0]
    assert end["reason"] == reason
    assert "asr_risk" in end, end            # 6926898 / 248bfcd 上代写的那条在这里或下面的弹幕计数失败
    assert end["asr_risk"]["segments"] == len(segments) > 0
    assert end["asr_risk"]["flagged"] == sum(1 for s in segments if s["asr"]["flags"]) > 0
    assert "evidence" in end["asr_risk"]
    assert end["comments_received"] == 3
    assert end["comment_fields"] == {"n": 3, "has": {"id": 3}}
    assert "breaks" in end and "monitored_sec" in end      # 拉流汇总
    return end


@pytest.mark.parametrize("stop_sec,grace", [(0.02, 1.0), (0.6, GRACE)],
                         ids=["source_stops_within_grace", "source_stops_after_grace"])
def test_a_stop_whose_source_shuts_down_slowly_still_writes_the_sessions_counts(monkeypatch, tmp_path, stop_sec,
                                                                                 grace):
    """within_grace 是对照：这一场自己收尾，session_end 之前有这一轮的 stream_break（宽限放宽到 1 秒，
    慢机器上也不会滑到代写那边）。after_grace 是 10-19 的形状：_stop_locked 代写，前面没有 stream_break——
    这一条以前只有 ended_at 和 reason。"""
    p = make(monkeypatch, tmp_path, stop_sec, grace=grace)

    async def go():
        task = await started(p)
        await p.stop_stream()
        await asyncio.wait({task}, timeout=5)
        return task

    task = asyncio.run(go())
    assert task.done()
    rows = audit_rows(tmp_path)
    assert_carries_the_sessions_counts(rows, "user_stop")
    assert p.comment_source.started == ["slowstop"]          # 计数是这一场自己起的弹幕数的
    # 确认两组走的确实是不同的路径：代写时这一轮的 stream_break 写不进已经关掉的审计
    assert bool(of_type(rows, "stream_break")) == (stop_sec < grace)


def test_the_window_close_fallback_writes_the_sessions_counts(monkeypatch, tmp_path):
    """关窗口时停止流程走不完（音频源收尾慢）：close_session 自己先关审计，session_end 由它写。
    这一条同样要带这一场的识别风险、证据和弹幕计数。之后停止流程再关是空操作。"""
    p = make(monkeypatch, tmp_path, stop_sec=0.6, grace=0.4)

    async def go():
        task = await started(p)
        finished = await window_close.close_session(p, audit_budget=0.1, total=4.0)
        await asyncio.wait({task}, timeout=5)
        return task, finished

    task, finished = asyncio.run(go())
    assert task.done() and finished
    rows = audit_rows(tmp_path)
    assert of_type(rows, "window_closed")
    assert not of_type(rows, "stream_break")         # 确实是兜底写的：这一场自己的收尾还没走到
    assert_carries_the_sessions_counts(rows, "window_closed")
    assert p.comment_source.started == ["slowstop"]


# ---- 没起弹幕的一场不带上一场的弹幕计数 ----------------------------------------------------------

DIRECT = "https://cdn.example.com/live/stream.flv"      # 直接流地址：没有主播名，弹幕起不了


def session_end_of(audit):
    ends = of_type(rows_of(Path(audit.path)), "session_end")
    assert len(ends) == 1, ends
    return ends[0]


def assert_no_comment_counts(end):
    assert "comments_received" not in end and "comment_fields" not in end, end


@pytest.mark.parametrize("close", ["own", "stand_in", "window_close"])
@pytest.mark.parametrize("second", ["direct_url", "comments_off", "comments_on"])
def test_the_next_session_reports_only_comments_it_started(monkeypatch, tmp_path, close, second):
    """上一场起了弹幕、收到 7 条，停止。下一场：直接流地址 / --no-comments 时弹幕没起，它的 session_end 不带
    弹幕计数（以前带上一场的 7）；弹幕照常起时带它自己的 5。三条收尾路径都一样：这一场自己收尾（宽限内）、
    _stop_locked 代写（音频源收尾过了宽限）、关窗口兜底。"""
    stop_sec, grace = {"own": (0.02, 1.0), "stand_in": (0.6, GRACE), "window_close": (0.6, 0.4)}[close]
    p = make(monkeypatch, tmp_path, stop_sec, grace=grace, counts=(7, 5))

    async def go():
        first = await started(p)
        first_audit = p.audit
        await p.stop_stream()
        await asyncio.wait({first}, timeout=5)
        if second == "comments_off":
            p.args.comments = False
        task = await started(p, url=DIRECT if second == "direct_url" else ROOM)
        second_audit = p.audit
        if close == "window_close":
            assert await window_close.close_session(p, audit_budget=0.1, total=4.0)
        else:
            await p.stop_stream()
        await asyncio.wait({task}, timeout=5)
        return first, task, first_audit, second_audit

    first, task, first_audit, second_audit = asyncio.run(go())
    assert first.done() and task.done() and first_audit is not second_audit
    earlier = session_end_of(first_audit)
    assert earlier["comments_received"] == 7 and earlier["comment_fields"] == {"n": 7, "has": {"id": 7}}
    end = session_end_of(second_audit)
    assert end["reason"] == ("window_closed" if close == "window_close" else "user_stop")
    assert end["asr_risk"]["segments"] > 0              # 这一场其余的汇总照带
    rows = rows_of(Path(second_audit.path))
    assert bool(of_type(rows, "stream_break")) == (close == "own")      # 确实走的是这条收尾路径
    if second == "comments_on":
        assert p.comment_source.started == ["slowstop", "slowstop"]
        assert end["comments_received"] == 5 and end["comment_fields"] == {"n": 5, "has": {"id": 5}}
    else:
        assert p.comment_source.started == ["slowstop"]
        assert_no_comment_counts(end)


@pytest.mark.parametrize("how", ["resolve_error", "stopped_while_resolving"])
def test_a_login_first_session_that_ends_before_its_comments_start_carries_no_earlier_count(monkeypatch, tmp_path,
                                                                                            how):
    """登录优先（macOS）：弹幕等第一次解析返回才起。上一场照这条路起了弹幕、收到 7 条；下一场解析失败
    （09-28 174313 的形状：resolve_error、0 段）或解析途中就点了停止，弹幕始终没起，session_end 不带 7。"""
    p = make(monkeypatch, tmp_path, stop_sec=0.02, grace=1.0, counts=(7, 5))
    p._comments_wait_for_resolve = lambda url: True

    async def go():
        resolving = asyncio.Event()
        first = await started(p)
        first_audit = p.audit
        await p.stop_stream()
        await asyncio.wait({first}, timeout=5)

        async def second_resolve(url, cookies=None, cookies_browser="auto", trace=None):
            resolving.set()
            if how == "resolve_error":
                raise resolver.ResolveError("offline", kind="offline")
            await asyncio.sleep(30)

        monkeypatch.setattr(resolver, "resolve_stream_url", second_resolve)
        task = p._stream_task = asyncio.ensure_future(p._run_stream_inner(ROOM))
        await asyncio.wait_for(resolving.wait(), 5)
        second_audit = p.audit
        if how == "stopped_while_resolving":
            await p.stop_stream()
        await asyncio.wait({task}, timeout=5)
        return first, task, first_audit, second_audit

    first, task, first_audit, second_audit = asyncio.run(go())
    assert first.done() and task.done() and first_audit is not second_audit
    assert session_end_of(first_audit)["comments_received"] == 7      # 上一场走 _start_pending_comments 起的
    end = session_end_of(second_audit)
    assert end["reason"] == ("resolve_error" if how == "resolve_error" else "user_stop")
    assert p.comment_source.started == ["slowstop"]
    assert_no_comment_counts(end)


# ---- AuditLog.end_fields 本身 ----------------------------------------------------------------

def _ends(tmp_path):
    return of_type(audit_rows(tmp_path), "session_end")


def test_explicit_fields_win_and_the_hook_runs_once(tmp_path):
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    calls = []

    def hook():
        calls.append(1)
        return {"asr_risk": {"segments": 9}, "comments_received": 4, "reason": "hook", "type": "x"}

    audit.end_fields = hook
    audit.close(reason="user_stop", asr_risk={"segments": 2})
    audit.close(reason="window_closed", comments_received=99)        # 已经关了：空操作，不再调钩子
    ends = _ends(tmp_path)
    assert len(ends) == 1 and calls == [1]
    end = ends[0]
    assert end["reason"] == "user_stop" and end["type"] == "session_end"     # 骨架字段盖不掉
    assert end["asr_risk"] == {"segments": 2}                                # 显式传的优先
    assert end["comments_received"] == 4                                     # 没传的由钩子补
    assert audit.end_fields is None                                          # 用完放掉（闭包里有证据缓冲）


@pytest.mark.parametrize("hook", [lambda: 1 / 0, lambda: "not a dict"], ids=["raises", "not_a_dict"])
def test_a_failing_hook_never_blocks_session_end(tmp_path, hook):
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    audit.end_fields = hook
    audit.close(reason="user_stop", silent=True)
    ends = _ends(tmp_path)
    assert len(ends) == 1
    assert ends[0]["reason"] == "user_stop" and ends[0]["silent"] is True


def test_a_close_without_a_reason_drops_the_hook_unused(tmp_path):
    """没有原因的 close()（老写法）照旧只写骨架：钩子不调，但也放掉。"""
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    calls = []
    audit.end_fields = lambda: calls.append(1) or {"comments_received": 1}
    audit.close()
    assert calls == [] and audit.end_fields is None
    assert set(_ends(tmp_path)[0]) == {"type", "ended_at"}


# ---- 只认这一份审计自己的状态（my_audit）--------------------------------------------------------

def test_the_summary_reads_only_the_state_owned_by_that_audit(monkeypatch, tmp_path):
    """旧一场的审计晚关（比如旧任务卡住、关窗口兜底）时，下一场已经开始：旧一场的汇总只读旧一场的会话
    状态；共享的弹幕来源此刻数的是下一场，不读。会话状态不属于这份审计时，识别风险也不带。"""
    p = make(monkeypatch, tmp_path, stop_sec=0.0)
    old = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    old_sess = p._new_session_state(old)
    new = p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    new_sess = p._session_state = p._new_session_state(new)

    # 这一场还没起弹幕：来源里的数不是它的，不读
    assert "comments_received" not in p._session_end_extras(new, new_sess)
    # 登录优先那条路：第一次解析返回后起弹幕（_start_pending_comments），计数从此归这一场
    p._comments_pending = (new, "slowstop")
    asyncio.run(p._start_pending_comments(new))
    assert p.comment_source.started == ["slowstop"]

    stale = p._session_end_extras(old, old_sess)
    assert stale["asr_risk"]["segments"] == 0
    assert "comments_received" not in stale and "comment_fields" not in stale

    current = p._session_end_extras(new, new_sess)
    assert current["comments_received"] == 3 and "asr_risk" in current

    mismatched = p._session_end_extras(old, new_sess)
    assert "asr_risk" not in mismatched and "comments_received" not in mismatched
    old.close()
    new.close()
