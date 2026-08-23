"""违禁词检测：业务上漏报代价远高于误报，所以这些用例全部围绕召回展开。"""
from app.detector import (
    TIER_EXACT,
    TIER_FUZZY,
    TIER_VARIANT,
    BannedTermDetector,
    normalize,
)


def det(terms, **kw):
    kw.setdefault("cooldown_sec", 0)      # 测试里不需要防刷屏冷却
    return BannedTermDetector(terms, **kw)


def terms_of(hits):
    return sorted(h["term"] for h in hits)


# ---- 归一化：西语 ASR 的重音/大小写/标点都不稳定 ----

def test_normalize_strips_accents_case_punctuation():
    assert normalize("¡Está PROHIBIDO!") == "esta prohibido"
    assert normalize("Cúrate  ya...") == "curate ya"


def test_empty_word_list_is_disabled():
    d = det([])
    assert d.enabled is False
    assert d.scan("cualquier cosa") == []


# ---- 一级：精确命中 ----

def test_exact_hit():
    d = det(["cura el cancer"])
    hits = d.scan("Este producto cura el cáncer, garantizado.", ts=100)
    assert terms_of(hits) == ["cura el cancer"]
    assert hits[0]["tier"] == TIER_EXACT


def test_accented_input_still_hits_unaccented_term():
    d = det(["prohibido"])
    assert d.scan("Está prohíbido decir eso", ts=100)


# ---- 二级：形态变体（单复数、阴阳性、指小词）----

def test_plural_variant():
    d = det(["medicamento"])
    hits = d.scan("vendemos medicamentos sin receta", ts=100)
    assert hits and hits[0]["tier"] == TIER_VARIANT


def test_gender_variant():
    d = det(["garantizado"])
    hits = d.scan("es una cura garantizada", ts=100)
    assert hits and hits[0]["tier"] == TIER_VARIANT


# ---- 三级：模糊（ASR 听错一两个字母）----

def test_fuzzy_hit_on_asr_typo():
    d = det(["garantizado"])
    hits = d.scan("esto es garantisado totalmente", ts=100)
    assert hits and hits[0]["tier"] in (TIER_VARIANT, TIER_FUZZY)


def test_short_terms_do_not_fuzzy_match():
    # 短词模糊匹配会让误报爆炸——4 个字母的词只做精确/变体
    d = det(["cura"])
    assert d.scan("la curva de la calle", ts=100) == []


# ---- 跨片段：短语被切在两段之间仍要命中（这是切短片段后的常见情况）----

def test_phrase_split_across_segments_still_hits():
    d = det(["sin receta medica"], window_sec=12)
    assert d.scan("vendemos medicamento sin", ts=100) == []
    hits = d.scan("receta medica para todos", ts=101)
    assert terms_of(hits) == ["sin receta medica"]


def test_window_expires_by_time_not_by_segment_count():
    d = det(["sin receta medica"], window_sec=5)
    d.scan("vendemos medicamento sin", ts=100)
    hits = d.scan("receta medica", ts=120)      # 20 秒后，前半句早已滑出窗口
    assert hits == []


# ---- 冷却：同一个词不刷屏，但不同词各自独立 ----

def test_cooldown_suppresses_repeat():
    d = BannedTermDetector(["prohibido"], cooldown_sec=30)
    assert d.scan("prohibido", ts=100)
    assert d.scan("prohibido otra vez", ts=110) == []     # 冷却中
    assert d.scan("prohibido de nuevo", ts=140)           # 冷却过后再报


def test_multiple_terms_reported_independently():
    d = det(["prohibido", "garantizado"])
    hits = d.scan("esto es prohibido y garantizado", ts=100)
    assert terms_of(hits) == ["garantizado", "prohibido"]


def test_hit_carries_context_for_review():
    d = det(["prohibido"])
    hits = d.scan("el vendedor dijo que esta prohibido hacerlo", ts=100)
    assert "prohibido" in hits[0]["context"]


def test_exact_tier_does_not_substring_match():
    # "cura" 不该命中 "curación"——短词子串匹配会让误报爆炸
    d = det(["cura"])
    assert d.scan("la curacion natural del cuerpo", ts=100) == []
    assert d.scan("esto cura todo", ts=200)          # 整词出现才命中


def test_enye_is_not_folded_into_n():
    """ñ 是西语独立字母，不能并成 n——año（年）↔ ano（粗俗词）这类碰撞
    会让高频口语反复误报，几轮下来中控就不信报警了。"""
    assert normalize("año") == "año"
    assert normalize("ano") == "ano"
    assert normalize("niño") == "niño"
    # 其它重音照常抹平（ASR 的重音很不稳定）
    assert normalize("está sí") == "esta si"
    assert normalize("pingüino") == "pinguino"

    d = det(["ano"])
    assert d.scan("hace un año que vendo esto", ts=100) == []   # 不再误报
    assert d.scan("dijo la palabra ano", ts=200)                # 真出现才报
