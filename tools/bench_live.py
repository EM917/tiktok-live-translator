"""实盘翻译基准：同一批真实字幕，多个引擎，盲评。

要回答的问题只有一个——**为了让中控实时看懂，1.8B 到底够不够**；不够的话，
是付网络和隐私的代价换 DeepL，还是承担 7B 的资源开销。

和已有两个基准的分工：
    bench_glossary.py  商品名有没有译成规范写法（词表遵从率）
    bench_meaning.py   15 条已知错例的回归集，防倒退
    bench_live.py      ← 这个。未针对性修过的新字幕，判整句语义

四条规则，每条都是为了防一种自欺：

1. **回归集的 15 条必须排除在外。** 词表针对它们改过、模板 bug 是靠它们发现的，
   它们已经是训练集。留在测试集里等于让模型考已经补过课的题。
2. **不能全是难句。** 120 条随机 + 80 条分层，既回答「日常字幕谁最准」，
   也回答「最容易让中控判断失误的句子谁最准」。全挑难题算出的错误率不代表实盘。
3. **逐句随机打乱 A/B/C。** 不能 A 列固定是某个引擎——评到二三十句就会从文风
   认出来，盲评就不盲了。对照表单独存盘，全部评完再揭晓。
4. **不只报一个准确率。** 真正该盯的是「会让中控理解错」的严重错误率，
   而不是总分。漏译、价格错、术语错各自单列。

另外偷偷重复约 18 条（同一句、同样三个译文，换个编号），用来检查评审自身的
一致性——同一条前后判得不一样，说明评分噪声大，结论要打折。


本地开源候选的实测记录（免得再走一遍）：

    Qwen3-4B      出局，且不是因为质量。Ollama 上关不掉它的思考模式——
                  `/no_think` 被忽略，`think:false` 只是把内心独白从
                  thinking 字段挪进 content。不设生成上限时，它花了 2409 个
                  token 才吐出一句 22 字的字幕。无论分数多高，这种东西不能
                  放在实时字幕路径上。
    Gemma 3 4B    正好落在它的体量该在的位置：词不达意回归集 14/15
                  （1.8B 是 13，7B 是 15），中位 894ms（1.8B 310ms，
                  7B 1139ms）。值得跑完整评测，但不是突破——它犯的错和
                  Hy-MT2 一模一样（"en órdenes de 40" 译成「40个订单」，
                  "gotas" 译成「剂量」）。

还有一条硬约束限定了候选范围：**实时档必须小到不和 Whisper 抢内存**。
7B（4.6GB 常驻）实测把识别从 1.4 秒拖到 3.2 秒，而识别在报警链路上。
所以实时翻译的内存预算约 2–3GB，7B 只能做按需调用、用完卸载的强模型档。

记在这里是因为 NLLB 当初是被推理否掉的、不是被测量否掉的——那和「测词表
遵从率却当成翻译质量」是同一个错误。

跑法：
    python3 tools/bench_live.py build            # 抽样，存成固定的一份
    python3 tools/bench_live.py run              # 跑引擎，出盲评表 + 对照表
    python3 tools/bench_live.py score ratings.json
"""
import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from app import provenance          # noqa: E402

from app.glossary import load as load_glossary     # noqa: E402
from app.translator import create_translator       # noqa: E402
from tools.bench_meaning import load_cases         # noqa: E402

OUT = Path("benchmarks")
SAMPLE = OUT / "live_sample.json"
SHEET = OUT / "live_blind_sheet.md"
MAPPING = OUT / "live_mapping.json"
RESULT = OUT / "live_result.md"

SEED = 20260824          # 固定种子：抽样必须可复现，否则换一批语料结论就变了
DUPLICATES = 18          # 偷偷重复多少条，用来测评审一致性

QUOTA = [("随机", 120), ("价格/数字", 30), ("商品名/词表", 20),
         ("中英混说", 10), ("残句", 10), ("口语/俚语", 10)]

_ENGLISH = re.compile(
    r"\b(the|you|your|and|is|are|can|we|they|this|that|for|with|have|"
    r"what|when|how|please|thank|going|want|need|now|here|there)\b", re.I)
# 只收**真的会被译错**的口语词。曾经写过 `re\w{4,}` 想抓阿根廷话的强调前缀
# `re-`，结果它命中的全是 realidad / recomiendo / relajar / regalar 这类普通
# 西语词——这一档 10 条抽出来一条真俚语都没有。宁可这一档凑不满，也不能拿
# 一堆普通句子冒充难句：那样算出的「口语错误率」是假的。
_SLANG = re.compile(
    r"\b(plata|guita|laburo|pibe|piba|copado|b[áa]rbaro|joya|posta|"
    r"quilombo|fiaca|zafar|bancar|chamuyo|viste|boludo|macana|pinta|"
    r"un toque|al toque|un mont[óo]n|de vuelta|vence|vencimiento|"
    r"nom[áa]s|capaz que|ni ah[íi]|est[áa] bueno|qu[ée] lindo)\b", re.I)
_MONEY = re.compile(r"\d|d[óo]lar|peso|descuento|cup[óo]n|precio|gratis|oferta", re.I)
_ENDS = re.compile(r"[.!?！？。…\"'»]\s*$")


def _bucket(text, g):
    if not _ENDS.search(text):
        return "残句"
    if _MONEY.search(text):
        return "价格/数字"
    if _SLANG.search(text):
        return "口语/俚语"
    if len(_ENGLISH.findall(text)) >= 3:
        return "中英混说"
    if g.matching(text):
        return "商品名/词表"
    return "随机"


def build():
    """从全部日志里分层抽样，存成固定的一份。"""
    g = load_glossary("glossary.txt")
    seen, pool = set(), []
    # 用来源清单而不是 glob：测试夹具曾经占到语料的两成，而 glob 分不出来。
    for meta in provenance.corpus():
        f = meta["path"]
        for line in open(f, encoding="utf-8"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "segment":
                continue
            t = (d.get("text") or "").strip()
            if len(t) < 25 or t.lower() in seen:
                continue
            seen.add(t.lower())
            pool.append({"text": t, "session": os.path.basename(f), "seq": d.get("seq")})

    # 回归集的句子一律排除：它们是训练集，不是测试集
    known = {c[0].strip().lower() for c in load_cases()}
    pool = [p for p in pool if p["text"].lower() not in known]

    rnd = random.Random(SEED)
    rnd.shuffle(pool)
    buckets = {}
    for p in pool:
        buckets.setdefault(_bucket(p["text"], g), []).append(p)

    picked, used = [], set()
    for name, n in QUOTA:
        if name == "随机":
            continue
        take = [p for p in buckets.get(name, []) if p["text"] not in used][:n]
        for p in take:
            p["bucket"] = name
            used.add(p["text"])
        picked += take
        if len(take) < n:
            print("[提示] 「{}」只凑到 {}/{} 条".format(name, len(take), n))
    rest = [p for p in pool if p["text"] not in used]
    for p in rest[:dict(QUOTA)["随机"]]:
        p["bucket"] = "随机"
        picked.append(p)

    OUT.mkdir(exist_ok=True)
    SAMPLE.write_text(json.dumps(picked, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = {}
    for p in picked:
        counts[p["bucket"]] = counts.get(p["bucket"], 0) + 1
    print("抽了 {} 条：{}".format(len(picked), counts))
    print("字符总量 {}（DeepL 按这个计额度）".format(sum(len(p["text"]) for p in picked)))


ENGINES = [("hymt2", "Hy-MT2 1.8B"), ("hymt2-7b", "Hy-MT2 7B")]


async def run():
    g = load_glossary("glossary.txt")
    items = json.loads(SAMPLE.read_text(encoding="utf-8"))
    engines = list(ENGINES)
    if os.environ.get("DEEPL_API_KEY"):
        engines.append(("deepl", "DeepL"))
    else:
        print("（未设置 DEEPL_API_KEY，跳过 DeepL）")

    outputs, latency, failures = {}, {}, {}
    for choice, label in engines:
        tr = create_translator(choice)
        inner = getattr(tr, "inner", tr)
        if hasattr(inner, "keep_alive"):
            inner.keep_alive = "10m"        # 批处理：别逐句装卸模型
        each, fail = [], 0
        for i, p in enumerate(items):
            t0 = time.time()
            out = await tr.translate(p["text"], "zh-CN", source="es",
                                     glossary=tuple(g.translation_pairs(p["text"])) or None)
            each.append((time.time() - t0) * 1000)
            if not out:
                fail += 1
            outputs.setdefault(i, {})[label] = g.apply(p["text"], out or "") or "（翻译失败）"
            if (i + 1) % 25 == 0:
                print("  {} {}/{}".format(label, i + 1, len(items)), flush=True)
        each.sort()
        latency[label] = (each[len(each) // 2], each[int(len(each) * 0.95)])
        failures[label] = fail
        await tr.close()

    rnd = random.Random(SEED + 1)
    order = list(range(len(items)))
    dupes = rnd.sample(order, min(DUPLICATES, len(order)))
    rows = order + dupes
    rnd.shuffle(rows)

    labels = [lab for _c, lab in engines]
    mapping, lines = [], []
    for n, idx in enumerate(rows, 1):
        shuffled = labels[:]
        rnd.shuffle(shuffled)
        mapping.append({"n": n, "item": idx, "order": shuffled,
                        "bucket": items[idx]["bucket"]})
        lines.append("### {}\n**ES** {}\n".format(n, items[idx]["text"]))
        for slot, lab in zip("ABC", shuffled):
            lines.append("- **{}** {}".format(slot, outputs[idx][lab]))
        lines.append("")

    OUT.mkdir(exist_ok=True)
    SHEET.write_text("\n".join(lines), encoding="utf-8")
    MAPPING.write_text(json.dumps(
        {"mapping": mapping, "latency": latency, "failures": failures,
         "engines": labels}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n盲评表 {} 条（含 {} 条重复）→ {}".format(len(rows), len(dupes), SHEET))
    for lab in labels:
        print("  {:<14} 中位 {:.0f}ms  P95 {:.0f}ms  失败 {}".format(
            lab, latency[lab][0], latency[lab][1], failures[lab]))


GRADES = ["correct", "minor", "major", "omission"]


def score(path):
    """把评分和对照表合起来出报告。ratings.json 形如
    {"1": {"A": "correct", "B": "major", "C": "minor"}, ...}"""
    meta = json.loads(MAPPING.read_text(encoding="utf-8"))
    ratings = json.loads(Path(path).read_text(encoding="utf-8"))
    by_n = {m["n"]: m for m in meta["mapping"]}

    tally = {lab: {gname: 0 for gname in GRADES} for lab in meta["engines"]}
    by_bucket = {}
    seen_item = {}
    inconsistent = 0
    for n_str, slots in ratings.items():
        m = by_n.get(int(n_str))
        if not m:
            continue
        for slot, grade in slots.items():
            lab = m["order"]["ABC".index(slot)]
            if grade not in GRADES:
                continue
            tally[lab][grade] += 1
            b = by_bucket.setdefault(m["bucket"], {la: {gg: 0 for gg in GRADES}
                                                   for la in meta["engines"]})
            b[lab][grade] += 1
            key = (m["item"], lab)
            if key in seen_item and seen_item[key] != grade:
                inconsistent += 1
            seen_item[key] = grade

    out = ["# 实盘翻译基准", ""]
    out.append("| 引擎 | 正确 | 轻微 | **严重** | 漏译 | 中位延迟 | P95 | 失败 |")
    out.append("|---|---|---|---|---|---|---|---|")
    for lab in meta["engines"]:
        t = tally[lab]
        n = sum(t.values()) or 1
        lat = meta["latency"][lab]
        out.append("| {} | {:.0f}% | {:.0f}% | **{:.0f}%** | {:.0f}% | {:.0f}ms | {:.0f}ms | {} |".format(
            lab, 100*t["correct"]/n, 100*t["minor"]/n, 100*t["major"]/n,
            100*t["omission"]/n, lat[0], lat[1], meta["failures"][lab]))
    out += ["", "评审一致性：重复条目里判得不一致 {} 处（越多说明结论越该打折）".format(inconsistent), ""]
    for bucket, tb in by_bucket.items():
        out.append("## {}".format(bucket))
        out.append("| 引擎 | 正确 | 轻微 | 严重 | 漏译 |")
        out.append("|---|---|---|---|---|")
        for lab in meta["engines"]:
            t = tb[lab]
            n = sum(t.values()) or 1
            out.append("| {} | {:.0f}% | {:.0f}% | {:.0f}% | {:.0f}% |".format(
                lab, 100*t["correct"]/n, 100*t["minor"]/n,
                100*t["major"]/n, 100*t["omission"]/n))
        out.append("")
    RESULT.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        build()
    elif cmd == "run":
        asyncio.run(run())
    elif cmd == "score":
        score(sys.argv[2])
    else:
        print(__doc__)
