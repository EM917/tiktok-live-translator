"""空响应率 × 输入长度：同一批真实字幕喂给两档模型。

起因：7B 在实盘上对约 2% 的字幕直接返回空（界面显示「翻译失败」），而
1.8B 同样的句子翻得好好的。要回答的是「是不是长句才这样」——先别猜。

跑法：python3 tools/bench_stability.py [语料上限]
"""
import asyncio
import glob
import json
import sys

sys.path.insert(0, ".")

from app.glossary import load                                  # noqa: E402
from app.translator import OllamaHyMT2Translator, _strip_special  # noqa: E402

MODELS = [("Hy-MT2 1.8B", "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M"),
          ("Hy-MT2 7B", "hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M")]
BUCKETS = [(0, 30), (30, 60), (60, 100), (100, 10000)]


def corpus(limit):
    """最近的会话优先——要的是当下这场直播的句子，不是几天前的。"""
    seen, out = set(), []
    for path in sorted(glob.glob("logs/*.jsonl"), reverse=True):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "segment":
                    continue
                text = (row.get("text") or "").strip()
                if len(text) < 5 or text in seen:
                    continue
                seen.add(text)
                out.append(text)
                if len(out) >= limit:
                    return out
    return out


async def translate(session, tr, text, pairs):
    body = {"model": tr.model,
            "prompt": tr._BOS + tr._USER
                      + tr._prompt(text, "zh-CN", "es", pairs) + tr._ASSISTANT,
            "raw": True, "stream": False, "options": tr._OPTIONS,
            "keep_alive": "30m"}
    try:
        async with session.post(tr.url, data=json.dumps(body)) as resp:
            data = await resp.json()
        return _strip_special(data.get("response") or "")
    except Exception:
        return ""


async def main():
    import aiohttp

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    g = load("glossary.txt")
    data = corpus(limit)
    print("语料 {} 句（取自最近的会话）\n".format(len(data)))
    empty_by_model = {}
    for label, model in MODELS:
        tr = OllamaHyMT2Translator()
        tr.model = model
        stat = {b: [0, 0] for b in BUCKETS}
        empties = []
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=90)) as session:
            for text in data:
                out = await translate(session, tr, text, g.translation_pairs(text))
                for b in BUCKETS:
                    if b[0] <= len(text) < b[1]:
                        stat[b][1] += 1
                        if not out:
                            stat[b][0] += 1
                            empties.append(text)
                        break
        bad = sum(e for e, _ in stat.values())
        tot = sum(n for _, n in stat.values())
        empty_by_model[label] = set(empties)
        print("{:<14} 空响应 {}/{} = {:.1f}%".format(
            label, bad, tot, 100.0 * bad / max(tot, 1)))
        for b in BUCKETS:
            e, n = stat[b]
            if n:
                print("   {:>3}-{:<4} 字符  {:>2}/{:<3} = {:5.1f}%".format(
                    b[0], b[1] if b[1] < 10000 else "+", e, n, 100.0 * e / n))
        for t in empties[:4]:
            print("     空（{} 字符）: {}".format(len(t), t[:66]))
        print()
        await tr.close()
    only7b = empty_by_model.get("Hy-MT2 7B", set()) - empty_by_model.get("Hy-MT2 1.8B", set())
    print("只有 7B 失败、1.8B 成功的句子: {} 句".format(len(only7b)))


asyncio.run(main())
