"""字幕译文在「回话」而不是在翻译：这一句显示原文（2026-10-14）。

2026-10-13/14 五场 Claude 字幕 2527 句里 82 句（3.2%）不是干净的译文：英语短句被当成对它的请求
（「I can't tell what "them" refers to…」「I can't draw images here」）、一段拒绝加就医建议、
译完加「Note:」「Wait, let me correct that」。这里钉住：
  * 检测规则（translator.looks_like_reply）认得出这几类，正常译文不误判；
  * 快译、降级引擎、重译、报警上下文四条路都把回话拦下，不上屏、不进审计的译文栏；
  * 拦下来的是「接口通着、回的不是译文」：不报引擎故障、不拉本地引擎，页面不算进连续失败；
  * 回话的原话记进 translation_rejected，并从重复句缓存里拿掉；
  * Claude 字幕的请求把这一句包在 <subtitle> 里，提示词写明不是对它说的话。

例句改写自本机日志，人名换成虚构的。测试不联网。
"""
import pytest

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
    assert "Simplified Chinese" in system


# ---------------------------------------------------------------------------
# 快译这条路
# ---------------------------------------------------------------------------

REPLY = "I can't tell what \"them\" refers to, since your message doesn't include any text to translate."


def test_a_reply_on_the_caption_path_shows_the_original_and_is_recorded(tmp_path, capsys):
    engine = Fast("claude", model="claude-haiku-5-5", out=REPLY)

    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="claude")
        p._heal_local_engine = noop
        await p._translate_and_update(job(7, "Just add them."))
        return p

    p = run(scenario())
    update = p.server.of("caption_update")[-1]
    assert update["translated"] is None and update["translate_state"] == "failed"
    assert update.get("not_translation") is True and "refused" not in update
    (rejected,) = rows(p.audit, "translation_rejected")
    assert (rejected["seq"], rejected["engine"], rejected["reason"]) == (7, "claude", "reply_meta")
    assert rejected["rejected"] == REPLY
    (tr_row,) = rows(p.audit, "translation")
    assert tr_row["ok"] is False and tr_row["translated"] is None
    # 接口是通的：不记引擎故障、不拉本地引擎
    assert rows(p.audit, "translation_engine_error") == []
    assert not p._bg_tasks
    assert "回来的不是第 7 条字幕的译文" in capsys.readouterr().out


def test_a_caught_reply_is_dropped_from_the_repeat_cache(tmp_path):
    class Flaky:
        name = "claude"
        model = "claude-haiku-5-5"

        def __init__(self):
            self.answers = [REPLY, "加进去就行。"]
            self.calls = 0

        async def translate(self, text, target, source="auto", glossary=None):
            self.calls += 1
            return self.answers.pop(0)

    inner = Flaky()

    async def scenario():
        p = bare_pipeline(tmp_path, CachedTranslator(inner), engine="claude")
        p._heal_local_engine = noop
        await p._translate_and_update(job(1, "Just add them."))
        await p._translate_and_update(job(2, "Just add them."))
        return p

    p = run(scenario())
    assert inner.calls == 2                          # 第二次重新问了，没从缓存拿回那句回话
    assert p.server.of("caption_update")[-1]["translated"] == "加进去就行。"


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
