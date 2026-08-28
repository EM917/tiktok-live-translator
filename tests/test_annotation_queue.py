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


def test_holdout_manifest_is_frozen_and_loadable():
    """holdout 静默失效比没有更糟。清单必须能读出、且两类冻结都在。"""
    from app.provenance import eval_holdout

    h = eval_holdout()
    assert "session-20260826-133138.jsonl" in h["sessions"]   # 260 对考卷
    assert "elisa._martinez" in h["streamers"]                # 未见主播考场


def test_reference_allowlist_not_blacklist():
    """参考显示走 allowlist：来源不明视同不许可，不是「不是 deepl 就行」。"""
    from tools.build_annotation_queue import reference_view

    local = reference_view({"fast": "好", "engine": "hymt2",
                            "strong": "很好",
                            "strong_model": "hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M"})
    assert local["fast"] == "好" and local["strong"] == "很好"

    v = reference_view({"fast": "x", "engine": "deepl",
                        "strong": "y", "strong_model": "some-future-cloud-mt"})
    assert v["fast"] is None and v["fast_withheld"] == "deepl"
    assert v["strong"] is None and v["strong_withheld"] == "some-future-cloud-mt"

    unknown = reference_view({"fast": "x", "engine": None,
                              "strong": None, "strong_model": None})
    assert unknown["fast"] is None and unknown["fast_withheld"] == "unknown"


def test_rebalance_caps_final_share():
    """成型后的配平：实际占比 ≤40%，裁分低的行、不裁对照组，宁短不凑。"""
    from tools.build_annotation_queue import rebalance

    queue = ([dict(_row("a%d" % i, ["promo"], "big", score=i), bucket="price_promo")
              for i in range(80)]
             + [dict(_row("b%d" % i, ["promo"], "mid", score=2), bucket="price_promo")
                for i in range(30)]
             + [dict(_row("c%d" % i, ["promo"], "small", score=1), bucket="price_promo")
                for i in range(20)])
    out = rebalance(queue, share_max=0.40)
    from collections import Counter
    dist = Counter(r["streamer"] for r in out)
    assert dist["big"] / len(out) <= 0.40 + 1e-9
    assert dist["mid"] == 30 and dist["small"] == 20    # 其余主播一条没少
    kept_big = [r["src"] for r in out if r["streamer"] == "big"]
    assert "a79" in kept_big and "a0" not in kept_big   # 裁的是分低的


def test_rebalance_stops_at_structural_floor():
    """只有两个主播时占比下限是 50%——裁到与第二名持平就停，绝不裁空。"""
    from tools.build_annotation_queue import rebalance

    queue = ([dict(_row("a%d" % i, ["promo"], "big", score=i), bucket="price_promo")
              for i in range(80)]
             + [dict(_row("b%d" % i, ["promo"], "small", score=1), bucket="price_promo")
                for i in range(20)])
    out = rebalance(queue, share_max=0.40)
    from collections import Counter
    dist = Counter(r["streamer"] for r in out)
    assert dist["big"] == dist["small"] == 20


def test_skeleton_collapses_near_duplicates():
    """"les queda en cincuenta y cinco / te queda a 55"——信息量几乎一样，
    人工只该标一条。"""
    a = skeleton("les queda en cincuenta y cinco")
    b = skeleton("te queda a cincuenta y cinco")
    c = skeleton("te queda a 55")
    assert a == b == c
    assert skeleton("llévate el pack de horchata") != a
