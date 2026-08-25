"""基准判据：术语算不算「翻出来了」。

判据偏严会把所有引擎的分数一起压低，比较时就看不出真实差距——而这个基准
存在的意义正是用来比较。实测踩到的两种误判：
    词表写「MCT 油」，模型输出「MCT油」          → 空格
    词表写「食欲焦虑（嘴馋）」，模型输出「食欲焦虑」  → 括注
"""
from tools._benchdata import term_present


def test_spacing_does_not_count_as_a_miss():
    assert term_present("MCT 油", "它还含有有机 Omega 3 油和MCT油")
    assert term_present("D3 K2 维生素滴剂", "赠送D3 K2维生素滴剂")


def test_a_parenthetical_gloss_is_optional():
    """括号里是给人看的补充说明，模型没带上不算错。"""
    assert term_present("食欲焦虑（嘴馋）", "有助于缓解食欲焦虑")
    assert term_present("水肿（体液滞留）", "可以消水肿")


def test_slash_alternatives_any_one_counts():
    assert term_present("嘴馋 / 想吃东西", "有点想吃东西")
    assert term_present("嘴馋 / 想吃东西", "老是嘴馋")


def test_a_genuine_miss_is_still_a_miss():
    """放宽不能放到把错译也算对——那样基准就废了。"""
    assert not term_present("排毒粉", "单独支付了清洁费用")
    assert not term_present("特价", "按原价出售")
    assert not term_present("蘑菇咖啡", "杏仁奶口味")


def test_empty_inputs_are_safe():
    assert not term_present("排毒粉", "")
    assert not term_present("", "任何内容")
