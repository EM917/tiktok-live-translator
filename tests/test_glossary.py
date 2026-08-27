

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
