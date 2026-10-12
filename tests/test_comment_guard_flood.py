"""弹幕留意的刷屏引擎（app/comment_guard.py 的 FloodEngine + CommentGuard）：C1 单人高频、
C3 同人同句、C2 多账号同句，以及内存有界。

原型 13 条刷屏序列按 spec §4.5 更新（期望值变在哪里，用 `# v2:` 注明）：
  * 刷屏一律不自动加入（tier == "struct"）；
  * 同人同句的问句最多到 note；
  * 多账号同句 v1 只出琥珀事件、不单独标红（主播喊口令时观众会齐刷同一句）。

所有序列都传真实时间戳（CLAUDE.md 第七条：不传 ts，滑动窗口永不过期）。纯 CPU，不碰模型和网络，
不 import main.py。用 tests/fixtures/comment_guard 的测试词表；主播名用虚构的 luna_demo。

这个文件不测的（在 PR-3 的 tests/test_comment_guard_pipeline.py）：`accept` 去重之后弹幕栏只显示 1 条、
升级打在那 1 条上、只发 1 条 comment_guard、guard_event 每秒至多 1 次——它们需要管线接线。这里钉住
的是引擎一侧的前提：哪一条弹幕的标注发生变化，以及守卫自己的内存上界。
"""
import random
from pathlib import Path

import pytest

from app import comment_guard as cg

HERE = Path(__file__).resolve().parent
RULES = cg.load_rules(HERE / "fixtures" / "comment_guard", HERE / "no-such-local-dir")
STREAMER = "luna_demo"


def guard(**kw):
    return cg.CommentGuard(RULES, streamer=STREAMER, **kw)


_cid = [0]


def cid():
    _cid[0] += 1
    return "f%d" % _cid[0]


def run(events, g=None):
    """events: [(ts, 用户, 文本)] 或 4 元组带固定 cid。返回 [(判定, 这条触发的刷屏事件)]。"""
    g = g or guard()
    out = []
    for ev in events:
        ts, user, text = ev[:3]
        r = g.judge(text, user=user, ts=ts, cid=ev[3] if len(ev) > 3 else cid())
        out.append((r, list(g.last_flood)))
    return out


def kinds(item):
    return [(e["kind"], e["lvl"], e["n"]) for e in item[1]]


def lvls(results):
    return [r["lvl"] for r, _ in results]


def brigades(results):
    return [e for _, evs in results for e in evs if e["kind"] == "brigade"]


# ------------------------------------------------------------------------------------------
# 原型 13 条刷屏序列
# ------------------------------------------------------------------------------------------

def test_proto_1_one_user_six_different_sentences_in_8_seconds():
    res = run([(1000 + i * 1.5, "u1", "texto numero %s hermoso" % "abcdef"[i]) for i in range(6)])
    assert [k for r in res for k in kinds(r)][-1] == ("user_burst", "alert", 6)
    assert res[-1][0]["lvl"] == "alert" and res[-1][0]["tier"] == "struct" and res[-1][0]["cats"] == ["flood_user"]
    assert not cg.auto_markable(res[-1][0])                 # v2: 刷屏一律不自动加入


def test_proto_2_three_messages_in_8_seconds_is_normal_chatting():
    res = run([(1000 + i * 3, "u1", t) for i, t in enumerate(["hola", "buenas tardes", "mi amor linda"])])
    assert lvls(res) == [None, None, None]


def test_proto_3_the_same_interaction_word_five_times_is_not_flooding():
    res = run([(1000 + i * 4, "u1", "quiero") for i in range(5)])
    assert lvls(res) == [None] * 5 and all(not evs for _, evs in res)


def test_proto_4_same_sentence_five_times_in_60_seconds_is_red_at_the_fifth():
    res = run([(1000 + i * 10, "u1", "esta transmision es lo mejor del mundo") for i in range(5)])
    assert lvls(res) == [None, None, "note", "note", "alert"]
    assert kinds(res[-1]) == [("same_text", "alert", 5)]
    assert not cg.auto_markable(res[-1][0])


def test_proto_5_same_sentence_three_times_is_only_a_note():
    res = run([(1000 + i * 10, "u1", "esta transmision es lo mejor del mundo") for i in range(3)])
    assert lvls(res) == [None, None, "note"]
    assert res[-1][0]["cats"] == ["flood_repeat"]


def test_proto_6_five_accounts_with_the_same_red_sentence_one_amber_event_each_message_red_on_its_own():
    """v2: 原型这里是 brigade alert。现在多账号同句只出琥珀事件；这句话本身带红条档词条，所以每条
    本来就是红条（事件只是把它们聚在一起）。"""
    res = run([(1000 + i * 3, "u%d" % i, "no compren es estafa") for i in range(5)])
    assert [e["lvl"] for e in brigades(res)] == ["note", "note", "amber"]
    assert brigades(res)[-1]["n"] == 5
    assert all(r["lvl"] == "alert" and r["cats"][0] == "accuse_call" and r["tier"] == "red" for r, _ in res)


def test_proto_7_six_accounts_wishing_happy_birthday_is_not_flooding():
    res = run([(1000 + i * 2, "u%d" % i, "Feliz cumpleaños Luna 🎂") for i in range(6)])
    assert lvls(res) == [None] * 6 and not brigades(res)


def test_proto_8_twelve_accounts_saying_yo_is_an_interaction_not_flooding():
    res = run([(1000 + i * 0.8, "u%d" % i, "yo") for i in range(12)])
    assert lvls(res) == [None] * 12 and not brigades(res)


def test_proto_9_twelve_accounts_with_hearts_is_positive_not_flooding():
    res = run([(1000 + i * 0.8, "u%d" % i, "❤️❤️") for i in range(12)])
    assert lvls(res) == [None] * 12 and not brigades(res)


def test_proto_10_four_accounts_with_the_same_neutral_long_sentence_is_a_note():
    res = run([(1000 + i * 4, "u%d" % i, "Aqui seguimos esperando a que respondan las preguntas del chat de hoy")
               for i in range(4)])
    assert kinds(res[-1]) == [("brigade", "note", 4)]
    assert lvls(res) == [None, None, "note", "note"]


def test_proto_11_five_accounts_with_near_identical_sentences_is_an_amber_event():
    words = ["hoy", "ya", "aqui", "ahora", "mismo"]
    res = run([(1000 + i * 3, "u%d" % i, "vengan al live de maria es mas barato %s" % words[i]) for i in range(5)])
    assert kinds(res[-1]) == [("brigade", "amber", 5)]            # v2: 原型是 alert
    assert res[-1][0]["lvl"] == "amber" and res[-1][0]["amb"] == "brigade"


def test_proto_12_the_same_comment_id_replayed_is_not_flooding():
    res = run([(1000 + i * 0.1, "u1", "hola bella", "same-id") for i in range(8)])
    assert lvls(res) == [None] * 8 and not any(evs for _, evs in res)
    assert res[1][0]["tags"] == ["dup_id"]


def test_proto_13_two_accounts_with_the_same_sentence_is_nothing():
    res = run([(1000, "u1", "no compren es estafa"), (1005, "u2", "no compren es estafa")])
    assert not brigades(res)


# ------------------------------------------------------------------------------------------
# 第二版评审例句里的刷屏行（spec §9）
# ------------------------------------------------------------------------------------------

def test_review_row_five_different_sentences_every_2_seconds_note_at_4_and_5_red_only_at_6():
    texts = ["uno hermoso dia", "dos lindo mundo", "tres bonita casa", "cuatro grande sol", "cinco fuerte mar",
             "seis lento rio"]
    res = run([(2 * i, "u1", t) for i, t in enumerate(texts)])
    assert lvls(res) == [None, None, None, "note", "note", "alert"]
    assert [k for k in kinds(res[5])] == [("user_burst", "alert", 6)]
    assert not cg.auto_markable(res[5][0])


def test_review_row_a_neglected_customer_repeating_a_price_question_is_at_most_a_note():
    res = run([(10 * i, "u1", "cuanto cuesta el de 30 dias?") for i in range(5)])
    assert lvls(res) == [None, None, "note", "note", "note"]
    assert kinds(res[4]) == [("same_text", "note", 5)]            # 第 5 次本该红：问句封顶 note


def test_same_sentence_without_a_question_four_times_is_a_note_and_five_times_red_not_auto_marked():
    res = run([(10 * i, "u1", "mandenme el pedido ya por favor") for i in range(5)])
    assert lvls(res) == [None, None, "note", "note", "alert"]
    assert not cg.auto_markable(res[4][0])


def test_tag_questions_also_cap_the_repeated_sentence_at_note():
    for text in ("cuanto sale el envio verdad", "ya llego mi paquete o no", "este es el precio final cierto"):
        res = run([(10 * i, "u1", text) for i in range(6)])
        assert "alert" not in lvls(res), text


def test_review_row_seven_accounts_with_the_same_slogan_one_amber_event_zero_red():
    res = run([(1.5 * i, "v%d" % i, "quiero el combo rosa") for i in range(7)])
    assert "alert" not in lvls(res)
    ev = brigades(res)
    assert [e["lvl"] for e in ev] == ["note", "note", "amber", "amber", "amber"]
    assert [e["n"] for e in ev] == [3, 4, 5, 6, 7]
    assert len({e["ev"] for e in ev}) == 1                       # 一个事件，账号数原地增长
    assert res[-1][0]["amb"] == "brigade" and not cg.auto_markable(res[-1][0])


def test_review_row_six_accounts_giving_their_giveaway_number_is_one_amber_event():
    res = run([(1.0 * i, "w%d" % i, "participo en el sorteo %d" % (i + 1)) for i in range(6)])
    assert "alert" not in lvls(res)
    ev = brigades(res)
    assert [e["lvl"] for e in ev][-2:] == ["amber", "amber"] and ev[-1]["n"] == 6
    assert len({e["ev"] for e in ev}) == 1


def test_five_accounts_with_the_same_neutral_sentence_in_20_seconds_make_exactly_one_amber_event():
    res = run([(4 * i, "z%d" % i, "alguien me explica como se usa el codigo") for i in range(5)])
    ev = brigades(res)
    assert ev[-1] == {"kind": "brigade", "lvl": "amber", "n": 5, "window": 20, "ev": ev[-1]["ev"]}
    assert "alert" not in lvls(res)


def test_thirty_accounts_spamming_yo_hearts_or_a_birthday_wish_are_not_reported():
    for text in ("yo", "❤❤❤", "🔥🔥", "Feliz cumpleaños!", "felicidades bella", "gracias gracias"):
        res = run([(0.3 * i, "m%d" % i, text) for i in range(30)])
        assert lvls(res) == [None] * 30, text
        assert not brigades(res), text


def test_replaying_the_same_ids_twice_reports_nothing_the_second_time():
    events = [(1000 + i, "u%d" % (i % 3), "hola como estan todos hoy", "id-%d" % i) for i in range(6)]
    g = guard()
    first = run(events, g)
    second = run([(ts + 5, u, t, c) for ts, u, t, c in events], g)
    assert all(r["tags"] == ["dup_id"] and r["lvl"] is None and not evs for r, evs in second)
    assert len(first) == len(second) == 6


def test_forty_accounts_with_the_same_sentence_share_one_event_and_one_cache_key():
    """PR-5 的同句缓存按 fold_key 做键，冲场时 40 个账号发同一句只问一次模型；这里钉住它依赖的前提：
    同一个事件 id、同一个 fold_key。"""
    texts = ["Quiero el combo ROSA!!", "quiero el combo rosa", "quierooo el combo rosa", "QUIERO EL COMBO ROSA"]
    res = run([(0.25 * i, "a%d" % i, texts[i % 4]) for i in range(40)])
    assert len({e["ev"] for e in brigades(res)}) == 1
    assert len({cg.fold_key(t) for t in texts}) == 1


# ------------------------------------------------------------------------------------------
# 时间窗口：必须是真实 ts
# ------------------------------------------------------------------------------------------

def test_the_window_expires_with_real_timestamps_but_never_with_a_constant_one():
    real = run([(40 * i, "u1", "esta transmision es lo mejor") for i in range(6)])
    assert lvls(real) == [None, None, None, None, None, None]        # 每次间隔 40 秒：60 秒窗口里最多 2 条
    frozen = run([(1000.0, "u1", "esta transmision es lo mejor") for _ in range(6)])
    assert lvls(frozen)[-1] == "alert"                               # 反证：ts 不动，窗口永不过期


def test_thresholds_can_be_overridden_per_machine(tmp_path):
    (tmp_path / "thresholds.txt").write_text("user_burst_note: 2/10\nuser_burst_alert: 3/10\n", encoding="utf-8")
    rules = cg.load_rules(HERE / "fixtures" / "comment_guard", tmp_path)
    g = cg.CommentGuard(rules, streamer=STREAMER)
    res = run([(i, "u1", "mensaje %s distinto" % "abc"[i]) for i in range(3)], g)
    assert lvls(res) == [None, "note", "alert"]


def test_burst_of_long_duration_counts_12_in_60_seconds():
    res = run([(5.0 * i, "u1", "mensaje largo numero %s nuevo" % chr(97 + i)) for i in range(12)])
    assert res[-1][0]["lvl"] == "alert" and kinds(res[-1]) == [("user_burst", "alert", 12)]


def test_mixed_users_do_not_borrow_each_others_counts():
    events = []
    for i in range(12):
        events.append((i * 1.0, "u%d" % (i % 4), "texto %s unico" % chr(97 + i)))
    assert lvls(run(events)) == [None] * 12


# ------------------------------------------------------------------------------------------
# 内存有界（§4.5）
# ------------------------------------------------------------------------------------------

def test_user_table_is_capped_at_3000_and_drops_the_500_oldest():
    g = guard()
    for i in range(cg.USER_TABLE):
        g.judge("hola %d" % i, user="u%d" % i, ts=float(i))
    assert len(g.flood.users) == cg.USER_TABLE
    g.judge("hola otro", user="uNew", ts=5000.0)
    assert len(g.flood.users) == cg.USER_TABLE + 1 - cg.USER_TABLE_DROP
    assert "u0" not in g.flood.users and "uNew" in g.flood.users


def test_per_user_history_keeps_only_120_seconds_and_at_most_200_entries():
    g = guard()
    for i in range(300):
        g.judge("mensaje %d" % i, user="u1", ts=1000.0 + i * 0.01)
    assert len(g.flood.users["u1"]) <= cg.USER_HISTORY_MAX
    g.judge("mensaje tardio", user="u1", ts=2000.0)
    assert len(g.flood.users["u1"]) == 1


def test_five_hundred_comments_a_minute_for_ten_minutes_stays_bounded():
    rng = random.Random(7)
    vocab = ["hola", "bella", "precio", "envio", "talla", "color", "gracias", "quiero", "cuanto", "cuesta",
             "mexico", "linda", "hermosa", "pedido", "llego", "bien", "mal", "rapido", "lento", "hoy"]
    g = guard()
    alerts = ambers = 0
    total = 500 * 10
    for i in range(total):
        ts = 1000.0 + i * (60.0 / 500)
        if i % 10 == 0:                                      # 一成是 50 个账号在刷同一句
            user, text = "brig%d" % (i // 10 % 50), "vengan todos a este sorteo gigante de hoy"
        else:
            user = "u%d" % rng.randrange(900)
            text = " ".join(rng.choice(vocab) for _ in range(rng.randrange(3, 7)))
        r = g.judge(text, user=user, ts=ts, cid="c%d" % i)
        alerts += r["lvl"] == "alert"
        ambers += r["lvl"] == "amber"
    assert len(g.flood.users) <= cg.USER_TABLE
    assert all(len(d) <= cg.USER_HISTORY_MAX for d in g.flood.users.values())
    assert len(g.flood.recent) <= cg.RECENT_MAX
    assert len(g._seen) <= cg.SEEN_IDS and len(g._acc) <= cg.USER_TABLE
    # 引擎一侧的「消息上界」：红条/琥珀只会来自真正的刷屏，不会随总条数线性增长
    assert alerts + ambers <= total // 4, (alerts, ambers)


def test_brigade_window_is_capped_even_if_thousands_arrive_in_one_second():
    g = guard()
    for i in range(cg.RECENT_MAX + 300):
        g.judge("mensaje %d distinto" % i, user="x%d" % i, ts=1000.0)
    assert len(g.flood.recent) == cg.RECENT_MAX


def test_flood_engine_works_standalone_without_the_lexicon():
    eng = cg.FloodEngine()
    out = []
    for i in range(4):
        out = eng.feed("u1", "algo unico %d" % i, frozenset(("algo", "unico")), 100.0 + i)
    assert out == [{"kind": "user_burst", "lvl": "note", "n": 4, "window": 10, "ev": None}]
    brig = []
    for i in range(5):
        brig = eng.feed("b%d" % i, "mismo texto largo aqui", frozenset(("mismo", "texto", "largo", "aqui")),
                        200.0 + i)
    assert [e["kind"] for e in brig] == ["brigade"] and brig[0]["lvl"] == "amber" and brig[0]["n"] == 5
    assert pytest.approx(0.8) == eng.th["brigade_jaccard"]
