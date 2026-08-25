"""翻译引擎的词表遵从率实测。

语料取自 logs/ 里的真实识别文本，判据是客观的——词表要求的中文有没有出现在
译文里。加新翻译引擎时用它做 A/B。

跑法：python3 tools/bench_glossary.py（需要 Ollama 在跑）

为什么只测这个：本工具的用户读中文那一行做合规判断，真正会害到他们的是
商品名、价格、促销条件被翻错，而不是句子读起来顺不顺。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, ".")

from app.glossary import load                      # noqa: E402
from app.translator import (                       # noqa: E402
    DeepLTranslator, OllamaGemmaTranslator, OllamaHyMT2Translator,
)
from tools._benchdata import sentences, term_present             # noqa: E402


def _hymt2(model):
    def make():
        tr = OllamaHyMT2Translator()
        tr.model = model
        return tr
    return make


ENGINES = [
    ("TranslateGemma 4B", OllamaGemmaTranslator, "str"),
    ("Hy-MT2 1.8B", _hymt2("hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M"), "pairs"),
    ("Hy-MT2 7B", _hymt2("hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M"), "pairs"),
]

# DeepL 不吃逐句词表提示，它用自己账号里的原生术语表（DeepLTranslator 会自动
# 建）。跑它会把整份语料发到 DeepL 并消耗字符额度，所以只在给了 key 时才跑。
if os.environ.get("DEEPL_API_KEY"):
    ENGINES.append(("DeepL + 原生术语表", DeepLTranslator, "pairs"))
else:
    print("（未设置 DEEPL_API_KEY，跳过 DeepL）")

# 今天照着测试语料新加/改过的词条：这些是「调过参」的，必须和原有词条分开看，
# 否则会把自己的调参成果当成模型能力。差距恰恰全在这一组——它们都是多词短语，
# 而促销条件、价格框架正是多词短语。
TUNED = {"排毒粉", "在做特价", "下单满（金额，美元）", "家人们",
         "日间版还是夜间版", "日间版滴剂", "夜间版滴剂",
         "墨西哥米浆", "蘑菇咖啡", "碧根果", "订单", "冰咖啡"}


async def main():
    g = load("glossary.txt")
    data = sentences(g)
    print("语料: {} 句，共 {} 个待检术语\n".format(
        len(data), sum(len(w) for _, _, w in data)))
    for name, cls, form in ENGINES:
        tr = cls()
        split = {"多词短语（今天新加）": [0, 0], "原有词条（多为商品名）": [0, 0]}
        each = []
        for text, _hint, wants in data:
            gl = (g.translation_hint(text) if form == "str"
                  else g.translation_pairs(text))
            t1 = time.time()
            out = await tr.translate(text, "zh-CN", source="es",
                                     glossary=gl or None) or ""
            each.append((time.time() - t1) * 1000)
            for w in wants:
                key = "多词短语（今天新加）" if w in TUNED else "原有词条（多为商品名）"
                split[key][1] += 1
                if term_present(w, out):
                    split[key][0] += 1
        each.sort()
        hit = sum(h for h, _ in split.values())
        tot = sum(t for _, t in split.values())
        print("{:<20} 总计 {:>3}/{:<3} = {:5.1f}%   中位 {:.0f}ms  P95 {:.0f}ms".format(
            name, hit, tot, 100.0 * hit / max(tot, 1),
            each[len(each) // 2], each[int(len(each) * 0.95)]))
        for key, (h, t) in split.items():
            print("     {:<14} {:>3}/{:<3} = {:5.1f}%".format(
                key, h, t, 100.0 * h / max(t, 1)))
        await tr.close()
    print()


asyncio.run(main())
