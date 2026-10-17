"""字幕这条路（Claude Haiku 5.5）带词表提示：管线算出的本句命中词条，原样进字幕请求的 system。

2026-10-17：俚语版字幕提示词让 Haiku 把主播自造的品名意译成别的说法，译后兜底替换找不到西语原文，
品名上不了屏。修法是让字幕请求也带重译早就带着的那段词表提示，而且只带本句命中的词条。这里钉住：
  * 命中的词条进请求、没命中的不进；整句一条都没命中时请求体和以前一字不差；
  * 回话被拦下后的重试发的是同一个请求（词表也在）；
  * 带词表的句子照样进重复句缓存，同一句不重复花钱；
  * 词表兜底替换照旧在译文回来之后做；
  * 只是更长单词一部分的词条、译法带括注的词条不进字幕请求（审查后加，见 translator._subtitle_pairs）；
  * 报警上下文翻译借用字幕实例时（没有强模型、强模型没给出译文），请求同样带词表。
用虚构品牌 acme（brands/README.md 的例子），不依赖任何真实词表。测试不联网。
"""
import pytest

from app import translator
from app.glossary import Glossary, in_text_as_word, parse
from tests.helpers import run
from tests.test_claude_comments import subtitle_engine
from tests.test_claude_strong import Flaky
from tests.test_claude_translator import SYSTEM_SLANG, Resp, message, text_block
from tests.test_resilience_translation import bare_pipeline, job, noop

ACME = parse("la crema acme | crema de acme => ACME 面霜\n"
             "suero acme => ACME 精华液\n"
             "acme => ACME 牌\n"
             "el gel acme => ACME 凝胶（小瓶）\n"
             "la loción zeta => ZETA 乳液\n")
BLOCK = "\nWhere the text contains one of these terms, translate it exactly as given here:\n"
REPLY = "I can't tell what \"them\" refers to, since your message doesn't include any text to translate."


def caption(p, seq=1):
    return [m for m in p.server.of("caption_update") if m["id"] == seq][-1]


def lines(tmp_path, wrapped, texts):
    async def scenario():
        p = bare_pipeline(tmp_path, wrapped, engine="claude")
        p.glossary = Glossary(ACME)
        p._heal_local_engine = noop
        for seq, text in enumerate(texts, 1):
            await p._translate_and_update(job(seq, text))
        return p

    return run(scenario())


def test_only_the_entry_the_line_names_goes_into_the_subtitle_request(monkeypatch, tmp_path):
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("今天买 ACME 面霜")))]
    p = lines(tmp_path, wrapped, ["compren tu crema acme hoy"])
    (post,) = session.posts
    assert post["body"]["system"] == SYSTEM_SLANG + BLOCK + "tu crema acme = ACME 面霜"
    assert "suero" not in post["body"]["system"]
    assert post["body"]["messages"] == [
        {"role": "user", "content": "<subtitle>\ncompren tu crema acme hoy\n</subtitle>"}]
    assert caption(p)["translated"] == "今天买 ACME 面霜"


def test_a_line_that_names_no_entry_sends_the_request_it_sent_before(monkeypatch, tmp_path):
    wrapped, inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("大家好")))]
    lines(tmp_path, wrapped, ["hola a todos"])
    (post,) = session.posts
    assert post["body"] == inner._body("hola a todos", "zh-CN")
    assert post["body"]["system"] == SYSTEM_SLANG


def test_the_reply_retry_sends_the_same_request_with_the_glossary(monkeypatch, tmp_path):
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block(REPLY))),
                         Resp(200, message(text_block("加上 ACME 精华液")))]
    p = lines(tmp_path, wrapped, ["agrega el suero acme"])
    first, second = session.posts
    assert first["body"] == second["body"]
    assert first["body"]["system"].endswith(BLOCK + "suero acme = ACME 精华液")
    assert caption(p)["translated"] == "加上 ACME 精华液"


def test_a_repeated_line_with_a_glossary_hit_is_served_from_the_cache(monkeypatch, tmp_path):
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("ACME 精华液")))]
    p = lines(tmp_path, wrapped, ["el suero acme", "el suero acme"])
    assert len(session.posts) == 1
    assert caption(p, 2)["translated"] == "ACME 精华液"


def test_the_post_translation_replacement_still_runs_after_the_request(monkeypatch, tmp_path):
    """模型没照词表写、把西语品名留在译文里时，兜底替换照旧把它换成词表译法。"""
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("试试suero acme吧")))]
    p = lines(tmp_path, wrapped, ["prueba el suero acme"])
    assert caption(p)["translated"] == "试试ACME 精华液吧"


@pytest.mark.parametrize("variant,text,expected", [
    ("acme", "un acmeador", False),              # 更长单词的一部分
    ("acme", "superacme", False),
    ("acme", "las acmes", True),                 # 复数 -s
    ("crema acme", "dos crema acmees", True),    # 复数 -es
    ("acme", "acmesa", False),                   # -s 后面还有字母：不是复数
    ("el suero acme", "del suero acme", True),   # de + el
    ("el suero acme", "al suero acme", False),   # al 里没有 el：matching() 本来就不命中
    ("el suero acme", "model suero acme", False),
    ("la crema acme", "dela crema acme", False),  # 只有 el 开头的写法放行缩合
    ("acme", "试试ACME吧", True),                 # 紧贴汉字、大小写不同
])
def test_whole_word_check_for_subtitle_hints(variant, text, expected):
    assert in_text_as_word(variant, text) is expected


def test_a_term_inside_a_longer_word_does_not_reach_the_subtitle_request(monkeypatch, tmp_path):
    """管线的命中是子串匹配：「un acmeador」也算命中 acme。这一条只是更长单词的一部分，
    不进字幕请求；同句里整词出现的词条照常带。"""
    wrapped, inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("一个新的调和器"))),
                         Resp(200, message(text_block("一个调和器和 ZETA 乳液")))]
    p = lines(tmp_path, wrapped, ["un acmeador nuevo", "un acmeador y la loción zeta"])
    assert [es for es, _ in p.glossary.translation_pairs("un acmeador nuevo")] == ["acme"]
    first, second = session.posts
    assert first["body"] == inner._body("un acmeador nuevo", "zh-CN")
    assert second["body"]["system"] == SYSTEM_SLANG + BLOCK + "la loción zeta = ZETA 乳液"


def test_a_bracketed_rendering_does_not_reach_the_subtitle_request(monkeypatch, tmp_path):
    wrapped, inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("这个凝胶")))]
    p = lines(tmp_path, wrapped, ["mira el gel acme"])
    assert [es for es, _ in p.glossary.translation_pairs("mira el gel acme")] == ["el gel acme"]
    (post,) = session.posts
    assert post["body"] == inner._body("mira el gel acme", "zh-CN")
    assert "（" not in post["body"]["system"]


def alert(monkeypatch, tmp_path, strong=None, context="compren la crema acme hoy"):
    """报警上下文翻译，字幕引擎是真的 Claude 实例（HTTP 是假的）；strong 为 None 时没有强模型可用。"""
    wrapped, inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("今天买 ACME 面霜")))]
    if strong is None:
        monkeypatch.setattr(translator, "create_claude_strong_translator", lambda: None)

    async def scenario():
        p = bare_pipeline(tmp_path, wrapped, engine="claude")
        p.glossary = Glossary(ACME)
        if strong is not None:
            p._strong = strong
        await p._translate_alert([1], context, "es")
        return p

    p = run(scenario())
    return p, inner, session


def test_an_alert_with_no_strong_model_asks_the_subtitle_instance_with_the_glossary(monkeypatch, tmp_path):
    """没有强模型时报警上下文直接交给字幕实例（first = strong or fast）。管线一直把词表传给它，
    以前字幕实例收下不用；现在字幕请求带词表，这条路也跟着带（审查后补的测试，PR 里写在改动下）。"""
    p, _inner, session = alert(monkeypatch, tmp_path)
    (post,) = session.posts
    assert post["body"]["system"] == SYSTEM_SLANG + BLOCK + "la crema acme = ACME 面霜"
    assert p.server.of("alert_update")[-1]["context_zh"] == "今天买 ACME 面霜"


def test_an_alert_the_strong_model_left_empty_is_retried_on_the_subtitle_instance_with_the_glossary(
        monkeypatch, tmp_path):
    strong = Flaky(error=(529, None))
    _p, _inner, session = alert(monkeypatch, tmp_path, strong=strong)
    assert strong.calls == 1
    (post,) = session.posts
    assert post["body"]["system"] == SYSTEM_SLANG + BLOCK + "la crema acme = ACME 面霜"


def test_an_alert_whose_strong_output_looks_like_a_reply_is_retried_without_the_glossary(
        monkeypatch, tmp_path):
    """强模型的译文像回话时，管线用整段原文、不带词表再问字幕实例一次（这次之前就是这样，没改）。"""
    strong = Flaky(result=REPLY)
    _p, inner, session = alert(monkeypatch, tmp_path, strong=strong)
    (post,) = session.posts
    assert post["body"] == inner._body("compren la crema acme hoy", "zh-CN")
