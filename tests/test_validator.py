"""译文可信度校验。

存在的理由：200 条真实字幕实测，默认档 14% 的译文会让中控理解错却看不出异常。
这一层不判断译文好不好，只判断有没有**客观可查**的破绽，让系统自己知道
哪一句不可信。

第一版刻意选高精确、低召回：漏掉的和现在一样照旧显示，而误报会把正常字幕成批
推给强引擎，既放大延迟又和 Whisper 抢内存——识别在报警链路上。
600 条人工标注实测：默认升级层标记 11 条，其中 7 条是真的严重错。
"""
from app.validator import check, verdict


def _levels(source, translated):
    return {lv for lv, _rule, _why in check(source, translated)}


def _rules(source, translated):
    return {rule for _lv, rule, _why in check(source, translated)}


# ---- commerce：数字都在，但关系被改坏 --------------------------------

def test_a_unit_price_read_as_a_quantity_is_caught():
    """实录最危险的一类：四个数字一个不少，单纯的数字保全查不出来。"""
    src = "Una por 20 o dos por 35. El envío es completamente gratis."
    assert "commerce" in _levels(src, "20个一个，或35个两个。配送完全免费。")


def test_the_correct_rendering_of_the_same_line_passes():
    """精确度是这一层的命根子：把对的判成错的，就会把正常字幕成批推给强引擎。"""
    src = "Una por 20 o dos por 35. El envío es completamente gratis."
    assert check(src, "一件20，两件35。包邮。") == []
    assert check(src, "买一瓶20元，或者买两瓶35元。") == []


def test_an_order_threshold_read_as_a_count_is_caught():
    """`en órdenes de 40` 是门槛金额，不是四十个订单。"""
    src = "hoy nos está dando cupón en órdenes de 40 o más"
    assert "commerce" in _levels(src, "今天它通过订单给你 40 个或更多的优惠码")
    assert check(src, "今天只要下单满40美元就能拿到优惠码") == []


# ---- hard：确定的错 --------------------------------------------------

def test_an_empty_translation_is_a_hard_failure():
    assert check("hola", "") == [("hard", "empty_output", "译文为空")]


def test_leaked_model_tokens_are_a_hard_failure():
    assert "hard" in _levels("hola", "价格是57.20。<｠end▁of▁message")


def test_a_dropped_price_is_caught():
    assert "hard" in _levels("Te lo llevas por 35 dólares.", "你可以把它带走。")


def test_currency_must_survive():
    assert "hard" in _levels("Menos de 4 dólares.", "不到4个。")
    assert check("Menos de 4 dólares.", "不到4美元。") == []


# ---- 精确度：这些**必须**放行 ----------------------------------------

def test_the_indefinite_article_is_not_a_number():
    """un / una 绝大多数时候是冠词不是数词。收进数字池的那一版，600 条里
    误伤了 46 条正确译文，精确率掉到 9%。"""
    assert check("Si quieres un café, yo te recomiendo el de colágeno.",
                 "如果你想喝咖啡，我推荐含胶原蛋白的那种。") == []
    assert check("En una botellita de agua de estas.", "用这种小瓶装的水。") == []


def test_magnitude_words_are_converted_before_comparing():
    """中文按「万」进位，西语按 millón 进位。不换算的话，`20 millones` 的
    正确译法「2000万」会被判成数字对不上。"""
    assert check("gente que tiene 20 millones de pasos",
                 "有些人的步数竟然有2000万步之多") == []


def test_ordinary_lines_are_left_alone():
    for src, out in [
        ("Gracias por su orden.", "感谢您的订单。"),
        ("El pack de tres que normalmente cuesta 165 se lo llevan en 65.",
         "通常售价165的三件装，现在只需65即可购买。"),
        ("Tienen 10 calorías, tienen fibra, vitamina A,",
         "它们含有10卡路里，还有膳食纤维、维生素A。"),
    ]:
        assert check(src, out) == [], out


# ---- 分层处置 --------------------------------------------------------

def test_suspect_alone_does_not_escalate():
    """源文没提折扣、译文冒出「折扣」值得复核，但这一层实测精确率只有 6%
    （18 标 1 中），开了会把正常字幕成批推给强引擎。只记录，不升级。"""
    src = "De 25 dólares, chicas y chicos, por los próximos 9 minutos"
    found = check(src, "所有商品均享受25美元折扣，接下来9分钟")
    assert {lv for lv, _r, _w in found} == {"suspect"}
    assert verdict(found) == "ok"
    assert verdict(found, escalate_from="suspect") == "escalate"


def test_hard_and_commerce_escalate_by_default():
    assert verdict([("hard", "missing_number", "x")]) == "escalate"
    assert verdict([("commerce", "price_read_as_quantity", "x")]) == "escalate"
    assert verdict([]) == "ok"


def test_every_finding_carries_a_rule_name():
    """影子数据要按**原因**拆开统计，不能只按层。实测各条规则的精确率差得
    很远——commerce 那两条 6 标 6 中，hard 里有的只有 43%。最终多半是挑规则
    放行，而不是整层开关。"""
    found = check("Una por 20 o dos por 35.", "20个一个，或35个两个。")
    assert found and all(len(f) == 3 and f[1] for f in found)
    assert _rules("Una por 20 o dos por 35.", "20个一个，或35个两个。") == {
        "price_read_as_quantity"}
    assert _rules("hola", "") == {"empty_output"}


def test_a_rule_whitelist_can_narrow_what_escalates():
    """等实盘数据出来，多半只放行那几条稳住高精确的规则。"""
    found = check("Una por 20 o dos por 35.", "20个一个，或35个两个。")
    assert verdict(found, rules={"price_read_as_quantity"}) == "escalate"
    assert verdict(found, rules={"token_leak"}) == "ok"


# ---- 影子模式：不能改变屏幕上的任何一条字幕 --------------------------

def test_shadow_mode_never_changes_the_subtitle(monkeypatch, tmp_path):
    """第一版只观察。校验一旦能改字幕，就必须先证明它不会误伤——而误伤的
    代价是把正常字幕成批推给强引擎，那会和 Whisper 抢内存，识别在报警链路上。
    """
    import asyncio

    from app import pipeline as P

    published = []
    audited = []

    class FakeAudit:
        def translation(self, *a, **k):
            pass

        def validation(self, seq, findings):
            audited.append((seq, findings))

    class FakeTr:
        async def translate(self, text, target, source="auto", glossary=None):
            return "20个一个，或35个两个。"        # 已知会被 commerce 层标记

    p = P.Pipeline.__new__(P.Pipeline)
    p.translator = FakeTr()
    p.glossary = None
    p.audit = FakeAudit()
    p.telemetry = type("T", (), {"record_translation": lambda self, ms: None})()

    async def publish(seq, translated, ok, ms, quality, target, extra):
        published.append(translated)

    p._publish_translation = publish
    job = {"id": 7, "text": "Una por 20 o dos por 35.", "target": "zh-CN",
           "lang": "es", "audio_end_ts": 0.0}
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        p._translate_and_update(job))

    assert published == ["20个一个，或35个两个。"]      # 原样上屏，一个字没动
    assert audited and audited[0][0] == 7               # 但记下来了
    assert any(lv == "commerce" for lv, _r, _w in audited[0][1])
