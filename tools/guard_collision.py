"""弹幕留意词表的碰撞计数：每条词条（含写在 `[段名:pending]` 里还没确认的红条档）在主播口播里
撞了多少个不同的句子、跨几场。

为什么要这个工具：判定词表是业务判断，agent 只产出候选。候选词条进表前必须知道它在带货西语里
本身有多常见——首稿词表在 85,394 个不同口播句子上有 345 句 alert，删掉撞车词、改成句式后降到
48 句（`tapadera` 119 次是瓶盖量勺，`basura` 91 次是 tirar a la basura）。这是口播不是弹幕，
回答的是「这个词在这个行业里多常见」，不是弹幕的误报率。

纯 CPU、只读：读 logs/ 里的 `session-*.jsonl`（走 provenance.corpus，不 glob 目录，夹具进不来），
不碰模型、不联网、不写词表。**直播转写进行中拒绝运行**（CLAUDE.md 第三条）：开头先查
`pgrep -f`，命中就退出。跑的时候用 `nice -n 19`。

用法（agent 在 worktree 里跑时，仓库自己的 logs/ 是空的，语料在 ~/tiktok-live-translator/logs；
`--logs` 不写时，仓库的 logs/ 不存在就自动退到那里）：
    nice -n 19 python3 tools/guard_collision.py --logs ~/tiktok-live-translator/logs
    python3 tools/guard_collision.py --logs ~/tiktok-live-translator/logs --section insult/threat
    python3 tools/guard_collision.py --logs ~/tiktok-live-translator/logs \\
        --term "te voy a matar" --as insult/threat      # 入表前的风险报告，状态写「候选（未入表）」
    python3 tools/guard_collision.py --json out.json        # 机器可读，不进仓库
语料为空（0 场或 0 段）时报错退出，不会打一份看着很干净的空报告。

红条档的约定（comment_guard/README.md）：没确认的词条写在 `[段名:pending]` 里（命中只出琥珀），
确认后挪到不带 `:pending` 的同名段。本工具两种都算，报告里分别标「已打开」和「待确认」。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, ".")

from app.stdio import harden_stdio                     # noqa: E402

harden_stdio()

from app import comment_guard as cg                    # noqa: E402
from app import provenance                             # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "comment_guard" / "defaults"
# 直播转写进行中的进程特征（CLAUDE.md 第三条）；拆开写，免得本文件自己被这条 pgrep 扫到
LIVE_PATTERNS = ("tiktok" + "cdn", "main" + ".py --browser")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?¡¿])\s+")
MIN_SENTENCE = 4


def live_running():
    """直播转写还在跑：返回命中的进程特征，没有返回 None。没有 pgrep（Windows）时查不了：行为不变
    （当没有），但往 stderr 打一行警告，让人知道这道闸这次没起作用。"""
    for pat in LIVE_PATTERNS:
        try:
            hit = subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0
        except OSError:
            print("[警告] 查不了直播是否在进行（pgrep 不可用）：请自己确认没有直播转写在跑再继续",
                  file=sys.stderr)
            return None
        if hit:
            return pat
    return None


def default_logs():
    """没传 --logs 时的语料目录：仓库自己的 logs/ 在就用它（返回 None 让 provenance 走默认），不在
    （agent 的 worktree 里就是这样）就退到 ~/tiktok-live-translator/logs。"""
    if provenance.LOG_DIR.is_dir():
        return None
    return Path.home() / "tiktok-live-translator" / "logs"


def build_rules(defaults_dir, local_dir):
    """词表：已确认和待确认（`:pending`）的红条档都在里面，线索档照常。"""
    return cg.load_rules(defaults_dir, local_dir)


def corpus_sentences(log_dir=None):
    """口播里不同的句子：{句子: [场次集合, 出现次数]}。同一句话出现几次、在几场里都记下。"""
    sents, segments, sessions = {}, 0, 0
    for meta in provenance.corpus(log_dir=log_dir):
        sessions += 1
        try:
            lines = Path(meta["path"]).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "segment" or not (row.get("raw_text") or "").strip():
                continue
            segments += 1
            for sent in SENTENCE_SPLIT.split(row["raw_text"]):
                sent = sent.strip()
                if len(sent) < MIN_SENTENCE:
                    continue
                slot = sents.setdefault(sent, [set(), 0])
                slot[0].add(meta["path"])
                slot[1] += 1
    return sents, segments, sessions


def collide(rules, sents, keep_examples=2):
    """每条词条的碰撞：({(文件, 段, 词条): {"sentences", "sessions", "occurrences", "examples"}},
    {(文件, 段): 这一段命中的不同句子数})。

    走 CommentGuard.lex_hits：和真判定同一条路径（豁免短语先盖住、否认式剪掉、目标词只在
    指向对方的句式里算），数的是「这条词条真的会命中的句子」。一个句子同时命中同一段的几条词条
    （estafa / estafas 这种单复数）时，段的合计只算一次。"""
    guard = cg.CommentGuard(rules)
    out, per_section = {}, defaultdict(int)
    for sent, (sess, occ) in sents.items():
        hits = guard.lex_hits(sent)
        hits.pop("_flags", None)
        for (file, sec), terms in hits.items():
            per_section[(file, sec)] += 1
            for term in set(terms):
                row = out.setdefault((file, sec, term), {
                    "sentences": 0, "occurrences": 0, "sessions": set(), "examples": []})
                row["sentences"] += 1
                row["occurrences"] += occ
                row["sessions"] |= sess
                if len(row["examples"]) < keep_examples:
                    row["examples"].append(sent[:140])
    return out, per_section


def entry_table(rules, hits, only=None, status=None):
    """按词表里的顺序列出每条词条：状态（enabled / pending / candidate）和碰撞数据。
    status 不传时按段名定：带 `:pending` 的是 pending，其余 enabled；--term 传 candidate。"""
    rows = []
    for (file, sec), entries in rules.entries.items():
        if cg.SECTIONS[file][sec] == cg.EXEMPT:
            continue
        base = cg.base_sec(sec)
        if only and "%s/%s" % (file, base) not in only:
            continue
        for key in entries:
            row = hits.get((file, sec, key), {})
            rows.append({
                "file": file, "section": base, "tier": cg.SECTIONS[file][sec],
                "entry": key, "original": rules.originals[(file, sec)].get(key, key),
                "status": status or ("pending" if cg.is_pending(sec) else "enabled"),
                "sentences": row.get("sentences", 0),
                "sessions": len(row.get("sessions", ())),
                "occurrences": row.get("occurrences", 0),
                "examples": row.get("examples", [])})
    return rows


STATUS_LABEL = {"enabled": "已打开", "pending": "待确认", "candidate": "候选（未入表）"}


def render(rows, args, per_section):
    lines = []
    by_sec = defaultdict(list)
    for r in rows:
        by_sec[(r["file"], r["section"], r["status"])].append(r)
    for (file, sec, status), items in by_sec.items():
        hit = [r for r in items if r["sentences"]]
        key = (file, sec + cg.PENDING if status == "pending" else sec)
        lines.append("")
        lines.append("== %s/%s  [%s档，%s]  共 %d 条，有命中 %d 条，命中的不同句子 %d" % (
            file, sec, items[0]["tier"], STATUS_LABEL[status],
            len(items), len(hit), per_section.get(key, 0)))
        for r in items:
            if r["sentences"] < args.min_hits and not args.all:
                continue
            lines.append("  %5d 句 / %2d 场  %s" % (r["sentences"], r["sessions"], r["entry"]))
            for ex in r["examples"][:args.examples]:
                lines.append("            e.g. %s" % ex)
    return "\n".join(lines)


def candidate_rules(term, section):
    """--term：只含这一条候选词条的 Rules（入表前的风险报告）。`--as insult/strong:pending` 和
    `insult/strong` 是同一段：候选还没入表，谈不上待确认还是已确认；统一成不带 :pending 的写法，
    报告标题里「命中的不同句子」才查得到。"""
    file, _, sec = section.partition("/")
    sec = cg.base_sec(sec)
    if file not in cg.SECTIONS or sec not in cg.SECTIONS[file]:
        raise SystemExit("[错误] --as 要写成 文件/段，例如 insult/strong；认得的段：{}".format(
            "、".join("%s/%s" % (f, s) for f, secs in cg.SECTIONS.items() for s in secs
                     if not cg.is_pending(s))))
    tmp = tempfile.TemporaryDirectory()
    (Path(tmp.name) / (file + ".txt")).write_text("[%s]\n%s\n" % (sec, term), encoding="utf-8")
    rules = cg.load_rules(tmp.name, Path(tmp.name) / "no-local")
    return rules, tmp


def main(argv=None):
    ap = argparse.ArgumentParser(description="弹幕留意词表在主播口播里的碰撞计数（只读，纯 CPU）")
    ap.add_argument("--logs", default=None,
                    help="语料目录（默认仓库的 logs/；它不存在就退到 ~/tiktok-live-translator/logs）")
    ap.add_argument("--defaults", default=str(DEFAULTS), help="词表目录（默认 comment_guard/defaults）")
    ap.add_argument("--local", default=None,
                    help="本机叠加目录；默认不叠加（报告只看入库的词表，可复现）")
    ap.add_argument("--section", action="append", default=[],
                    help="只看这一段，例如 insult/threat（可重复）")
    ap.add_argument("--term", default=None, help="只审计这一条候选词条（入表前的风险报告）")
    ap.add_argument("--as", dest="as_section", default="insult/strong",
                    help="--term 当作哪一段的词条（默认 insult/strong）")
    ap.add_argument("--examples", type=int, default=2, help="每条词条最多列几个口播例句")
    ap.add_argument("--min-hits", type=int, default=1, help="只列命中不少于这么多句的词条")
    ap.add_argument("--all", action="store_true", help="没命中的词条也列出来")
    ap.add_argument("--json", default=None, help="把逐条数据另存成 JSON（不要放进仓库）")
    args = ap.parse_args(argv)

    # 硬闸：第一件事，在读任何语料之前
    hit = live_running()
    if hit:
        raise SystemExit("直播转写仍在进行（命中进程特征：{}），拒绝跑碰撞计数".format(hit))

    # 默认不叠加本机文件：报告只看入库的词表，别人跑出来的数字能对上（指向一个不存在的目录）
    local = Path(args.local) if args.local else Path(tempfile.gettempdir()) / (
        "guard-collision-no-local-%d" % os.getpid())
    keep, status = None, None
    if args.term:
        rules, keep = candidate_rules(args.term, args.as_section)
        status = "candidate"
    else:
        rules = build_rules(args.defaults, local)
    logs = Path(args.logs).expanduser() if args.logs else default_logs()
    sents, segments, sessions = corpus_sentences(logs)
    if not sessions or not segments:
        raise SystemExit("语料为空（{} 场 / {} 段）：{} 里没有真实直播会话，碰撞数全是 0 不代表词条安全。"
                         "用 --logs 指向 ~/tiktok-live-translator/logs".format(
                             sessions, segments, logs or provenance.LOG_DIR))
    print("语料：{} 场 / {} 段 / {} 个不同的口播句子（来源 provenance.corpus，非 glob；"
          "主播口播，不是弹幕）".format(sessions, segments, len(sents)))
    hits, per_section = collide(rules, sents, max(args.examples, 2))
    rows = entry_table(rules, hits, only=set(args.section) or None, status=status)
    print(render(rows, args, per_section))
    if args.json:
        data = {"corpus": {"sessions": sessions, "segments": segments, "sentences": len(sents)},
                "sections": {"%s/%s" % key: n for key, n in sorted(per_section.items())},
                "entries": rows}
        Path(args.json).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if keep is not None:
        keep.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
