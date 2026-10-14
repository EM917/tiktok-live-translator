"""弹幕留意规则层（app/comment_guard.py）：第二轮审查找出的缺陷，每条一个回归测试。

每条都对着审查员给的复现例句写（`# #N` 是审查意见的编号），修复前会失败。用 tests/fixtures/comment_guard
的测试词表（红条档全部已确认）；主播名用虚构的 luna_demo，品牌用虚构的 marcaejemplo。纯 CPU，不联网、
不加载模型、不 import main.py。
"""
import json
import math
import os
import random
import time
from pathlib import Path

import pytest

from app import comment_guard as cg
from tests.guard_perf import budget_ms

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "comment_guard"
NO_LOCAL = HERE / "no-such-local-dir"
STREAMER = "luna_demo"
RULES = cg.load_rules(FIX, NO_LOCAL)


def judge(text, user="u1", ts=1000.0, rules=RULES, streamer=STREAMER, **kw):
    return cg.CommentGuard(rules, streamer=streamer).judge(text, user=user, ts=ts, **kw)


def guard(rules=RULES, **kw):
    return cg.CommentGuard(rules, streamer=kw.pop("streamer", STREAMER), **kw)


def write(dirpath, name, text):
    (dirpath / name).write_text(text, encoding="utf-8")
    return dirpath


def small_defaults(tmp_path, **files):
    d = tmp_path / "defaults"
    d.mkdir()
    for name in ("insult", "accuse", "promo", "interact", "cues"):
        write(d, name + ".txt", files.get(name, ""))
    write(d, "thresholds.txt", files.get("thresholds", ""))
    return d


def lvl(text, **kw):
    return judge(text, **kw)["lvl"]


# ------------------------------------------------------------------------------------------
# 电话：单号上下文、日期
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [                                                  # #0
    "telefono 5512345678", "mi telefono: 5512345678", "escribeme al telefono 5512345678",
    "mi celular uno 5512345678",
])
def test_telefono_and_uno_are_not_order_number_labels(text):
    assert cg.phone_hits(cg.light(text)) == ["5512345678"], text


@pytest.mark.parametrize("text", [                                                  # #0
    "no. 5512345678", "pedido #1234567890", "mi numero de guia es 5512345678", "numero de pedido: 5512345678",
    "tracking 5512345678", "seguimiento 5512345678", "order 5512345678", "guia: 5512345678",
    "folio: 5512345678", "nu 5512345678",
])
def test_order_number_labels_still_exclude_the_number(text):
    assert cg.phone_hits(cg.light(text)) == [], text


def test_telefono_with_an_offer_word_is_red_and_alone_is_a_note():                  # #0
    r = judge("escribeme al telefono 5512345678")
    assert (r["lvl"], r["cats"], r["tier"]) == ("alert", ["promo_contact"], "struct")
    r = judge("mi telefono: 5512345678")
    assert (r["lvl"], r["cats"], r["tier"]) == ("note", ["promo_contact"], "struct")


@pytest.mark.parametrize("text,digits", [                                           # #14
    ("55-12-34-56-78", "5512345678"), ("(55) 12-34-56-78", "5512345678"),
    ("12-34-56-78", "12345678"), ("55 1234 5678", "5512345678"), ("044 55 1234 5678", "0445512345678"),
    ("55-01-02-03-04", "5501020304"),                       # 里面的 01-02-03 看着像日期，但它在更长的数字组里
])
def test_dash_grouped_phone_numbers_are_not_eaten_by_the_date_regex(text, digits):
    assert cg.phone_hits(cg.light(text)) == [digits], text


@pytest.mark.parametrize("text", ["12/05/1990", "hoy 12-05-90 es mi cumple", "31/12/2024", "1/2/99"])
def test_real_dates_are_still_not_phone_numbers(text):                              # #14
    assert cg.phone_hits(cg.light(text)) == [], text


def test_dash_grouped_number_with_an_offer_word_is_red():                           # #14
    assert lvl("55-12-34-56-78 escribeme al privado") == "alert"


def test_thirteen_digits_without_a_plus_or_a_044_prefix_are_still_a_tracking_number():   # #14
    assert cg.phone_hits("5215512345678") == []


# ------------------------------------------------------------------------------------------
# 辱骂：否认、estas、自指、puta madre
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [                                                  # #2
    "no eres fea, eres hermosa", "no estas gorda, estas perfecta", "ya no estas gorda felicidades",
    "nunca me pareces fea", "eres tan bella que no pareces gorda", "no te ves gorda",
    "jamas eres fea", "you are not ugly",
])
def test_negated_second_person_target_frames_are_reassurance_not_insults(text):
    r = judge(text)
    assert r["lvl"] is None and not cg.auto_markable(r), text


def test_a_plain_second_person_target_frame_is_still_red():                         # #2
    for text in ("eres fea", "te ves gorda", "eres una gorda", "no se si te importa pero eres fea"):
        r = judge(text)
        assert (r["lvl"], r["tier"]) == ("alert", "red"), text


@pytest.mark.parametrize("text", [                                                  # #8
    "quiero quitarme estas feas manchas", "estas feas ojeras no se quitan", "como quito estas gordas llantas",
    "estas gordas llantitas me tienen mal", "mira estas feas arrugas", "estas feas estrias, que me recomiendas?",
    "que me recomiendan para estas gordas piernas",
    "si estas operada puedes usar la faja?", "sirve para cuando estas operada del estomago?",
    "para cuando estas gorda y quieres bajar rapido", "ur fat burner is the best", "if you are fat this works!",
])
def test_demonstrative_estas_conditionals_and_bare_ur_are_not_target_frames(text):
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", [                                                  # #8
    "estas gorda", "eres gordas", "you are so ugly", "you are really fat", "u r so fat", "ur so ugly",
    "ur such a fat",
])
def test_real_second_person_frames_with_degree_words_are_still_red(text):
    assert lvl(text) == "alert", text


@pytest.mark.parametrize("text", [                                                  # #25
    "que pendeja soy, olvide pagar", "soy una idiota, puse mal mi direccion", "que estupida soy, no vi el codigo",
    "i am such an idiot", "me siento una pendeja",
])
def test_a_strong_word_in_a_first_person_sentence_is_amber_self_and_never_auto_marked(text):
    r = judge(text)
    assert (r["lvl"], r["amb"], r["tags"], r["route"], r["tier"]) == ("amber", "self", ["self"], "confirm", "red"), text
    assert not cg.auto_markable(r)


def test_second_person_marker_keeps_a_strong_word_red_even_with_a_first_person_verb():   # #25
    for text in ("eres una pendeja", "soy tu fan y tu eres una pendeja", "you are an idiot, im sure"):
        r = judge(text)
        assert (r["lvl"], r["tags"]) == ("alert", []), text


@pytest.mark.parametrize("text", [                                                  # #6
    "hija de tu puta madre", "hijo de la puta madre", "hijos de la puta madre", "tu puta madre",
    "vete a tu puta madre", "eres una hija de la puta madre", "que te parta un rayo hija de la puta madre",
    "chinga tu puta madre",
])
def test_puta_madre_insults_are_not_swallowed_by_the_exclamation_exemption(text):
    r = judge(text)
    assert (r["lvl"], r["tier"]) == ("alert", "red"), text
    assert r["why"][0].startswith("insult/strong:"), text


@pytest.mark.parametrize("text", ["de puta madre", "la puta madre!", "dices puta madre por todo",
                                  "de puta madre el producto", "puta madre!"])
def test_puta_madre_as_an_exclamation_is_still_exempt(text):
    assert lvl(text) is None, text


# ------------------------------------------------------------------------------------------
# 指称：问句、否认式、担心/求证
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [                                                  # #4
    "hay puros estafadores aqui", "hay mucha estafa en este live", "dan puro humo", "tienen productos falsos",
    "tienen puro fraude", "alguien denuncie esta estafa", "hay que denunciar a estos estafadores",
    "hay estafadores en este live", "tienen cara de estafadores", "tiene cara de ladrona",
    "what a scam", "what a fraud this live is", "cuanta estafa!", "cuanto ladron junto",
    "pueden reportar a esta estafadora", "alguien denuncie a esta estafadora",
])
def test_statements_that_start_like_a_question_but_arent_are_amber_maybe_question(text):
    r = judge(text)
    assert (r["lvl"], r["amb"], r["route"]) == ("amber", "maybe_question", "confirm"), (text, r)
    assert "question_exempt" not in r["tags"] and not cg.auto_markable(r)


@pytest.mark.parametrize("text", [                                                  # #4
    "donde compro, es estafa", "como puede ser que sea estafa", "is it a scam", "how can you scam people like this",
    "cual es la estafa", "¿hay estafa?", "hay estafa?", "es una estafa?",
    "son unos estafadores, verdad", "esto es un fraude, no", "ladrones, cierto",
])
def test_real_questions_and_tag_questions_stay_question_exempt(text):
    r = judge(text)
    assert r["lvl"] == "note" and "question_exempt" in r["tags"], (text, r)


def test_price_questions_that_start_with_cuanto_are_still_questions():             # #4
    for text in ("cuanto cuesta el combo", "cuantos dias dura", "cuanto es el envio"):
        assert cg.is_question(cg.light(text), cg.fold(text)), text
        assert not cg.weak_question_start(cg.light(text), cg.fold(text)), text
    assert cg.weak_question_start(cg.light("cuanta estafa"), cg.fold("cuanta estafa"))


@pytest.mark.parametrize("text", [                                                  # #5
    "espero que no sea estafa", "ojala no sea estafa", "no quiero que sea estafa", "no creo que sea estafa",
    "dudo que sea estafa", "no parece estafa", "no vaya a ser estafa, es la primera vez que compro",
    "pense que era estafa pero me llego todo perfecto", "tenia miedo de que fuera estafa pero ya me llego",
    "tengo miedo de que sea fraude", "cuidado con las estafas en tiktok, aqui si es seguro",
    "mi banco me bloqueo el pago por posible fraude", "los ladrones se llevaron mi paquete del porche",
    "thief took my package from the door", "this is not a scam", "not a scam i got mine", "its not a scam",
    "isnt a scam", "it is not fake", "never a scam", "no scam here",
    "ya compre tres veces y nunca me estafaron", "dicen que es estafa pero a mi me llego rapido",
    "no creo que sean ladrones",
])
def test_worried_buyer_reassurance_and_defender_phrasings_are_cut_like_a_denial(text):
    r = judge(text)
    assert r["lvl"] is None and not cg.auto_markable(r), (text, r)


@pytest.mark.parametrize("text,lv", [                                               # #5 对照：真指控不受影响
    ("es una estafa", "amber"), ("eres una ladrona", "alert"), ("son unos estafadores", "amber"),
    ("ustedes son unos estafadores", "alert"),
    ("this is a scam", "alert"), ("la verdad pense que no, pero es estafa", "alert"),
    ("estafadores!! cuidado con esta cuenta", "alert"),
])
def test_real_accusations_are_not_cut_by_the_widened_denial(text, lv):
    assert lvl(text) == lv, text


@pytest.mark.parametrize("text,lv", [("no, es una estafa", "amber"), ("no, es estafa pura", "amber"),
                                     ("No. Estafa.", "alert"), ("no es una estafa", None),
                                     ("no es estafa, es real", None)])
def test_a_no_before_a_comma_does_not_deny_the_next_clause(text, lv):               # #50
    assert lvl(text) == lv, text


# ------------------------------------------------------------------------------------------
# 引流：@账号、邮箱、ask、链接
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [                                                  # #7
    "@lupita escribeme al privado", "@ana mandame mensaje cuando llegues", "@sofi escribeme porfa",
    "@carmen mandame tu direccion", "@rocio mandame el video", "@vero dime por privado",
    "@lalo ven estamos en vivo", "@betty mi celular se mojo", "@nancy sigueme que te sigo",
    "@marisol follow me que te sigo", "@sofi_88 te paso mi opinion despues",
    "@lupe mira esta es mi tienda favorita",
])
def test_calling_a_friend_with_an_at_mention_is_not_a_redirect(text):
    assert lvl(text) in (None, "note"), text


@pytest.mark.parametrize("text", [                                                  # #7
    "sigueme en @x_tienda", "mi ig @x_tienda", "vengan a mi live @x_tienda", "siganme en instagram @tienda.bella22",
    "buscame como @x_tienda", "mi instagram @fulana_77", "ig: tiendabella22 mas barato en mi tienda",
])
def test_an_at_handle_introduced_by_an_account_label_is_still_a_redirect(text):
    assert lvl(text) == "alert", text


def test_labeled_handles_alone_are_not_even_a_note_and_bare_mentions_are_not_labeled():   # #7
    assert cg.handle_hits("@lupita hola") == []
    assert cg.handle_hits("@lupita hola", labeled_only=False) == ["lupita"]
    assert cg.handle_hits("mi ig @x_tienda") == ["x_tienda"]
    assert cg.handle_hits("ig: x_tienda") == ["x_tienda"]
    assert cg.handle_hits("sigueme en @luna_demo", streamer="luna_demo") == []     # 主播自己不算


@pytest.mark.parametrize("text", [                                                  # #19
    "5512345678 @fulana_77", "ig: fulana_77 tel 5512345678", "@amiga 5512345678",
])
def test_phone_plus_any_at_account_is_a_red_structural_contact(text):
    r = judge(text)
    assert (r["lvl"], r["cats"], r["tier"]) == ("alert", ["promo_contact"], "struct"), text
    assert not cg.auto_markable(r)


def test_phone_plus_the_streamers_own_handle_is_just_a_phone_note():               # #19
    assert lvl("@luna_demo 5512345678") == "note"


def test_an_email_address_is_a_contact_not_a_link():                                # #31
    assert cg.url_hits(cg.light("mi correo es maria.lopez88@gmail.com")) == []
    assert cg.email_hits(cg.light("mi correo es maria.lopez88@gmail.com")) == ["maria.lopez88@gmail.com"]
    r = judge("mi correo es maria.lopez88@gmail.com para el recibo")
    assert (r["lvl"], r["cats"], r["tier"]) == ("note", ["promo_contact"], "struct")
    for text in ("mandame mensaje a maria.lopez88@gmail.com", "vengan a mi live maria@gmail.com",
                 "escribeme a juan@gmail.com"):
        r = judge(text)
        assert r["lvl"] == "alert" and "promo_link" not in r["cats"], text
        assert not cg.auto_markable(r) or r["tier"] == "red"
    # 邮箱用户名里的数字不是电话
    assert cg.phone_hits(cg.light("juan5512345678@gmail.com")) == []


@pytest.mark.parametrize("text", [                                                  # #28
    "vengan a mi live, donde esta el link", "vengan a mi live donde les doy el link",
    "pasen a mi live, donde esta mi tienda con todo mas barato",
    "mas barato en mi tienda, donde les paso el link", "ven a mi live como compro mas barato en mi tienda",
])
def test_a_pull_phrase_is_not_hidden_by_a_trailing_ask_sentence(text):
    r = judge(text)
    assert (r["lvl"], r["cats"]) == ("alert", ["promo_pull"]), text


@pytest.mark.parametrize("text", ["pasame tu mi whatsapp", "tienen whatsapp?", "donde esta el link de la tienda",
                                  "dame tu telegram", "me pasan su numero", "do you have whatsapp?"])
def test_a_customer_asking_for_contact_is_still_exempt(text):                       # #28 / #48
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", [                                                  # #15
    "mira https://www.tiktok.com/@luna_demo, es la mejor", "sigan a https://www.tiktok.com/@luna_demo.",
    "(https://www.tiktok.com/@luna_demo)", "https://www.tiktok.com/@luna_demo!!",
    "https://www.tiktok.com/@luna_demo/live", "https://www.tiktok.com/@LUNA_DEMO?lang=es",
    "https://vm.tiktok.com/@luna_demo", "tiktok.com/@luna_demo/video/1",
])
def test_own_streamer_link_is_exempt_even_with_trailing_punctuation(text):
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", [                                                  # #16
    "http://evil.com/?r=https://www.tiktok.com/@luna_demo/video/1", "https://evil.com/tiktok.com/@luna_demo/x",
    "https://www.tiktok.com/@luna_demo_fake/video/1", "https://www.tiktok.com/@luna_demo.fake",
    "https://tiktok.com.evil.com/@luna_demo/x", "https://www.tiktok.com/@otra",
])
def test_a_foreign_url_that_merely_contains_the_streamers_tiktok_path_is_a_redirect(text):
    r = judge(text)
    assert (r["lvl"], r["cats"]) == ("alert", ["promo_link"]), (text, r)


def test_lookalike_domains_are_not_covered_by_mine_and_exempt_domains(tmp_path):     # #49
    write(tmp_path, "mine.txt", "marcaejemplo.com\n@marcaejemplo\n")
    write(tmp_path, "exempt_domains.txt", "correos-ejemplo.mx/track\n")
    rules = cg.load_rules(FIX, tmp_path)
    for text in ("visita www.marcaejemplo.com.ofertas-xyz.shop mas barato", "https://tienda-marcaejemplo.com/gratis",
                 "https://evil.com/?u=marcaejemplo.com", "rastreo en correos-ejemplo.mx/otra",
                 "visita marcaejemplo.com.evil.shop"):
        assert lvl(text, rules=rules) == "alert", text
    for text in ("pedido en www.marcaejemplo.com", "https://tienda.marcaejemplo.com/x", "visita marcaejemplo.com",
                 "rastreo en correos-ejemplo.mx/track/123"):
        assert lvl(text, rules=rules) is None, text
    # 账号是精确匹配
    assert lvl("siganme en @marcaejemplo_oficial", rules=rules) == "alert"
    assert lvl("siganme en @marcaejemplo", rules=rules) == "note"


@pytest.mark.parametrize("text", [                                                  # #36
    "mitienda . com", "mitienda [dot] com", "mitienda (dot) com", "mitienda,com", "https :// mitienda . com",
    "h t t p s : / / mitienda . com", "w a . m e / 5215512345678", "mitienda punto com", "mitienda(.)com",
])
def test_more_obfuscated_links_are_unwrapped(text):
    assert cg.url_hits(cg.light(text)), text
    assert lvl(text) == "alert", text


@pytest.mark.parametrize("text", ["mitienda com", "www mitienda com", "hola . como estas", "ok . comprame uno",
                                  "gracias, comprare dos"])
def test_plain_words_next_to_com_are_not_links(text):
    assert cg.url_hits(cg.light(text)) == [], text


def test_a_question_mark_inside_the_url_does_not_make_the_link_a_question():       # #16
    assert lvl("http://evil.com/?r=1") == "alert"
    assert judge("lo vi en amazon.com mas barato?")["amb"] == "question_link"


# ------------------------------------------------------------------------------------------
# 折叠：隐形字符、拆字、遮挡符
# ------------------------------------------------------------------------------------------

INVISIBLES = ["⁦", "⁩", "⁪", "؜", "᠎", "⠀", "\U000e0041", "҈", "҉",
              "ㅤ", "ᅟ", "ᅠ", "ﾠ", "\U000e0100", "\U000e01ef", "឴", "\ud800"]


@pytest.mark.parametrize("ch", INVISIBLES, ids=["U+%04X" % ord(c) for c in INVISIBLES])
@pytest.mark.parametrize("word,mid", [("hijueputa", 4), ("estafa", 3), ("pendeja", 3)])
def test_invisible_and_format_characters_inside_a_word_do_not_hide_it(ch, word, mid):     # #43 #57
    text = word[:mid] + ch + word[mid:]
    assert cg.fold(text) == cg.fold(word)
    assert lvl(text) == "alert", repr(text)


def test_every_unicode_format_character_is_removed():                               # #43
    cf = [chr(cp) for cp in range(0x20000) if cg.unicodedata.category(chr(cp)) == "Cf"]
    assert cf and cg.light("a" + "".join(cf) + "b") == "ab"


@pytest.mark.parametrize("text", [                                                  # #34
    "p🔥u🔥t🔥a", "p😈u😈t😈a", "e❤s❤t❤a❤f❤a", "ya vi e💸s💸t💸a💸f💸a", "p,u,t,a", "p;u;t;a", "p|u|t|a",
    "p/u/t/a", "p+u+t+a", "p~u~t~a", "p . u . t . a", "e/s/t/a/f/a",
])
def test_letters_spread_out_by_any_separator_are_joined(text):
    assert lvl(text) == "alert", text


def test_short_runs_of_single_letters_are_not_joined():                             # #34
    assert cg.fold("S M L XL") == "s m l xl"
    assert cg.fold("talla S M L") == "talla s m l"
    assert cg.fold("a b c d") == "abcd"


@pytest.mark.parametrize("text", [                                                  # #35
    "p*ta", "p*nd*ja", "pend*ja", "est*fa", "estaf*", "id*ota", "put*", "p#ta", "l*drona", "f*ck you", "b*tch",
    "fvck you", "fuk you", "fck you", "eres una p%ta", "no compren, es una est*fa",
])
def test_letters_masked_with_symbols_are_recognised(text):
    assert lvl(text) == "alert", text


@pytest.mark.parametrize("text", ["pata", "peta", "hola*mundo", "gracias a*b*c", "5*5=25", "mi pata"])
def test_masked_word_matching_only_runs_on_tokens_with_a_mask_symbol(text):
    assert lvl(text) is None, text


# ------------------------------------------------------------------------------------------
# 长度、类型、时间戳
# ------------------------------------------------------------------------------------------

def test_max_chars_is_applied_and_really_cuts_a_trailing_link():                    # #48
    assert lvl("a" * 1000 + " www.tienda.com") is None
    assert lvl("a" * 990 + " www.tienda.com") == "alert"


def test_nfkc_expansion_is_cut_again_so_a_comment_cannot_grow_to_18k_characters():  # #56
    assert len(cg.light("ﷺ" * 1000)) <= cg.MAX_CHARS
    g = guard()
    g.judge("ﷺ" * 1000, user="u", ts=1.0)
    assert all(len(k) <= cg.MAX_CHARS for _, *keys in g.flood.users["u"] for k in keys if k)
    best = min(_timed(guard(), "ﷺ" * 1000, i) for i in range(5))
    assert best < budget_ms(guard), "%.1f ms (上限 %.1f ms)" % (best, budget_ms(guard))


def _timed(g, text, i):
    t0 = time.perf_counter()
    g.judge(text, user="adv%d" % i, ts=1000.0 + i * 1000)
    return (time.perf_counter() - t0) * 1000.0


def test_lone_surrogates_do_not_break_the_brigade_event(): # #57
    g = guard()
    for i in range(6):
        g.judge("\ud800", user="u%d" % i, ts=1 + i * 0.1)
    for i in range(4):
        g.judge("\ud83d", user="v%d" % i, ts=2 + i * 0.1)
        g.judge("😀\ud800", user="w%d" % i, ts=3 + i * 0.1)
    assert cg.fold_key("\ud800") == cg.fold_key("")


@pytest.mark.parametrize("bad", [None, "1000.5", float("nan"), float("inf"), -math.inf, True, [1], 1 + 2j])
def test_a_bad_timestamp_raises_before_any_state_changes(bad):                      # #45
    g = guard()
    with pytest.raises((TypeError, ValueError)):
        g.judge("hijueputa", user="u", ts=bad, cid="c1")
    assert not g.flood.users and not g._seen and not g._acc and len(g.flood.recent) == 0
    # 之后这个用户一切正常：没有被毒化的历史
    for i in range(3):
        assert g.judge("hijueputa", user="u", ts=1000.0 + 5 * i, cid="ok%d" % i)["lvl"] == "alert"


def test_int_and_float_timestamps_are_fine():                                       # #45
    g = guard()
    assert g.judge("hola", user="u", ts=1000)["lvl"] is None
    assert g.judge("hola", user="u", ts=1000.5)["lvl"] is None


def test_flood_engine_checks_its_own_timestamp_too():                               # #45
    eng = cg.FloodEngine()
    with pytest.raises(TypeError):
        eng.feed("u", "hola", frozenset(("hola",)), None)
    assert "u" not in eng.users


def test_odd_argument_types_degrade_instead_of_raising():                           # #58
    g = guard()
    assert g.judge(None, user="u", ts=1.0)["lvl"] is None
    for i, text in enumerate((5, 3.5, True, b"hola", ["hijueputa"], ("a", "b"), {"k": "v"})):
        r = g.judge(text, user="u%d" % i, ts=1.0 + i)
        assert set(r) == {"lvl", "amb", "cats", "sub", "why", "tier", "tags", "route"}
    assert g.judge(["hijueputa"], user="x", ts=9.0)["lvl"] == "alert"          # str() 之后照判
    assert g.judge("hola", user=["u"], ts=2.0, cid=["c"])["lvl"] is None        # 不可哈希的 user / cid
    assert g.judge("hola", user={"a": 1}, ts=3.0, cid={"b": 2})["lvl"] is None


def test_load_trusted_survives_nul_and_odd_names(tmp_path):                         # #58
    assert cg.load_trusted("x\x00y", tmp_path) == frozenset()
    assert cg.load_trusted(None, tmp_path) == frozenset()
    assert cg.load_trusted(123, tmp_path) == frozenset()
    (tmp_path / "trusted").mkdir()
    (tmp_path / "trusted" / "etc.txt").write_text("999\n", encoding="utf-8")
    assert cg.load_trusted("etc", tmp_path) == {"999"}
    # 路径穿越：目标文件真的存在时，守卫也得拦住
    (tmp_path / "secret.txt").write_text("111\n", encoding="utf-8")
    assert cg.load_trusted("../secret", tmp_path) == frozenset()
    assert cg.load_trusted("..\\secret", tmp_path) == frozenset()
    assert cg.load_trusted(".hidden", tmp_path) == frozenset()


def test_trusted_uids_are_matched_case_insensitively(tmp_path):                      # #48
    (tmp_path / "trusted").mkdir()
    (tmp_path / "trusted" / "luna_demo.txt").write_text("S:MS4wLjAB\n", encoding="utf-8")
    trusted = cg.load_trusted("luna_demo", tmp_path)
    assert trusted == frozenset({"s:ms4wljab"})
    g = guard(trusted=trusted)
    assert g.judge("eres una pendeja", user="s:MS4wLjAB", ts=1.0)["tags"] == ["trusted"]
    g2 = guard(trusted=["S:ABC"])
    assert g2.judge("eres una pendeja", user="s:abc", ts=1.0)["tags"] == ["trusted"]


# ------------------------------------------------------------------------------------------
# 主播自豁免、trusted 的多个键、uid_ok
# ------------------------------------------------------------------------------------------

def test_the_streamers_own_account_is_matched_by_handle_never_by_nickname():       # #69
    g = guard()
    for kw in ({"handle": "luna_demo"}, {"handle": "Luna_Demo"}, {"handle": " @LUNA_DEMO "}):
        r = g.judge("eres una pendeja", user="h:luna_demo", ts=1.0, **kw)
        assert r["tags"] == ["trusted"], kw
    # 昵称（user 键）叫主播名的普通观众不豁免：任何人都能把昵称改成主播名来躲检测
    for user in ("luna_demo", "Luna_Demo"):
        assert g.judge("eres una pendeja", user=user, ts=2.0)["lvl"] == "alert"
    assert g.judge("eres una pendeja", user="u9", ts=3.0, handle="otra")["lvl"] == "alert"
    assert g.judge("eres una pendeja", user="u8", ts=4.0, handle=None)["lvl"] == "alert"


def test_trusted_list_matches_the_user_key_or_any_of_the_other_keys():              # #69
    g = guard(trusted=["7234567890123456789"])
    assert g.judge("eres una pendeja", user="s:MS4wLjAB", ts=1.0,
                   keys=("7234567890123456789",))["tags"] == ["trusted"]
    assert g.judge("eres una pendeja", user="7234567890123456789", ts=2.0)["tags"] == ["trusted"]
    assert g.judge("eres una pendeja", user="s:MS4wLjAB", ts=3.0, keys=("h:otra",))["lvl"] == "alert"
    assert g.judge("eres una pendeja", user="s:MS4wLjAB", ts=4.0, keys="7234567890123456789")["tags"] == ["trusted"]


def test_auto_markable_takes_the_uid_source_into_account():                         # #68
    r = judge("eres una pendeja")
    assert cg.auto_markable(r)                                       # 不传 uid_src：只看判定本身
    for src in ("id", "id_str", "sec_uid"):
        assert cg.uid_ok(src) and cg.auto_markable(r, uid_src=src)
    for src in ("handle", "nick", "", "xx"):
        assert not cg.uid_ok(src) and not cg.auto_markable(r, uid_src=src), src
    assert cg.UID_OK_SRC == ("id", "id_str", "sec_uid")
    assert not cg.uid_ok(None)
    assert not cg.auto_markable(judge("es una estafa"), uid_src="id")      # 琥珀永远不加人


# ------------------------------------------------------------------------------------------
# 时钟被往回拨
# ------------------------------------------------------------------------------------------

def test_wall_clock_step_back_does_not_count_future_entries():                      # #59
    g = guard()
    for ts in (10000, 10030, 10060, 10090):
        g.judge("texto %d" % ts, user="u", ts=ts)
    results = [g.judge("otro texto %d" % ts, user="u", ts=ts) for ts in (9520, 9550, 9580, 9610)]
    assert [r["lvl"] for r in results] == [None] * 4, results
    assert g.last_flood == []
    # 6 条旧的，往回拨 60 秒，再来 1 条
    g = guard()
    for i in range(6):
        g.judge("mensaje distinto %d" % i, user="u", ts=1000 + i)
    assert g.judge("uno mas", user="u", ts=940)["lvl"] is None


def test_cost_budget_is_not_stuck_degraded_when_the_clock_steps_back():             # #59
    b = cg.CostBudget()
    b.charge(1_000_000.0, 60)
    assert b.structure_only(1_000_001.0)
    assert not b.structure_only(1_000_000.0 - 3600)
    assert not b.structure_only(-5.0) and not cg.CostBudget().structure_only(-5.0)
    b.charge(1_000_000.0 - 3600, 60)
    assert b.trips == 2


# ------------------------------------------------------------------------------------------
# 词表机制：空正则、段头、阈值、符号词条、体检
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["re:", "re:.*", "re:x|", "re:a?", "re:(?:)", "re:|x", "re:\\b", "re:\\s*",
                                   "RE:.*"])
def test_a_regex_entry_that_matches_everything_is_dropped_with_a_warning(tmp_path, entry):   # #41
    for sec, file in (("strong", "insult"), ("assert", "accuse"), ("pull", "promo")):
        d = small_defaults(tmp_path / file) if (tmp_path / file).mkdir() is None else None
        write(d, file + ".txt", "[%s]\n%s\n" % (sec, entry))
        rules = cg.load_rules(d, NO_LOCAL)
        assert [w["reason"] for w in rules.warnings] == ["matches_empty"], (file, entry, rules.warnings)
        r = judge("hola buenas tardes bella", rules=rules)
        assert r["lvl"] is None and not cg.auto_markable(r)
        assert rules.counts.get((file, sec), 0) == 0


def test_a_regex_that_only_matches_something_specific_is_kept(tmp_path):            # #41
    d = small_defaults(tmp_path, insult="[strong]\nre:\\bperdi \\d+ kilos\\b\n")
    rules = cg.load_rules(d, NO_LOCAL)
    assert rules.warnings == [] and judge("perdi 20 kilos", rules=rules)["lvl"] == "alert"


def test_an_invalid_regex_is_reported_by_the_guard_and_not_by_the_banned_terms_detector(tmp_path, capsys):   # #51
    d = small_defaults(tmp_path, accuse="[call]\nre:(\nno compren\n")
    rules = cg.load_rules(d, NO_LOCAL)
    out = capsys.readouterr()
    assert out.out == "" and out.err == "", "不许打印违禁词表的警告"
    assert [(w["reason"], w["entry"]) for w in rules.warnings] == [("invalid_regex", "re:(")]
    assert judge("no compren nada", rules=rules)["cats"] == ["accuse_call"]


def test_a_regex_in_a_section_that_cannot_use_one_is_reported(tmp_path):            # #62
    d = small_defaults(tmp_path, insult="[exempt-phrases]\nre:\\bfoo\\b\n[target]\nre:fe+a\nfea\n",
                       accuse="[assert-frame]\nre:x\n", promo="[ask]\nRE:dame tu (cel|tel)\n")
    rules = cg.load_rules(d, NO_LOCAL)
    got = sorted((w["file"], w["section"], w["reason"]) for w in rules.warnings)
    assert got == [("accuse", "assert-frame", "regex_not_supported"), ("insult", "exempt-phrases", "regex_not_supported"),
                   ("insult", "target", "regex_not_supported")]
    assert rules.target_words == ("fea",)
    # ask 段认 re:，大小写不敏感
    assert any(rx.search("dame tu cel") for rx in rules.ask_res)


def test_local_exempt_file_rejects_regex_lines(tmp_path):                           # #62
    write(tmp_path, "exempt.txt", "[accuse]\nre:puro.*\npuro cuento ejemplo\n")
    rules = cg.load_rules(FIX, tmp_path)
    assert [w["reason"] for w in rules.warnings] == ["regex_not_supported"]
    assert judge("ese puro cuento ejemplo", rules=rules)["lvl"] is None


def test_missing_default_files_and_directory_are_reported(tmp_path):                # #62 #70
    rules = cg.load_rules(tmp_path / "nope", NO_LOCAL)
    assert [w["reason"] for w in rules.warnings] == ["defaults_missing"] and rules.count == 0
    d = tmp_path / "half"
    d.mkdir()
    write(d, "insult.txt", "[strong]\nidiota\n")
    rules = cg.load_rules(d, NO_LOCAL)
    missing = sorted(w["file"] for w in rules.warnings if w["reason"] == "missing_file")
    assert missing == ["accuse", "cues", "interact", "promo", "thresholds"]
    assert cg.load_rules(FIX, NO_LOCAL).warnings == []


def test_a_minus_line_that_removes_nothing_is_reported(tmp_path):                   # #62
    write(tmp_path, "insult.txt", "-CTM\n-no existe esta\n")
    rules = cg.load_rules(FIX, tmp_path)
    assert [(w["reason"], w["entry"], w["src"]) for w in rules.warnings] == [("removal_unmatched", "no existe esta", "local")]
    assert "ctm" not in rules.entries[("insult", "strong")]


def test_warnings_are_capped_and_the_rest_counted_by_reason(tmp_path):              # #70
    write(tmp_path, "insult.txt", "".join("suelta%d\n" % i for i in range(500)))
    rules = cg.load_rules(FIX, tmp_path)
    assert len(rules.warnings) == cg.MAX_WARNINGS + 1
    assert rules.warnings[-1] == {"reason": "more", "n": 450, "by_reason": {"entry_before_section": 450}}
    assert all(w["reason"] == "entry_before_section" for w in rules.warnings[:-1])
    assert len(json.dumps(rules.warnings)) < 20000


@pytest.mark.parametrize("header", ["[Medium]", "[ medium ]", "[MEDIUM]", "[sexual_soft]", "［medium］", "[ Medium ]"])
def test_section_headers_are_normalised_never_taken_for_entries(tmp_path, header):     # #42
    name = "sexual-soft" if "sexual" in header else "medium"
    d = small_defaults(tmp_path, insult="[strong]\nhijueputa\n%s\ntonta\n" % header)
    rules = cg.load_rules(d, NO_LOCAL)
    assert rules.warnings == [], header
    assert rules.entries[("insult", "strong")] == ["hijueputa"]
    assert rules.entries[("insult", name)] == ["tonta"]
    assert judge("eres tonta", rules=rules)["lvl"] == "note"


@pytest.mark.parametrize("header", ["[medium", "[medium]:", "[medium]x", "[ medium"])
def test_a_header_missing_its_bracket_is_a_warning_and_swallows_what_follows(tmp_path, header):   # #42
    d = small_defaults(tmp_path, insult="[strong]\nhijueputa\n%s\ntonta\n" % header)
    rules = cg.load_rules(d, NO_LOCAL)
    assert [w["reason"] for w in rules.warnings] == ["bad_section"], header
    assert rules.entries[("insult", "strong")] == ["hijueputa"]              # tonta 没有悄悄落进上一段
    assert judge("eres tonta", rules=rules)["lvl"] is None


def test_unknown_capitalised_section_is_reported_in_lowercase(tmp_path):            # #42
    d = small_defaults(tmp_path, insult="[Strng]\nfoo\n")
    rules = cg.load_rules(d, NO_LOCAL)
    assert [(w["section"], w["reason"]) for w in rules.warnings] == [("strng", "unknown_section")]


def test_local_section_header_typos_do_not_promote_words_into_a_red_section(tmp_path):    # #42
    write(tmp_path, "insult.txt", "[medium]\nfoo1\n[Strong]\nmiinsulto\n")
    rules = cg.load_rules(FIX, tmp_path)
    assert rules.warnings == []
    assert "miinsulto" in rules.entries[("insult", "strong")]
    assert "strong" not in rules.entries[("insult", "medium")]


@pytest.mark.parametrize("line", ["user_burst_note: 0/10", "user_burst_alert: 12/0", "user_burst_alert: -1/10",
                                  "brigade_jaccard: 2", "brigade_jaccard: 0", "budget_ms: 0", "budget_ms: -5",
                                  "same_text_note: nan/60", "brigade_min_words: 0", "caps_lead: 1.5/3",
                                  "caps_lead: 0.6/0", "budget_window_sec: 0", "accumulate: 2/0",
                                  "user_burst_alert_long: inf/60"])
def test_out_of_range_thresholds_are_a_warning_and_keep_the_default(tmp_path, line):   # #60
    d = small_defaults(tmp_path, thresholds=line + "\n")
    rules = cg.load_rules(d, NO_LOCAL)
    key = line.split(":")[0]
    assert rules.thresholds[key] == cg.DEFAULT_THRESHOLDS[key], line
    assert [(w["reason"], w["entry"]) for w in rules.warnings] == [("bad_threshold", line)]


def test_in_range_thresholds_are_accepted(tmp_path):                                # #60
    d = small_defaults(tmp_path, thresholds="user_burst_note: 1/5\nbrigade_jaccard: 1\nbudget_ms: 1\ncaps_lead: 1/1\n")
    rules = cg.load_rules(d, NO_LOCAL)
    assert rules.warnings == []
    assert rules.thresholds["user_burst_note"] == (1, 5) and rules.thresholds["brigade_jaccard"] == 1.0


@pytest.mark.parametrize("entry", ["!!!", "?", ".", "-", "====", "...", "¿", "¡¡", "* * *"])
def test_symbol_only_entries_that_are_ascii_punctuation_are_dropped(tmp_path, entry):  # #61
    d = small_defaults(tmp_path, insult="[strong]\n%s\n" % entry)
    rules = cg.load_rules(d, NO_LOCAL)
    assert [w["reason"] for w in rules.warnings] == ["empty"], entry
    assert judge("hola !!! que tal? bien. - ok ==== nada", rules=rules)["lvl"] is None


def test_emoji_entries_are_still_matched_literally(tmp_path):                       # #61
    d = small_defaults(tmp_path, insult="[strong]\n🖕\n🤬 🤬\n")
    rules = cg.load_rules(d, NO_LOCAL)
    assert rules.warnings == [] and judge("🖕", rules=rules)["lvl"] == "alert"
    assert judge("hola 🤬 🤬", rules=rules)["lvl"] == "alert"


def test_rules_hash_follows_every_local_file_that_changes_a_verdict(tmp_path):      # #46 #48
    base = cg.load_rules(FIX, NO_LOCAL).hash
    seen = {base}
    for name, text in (("exempt.txt", "[accuse]\npuro humo ejemplo\n"), ("competitors.txt", "Tienda Rival\n"),
                       ("mine.txt", "5512345678\n"), ("exempt_domains.txt", "correos.example\n")):
        d = tmp_path / name.split(".")[0]
        d.mkdir()
        write(d, name, text)
        h = cg.load_rules(FIX, d).hash
        assert h not in seen and len(h) == 12, name
        seen.add(h)
        write(d, name, text + "otra linea ejemplo\n")
        assert cg.load_rules(FIX, d).hash not in seen, name
    # 同样的内容 -> 同样的指纹，顺序无关
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    write(a, "mine.txt", "x1\ny2\n")
    write(b, "mine.txt", "y2\nx1\n")
    assert cg.load_rules(FIX, a).hash == cg.load_rules(FIX, b).hash


def test_rules_hash_changes_when_only_a_threshold_changes(tmp_path):                # #48
    base = cg.load_rules(FIX, NO_LOCAL).hash
    write(tmp_path, "thresholds.txt", "user_burst_note: 5/10\n")
    assert cg.load_rules(FIX, tmp_path).hash != base


# ------------------------------------------------------------------------------------------
# 变异存活点（审查 #48）：规格里的上限和不变量要真的被钉住
# ------------------------------------------------------------------------------------------

def test_accumulation_table_is_bounded_at_3000_users():                             # #48
    g = guard()
    for i in range(cg.USER_TABLE + 400):
        g.judge("es ridículo el precio", user="acc%d" % i, ts=1000.0 + i * 0.01)
    assert len(g._acc) <= cg.USER_TABLE
    assert len(g._acc) < cg.USER_TABLE + 400


def test_the_streamers_own_at_handle_is_never_a_redirect():                         # #48
    assert lvl("siganme en @luna_demo") != "alert"
    assert lvl("siganme en @LUNA_DEMO") != "alert"
    assert lvl("siganme en @luna_demo_fake") == "alert"
    assert cg.handle_hits("ig: luna_demo @luna_demo", "luna_demo") == []


def test_the_brigade_look_back_is_capped_at_500_entries():                          # #48
    eng = cg.FloodEngine()
    toks = frozenset(("uno", "dos", "tres", "cuatro"))
    # 两个账号发了口令，之后 550 条互不相似的弹幕把它们挤出回看范围；第三个账号再发口令时，
    # 回看只看得到它自己：凑不够 3 个账号，不成事件。回看上限去掉的话这里会成事件
    for i in range(2):
        eng.feed("old%d" % i, "uno dos tres cuatro", toks, 1000.0 + i * 0.001)
    for i in range(cg.RECENT_SCAN + 50):
        eng.feed("f%d" % i, "fill %d" % i, frozenset(("fill", str(i))), 1000.5 + i * 0.001)
    out = eng.feed("old2", "uno dos tres cuatro", toks, 1001.5)
    assert [e for e in out if e["kind"] == "brigade"] == [], out
    assert not eng.clusters


def test_no_alerts_in_the_five_thousand_message_memory_test():                      # #48
    g = guard()
    alerts = 0
    for i in range(5000):
        r = g.judge("mensaje numero %d de la fila %d" % (i, i % 97), user="m%d" % (i % 800), ts=1000.0 + i * 0.12,
                    cid="mem%d" % i)
        alerts += r["lvl"] == "alert"
    assert alerts == 0
    assert len(g.flood.users) <= cg.USER_TABLE


# ------------------------------------------------------------------------------------------
# 工业词（#54）和 I3 的 20 ms
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [                                                  # #54
    "they are fake lashes but look so real", "they are fake nails", "are fake nails ok for sensitive skin",
    "is fake hair ok", "i love my fake tan", "these fake plants are cute", "fake flowers are better",
    "they are fake eyelashes", "uñas postizas hermosas",
])
def test_english_fake_beauty_talk_is_exempt(text):
    assert lvl(text) is None, text


def test_fake_is_still_an_accusation_when_it_is_about_the_product():                # #54
    assert lvl("this is fake") == "alert"
    assert lvl("they are fake") == "alert"


def test_judge_stays_under_20_ms_when_the_flood_window_is_full():                   # #44
    """窗口里已经有 600 条互不相似的 1000 字长弹幕（500 个单字词）时，再来的一条仍要在 I3 的
    20 ms 以内，结构模式（超预算降级）下同样。修复前：普通模式约 24 ms，结构模式约 16 ms。"""
    rng = random.Random(3)
    pool = ["%c" % (0x4E00 + i) for i in range(3000)]

    def long_comment():
        return " ".join(rng.sample(pool, 500))[:1000]

    g = guard()
    for i in range(600):
        g.judge(long_comment(), user="fill%d" % i, ts=1000.0 + i * 0.02, cid="fill%d" % i)
    assert len(g.flood.recent) >= cg.RECENT_SCAN
    best = {}
    for mode in (False, True):
        times = []
        for i in range(5):
            t0 = time.perf_counter()
            g.judge(long_comment(), user="probe%d" % i, ts=1013.0 + i * 0.01,
                    structure_only=mode, cid="probe-%s-%d" % (mode, i))
            times.append((time.perf_counter() - t0) * 1000.0)
        best[mode] = min(times)
    assert best[False] < budget_ms(guard) and best[True] < budget_ms(guard), (best, budget_ms(guard))


def test_similar_prefilter_skips_comments_whose_word_counts_are_too_far_apart():    # #44
    eng = cg.FloodEngine()
    a = frozenset("w%d" % i for i in range(10))
    b = frozenset("w%d" % i for i in range(40))
    assert not eng._similar("a", a, "b", b)                    # 10/40 < 0.8：根本不用算交集
    c = frozenset("w%d" % i for i in range(9)) | {"zz"}
    assert eng._similar("a", a, "c", c) is (9 / 11 >= 0.8)      # 与旧的 |a ∪ b| 算法同值
    assert eng._similar("k", a, "k", b)                        # 句子键相同 -> 相同


# ==========================================================================================
# 复核之后（第三轮）：上一轮修复自己带来的新缺口。每条对着复核员给的复现例句写，修复前会失败
# ==========================================================================================

# ---- 否定 / 条件 / 句式（#2 #8）

@pytest.mark.parametrize("text", [                                                  # #2
    "no inventes eres fea", "ni modo eres fea", "ni modo, eres fea", "no jodas eres gorda", "no te creas eres fea",
    "no me hables eres una gorda", "no llores eres fea", "nunca cambias, eres fea", "ya no te aguanto eres fea",
    "no te soporto eres fea",                                           # 否定词和句式词之间隔着别的动词
    "no tu eres fea", "no. tu eres fea", "pues no, tu eres fea",        # 反驳：「不，你才丑」
    "no, eres fea", "no se, eres fea", "me caes mal. no. eres fea", "dije que no. eres fea",   # 应答 / 跨句
    "no way you are ugly", "no, you are fat", "no u r ugly", "no you are ugly",                # 英语
    "no inventes pinche gorda", "ni modo pinche gorda", "no, gorda de mierda", "ni modo gorda de mierda",
    "no, que gorda eres",                                                # 非系动词句式本来就是骂
])
def test_an_interjection_or_a_reply_before_a_target_frame_is_not_a_reassurance(text):
    r = judge(text)
    assert (r["lvl"], r["tier"]) == ("alert", "red") and cg.auto_markable(r), (text, r)


@pytest.mark.parametrize("text", [                                                  # #2
    "no digas que estas gorda", "para nada eres fea", "jamas pense que eres fea", "tu no estas gorda",
    "no te ves gorda", "nunca me pareces fea", "ya no estas gorda", "ni eres fea ni estas gorda",
])
def test_reassurances_that_hug_the_target_frame_are_still_cut(text):
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", [                                                  # #8
    "que feas estas fajas, no me gustan", "que feas estas manchas", "que gordas estas llantas",
    "que feas estas ojeras me salieron", "you are a fat burner", "you are a fat loss expert",
])
def test_que_plural_estas_and_a_before_the_target_word_are_not_target_frames(text):
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", [                                                  # #8
    "ur ugly", "ur fat", "ur fat lol", "ur ugly bro", "ur fat. whatever", "ur ugly, i dont care", "UR UGLY!!",
])
def test_a_bare_ur_that_ends_the_sentence_is_an_insult(text):
    assert lvl(text) == "alert", text


@pytest.mark.parametrize("text", [                                                  # #8
    "ur fat burner", "ur fat burner. ok", "ur ugly sweater is cute", "i love ur ugly sweater", "if ur ugly. ok",
    "you are fat burning queen", "you are fat loss queen", "your fat burner is great",
])
def test_a_bare_ur_with_more_words_after_it_and_fat_burning_compounds_are_not_insults(text):
    assert lvl(text) is None, text


def test_que_singular_estas_is_still_an_insult():                                   # #8
    assert lvl("que fea estas") == "alert"
    assert lvl("que gorda estas") == "alert"


@pytest.mark.parametrize("text", [                                                  # #8
    "tu si que eres fea", "tu si eres fea", "si, eres fea", "sí eres fea", "si. estas gorda", "ahora si estas gorda",
    "pues si, estas gorda", "claro que si eres fea", "ella si te ves gorda",
])
def test_an_emphatic_si_is_not_a_conditional(text):
    assert lvl(text) == "alert", text


def test_plural_target_words_are_judged_against_confirmed_and_pending_together(tmp_path):   # #8
    """只确认了 fea 和 gorda、feas 和 gordas 还在 :pending 里：待确认那一边的复数也不能当单数。"""
    d = small_defaults(tmp_path, insult="[target]\nfea\ngorda\n[target:pending]\nfeas\ngordas\n")
    rules = cg.load_rules(d, NO_LOCAL)
    for text in ("estas feas manchas", "mira estas gordas llantas", "que feas estas fajas"):
        assert lvl(text, rules=rules) is None, text
    assert lvl("estas fea", rules=rules) == "alert"


# ---- 担心 / 辩护的说法只剪窄（#5 #50）

@pytest.mark.parametrize("text", [                                                  # #5
    "posible estafa", "posible estafa en este live", "esto es una posible estafa", "posible ladron",
    "esa presunta estafadora no responde", "presunto ladron", "presunta ladrona", "alerta de estafa",
    "alerta por estafa", "proteccion contra estafas",                  # 对主播的指控 / 短警告
    "ni la ladrona esa me contesta", "ni la ladrona de la tienda responde",
    "ni los estafadores de la tienda me responden", "no los ladrones estos",       # la / los 后面接名词
    "no se parece estafa", "no se creo que es estafa", "no se me parece estafa", "no se, parece estafa",
])
def test_possible_scam_short_warnings_and_article_plus_noun_are_still_accusations(text):
    assert lvl(text) in ("alert", "amber"), text


@pytest.mark.parametrize("text", [                                                  # #5
    "mi banco me bloqueo el pago por posible fraude", "mi tarjeta tiene alerta de fraude",
    "aqui no se estafa a nadie", "no se roba a nadie aqui",
])
def test_bank_notices_and_se_denials_are_still_cut(text):
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", [                                                  # #5 #50
    "dicen que es estafa, pero a mi me llego rapido",
    "Dicen que es una estafa, pero yo ya compre tres veces y llego perfecto",
    "escuche que es una estafa, pero a mi me llego", "dicen que es fraude, aunque yo compre y llego bien",
    "dicen que es fraude, sin embargo me llego", "dicen que es estafa. pero a mi me llego rapido",
    "dicen que es estafa pero a mi me llego rapido",
])
def test_a_quoted_accusation_followed_by_pero_after_a_comma_is_cut_too(text):
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", ["dicen que es estafa", "dicen que es estafa, y creo que tienen razon",
                                  "pero dicen que es estafa"])
def test_a_quoted_accusation_without_a_pero_after_it_is_still_amber(text):
    assert lvl(text) == "amber", text


# ---- @账号标签、电话、邮箱、链接（#7 #19 #31 #15 #16 #49 #36）

@pytest.mark.parametrize("text", [                                                  # #7
    "sígueme en @tienda22", "síganme en @tienda22", "SÍGANME EN @TIENDA22", "mi página @tienda22 escribeme",
    "búscame como @tienda22", "encuéntrame como @tienda22",
])
def test_accented_account_labels_still_make_the_at_handle_a_redirect(text):
    r = judge(text)
    assert (r["lvl"], r["cats"], r["tier"]) == ("alert", ["promo_contact"], "struct"), text
    assert cg.handle_hits(cg.light(text)) == ["tienda22"]


@pytest.mark.parametrize("text", [                                                  # #7
    "sigueme @lupita_ventas", "dm me @lupita_ventas", "follow me @x_tienda", "siganme @tienda.bella22",
    "escribeme @tienda22", "agregame @tienda22",
])
def test_an_imperative_right_before_the_at_handle_is_a_redirect(text):
    assert lvl(text) == "alert", text


@pytest.mark.parametrize("text", [                                                  # #7
    "mi perfil: privado, dime por privado", "tu pagina: preciosa, mandame mensaje", "mi usuario: pedro, escribeme",
    "perfil: nuevo, sigueme", "mi cuenta-nueva, escribeme", "mi cuenta: 1234 mandame mensaje",
    "escribeme mi cuenta: bloqueada", "mi cuenta - ahorita no sirve mandame mensaje",
])
def test_a_generic_noun_with_a_colon_but_no_at_is_not_an_account(text):
    assert cg.handle_hits(cg.light(text)) == [], text
    assert lvl(text) in (None, "note"), text


@pytest.mark.parametrize("text", [                                                  # #19
    "wsp: 5512345678 para el pedido", "WhatsApp: 5512345678 quiero 2 vestidos", "whatsapp: 5512345678",
    "whatsapp:5512345678", "wasap: 5512345678", "wa-5512345678", "ws - 5512345678", "telegram: 5512345678",
    "tg: 5512345678", "ig: 5512345678", "whatsapp @5512345678", "perfil: 5512345678", "cuenta: 5512345678",
])
def test_a_labelled_phone_number_is_a_phone_not_also_an_account(text):
    assert cg.handle_hits(cg.light(text)) == [], text
    assert cg.handle_hits(cg.light(text), labeled_only=False) == [], text
    r = judge(text)
    assert (r["lvl"], r["cats"], r["sub"]) == ("note", ["promo_contact"], "phone"), (text, r)


@pytest.mark.parametrize("text", ["quiero comprar 5512345678 @ana", "@ana quiero pedir 5512345678",
                                  "mi numero 5512345678 para pedir @ana", "pedido 2 vestidos 5512345678 @ana_lopez"])
def test_a_phone_and_an_account_in_an_order_message_is_only_a_note(text):             # #19
    r = judge(text)
    assert (r["lvl"], r["cats"], r["tier"]) == ("note", ["promo_contact"], "struct"), (text, r)


def test_the_own_account_in_mine_txt_ignores_a_trailing_dot(tmp_path):               # #19
    write(tmp_path, "mine.txt", "@tienda_luna\n5598765432\n")
    rules = cg.load_rules(FIX, tmp_path)
    assert lvl("5512345678 @tienda_luna.", rules=rules) == "note"
    assert lvl("5512345678 @tienda_luna", rules=rules) == "note"
    assert lvl("5512345678 @otra_tienda", rules=rules) == "alert"


@pytest.mark.parametrize("text", [                                                  # #31
    "gana un iphone gratis aqui https://www.tiktok.com@evil-tienda.shop", "http://luna_demo@evil-tienda.shop",
    "https://instagram.com@evil.shop", "http://wa.me@evil.shop", "http://soporte@evil-tienda.com",
    "tiktok.com@evil.shop", "www.tiktok.com@evil.shop", "https://www.tiktok.com@evil.shop ya",
])
def test_a_link_disguised_with_a_login_part_is_not_an_email(text):
    assert cg.email_hits(cg.light(text)) == [], text
    r = judge(text)
    assert (r["lvl"], r["cats"]) == ("alert", ["promo_link"]), (text, r)


def test_an_email_on_the_shops_own_domain_is_not_a_contact(tmp_path):                # #31
    write(tmp_path, "mine.txt", "marcaejemplo.com\nventas@otra-casa.com\n")
    write(tmp_path, "exempt_domains.txt", "correos-ejemplo.mx\n")
    rules = cg.load_rules(FIX, tmp_path)
    assert lvl("escribanos a soporte@marcaejemplo.com", rules=rules) is None
    assert lvl("escribeme a soporte@marcaejemplo.com", rules=rules) in (None, "note")    # 只剩 offer 词的 note
    assert lvl("escribeme a atencion@correos-ejemplo.mx", rules=rules) in (None, "note")
    assert lvl("escribeme a ventas@otra-casa.com", rules=rules) in (None, "note")        # 整个地址写进了 mine
    assert lvl("escribeme a atencion@otra-casa.com", rules=rules) == "alert"
    assert lvl("escribeme a soporte@marcaejemplo.com.evil.shop", rules=rules) == "alert"


@pytest.mark.parametrize("text", [                                                  # #15 #16
    "https://www.tiktok.com/@luna_demo,https://evil.com/promo", "https://www.tiktok.com/@luna_demo,evil.com/promo",
    "https://www.tiktok.com/@luna_demo🔥https://evil.com", "https://www.tiktok.com/@luna_demo|https://wa.me/5512345678",
    "https://www.tiktok.com/@luna_demo/video/1,https://evil.com",
    "https://evil.com\\@www.tiktok.com/@luna_demo", "http://evil.com:80\\@m.tiktok.com/@luna_demo",
    "https://www.tiktok.com,https://evil.com/@luna_demo",
])
def test_a_second_link_glued_to_the_streamers_page_or_a_backslash_host_is_a_redirect(text):
    r = judge(text)
    assert (r["lvl"], r["cats"][0]) == ("alert", "promo_link"), (text, r)


@pytest.mark.parametrize("text", [                                                  # #15
    "https://www.tiktok.com/@luna_demo/video/7123456789012345678?_r=1&_t=ZM-8abc&lang=es",
    "https://www.tiktok.com/@luna_demo/live", "mira https://www.tiktok.com/@luna_demo, es la mejor",
])
def test_real_share_links_to_the_streamers_page_are_still_exempt(text):
    assert lvl(text) is None, text


def test_the_userinfo_and_port_of_a_link_are_not_the_host():                          # #16
    assert cg._split_url("https://user:pw@host.com:8080/x?y") == ("host.com", "/x?y")
    assert cg._split_url("https://www.tiktok.com@evil.shop/x") == ("evil.shop", "/x")
    assert cg._split_url("https://evil.com\\@www.tiktok.com/x")[0] == "evil.com"
    assert cg._split_url("www.marcaejemplo.com😍") == ("www.marcaejemplo.com", "😍")


def test_text_glued_to_the_shops_own_link_does_not_make_it_a_redirect(tmp_path):      # #49
    write(tmp_path, "mine.txt", "marcaejemplo.com\n")
    rules = cg.load_rules(FIX, tmp_path)
    for tail in ("😍", "❤️", "🔥🔥", ",gracias", "*", "|gracias", "<3", "=)", "~", "!!!", ")"):
        for head in ("pedido en www.marcaejemplo.com", "pedido en https://marcaejemplo.com"):
            assert lvl(head + tail, rules=rules) is None, head + tail
    # 粘着的是第二个链接就不放过
    assert lvl("pedido en www.marcaejemplo.com,https://evil.com", rules=rules) == "alert"


def test_country_code_second_level_lookalikes_are_not_covered_by_the_shops_domain(tmp_path):   # #49
    write(tmp_path, "mine.txt", "marcaejemplo.com\n")
    rules = cg.load_rules(FIX, tmp_path)
    for text in ("visita marcaejemplo.com.mx", "visita marcaejemplo.com.br", "visita marcaejemplo.com.ar",
                 "visita marcaejemplo.com.co", "visita marcaejemplo.com.mx/oferta"):
        assert lvl(text, rules=rules) == "alert", text
    # 反过来：自家就是 .com.mx 的，整个域名才算自己的
    write(tmp_path, "mine.txt", "marcaejemplo.com.mx\n")
    rules = cg.load_rules(FIX, tmp_path)
    assert lvl("pedido en marcaejemplo.com.mx", rules=rules) is None
    assert lvl("pedido en marcaejemplo.com", rules=rules) == "alert"


@pytest.mark.parametrize("text", [                                                  # #36
    "hola, link porfa", "si, link por favor", "yes, link please", "hello, link pls",
    "Hola buenas noches, link de compra porfa", "Gracias. Link por favor", "Hola. Info por favor", "Precio. Link",
    "Ok. Shop?", "Hola, store open?", "Que bonito. Link?", "buen dia. link?", "mucho gusto. Info?", "mira, shop",
    "me gusta, net", "Hola. Online hay?", "hola. app nueva?", "gracias. tv nueva",
])
def test_ordinary_sentences_with_a_word_that_is_also_a_tld_are_not_links(text):
    assert cg.url_hits(cg.light(text)) == [], text
    assert lvl(text) is None, text


# ---- 隐形字符（#43）

@pytest.mark.parametrize("ch", ["\u180b", "\u180c", "\u180d", "\u180f", "\u2065", "\ufff0", "\ufff8",
                                "\U000e0080", "\U000e00ff", "\U000e01f0", "\U000e0fff"],
                         ids=lambda c: "U+%04X" % ord(c))
@pytest.mark.parametrize("word,mid", [("hijueputa", 4), ("estafa", 3), ("pendeja", 3)])
def test_other_default_ignorable_code_points_do_not_split_a_word_either(ch, word, mid):
    text = word[:mid] + ch + word[mid:]
    assert cg.fold(text) == cg.fold(word)
    assert lvl(text) == "alert", repr(text)


@pytest.mark.parametrize("text", [
    "eres\u2800una\u2800pendeja", "hola\u2800estafa", "eres\u3164pendeja", "eres\uffa0pendeja",
    "no\u2800compren\u2800esta\u2800tienda", "estafa\u2800total\u2800no\u2800compren", "eres\u115fpendeja",
    "est\u2800afa", "hiju\u3164eputa",
])
def test_blank_looking_characters_are_read_both_as_a_gap_and_as_a_space(text):        # #43
    """盲文空白、韩文填充符占一个空格宽：既可能把词劈开（est\u2800afa），也可能拿来当空格（eres\u2800una\u2800pendeja）。"""
    assert lvl(text) == "alert", repr(text)
    hits = guard().lex_hits(text)
    hits.pop("_flags")
    assert hits, repr(text)


def test_the_second_reading_of_a_blank_character_only_adds_what_the_plain_text_would_hit():   # #43
    g = guard()
    assert g.lex_hits("estafa\u2800total") == g.lex_hits("estafa total")
    assert g.lex_hits("hola\u2800buenas") == g.lex_hits("hola buenas")


# ---- 刷屏事件（#17 #59）与其它

def test_a_message_that_only_resembles_the_middle_variant_is_still_counted():         # #17
    v0 = "quiero el combo rosa por favor"
    v1, v2 = v0 + " ya", v0 + " ya mismo"
    g = guard()
    r = None
    for user, text, dt in (("A", v1, 0), ("B", v0, 1), ("C", v0, 2), ("D", v2, 3), ("E", v1, 4)):
        r = g.judge(text, user=user, ts=1000.0 + dt)
    ev = [e for e in g.last_flood if e["kind"] == "brigade"]
    assert len(ev) == 1 and ev[0]["n"] == 5 and ev[0]["lvl"] == "amber", g.last_flood
    assert (r["lvl"], r["amb"]) == (None, None)            # 引擎数到了，判定不报（2026-10-14）
    assert ev[0]["first_ts"] == 1000.0 and ev[0]["last_ts"] == 1004.0
    assert ev[0]["accounts"] == ["A", "B", "C", "D", "E"]


def test_the_brigade_event_keeps_one_id_while_it_backfills(tmp_path):                  # #17
    v0 = "quiero el combo rosa por favor"
    g = guard()
    ids = set()
    for i in range(12):
        g.judge(v0 if i % 2 else v0 + " ya", user="u%d" % i, ts=1000.0 + i * 0.5)
        ids |= {e["ev"] for e in g.last_flood if e["kind"] == "brigade"}
    assert len(ids) == 1


def test_the_events_accounts_are_listed_in_the_order_they_first_said_it():            # #17
    eng = cg.FloodEngine()
    toks = frozenset(("uno", "dos", "tres", "cuatro"))
    for user, ts in (("A", 1000.0), ("B", 1001.0), ("A", 1002.0), ("C", 1003.0)):
        out = eng.feed(user, "uno dos tres cuatro", toks, ts)
    ev = [e for e in out if e["kind"] == "brigade"][0]
    assert ev["accounts"] == ["A", "B", "C"] and ev["n"] == 3


def test_a_clock_step_back_never_moves_the_events_last_timestamp_backwards():          # #17
    eng = cg.FloodEngine()
    toks = frozenset(("uno", "dos", "tres", "cuatro"))
    for i, ts in enumerate((1000.0, 1001.0, 1003.0, 1002.5)):
        out = eng.feed("u%d" % i, "uno dos tres cuatro", toks, ts)
    ev = [e for e in out if e["kind"] == "brigade"][0]
    assert ev["last_ts"] == 1003.0 and ev["first_ts"] <= ev["last_ts"]


def test_a_customer_repeating_a_weak_question_five_times_is_a_note_not_a_flood_alert():   # #4
    """'alguien me ayuda' / 'hay descuento' / 'tienen talla M' 没有问号，规则词表那边不当问句（可能是陈述），
    刷屏这边照样当问句：被冷落的顾客每隔几秒重复一次，最多到 note。"""
    for text in ("alguien me ayuda", "hay descuento", "tienen talla M", "cuanta ganga"):
        g = guard()
        out = [g.judge(text, user="u", ts=1000.0 + 6 * i) for i in range(5)]
        flood = [e for e in g.last_flood if e["kind"] == "same_text"]
        assert flood and flood[0]["lvl"] == "note", (text, g.last_flood)
        assert out[-1]["lvl"] in (None, "note"), (text, out[-1])


def test_the_accumulation_window_only_counts_entries_up_to_now():                      # #59
    g = guard()
    assert g.judge("stupid", user="u", ts=10000.0)["lvl"] == "note"
    r = g.judge("stupid", user="u", ts=9000.0)         # 时钟往回拨了 1000 秒：第一条在「未来」
    assert r["lvl"] == "note" and "accumulated" not in r["tags"], r
    # 单调的两条仍然累计出琥珀
    g = guard()
    g.judge("stupid", user="u", ts=9000.0)
    assert g.judge("stupid", user="u", ts=9100.0)["amb"] == "accumulated"


def test_future_entries_are_ignored_by_the_same_text_counts_and_the_cost_budget():     # #59
    g = guard()
    for ts in (1030.0, 1031.0, 1032.0, 1033.0):
        g.judge("quiero el combo rosa", user="u", ts=ts)
    g.judge("quiero el combo rosa", user="u", ts=1000.0)            # 往回拨之后第 5 次：前 4 次都在「未来」
    assert not [e for e in g.last_flood if e["kind"] == "same_text"], g.last_flood
    b = cg.CostBudget()
    b.charge(100.0, 60)
    b.charge(50.0, 10)                                               # 往回拨：未来的那条被清掉
    assert [t for t, _ in b.spent] == [50.0]


# ---- 长度、类型（#56 #58 #68）

def test_a_payload_behind_a_run_of_expanding_characters_is_still_found():              # #56
    link = " wa.me/5512345678 vengan a mi tienda www.evil-shop.com"
    for pad in (54, 55, 56, 100, 150):
        r = judge("ﷺ" * pad + link)
        assert r["lvl"] == "alert" and "promo_link" in r["cats"] and "promo_contact" in r["cats"], pad
    assert judge("ﷻ" * 126 + link)["lvl"] == "alert"
    assert judge("ﷺ" * 56 + " hijueputa")["lvl"] == "alert"
    assert len(cg.light("ﷺ" * 150)) <= 150 * cg.NFKC_EXPAND_MAX
    assert len(cg.light("ﷺ" * 1000)) <= cg.MAX_CHARS


def test_ordinary_nfkc_expansions_keep_their_full_text():                              # #56
    assert cg.light("㍿") == "株式会社" and cg.light("…") == "..." and cg.light("ﬃ") == "ffi"
    assert cg.light("⑽") == "(10)" and cg.light("Ｈｏｌａ") == "hola"


def test_lex_hits_cuts_the_text_the_same_way_judge_does():                             # #56
    text = "e\u0301" * 600 + " hijueputa"           # 分解写法的 é：1200 个码点，NFKC 之后才缩成 600；脏话在 MAX_CHARS 之后
    assert judge(text)["lvl"] is None
    g = guard()
    hits = g.lex_hits(text)
    hits.pop("_flags")
    assert hits == {}


def test_odd_user_and_cid_arguments_are_hashed_before_the_flood_engine_sees_them():    # #58
    g = guard()
    # 用一条不被互动词豁免的文本：豁免词整条返回，根本碰不到 user
    assert g.judge("x", user=["u"], ts=1.0, cid=["c"])["lvl"] is None
    assert g.judge("quiero el 1", user={"a": 1}, ts=2.0, cid={"b": 2})["lvl"] is None
    assert g.judge("quiero el 2", user=["u"], ts=3.0)["lvl"] is None
    # 空的不可哈希值和 None 一样是「没有」：不会变成共用的 "[]" 键、被当成同一个人刷屏
    g = guard()
    for i in range(12):
        r = g.judge("quiero comprar", user=[], ts=1000.0 + i)
    assert r["lvl"] is None and not g.flood.users
    g2 = guard()
    assert g2.judge("quiero comprar", user=set(), ts=1.0)["lvl"] is None and not g2.flood.users


def test_non_iterable_keys_do_not_break_the_trusted_check():                            # #58
    g = guard(trusted=["7234567890123456789"])
    assert g.judge("hola", user="x", ts=1.0, keys=5)["lvl"] is None
    assert g.judge("eres una pendeja", user="x", ts=2.0, keys=7234567890123456789)["tags"] == ["trusted"]


def test_load_trusted_survives_a_lone_surrogate_name(tmp_path):                         # #58
    assert cg.load_trusted("\ud800", tmp_path) == frozenset()
    assert cg.load_trusted("ab\ud800cd", tmp_path) == frozenset()


def test_a_nickname_only_viewer_is_not_auto_markable_even_when_uid_src_is_absent():      # #68
    from types import SimpleNamespace

    from app.comment_source import event_to_item

    def item(user_obj):
        return event_to_item(SimpleNamespace(comment="eres una pendeja", user=user_obj,
                                             common=SimpleNamespace(msg_id=5)))

    nick_only = item(SimpleNamespace(nickname="Ana"))
    assert "uid_src" not in nick_only
    r = judge(nick_only["text"], user=nick_only["user"])
    assert r["lvl"] == "alert"
    assert not cg.auto_markable(r, nick_only.get("uid_src"))             # 显式的 None：没有稳定账号键
    assert not cg.auto_markable(r, uid_src=None)
    assert cg.auto_markable(r)                                           # 不传：只看判定本身
    with_id = item(SimpleNamespace(nickname="Ana", id=1234567890123456789))
    assert with_id["uid_src"] == "id" and cg.auto_markable(r, with_id.get("uid_src"))
    handle_only = item(SimpleNamespace(nickname="", unique_id="ana_demo"))
    assert handle_only["uid_src"] == "handle" and not cg.auto_markable(r, handle_only.get("uid_src"))
    assert not cg.uid_ok(None) and not cg.uid_ok(["id"]) and not cg.uid_ok({})


# ---- 词表机制（#60 #61 #70）

@pytest.mark.parametrize("line", [
    "same_text_note: 1" + "0" * 309 + "/60", "user_burst_note: 4/" + "9" * 400, "caps_lead: 0.6/" + "9" * 400,
    "caps_lead: " + "9" * 400 + "/3", "budget_ms: -" + "9" * 400,
])
def test_a_huge_integer_threshold_is_a_warning_and_keeps_the_default(tmp_path, line):       # #60
    d = small_defaults(tmp_path, thresholds=line + "\n")
    rules = cg.load_rules(d, NO_LOCAL)                 # 309 位的整数连 math.isfinite 都会抛 OverflowError：不能漏出来
    key = line.split(":")[0]
    assert rules.thresholds[key] == cg.DEFAULT_THRESHOLDS[key], line
    assert [w["reason"] for w in rules.warnings] == ["bad_threshold"], line


def test_an_emoji_newer_than_this_pythons_unicode_database_is_still_an_emoji(tmp_path, monkeypatch):   # #61
    """Python 3.9 的 Unicode 数据库里 🫶 🩷 🫠 还没分配（类别 Cn）：不能因此被当成空词条丢掉。"""
    real = cg.unicodedata.category
    new_emoji = {"\U0001FAF6", "\U0001FA77", "\U0001FAE0"}
    monkeypatch.setattr(cg.unicodedata, "category", lambda c: "Cn" if c in new_emoji else real(c))
    d = small_defaults(tmp_path, insult="[strong]\n\U0001FAF6\n\U0001FA77\n\U0001FAE0\n")
    rules = cg.load_rules(d, NO_LOCAL)
    assert rules.warnings == [] and judge("hola \U0001FAF6 amiga", rules=rules)["lvl"] == "alert"
    # 纯 ASCII 标点和别的标点仍然丢掉
    d2 = small_defaults(tmp_path / "b", insult="[strong]\n:)\n@\n$\n!!!\n") if (tmp_path / "b").mkdir() is None else None
    assert [w["reason"] for w in cg.load_rules(d2, NO_LOCAL).warnings] == ["empty"] * 4


def test_an_emoji_entry_with_a_skin_tone_or_a_math_symbol_is_matched_literally(tmp_path):   # #61
    d = small_defaults(tmp_path, insult="[strong]\n\U0001F595\U0001F3FD\n≠\n")
    rules = cg.load_rules(d, NO_LOCAL)
    assert rules.warnings == []
    assert judge("hola \U0001F595\U0001F3FD", rules=rules)["lvl"] == "alert"
    assert judge("a ≠ b", rules=rules)["lvl"] == "alert"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="需要非 root 的 POSIX 用户")
def test_an_unreadable_defaults_directory_degrades_to_read_errors_instead_of_raising(tmp_path):   # #70
    d = small_defaults(tmp_path, insult="[strong]\nidiota\n")
    os.chmod(d, 0)
    try:
        rules = cg.load_rules(d, NO_LOCAL)
        assert rules.count == 0 and rules.warnings
        assert {w["reason"] for w in rules.warnings} <= {"read_error", "defaults_missing"}
    finally:
        os.chmod(d, 0o700)
    # 整个上级目录进不去
    parent = tmp_path / "locked"
    parent.mkdir()
    inner = small_defaults(parent, insult="[strong]\nidiota\n")
    os.chmod(parent, 0)
    try:
        assert cg.load_rules(inner, NO_LOCAL).count == 0
    finally:
        os.chmod(parent, 0o700)


def test_the_brigade_scan_ignores_entries_from_the_future():                         # #59
    eng = cg.FloodEngine()
    toks = frozenset(("uno", "dos", "tres", "cuatro"))
    for i in range(3):
        eng.feed("old%d" % i, "uno dos tres cuatro", toks, 2000.0 + i * 0.1)
    assert len(eng.clusters) == 1
    # 时钟往回拨了 1000 秒：旧事件到期，新来的两个账号只能和「未来」的三条凑成事件
    out = eng.feed("new0", "uno dos tres cuatro", toks, 1000.0)
    out += eng.feed("new1", "uno dos tres cuatro", toks, 1000.1)
    assert [e for e in out if e["kind"] == "brigade"] == [], out
    assert len(eng.clusters) == 0


def test_the_same_text_counts_have_an_upper_bound_at_now():                           # #59
    eng = cg.FloodEngine()
    toks = frozenset(("quiero", "el", "combo"))
    for ts in (1030.0, 1031.0, 1032.0, 1033.0, 1034.0):
        eng.feed("u", "quiero el combo", toks, ts)
    out = eng.feed("u", "quiero el combo", toks, 1000.0)          # 5 条都在「未来」
    assert [e for e in out if e["kind"] == "same_text"] == [], out


# ------------------------------------------------------------------------------------------
# 复核第一轮之后找出的回归和漏网（每条一个测试，修复前会失败）
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [                                                  # #2 复核
    "no no manches eres fea", "no no mames eres fea", "no chingona eres fea",
    "no \U0001F602 eres fea", "no \U0001F644 eres fea", "no - eres fea", "no (eres fea)", 'no "eres fea"',
    "no *eres fea*", "no / eres fea", "no-eres fea", "no, no manches, eres fea",
])
def test_a_no_that_is_separated_from_the_target_frame_by_a_phrase_or_a_symbol_does_not_deny_it(text):
    r = judge(text)
    assert (r["lvl"], r["tier"]) == ("alert", "red"), (text, r)


@pytest.mark.parametrize("text", [                                                  # #2 复核：紧挨着的仍是安慰
    "no eres fea", "no eres fea!!", "no eres fea \U0001F60D", "no estas gorda \U0001F495", "no te ves gorda",
    "no eres fea (eres hermosa)", "ya no estas gorda felicidades",
])
def test_a_no_that_hugs_the_target_frame_still_denies_it_even_with_symbols_elsewhere(text):
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", [                                                  # #5 复核：真指控仍然报
    "estos ladrones se llevaron mi pedido y no me devuelven nada", "la ladrona se llevo mi envio y nunca llego",
    "los ladrones se llevaron mi pedido de esta tienda", "ladrones robaron mi paquete en esta tienda",
    "esta tienda esta en revision por posible fraude", "esta cuenta fue reportada por posible fraude",
    "esta cuenta tiene alerta de fraude", "alerta de fraude en esta cuenta",
    "mi banco dijo que esta cuenta tiene alerta de fraude",
    "no sean estafadores", "no sea ladrona y devuelva mi dinero", "no sean ladrones, bajen el precio",
    "no seas ladrona", "dudo mucho que no sea estafa", "no creo que no sea estafa",
    "pense que era estafa, no me equivoque", "creia que era estafa y lo confirme", "temia que fuera estafa y lo fue",
    "pense que era estafa y si lo es",
])
def test_real_accusations_that_share_words_with_the_worried_buyer_phrasings_are_not_cut(text):
    assert lvl(text) in ("alert", "amber"), text


@pytest.mark.parametrize("text", [                                                  # #5 复核：担心 / 辩护的变体仍然剪
    "no vaya a ser que sea estafa", "no vaya a ser que sea una estafa", "tengo miedo que sea estafa",
    "me da miedo que sea estafa", "ojala que no sea estafa", "i dont think its a scam", "im afraid its a scam",
    "i thought it was a scam but it arrived", "cero estafa", "mi tarjeta tiene alerta de fraude",
    "los ladrones se llevaron mi paquete del porche",
])
def test_more_worried_buyer_phrasings_are_cut_like_a_denial(text):
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", [                                                  # #7 复核
    "Sígueme en TikTok \U0001F449 @tienda22", "IG ➡️ @tienda22 escribeme", "siganme \U0001F449 @tienda22",
    "sigueme en mi ig es @tienda22", "siganme en instagram de @tienda22", "siganme!! @tienda22",
    "tiktok: @tienda22 escribeme", "ig - @tienda22 escribeme",
])
def test_an_account_label_followed_by_an_emoji_or_a_connector_word_and_the_handle_is_still_a_redirect(text):
    r = judge(text)
    assert (r["lvl"], r["cats"], r["tier"]) == ("alert", ["promo_contact"], "struct"), (text, r)
    assert set(cg.handle_hits(cg.light(text))) == {"tienda22"}, text


@pytest.mark.parametrize("text", [                                                  # #7 复核：朋友对话仍不算
    "@lupita escribeme al privado", "@nancy sigueme que te sigo", "hola @ana como estas",
    "gracias a @ana por el tip", "vi a @ana en el live de ayer", "@ana @luis vean esto",
])
def test_the_wider_label_gap_does_not_turn_friend_mentions_into_redirects(text):
    assert cg.handle_hits(cg.light(text)) == [], text
    assert lvl(text) in (None, "note"), text


@pytest.mark.parametrize("text", [                                                  # #8 复核
    "si tu ya estas operada puedes usar la faja?", "si de verdad estas gorda usa la talla xl",
    "if you think you are fat this will help", "si estas operada puedes usar la faja?",
])
def test_a_conditional_that_leads_the_clause_a_few_words_before_the_frame_is_not_an_insult(text):
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", [                                                  # #8 复核：这些结果不变
    "si, estas gorda", "tu si eres fea", "cuando te ves al espejo estas fea", "no se si te importa pero eres fea",
    "pero si estas gorda", "mira si eres fea", "hoy si estas fea",
])
def test_conditionals_far_from_the_frame_or_with_an_emphatic_si_stay_insults(text):
    assert lvl(text) == "alert", text


@pytest.mark.parametrize("text", [                                                  # #8 复核：ur 是「你的」
    "burn ur fat", "this will melt ur fat", "does it work on ur fat", "i can see ur fat",
])
def test_a_bare_ur_in_the_middle_of_a_sentence_is_a_possessive_not_an_insult(text):
    assert lvl(text) is None, text


@pytest.mark.parametrize("text", ["omg ur fat", "lol ur ugly", "lol. ur fat", "ur fat"])
def test_a_bare_ur_that_starts_the_clause_is_still_an_insult(text):
    assert lvl(text) == "alert", text


@pytest.mark.parametrize("text", [                                                  # #19 复核
    "@luna 666666666", "@luna 88888888", "@luna 1 1 1 1 1 1 1 1 1", "@luna tienen en 36 37 38 39?",
    "@ana tienen 38 40 42 44?", "@luna cuanto cuesta 350 450 550", "@luna sorteo numero 05 12 18 23 31",
    "@ana 2024-2025-2026", "@ana 12345678",
])
def test_greetings_size_lists_and_years_next_to_an_at_mention_are_not_a_contact_list(text):
    r = judge(text)
    assert (r["lvl"], r["tier"]) in ((None, None), ("note", "struct")), (text, r)
    assert r["lvl"] != "alert", (text, r)


@pytest.mark.parametrize("text", [                                                  # #19 复核：真电话 + @ 仍是红条
    "+504 9999 8888 @tienda_x", "55 1234 5678 @x_tienda", "@ana 5512345678", "5512345678 @ana_tienda",
    "555 123 4567 @ana_tienda", "cero cinco cinco uno dos tres cuatro cinco seis siete @ana_tienda",
])
def test_a_real_phone_number_next_to_an_at_mention_is_still_a_red_contact(text):
    r = judge(text)
    assert (r["lvl"], r["cats"], r["tier"], r["why"]) == ("alert", ["promo_contact"], "struct",
                                                           ["promo/phone+handle"]), (text, r)


@pytest.mark.parametrize("text", [                                                  # #31 复核
    "visita a@mitienda.com/promo", "x@wa.me/5512345678", "mi@bit.ly/abc123", "x@t.me/canal_ofertas",
    "ig@instagram.com/tienda_x", "a@mitienda.com:8080/x", "a@mitienda.com\\promo", "a@mitienda.com#promo",
    "x@tienda.com.mx/promo", "x@tienda.com.co/oferta", "x@sub.tienda.com/promo",     # 三段以上的域名
])
def test_a_link_with_an_x_at_in_front_of_the_domain_is_still_a_link(text):
    assert cg.email_hits(cg.light(text)) == [], text
    assert cg.url_hits(cg.light(text)), text
    r = judge(text)
    assert (r["lvl"], r["cats"]) == ("alert", ["promo_link"]), (text, r)


@pytest.mark.parametrize("text", ["escribeme a ana@gmail.com", "ana@gmail.com.", "ana@gmail.com, gracias",
                                  "ana@gmail.com.mx"])
def test_a_plain_email_address_is_still_an_email_not_a_link(text):
    assert cg.email_hits(cg.light(text)) and cg.url_hits(cg.light(text)) == [], text


@pytest.mark.parametrize("cp", [0x2066, 0x2069, 0x061C, 0x180E, 0x0488, 0x17B4, 0xE0041, 0x200B, 0x200E, 0x00AD,
                                0x2800, 0x3164])
@pytest.mark.parametrize("pattern", ["eres{c}una{c}pendeja", "estafa{c}total", "hola{c}pendeja", "est{c}afa total"])
def test_an_invisible_character_is_read_both_as_a_split_and_as_a_separator(cp, pattern):   # #43 复核
    text = pattern.format(c=chr(cp))
    assert lvl(text) in ("alert", "amber"), (hex(cp), text)


def test_emoji_glue_characters_do_not_trigger_the_second_reading():                  # #43 复核
    guard_ = guard()
    for text in ("hola ❤️ amiga", "\U0001F469‍\U0001F469‍\U0001F467 hola", "1️⃣ quiero"):
        assert guard_._prep(text)[3] is None, text
    assert guard_._prep("eres⁦una⁦pendeja")[3] is not None


def test_mine_and_exempt_domains_entries_with_an_at_in_the_path_are_profile_pages(tmp_path):   # #49 复核
    write(tmp_path, "mine.txt", "tiktok.com/@marcaejemplo\n")
    write(tmp_path, "exempt_domains.txt", "youtube.com/@canalejemplo\n")
    rules = cg.load_rules(FIX, tmp_path)
    for text in ("mira https://www.tiktok.com/@marcaejemplo/video/7123456789", "mira tiktok.com/@marcaejemplo",
                 "mira https://www.youtube.com/@canalejemplo", "mira https://www.youtube.com/@canalejemplo/videos"):
        assert lvl(text, rules=rules) is None, text
    for text in ("mira tiktok.com/@marcaejemplo2", "mira https://www.tiktok.com/@otra",
                 "mira youtube.com/@canalejemplo2"):
        assert lvl(text, rules=rules) == "alert", text


def test_a_lookalike_with_a_non_ascii_label_is_not_covered_by_the_own_domain(tmp_path):   # #49 复核
    write(tmp_path, "mine.txt", "marcaejemplo.com\ndiseñosluna.com\n")
    rules = cg.load_rules(FIX, tmp_path)
    for text in ("www.marcaejemplo.com.éxito.shop", "https://marcaejemplo.com.ñandu.shop/oferta",
                 "marcaejemplo.com.ñandu.shop", "www.marcaejemplo.comé.shop",
                 "www.marcaejemplo.com.evil.shop"):
        assert lvl(text, rules=rules) == "alert", text
    for text in ("www.marcaejemplo.com", "https://www.marcaejemplo.com/p/1", "www.diseñosluna.com"):
        assert lvl(text, rules=rules) is None, text


@pytest.mark.parametrize("text,expected", [                                         # #50 复核
    ("es f, a, k, e", "amber"), ("es f. a. k. e", "amber"), ("e, s, t, a, f, a", "alert"),
    ("no, es una estafa", "amber"), ("No. Estafa.", "alert"),
])
def test_clause_wise_folding_still_joins_spaced_letters_that_contain_slang_letters(text, expected):
    assert lvl(text) == expected, text
    r = judge("p, o, r, q, u, e, r, i, a")
    assert r["lvl"] == "note"
