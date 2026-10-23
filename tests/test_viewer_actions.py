"""Pipeline 这一侧：手机「重译」按钮落到 self.retranslate() 之前的那一段。

这个文件不测 retranslate() 本身怎么翻（那是 test_translation_quality.py 等的
事）——它测的是 on_action 回调的校验、串行化、排队上限、留痕，四件事都是
ViewerHub 那边（tests/test_viewer_hub.py）钉不住的，因为 ViewerHub 从不
import pipeline，也从不知道 self._recent / self.retranslate 是什么。

一条不变式贯穿全文件：中控桌面那颗「重译」按钮从 handle_control 进来，trigger
是 "manual"，完全不经过这里的信号量和排队计数——手机点得再快，也不该让桌面端
等一等。桌面自己怎么排队（直接交回 retranslate() 的协程，还是放进后台任务）是
handle_control 的事，不在这个文件里钉。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app import settings as settings_mod
from app import viewer_share as viewer_share_mod
from app.pipeline import Pipeline
from tests.helpers import run


class StubServer:
    port = 8765

    def __init__(self):
        self.config = {}
        self.messages = []
        self.viewer = None
        self.history = []
        self.alerts = []
        self.comments = []

    async def status(self, state, detail="", command=None):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)


class FakeAudit:
    def __init__(self):
        self.records = []

    def viewer_action(self, action, seq, ip):
        self.records.append(("viewer_action", action, seq, ip))


@pytest.fixture
def pl(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5,
        context=False, asr_temperature=None, glossary=None, backend="auto",
        model=None, device="auto", compute_type="auto", denoise="off",
        banned_terms=None)
    return Pipeline(args, StubServer())


# ---- 校验：合法 id 才往下传，且 trigger 恰好是 "viewer" ----
def test_a_known_id_calls_retranslate_with_trigger_viewer(pl):
    pl._recent[7] = {"text": "hola", "lang": "es", "target": "zh-CN"}
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append((seq, trigger))

    pl.retranslate = fake_retranslate
    result = pl._on_viewer_action("retranslate", {"id": 7}, "192.168.1.50")
    assert asyncio.iscoroutine(result)
    run(result)
    assert calls == [(7, "viewer")]
    assert pl._viewer_action_pending == 0        # 跑完之后计数要归零


def test_an_id_the_program_does_not_recognize_is_ignored(pl):
    """self._recent 是权威表：程序自己都不认得的字幕，不该往下传给强模型。"""
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)

    pl.retranslate = fake_retranslate
    assert pl._on_viewer_action("retranslate", {"id": 999}, "1.2.3.4") is None
    assert calls == []
    assert pl._viewer_action_pending == 0


@pytest.mark.parametrize("action,payload", [
    ("something_else", {"id": 1}),
    ("retranslate", {"id": "not-an-int"}),
    ("retranslate", {"id": True}),
    ("retranslate", "not a dict"),
    ("retranslate", {}),
])
def test_malformed_calls_are_ignored_without_touching_retranslate(pl, action, payload):
    pl._recent[1] = {"text": "x"}
    calls = []
    pl.retranslate = lambda seq, trigger="manual": calls.append(seq)
    assert pl._on_viewer_action(action, payload, "1.2.3.4") is None
    assert calls == []


# ---- 串行化：手机的重译一次只跑一条 ----
def test_viewer_retranslates_are_serialized_by_the_semaphore(pl):
    for i in range(3):
        pl._recent[i] = {"text": "x"}
    concurrency = {"now": 0, "peak": 0}

    async def fake_retranslate(seq, trigger="manual"):
        concurrency["now"] += 1
        concurrency["peak"] = max(concurrency["peak"], concurrency["now"])
        await asyncio.sleep(0.02)
        concurrency["now"] -= 1

    pl.retranslate = fake_retranslate

    async def scenario():
        coros = [pl._on_viewer_action("retranslate", {"id": i}, "1.2.3.4")
                 for i in range(3)]
        await asyncio.gather(*coros)

    run(scenario())
    assert concurrency["peak"] == 1


def test_the_operator_button_does_not_go_through_the_viewer_semaphore(pl):
    """中控桌面的重译从 handle_control 进来，trigger 是 "manual"，不经过手机这边的
    信号量，也完全不碰 _viewer_action_pending。这里只钉交回的协程跑完时 retranslate()
    已经以 manual 调过；桌面是直接交回 retranslate() 的协程还是放进后台任务，不归这里管。"""
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(trigger)

    pl.retranslate = fake_retranslate
    result = pl.handle_control({"type": "retranslate", "id": 5})
    assert asyncio.iscoroutine(result)
    run(result)
    assert calls == ["manual"]
    assert pl._viewer_action_pending == 0


# ---- 排队上限 ----
def test_pending_count_caps_further_viewer_actions(pl):
    for i in range(pipeline_mod.VIEWER_RETRANSLATE_QUEUE_MAX + 1):
        pl._recent[i] = {"text": "x"}
    # Event 要在协程里建：Python 3.9 的 asyncio.Event() 构造时会去拿当前事件循环，
    # 前一个 asyncio.run 结束后主线程没有循环，直接 RuntimeError（3.10 起才不绑循环）
    gate = None

    async def fake_retranslate(seq, trigger="manual"):
        await gate.wait()

    pl.retranslate = fake_retranslate

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        started = [pl._on_viewer_action("retranslate", {"id": i}, "1.2.3.4")
                   for i in range(pipeline_mod.VIEWER_RETRANSLATE_QUEUE_MAX)]
        assert all(c is not None for c in started)
        assert pl._viewer_action_pending == pipeline_mod.VIEWER_RETRANSLATE_QUEUE_MAX
        # 排队已经够多了：下一次点击安静地什么都不返回，不是报错
        overflow = pl._on_viewer_action(
            "retranslate", {"id": pipeline_mod.VIEWER_RETRANSLATE_QUEUE_MAX}, "1.2.3.4")
        assert overflow is None

        tasks = [asyncio.ensure_future(c) for c in started]
        gate.set()
        await asyncio.gather(*tasks)
        assert pl._viewer_action_pending == 0

        # 跑完之后计数归零：下一次点击又能正常入队
        again = pl._on_viewer_action("retranslate", {"id": 0}, "1.2.3.4")
        assert again is not None
        gate.set()
        await again

    run(scenario())


# ---- 留痕 ----
def test_a_viewer_retranslate_writes_the_viewer_action_audit_event(pl):
    pl._recent[3] = {"text": "hola"}
    audit = FakeAudit()
    pl.audit = audit

    async def fake_retranslate(seq, trigger="manual"):
        return None

    pl.retranslate = fake_retranslate
    coro = pl._on_viewer_action("retranslate", {"id": 3}, "9.9.9.9")
    run(coro)
    assert ("viewer_action", "retranslate", 3, "9.9.9.9") in audit.records


def test_the_audit_event_is_still_attempted_when_retranslate_itself_blows_up(pl):
    """审计不能因为强模型那边炸了就整条一起丢——事后要能答「手机点没点过」。"""
    pl._recent[3] = {"text": "hola"}
    audit = FakeAudit()
    pl.audit = audit

    async def boom(seq, trigger="manual"):
        raise RuntimeError("strong model exploded")

    pl.retranslate = boom
    coro = pl._on_viewer_action("retranslate", {"id": 3}, "9.9.9.9")
    with pytest.raises(RuntimeError):
        run(coro)
    assert ("viewer_action", "retranslate", 3, "9.9.9.9") in audit.records
    assert pl._viewer_action_pending == 0          # 计数仍然要还回去


# ---- 识别健康不是 ok 时，手机点的重译不开始（专家审查 S7）----
# 重译要动用强模型；本地引擎时它和识别抢同一块显存与算力。识别已经落后时再让手机点出强模型调用，
# 落后只会更多，报警跟着更晚（CLAUDE.md 第六条）。手机上要看得到「重译失败」，不能没回音。
# 两头都不是 ok 才拦：界面上显示着的等级（_health_shown），和此刻的实况（积压、识别出错、识别卡住）。
IP = "192.168.1.50"

# 积压取统计循环的阈值（_health_level）：lagging 刚过「开始落后」，degraded 刚过「降级」
BACKLOG = {"ok": 0.0,
           "lagging": pipeline_mod.AUDIO_BACKLOG_WARN_SEC + 1.0,
           "degraded": pipeline_mod.AUDIO_BACKLOG_DEGRADED_SEC + 1.0}


class HealthAudit(FakeAudit):
    """_announce_health 会写 health：统计循环那条测试要用。"""

    def health(self, level, backlog_sec, **fields):
        self.records.append(("health", level, fields.get("reason")))


def _shown(pl, audit, level, backlog=None):
    """界面上显示着 level（属于 audit 那一场）；此刻的积压与它相符，另给 backlog 时用给的。"""
    pl._health_shown = (audit, level, "识别开始落后")
    pl.telemetry.set_backlog(BACKLOG[level] if backlog is None else backlog)


def _updates(pl, seq):
    return [m for m in pl.server.messages
            if m.get("type") == "caption_update" and m.get("id") == seq]


def _refusals(pl, seq):
    return [m for m in _updates(pl, seq) if m.get("strong_state") == "failed"]


def _viewer_actions(audit):
    return [r for r in audit.records if r[0] == "viewer_action"]


@pytest.mark.parametrize("level", ["lagging", "degraded"])
def test_a_viewer_retranslate_is_refused_while_recognition_is_behind(pl, level):
    pl._recent[4] = {"text": "hola"}
    audit = FakeAudit()
    pl.audit = audit
    _shown(pl, audit, level)
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)

    pl.retranslate = fake_retranslate
    result = pl._on_viewer_action("retranslate", {"id": 4}, IP)
    assert asyncio.iscoroutine(result)             # 有回音：hub 会调度它
    # 当场就拒，不进排队：只靠排队轮到时那一道检查的话，这里已经是 1
    assert pl._viewer_action_pending == 0
    run(result)
    assert calls == []                             # 强模型一次都没碰
    assert len(_refusals(pl, 4)) == 1              # 手机按钮标成「重译失败」
    # trigger 是 viewer：桌面上没人点过，不冒字幕旁的提示
    assert not any(m.get("type") == "retranslate_note" for m in pl.server.messages)
    assert pl._viewer_action_pending == 0
    assert pl._viewer_action_sem_obj is None       # 连手机那把信号量都没碰
    # 当场拒掉的点击也留一条痕，和排上队再被拒的一样：事后要答得出「手机点没点过」
    assert _viewer_actions(audit) == [("viewer_action", "retranslate", 4, IP)]


def test_a_viewer_retranslate_goes_ahead_when_recognition_is_ok(pl):
    pl._recent[4] = {"text": "hola"}
    audit = FakeAudit()
    pl.audit = audit
    _shown(pl, audit, "ok")
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append((seq, trigger))

    pl.retranslate = fake_retranslate
    run(pl._on_viewer_action("retranslate", {"id": 4}, IP))
    assert calls == [(4, "viewer")]
    assert _refusals(pl, 4) == []


def test_a_lagging_state_left_over_from_an_earlier_session_does_not_block(pl):
    """只认属于当前审计的那一条：上一场最后报的 lagging 不该拦住这一场（积压照样设成落后，
    拦不拦只看审计是不是这一场的）。"""
    pl._recent[4] = {"text": "hola"}
    pl.audit = FakeAudit()
    _shown(pl, FakeAudit(), "lagging")
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)

    pl.retranslate = fake_retranslate
    run(pl._on_viewer_action("retranslate", {"id": 4}, IP))
    assert calls == [4]


def test_no_session_means_nothing_to_protect(pl):
    """没在监听（audit 是 None）时没有识别在跑，停播后对着留下的字幕点重译照常。"""
    pl._recent[4] = {"text": "hola"}
    pl.audit = None
    _shown(pl, None, "lagging")
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)

    pl.retranslate = fake_retranslate
    run(pl._on_viewer_action("retranslate", {"id": 4}, IP))
    assert calls == [4]


def test_a_lagging_state_the_stats_loop_never_takes_back_does_not_block(pl):
    """GPU 识别连续出错、改用 CPU 之后，_on_asr_failures 播一条 lagging（reason=asr_fallback）。
    统计循环只在它自己上一跳算出的积压等级变了时才播；那一刻积压是 ok 的话，之后积压一直正常就
    一直不播，_health_shown 整场停在 lagging。只看它，这一场手机点的重译全部被拒，而识别并没有
    落后。这里真跑三跳统计循环（积压一直是 0），然后手机点重译：要照常开始。"""
    pl._recent[4] = {"text": "hola"}
    audit = HealthAudit()
    pl.audit = audit
    calls = []
    ticks = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append((seq, trigger))

    async def last_step_of_a_tick():
        ticks.append(len(ticks))
        if len(ticks) == 3:
            raise asyncio.CancelledError

    pl.retranslate = fake_retranslate
    pl._guard_tick = last_step_of_a_tick

    async def scenario():
        # 改用 CPU 成功时 _on_asr_failures 末尾播的就是这样一条
        await pl._announce_health("lagging", 0.0, reason="asr_fallback",
                                  text="⚠️ GPU 识别连续出错，已改用 CPU 识别")
        assert pl._health_shown[:2] == (audit, "lagging")
        try:
            await pl._stats_loop(interval=0)
        except asyncio.CancelledError:
            pass
        assert len(ticks) == 3                     # 三跳都跑到了最后一步，不是半路出错退出
        await pl._on_viewer_action("retranslate", {"id": 4}, IP)

    run(scenario())
    assert calls == [(4, "viewer")]
    assert _refusals(pl, 4) == []


def test_the_same_lagging_state_blocks_while_the_backlog_is_really_behind(pl):
    """同一条 asr_fallback 的 lagging，积压真的过了「开始落后」时照样拦。"""
    pl._recent[4] = {"text": "hola"}
    audit = HealthAudit()
    pl.audit = audit
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)

    pl.retranslate = fake_retranslate

    async def scenario():
        await pl._announce_health("lagging", 0.0, reason="asr_fallback",
                                  text="⚠️ GPU 识别连续出错，已改用 CPU 识别")
        pl.telemetry.set_backlog(BACKLOG["lagging"])
        await pl._on_viewer_action("retranslate", {"id": 4}, IP)

    run(scenario())
    assert calls == []
    assert len(_refusals(pl, 4)) == 1


@pytest.mark.parametrize("why", ["asr_failing", "asr_stalled"])
def test_failing_or_stalled_recognition_blocks_even_with_an_empty_backlog(pl, why):
    """识别连续出错时出错的调用返回得快、积压归零；一段识别卡住时没人说话积压也涨不上去。
    这两种积压都不说明识别在正常跑：照样拦。"""
    pl._recent[4] = {"text": "hola"}
    audit = FakeAudit()
    pl.audit = audit
    _shown(pl, audit, "degraded", backlog=0.0)
    if why == "asr_failing":
        pl._asr_failing = audit
    else:
        pl._asr_watch = {"audit": audit, "stalled": True, "dropped": 0}
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)

    pl.retranslate = fake_retranslate
    run(pl._on_viewer_action("retranslate", {"id": 4}, IP))
    assert calls == []
    assert len(_refusals(pl, 4)) == 1


@pytest.mark.parametrize("why", ["asr_failing", "asr_stalled"])
def test_failing_or_stalled_marks_from_an_earlier_session_do_not_block(pl, why):
    """出错、卡住的记号也只认这一场的：上一场留下的，积压正常时不拦。"""
    pl._recent[4] = {"text": "hola"}
    audit = FakeAudit()
    pl.audit = audit
    _shown(pl, audit, "degraded", backlog=0.0)
    if why == "asr_failing":
        pl._asr_failing = FakeAudit()
    else:
        pl._asr_watch = {"audit": FakeAudit(), "stalled": True, "dropped": 0}
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)

    pl.retranslate = fake_retranslate
    run(pl._on_viewer_action("retranslate", {"id": 4}, IP))
    assert calls == [4]


@pytest.mark.parametrize("state", ["strong", "inflight"])
def test_a_refusal_leaves_a_caption_already_strong_or_being_retranslated_alone(pl, state):
    """这一条已经是强模型译的、或者桌面正在重译它：retranslate() 对手机点的这两种什么都不发，
    识别落后时也该一样。照样广播 failed 的话，桌面和各台手机上这一条都会被标成「重译失败」，
    桌面那颗正在等结果的按钮还会被解禁。"""
    pl._recent[4] = {"text": "hola"}
    audit = FakeAudit()
    pl.audit = audit
    _shown(pl, audit, "lagging")
    if state == "strong":
        pl._quality[4] = pipeline_mod.QUALITY_STRONG
    else:
        pl._strong_inflight.add(4)
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)

    pl.retranslate = fake_retranslate
    result = pl._on_viewer_action("retranslate", {"id": 4}, IP)
    if result is not None:
        run(result)
    assert calls == []
    assert _updates(pl, 4) == []                   # 这一条的状态一条都不发
    assert _viewer_actions(audit) == [("viewer_action", "retranslate", 4, IP)]
    assert pl._viewer_action_pending == 0


def test_the_refusal_is_visible_even_when_the_queue_is_full(pl):
    """落后时排队多半也满了：这时候也要有回音，不能被「排满了」安静吞掉。"""
    pl._recent[4] = {"text": "hola"}
    audit = FakeAudit()
    pl.audit = audit
    _shown(pl, audit, "degraded")
    pl._viewer_action_pending = pipeline_mod.VIEWER_RETRANSLATE_QUEUE_MAX
    result = pl._on_viewer_action("retranslate", {"id": 4}, IP)
    assert result is not None
    run(result)
    assert len(_refusals(pl, 4)) == 1
    assert pl._viewer_action_pending == pipeline_mod.VIEWER_RETRANSLATE_QUEUE_MAX
    assert _viewer_actions(audit) == [("viewer_action", "retranslate", 4, IP)]


def test_a_queued_click_is_refused_at_its_turn_if_recognition_fell_behind_meanwhile(pl):
    """排队等信号量的那几秒里识别开始落后：轮到它时再看一次，不开始重译。"""
    for i in (1, 2):
        pl._recent[i] = {"text": "x"}
    audit = FakeAudit()
    pl.audit = audit
    _shown(pl, audit, "ok")
    gate = None
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)
        await gate.wait()

    pl.retranslate = fake_retranslate

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        first = asyncio.ensure_future(pl._on_viewer_action("retranslate", {"id": 1}, "1.2.3.4"))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(pl._on_viewer_action("retranslate", {"id": 2}, "1.2.3.4"))
        await asyncio.sleep(0.01)
        _shown(pl, audit, "lagging")                 # 第一条跑着的时候落后了
        gate.set()
        await asyncio.gather(first, second)

    run(scenario())
    assert calls == [1]                               # 已经在跑的那条不打断
    assert len(_refusals(pl, 2)) == 1
    assert pl._viewer_action_pending == 0
    # 两次点击各留一条痕，不多不少：事后要答得出「手机点没点过」
    assert [r[2] for r in _viewer_actions(audit)] == [1, 2]


def test_a_queued_click_leaves_a_caption_the_desktop_started_meanwhile_alone(pl):
    """排队那几秒里桌面开始重译这一条、识别也落后了：轮到它时照 retranslate() 的样子什么都不发，
    不把桌面正在等的这一条标成失败。"""
    for i in (1, 2):
        pl._recent[i] = {"text": "x"}
    audit = FakeAudit()
    pl.audit = audit
    _shown(pl, audit, "ok")
    gate = None
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)
        await gate.wait()

    pl.retranslate = fake_retranslate

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        first = asyncio.ensure_future(pl._on_viewer_action("retranslate", {"id": 1}, "1.2.3.4"))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(pl._on_viewer_action("retranslate", {"id": 2}, "1.2.3.4"))
        await asyncio.sleep(0.01)
        pl._strong_inflight.add(2)                    # 桌面那边开始重译第 2 条
        _shown(pl, audit, "lagging")
        gate.set()
        await asyncio.gather(first, second)

    run(scenario())
    assert calls == [1]
    assert _updates(pl, 2) == []
    assert pl._viewer_action_pending == 0
    assert [r[2] for r in _viewer_actions(audit)] == [1, 2]


def test_the_refusal_text_is_paired_and_only_states_what_happened():
    """规则九：成对写；规则八：只写观察和能做的事，不写「可能 / 似乎 / might」。"""
    text = viewer_share_mod.VIEWER_RETRANSLATE_PAUSED
    assert str(text) == "识别落后时，手机上点的重译先不执行"
    assert text.en.startswith("Retranslating from a phone is paused")
    for word in ("可能", "似乎", "might", "maybe", "probably"):
        assert word not in str(text) and word not in text.en
