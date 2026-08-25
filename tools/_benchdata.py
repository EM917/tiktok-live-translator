"""benchmark 用的真实语料抽取，以及「这个术语算不算翻出来了」的判定。"""
import glob
import json
import re


def term_present(term, text):
    """词表要求的中文有没有出现在译文里。

    直接做子串比对会低估：实测 1.8B 明明输出了「MCT油」，却因为词表里写的是
    「MCT 油」（带空格）判成漏；输出「食欲焦虑」也被判漏，因为词表写的是
    「食欲焦虑（嘴馋）」。判据偏严会让所有引擎的分数一起被压低，比较时看不出
    真实差距——而这个基准存在的意义正是用来做比较。

    所以比对前先归一：去掉空格，括号里的补充说明视为可选，
    斜杠分隔的写法任一命中即可。
    """
    def norm(x):
        return re.sub(r"\s+", "", x or "")

    body = norm(text)
    for part in str(term).split("/"):          # 「嘴馋 / 想吃东西」任一即可
        want = norm(part)
        if not want:
            continue
        if want in body:
            return True
        stripped = norm(re.sub(r"[（(][^）)]*[）)]", "", part))   # 去掉括注
        if stripped and stripped in body:
            return True
    return False


def sentences(glossary, min_len=15):
    """返回 [(西语原文, 紧凑提示串, [期望出现的中文...])]，按原文去重。"""
    seen, out = set(), []
    for path in sorted(glob.glob("logs/*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "segment":
                    continue
                text = (row.get("text") or "").strip()
                if len(text) < min_len or text in seen:
                    continue
                hits = glossary.matching(text)
                if not hits:
                    continue
                seen.add(text)
                out.append((text, glossary.translation_hint(text),
                            [zh for _, zh, _ in hits]))
    return out
