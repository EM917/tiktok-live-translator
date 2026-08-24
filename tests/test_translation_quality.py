"""译文质量等级：低等级不得覆盖已生效的高等级结果。

起因是一个竞态。强模型重译走独立协程，不经过翻译队列，而队列可积压 4 条、
单 worker 顺序处理——所以「快译一定先到」是在赌执行顺序。队列一堵，2.3 秒的
强译先落地，随后排到的快译再把它盖回去；而前端的「强模型重译」标记只加不减，
于是屏幕上是快译的内容，旁边却标着强译。标错比不标更有害。
"""
import asyncio

import pytest

from app.pipeline import QUALITY_FAST, QUALITY_STRONG, Pipeline


class FakeServer:
    def __init__(self):
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(msg)

    async def status(self, *a, **k):
        pass


def make():
    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p._quality = {}
    return p


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def publish(p, level, ok=True, text="译文"):
    return run(p._publish_translation(7, text if ok else None, ok, 100.0,
                                      level, "zh-CN"))


def test_fast_cannot_overwrite_strong():
    """队列积压时快译会后到——它不能把已经上屏的强译盖回去。"""
    p = make()
    assert publish(p, QUALITY_STRONG, text="强译") is True
    assert publish(p, QUALITY_FAST, text="快译") is False
    assert [m["translated"] for m in p.server.sent] == ["强译"]


def test_strong_upgrades_fast():
    p = make()
    assert publish(p, QUALITY_FAST, text="快译") is True
    assert publish(p, QUALITY_STRONG, text="强译") is True
    assert [m["translated"] for m in p.server.sent] == ["快译", "强译"]


def test_a_failed_strong_does_not_claim_the_level():
    """强模型对约 2% 的句子返回空。若失败也占住等级，这一条会被永久挡成空白。"""
    p = make()
    assert publish(p, QUALITY_STRONG, ok=False) is True   # 还没有译文，失败可以上报
    assert publish(p, QUALITY_FAST, text="快译") is True   # 快译仍须生效
    assert p.server.sent[-1]["translated"] == "快译"


def test_a_failure_never_erases_a_working_translation():
    p = make()
    publish(p, QUALITY_FAST, text="快译")
    assert publish(p, QUALITY_STRONG, ok=False) is False
    assert p.server.sent[-1]["translated"] == "快译"


@pytest.mark.parametrize("level", [QUALITY_FAST, QUALITY_STRONG])
def test_quality_is_always_reported(level):
    """前端要靠它做二次把关，也要靠它决定标记加还是去。"""
    p = make()
    publish(p, level)
    assert p.server.sent[-1]["quality"] == level


def test_same_level_may_refresh():
    """同等级重发是允许的——例如换目标语言后重译。"""
    p = make()
    publish(p, QUALITY_FAST, text="旧")
    assert publish(p, QUALITY_FAST, text="新") is True
    assert p.server.sent[-1]["translated"] == "新"


# ---- 审计：两种译文必须能分辨 ----

def test_audit_distinguishes_fast_from_strong(tmp_path, monkeypatch):
    """同一个 seq 会同时有快译和强译。若类型相同，事后就无法判断哪条是哪个
    模型翻的——而这正是复核最需要区分的东西。

    真实后果：批量重译工具用 `type == "translation"` 取 baseline，同一 seq
    两条时字典推导取后者，于是强译被当成「原译文」，比对永远得出「没有变化」。
    """
    import json

    from app import audit as audit_mod

    monkeypatch.setattr(audit_mod, "LOG_DIR", tmp_path)
    log = audit_mod.AuditLog(room_url="https://example.com/@x/live")
    log.translation(7, "快译", 300.0, True)
    log.translation_strong(7, "强译", 2300.0, True,
                           "hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M", "banned_term")
    log.close()

    rows = [json.loads(x) for x in
            next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines() if x]
    fast = [r for r in rows if r["type"] == "translation"]
    strong = [r for r in rows if r["type"] == "translation_strong"]
    assert len(fast) == 1 and len(strong) == 1
    assert fast[0]["translated"] == "快译"
    assert strong[0]["translated"] == "强译"
    # 强译必须写明是哪个模型、谁触发的
    assert "7B" in strong[0]["model"]
    assert strong[0]["trigger"] == "banned_term"
