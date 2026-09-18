"""主播语言限定在几种（如 "es,en"）：只在这些语言里自动检测，不是强制单一语种。

起因：中控日常开「自动检测」是因为主播西语夹英语，但不限制的逐段自动检测会把
两成以上的段贴上韩语/阿拉伯语/俄语等无关标签——今天一天就出现了几十条这类
幻觉字幕。列表形式让中控事先框定「这场只会说这些语言」，解决办法不是新增一个
「强制单语种」开关（主播确实会在两种语言间切换），而是把 Whisper 的检测结果
限定在允许集合里：检测跑偏时用主语言强制重转一遍，而不是任由错误标签流入
展示/翻译/脚本离群判定。

覆盖：
  * app.asr._parse_language_spec：单一语种/auto 行为不变；列表校验（无效码丢弃、
    去重、最多 4 个、少于 2 个时退化）；
  * create_transcriber 到两个后端 __init__ 的接线（allowed_langs/primary_lang）；
  * MLXTranscriber/Transcriber.transcribe：检测语言不在允许列表时强制重转一遍，
    结果带 relabeled_from；允许时/未设允许列表时不重转；
  * app.audit.AuditLog.segment 只在真的重转过时才带 relabeled_from 这一列；
  * app.settings.resolve_source 的新默认 "es,en"（已在 test_settings.py 里覆盖，
    这里补列表值经过 create_transcriber 的完整链路）；
  * Pipeline.handle_control("start") 对 "es,en" 的保存/回显往返。

全程不 import mlx / faster_whisper 真身，不加载任何模型（假模块塞进 sys.modules，
做法与 tests/test_mlx_cache_limit.py 一致）；不 import main.py。
"""
import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app import asr
from app.audit import AuditLog


# ---- _parse_language_spec：纯逻辑，不碰任何后端 ----------------------------------

def test_single_code_unchanged():
    assert asr._parse_language_spec("es") == ("es", None, None)
    assert asr._parse_language_spec("ja") == ("ja", None, None)


def test_none_and_auto_unchanged():
    assert asr._parse_language_spec(None) == (None, None, None)
    assert asr._parse_language_spec("auto") == ("auto", None, None)
    assert asr._parse_language_spec("") == ("", None, None)


def test_list_form_splits_into_forced_none_plus_allowed_and_primary():
    forced, allowed, primary = asr._parse_language_spec("es,en")
    assert forced is None                  # 交给 Whisper 自己在允许集合里选
    assert allowed == ("es", "en")
    assert primary == "es"                 # 第一个是主语言


def test_whitespace_and_case_are_normalized():
    forced, allowed, primary = asr._parse_language_spec(" ES , En ")
    assert forced is None and allowed == ("es", "en") and primary == "es"


def test_duplicate_codes_are_deduped_keeping_first_order():
    _, allowed, primary = asr._parse_language_spec("es,en,es")
    assert allowed == ("es", "en") and primary == "es"


def test_invalid_codes_are_dropped_without_erroring():
    """表外的码（拼错、旧值）直接丢弃，不能让识别器因此起不来。"""
    forced, allowed, primary = asr._parse_language_spec("es,xx,en")
    assert allowed == ("es", "en") and primary == "es"


def test_all_invalid_codes_falls_back_to_unrestricted_auto():
    """整份列表都是无效码：退化成不限制的自动检测（None, None, None），
    比因为一个手改错的值让识别报错更安全。"""
    assert asr._parse_language_spec("xx,yy") == (None, None, None)


def test_one_valid_code_left_falls_back_to_single_forced_language():
    """校验完只剩一个有效码：当成单一语种（强制这个语言），沿用已有行为。"""
    assert asr._parse_language_spec("xx,es") == ("es", None, None)


def test_more_than_four_codes_are_trimmed_to_the_first_four():
    """决定：多出的从尾部截掉，不是整体拒绝——首位主语言不受影响。"""
    _, allowed, primary = asr._parse_language_spec("es,en,ja,ko,pt")
    assert allowed == ("es", "en", "ja", "ko")
    assert primary == "es"
    assert len(allowed) == asr.MAX_SOURCE_LANGS


def test_allowed_codes_match_the_ui_dropdown():
    """web/index.html #source-lang 提供的单语种码必须都在允许表里，
    否则用户在界面选的语言到了这里会被当成无效码丢弃。"""
    ui_codes = {"es", "en", "ja", "ko", "pt", "fr", "de", "ru", "ar", "th", "vi", "id", "zh"}
    assert asr.SOURCE_LANG_CODES == ui_codes


# ---- create_transcriber 接线：allowed_langs / primary_lang 到达两个后端 -----------

def test_ct2_transcriber_wires_allowed_langs_from_list_form(monkeypatch):
    fake = ModuleType("faster_whisper")
    fake.WhisperModel = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    t = asr.create_transcriber("ct2", "small", device="cpu", compute_type="int8",
                               language="es,en")
    assert isinstance(t, asr.Transcriber)
    assert t.language is None
    assert t.allowed_langs == ("es", "en")
    assert t.primary_lang == "es"


def test_ct2_transcriber_keeps_single_code_forced_behaviour(monkeypatch):
    fake = ModuleType("faster_whisper")
    fake.WhisperModel = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    t = asr.create_transcriber("ct2", "small", device="cpu", compute_type="int8",
                               language="ja")
    assert t.language == "ja" and t.allowed_langs is None and t.primary_lang is None


@pytest.fixture
def fake_mlx_module(monkeypatch):
    """假的 mlx_whisper：只记调用，不加载任何东西（做法同 test_mlx_cache_limit.py）。"""
    module = ModuleType("mlx_whisper")

    def transcribe(audio, **kw):
        return {"segments": [], "language": kw.get("language")}

    module.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)
    return module


def test_mlx_transcriber_wires_allowed_langs_from_list_form(fake_mlx_module):
    t = asr.MLXTranscriber("large-v3", language="es,en")
    assert t.language is None
    assert t.allowed_langs == ("es", "en")
    assert t.primary_lang == "es"


def test_mlx_transcriber_keeps_single_code_forced_behaviour(fake_mlx_module):
    t = asr.MLXTranscriber("large-v3", language="pt")
    assert t.language == "pt" and t.allowed_langs is None and t.primary_lang is None


def test_mlx_warmup_never_receives_the_raw_comma_list(monkeypatch):
    """预热调用必须传解析后的 self.language（列表形式是 None），不是原始
    "es,en"——mlx-whisper 的 language 参数不认逗号列表，传原值会在预热阶段就炸。"""
    seen = []

    def transcribe(audio, **kw):
        seen.append(kw.get("language"))
        return {"segments": [], "language": kw.get("language")}

    module = ModuleType("mlx_whisper")
    module.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)

    asr.MLXTranscriber("large-v3", language="es,en")
    assert seen == [None]          # 预热那一次：解析后是 None，不是 "es,en"


# ---- MLX transcribe：检测语言不在允许列表时重转一遍 -------------------------------

def _mlx_segment(text="hola"):
    return {"no_speech_prob": 0.1, "compression_ratio": 1.2,
            "avg_logprob": -0.3, "text": text}


class _SequencedMLX:
    """假的 mlx_whisper：预热那次不计入序列（没有 temperature kwarg）；
    之后按顺序吐出预先安排好的语言，每次都记下调用时的 language 参数。"""

    def __init__(self, languages):
        self.languages = list(languages)
        self.calls = []

    def transcribe(self, audio, **kw):
        if "temperature" not in kw:
            return {"segments": [], "language": None}   # 预热
        self.calls.append(kw.get("language"))
        lang = self.languages.pop(0) if self.languages else "es"
        return {"segments": [_mlx_segment()], "language": lang}


def _install_sequenced_mlx(monkeypatch, languages):
    fake = _SequencedMLX(languages)
    module = ModuleType("mlx_whisper")
    module.transcribe = fake.transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)
    return fake


def test_mlx_reruns_forced_to_primary_when_detected_language_is_not_allowed(monkeypatch):
    fake = _install_sequenced_mlx(monkeypatch, ["ko", "es"])
    t = asr.MLXTranscriber("large-v3", language="es,en")

    result = t.transcribe(b"\x00\x00" * 1600)

    assert fake.calls == [None, "es"]          # 先不限制，再强制主语言重转
    assert result.language == "es"
    assert result.relabeled_from == "ko"
    assert result.text == "hola"


def test_mlx_does_not_rerun_when_detected_language_is_allowed(monkeypatch):
    fake = _install_sequenced_mlx(monkeypatch, ["en"])
    t = asr.MLXTranscriber("large-v3", language="es,en")

    result = t.transcribe(b"\x00\x00" * 1600)

    assert fake.calls == [None]                # 只有一次真实调用
    assert result.language == "en"
    assert result.relabeled_from == ""


def test_mlx_does_not_rerun_when_allowed_langs_is_none(monkeypatch):
    """没设允许列表（不限制的自动检测，"auto" 在到达这里之前已被 resolve_source/
    handle_control 归一成 None）：不管检测到什么语言都不重转——这是改动前就有的
    行为，不能被这次改动波及。"""
    fake = _install_sequenced_mlx(monkeypatch, ["ko"])
    t = asr.MLXTranscriber("large-v3", language=None)

    result = t.transcribe(b"\x00\x00" * 1600)

    assert fake.calls == [None]
    assert result.language == "ko"
    assert result.relabeled_from == ""


# ---- CT2（faster-whisper）transcribe：同一套规则 ---------------------------------

class _SequencedCT2Model:
    """假的 WhisperModel：transcribe 返回 (segments, info)，segments 是普通对象
    （没有 faster_whisper 也能构造），info.language 按顺序吐出。"""

    def __init__(self, languages):
        self.languages = list(languages)
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append(kw.get("language"))
        lang = self.languages.pop(0) if self.languages else "es"
        segment = SimpleNamespace(no_speech_prob=0.1, compression_ratio=1.2,
                                  avg_logprob=-0.3, text="hola")
        info = SimpleNamespace(language=lang)
        return [segment], info


def _install_ct2_model(monkeypatch, languages):
    model = _SequencedCT2Model(languages)
    fake = ModuleType("faster_whisper")
    fake.WhisperModel = lambda *a, **k: model
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    return model


def test_ct2_reruns_forced_to_primary_when_detected_language_is_not_allowed(monkeypatch):
    model = _install_ct2_model(monkeypatch, ["ko", "es"])
    t = asr.Transcriber("large-v3", device="cpu", compute_type="int8", language="es,en")
    assert t.allowed_langs == ("es", "en") and t.primary_lang == "es" and t.language is None

    result = t.transcribe(b"\x00\x00" * 1600)

    assert model.calls == [None, "es"]         # 先不限制，检测跑偏后强制主语言重转
    assert result.language == "es"
    assert result.relabeled_from == "ko"
    assert result.text == "hola"


def test_ct2_does_not_rerun_when_detected_language_is_allowed(monkeypatch):
    model = _install_ct2_model(monkeypatch, ["en"])
    t = asr.Transcriber("large-v3", device="cpu", compute_type="int8", language="es,en")

    result = t.transcribe(b"\x00\x00" * 1600)

    assert model.calls == [None]
    assert result.language == "en"
    assert result.relabeled_from == ""


def test_ct2_does_not_rerun_when_allowed_langs_is_none(monkeypatch):
    model = _install_ct2_model(monkeypatch, ["ko"])
    t = asr.Transcriber("large-v3", device="cpu", compute_type="int8", language="es")

    result = t.transcribe(b"\x00\x00" * 1600)

    assert model.calls == ["es"]               # 单一语种：强制传给 Whisper，检测结果不影响是否重转
    assert result.language == "ko"
    assert result.relabeled_from == ""


# ---- AuditLog.segment：relabeled_from 只在真的发生过重转时才出现 ------------------

def _segment_rows(log):
    lines = log.path.read_text(encoding="utf-8").splitlines()
    return [r for r in (json.loads(line) for line in lines) if r["type"] == "segment"]


def test_audit_segment_omits_relabeled_from_when_not_set(tmp_path):
    log = AuditLog(room_url="https://www.tiktok.com/@bella/live", log_dir=tmp_path)
    result = asr.ASRResult(text="hola", language="es", raw_text="hola")
    log.segment(1, result, 1.0, 5.0, [])
    log.close()

    assert "relabeled_from" not in _segment_rows(log)[0]


def test_audit_segment_carries_relabeled_from_when_set(tmp_path):
    log = AuditLog(room_url="https://www.tiktok.com/@bella/live", log_dir=tmp_path)
    result = asr.ASRResult(text="hola", language="es", raw_text="hola",
                           relabeled_from="ko")
    log.segment(1, result, 1.0, 5.0, [])
    log.close()

    segment_row = _segment_rows(log)[0]
    assert segment_row["relabeled_from"] == "ko"
    assert segment_row["language"] == "es"


# ---- settings.resolve_source：新默认 "es,en"（详见 test_settings.py），这里补一条
#      端到端：默认值原样喂给 create_transcriber 也能正确拆解 -----------------------

def test_settings_default_flows_correctly_into_create_transcriber(monkeypatch):
    from app import settings

    default = settings.resolve_source(None, None)
    assert default == "es,en"

    fake = ModuleType("faster_whisper")
    fake.WhisperModel = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    t = asr.create_transcriber("ct2", "small", device="cpu", compute_type="int8",
                               language=default)
    assert t.allowed_langs == ("es", "en") and t.primary_lang == "es"


# ---- Pipeline.handle_control("start")：UI 传来的 "es,en" 原样保存/回显 ------------

def test_pipeline_start_round_trips_the_list_form_source(monkeypatch, tmp_path):
    from app import settings
    from app.pipeline import Pipeline

    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")

    started = []

    async def fake_start_with_ack(self, url, media=None):
        started.append((url, media))

    monkeypatch.setattr(Pipeline, "_start_with_ack", fake_start_with_ack)

    p = Pipeline.__new__(Pipeline)
    p.args = SimpleNamespace(source=None, source_requested=None)
    p.server = SimpleNamespace(config={})

    coro = p.handle_control({"type": "start",
                             "url": "https://www.tiktok.com/@bella/live",
                             "source": "es,en", "alerts": True})
    asyncio.run(coro)

    # 传给识别器的（args.source）、写进 settings.json 的、回显给界面的
    # （server.config），三处都要是完整的 "es,en"，不能被截断或拆散
    assert p.args.source == "es,en"
    assert p.args.source_requested == "es,en"
    assert p.server.config["source_lang"] == "es,en"
    assert settings.load_settings()["source_lang"] == "es,en"
    assert started == [("https://www.tiktok.com/@bella/live", None)]


def test_pipeline_start_source_auto_still_means_unrestricted_detection(monkeypatch, tmp_path):
    """"es,en" 是新默认，但界面上选「自动检测」时的老行为（args.source = None）
    不能被这次改动波及。"""
    from app import settings
    from app.pipeline import Pipeline

    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")

    async def fake_start_with_ack(self, url, media=None):
        return None

    monkeypatch.setattr(Pipeline, "_start_with_ack", fake_start_with_ack)

    p = Pipeline.__new__(Pipeline)
    p.args = SimpleNamespace(source="es,en", source_requested="es,en")
    p.server = SimpleNamespace(config={})

    coro = p.handle_control({"type": "start",
                             "url": "https://www.tiktok.com/@bella/live",
                             "source": "auto", "alerts": False})
    asyncio.run(coro)

    assert p.args.source is None
    assert settings.load_settings()["source_lang"] == "auto"
