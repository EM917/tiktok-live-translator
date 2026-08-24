"""用最强的本地模型重译一场直播的审计日志。

为什么值得单独做一遍：合规复核本来就是事后进行的。直播时用低延迟档保证
违禁词报警不迟到，事后再用最强模型把**记录**翻准——两边都拿满，而且对直播
零影响（那时已经没有 Whisper 在抢内存了）。

只**追加** translation_strong 记录，不改动原有任何一行：审计日志的价值在于
它记录的是当时发生了什么，事后覆盖会毁掉这一点。

跑法：
    python3 tools/retranslate_audit.py                 # 最近一场
    python3 tools/retranslate_audit.py logs/xxx.jsonl  # 指定一场
    python3 tools/retranslate_audit.py --alerts-only   # 只重译命中违禁词的段
"""
import asyncio
import glob
import json
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")

from app.glossary import load as load_glossary          # noqa: E402
from app.translator import create_strong_translator     # noqa: E402


def latest_log():
    files = sorted(glob.glob("logs/*.jsonl"))
    return files[-1] if files else None


def read_rows(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue          # 崩溃时可能留下半行，跳过即可
    return rows


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    alerts_only = "--alerts-only" in sys.argv
    path = args[0] if args else latest_log()
    if not path:
        print("logs/ 下没有找到审计日志")
        return

    rows = read_rows(path)
    segments = [r for r in rows if r.get("type") == "segment" and (r.get("text") or "").strip()]
    if alerts_only:
        segments = [r for r in segments if r.get("hits")]
    done = {r.get("seq") for r in rows if r.get("type") == "translation_strong"}
    todo = [r for r in segments if r.get("seq") not in done]
    old = {r["seq"]: r.get("translated") for r in rows
           if r.get("type") == "translation" and r.get("ok")}

    tr = create_strong_translator()
    if tr is None:
        print("本机没有可用的本地翻译模型——先装 Ollama 并让程序拉一次模型")
        return
    print("{}\n共 {} 段{}，其中 {} 段待重译，使用 {}\n".format(
        path, len(segments), "（仅命中违禁词的）" if alerts_only else "",
        len(todo), tr.model))

    g = load_glossary("glossary.txt")
    changed = []
    t0 = time.time()
    with open(path, "a", encoding="utf-8") as out:
        for i, row in enumerate(todo, 1):
            text = row["text"].strip()
            pairs = tuple(g.translation_pairs(text)) if g else ()
            new = await tr.translate(text, "zh-CN", source=row.get("language") or "auto",
                                     glossary=pairs or None)
            if new and g:
                new = g.apply(text, new)
            out.write(json.dumps({
                "type": "translation_strong", "seq": row["seq"],
                "at": datetime.now().isoformat(timespec="seconds"),
                "model": tr.model, "translated": new, "ok": bool(new),
            }, ensure_ascii=False) + "\n")
            out.flush()
            was = old.get(row["seq"])
            if new and was and new.strip() != was.strip():
                changed.append((text, was, new))
            print("\r  {}/{}".format(i, len(todo)), end="", flush=True)
    await tr.close()

    print("\r  完成 {} 段，用时 {:.0f} 秒\n".format(len(todo), time.time() - t0))
    if changed:
        print("译文有变化的 {} 段（复核时优先看这些）：\n".format(len(changed)))
        for text, was, new in changed[:15]:
            print("  ES  {}".format(text[:88]))
            print("  原  {}".format((was or "")[:80]))
            print("  新  {}\n".format(new[:80]))
        if len(changed) > 15:
            print("  …另有 {} 段".format(len(changed) - 15))
    else:
        print("译文没有变化。")


asyncio.run(main())
