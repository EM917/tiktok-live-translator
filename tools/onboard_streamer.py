"""新主播接入审计：把「这个主播需要哪些规则」的排查过程固化成一条命令。

为什么需要它：规则本身不泛化，但**发现规则的流程**泛化。同一套称呼清洗规则，
主播① 1604 句里只触发 0.4%，主播② 183 句里触发 32.8%——80 倍。给所有主播套
同一份清单一定是错的，而每接一个新主播先扫一轮语料，是可以标准化的。

**这个工具只提建议，永远不自己改生产规则。** 今天已经有多个「看起来合理的
优化」跑完语料发现是净负收益（跳过专有名词里的数字：救 2 条、伤 3 条；把全部
爱称映射成一个中文词：修好 2 条、制造出「爱邮递员」）。所以流程必须是

    发现 → 度量 → 找反例 → A/B → 人工拍板 → 才生效

跑法：
    python3 tools/onboard_streamer.py logs/session-xxx.jsonl
    python3 tools/onboard_streamer.py logs/session-xxx.jsonl --ablate
    python3 tools/onboard_streamer.py logs/xxx.jsonl --glossary old.txt --ablate

--ablate 会把整场重译多遍（慢），用来把收益拆到每一层上。不拆的话很容易把
组合包的收益误算成某一个模块的——这个错今天真发生过。
"""
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, ".")

from app import provenance          # noqa: E402

from app.glossary import Glossary, load, parse        # noqa: E402
from app.vocative import VOCATIVES, strip             # noqa: E402

# 称呼候选取的是超集——工具的职责是把它们连同反例一起摆出来，不是替人决定。
CANDIDATES = [
    "mi niña", "mi reina", "mi amor", "mi vida", "mi gente", "mi cielo",
    "amor", "amorcito", "amorcitos", "amores", "corazón", "cariño",
    "hermosa", "hermosas", "hermoso", "preciosa", "preciosas", "precioso",
    "reina", "reinas", "linda", "lindas", "mami", "bebé", "chula", "guapa",
    "chicas", "chicos", "muchachos", "gente",
]
_EDGE = "[,;.!?¡¿]"
# 称呼不会跟在介词或不定冠词后面。两条实测依据：
#   "Le ayuda mucho a mi mami"        —— 介词 a，mami 是宾语（真的是妈妈）
#   "Sintiéndome como una reina"      —— como + una，reina 是表语（比喻）
# 这两条正是当初手工把 mami 和裸 reina 排除掉的依据，现在变成程序判据。
# 介词和称呼之间可以隔一个限定词——"a **mi** mami"、"como **una** reina"
# 都是这种形状，只认紧邻的话两条都漏。
_PREP = re.compile(
    r"(?:^|\s)(?:a|de|con|para|por|en|sin|sobre|hacia|como)\s+"
    r"(?:(?:mi|tu|su|mis|tus|sus|el|la|los|las|un|una|unos|unas)\s+)?$", re.I)


def captions(path):
    src, tr = {}, {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "segment" and (d.get("text") or "").strip():
            src[d.get("seq")] = d["text"].strip()
        elif d.get("type") == "translation" and d.get("ok"):
            tr[d.get("seq")] = d.get("translated") or ""
    return src, tr


def vocative_audit(texts, corpus=None):
    """① 称呼审计：出现次数、小句边界次数、以及**反例**。

    反例必须跨**全部**语料找，不能只看被审计的这一场。实测过一次：只看当场时
    `reina` 零反例、看起来可以直接摘，而另一个主播的语料里有
    "Sintiéndome como una reina"（感觉像个女王）——那是比喻不是称呼。
    一场语料证明不了一个词永远是称呼。
    """
    corpus = corpus if corpus is not None else texts
    rows = []
    for cand in CANDIDATES:
        word = re.compile(r"(?<![\wáéíóúñÁÉÍÓÚÑ])" + re.escape(cand)
                          + r"(?![\wáéíóúñÁÉÍÓÚÑ])", re.I)
        total = edge = 0
        for t in texts:
            for m in word.finditer(t):
                total += 1
                tail = t[m.end():m.end() + 2].lstrip()
                at_edge = (not tail) or re.match(_EDGE, tail)
                if not at_edge:
                    continue
                edge += 1
        # 反例在全语料里找：边界上、但前面跟着介词 → 它是句子成分不是称呼
        # （"Le ayuda mucho a mi mami" 就是这么被排除的）
        counter_ex = []
        for t in corpus:
            for m in word.finditer(t):
                tail = t[m.end():m.end() + 2].lstrip()
                if ((not tail) or re.match(_EDGE, tail)) and _PREP.search(t[:m.start()]):
                    counter_ex.append(t)
        if total:
            rows.append({"term": cand, "total": total, "edge": edge,
                         "counter": counter_ex})
    return sorted(rows, key=lambda r: -r["edge"])


def verdict(row, mapped=frozenset()):
    if row["term"].lower() in mapped:
        return "词表已映射（摘掉会丢「在对谁说话」，需人工权衡）"
    if row["counter"]:
        return "拒绝（有实义反例）"
    if row["edge"] == 0:
        return "不适用（从不在小句边界）"
    if row["edge"] < 3:
        return "证据不足（边界出现 < 3 次）"
    if row["term"] in VOCATIVES:
        return "已在名单里"
    return "★ 摘除候选"


def domain_audit(src, tr, g, top=18):
    """② 电商/领域词审计：高频、且**词表没收**的西语短语，附两条真实译例。

    这里不自动判断译得对不对——没有对齐就没法可靠地说「这个中文片段对应那个
    西语词」。工具能做的是把候选和证据摆出来，判断留给人。
    """
    covered = {v.lower() for vs, _zh in g.entries for v in vs}
    # 称呼归第①块管，别在这里重复出现
    covered |= {c.lower() for c in CANDIDATES}
    covered |= {c.split()[-1].lower() for c in CANDIDATES if " " in c}
    freq = Counter()
    where = defaultdict(list)
    for seq, t in src.items():
        low = t.lower()
        words = re.findall(r"[a-záéíóúñü]+", low)
        for n in (1, 2, 3):
            for i in range(len(words) - n + 1):
                ng = " ".join(words[i:i + n])
                if len(ng) < 4 or ng in covered:
                    continue
                freq[ng] += 1
                if seq in tr and len(where[ng]) < 2:
                    where[ng].append((t, tr[seq]))
    # 只留下够高频、且不是纯功能词的
    # 功能词和泛指名词：出现频率高但没有领域含义，收进来只会淹没真正的候选
    STOP = re.compile(
        r"^(que|para|con|los|las|una|uno|del|por|más|pero|como|si|no|ya|"
        r"esto|esta|este|esos|esas|todo|toda|todos|muy|también|ahorita|"
        r"ustedes|nosotros|ellos|quiere|quieren|quieres|tiene|tienen|"
        r"puede|pueden|puedes|está|están|estás|vamos|aquí|ahí|bien|"
        r"otro|otra|otros|producto|productos|cosa|cosas|gracias|hola|"
        r"hacer|haga|hagan|dice|decir|saber|dejen|dejes|vaya|van|voy)\b")
    out = [(ng, c) for ng, c in freq.most_common(400)
           if c >= 4 and not STOP.match(ng) and where[ng]]
    return out[:top], where


async def ablate(src, tr, base_glossary, extra_entries, voc_terms):
    """④ 消融：把收益拆到每一层上。

    不拆的话很容易把组合包的收益算成某一个模块的——今天真发生过：68 条变化里
    既有称呼摘除也有电商词，我却按摘除的功劳报了整个 11.6 个百分点。
    """
    from app.translator import create_translator

    plus = Glossary(base_glossary.entries + [e[:2] for e in extra_entries])

    def maybe_strip(t, on):
        return strip(t)[0] if on else t

    configs = [("① 基线", base_glossary, False),
               ("② 只加称呼摘除", base_glossary, True),
               ("③ 只加领域词", plus, False),
               ("④ 两个都加", plus, True)]
    engine = create_translator("hymt2")
    results = {}
    for name, g, voc in configs:
        outs = {}
        for seq, s in src.items():
            if seq not in tr:
                continue
            t = maybe_strip(s, voc)
            o = await engine.translate(t, "zh-CN", source="es",
                                       glossary=tuple(g.translation_pairs(t)) or None)
            outs[seq] = g.apply(t, o or "")
        results[name] = outs
        print("  {} 完成".format(name), flush=True)
    await engine.close()
    return results


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return
    path = args[0]
    gpath = args[1] if len(args) > 1 else "glossary.txt"
    src, tr = captions(path)
    texts = list(src.values())
    g = Glossary(parse(Path(gpath).read_text(encoding="utf-8"))) if Path(gpath).exists() else load()
    print("语料 {}：{} 句字幕，{} 句有译文\n".format(Path(path).name, len(src), len(tr)))

    print("① 称呼审计")
    print("   {:<12} {:>6} {:>8}  {}".format("候选", "出现", "在边界", "判断"))
    # 反例跨全部日志找，不只看当场
    all_texts = []
    for meta in provenance.corpus():
        all_texts.extend(captions(meta["path"])[0].values())
    hits = vocative_audit(texts, corpus=all_texts or texts)
    mapped = {v.lower() for vs, _zh in g.entries for v in vs}
    for r in hits[:14]:
        print("   {:<12} {:>6} {:>8}  {}".format(
            r["term"], r["total"], r["edge"], verdict(r, mapped)))
        for c in r["counter"][:1]:
            print("        反例: {}".format(c[:88]))
    strip_n = sum(1 for t in texts if strip(t)[1])
    print("\n   现有名单在这场的触发率: {}/{} = {:.1f}%".format(
        strip_n, len(texts), 100 * strip_n / max(len(texts), 1)))

    print("\n② 领域词审计（高频且词表未收）")
    cands, where = domain_audit(src, tr, g)
    for ng, c in cands:
        print("   {:<26} {:>3} 次".format(ng, c))
        s, z = where[ng][0]
        print("        ES {}\n        ZH {}".format(s[:82], z[:82]))

    if "--ablate" not in sys.argv:
        print("\n（加 --ablate 可把收益拆到每一层；会把整场重译多遍，较慢）")
        return

    print("\n④ 消融：整场重译 4 遍")
    res = await ablate(src, tr, g, [], VOCATIVES)
    base = res["① 基线"]
    print("\n   {:<16} {:>8} {:>10}".format("配置", "改动条数", "占比"))
    for name, outs in res.items():
        n = sum(1 for k in outs if outs[k] != base.get(k))
        print("   {:<16} {:>8} {:>9.1f}%".format(name, n, 100 * n / max(len(outs), 1)))
    print("\n   改动条数只说明**影响面**，不等于收益。每一层的好坏仍要人工逐条判——")
    print("   今天实测过：同一批改动里 20 条改善、2 条变差，自动算是算不出来的。")
    Path("benchmarks").mkdir(exist_ok=True)
    Path("benchmarks/onboard_ablation.json").write_text(
        json.dumps({k: v for k, v in res.items()}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print("\n   四份译文已存到 benchmarks/onboard_ablation.json，供逐条对照。")


if __name__ == "__main__":
    asyncio.run(main())
