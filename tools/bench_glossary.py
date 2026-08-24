"""词表遵从率实测：语料取自 logs/ 里的真实识别文本，判据是词表要求的中文
有没有出现在译文里。

跑法：python3 tools/bench_glossary.py（需要 Ollama 在跑）

2026-08-24 的结果（175 句 / 269 个术语）：
    Keep these terms exactly as given      46.1%   ← 线上在用
    You MUST use these exact translations  48.7%   （标准误约 3pp，与上者无差别）
    Glossary (mandatory)                   38.3%   ← 明显更差

结论：换措辞救不了。这类纯翻译模型对指令区术语表的遵从率就在五成上下，
要再往上走得换有原生术语干预能力的模型。加新引擎时用这个脚本对比。
"""
import asyncio
import glob
import json
import os
import sys
import time

sys.path.insert(0, ".")

import aiohttp  # noqa: E402
from app.glossary import load

URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/generate"

VARIANTS = {
 "现状(Keep exactly)": "Translate this {sn} text into {tn}. Output only the translation. Keep these terms exactly as given: {g}.",
 "MUST use":           "Translate this {sn} text into {tn}. Output only the translation. You MUST use these exact translations: {g}.",
 "Glossary(mandatory)":"Translate this {sn} text into {tn}. Output only the translation. Glossary (mandatory): {g}.",
}

def sentences():
    g = load("glossary.txt")
    seen, out = set(), []
    for f in sorted(glob.glob("logs/*.jsonl")):
        for line in open(f, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") != "segment":
                continue
            t = (r.get("text") or "").strip()
            if len(t) < 15 or t in seen:
                continue
            m = g.matching(t)
            if not m:
                continue
            seen.add(t)
            out.append((t, g.translation_hint(t), [zh for _, zh, _ in m]))
    return out

async def run(session, head, text):
    body = {"model": "translategemma:4b", "prompt": head + "\n\n" + text, "stream": False,
            "options": {"temperature": 0, "num_predict": 200}, "keep_alive": "30m"}
    async with session.post(URL, data=json.dumps(body)) as r:
        d = await r.json()
    return (d.get("response") or "").strip()

async def main():
    data = sentences()
    print("语料: {} 句（含词表命中），共 {} 个待检术语\n".format(
        len(data), sum(len(z) for _, _, z in data)))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        for name, tpl in VARIANTS.items():
            hit = tot = 0
            t0 = time.time()
            misses = []
            for text, hint, wants in data:
                head = tpl.format(sn="Spanish", tn="Simplified Chinese", g=hint)
                out = await run(s, head, text)
                for w in wants:
                    tot += 1
                    if w in out:
                        hit += 1
                    elif len(misses) < 4:
                        misses.append((w, out[:52]))
            ms = (time.time() - t0) / max(len(data), 1) * 1000
            print("{:<22} 遵从 {:>3}/{:<3} = {:5.1f}%   均 {:.0f}ms/句".format(
                name, hit, tot, 100.0*hit/max(tot,1), ms))
            for w, o in misses:
                print("      漏「{}」→ {}".format(w, o))
    print()

asyncio.run(main())
