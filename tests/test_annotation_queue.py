"""标注队列组装：桶归属、主播软上限、DeepL 标注面隔离。

队列不是候选池：人工时间要花在配额均衡的难例上，DeepL 的译文绝不能成为
训练标注的起点（条款约束），单一主播不能主导训练集。
"""
from tools.build_annotation_queue import bucket_of, fill_queue
from tools.mine_training_candidates import skeleton


def _row(src, fams, streamer="a", score=5):
    return {"src": src, "families": fams, "streamer": streamer,
            "score": score, "fast": "x", "strong": None, "engine": "hymt2",
            "session": "s", "seq": 1}


def test_bucket_priority_price_beats_jargon():
    """命中价格的行话句归 price_promo——值钱的是价格关系。曾经排反，
    jargon 抢走 221 条把 price_promo 饿到 72 条。"""
    assert bucket_of(["price_number", "jargon"]) == "price_promo"
    assert bucket_of(["jargon"]) == "jargon"
    assert bucket_of(["hist_major", "price_number"]) == "historic"
    assert bucket_of([]) is None


def test_streamer_cap_holds_through_quota_fill():
    """上限贯穿配额选取：额度用尽的主播让位，不会在配额阶段被重新洗超。"""
    rows = ([_row("a%d con 5 dólares extra" % i, ["price_number"], "big", 9)
             for i in range(50)]
            + [_row("b%d con 7 pesos extra" % i, ["price_number"], "small", 1)
               for i in range(50)])
    picked, taken, _srcs, limit = fill_queue(rows, batch=40, cap=0.5)
    assert limit == 20
    assert taken["big"] == 20                 # 分高但被上限拦住
    assert taken["small"] > 0                 # 让位给了小主播


def test_skeleton_collapses_near_duplicates():
    """"les queda en cincuenta y cinco / te queda a 55"——信息量几乎一样，
    人工只该标一条。"""
    a = skeleton("les queda en cincuenta y cinco")
    b = skeleton("te queda a cincuenta y cinco")
    c = skeleton("te queda a 55")
    assert a == b == c
    assert skeleton("llévate el pack de horchata") != a
