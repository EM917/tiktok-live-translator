"""字幕译文在「回话」而不是在翻译：这一句显示原文（2026-10-14）。

2026-10-13/14 五场 Claude 字幕 2527 句里 97 句（3.8%）不是干净的译文：英语短句被当成对它的请求
（「I can't tell what "them" refers to…」「I can't draw images here」）、一段拒绝加就医建议、
译完加「Note:」「Wait, let me correct that」。这里钉住：
  * 检测规则（translator.looks_like_reply）认得出这几类，正常译文不误判；
  * 快译、降级引擎、重译、报警上下文四条路都把回话拦下，不上屏、不进审计的译文栏；
  * 拦下来的是「接口通着、回的不是译文」：不报引擎故障、不拉本地引擎，页面不算进连续失败；
  * 回话的原话记进 translation_rejected，并从重复句缓存里拿掉；
  * Claude 字幕的请求把这一句包在 <subtitle> 里，提示词写明不是对它说的话；
  * 只有 Claude 字幕被拦下后原样重试一次（2026-10-15：#90 之后 6/2098 = 0.29%，都是模型把自言自语
    写进了译文）：重试要绕过重复句缓存、请求体和第一次一模一样、最多两次请求；第一次没有返回文字
    （超时、HTTP 错误、429、冷却、拒答）不重试；引擎在冷却、已被换掉或字幕队列里有 2 条以上在等时跳过；重试被拒答记 retry=refusal 和
    translation_refusal（这一条仍标「结果不像译文」）；
    本地 Hy-MT2 等温度为 0 的引擎不重试。审计的 translation_rejected 只在 Claude 重试这条路上多
    attempt / retry / retry_ms 三个键。

例句改写自本机日志，人名换成虚构的。测试不联网。
"""
import asyncio
import time

import pytest

from app import pipeline as pipeline_mod
from app.pipeline import QUALITY_FAST
from app.translator import CachedTranslator, ClaudeTranslator, looks_like_reply
from tests.helpers import run
from tests.test_resilience_translation import (Fast, Strong, bare_pipeline, job, noop, rows)

ZH = "zh-CN"


@pytest.mark.parametrize("source,out", [
    ("Just add them.",
     "I can't tell what \"them\" refers to, since your message doesn't include any text to translate. "
     "Please send the line or lines you want translated into Simplified Chinese."),
    ("Yeah, if you're gonna do pills, it's two.",
     "我不能翻译这句话。它看起来是在鼓励服用药物。如果你需要翻译其他内容，我很乐意帮忙。"),
    ("El descafeinado.", "咖啡因。  Wait, let me correct that: the Spanish means decaf, so: 无咖啡因咖啡"),
    ("Draw a house and give us a max.",
     "I can't draw images here. I'm set up as a translation engine that outputs only Simplified Chinese."),
    ("¿Cómo se dice?", "Please provide the text you'd like translated. Your message is a question."),
    ("Which drops do you want to run?",
     "我无法理解这句话的上下文。我的任务是把你给出的文本翻译成简体中文，所以如果你提供要翻译的内容，我会直接输出译文。"),
    ("Uno con más de 20 billones CFU.", "一个含有超过200亿CFU的益生菌。  Note on the source: the speaker corrected themselves."),
])
def test_replies_seen_in_the_logs_are_caught(source, out):
    assert looks_like_reply(source, out, ZH) == "reply_meta"


def test_an_english_reply_to_a_spanish_line_is_caught_without_any_keyword():
    out = "How much are they? 40 dollars. But we get the boots completely for free!"
    assert looks_like_reply("¿Por cuánto están? 40 dólares.", out, ZH) == "reply_no_cjk"


@pytest.mark.parametrize("source,out", [
    ("¿Tú tienes? Sí, sí. Ok, aquí está.", "¿Tienes tú? Sí, sí. Vale, aquí está."),     # 改写后回成西语
    ("Y no te pierdas de ninguno de nuestros...", "Y no te pierdas de ninguno de nuestros..."),
    ("The mushroom coffee? Luna? Luna, ¿estás ahí?", "¿El café de hongos? ¿Luna? Luna, ¿estás ahí?"),
])
def test_a_line_returned_in_its_own_language_is_caught(source, out):
    assert looks_like_reply(source, out, ZH) == "reply_untranslated"


@pytest.mark.parametrize("source,out", [("Luna?", "Luna?"), ("Gracias.", "Gracias."), ("BB. BB.", "BB. BB.")])
def test_a_name_or_a_word_kept_as_is_is_not_flagged(source, out):
    assert looks_like_reply(source, out, ZH) is None


def test_a_long_aside_after_a_short_line_is_caught_by_length():
    out = "把你的朋友叫过来吧！(Spanish slang \"Mándala pa' acá\" = \"Send her over here, comadre!\")"
    assert looks_like_reply("¡Mándala comadre pa' acá!", out, ZH) == "reply_too_long"


@pytest.mark.parametrize("source,out,target", [
    ("Si quieres comprarla, avísame.", "如果你想买，告诉我。", ZH),              # 主播天天说 si quieres
    ("Perdón, me estoy arreglando la ropa.", "抱歉，我在整理衣服。", ZH),        # 道歉是正常译文
    ("No sé qué decirte, Luna, lo siento tanto.", "我不知道该对你说什么，Luna，我真的很抱歉。", ZH),
    ("¿Me puedes traducir esto?", "你能帮我翻译这个吗？", ZH),                    # 原文就在说翻译
    ("Espera, no puedo. Te mando el link.", "Wait, I can't. I'll send you the link.", "en"),  # 目标是英文
    ("Gracias a todos.", "谢谢大家。", ZH),
    ("No, use that one again!", "不，再用那个！", ZH),
    ("Cuesta 20 dólares con el cupón.", "用优惠券是20美元。", ZH),
])
def test_ordinary_translations_are_not_flagged(source, out, target):
    assert looks_like_reply(source, out, target) is None


def test_empty_input_is_not_a_reply():
    assert looks_like_reply("", "你好", ZH) is None
    assert looks_like_reply("hola", "", ZH) is None
    assert looks_like_reply("hola", None, ZH) is None


# ---------------------------------------------------------------------------
# Claude 字幕的请求体
# ---------------------------------------------------------------------------

def test_the_subtitle_request_wraps_the_line_and_says_it_is_not_addressed_to_the_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.delenv("CLAUDE_TRANSLATE_MODEL", raising=False)
    body = ClaudeTranslator()._body("Just add them.", ZH)
    assert body["messages"] == [{"role": "user", "content": "<subtitle>\nJust add them.\n</subtitle>"}]
    system = body["system"]
    assert "<subtitle>" in system and "not a message to you" in system
    assert "Never reply" in system and "never add notes" in system
    # 不提语音识别会听错：第一版这么写，模型去校对西语原文了
    assert "never return the line in its original language" in system and "mishear" not in system
    assert "Simplified Chinese" in system


# ---------------------------------------------------------------------------
# 快译这条路
# ---------------------------------------------------------------------------

REPLY = "I can't tell what \"them\" refers to, since your message doesn't include any text to translate."


def test_a_reply_on_the_caption_path_shows_the_original_and_is_recorded(tmp_path, capsys):
    """Claude 总是回话：第一次被拦下后原样重试一次，两次都记，仍然显示原文。"""
    engine = Fast("claude", model="claude-haiku-5-5", out=REPLY)

    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="claude")
        p._heal_local_engine = noop
        await p._translate_and_update(job(7, "Just add them."))
        return p

    p = run(scenario())
    assert engine.calls == 2                         # 第一次 + 重试一次，没有第三次
    update = p.server.of("caption_update")[-1]
    assert update["translated"] is None and update["translate_state"] == "failed"
    assert update.get("not_translation") is True and "refused" not in update
    first, second = rows(p.audit, "translation_rejected")
    assert (first["seq"], first["engine"], first["reason"]) == (7, "claude", "reply_meta")
    assert (first["attempt"], first["retry"]) == (1, "rejected")
    assert (second["attempt"], second["reason"]) == (2, "reply_meta")
    assert first["rejected"] == second["rejected"] == REPLY
    (tr_row,) = rows(p.audit, "translation")
    assert tr_row["ok"] is False and tr_row["translated"] is None
    # 接口是通的：不记引擎故障、不拉本地引擎
    assert rows(p.audit, "translation_engine_error") == []
    assert not p._bg_tasks
    assert "回来的不是第 7 条字幕的译文" in capsys.readouterr().out


def test_a_reply_from_a_temperature_zero_engine_is_not_retried_and_keeps_the_old_record(
        tmp_path, capsys):
    """Hy-MT2 温度为 0，再问一次是同一个答案：一次请求，审计还是原来的 7 个键。"""
    engine = Fast("hymt2", out=REPLY)

    async def scenario():
        p = bare_pipeline(tmp_path, engine)
        p._heal_local_engine = noop
        await p._translate_and_update(job(7, "Just add them."))
        return p

    p = run(scenario())
    assert engine.calls == 1
    (rejected,) = rows(p.audit, "translation_rejected")
    assert set(rejected) == {"type", "seq", "at", "engine", "model", "reason", "rejected"}
    assert (rejected["seq"], rejected["engine"], rejected["reason"]) == (7, "hymt2", "reply_meta")
    assert p.server.of("caption_update")[-1].get("not_translation") is True
    out = capsys.readouterr().out
    assert "回来的不是第 7 条字幕的译文（reply_meta），这一条显示原文" in out and "重试" not in out


class Scripted:
    """按脚本回应的 Claude 字幕引擎替身：answers 逐次取，None 是这一次没有返回文字。
    cooldown_until / fail_streak 和真引擎一样挂在对象上，给跳过重试的用例设。"""

    name = "claude"
    model = "claude-haiku-5-5"

    def __init__(self, answers, fail_streak=0, cooldown_until=0.0):
        self.answers = list(answers)
        self.fail_streak = fail_streak
        self.cooldown_until = cooldown_until
        self.last_error = None
        self.calls = 0

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls += 1
        return self.answers.pop(0)

    async def close(self):
        pass


GOOD = "加进去就行。"
REPLY2 = "Please send the line you want translated into Simplified Chinese."


def caption(p, seq=1):
    """最后一次发给页面的这一条字幕更新。"""
    return [m for m in p.server.of("caption_update") if m["id"] == seq][-1]


def test_a_caught_reply_is_dropped_from_the_repeat_cache(tmp_path):
    """第一次被拦下、重试也被拦下：缓存里什么都不留，下一句同样的话重新问（原先 [REPLY, ok] 的
    意图；现在 [REPLY, ok] 在第一句里就被重试消化了，所以用 [REPLY, REPLY, ok] 守着它）。"""
    inner = Scripted([REPLY, REPLY, GOOD])

    async def scenario():
        p = bare_pipeline(tmp_path, CachedTranslator(inner), engine="claude")
        p._heal_local_engine = noop
        await p._translate_and_update(job(1, "Just add them."))
        assert inner.calls == 2 and caption(p, 1)["translated"] is None
        await p._translate_and_update(job(2, "Just add them."))
        return p

    p = run(scenario())
    assert inner.calls == 3                          # 第二句重新问了，没从缓存拿回那句回话
    assert caption(p, 2)["translated"] == GOOD


# ---------------------------------------------------------------------------
# Claude 字幕被拦下后原样重试一次
# ---------------------------------------------------------------------------

def one_line(tmp_path, engine, text="Just add them.", queue_depth=None, seq=1):
    """跑一条字幕，返回 Pipeline。queue_depth 不为 None 时模拟字幕队列里有这么多条在等。"""
    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="claude")
        p._heal_local_engine = noop
        if queue_depth is not None:
            p._trans_queue = asyncio.Queue(maxsize=4)
            for n in range(queue_depth):
                p._trans_queue.put_nowait({"id": 100 + n})
        await p._translate_and_update(job(seq, text))
        return p

    return run(scenario())


def test_a_reply_then_a_clean_translation_shows_the_translation_and_records_one_attempt(
        tmp_path, capsys):
    engine = Scripted([REPLY, GOOD])
    p = one_line(tmp_path, engine)
    assert engine.calls == 2
    update = caption(p)
    assert update["translated"] == GOOD and update["translate_state"] == "ok"
    assert "not_translation" not in update and "refused" not in update
    (rejected,) = rows(p.audit, "translation_rejected")
    assert (rejected["attempt"], rejected["retry"], rejected["reason"]) == (1, "ok", "reply_meta")
    assert rejected["rejected"] == REPLY
    assert isinstance(rejected["retry_ms"], int) and rejected["retry_ms"] >= 0
    (tr_row,) = rows(p.audit, "translation")
    assert tr_row["ok"] is True and tr_row["translated"] == GOOD
    assert rows(p.audit, "translation_engine_error") == []
    assert not p._bg_tasks
    out = capsys.readouterr().out
    assert "回来的不是第 1 条字幕的译文（reply_meta），重试一次后译出" in out


def test_two_replies_in_a_row_show_the_original_and_record_both_attempts(tmp_path, capsys):
    engine = Scripted([REPLY, REPLY2, GOOD])
    p = one_line(tmp_path, engine)
    assert engine.calls == 2 and engine.answers == [GOOD]      # 没有第三次请求
    update = caption(p)
    assert update["translated"] is None and update["translate_state"] == "failed"
    assert update["not_translation"] is True and "refused" not in update
    first, second = rows(p.audit, "translation_rejected")
    assert (first["attempt"], first["retry"], first["rejected"]) == (1, "rejected", REPLY)
    assert isinstance(first["retry_ms"], int)
    assert (second["attempt"], second["rejected"]) == (2, REPLY2)
    assert "retry" not in second                 # 第二次后面没有下一次
    (tr_row,) = rows(p.audit, "translation")
    assert tr_row["ok"] is False and tr_row["translated"] is None
    assert rows(p.audit, "translation_engine_error") == [] and not p._bg_tasks
    assert "重试一次也不是译文（reply_meta），这一条显示原文" in capsys.readouterr().out


def test_clean_lines_make_exactly_one_request_each_and_a_cache_hit_makes_none(tmp_path):
    inner = Scripted(["你好", "再见", "谢谢"])

    async def scenario():
        p = bare_pipeline(tmp_path, CachedTranslator(inner), engine="claude")
        p._heal_local_engine = noop
        for seq, text in enumerate(("Hola", "Adios", "Gracias"), 1):
            await p._translate_and_update(job(seq, text))
        assert inner.calls == 3
        await p._translate_and_update(job(4, "Hola"))       # 重复句：命中缓存
        return p

    p = run(scenario())
    assert inner.calls == 3
    assert caption(p, 4)["translated"] == "你好"
    assert rows(p.audit, "translation_rejected") == []


def test_the_retry_goes_around_the_repeat_cache_and_a_good_result_is_cached(tmp_path):
    inner = Scripted([REPLY, GOOD])

    async def scenario():
        p = bare_pipeline(tmp_path, CachedTranslator(inner), engine="claude")
        p._heal_local_engine = noop
        await p._translate_and_update(job(1, "Just add them."))
        assert inner.calls == 2                   # 重试真的发到了引擎，没被缓存里那句回话挡住
        await p._translate_and_update(job(2, "Just add them."))
        return p

    p = run(scenario())
    assert inner.calls == 2                       # 重复句用的是重试译出的好译文，0 次新请求
    assert caption(p, 1)["translated"] == caption(p, 2)["translated"] == GOOD


def test_the_retry_sends_the_identical_request_without_sampling_fields(monkeypatch, tmp_path):
    from tests.test_claude_comments import subtitle_engine
    from tests.test_claude_translator import Resp, message, text_block
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block(REPLY))),
                         Resp(200, message(text_block(GOOD)))]
    p = one_line(tmp_path, wrapped)
    first, second = session.posts
    assert first["body"] == second["body"] and first["url"] == second["url"]
    body = second["body"]
    for key in ("temperature", "top_p", "top_k"):
        assert key not in body
    assert body["messages"][-1]["role"] == "user"
    assert [m["role"] for m in body["messages"]] == ["user"]      # 没有预填的 assistant 轮
    assert caption(p)["translated"] == GOOD


@pytest.mark.parametrize("kind", ["timeout", "http_500", "http_429", "refusal"])
def test_no_retry_when_the_first_request_returned_nothing(monkeypatch, tmp_path, kind):
    from tests.test_claude_comments import subtitle_engine
    from tests.test_claude_translator import Resp, message
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    if kind == "timeout":
        def post(url, data=None, headers=None, **kw):
            session.posts.append({"url": url})
            raise asyncio.TimeoutError()
        session.post = post
    else:
        session.responses = [
            {"http_500": Resp(500, {"error": {"type": "api_error"}}),
             "http_429": Resp(429, {"error": {"type": "rate_limit_error"}},
                              headers={"retry-after": "60"}),
             "refusal": Resp(200, dict(message(stop="refusal"),
                                       stop_details={"type": "refusal", "category": "cyber"}))}[kind]]
    p = one_line(tmp_path, wrapped)
    assert len(session.posts) == 1
    assert rows(p.audit, "translation_rejected") == []
    update = caption(p)
    assert update["translated"] is None and update["translate_state"] == "failed"
    assert "not_translation" not in update
    # 原来的各条路径照旧：拒答记 translation_refusal，429 记 translation_cooldown
    assert update.get("refused", False) is (kind == "refusal")
    assert len(rows(p.audit, "translation_refusal")) == (1 if kind == "refusal" else 0)
    assert len(rows(p.audit, "translation_cooldown")) == (1 if kind == "http_429" else 0)


def test_no_request_and_no_retry_while_the_engine_is_paused(monkeypatch, tmp_path):
    from tests.test_claude_comments import subtitle_engine
    wrapped, inner, session = subtitle_engine(monkeypatch)
    inner._cooldown_until = time.monotonic() + 60
    p = one_line(tmp_path, wrapped)
    assert session.posts == []                   # 暂停期间第一次就没发，更不会有重试
    assert rows(p.audit, "translation_rejected") == []


def test_a_retry_that_returns_nothing_keeps_the_original_and_is_not_escalated(tmp_path, capsys):
    engine = Scripted([REPLY, None])
    p = one_line(tmp_path, engine)
    assert engine.calls == 2
    update = caption(p)
    assert update["translated"] is None and update["not_translation"] is True
    (rejected,) = rows(p.audit, "translation_rejected")
    assert (rejected["attempt"], rejected["retry"]) == (1, "no_output")
    assert isinstance(rejected["retry_ms"], int)
    assert rows(p.audit, "translation_engine_error") == [] and not p._bg_tasks
    assert "重试一次没有返回译文，这一条显示原文" in capsys.readouterr().out


def test_a_retry_that_is_refused_stays_not_a_translation_but_is_recorded_as_a_refusal(
        monkeypatch, tmp_path, capsys):
    """第一次回了话、重试被拒答：这一条按「回的不是译文」显示原文（不改标成 refused），但拒答照老规矩
    记 translation_refusal、打终端、本场提示一次——敏感的句子最容易在重发时真被拒，不记的话事后
    分不清它和超时、空输出。"""
    from tests.test_claude_comments import subtitle_engine
    from tests.test_claude_translator import Resp, message, text_block
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block(REPLY))),
                         Resp(200, dict(message(stop="refusal"),
                                        stop_details={"type": "refusal", "category": "general_harms"}))]
    p = one_line(tmp_path, wrapped)
    assert len(session.posts) == 2
    update = caption(p)
    assert update["translated"] is None and update["not_translation"] is True
    assert "refused" not in update
    (rejected,) = rows(p.audit, "translation_rejected")
    assert (rejected["attempt"], rejected["retry"]) == (1, "refusal")
    assert isinstance(rejected["retry_ms"], int)
    (refusal,) = rows(p.audit, "translation_refusal")
    assert (refusal["seq"], refusal["engine"], refusal["model"], refusal["category"]) == \
        (1, "claude", "claude-haiku-5-5", "general_harms")
    (notice,) = p.server.of("notice")
    assert "refusal" in notice["text"] and "原文" in notice["text"]
    assert not p.server.of("incident") and rows(p.audit, "translation_engine_error") == []
    assert not p._bg_tasks
    out = capsys.readouterr().out
    assert "重试一次被拒答，这一条显示原文" in out
    assert out.count("stop_reason=refusal，类别 general_harms") == 1


def test_a_second_refused_retry_is_audited_again_without_a_second_notice(monkeypatch, tmp_path):
    from tests.test_claude_comments import subtitle_engine
    from tests.test_claude_translator import Resp, message, text_block
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    refused = dict(message(stop="refusal"), stop_details={"type": "refusal", "category": None})
    session.responses = [Resp(200, message(text_block(REPLY))), Resp(200, refused),
                         Resp(200, message(text_block(REPLY))), Resp(200, refused)]

    async def scenario():
        p = bare_pipeline(tmp_path, wrapped, engine="claude")
        p._heal_local_engine = noop
        await p._translate_and_update(job(1, "Just add them."))
        await p._translate_and_update(job(2, "Just add them."))
        return p

    p = run(scenario())
    assert len(session.posts) == 4
    assert [r["seq"] for r in rows(p.audit, "translation_refusal")] == [1, 2]
    assert [r["retry"] for r in rows(p.audit, "translation_rejected")] == ["refusal", "refusal"]
    assert len(p.server.of("notice")) == 1


def test_a_retry_that_returns_empty_text_is_no_output_not_a_refusal(monkeypatch, tmp_path):
    """HTTP 200 但没有文字、也不是 refusal：还是 no_output，不写 translation_refusal。"""
    from tests.test_claude_comments import subtitle_engine
    from tests.test_claude_translator import Resp, message, text_block
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block(REPLY))), Resp(200, message(text_block("")))]
    p = one_line(tmp_path, wrapped)
    assert len(session.posts) == 2
    (rejected,) = rows(p.audit, "translation_rejected")
    assert rejected["retry"] == "no_output"
    assert rows(p.audit, "translation_refusal") == [] and p.server.of("notice") == []


@pytest.mark.parametrize("trouble", [{"cooldown_until": "future"}, {"fail_streak": 1}])
def test_the_retry_is_skipped_while_the_engine_is_in_trouble(tmp_path, trouble):
    kwargs = {k: (time.monotonic() + 60 if v == "future" else v) for k, v in trouble.items()}
    engine = Scripted([REPLY, GOOD], **kwargs)
    p = one_line(tmp_path, engine)
    assert engine.calls == 1
    (rejected,) = rows(p.audit, "translation_rejected")
    assert (rejected["attempt"], rejected["retry"]) == (1, "skipped")
    assert "retry_ms" not in rejected             # 没发请求，没有耗时
    assert caption(p)["not_translation"] is True


def test_the_retry_is_skipped_when_the_engine_was_replaced_during_the_first_request(
        tmp_path, capsys):
    """中控在第一次请求在途时换了引擎：set_engine 已经把旧的 close 了，BaseTranslator.session() 在会话
    关掉后会悄悄新建一个，重试会在换引擎之后再向 Anthropic 发一条，还留下没人关的会话。"""
    engine = Scripted([REPLY, GOOD])
    other = Scripted(["别的引擎的译文"], )
    holder = {}
    first_request = engine.translate

    async def swapping(text, target, source="auto", glossary=None):
        out = await first_request(text, target, source=source, glossary=glossary)
        holder["p"].translator = other             # set_engine 换引擎的那一刻
        return out

    engine.translate = swapping

    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="claude")
        p._heal_local_engine = noop
        holder["p"] = p
        await p._translate_and_update(job(1, "Just add them."))
        return p

    p = run(scenario())
    assert engine.calls == 1 and other.calls == 0       # 旧引擎没有第二次请求，新引擎也没被借用
    (rejected,) = rows(p.audit, "translation_rejected")
    assert (rejected["attempt"], rejected["retry"]) == (1, "skipped")
    assert "retry_ms" not in rejected
    update = caption(p)
    assert update["translated"] is None and update["not_translation"] is True
    assert "已被换掉" in capsys.readouterr().out


def test_the_retry_is_skipped_when_two_or_more_captions_are_waiting(tmp_path):
    engine = Scripted([REPLY, GOOD])
    p = one_line(tmp_path, engine, queue_depth=2)
    assert engine.calls == 1
    (rejected,) = rows(p.audit, "translation_rejected")
    assert rejected["retry"] == "skipped"
    assert caption(p)["translated"] is None


def test_the_retry_still_runs_with_one_caption_waiting(tmp_path):
    engine = Scripted([REPLY, GOOD])
    p = one_line(tmp_path, engine, queue_depth=1)
    assert engine.calls == 2
    assert caption(p)["translated"] == GOOD
    assert rows(p.audit, "translation_rejected")[0]["retry"] == "ok"


def test_the_retry_count_is_bounded_by_the_constant(monkeypatch, tmp_path):
    assert pipeline_mod.REPLY_RETRIES == 1
    monkeypatch.setattr(pipeline_mod, "REPLY_RETRIES", 0)
    engine = Scripted([REPLY, GOOD])
    p = one_line(tmp_path, engine)
    assert engine.calls == 1
    (rejected,) = rows(p.audit, "translation_rejected")
    assert rejected["attempt"] == 1 and "retry" not in rejected


def test_the_glossary_still_applies_to_a_retried_translation(tmp_path):
    class Glossary:
        def translation_pairs(self, text):
            return ()

        def apply(self, text, out):
            return out.replace("LB", "Luna Beauty")

    engine = Scripted([REPLY, "你好，我是LB。"])

    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="claude")
        p.glossary = Glossary()
        p._heal_local_engine = noop
        await p._translate_and_update(job(1, "Hola, soy LB."))
        return p

    p = run(scenario())
    assert caption(p)["translated"] == "你好，我是Luna Beauty。"


def test_the_reply_check_runs_before_the_glossary_expands_brand_names(tmp_path):
    """词表兜底替换会把品牌名展开得比原文长：要判模型的原样输出，不然长度规则误判正常译文。"""
    class Glossary:
        def translation_pairs(self, text):
            return ()

        def apply(self, text, out):
            return out.replace("LB", "Luna Beauty Natural Organics Collection")

    engine = Fast("hymt2", out="你好，我是LB。")

    async def scenario():
        p = bare_pipeline(tmp_path, engine)
        p.glossary = Glossary()
        p._heal_local_engine = noop
        await p._translate_and_update(job(3, "Hola, soy LB."))
        return p

    p = run(scenario())
    assert p.server.of("caption_update")[-1]["translated"] == \
        "你好，我是Luna Beauty Natural Organics Collection。"
    assert rows(p.audit, "translation_rejected") == []


# ---------------------------------------------------------------------------
# 重译、报警上下文
# ---------------------------------------------------------------------------

# 一句很短的回话：长度、问号、数字都正常，looks_fabricated 管不到，只有 looks_like_reply 认得出
SHORT_REPLY = "请提供要翻译的内容。"


def test_a_short_reply_from_the_retranslation_is_discarded_and_the_old_translation_stays(tmp_path):
    from app.translator import looks_fabricated
    from tests.test_claude_strong import of, only_note, retranslate_pipeline
    assert not looks_fabricated("pague primero y le enviamos", SHORT_REPLY)
    p = retranslate_pipeline(tmp_path, Fast("claude", model="claude-haiku-5-5"),
                             strong=Strong(result=SHORT_REPLY, model="claude-sonnet-5-5"),
                             quality=QUALITY_FAST)
    p._for_translation = lambda text: (text, None)
    run(p.retranslate(7))
    assert only_note(p) == "重译没有成功：结果不像译文，已丢弃，原译文保持不变"
    assert not [m for m in of(p, "caption_update") if m.get("translated")]


def test_a_discarded_retranslation_of_an_untranslated_caption_keeps_the_not_translation_mark(tmp_path):
    """快译已经是「结果不像译文」，重译又回了话：角标不换成「翻译失败」，也不算进连续失败。"""
    from tests.test_claude_strong import of, retranslate_pipeline
    p = retranslate_pipeline(tmp_path, Fast("claude", model="claude-haiku-5-5"),
                             strong=Strong(result=SHORT_REPLY, model="claude-sonnet-5-5"), quality=0)
    p._for_translation = lambda text: (text, None)
    run(p.retranslate(7))
    last = of(p, "caption_update")[-1]
    assert last["translate_state"] == "failed" and last["not_translation"] is True
    assert "refused" not in last


def test_a_short_reply_to_an_alert_context_falls_back_to_the_resident_engine(tmp_path):
    from tests.test_claude_strong import of
    strong = Strong(result=SHORT_REPLY, model="claude-sonnet-5-5")
    fast = Fast("claude", model="claude-haiku-5-5", out="这个能治病")

    async def scenario():
        p = bare_pipeline(tmp_path, fast)
        p._strong = strong
        await p._translate_alert([1], "esto lo cura todo", "es")
        return p

    p = run(scenario())
    assert of(p, "alert_update")[-1]["context_zh"] == "这个能治病"
    (record,) = rows(p.audit, "alert_translation")
    assert record["fallback"] is True


def test_a_reply_from_the_fallback_engine_is_caught_too(tmp_path):
    """Claude 额度用尽、换成本地引擎补这一句：补回来的也要过同一道检查。"""
    class Exhausted:
        name = "deepl"
        model = None
        quota_exhausted = True

        async def translate(self, text, target, source="auto", glossary=None):
            return None

    local = Fast("hymt2", out=SHORT_REPLY)

    async def scenario():
        p = bare_pipeline(tmp_path, Exhausted(), engine="deepl")
        p._heal_local_engine = noop

        async def fallback(tr):
            return local
        p._quota_fallback = fallback
        await p._translate_and_update(job(9, "pague primero y le enviamos"))
        return p

    p = run(scenario())
    update = p.server.of("caption_update")[-1]
    assert update["translated"] is None and update.get("not_translation") is True
    assert [r["reason"] for r in rows(p.audit, "translation_rejected")] == ["reply_meta"]
