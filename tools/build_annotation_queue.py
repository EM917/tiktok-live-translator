"""把候选池组装成人工标注队列：candidate pool → 语义近重复去重 → 主播软上限
→ 家族配额 → annotation queue。

候选池偏难是刻意的，但训练集不能被单一主播或单一家族主导，人工时间也不能
花在近重复上（"les queda en 55 / te queda a 55" 标一条就够）。队列铁律：

  * **DeepL 译文不进标注面**：engine=deepl 的行只保留 src + 强译参考，
    fast 被替换为占位说明。DeepL 输出留在评估档案里没问题，但训练标注的
    起点不能是它——手工改过一遍也洗不干净 provenance。
  * 主播软上限（默认 38%）：超出的挤到候补池，第一版 LoRA 要泛化到新主播，
    不能训成 Daisy specialist。
  * 家族配额（首批 500）：150 价格/促销 · 100 碎片 · 80 行话 · 70 历史
    major/摩擦 · 50 快强分歧 · 50 普通对照。普通对照必须有——训练集全是
    难例会教出「见什么都过度解释」的模型（最终 SFT 语料还要再混大量普通句，
    那是组训练集时的事）。
  * 每行带上下文（前后句）：给标注者看的。**必须靠上下文才能译对的句子标
    context_required 并排除**——推理时模型只看单句，用上下文写出单句推不出
    的完美译文是在教模型猜。

标注动作枚举（UI 按这个做）：accept_fast / accept_strong / manual /
asr_garbage / context_required / skip。

用法：python3 tools/build_annotation_queue.py [--logs DIR] [--batch 500]
"""
import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, ".")

from app import provenance                                    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

STREAMER_CAP = 0.35
# 首批配额：bucket -> 目标条数。分不满的桶把余量让给 price_promo / fragment
QUOTAS = [("historic", 70), ("disagreement", 50), ("fragment", 100),
          ("jargon", 80), ("price_promo", 150)]
CONTROLS = 50


def bucket_of(families):
    """一行归一个桶。优先级按「这条最该被谁认领」：历史难例与分歧最稀缺；
    价格/促销排在行话**前面**——命中价格的行话句，值钱的是价格关系，
    行话桶只收「纯行话」（曾经排反，jargon 抢走 221 条把 price_promo
    饿到 72 条）。"""
    fams = set(families)
    if fams & {"hist_major", "hist_friction"}:
        return "historic"
    if "disagreement" in fams:
        return "disagreement"
    if "fragment" in fams:
        return "fragment"
    if fams & {"price_number", "promo"}:
        return "price_promo"
    if "jargon" in fams:
        return "jargon"
    return None


def fill_queue(rows, batch, quotas=QUOTAS, cap=STREAMER_CAP):
    """桶配额与主播软上限**联合**选取。

    上限必须贯穿选取全程：先截池再配额会让配额重新洗出一个超限的组合
    （实测 bella 在最终队列占到 68%）。cap 相对目标队列规模（含对照组），
    额度用尽的主播直接跳过，让位给下一名。
    返回 (picked, taken, used_srcs)——对照组接着同一份计数继续选。"""
    limit = max(1, int(batch * cap))
    taken, picked, used_srcs = Counter(), [], set()
    by_bucket = {}
    for r in rows:
        b = bucket_of(r["families"])
        if b:
            by_bucket.setdefault(b, []).append(r)
    for group in by_bucket.values():
        group.sort(key=lambda r: -r["score"])

    def take(name, want):
        got = 0
        for r in by_bucket.get(name, []):
            if got >= want:
                break
            if r["src"] in used_srcs or taken[r["streamer"]] >= limit:
                continue
            picked.append(dict(r, bucket=name))
            used_srcs.add(r["src"])
            taken[r["streamer"]] += 1
            got += 1
        return got

    leftover = 0
    for name, want in quotas:
        leftover += want - take(name, want)
    # 余量优先补 price_promo，再补 fragment（语料里最富余的两桶）
    for name in ("price_promo", "fragment"):
        if leftover <= 0:
            break
        leftover -= take(name, leftover)
    return picked, taken, used_srcs, limit


def load_contexts(log_dir):
    """(session, seq) -> (上一句, 下一句)。标注者看，不进训练输入。"""
    ctx = {}
    for meta in provenance.corpus(log_dir=log_dir):
        session = Path(meta["path"]).name
        segs = []
        for line in Path(meta["path"]).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") == "segment" and (r.get("text") or "").strip():
                segs.append((r["seq"], r["text"].strip()))
        for i, (seq, _text) in enumerate(segs):
            ctx[(session, seq)] = (segs[i - 1][1] if i else "",
                                   segs[i + 1][1] if i + 1 < len(segs) else "")
    return ctx


def ordinary_controls(log_dir, exclude_srcs, taken, limit, n=CONTROLS):
    """普通对照句：一个难例家族都不命中的代表性句子，固定种子抽样。
    延续同一份主播计数——对照组绕开上限的话，总占比又会被顶回去。"""
    import random
    from tools.mine_training_candidates import families_of
    from app.glossary import load as load_glossary

    g = load_glossary()
    rng = random.Random(20260828)
    pool = []
    for meta in provenance.corpus(log_dir=log_dir):
        session = Path(meta["path"]).name
        recs = [json.loads(line) for line in
                Path(meta["path"]).read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("{")]
        segs = {r["seq"]: r for r in recs if r.get("type") == "segment"}
        for r in recs:
            if r.get("type") != "translation" or not r.get("ok"):
                continue
            s = segs.get(r["seq"], {})
            src = (s.get("text") or "").strip()
            if (s.get("language") != "es" or not 20 <= len(src) <= 90
                    or src in exclude_srcs):
                continue
            if families_of(src, r.get("translated") or "", None, g):
                continue
            pool.append({"src": src, "fast": r.get("translated"),
                         "strong": None, "engine": r.get("engine"),
                         "families": [], "score": 0, "session": session,
                         "streamer": meta["streamer"], "seq": r["seq"]})
    rng.shuffle(pool)
    seen, out = set(), []
    for r in pool:
        if r["src"] in seen or taken[r["streamer"]] >= limit:
            continue
        seen.add(r["src"])
        taken[r["streamer"]] += 1
        out.append(dict(r, bucket="control"))
        if len(out) >= n:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=None)
    ap.add_argument("--candidates", default=None,
                    help="miner 输出（默认取 logs 目录里最新的一份）")
    ap.add_argument("--batch", type=int, default=500)
    args = ap.parse_args()
    log_dir = Path(args.logs) if args.logs else provenance.LOG_DIR

    if args.candidates:
        cand_path = Path(args.candidates)
    else:
        cands = sorted(log_dir.glob("training-candidates-*.jsonl"))
        if not cands:
            print("没有候选池——先跑 tools/mine_training_candidates.py")
            return
        cand_path = cands[-1]
    rows = [json.loads(line) for line in
            cand_path.read_text(encoding="utf-8").splitlines()]

    picked, taken, used_srcs, limit = fill_queue(rows, args.batch)
    controls = ordinary_controls(log_dir, used_srcs, taken, limit)
    queue = picked + controls

    ctx = load_contexts(log_dir)
    out_rows = []
    for i, r in enumerate(queue, 1):
        prev, nxt = ctx.get((r["session"], r["seq"]), ("", ""))
        fast, engine = r.get("fast"), r.get("engine")
        if engine == "deepl":
            # 训练标注面隔离 DeepL：起点只能是 src + 本地/开源参考
            fast = None
        out_rows.append({
            "id": i, "src": r["src"], "context_prev": prev,
            "context_next": nxt,
            "fast": fast,
            "fast_withheld": "deepl" if engine == "deepl" else None,
            "strong": r.get("strong"), "bucket": r["bucket"],
            "families": r["families"], "session": r["session"],
            "streamer": r["streamer"], "seq": r["seq"],
            "label": None, "target": None,
        })

    out = log_dir / "annotation-queue-{}.jsonl".format(
        date.today().strftime("%Y%m%d"))
    with out.open("w", encoding="utf-8") as fh:
        for row in out_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("标注队列 {} 条 → {}".format(len(out_rows), out.name))
    print("  候选池 {} 条（骨架去重已在 miner 完成），主播上限 {} 条/人".format(
        len(rows), limit))
    print("  桶分布:", dict(Counter(r["bucket"] for r in out_rows)))
    dist = Counter(r["streamer"] for r in out_rows)
    top, top_n = dist.most_common(1)[0]
    print("  主播分布: {}（最高 {} 占 {:.0%}——软上限按目标规模 {} 算，"
          "池子填不满时实际占比会略高）".format(
              dict(dist), top, top_n / len(out_rows), args.batch))
    print("  DeepL fast 被隔离的:", sum(1 for r in out_rows if r["fast_withheld"]))


if __name__ == "__main__":
    main()
