"""流地址解析在 Pipeline 层的重试策略：TikTok 明确「不给程序」（browser_only）时
隔一会儿自动重试几次，其它失败原样抛出、不重试。"""
import asyncio
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app.pipeline import Pipeline
from app.resolver import ResolveError


class StubServer:
    def __init__(self):
        self.config = {}
        self.messages = []

    async def status(self, state, detail=""):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)


def make_pipeline(monkeypatch, tmp_path):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None,
    )
    server = StubServer()
    p = Pipeline(args, server)
    p.BROWSER_ONLY_RETRY_SEC = 0.01
    return p, server


def run(coro):
    return asyncio.run(coro)


def test_browser_only_is_retried_then_succeeds(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    calls = []

    async def flaky(url, cookies=None, cookies_browser="auto"):
        calls.append(url)
        if len(calls) < 3:
            raise ResolveError("4003110", kind="browser_only")
        return "https://pull-flv-x.tiktokcdn-us.com/a.flv"

    import app.resolver as resolver_mod
    monkeypatch.setattr(resolver_mod, "resolve_stream_url", flaky)
    assert run(p._resolve_media("https://www.tiktok.com/@x/live")) == "https://pull-flv-x.tiktokcdn-us.com/a.flv"
    assert len(calls) == 3
    waits = [m for m in server.messages if m.get("type") == "status" and "自动重试" in m.get("detail", "")]
    assert len(waits) == 2                      # 前两次失败各提示一次「N 秒后自动重试」


def test_browser_only_gives_up_with_a_plain_message(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def always(url, cookies=None, cookies_browser="auto"):
        raise ResolveError("4003110", kind="browser_only")

    import app.resolver as resolver_mod
    monkeypatch.setattr(resolver_mod, "resolve_stream_url", always)
    with pytest.raises(ResolveError) as exc:
        run(p._resolve_media("https://www.tiktok.com/@x/live"))
    assert exc.value.kind == "browser_only"
    assert "过几分钟" in str(exc.value)


@pytest.mark.parametrize("kind", ["offline", "not_found", "network", "internal", "unknown", "login"])
def test_other_failures_are_not_retried(monkeypatch, tmp_path, kind):
    p, server = make_pipeline(monkeypatch, tmp_path)
    calls = []

    async def failing(url, cookies=None, cookies_browser="auto"):
        calls.append(url)
        raise ResolveError("x", kind=kind)

    import app.resolver as resolver_mod
    monkeypatch.setattr(resolver_mod, "resolve_stream_url", failing)
    with pytest.raises(ResolveError):
        run(p._resolve_media("https://www.tiktok.com/@x/live"))
    assert len(calls) == 1
