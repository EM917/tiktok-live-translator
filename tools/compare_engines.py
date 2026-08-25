"""把几个翻译引擎放在同一批真实字幕上对比，先看数据再决定要不要换。

判据是客观的：词表要求的中文有没有出现在译文里。本工具的用户读中文那一行做
合规判断，真正会害到他们的是商品名、价格、促销条件被翻错，不是句子顺不顺。

跑法：
    python3 tools/compare_engines.py                    # 本地引擎
    DEEPL_API_KEY=xxx python3 tools/compare_engines.py  # 连 DeepL 一起比

DeepL 会把字幕文本发送到 DeepL 的服务器，这一点在决定是否采用前要先明确。
免费额度 key 以 :fx 结尾，程序会自动走对应域名。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, ".")

from app.glossary import load                       # noqa: E402
from app.translator import create_translator        # noqa: E402
from tools._benchdata import sentences, term_present              # noqa: E402


def engines():
    out = [("Hy-MT2 1.8B", "hymt2"), ("Hy-MT2 7B", "hymt2-7b")]
    if os.environ.get("DEEPL_API_KEY"):
        out.append(("DeepL", "deepl"))
    else:
        print("（未设置 DEEPL_API_KEY，跳过 DeepL）\n")
    return out


async def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    g = load("glossary.txt")
    data = sentences(g)[:limit]
    if not data:
        print("logs/ 里没有带词表命中的字幕，先跑一场直播再来")
        return
    print("语料 {} 句，共 {} 个待检术语\n".format(
        len(data), sum(len(w) for _, _, w in data)))

    for label, choice in engines():
        try:
            tr = create_translator(choice)
        except RuntimeError as exc:
            print("{:<14} 跳过：{}".format(label, exc))
            continue
        hit = tot = fail = 0
        each = []
        misses = []
        for text, _hint, wants in data:
            t0 = time.time()
            out = await tr.translate(text, "zh-CN", source="es",
                                     glossary=tuple(g.translation_pairs(text)) or None)
            each.append((time.time() - t0) * 1000)
            if not out:
                fail += 1
                continue
            out = g.apply(text, out)
            for w in wants:
                tot += 1
                if term_present(w, out):
                    hit += 1
                elif len(misses) < 3:
                    misses.append((w, out[:48]))
        each.sort()
        print("{:<14} 词表遵从 {:>3}/{:<3} = {:5.1f}%   中位 {:.0f}ms   翻译失败 {}".format(
            label, hit, tot, 100.0 * hit / max(tot, 1),
            each[len(each) // 2] if each else 0, fail))
        for w, o in misses:
            print("      漏「{}」→ {}".format(w, o))
        await tr.close()
    print()


asyncio.run(main())
