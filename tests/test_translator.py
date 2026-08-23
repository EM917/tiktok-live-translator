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
