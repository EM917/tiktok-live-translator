"""弹幕留意规则层（app/comment_guard.py）：第二轮审查找出的缺陷，每条一个回归测试。

每条都对着审查员给的复现例句写（`# #N` 是审查意见的编号），修复前会失败。用 tests/fixtures/comment_guard
的测试词表（红条档全部已确认）；主播名用虚构的 luna_demo，品牌用虚构的 marcaejemplo。纯 CPU，不联网、
不加载模型、不 import main.py。
"""
import json
import math
import random
import time
from pathlib import Path

import pytest

from app import comment_guard as cg

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
    assert all(len(key) <= cg.MAX_CHARS for _, key in g.flood.users["u"])
    best = min(_timed(guard(), "ﷺ" * 1000, i) for i in range(5))
    assert best < 20.0, "%.1f ms" % best


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
    assert best[False] < 20.0 and best[True] < 20.0, best


def test_similar_prefilter_skips_comments_whose_word_counts_are_too_far_apart():    # #44
    eng = cg.FloodEngine()
    a = frozenset("w%d" % i for i in range(10))
    b = frozenset("w%d" % i for i in range(40))
    assert not eng._similar("a", a, "b", b)                    # 10/40 < 0.8：根本不用算交集
    c = frozenset("w%d" % i for i in range(9)) | {"zz"}
    assert eng._similar("a", a, "c", c) is (9 / 11 >= 0.8)      # 与旧的 |a ∪ b| 算法同值
    assert eng._similar("k", a, "k", b)                        # 句子键相同 -> 相同
