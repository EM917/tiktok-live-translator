

def test_the_determiner_slot_also_accepts_possessives():
    """词条写 `las gotitas`，主播嘴里常说 `tus gotitas`（实录 11 次）。
    89 条词条里 62 条以冠词开头，不认物主代词等于七成条目对这类说法失效——
    实录里 "aprovecha tus gotitas" 被译成了「好好利用你的能量吧」。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse("las gotitas | las gotas => 滴剂\nel carrito => 小黄车"))
    assert g.matching("aprovecha tus gotitas")[0][1] == "滴剂"
    assert g.matching("ya pagó su gotitas")[0][1] == "滴剂"
    assert g.matching("no dejes nada en tu carrito")[0][1] == "小黄车"


def test_a_bare_word_still_does_not_match():
    """只换槽位、不去掉槽位。去掉的话普通词会被绑成商品名，把整段拽偏——
    见 glossary.txt 里 gotitas 那条的教训。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse("las gotitas => 滴剂"))
    assert g.matching("por eso se llama gotitas") == []
    assert g.matching("gotitas") == []


def test_matched_form_is_the_one_actually_in_the_text():
    """apply() 要拿它去替换残留的西语，返回的必须是文本里真实出现的那个写法。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse("las gotas de noche => 夜间版滴剂"))
    assert g.matching("aprovecha tus gotas de noche")[0][0] == "tus gotas de noche"


# ---- apply()：兜底替换的词边界 -------------------------------------------------
# 事故（2026-10-16 审阅）：边界用的是 (?<![\w])…(?![\w])，Python 3 的 \w 连汉字都算，
# 品牌名两侧紧挨汉字时（「试试Sol Dorado吧」）永远替换不上——而中文译文里这才是常态。

def _brand_glossary():
    from app.glossary import Glossary, parse

    return Glossary(parse("sol dorado => 金阳\nla moringa | moringa => 辣木"))


def test_apply_replaces_a_latin_residue_glued_to_chinese_characters():
    g = _brand_glossary()
    cases = [
        ("prueba sol dorado ya", "试试Sol Dorado吧", "试试金阳吧"),
        ("con sol dorado", "用Sol Dorado，很好", "用金阳，很好"),
        ("la moringa es buena", "moringa很好", "辣木很好"),
        ("me gusta la moringa", "我喜欢moringa茶", "我喜欢辣木茶"),
        ("sol dorado", "Sol Dorado", "金阳"),                   # 整句只有它
        ("sol dorado", "(Sol Dorado)", "(金阳)"),               # 紧贴标点本来就行
    ]
    for source, translated, expected in cases:
        assert g.apply(source, translated) == expected, translated


def test_apply_still_refuses_to_cut_into_a_latin_word():
    """放行汉字邻居，不等于放行拉丁词内部：moringas / xmoringa 是别的词，不能切。"""
    g = _brand_glossary()
    for translated in ("这些moringas很好", "xmoringa很好", "超级moringa2号",
                       "moringa_x很好", "las moringas ricas"):
        assert g.apply("la moringa", translated) == translated, translated


def test_apply_treats_accented_latin_letters_as_word_characters():
    """ñ é ü 也是拉丁字母：贴在它们旁边是词的一部分，不是边界。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse("cana => 甘蔗"))
    assert g.apply("cana", "这是cañas好") == "这是cañas好"
    assert g.apply("cana", "这是canáa好") == "这是canáa好"
    assert g.apply("cana", "这是cana好") == "这是甘蔗好"


def test_apply_ignores_case_and_tries_the_longer_variant_first():
    from app.glossary import Glossary, parse

    g = Glossary(parse("moringa | moringa en polvo => 辣木粉"))
    assert g.apply("moringa en polvo", "试试MORINGA EN POLVO吧") == "试试辣木粉吧"   # 长写法整条换，不剩「粉」
    assert g.apply("moringa", "试试Moringa吧") == "试试辣木粉吧"


def test_apply_leaves_a_translation_that_already_has_the_chinese_name_alone():
    """已有中文译法就不动（哪怕别处还残留西语）——中文译法若把西语原词包在里面，
    再替换会展开两遍。这是有意的保守，别为「更彻底」改掉它。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse("sol dorado => Sol Dorado 牌"))
    assert g.apply("sol dorado", "试试Sol Dorado 牌吧") == "试试Sol Dorado 牌吧"
    g2 = _brand_glossary()
    assert g2.apply("sol dorado", "金阳很好，Sol Dorado") == "金阳很好，Sol Dorado"


def test_apply_returns_empty_translations_untouched():
    g = _brand_glossary()
    assert g.apply("sol dorado", None) is None
    assert g.apply("sol dorado", "") == ""
    assert g.apply("nada que ver", "试试Sol Dorado吧") == "试试Sol Dorado吧"   # 源文没命中


BRACKETED = ("ACME 凝胶（小瓶）", "ACME 凝胶 (小瓶)", "ACME 凝胶【小瓶】", "ACME 凝胶[小瓶]")


def test_a_bracketed_note_is_any_opening_bracket_in_the_rendering():
    from app.glossary import has_bracket_note

    assert all(has_bracket_note(r) for r in BRACKETED)
    assert not has_bracket_note("ACME 凝胶")
    assert not has_bracket_note("ACME 牌")


def test_apply_does_not_swap_in_a_bracketed_rendering():
    """括注是给译法加的说明，不是要上屏的字（has_bracket_note）。模型把西语留在译文里时，兜底替换不拿
    带括注的译法去换——2026-10-13 那场 seq 36，模型把西语原句回了出来，兜底替换把 la ansiedad 换成了
    「食欲焦虑（嘴馋）」。残留就留着，和词表里没有这一条时一样；同句里不带括注的词条照换。
    虚构品牌 acme（brands/README.md 的例子）。"""
    from app.glossary import Glossary, parse

    for rendering in BRACKETED:
        g = Glossary(parse("el gel acme => {}\nla crema acme => ACME 面霜".format(rendering)))
        source = "mira el gel acme y la crema acme"
        assert g.apply(source, "看看el gel acme和la crema acme") == "看看el gel acme和ACME 面霜", rendering
        plain = Glossary(parse("la crema acme => ACME 面霜"))
        assert g.apply(source, "el gel acme") == plain.apply(source, "el gel acme") == "el gel acme"


def test_every_caller_goes_through_the_one_apply():
    """字幕、弹幕、重译、报警上下文各处兜底都是同一个 Glossary.apply——边界只在一个地方，
    改这一处就是改全部。有人另写一份边界逻辑，这条就该红。"""
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    for path in sorted(app_dir.glob("*.py")):
        if path.name == "glossary.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\(\?<!\[\\w\]\)", text), \
            "%s 里自己写了一份 \\w 词边界；兜底替换只该走 Glossary.apply" % path.name
