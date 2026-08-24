"""benchmark 用的真实语料抽取：从审计日志里取有词表命中的识别文本。"""
import glob
import json


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
