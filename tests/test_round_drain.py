"""断流之后马上显示「正在重连」，翻译排空不再挡着重连（专家审查 B3，本批做 B3a）。

以前流一断（读到流尾、看门狗断开），_stream_session 要等识别把积压做完、翻译再等最多 5 秒才返回，
重连循环这时才发 connecting——这段时间界面一直显示「直播中」。补核实测：8 段、每段识别 1 秒，
EOF 到开始重连 8.1 秒。现在：

  * reader 自然结束（不是被取消）就发 status('connecting', 「直播流中断，正在重连…」)，识别排空期间
    出来的字幕照常上屏；
  * 翻译排空交给后台任务（登记在这一场的 sess["translation_drains"]），不再推迟下一轮解析；这一场
    自然结束时等它跑完，停止时取消它，计数和译文都落在这一场；
  * 识别排空仍在这一轮里做完（B3b 才改成整场一条识别队列）：8 段全部识别、顺序不乱，stream_break 和
    last_round 在下一轮解析之前写好，线程池在最后一段识别完之后才关，每轮只写一条 stream_break。

全部用桩件：不联网、不起 ffmpeg、不加载模型。主播名是虚构的。
"""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import app.asr
import app.audio
import app.segmenter
from app import i18n, reconnect_policy, resolver
from app import pipeline as pipeline_mod
from app.audio import FRAME_BYTES
from app.audit import AuditLog
from app.pipeline import Pipeline
from app.server import CaptionServer
from tests import i18n_rules as rules
from tests.helpers import run

ROOM = "https://www.tiktok.com/@ficticia_drain/live"
SEG_FRAMES = 10                  # 一段 = 10 帧 = 1 秒音频
N_SEGMENTS = 8
RECONNECTING = "直播流中断，正在重连…"
RECONNECTING_EN = "The stream was interrupted. Reconnecting…"


@pytest.fixture(autouse=True)
def _no_hardware_probe(monkeypatch):
    """走 start_stream 的用例不探本机硬件（同 tests/test_live_stall.py）。"""
    import app.hwdetect
    monkeypatch.setattr(app.hwdetect, "recommend", lambda backend=None, device=None: {
        "backend": "ct2", "model": "small", "device": "cpu", "compute_type": "int8", "note": ""})


class RecordingServer(CaptionServer):
    """真的 CaptionServer（历史、持续提示照常落），外加给每条消息记下收到的时刻。"""

    def __init__(self):
        super().__init__(port=0)
        self.timed = []

    async def broadcast(self, msg):
        self.timed.append((time.monotonic(), msg))
        await super().broadcast(msg)

    def statuses(self):
        return [(t, m["state"], m.get("detail", "")) for t, m in self.timed
                if m.get("type") == "status"]

    def captions(self):
        return [(t, m) for t, m in self.timed if m.get("type") == "caption"]

    def states(self):
        out = {}
        for _t, m in self.timed:
            if m.get("type") == "caption":
                out[m["id"]] = m.get("translate_state")
            elif m.get("type") == "caption_update" and "translate_state" in m:
                out[m["id"]] = m["translate_state"]
        return out


def make_pipeline(monkeypatch, tmp_path):
    """不替换 asyncio.sleep：这里量的就是真实的等待。"""
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False)
    p = Pipeline(args, RecordingServer())

    async def noop(*a, **k):
        return None

    p.run_selfcheck = noop
    p.ensure_local_translator = noop
    p._login_source = noop
    return p


def with_session(p, tmp_path):
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    return sess


def rows(audit):
    return [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()]


def of_type(records, kind):
    return [r for r in records if r.get("type") == kind]


def eof_source(n_frames, eof):
    """吐 n_frames 帧（每帧让一次事件循环）然后读到流尾；eof["t"] 记下流尾的时刻。"""
    class Source:
        def __init__(self, media, denoise_model=None):
            self.stopping = False

        async def frames(self):
            for _ in range(n_frames):
                yield b"\x01" * FRAME_BYTES
                await asyncio.sleep(0)
            eof["t"] = time.monotonic()

        def stderr_tail(self):
            return ""

        async def stop(self, urgent=False):
            pass

    return Source


class EveryTenFrames:
    """10 帧切一段；flush 不吐东西（这一组用例不测切段器余量，见 tests/test_round_discard.py）。"""

    def __init__(self, *a, **k):
        self.n = 0
        self.silence_rms = 300.0

    def feed(self, frame):
        self.n += 1
        return [frame * SEG_FRAMES] if self.n % SEG_FRAMES == 0 else []

    def flush(self):
        return []


class GatedASR:
    """第一段等测试放行（threading.Event：识别跑在线程池里），之后每段 delay 秒。按顺序记下识别完的时刻。"""
    backend = "fake"

    def __init__(self, gate=None, delay=0.02):
        self.gate = gate
        self.delay = delay
        self.n = 0
        self.done = []

    def transcribe(self, pcm):
        self.n += 1
        n = self.n
        if n == 1 and self.gate is not None:
            self.gate.wait(10)
        time.sleep(self.delay)
        self.done.append((n, time.monotonic()))
        text = "frase numero %d" % n
        return SimpleNamespace(text=text, raw_text=text, language="es", rejected=[])


def install(monkeypatch, source):
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", source)
    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", EveryTenFrames)


# ---- 状态马上改成「正在重连」，识别排空照常做完 ------------------------------------------------

def test_connecting_shows_within_half_a_second_of_eof_while_the_backlog_is_recognized(
        monkeypatch, tmp_path):
    p = make_pipeline(monkeypatch, tmp_path)
    sess = with_session(p, tmp_path)
    eof = {}
    install(monkeypatch, eof_source(N_SEGMENTS * SEG_FRAMES, eof))
    gate = threading.Event()
    asr = GatedASR(gate)

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            p._stream_session("http://cdn/s.flv", asr, None, loop, sess=sess))
        while "t" not in eof:
            await asyncio.sleep(0.005)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not any(
                s == "connecting" for _t, s, _d in p.server.statuses()):
            await asyncio.sleep(0.005)
        recognized_then = len(asr.done)
        gate.set()
        result = await asyncio.wait_for(task, 10)
        return recognized_then, result

    try:
        recognized_then, (got_audio, secs) = run(scenario())
    finally:
        gate.set()
    connecting = [(t, d) for t, s, d in p.server.statuses() if s == "connecting"]
    assert len(connecting) == 1
    at, detail = connecting[0]
    assert at - eof["t"] < 0.5
    assert recognized_then == 0                     # 第一段还卡在识别里，就已经改口了
    assert str(detail) == RECONNECTING
    assert i18n.render(detail, i18n.EN) == RECONNECTING_EN
    assert not rules.ZH_LABELS.search(RECONNECTING) and not rules.ZH_CAUSAL.search(RECONNECTING)
    assert not rules.EN_LABELS.search(RECONNECTING_EN)
    assert not rules.EN_CAUSAL.search(RECONNECTING_EN)
    # 识别排空在这一轮里做完：8 段全部识别、顺序不乱，排空期间的字幕照常上屏
    assert got_audio is True and secs == pytest.approx(N_SEGMENTS * SEG_FRAMES * 0.1)
    assert [n for n, _ in asr.done] == list(range(1, N_SEGMENTS + 1))
    captions = p.server.captions()
    assert [m["original"] for _t, m in captions] == [
        "frase numero %d" % n for n in range(1, N_SEGMENTS + 1)]
    assert all(t > at for t, _m in captions)        # 全部在「正在重连」之后才出来，照样上屏
    statuses = [s for _t, s, _d in p.server.statuses()]
    assert statuses.index("live") < statuses.index("connecting")
    breaks = of_type(rows(p.audit), "stream_break")
    assert len(breaks) == 1 and breaks[0]["reason"] == "eof"


def test_a_round_that_never_went_live_does_not_announce_a_reconnect(monkeypatch, tmp_path):
    """一帧都没拉到的一轮：界面本来就是「正在连接」，不改口说「直播流中断」。"""
    p = make_pipeline(monkeypatch, tmp_path)
    sess = with_session(p, tmp_path)
    install(monkeypatch, eof_source(0, {}))

    async def scenario():
        loop = asyncio.get_running_loop()
        return await p._stream_session("http://cdn/s.flv", GatedASR(), None, loop, sess=sess)

    assert run(scenario()) == (False, 0.0)
    assert [s for _t, s, _d in p.server.statuses()] == []


def test_a_cancelled_round_does_not_announce_a_reconnect(monkeypatch, tmp_path):
    """停止、换主播取消这一轮：界面由停止那一边负责（「正在停止…」），这里不插一句「正在重连」。"""
    p = make_pipeline(monkeypatch, tmp_path)
    sess = with_session(p, tmp_path)

    class Live:
        def __init__(self, media, denoise_model=None):
            self.stopping = False

        async def frames(self):
            for _ in range(5):
                yield b"\x01" * FRAME_BYTES
            await asyncio.sleep(3600)

        def stderr_tail(self):
            return ""

        async def stop(self, urgent=False):
            pass

    install(monkeypatch, Live)

    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            p._stream_session("http://cdn/s.flv", GatedASR(), None, loop, sess=sess))
        while not any(s == "live" for _t, s, _d in p.server.statuses()):
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert [s for _t, s, _d in p.server.statuses()] == ["live"]


def test_the_pool_closes_only_after_the_last_segment_and_the_round_is_recorded_once(
        monkeypatch, tmp_path):
    """线程池在这一轮最后一段识别完之后才关；stream_break 和 last_round 在 _stream_session 返回之前写好
    （重连循环拿到返回值就去解析下一轮），每轮只写一条 stream_break。"""
    p = make_pipeline(monkeypatch, tmp_path)
    sess = with_session(p, tmp_path)
    eof = {}
    install(monkeypatch, eof_source(N_SEGMENTS * SEG_FRAMES, eof))
    shut = []

    class RecordingPool(ThreadPoolExecutor):
        def shutdown(self, wait=True, **kw):
            shut.append(time.monotonic())
            super().shutdown(wait=wait, **kw)

    monkeypatch.setattr(pipeline_mod, "ThreadPoolExecutor", RecordingPool)
    asr = GatedASR(delay=0.03)

    async def scenario():
        loop = asyncio.get_running_loop()
        got = await p._stream_session("http://cdn/s.flv", asr, None, loop, sess=sess)
        # 返回的这一刻：这一轮的记录都已经写好，重连循环可以直接去解析
        return got, list(of_type(rows(p.audit), "stream_break")), dict(sess.get("last_round") or {})

    got, breaks, last_round = run(scenario())
    assert got[0] is True
    assert len(asr.done) == N_SEGMENTS
    assert len(shut) == 1 and max(t for _n, t in asr.done) <= shut[0]
    assert len(breaks) == 1 and breaks[0]["break_no"] == 1
    assert "dead" in last_round
    assert sess["round"] is None and sess["break_no"] == 1


# ---- 翻译排空交给后台 ------------------------------------------------------------------------

class Stuck:
    """翻译卡住：进来就一直等。只在协程里构造（3.9 的 asyncio.Event 构造时绑事件循环）。"""
    name = "hymt2"

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = 0

    async def translate(self, text, target, source="auto", glossary=None):
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class FirstWaits:
    """第一条翻译等测试放行，之后的马上回来。只在协程里构造（同 Stuck）。"""
    name = "hymt2"

    def __init__(self):
        self.release = asyncio.Event()
        self.calls = 0

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls += 1
        if self.calls == 1:
            await self.release.wait()
        return "大家好，今天有优惠"


def test_the_translation_drain_no_longer_holds_the_round(monkeypatch, tmp_path):
    """在途翻译卡住：以前这一轮要再等 TRANSLATION_DRAIN_SEC 才返回；现在识别排空完就返回，翻译排空在后台
    等满 TRANSLATION_DRAIN_SEC 再把卡片收成「已跳过」，然后放掉这一轮的翻译队列。"""
    p = make_pipeline(monkeypatch, tmp_path)
    sess = with_session(p, tmp_path)
    # 排空时限远大于断言的上限：慢的 CI 跑器上这一轮多花零点几秒也不会误判，钉的行为不变
    p.TRANSLATION_DRAIN_SEC = 3.0
    install(monkeypatch, eof_source(2 * SEG_FRAMES, {}))

    async def scenario():
        p.translator = Stuck()
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        await p._stream_session("http://cdn/s.flv", GatedASR(), None, loop, sess=sess)
        took = time.monotonic() - t0
        drains = list(sess["translation_drains"])
        pending_then = dict(p.server.states())
        queue_then = p._trans_queue
        await asyncio.wait_for(asyncio.gather(*drains), 10.0)
        await asyncio.sleep(0.05)                       # _drop_job 的广播是 _spawn 出去的
        return took, drains, pending_then, queue_then

    took, drains, pending_then, queue_then = run(scenario())
    assert took < 1.0                                   # 没有再等 3 秒
    assert len(drains) == 1
    assert "pending" in pending_then.values()           # 返回的那一刻翻译还在跑
    assert queue_then is not None                       # 排空期间重试、弹幕让路看的还是这一轮的队列
    assert set(p.server.states().values()) == {"dropped"}
    assert p._trans_queue is None
    assert p.telemetry.translation_queue_depth == 0
    assert sess["translation_drains"] == set()
    assert p.translator.cancelled == 1


def test_a_round_without_a_session_still_drains_inline(monkeypatch, tmp_path):
    """没传 sess 的单独一轮没人替后台任务收尾：照旧在这一轮里等完（tests/test_cancelled_translation.py
    的那几条钉着这一边），不留后台任务。"""
    p = make_pipeline(monkeypatch, tmp_path)
    p.TRANSLATION_DRAIN_SEC = 0.3
    install(monkeypatch, eof_source(SEG_FRAMES, {}))

    async def scenario():
        p.translator = Stuck()
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        await p._stream_session("http://cdn/s.flv", GatedASR(), None, loop)
        return time.monotonic() - t0

    assert run(scenario()) >= 0.3
    assert set(p.server.states().values()) == {"dropped"}
    assert p._trans_queue is None


def test_an_old_drain_leaves_the_next_rounds_queue_depth_alone(monkeypatch, tmp_path):
    """后台排空期间下一轮已经开始：旧的翻译 worker 接着出队（第一条翻完，再取第二条和哨兵）、排空收尾，都不改
    新一轮的队列深度和 self._trans_queue。第一条翻译在换队列之后才放行：旧 worker 出队时写深度的那一处
    （_note_trans_depth）在换队列之后真的走到，钉的是它的守卫，不只是收尾 _settle_translations 那一处。"""
    p = make_pipeline(monkeypatch, tmp_path)
    sess = with_session(p, tmp_path)
    p.TRANSLATION_DRAIN_SEC = 5.0
    install(monkeypatch, eof_source(2 * SEG_FRAMES, {}))

    async def scenario():
        p.translator = FirstWaits()
        loop = asyncio.get_running_loop()
        await p._stream_session("http://cdn/s.flv", GatedASR(), None, loop, sess=sess)
        drains = list(sess["translation_drains"])
        old = p._trans_queue
        left_then = old.qsize()                         # 换队列这一刻旧队列里还有几样（第二条、哨兵）
        nxt = asyncio.Queue(maxsize=4)                  # 下一轮开始了
        p._trans_queue = nxt
        p.telemetry.translation_queue_depth = 3
        p.translator.release.set()                      # 旧 worker 翻完第一条，接着出队
        await asyncio.wait_for(asyncio.gather(*drains), 2.0)
        await asyncio.sleep(0.05)                       # 译文的广播
        return nxt, drains, left_then, old.qsize()

    nxt, drains, left_then, left_after = run(scenario())
    assert left_then >= 1 and left_after == 0           # 换队列之后旧 worker 确实又出过队
    assert p.translator.calls == 2
    assert not drains[0].cancelled()                    # 自己跑完的，不是到点被取消
    assert p._trans_queue is nxt
    assert p.telemetry.translation_queue_depth == 3
    assert set(p.server.states().values()) == {"ok"}    # 两条都翻完了，没有一条被收成「已跳过」


def test_a_new_round_starts_its_queue_depth_from_zero(monkeypatch, tmp_path):
    """上一轮留下的深度（后台排空还没跑完）不算到这一轮头上：弹幕翻译不白等。"""
    p = make_pipeline(monkeypatch, tmp_path)
    sess = with_session(p, tmp_path)
    install(monkeypatch, eof_source(0, {}))
    p.telemetry.translation_queue_depth = 4
    seen = []
    real = Pipeline._stage

    def stage(self, s, name, budget, **extra):
        if name == "spawn":
            seen.append(self.telemetry.translation_queue_depth)
        return real(self, s, name, budget, **extra)

    monkeypatch.setattr(Pipeline, "_stage", stage)

    async def scenario():
        loop = asyncio.get_running_loop()
        await p._stream_session("http://cdn/s.flv", GatedASR(), None, loop, sess=sess)

    run(scenario())
    assert seen == [0]


# ---- 整场：下一轮的解析不等翻译；自然结束等它，停止取消它 ------------------------------------------

def _loop_fakes(monkeypatch, p, rounds, translate_sec, resolve_hook=None, offline_after=None):
    """走 start_stream 的整场桩件。rounds：每一轮吐几帧（之后读到流尾）；最后一轮之后一直挂着（直播还在）。
    translate_sec：每条字幕翻译要多久。offline_after：第几次解析起回「不在播」（None 表示一直在播）。"""
    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", False)
    monkeypatch.setattr(reconnect_policy.V1Policy, "plan", lambda self, silent_sec=None:
                        reconnect_policy.Plan(0, "lookup", attempt=1, attempts=5))
    monkeypatch.setattr(app.asr, "create_transcriber", lambda **kw: GatedASR())
    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", EveryTenFrames)
    plan = iter(rounds)
    log = {"eof": [], "resolves": [], "translations": [], "cancelled": 0}

    class Source:
        def __init__(self, media, denoise_model=None):
            self.stopping = False
            self.n = next(plan, None)

        async def frames(self):
            for _ in range(self.n or 0):
                yield b"\x01" * FRAME_BYTES
                await asyncio.sleep(0)
            if self.n is None:
                await asyncio.sleep(3600)
            log["eof"].append(time.monotonic())

        def stderr_tail(self):
            return ""

        async def stop(self, urgent=False):
            pass

    monkeypatch.setattr(app.audio, "FFmpegAudioSource", Source)

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        log["resolves"].append(time.monotonic())
        if resolve_hook is not None:
            resolve_hook(len(log["resolves"]))
        if offline_after is not None and len(log["resolves"]) >= offline_after:
            raise resolver.ResolveError("房间不在播", kind="offline")
        return "http://cdn/s.flv"

    async def reachable(*a, **k):
        return True, ""

    monkeypatch.setattr(resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
    orig_emit = p._emit_original

    async def emit(result, *a, **k):
        await orig_emit(result, *a, **k)
        return {"id": p._counter, "text": result.text}

    async def slow_translate(job):
        log["translations"].append(("start", job["id"], time.monotonic()))
        try:
            await asyncio.sleep(translate_sec)
        except asyncio.CancelledError:
            log["cancelled"] += 1
            raise
        log["translations"].append(("end", job["id"], time.monotonic()))

    p._emit_original = emit
    p._translate_and_update = slow_translate
    return log


def test_the_next_lookup_does_not_wait_for_the_translation_drain(monkeypatch, tmp_path):
    """第一轮流断了、最后一条字幕还要翻 2 秒：下一轮的解析不再等它（以前至少等到它翻完，最多 5 秒）；
    解析的那一刻上一轮的 stream_break 已经写好。之后点停止，后台的翻译排空跟着取消。"""
    p = make_pipeline(monkeypatch, tmp_path)
    at_resolve = {}

    def hook(n):
        if n == 2:
            sess = p._session_state
            at_resolve["breaks"] = len(of_type(rows(sess["audit"]), "stream_break"))
            at_resolve["round"] = sess["round"]
            at_resolve["drains"] = len(sess["translation_drains"])

    log = _loop_fakes(monkeypatch, p, [2 * SEG_FRAMES], translate_sec=2.0, resolve_hook=hook)

    async def scenario():
        await p.start_stream(ROOM)
        task = p._stream_task
        while len(log["resolves"]) < 2:
            await asyncio.sleep(0.01)
        t0 = time.monotonic()
        await p.stop_stream()
        took = time.monotonic() - t0
        await asyncio.sleep(0.05)
        return task, took

    task, took = run(scenario())
    assert task.done() and took < 1.0
    eof_at, second_lookup = log["eof"][0], log["resolves"][1]
    assert second_lookup - eof_at < 1.0                 # 不等那 2 秒的翻译
    ends = [t for kind, _i, t in log["translations"] if kind == "end"]
    assert all(t > second_lookup for t in ends)         # 解析的时候上一轮的翻译还在跑
    assert at_resolve == {"breaks": 1, "round": None, "drains": 1}
    assert log["cancelled"] >= 1                        # 停止把后台的排空一起取消了
    session_end = of_type(_all_rows(tmp_path), "session_end")
    assert len(session_end) == 1 and session_end[0]["reason"] == "user_stop"


def _all_rows(tmp_path):
    out = []
    for f in sorted((tmp_path / "logs").glob("session-*.jsonl")):
        out.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines())
    return out


def test_a_natural_end_lets_the_last_translations_finish(monkeypatch, tmp_path):
    """流断了之后查到不在播、这一场自然结束：最后几条字幕的翻译照样跑完（和以前在那一轮里等的一样），
    session_end 在它们之后才写。"""
    p = make_pipeline(monkeypatch, tmp_path)
    log = _loop_fakes(monkeypatch, p, [2 * SEG_FRAMES], translate_sec=0.4, offline_after=2)

    async def confirm(url, exc, sess, waited):
        sess["end"] = {"reason": "offline"}
        return "offline", waited

    monkeypatch.setattr(p, "_confirm_offline", confirm)

    async def scenario():
        await p.start_stream(ROOM)
        await asyncio.wait_for(p._stream_task, 5)

    run(scenario())
    ends = [t for kind, _i, t in log["translations"] if kind == "end"]
    starts = [t for kind, _i, t in log["translations"] if kind == "start"]
    assert len(starts) == 2 and len(ends) == 2 and log["cancelled"] == 0
    records = _all_rows(tmp_path)
    end = of_type(records, "session_end")
    assert len(end) == 1 and end[0]["reason"] == "offline"
    assert "discarded_sec" not in end[0]


def _hanging_drain_round(monkeypatch, p, sess):
    """跑一轮：两段字幕，翻译卡住，TRANSLATION_DRAIN_SEC 远长于测试——这一轮的后台排空自己不会到点，只能被
    这一场的收尾取消。返回这个排空任务（在协程里调）。"""
    p.TRANSLATION_DRAIN_SEC = 60.0
    install(monkeypatch, eof_source(2 * SEG_FRAMES, {}))

    async def go():
        p.translator = Stuck()
        loop = asyncio.get_running_loop()
        await p._stream_session("http://cdn/s.flv", GatedASR(), None, loop, sess=sess)
        drains = list(sess["translation_drains"])
        assert len(drains) == 1 and not drains[0].done()
        return drains[0]

    return go


def _watch_skips(p, audit):
    """每记一次「已跳过」（_drop_job → telemetry.drop_translation，同步），记下那一刻 session_end 写了没有。"""
    seen = []
    real = p.telemetry.drop_translation

    def drop():
        seen.append(bool(of_type(rows(audit), "session_end")))
        real()

    p.telemetry.drop_translation = drop
    return seen


def test_session_end_cancels_a_drain_that_outlives_it(monkeypatch, tmp_path):
    """_end_session 兜底：没走 _run_stream_inner 收尾的，关审计之前也把这一场的后台排空取消掉，两条「已跳过」
    记在 session_end 之前。在协程里、_end_session 一返回就取结果：asyncio.run 退出时会把没结束的任务全部取消，
    放到 run() 外面断言 cancelled() 什么都钉不住（第 1 轮审查：删掉兜底那一行，旧写法照样绿）。"""
    p = make_pipeline(monkeypatch, tmp_path)
    sess = with_session(p, tmp_path)
    audit = p.audit
    hanging = _hanging_drain_round(monkeypatch, p, sess)
    skips = _watch_skips(p, audit)

    async def scenario():
        drain = await hanging()
        await p._end_session(audit, sess=sess, reason="user_stop")
        return {"cancelled": drain.cancelled(), "skips": list(skips),
                "left": set(sess["translation_drains"]),
                "ends": [r["reason"] for r in of_type(rows(audit), "session_end")]}

    got = run(scenario())
    assert got == {"cancelled": True, "skips": [False, False], "left": set(), "ends": ["user_stop"]}


def test_a_stop_past_the_grace_cancels_the_drain_before_writing_session_end(monkeypatch, tmp_path):
    """_stop_locked 兜底：旧任务被取消后卡在收尾里（识别线程或音频源停不下来），等满 STOP_GRACE_SEC 撒手，
    session_end 由 _stop_locked 代写。这时这一场的后台排空也要在关审计之前取消：stop_stream 返回的那一刻
    排空已经取消、两条「已跳过」都记在 session_end 之前、session_end 已经写好，而旧任务还没结束。"""
    p = make_pipeline(monkeypatch, tmp_path)
    sess = with_session(p, tmp_path)
    audit = p.audit
    p.STOP_GRACE_SEC = 0.2
    hanging = _hanging_drain_round(monkeypatch, p, sess)
    skips = _watch_skips(p, audit)

    async def scenario():
        drain = await hanging()
        release = asyncio.Event()

        async def stuck_teardown():
            # 旧的直播任务：被取消后一直卡在收尾里，走不到自己的 _end_session
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await release.wait()

        old = p._stream_task = asyncio.ensure_future(stuck_teardown())
        await asyncio.sleep(0)
        t0 = time.monotonic()
        await p.stop_stream()
        got = {"took": time.monotonic() - t0, "old_done": old.done(),
               "cancelled": drain.cancelled(), "skips": list(skips),
               "left": set(sess["translation_drains"]),
               "ends": [r["reason"] for r in of_type(rows(audit), "session_end")]}
        release.set()
        await old
        return got

    got = run(scenario())
    assert got.pop("took") >= 0.15                      # 确实等过了宽限、走的是代写那一支
    assert got == {"old_done": False, "cancelled": True, "skips": [False, False], "left": set(),
                   "ends": ["user_stop"]}
