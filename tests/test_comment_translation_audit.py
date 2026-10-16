"""弹幕翻译没译出来时记下原因（2026-10-14：直播中一条弹幕只显示原文，事后查不到为什么）。

打包运行时终端输出指向 /dev/null，所以原因要进会话审计：
  * 每批有没译出来的，写一条 comment_translation_failed，只有代码和数字（弹幕与审计隔离）；
  * 这一场的结局计数并进 session_end 的 comment_translation，晚到的旧任务拿不到下一场的数；
  * 被判成「不像译文」的回话从重复句缓存里拿掉，同一句下次重新问。

测试一律用假 HTTP 层，不联网。
"""
import asyncio
import json
import time

from app import comments as comments_mod
from app.comments import CommentTranslator, implausible_reason, plausible_translation
from app.translator import CachedTranslator
from tests.helpers import run
from tests.test_claude_comments import (META_REPLY, Sibling, items, make, reply, subtitle_engine,
                                        updates)
from tests.test_claude_translator import Resp, message
from tests.test_resilience_translation import wait_for


class FakeAudit:
    def __init__(self):
        self.records = []

    def comment_translation_failed(self, engine, model, batch, reasons, ms, **extra):
        # extra 只有重发过的批次才有（retried / rescued 及按原因拆开的两个），和真的 AuditLog 一样
        self.records.append(dict({"engine": engine, "model": model, "batch": batch,
                                  "reasons": dict(reasons), "ms": ms}, **extra))


def scenario_with(monkeypatch, made, *texts, **overrides):
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    audit = FakeAudit()
    holder = {"tr": wrapped, "session": audit}
    ct, sent = make(holder, made, **overrides)

    async def go():
        await ct.accept(items(*texts))
        assert await wait_for(lambda: len(updates(sent)) == len(texts))
        await ct.close()

    run(go())
    return ct, audit, sent


def test_a_429_is_recorded_as_its_status_for_every_comment_in_the_batch(monkeypatch):
    made = Sibling([Resp(429, {"error": {"type": "rate_limit_error"}}, headers={"retry-after": "60"})])
    ct, audit, _ = scenario_with(monkeypatch, made, "hola amigos", "que precio tiene")
    assert audit.records == [{"engine": "claude", "model": "claude-haiku-5-5", "batch": 2,
                              "reasons": {"http_429": 2}, "ms": audit.records[0]["ms"]}]
    assert ct.translation_summary(audit) == {"ok": 0, "failed": {"http_429": 2}}


def test_requests_skipped_while_paused_are_recorded_as_cooldown(monkeypatch):
    made = Sibling([Resp(429, {"error": {"type": "rate_limit_error"}}, headers={"retry-after": "60"})])
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    audit = FakeAudit()
    ct, sent = make({"tr": wrapped, "session": audit}, made)

    async def go():
        await ct.accept(items("hola amigos"))
        assert await wait_for(lambda: len(updates(sent)) == 1)
        await ct.accept({"items": [{"id": "later", "user": "u", "text": "otra pregunta"}]})
        assert await wait_for(lambda: len(updates(sent)) == 2)
        await ct.close()

    run(go())
    assert [r["reasons"] for r in audit.records] == [{"http_429": 1}, {"cooldown": 1}]


def test_a_refused_comment_is_recorded_as_refusal_and_the_rest_count_as_ok(monkeypatch):
    refusal = dict(message(stop="refusal"), stop_details={"type": "refusal", "category": None})
    made = Sibling([Resp(200, refusal), reply("你好"), Resp(200, refusal)])
    ct, audit, sent = scenario_with(monkeypatch, made, "hola", "malo malo")
    assert updates(sent) == {"c0": ("ok", "你好"), "c1": ("failed", None)}
    assert [r["reasons"] for r in audit.records] == [{"refusal": 1}]
    assert ct.translation_summary(audit) == {"ok": 1, "failed": {"refusal": 1}}


def test_a_reply_instead_of_a_translation_is_recorded_with_a_reply_code(monkeypatch):
    made = Sibling([reply(META_REPLY), reply(META_REPLY)])       # 第一次和重发都是回话
    _ct, audit, sent = scenario_with(monkeypatch, made, "Sii todo")
    assert updates(sent)["c0"] == ("failed", None)
    (why,) = audit.records[0]["reasons"]
    assert why.startswith("reply_")
    assert (audit.records[0]["retried"], audit.records[0]["rescued"]) == (1, 0)
    assert (audit.records[0]["retried_reasons"], audit.records[0]["rescued_reasons"]) == \
        ({why: 1}, {})


def test_a_batch_that_runs_past_the_deadline_is_recorded_as_timeout(monkeypatch):
    class Stuck:
        name = "hymt2"

        async def translate(self, text, target, source="auto", glossary=None):
            await asyncio.sleep(3600)

    audit = FakeAudit()
    sent = []

    async def broadcast(msg):
        sent.append(msg)

    ct = CommentTranslator(broadcast=broadcast, translator=lambda: Stuck(), target=lambda: "zh-CN",
                           session=lambda: audit)
    ct.BATCH_WINDOW_SEC = 0.01
    ct.TRANSLATE_TIMEOUT_SEC = 0.05

    async def go():
        await ct.accept(items("hola", "adios"))
        assert await wait_for(lambda: len(updates(sent)) == 2)

    run(go())
    assert [(r["engine"], r["batch"], r["reasons"]) for r in audit.records] == \
        [("hymt2", 2, {"timeout": 2})]


def test_the_audit_record_holds_only_codes_and_numbers(monkeypatch):
    """没有重发的批次：记录还是原来的 5 个键（重发关掉，回话就直接失败）。"""
    monkeypatch.setattr(comments_mod, "REPLY_RETRIES", 0)
    made = Sibling([reply("1. 你好朋友们\n2. " + META_REPLY)])
    _ct, audit, _ = scenario_with(monkeypatch, made, "hola amigos", "Sii todo secreto")
    raw = json.dumps(audit.records, ensure_ascii=False)
    for needle in ("hola", "Sii", "secreto", "朋友", META_REPLY[:6]):
        assert needle not in raw, needle
    assert set(audit.records[0]) == {"engine", "model", "batch", "reasons", "ms"}


def test_a_retried_batch_adds_only_the_retry_keys_to_the_record(monkeypatch):
    made = Sibling([reply("1. 你好朋友们\n2. " + META_REPLY), reply(META_REPLY)])
    _ct, audit, _ = scenario_with(monkeypatch, made, "hola amigos", "Sii todo secreto")
    raw = json.dumps(audit.records, ensure_ascii=False)
    for needle in ("hola", "Sii", "secreto", "朋友", META_REPLY[:6]):
        assert needle not in raw, needle
    assert set(audit.records[0]) == {"engine", "model", "batch", "reasons", "ms",
                                     "retried", "rescued", "retried_reasons", "rescued_reasons"}
    assert (audit.records[0]["retried"], audit.records[0]["rescued"]) == (1, 0)


def test_successful_batches_write_no_record_but_are_counted(monkeypatch):
    made = Sibling([reply("1. 你好\n2. 多少钱")])
    ct, audit, _ = scenario_with(monkeypatch, made, "hola", "cuanto cuesta")
    assert audit.records == []
    assert ct.translation_summary(audit) == {"ok": 2, "failed": {}}


def test_the_summary_belongs_to_one_session_only(monkeypatch):
    """CommentTranslator 跨场常驻：换了一场就从零数，旧场的 audit 拿不到新场的数（CLAUDE.md 第七条）。"""
    made = Sibling([reply("你好"), reply("再见")])
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    first, second = FakeAudit(), FakeAudit()
    holder = {"tr": wrapped, "session": first}
    ct, sent = make(holder, made)

    async def go():
        await ct.accept({"items": [{"id": "a", "user": "u", "text": "hola"}]})
        assert await wait_for(lambda: len(updates(sent)) == 1)
        holder["session"] = second
        await ct.accept({"items": [{"id": "b", "user": "u", "text": "adios"}]})
        assert await wait_for(lambda: len(updates(sent)) == 2)
        await ct.close()

    run(go())
    assert ct.translation_summary(first) is None
    assert ct.translation_summary(second) == {"ok": 1, "failed": {}}
    assert ct.translation_summary(None) is None


def test_a_rejected_reply_is_dropped_from_the_repeat_cache(monkeypatch):
    """同一句下次要重新问：缓存里留着那句回话，这一场这句就再也翻不出来了。"""
    # 第一条：第一次和重发都是回话 → 失败；第二条同样的话：缓存里什么都没留，重新问 → 译出来
    made = Sibling([reply(META_REPLY), reply(META_REPLY), reply("什么都行")])
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    audit = FakeAudit()
    ct, sent = make({"tr": wrapped, "session": audit}, made, DEDUPE_CONTENT_SEC=0.0)

    async def go():
        await ct.accept({"items": [{"id": "a", "user": "u", "text": "Sii todo"}]})
        assert await wait_for(lambda: len(updates(sent)) == 1)
        await ct.accept({"items": [{"id": "b", "user": "v", "text": "Sii todo"}]})
        assert await wait_for(lambda: len(updates(sent)) == 2)
        await ct.close()

    run(go())
    assert updates(sent) == {"a": ("failed", None), "b": ("ok", "什么都行")}
    assert len(made.made[0].session_log.posts) == 3          # 第一条两次（含重发）+ 第二条一次


def test_retried_and_rescued_counts_reach_the_summary_and_the_failure_record(monkeypatch):
    """c0 本来就好；c1 被判成回话、重发后译出来（救回）；c2 重发也是回话（仍失败，记第一次的代码）。"""
    made = Sibling([reply("1. 你好朋友们\n2. " + META_REPLY + "\n3. " + META_REPLY),
                    reply("是的，全部"), reply(META_REPLY)])
    ct, audit, sent = scenario_with(monkeypatch, made, "hola amigos", "Sii todo", "Sii tambien")
    assert updates(sent) == {"c0": ("ok", "你好朋友们"), "c1": ("ok", "是的，全部"),
                             "c2": ("failed", None)}
    (record,) = audit.records
    assert (record["batch"], record["reasons"], record["retried"], record["rescued"]) == \
        (3, {"reply_meta": 1}, 2, 1)
    assert (record["retried_reasons"], record["rescued_reasons"]) == \
        ({"reply_meta": 2}, {"reply_meta": 1})
    assert ct.translation_summary(audit) == {"ok": 2, "failed": {"reply_meta": 1},
                                             "retried": 2, "rescued": 1,
                                             "retried_reasons": {"reply_meta": 2},
                                             "rescued_reasons": {"reply_meta": 1}}


def test_the_rescue_rate_can_be_worked_out_per_first_reason_code(monkeypatch):
    """被救回的不在 reasons 里：不按第一次的原因拆开，「救回率低于两成就撤掉」只能看总数，
    reply_meta 常被救回、reply_fabricated 从不被救回时，总数读起来像整体失败。"""
    made = Sibling([reply("1. 你好朋友们\n2. " + META_REPLY + "\n3. 我付了钱"),
                    reply("是的，全部"), reply("我付了钱")])      # c1 重发后译出；c2 重发还是丢了数字
    ct, audit, sent = scenario_with(monkeypatch, made, "hola amigos", "Sii todo",
                                    "pague 20 dolares")
    assert updates(sent) == {"c0": ("ok", "你好朋友们"), "c1": ("ok", "是的，全部"),
                             "c2": ("failed", None)}
    (record,) = audit.records
    assert record["reasons"] == {"reply_fabricated": 1}
    assert (record["retried_reasons"], record["rescued_reasons"]) == \
        ({"reply_meta": 1, "reply_fabricated": 1}, {"reply_meta": 1})
    summary = ct.translation_summary(audit)
    assert (summary["retried_reasons"], summary["rescued_reasons"]) == \
        (record["retried_reasons"], record["rescued_reasons"])
    assert sum(summary["retried_reasons"].values()) == summary["retried"] == 2
    assert sum(summary["rescued_reasons"].values()) == summary["rescued"] == 1


def test_a_batch_rescued_entirely_writes_no_failure_record_but_is_counted(monkeypatch):
    made = Sibling([reply(META_REPLY), reply("是的，全部")])
    ct, audit, sent = scenario_with(monkeypatch, made, "Sii todo")
    assert updates(sent) == {"c0": ("ok", "是的，全部")}
    assert audit.records == []
    # 整批救回来不写 comment_translation_failed，按原因拆开的数就只能从场末计数里看
    assert ct.translation_summary(audit) == {"ok": 1, "failed": {}, "retried": 1, "rescued": 1,
                                             "retried_reasons": {"reply_meta": 1},
                                             "rescued_reasons": {"reply_meta": 1}}


def test_the_retry_counts_are_this_sessions_only(monkeypatch):
    made = Sibling([reply(META_REPLY), reply("是的，全部"), reply("你好")])
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    first, second = FakeAudit(), FakeAudit()
    holder = {"tr": wrapped, "session": first}
    ct, sent = make(holder, made)

    async def go():
        await ct.accept({"items": [{"id": "a", "user": "u", "text": "Sii todo"}]})
        assert await wait_for(lambda: len(updates(sent)) == 1)
        holder["session"] = second
        await ct.accept({"items": [{"id": "b", "user": "u", "text": "hola"}]})
        assert await wait_for(lambda: len(updates(sent)) == 2)
        await ct.close()

    run(go())
    assert ct.translation_summary(first) is None
    assert ct.translation_summary(second) == {"ok": 1, "failed": {}}     # 这一场没重发过，不带两个键


def test_the_real_audit_log_writes_the_retry_keys_only_when_something_was_retried(tmp_path):
    from app.audit import AuditLog
    audit = AuditLog(room_url="https://www.tiktok.com/@x/live", log_dir=tmp_path)
    audit.comment_translation_failed("claude", "claude-haiku-5-5", 1, {"reply_meta": 1}, 900)
    audit.comment_translation_failed("claude", "claude-haiku-5-5", 3, {"reply_meta": 1}, 1900,
                                     retried=2, rescued=1,
                                     retried_reasons={"reply_meta": 2},
                                     rescued_reasons={"reply_meta": 1})
    audit.close(reason="user_stop")
    plain, retried = [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()
                      if '"comment_translation_failed"' in line]
    assert set(plain) == {"type", "at", "engine", "model", "batch", "reasons", "ms"}
    assert set(retried) == set(plain) | {"retried", "rescued", "retried_reasons", "rescued_reasons"}
    assert (retried["retried"], retried["rescued"]) == (2, 1)
    assert (retried["retried_reasons"], retried["rescued_reasons"]) == \
        ({"reply_meta": 2}, {"reply_meta": 1})


def test_cached_translator_forget_drops_only_that_sentence():
    class Echo:
        name = "hymt2"

        def __init__(self):
            self.calls = 0

        async def translate(self, text, target, source="auto", glossary=None):
            self.calls += 1
            return "[{}] {}".format(self.calls, text)

    inner = Echo()
    tr = CachedTranslator(inner)

    async def go():
        a1 = await tr.translate("uno", "zh-CN")
        b1 = await tr.translate("dos", "zh-CN")
        tr.forget("uno", "zh-CN")
        a2 = await tr.translate("uno", "zh-CN")
        b2 = await tr.translate("dos", "zh-CN")
        return a1, b1, a2, b2

    a1, b1, a2, b2 = run(go())
    assert a1 != a2 and b1 == b2
    tr.forget("never seen", "zh-CN")          # 没缓存过的不报错


def test_implausible_reason_agrees_with_plausible_translation():
    cases = [("pero no se", "请提供更多上下文。"), ("pero no se", "但我不知道"),
             ("hola", ""), ("¿qué? ¿cómo?", "好的")]
    for text, out in cases:
        assert plausible_translation(text, out, "zh-CN") is (implausible_reason(text, out, "zh-CN") is None)


def test_why_no_output_only_blames_a_status_seen_on_this_call():
    from app.comments import why_no_output

    class T:
        last_error = None

    t = T()
    before = t.last_error
    assert why_no_output(t, False, before, None) == "no_output"
    t.last_error = (500, None)
    assert why_no_output(t, False, before, None) == "http_500"
    assert why_no_output(t, False, t.last_error, None) == "no_output"    # 旧的状态码不算到这一次
    assert why_no_output(t, True, before, None) == "cooldown"
    assert why_no_output(t, False, before, ("refusal", None)) == "refusal"


def test_session_end_carries_this_sessions_comment_translation_counts(monkeypatch, tmp_path):
    """真的审计文件：失败那一批有一条 comment_translation_failed，session_end 带着本场的计数。"""
    from app import resolver
    from tests.test_comments import make_pipeline
    monkeypatch.setattr(resolver, "_login_first_enabled", lambda: False)
    p, _server = make_pipeline(monkeypatch, tmp_path)

    class Engine:
        name = "claude"
        model = "claude-haiku-5-5"

    async def go():
        await p._begin_session("https://www.tiktok.com/@abc/live")
        audit = p.audit
        p.comments._note_outcome(audit, Engine(), 3, 2, {"http_500": 1}, time.monotonic())
        await p._end_session(audit, reason="offline")
        return audit

    audit = run(go())
    records = [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    failed = [r for r in records if r["type"] == "comment_translation_failed"]
    assert [(r["engine"], r["model"], r["batch"], r["reasons"]) for r in failed] == \
        [("claude", "claude-haiku-5-5", 3, {"http_500": 1})]
    end = next(r for r in records if r["type"] == "session_end")
    assert end["comment_translation"] == {"ok": 2, "failed": {"http_500": 1}}
