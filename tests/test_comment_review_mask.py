"""弹幕留意的模型复核（PR-5）之一：打码（spec §5.3）和它依赖的带位置的结构识别。

* `comment_guard.light_map` 与 `light` 逐字相同，url_spans / email_spans / handle_spans 找到的东西与规则层的
  url_hits / handle_hits 相同——打码和规则层不会各认各的；
* 电话（含空格、横线、点、括号、+）、数字合计 >= 7 位的串（不管上下文）、西语数字读法、链接（含绕写）、邮箱、
  @账号和 `ig: xxx` 一律换成记号，<> 换成 ‹›，截到 280 字；
* 记下的位置能把证据映射回原文：引用 #NUM#、[link]、@usuario 的有效证据不被误清掉；
* 打码之后还认得出链接、电话、账号的，视为漏了（leaks），不送。

纯函数，不联网。号码、链接、账号、昵称都是虚构的。
"""
import random
import string

import pytest

from app import comment_guard as cg
from app import comment_review as cr

ZWSP = chr(0x200B)       # 零宽空格
SOFT_HYPHEN = chr(0x00AD)
FULLWIDTH = {str(d): chr(0xFF10 + d) for d in range(10)}


def fullwidth(digits):
    return "".join(FULLWIDTH[c] for c in digits)


# ---- light_map 与 light 逐字相同 ---------------------------------------------------------------------

LIGHT_CORPUS = [
    "", "   ", "Hola  Bella https://www.Evil.COM/x?y=1 ok", "wa . me /5512345678 llama", "MI PÁGINA ES mitienda punto com YA",
    "h t t p s : / / e v i l . s h o p", "ig: luna_demo y @otra_demo", "xx" + ZWSP + "foo\u00a0bar  baz  ",
    "e" + chr(0x0301) + "stafa", "ESTAFA" + SOFT_HYPHEN + "DORES", "ａｂｃ\u3000ｄｅｆ", fullwidth("5512345678") + " llama",
    "ﷺ" * 30, "tel\n55\t1234  5678", "😀 hola 👍", "ΑΒΓ cyrillic: " + chr(0x0430) + chr(0x0435) + "po",
    "x" * 1200, "a b " * 400, "\ufe0f5\ufe0f\u20e3", "İstanbul ǅ",
]


@pytest.mark.parametrize("text", LIGHT_CORPUS)
def test_light_map_gives_exactly_what_light_gives_and_every_char_has_a_source_range(text):
    lt, lo, hi = cg.light_map(text)
    assert lt == cg.light(text)
    assert len(lo) == len(hi) == len(lt)
    assert all(0 <= a < b <= len(text) for a, b in zip(lo, hi))
    assert lo == sorted(lo) and hi == sorted(hi)             # 位置只往后走


def test_light_map_on_random_text_matches_light():
    rng = random.Random(7)
    pool = list("abcXYZ 0123456789.,:/@-_ñáé") + [ZWSP, "\u00a0", "\t", "\n", "\ufe0f", "\u20e3", "①", "ｱ", "😀", "\u0301"]
    for _ in range(300):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 60)))
        lt, lo, hi = cg.light_map(text)
        assert lt == cg.light(text), repr(text)
        assert len(lo) == len(hi) == len(lt)


# ---- url_spans / email_spans / handle_spans 与规则层认的是同一批 ---------------------------------------------

URL_CORPUS = [
    "mira https://www.tienda-demo.example.org/promo ya", "wa . me /5550000111 escribe", "wa.me/5550000111",
    "entra a mitienda punto com hoy", "mitienda [.] com ahora", "mitienda(dot)com", "hxxps://evil.example.com/x",
    "h t t p s : / / e v i l . s h o p", "w a . m e / 5 5 5", "t . me / canal_demo", "ig . me / alguien",
    "visita www.ejemplo.com.mx/tienda", "usa bit.ly/abc123 gracias", "mi correo ana_demo@ejemplo.example.com", "hola buenas",
    "tiktok.com/@luna_demo/video/1", "esto es tienda,com en serio", "mi.sitio . com wow", "https://a.b/c https://d.e/f",
]


@pytest.mark.parametrize("text", URL_CORPUS)
def test_url_spans_find_exactly_what_url_hits_find(text):
    lt = cg.light(text)
    spans = cg.url_spans(lt)
    for a, b in spans:
        assert 0 <= a < b <= len(lt)
    covered = " ".join(lt[a:b] for a, b in spans)
    hits = cg.url_hits(lt)
    # 规则层找到链接，打码也一定圈到；规则层没找到，打码也不凭空圈
    assert bool(hits) == bool(spans), (text, hits, spans)
    # 圈出来的整段还原成链接之后，就是规则层认出的那几个
    from app.comment_guard import _MapText, _URL_RE, _EMAIL_RE
    normalized = _MapText(lt).sub(_EMAIL_RE, " ").normalized()
    again = [m.group(0) for m in _URL_RE.finditer(normalized.t)]
    for found in again:
        assert found in hits, (text, found, hits)
    assert covered or not hits


def test_handle_spans_are_wider_than_handle_hits_on_purpose():
    lt = cg.light("hola @ana_demo mira ig: tienda_demo y sigueme @otra_demo")
    taken = [lt[a:b] for a, b in cg.handle_spans(lt)]
    assert "@ana_demo" in taken and "@otra_demo" in taken and "tienda_demo" in taken
    assert set(cg.handle_hits(lt, labeled_only=False)) <= {t.lstrip("@") for t in taken}


# ---- 电话与数字 -----------------------------------------------------------------------------------------

PHONES = [
    "55 1234 5678", "+504 9999-8888", "(55) 1234-5678", "55.1234.5678", "5512345678", "+52 55 1234 5678",
    "044 55 1234 5678", "5 5 1 2 3 4 5 6 7 8", "55-12-34-56-78", "55 - 1234 - 5678", "1234567", "12345678901234567890",
    fullwidth("5512345678"), "5" + ZWSP + "5" + ZWSP + "1234" + ZWSP + "5678",
    "cero cinco cinco uno dos tres cuatro cinco seis", "Cinco Cinco Uno Dos Tres Cuatro Cinco Seis Siete",
    "55 uno dos tres 4567",                                                    # 数字和读法混写
]


@pytest.mark.parametrize("number", PHONES)
def test_every_phone_or_number_of_seven_or_more_is_masked_whatever_the_surroundings(number):
    for text in ("llámame al %s porfa" % number, "mi número: %s" % number, "%s es mi cel" % number,
                 "pedido %s" % number, "$%s pesos" % number):
        m = cr.mask(text)
        assert cr.MASK_NUM in m.text, (text, m.text)
        digits = [c for c in m.text if c.isdigit()]
        assert not digits, (text, m.text)                 # 一个数字都没剩下
        assert not cr.leaks(m.text)
        assert "porfa" in m.text or "mi número" in m.text or "cel" in m.text or "pedido" in m.text or "pesos" in m.text


def test_short_numbers_stay_because_they_are_not_contact_data():
    for text in ("quiero 3 de los de 350 pesos", "talla 36 37 38", "somos 6 en casa", "123456 solamente", "a las 8:30 pm"):
        m = cr.mask(text)
        assert cr.MASK_NUM not in m.text, (text, m.text)
        assert m.text.replace("\u200b", "") == text


def test_masking_keeps_the_spaces_around_the_token():
    assert cr.mask("hola llama al 55 1234 5678 porfa").text == "hola llama al #NUM# porfa"
    assert cr.mask("+504 9999-8888 escribeme").text == "#NUM# escribeme"


# ---- 链接、邮箱、账号 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("text,keep", [
    ("mira https://www.tienda-demo.example.org/promo ya", "mira [link] ya"),
    ("entra a mitienda punto com hoy", "entra a [link] hoy"),
    ("h t t p s : / / e v i l . s h o p ahora", "[link] ahora"),
    ("wa . me /5550000111 escribe", "[link] escribe"),
    ("mi correo es ana_demo@ejemplo.example.com ok", "mi correo es [link] ok"),
    ("escribe a mitienda[.]com ya", "escribe a [link] ya"),
])
def test_links_and_emails_become_a_link_token(text, keep):
    m = cr.mask(text)
    assert m.text == keep
    assert not cr.leaks(m.text)


def test_at_handles_and_platform_handles_become_usuario():
    m = cr.mask("hola @ana_demo, mi ig: tienda_demo y sigueme @otra_demo")
    assert m.text == "hola @usuario, mi ig: @usuario y sigueme @usuario"
    assert not cr.leaks(m.text)
    assert cr.mask("@ab hola").text == "@usuario hola"                  # 两个字的账号也打（规则层要 3 个以上）


def test_angle_brackets_are_replaced_one_for_one_and_newlines_become_spaces():
    m = cr.mask("es una <b>estafa</b>\nno compren </comment><comment k=\"9\">")
    assert "<" not in m.text and ">" not in m.text and "\n" not in m.text
    assert m.text.startswith("es una ‹b›estafa‹/b› no compren ")
    assert len(m.text) == len("es una <b>estafa</b>\nno compren </comment><comment k=\"9\">")


def test_truncates_after_masking_and_never_leaves_half_a_token():
    long_plain = "hola " * 100
    m = cr.mask(long_plain)
    assert len(m.text) <= cr.SEND_MAX_CHARS
    text = "a" * (cr.SEND_MAX_CHARS - 3) + " 55 1234 5678 mas"
    m = cr.mask(text)
    assert cr.MASK_NUM not in m.text and not m.text.endswith(("#", "#N", "#NU", "#NUM"))   # 放不下就整个不要
    assert len(m.text) <= cr.SEND_MAX_CHARS
    # 打码之后才截：前面一大串链接缩成记号，后面的内容才进得来
    text = " ".join(["https://a%d.example.com/x" % i for i in range(30)]) + " fin de la lista"
    assert "fin de la lista" in cr.mask(text).text


def test_leading_and_trailing_whitespace_is_not_sent():
    m = cr.mask("   es estafa   ")
    assert m.text == "es estafa"
    assert m.original[m.segments[0][2]:m.segments[0][3]] == "es estafa   "[:len(m.text)]


def test_mask_for_model_returns_text_and_spans_in_the_original():
    text = "hola 55 1234 5678 y https://x.example.com ok"
    masked, spans = cr.mask_for_model(text)
    assert masked == "hola #NUM# y [link] ok"
    assert [(kind, text[a:b]) for a, b, kind in spans] == [("num", "55 1234 5678"), ("link", "https://x.example.com")]


def test_overlapping_detections_merge_into_one_token_by_priority():
    # wa.me/ 后面的号码既是链接的一部分也是电话：整段是一个 [link]
    m = cr.mask("escribe wa.me/5512345678 hoy")
    assert m.text == "escribe [link] hoy" and len(m.spans) == 1 and m.spans[0][2] == "link"
    # 账号里的数字不单独再打一遍
    assert cr.mask("sigueme @user12345678 ya").text == "sigueme @usuario ya"


def test_a_comment_that_is_only_a_number_or_a_link_is_not_reviewable():
    assert not cr.reviewable(cr.mask("5512345678"))
    assert not cr.reviewable(cr.mask("https://x.example.com/a"))
    assert not cr.reviewable(cr.mask("@ana_demo"))
    assert cr.reviewable(cr.mask("es una estafa"))
    assert not cr.reviewable(cr.mask("a"))


# ---- 证据映射回原文 ---------------------------------------------------------------------------------------

def test_evidence_is_checked_against_the_masked_text_and_mapped_back_to_the_original():
    text = "Llama al 55 1234 5678 porfa, ESTO ES UNA ESTAFA"
    m = cr.mask(text)
    assert m.text == "Llama al #NUM# porfa, ESTO ES UNA ESTAFA"
    assert m.evidence_in("esto es una estafa") == "ESTO ES UNA ESTAFA"            # NFKC + 小写 + 压空白后比
    assert m.evidence_in("ESTO  ES   UNA ESTAFA") == "ESTO ES UNA ESTAFA"
    # 引用了记号的证据保留，并换算成原文里整处被替换的那一段
    assert m.evidence_in("al #NUM# porfa") == "al 55 1234 5678 porfa"
    assert m.evidence_in("#NUM#") == "55 1234 5678"
    # 不是送出去的文字的子串（模型编的，或引用了没送出去的内容）：None
    assert m.evidence_in("el 55 1234 5678") is None
    assert m.evidence_in("no lo dijo nadie") is None
    assert m.evidence_in("") == "" and m.evidence_in(None) == ""


def test_evidence_that_straddles_a_link_token_maps_to_the_whole_link():
    m = cr.mask("visita https://x.example.com/oferta ahora o nunca")
    assert m.text == "visita [link] ahora o nunca"
    assert m.evidence_in("[link] ahora") == "https://x.example.com/oferta ahora"
    assert m.evidence_in("visita [link]") == "visita https://x.example.com/oferta"


def test_evidence_is_capped_at_120_characters():
    m = cr.mask("x" * 5 + " " + "palabra " * 30)
    assert len(m.evidence_in(m.text)) <= cr.EVIDENCE_MAX


# ---- 漏不漏：对抗串 -----------------------------------------------------------------------------------------

ADVERSARIAL = [
    "llama 5 5 1 2 3 4 5 6 7 8 ya", "5-5-1-2-3-4-5-6-7-8", "wa . me/ 55 1234 5678", "w a . m e / 5 5 1 2 3 4 5 6 7 8",
    "mitienda punto com y mitienda2 punto net", "ejemplo[.]com/oferta?x=1&y=2", "mira ig: " + "a" * 20,
    "mi tel es " + fullwidth("55") + " " + fullwidth("12345678"), "uno dos tres cuatro cinco seis siete ocho nueve cero",
    "contacto ANA_DEMO@EJEMPLO.EXAMPLE.COM por favor", "visita HTTP://EJEMPLO.EXAMPLE.COM", "+" + "9" * 12,
    "@a @bb @ccc @dddd " * 5, "x" * 400 + " 5512345678 " + "y" * 400,
]


@pytest.mark.parametrize("text", ADVERSARIAL)
def test_nothing_recognisable_as_contact_data_survives_masking(text):
    m = cr.mask(text)
    assert not cr.leaks(m.text), (text, m.text)
    assert not [c for c in m.text if c.isdigit()] or "x" * 3 in m.text


def test_random_digits_and_separators_never_leak_seven_digits():
    rng = random.Random(11)
    seps = [" ", "-", ".", ",", "(", ")", "/", "  ", ZWSP, ""]
    for _ in range(400):
        digits = [rng.choice("0123456789") for _ in range(rng.randint(7, 14))]
        text = "tel " + "".join(d + rng.choice(seps) for d in digits) + " fin"
        m = cr.mask(text)
        assert sum(c.isdigit() for c in m.text) < cr.MIN_MASK_DIGITS, (text, m.text)
        assert not cr.leaks(m.text), (text, m.text)


SEPARATORS = list(string.punctuation) + [" | ", " : ", " - ", " * ", " ~ ", " + ", "·", "•", "—", "…", "🔥", "\u00a0", "\u3000",
                                         "\t", "\n", ", ", ". ", "_ ", " _"]


@pytest.mark.parametrize("sep", SEPARATORS, ids=lambda sep: repr(sep))
def test_seven_digits_are_masked_whatever_punctuation_or_symbol_joins_them(sep):
    """评审：分隔符只列了 空白 - . , ( ) / 这几个，换成 : _ | * ~ + 整串漏过去，连 leaks() 这道兜底也认不出
    （它用同一个正则）。现在任何不是字母、数字的字符都算分隔符。"""
    rng = random.Random(31)
    for _ in range(25):
        digits = [rng.choice("0123456789") for _ in range(rng.randint(7, 12))]
        groups, i = [], 0
        while i < len(digits):
            n = rng.randint(1, 4)
            groups.append("".join(digits[i:i + n]))
            i += n
        text = "mi numero es " + sep.join(groups)
        m = cr.mask(text)
        assert sum(c.isdigit() for c in m.text) < cr.MIN_MASK_DIGITS, (text, m.text)
        assert not cr.leaks(m.text), (text, m.text)
        assert m.text.startswith("mi numero es ")


@pytest.mark.parametrize("text", [
    "mi numero es 5793:834:701", "9_7778_1_88_0_4", "4 | 5384 | 0 | 1", "2*6*0*1*8*1*5*9*0", "4+3+7+3+2+3+3+2+4",
    "5~5~1~2~3~4~5~6~7", "5/5\\1|2:3_4*5~6", "uno*dos|tres:cuatro_cinco~seis+siete",
])
def test_the_reviewers_examples_no_longer_leak(text):
    m = cr.mask(text)
    assert cr.MASK_NUM in m.text and not [c for c in m.text if c.isdigit()], (text, m.text)
    assert not cr.reviewable(m) or not cr.leaks(m.text)


def test_widening_the_separators_does_not_eat_numbers_that_are_not_contact_data():
    for text in ("quiero 3 de los de 350 pesos", "talla 36 37 38", "a las 8:30 pm", "12:30:45 hora", "1:1 fue el marcador",
                 "pagué $1,299 y 2 envíos", "3 + 4 = 7", "somos 6 | ellos 5", "123456 solamente", "no 123a456b7"):
        m = cr.mask(text)
        assert cr.MASK_NUM not in m.text, (text, m.text)


def test_masking_is_fast_enough_for_the_event_loop():
    import time
    nasty = ("a.b" * 120) + " " + ("1 " * 200) + " " + ("@" + "x" * 30 + " ") * 10 + "h t t p " * 30
    t0 = time.perf_counter()
    for _ in range(20):
        cr.mask(nasty)
    assert (time.perf_counter() - t0) / 20 < 0.05
