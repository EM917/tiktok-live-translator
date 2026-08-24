"""模糊匹配必须逐词比对，不能把整个短语拼成一个字符串比。

实盘误报：主播说 "les va a bajar los cupones de descuento"（把折扣券降下来），
被判成命中 `bajar kilos`。整串相似度 "bajar los" vs "bajar kilos" = 0.900，
越过 0.86 阈值——公共动词 `bajar` 把分数抬了上去，而真正有区别的那个词
（los / kilos）毫不相干。`bajar` 在带货话术里到处都是（bajar precios、
bajar cupones、bajar la app），所以所有 `bajar …` 的词条都会对任意
「bajar 什么」误报。

误报的代价不只是烦：报错几次之后，中控就不再相信红色警报了——那等于漏报。
"""
from app.detector import BannedTermDetector

TERMS = ["bajar kilos", "bajar de peso", "perder kilos", "adelgazar",
         "quemagrasas", "reduce tallas"]


def hits(text):
    return [(h["term"], h["tier"]) for h in BannedTermDetector(TERMS).scan(text)]


def test_the_real_false_positive_is_gone():
    assert hits("les va a bajar los cupones de descuento") == []


def test_other_common_bajar_phrases_do_not_fire():
    """bajar 是高频动词，不能一沾就报。"""
    for text in ("vamos a bajar los precios hoy",
                 "pueden bajar la aplicacion gratis",
                 "hay que bajar el aire acondicionado"):
        assert hits(text) == [], text


def test_genuine_matches_still_fire():
    """减少误报不能以漏报为代价——漏报是这个工具最不能犯的错。"""
    assert hits("quiero bajar kilos rapido")
    assert hits("esto ayuda a bajar de peso")
    assert hits("vas a perder kilos en un mes")
    assert hits("reduce tallas en dos semanas")


def test_asr_letter_errors_still_caught():
    """模糊级存在的理由是 ASR 听错一两个字母，这个能力要保住。"""
    assert hits("es para adelgasar")          # adelgazar 少一个 z
    assert hits("producto quemagrasa")        # quemagrasas 少一个 s


def test_short_words_require_an_exact_match():
    """三四个字母的词做模糊匹配撞车率太高——ASR 听错的通常是长词。"""
    d = BannedTermDetector(["cura"])
    assert [h["term"] for h in d.scan("la curva de la calle")] == []
