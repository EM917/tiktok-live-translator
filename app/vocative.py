"""把对观众的称呼从**送去翻译的那份文本**里摘掉。

起因是一类修不掉的错：词表提示只是个建议，模型可以把它贴到任何位置上。
把 `amorcito` 映射成中文称呼之后，实录里出现了

    paquetería amorcito   →  爱邮递员
    ¡Vamos amorcitos!     →  亲爱的人
    listo amor            →  爱了

收窄映射范围只能减少机会，不能根除——只要那个词进了模型，它就有可能粘到旁边
的名词上。所以改成**根本不让它进去**：翻译前摘掉，模型看不见，也就贴不上。

这招只对称呼成立，因为称呼是可摘除的：它不承载内容，只标记「在跟谁说话」，
中控不看这个也完全读得懂。商品名、价格、功效一个字都不能这样处理。

**摘的只是送给翻译器的那一份。** 屏幕上的西语原文、审计日志、违禁词检测
统统用未经改动的原文——报警跑在原文上，这条链路不能被翻译预处理碰。
"""
import re

# 一度把 hermosa / preciosa 排除在外，怕它们是真的形容词（"Esta crema es
# hermosa"）。查了 1727 句语料：这几个词**出现在小句边界的每一次都是称呼**，
# 零例外。做消歧的其实是下面那条位置规则，不是名单本身——`hermosa` 总共出现
# 9 次，其中 2 次在句中（可能是形容词），而那 2 次本来就碰不到。
#
# mami 仍然不收，但理由不同：实录里 "Le ayuda mucho a mi mami" 真的是在说
# 妈妈，而且它就在小句边界上——那是位置规则挡不住的一例。
VOCATIVES = ("mi niña", "mi reina", "mi amor", "mi vida", "mi gente",
             "amorcito", "amorcitos", "amor", "reinas",
             "hermosa", "hermosas", "preciosa", "preciosas", "precioso")

# 必须紧跟小句边界。ASR 输出经常没有标点，所以只认右边界不认左边界：
# 实录里称呼几乎总在小句末尾（`paquetería amorcito.`、`ahorita mi niña.`）。
# 这样 "mi amor por este producto es real" 里的 mi amor 不会被误摘。
_RE = re.compile(
    r"(?<![\wáéíóúñÁÉÍÓÚÑ])(?:{})(?![\wáéíóúñÁÉÍÓÚÑ])\s*(?=[,;.!?¡¿]|$)".format(
        "|".join(re.escape(v) for v in VOCATIVES)), re.I)


def strip(text):
    """返回 (摘除后的文本, 是否摘过)。摘不动就原样返回。"""
    if not text:
        return text, False
    out = _RE.sub("", text)
    if out == text:
        return text, False
    out = re.sub(r"\s+(?=[,;.!?])", "", out)     # 摘完留下的「词 ，」
    out = re.sub(r",\s*(?=[,.;!?])", "", out)    # 连在一起的标点
    out = re.sub(r"\s{2,}", " ", out).strip(" ,")
    # 摘到只剩标点就别摘了——空文本给翻译器只会得到空译文
    return (out, True) if re.search(r"[\wáéíóúñ]", out) else (text, False)
