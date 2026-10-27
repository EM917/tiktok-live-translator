"""DeepL 额度用完、改发 Google：除了页脚那条一闪而过的 notice，还要挂一条持续提示。

专家审查 U2（2026-10-21，10-22 补核部分成立，范围收窄到这一类）：notice 写进页脚 11px 的淡灰小字，
6 秒后消失、后一条顶掉前一条，直播中设置组又是隐藏的。别的几种走页脚的提示数据还在本机，
这一种是字幕开始发给另一家服务——不能只闪 6 秒。

后端这一步：挂一条 engine:google-fallback（warn，文字沿用那句 notice）。不带 session: 前缀，
跨场保留；中控成功换了引擎、或者翻译器已经不是 Google 时撤。改用本地引擎那一支照旧只发 notice。
页脚 notice 的排队和停留时间在前端那一批（b4-banners）做。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import i18n
from app import pipeline as pipeline_mod
from app.pipeline import Pipeline
from app.server import CaptionServer
from app.viewer import filter_payload

KEY = Pipeline.GOOGLE_FALLBACK_INCIDENT


class RecordingServer(CaptionServer):
    def __init__(self):
        super().__init__(port=0)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(msg)
        await super().broadcast(msg)

    def incidents(self, key):
        return [m for m in self.messages if m.get("type") == "incident" and m.get("id") == key]


class Engine:
    def __init__(self, name):
        self.name = name
        self.last_error = None
        self.cooldown_until = 0.0
        self.quota_exhausted = name == "deepl"
        self.closed = False

    async def translate(self, *a, **k):
        return None

    async def close(self):
        self.closed = True


@pytest.fixture
def pl(monkeypatch, tmp_path):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False,
        cookies_browser="none", translator_note=None)
    p = Pipeline(args, RecordingServer())
    p.args.translator = "deepl"                 # 中控选的是 DeepL（构造时不建真的 DeepL 引擎）

    async def noop(*a, **k):
        return None

    p.run_selfcheck = noop
    p.ensure_local_translator = noop
    return p


def shown(p):
    return (p.server.config.get("incidents") or {}).get(KEY)


def fall_back_to(monkeypatch, p, name):
    monkeypatch.setattr(pipeline_mod, "create_translator", lambda engine: Engine(name))
    p.translator = old = Engine("deepl")
    return asyncio.run(p._quota_fallback(old))


def test_falling_back_to_google_hangs_a_lasting_notice(monkeypatch, pl):
    new = fall_back_to(monkeypatch, pl, "google")
    assert new.name == "google" and pl.translator is new
    item = shown(pl)
    assert item is not None and item["level"] == "warn"
    assert "发送给 Google" in item["text"]                        # 说清字幕开始发给 Google
    assert "Captions are now sent to Google" in i18n.render(item["text"], i18n.EN)
    notices = [m for m in pl.server.messages if m.get("type") == "notice"]
    assert len(notices) == 1 and notices[0]["text"] == item["text"]   # 文字就是那句 notice
    assert not KEY.startswith("session:")


def test_falling_back_to_a_local_engine_only_sends_the_notice(monkeypatch, pl):
    fall_back_to(monkeypatch, pl, "hymt2")
    assert shown(pl) is None
    assert pl.server.incidents(KEY) == []
    assert [m["type"] for m in pl.server.messages].count("notice") == 1


def test_it_survives_into_the_next_session(monkeypatch, pl):
    """下一场开始时只清 session: 开头的提示；下一场还在用 Google，这一条留着。"""
    fall_back_to(monkeypatch, pl, "google")
    asyncio.run(pl._clear_session_incidents())
    assert shown(pl) is not None


def test_switching_engines_in_settings_clears_it(monkeypatch, pl):
    fall_back_to(monkeypatch, pl, "google")
    monkeypatch.setattr(pipeline_mod, "create_translator", lambda engine: Engine(engine))
    asyncio.run(pl.set_engine("deepl"))
    assert pl.translator.name == "deepl"
    assert shown(pl) is None
    assert pl.server.incidents(KEY)[-1]["level"] == "clear"


def test_choosing_google_by_hand_also_clears_it(monkeypatch, pl):
    """中控自己在设置里选了 Google：那是他知情的选择，不再挂着「已自动改用」。"""
    fall_back_to(monkeypatch, pl, "google")
    monkeypatch.setattr(pipeline_mod, "create_translator", lambda engine: Engine(engine))
    asyncio.run(pl.set_engine("google"))
    assert shown(pl) is None


def test_it_clears_once_the_translator_is_no_longer_google(monkeypatch, pl):
    """别的路径换掉了翻译器（比如后来装好了本地模型）：引擎一公布就撤。"""
    fall_back_to(monkeypatch, pl, "google")
    pl.translator = Engine("hymt2")
    asyncio.run(pl._publish_engine())
    assert shown(pl) is None


def test_it_stays_while_google_is_still_translating(monkeypatch, pl):
    """选了一个还没下载的本地模型：本场继续用 Google（见 _keep_engine_until_model），提示留着。"""
    fall_back_to(monkeypatch, pl, "google")
    monkeypatch.setattr(Pipeline, "_stream_active", lambda self: True)
    asyncio.run(pl._keep_engine_until_model("hymt2-7b", "hy-mt2-7b"))
    assert pl.translator.name == "google"
    assert shown(pl) is not None


def test_the_phone_gets_it_too(monkeypatch, pl):
    fall_back_to(monkeypatch, pl, "google")
    (msg,) = [m for m in pl.server.incidents(KEY) if m["level"] != "clear"]
    out = filter_payload(msg)
    assert out is not None and out["id"] == KEY and out["level"] == "warn"
    assert "Google" in out["text"]


def test_no_cause_labels_in_the_notice(monkeypatch, pl):
    """第八条：只写观察到的事和能做的事（中英两边）。"""
    fall_back_to(monkeypatch, pl, "google")
    text = shown(pl)["text"]
    for label in ("年龄", "限流", "封禁", "可能", "似乎"):
        assert label not in text
    english = i18n.render(text, i18n.EN)
    for label in ("might", "maybe", "probably", "rate limit", "banned"):
        assert label not in english.lower()
