"""出厂词表里哪些红条档段已经由用户确认：弹幕留意的几个测试文件共用的一份真相。

用户逐段确认红条档词条（comment_guard/README.md「确认红条档词条」）。确认一段之后，**只改这里的
`RED_CONFIRMED`**（再把词条从 `defaults/*.txt` 里的 `[段名:pending]` 挪到同名的 `[段名]`、同步夹具），
所有读出厂词表的测试都跟着它走，不需要再逐个改期望：

* tests/test_comment_guard_rules.py：出厂词表里已确认的段集合 == RED_CONFIRMED；不在里面的段命中只出琥珀；
* tests/test_comment_guard_lexicon.py：收窄过的词条、新增的候选词条在出厂词表下的期望（同一份期望对夹具
  词表和出厂词表各跑一遍）；
* 同一个模块里的 `check_*` 函数还被「确认流程模拟」那条测试拿去跑一份改过的出厂词表副本，证明按
  README 的步骤确认两段以后，这些测试仍然是绿的。

`RED_CONFIRMED` 里的每一项是 `(文件, 段名)`，例如 `("insult", "threat")`。已确认的段里仍可能留着
没确认的词条（用户逐条确认时），所以对已确认的段，期望是「红条，或者琥珀待确认」都行；不在集合里的段，
一定是琥珀待确认。
"""
from app import comment_guard as cg

# 出厂词表里已确认（不带 :pending）的红条档段。用户 2026-10-13 按确认表的建议确认了 insult/threat 和
# accuse/call 两段里的大部分词条（各留几条在同名的 :pending 段里）
RED_CONFIRMED = frozenset({("insult", "threat"), ("accuse", "call")})

# 判定结果的主类别 -> 它所在的词表文件
FILE_OF_CAT = {"threat": "insult", "insult": "insult", "sexual": "insult", "accuse_call": "accuse",
               "accuse_report": "accuse", "accuse_claim": "accuse", "promo_pull": "promo",
               "promo_scam": "promo"}
RED_SECTIONS = sorted((f, sec) for f, secs in cg.SECTIONS.items() for sec, tier in secs.items()
                      if tier == cg.RED and not cg.is_pending(sec))


def section_of(result):
    """一条判定的红条档来源 (文件, 段)；命中不是来自红条档词表（结构信号、线索档）返回 None。"""
    if not result or result["tier"] != cg.RED or not result["cats"]:
        return None
    return FILE_OF_CAT.get(result["cats"][0]), result["sub"]


def assert_pending(result, text=""):
    """没确认的红条档命中：琥珀、amb == pending、route confirm、绝不自动加入关注名单。"""
    assert (result["lvl"], result["amb"], result["route"]) == ("amber", "pending", "confirm"), (text, result)
    assert "pending" in result["tags"] and not cg.auto_markable(result), (text, result)


def assert_red(result, text=""):
    assert (result["lvl"], result["tier"]) == ("alert", "red"), (text, result)
    assert "pending" not in result["tags"] and cg.auto_markable(result), (text, result)


def assert_flagged_by_shipped(result, text="", confirmed=None):
    """出厂词表下，一条恶意说法该是什么样：它所在的段在 confirmed 里 -> 红条（词条已确认），或者琥珀待确认
    （同一段里这一条还没确认）；不在 -> 一定是琥珀待确认。"""
    confirmed = RED_CONFIRMED if confirmed is None else confirmed
    if section_of(result) in confirmed:
        if result["lvl"] == "alert":
            assert_red(result, text)
        else:
            assert_pending(result, text)
    else:
        assert_pending(result, text)


def check_defaults_state(rules, confirmed=None):
    """出厂词表的状态：载入没有警告；已确认（不带 :pending 且有词条）的红条档段集合 == confirmed；每个红条档段
    不是已确认就是待确认（不会有哪一段两边都空）；线索档和豁免档照常启用。"""
    confirmed = set(RED_CONFIRMED if confirmed is None else confirmed)
    assert rules.warnings == []
    found = {key for key, n in rules.counts.items()
             if n and cg.SECTIONS[key[0]][key[1]] == cg.RED and not cg.is_pending(key[1])}
    assert found == confirmed, \
        "出厂词表里已确认的红条档段和 RED_CONFIRMED 对不上（见 comment_guard/README.md 的确认流程）：%s" % sorted(found)
    pending = {(f, cg.base_sec(sec)) for (f, sec), n in rules.counts.items() if n and cg.is_pending(sec)}
    assert pending | found == set(RED_SECTIONS), "每个红条档段都该有词条：不是已确认就是待确认"
    assert rules.pending_count > 0 or found == set(RED_SECTIONS)
    clue = {key for key, n in rules.counts.items() if n and cg.SECTIONS[key[0]][key[1]] == cg.CLUE}
    assert {("insult", "medium"), ("insult", "watch"), ("insult", "sexual-soft"), ("accuse", "institution"),
            ("accuse", "negative"), ("promo", "offer"), ("promo", "lure"), ("promo", "scam-weak"),
            ("cues", "irony"), ("cues", "neg-emoji")} <= clue
    exempt = {key for key, n in rules.counts.items() if n and cg.SECTIONS[key[0]][key[1]] == cg.EXEMPT}
    assert {("insult", "exempt-phrases"), ("accuse", "exempt-phrases"), ("promo", "ask"),
            ("interact", "words"), ("interact", "places"), ("interact", "positive"),
            ("interact", "positive-emoji")} <= exempt
