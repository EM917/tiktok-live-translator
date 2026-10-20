

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


def test_the_screen_part_of_a_rendering_is_the_text_before_its_note():
    from app.glossary import screen_rendering

    assert {screen_rendering(r) for r in BRACKETED} == {"ACME 凝胶"}
    assert screen_rendering("ACME 凝胶（小瓶）装") == "ACME 凝胶"   # 括注后面还有字：只要括注前那段
    assert screen_rendering("ACME 牌") == "ACME 牌"                # 不带括注：原样
    assert screen_rendering("（小瓶）") == ""                      # 整条都在括号里：没有能上屏的字


def test_apply_puts_only_the_text_before_the_note_on_screen():
    """括注是给译法加的说明，不是要上屏的字（has_bracket_note）。模型把西语留在译文里时，兜底替换换上的是
    括注前那段：2026-10-13 那场 seq 36，模型把西语原句回了出来，兜底替换把 la ansiedad 换成了整条
    「食欲焦虑（嘴馋）」，括号上了屏。只换掉括号、不留西语：apply 是所有引擎共用的，西语留着就是每个引擎
    都把西语摆在屏上。同句里不带括注的词条照换。虚构品牌 acme（brands/README.md 的例子）。"""
    from app.glossary import Glossary, parse

    for rendering in BRACKETED:
        g = Glossary(parse("el gel acme => {}\nla crema acme => ACME 面霜".format(rendering)))
        source = "mira el gel acme y la crema acme"
        assert g.apply(source, "看看el gel acme和la crema acme") == "看看ACME 凝胶和ACME 面霜", rendering
        assert g.apply(source, "el gel acme") == "ACME 凝胶", rendering
        # 括注前那段已经在译文里：模型翻对了，不动（和不带括注的词条同一条规则）
        assert g.apply(source, "ACME 凝胶，还有el gel acme") == "ACME 凝胶，还有el gel acme", rendering


def test_apply_leaves_a_note_the_model_wrote_itself_alone():
    """兜底替换只换残留的西语，不改模型自己写的字：本地模型的提示词、DeepL 的术语表里照旧是整条译法，
    模型照抄了括注，这里不去删（只管「换上去的不带括注」）。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse("el gel acme => ACME 凝胶（小瓶）"))
    assert g.apply("mira el gel acme", "看看ACME 凝胶（小瓶）") == "看看ACME 凝胶（小瓶）"


def test_apply_skips_a_rendering_that_is_all_note():
    from app.glossary import Glossary, parse

    g = Glossary(parse("el gel acme => （小瓶）"))
    assert g.apply("mira el gel acme", "看看el gel acme") == "看看el gel acme"


def test_apply_still_lets_the_longer_bracketed_form_win():
    """兜底替换照旧按长短取舍（matching() 不带 without_notes）：长写法的译法带括注时，换上的是它括注前那段，
    不是被它包含的短写法——「una orden de 30」该读成下单满 30 美元，不是「订单 30」。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse("la crema acme de noche => ACME 晚霜（小瓶）\nla crema acme => ACME 面霜"))
    source = "mira la crema acme de noche"
    assert g.apply(source, "看看la crema acme de noche") == "看看ACME 晚霜"


def test_without_notes_drops_bracketed_lines_before_the_longest_match():
    """重译请求不带括注词条。先按长短取舍、再去掉括注词条的话，被括注长写法包含的短写法已经被挤掉了，
    这句一条都不剩（模板里「tu orden de」=> 下单满（金额，美元）挤掉「tu orden」=> 订单）。without_notes
    在取舍之前就去掉，短写法回来；不传时一切照旧。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse("la crema acme de noche => ACME 晚霜（小瓶）\nla crema acme => ACME 面霜\n"
                       "suero acme => ACME 精华液"))
    text = "mira la crema acme de noche y el suero acme"
    assert [(es, zh) for es, zh, _ in g.matching(text)] == [
        ("la crema acme de noche", "ACME 晚霜（小瓶）"), ("suero acme", "ACME 精华液")]
    assert [(es, zh) for es, zh, _ in g.matching(text, without_notes=True)] == [
        ("la crema acme", "ACME 面霜"), ("suero acme", "ACME 精华液")]
    assert [tuple(pr) for pr in g.translation_pairs(text, without_notes=True)] == [
        ("la crema acme", "ACME 面霜"), ("suero acme", "ACME 精华液")]
    assert [tuple(pr) for pr in g.translation_pairs(text)] == [
        ("la crema acme de noche", "ACME 晚霜（小瓶）"), ("suero acme", "ACME 精华液")]


def test_without_notes_takes_the_limit_after_dropping_the_bracketed_lines():
    from app.glossary import Glossary, parse

    g = Glossary(parse("uno acme => 一（注）\ndos acme => 二\ntres acme => 三"))
    text = "uno acme, dos acme, tres acme"
    assert [pr[0] for pr in g.translation_pairs(text, limit=2)] == ["uno acme", "dos acme"]
    assert [pr[0] for pr in g.translation_pairs(text, limit=2, without_notes=True)] == ["dos acme", "tres acme"]


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
