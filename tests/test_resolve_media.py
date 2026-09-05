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


# ---- 用户自带音频源（房间链接 + 直连地址）----

def test_media_override_is_used_and_room_link_keeps_streamer(monkeypatch, tmp_path):
    """有些直播间 TikTok 只把流地址给真正的浏览器，用户可以自带一个直连地址。
    这时房间链接仍然是主输入——弹幕、词表、审计都靠它认主播。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p._media_override = "https://pull-flv-x.tiktokcdn-us.com/a.flv?sign=1"
    called = []

    async def should_not_run(url, cookies=None, cookies_browser="auto"):
        called.append(url)
        raise ResolveError("4003110", kind="browser_only")

    import app.resolver as resolver_mod
    monkeypatch.setattr(resolver_mod, "resolve_stream_url", should_not_run)

    async def ok(url, trusted=False):
        return url

    async def works(url):
        return True

    monkeypatch.setattr(resolver_mod, "_check_media_url", ok)
    monkeypatch.setattr(resolver_mod, "_media_url_works", works)
    got = run(p._resolve_media("https://www.tiktok.com/@bella2/live"))
    assert got == "https://pull-flv-x.tiktokcdn-us.com/a.flv?sign=1"
    assert called == []                       # 自带地址可用时不必再去解析
    # 房间链接照旧决定主播身份（弹幕/词表/审计都用它）
    from app.provenance import streamer_of
    assert streamer_of("https://www.tiktok.com/@bella2/live") == "bella2"


def test_expired_media_override_falls_back_to_normal_resolution(monkeypatch, tmp_path):
    """自带地址过期（签名实测约两周有效）时不能就此卡死：丢掉它，回到正常解析。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p._media_override = "https://pull-flv-x.tiktokcdn-us.com/old.flv?sign=1"

    async def resolved(url, cookies=None, cookies_browser="auto"):
        return "https://pull-flv-x.tiktokcdn-us.com/new.flv"

    async def ok(url, trusted=False):
        return url

    async def broken(url):
        return False

    import app.resolver as resolver_mod
    monkeypatch.setattr(resolver_mod, "resolve_stream_url", resolved)
    monkeypatch.setattr(resolver_mod, "_check_media_url", ok)
    monkeypatch.setattr(resolver_mod, "_media_url_works", broken)
    got = run(p._resolve_media("https://www.tiktok.com/@bella2/live"))
    assert got == "https://pull-flv-x.tiktokcdn-us.com/new.flv"
    assert p._media_override is None          # 失效的地址不再留着拖累重连
    assert any("过期" in (m.get("detail") or "") for m in server.messages)


def test_start_control_message_passes_media_through(monkeypatch, tmp_path):
    """UI 的 start 消息带 media 时要一路传到本场的 _media_override。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    seen = {}

    async def fake_start(url, media=None):
        seen["url"] = url
        seen["media"] = media

    p.start_stream = fake_start
    run(p.handle_control({"type": "start",
                          "url": "https://www.tiktok.com/@bella2/live",
                          "media": "https://pull-flv-x.tiktokcdn-us.com/a.flv"}))
    assert seen == {"url": "https://www.tiktok.com/@bella2/live",
                    "media": "https://pull-flv-x.tiktokcdn-us.com/a.flv"}
