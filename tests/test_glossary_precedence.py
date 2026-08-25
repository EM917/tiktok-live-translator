"""词表命中：长写法优先。

有些口语短语的意思取决于后面跟什么，语料里两种意思都出现过：
    un montón de cosas      很多东西
    estoy sentada un montón 坐了很久
    lo hago de vuelta       再做一遍
    de vuelta acá           回到这里
不区分长短的话，一句话会同时命中两条相反的释义，提示词自相矛盾。
"""
from app.glossary import Glossary


def make():
    return Glossary([
        (["un montón de"], "很多"),
        (["sentada un montón", "sentado un montón"], "坐了很久"),
        (["lo hago de vuelta"], "再做一遍"),
    ])


def zh(g, text):
    return sorted(x[1] for x in g.matching(text))


def test_the_longer_form_wins():
    g = make()
    assert zh(g, "sirve para un montón de cosas") == ["很多"]
    assert zh(g, "estoy sentada un montón, entonces") == ["坐了很久"]


def test_overlapping_but_not_nested_forms_are_both_kept():
    """规则只处理**包含**关系，不处理交叠。

    "sentada un montón de veces" 里两个写法互相交叠、谁也不包含谁，于是两条
    都会进提示词。按长度取舍在这里恰好是错的——这句真正的意思是「坐下很多次」，
    该赢的是较短的 `un montón de`。这是语言本身的歧义，长短规则解决不了，
    所以不假装解决：如实记下边界，1044 段真实语料里没有出现过这种交叠。
    """
    g = make()
    assert len(g.matching("estoy sentada un montón de veces")) == 2


def test_unrelated_entries_still_all_match():
    """长者优先只在互为子串时生效，不能顺手吃掉不相干的词条。"""
    g = Glossary([(["las gotas"], "滴剂"), (["gratis"], "免费")])
    assert zh(g, "van a agarrar las gotas gratis") == ["免费", "滴剂"]


def test_a_phrase_not_present_does_not_match():
    g = make()
    assert g.matching("y de vuelta acá estaría bueno") == []
