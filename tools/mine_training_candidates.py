"""训练候选挖掘：从真实语料里挑「值得人工标注」的难例——不是字符换配对。

批量拿 DeepL 产训练对的路线因服务条款风险被否：训练目标改由人工 / 许可明确
的来源构建，瓶颈从额度变成人力。这个工具保证人力花在刀刃上：1.8B 已经完全
会的（hola chicas、gracias amor 一类）边际价值趋零，值钱的是——

    价格/数字 · 促销条件 · 碎片句 · 行话（词表命中） · 快/强译分歧 ·
    历史盲评 major · 高摩擦行

输出 JSONL（logs/training-candidates-*.jsonl）：
    {src, fast, strong, engine, families, score, session, streamer, seq}
人工确认 target 之后进训练集。无论最终 LoRA、换模型还是不训练，都不浪费。

用法：python3 tools/mine_training_candidates.py [--logs DIR] [--top 0.2]
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")

from app import provenance                                    # noqa: E402
from app.glossary import load as load_glossary                # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

_DIGITS = re.compile(r"\d")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_WORD_NUM = re.compile(
    r"\b(uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|"
    r"quince|veinte|treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|"
    r"noventa|cien|ciento|mil)\b")
_PRON = re.compile(r"\b(me|te|le|les|nos|se|lo|la|los|las)\b")
_DET = re.compile(r"\b(el|la|los|las|un|una|unos|unas|tu|tus|su|sus|mi|mis)\b")


def skeleton(src):
    """近重复判定的骨架：归一化后数字（含拼写数字）/代词/冠词槽位统一。

    "les queda en cincuenta y cinco" 与 "te queda a cincuenta y cinco"
    骨架相同——对模型的信息量几乎一样，人工只标最难的一条。去重必须发生在
    **全量命中池**上（top 截选之后近重复早被打散，实测 846 条精选里骨架
    零重复，去重形同虚设）。"""
    from app.detector import normalize

    s = normalize(src)
    s = _NUM.sub("<NUM>", s)
    s = _WORD_NUM.sub("<NUM>", s)
    s = _PRON.sub("<PRON>", s)
    s = _DET.sub("", s)
    s = re.sub(r"\b(en|a|de|y|que|con)\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    # 拼写复合数字（cincuenta y cinco）会折出多个 <NUM>，与数字写法（55）
    # 的单个 <NUM> 对不上——连续 <NUM> 折叠成一个
    return re.sub(r"(?:<NUM> ?)+", "<NUM> ", s).strip()
_PROMO = re.compile(r"\b(cup[oó]n|descuento|oferta|gratis|orden|pedido|compra|"
                    r"pack|especial|promoci[oó]n|env[ií]o|rebaja|d[oó]lare?s?)\b",
                    re.I)

# 权重定死在这里，改动要有理由——排序漂移会让两次挖掘结果没法比
WEIGHTS = {"disagreement": 4, "price_number": 3, "promo": 3,
           "jargon": 2, "fragment": 2, "hist_major": 5, "hist_friction": 2}


def families_of(src, fast, strong, glossary, hist=None):
    """一条语料命中的难例家族。纯函数，好测。"""
    fams = []
    if _DIGITS.search(src):
        fams.append("price_number")
    if _PROMO.search(src):
        fams.append("promo")
    if len(src) < 20 or src.rstrip().endswith(("...", "…")):
        fams.append("fragment")
    if glossary is not None and glossary.matching(src):
        fams.append("jargon")
    if strong:
        from tools.retranslate_audit import suspicion
        score, _flags = suspicion(src, fast, strong)
        if score >= 0.5:
            fams.append("disagreement")
    if hist:
        if hist.get("major"):
            fams.append("hist_major")
        if hist.get("friction"):
            fams.append("hist_friction")
    return fams


def score_of(fams):
    return sum(WEIGHTS[f] for f in fams)


def _historic_labels(log_dir):
    """已归档盲评的逐 seq 标注（只覆盖被评过的那场 bella 0826）。
    归档和会话日志在同一个目录里——跟着 --logs 走，别锚死在代码目录。"""
    out = {}
    def _load(name):
        p = Path(log_dir) / name
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except OSError:
            return []
    for r in _load("blindeval-20260826-blind_rows.json"):
        if r.get("engine") == "hymt2" and r.get("verdict") == "major":
            out.setdefault(r["seq"], {})["major"] = True
    for r in _load("readability-20260827-rows.json"):
        if r.get("engine") == "hymt2" and r.get("friction"):
            out.setdefault(r["seq"], {})["friction"] = True
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=None)
    ap.add_argument("--top", type=float, default=0.2,
                    help="每个主播取分数最高的比例（默认 0.2）")
    args = ap.parse_args()

    g = load_glossary()          # 只用全局表判行话，不掺 profile
    hist_all = _historic_labels(args.logs or provenance.LOG_DIR)
    holdout = provenance.eval_holdout(strict=True)   # 训练侧：清单读不出直接退出
    seen, rows = set(), []
    skipped_holdout = 0
    for meta in provenance.corpus(log_dir=args.logs):
        session = Path(meta["path"]).name
        # 评估 holdout 硬排除：考卷内容一个字都不进训练侧管线
        if session in holdout["sessions"] or meta["streamer"] in holdout["streamers"]:
            skipped_holdout += 1
            continue
        recs = []
        for line in Path(meta["path"]).read_text(encoding="utf-8").splitlines():
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue
        segs = {r["seq"]: r for r in recs if r.get("type") == "segment"}
        strong = {r["seq"]: (r.get("translated") or "", r.get("model"))
                  for r in recs if r.get("type") == "translation_strong"
                  and r.get("ok")}
        hist = hist_all if "20260826-133138" in session else {}
        # 老日志没有逐条 engine 字段：按延迟指纹做会话级推断（本地模型的
        # 地板 <100ms，网络 API 光往返就不止）——与排查引擎归属时的判据一致。
        # 判不清的保持 None，下游按「来源不明」把参考隔离掉
        ms = sorted(r["translate_ms"] for r in recs
                    if r.get("type") == "translation" and r.get("ok")
                    and r.get("translate_ms", 0) > 5)
        inferred = "hymt2" if ms and ms[0] < 100 else None
        for r in recs:
            if r.get("type") != "translation" or not r.get("ok"):
                continue
            s = segs.get(r["seq"], {})
            src = (s.get("text") or "").strip()
            if s.get("language") != "es" or len(src) < 15 or src in seen:
                continue
            s_text, s_model = strong.get(r["seq"], (None, None))
            fams = families_of(src, r.get("translated") or "",
                               s_text, g, hist.get(r["seq"]))
            if not fams:
                continue
            seen.add(src)
            engine = r.get("engine") or inferred
            rows.append({"src": src, "fast": r.get("translated"),
                         "strong": s_text, "strong_model": s_model,
                         "engine": engine,
                         "engine_inferred": r.get("engine") is None and engine is not None,
                         "families": fams, "score": score_of(fams),
                         "session": session, "streamer": meta["streamer"],
                         "seq": r["seq"]})

    # 骨架聚类在全量池上做：同骨架只有分数最高的一条当代表，但 alternates
    # **保留在输出里**（representative=False）——将来某个 cluster 需要补数字
    # 变化/句式变化时，材料还在，不是删除而是「先只标一个」
    import hashlib
    clusters = {}
    for row in sorted(rows, key=lambda r: -r["score"]):
        clusters.setdefault(skeleton(row["src"]), []).append(row)
    reps = []
    for skel, members in clusters.items():
        cid = hashlib.sha256(skel.encode("utf-8")).hexdigest()[:10]
        for i, m in enumerate(members):
            m["cluster_id"] = cid
            m["cluster_size"] = len(members)
            m["representative"] = i == 0
        reps.append(members[0])
    merged = len(rows) - len(reps)
    alternates = [m for members in clusters.values() for m in members[1:]]
    rows = reps

    # 主播内部按分排序、各取头部比例——多样性不靠运气
    picked = []
    by_streamer = {}
    for row in rows:
        by_streamer.setdefault(row["streamer"], []).append(row)
    for _streamer, group in sorted(by_streamer.items()):
        group.sort(key=lambda r: -r["score"])
        take = max(20, int(len(group) * args.top))
        picked.extend(group[:take])
    picked.sort(key=lambda r: -r["score"])

    from datetime import date
    out_dir = Path(args.logs) if args.logs else provenance.LOG_DIR
    out = out_dir / "training-candidates-{}.jsonl".format(
        date.today().strftime("%Y%m%d"))
    picked_ids = {id(r) for r in picked}
    with out.open("w", encoding="utf-8") as fh:
        for row in picked:
            fh.write(json.dumps(dict(row, selected=True),
                                ensure_ascii=False) + "\n")
        # 落选代表与 alternates 也归档（selected=False）：队列只消费
        # selected 行，但材料一条不丢
        for row in rows:
            if id(row) not in picked_ids:
                fh.write(json.dumps(dict(row, selected=False),
                                    ensure_ascii=False) + "\n")
        for row in alternates:
            fh.write(json.dumps(dict(row, selected=False),
                                ensure_ascii=False) + "\n")

    fam_counts = Counter(f for r in picked for f in r["families"])
    print("候选池 {} 条（命中 {} 条，骨架聚类收拢 {} 条为 alternates，"
          "holdout 排除 {} 场，各主播取前 {:.0%}）→ {}".format(
              len(picked), len(rows), merged, skipped_holdout,
              args.top, out.name))
    print("家族分布:", dict(fam_counts.most_common()))
    print("主播分布:", dict(Counter(r["streamer"] for r in picked)))
    print("有强译对照的:", sum(1 for r in picked if r["strong"]))
    for r in picked[:5]:
        print("  [{}] {} | {}".format("+".join(r["families"]),
                                      r["src"][:60], (r["fast"] or "")[:40]))


if __name__ == "__main__":
    main()
