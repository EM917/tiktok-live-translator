"""GoogleWebTranslator 的 429 限流冷却：真实试运行里踩到的场景——
免费接口按 IP 限流后，不能在每条字幕上继续撞。"""
import asyncio

from app.translator import GoogleWebTranslator


class FakeResp:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls = 0
        self.closed = False

    def get(self, url, params=None):
        self.calls += 1
        resp = self.resp

        class Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *a):
                return False

        return Ctx()


def make(resp):
    tr = GoogleWebTranslator()
    fake = FakeSession(resp)

    async def session():
        return fake

    tr.session = session
    return tr, fake


def test_success_parses_segments():
    tr, fake = make(FakeResp(200, [[["你好，", "Hola,", None], ["朋友", "amigo", None]]]))
    out = asyncio.run(tr.translate("Hola, amigo", "zh-CN", source="es"))
    assert out == "你好，朋友"
    assert fake.calls == 1


def test_429_sets_cooldown_and_stops_requests():
    tr, fake = make(FakeResp(429))
    assert asyncio.run(tr.translate("hola", "zh-CN")) is None
    assert fake.calls == 1
    # 冷却期内后续请求直接短路，不再打接口
    assert asyncio.run(tr.translate("hola otra vez", "zh-CN")) is None
    assert fake.calls == 1
    assert tr._cooldown_until > 0


def test_cooldown_expires(monkeypatch):
    tr, fake = make(FakeResp(200, [[["好", "ok", None]]]))
    import time
    tr._cooldown_until = time.time() - 1     # 冷却已过期
    assert asyncio.run(tr.translate("ok", "zh-CN")) == "好"
    assert fake.calls == 1


# ---- 重复句缓存：带货直播话术重复率极高，命中即 0ms ----

class CountingTranslator:
    name = "counting"

    def __init__(self, out="译文"):
        self.calls = 0
        self.out = out

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls += 1
        return self.out

    async def close(self):
        pass


def test_cache_returns_same_result_without_calling_engine():
    from app.translator import CachedTranslator
    inner = CountingTranslator()
    tr = CachedTranslator(inner)
    assert asyncio.run(tr.translate("envío gratis", "zh-CN", source="es")) == "译文"
    assert asyncio.run(tr.translate("envío gratis", "zh-CN", source="es")) == "译文"
    assert inner.calls == 1                      # 第二次没打引擎
    assert (tr.hits, tr.misses) == (1, 1)


def test_cache_key_includes_target_and_source():
    from app.translator import CachedTranslator
    inner = CountingTranslator()
    tr = CachedTranslator(inner)
    asyncio.run(tr.translate("hola", "zh-CN", source="es"))
    asyncio.run(tr.translate("hola", "en", source="es"))      # 目标语言不同
    asyncio.run(tr.translate("hola", "zh-CN", source="auto"))  # 源语言不同
    assert inner.calls == 3


def test_failures_are_not_cached():
    """失败不能进缓存——否则一次网络抖动会把这句话永久钉成「翻译失败」。"""
    from app.translator import CachedTranslator

    class FlakyTranslator(CountingTranslator):
        async def translate(self, text, target, source="auto", glossary=None):
            self.calls += 1
            return None if self.calls == 1 else "成功了"

    inner = FlakyTranslator()
    tr = CachedTranslator(inner)
    assert asyncio.run(tr.translate("hola", "zh-CN")) is None
    assert asyncio.run(tr.translate("hola", "zh-CN")) == "成功了"   # 会重试


def test_cache_evicts_oldest_beyond_capacity():
    from app.translator import CachedTranslator
    inner = CountingTranslator()
    tr = CachedTranslator(inner, capacity=2)
    for word in ("a", "b", "c"):
        asyncio.run(tr.translate(word, "zh-CN"))
    asyncio.run(tr.translate("a", "zh-CN"))       # a 已被挤出
    assert inner.calls == 4


def test_gemma_prompt_is_short():
    """长指令每句都要重新预填充，是纯固定成本：实测 92→35 token 省了 150ms。"""
    from app.translator import OllamaGemmaTranslator
    p = OllamaGemmaTranslator()._prompt("hola", "zh-CN", "es")
    head = p.split("\n\n")[0]
    assert len(head.split()) <= 15
    assert "Spanish" in head and "Simplified Chinese" in head


def test_sampling_options_stay_constant_across_calls():
    """采样参数逐次必须一致——变了 Ollama 会重载模型（实测约 4 秒停顿）。

    `num_predict` 是例外，它按源文长度算（见 predict_cap，用来让捏造在生成
    阶段就吐不出来）。实测过它不触发重载：连续用 48/80/120/200 调用，
    load_duration 始终不足 1ms，总耗时稳定在 24ms。
    """
    from app.translator import OllamaGemmaTranslator, OllamaHyMT2Translator

    for cls in (OllamaGemmaTranslator, OllamaHyMT2Translator):
        assert "num_predict" not in cls._OPTIONS, cls.__name__
        assert cls._OPTIONS["temperature"] == 0, cls.__name__
