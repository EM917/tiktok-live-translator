"""弹幕留意词表（comment_guard/defaults，PR-2 第二轮的 D 部分）：收窄过的词条和新增的候选词条。

每个被收窄的词条钉两样：**正常说法不报**（审查员列出的误报例句）、**恶意说法照报**。每个新候选词条
至少钉一条会命中的句子，带「后面接正常说法」的收窄写法再钉一条不报的。同一份期望对两份词表各跑一遍：

* 夹具词表（红条档全部已确认）：恶意说法是红条（alert，tier == "red"，可自动加入关注名单）；
* 出厂词表（红条档全部是 `:pending`）：恶意说法是琥珀（amb == "pending"，route == "confirm"），
  绝不自动加入关注名单。

正常说法在两份词表里都必须完全不可见（lvl 不是 alert / amber）。句子全是虚构的。纯 CPU，不联网、不加载
模型、不 import main.py。
"""
from pathlib import Path

import pytest

from app import comment_guard as cg

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "comment_guard"
DEFAULTS = HERE.parent / "comment_guard" / "defaults"
NO_LOCAL = HERE / "no-such-local-dir"
STREAMER = "luna_demo"
LEX = {"fixture": cg.load_rules(FIX, NO_LOCAL), "defaults": cg.load_rules(DEFAULTS, NO_LOCAL)}
VISIBLE = ("alert", "amber")


def judge(text, which, user="u1", ts=1000.0):
    return cg.CommentGuard(LEX[which], streamer=STREAMER).judge(text, user=user, ts=ts)


def assert_quiet(text, which):
    r = judge(text, which)
    assert r["lvl"] not in VISIBLE, (text, which, r)


def assert_flagged(text, cat, which):
    r = judge(text, which)
    assert cat in r["cats"], (text, which, r)
    if which == "fixture":
        assert (r["lvl"], r["tier"]) == ("alert", "red"), (text, r)
        assert cg.auto_markable(r), (text, r)
    else:
        assert (r["lvl"], r["amb"], r["route"]) == ("amber", "pending", "confirm"), (text, r)
        assert "pending" in r["tags"] and not cg.auto_markable(r), (text, r)


def cases(table):
    """{词条: (正常说法们, [(恶意说法, 类别)])} -> 参数化用的 (词条, 句子, ...) 列表。"""
    quiet = [(entry, text) for entry, (benign, _) in table.items() for text in benign]
    flagged = [(entry, text, cat) for entry, (_, bad) in table.items() for text, cat in bad]
    return quiet, flagged


# ------------------------------------------------------------------------------------------
# D.1 收窄过的现有词条：审查意见 #23 #24 #26 #12 里的误报例句 + 恶意说法
# ------------------------------------------------------------------------------------------
NARROWED = {
    "a reportar（删）": (
        ["vengo a reportar que ya me llego mi pedido", "hoy toca a reportar las ventas del dia"],
        [("vamos a reportar esta cuenta", "accuse_call"), ("denunciemos a esta tienda", "accuse_call")]),
    "no compren": (
        ["no compren tantos como yo jaja", "no compren todo hoy que se acaba", "no compren mas por favor jaja",
         "no compren muchos que se acaban", "no compren en otras tiendas, aqui es original",
         "no compren si no lo necesitan"],
        [("no compren", "accuse_call"), ("no compren a esta senora", "accuse_call"),
         ("no compren, es mentira", "accuse_call")]),
    "reporten": (
        ["reporten su pedido por inbox", "reporten sus pedidos aqui", "reporten aqui sus pagos",
         "reporten su pago por inbox", "reporten por aqui su direccion", "reporten su pedido en este grupo",
         "reporten sus pedidos en esta pagina"],
        [("reporten este live", "accuse_call"), ("reporten esta cuenta", "accuse_call"),
         ("reporten", "accuse_call")]),
    "boicot": (
        ["hagan boicot al precio jaja", "hagamos boicot al precio alto", "le ponen el boicot"],
        [("boicot a esta tienda", "accuse_call"), ("hagan boicot a esta live", "accuse_call"),
         ("hagamos un boicot", "accuse_call")]),
    "te voy a matar / te vamos a matar": (
        ["te voy a matar de risa con este video", "te voy a matar con las ofertas", "te voy a matar con tanta belleza",
         "te vamos a matar con estos precios", "te vamos a matar de risa bella", "te voy a matar jajaja",
         "con el 10% de descuento te vamos a matar con ese precio"],
        [("te voy a matar", "threat"), ("te vamos a matar", "threat"), ("te voy a matar hijueputa", "threat"),
         ("te voy a matar pendeja", "threat")]),
    "se donde vives / se donde vive": (
        ["no sé dónde vives, pasame tu direccion", "yo no se donde vive", "ni se donde vives", "nunca se donde vives"],
        [("se donde vives", "threat"), ("yo se donde vive", "threat"), ("ya se donde vives, vieja", "threat")]),
    "kill yourself": (
        ["kill yourself laughing at these prices lol", "you cant kill yourself with vitamins",
         "dont kill yourself over the price", "kill yourself if you want but the combo is cheap"],
        [("kill yourself", "threat"), ("go kill yourself", "threat"), ("just kill yourself already", "threat")]),
    "ojala te pase algo": (
        ["ojala te pase algo bueno hoy", "ojala te pase algo bonito hoy bella", "ojala te pase algo lindo en tu cumple",
         "ojala te pase algo padre", "ojala te pase algo chido", "ojala te pase algo bien", "ojala te pase algo good"],
        [("ojala te pase algo", "threat"), ("ojala te pase algo malo", "threat"), ("ojala te pase algo feo vieja", "threat")]),
    "no caigan": (
        ["no caigan en la tentacion jaja", "no caigan en la tentacion, yo ya compre 3", "no caigan en la oferta, yo ya gaste mi sueldo"],
        [("no caigan", "accuse_call"), ("no caigan en su trampa", "accuse_call"), ("no caigan, es una estafa", "accuse_call")]),
    "no se dejen enganar": (
        ["no se dejen enganar, esto si es original", "no se dejen enganar es original", "no se dejen enganar son originales"],
        [("no se dejen enganar", "accuse_call"), ("no se dejen enganar de esta tienda", "accuse_call")]),
    "cuidado / ojo / aguas / alerta": (
        ["ojo con esta marca, esta buenisima", "cuidado con este live, te hace comprar todo jajaja",
         "aguas con esta tienda que te vacia la cartera", "cuidado con esta señora que me tiene gastando todo mi sueldo jaja",
         "cuidado con esta señora", "alerta con esta tienda, hay oferta", "cuidado con las tallas"],
        [("cuidado con esta tienda que no envia nada", "accuse_call"), ("ojo con esta cuenta, roba", "accuse_call"),
         ("aguas con esta marca falsa", "accuse_call"), ("cuidado con esta señora, miente", "accuse_call")]),
    "felicidades ganaste": (
        ["felicidades ganaste el sorteo", "felicidades ganaste bella, te lo mereces", "felicidades, ganaste!!",
         "felicidades ganaste"],
        [("felicidades ganaste, reclama tu premio aqui", "promo_scam"),
         ("felicidades ganaste escribe al 5512345678", "promo_scam"),
         ("reclama tu premio, felicidades ganaste", "promo_scam"), ("felicidades ganaste, entra al link", "promo_scam")]),
    "soporte de tiktok / tiktok support": (
        ["ya le escribi al soporte de tiktok y no me responden", "contacten al soporte de tiktok shop si no les llega",
         "tiktok support said it will arrive tomorrow", "soporte tiktok no me ayuda"],
        [("somos el soporte de tiktok", "promo_scam"), ("soy del soporte de tiktok", "promo_scam"),
         ("soporte de tiktok: verifica tu cuenta", "promo_scam"), ("tiktok support: your account has a problem", "promo_scam"),
         ("le saluda soporte tiktok", "promo_scam")]),
    "seguidores gratis": (
        ["quiero seguidores gratis jaja", "como consigo seguidores gratis"],
        [("seguidores gratis aqui", "promo_scam"), ("seguidores gratis, entra a mi perfil", "promo_scam"),
         ("click para seguidores gratis", "promo_scam")]),
    "muestra / ensena + 部位（衣物、怎么穿）": (
        ["muestranos como se ve en las nalgas", "muéstrame cómo se ve en el culo la licra",
         "enseñame la parte de las nalgas del pantalon", "muestra como se aplica la crema en los senos",
         "enseñanos el brasier en los senos", "muestranos como queda la faja en las nalgas"],
        [("enseña las tetas", "sexual"), ("muestra el culo", "sexual"), ("muestrame las nalgas", "sexual"),
         ("muestranos las tetas", "sexual")]),
    "quitate todo": (
        ["quitate todo el estres con este te", "quitate todo el cansancio con esta vitamina"],
        [("quitate toda la ropa", "sexual"), ("quitate todo eso", "sexual"), ("quitate la ropa", "sexual"),
         ("desnudate", "sexual")]),
    "me la como（删）": (
        ["esa barrita me la como en dos minutos", "el chocolate me la como toda", "esa tarta me la como yo sola"],
        [("te cojo", "sexual")]),
    "mas barato en mi / mejor precio en mi / cheaper in my": (
        ["mas barato en mi pais", "lo venden mas barato en mi ciudad", "mejor precio en mi opinion",
         "mejor precio en mi vida", "cheaper in my country", "cheaper at my house"],
        [("mas barato en mi tienda", "promo_pull"), ("mas barato en mi live", "promo_pull"),
         ("mejor precio en mi perfil", "promo_pull"), ("cheaper in my store", "promo_pull"),
         ("cheaper at my shop", "promo_pull")]),
    "aqui es mas caro": (
        ["aqui es mas caro que en amazon"],
        [("aqui es mas caro, vengan a mi tienda", "promo_pull")]),
    "mi abogado": (
        ["mi abogado tambien lo compro", "mi abogado me dijo que este suplemento le sirvio", "mi abogado ya vio el contrato",
         "mi abogado esta de viaje"],
        [("mi abogado los va a demandar", "accuse_report"), ("voy a hablar con mi abogado", "accuse_report"),
         ("mi abogado ya esta en camino", "accuse_report"), ("llamar a mi abogado ahorita mismo", "accuse_report")]),
}
NARROWED_QUIET, NARROWED_FLAGGED = cases(NARROWED)


@pytest.mark.parametrize("which", sorted(LEX))
@pytest.mark.parametrize("entry,text", NARROWED_QUIET, ids=["%s|%s" % (e[:14], t[:40]) for e, t in NARROWED_QUIET])
def test_narrowed_entries_stay_quiet_on_ordinary_talk(entry, text, which):
    assert_quiet(text, which)


@pytest.mark.parametrize("which", sorted(LEX))
@pytest.mark.parametrize("entry,text,cat", NARROWED_FLAGGED, ids=["%s|%s" % (e[:14], t[:40]) for e, t, _ in NARROWED_FLAGGED])
def test_narrowed_entries_still_flag_the_real_thing(entry, text, cat, which):
    assert_flagged(text, cat, which)


def test_the_two_deleted_entries_are_really_gone_from_both_lexicons():
    for rules in LEX.values():
        every = {e for entries in rules.entries.values() for e in entries}
        assert "a reportar" not in every and "me la como" not in every


# ------------------------------------------------------------------------------------------
# D.2 新候选词条（全部 :pending）
# ------------------------------------------------------------------------------------------
CANDIDATES = {
    "threat：暴力、纵火、强奸": (
        ["voy a ir a la tienda", "te voy a pegar el link", "te voy a romper el record de compras",
         "vamos a quemar grasa con la faja", "te voy a quemar las calorias", "se quemo la tienda de la esquina",
         "la quemaron en la tienda de enfrente"],
        [("te voy a partir la madre", "threat"), ("te voy a romper la cara", "threat"), ("te vamos a partir la cara", "threat"),
         ("te voy a golpear", "threat"), ("voy a ir a tu casa", "threat"), ("les vamos a quemar la tienda", "threat"),
         ("te voy a quemar la casa", "threat"), ("te voy a violar", "threat"), ("voy a violarte", "threat")]),
    "threat：咒人死": (
        ["que te mueras de la risa con este live", "muerete de envidia con mi faja nueva", "ojala y te mueras de la risa",
         "me muero de risa", "te vas a morir de la risa", "muerete de amor con este precio"],
        [("ojala te mueras", "threat"), ("ojala y te mueras", "threat"), ("que te mueras", "threat"),
         ("que te mueras vieja", "threat"), ("muerete vieja", "threat"), ("mereces morir", "threat")]),
    "threat：英语": (
        ["i will find you a coupon code", "i will find the link", "go die trying lol", "hope you get the combo",
         "ill find you on instagram", "i will find you tomorrow"],
        [("i will find you", "threat"), ("ill find you", "threat"), ("i know where you live", "threat"),
         ("i hope you get cancer", "threat"), ("hope you get cancer", "threat"), ("unalive yourself", "threat"),
         ("go die", "threat")]),
    "call：单数命令式、别给钱、别点赞": (
        ["no compres tantos que te vas a endeudar", "no compres mas hoy amiga ya llevas 3",
         "no le des tu dinero a nadie mas que a nosotras jaja", "no pierdan su dinero en tiendas piratas, compren aqui",
         "no gasten su dinero en otra tienda, aqui esta mas barato", "no le den like a la competencia jaja",
         "dont waste your money on cheap ones, buy this one", "no confien en mi, voy a comprar todo",
         "no le crean a las criticas, a mi me funciono", "nadie compre mas que yo jaja",
         "dont buy more than you need", "dont buy it if you dont need it", "we boycott high prices, buy here",
         "no compres en otra tienda, aqui hay de todo", "no compres si no lo necesitas"],
        [("no compres aqui", "accuse_call"), ("no le compres a esta vieja", "accuse_call"), ("no compres nada", "accuse_call"),
         ("no le den su dinero", "accuse_call"), ("no les den su dinero a estos", "accuse_call"),
         ("no gasten su dinero aqui", "accuse_call"), ("no pierdan su dinero aqui", "accuse_call"),
         ("no le den like a esta senora", "accuse_call"), ("no le crean nada", "accuse_call"),
         ("no crean nada de lo que dice", "accuse_call"), ("dont waste your money here", "accuse_call"),
         ("no confien en ella", "accuse_call"), ("no le crean", "accuse_call"), ("nadie compre aqui", "accuse_call"),
         ("dont buy from her", "accuse_call"), ("we boycott this store", "accuse_call")]),
    "assert：整句指称": (
        ["no estan mintiendo, a mi me llego", "esto no es un negocio piramidal verdad", "no es un esquema piramidal",
         "no son fakes, son originales", "le dije a mi hermana que no son productos chafas"],
        [("roban a la gente", "accuse_claim"), ("aqui roban a la gente", "accuse_claim"), ("engañan a la gente", "accuse_claim"),
         ("estan engañando a la gente", "accuse_claim"), ("ya vi, es un negocio piramidal", "accuse_claim"),
         ("estan mintiendo", "accuse_claim"), ("productos chafas", "accuse_claim"), ("ya vi, son fakes", "accuse_claim"),
         ("vendes productos falsificados", "accuse_claim"), ("all lies", "accuse_claim"),
         ("she is lying to you", "accuse_claim")]),
    "sexual：明确的性要求": (
        ["quitate la blusa y pruebate la faja", "quitate el brasier para ver como queda la faja",
         "quitate toda la ropa vieja, estrena esto", "quitate todo eso de encima y ponte la faja jaja",
         "quiero verte sin ropa interior con la faja puesta", "show me the boobs lift bra",
         "show me the tits support bra", "te quiero coger el pedido", "te voy a coger el telefono",
         "voy a coger el pedido ya", "muestra el culito de la faja"],
        [("quiero cogerte", "sexual"), ("te quiero coger", "sexual"), ("te voy a coger", "sexual"),
         ("send nudes", "sexual"), ("show me your tits", "sexual"), ("show me your boobs", "sexual"),
         ("quitate la blusa", "sexual"), ("quiero verte desnuda", "sexual"), ("quiero verte sin ropa", "sexual"),
         ("chupamela", "sexual"), ("mamame la verga", "sexual"), ("enseña el culito", "sexual")]),
    "slur：种族 / 排外辱骂": (
        ["el cabello mojado se ve increible", "la piel mojada queda suave", "mi pelo mojado con este shampoo",
         "se me mojo la faja", "estoy mojada de la lluvia", "una mojada y listo", "regresate a ver el video",
         "vete a tu casa a descansar", "go back to your video", "i want to go back to mexico",
         "we are going back to mexico soon", "sudadera nueva", "illegal parking aqui", "beanie nuevo", "wet back brace"],
        [("beaner", "insult"), ("wetback", "insult"), ("nigger", "insult"), ("sudaca", "insult"), ("sucia india", "insult"),
         ("go back to your country", "insult"), ("go back to mexico", "insult"), ("illegals go home", "insult"),
         ("vete a tu pais", "insult"), ("regresate a tu pais", "insult"), ("pinche mojado", "insult"),
         ("mojada de mierda", "insult"), ("eres una mojada", "insult")]),
    "pull / scam-strong：拉人去别处、卖粉": (
        ["checa mi pedido", "checa mi guia", "pasate a la tienda de abajo", "unete a mi grupo de whatsapp del vecindario",
         "check out my order", "check my order status", "check my package", "vendo mi carro"],
        [("pasate a mi live", "promo_pull"), ("pasate por mi live", "promo_pull"), ("visita mi live", "promo_pull"),
         ("checa mi perfil", "promo_pull"), ("unete a mi live", "promo_pull"), ("check out my page", "promo_pull"),
         ("check my profile", "promo_pull"), ("unete a mi grupo de telegram", "promo_pull"),
         ("vendo seguidores", "promo_scam"), ("venta de cuentas", "promo_scam"), ("f4f", "promo_scam"),
         ("sub4sub", "promo_scam")]),
}
CAND_QUIET, CAND_FLAGGED = cases(CANDIDATES)


@pytest.mark.parametrize("which", sorted(LEX))
@pytest.mark.parametrize("entry,text", CAND_QUIET, ids=["%s|%s" % (e[:10], t[:40]) for e, t in CAND_QUIET])
def test_candidate_entries_stay_quiet_on_their_ordinary_twins(entry, text, which):
    assert_quiet(text, which)


@pytest.mark.parametrize("which", sorted(LEX))
@pytest.mark.parametrize("entry,text,cat", CAND_FLAGGED, ids=["%s|%s" % (e[:10], t[:40]) for e, t, _ in CAND_FLAGGED])
def test_candidate_entries_flag_what_they_were_written_for(entry, text, cat, which):
    assert_flagged(text, cat, which)


def test_death_wishes_moved_from_medium_to_threat_and_are_no_longer_a_clue_note():
    for rules in LEX.values():
        medium = set(rules.entries[("insult", "medium")])
        assert not medium & {"ojala te mueras", "ojala y te mueras", "que te mueras", "muerete"}
    r = judge("que te mueras", "defaults")
    assert (r["lvl"], r["amb"], r["cats"], r["sub"], r["tier"]) == ("amber", "pending", ["threat"], "threat", "red")


def test_no_candidate_or_narrowed_entry_is_confirmed_in_the_shipped_lexicon():
    """用户还没确认任何一条：出厂词表里这些词条全在 :pending 段里（threat / call 是先确认的两段，也一样）。"""
    rules = LEX["defaults"]
    for key, n in rules.counts.items():
        if cg.SECTIONS[key[0]][key[1]] == cg.RED:
            assert cg.is_pending(key[1]) or n == 0, key
    for text in ("te voy a violar", "no compres aqui", "beaner", "send nudes", "pasate a mi live"):
        r = judge(text, "defaults")
        assert r["lvl"] == "amber" and not cg.auto_markable(r), (text, r)


# ------------------------------------------------------------------------------------------
# 新的红条档段 insult/slur
# ------------------------------------------------------------------------------------------

def small_defaults(tmp_path, **files):
    d = tmp_path / "defaults"
    d.mkdir()
    for name in ("insult", "accuse", "promo", "interact", "cues"):
        (d / (name + ".txt")).write_text(files.get(name, ""), encoding="utf-8")
    (d / "thresholds.txt").write_text("", encoding="utf-8")
    return d


def test_slur_is_a_red_tier_section_with_a_pending_twin_and_its_own_sub_category(tmp_path):
    assert cg.SECTIONS["insult"]["slur"] == cg.RED and cg.SECTIONS["insult"]["slur:pending"] == cg.RED
    pend = cg.load_rules(small_defaults(tmp_path, insult="[slur:pending]\nbeaner\n"), NO_LOCAL)
    assert pend.warnings == [] and pend.counts[("insult", "slur:pending")] == 1
    r = cg.CommentGuard(pend).judge("beaner", user="u", ts=1.0)
    assert (r["lvl"], r["amb"], r["cats"], r["sub"], r["tier"]) == ("amber", "pending", ["insult"], "slur", "red")
    assert r["why"] == ["insult/slur:beaner"] and not cg.auto_markable(r)
    (tmp_path / "b").mkdir()
    done = cg.load_rules(small_defaults(tmp_path / "b", insult="[slur]\nbeaner\n"), NO_LOCAL)
    r = cg.CommentGuard(done).judge("beaner", user="u", ts=1.0)
    assert (r["lvl"], r["cats"], r["sub"], r["tier"]) == ("alert", ["insult"], "slur", "red")
    assert cg.auto_markable(r)


def test_slur_matches_only_exact_entries_not_morphological_variants(tmp_path):
    rules = cg.load_rules(small_defaults(tmp_path, insult="[slur]\nsudaca\n"), NO_LOCAL)
    guard = cg.CommentGuard(rules)
    assert guard.judge("sudaca", user="u", ts=1.0)["lvl"] == "alert"
    assert guard.judge("sudaco", user="u2", ts=2.0)["lvl"] is None


def test_moving_a_slur_from_pending_to_confirmed_changes_the_rules_hash(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = cg.load_rules(small_defaults(tmp_path / "a", insult="[slur:pending]\nbeaner\n"), NO_LOCAL)
    b = cg.load_rules(small_defaults(tmp_path / "b", insult="[slur]\nbeaner\n"), NO_LOCAL)
    assert a.hash != b.hash


def test_the_shipped_slur_section_is_pending_only():
    rules = LEX["defaults"]
    assert rules.counts[("insult", "slur:pending")] >= 10 and rules.counts.get(("insult", "slur"), 0) == 0
    assert cg.load_rules(FIX, NO_LOCAL).counts[("insult", "slur")] == rules.counts[("insult", "slur:pending")]
