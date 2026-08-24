"""按「合规操作员会不会被误导」给翻译引擎打分。

语料是 2026-08-24 从一场真实西语带货直播抓下来的 18 条字幕，标注来自三名
独立评审逐句复核 + 一轮反驳验证（37 条确认错误，去重后 23 条）。

判据是客观的：期望的中文出现了吗，已知的错误说法还在不在。不做「自然度」
这类主观打分——用户读中文那一行做合规判断，害到他们的是商品名/价格/促销
条件被翻错，不是句子读起来顺不顺。

跑法：python3 tools/bench_translation.py（需要 Ollama 在跑）
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

from app.glossary import load                      # noqa: E402
from app.translator import (                       # noqa: E402
    OllamaGemmaTranslator, OllamaHyMT2Translator,
)

# (类别, 西语原句, 期望出现的中文任一, 禁止出现的错误说法任一)
CASES = [
    ("product", "Ok, entonces ¿cómo le va a hacer? Porque ya pagó la limpieza por separado.",
     ["排毒", "清体"], ["清洁费", "打扫", "清洗"]),
    ("product", "Ahorita me paso a la limpieza para que le agregue azucarito y luego nos pasamos.",
     ["排毒", "清体"], ["去清洗", "去清洁", "打扫"]),
    ("price", "Ahorita todos los tenemos en especial chicos, que otro producto más les gustaría que les ancle ahí en pantalla.",
     ["特价", "优惠"], ["年轻人", "特别是对于"]),
    ("promo", "Si hacen una orden de 30, van a agarrar las gotas gratis, puedes elegir entre día o noche.",
     ["美元", "$"], ["30 瓶", "30瓶", "30 支", "30支", "30 个", "30个", "30 元", "30元"]),
    ("product", "Si hacen una orden de 30, van a agarrar las gotas gratis, puedes elegir entre día o noche.",
     ["日间版", "白天版", "日间款"], []),
    ("product", "Los pocos de nuez, tenemos de coco, horchata, cappuccino, tenemos el tradicional, el de hongos.",
     ["蘑菇", "菌菇"], ["灵芝"]),
    ("product", "Los pocos de nuez, tenemos de coco, horchata, cappuccino, tenemos el tradicional, el de hongos.",
     ["米浆", "欧洽塔"], []),
    ("product", "Que es este de aquí, también tenemos el de nuez de regreso, bueno nomás quedan muy pocos.",
     ["碧根果", "山核桃"], []),
    ("promo", "El de... el de... no pude cancelar. Ocupo ir a sus órdenes y cancelarlo y volver a intentar.",
     ["订单"], ["指示", "命令"]),
    ("promo", "Y volver a intentarlo otra vez. Ocupa ir a sus ordenes, cancelarlo y volver a intentar todo otra vez.",
     ["订单"], ["指示", "命令"]),
    ("promo", "Ya no se le va a aplicar ni las gotas ni el cupón.",
     ["滴剂"], []),
    ("product", "Vamos a la limonada. Igual si quiere irse al carrito, si quiere irse al carrito naranja, ahí también las va a encontrar todos los productos.",
     ["小黄车", "购物车"], []),
]

ENGINES = [
    ("TranslateGemma 4B", OllamaGemmaTranslator, "str"),
    ("Hy-MT2 1.8B", OllamaHyMT2Translator, "pairs"),
]


def score(out, expect, forbid):
    if expect and not any(e in out for e in expect):
        return False
    return not any(f in out for f in forbid)


async def main():
    g = load("glossary.txt")
    for name, cls, form in ENGINES:
        tr = cls()
        by_cat, fails = {}, []
        t0 = time.time()
        for cat, text, expect, forbid in CASES:
            gl = (g.translation_hint(text) if form == "str"
                  else g.translation_pairs(text))
            out = await tr.translate(text, "zh-CN", source="es",
                                     glossary=gl or None) or ""
            out = g.apply(text, out)
            ok = score(out, expect, forbid)
            hit, tot = by_cat.get(cat, (0, 0))
            by_cat[cat] = (hit + (1 if ok else 0), tot + 1)
            if not ok:
                fails.append((cat, expect[0] if expect else "", out[:62]))
        total_hit = sum(h for h, _ in by_cat.values())
        total = sum(t for _, t in by_cat.values())
        ms = (time.time() - t0) / len(CASES) * 1000
        print("{:<20} {:>2}/{:<2} = {:5.1f}%   均 {:.0f}ms".format(
            name, total_hit, total, 100.0 * total_hit / total, ms))
        for cat in sorted(by_cat):
            h, t = by_cat[cat]
            print("    {:<9} {}/{}".format(cat, h, t))
        for cat, want, out in fails:
            print("    ✗ [{}] 期望「{}」→ {}".format(cat, want, out))
        print()
        await tr.close()


asyncio.run(main())
