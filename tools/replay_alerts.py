"""报警回放：collision policy 生效前后，对全部真实语料重跑检测器并对比。

merge gate 的执行器（不是可选项）：
  * 已知误报（durar→curar 一类）必须清零；
  * exact / variant 级的真命中一条都不能掉；
  * 不允许出现新增命中（policy 只会收紧模糊层，出现新增说明实现有 bug）。

用法：
    python3 tools/replay_alerts.py [--logs DIR] [--terms FILE]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app import provenance                                    # noqa: E402
from app.detector import (                                    # noqa: E402
    BannedTermDetector, load_fuzzy_policy, load_terms,
)

ROOT = Path(__file__).resolve().parent.parent


def session_hits(path, detector):
    detector.reset_state()
    hits = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return hits
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") != "segment":
            continue
        text = row.get("raw_text") or row.get("text") or ""
        ts = row.get("audio_end_ts") or 0
        for h in detector.scan(text, ts=ts):
            hits.append((row.get("seq"), h["term"], h["tier"]))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=None)
    ap.add_argument("--terms", default=None)
    args = ap.parse_args()
    terms_path = Path(args.terms) if args.terms else ROOT / "banned_terms.txt"
    terms = load_terms(terms_path)
    policy = load_fuzzy_policy(ROOT / "banned_fuzzy_policy.txt")
    print("policy: {}\n".format(policy or "（空）"))

    removed_total, removed_bad, added_total = 0, 0, 0
    for meta in provenance.corpus(log_dir=args.logs):
        # 两边都关掉命中冷却：冷却是跨命中的时间耦合——旧检测器的一次误报
        # 会占用冷却窗口、吞掉几秒后的真命中；收紧后真命中浮出来，会被
        # 差集误判成「新增」。gate 比的是匹配行为本身，不是报警节流
        old = set(session_hits(meta["path"],
                               BannedTermDetector(terms, cooldown_sec=0)))
        new = set(session_hits(meta["path"],
                               BannedTermDetector(terms, cooldown_sec=0,
                                                  fuzzy_policy=policy)))
        removed = old - new
        added = new - old
        if not removed and not added:
            continue
        print(Path(meta["path"]).name, "({})".format(meta["streamer"]))
        for seq, term, tier in sorted(removed):
            mark = "" if tier == "fuzzy" else "   ← ❌ 非模糊级命中被移除！"
            if tier != "fuzzy":
                removed_bad += 1
            removed_total += 1
            print("  - seq {:<5} {} [{}]{}".format(seq, term, tier, mark))
        for seq, term, tier in sorted(added):
            added_total += 1
            print("  + seq {:<5} {} [{}]   ← ❌ 收紧不该带来新增".format(
                seq, term, tier))
        print()
    print("移除 {} 条（其中非模糊级 {} 条），新增 {} 条".format(
        removed_total, removed_bad, added_total))
    if removed_bad or added_total:
        print("❌ GATE FAILED")
        sys.exit(1)
    print("✅ gate 通过：仅移除模糊级命中，exact/variant 全保留，无新增")


if __name__ == "__main__":
    main()
