"""桌面点「重译」放后台跑：控制消息循环不等强模型，停止时取消、留痕、放掉连接。

专家审查 B6（2026-10-21，10-22 补核成立）：server 对每个页面的控制消息逐条 await on_control，而字幕
重译直接把 retranslate() 的协程交回去——在消息循环里等强模型。本地强模型最长 60 秒（日志里 p90 8.6 秒），
这期间点的「停止」「换主播」都排在它后面；前端补核点真按钮，9.46 秒后才出现「正在停止…」。

审查 P7 另外三处：
  1. retranslate() 被取消时（以前页面一断开就会）漏掉 _strong_inflight.discard 和 _release_strong；
  2. 停止后它接着跑，结果写进已经关掉的审计；
  3. 排在后面的那一条入队时没有任何反馈，像是没点上。

这里的「server 消息循环」照 app/server.py 的写法逐条 on_control、是协程就 await。
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app.audit import AuditLog
from app.pipeline import QUALITY_FAST, QUALITY_STRONG, Pipeline
from app.server import CaptionServer

ROOM = "https://www.tiktok.com/@ficticia_tres/live"


class RecordingServer(CaptionServer):
    def __init__(self):
        super().__init__(port=0)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(msg)
        await super().broadcast(msg)

    def strong_states(self, seq):
        return [m["strong_state"] for m in self.messages
                if m.get("type") == "caption_update" and m.get("id") == seq and "strong_state" in m]


class SlowStrong:
    """假的强模型：每次调用睡 delay 秒；记下同时在跑的最多几条。

    只在协程里（asyncio.run 的 scenario 里）构造：Python 3.9 的 asyncio.Event() 构造时就绑当前事件循环，
    在循环外建的，之前跑过 asyncio.run 就报 no current event loop，没跑过就绑到另一个循环上，
    wait() 时报 attached to a different loop（CI 的 3.9 任务，PR #123）。"""
    model = "fake-strong"

    RESULTS = {"Hola a todos": "大家好", "Gracias por estar aquí": "谢谢大家来这里"}

    def __init__(self, delay=5.0):
        self.delay = delay
        self.calls = []
        self.running = 0
        self.peak = 0
        self.started = asyncio.Event()

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls.append(text)
        self.running += 1
        self.peak = max(self.peak, self.running)
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.running -= 1
        return self.RESULTS[text]


@pytest.fixture
def pl(monkeypatch, tmp_path):
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
    p = Pipeline(args, RecordingServer())
    p.glossary = None
    p._hold_sleep_guard = lambda *a, **k: None
    p._release_sleep_guard = lambda *a, **k: None
    for seq, text in ((1, "Hola a todos"), (2, "Gracias por estar aquí")):
        p._recent[seq] = {"id": seq, "text": text, "lang": "es", "target": "zh-CN"}
        p._quality[seq] = QUALITY_FAST
    return p


def _with_strong(p, strong):
    async def give():
        return strong

    p._strong_translator = give
    return strong


def _session(p, tmp_path):
    """一场在跑的会话：本场审计 + 本场观察状态（桌面重译登记在它的 retranslate_tasks 里）。"""
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    p._session_state = p._new_session_state(p.audit)
    return p.audit


async def _server_loop(p, *messages):
    """app/server.py 的消息循环：逐条 on_control，是协程就 await。返回每条开始处理的时刻。"""
    started = []
    for msg in messages:
        started.append((msg["type"], time.monotonic()))
        result = p.handle_control(msg)
        if asyncio.iscoroutine(result):
            await result
    return started


def _rows(audit):
    return [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()]


def test_stop_is_handled_while_a_retranslate_waits_for_the_strong_model(pl, tmp_path):
    """先点重译（强模型要 5 秒）再点停止：停止 0.5 秒内就开始处理。以前要等强模型回来。"""
    _session(pl, tmp_path)

    async def scenario():
        strong = _with_strong(pl, SlowStrong(delay=5.0))
        stop_at = []
        real_stop = pl.stop_stream

        async def timed_stop(quiet=False):
            stop_at.append(time.monotonic())
            await real_stop(quiet=quiet)

        pl.stop_stream = timed_stop
        t0 = time.monotonic()
        loop = asyncio.ensure_future(_server_loop(
            pl, {"type": "retranslate", "id": 1}, {"type": "stop"}))
        await asyncio.wait_for(loop, 2.0)
        return t0, stop_at, strong

    t0, stop_at, strong = asyncio.run(scenario())
    assert stop_at and stop_at[0] - t0 < 0.5
    assert len(strong.calls) == 1


def test_two_clicks_never_overlap_and_the_second_shows_pending_at_once(pl):
    async def scenario():
        strong = _with_strong(pl, SlowStrong(delay=0.2))
        await _server_loop(pl, {"type": "retranslate", "id": 1}, {"type": "retranslate", "id": 2})
        await asyncio.sleep(0)
        # 两条都已入队：第二条还在排队，按钮已经是「重译中…」
        queued = (pl.server.strong_states(2), len(strong.calls))
        for _ in range(100):
            if len(strong.calls) == 2 and strong.running == 0 and pl.server.strong_states(2)[-1:] == ["ok"]:
                break
            await asyncio.sleep(0.02)
        return queued, strong

    (states_2_when_queued, calls_when_queued), strong = asyncio.run(scenario())
    assert states_2_when_queued == ["pending"] and calls_when_queued == 1
    assert strong.peak == 1                         # 强模型调用不重叠
    assert pl.server.strong_states(1) == ["pending", "ok"]
    assert pl.server.strong_states(2) == ["pending", "ok"]   # 入队发过 pending，开始时不重复发
    assert pl._quality[1] == pl._quality[2] == QUALITY_STRONG


def test_evidence_is_saved_when_the_click_is_queued(pl):
    """证据音频入队就存（排在前面的可能要等一分钟，环形缓冲等不了），而且只存一次。"""
    saved = []
    pl._evidence_retranslate = lambda seq: saved.append(seq)

    async def scenario():
        _with_strong(pl, SlowStrong(delay=0.2))
        await _server_loop(pl, {"type": "retranslate", "id": 1}, {"type": "retranslate", "id": 2})
        early = list(saved)
        await asyncio.sleep(0.6)
        return early

    early = asyncio.run(scenario())
    assert early == [1, 2]
    assert saved == [1, 2]


def test_stop_cancels_a_running_retranslate_and_records_it(pl, tmp_path):
    audit = _session(pl, tmp_path)

    async def scenario():
        strong = _with_strong(pl, SlowStrong(delay=5.0))
        await _server_loop(pl, {"type": "retranslate", "id": 1})
        await asyncio.wait_for(strong.started.wait(), 1.0)
        assert 1 in pl._strong_inflight and pl._strong_users()
        await _server_loop(pl, {"type": "stop"})
        return strong.running

    assert asyncio.run(scenario()) == 0             # 强模型那一次调用也停了
    assert pl._strong_inflight == set()             # 放掉了
    assert pl._strong_users() == {}                 # 强模型连接也放掉了
    rows = _rows(audit)
    (cancelled,) = [r for r in rows if r["type"] == "translation_strong"]
    assert cancelled["error"] == "cancelled" and cancelled["ok"] is False
    assert cancelled["translated"] is None and cancelled["trigger"] == "manual"
    assert cancelled["model"] == "fake-strong"
    assert "queued" not in cancelled                # 已经在等强模型：不带 queued，形状和以前一样
    types = [r["type"] for r in rows]
    assert types.index("translation_strong") < types.index("session_end")   # 关审计之前记下
    assert pl.server.strong_states(1) == ["pending", "cancelled"]   # 按钮撤回「重译」
    assert pl.audit is None


def test_a_queued_click_cancelled_by_stop_gets_its_button_back(pl, tmp_path):
    """排在后面、还没轮到的那一条：没调强模型，「重译中…」要撤回来；审计里也记一条 cancelled，带
    queued=true，和已经在等强模型时被取消的那条分得开（中控点过「重译」这件事要有痕迹）。"""
    audit = _session(pl, tmp_path)

    async def scenario():
        for seq in (1, 2):                          # 服务端留存里有这两条字幕（重连回放用）
            await pl.server.broadcast({"type": "caption", "id": seq, "text": "x", "translated": "y"})
        strong = _with_strong(pl, SlowStrong(delay=5.0))
        await _server_loop(pl, {"type": "retranslate", "id": 1}, {"type": "retranslate", "id": 2})
        await asyncio.wait_for(strong.started.wait(), 1.0)
        await _server_loop(pl, {"type": "stop"})
        return strong

    strong = asyncio.run(scenario())
    assert len(strong.calls) == 1
    assert pl.server.strong_states(2) == ["pending", "cancelled"]
    rows = _rows(audit)
    strong_rows = sorted((r for r in rows if r["type"] == "translation_strong"), key=lambda r: r["seq"])
    assert [(r["seq"], r["error"], r.get("queued")) for r in strong_rows] == [
        (1, "cancelled", None), (2, "cancelled", True)]
    queued = strong_rows[1]
    assert queued["ok"] is False and queued["translated"] is None and queued["trigger"] == "manual"
    assert queued["model"] is None and queued["translate_ms"] == 0.0     # 强模型一次也没调
    types = [r["type"] for r in rows]
    assert max(i for i, kind in enumerate(types) if kind == "translation_strong") < types.index("session_end")
    # 服务端留存的字幕没有停在 pending：重连回放不会显示「重译中…」
    kept = {m["id"]: m.get("strong_state") for m in pl.server.history if m.get("id") in (1, 2)}
    assert kept == {1: "cancelled", 2: "cancelled"}


def test_session_end_cancels_this_sessions_retranslates_before_closing(pl, tmp_path):
    """自然结束（下播）走 _end_session：同样先取消再关审计，不往关掉的审计里写。"""
    audit = _session(pl, tmp_path)
    sess = pl._session_state

    async def scenario():
        strong = _with_strong(pl, SlowStrong(delay=5.0))
        await _server_loop(pl, {"type": "retranslate", "id": 1})
        await asyncio.wait_for(strong.started.wait(), 1.0)
        await pl._end_session(audit, sess=sess, reason="offline")

    asyncio.run(scenario())
    rows = _rows(audit)
    assert [r["error"] for r in rows if r["type"] == "translation_strong"] == ["cancelled"]
    assert rows[-1]["type"] == "session_end" and rows[-1]["reason"] == "offline"
    assert pl._strong_inflight == set() and pl._strong_users() == {}


def test_a_late_finalizer_does_not_cancel_the_next_sessions_retranslates(pl, tmp_path):
    """旧场晚到的收尾只取消它自己那一场登记的重译（CLAUDE.md 第七条）。"""
    old_audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "old")
    old_sess = pl._new_session_state(old_audit)
    _session(pl, tmp_path)                          # 下一场已经开始

    async def scenario():
        strong = _with_strong(pl, SlowStrong(delay=0.3))
        await _server_loop(pl, {"type": "retranslate", "id": 1})
        await asyncio.wait_for(strong.started.wait(), 1.0)
        await pl._end_session(old_audit, sess=old_sess, reason="new_session")
        for _ in range(50):
            if pl.server.strong_states(1)[-1:] == ["ok"]:
                break
            await asyncio.sleep(0.02)

    asyncio.run(scenario())
    assert pl.server.strong_states(1) == ["pending", "ok"]


def test_a_cancelled_direct_call_releases_what_it_held(pl, tmp_path):
    """以前页面一断开，server 那边等着的 retranslate() 就被取消：_strong_inflight 和强模型连接漏掉，
    这一条之后再点只会回「正在重译中」。现在 finally 里放掉，取消也记进审计。"""
    audit = _session(pl, tmp_path)

    async def scenario():
        strong = _with_strong(pl, SlowStrong(delay=5.0))
        task = asyncio.ensure_future(pl.retranslate(1))
        await asyncio.wait_for(strong.started.wait(), 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert pl._strong_inflight == set() and pl._strong_users() == {}
    audit.close(reason="user_stop")
    assert [r["error"] for r in _rows(audit) if r["type"] == "translation_strong"] == ["cancelled"]


def test_a_refused_click_is_not_left_pending(pl):
    """找不到原文的那一条：入队不发 pending，retranslate() 自己回 failed 和说明。"""
    async def scenario():
        _with_strong(pl, SlowStrong(delay=0.0))
        await _server_loop(pl, {"type": "retranslate", "id": 99})
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert pl.server.strong_states(99) == ["failed"]
    assert any(m.get("type") == "retranslate_note" and m.get("id") == 99
               for m in pl.server.messages)


def test_the_handler_returns_before_the_strong_model_answers(pl):
    """交给 server 的协程排上队或开始了就返回：它本身不等强模型。"""
    async def scenario():
        strong = _with_strong(pl, SlowStrong(delay=5.0))
        t0 = time.monotonic()
        await _server_loop(pl, {"type": "retranslate", "id": 1})
        took = time.monotonic() - t0
        await asyncio.wait_for(strong.started.wait(), 1.0)
        for task in list(pl._bg_tasks):
            task.cancel()
        return took

    assert asyncio.run(scenario()) < 0.5


# ---- 后台任务里出了普通异常：按钮不能停在「重译中…」（专家审查 B6 复核）----

class Broken(Exception):
    pass


def test_a_strong_model_that_fails_to_start_gives_the_button_back(pl, capsys):
    """入队时就发了 pending；之后准备强模型时抛了普通异常（这里是 OSError）。以前只打一行警告、不发终态，
    桌面按钮和服务端留存的那条字幕一直停在「重译中…」。现在补一条 failed。"""
    async def boom():
        raise OSError("socket 坏了")

    pl._strong_translator = boom

    async def scenario():
        await pl.server.broadcast({"type": "caption", "id": 1, "text": "x", "translated": "y"})
        await _server_loop(pl, {"type": "retranslate", "id": 1})
        for _ in range(100):
            if pl.server.strong_states(1)[-1:] == ["failed"]:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert pl.server.strong_states(1) == ["pending", "failed"]
    kept = {m["id"]: m.get("strong_state") for m in pl.server.history if m.get("id") == 1}
    assert kept == {1: "failed"}                     # 重连回放也不会显示「重译中…」
    assert pl._strong_inflight == set()
    assert "[警告] 重译出错" in capsys.readouterr().out


def test_an_error_after_the_strong_model_was_taken_also_gives_the_button_back(pl):
    """已经占上强模型之后才抛（拼提示的时候）：同样补 failed，_strong_inflight 和强模型连接照样放掉。"""
    def broken(text):
        raise Broken("bad text")

    pl._for_translation = broken

    async def scenario():
        _with_strong(pl, SlowStrong(delay=0.0))
        await _server_loop(pl, {"type": "retranslate", "id": 1})
        for _ in range(100):
            if pl.server.strong_states(1)[-1:] == ["failed"]:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert pl.server.strong_states(1) == ["pending", "failed"]
    assert pl._strong_inflight == set() and pl._strong_users() == {}


def test_a_click_that_finishes_normally_gets_no_extra_failed(pl):
    """正常走完的那一条只有 pending、ok：补 failed 只在终态还没发的时候。"""
    async def scenario():
        _with_strong(pl, SlowStrong(delay=0.0))
        await _server_loop(pl, {"type": "retranslate", "id": 1})
        for _ in range(100):
            if pl.server.strong_states(1)[-1:] == ["ok"]:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert pl.server.strong_states(1) == ["pending", "ok"]


# ---- 等强模型的时候快译到了、强模型又没出译文：按钮不能停在「重译中…」（#123 补改核对发现）----

class GatedEmpty:
    """假的强模型：等测试放行，然后什么都没译出来（返回空）。只在协程里构造（见 SlowStrong）。"""
    model = "fake-strong"

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def translate(self, text, target, source="auto", glossary=None):
        self.started.set()
        await self.release.wait()
        return ""


def _notes(p, seq):
    return [str(m["text"]) for m in p.server.messages
            if m.get("type") == "retranslate_note" and m.get("id") == seq]


def test_a_fast_translation_that_lands_during_the_wait_does_not_strand_the_button(pl):
    """点「重译」时这一条连快译都还没有；等强模型的这段时间里快译上了屏，强模型又没出译文。以前「有没有译文」
    在开头看一次（没有），于是走「连快译都还没有」那一支：_publish_translation 因为已有可用译文直接返回、
    不发 strong_state，桌面按钮和服务端留存的那条字幕一直停在「重译中…」，提示还说「这一条仍然没有译文」。"""
    pl._recent[3] = {"id": 3, "text": "Hola a todos", "lang": "es", "target": "zh-CN"}

    async def scenario():
        strong = _with_strong(pl, GatedEmpty())
        await pl.server.broadcast({"type": "caption", "id": 3, "text": "Hola a todos",
                                   "translated": None})
        await _server_loop(pl, {"type": "retranslate", "id": 3})
        await asyncio.wait_for(strong.started.wait(), 1.0)
        assert pl._quality.get(3, 0) == 0                 # 点的那一刻还没有译文
        await pl._publish_translation(3, "大家好", True, 10.0, QUALITY_FAST, "zh-CN")
        strong.release.set()
        for _ in range(100):
            if pl.server.strong_states(3)[-1:] == ["failed"]:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert pl.server.strong_states(3) == ["pending", "failed"]
    kept = next(m for m in pl.server.history if m.get("id") == 3)
    assert kept.get("strong_state") == "failed"           # 重连回放也不会显示「重译中…」
    assert kept["translated"] == "大家好"                  # 快译原样留着，没被失败擦掉
    assert pl._strong_inflight == set()
    notes = _notes(pl, 3)
    assert len(notes) == 1 and notes[0].endswith("原译文保持不变")


def test_a_direct_retranslate_call_also_reports_failed_when_the_fast_one_landed(pl):
    """不经桌面队列直接调 retranslate()（手机、报警自动触发走这里）：同样发 failed，不留在 pending。"""
    pl._recent[3] = {"id": 3, "text": "Hola a todos", "lang": "es", "target": "zh-CN"}

    async def scenario():
        strong = _with_strong(pl, GatedEmpty())
        await pl.server.broadcast({"type": "caption", "id": 3, "text": "Hola a todos",
                                   "translated": None})
        task = asyncio.ensure_future(pl.retranslate(3, trigger="viewer"))
        await asyncio.wait_for(strong.started.wait(), 1.0)
        await pl._publish_translation(3, "大家好", True, 10.0, QUALITY_FAST, "zh-CN")
        strong.release.set()
        await asyncio.wait_for(task, 1.0)

    asyncio.run(scenario())
    assert pl.server.strong_states(3) == ["pending", "failed"]
    assert pl._quality[3] == QUALITY_FAST


def test_without_a_fast_translation_the_failure_still_marks_the_caption_untranslated(pl):
    """对照：强模型回来时仍然没有任何译文，照旧走「这一条没有译文」那一支（translated=None、failed）。"""
    pl._recent[3] = {"id": 3, "text": "Hola a todos", "lang": "es", "target": "zh-CN"}

    async def scenario():
        strong = _with_strong(pl, GatedEmpty())
        await pl.server.broadcast({"type": "caption", "id": 3, "text": "Hola a todos",
                                   "translated": None})
        await _server_loop(pl, {"type": "retranslate", "id": 3})
        await asyncio.wait_for(strong.started.wait(), 1.0)
        strong.release.set()
        for _ in range(100):
            if pl.server.strong_states(3)[-1:] == ["failed"]:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert pl.server.strong_states(3) == ["pending", "failed"]
    updates = [m for m in pl.server.messages
               if m.get("type") == "caption_update" and m.get("id") == 3
               and m.get("strong_state") == "failed"]
    assert len(updates) == 1 and updates[0]["translated"] is None
    assert _notes(pl, 3)[0].endswith("这一条仍然没有译文")
