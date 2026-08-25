"""词不达意回归集的判据。

这个集子是本项目第一个测「中控读了会不会理解错」的指标。此前唯一的翻译指标是
词表遵从率，它只问商品名有没有译成规范写法——「plata gratis」译成「免费银币」
不违反任何词条，却让人完全看不懂。那类错误因此长期没有被任何测量覆盖。
"""
from tools.bench_meaning import judge, load_cases


def test_a_forbidden_word_fails_even_when_the_rest_is_right():
    ok, why = judge("这是 TikTok 送你的免费银币", ["钱"], ["银币"])
    assert not ok and "银币" in why


def test_required_meaning_is_any_of_the_alternatives():
    """中文说法本来就多，写死一种会把对的判成错的。"""
    assert judge("把它吸满", ["滴管", "吸", "装满"], [])[0]
    assert judge("把滴管装满", ["滴管", "吸", "装满"], [])[0]
    assert not judge("给它充电", ["滴管", "吸", "装满"], [])[0]


def test_a_case_may_assert_only_what_must_not_appear():
    """有些错法明确，正确说法却有很多种——只写禁止项，不硬凑必须项。"""
    assert judge("他们会错过", [], ["完蛋"])[0]
    assert not judge("他们就完蛋了", [], ["完蛋"])[0]


def test_the_set_keeps_controls_that_currently_pass(tmp_path):
    """只放错例子的回归集可以被糊弄：改动只要让那几条过就算赢，却可能把本来
    对的句子弄坏。对照组的存在就是为了让那种倒退暴露出来。"""
    cases = load_cases()
    assert len(cases) >= 12
    # 断言里带「禁止」的是错例，只带「必须」的多为对照——两类都要有
    assert any(never for _t, _m, never in cases)
    assert any(must and not never for _t, must, never in cases)


def test_comment_lines_and_blank_lines_are_ignored(tmp_path):
    f = tmp_path / "cases.txt"
    f.write_text("# 说明\n\nhola | 你好 | 再见\n", encoding="utf-8")
    assert load_cases(f) == [("hola", ["你好"], ["再见"])]
