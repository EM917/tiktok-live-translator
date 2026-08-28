"""逐词模糊预算 policy：离线发现碰撞 → 人工确认 → 生产读固定文件。

`curar` 距离 1 就是高频合法词 `durar`（实录 5 场 / 3 主播、10 条历史误报），
仅凭 ASR 文本无法区分——这是信息论上的限制。policy 把这类词的模糊预算降到
0：**主动放弃**那一档召回，exact / variant 照常。预算按词、不按整条词表，
多词短语里 exact 命中的词天然是 anchor。
"""
from pathlib import Path

from app.detector import BannedTermDetector, load_fuzzy_policy

POLICY_FILE = Path("banned_fuzzy_policy.txt")


def hits(detector, text):
    detector.reset_state()
    return [(h["term"], h["tier"]) for h in detector.scan(text, ts=1000.0)]


def test_the_policy_kills_the_durar_false_positive():
    """同一句话：没有 policy 时按预算必然误报，有 policy 后安静。
    两边都断言，证明变化确实来自 policy 而不是别处。"""
    line = "el precio de uno cada uno les va a durar de uno a dos meses"
    assert hits(BannedTermDetector(["curar"]), line) == [("curar", "fuzzy")]
    assert hits(BannedTermDetector(["curar"], fuzzy_policy={"curar": 0}),
                line) == []


def test_exact_and_variant_survive_a_zero_budget():
    """policy 只收模糊层。原样说出违禁词、或它的形态变化，照报不误。"""
    det = BannedTermDetector(["curar"], fuzzy_policy={"curar": 0})
    assert hits(det, "esto te va a curar el higado") == [("curar", "exact")]


def test_budgets_are_per_token_and_exact_tokens_anchor():
    """多词短语不一刀切：某个词预算清零，不影响其它词自己的模糊预算——
    exact 命中的词就是 anchor，另一个词照旧按自己的预算容错。"""
    det = BannedTermDetector(["quemar grasa"], fuzzy_policy={"quemar": 0})
    # anchor 在 quemar（exact），grasa 听错一个字母：仍然命中
    assert hits(det, "para quemar graza rapido") == [("quemar grasa", "fuzzy")]
    # 碰撞方向：quedar ≠ quemar，预算 0 → 不命中
    assert hits(det, "no va a quedar grasa") == []


def test_load_fuzzy_policy_parses_and_normalizes(tmp_path):
    p = tmp_path / "policy.txt"
    p.write_text("# 注释\n"
                 "curar => fuzzy 0   # collision: durar\n"
                 "Médico => fuzzy 1\n"
                 "这行认不出来\n", encoding="utf-8")
    policy = load_fuzzy_policy(p)
    assert policy == {"curar": 0, "medico": 1}    # 归一化与检测端一致
    assert load_fuzzy_policy(tmp_path / "missing.txt") == {}


# ---- 随代码分发的那份 policy：把「主动牺牲」钉进测试，防止被悄悄改掉 ----

def test_shipped_policy_documents_the_sacrifice():
    """这些词的模糊档召回是**主动放弃**的（邻居是高频合法词，文本层无法
    区分）。哪天有人删掉 policy 里的某一行，这条测试会把牺牲的存在感拉回来
    ——恢复模糊档必须重新过 collision audit，不能顺手删一行了事。"""
    policy = load_fuzzy_policy(POLICY_FILE)
    for token in ("curar", "quemar", "quema", "medico"):
        assert policy.get(token) == 0, token


def test_shipped_policy_keeps_real_sessions_quiet():
    """回放 gate 的最小内嵌版：durar 一类话术不再触发 curar 模糊报警。"""
    det = BannedTermDetector(["curar", "quema grasas"],
                             fuzzy_policy=load_fuzzy_policy(POLICY_FILE))
    assert hits(det, "no va a durar mucho el link") == []
    assert hits(det, "quedan poquitas chicas") == []
    # 真说了违禁词照报
    assert hits(det, "este producto quema grasas de verdad") == \
        [("quema grasas", "exact")]
