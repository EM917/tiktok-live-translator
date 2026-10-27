"""asr._FilterMixin：质量过滤 + 幻觉丢弃 + 滚动上下文（纯逻辑，两后端共用）。

文件末尾是语言屏蔽（专家审查 M6，开关 TLT_ASR_MASK_LANGS 默认关）：主播语言设了允许列表时，先单独做
一次语言检测，在允许列表里挑概率最高的语言只解码一遍，不再「先自动检测、不在列表里整段重转」；被屏蔽
掉的语言照样记进 relabeled_from。用假的 mlx_whisper / mlx.core / faster_whisper（塞进 sys.modules），
不 import 真的 mlx、不加载任何模型（做法同 tests/test_mlx_cache_limit.py）。
"""
import json
import math
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from app import asr, asr_risk
from app.asr import _FilterMixin
from app.audit import AuditLog


class Filter(_FilterMixin):
    def __init__(self):
        self._context = ""


def fold(segs, lang="en", f=None):
    f = f or Filter()
    return f._fold(iter(segs), lang), f


def test_normal_speech_passes_and_feeds_context():
    (text, lang), f = fold([(0.1, 1.2, -0.3, " Hello world ")])
    assert text == "Hello world"
    assert lang == "en"
    assert "Hello world" in f._context        # 进入滚动上下文


def test_high_no_speech_prob_dropped():
    (text, _), _ = fold([(0.95, 1.2, -0.3, "ghost words")])
    assert text == ""


def test_repetition_dropped():
    (text, _), _ = fold([(0.1, 3.0, -0.3, "la la la la")])   # 复读机式压缩比
    assert text == ""


def test_low_confidence_dropped():
    (text, _), _ = fold([(0.1, 1.2, -2.0, "background music noise")])
    assert text == ""


def test_hallucination_with_low_confidence_dropped():
    # 静音段的经典幻觉：置信度低时整条丢弃
    (text, _), _ = fold([(0.1, 1.2, -0.9, "Thank you.")])
    assert text == ""


def test_hallucination_text_with_high_confidence_kept():
    # 主播真的在说 thank you（高置信）不能误杀
    (text, _), _ = fold([(0.1, 1.2, -0.1, "Thank you.")])
    assert text == "Thank you."


def test_punctuation_only_dropped():
    """音乐/噪声段 Whisper 常吐一个裸 "!"——不是话，不该占字幕更不该送翻译
    （实测一场里 13 段 "!" 被原样翻译上屏）。"""
    result, f = fold([(0.1, 1.2, -0.3, "!")])
    assert result.text == ""
    assert result.rejected[0]["reason"] == "punctuation"
    assert result.raw_text == "!"             # 违禁词检测的输入不受影响
    assert f._context == ""                   # 垃圾不进滚动上下文


def test_digits_survive_punctuation_filter():
    """价格和数量是合规要看的内容，纯数字段不能当垃圾丢。"""
    result, _ = fold([(0.1, 1.2, -0.3, "30?")])
    assert result.text == "30?"


def test_context_rolls_at_400_chars():
    f = Filter()
    f._context = "x" * 400
    (text, _), _ = fold([(0.1, 1.2, -0.2, "new words")], f=f)
    assert text == "new words"
    assert len(f._context) <= 400
    assert f._context.endswith("new words")


# ---- 幻觉过滤：Whisper 会在无人声段吐训练数据里的片尾套话 ----

def test_spanish_youtube_outro_dropped():
    """实测一场西语带货直播里 14% 的字幕是这种幻觉——而当时词表里
    只有英文和中文条目，西语的全部漏了过去。"""
    for text in ("¡Gracias por ver el video!", "¡SUSCRIBETE!",
                 "Gracias por ver este video", "Suscríbete al canal"):
        (out, _), _ = fold([(0.1, 1.2, -0.9, text)])
        assert out == "", text


def test_multiple_outros_in_one_segment_dropped():
    """Whisper 常把几句片尾语粘成一段，只比对整段就会漏掉。"""
    (out, _), _ = fold([(0.1, 1.2, -0.9,
                         "¡Gracias por ver el video! ¡Suscríbete al canal!")])
    assert out == ""


def test_real_speech_containing_gracias_survives():
    """主播真的在句子里说 gracias 不能被误杀——只有整段全是套话才丢。"""
    (out, _), _ = fold([(0.1, 1.2, -0.9, "Gracias por la rosa, se los agradezco")])
    assert "Gracias" in out
    (out2, _), _ = fold([(0.1, 1.2, -0.9, "Gracias por ver. Ahora vamos con la moringa.")])
    assert "moringa" in out2       # 一半是真话，整段不能丢


# ---- 字幕组署名：表里带 "." 的条目曾经永远比不中 ----

def test_amara_credit_dropped_in_every_language_even_with_high_confidence():
    """「Subtitles by the Amara.org community」2026-09-17 线上原样上屏：
    归一化保留句号做拆句，amara.org 里的点把一句拆成两半，表里三条 amara
    条目从未命中过。署名不是任何主播会说的话，置信度再高也丢。"""
    for text in ("Subtitles by the Amara.org community",
                 "Subtítulos realizados por la comunidad de Amara.org",
                 "字幕由Amara.org社区提供",
                 "Legendas pela comunidade Amara.org"):
        (out, _), _ = fold([(0.1, 1.2, -0.1, text)])
        assert out == "", text


def test_table_entries_with_punctuation_are_live():
    """表里写着 "thanks for watching!"（带叹号）这类条目也要能比中。"""
    (out, _), _ = fold([(0.1, 1.2, -0.9, "Thanks for watching!")])
    assert out == ""


def test_decimal_numbers_are_not_split_into_hallucination_parts():
    """摘掉词内的点不能误伤：价格是合规要看的内容，整句必须保留。"""
    (out, _), _ = fold([(0.1, 1.2, -0.9, "Cuesta 3.5 dólares, chicas.")])
    assert "3.5" in out


# ---- 网址署名 / 文字系统离群（2026-09-17 一天里上屏的 45 条幻觉）----

def test_url_credit_line_dropped_even_with_high_confidence():
    """「Más información www.cdc.gov.ar」一天里 5 条上屏：带网址的短句是训练集署名。"""
    for text in ("Más información www.cdc.gov.ar",
                 "Más información www.albertosanagustin.com", "www.example.com"):
        (out, _), _ = fold([(0.1, 1.2, -0.1, text)])
        assert out == "", text


def test_long_sentence_with_a_url_is_kept():
    """长句里夹一个网址可能是主播真在念，不动。"""
    (out, _), _ = fold([(0.1, 1.2, -0.3,
                         "Chicas entren a la página oficial www.tienda.com para ver todos los precios de hoy")])
    assert "www.tienda.com" in out


def test_foreign_script_outlier_dropped_after_latin_history():
    """西语直播里突然一段韩文/阿拉伯文：最近接受的字幕里没这种文字，扣下。"""
    f = Filter()
    for i in range(6):
        fold([(0.1, 1.2, -0.3, "Hola chicas, bienvenidas número {}".format(i))], f=f)
    for text in ("안녕하세요 여러분.", "المترجمات لكثير من الاشتراك في القناة",
                 "И вот она, новая маска."):
        res = f._fold(iter([(0.1, 1.2, -0.1, text)]), "es")
        assert res.text == "", text
        assert text in res.raw_text        # 违禁词检测的输入一个字都不少
        assert res.rejected[-1]["reason"] == "script_outlier"


def test_a_real_language_switch_costs_only_the_first_segment():
    """同一种文字连着来两段就当真换了语言：第二段起放行，之后进入历史。"""
    f = Filter()
    for i in range(6):
        fold([(0.1, 1.2, -0.3, "Hola chicas {}".format(i))], f=f)
    (a, _), _ = fold([(0.1, 1.2, -0.2, "안녕하세요 여러분")], f=f)
    (b, _), _ = fold([(0.1, 1.2, -0.2, "오늘은 립스틱을 소개합니다")], f=f)
    (c, _), _ = fold([(0.1, 1.2, -0.2, "이 제품은 정말 좋아요")], f=f)
    assert a == "" and b != "" and c != ""


def test_no_script_rule_without_history():
    """开播头几段没有参照，不按文字系统判。"""
    (out, _), _ = fold([(0.1, 1.2, -0.2, "안녕하세요 여러분")])
    assert out != ""


def test_mixed_spanish_english_stream_is_never_an_outlier():
    """西语和英语同是拉丁字母：主播中英西夹杂不受影响。"""
    f = Filter()
    for i in range(8):
        fold([(0.1, 1.2, -0.3, "Hola chicas {}".format(i))], f=f)
    (out, _), _ = fold([(0.1, 1.2, -0.2, "Oh my god it's so good, twenty dollars")], f=f)
    assert "twenty" in out


# ---- YouTube 片尾：不看置信度（2026-10-14：114 份日志里按旧表有 276 段整段套话上了屏）----

def test_youtube_outro_dropped_even_with_high_confidence():
    """「¡Gracias por ver el video!」的置信度常贴着 -0.6，拦没拦住近乎抛硬币：10-14 直播录音
    离线重跑，第 549 段就是 -0.38 上的屏。字幕组署名、提到「视频」的片尾语、「¡Suscríbete al
    canal!」整段只有这些就是训练集套话，置信度再高也丢；raw_text 照旧留给违禁词检测。"""
    for text in ("¡Gracias por ver el video!", "¡Gracias por ver este video!",
                 "¡Suscríbete al canal!", "Gracias por ver el video.",
                 "¡Gracias por ver el video! ¡Suscríbete al canal!",
                 "¡Gracias por ver este video! ¡Suscríbete al canal para no perderte los próximos videos!",
                 "Si te gustó el video, dale like, suscríbete y comparte."):
        res, f = fold([(0.1, 1.2, -0.38, text)])
        assert res.text == "", text
        assert res.raw_text == text                       # 违禁词检测的输入不受影响
        assert res.rejected[-1]["reason"] == "hallucination"
        assert f._context == ""                           # 套话不进滚动上下文


def test_every_youtube_outro_entry_is_live():
    """表里每一条都要真能比中：条目中间带了句末标点（拆成两句），这一条就永远比不中。"""
    from app.asr import _HALLUCINATIONS, _YOUTUBE_OUTROS
    assert _YOUTUBE_OUTROS <= _HALLUCINATIONS
    for entry in _YOUTUBE_OUTROS:
        (out, _), _ = fold([(0.1, 1.2, -0.1, entry)])
        assert out == "", entry


def test_ambiguous_phrase_mixed_with_an_outro_dropped():
    """整段都是套话、其中有一句只可能是片尾，那句 thank you 也不是主播说的。"""
    (out, _), _ = fold([(0.1, 1.2, -0.2, "Thank you. ¡Suscríbete al canal!")])
    assert out == ""


# 主播真会说的话：TikTok 有订阅和点赞，有主播把自己的页面叫「mi canal」，双语主播会用英文
# 道别。这些放进「不看置信度」那一层就会误丢真话，钉住它们只在低置信时才丢。
STREAMER_SAYS = ("¡Suscríbete!", "¡Nos vemos!", "Música", "¡Hasta la próxima!", "Gracias por ver.",
                 "Dale like y suscríbete.", "¡Suscríbete a mi canal!", "¡Suscríbete a nuestro canal!",
                 "No olvides suscribirte.", "Thank you for watching!", "Please subscribe.",
                 "Thank you.", "Bye!", "Gracias por su atención.")


def test_phrases_a_streamer_really_says_still_need_low_confidence():
    for text in STREAMER_SAYS:
        (out, _), _ = fold([(0.1, 1.2, -0.2, text)])
        assert out == text, text
        (low, _), _ = fold([(0.1, 1.2, -0.9, text)])
        assert low == "", text                            # 低置信时照旧当套话丢


def test_every_conditional_entry_needs_low_confidence():
    """要看置信度那一层逐条钉住：哪一条被挪进 _YOUTUBE_OUTROS，这里就红。"""
    from app.asr import _HALLUCINATIONS, _YOUTUBE_OUTROS
    for entry in _HALLUCINATIONS - _YOUTUBE_OUTROS:
        (out, _), _ = fold([(0.1, 1.2, -0.2, entry)])
        assert out == entry, entry
        (low, _), _ = fold([(0.1, 1.2, -0.9, entry)])
        assert low == "", entry


def test_split_keeps_every_old_entry():
    """分层前的表一条都不能丢（2026-10-14 分层时的全集）。"""
    from app.asr import _HALLUCINATIONS
    old = {
        "thank you", "thanks for watching", "thank you for watching", "you",
        "please subscribe", "subscribe", "bye", "so", "thanks for watching!", "see you next time",
        "subtitles by the amara.org community", "subtitles by amara.org",
        "subtitled by the amara.org community", "legendas pela comunidade amara.org",
        "sous-titres realises par la communaute d'amara.org",
        "gracias por ver", "gracias por ver el video", "gracias por ver este video", "gracias por vernos",
        "suscribete", "suscribete al canal", "suscribanse", "no olvides suscribirte",
        "no olviden suscribirse", "hasta la proxima", "nos vemos", "gracias por su atencion",
        "subtitulos realizados por la comunidad de amara.org", "mas videos", "dale like y suscribete",
        "aplausos", "musica", "risas", "mas informacion", "obrigado", "obrigada",
        "字幕由amara.org社区提供", "请不吝点赞订阅转发打赏支持明镜与点点栏目", "谢谢观看", "请订阅",
        "감사합니다", "구독과 좋아요 부탁드립니다", "다음 영상에서 만나요",
        "المترجمات لكثير من الاشتراك في القناة", "اشتركوا في القناة",
        "субтитры сделал dimatorzok", "субтитры создавал dimatorzok", "продолжение следует",
        "ご視聴ありがとうございました", "チャンネル登録お願いします", "תודה רבה",
    }
    assert old <= _HALLUCINATIONS


def test_outro_words_inside_real_speech_survive():
    """日志里有一段识别成「Suscríbete al canal y usa los cupones de TikTok」：整段不全是套话就不动。"""
    for text in ("Suscríbete al canal y usa los cupones de TikTok.",
                 "Gracias por ver el video. Ahora vamos con la moringa."):
        (out, _), _ = fold([(0.1, 1.2, -0.2, text)])
        assert out == text, text


def test_empty_and_punctuation_only_input_is_not_a_hallucination():
    from app.asr import _has_youtube_outro, _is_all_hallucination
    for text in ("", None, ".", "¡¿?!", "..."):
        assert not _is_all_hallucination(text), text
        assert not _has_youtube_outro(text), text


# ---- 语言屏蔽（M6，开关默认关）-----------------------------------------------------------------

PCM = b"\x00\x00" * 1600
KO_FIRST = {"ko": 0.6, "en": 0.3, "es": 0.1}          # 检测跑偏成韩语，允许列表里英语比西语更像


class FakeMLX:
    """假的 mlx-whisper 全家：mlx_whisper（transcribe）、mlx_whisper.audio、mlx_whisper.transcribe
    （ModelHolder）、mlx.core（只有 float16）。steps 记下语言检测那一步依次调了什么；calls 记每次真识别
    的 language 参数（预热那次不带 temperature，不算）。language=None 时，假 transcribe 像真的一样
    自己检测：取概率最大的。multilingual=False 是纯英文模型；detect_error 是检测那一步要抛的错；
    holder=False 是 mlx_whisper.transcribe 里没有 ModelHolder（升级后内部实现变了的样子）。"""

    N_SAMPLES, N_FRAMES, N_MELS = 480000, 3000, 128

    def __init__(self, probs, multilingual=True, detect_error=None, holder=True):
        self.probs = dict(probs)
        self.multilingual = multilingual
        self.detect_error = detect_error
        self.holder = holder
        self.steps = []
        self.calls = []

    def install(self, monkeypatch):
        fake = self
        core = ModuleType("mlx.core")
        core.float16 = "float16"
        mlx_pkg = ModuleType("mlx")
        mlx_pkg.core = core

        class Segment:
            def astype(self, dtype):
                fake.steps.append(("astype", dtype))
                return "mel_segment"

        def log_mel_spectrogram(audio, n_mels, padding):
            fake.steps.append(("log_mel", type(audio).__name__, n_mels, padding))
            return "mel"

        def pad_or_trim(array, length, axis):
            fake.steps.append(("pad_or_trim", array, length, axis))
            return Segment()

        audio_mod = ModuleType("mlx_whisper.audio")
        audio_mod.N_SAMPLES, audio_mod.N_FRAMES = self.N_SAMPLES, self.N_FRAMES
        audio_mod.log_mel_spectrogram, audio_mod.pad_or_trim = log_mel_spectrogram, pad_or_trim

        class Model:
            is_multilingual = self.multilingual
            dims = SimpleNamespace(n_mels=self.N_MELS)

            def detect_language(self, mel):
                fake.steps.append(("detect_language", mel))
                if fake.detect_error is not None:
                    raise fake.detect_error
                return None, dict(fake.probs)

        class ModelHolder:
            @classmethod
            def get_model(cls, model_path, dtype):
                fake.steps.append(("get_model", model_path, dtype))
                return Model()

        transcribe_mod = ModuleType("mlx_whisper.transcribe")
        if self.holder:
            transcribe_mod.ModelHolder = ModelHolder

        def transcribe(audio, **kw):
            if "temperature" not in kw:
                return {"segments": [], "language": None}            # 预热
            fake.calls.append(kw.get("language"))
            lang = kw.get("language") or max(fake.probs, key=fake.probs.get)
            return {"segments": [{"no_speech_prob": 0.1, "compression_ratio": 1.2,
                                  "avg_logprob": -0.3, "text": "hola", "tokens": [1, 2]}],
                    "text": "hola", "language": lang}

        pkg = ModuleType("mlx_whisper")
        pkg.transcribe = transcribe
        for name, module in (("mlx", mlx_pkg), ("mlx.core", core), ("mlx_whisper", pkg),
                             ("mlx_whisper.audio", audio_mod),
                             ("mlx_whisper.transcribe", transcribe_mod)):
            monkeypatch.setitem(sys.modules, name, module)
        return self


class FakeCT2Model:
    """假的 faster_whisper.WhisperModel。detect_language / transcribe 的签名照 faster-whisper 1.2.1 写成
    具名参数、没有 **kwargs（只列用得到的）：参数名写错就和真的一样 TypeError。"""

    def __init__(self, probs):
        self.probs = dict(probs)
        self.calls = []
        self.detects = []

    def detect_language(self, audio=None, features=None, vad_filter=False, vad_parameters=None,
                        language_detection_segments=1, language_detection_threshold=0.5):
        self.detects.append({"audio": audio, "features": features, "vad_filter": vad_filter})
        ranked = sorted(self.probs.items(), key=lambda kv: -kv[1])
        return ranked[0][0], ranked[0][1], ranked

    def transcribe(self, audio, language=None, vad_filter=False, beam_size=5, temperature=0.0,
                   condition_on_previous_text=True, initial_prompt=None, max_new_tokens=None):
        self.calls.append(language)
        lang = language or max(self.probs, key=self.probs.get)
        segment = SimpleNamespace(no_speech_prob=0.1, compression_ratio=1.2, avg_logprob=-0.3,
                                  text="hola", tokens=[1, 2], seek=0, start=0.0, end=1.0,
                                  temperature=0.0)
        return [segment], SimpleNamespace(language=lang)


class FakeCT2ModelBefore11(FakeCT2Model):
    """faster-whisper 1.1 之前（requirements 写的是 >=1.0）：WhisperModel 还没有公开的 detect_language。"""

    def __getattribute__(self, name):
        if name == "detect_language":
            raise AttributeError(name)
        return super().__getattribute__(name)


@pytest.fixture
def isolated(monkeypatch):
    """真的 mlx 先藏起来、开关的环境变量清掉、「只说一次」的记录清空；用例结束断言没 import 过真的 mlx。"""
    for name in [n for n in sys.modules if n == "mlx" or n.startswith(("mlx.", "mlx_whisper"))]:
        monkeypatch.delitem(sys.modules, name)
    for name in (asr.TOKEN_CAP_ENV, asr.MASK_LANGS_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(asr, "_mlx_said", set())
    yield monkeypatch
    real = getattr(sys.modules.get("mlx.core"), "__file__", None)
    assert real is None, "这些用例不许 import 真的 mlx.core"


def install_ct2(monkeypatch, model):
    module = ModuleType("faster_whisper")
    module.WhisperModel = lambda *a, **k: model
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return model


def ct2(language="es,en", **kw):
    return asr.Transcriber("small", device="cpu", compute_type="int8", language=language, **kw)


def test_mask_is_off_by_default_and_the_rerun_path_is_unchanged(isolated):
    """开关默认关：两个后端都照旧「先自动检测、不在列表里用主语言整段重转」，不做单独的检测。"""
    fake = FakeMLX(KO_FIRST).install(isolated)
    m = asr.MLXTranscriber("large-v3", language="es,en")
    model = install_ct2(isolated, FakeCT2Model(KO_FIRST))
    c = ct2()
    assert m.mask_langs is False and c.mask_langs is False
    rm, rc = m.transcribe(PCM), c.transcribe(PCM)
    assert fake.calls == [None, "es"] and model.calls == [None, "es"]
    assert fake.steps == [] and model.detects == []
    for r in (rm, rc):
        assert r.language == "es" and r.relabeled_from == "ko"
        assert r.signals["calls"] == 2
        assert r.windows[0]["first_call"]["language"] == "ko"


def test_mlx_mask_decodes_once_in_the_most_likely_allowed_language(isolated):
    """打开后：检测成韩语的段直接用允许列表里概率最高的语言（这里是英语，不一定是主语言）解码一遍；
    被屏蔽的韩语照样记进 relabeled_from，calls 是 1（区别于重转的 2），没有「第一遍」可记。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    fake = FakeMLX(KO_FIRST).install(isolated)
    t = asr.MLXTranscriber("large-v3", language="es,en")
    assert t.mask_langs is True
    result = t.transcribe(PCM)
    assert fake.calls == ["en"]
    assert result.language == "en"
    assert result.relabeled_from == "ko"
    assert result.text == "hola"
    assert result.signals["calls"] == 1
    assert "first_call" not in result.windows[0]


def test_mlx_mask_keeps_an_allowed_detection_and_leaves_no_relabel(isolated):
    """检测结果本来就在列表里：用它解码（和让 mlx 自己检测是同一个语言），不记 relabeled_from。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "1")
    fake = FakeMLX({"es": 0.8, "ko": 0.15, "en": 0.05}).install(isolated)
    result = asr.MLXTranscriber("large-v3", language="es,en").transcribe(PCM)
    assert fake.calls == ["es"]
    assert result.language == "es" and result.relabeled_from == ""
    assert result.signals["calls"] == 1


def test_mlx_detection_repeats_mlx_whisper_own_steps(isolated):
    """单独做的检测要和 mlx_whisper.transcribe 在 language=None 时自己那一步一样（0.4.3 源码）：同一个
    缓存模型（fp16）、补 30 秒静音的梅尔谱、截前 30 秒（axis=-2）、转 fp16、detect_language。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    fake = FakeMLX(KO_FIRST).install(isolated)
    t = asr.MLXTranscriber("large-v3", language="es,en")
    t.transcribe(PCM)
    assert fake.steps == [
        ("get_model", "mlx-community/whisper-large-v3-mlx", "float16"),
        ("log_mel", "ndarray", FakeMLX.N_MELS, FakeMLX.N_SAMPLES),
        ("pad_or_trim", "mel", FakeMLX.N_FRAMES, -2),
        ("astype", "float16"),
        ("detect_language", "mel_segment"),
    ]


def test_ct2_mask_uses_detect_language_with_the_same_vad_and_decodes_once(isolated):
    isolated.setenv(asr.MASK_LANGS_ENV, "true")
    model = install_ct2(isolated, FakeCT2Model(KO_FIRST))
    result = ct2().transcribe(PCM)
    assert len(model.detects) == 1
    detect = model.detects[0]
    assert detect["vad_filter"] is True and detect["features"] is None   # 和 transcribe 一样先过 VAD
    assert isinstance(detect["audio"], np.ndarray) and detect["audio"].dtype == np.float32
    assert model.calls == ["en"]
    assert result.language == "en" and result.relabeled_from == "ko"
    assert result.signals["calls"] == 1


def test_mask_falls_back_to_the_rerun_when_detection_is_unavailable(isolated, capsys):
    """faster-whisper 1.1 之前没有公开的 detect_language：提示一次，每段照旧走自动检测 + 重转，
    识别不停、结果和开关关着时一样。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    model = install_ct2(isolated, FakeCT2ModelBefore11(KO_FIRST))
    t = ct2()
    first, second = t.transcribe(PCM), t.transcribe(PCM)
    assert model.calls == [None, "es", None, "es"]
    for r in (first, second):
        assert r.language == "es" and r.relabeled_from == "ko" and r.signals["calls"] == 2
    out = capsys.readouterr().out
    assert out.count("[警告] 识别前单独做的语言检测出错（AttributeError") == 1


def test_mask_does_nothing_without_an_allowed_list(isolated):
    """单一语种（强制）和不限制的自动检测：没有列表可框，开关开着也不做单独的检测。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    fake = FakeMLX(KO_FIRST).install(isolated)
    asr.MLXTranscriber("large-v3", language="es").transcribe(PCM)
    auto = asr.MLXTranscriber("large-v3", language=None).transcribe(PCM)
    assert fake.steps == []
    assert fake.calls == ["es", None]
    assert auto.language == "ko" and auto.relabeled_from == ""


def test_mask_switch_reaches_both_backends_through_create_transcriber(isolated):
    FakeMLX(KO_FIRST).install(isolated)
    install_ct2(isolated, FakeCT2Model(KO_FIRST))
    assert asr.create_transcriber("mlx", "large-v3", language="es,en").mask_langs is False
    assert asr.create_transcriber("mlx", "large-v3", language="es,en", mask_langs=True).mask_langs
    assert asr.create_transcriber("ct2", "small", language="es,en", mask_langs=True).mask_langs
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    assert asr.create_transcriber("ct2", "small", language="es,en").mask_langs is True
    assert asr.create_transcriber("ct2", "small", language="es,en", mask_langs=False).mask_langs is False


def test_mask_choice_ties_and_missing_probabilities_go_to_the_primary_language():
    allowed = ("es", "en", "pt")
    assert asr._mask_to_allowed("ko", {"ko": 0.9}, allowed) == ("es", "ko")
    assert asr._mask_to_allowed("ko", {"ko": 0.6, "en": 0.2, "es": 0.2}, allowed) == ("es", "ko")
    assert asr._mask_to_allowed("ko", {"ko": 0.6, "pt": 0.3, "es": 0.1}, allowed) == ("pt", "ko")
    assert asr._mask_to_allowed("ko", {"ko": 0.6, "en": math.nan, "es": 0.01}, allowed) == ("es", "ko")
    assert asr._mask_to_allowed("en", {"en": 0.4, "ko": 0.5}, allowed) == ("en", "")
    with pytest.raises(ValueError):
        asr._mask_to_allowed("", {"es": 1.0}, allowed)


def test_masked_segment_keeps_the_relabel_signals_downstream(isolated, tmp_path):
    """屏蔽时 relabeled_from 照旧有值：asr_risk 照样标 relabel（只记审计），审计 segment 照样带
    relabeled_from，asr 子对象里 calls 是 1——不新增字段，下游三处含义不变。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    FakeMLX(KO_FIRST).install(isolated)
    result = asr.MLXTranscriber("large-v3", language="es,en").transcribe(PCM)
    risk = asr_risk.assess(result, backend="mlx", model="large-v3")
    assert "relabel" in risk.flags
    log = AuditLog(room_url="https://www.tiktok.com/@demo_host_m6/live", log_dir=tmp_path)
    log.segment(1, result, 1.0, 5.0, [], asr=risk.audit)
    log.close()
    rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    seg = [r for r in rows if r["type"] == "segment"][0]
    assert seg["relabeled_from"] == "ko" and seg["language"] == "en"
    assert seg["asr"]["calls"] == 1


# ---- 语言屏蔽：终端提示、纯英文模型、检测出错（M6 第二轮审查）----------------------------------------

MASK_ACTIVE = "[信息] 识别语言屏蔽已打开：主播语言列表外的语言在检测阶段就屏蔽，不再整段重转"
MASK_INACTIVE = "[信息] 识别语言屏蔽开关开着，但这次识别没有主播语言列表（比如 es,en），不生效"
MASK_ERROR = "[警告] 识别前单独做的语言检测出错（"
MASK_ERROR_MORE = "[警告] 识别前单独做的语言检测已经出过 5 种错"


def test_mask_notice_says_active_only_when_there_is_an_allowed_list(isolated, capsys):
    """有允许列表：说「已打开」，只说一次（两个后端各建一个也是一次）。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    FakeMLX(KO_FIRST).install(isolated)
    install_ct2(isolated, FakeCT2Model(KO_FIRST))
    asr.MLXTranscriber("large-v3", language="es,en")
    ct2(language="es,pt")
    out = capsys.readouterr().out
    assert out.count(MASK_ACTIVE) == 1
    assert MASK_INACTIVE not in out


@pytest.mark.parametrize("language", ["es", None, "auto", "es,xx"])
def test_mask_notice_says_inactive_without_an_allowed_list(isolated, capsys, language):
    """单一语种、自动检测、列表校验完只剩一个码（"es,xx"）：开关开着也不屏蔽，终端照实说不生效，
    不说「已打开」。开关的状态照旧是开（mask_langs 为真），_masked_language 那里不做检测。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    fake = FakeMLX(KO_FIRST).install(isolated)
    install_ct2(isolated, FakeCT2Model(KO_FIRST))
    m = asr.MLXTranscriber("large-v3", language=language)
    c = ct2(language=language)
    assert m.mask_langs is True and c.mask_langs is True
    assert m.allowed_langs is None and c.allowed_langs is None
    m.transcribe(PCM)
    assert fake.steps == []
    out = capsys.readouterr().out
    assert out.count(MASK_INACTIVE) == 1
    assert MASK_ACTIVE not in out


def test_mask_notice_follows_the_explicit_argument_too(isolated, capsys):
    """评测脚本直接传 mask_langs=True（不看环境变量）时同样按有没有列表说哪一句。"""
    FakeMLX(KO_FIRST).install(isolated)
    asr.create_transcriber("mlx", "large-v3", language="es", mask_langs=True)
    assert capsys.readouterr().out.count(MASK_INACTIVE) == 1
    asr.create_transcriber("mlx", "large-v3", language="es,en", mask_langs=True)
    assert capsys.readouterr().out.count(MASK_ACTIVE) == 1


def test_mask_off_says_nothing_with_or_without_a_list(isolated, capsys):
    FakeMLX(KO_FIRST).install(isolated)
    asr.MLXTranscriber("large-v3", language="es,en")
    asr.MLXTranscriber("large-v3", language="es")
    out = capsys.readouterr().out
    assert MASK_ACTIVE not in out and MASK_INACTIVE not in out


@pytest.mark.parametrize("value", ["112", "enable", "开", "onn"])
def test_an_unknown_mask_switch_value_is_off_with_one_warning(isolated, capsys, value):
    """写错的值（TLT_MLX_CACHE_MB 的做法）：按关处理，终端说一次，免得评测时以为开关开了。"""
    isolated.setenv(asr.MASK_LANGS_ENV, value)
    fake = FakeMLX(KO_FIRST).install(isolated)
    first = asr.MLXTranscriber("large-v3", language="es,en")
    second = asr.MLXTranscriber("large-v3", language="es,en")
    assert first.mask_langs is False and second.mask_langs is False
    first.transcribe(PCM)
    assert fake.steps == [] and fake.calls == [None, "es"]       # 照旧走重转，没做单独的检测
    out = capsys.readouterr().out
    warning = "[警告] 环境变量 TLT_ASR_MASK_LANGS 的值 {!r} 不是 on/1/true/yes，也不是 off/0/false/no，按关处理"
    assert out.count(warning.format(value)) == 1
    assert MASK_ACTIVE not in out and MASK_INACTIVE not in out


@pytest.mark.parametrize("value", ["", "off", "OFF", " 0 ", "false", "no"])
def test_a_known_off_mask_switch_value_is_silent(isolated, capsys, value):
    """（用 ct2：mlx 的识别器建好时还会说 MLX 缓冲缓存上限那一行，和开关无关。）"""
    isolated.setenv(asr.MASK_LANGS_ENV, value)
    install_ct2(isolated, FakeCT2Model(KO_FIRST))
    assert ct2(language="es,en").mask_langs is False
    assert capsys.readouterr().out == ""


def test_ct2_english_only_model_is_english_without_detecting(isolated):
    """纯英文模型（ctranslate2 的 is_multilingual 为 False）：和 faster-whisper 的 transcribe 一样直接当
    英语，不调 detect_language。英语在列表里就用英语、不记屏蔽；不在列表里就框进列表、记下 en。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    model = FakeCT2Model(KO_FIRST)                 # 真去检测的话会是韩语
    model.model = SimpleNamespace(is_multilingual=False)
    install_ct2(isolated, model)
    kept = ct2(language="es,en")
    assert kept._language_probs(np.zeros(1600, dtype=np.float32)) == ("en", {"en": 1.0})
    result = kept.transcribe(PCM)
    assert model.detects == []
    assert model.calls == ["en"]
    assert result.language == "en" and not result.relabeled_from
    assert result.signals["calls"] == 1
    masked = ct2(language="es,pt").transcribe(PCM)
    assert model.detects == []
    assert model.calls[-1] == "es"
    assert masked.language == "es" and masked.relabeled_from == "en"
    assert masked.signals["calls"] == 1


def test_mlx_english_only_model_is_english_without_detecting(isolated):
    """mlx 的纯英文模型：取到缓存的模型就返回英语，不算梅尔谱、不调 detect_language（和 mlx-whisper
    0.4.3 的 transcribe 一样）。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    fake = FakeMLX(KO_FIRST, multilingual=False).install(isolated)
    t = asr.MLXTranscriber("large-v3", language="es,en")
    assert t._language_probs(np.zeros(1600, dtype=np.float32)) == ("en", {"en": 1.0})
    fake.steps.clear()
    result = t.transcribe(PCM)
    assert fake.steps == [("get_model", "mlx-community/whisper-large-v3-mlx", "float16")]
    assert fake.calls == ["en"]
    assert result.language == "en" and not result.relabeled_from
    assert result.signals["calls"] == 1
    masked = asr.MLXTranscriber("large-v3", language="es,pt").transcribe(PCM)
    assert fake.calls[-1] == "es"
    assert masked.relabeled_from == "en" and masked.signals["calls"] == 1


@pytest.mark.parametrize("fault", ["detect_raises", "no_model_holder"])
def test_mlx_detection_failure_falls_back_to_the_rerun(isolated, capsys, fault):
    """屏蔽开着、mlx 那一步检测出错（检测本身抛错；或者升级以后 mlx_whisper.transcribe 里没了 ModelHolder）：
    这一段照旧「先自动检测、不在列表里用主语言整段重转」，结果和开关关着时一样；终端提示一次。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    if fault == "detect_raises":
        fake = FakeMLX(KO_FIRST, detect_error=RuntimeError("[metal] command buffer failed")).install(isolated)
        kind = "RuntimeError: [metal] command buffer failed"
    else:
        fake = FakeMLX(KO_FIRST, holder=False).install(isolated)
        kind = "AttributeError: "
    t = asr.MLXTranscriber("large-v3", language="es,en")
    first, second = t.transcribe(PCM), t.transcribe(PCM)
    assert fake.calls == [None, "es", None, "es"]
    for r in (first, second):
        assert r.language == "es" and r.relabeled_from == "ko"
        assert r.text == "hola"
        assert r.signals["calls"] == 2
        assert r.windows[0]["first_call"]["language"] == "ko"
    out = capsys.readouterr().out
    assert out.count(MASK_ERROR) == 1
    assert MASK_ERROR + kind in out


class FakeCT2ModelFailing(FakeCT2Model):
    """detect_language 依次抛 errors 里的错。"""

    def __init__(self, probs, errors):
        super().__init__(probs)
        self.errors = list(errors)

    def detect_language(self, audio=None, features=None, vad_filter=False, vad_parameters=None,
                        language_detection_segments=1, language_detection_threshold=0.5):
        self.detects.append({"audio": audio, "features": features, "vad_filter": vad_filter})
        raise self.errors.pop(0)


def test_detection_errors_are_told_once_per_cause_not_once_per_type(isolated, capsys):
    """同一类型、原因不同的错各说一次（只按类型去重会把后来的原因吞掉）；同一原因只是数字不同（形状、
    地址）算一种，不每段刷一行。每一段都照旧走重转，一段不少。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    errors = [RuntimeError("encoder input shape (1, 128, 3000)"),
              RuntimeError("encoder input shape (1, 128, 2999)"),
              RuntimeError("VAD model failed to load at 0x7f3a2c"),
              ValueError("empty audio"),
              RuntimeError("encoder input shape (1, 128, 3001)"),
              RuntimeError("VAD model failed to load at 0x7f9b10"),
              RuntimeError("out of memory")]
    model = install_ct2(isolated, FakeCT2ModelFailing(KO_FIRST, errors))
    t = ct2()
    results = [t.transcribe(PCM) for _ in range(7)]
    assert model.calls == [None, "es"] * 7
    assert all(r.relabeled_from == "ko" and r.signals["calls"] == 2 for r in results)
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith(MASK_ERROR)]
    assert lines == [
        MASK_ERROR + "RuntimeError: encoder input shape (1, 128, 3000)），改回先自动检测、不在主播语言里再整段重转",
        MASK_ERROR + "RuntimeError: VAD model failed to load at 0x7f3a2c），改回先自动检测、不在主播语言里再整段重转",
        MASK_ERROR + "ValueError: empty audio），改回先自动检测、不在主播语言里再整段重转",
        MASK_ERROR + "RuntimeError: out of memory），改回先自动检测、不在主播语言里再整段重转",
    ]
    assert MASK_ERROR_MORE not in out


def test_detection_error_kinds_are_capped_with_one_closing_line(isolated, capsys):
    """原因各不相同、又不止数字不同时，最多说 5 种，第 6 种起只补一句「不逐条提示」（也只说一次）；
    之后再出已经说过的原因也不重复。识别一段不停。"""
    isolated.setenv(asr.MASK_LANGS_ENV, "on")
    reasons = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "alpha"]
    model = install_ct2(isolated, FakeCT2ModelFailing(KO_FIRST, [RuntimeError(r) for r in reasons]))
    t = ct2()
    for _ in reasons:
        t.transcribe(PCM)
    assert model.calls == [None, "es"] * len(reasons)
    out = capsys.readouterr().out
    told = [line for line in out.splitlines() if line.startswith(MASK_ERROR)]
    assert [line.split("RuntimeError: ")[1].split("）")[0] for line in told] == reasons[:5]
    assert out.count(MASK_ERROR_MORE) == 1
    assert asr.MASK_ERROR_KINDS == 5
