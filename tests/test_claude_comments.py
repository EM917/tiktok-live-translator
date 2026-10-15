"""字幕引擎是 Claude 时，观众弹幕也用 Claude 翻（Haiku 5.5，2026-10-13 用户决定）。

约定：
  * 弹幕用**另建的实例**，不和字幕引擎共用——弹幕撞上 429 暂停的只能是它自己，字幕不停；
  * 照旧攒批（带编号的几行）、照旧给字幕翻译让路（最多等 IDLE_WAIT_MAX_SEC）；
  * Google、DeepL、OpenAI、本地 7B 照旧只显示原文，每场提示一次，提示的话要和事实一致。

测试一律用假 HTTP 层，不联网。
"""
import asyncio
import json
import time

import pytest

from app import comments as comments_mod
from app import i18n
from app.comments import COMMENT_ENGINES, CommentTranslator, engine_allows_comments
from app.translator import CachedTranslator, ClaudeTranslator
from tests import i18n_rules
from tests.helpers import run
from tests.test_claude_translator import RecordingSession, Resp, attach, message, text_block
from tests.test_resilience_translation import Fast, full_pipeline, wait_for


def subtitle_engine(monkeypatch):
    """字幕引擎：真的 ClaudeTranslator 包在缓存里，HTTP 层换成会记账的空会话。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.delenv("CLAUDE_TRANSLATE_MODEL", raising=False)
    inner = ClaudeTranslator()
    session = RecordingSession([])
    attach(inner, session)
    return CachedTranslator(inner), inner, session


class Sibling:
    """claude_factory 造出来的弹幕实例：真的 ClaudeTranslator，会话由测试脚本化。"""

    def __init__(self, responses):
        self.made = []
        self.responses = responses

    def __call__(self):
        tr = ClaudeTranslator(model=ClaudeTranslator.COMMENT_MODEL, role="comment")
        tr.session_log = RecordingSession(list(self.responses))
        attach(tr, tr.session_log)
        self.made.append(tr)
        return tr


def make(holder, factory, busy=None, **overrides):
    sent = []

    async def broadcast(msg):
        sent.append(msg)

    ct = CommentTranslator(broadcast=broadcast, translator=lambda: holder["tr"],
                           target=lambda: "zh-CN", glossary=lambda: None, busy=busy,
                           session=lambda: holder.get("session"), claude_factory=factory)
    ct.BATCH_WINDOW_SEC = 0.01
    for k, v in overrides.items():
        setattr(ct, k, v)
    return ct, sent


def updates(sent):
    return {m["id"]: (m["state"], m["translated"]) for m in sent if m["type"] == "comment_update"}


def items(*texts):
    return {"items": [{"id": "c%d" % i, "user": "u", "text": t} for i, t in enumerate(texts)]}


def reply(text):
    return Resp(200, message(text_block(text), model="claude-haiku-5-5"))


# ---------------------------------------------------------------------------
# 哪些引擎翻弹幕
# ---------------------------------------------------------------------------

def test_claude_joins_the_local_models_and_nothing_else_does():
    assert COMMENT_ENGINES == ("hymt2", "gemma", "claude")
    for name in ("hymt2", "gemma", "claude"):
        assert engine_allows_comments(Fast(name))
    for name in ("hymt2-7b", "google", "deepl", "openai", "none"):
        assert not engine_allows_comments(Fast(name))


def test_the_original_only_hint_matches_the_facts_in_both_languages():
    hint = comments_mod.original_only_hint(Fast("google"))
    assert hint == comments_mod.REMOTE_ENGINE_HINT
    assert hint == ("当前翻译引擎不翻译弹幕（只有本地 Hy-MT2 1.8B、本地 TranslateGemma 和 Claude "
                    "会），弹幕只显示原文")
    english = i18n.render(hint, i18n.EN)
    assert english == ("The current translation engine doesn’t translate comments. Only Local "
                       "Hy-MT2 1.8B, Local TranslateGemma and Claude do, so comments show the "
                       "original text.")
    assert not i18n_rules.EN_LABELS.search(english) and not i18n_rules.EN_CAUSAL.search(english)
    # 7B 是本地模型，照旧单独一句
    assert comments_mod.original_only_hint(Fast("hymt2-7b")) == comments_mod.LARGE_MODEL_HINT
    # 以前那句「不是本地模型」对 Claude 已经不是事实
    assert "不是本地模型" not in hint


@pytest.mark.parametrize("name", ["google", "deepl", "openai"])
def test_other_remote_engines_still_show_the_original_and_say_so_once(name):
    engine = Fast(name)
    holder = {"tr": engine, "session": "一场"}
    made = Sibling([])
    ct, sent = make(holder, made)

    async def scenario():
        await ct.accept(items("hola amigos", "que precio tiene"))
        await asyncio.sleep(0.05)

    run(scenario())
    assert engine.calls == 0 and made.made == []
    assert {m["id"]: m["state"] for m in sent if m["type"] == "comment"} == {
        "c0": "skipped", "c1": "skipped"}
    assert [m["text"] for m in sent if m["type"] == "notice"] == [
        comments_mod.REMOTE_ENGINE_HINT]


# ---------------------------------------------------------------------------
# Claude：另建的实例、Haiku 5.5、攒批
# ---------------------------------------------------------------------------

def test_comments_are_translated_by_a_separate_haiku_instance(monkeypatch):
    wrapped, inner, sub_session = subtitle_engine(monkeypatch)
    made = Sibling([reply("1. 你好朋友\n2. 这个多少钱")])
    holder = {"tr": wrapped}
    ct, sent = make(holder, made)

    async def scenario():
        await ct.accept(items("hola amigos", "que precio tiene"))
        assert await wait_for(lambda: len(updates(sent)) == 2)

    run(scenario())
    assert updates(sent) == {"c0": ("ok", "你好朋友"), "c1": ("ok", "这个多少钱")}
    (sibling,) = made.made
    assert sibling is not inner and sibling.model == "claude-haiku-5-5"
    assert sub_session.posts == []                       # 字幕引擎一次都没用
    (post,) = sibling.session_log.posts
    body = post["body"]
    assert body["model"] == "claude-haiku-5-5"
    assert body["thinking"] == {"type": "disabled"} and body["output_config"] == {"effort": "low"}
    assert body["messages"] == [{"role": "user", "content": "<comments>\n1. hola amigos\n2. que precio tiene\n</comments>"}]
    assert "untrusted" in body["system"]
    assert not [m for m in sent if m["type"] == "notice"]    # Claude 翻弹幕：没有「只显示原文」的提示


def test_a_single_comment_goes_out_as_plain_text(monkeypatch):
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    made = Sibling([reply("我爱你")])
    ct, sent = make({"tr": wrapped}, made)

    async def scenario():
        await ct.accept(items("te amo"))
        assert await wait_for(lambda: updates(sent))

    run(scenario())
    assert updates(sent) == {"c0": ("ok", "我爱你")}
    assert made.made[0].session_log.posts[0]["body"]["messages"][0]["content"] == "<comments>\nte amo\n</comments>"


def test_the_instance_is_built_once_and_rebuilt_when_the_subtitle_engine_is_replaced(monkeypatch):
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    made = Sibling([reply("你好") for _ in range(10)])
    holder = {"tr": wrapped}
    ct, sent = make(holder, made)

    async def scenario():
        for i in range(2):
            await ct.accept({"items": [{"id": "x%d" % i, "user": "u", "text": "hola %d" % i}]})
            assert await wait_for(lambda n=i + 1: len(updates(sent)) == n)
        assert len(made.made) == 1                        # 同一个字幕引擎：接着用
        old = made.made[0]
        wrapped2, _i2, _s2 = subtitle_engine(monkeypatch)        # 重填密钥：新的字幕引擎对象
        holder["tr"] = wrapped2
        await ct.accept({"items": [{"id": "y", "user": "u", "text": "hola otra vez"}]})
        assert await wait_for(lambda: len(updates(sent)) == 3)
        assert len(made.made) == 2 and made.made[1] is not old
        await asyncio.sleep(0.02)
        await ct.close()

    run(scenario())


def test_without_a_key_the_comment_instance_cannot_be_built_and_the_batch_fails(monkeypatch, capsys):
    wrapped, _inner, _s = subtitle_engine(monkeypatch)

    def no_key():
        raise RuntimeError("还没有填 Claude 密钥")

    ct, sent = make({"tr": wrapped}, no_key)

    async def scenario():
        await ct.accept(items("hola amigos"))
        assert await wait_for(lambda: updates(sent))

    run(scenario())
    assert updates(sent) == {"c0": ("failed", None)}


# ---------------------------------------------------------------------------
# 弹幕被限速，字幕照常翻
# ---------------------------------------------------------------------------

def test_a_429_on_the_comment_instance_never_pauses_the_subtitle_engine(monkeypatch):
    wrapped, inner, sub_session = subtitle_engine(monkeypatch)
    sub_session.responses.append(Resp(200, message(text_block("字幕译文"))))
    made = Sibling([Resp(429, {"error": {"type": "rate_limit_error"}},
                         headers={"retry-after": "60"})])
    ct, sent = make({"tr": wrapped}, made)

    async def scenario():
        await ct.accept(items("hola amigos", "que precio tiene"))
        assert await wait_for(lambda: len(updates(sent)) == 2)
        sibling = made.made[0]
        assert sibling.cooldown_until > time.monotonic() + 30
        assert inner.cooldown_until <= time.monotonic()          # 字幕引擎没被牵连
        assert await wrapped.translate("hola", "zh-CN") == "字幕译文"

    run(scenario())
    assert updates(sent) == {"c0": ("failed", None), "c1": ("failed", None)}
    # 暂停期间不再发请求：批量那一次之后，逐条重试都在冷却里直接返回
    assert len(made.made[0].session_log.posts) == 1


def test_an_http_error_on_a_batch_is_not_retried_comment_by_comment(monkeypatch):
    """一批 5 条弹幕出错后逐条重发，请求数就是 6 倍。接口回了错（HTTP 非 200）时逐条重发也一样会错。"""
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    made = Sibling([Resp(500, {"error": {"type": "api_error"}})] * 6)
    ct, sent = make({"tr": wrapped}, made)

    async def scenario():
        await ct.accept(items("uno dos", "tres cuatro", "cinco seis", "siete ocho"))
        assert await wait_for(lambda: len(updates(sent)) == 4)

    run(scenario())
    assert set(updates(sent).values()) == {("failed", None)}
    assert len(made.made[0].session_log.posts) == 1


def test_a_refused_batch_is_retried_one_by_one_to_isolate_the_refused_comment(monkeypatch):
    """拒答是 HTTP 200：逐条重译正好把拒答的那一条隔离出来，其余照常翻。"""
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    refusal = dict(message(stop="refusal"), stop_details={"type": "refusal", "category": None})
    made = Sibling([Resp(200, refusal), reply("你好"), Resp(200, refusal)])
    ct, sent = make({"tr": wrapped}, made)

    async def scenario():
        await ct.accept(items("hola", "malo malo"))
        assert await wait_for(lambda: len(updates(sent)) == 2)

    run(scenario())
    assert updates(sent) == {"c0": ("ok", "你好"), "c1": ("failed", None)}
    assert len(made.made[0].session_log.posts) == 3


# ---------------------------------------------------------------------------
# 照旧让路给字幕翻译
# ---------------------------------------------------------------------------

def test_comments_still_wait_for_subtitle_translation(monkeypatch):
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    made = Sibling([reply("你好")])
    state = {"busy": True}
    ct, sent = make({"tr": wrapped}, made, busy=lambda: state["busy"])

    async def scenario():
        await ct.accept(items("hola"))
        await asyncio.sleep(0.4)
        assert made.made == [] or made.made[0].session_log.posts == []     # 字幕占线：弹幕不动
        state["busy"] = False
        assert await wait_for(lambda: updates(sent))

    run(scenario())
    assert updates(sent) == {"c0": ("ok", "你好")}


def test_the_wait_is_bounded_so_busy_subtitles_cannot_starve_comments(monkeypatch):
    wrapped, _inner, _s = subtitle_engine(monkeypatch)
    made = Sibling([reply("你好")])
    ct, sent = make({"tr": wrapped}, made, busy=lambda: True)
    ct.IDLE_WAIT_MAX_SEC = 0.2

    async def scenario():
        await ct.accept(items("hola"))
        assert await wait_for(lambda: updates(sent), limit=300)

    run(scenario())
    assert updates(sent) == {"c0": ("ok", "你好")}


# ---------------------------------------------------------------------------
# 接进 Pipeline
# ---------------------------------------------------------------------------

def test_the_pipeline_translates_comments_with_claude_through_its_own_instance(
        monkeypatch, tmp_path):
    wrapped, inner, sub_session = subtitle_engine(monkeypatch)
    p = full_pipeline(monkeypatch, tmp_path, wrapped)
    made = Sibling([reply("你好")])
    p.comments._claude_factory = made
    p.comments.BATCH_WINDOW_SEC = 0.01

    async def scenario():
        await p.comments.accept({"items": [{"id": "a", "user": "u", "text": "hola"}]})
        assert await wait_for(lambda: p.server.of("comment_update"))

    run(scenario())
    assert [(m["state"], m["translated"]) for m in p.server.of("comment_update")] == [
        ("ok", "你好")]
    assert sub_session.posts == [] and len(made.made) == 1
    assert json.loads(json.dumps(made.made[0].session_log.posts[0]["body"]))["model"] == (
        "claude-haiku-5-5")


# ---------------------------------------------------------------------------
# 回话不是译文（2026-10-13 直播实录）
# ---------------------------------------------------------------------------
# 单独一条两个词的弹幕「Sii todo」，Haiku 5.5 当成了对它说的话，用英文回了一句「没有要翻译的
# 弹幕，请贴给我」，这句话被当成译文显示在弹幕栏里

META_REPLY = ('I see only "Sii todo" with no comment list or content to translate. Could you paste '
              "the viewer comments you'd like translated into Simplified Chinese?")


def test_the_comment_prompt_says_the_input_is_always_comments_and_never_to_reply(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    tr = ClaudeTranslator(model=ClaudeTranslator.COMMENT_MODEL, role="comment")
    body = tr._body("Sii todo", "zh-CN")
    assert body["messages"] == [{"role": "user", "content": "<comments>\nSii todo\n</comments>"}]
    system = body["system"]
    assert "<comments>" in system and "never reply" in system and "never ask for more input" in system
    assert "even a very short one" in system
    # 字幕和重译包的是另一种标签（2026-10-14 起），提示词也是另一份
    sub = ClaudeTranslator()
    assert sub._body("Sii todo", "zh-CN")["messages"][0]["content"] == "<subtitle>\nSii todo\n</subtitle>"
    assert "<comments>" not in sub._body("Sii todo", "zh-CN")["system"]


@pytest.mark.parametrize("text,out,target,ok", [
    ("Sii todo", META_REPLY, "zh-CN", False),            # 实录：英文回话
    ("Sii todo", "是的，全部", "zh-CN", True),
    ("hola", "你好", "zh-CN", True),
    ("hola", "hello", "en", True),                        # 目标语言不是中文时不查汉字
    ("ok", "好的，我明白了，请问您想翻译哪些弹幕？我可以帮您把它们翻译成中文，" * 2, "zh-CN", False),
    ("te amo", "Could you paste the comments?", "zh-CN", False),   # 没有汉字、还多出好几个词
    ("Luna Demo Shop", "Luna Demo Shop", "zh-CN", True),     # 品牌名原样保留是正常译文
    ("你好主播", "你好主播", "zh-CN", True),                # 原文就是中文：不按「没有汉字」判
    ("precio 350", "价格", "zh-CN", False),               # 数字丢了（looks_fabricated）
    # 同一天第二次实录：中文回话，拿不准、要上下文
    ("pero no se", "我不太确定\"pero no se\"的确切含义,它是西班牙语的口语表达,通常意思是\"但我不知道\"或"
     "\"不过我不清楚\"。如果您能提供更多上下文,或者这是直播评论中的某一条,我可以再帮您更准确地翻译。",
     "zh-CN", False),
    ("pero no se", "请提供更多上下文。", "zh-CN", False),     # 短的回话也拦
    ("pero no se", "但我不知道", "zh-CN", True),
    ("me puedes traducir esto?", "你能帮我翻译这个吗？", "zh-CN", True),   # 原文就在说翻译
])
def test_plausible_translation(text, out, target, ok):
    assert comments_mod.plausible_translation(text, out, target) is ok


def test_a_reply_instead_of_a_translation_shows_the_original_only(monkeypatch):
    tr, inner, _ = subtitle_engine(monkeypatch)
    made = Sibling([reply(META_REPLY)])
    holder = {"tr": tr}
    ct, sent = make(holder, made)

    async def scenario():
        await ct.accept(items("Sii todo"))
        await wait_for(lambda: updates(sent).get("c0", (None,))[0] in ("ok", "failed"))
        await ct.close()

    run(scenario())
    assert updates(sent)["c0"] == ("failed", None)


def test_in_a_batch_only_the_reply_like_line_is_dropped(monkeypatch):
    tr, inner, _ = subtitle_engine(monkeypatch)
    made = Sibling([reply("1. 你好朋友们\n2. " + META_REPLY)])
    holder = {"tr": tr}
    ct, sent = make(holder, made)

    async def scenario():
        await ct.accept(items("hola amigos", "Sii todo"))
        await wait_for(lambda: len(updates(sent)) == 2)
        await ct.close()

    run(scenario())
    got = updates(sent)
    assert got["c0"] == ("ok", "你好朋友们")
    assert got["c1"] == ("failed", None)
