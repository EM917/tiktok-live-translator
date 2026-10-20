"""字幕这条路（Claude Haiku 5.5）带词表提示：管线算出的本句命中词条，原样进字幕请求的 system。

2026-10-17：俚语版字幕提示词让 Haiku 把主播自造的品名意译成别的说法，译后兜底替换找不到西语原文，
品名上不了屏。修法是让字幕请求也带重译早就带着的那段词表提示，而且只带本句命中的词条。这里钉住：
  * 命中的词条进请求、没命中的不进；整句一条都没命中时请求体和以前一字不差；
  * 回话被拦下后的重试发的是同一个请求（词表也在）；
  * 带词表的句子照样进重复句缓存，同一句不重复花钱；
  * 词表兜底替换照旧在译文回来之后做；
  * 只是更长单词一部分的词条、译法带括注的词条不进字幕请求（审查后加，见 translator._subtitle_pairs）；
  * 报警上下文翻译借用字幕实例时（没有强模型、强模型没给出译文），请求同样带词表；
  * 只带品牌表（brands/）和主播 profile（profiles/）的词条，全局表 glossary.txt 的不带（用户要的是「字幕也
    带上品牌词表提示」；全局表是首启从公开模板复制的，里面的日常短语按语境本不该照着译）。重译照旧三层都带。
    只加载其中一层（只选了品牌、主播没有 profile；或者只有 profile）时也一样；
  * 重译和报警上下文翻译（Sonnet 5.5）也不带译法带括注的词条，兜底替换也不拿它去换（2026-10-20 用户定
    「重新翻译也不带带括号的词条」）。
用虚构品牌 acme（brands/README.md 的例子）、虚构主播 luna，不依赖任何真实词表。测试不联网。
"""
import shutil
from pathlib import Path

import pytest

from app import glossary as G
from app import translator
from app.glossary import in_text_as_word, parse
from tests.helpers import run
from tests.test_claude_comments import subtitle_engine
from tests.test_claude_strong import Flaky
from tests.test_claude_translator import SYSTEM_SLANG, Resp, message, text_block
from tests.test_resilience_translation import bare_pipeline, job, noop

BRAND = ("la crema acme | crema de acme => ACME 面霜\n"     # brands/acme.txt
         "suero acme => ACME 精华液\n"
         "acme => ACME 牌\n"
         "el gel acme => ACME 凝胶（小瓶）\n")
PROFILE = "la loción zeta => ZETA 乳液\n"                   # profiles/luna.txt
GLOBAL = "el combo => 套装\n"                               # glossary.txt：代表模板里那类日常短语
BLOCK = "\nWhere the text contains one of these terms, translate it exactly as given here:\n"
REPLY = "I can't tell what \"them\" refers to, since your message doesn't include any text to translate."


def caption(p, seq=1):
    return [m for m in p.server.of("caption_update") if m["id"] == seq][-1]


def layered(monkeypatch, tmp_path, global_text=GLOBAL, profile_text=PROFILE, streamer="luna", brand="acme"):
    """三层词表各放一份，照生产 load(streamer, brand) 读进来（目录都指到 tmp_path 下）。
    streamer / brand 换成 None 或没有文件的名字，就是只有其中一层的那几种场次。"""
    root = tmp_path / "gl"
    (root / "brands").mkdir(parents=True)
    (root / "profiles").mkdir()
    (root / "glossary.txt").write_text(global_text, encoding="utf-8")
    (root / "brands" / "acme.txt").write_text(BRAND, encoding="utf-8")
    (root / "profiles" / "luna.txt").write_text(profile_text, encoding="utf-8")
    monkeypatch.setattr(G, "GLOSSARY_FILE", root / "glossary.txt")
    monkeypatch.setattr(G, "GLOSSARY_EXAMPLE", root / "none.example.txt")
    monkeypatch.setattr(G, "BRAND_DIR", root / "brands")
    monkeypatch.setattr(G, "PROFILE_DIR", root / "profiles")
    return G.load(streamer=streamer, brand=brand)


def lines(monkeypatch, tmp_path, wrapped, texts, gl=None):
    if gl is None:
        gl = layered(monkeypatch, tmp_path)

    async def scenario():
        p = bare_pipeline(tmp_path, wrapped, engine="claude")
        p.glossary = gl
        p._heal_local_engine = noop
        for seq, text in enumerate(texts, 1):
            await p._translate_and_update(job(seq, text))
        return p

    return run(scenario())


def test_only_the_entry_the_line_names_goes_into_the_subtitle_request(monkeypatch, tmp_path):
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("今天买 ACME 面霜")))]
    p = lines(monkeypatch, tmp_path, wrapped, ["compren tu crema acme hoy"])
    (post,) = session.posts
    assert post["body"]["system"] == SYSTEM_SLANG + BLOCK + "tu crema acme = ACME 面霜"
    assert "suero" not in post["body"]["system"]
    assert post["body"]["messages"] == [
        {"role": "user", "content": "<subtitle>\ncompren tu crema acme hoy\n</subtitle>"}]
    assert caption(p)["translated"] == "今天买 ACME 面霜"


def test_a_line_that_names_no_entry_sends_the_request_it_sent_before(monkeypatch, tmp_path):
    wrapped, inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("大家好")))]
    lines(monkeypatch, tmp_path, wrapped, ["hola a todos"])
    (post,) = session.posts
    assert post["body"] == inner._body("hola a todos", "zh-CN")
    assert post["body"]["system"] == SYSTEM_SLANG


def test_the_reply_retry_sends_the_same_request_with_the_glossary(monkeypatch, tmp_path):
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block(REPLY))),
                         Resp(200, message(text_block("加上 ACME 精华液")))]
    p = lines(monkeypatch, tmp_path, wrapped, ["agrega el suero acme"])
    first, second = session.posts
    assert first["body"] == second["body"]
    assert first["body"]["system"].endswith(BLOCK + "suero acme = ACME 精华液")
    assert caption(p)["translated"] == "加上 ACME 精华液"


def test_a_repeated_line_with_a_glossary_hit_is_served_from_the_cache(monkeypatch, tmp_path):
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("ACME 精华液")))]
    p = lines(monkeypatch, tmp_path, wrapped, ["el suero acme", "el suero acme"])
    assert len(session.posts) == 1
    assert caption(p, 2)["translated"] == "ACME 精华液"


def test_the_post_translation_replacement_still_runs_after_the_request(monkeypatch, tmp_path):
    """模型没照词表写、把西语品名留在译文里时，兜底替换照旧把它换成词表译法。"""
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("试试suero acme吧")))]
    p = lines(monkeypatch, tmp_path, wrapped, ["prueba el suero acme"])
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
    p = lines(monkeypatch, tmp_path, wrapped, ["un acmeador nuevo", "un acmeador y la loción zeta"])
    assert [es for es, _ in p.glossary.translation_pairs("un acmeador nuevo")] == ["acme"]
    first, second = session.posts
    assert first["body"] == inner._body("un acmeador nuevo", "zh-CN")
    assert second["body"]["system"] == SYSTEM_SLANG + BLOCK + "la loción zeta = ZETA 乳液"


def test_a_bracketed_rendering_does_not_reach_the_subtitle_request(monkeypatch, tmp_path):
    wrapped, inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("这个凝胶")))]
    p = lines(monkeypatch, tmp_path, wrapped, ["mira el gel acme"])
    assert [es for es, _ in p.glossary.translation_pairs("mira el gel acme")] == ["el gel acme"]
    (post,) = session.posts
    assert post["body"] == inner._body("mira el gel acme", "zh-CN")
    assert "（" not in post["body"]["system"]


# 报警上下文里同时有品牌表和全局表的词条：借用字幕实例时只带品牌表那条，和字幕一样
CONTEXT = "compren la crema acme y el combo hoy"


def alert(monkeypatch, tmp_path, strong=None, context=CONTEXT):
    """报警上下文翻译，字幕引擎是真的 Claude 实例（HTTP 是假的）；strong 为 None 时没有强模型可用。"""
    wrapped, inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("今天买 ACME 面霜")))]
    if strong is None:
        monkeypatch.setattr(translator, "create_claude_strong_translator", lambda: None)
    gl = layered(monkeypatch, tmp_path)

    async def scenario():
        p = bare_pipeline(tmp_path, wrapped, engine="claude")
        p.glossary = gl
        if strong is not None:
            p._strong = strong
        await p._translate_alert([1], context, "es")
        return p

    p = run(scenario())
    return p, inner, session


def test_an_alert_with_no_strong_model_asks_the_subtitle_instance_with_the_glossary(monkeypatch, tmp_path):
    """没有强模型时报警上下文直接交给字幕实例（first = strong or fast）。管线一直把词表传给它，
    以前字幕实例收下不用；现在字幕请求带词表，这条路也跟着带，筛法和字幕一样：只带品牌表和 profile 的。"""
    p, _inner, session = alert(monkeypatch, tmp_path)
    assert [pr.layer for pr in p.glossary.translation_pairs(CONTEXT)] == ["brand", "global"]
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
    assert post["body"] == inner._body(CONTEXT, "zh-CN")


# ---------------------------------------------------------------------------
# 按层筛：字幕请求只带品牌表和主播 profile 的词条，全局表 glossary.txt 的不带；重译照旧三层都带
# ---------------------------------------------------------------------------

def test_each_hint_knows_the_layer_it_came_from(monkeypatch, tmp_path):
    gl = layered(monkeypatch, tmp_path, global_text=GLOBAL + "suero acme => 精华液\n")
    pairs = gl.translation_pairs("la crema acme, el combo, el suero acme y la loción zeta")
    assert [(tuple(pr), pr.layer) for pr in pairs] == [
        (("la loción zeta", "ZETA 乳液"), "profile"),
        (("la crema acme", "ACME 面霜"), "brand"),
        (("suero acme", "ACME 精华液"), "brand"),      # 全局表也有这个写法：合并后归品牌表
        (("el combo", "套装"), "global")]
    # 同一个写法 profile 和品牌表都有：合并后 profile 赢，层也记成 profile
    over = layered(monkeypatch, tmp_path / "over", profile_text=PROFILE + "el gel acme => ACME 凝胶\n")
    assert [(tuple(pr), pr.layer) for pr in over.translation_pairs("mira el gel acme")] == [
        (("el gel acme", "ACME 凝胶"), "profile")]
    # 直接用 Glossary(entries) 建的不知道层，整份算全局表
    assert {pr.layer for pr in G.Glossary(parse(BRAND)).translation_pairs("la crema acme")} == {"global"}


@pytest.mark.parametrize("streamer,brand,line,layer", [
    (None, "acme", "la crema acme = ACME 面霜", "brand"),        # 这一场只选了品牌，没传主播
    ("ghost", "acme", "la crema acme = ACME 面霜", "brand"),     # 主播没有自己的 profile：品牌表主要就是给这种主播用的
    ("luna", None, "la loción zeta = ZETA 乳液", "profile"),     # 有 profile，这一场没选品牌
])
def test_a_single_layer_session_still_tags_its_lines_and_sends_them(
        monkeypatch, tmp_path, streamer, brand, line, layer):
    """只加载品牌表或只加载 profile 时，那一层的词条也要记对层、进字幕请求。上面的用例都是两层一起加载的：
    审查时把 load() 记层改成「读到品牌表才记」或「读到 profile 才记」，那些用例照样全绿，可单独一层的
    词条会全记成全局表，字幕请求一条都不带，也不报错。"""
    gl = layered(monkeypatch, tmp_path, streamer=streamer, brand=brand)
    text = "la crema acme, el combo y la loción zeta"
    assert [(pr[0], pr.layer) for pr in gl.translation_pairs(text)] == [
        (line.split(" = ")[0], layer), ("el combo", "global")]
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("面霜、套装和乳液")))]
    lines(monkeypatch, tmp_path, wrapped, [text], gl=gl)
    (post,) = session.posts
    assert post["body"]["system"] == SYSTEM_SLANG + BLOCK + line
    assert not (tmp_path / "gl" / "profiles" / "ghost.txt").exists()   # 没有模板的主播不会凭空建出文件


def test_a_global_glossary_phrase_stays_out_of_the_subtitle_request(monkeypatch, tmp_path):
    """全局表的词条不进字幕请求；译文回来后的兜底替换照旧用全部三层（那边只换残留的西语，没变）。"""
    wrapped, inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("今天带走el combo")))]
    p = lines(monkeypatch, tmp_path, wrapped, ["llévate el combo hoy"])
    assert [pr.layer for pr in p.glossary.translation_pairs("llévate el combo hoy")] == ["global"]
    (post,) = session.posts
    assert post["body"] == inner._body("llévate el combo hoy", "zh-CN")
    assert caption(p)["translated"] == "今天带走套装"


def test_a_profile_term_goes_into_the_subtitle_request_and_a_global_one_on_the_same_line_does_not(
        monkeypatch, tmp_path):
    wrapped, _inner, session = subtitle_engine(monkeypatch)
    session.responses = [Resp(200, message(text_block("ZETA 乳液和套装")))]
    lines(monkeypatch, tmp_path, wrapped, ["la loción zeta y el combo"])
    (post,) = session.posts
    assert post["body"]["system"] == SYSTEM_SLANG + BLOCK + "la loción zeta = ZETA 乳液"


def retranslate(monkeypatch, tmp_path, text, out):
    """点「重译」：字幕引擎是真的 Claude 实例，强模型是真的 Sonnet 5.5 实例（HTTP 都是假的），三层词表。
    返回 (管线, Sonnet 收到的那一个请求)。字幕实例一次都不该被问。"""
    from app.pipeline import QUALITY_FAST
    from tests.test_claude_strong import claude_engine, retranslate_pipeline
    from tests.test_claude_translator import RecordingSession, attach

    gl = layered(monkeypatch, tmp_path)
    wrapped, subtitle = claude_engine(monkeypatch)
    big = translator.create_claude_strong_translator()
    session = RecordingSession([Resp(200, message(text_block(out)))])
    attach(big, session)
    sub_session = subtitle._session = RecordingSession([])
    p = retranslate_pipeline(tmp_path, wrapped, strong=big, quality=QUALITY_FAST)
    p.glossary = gl
    p._recent[7]["text"] = text

    async def scenario():
        await p._strong_translator()          # 同 test_claude_strong：先对一遍引擎种类，已有的实例要保留
        await p.retranslate(7)

    run(scenario())
    assert p._strong is big
    (post,) = session.posts
    assert post["body"]["model"] == "claude-sonnet-5-5"
    assert sub_session.posts == []
    return p, post


def test_retranslate_still_sends_the_lines_of_every_layer(monkeypatch, tmp_path):
    """重译（Sonnet 5.5）：管线给的命中词条三层都进请求，全局表的也在。"""
    _p, post = retranslate(monkeypatch, tmp_path, "la crema acme, el combo y la loción zeta",
                           "ACME 面霜、套装和 ZETA 乳液")
    assert post["body"]["system"].endswith(
        BLOCK + "la loción zeta = ZETA 乳液\nla crema acme = ACME 面霜\nel combo = 套装")


def test_retranslate_leaves_out_the_bracketed_line_and_sends_the_rest(monkeypatch, tmp_path):
    """2026-10-20 那场 46 次重译里，2 次请求带了「食欲焦虑（嘴馋）」，2 次都连括号抄上了屏；用户定「重新翻译也
    不带带括号的词条」。同一句里不带括注的词条照带。Sonnet 把西语品名留在译文里时，兜底替换也不把括注译法
    换上去（残留留着，和词表里没有这一条时一样）。字幕请求那边本来就不带它（上面那条测试），没变。"""
    text = "mira el gel acme y la crema acme"
    p, post = retranslate(monkeypatch, tmp_path, text, "看看el gel acme和 ACME 面霜")
    assert [(pr[0], pr[1]) for pr in p.glossary.translation_pairs(text)] == [
        ("la crema acme", "ACME 面霜"), ("el gel acme", "ACME 凝胶（小瓶）")]
    assert post["body"]["system"] == SYSTEM_SLANG + BLOCK + "la crema acme = ACME 面霜"
    final = [m for m in p.server.of("caption_update") if m.get("translated")][-1]
    assert final["translated"] == "看看el gel acme和 ACME 面霜" and final["strong"] is True


def test_an_alert_context_on_sonnet_leaves_out_the_bracketed_line(monkeypatch, tmp_path):
    """报警上下文翻译用的是同一个 Sonnet 实例（role="strong"），同样不带译法带括注的词条；全局表的照带。"""
    from tests.test_claude_translator import RecordingSession, attach

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    big = translator.create_claude_strong_translator()
    strong_session = RecordingSession([Resp(200, message(text_block("看看 ACME 凝胶、ACME 面霜和套装")))])
    attach(big, strong_session)
    context = "mira el gel acme, la crema acme y el combo"
    p, _inner, session = alert(monkeypatch, tmp_path, strong=big, context=context)
    assert session.posts == []                              # 字幕实例没被问
    (post,) = strong_session.posts
    assert post["body"]["model"] == "claude-sonnet-5-5"
    assert post["body"]["system"] == SYSTEM_SLANG + BLOCK + "la crema acme = ACME 面霜\nel combo = 套装"
    assert p.server.of("alert_update")[-1]["context_zh"] == "看看 ACME 凝胶、ACME 面霜和套装"


def test_no_line_of_the_public_template_reaches_the_subtitle_request(monkeypatch, tmp_path):
    """glossary.example.txt 是每个用户首启都会复制成 glossary.txt 的公开模板：它的每一条（读文件，不在这里
    抄词条）在字幕请求里都不出现；重译里照常出现，只有译法带括注的那几条不出现（「食欲焦虑（嘴馋）」就在里面）。"""
    template = Path(G.ROOT) / "glossary.example.txt"
    root = tmp_path / "tpl"
    root.mkdir()
    shutil.copy(template, root / "glossary.txt")
    monkeypatch.setattr(G, "GLOSSARY_FILE", root / "glossary.txt")
    monkeypatch.setattr(G, "GLOSSARY_EXAMPLE", root / "none.example.txt")
    monkeypatch.setattr(G, "PROFILE_DIR", root / "profiles")
    monkeypatch.setattr(G, "BRAND_DIR", root / "brands")
    gl = G.load()
    entries = parse(template.read_text(encoding="utf-8-sig"))
    assert len(entries) > 20
    wrapped, sub, _session = subtitle_engine(monkeypatch)
    from tests.test_claude_translator import strong as strong_engine
    redo = strong_engine(monkeypatch)
    bracketed = 0
    for variants, zh in entries:
        text = variants[0]
        hint = tuple(gl.translation_pairs(text))
        assert hint and {pr.layer for pr in hint} == {"global"}, text
        assert sub._body(text, "zh-CN", hint) == sub._body(text, "zh-CN"), text
        sent = redo._body(text, "zh-CN", hint)
        if G.has_bracket_note(zh):
            bracketed += 1
            assert zh not in sent["system"], text
        else:
            assert sent != redo._body(text, "zh-CN"), text
    assert bracketed > 0
