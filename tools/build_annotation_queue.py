"""把候选池组装成人工标注队列：candidate pool → 语义近重复去重 → 主播软上限
→ 家族配额 → annotation queue。

候选池偏难是刻意的，但训练集不能被单一主播或单一家族主导，人工时间也不能
花在近重复上（"les queda en 55 / te queda a 55" 标一条就够）。队列铁律：

  * **参考译文走 allowlist，不走 blacklist**：标注面只显示许可明确的本地/
    开源引擎（Hy-MT2 / TranslateGemma）的输出；DeepL、来源不明（老日志推断
    不出引擎）的一律 withheld 并注明原因。将来强译换了模型也不会悄悄把
    不该当 teacher 的输出送进标注面——手工改过一遍也洗不干净 provenance。
  * **评估 holdout 硬排除**（eval_holdout.json）：考卷 session / 未见主播
    一个字不进队列。miner 已排除过一次，这里再排一次做双保险。
  * 主播软上限（STREAMER_CAP=0.35，按目标规模算的绝对预算）+ 队列成型后
    再做一次 rebalance，把任何主播的**实际占比**压到 ≤40%——池子填不满就
    填不满，绝不为凑数放宽。第一版 LoRA 要泛化到新主播，不能训成
    单主播 specialist。
  * 家族配额（首批 500）：150 价格/促销 · 100 碎片 · 80 行话 · 70 历史
    major/摩擦 · 50 快强分歧 · 50 普通对照。普通对照必须有——训练集全是
    难例会教出「见什么都过度解释」的模型（最终 SFT 语料还要再混大量普通句，
    那是组训练集时的事）。
  * 每行带上下文（前后句）：给标注者看的。**必须靠上下文才能译对的句子标
    context_required 并排除**——推理时模型只看单句，用上下文写出单句推不出
    的完美译文是在教模型猜。

标注动作枚举（UI 按这个做）：accept_local / accept_strong / manual /
asr_garbage / context_required / profile_only / skip。
profile_only = 内容依赖主播专属品名/SKU：不进 base 训练集，留档给
profile/glossary 资产。target 硬规则见 benchmarks/annotation_guide.md。

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
FINAL_SHARE_MAX = 0.40

# 标注面允许显示的参考来源：本地/开源、许可明确。判据认模型家族标识，
# 不认「不是 deepl 就行」——来源不明视同不许可
_ALLOWED_ENGINES = {"hymt2", "hymt2-7b", "gemma"}
_ALLOWED_MODEL_MARKS = ("hy-mt2", "translategemma")


def reference_view(row):
    """一行候选在标注面上的参考视图：allowlist 之外的一律 withheld 并注明。"""
    fast, engine = row.get("fast"), row.get("engine")
    if engine in _ALLOWED_ENGINES:
        fast_v, fast_w = fast, None
    else:
        fast_v, fast_w = None, (engine or "unknown")
    s_text, s_model = row.get("strong"), row.get("strong_model")
    if s_text and s_model and any(m in s_model.lower()
                                  for m in _ALLOWED_MODEL_MARKS):
        strong_v, strong_w = s_text, None
    else:
        strong_v = None
        strong_w = (s_model or "unknown") if s_text else None
    return {"fast": fast_v, "fast_engine": engine,
            "fast_withheld": fast_w,
            "strong": strong_v, "strong_engine": s_model,
            "strong_withheld": strong_w}


def rebalance(queue, share_max=FINAL_SHARE_MAX):
    """队列成型后的最终配平：任何主播实际占比压到 share_max 以下。
    从该主播分数最低的非对照行开始裁——宁可队列短，不凑数。"""
    queue = list(queue)
    while queue:
        dist = Counter(r["streamer"] for r in queue)
        top, n = dist.most_common(1)[0]
        second = dist.most_common(2)[1][1] if len(dist) > 1 else 0
        # 两个终止条件：达标，或已裁到与第二名持平——主播只有 k 个时
        # 占比下限就是 1/k，硬追 40% 会把队列裁空
        if n / len(queue) <= share_max or n <= second:
            break
        victims = sorted((r for r in queue
                          if r["streamer"] == top and r["bucket"] != "control"),
                         key=lambda r: r.get("score", 0))
        if not victims:
            break
        queue.remove(victims[0])
    return queue


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


def ordinary_controls(log_dir, exclude_srcs, n=CONTROLS, cap=STREAMER_CAP,
                      holdout=None):
    """普通对照句：一个难例家族都不命中的代表性句子，固定种子抽样。

    用**自己的**主播计数（按 cap 比例摊到对照组规模上）：曾经和难例共用
    计数，配额跑完把额度吃光，50 条对照只剩 2 条——没有对照组的训练集会
    教出「见什么都过度解释」的模型。总占比由末端 rebalance 兜底。"""
    taken = Counter()
    limit = max(1, int(n * max(cap, 1.0 / 2)))   # 可训主播可能只剩两个
    import random
    from tools.mine_training_candidates import families_of
    from app.glossary import load as load_glossary

    g = load_glossary()
    rng = random.Random(20260828)
    pool = []
    for meta in provenance.corpus(log_dir=log_dir):
        session = Path(meta["path"]).name
        if holdout and (session in holdout["sessions"]
                        or meta["streamer"] in holdout["streamers"]):
            continue
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
    rows = [r for r in rows if r.get("selected", True)]     # alternates 不进队列
    # holdout 双保险：miner 排过一次，这里再排一次
    holdout = provenance.eval_holdout()
    rows = [r for r in rows
            if r["session"] not in holdout["sessions"]
            and r["streamer"] not in holdout["streamers"]]

    picked, taken, used_srcs, limit = fill_queue(rows, args.batch)
    controls = ordinary_controls(log_dir, used_srcs, holdout=holdout)
    queue = rebalance(picked + controls)

    ctx = load_contexts(log_dir)
    out_rows = []
    for i, r in enumerate(queue, 1):
        prev, nxt = ctx.get((r["session"], r["seq"]), ("", ""))
        out_rows.append({
            "id": i, "src": r["src"], "context_prev": prev,
            "context_next": nxt,
            **reference_view(r),
            "bucket": r["bucket"],
            "cluster_id": r.get("cluster_id"),
            "cluster_size": r.get("cluster_size", 1),
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
    print("  参考被隔离的: fast {} 条 / strong {} 条（allowlist 之外）".format(
        sum(1 for r in out_rows if r["fast_withheld"]),
        sum(1 for r in out_rows if r["strong_withheld"])))


if __name__ == "__main__":
    main()
