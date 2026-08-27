"""称呼摘除：让模型根本看不到那个词。

起因是一类靠收窄词表修不掉的错。词表提示只是建议，模型可以把它贴到任何位置：
    paquetería amorcito  →  爱邮递员
    ¡Vamos amorcitos!    →  亲爱的人
    listo amor           →  爱了
只要那个词进了模型就有机会粘到旁边的名词上。所以翻译前摘掉它。

这招只对称呼成立——称呼不承载内容，中控不看也读得懂。商品名、价格、功效
一个字都不能这样处理。
"""
from app.vocative import strip


def test_a_trailing_vocative_is_removed():
    out, hit = strip("No se está actualizando mi reina, no sé por qué no lo ha "
                     "recogido paquetería amorcito.")
    assert hit
    assert "amorcito" not in out and "mi reina" not in out
    assert "paquetería" in out and "actualizando" in out


def test_the_sentence_stays_readable_after_removal():
    """摘完不能留下「词 ，」或者连着的标点。"""
    assert strip("llevar tres productos mi niña")[0] == "llevar tres productos"
    assert strip("¿Qué otro producto quieres que te ancle, reinas?")[0] == (
        "¿Qué otro producto quieres que te ancle?")
    assert strip("¡Vamos amorcitos!")[0] == "¡Vamos!"


def test_the_same_words_carrying_meaning_are_left_alone():
    """摘错是丢信息，比译得不漂亮严重。这几条都必须原样留下。"""
    for keep in ("Le ayuda mucho a mi mami.",
                 "Sintiéndome como una reina. Es rico.",
                 "mi amor por este producto es real",
                 "Esta crema hermosa cuesta 20 dólares."):
        assert strip(keep) == (keep, False), keep


def test_position_not_the_word_list_does_the_disambiguation():
    """一度把 hermosa 排除在名单外，怕它是形容词。1727 句语料里它出现在小句
    边界的每一次都是称呼——真正在消歧的是位置规则。句中的用法碰都碰不到。"""
    assert strip("Ahí les puedes escribir hermosa, ¿qué pasó?")[1]
    assert strip("Esta crema hermosa cuesta 20 dólares.") == (
        "Esta crema hermosa cuesta 20 dólares.", False)


def test_a_vocative_without_the_comma_is_left_alone():
    """ASR 经常漏标点。漏了就摘不到——宁可漏摘，不能摘错。"""
    line = "Ahí les puedes escribir amor qué fue lo que pasó con tu paquete."
    assert strip(line) == (line, False)


def test_a_line_that_is_only_a_vocative_is_left_alone():
    """摘到只剩标点就别摘——空文本送进翻译器只会换回空译文。"""
    assert strip("¡Amorcitos!") == ("¡Amorcitos!", False)


def test_the_operator_still_sees_the_untouched_spanish(monkeypatch):
    """摘的只是送给翻译器的那一份。屏幕上的原文、审计、违禁词检测都必须是
    未经改动的文本——报警跑在原文上，这条链路不能被翻译预处理碰。"""
    import asyncio

    from app import pipeline as P

    seen = {}

    class FakeTr:
        async def translate(self, text, target, source="auto", glossary=None):
            seen["sent"] = text
            return "译文"

    p = P.Pipeline.__new__(P.Pipeline)
    p.translator = FakeTr()
    p.glossary = None
    p.audit = None
    p.telemetry = type("T", (), {"record_translation": lambda self, ms: None})()

    async def publish(*a, **k):
        pass

    p._publish_translation = publish
    original = "llevar tres productos mi niña"
    job = {"id": 1, "text": original, "target": "zh-CN", "lang": "es",
           "audio_end_ts": 0.0}
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        p._translate_and_update(job))

    assert seen["sent"] == "llevar tres productos"     # 翻译器收到的是摘过的
    assert job["text"] == original                     # 原文一个字没动
