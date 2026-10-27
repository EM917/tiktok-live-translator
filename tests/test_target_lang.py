"""顶栏「字幕译成」下拉只列四种（用户 2026-10-20），后端照旧什么目标语言都收。

界面这一侧（四个选项、补一项显示四种以外的当前目标）在 tests/target-lang.test.mjs。这里钉后端：
  * 没有白名单：--target ja、settings.json 里存着的法语、页面发来的 set_target 原样生效、原样记住、
    原样广播给页面——不会被收窄成四种之一，也不会被悄悄换成简体中文；
  * 西语直播译成西语（四种之一）时的现有行为：识别出的语言就是目标语言的那一段不送翻译，字幕显示原文；
    同一场里的英语段照常译成西语；西语弹幕照常送翻译引擎（弹幕只对「中文目标 + 含汉字」跳过）。
搭最小 Pipeline 的方式照 tests/test_pipeline_decoupling.py。
"""
from types import SimpleNamespace

from app import pipeline as pipeline_mod
from app import settings
from app.asr import ASRResult
from app.comments import needs_translation
from app.pipeline import Pipeline
from tests.helpers import run


class StubServer:
    def __init__(self):
        self.config = {}
        self.messages = []

    async def status(self, state, detail=""):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)

    def of_type(self, t):
        return [m for m in self.messages if m.get("type") == t]


class FastTranslator:
    def __init__(self):
        self.seen = []

    async def translate(self, text, target, source="auto", glossary=None):
        self.seen.append((text, target, source))
        return "[{}] {}".format(target, text)


def make_pipeline(monkeypatch, tmp_path, target, translator=None):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)
    args = SimpleNamespace(
        cookies=None, target=target, translator="none", source="es,en",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None,
    )
    server = StubServer()
    p = Pipeline(args, server)
    p.translator = translator
    return p, server


def test_a_target_outside_the_four_starts_as_is(monkeypatch, tmp_path):
    """--target ja（或 main.py 从 settings.json 读回的 ja）：页面拿到的就是 ja，下拉据此补一项。"""
    p, server = make_pipeline(monkeypatch, tmp_path, "ja")
    assert p.target == "ja"
    assert server.config["target_lang"] == "ja"


def test_set_target_accepts_any_language_and_remembers_it(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, "zh-CN")
    for value in ("fr", "es", "pt-BR", "zh-TW"):
        run(p.handle_control({"type": "set_target", "value": value}))
        assert p.target == value
        assert settings.load_settings()["target_lang"] == value
        assert server.of_type("config")[-1] == {"type": "config", "target_lang": value}


def test_phone_viewer_and_float_window_pass_any_target_through():
    """手机同看和浮窗都没有这个下拉，只是把字幕里的 target_lang 原样带过去：四种以外的也不改、不丢。"""
    from app import viewer
    from app.server import CaptionServer

    cap = {"type": "caption", "id": 1, "ts": 1.0, "original": "hola", "translated": None,
           "translate_state": "pending", "src_lang": "es", "target_lang": "ja"}
    assert viewer.filter_payload(cap)["target_lang"] == "ja"
    assert CaptionServer._float_config({"target_lang": "ja", "engine": {}}) == {"target_lang": "ja"}


def caption_of(server):
    return server.of_type("caption")[-1]


def test_spanish_target_shows_spanish_speech_as_is(monkeypatch, tmp_path):
    """西语直播、字幕译成西语：识别出 es 的段不送翻译，字幕就是原文（translate_state skipped，
    页面上没有「未翻译」一类角标——live-ui.js captionStateChip 只给 failed / dropped 画）。"""
    tr = FastTranslator()
    p, server = make_pipeline(monkeypatch, tmp_path, "es", translator=tr)

    async def scenario():
        return await p._emit_original(ASRResult(text="hola chicas", language="es", raw_text="hola chicas",
                                                rejected=[]), audio_end_ts=100.0, asr_ms=100)

    assert run(scenario()) is None                      # 不进翻译队列
    cap = caption_of(server)
    assert (cap["translate_state"], cap["src_lang"], cap["target_lang"]) == ("skipped", "es", "es")
    assert cap["original"] == "hola chicas" and cap["translated"] is None
    assert tr.seen == []


def test_spanish_target_still_translates_the_english_segments(monkeypatch, tmp_path):
    """同一场里主播说英语（主播语言默认 es,en）：照常译成西语。"""
    tr = FastTranslator()
    p, server = make_pipeline(monkeypatch, tmp_path, "es", translator=tr)

    async def scenario():
        job = await p._emit_original(ASRResult(text="thank you guys", language="en", raw_text="thank you guys",
                                               rejected=[]), audio_end_ts=100.0, asr_ms=100)
        await p._translate_and_update(job)

    run(scenario())
    assert caption_of(server)["translate_state"] == "pending"
    assert tr.seen and tr.seen[0][:2] == ("thank you guys", "es")


def test_spanish_comments_still_go_to_the_engine_when_the_target_is_spanish():
    """弹幕没有按语言跳过（识别结果里才有语言）：只有中文目标遇到含汉字的弹幕才算「已是目标语言」。
    这是现有行为，四种之一的西语目标不改它。"""
    assert needs_translation("hola, cuánto cuesta", "es") is True
    assert needs_translation("谢谢主播", "zh-CN") is False
    assert needs_translation("谢谢主播", "es") is True
