"""碰撞审计与回放 gate 的工具测试。

这两个工具承担「发现碰撞」和「CI 拦截回归」两项安全职责，第一轮审查
发现的三个缺陷全部出在它们身上而 366 个测试没碰到——工具代码不能裸奔。
"""
import json
from collections import defaultdict

from app.detector import BannedTermDetector
from tools.collision_audit import neighbours
from tools.replay_alerts import session_hits


def _stats(counts):
    count = dict(counts)
    sessions = defaultdict(set)
    streamers = defaultdict(set)
    for tok in count:
        sessions[tok] = {"s1", "s2"}
        streamers[tok] = {"a"}
    return count, sessions, streamers


def test_a_neighbour_that_is_itself_banned_is_never_called_benign():
    """--term 模式的教训：候选词 durar 的邻居 curar 本身就在词表里——
    「撞到另一个违禁词」和「撞到无关合法词」风险含义完全不同，
    已有词表必须完整参与 is_banned 判定。"""
    count, sessions, streamers = _stats({"curar": 20})
    det = BannedTermDetector([])
    rows = neighbours(["durar"], {"curar"}, det, count, sessions, streamers)
    row = [r for r in rows if r["neighbour"] == "curar"][0]
    assert row["is_banned"] is True
    assert row["benign"] is False


def test_a_policy_zeroed_token_is_not_flagged_again():
    """审计要能回答「哪些还没处理」：已经收紧到 0 的词不再列出，
    否则每次全表审计都把处理过的碰撞重标一遍，新碰撞被噪音淹没。"""
    count, sessions, streamers = _stats({"durar": 20})
    det = BannedTermDetector([], fuzzy_policy={"curar": 0})
    assert neighbours(["curar"], set(), det, count, sessions, streamers) == []
    det_no_policy = BannedTermDetector([])
    assert neighbours(["curar"], set(), det_no_policy,
                      count, sessions, streamers)      # 反证：没 policy 会列出


def test_session_hits_with_cooldown_disabled_sees_every_hit(tmp_path):
    """回放 gate 关掉冷却：旧检测器的误报会占用冷却窗口、吞掉几秒后的
    真命中——带着冷却比对，合法的收紧会被误判成「新增命中」。"""
    log = tmp_path / "session-1.jsonl"
    rows = [
        {"type": "segment", "seq": 1, "audio_end_ts": 100.0,
         "raw_text": "les va a durar mucho"},
        {"type": "segment", "seq": 2, "audio_end_ts": 115.0,
         "raw_text": "esto te va a curar el higado"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    with_cooldown = session_hits(log, BannedTermDetector(["curar"]))
    assert with_cooldown == [(1, "curar", "fuzzy")]     # 真命中被冷却吞掉

    old = session_hits(log, BannedTermDetector(["curar"], cooldown_sec=0))
    new = session_hits(log, BannedTermDetector(["curar"], cooldown_sec=0,
                                               fuzzy_policy={"curar": 0}))
    assert (1, "curar", "fuzzy") in old
    assert (2, "curar", "exact") in old                 # 冷却关掉后可见
    assert new == [(2, "curar", "exact")]               # 收紧只移除误报
    assert set(new) - set(old) == set()                 # 没有伪「新增」


def test_short_word_budget_cannot_be_raised_by_policy():
    """policy 只能收紧不能放宽：短词的精确下限、长词的默认上限都压得住
    写大了的 policy 值。"""
    det = BannedTermDetector([], fuzzy_policy={"fda": 2, "quemar": 9})
    assert det._edit_budget("fda") == 0        # 短词下限不被抬高
    assert det._edit_budget("quemar") == 1     # 超出默认按默认执行
