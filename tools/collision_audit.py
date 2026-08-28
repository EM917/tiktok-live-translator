"""违禁词模糊层的碰撞审计：离线发现 → 人工确认 → 固化成 policy。

流程刻意是三段而不是一段：**运行时绝不根据词频自动开关模糊匹配**。那会让
检测行为随日志漂移（今天 19 次不关、明天 20 次突然关），无法复现、无法审计。
这个工具只负责第一段——把真实语料里与违禁词编辑距离在预算内的**高频合法
邻居**连同证据摆出来；人确认后写进 banned_fuzzy_policy.txt，生产端只读那份
固定文件。

起因是一个跨多场重复出现的误报：`durar`（持续，带货高频词「能用一到两个月」）
与违禁词 `curar`（治愈）编辑距离 1，5–7 字符统一预算 1 的规则下必然相撞。
这不是阈值不够聪明——距离 1 的候选本身就是另一个高频合法词时，仅凭 ASR
文本无法区分，是信息论上的限制。`curar` 只是第一个被发现的碰撞，所以做的
是审计工具，不是 if-durar 黑名单。

用法：
    python3 tools/collision_audit.py                     # 全表审计
    python3 tools/collision_audit.py --term "nueva cosa" # 新词入表前的风险报告
    python3 tools/collision_audit.py --logs ../logs      # 指定语料目录
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, ".")

from app import provenance                                    # noqa: E402
from app.detector import (                                    # noqa: E402
    BannedTermDetector, _morph_variants, load_terms, normalize,
)

ROOT = Path(__file__).resolve().parent.parent

# 「高置信良性碰撞」的门槛：语料里至少出现这么多次、跨这么多场。
# 只影响报告里的分类标注，不自动生效——生效永远要过人工确认。
MIN_COUNT = 5
MIN_SESSIONS = 2


def corpus_tokens(log_dir=None):
    """真实语料的词频：token -> (出现次数, 场次集合, 主播集合)。

    语料来源走 provenance.corpus()——**不 glob 目录**，测试夹具和非直播
    文件进不来（那次污染的教训）。扫 raw_text：检测器扫的就是它。"""
    count = Counter()
    sessions = defaultdict(set)
    streamers = defaultdict(set)
    for meta in provenance.corpus(log_dir=log_dir):
        try:
            lines = Path(meta["path"]).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "segment":
                continue
            for tok in normalize(row.get("raw_text") or row.get("text") or "").split():
                count[tok] += 1
                sessions[tok].add(meta["path"])
                streamers[tok].add(meta["streamer"])
    return count, sessions, streamers


def neighbours(term_tokens, banned_tokens, det, count, sessions, streamers):
    """一个违禁词条里每个 token 在语料里的预算内邻居，附证据。"""
    out = []
    for tok in term_tokens:
        budget = det._edit_budget(tok)
        if budget == 0:
            continue
        for cand, n in count.items():
            if cand == tok or n == 0:
                continue
            if abs(len(cand) - len(tok)) > budget:
                continue
            if not det._edits_within(tok, cand, budget):
                continue
            morph = cand in _morph_variants(tok) or tok in _morph_variants(cand)
            out.append({
                "token": tok, "neighbour": cand, "dist_budget": budget,
                "count": n, "sessions": len(sessions[cand]),
                "streamers": len(streamers[cand]),
                "is_banned": cand in banned_tokens,
                "morph": morph,
                "benign": (n >= MIN_COUNT and len(sessions[cand]) >= MIN_SESSIONS
                           and not morph and cand not in banned_tokens),
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=None, help="语料目录（默认 logs/）")
    ap.add_argument("--terms", default=None, help="违禁词表（默认 banned_terms.txt）")
    ap.add_argument("--term", default=None, help="只审计这一个候选词（入表前风险报告）")
    args = ap.parse_args()

    terms_path = Path(args.terms) if args.terms else ROOT / "banned_terms.txt"
    if args.term:
        raw_terms = [args.term]
    else:
        raw_terms = [t for t in load_terms(terms_path)
                     if not t.lower().startswith("re:")]
    banned_tokens = {tok for t in raw_terms for tok in normalize(t).split()}
    det = BannedTermDetector([])          # 只借它的预算与编辑距离，policy 不载入
    count, sessions, streamers = corpus_tokens(args.logs)
    print("语料：{} 个不同 token（来源 provenance.corpus，非 glob）\n".format(
        len(count)))

    flagged = 0
    for raw in raw_terms:
        rows = neighbours(normalize(raw).split(), banned_tokens, det,
                          count, sessions, streamers)
        rows = [r for r in rows if r["count"] > 0]
        if not rows:
            continue
        print("term: {}".format(raw))
        anchors = [t for t in normalize(raw).split()
                   if all(r["token"] != t for r in rows)]
        if anchors:
            print("  （短语内无碰撞的 anchor 词：{}）".format(", ".join(anchors)))
        for r in sorted(rows, key=lambda r: -r["count"]):
            tag = []
            if r["benign"]:
                tag.append("⚠ 高置信良性碰撞")
                flagged += 1
            if r["morph"]:
                tag.append("形态变化（variant 级本来就会命中）")
            if r["is_banned"]:
                tag.append("本身也是违禁词")
            print("  {:<14} ↔ {:<14} 预算 {}  {} 次 / {} 场 / {} 主播  {}".format(
                r["token"], r["neighbour"], r["dist_budget"], r["count"],
                r["sessions"], r["streamers"], "、".join(tag)))
        for r in rows:
            if r["benign"]:
                print("  建议（人工确认后写进 banned_fuzzy_policy.txt）：")
                print("    {} => fuzzy 0   # collision: {} (dist<= {}, {} 次 / "
                      "{} 场 / {} 主播)".format(
                          r["token"], r["neighbour"], r["dist_budget"],
                          r["count"], r["sessions"], r["streamers"]))
        print()
    if not flagged:
        print("没有发现高置信良性碰撞（门槛：≥{} 次且 ≥{} 场）。".format(
            MIN_COUNT, MIN_SESSIONS))


if __name__ == "__main__":
    main()
