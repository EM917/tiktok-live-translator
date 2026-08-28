"""训练候选挖掘的家族判定与打分（纯函数层）。

挖掘的存在理由：训练目标由人工/许可明确的来源构建后，瓶颈是人力——
1.8B 已经会的句子边际价值趋零，人力必须花在价格/促销/碎片/行话/分歧/
历史难例上。排序权重定死，两次挖掘结果才可比。
"""
from app.glossary import Glossary
from tools.mine_training_candidates import WEIGHTS, families_of, score_of

G = Glossary([(["el carrito"], "小黄车")])


def test_easy_smalltalk_scores_zero():
    """hola chicas 一类：一个家族都不命中，不进候选池。"""
    assert families_of("hola chicas como estan", "你们好家人们", None, G) == []


def test_money_families_stack():
    fams = families_of("Si aplicas el cupón te sale en 55 dólares",
                       "用优惠码是55美元", None, G)
    assert "price_number" in fams and "promo" in fams
    assert score_of(fams) == WEIGHTS["price_number"] + WEIGHTS["promo"]


def test_fragment_and_jargon():
    fams = families_of("y el carrito...", "还有小黄车……", None, G)
    assert "fragment" in fams and "jargon" in fams


def test_disagreement_needs_a_real_gap():
    src = "cuesta 55 dólares el pack"
    fams = families_of(src, "套装55美元", "套装55美元", G)
    assert "disagreement" not in fams              # 强译一致：无分歧
    fams = families_of(src, "套装五十五", "这个套装卖55美元，很划算", G)
    assert "disagreement" in fams                  # 数字丢失+大改写


def test_historic_labels_carry_the_heaviest_weight():
    fams = families_of("Tenemos el pack 32 de horchata",
                       "我们有32包", None, G,
                       hist={"major": True, "friction": True})
    assert "hist_major" in fams and "hist_friction" in fams
    assert score_of(["hist_major"]) > score_of(["promo"])
