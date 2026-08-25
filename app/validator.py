"""译文可信度校验：模型犯错之后，让系统自己知道这一句不可信。

在此之前，翻译只有两种结局——返回了就显示，没返回就显示失败。而实测 200 条
真实字幕里，默认档有 14% 的译文会让中控**理解错**却看不出异常。价格那一档最
严重（23%），偏偏也是最不能错的一档。

这一层不判断译文好不好，只判断**有没有客观可查的破绽**。分三层是因为它们的
确定性完全不同，混在一起会让整层不可用：

  hard     确定的错。数字凭空消失或多出来、货币单位丢了、特殊 token 漏出来、
           空输出。这一层要做到接近零误报，因为它有资格直接判失败。
  commerce 数字都在、但**关系被改坏**。`una por 20 o dos por 35` 译成
           「20个一个，35个两个」——四个数字一个不少，单纯的数字保全查不出来。
           这一层把源文里的商业事实解析成结构再比对。
  suspect  不确定，但值得复核。源文没有 descuento / cupón，译文却冒出「折扣」
           「优惠码」——`De 25 dólares` 变成「25美元折扣」就是这么来的。
           这一层**不判错**，只建议升级到强引擎重译。

刻意选择高精确、低召回：漏掉的那些和现在一样（照旧显示），而误报会把正常字幕
成批推给强引擎，既放大延迟又和 Whisper 抢内存——识别在报警链路上。
"""
import re

# 特殊 token 残留见 translator.py 的清理正则；这里只判断有没有，不负责清
_LEAK = re.compile(r"[｜｠]|▁|hy[-_][A-Za-z]|end.of.(message|sentence)")
_DIGITS = re.compile(r"\d+(?:[.,]\d+)?")
_CURRENCY_ES = re.compile(r"\$|d[óo]lar|peso|usd", re.I)
_CURRENCY_ZH = re.compile(r"美元|美金|元|块|刀|＄|\$")
_PERCENT = re.compile(r"%|por ciento", re.I)
# 量词：价格后面直接跟量词，说明模型把钱读成了件数
_COUNTER = re.compile(r"^(个|件|瓶|杯|包|份|次|支|盒|袋|片|颗|单位|订单|张|条|滴)")
_CN_NUM = {"零":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,
           "八":8,"九":9,"十":10}
# 西语数词也要算进源文的数字池。否则 "una por 20 o dos por 35" 正确译成
# 「一件20，两件35」会被判成「凭空多出数字 1 和 2」——第一版最不能出的就是
# 这种把正确译文判错的误报。

_ES_QTY = {"una":1,"uno":1,"un":1,"dos":2,"tres":3,"cuatro":4,"cinco":5,
           "seis":6,"siete":7,"ocho":8,"nueve":9,"diez":10,"veinte":20,
           "treinta":30,"cuarenta":40,"cincuenta":50,"cien":100}
# 数字池里**不能收 un / una / uno**：它们绝大多数时候是不定冠词而不是数词。
# 实测教训：收了之后 "Si quieres un café"（你想要一杯咖啡）的正确译文会被判成
# 「源文的数字 1 在译文里没有了」——600 条里误伤 46 条正确译文，精确率 9%。
# 「一个还是两个」这种真的在计数的场合由下面的 _BUNDLE 单独解析。
_ES_COUNTABLE = {k: v for k, v in _ES_QTY.items() if k not in ("un", "una", "uno")}
_ES_WORD_NUM = re.compile(r"\b(" + "|".join(_ES_COUNTABLE) + r")\b", re.I)
# "una por 20 o dos por 35" —— 数量与单价的绑定关系
_BUNDLE = re.compile(
    r"\b(una|uno|un|dos|tres|cuatro|cinco|\d+)\s+por\s+(\d+(?:[.,]\d+)?)", re.I)
# "en órdenes de 40" / "una orden de 30" —— 门槛金额，不是件数
_THRESHOLD = re.compile(r"[óo]rden(?:es)?\s+de\s+(\d+)", re.I)
# 源文里没有这些概念，译文却冒出来，多半是模型自己补的
_INVENTED = [
    (re.compile(r"折扣|打折"), re.compile(r"descuento|descontar|oferta|rebaja|off\b", re.I)),
    (re.compile(r"优惠码|优惠券|代金券"), re.compile(r"cup[óo]n|c[óo]digo|coupon", re.I)),
    (re.compile(r"免费|赠送|赠品"), re.compile(r"gratis|regal|free|obsequio", re.I)),
]


def _cn_to_int(s):
    """把「二十」「三十五」这类中文数字读成整数；读不出来返回 None。"""
    if not s or any(c not in _CN_NUM for c in s):
        return None
    if "十" not in s:
        return _CN_NUM.get(s[0]) if len(s) == 1 else None
    head, _, tail = s.partition("十")
    return (_CN_NUM[head] if head else 1) * 10 + (_CN_NUM[tail] if tail else 0)


# 量级词。中文按「万」进位，西语按 mil / millón 进位——两边的进位单位不同，
# 不换算就会把正确的译文判错：`20 millones` 的正确译法就是「2000万」，
# 字面比对下 20 和 2000 对不上（实测误伤 3 条正确译文）。
_SCALE_ZH = {"万": 10**4, "亿": 10**8, "千": 10**3, "百": 10**2}
_SCALE_ES = {"mil": 10**3, "millon": 10**6, "millones": 10**6, "millón": 10**6}


def _scaled(num, tail):
    """数字后面若跟着量级词，换算成实际数值。"""
    t = tail.lstrip()
    for word, mult in _SCALE_ZH.items():
        if t.startswith(word):
            return num * mult
    low = t.lower()
    # 长的先试：`millones` 会被 `mil` 的前缀吃掉，20 millones 就成了 20000。
    for word in sorted(_SCALE_ES, key=len, reverse=True):
        if low.startswith(word):
            return num * _SCALE_ES[word]
    return num


def _numbers(text, cn=False, es=False):
    """文本里出现的数值。

    cn=True 时把中文数字也读进来（用于译文），es=True 时把西语数词也读进来
    （用于源文）。两边都放宽才能避免「正确译文被判成凭空多出数字」。
    """
    out = set()
    for m in _DIGITS.finditer(text):
        raw = m.group(0).replace(",", ".")
        # 试过「跳过专有名词里的数字」（频道名 "Queens 3"），但西语句首本来就
        # 大写，`Tienen 10 calorías` 也被跳掉了——救回 2 条却新伤 3 条，净亏，
        # 所以不做。
        try:
            v = _scaled(float(raw), text[m.end():m.end() + 12])
        except ValueError:
            out.add(raw)
            continue
        # 带量级词时**只**收换算后的值：源文 "20 millones" 的正确译法是
        # 「2000万」，两边都收原值的话 20 永远配不上，会把正确译文判错。
        out.add(raw if v == float(raw) else "{:g}".format(v))
    if cn:
        for m in re.finditer(r"[零一二两三四五六七八九十]+", text):
            v = _cn_to_int(m.group(0))
            if v is not None:
                sv = _scaled(v, text[m.end():m.end() + 4])
                out.add(str(v) if sv == v else "{:g}".format(sv))
    if es:
        for m in _ES_WORD_NUM.finditer(text):
            v = _ES_COUNTABLE[m.group(1).lower()]
            sv = _scaled(v, text[m.end():m.end() + 12])
            out.add(str(v) if sv == v else "{:g}".format(sv))
    return out


def _same(a, b):
    """35 与 35.0 与 35,00 算同一个数。"""
    try:
        return abs(float(a) - float(b)) < 0.01
    except ValueError:
        return a == b


def _present(value, pool):
    return any(_same(value, p) for p in pool)


def check(source, translated, glossary=None):
    """返回 [(层级, 规则名, 说明)]；空列表表示没查出破绽。

    层级是 "hard" / "commerce" / "suspect"，规则名用来把影子数据按**原因**
    拆开统计。只按层看会把好规则和坏规则捆在一起——实测 commerce 层
    6 标 6 中零误伤，而 hard 层里各条规则的精确率差得很远。最终多半不是
    「整层开关」，而是挑出在实盘里稳住高精确的那几条规则单独放行。
    """
    found = []
    if not (translated or "").strip():
        return [("hard", "empty_output", "译文为空")]
    if _LEAK.search(translated):
        found.append(("hard", "token_leak", "译文里残留了模型的特殊 token"))

    src_nums = _numbers(source, es=True)
    out_nums = _numbers(translated, cn=True)
    # 「多出来的数字」只看阿拉伯数字：中文里「三件套」「两个」这类量化说法
    # 本来就会带出数词，拿它判错会误伤大量正确译文。
    extra_pool = _numbers(translated)
    for n in src_nums:
        if not _present(n, out_nums):
            found.append(("hard", "missing_number", "源文里的数字 {} 在译文里没有了".format(n)))
    for n in extra_pool:
        if not _present(n, src_nums):
            found.append(("hard", "invented_number", "译文里的数字 {} 是源文没有的".format(n)))
    if _CURRENCY_ES.search(source) and not _CURRENCY_ZH.search(translated):
        found.append(("hard", "missing_currency", "源文提到金额，译文里没有货币单位"))
    if _PERCENT.search(source) and "%" not in translated and "百分" not in translated:
        found.append(("hard", "missing_percent", "源文的百分比在译文里没有了"))

    # ---- 商业结构：数字都在，但关系被改坏 ----
    for m in _BUNDLE.finditer(source):
        qty_raw, price = m.group(1).lower(), m.group(2)
        qty = str(_ES_QTY.get(qty_raw, qty_raw))
        if not (_present(qty, out_nums) and _present(price, out_nums)):
            continue
        for pm in re.finditer(re.escape(price.split(".")[0]), translated):
            tail = translated[pm.end():pm.end() + 4].lstrip()
            if _COUNTER.match(tail):
                found.append(("commerce", "price_read_as_quantity",
                              "「{} por {}」里的单价 {} 在译文里被当成了件数（后面跟着「{}」）"
                              .format(qty_raw, price, price, tail[0])))
                break
    for m in _THRESHOLD.finditer(source):
        amount = m.group(1)
        for pm in re.finditer(re.escape(amount), translated):
            tail = translated[pm.end():pm.end() + 4].lstrip()
            if _COUNTER.match(tail) and not _CURRENCY_ZH.match(tail):
                found.append(("commerce", "threshold_read_as_count",
                              "「orden de {}」是门槛金额，译文却把 {} 读成了「{}」"
                              .format(amount, amount, tail[0])))
                break

    # ---- 可疑：源文没有的商业概念 ----
    for zh_re, es_re in _INVENTED:
        m = zh_re.search(translated)
        if m and not es_re.search(source):
            found.append(("suspect", "invented_promo_concept",
                          "源文没提到，译文却出现了「{}」".format(m.group(0))))
    return found


LEVELS = ("hard", "commerce", "suspect")


def verdict(findings, escalate_from="commerce", rules=None):
    """把 findings 归结成一个动作：ok / escalate。

    默认 hard 与 commerce 触发升级，suspect 只记录——第一版宁可少抓，
    也不能把正常字幕成批推给强引擎（那会和 Whisper 抢内存，而识别在报警链路上）。

    `rules` 给的是一份白名单，只有名字在里面的规则才有资格触发升级。等实盘影子
    数据按规则拆开之后，多半会走这条路：放行那几条稳住高精确的，其余继续观察。
    """
    if not findings:
        return "ok"
    if rules is not None:
        return "escalate" if any(rule in rules for _lv, rule, _w in findings) else "ok"
    idx = LEVELS.index(escalate_from)
    return "escalate" if any(LEVELS.index(lv) <= idx for lv, _r, _w in findings) else "ok"
