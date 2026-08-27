"""DeepL 月额度用尽（456）的自动降级。

不切换的话，额度用尽后每条字幕都「翻译失败」直到月底——DeepL 成为默认档的
前提就是这个洞必须补上。降级是一次性的、当条重试、界面有常驻提示，且用户
在下拉框里的选择不动：下月额度恢复后自动回到 DeepL。
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app.audit import AuditLog
from app.telemetry import Telemetry
from app.translator import CachedTranslator, DeepLTranslator


def run(coro):
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def deepl(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "test-key:fx")
    return DeepLTranslator()


def test_456_sets_quota_flag(deepl):
    async def api(method, path, form=None, body=None):
        return 456, {"message": "Quota exceeded"}

    deepl._api = api
    assert run(deepl.translate("hola", "zh-CN")) is None
    assert deepl.quota_exhausted is True


def test_429_rate_limit_does_not_set_quota_flag(deepl):
    """限流等两分钟就好，绝不能触发按月的降级。"""
    async def api(method, path, form=None, body=None):
        return 429, {}

    deepl._api = api
    assert run(deepl.translate("hola", "zh-CN")) is None
    assert deepl.quota_exhausted is False


def test_usage_reads_endpoint(deepl):
    async def api(method, path, form=None, body=None):
        assert (method, path) == ("GET", "/v2/usage")
        return 200, {"character_count": 123456, "character_limit": 1000000}

    deepl._api = api
    assert run(deepl.usage()) == {"used": 123456, "limit": 1000000}


def test_usage_failure_returns_none(deepl):
    async def api(method, path, form=None, body=None):
        return 500, {}

    deepl._api = api
    assert run(deepl.usage()) is None


def test_cached_wrapper_exposes_quota_flag(deepl):
    wrapped = CachedTranslator(deepl)
    assert wrapped.quota_exhausted is False
    deepl.quota_exhausted = True
    assert wrapped.quota_exhausted is True


# ---- 管线层：切换 + 当条重试 ----

class FakeServer:
    def __init__(self):
        self.sent = []
        self.config = {}

    async def broadcast(self, msg):
        self.sent.append(msg)

    async def status(self, *a, **k):
        pass


class ExhaustedDeepL:
    name = "deepl"
    quota_exhausted = True

    async def translate(self, *a, **k):
        return None

    async def close(self):
        self.closed = True


class LocalEngine:
    name = "hymt2"

    async def translate(self, *a, **k):
        return "你好"

    async def close(self):
        pass


def _pipeline(tmp_path, old):
    from app.pipeline import Pipeline

    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p.glossary = None
    p.telemetry = Telemetry()
    p.translator = old
    p._quality = {}
    p.args = SimpleNamespace(translator="deepl", translator_note=None)
    p.audit = AuditLog(room_url="https://www.tiktok.com/@real/live",
                       log_dir=tmp_path)
    return p


def test_quota_exhaustion_swaps_engine_and_retries(monkeypatch, tmp_path):
    # 走生产的真实形态：create_translator 返回的引擎都套着 CachedTranslator，
    # quota 标志必须穿过它的 property 透传链才算数
    old = CachedTranslator(ExhaustedDeepL())
    p = _pipeline(tmp_path, old)
    monkeypatch.setattr("app.pipeline.create_translator",
                        lambda name: CachedTranslator(LocalEngine()))
    run(p._translate_and_update({"id": 1, "text": "hola", "lang": "es",
                                 "target": "zh-CN",
                                 "audio_end_ts": time.time()}))
    p.audit.close()

    assert p.translator.name == "hymt2"               # 引擎已切换
    assert getattr(old.inner, "closed", False)        # 旧引擎已关闭
    assert p.args.translator_note                     # 面板有常驻提示
    notices = [m for m in p.server.sent if m.get("type") == "notice"]
    assert notices and "额度" in notices[0]["text"]
    updates = [m for m in p.server.sent if m.get("type") == "caption_update"]
    assert updates[-1]["translated"] == "你好"        # 当前这条被新引擎补上
    rows = [json.loads(line) for line in open(p.audit.path, encoding="utf-8")]
    tr_row = [r for r in rows if r["type"] == "translation"][0]
    assert tr_row["engine"] == "hymt2"                # 审计如实记录切换后的引擎
    assert tr_row["ok"] is True


def test_fallback_happens_only_once(monkeypatch, tmp_path):
    old = ExhaustedDeepL()
    p = _pipeline(tmp_path, old)
    made = []
    monkeypatch.setattr("app.pipeline.create_translator",
                        lambda name: made.append(name) or LocalEngine())
    run(p._quota_fallback(old))
    again = run(p._quota_fallback(old))       # 旧引用再触发：不再重建
    p.audit.close()
    assert made == ["auto"]
    assert again is p.translator


def test_google_fallback_note_admits_it_is_not_local(monkeypatch, tmp_path):
    """兜底落到 Google 时，提示必须说清「字幕会发送给 Google」——
    合规工具不能一边把数据发给第三方、一边告诉中控是本地引擎。"""
    class GoogleEngine(LocalEngine):
        name = "google"

    old = ExhaustedDeepL()
    p = _pipeline(tmp_path, old)
    monkeypatch.setattr("app.pipeline.create_translator",
                        lambda name: GoogleEngine())
    run(p._quota_fallback(old))
    p.audit.close()
    assert "Google" in p.args.translator_note
    assert "发送给 Google" in p.args.translator_note
    assert "本地引擎" not in p.args.translator_note


def test_manual_switch_during_probe_wins(monkeypatch, tmp_path):
    """探测本地引擎期间用户手动换了引擎：尊重用户的选择，丢弃我们建的。"""
    old = ExhaustedDeepL()
    p = _pipeline(tmp_path, old)
    user_choice = LocalEngine()
    built = LocalEngine()

    def create(name):
        p.translator = user_choice        # 模拟探测期间 set_engine 生效
        return built

    monkeypatch.setattr("app.pipeline.create_translator", create)
    got = run(p._quota_fallback(old))
    p.audit.close()
    assert got is user_choice
    assert p.translator is user_choice
    assert p.args.translator_note is None     # 不再广播过期的降级提示


def test_publish_engine_carries_deepl_usage(monkeypatch, tmp_path):
    class DeepLLike:
        name = "deepl"

        async def usage(self):
            return {"used": 350000, "limit": 1000000}

    p = _pipeline(tmp_path, CachedTranslator(DeepLLike()))
    run(p._publish_engine())
    p.audit.close()
    engine_msgs = [m for m in p.server.sent if m.get("type") == "engine"]
    assert engine_msgs[-1]["usage"] == {"used": 350000, "limit": 1000000}


def test_ordinary_failure_does_not_swap(monkeypatch, tmp_path):
    """普通失败（网络抖动等）绝不能触发降级——缓存那层早有共识：
    一次抖动不该造成任何持久改变。"""
    class FlakyDeepL(ExhaustedDeepL):
        quota_exhausted = False

    old = FlakyDeepL()
    p = _pipeline(tmp_path, old)
    monkeypatch.setattr("app.pipeline.create_translator",
                        lambda name: LocalEngine())
    run(p._translate_and_update({"id": 1, "text": "hola", "lang": "es",
                                 "target": "zh-CN",
                                 "audio_end_ts": time.time()}))
    p.audit.close()
    assert p.translator is old
