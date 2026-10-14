#!/usr/bin/env python3
"""弹幕留意的模型复核评估（PR-5，spec §5.13）：虚构弹幕集 × 多遍，打真的 Claude 接口，只测格式和稳定性。

**不主张准确率**：弹幕集是虚构的、标签是作者自己写的（`intended`），只拿来看这几件事：
  * 格式合格率：回应能解析、k 对得上、字段合要求的比例；
  * 同一输入跑几遍，标签是否一致；
  * 每次调用的 P50 / P95 延迟；
  * 拒答（stop_reason == refusal）率——辱骂和涉性原文可能触发 general_harms，超过 5% 要改提示词的框定方式再评一轮；
  * 总花费。
准确率要靠真实弹幕标注（PR-7 留存），那时用 McNemar 配对检验（CLAUDE.md 第四条）。

**安全闸**
  * **直播转写进行中拒绝运行**（CLAUDE.md 第三条）：开头先查 `pgrep -f`，命中就退出，**此时一个 HTTP 请求都不发、
    连密钥都不读**。Claude 不占显存，但第三条字面上算「模型批量生成」。
  * **花费硬上限**（默认 $0.10）：每次调用之前先按最坏情况（输入按估计上界、输出按 max_tokens 全用满）算一遍，
    已花的加上这次的最坏情况超过上限就不发了；每次回来按回应里的 usage 记真实花费（Haiku 5.5：输入 $0.10 /
    输出 $0.50 每百万 token；缓存写按 1.25 倍、缓存读按 0.1 倍），没有 usage 的按最坏情况记。
  * **密钥**从环境变量 ANTHROPIC_API_KEY 或 settings.json 的 api_keys 里读，**不打印、不写日志、不写报告**
    （连尾四位都不写）。请求只发给 api.anthropic.com，不读 ANTHROPIC_BASE_URL。
  * 接口出错（密钥被拒、模型找不到、限流、连不上）：记下来、停下来，**不循环重试**。
  * 开跑前打印预估费用并要人确认（`--yes` 跳过确认）。

用法（在 worktree 里跑时，仓库自己的 settings.json 可能没有密钥；`--settings` 指向有密钥的那份）：
    nice -n 19 python3 tools/guard_review_eval.py claude tests/fixtures/comment_guard/review_cases.jsonl out.json --yes
    python3 tools/guard_review_eval.py claude cases.jsonl out.json --runs 3 --cap 0.10 --settings ~/tiktok-live-translator/settings.json
    python3 tools/guard_review_eval.py claude cases.jsonl out.json --no-hints --yes     # 不带规则提示的那一臂
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import comment_review as cr  # noqa: E402
from app.stdio import harden_stdio  # noqa: E402

harden_stdio()      # 重定向到文件时（Windows 的 GBK 代码页）中文输出不崩

# 直播转写进行中的进程特征（CLAUDE.md 第三条）；拆开写，免得本文件自己被这条 pgrep 扫到
LIVE_PATTERNS = ("tiktok" + "cdn", "main" + ".py --browser")
DEFAULT_CASES = ROOT / "tests" / "fixtures" / "comment_guard" / "review_cases.jsonl"

# Claude Haiku 5.5 的价格（美元 / 百万 token；提示不超过 100K token 时），claude-api 技能缓存于 2026-10-06
PRICE_IN, PRICE_OUT = 0.10, 0.50
CACHE_WRITE_FACTOR, CACHE_READ_FACTOR = 1.25, 0.10
# 估算输入 token 数时每个 token 按多少个字符算：取得很保守（真实西语 + 英文约 3~4 字符一个 token）
CHARS_PER_TOKEN_FLOOR = 2.0


def live_running():
    """直播转写还在跑：返回命中的进程特征，没有返回 None。没有 pgrep（Windows）时查不了：行为不变
    （当没有），但往 stderr 打一行警告，让人知道这道闸这次没起作用。"""
    for pat in LIVE_PATTERNS:
        try:
            hit = subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0
        except OSError:
            print("[警告] 查不了直播是否在进行（pgrep 不可用）：请自己确认没有直播转写在跑再继续", file=sys.stderr)
            return None
        if hit:
            return pat
    return None


def load_cases(path):
    cases = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row.get("id"), str) or not isinstance(row.get("text"), str):
            raise ValueError("{} 第 {} 行缺 id 或 text".format(path, number))
        cases.append(row)
    return cases


def find_key(settings_paths):
    """(密钥, 来源) —— 来源只写 "env" 或 "settings"，不写密钥的任何一段。没有返回 (None, None)。"""
    val = (os.environ.get(cr.KEY_NAME) or "").strip()
    if val:
        return val, "env"
    for path in settings_paths:
        try:
            data = json.loads(Path(path).expanduser().read_bytes())
        except (OSError, ValueError):
            continue
        keys = data.get("api_keys") if isinstance(data, dict) else None
        val = str((keys or {}).get(cr.KEY_NAME, "")).strip() if isinstance(keys, dict) else ""
        if val:
            return val, "settings"
    return None, None


def default_settings_paths():
    return [ROOT / "settings.json", Path.home() / "tiktok-live-translator" / "settings.json"]


def build_entries(cases, hints=True):
    """要问模型的条目（和生产里 GuardRuntime._take_batch 造的同一个形状）；打码之后没什么可判的跳过。"""
    entries, skipped = [], []
    for row in cases:
        masked = cr.mask(row["text"])
        if not cr.reviewable(masked):
            skipped.append(row["id"])
            continue
        prev = cr.mask(row["prev"]) if row.get("prev") else None
        entries.append({
            "cid": row["id"], "masked": masked, "prev_masked": prev if prev is not None and prev.text else None,
            "hint": (row.get("hint") or "") if hints else "", "flags": list(row.get("flags") or ()) if hints else [],
            "intended": row.get("intended")})
    return entries, skipped


def cost_usd(result):
    """一次调用的真实花费（按回应里的 usage）。没有 usage 返回 None，调用方按最坏情况记。"""
    if result.in_tokens is None or result.out_tokens is None:
        return None
    read = result.cache_read_tokens or 0
    write = result.cache_write_tokens or 0
    plain = max(0, result.in_tokens - read - write)
    return (plain * PRICE_IN + write * PRICE_IN * CACHE_WRITE_FACTOR + read * PRICE_IN * CACHE_READ_FACTOR
            + result.out_tokens * PRICE_OUT) / 1e6


def worst_case_usd(reviewer, batch, observed_in=0):
    """这一批最坏的花费：输入按估计上界（或见过的最大输入 token 数的 1.3 倍，取大的），输出把 max_tokens 用满，
    缓存全按写入价。"""
    items = [{"k": str(i + 1), "text": e["masked"].text, "hint": e.get("hint"), "flags": e.get("flags"),
              "prev": e["prev_masked"].text if e.get("prev_masked") else ""} for i, e in enumerate(batch)]
    body = reviewer.build_body(cr.build_user_message(items))
    chars = len(json.dumps(body, ensure_ascii=False))
    in_tokens = max(chars / CHARS_PER_TOKEN_FLOOR, observed_in * 1.3)
    return (in_tokens * PRICE_IN * CACHE_WRITE_FACTOR + body["max_tokens"] * PRICE_OUT) / 1e6


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo), 1)


HARD_ERRORS = ("http_4xx", "http_5xx", "timeout")


async def run_eval(reviewer, entries, runs=3, batch=8, cap=0.10, max_calls=120, clock=time.monotonic):
    """跑 runs 遍，每遍把 entries 按同样的次序切成 batch 条一批。返回报告 dict（不含密钥、不含弹幕原文）。"""
    chunks = [entries[i:i + batch] for i in range(0, len(entries), batch)]
    spent, calls, observed_in = 0.0, [], 0
    verdicts = {}                      # run -> {cid: Verdict}
    stopped = None
    for run in range(1, runs + 1):
        verdicts[run] = {}
        for chunk in chunks:
            if len(calls) >= max_calls:
                stopped = "max_calls"
                break
            worst = worst_case_usd(reviewer, chunk, observed_in)
            if spent + worst > cap:
                stopped = "cost_cap"
                break
            t0 = clock()
            result = await reviewer.review(chunk)
            wall_ms = int((clock() - t0) * 1000)
            real = cost_usd(result)
            charge = real if real is not None else worst
            spent += charge
            observed_in = max(observed_in, result.in_tokens or 0)
            calls.append({"run": run, "n": len(chunk), "valid": result.valid, "outcome": result.outcome,
                          "ms": result.ms or wall_ms, "in_tokens": result.in_tokens, "out_tokens": result.out_tokens,
                          "cache_read_tokens": result.cache_read_tokens,
                          "cache_write_tokens": result.cache_write_tokens, "cost_usd": round(charge, 6),
                          "cost_estimated": real is None, "refusal_category": result.refusal_category,
                          "http": result.http, "evidence_cleared": result.evidence_cleared})
            verdicts[run].update(result)
            if result.outcome in HARD_ERRORS:
                stopped = "api_error:" + result.outcome
                break
            if spent > cap:
                stopped = "cost_cap"
                break
        if stopped:
            break
    return summarize(entries, verdicts, calls, spent, cap, runs, stopped, batch)


def summarize(entries, verdicts, calls, spent, cap, runs_requested, stopped, batch):
    asked = sum(c["n"] for c in calls)
    valid = sum(c["valid"] for c in calls)
    completed = [r for r in verdicts if verdicts[r] or any(c["run"] == r for c in calls)]
    full_runs = [r for r in completed if sum(c["n"] for c in calls if c["run"] == r) == len(entries)]
    ok_calls = [c for c in calls if c["outcome"] == "ok"]
    refusals = [c for c in calls if c["outcome"] == "refusal"]
    by_id = {e["cid"]: e for e in entries}

    # ---- 同一输入跑几遍的一致率：只比每一遍都有判定的条目
    comparable = [cid for cid in by_id if full_runs and all(cid in verdicts[r] for r in full_runs)]
    same_label = same_attack = same_pair = 0
    for cid in comparable:
        labels = [verdicts[r][cid].label for r in full_runs]
        attacks = [verdicts[r][cid].attack for r in full_runs]
        pairs = [(verdicts[r][cid].label, verdicts[r][cid].sure) for r in full_runs]
        same_label += len(set(labels)) == 1
        same_attack += len(set(attacks)) == 1
        same_pair += len(set(pairs)) == 1
    lat = [c["ms"] for c in calls if c["outcome"] in ("ok", "invalid", "refusal", "max_tokens")]

    def rate(a, b):
        return round(a / float(b), 4) if b else None

    # 作者自己写的标签对一眼：只看第一遍。虚构集，不是准确率
    first = verdicts.get(1) or {}
    sanity = Counter()
    for cid, v in first.items():
        sanity["agree" if by_id[cid].get("intended") == v.label else "differ"] += 1
    return {
        "tool": "guard_review_eval", "finished_at": datetime.now().isoformat(timespec="seconds"),
        "model": cr.guard_model(), "prompt_version": cr.PROMPT_VERSION, "prompt_hash": cr.load_prompt()[1],
        "cases": len(entries), "runs_requested": runs_requested, "runs_completed": len(full_runs), "batch": batch,
        "calls": len(calls), "stopped_early": stopped,
        "cost": {"usd": round(spent, 6), "cap_usd": cap, "per_call_usd": round(spent / len(calls), 6) if calls else None,
                 "within_cap": spent <= cap, "estimated_calls": sum(1 for c in calls if c["cost_estimated"]),
                 "in_tokens": sum(c["in_tokens"] or 0 for c in calls), "out_tokens": sum(c["out_tokens"] or 0 for c in calls),
                 "cache_read_tokens": sum(c["cache_read_tokens"] or 0 for c in calls),
                 "prices_usd_per_mtok": {"in": PRICE_IN, "out": PRICE_OUT}},
        "format": {"calls_ok": len(ok_calls), "calls_total": len(calls), "calls_ok_rate": rate(len(ok_calls), len(calls)),
                   "verdicts_valid": valid, "verdicts_asked": asked, "valid_rate": rate(valid, asked),
                   "outcomes": dict(Counter(c["outcome"] for c in calls)),
                   "evidence_cleared": sum(c["evidence_cleared"] for c in calls)},
        "agreement": {"cases_compared": len(comparable), "runs_compared": len(full_runs),
                      "label_identical": same_label, "label_rate": rate(same_label, len(comparable)),
                      "attack_or_not_identical": same_attack, "attack_or_not_rate": rate(same_attack, len(comparable)),
                      "label_and_sure_identical": same_pair, "label_and_sure_rate": rate(same_pair, len(comparable))},
        "latency_ms": {"n": len(lat), "p50": percentile(lat, 0.5), "p95": percentile(lat, 0.95),
                       "max": max(lat) if lat else None},
        "refusal": {"calls": len(refusals), "rate": rate(len(refusals), len(calls)),
                    "cases": sum(c["n"] for c in refusals),
                    "categories": dict(Counter(str(c["refusal_category"]) for c in refusals))},
        "labels_run1": dict(Counter(v.label for v in first.values())),
        "sanity_vs_author_labels_run1": {
            "note": "虚构集、标签是作者自己写的，只用来一眼看出有没有离谱的偏差，不是准确率",
            "agree": sanity["agree"], "differ": sanity["differ"]},
        "per_call": calls,
    }


def format_report(rep):
    """给人看的摘要（markdown）。"""
    f, a, lat, ref, cost = rep["format"], rep["agreement"], rep["latency_ms"], rep["refusal"], rep["cost"]

    def pct(x):
        return "n/a" if x is None else "{:.1f}%".format(x * 100)

    lines = [
        "# 弹幕留意模型复核评估（虚构集，只测格式和稳定性）", "",
        "- 模型 {}，提示词 {}（{}）".format(rep["model"], rep["prompt_version"], rep["prompt_hash"]),
        "- {} 条 × {} 遍（要求 {} 遍），每批 {} 条，共 {} 次调用；提前停止：{}".format(
            rep["cases"], rep["runs_completed"], rep["runs_requested"], rep["batch"], rep["calls"],
            rep["stopped_early"] or "否"),
        "- 花费 ${:.4f}（上限 ${:.2f}，{}）；输入 {} token（缓存读 {}）、输出 {} token".format(
            cost["usd"], cost["cap_usd"], "未超" if cost["within_cap"] else "超了", cost["in_tokens"],
            cost["cache_read_tokens"], cost["out_tokens"]),
        "- 格式合格：{} / {} 条判定（{}）；调用 ok {} / {}；各结局 {}".format(
            f["verdicts_valid"], f["verdicts_asked"], pct(f["valid_rate"]), f["calls_ok"], f["calls_total"], f["outcomes"]),
        "- 同一输入跑 {} 遍：标签一致 {} / {}（{}），攻击与否一致 {}，标签与 sure 都一致 {}".format(
            a["runs_compared"], a["label_identical"], a["cases_compared"], pct(a["label_rate"]),
            pct(a["attack_or_not_rate"]), pct(a["label_and_sure_rate"])),
        "- 延迟（每次调用）：P50 {} ms，P95 {} ms，最大 {} ms（{} 次）".format(lat["p50"], lat["p95"], lat["max"], lat["n"]),
        "- 拒答：{} 次调用（{}），涉及 {} 条；类别 {}".format(ref["calls"], pct(ref["rate"]), ref["cases"], ref["categories"]),
        "- 第一遍的标签分布：{}".format(rep["labels_run1"]),
        "- 与作者自己写的标签一眼对比（不是准确率）：一致 {}，不一致 {}".format(
            rep["sanity_vs_author_labels_run1"]["agree"], rep["sanity_vs_author_labels_run1"]["differ"]),
    ]
    return "\n".join(lines) + "\n"


def estimate_usd(reviewer, entries, runs, batch):
    chunks = [entries[i:i + batch] for i in range(0, len(entries), batch)]
    return sum(worst_case_usd(reviewer, c) for c in chunks) * runs


def main(argv=None):
    ap = argparse.ArgumentParser(description="弹幕留意模型复核评估（只测格式和稳定性，不主张准确率）")
    ap.add_argument("backend", choices=["claude"], help="一个进程只跑一个后端（本地后端是 PR-6）")
    ap.add_argument("cases", nargs="?", default=str(DEFAULT_CASES), help="弹幕集 jsonl（默认虚构集）")
    ap.add_argument("out", nargs="?", default=None, help="报告 json 的路径；同名 .md 写一份摘要")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--batch", type=int, default=cr.ReviewPolicy.BATCH_MAX)
    ap.add_argument("--cap", type=float, default=0.10, help="花费硬上限（美元）")
    ap.add_argument("--max-calls", type=int, default=120)
    ap.add_argument("--settings", action="append", default=None, help="读密钥的 settings.json（可重复）")
    ap.add_argument("--no-hints", action="store_true", help="不带规则的 hint / flags（对照臂）")
    ap.add_argument("--yes", action="store_true", help="不再要人确认预估费用")
    args = ap.parse_args(argv)

    # 第一道：直播转写进行中就退出。此时不读密钥、不建任何网络对象
    live = live_running()
    if live:
        print("直播转写仍在进行（命中进程特征 {}），拒绝运行评估。等停播再跑（CLAUDE.md 第三条）。".format(live),
              file=sys.stderr)
        return 2

    key, source = find_key([Path(p) for p in (args.settings or default_settings_paths())])
    if not key:
        print("没有 Claude 密钥（环境变量 ANTHROPIC_API_KEY 和 settings.json 的 api_keys 里都没有），不运行。", file=sys.stderr)
        return 3
    entries, skipped = build_entries(load_cases(args.cases), hints=not args.no_hints)
    reviewer = cr.ClaudeReviewer(key)
    if reviewer.prompt is None:
        print("读不到提示词 {}，不运行。".format(cr.PROMPT_FILE), file=sys.stderr)
        return 3
    est = estimate_usd(reviewer, entries, args.runs, args.batch)
    print("密钥：已找到（来自 {}；不显示）。模型 {}，{} 条（打码后无可判的跳过 {} 条）× {} 遍，每批 {} 条。".format(
        source, reviewer.model, len(entries), len(skipped), args.runs, args.batch))
    print("预估花费上限（按最坏情况）：${:.4f}；硬上限 ${:.2f}。".format(est, args.cap))
    if not args.yes:
        if input("继续？输入 y 确认：").strip().lower() != "y":
            print("已取消。")
            return 1

    async def go():
        try:
            return await run_eval(reviewer, entries, runs=args.runs, batch=args.batch, cap=args.cap,
                                  max_calls=args.max_calls)
        finally:
            await reviewer.close()

    report = asyncio.run(go())
    report["skipped_unreviewable"] = skipped
    report["hints"] = not args.no_hints
    text = format_report(report)
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out.with_suffix(".md").write_text(text, encoding="utf-8")
        print("报告已写入 {}".format(out))
    return 0 if not report["stopped_early"] or report["stopped_early"] in ("cost_cap", "max_calls") else 4


if __name__ == "__main__":
    sys.exit(main())
