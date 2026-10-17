"""字幕这条路（Claude Haiku 5.5）带词表提示：管线算出的本句命中词条，原样进字幕请求的 system。

2026-10-17：俚语版字幕提示词让 Haiku 把主播自造的品名意译成别的说法，译后兜底替换找不到西语原文，
品名上不了屏。修法是让字幕请求也带重译早就带着的那段词表提示，而且只带本句命中的词条。这里钉住：
  * 命中的词条进请求、没命中的不进；整句一条都没命中时请求体和以前一字不差；
  * 回话被拦下后的重试发的是同一个请求（词表也在）；
  * 带词表的句子照样进重复句缓存，同一句不重复花钱；
  * 词表兜底替换照旧在译文回来之后做。
用虚构品牌 acme（brands/README.md 的例子），不依赖任何真实词表。测试不联网。
"""
from app.glossary import Glossary, parse
from tests.helpers import run
from tests.test_claude_comments import subtitle_engine
from tests.test_claude_translator import SYSTEM_SLANG, Resp, message, text_block
from tests.test_resilience_translation import bare_pipeline, job, noop

ACME = parse("la crema acme | crema de acme => ACME 面霜\n"
             "suero acme => ACME 精华液\n")
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
