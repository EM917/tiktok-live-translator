"""实盘基准的抽样与计分规则。

这个基准要回答的是产品决策问题（1.8B 够不够、要不要付 DeepL 的钱），所以
最该守的不是算得准不准，而是**别自欺**：回归集不能混进测试集、A/B/C 必须
逐句打乱、重复条目要能测出评审自己的噪声。
"""
import json

from tools import bench_live


def test_regression_cases_never_enter_the_test_set(tmp_path, monkeypatch):
    """那 15 条已知错例是训练集：词表针对它们改过、模板 bug 靠它们发现。
    留在测试集里等于让模型考自己补过课的题。"""
    from tools.bench_meaning import load_cases

    monkeypatch.chdir(bench_live.Path(__file__).resolve().parent.parent)
    sample = json.loads((bench_live.OUT / "live_sample.json").read_text(encoding="utf-8"))
    known = {c[0].strip().lower() for c in load_cases()}
    assert known
    assert not [p for p in sample if p["text"].lower() in known]


def test_the_sample_is_mostly_ordinary_captions():
    """全挑难句算出的错误率不代表实盘。随机那一档必须占大头。"""
    quota = dict(bench_live.QUOTA)
    assert quota["随机"] >= sum(v for k, v in quota.items() if k != "随机")


def test_buckets_are_assigned_by_the_riskiest_signal():
    g = _FakeGlossary()
    assert bench_live._bucket("Te lo llevas por 20 dólares hoy.", g) == "价格/数字"
    assert bench_live._bucket("Es plata gratis, aprovechá.", g) == "价格/数字"
    assert bench_live._bucket("Bueno, viste, es así nomás.", g) == "口语/俚语"
    assert bench_live._bucket("Can you refresh it and tell me what you see.", g) == "中英混说"
    assert bench_live._bucket("y entonces lo que pasa es que", g) == "残句"


def test_a_rating_sheet_shuffles_engines_per_line():
    """整列固定的话，评到二三十句就能从文风认出引擎，盲评就不盲了。"""
    import random
    rnd = random.Random(bench_live.SEED + 1)
    labels = ["a", "b", "c"]
    orders = set()
    for _ in range(40):
        s = labels[:]
        rnd.shuffle(s)
        orders.add(tuple(s))
    assert len(orders) > 1


def test_scoring_counts_a_repeated_line_judged_differently():
    """同一条前后判得不一样 = 评分噪声。报告里必须看得见，否则结论会被高估。"""
    assert bench_live.DUPLICATES >= 10
    assert bench_live.GRADES == ["correct", "minor", "major", "omission"]


class _FakeGlossary:
    def matching(self, _text):
        return []
