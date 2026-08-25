

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


def test_only_entities_become_whisper_hotwords():
    """识别热词只能放专有名词。Whisper 的 initial_prompt 是当作上文续写的，
    塞进「下单满…美元」这类动词短语会诱导它把没说的话听出来——实测 99 个词的
    提示曾让它凭空多识别出一个词。

    这条以前是靠**文件顺序**碰巧成立的（商品恰好排在前 18 条），有人往前面插
    一条促销短语就会污染识别。现在按类型挑，与顺序无关。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse(
        "# [类型: 短语]\n"
        "una orden de => 下单满（金额，美元）\n"
        "# [类型: 实体]\n"
        "la moringa => 辣木\n"))
    prompt = g.asr_prompt()
    assert "moringa" in prompt
    assert "orden" not in prompt


def test_both_types_still_feed_the_translation_hint():
    """翻译提示两类都要——商品名和业务动作都会被译错。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse(
        "# [类型: 实体]\nla moringa => 辣木\n"
        "# [类型: 短语]\nte ancle => 置顶到小黄车\n"))
    pairs = dict(g.translation_pairs("quieres que te ancle la moringa"))
    assert pairs == {"la moringa": "辣木", "te ancle": "置顶到小黄车"}


def test_an_untyped_glossary_still_behaves_as_before():
    """不写类型默认是实体，旧词表原样可用。"""
    from app.glossary import Glossary, parse

    g = Glossary(parse("la moringa => 辣木"))
    assert g.kinds == ["实体"] and "moringa" in g.asr_prompt()
