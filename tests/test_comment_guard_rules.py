"""弹幕留意规则层（app/comment_guard.py）：单条判定、词表机制、第二版评审例句。

**用 tests/fixtures/comment_guard/ 的测试词表**（红条档在里面是打开的），不依赖正式默认词表的内容：
正式默认词表的红条档整段注释在「待业务确认」里，用户逐条确认后才打开（comment_guard/README.md）。
唯一读正式默认词表的是最底下 `test_defaults_*` 那几条，它们钉的恰恰是「红条档没有擅自打开」。

原型（scratchpad 里的 inv-rules-proto）的 133 条用例：117 条单条在本文件，3 条累计和 13 条刷屏序列
分别在本文件的累计一节和 tests/test_comment_guard_flood.py。期望值按 spec §4 更新，每处改动在表里
用 `# v2:` 注明理由。主播名一律用虚构的 luna_demo（真实主播名不进仓库）。

纯 CPU：不联网、不加载模型、不 import main.py。
"""
import time
from pathlib import Path

import pytest

from app import comment_guard as cg

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "comment_guard"
DEFAULTS = HERE.parent / "comment_guard" / "defaults"
NO_LOCAL = HERE / "no-such-local-dir"
STREAMER = "luna_demo"
RULES = cg.load_rules(FIX, NO_LOCAL)

# 默认词表里已经打开的红条档段：用户确认后才往里加（comment_guard/README.md「待业务确认块」）。
# 现在没有任何一段被确认；用户先确认的是 insult/threat 和 accuse/call 两小段
RED_CONFIRMED = frozenset()

RESULT_KEYS = {"lvl", "amb", "cats", "sub", "why", "tier", "tags", "route"}


def judge(text, user="u1", ts=1000.0, rules=RULES, streamer=STREAMER, **kw):
    """一条弹幕，新状态（刷屏/累计互不影响）。"""
    return cg.CommentGuard(rules, streamer=streamer).judge(text, user=user, ts=ts, **kw)


def guard(rules=RULES, **kw):
    return cg.CommentGuard(rules, streamer=kw.pop("streamer", STREAMER), **kw)


# ------------------------------------------------------------------------------------------
# 原型 117 条单条用例（文本, v2 的 lvl, 主类别, tier, 备注）
# ------------------------------------------------------------------------------------------
SINGLE = [
    # ---- A 辱骂：应报
    ('eres una pendeja', 'alert', 'insult', 'red', '强词'),
    ('hijueputa mentirosa', 'alert', 'insult', 'red', '强词+断言'),
    ('hijoeputaaaaa', 'alert', 'insult', 'red', '拉长压缩'),
    ('PENDEJAAAAA', 'alert', 'insult', 'red', '大写+拉长'),
    ('p u t a', 'alert', 'insult', 'red', '拆字拼回'),
    ('put@', 'alert', 'insult', 'red', 'leet @'),
    ('h1j0 d3 put4', 'alert', 'insult', 'red', 'leet'),
    ('vete a la verga', 'alert', 'insult', 'red', '短语'),
    ('ctm', 'alert', 'insult', 'red', '缩写'),
    ('eres una gorda', 'alert', 'insult', 'red', '身材词+指向句式'),
    ('pinche gorda', 'alert', 'insult', 'red', 'pinche+身材词'),
    ('que fea eres', 'alert', 'insult', 'red', '身材词+指向'),
    ('enseña las tetas', 'alert', 'sexual', 'red', '性要求'),
    ('desnúdate mamacita', 'alert', 'sexual', 'red', '性要求'),
    ('cállate idiota', 'alert', 'insult', 'red', '强词'),
    ('te voy a matar', 'alert', 'threat', 'red', '威胁'),
    ('🖕🖕🖕', 'alert', 'insult', 'red', '表情：只有符号的词条按字面匹配'),
    ('estas buenota mamacita', 'note', 'sexual', 'clue', '性暗示软：只记 note'),
    # ---- A 辱骂：不该报
    ('qué rico sabe el detox', None, None, None, 'rico/rica 是口味'),
    ('estoy muy gorda necesito esto', None, None, None, '顾客自述（selfref）'),
    ('mi gordita hermosa', None, None, None, '昵称'),
    ('a toda madre el producto', None, None, None, '正面口语'),
    ('no mames qué barato', None, None, None, '感叹'),
    ('qué chingón está esto', None, None, None, '正面'),
    ('está de poca madre', None, None, None, '正面'),
    ('la computadora no abre el live', None, None, None, '子串碰撞 computadora/puta'),
    ('Maricarmen te amo', None, None, None, '人名 Maricarmen/marica'),
    ('¿qué idioma hablan?', None, None, None, 'idioma≠idiota（模糊匹配关）'),
    ('mi perro está loco por esto', None, None, None, 'loco 无指向句式'),
    ('tanto tiempo sin verte', None, None, None, 'tanto/tonto'),
    ('es ridículo el precio', 'note', 'insult', 'clue', 'medium 档：只 note'),
    ('tú eres bien cabrona', 'note', 'insult', 'clue', '中档+指向：只 note，进模型'),
    ('dices puta madre por todo', None, None, None, 'puta madre 感叹'),
    ('compré las pestañas falsas ayer', None, None, None, '行业词'),
    ('jajajajaja 😂😂', None, None, None, '笑'),
    # ---- B 指称/举报：应报
    # v2: 没有问号、以 es/no es/son 开头的 <=6 词短句不再直接标红，降为琥珀 maybe_question（§4.5）
    ('es una estafa', 'amber', 'accuse_claim', 'red', 'maybe_question'),
    ('ESTAFADORES!!!', 'alert', 'accuse_claim', 'red', ''),
    ('estafaaaaa', 'alert', 'accuse_claim', 'red', '拉长'),
    ('e s t a f a', 'alert', 'accuse_claim', 'red', '拆字'),
    ('this is a scam', 'alert', 'accuse_claim', 'red', 'English'),
    ("don't buy from her, she is a liar", 'alert', 'accuse_call', 'red', 'English + 撇号；号召排在断言前'),
    ('report this live', 'alert', 'accuse_call', 'red', 'English'),
    ('you are ugly', 'alert', 'insult', 'red', 'English 指向句式'),
    ('fuck you', 'alert', 'insult', 'red', 'English'),
    ('fat burner works great', None, None, None, 'fat burner 是商品词'),
    # v2: 认成问句的不再是 none：句里有指称词，记 note 并带 question_exempt，作为升级型送模型（§4.5、§5.2）；
    # 没有模型时 note 不上界面，所以对中控而言「不显示」没变
    ('is it a scam?', 'note', 'accuse_claim', 'red', 'English 问句 -> question_exempt'),
    ('dm me, cheaper at my shop', 'alert', 'promo_pull', 'red', 'English offer+pull'),
    ('dm me', 'note', 'promo_contact', 'clue', 'English 话术单独出现只 note'),
    ('do you have whatsapp?', None, None, None, 'English ask'),
    ("it's totally fake", 'alert', 'accuse_claim', 'red', 'English fake 句式'),
    ('tirar el dinero a la basura', None, None, None, 'basura 泛指(主播口播实测误报)'),
    ('eres una basura', 'alert', 'insult', 'red', 'basura+指向句式'),
    ('la vaca y el colágeno', None, None, None, 'vaca=牛(口播实测)'),
    ('está feo el sabor', None, None, None, '口味差评不是骂人'),
    ('no estoy hablando con nada falso', None, None, None, '否认(口播实测)'),
    ('dos tapaderas de polvo', None, None, None, 'tapadera=瓶盖量勺(口播实测 119 次)'),
    ('me robaron el teléfono ayer', None, None, None, 'robar 的一般用法'),
    ('vamos a engañar el cerebro', None, None, None, 'engañar 的一般用法(口播实测)'),
    ('hay mucha demanda de este producto', None, None, None, 'demanda=需求'),
    ('no compres tantos', None, None, None, '单数 no compres 不入表'),
    ('ya vi, es puro fake', 'alert', 'accuse_claim', 'red', 'fake 在句式里'),
    ('¿cómo va a haber estafa? es TikTok', None, None, None, '反问式否认'),
    ('gracias al equipo de TikTok Shop', None, None, None, '感谢官方不是冒充'),
    ('invierte en ti, tómalo', 'note', 'promo_scam', 'clue', '弱诈骗词单独出现只 note'),
    ('gana dinero desde casa, trabajo desde casa', 'alert', 'promo_scam', 'struct', '两个弱词=alert（组合升级，结构信号）'),
    # v2: 同上，带问号的问句 -> note + question_exempt
    ('es una estafa?', 'note', 'accuse_claim', 'red', '有问号才当问句 -> question_exempt'),
    ('mi perra es hermosa', 'note', 'insult', 'clue', 'perra 档：只 note（已知：宠物句子进 note，不弹红）'),
    ('el pedido está retrasado', None, None, None, 'retrasado 是物流词，不入表'),
    ('ordinaria la caja', None, None, None, 'ordinaria 不入表'),
    ('el zorro del cuento', None, None, None, 'zorro≠zorra'),
    ('no compren, es mentira', 'alert', 'accuse_call', 'red', '号召+断言'),
    ('puro cuento', 'alert', 'accuse_claim', 'red', ''),
    ('ladrona', 'alert', 'accuse_claim', 'red', ''),
    ('los voy a reportar', 'alert', 'accuse_call', 'red', '号召（原型备注「威胁举报」，v2 的类别是 call）'),
    ('vamos a denunciarlos en profeco', 'alert', 'accuse_report', 'red', 'enclitic'),
    ('reporten este live', 'alert', 'accuse_call', 'red', '号召举报'),
    ('ya la reporté', 'alert', 'accuse_report', 'red', '已举报'),
    ('producto falso', 'alert', 'accuse_claim', 'red', 'falso 断言'),
    ('esto es fake', 'alert', 'accuse_claim', 'red', ''),
    ('cuidado con esta señora', 'alert', 'accuse_call', 'red', '警示'),
    # v2: me estafaron/robaron/engañaron -> grievance -> 琥珀（规则自己拿不准：可能是真客诉）
    ('me estafaron, nunca llegó mi pedido', 'amber', 'accuse_claim', 'red', '亲身经历(客诉) -> grievance'),
    # v2: 以 no es 开头的 3 词短句、没有问号 -> 琥珀 maybe_question
    ('no es original', 'amber', 'accuse_claim', 'red', '陈述句 -> maybe_question'),
    # ---- B 不该报
    # v2: 问句：none -> note + question_exempt（见上）
    ('¿es estafa?', 'note', 'accuse_claim', 'red', '问句 -> question_exempt'),
    ('¿no es estafa?', None, None, None, '问句；「no es estafa」本身是豁免短语'),
    ('¿esto es real o es falso?', 'note', 'accuse_claim', 'red', '问句 -> question_exempt'),
    ('es original?', None, None, None, '问句，句里没有指称词'),
    ('no es estafa, es real', None, None, None, '主播口头禅/否认'),
    ('no es mentira, funciona', None, None, None, '否认'),
    ('ya llegó mi pedido, no es falso', None, None, None, '否认'),
    ('¿tiene registro cofepris?', None, None, None, '问句+机构名（线索档，问句里不记）'),
    ('ladrona de corazones 😍', None, None, None, '调情'),
    ('pestañas falsas', None, None, None, '行业词'),
    ('mi pedido no ha llegado', None, None, None, '客诉但无断言词'),
    # v2: 线索档（institution）命中记 note（不上界面，只计数），原型是 score 1 -> none
    ('quiero reportar que no me llegó mi pedido', 'note', 'accuse_claim', 'clue', 'institution 档只记分 -> note'),
    ('¿hasta cuándo nos van a estafar?', 'note', 'accuse_claim', 'red', '反问：规则 note + 进模型'),
    # ---- D 引流：应报
    ('vengan a mi live, más barato', 'alert', 'promo_pull', 'red', '拉人'),
    ('wa.me/5215512345678', 'alert', 'promo_link', 'struct', '链接'),
    ('escríbeme al 55 1234 5678', 'alert', 'promo_contact', 'struct', '号码+话术'),
    ('síganme en instagram @tienda.bella22', 'alert', 'promo_contact', 'struct', '话术+账号'),
    ('visita www.tiendabarata.com', 'alert', 'promo_link', 'struct', '网址'),
    ('visita tiendabarata punto com', 'alert', 'promo_link', 'struct', 'punto com 绕写'),
    ('wa . me / 5215512345678', 'alert', 'promo_link', 'struct', '空格绕写'),
    ('gana dinero desde casa escribeme', 'alert', 'promo_scam', 'struct', '弱词+话术'),
    ('soporte de tiktok: su cuenta será suspendida', 'alert', 'promo_scam', 'red', '冒充官方'),
    ('cinco cinco uno dos tres cuatro cinco seis siete ocho escríbeme', 'alert', 'promo_contact', 'struct', '数字读法+话术'),
    ('5️⃣5️⃣1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣8️⃣ escríbeme', 'alert', 'promo_contact', 'struct', 'keycap 数字'),
    ('ig: tiendabella22 mas barato en mi tienda', 'alert', 'promo_pull', 'red', ''),
    # ---- D 不该报
    ('¿tienen whatsapp?', None, None, None, '顾客要联系方式'),
    ('¿me pasan su número?', None, None, None, '顾客要联系方式'),
    ('mi guía es 1234567890123', None, None, None, '单号'),
    ('pedido #1234567890', None, None, None, '单号上下文'),
    ('$1,299.00 pesos', None, None, None, '金额'),
    ('@luna_demo hola hermosa', None, None, None, '@主播'),
    ('hoy 12/05/1990 es mi cumple', None, None, None, '日期'),
    ('mi número es 5512345678 para el pedido', 'note', 'promo_contact', 'struct', '顾客留号码：只 note'),
    ('mi tienda favorita es esta', 'note', 'promo_contact', 'clue', '话术单独出现：只 note'),
    ('el código es 2x1', None, None, None, ''),
]
# 原型只有 117 条；少了就是搬漏了
assert len(SINGLE) == 117


@pytest.mark.parametrize("text,lvl,cat,tier,note", SINGLE, ids=[c[0][:40] for c in SINGLE])
def test_prototype_single_message_cases(text, lvl, cat, tier, note):
    r = judge(text)
    assert set(r) == RESULT_KEYS
    assert r["lvl"] == lvl, (r, note)
    assert (r["cats"][0] if r["cats"] else None) == cat, (r, note)
    assert r["tier"] == tier, (r, note)


# ------------------------------------------------------------------------------------------
# 文本折叠
# ------------------------------------------------------------------------------------------

def test_light_keeps_question_marks_mentions_digits_and_emoji():
    assert cg.light("¿Es #1 @Ana? 😂") == "¿es #1 @ana? 😂"


def test_fold_undoes_the_common_typing_drift():
    assert cg.fold("Estáfa") == cg.fold("ESTAFA") == "estafa"
    assert cg.fold("señora") == "senora"                       # ñ 写成 n（弹幕是手机打字，常省 ñ）
    assert cg.fold("estafaaaaa") == "estafa"                   # 拉长
    assert cg.fold("llamen") == "llamen"                       # ll rr cc 双写不动
    assert cg.fold("e s t a f a") == "estafa"                  # 拆字
    assert cg.fold("p.u.t.a") == "puta"
    assert cg.fold("m13rd4") == "mierda"                       # leet
    assert cg.fold("don't buy") == "dont buy"                  # 撇号
    assert cg.fold("xq no") == "porque no"                     # 俚语
    assert cg.fold("ＥＳＴＡＦＡ") == "estafa"                    # 全角


def test_fold_removes_zero_width_and_confusable_letters():
    zw = chr(0x200B)
    assert cg.fold("est" + zw + "afa") == "estafa"
    assert cg.fold("p" + chr(0x0435) + "ndeja") == "pendeja"   # 西里尔 е
    assert judge("p" + chr(0x0435) + "ndeja")["lvl"] == "alert"


def test_fold_key_is_the_same_for_the_same_sentence_with_drift():
    assert cg.fold_key("Quiero el COMBO rosaaaa!") == cg.fold_key("quiero el combo rosa")
    assert cg.fold_key("quiero el combo rosa") != cg.fold_key("quiero el combo azul")


# ------------------------------------------------------------------------------------------
# 结果的形状与语义
# ------------------------------------------------------------------------------------------

def test_every_result_has_exactly_the_documented_keys_even_when_nothing_matched():
    for text in ("hola", "", "   ", "eres una pendeja", "wa.me/1", "🤡"):
        assert set(judge(text)) == RESULT_KEYS
    assert judge("hola") == {"lvl": None, "amb": None, "cats": [], "sub": None, "why": [],
                             "tier": None, "tags": [], "route": None}


def test_why_names_the_matching_entry_as_section_and_term():
    r = judge("eres una pendeja")
    assert r["why"][0] == "insult/strong:pendeja"
    assert r["sub"] == "strong"


def test_threat_is_the_primary_category_when_it_comes_with_an_insult():
    r = judge("te voy a matar hijueputa")
    assert r["cats"][:2] == ["threat", "insult"]


def test_judge_requires_a_real_timestamp():
    with pytest.raises(TypeError):
        cg.CommentGuard(RULES).judge("hola", user="u1")


def test_text_longer_than_the_worker_cap_is_cut():
    assert judge("a" * 5000)["lvl"] is None


# ------------------------------------------------------------------------------------------
# 子串 / 变体 / 模糊：整词匹配，变体档只在两段开，模糊档全关
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "la computadora no abre", "el cómputo salió bien", "Maricarmen y Vergara saludan",
    "el artículo y el vehículo",               # culo 是 watch 词，但 artículo / vehículo 里的 culo 不是整词
])
def test_whole_word_matching_never_hits_inside_another_word(text):
    assert judge(text)["lvl"] is None


def test_variant_tier_is_off_outside_strong_and_assert():
    assert judge("mi perro está loco")["lvl"] is None          # perro 不当 perra（medium 只认原样）
    assert judge("los perros ladran")["lvl"] is None
    assert judge("el zorro y el zorrillo")["lvl"] is None
    assert judge("mi perra es hermosa")["lvl"] == "note"       # 原样才算
    assert judge("eres una pendeja")["lvl"] == "alert"
    assert judge("pendejos")["lvl"] == "alert"                 # strong 开变体：复数照样命中
    assert judge("estafadores")["lvl"] == "alert"              # assert 也开变体


def test_fuzzy_tier_is_off_so_neighbouring_real_words_do_not_collide():
    for text in ("yo estaba ahí", "tanto gusto", "qué idioma hablan", "la salsa estaba rica",
                 "me llegó la falta de stock", "estoy en la cocina", "mi puerta"):
        assert judge(text)["lvl"] is None, text


def test_accents_and_capitals_do_not_matter():
    assert judge("ESTAFA")["lvl"] == judge("estáfa")["lvl"] == "alert"       # 不以 es/son 开头：直接红
    assert judge("Hijueputa!!!")["lvl"] == "alert"


# ------------------------------------------------------------------------------------------
# B 类：问句、否认、行业词、琥珀标记
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "es estafa o no", "es falso verdad", "son originales o son falsos", "es pirata o original",
    "esto es falso o que",
    # 其余几种问句写法
    "¿es una estafa?", "es una estafa cierto", "es fraude neta", "es falso, si o no",
    "como va a ser una estafa si llega a tiempo",
])
def test_questions_with_a_reference_word_are_not_shown_but_go_to_the_model(text):
    """评审例句：第一版这些都是红条（没有问号的追问会一直红着，还可能被自动加入关注名单）。"""
    r = judge(text)
    assert r["lvl"] in ("note", None), r
    if r["lvl"] == "note":
        assert "question_exempt" in r["tags"] or "rhetorical_question" in r["tags"]
        assert r["route"] == "upgrade"


@pytest.mark.parametrize("text", [
    "es estafa o no", "es falso verdad", "son originales o son falsos", "es pirata o original",
    "esto es falso o que",
])
def test_review_sentences_become_question_exempt_with_upgrade_route(text):
    r = judge(text)
    assert (r["lvl"], r["tags"], r["route"], r["tier"]) == ("note", ["question_exempt"], "upgrade", "red")


def test_question_without_a_reference_word_is_not_routed_anywhere():
    r = judge("¿cuánto cuesta el de 30 días?")
    assert (r["lvl"], r["route"]) == (None, None)


def test_neta_only_counts_as_a_tag_question_at_the_end():
    assert judge("neta es fraude")["lvl"] == "alert"           # 句首的 neta 是断言
    assert judge("es fraude neta")["tags"] == ["question_exempt"]


def test_choice_question_needs_an_antonym_so_an_accusation_is_not_hidden():
    r = judge("ladrones o estafadores")
    assert r["lvl"] == "alert" and "question_exempt" not in r["tags"]


def test_short_assertion_without_question_mark_is_amber_not_red():
    for text, why in (("es falso", "maybe_question"), ("no es original", "maybe_question"),
                      ("son estafadores", "maybe_question")):
        r = judge(text)
        assert (r["lvl"], r["amb"], r["route"]) == ("amber", why, "confirm"), text
        assert "maybe_question" in r["tags"]


def test_longer_or_not_copular_assertions_stay_red():
    for text in ("neta es fraude", "ya vi, es puro fake", "esto es falso", "producto falso",
                 "es una estafa total de verdad aqui", "dicen que no"):
        r = judge(text)
        assert r["lvl"] in ("alert", None), (text, r)
    assert judge("es una estafa total de verdad aqui")["lvl"] == "alert"


def test_grievance_is_amber_and_keeps_the_observation_only():
    r = judge("me estafaron, nunca llegó mi pedido")
    assert (r["lvl"], r["amb"], r["tier"], r["route"]) == ("amber", "grievance", "red", "confirm")
    assert judge("me robaron el teléfono ayer")["lvl"] is None


def test_quote_is_amber_for_accusations_and_insults():
    r = judge("dicen que es estafa y que no llega nada")
    assert (r["lvl"], r["amb"]) == ("amber", "quote")
    r = judge("me dijeron que eres pendeja")
    assert (r["lvl"], r["amb"]) == ("amber", "quote")
    assert judge("dicen que es estafa, ¿es cierto?")["lvl"] == "note"   # 有问号：问句


def test_denial_is_cut_before_scoring():
    for text in ("no es estafa", "no es fraude, es real", "nunca fue una estafa", "no estoy hablando con nada falso",
                 "¿cómo va a haber estafa? es TikTok", "sin fraude ni mentira"):
        assert judge(text)["lvl"] is None, text


def test_industry_words_are_exempt_and_the_local_file_can_add_more(tmp_path):
    assert judge("pestañas falsas y uñas falsas")["lvl"] is None
    assert judge("joyeria falsa")["lvl"] is None
    assert judge("ese puro cuento ejemplo")["lvl"] == "alert"
    (tmp_path / "exempt.txt").write_text("# 虚构口头禅\n[accuse]\npuro cuento ejemplo\n", encoding="utf-8")
    rules = cg.load_rules(FIX, tmp_path)
    assert judge("ese puro cuento ejemplo", rules=rules)["lvl"] is None
    assert judge("ese puro cuento", rules=rules)["lvl"] == "alert"


def test_call_and_report_phrases():
    assert judge("reporten esta cuenta")["cats"] == ["accuse_call"]
    assert judge("no compren nada aqui")["cats"] == ["accuse_call"]
    assert judge("voy a ir a profeco")["cats"] == ["accuse_report"]
    assert judge("dont buy from this live")["cats"] == ["accuse_call"]
    r = judge("i will report this live")
    assert r["cats"] == ["accuse_call", "accuse_report"]          # 号召排在扬言前
    # 机构名单独出现只是线索档
    r = judge("profeco")
    assert (r["lvl"], r["tier"]) == ("note", "clue")


# ------------------------------------------------------------------------------------------
# A 类：目标词、自述、昵称、表情
# ------------------------------------------------------------------------------------------

def test_target_words_need_a_second_person_frame():
    assert judge("eres una gorda")["lvl"] == "alert"
    assert judge("te ves fea hoy")["lvl"] == "alert"
    assert judge("pinche fea")["lvl"] == "alert"
    assert judge("la gorda del fondo")["lvl"] is None         # la/el/esa/tu/su 前缀不接：口播审计里的误报大户
    assert judge("tu fea manera")["lvl"] is None
    assert judge("está feo el sabor")["lvl"] is None


def test_target_word_alone_is_a_vocative_and_goes_to_the_model_as_a_note():
    r = judge("gorda")
    assert (r["lvl"], r["tags"], r["route"], r["tier"]) == ("note", ["vocative"], "upgrade", "red")


def test_self_reference_next_to_a_target_frame_is_a_note_for_the_model():
    r = judge("yo tambien estoy gorda igual que tu estas gorda")
    assert (r["lvl"], r["tags"], r["route"]) == ("note", ["self"], "upgrade")


def test_medium_with_second_person_is_a_note_for_the_model_and_alone_is_not_routed():
    r = judge("tú eres bien cabrona")
    assert (r["lvl"], r["tags"], r["route"], r["tier"]) == ("note", ["medium_targeted"], "upgrade", "clue")
    r = judge("es ridículo el precio")
    assert (r["lvl"], r["tags"], r["route"]) == ("note", [], None)


def test_exempt_phrases_cover_a_hit_only_when_they_cover_all_of_it():
    assert judge("de puta madre el producto")["lvl"] is None
    assert judge("puta madre!")["lvl"] is None
    assert judge("eres una puta")["lvl"] == "alert"
    assert judge("a toda madre y no mames")["lvl"] is None


def test_middle_finger_alone_is_red_and_negative_emoji_alone_is_only_a_lead():
    r = judge("🖕")
    assert (r["lvl"], r["tier"], r["why"]) == ("alert", "red", ["insult/strong:🖕"])
    r = judge("jaja 🤡🤡")
    assert (r["lvl"], r["route"], r["cats"], r["tier"]) == ("lead", "upgrade", [], "clue")
    # 字母少于 2 个的不送审：纯表情的负面线索只是 lead，不会去问模型
    r = judge("🤡🤡")
    assert (r["lvl"], r["route"]) == ("lead", None)


# ------------------------------------------------------------------------------------------
# 负面线索 lead（cues.txt）：只决定送不送模型
# ------------------------------------------------------------------------------------------

def test_cues_become_a_lead_only_when_nothing_else_matched():
    for text in ("sí claro, muy honestos ustedes", "ajá y yo soy la reina", "ya quisieras tú", "cómo no..."):
        r = judge(text)
        assert (r["lvl"], r["route"], r["sub"]) == ("lead", "upgrade", "irony"), text
    r = judge("NO SE QUE PASA CON ESTE LIVE")
    assert (r["lvl"], r["sub"]) == ("lead", "caps")
    assert judge("NO ME GUSTA")["lvl"] == "lead"                      # 3 个词
    assert judge("QUE BONITO")["lvl"] is None                         # 只有 2 个词
    assert judge("ajá, eres una pendeja")["lvl"] == "alert"           # 有词表命中时 lead 让位
    r = judge("🙄", mention=True)
    assert r["lvl"] == "lead" and "mention" in r["tags"]
    assert judge("hola bella 😍")["lvl"] is None


# ------------------------------------------------------------------------------------------
# D 类：链接、电话、@账号、组合升级
# ------------------------------------------------------------------------------------------

def test_link_to_the_streamers_own_tiktok_page_is_exempt_whatever_the_case_or_prefix():
    for url in ("https://www.tiktok.com/@luna_demo/video/123 este es el producto?",
                "https://www.tiktok.com/@Luna_Demo/video/123",
                "https://m.tiktok.com/@luna_demo", "tiktok.com/@luna_demo/live",
                "https://TikTok.com/@luna_demo?lang=es"):
        assert judge(url)["lvl"] is None, url
    assert judge("https://www.tiktok.com/@otra_tienda/video/1")["lvl"] == "alert"
    assert judge("https://www.tiktok.com/@luna_demo_fake/video/1")["lvl"] == "alert"   # 前缀相同不算


def test_link_inside_a_question_is_amber_and_not_sent_to_the_model():
    r = judge("lo vi en amazon.com mas barato?")
    assert (r["lvl"], r["amb"], r["cats"], r["tier"], r["route"]) == \
        ("amber", "question_link", ["promo_link"], "struct", None)
    assert judge("¿donde compro? tienda.mx")["amb"] == "question_link"
    assert judge("lo vi en amazon.com mas barato")["lvl"] == "alert"


def test_a_tail_tag_cannot_turn_a_link_into_a_question():
    """链接的问句豁免只认 ?/¿ 和疑问词开头，不认句尾追问：不能靠结尾加个 no 躲过去。"""
    assert judge("visita tienda.com mas barato no")["lvl"] == "alert"
    assert judge("visita tienda.com mas barato verdad")["lvl"] == "alert"


def test_link_evasions_are_unwrapped():
    for text in ("visita tienda punto com", "visita tienda dot com", "tienda[.]com", "hxxp://tienda.xyz",
                 "wa . me / 5215512345678", "t . me/ejemplo"):
        assert judge(text)["lvl"] == "alert", text


def test_links_are_never_auto_markable_and_flag_nothing_in_a_question_for_the_model():
    r = judge("visita www.tiendabarata.com")
    assert r["tier"] == "struct" and not cg.auto_markable(r)


def test_local_mine_and_exempt_domains_are_not_redirects(tmp_path):
    (tmp_path / "mine.txt").write_text("# 自家\nmarcaejemplo.com\n5598765432\n@marcaejemplo\n", encoding="utf-8")
    (tmp_path / "exempt_domains.txt").write_text("correos-ejemplo.mx\n", encoding="utf-8")
    rules = cg.load_rules(FIX, tmp_path)
    assert judge("pedido en www.marcaejemplo.com", rules=rules)["lvl"] is None
    assert judge("rastreo en correos-ejemplo.mx/track", rules=rules)["lvl"] is None
    own = judge("escribeme al 55 9876 5432", rules=rules)
    assert (own["lvl"], own["tier"]) == ("note", "clue")              # 号码是自己的：只剩 offer 话术这条线索
    assert judge("escribeme al 55 9876 5432")["lvl"] == "alert"
    assert judge("siganme en @marcaejemplo")["lvl"] == "alert"
    assert judge("siganme en @marcaejemplo", rules=rules)["lvl"] == "note"     # 账号是自己的：只剩 offer 话术
    assert judge("pedido en www.otra-tienda.com", rules=rules)["lvl"] == "alert"


def test_defaults_dir_may_carry_an_exempt_domains_file(tmp_path):
    """TikTok Shop 商品链接的写法要用户贴实例核对后才写进 defaults/exempt_domains.txt，机制先留着。"""
    for f in FIX.glob("*.txt"):
        (tmp_path / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "exempt_domains.txt").write_text("shop-ejemplo.test/producto/\n", encoding="utf-8")
    rules = cg.load_rules(tmp_path, NO_LOCAL)
    assert judge("mira https://shop-ejemplo.test/producto/123", rules=rules)["lvl"] is None
    assert judge("mira https://otro.test/producto/123", rules=rules)["lvl"] == "alert"


@pytest.mark.parametrize("text,count", [
    ("mi número es 5512345678", 1), ("+504 9999 8888 escribeme", 1), ("9999-8888", 1),
    ("llama al 55.1234.5678", 1), ("+5215512345678", 1),
])
def test_phone_numbers_are_found_with_separators(text, count):
    assert len(cg.phone_hits(cg.light(text))) == count


@pytest.mark.parametrize("text", [
    "$1,299.00 pesos", "350 pesos", "12/05/1990", "guia 123456789012", "pedido #1234567890",
    "1234567890123456", "tengo 2 gatos y 3 perros", "son 1,500 lempiras", "folio: 5512345678",
])
def test_phone_exclusions_money_dates_order_numbers_and_long_numbers(text):
    assert cg.phone_hits(cg.light(text)) == [], text


def test_thirteen_digits_need_a_plus_to_count_as_a_phone_number():
    assert cg.phone_hits("5215512345678") == []
    assert len(cg.phone_hits("+5215512345678")) == 1


def test_spelled_digits_count_as_a_phone_number():
    assert cg.phone_hits("cinco cinco uno dos tres cuatro cinco seis siete ocho") == ["spelled"]


def test_phone_alone_and_with_order_words_are_only_notes():
    for text in ("5512345678", "mi numero es 5512345678 para el pedido", "55 1234 5678 quiero comprar"):
        r = judge(text)
        assert (r["lvl"], r["cats"], r["tier"], r["route"]) == ("note", ["promo_contact"], "struct", None), text


def test_phone_with_offer_pull_or_handle_cue_is_red_but_structural():
    for text in ("escribeme al 5512345678", "mandame mensaje 5512345678", "vengan a mi live 5512345678",
                 "siganme en @ejemplo_tienda"):
        r = judge(text)
        assert (r["lvl"], r["tier"]) in (("alert", "struct"), ("alert", "red")), text
        assert not cg.auto_markable(r) or r["tier"] == "red"
    assert judge("escribeme al 5512345678")["tier"] == "struct"
    # 顾客下单语境：有拉人话术时不降级
    assert judge("vengan a mi live 5512345678 quiero")["lvl"] == "alert"


def test_handle_alone_is_not_even_a_note_and_the_streamers_own_handle_never_counts():
    assert judge("sigan a @ejemplo_tienda")["lvl"] is None
    assert judge("hola @luna_demo!")["lvl"] is None
    assert judge("ig: ejemplo_tienda")["lvl"] is None


def test_whatsapp_word_alone_and_customer_asking_for_contact_are_never_reported():
    for text in ("whatsapp", "¿tienen whatsapp?", "do you have whatsapp?", "dame tu telegram",
                 "me pasan el link de la tienda"):
        assert judge(text)["lvl"] is None, text


def test_scam_weak_combos_are_structural_red_and_single_is_a_clue():
    r = judge("invierte en ti")
    assert (r["lvl"], r["tier"]) == ("note", "clue")
    r = judge("gana dinero y trabajo desde casa")
    assert (r["lvl"], r["tier"], r["cats"]) == ("alert", "struct", ["promo_scam"])
    r = judge("invierte aqui tienda-ejemplo.com")
    assert r["lvl"] == "alert" and "promo_link" in r["cats"]
    r = judge("gana dinero desde casa 5512345678")
    assert (r["lvl"], r["tier"]) == ("alert", "struct")


def test_competitor_file_turns_a_price_war_line_into_a_redirect(tmp_path):
    (tmp_path / "competitors.txt").write_text("# 虚构竞品\nTienda Rival\n", encoding="utf-8")
    rules = cg.load_rules(FIX, tmp_path)
    r = judge("mas barato en tienda rival", rules=rules)
    assert (r["lvl"], r["cats"], r["tier"]) == ("alert", ["promo_pull"], "struct")
    assert judge("mas barato en tienda rival?", rules=rules)["lvl"] == "note"     # 问句：回到线索档的 lure
    assert judge("mas barato en tienda rival")["lvl"] == "note"                  # 没有名单：只是 lure
    assert judge("conozco tienda rival", rules=rules)["lvl"] is None


# ------------------------------------------------------------------------------------------
# 累计（只在 insult 线索档之间算，结果是琥珀）
# ------------------------------------------------------------------------------------------

def seq(g, events):
    return [g.judge(t, user=u, ts=ts, cid="c%d" % i) for i, (u, t, ts) in enumerate(events)]


def test_two_clue_tier_insults_from_one_viewer_in_10_minutes_make_the_second_amber():
    r1, r2 = seq(guard(), [("u1", "es ridículo el precio", 1000), ("u1", "eres tonta", 1300)])
    assert (r1["lvl"], r2["lvl"], r2["amb"], r2["route"]) == ("note", "amber", "accumulated", "confirm")
    assert "accumulated" in r2["tags"] and not cg.auto_markable(r2)


def test_accumulation_needs_the_same_viewer_and_the_10_minute_window():
    a, b = seq(guard(), [("u1", "es ridículo el precio", 1000), ("u2", "eres tonta", 1100)])
    assert (a["lvl"], b["lvl"]) == ("note", "note")
    a, b = seq(guard(), [("u1", "es ridículo el precio", 1000), ("u1", "eres tonta", 1700)])
    assert (a["lvl"], b["lvl"]) == ("note", "note")


def test_review_example_jaja_eres_tonta_five_minutes_later_is_amber_not_red():
    """评审例句 1：第一版第二句是红（accumulated），会自动加入关注名单。"""
    r1, r2 = seq(guard(), [("u1", "jaja eres tonta", 0), ("u1", "tonta jajaja eres bien tonta", 300)])
    assert r1["lvl"] == "note"
    assert (r2["lvl"], r2["amb"]) == ("amber", "accumulated")
    assert not cg.auto_markable(r2)


def test_watch_words_also_accumulate_but_flood_offer_and_phone_notes_do_not():
    a, b = seq(guard(), [("u1", "pinche precio", 0), ("u1", "pinche calor", 100)])
    assert (a["lvl"], b["lvl"], b["amb"]) == ("note", "amber", "accumulated")
    # 评审例句 2、3：offer / 电话的 note 不累计
    a, b = seq(guard(), [("u2", "mandame mensaje por privado", 0), ("u2", "escribeme al privado porfa", 100)])
    assert (a["lvl"], b["lvl"], b["amb"]) == ("note", "note", None)
    a, b = seq(guard(), [("u3", "mi numero es 5512345678 para el pedido", 0),
                         ("u3", "mi numero es 5512345678 para el pedido", 120)])
    assert (a["lvl"], b["lvl"]) == ("note", "note")


def test_vocative_and_self_do_not_take_part_in_accumulation():
    a, b = seq(guard(), [("u1", "gorda", 0), ("u1", "gorda", 10)])
    assert (a["lvl"], b["lvl"]) == ("note", "note")


def test_accumulate_red_is_a_red_tier_switch_default_off(tmp_path):
    assert RULES.thresholds["accumulate_red"] is False
    (tmp_path / "thresholds.txt").write_text("accumulate_red: on\n", encoding="utf-8")
    rules = cg.load_rules(FIX, tmp_path)
    assert rules.thresholds["accumulate_red"] is True
    a, b = seq(guard(rules), [("u1", "es ridículo el precio", 0), ("u1", "eres tonta", 100)])
    assert (a["lvl"], b["lvl"], b["tier"]) == ("note", "alert", "clue")
    assert not cg.auto_markable(b)           # 线索档来的红条：永远不自动加入


# ------------------------------------------------------------------------------------------
# 自动加入的口径与豁免对象
# ------------------------------------------------------------------------------------------

def test_auto_markable_only_for_a_clean_red_tier_hit():
    assert cg.auto_markable(judge("eres una pendeja"))
    assert cg.auto_markable(judge("te voy a matar"))
    assert cg.auto_markable(judge("reporten este live"))
    for text in ("es una estafa",                         # 琥珀
                 "https://www.tiktok.com/@otra/video/1",   # 链接：结构信号
                 "escribeme al 5512345678",                # 电话组合：结构信号
                 "es ridículo el precio",                  # note
                 "gorda",                                  # vocative
                 "es estafa o no",                         # question_exempt
                 "dicen que eres pendeja"):                # quote
        assert not cg.auto_markable(judge(text)), text
    assert not cg.auto_markable(None)


def test_anchor_mod_trusted_and_the_streamer_are_exempt_whole():
    g = guard(trusted=["12345"])
    for kw in ({"role": "anchor"}, {"role": "mod"}, {"user": "12345"}, {"user": "Luna_Demo"}):
        user = kw.pop("user", "u1")
        r = g.judge("eres una pendeja www.x.com", user=user, ts=1.0, **kw)
        assert (r["lvl"], r["tags"]) == (None, ["trusted"])
    assert g.judge("eres una pendeja", user="u9", ts=2.0)["lvl"] == "alert"


def test_trusted_file_lists_uids_per_streamer(tmp_path):
    (tmp_path / "trusted").mkdir()
    (tmp_path / "trusted" / "luna_demo.txt").write_text("# 房管\n111\n222\n", encoding="utf-8")
    assert cg.load_trusted("Luna_Demo", tmp_path) == {"111", "222"}
    assert cg.load_trusted("otra", tmp_path) == frozenset()
    assert cg.load_trusted("../etc", tmp_path) == frozenset()
    assert cg.load_trusted("", tmp_path) == frozenset()


def test_same_comment_id_twice_is_a_replay_not_a_second_comment():
    g = guard()
    first = g.judge("eres una pendeja", user="u1", ts=1.0, cid="abc")
    again = g.judge("eres una pendeja", user="u1", ts=2.0, cid="abc")
    assert first["lvl"] == "alert"
    assert (again["lvl"], again["tags"]) == (None, ["dup_id"])


def test_seen_id_table_is_bounded():
    g = guard()
    for i in range(cg.SEEN_IDS + 500):
        g.judge("hola", user="u1", ts=float(i), cid="id%d" % i)
    assert len(g._seen) == cg.SEEN_IDS


def test_structure_only_mode_runs_links_phones_and_flood_but_not_the_lexicon():
    g = guard()
    r = g.judge("hijueputa", user="u1", ts=1.0, structure_only=True)
    assert r["lvl"] is None
    r = g.judge("hijueputa www.tienda.com", user="u1", ts=2.0, structure_only=True)
    assert (r["lvl"], r["cats"]) == ("alert", ["promo_link"])
    r = g.judge("5512345678", user="u1", ts=3.0, structure_only=True)
    assert r["lvl"] == "note"


def test_no_user_key_means_no_flood_counting():
    g = guard()
    results = [g.judge("hola amigos como estan", user=None, ts=1000.0 + i, cid=str(i)) for i in range(10)]
    assert all(r["lvl"] is None for r in results) and g.last_flood == []


def test_cost_budget_trips_after_50_ms_in_a_second_and_holds_for_5_seconds():
    b = cg.CostBudget()
    assert not b.structure_only(0.0)
    b.charge(0.0, 20)
    b.charge(0.4, 20)
    assert not b.structure_only(0.4)
    b.charge(0.8, 20)                                   # 滚动 1 秒累计 60 ms > 50 ms
    assert b.structure_only(0.9) and b.trips == 1
    assert b.structure_only(5.7)
    assert not b.structure_only(5.9)
    b.charge(10.0, 30)                                  # 旧的花费已滚出窗口
    assert not b.structure_only(10.1)


# ------------------------------------------------------------------------------------------
# 长文与性能
# ------------------------------------------------------------------------------------------

def test_long_text_matches_only_head_60_and_tail_20_words_but_structure_signals_survive():
    filler = " ".join("palabra%s" % chr(97 + i % 20) for i in range(100))
    # 红条词在中间（第 60~80 个词之外）：不匹配，这是有意的取舍（防假短语 + 性能）
    mid = ("relleno " * 70) + "hijueputa " + ("relleno " * 70)
    assert judge(mid)["lvl"] is None
    # 头部和尾部的红条词照常命中
    assert judge("hijueputa " + filler)["lvl"] == "alert"
    assert judge(filler + " hijueputa")["lvl"] == "alert"
    # 链接/电话/@ 在全文上抓，放在最后也跑不掉
    assert judge(filler + " www.tienda-ejemplo.com")["cats"] == ["promo_link"]
    assert judge(filler + " escribeme al 5512345678")["lvl"] == "alert"


def test_head_and_tail_do_not_join_into_a_false_phrase():
    """头 60 词的最后一个词 no + 尾 20 词的第一个词 compren：中间被断点词隔开，不能拼出「no compren」。"""
    head = ("relleno " * 59) + "no"
    tail = "compren " + ("relleno " * 19)
    text = head + " " + ("medio " * 30) + tail
    assert judge(text)["lvl"] is None
    assert cg.clip_words(cg.fold(text)).split().count(cg.BREAK_WORD) == 1
    assert judge("no compren " + ("relleno " * 5))["lvl"] == "alert"
    # 反证：没有中间那段，「no compren」是挨着的
    assert judge(head + " compren " + ("relleno " * 19))["lvl"] == "alert"


ADVERSARIAL = {
    "digits spaced": "1 " * 500, "digits": "1" * 1000, "dots": "." * 1000, "a.b.c": "a.b.c.d.e." * 100,
    "mixed leet": "m1 " * 333, "words": "estafa " * 142, "phone-like": "55 1234 5678 " * 80,
    "url-like": "www.a.com " * 100, "at": "@abc " * 250, "caps": "HOLA BELLA " * 90,
    "question": "es estafa? " * 90, "emoji": "🤡" * 500, "no": "no " * 330,
    "no es": "no es " * 160, "nbsp": "a\u00a0" * 500, "punto com": "tienda punto com " * 58,
    "brackets": "a[.]" * 250, "wa me": "wa . me / " * 100, "tiktok urls": "tiktok.com/@x " * 70,
    "spaced letters": "e s " * 250, "inverted ?": "¿" * 1000, "keycap": "5️⃣" * 300,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_1000_char_adversarial_strings_stay_under_20_ms(name):
    """I3：规则判定同步完成，不让事件循环久占。原型实测最坏约 8 ms，CI 余量放到 20 ms；
    取 5 次里最快的一次，免得一次调度抖动判失败。"""
    g = guard()
    text = ADVERSARIAL[name][:1000]
    best = min(_timed(g, text, i) for i in range(5))
    assert best < 20.0, "%s: %.1f ms" % (name, best)


def _timed(g, text, i):
    t0 = time.perf_counter()
    g.judge(text, user="adv%d" % i, ts=1000.0 + i * 1000, cid="adv-%s-%d" % (text[:5], i))
    return (time.perf_counter() - t0) * 1000.0


# ------------------------------------------------------------------------------------------
# 词表机制：加载、本机叠加、体检
# ------------------------------------------------------------------------------------------

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


def test_fixture_lexicon_loads_clean_and_has_all_three_tiers():
    assert RULES.warnings == []
    counts = RULES.counts
    for key in (("insult", "strong"), ("accuse", "call"), ("promo", "pull")):
        assert counts[key] > 0
    assert counts[("insult", "medium")] > 0 and counts[("interact", "words")] > 0
    assert RULES.count > 300 and len(RULES.hash) == 12


def test_entries_are_folded_on_load_so_the_written_form_does_not_matter(tmp_path):
    d = small_defaults(tmp_path, insult="[strong]\nEstáfa Señora\nÑANDÚ\n")
    rules = cg.load_rules(d, NO_LOCAL)
    assert rules.entries[("insult", "strong")] == ["estafa senora", "nandu"]
    assert rules.originals[("insult", "strong")]["estafa senora"] == "Estáfa Señora"


def test_unknown_section_entry_before_section_and_bad_regex_are_reported_as_codes(tmp_path):
    d = small_defaults(tmp_path, insult="suelto\n[strng]\nfoo\n[strong]\nre:(abc\nre:perdí \\d+ kilos\nbar # nota\n")
    rules = cg.load_rules(d, NO_LOCAL)
    reasons = {(w["section"], w["reason"]) for w in rules.warnings}
    assert (None, "entry_before_section") in reasons
    assert ("strng", "unknown_section") in reasons
    assert ("strong", "invalid_regex") in reasons
    assert ("strong", "trailing_comment") in reasons
    assert any(w["reason"] in ("accent", "punctuation") for w in rules.warnings)
    assert all(set(w) == {"src", "file", "section", "line", "entry", "reason"} for w in rules.warnings)
    assert all(isinstance(w["reason"], str) and w["reason"].isidentifier() for w in rules.warnings)


def test_thresholds_file_overrides_and_reports_bad_lines(tmp_path):
    d = small_defaults(tmp_path, thresholds="user_burst_alert: 7/10\nsame_text_note: x/y\nnope: 1\nbrigade_jaccard: 0.9\n"
                                            "accumulate_red: maybe\ncaps_lead: 0.5/4\n")
    rules = cg.load_rules(d, NO_LOCAL)
    assert rules.thresholds["user_burst_alert"] == (7, 10)
    assert rules.thresholds["brigade_jaccard"] == 0.9
    assert rules.thresholds["caps_lead"] == (0.5, 4)
    assert rules.thresholds["same_text_note"] == (3, 60)               # 坏值：保留默认
    assert rules.thresholds["accumulate_red"] is False
    bad = sorted((w["reason"], w["entry"]) for w in rules.warnings)
    assert bad == [("bad_threshold", "accumulate_red: maybe"), ("bad_threshold", "same_text_note: x/y"),
                   ("unknown_threshold", "nope: 1")]


def test_local_files_append_and_minus_lines_switch_a_default_off(tmp_path):
    assert judge("ctm")["lvl"] == "alert"
    write(tmp_path, "insult.txt", "# 本机\n-CTM\n[strong]\nzoquete\n")
    write(tmp_path, "accuse.txt", "[call]\nboycott marca ejemplo\n")
    rules = cg.load_rules(FIX, tmp_path)
    assert judge("ctm", rules=rules)["lvl"] is None                     # 这条默认被关掉
    assert judge("eres un zoquete", rules=rules)["lvl"] == "alert"       # 追加
    assert judge("boycott marca ejemplo", rules=rules)["cats"] == ["accuse_call"]
    assert judge("eres una idiota", rules=rules)["lvl"] == "alert"       # 没动别的
    assert judge("ptm", rules=rules)["lvl"] == "alert"


def test_minus_line_matches_by_folded_form(tmp_path):
    write(tmp_path, "insult.txt", "-PENDÉJA\n")
    rules = cg.load_rules(FIX, tmp_path)
    assert "pendeja" not in rules.entries[("insult", "strong")]


def test_same_entry_in_two_sections_keeps_the_stricter_one(tmp_path):
    d = small_defaults(tmp_path, insult="[medium]\ncabron\n[strong]\ncabron\n")
    rules = cg.load_rules(d, NO_LOCAL)
    assert rules.entries[("insult", "strong")] == ["cabron"]
    assert ("insult", "medium") not in rules.entries or rules.entries[("insult", "medium")] == []


def test_rules_hash_changes_when_an_entry_or_a_threshold_changes(tmp_path):
    base = cg.load_rules(FIX, NO_LOCAL).hash
    assert cg.load_rules(FIX, NO_LOCAL).hash == base
    write(tmp_path, "insult.txt", "[strong]\nzoquete\n")
    assert cg.load_rules(FIX, tmp_path).hash != base


def test_missing_defaults_dir_and_missing_files_never_raise(tmp_path):
    rules = cg.load_rules(tmp_path / "nope", tmp_path / "nope-local")
    assert rules.count == 0
    assert cg.CommentGuard(rules).judge("hijueputa", user="u", ts=1.0)["lvl"] is None
    # 链接和刷屏是结构信号，不靠词表
    assert cg.CommentGuard(rules).judge("www.tienda.com", user="u", ts=1.0)["lvl"] == "alert"


def test_non_utf8_lexicon_file_is_reported_not_fatal(tmp_path):
    d = small_defaults(tmp_path)
    (d / "insult.txt").write_bytes("[strong]\nbueno\ncaf\xe9 horrible\n".encode("latin-1"))
    rules = cg.load_rules(d, NO_LOCAL)
    reasons = {w["reason"] for w in rules.warnings}
    assert "decode_error" in reasons and "skipped_line" in reasons
    assert "bueno" in rules.entries[("insult", "strong")]


# ------------------------------------------------------------------------------------------
# 正式默认词表：红条档没有擅自打开（CLAUDE.md 第五条、spec I7）
# ------------------------------------------------------------------------------------------

def test_defaults_load_clean_and_enable_only_the_clue_and_exempt_tiers():
    rules = cg.load_rules(DEFAULTS, NO_LOCAL)
    assert rules.warnings == []
    enabled_red = {key for key, n in rules.counts.items() if n and cg.SECTIONS[key[0]][key[1]] == cg.RED}
    assert enabled_red == set(RED_CONFIRMED), "默认词表里有没经用户确认的红条档段：%s" % sorted(enabled_red)
    clue = {key for key, n in rules.counts.items() if n and cg.SECTIONS[key[0]][key[1]] == cg.CLUE}
    assert {("insult", "medium"), ("insult", "watch"), ("insult", "sexual-soft"), ("accuse", "institution"),
            ("accuse", "negative"), ("promo", "offer"), ("promo", "lure"), ("promo", "scam-weak"),
            ("cues", "irony"), ("cues", "neg-emoji")} <= clue
    exempt = {key for key, n in rules.counts.items() if n and cg.SECTIONS[key[0]][key[1]] == cg.EXEMPT}
    assert {("insult", "exempt-phrases"), ("accuse", "exempt-phrases"), ("promo", "ask"),
            ("interact", "words"), ("interact", "places"), ("interact", "positive"),
            ("interact", "positive-emoji")} <= exempt


def test_defaults_accumulate_red_is_off_and_only_commented_in_the_pending_block():
    text = (DEFAULTS / "thresholds.txt").read_text(encoding="utf-8")
    active = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any(ln.startswith("accumulate_red") for ln in active)
    assert "# accumulate_red: on" in text
    assert cg.load_rules(DEFAULTS, NO_LOCAL).thresholds["accumulate_red"] is False


def test_with_the_default_lexicon_red_words_are_inert_but_structure_signals_still_work():
    rules = cg.load_rules(DEFAULTS, NO_LOCAL)
    for text in ("eres una pendeja", "te voy a matar", "es una estafa", "reporten este live", "fuck you"):
        r = judge(text, rules=rules)
        assert r["lvl"] is None and not cg.auto_markable(r), text
    r = judge("visita www.tienda-ejemplo.com", rules=rules)
    assert (r["lvl"], r["tier"]) == ("alert", "struct")
    assert judge("es ridículo el precio", rules=rules)["lvl"] == "note"     # 线索档照常记 note


def test_default_pending_block_follows_the_documented_convention():
    from tools.guard_collision import PENDING_MARK, count_pending, uncomment_pending

    def block_of(text):
        lines = text.splitlines()
        marks = [n for n, ln in enumerate(lines) if PENDING_MARK.match(ln.strip())]
        assert len(marks) == 1, "每个文件恰好一个「待业务确认」块"
        return lines[marks[0] + 1:]

    first = {"insult": "threat", "accuse": "call"}                  # 用户先确认的两小段排在块的最前面
    for name in ("insult", "accuse", "promo"):
        text = (DEFAULTS / (name + ".txt")).read_text(encoding="utf-8")
        assert count_pending(text) > 10, name
        headers = [ln.strip()[1:].strip() for ln in block_of(text)
                   if ln.strip().startswith("#") and not ln.strip().startswith("##")
                   and ln.strip()[1:].strip().startswith("[")]
        if name in first:
            assert headers[0] == "[%s]" % first[name], (name, headers[:3])
        # 块里单个 # 开头的行只能是词条（词表是西语/英语，不会有中文）；说明必须用 ##
        active = block_of(uncomment_pending(text))
        assert not [ln for ln in active if ln and not ln.startswith("#")
                    and any(0x4E00 <= ord(ch) <= 0x9FFF for ch in ln)], name
        # 块外没有被碰过：启用的部分在块之前，块之后不再有启用的行
        assert not [ln for ln in block_of(text) if ln.strip() and not ln.lstrip().startswith("#")], name


def test_default_pending_block_loads_clean_when_switched_on():
    """候选词条本身是合法的：去掉注释之后没有体检警告，红条档的条数从 0 变成上百。"""
    from tools.guard_collision import build_all_rules

    enabled, allr, tmp = build_all_rules(DEFAULTS, NO_LOCAL)
    try:
        assert allr.warnings == [], allr.warnings
        assert allr.counts[("insult", "threat")] == 10 and allr.counts[("accuse", "call")] > 20
        assert sum(1 for k, n in enabled.counts.items() if n and cg.SECTIONS[k[0]][k[1]] == cg.RED) == 0
        assert allr.count > enabled.count + 150
    finally:
        tmp.cleanup()
