"""tools/bench_claude_vs_local.py：抽样、配对、统计、费用估算、缓存与 --dry-run。

一律不联网：HTTP 层是假的，git 是假的，pgrep 是假的，等待是假的。夹具里的主播、品牌、
词表都是虚构的，日志写在 tmp_path 里——绝不碰真实的 logs/。
"""
import asyncio
import hashlib
import json
import re
from pathlib import Path

import pytest

from app import glossary as glossary_mod
from app.translator import ClaudeTranslator
from tools import bench_claude_vs_local as bench
from tools.bench_meaning import load_cases
from tests.helpers import run

REPO = Path(__file__).resolve().parent.parent
COMMIT = "abc1234"
KEY = "sk-ant-sentinel-do-not-print-0000"

GLOBAL = "envío gratis | envio gratis => 包邮\nsolo por hoy => 仅限今天\n"
PROFILE = "las gotitas => 维生素滴剂\nel batido verde => 绿色奶昔\n"
PROFILE_OLD = "las gotitas => 滴剂\n"


# ---------------------------------------------------------------------------
# 夹具：一套虚构的程序目录 + 会话日志
# ---------------------------------------------------------------------------

class FakeGit:
    """code_commit 的 glossary.py 就是现在这一份；没有模板历史。"""

    def __init__(self, same=True):
        self.same = same
        self.shown = []

    def show(self, rev, path):
        self.shown.append((rev, path))
        if path == "app/glossary.py" and rev == COMMIT:
            src = (REPO / "app" / "glossary.py").read_bytes()
            return src if self.same else src.replace(b"limit=6", b"limit=5")
        return None

    def history(self, path):
        return []

    def run(self, *args):
        return b"abc1234\n"


def h12(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:12]


def merged_hash(global_text, profile_text=None):
    entries = glossary_mod.parse(global_text)
    if profile_text:
        entries = glossary_mod._merge(glossary_mod.parse(profile_text), entries)
    return glossary_mod.fingerprint(entries)


WORDS = ["rojo", "azul", "verde", "lindo", "suave", "fresco", "rico", "nuevo", "fuerte", "claro",
         "dulce", "fino", "grande", "ligero", "limpio", "puro", "seco", "simple", "sano", "tibio"]


def sentence(i, extra=""):
    """互不重复、骨架也不重复的西语句子（骨架会把数字、代词、冠词抹平，所以用不同的词）。"""
    a, b, c = WORDS[i % 20], WORDS[(i // 20) % 20], WORDS[(i // 400) % 20]
    return "hoy probamos algo {} {} {} chicas{}.".format(a, b, c, extra)


def session_start(streamer, profile_text=None, global_text=GLOBAL, commit=COMMIT, **extra):
    row = {"type": "session_start", "room_url": "https://www.tiktok.com/@{}/live".format(streamer),
           "streamer": streamer, "started_at": "2026-10-01T12:00:00", "code_commit": commit,
           "translator_active": "hymt2", "glossary_hash": h12(global_text),
           "profile": streamer if profile_text else None,
           "profile_hash": h12(profile_text) if profile_text else None,
           "brand": None, "brand_hash": None,
           "merged_glossary_hash": merged_hash(global_text, profile_text)}
    row.update(extra)
    return row


def seg_rows(seq, text, translated, engine="hymt2", language="es", ok=True, ms=300.0):
    return [{"type": "segment", "seq": seq, "language": language, "text": text, "raw_text": text},
            {"type": "translation", "seq": seq, "translated": translated, "translate_ms": ms,
             "ok": ok, "engine": engine}]


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


@pytest.fixture
def world(tmp_path):
    """root（词表 + eval_holdout.json）与 logs（几场会话），返回路径和预期。"""
    root = tmp_path / "root"
    (root / "profiles").mkdir(parents=True)
    (root / "glossary.txt").write_text(GLOBAL, encoding="utf-8")
    # 主播 profile 后来改过：当时那一版只剩 .bak
    (root / "profiles" / "bellatest.txt").write_text(PROFILE + "el cafecito => 蘑菇咖啡\n",
                                                     encoding="utf-8")
    (root / "profiles" / "bellatest.txt.bak-20261001-000000").write_text(PROFILE, encoding="utf-8")
    (root / "profiles" / "vocal.txt").write_text("vocative_strip: on\n" + PROFILE_OLD,
                                                 encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    regression = load_cases()[0][0]
    a = [session_start("bellatest", PROFILE)]
    seq = 0
    for i in range(300):
        seq += 1
        text = sentence(i, " con las gotitas" if i % 3 == 0 else "")
        local = "今天试试{}号{}".format(i, "，维生素滴剂" if i % 3 == 0 else "")
        a += seg_rows(seq, text, local)
    a += seg_rows(seq + 1, "", "空")                                  # 整段被拒
    a += seg_rows(seq + 2, "hola chicas", "大家好")                   # 少于 3 个词
    a += seg_rows(seq + 3, "otra cosa muy distinta aquí", "别的", engine="deepl")
    a += seg_rows(seq + 4, "this is an english sentence", "英文", language="en")
    a += seg_rows(seq + 5, sentence(0, " con las gotitas"), "重复")    # 规范化后重复
    a += seg_rows(seq + 6, regression, "回归集")
    a += seg_rows(seq + 7, "una frase sin traducción alguna", "x", ok=False)
    a.append("{\"type\": \"segment\", \"seq\": 99")                  # 半行
    # 同一个文件里第二场：seq 重新编号
    a.append(session_start("daisytest"))
    for i in range(120):
        a += seg_rows(i + 1, sentence(1000 + i), "第二场{}".format(i))
    write_jsonl(logs / "session-20261001-120000.jsonl", [r for r in a if isinstance(r, dict)])
    with open(logs / "session-20261001-120000.jsonl", "a", encoding="utf-8") as fh:
        fh.write("{\"type\": \"segment\", \"seq\"\n")
    # 一场来自密封考卷、一场测试夹具、一场没有来源记录、一场词表对不上、一场开了 vocative_strip
    write_jsonl(logs / "session-20261002-120000.jsonl",
                [session_start("sealedtest")] + seg_rows(1, sentence(5000), "密封"))
    fixture = session_start("x")
    fixture["room_url"] = "https://www.tiktok.com/@x/live"
    write_jsonl(logs / "session-20261003-120000.jsonl", [fixture] + seg_rows(1, sentence(5001), "夹具"))
    old = session_start("oldtest")
    old.pop("merged_glossary_hash")
    write_jsonl(logs / "session-20261004-120000.jsonl", [old] + seg_rows(1, sentence(5002), "旧"))
    write_jsonl(logs / "session-20261005-120000.jsonl",
                [session_start("lost", merged_glossary_hash="ffffffffffff")]
                + seg_rows(1, sentence(5003), "对不上"))
    write_jsonl(logs / "session-20261006-120000.jsonl",
                [session_start("vocal", "vocative_strip: on\n" + PROFILE_OLD)]
                + seg_rows(1, sentence(5004), "称呼"))
    holdout = {"dev_eval": {"sessions": {"session-20261001-120000.jsonl": "x"}, "streamers": {}},
               "sealed_test": {"sessions": {"session-20261002-120000.jsonl": "x"}, "streamers": {}}}
    (root / "eval_holdout.json").write_text(json.dumps(holdout), encoding="utf-8")
    return {"root": root, "logs": logs, "out": tmp_path / "out", "regression": regression}


def resolver_for(world, same=True):
    return bench.GlossaryResolver(world["root"], FakeGit(same))


def pool_for(world, same=True):
    holdout = bench.read_holdout(world["root"] / "eval_holdout.json")
    return bench.build_pool(world["logs"], resolver_for(world, same), holdout,
                            regression=[world["regression"]], exam=set())


def snapshot(path):
    return sorted((str(p.relative_to(path)), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in Path(path).rglob("*"))


# ---------------------------------------------------------------------------
# 配对
# ---------------------------------------------------------------------------

def test_blocks_split_at_each_session_start_and_skip_torn_lines(world):
    blocks = list(bench.iter_blocks(world["logs"] / "session-20261001-120000.jsonl"))
    assert [b["start"]["streamer"] for b in blocks] == ["bellatest", "daisytest"]
    # 两条半行（崩溃时写了一半）跳过；每场的行数只算完整的那些
    assert [len(b["rows"]) for b in blocks] == [2 * 307, 2 * 120]


def test_pairs_are_matched_by_seq_inside_one_session_with_neighbouring_lines():
    block = {"start": {}, "rows": [
        {"type": "segment", "seq": 1, "text": "uno dos tres"},
        {"type": "translation", "seq": 2, "translated": "二", "ok": True, "engine": "hymt2"},
        {"type": "segment", "seq": 2, "text": ""},
        {"type": "translation", "seq": 1, "translated": "", "ok": True, "engine": "hymt2"},
        {"type": "translation", "seq": 1, "translated": "一", "ok": True, "engine": "hymt2"},
        {"type": "segment", "seq": 3, "text": "cuatro cinco seis"},
    ]}
    pairs = bench.pairs_of_block(block)
    assert [(s["seq"], t and t["translated"], p, n) for s, t, p, n in pairs] == [
        (1, "一", "", "cuatro cinco seis"),
        (2, "二", "uno dos tres", "cuatro cinco seis"),
        (3, None, "uno dos tres", "")]


def test_the_pool_keeps_only_same_source_hymt2_pairs_and_counts_every_exclusion(world):
    pool, funnel, sessions = pool_for(world)
    assert funnel["候选池"] == len(pool) == 420
    assert funnel["sealed_test（密封考卷）"] == 1
    assert funnel["测试夹具会话"] == 1
    assert funnel["没有来源记录（08-27 之前）"] == 1
    assert funnel["词表无法逐字还原"] == 1
    assert funnel["vocative_strip 开着（送翻文本无法还原）"] == 1
    assert funnel["text 为空（整段被拒）"] == 1
    assert funnel["少于 3 个词"] == 1
    assert funnel["引擎不是 hymt2（deepl）"] == 1
    assert funnel["非西语"] == 1
    assert funnel["规范化后重复"] == 1
    assert funnel["回归集（meaning_cases）"] == 1
    assert funnel["没有 ok 译文"] == 1
    assert len({p["id"] for p in pool}) == len(pool)
    first = next(p for p in pool if p["streamer"] == "bellatest" and p["seq"] == 1)
    assert first["text"] == sentence(0, " con las gotitas")
    assert first["local"] == "今天试试0号，维生素滴剂" and first["local_ms"] == 300.0
    # 当时那一版 profile 的词（.bak 里的），不是现在文件里的
    assert first["entries"] == [[["las gotitas", "tu gotitas", "tus gotitas", "su gotitas",
                                  "sus gotitas", "mi gotitas", "mis gotitas", "nuestro gotitas",
                                  "nuestra gotitas"], "维生素滴剂"]]
    second = [p for p in pool if p["streamer"] == "daisytest"]
    assert len(second) == 120 and second[0]["seq"] == 1          # 第二场 seq 重新编号
    assert all(p["prev"] or p["seq"] == 1 for p in second)


def test_a_session_whose_glossary_code_differs_is_excluded_not_guessed(world):
    pool, funnel, _s = pool_for(world, same=False)
    assert pool == [] and funnel["该场 glossary.py 版本对不上"] == 307 + 120 + 1 + 1


def test_a_comment_only_edit_to_the_global_list_still_resolves_but_a_profile_must_match(world):
    """全局表只改了注释：字节指纹对不上，条目一字不差，合并指纹照样对上。profile 不退回：
    开关行不在指纹里，换一份字节就说不清当时 vocative_strip 开没开。"""
    (world["root"] / "glossary.txt").write_text("# 新加的注释\n" + GLOBAL, encoding="utf-8")
    resolver = resolver_for(world)
    g, why, _o = resolver.for_session(session_start("bellatest", PROFILE))
    assert why == "ok" and g.translation_pairs("envío gratis hoy") == [("envío gratis", "包邮")]
    g, why, _o = resolver.for_session(session_start("bellatest", PROFILE,
                                                    profile_hash="000000000000"))
    assert g is None and why == "glossary_unmatched"


def test_the_resolver_reads_only_and_never_creates_files(world):
    (world["root"] / "glossary.txt").unlink()                # glossary.load() 在这时会复制模板
    before = snapshot(world["root"])
    holdout = bench.read_holdout(world["root"] / "eval_holdout.json")
    bench.build_pool(world["logs"], resolver_for(world), holdout, exam=set())
    assert snapshot(world["root"]) == before


def test_a_mini_glossary_behaves_like_the_whole_one():
    """样本只存命中的词条：matching / translation_pairs / apply 必须和整张表一样。"""
    text = ("Hoy envío gratis solo por hoy con tus gotitas y la limpieza de la limpiecita, "
            "un montón de cosas")
    full = glossary_mod.Glossary(glossary_mod.parse(
        "la limpieza => 排毒粉\nla limpiecita => 排毒粉小包\nun montón de cosas => 很多东西\n"
        "un montón => 很多\n" + GLOBAL + PROFILE))
    entries = [[list(v), zh] for _h, zh, v in full.matching(text)]
    mini = bench.mini_glossary(entries)
    assert mini.matching(text) == full.matching(text)
    assert mini.translation_pairs(text) == full.translation_pairs(text)
    out = "今天 envío gratis，用 gotitas"
    assert mini.apply(text, out) == full.apply(text, out)


# ---------------------------------------------------------------------------
# 抽样
# ---------------------------------------------------------------------------

def test_allocation_matches_the_eval_plan_for_the_real_pool_sizes():
    """eval-plan 的例子：N=500 时随机 350 条，bella 封顶 40%，其余按池子成比例。"""
    sizes = {"bellaallnatural": 25310, "daisycabral_": 5320, "susycampos12": 3590,
             "itzesantana11": 2810, "celestepimienta2": 727, "jessyjewelry1": 643,
             ".brendaayala1": 282, "其他": 316}
    assert bench.allocate(sizes, 350) == {
        "bellaallnatural": 140, "daisycabral_": 82, "susycampos12": 55, "itzesantana11": 43,
        "celestepimienta2": 11, "jessyjewelry1": 10, ".brendaayala1": 4, "其他": 5}


def test_allocation_never_exceeds_a_pool_or_the_total():
    got = bench.allocate({"a": 1000, "b": 3, "c": 5}, 50)
    assert got == {"a": 42, "b": 3, "c": 5} and sum(got.values()) == 50
    assert bench.allocate({"a": 10}, 50) == {"a": 10}
    assert bench.largest_remainder({"x": 1.5, "y": 1.5, "z": 0.0}, 3) == {"x": 2, "y": 1, "z": 0}


def test_the_sample_is_reproducible_split_and_capped(world):
    pool, _f, _s = pool_for(world)
    one, notes = bench.draw_sample(pool, 100, seed=7)
    two, _n2 = bench.draw_sample(pool, 100, seed=7)
    other, _n3 = bench.draw_sample(pool, 100, seed=8)
    assert [p["id"] for p in one] == [p["id"] for p in two]
    assert [p["id"] for p in one] != [p["id"] for p in other]
    assert len(one) == 100 and len({p["id"] for p in one}) == 100
    random_part = [p for p in one if p["part"] == "random"]
    assert len(random_part) == 70
    # 只有两个主播：40% 的上限放不下 70 条，按剩余池子放宽，并且说出来
    assert sorted(sum(p["group"] == g for p in random_part) for g in ("bellatest", "daisytest")) \
        == [32, 38]
    assert any("上限放不下" in n for n in notes)
    for p in one:
        assert p["bucket"] and set(p["flags"]) == {"has_number", "promo", "fragment", "product_term"}
    hard = [p for p in one if p["part"] == "hard"]
    assert len(hard) == 30 and all(p["bucket"] == "商品名/词表" for p in hard)


def test_the_hard_quotas_scale_past_five_hundred():
    shares = {g: 600 * w / 150 for g, w in bench.HARD_QUOTA}
    assert sum(bench.largest_remainder(shares, 600).values()) == 600


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("b,c,p", [(35, 16, 0.011), (28, 13, 0.028), (29, 15, 0.049)])
def test_exact_mcnemar_reproduces_the_0901_round(b, c, p):
    """09-01 四方卷：1.8B 对 DeepL / 12B / 7B 的 major 不一致对（eval-plan §1.2 复现过）。"""
    assert round(bench.mcnemar_exact(b, c), 3) == p


def test_exact_mcnemar_edges():
    assert bench.mcnemar_exact(0, 0) == 1.0
    assert bench.mcnemar_exact(5, 5) == 1.0
    assert bench.mcnemar_exact(8, 30) < 0.001
    assert bench.mcnemar_exact(0, 6) == pytest.approx(2 / 64)


def test_wald_interval_matches_the_engine_benchmarks_doc():
    """docs：7B 对 DeepL，b=25、c=20、n=259 → 1.9%，[-3.1%, +7.0%]。这里的差是「后者 − 前者」。"""
    d, lo, hi = bench.wald_ci(25, 20, 259)
    assert (round(100 * d, 1), round(100 * lo, 1), round(100 * hi, 1)) == (-1.9, -7.0, 3.1)
    d, lo, hi = bench.wald_ci(20, 25, 259)
    assert (round(100 * d, 1), round(100 * lo, 1), round(100 * hi, 1)) == (1.9, -3.1, 7.0)


def test_newcombe_interval_contains_the_difference_and_tightens_with_n():
    d, lo, hi = bench.newcombe_ci(10, 25, 20, 204)
    assert lo < d < hi and -1 <= lo and hi <= 1
    assert d == pytest.approx((20 - 25) / 259)
    _d2, lo2, hi2 = bench.newcombe_ci(40, 100, 80, 816)
    assert hi2 - lo2 < hi - lo
    assert bench.newcombe_ci(0, 0, 0, 50)[1:] == pytest.approx((-0.0712, 0.0712), abs=1e-3)


def test_holm_adjusts_in_order_and_stays_monotone():
    assert bench.holm([0.01, 0.04]) == pytest.approx([0.02, 0.04])
    assert bench.holm([0.04, 0.01, 0.03]) == pytest.approx([0.06, 0.03, 0.06])
    assert bench.holm([0.5, 0.9]) == pytest.approx([1.0, 1.0])


def test_kappa_clopper_pearson_and_percentiles():
    assert bench.cohen_kappa([1, 1, 0, 0], [1, 1, 0, 0]) == 1.0
    assert bench.cohen_kappa([1, 0, 1, 0], [1, 1, 0, 0]) == pytest.approx(0.0)
    assert bench.clopper_pearson(0, 10) == pytest.approx((0.0, 0.3085), abs=1e-3)
    assert bench.clopper_pearson(5, 10) == pytest.approx((0.1871, 0.8129), abs=1e-3)
    assert bench.clopper_pearson(10, 10)[1] == 1.0
    xs = list(range(1, 101))
    assert bench.percentile(xs, 0.5) == 51 and bench.percentile(xs, 0.99) == 100
    assert bench.median([3, 1, 2, 4]) == 2.5


def test_paired_test_counts_the_table():
    res = bench.paired_test([(True, True), (True, False), (True, False), (False, True),
                             (False, False)])
    assert (res["a"], res["b"], res["c"], res["d"], res["n"]) == (1, 2, 1, 1, 5)
    assert (res["local_bad"], res["claude_bad"]) == (3, 2)
    assert res["diff"] == pytest.approx(-0.2)


# ---------------------------------------------------------------------------
# 费用
# ---------------------------------------------------------------------------

def test_usage_cost_uses_the_published_rate_cards():
    assert bench.usage_cost("claude-haiku-5-5", {"input_tokens": 100_000}) == pytest.approx(0.01)
    assert bench.usage_cost("claude-haiku-5-5", {"output_tokens": 1_000_000}) == pytest.approx(0.50)
    # 提示超过 10 万 token 换高价那张表
    assert bench.usage_cost("claude-haiku-5-5", {"input_tokens": 400_000}) == pytest.approx(0.20)
    assert bench.usage_cost("claude-sonnet-5-5", {"input_tokens": 1000, "output_tokens": 1000}) \
        == pytest.approx(0.012)
    assert bench.usage_cost("claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 1000,
                                                 "cache_read_input_tokens": 1000}) \
        == pytest.approx(0.0244)
    with pytest.raises(bench.Stop):
        bench.usage_cost("claude-unknown", {"input_tokens": 1})


def test_estimates_scale_with_the_sample_and_name_every_phase(world):
    pool, _f, _s = pool_for(world)
    small, _ = bench.draw_sample(pool, 50, 1)
    big, _ = bench.draw_sample(pool, 100, 1)
    e1 = bench.estimate_costs(small, "claude-haiku-5-5", ["claude-sonnet-5-5", "claude-opus-5-5"],
                              "claude-opus-5-5")
    e2 = bench.estimate_costs(big, "claude-haiku-5-5", ["claude-sonnet-5-5", "claude-opus-5-5"],
                              "claude-opus-5-5")
    assert [e["kind"] for e in e1] == ["translate", "judge", "judge", "adjudicate"]
    assert [e["calls"] for e in e2] == [100, 125, 105, 25]
    for a, b in zip(e1, e2):
        assert b["median"] == pytest.approx(2 * a["median"], rel=0.15) and b["high"] > b["median"]
    # 翻译关了思考：每句不到千分之一美元
    assert e2[0]["median"] / 100 < 0.001


def test_the_budget_stops_before_a_request_would_cross_the_cap():
    b = bench.Budget(1.0, spent=0.98)
    b.check(0.01)
    with pytest.raises(bench.Stop, match="没有发"):
        b.check(0.03)


def test_the_estimate_uses_the_production_request_body(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.delenv("CLAUDE_TRANSLATE_MODEL", raising=False)
    assert bench.production_body("claude-haiku-5-5", "hola amigas") \
        == ClaudeTranslator()._body("hola amigas", "zh-CN")


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def test_the_cache_resumes_and_skips_a_torn_last_line(tmp_path):
    c = bench.JsonlCache(tmp_path / "x.jsonl")
    c.append({"key": "a", "cost": 0.5})
    c.append({"key": "b", "cost": 0.25})
    with open(tmp_path / "x.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"key": "c", "co')
    again = bench.JsonlCache(tmp_path / "x.jsonl")
    assert "a" in again and "b" in again and "c" not in again
    assert again.spent() == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# 假 HTTP 层上的翻译、评分、报告
# ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.closed = False

    async def post(self, url, headers, body):
        self.calls.append({"url": url, "headers": headers, "body": body})
        status, payload, hdrs = self.handler(body, len(self.calls))
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return status, hdrs or {}, text, 250.0

    async def close(self):
        self.closed = True


def message(model, text, stop="end_turn", usage=None, **extra):
    content = [{"type": "thinking", "thinking": "", "signature": "s"}] if stop != "refusal" else []
    if text is not None:
        content.append({"type": "text", "text": text})
    return dict({"type": "message", "model": model, "content": content, "stop_reason": stop,
                 "usage": usage or {"input_tokens": 80, "output_tokens": 20}}, **extra)


def grade(text):
    bad = "坏" in text
    return {"verdict": "major" if bad else "correct", "error_type": "meaning" if bad else "none",
            "tier": "unusable" if bad else "good", "friction": bad, "note": "x"}


def handler(body, n):
    """翻译：原文里有 malo 就译坏、有 rechazo 就拒答；评委：译文里有「坏」就判 major，
    Opus 还把带「疑」的也判 major（制造分歧，让裁决有事做）。"""
    model = body["model"]
    if model == "claude-haiku-5-5":
        text = body["messages"][0]["content"]
        if "rechazo" in text:
            return 200, dict(message(model, "这款", stop="refusal"),
                             stop_details={"type": "refusal", "category": "general_harms"}), None
        return 200, message(model, "坏译文" if "malo" in text else "好译文"), None
    prompt = body["messages"][0]["content"]
    a = re.search(r"<译文A>(.*?)</译文A>", prompt).group(1)
    b = re.search(r"<译文B>(.*?)</译文B>", prompt).group(1)

    def g(t):
        if model == "claude-opus-5-5" and "疑" in t:
            return grade("坏")
        return grade(t)

    return 200, message(model, json.dumps({"A": g(a), "B": g(b)}, ensure_ascii=False),
                        usage={"input_tokens": 1500, "output_tokens": 900}), None


def no_live(cmd, capture_output=True):
    class R:
        returncode = 1
    return R()


def items_for_run(n=12):
    items = []
    for i in range(n):
        text = sentence(i) + (" malo" if i % 4 == 0 else "") + (" rechazo" if i == 5 else "")
        local = "本地{}".format("坏" if i % 3 == 0 else ("疑" if i == 7 else ""))
        items.append({"id": "s#0#{}".format(i), "file": "session-x.jsonl", "streamer": "bellatest",
                      "text": text, "local": local, "local_ms": 300.0, "entries": [],
                      "prev": "", "next": "", "part": "random" if i < 8 else "hard",
                      "group": "bellatest", "bucket": "随机",
                      "flags": {"has_number": False, "promo": False, "fragment": False,
                                "product_term": False}})
    return items


async def noop_sleep(_s):
    return None


def test_translation_uses_the_production_request_and_resumes_without_paying_twice(tmp_path):
    items = items_for_run()
    t = FakeTransport(handler)
    api = bench.Api(KEY, t, sleep=noop_sleep)
    cache = bench.JsonlCache(tmp_path / "translations.jsonl")
    budget = bench.Budget(1.0)
    logs = []
    assert run(bench.translate_phase(items, "claude-haiku-5-5", api, cache, budget, no_live,
                                     logs.append)) == 12
    body = t.calls[0]["body"]
    assert body == bench.production_body("claude-haiku-5-5", items[0]["text"])
    assert body["thinking"] == {"type": "disabled"} and body["output_config"] == {"effort": "low"}
    assert not {"temperature", "top_p", "top_k", "fallbacks"} & set(body)
    assert t.calls[0]["headers"]["x-api-key"] == KEY
    rec = cache.get("claude-haiku-5-5|s#0#5")
    assert rec["stop_reason"] == "refusal" and rec["out"] is None and rec["category"] == "general_harms"
    assert bench.production_outcome(rec) == (None, "refusal")
    assert cache.get("claude-haiku-5-5|s#0#1")["out"] == "好译文"
    assert budget.spent == pytest.approx(12 * (80 * 0.10 + 20 * 0.50) / 1e6)
    # 重跑：全在缓存里，一个请求都不发
    again = bench.JsonlCache(tmp_path / "translations.jsonl")
    assert run(bench.translate_phase(items, "claude-haiku-5-5", api, again, bench.Budget(1.0),
                                     no_live, logs.append)) == 0
    assert len(t.calls) == 12


def test_rate_limits_and_overload_are_retried_after_the_servers_wait(tmp_path):
    waits = []

    async def sleep(s):
        waits.append(s)

    def flaky(body, n):
        if n == 1:
            return 429, {"type": "error", "error": {"type": "rate_limit_error"}}, {"retry-after": "7"}
        if n == 2:
            return 529, {"type": "error", "error": {"type": "overloaded_error"}}, None
        return handler(body, n)

    t = FakeTransport(flaky)
    cache = bench.JsonlCache(tmp_path / "t.jsonl")
    run(bench.translate_phase(items_for_run(1), "claude-haiku-5-5", bench.Api(KEY, t, sleep=sleep),
                              cache, bench.Budget(1.0), no_live, lambda *_: None))
    assert waits == [7.0, 4.0] and len(t.calls) == 3
    assert cache.get("claude-haiku-5-5|s#0#0")["attempts"] == 3


def test_server_errors_that_do_not_clear_stop_without_caching(tmp_path):
    t = FakeTransport(lambda body, n: (500, {"type": "error"}, None))
    cache = bench.JsonlCache(tmp_path / "t.jsonl")
    with pytest.raises(bench.Stop, match="没进缓存"):
        run(bench.translate_phase(items_for_run(1), "claude-haiku-5-5",
                                  bench.Api(KEY, t, sleep=noop_sleep, max_attempts=3), cache,
                                  bench.Budget(1.0), no_live, lambda *_: None))
    assert len(t.calls) == 3 and not cache.rows


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_stops_at_once_and_is_never_printed(tmp_path, capsys, status):
    said = {"type": "error", "error": {"type": "authentication_error",
                                       "message": "invalid x-api-key " + KEY}}
    t = FakeTransport(lambda body, n: (status, said, None))
    with pytest.raises(bench.Stop) as exc:
        run(bench.translate_phase(items_for_run(1), "claude-haiku-5-5",
                                  bench.Api(KEY, t, sleep=noop_sleep),
                                  bench.JsonlCache(tmp_path / "t.jsonl"), bench.Budget(1.0),
                                  no_live, print))
    assert len(t.calls) == 1
    assert KEY not in str(exc.value) and KEY not in capsys.readouterr().out
    assert KEY not in repr(bench.Api(KEY, t))


def test_a_400_stops_with_the_apis_own_words(tmp_path):
    said = {"type": "error", "error": {"type": "invalid_request_error",
                                       "message": "thinking.type: disabled is not supported"}}
    t = FakeTransport(lambda body, n: (400, said, None))
    with pytest.raises(bench.Stop, match="thinking.type: disabled is not supported"):
        run(bench.translate_phase(items_for_run(1), "claude-haiku-5-5",
                                  bench.Api(KEY, t, sleep=noop_sleep),
                                  bench.JsonlCache(tmp_path / "t.jsonl"), bench.Budget(1.0),
                                  no_live, print))


def test_a_different_served_model_stops_the_run(tmp_path):
    t = FakeTransport(lambda body, n: (200, message("claude-haiku-4-5", "x"), None))
    cache = bench.JsonlCache(tmp_path / "t.jsonl")
    with pytest.raises(bench.Stop, match="不是请求的"):
        run(bench.translate_phase(items_for_run(1), "claude-haiku-5-5",
                                  bench.Api(KEY, t, sleep=noop_sleep), cache, bench.Budget(1.0),
                                  no_live, print))
    assert not cache.rows


def test_the_cap_stops_before_sending(tmp_path):
    t = FakeTransport(handler)
    with pytest.raises(bench.Stop, match="没有发"):
        run(bench.translate_phase(items_for_run(3), "claude-haiku-5-5",
                                  bench.Api(KEY, t, sleep=noop_sleep),
                                  bench.JsonlCache(tmp_path / "t.jsonl"), bench.Budget(0.0),
                                  no_live, print))
    assert t.calls == []


def test_a_live_transcription_stops_paid_calls(tmp_path):
    def live(cmd, capture_output=True):
        class R:
            returncode = 0 if "tiktokcdn" in cmd else 1
        return R()

    t = FakeTransport(handler)
    with pytest.raises(bench.Stop) as exc:
        run(bench.translate_phase(items_for_run(2), "claude-haiku-5-5",
                                  bench.Api(KEY, t, sleep=noop_sleep),
                                  bench.JsonlCache(tmp_path / "t.jsonl"), bench.Budget(1.0),
                                  live, print))
    assert exc.value.code == 3 and t.calls == []


def test_production_outcome_counts_what_the_operator_would_see():
    ok = {"stop_reason": "end_turn", "out": "好", "ms": 800}
    assert bench.production_outcome(ok) == ("好", "ok")
    assert bench.production_outcome(dict(ok, ms=10_500)) == (None, "over_timeout")
    assert bench.production_outcome(dict(ok, stop_reason="max_tokens")) == (None, "max_tokens")
    assert bench.production_outcome(dict(ok, out=None)) == (None, "empty")
    assert bench.production_outcome(None) == (None, "missing")


def test_blind_form_keeps_content_and_hides_style():
    assert bench.blind_form("译文：满30美元,包邮!") == "满30美元，包邮！"
    assert bench.blind_form("  Price: 30 USD ") == "Price: 30 USD"
    assert bench.blind_form(None) == bench.NO_TRANSLATION


def test_orders_are_seeded_per_judge_and_swaps_are_the_reverse():
    o = bench.order_for(1, "claude-sonnet-5-5", "main", "x")
    assert o == bench.order_for(1, "claude-sonnet-5-5", "main", "x")
    assert bench.order_for(1, "claude-sonnet-5-5", "swap", "x") == list(reversed(o))
    seen = {tuple(bench.order_for(1, "claude-sonnet-5-5", "main", str(i))) for i in range(40)}
    assert seen == {("local", "claude"), ("claude", "local")}


def test_judging_maps_grades_back_adjudicates_disputes_and_reports(tmp_path):
    items = items_for_run(12)
    t = FakeTransport(handler)
    api = bench.Api(KEY, t, sleep=noop_sleep)
    tcache = bench.JsonlCache(tmp_path / "translations.jsonl")
    jcache = bench.JsonlCache(tmp_path / "judgments.jsonl")
    acache = bench.JsonlCache(tmp_path / "adjudications.jsonl")
    budget = bench.Budget(5.0)
    judges = ["claude-sonnet-5-5", "claude-opus-5-5"]
    run(bench.translate_phase(items, "claude-haiku-5-5", api, tcache, budget, no_live, print))
    run(bench.judge_phase(items, "claude-haiku-5-5", judges, 3, api, tcache, jcache, budget,
                          no_live, print))
    dup, swap = bench.repeat_ids(items, 3)
    assert len(jcache.rows) == 12 * 2 + len(dup) * 2 + len(swap)
    body = next(c["body"] for c in t.calls if c["body"]["model"] == "claude-opus-5-5")
    assert body["output_config"]["effort"] == "high" and "format" in body["output_config"]
    assert "thinking" not in body and not {"temperature", "fallbacks"} & set(body)
    assert "Claude" not in body["messages"][0]["content"]          # 盲评：没有引擎名
    disputed = bench.disputed(items, judges, jcache)
    assert [it["id"] for it in disputed] == ["s#0#7"]              # 只有 Opus 把「疑」判成 major
    run(bench.adjudicate_phase(items, "claude-haiku-5-5", judges, "claude-opus-5-5", 3, api,
                               tcache, jcache, acache, budget, no_live, print))
    assert list(acache.rows) == ["claude-opus-5-5|adjudicate|s#0#7"]
    labels = bench.consensus_labels(items, "claude-haiku-5-5", judges, "claude-opus-5-5",
                                    tcache, jcache, acache)
    assert labels["s#0#7"]["local"]["major"] is True
    assert labels["s#0#7"]["local"]["major_source"] == "adjudicated"
    assert labels["s#0#5"]["claude"]["failed"] and labels["s#0#5"]["claude"]["major"]
    assert labels["s#0#4"]["claude"]["major"] and not labels["s#0#1"]["claude"]["major"]
    pairs = bench.endpoint_pairs(labels, "major")
    expect_local = {i for i in range(12) if i % 3 == 0 or i == 7}
    expect_claude = {i for i in range(12) if i % 4 == 0 or i == 5}
    assert sorted(pairs) == sorted((i in expect_local, i in expect_claude) for i in range(12))
    manifest = {"seed": 3, "sample_sha256": "0" * 64, "request_shape": {"model": "claude-haiku-5-5"}}
    path = bench.report(items, manifest, "claude-haiku-5-5", judges, "claude-opus-5-5",
                        tcache, jcache, acache, [], tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "没测出差异，不等于等价" in text and "只信本轮之内的配对比较" in text
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    res = data["primary"]["major"]
    assert (res["b"], res["c"]) == (len(expect_local - expect_claude),
                                    len(expect_claude - expect_local))
    assert data["failures"]["reasons"] == {"refusal": 1}


def test_glossary_pinning_is_listed_for_a_human_and_only_a_written_override_clears_it(tmp_path):
    """词表钉写会被评委当成捏造：列出来交人工，不用子串自动豁免；人工写了理由才改判。"""
    item = dict(items_for_run(1)[0], id="g1", text="hoy con las gotitas chicas",
                local="今天用维生素滴剂", entries=[[["las gotitas"], "维生素滴剂"]])
    tcache = bench.JsonlCache(tmp_path / "t.jsonl")
    tcache.append({"key": "claude-haiku-5-5|g1", "out": "今天用滴剂", "stop_reason": "end_turn",
                   "ms": 500.0, "cost": 0.0})
    jcache = bench.JsonlCache(tmp_path / "j.jsonl")
    pinned = {"verdict": "major", "error_type": "fabrication", "tier": "good", "friction": False,
              "note": "凭空多出维生素"}
    fine = {"verdict": "correct", "error_type": "none", "tier": "good", "friction": False,
            "note": "x"}
    for judge in ("claude-sonnet-5-5", "claude-opus-5-5"):
        jcache.append({"key": "{}|main|g1".format(judge), "order": ["local", "claude"],
                       "grades": {"A": pinned, "B": fine}, "cost": 0.0})
    acache = bench.JsonlCache(tmp_path / "a.jsonl")
    judges = ["claude-sonnet-5-5", "claude-opus-5-5"]
    labels = bench.consensus_labels([item], "claude-haiku-5-5", judges, None, tcache, jcache, acache)
    review = bench.manual_review([item], labels, "claude-haiku-5-5", tcache)
    assert [(r["id"], r["engine"], r["error_type"]) for r in review] == [("g1", "local", "fabrication")]
    assert labels["g1"]["local"]["major"] is True                  # 没有自动豁免
    override = [{"id": "g1", "engine": "local", "major": False,
                 "reason": "照用中控指定的写法，原文指的就是这款滴剂"}]
    labels = bench.consensus_labels([item], "claude-haiku-5-5", judges, None, tcache, jcache, acache,
                                    override)
    assert labels["g1"]["local"]["major"] is False
    assert labels["g1"]["local"]["major_source"] == "override"
    assert bench.manual_review([item], labels, "claude-haiku-5-5", tcache) == []
    path = bench.report([item], {"seed": 1}, "claude-haiku-5-5", judges, None, tcache, jcache, acache,
                        override, tmp_path)
    assert "照用中控指定的写法" in path.read_text(encoding="utf-8")


def test_overrides_need_a_reason_and_a_known_item(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps([{"id": "a", "engine": "claude", "major": False, "reason": " "}]),
                    encoding="utf-8")
    with pytest.raises(bench.Stop, match="reason"):
        bench.load_overrides(path, {"a"})
    path.write_text(json.dumps([{"id": "a", "engine": "claude", "major": False,
                                 "reason": "照用词表写法"}]), encoding="utf-8")
    assert bench.load_overrides(path, {"a"})[0]["reason"] == "照用词表写法"
    with pytest.raises(bench.Stop):
        bench.load_overrides(path, {"b"})


def test_grading_waits_for_the_holdout_rule(tmp_path):
    items = items_for_run(2)
    holdout = {"dev_eval": {"sessions": set(), "streamers": set()},
               "sealed_test": {"sessions": set(), "streamers": set()}}
    with pytest.raises(bench.Stop) as exc:
        bench.require_dev_eval(items, holdout, tmp_path)
    assert exc.value.code == 4
    assert list(json.loads((tmp_path / "dev_eval-additions.json").read_text(encoding="utf-8"))) \
        == ["session-x.jsonl"]
    holdout["dev_eval"]["sessions"].add("session-x.jsonl")
    bench.require_dev_eval(items, holdout, tmp_path)


def test_a_broken_holdout_file_refuses_to_sample(tmp_path):
    (tmp_path / "h.json").write_text("{", encoding="utf-8")
    with pytest.raises(bench.Stop):
        bench.read_holdout(tmp_path / "h.json")
    (tmp_path / "h.json").write_text(json.dumps({"dev_eval": {}}), encoding="utf-8")
    with pytest.raises(bench.Stop):
        bench.read_holdout(tmp_path / "h.json")


# ---------------------------------------------------------------------------
# 入口：--dry-run、--max-cost、冻结样本
# ---------------------------------------------------------------------------

def argv_for(world, *extra):
    return ["--root", str(world["root"]), "--logs", str(world["logs"]), "--out", str(world["out"]),
            "--n", "40", *extra]


def test_dry_run_sends_nothing_writes_nothing_and_reads_no_key(world, monkeypatch):
    monkeypatch.setattr(bench, "Git", lambda repo: FakeGit())
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)

    def no_key():
        raise AssertionError("dry-run 不该读密钥")

    monkeypatch.setattr(bench, "read_key", no_key)
    t = FakeTransport(handler)
    before = snapshot(world["root"].parent)
    out = []
    code = bench.main(argv_for(world, "--dry-run"), transport=t, sleep=noop_sleep, run=no_live,
                      log=out.append)
    text = "\n".join(map(str, out))
    assert code == 0, text
    assert t.calls == [] and not world["out"].exists()
    assert snapshot(world["root"].parent) == before
    assert "候选池" in text and "费用估算" in text and "没有发出任何请求" in text
    assert '"thinking": {"type": "disabled"}' in text and KEY not in text


def test_a_real_run_needs_a_cap(world, monkeypatch):
    monkeypatch.setattr(bench, "Git", lambda repo: FakeGit())
    out = []
    assert bench.main(argv_for(world), run=no_live, log=out.append) == 2
    assert "--max-cost" in out[-1] and not world["out"].exists()


def test_the_judges_cannot_include_the_model_under_test(world):
    out = []
    assert bench.main(argv_for(world, "--judges", "claude-haiku-5-5", "--dry-run"),
                      log=out.append) == 2
    assert "不能当自己的评委" in out[-1]


def test_a_full_run_freezes_the_sample_first_and_a_rerun_pays_nothing(world, monkeypatch):
    monkeypatch.setattr(bench, "Git", lambda repo: FakeGit())
    holdout = json.loads((world["root"] / "eval_holdout.json").read_text(encoding="utf-8"))
    t = FakeTransport(handler)
    out = []
    # main 读密钥走 read_key：这里换成假的，确认密钥不出现在任何输出里
    monkeypatch.setattr(bench, "read_key", lambda: (KEY, "环境变量"))
    code = bench.main(argv_for(world, "--max-cost", "20"), transport=t, sleep=noop_sleep,
                      run=no_live, log=out.append)
    text = "\n".join(map(str, out))
    assert code == 0, text
    assert KEY not in text
    manifest = json.loads((world["out"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sample_sha256"] == bench.sha256_file(world["out"] / "sample.json")
    assert manifest["request_shape"]["thinking"] == {"type": "disabled"}
    assert (world["out"] / "report.md").exists() and (world["out"] / "manual-review.md").exists()
    assert holdout == json.loads((world["root"] / "eval_holdout.json").read_text(encoding="utf-8"))
    sent = len(t.calls)
    assert sent >= 40 + 2 * 40
    # 重跑：样本、译文、评分都在缓存里，一个请求都不发
    out2 = []
    assert bench.main(argv_for(world, "--max-cost", "20"), transport=t, sleep=noop_sleep,
                      run=no_live, log=out2.append) == 0
    assert len(t.calls) == sent
    # 冻结后换 --n：拒绝
    out3 = []
    assert bench.main(argv_for(world, "--max-cost", "20")[:-2] + ["--n", "30", "--max-cost", "20"],
                      transport=t, run=no_live, log=out3.append) == 2
    assert "换 --out" in out3[-1]


def test_judging_refuses_until_the_sessions_are_in_dev_eval(world, monkeypatch):
    monkeypatch.setattr(bench, "Git", lambda repo: FakeGit())
    monkeypatch.setattr(bench, "read_key", lambda: (KEY, "环境变量"))
    holdout = json.loads((world["root"] / "eval_holdout.json").read_text(encoding="utf-8"))
    holdout["dev_eval"]["sessions"] = {}
    (world["root"] / "eval_holdout.json").write_text(json.dumps(holdout), encoding="utf-8")
    t = FakeTransport(handler)
    out = []
    assert bench.main(argv_for(world, "--max-cost", "20"), transport=t, sleep=noop_sleep,
                      run=no_live, log=out.append) == 4
    assert t.calls == []                         # 花钱之前就查：一个请求都没发
    # 只翻译是允许的（翻译不是评级）
    out = []
    assert bench.main(argv_for(world, "--max-cost", "20", "--phase", "translate"), transport=t,
                      sleep=noop_sleep, run=no_live, log=out.append) == 0
    assert t.calls and all(c["body"]["model"] == "claude-haiku-5-5" for c in t.calls)
    assert (world["out"] / "dev_eval-additions.json").exists()


def test_a_cap_below_the_estimate_refuses_to_start(world, monkeypatch):
    monkeypatch.setattr(bench, "Git", lambda repo: FakeGit())
    monkeypatch.setattr(bench, "read_key", lambda: (KEY, "环境变量"))
    t = FakeTransport(handler)
    out = []
    assert bench.main(argv_for(world, "--max-cost", "0.01"), transport=t, sleep=noop_sleep,
                      run=no_live, log=out.append) == 2
    assert t.calls == [] and "不开工" in out[-1]


def test_the_tool_never_calls_ollama():
    src = (REPO / "tools" / "bench_claude_vs_local.py").read_text(encoding="utf-8")
    assert "11434" not in src and "/api/generate" not in src and "create_translator" not in src
    assert asyncio.iscoroutinefunction(bench.translate_phase)
