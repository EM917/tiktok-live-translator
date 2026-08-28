"""用最强的本地模型重译一场直播的审计日志，并把**最可疑的译文顶到前面**。

为什么值得单独做一遍：合规复核本来就是事后进行的。直播时用低延迟档保证
违禁词报警不迟到，事后再用最强模型把**记录**翻准——两边都拿满，而且对直播
零影响（那时已经没有 Whisper 在抢内存了）。

只**追加** translation_strong 记录，不改动原有任何一行：审计日志的价值在于
它记录的是当时发生了什么，事后覆盖会毁掉这一点。

为什么要排序：靠人肉眼扫一整场字幕找错，慢且必然漏。而两个模型对同一句话
分歧越大，其中一个越可能是错的——这个信号不需要任何裁判，实测能把真问题顶上来
（首次跑就翻出 `es una orden de 30` 被译成「30 片」而非「满 30 美元」，
那是词表当时漏掉的一条促销条件）。

试过但**不成立**、因此没有采用的两种做法，记在这里免得再走一遍：
  * 回译后比对词汇重合度——好译文和坏译文得分一样（西语同义改写本来就换词）
  * 让本地模型当裁判判「一致/不一致」——1.8B 对所有句子都判不一致，
    7B 十次里对四次，比瞎猜还差

跑法：
    python3 tools/retranslate_audit.py                 # 最近一场
    python3 tools/retranslate_audit.py logs/xxx.jsonl  # 指定一场
    python3 tools/retranslate_audit.py --alerts-only   # 只重译命中违禁词的段
"""
import asyncio
import difflib
import glob
import re
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")

from app.glossary import load as load_glossary          # noqa: E402
from app.translator import create_strong_translator     # noqa: E402


_DIGITS = re.compile(r"\d+")


def suspicion(source, fast, strong):
    """这一段有多可疑。分数越高越该先看。

    两个模型对同一句分歧越大，其中一个越可能错——这是全部的依据，不做语义判断。
    另外叠加几条结构信号：数字消失、问句消失，都是确定性的错误。
    """
    score = 1 - difflib.SequenceMatcher(None, fast or "", strong or "").ratio()
    flags = []
    for name, text in (("快译", fast), ("强译", strong)):
        want = set(_DIGITS.findall(source))
        if want and text and not (want & set(_DIGITS.findall(text))):
            flags.append("{}丢了数字".format(name))
            score += 0.5
        if ("?" in source or "？" in source) and text and \
                "?" not in text and "？" not in text:
            flags.append("{}丢了问句".format(name))
            score += 0.3
    return score, flags


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
    # 与这一场直播时的预处理保持一致：profile 开了 vocative_strip 的主播，
    # 快译吃的是摘除称呼的文本，重译也必须吃同一份
    from app.glossary import profile_options
    from app.provenance import streamer_of
    from app.vocative import strip as strip_vocative
    start = next((r for r in rows if r.get("type") == "session_start"), {})
    strip_on = bool(profile_options(
        streamer_of(start.get("room_url", ""))).get("vocative_strip"))
    segments = [r for r in rows if r.get("type") == "segment" and (r.get("text") or "").strip()]
    if alerts_only:
        segments = [r for r in segments if r.get("hits")]
    # 直播中命中违禁词、或中控手动点过「重译」的段落，已经是强模型译的了，
    # 跳过——两条路径写的是同一种记录类型，正是为了这里能认出来。
    done = {r.get("seq") for r in rows
            if r.get("type") == "translation_strong" and r.get("ok")}
    todo = [r for r in segments if r.get("seq") not in done]
    # baseline 只能取 type == "translation"（快译）。曾经这里把强译也算进来，
    # 而同一个 seq 有两条时字典推导取的是后者——于是强译被当成了「原译文」，
    # 比对结果永远是「没有变化」，恰好在最该复核的那些段落上失效。
    old = {r["seq"]: r.get("translated") for r in rows
           if r.get("type") == "translation" and r.get("ok")}

    tr = create_strong_translator()
    if tr is None:
        print("本机没有可用的本地翻译模型——先装 Ollama 并让程序拉一次模型")
        return
    # 直播时强模型是 keep_alive=0 的：它必须调完就从显存消失，否则会和 Whisper
    # 抢内存，而识别在报警链路上。但这里是**收工后的批处理**，没有 Whisper 在跑，
    # 逐句卸载只是把 1.9 秒的载入时间乘以段数白扔掉（实测 567 段跑成了两小时）。
    tr.keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", "10m")
    print("{}\n共 {} 段{}，其中 {} 段待重译，使用 {}\n".format(
        path, len(segments), "（仅命中违禁词的）" if alerts_only else "",
        len(todo), tr.model))

    g = load_glossary("glossary.txt")
    changed = []
    t0 = time.time()
    with open(path, "a", encoding="utf-8") as out:
        for i, row in enumerate(todo, 1):
            text = row["text"].strip()
            if strip_on:
                # 这一场的快译翻的是摘除称呼后的文本（profile 开了
                # vocative_strip）。重译必须吃同一份输入，否则称呼的有无会被
                # 当成「模型分歧」，在爱称密集的主播身上（实测 38.7% 的句子
                # 带称呼）把真问题淹没。
                text = strip_vocative(text)[0]
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
    # 批处理跑完就把它从显存里请出去，别让 keep_alive 继续占着——用户很可能
    # 紧接着就要开下一场直播。
    tr.keep_alive = 0
    try:
        await tr.translate("ok", "zh-CN", source="es")
    except Exception:
        pass
    await tr.close()

    print("\r  完成 {} 段，用时 {:.0f} 秒\n".format(len(todo), time.time() - t0))
    if not changed:
        print("译文没有变化。")
        return
    ranked = []
    for text, was, new in changed:
        score, flags = suspicion(text, was, new)
        ranked.append((score, flags, text, was, new))
    ranked.sort(key=lambda r: -r[0])
    print("译文有变化的 {} 段，按可疑程度排序（先看前面的）：\n".format(len(ranked)))
    for score, flags, text, was, new in ranked[:15]:
        mark = "  ⚠ " + "、".join(flags) if flags else ""
        print("  分歧 {:.2f}{}".format(score, mark))
        print("    ES  {}".format(text[:86]))
        print("    快译 {}".format((was or "")[:78]))
        print("    强译 {}\n".format(new[:78]))
    if len(ranked) > 15:
        print("  …另有 {} 段，分歧均低于 {:.2f}".format(len(ranked) - 15, ranked[14][0]))


if __name__ == "__main__":       # suspicion() 会被挖掘工具 import，别一 import 就开跑
    asyncio.run(main())
