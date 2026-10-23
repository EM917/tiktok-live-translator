"""生成阶段的长度上限——让捏造在物理上吐不出来。

事后检查是补不完的：第一版看「丢了问句/数字」，漏掉了短句后接一整段虚构内容；
补上长度比之后，谁知道下一种是什么样。所以在生成时就按源文长度限制 token 数，
把「事后判掉」换成「根本产不出来」。

阈值取自实测而非拍脑袋：70 句真实翻译的「输出 token / 源文字符」比值最大
0.364（中位 0.22），上限取 0.8 留 2.2 倍余量，那 70 句零截断；而捏造那条的
比值约 3.6，必然撞上限。于是「撞上限」本身成了近乎确定的信号。

后半是识别（Whisper）解码的生成上限（专家审查 M6）：复读死循环会一直解码到上限才停，mlx 不传时是
224 个 token。开关 TLT_ASR_TOKEN_CAP 默认关；开了以后 mlx 传 sample_len=112、faster-whisper（ct2）传
max_new_tokens=112——两个后端参数名不一样。全程用假的 mlx_whisper / faster_whisper（塞进 sys.modules），
不 import 真的 mlx、不加载任何模型（做法同 tests/test_mlx_cache_limit.py）。
"""
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app import asr
from app.translator import (MIN_PREDICT, TOKENS_PER_SOURCE_CHAR, predict_cap)


def test_the_cap_scales_with_the_source():
    assert predict_cap("x" * 100) == 80
    assert predict_cap("x" * 250) == 200


def test_short_sources_get_a_floor():
    """短句按比例算会小到把正常译文卡掉，所以有地板。"""
    assert predict_cap("Nada.") == MIN_PREDICT
    assert predict_cap("Sigan dando el micrófono.") == MIN_PREDICT
    assert predict_cap("") == MIN_PREDICT


def test_the_cap_is_never_unbounded():
    assert predict_cap("x" * 100000) == 200


def test_the_margin_over_real_translations_is_real():
    """实测最大比值 0.364，上限 0.8 —— 余量要留够，否则会截断正常译文。"""
    assert TOKENS_PER_SOURCE_CHAR / 0.364 > 2.0


def test_a_fabricated_length_would_exceed_the_cap():
    """实盘那条：25 字符源文吐出约 90 token（比值 3.6）——必然撞上限。"""
    src = "Sigan dando el micrófono."
    would_need = len(src) * 3.6
    assert would_need > predict_cap(src)


# ---- 识别（Whisper）解码的生成上限（M6，开关默认关）--------------------------------------------

PCM = b"\x00\x00" * 1600

# 改动前两个后端每次识别传的参数：开关关着时必须一个不多一个不少
MLX_KEYS_BEFORE = {"path_or_hf_repo", "language", "temperature", "condition_on_previous_text", "fp16"}
CT2_KEYS_BEFORE = {"language", "vad_filter", "beam_size", "temperature", "condition_on_previous_text"}


class FakeCT2Model:
    """假的 faster_whisper.WhisperModel。transcribe 的签名照 faster-whisper 1.2.1 写成具名参数、没有
    **kwargs（只列这里用得到的几个）：传了它不认的名字（比如 mlx 的 sample_len）就和真的一样 TypeError。"""

    def __init__(self, languages):
        self.languages = list(languages)
        self.calls = []

    def transcribe(self, audio, language=None, vad_filter=False, beam_size=5, temperature=0.0,
                   condition_on_previous_text=True, initial_prompt=None, max_new_tokens=None):
        kw = {"language": language, "vad_filter": vad_filter, "beam_size": beam_size,
              "temperature": temperature, "condition_on_previous_text": condition_on_previous_text}
        if initial_prompt is not None:
            kw["initial_prompt"] = initial_prompt
        if max_new_tokens is not None:
            kw["max_new_tokens"] = max_new_tokens
        self.calls.append(kw)
        lang = language or (self.languages.pop(0) if self.languages else "es")
        segment = SimpleNamespace(no_speech_prob=0.1, compression_ratio=1.2, avg_logprob=-0.3,
                                  text="hola", tokens=[1, 2, 3], seek=0, start=0.0, end=1.0,
                                  temperature=0.0)
        return [segment], SimpleNamespace(language=lang)


@pytest.fixture
def whisper(monkeypatch):
    """假的两个后端，记下每次 transcribe 的参数（mlx 的预热那次单独记）。开关的环境变量先清掉；
    真的 mlx 先藏起来，用例结束断言没 import 过。"""
    for name in [n for n in sys.modules if n == "mlx" or n.startswith(("mlx.", "mlx_whisper"))]:
        monkeypatch.delitem(sys.modules, name)
    for name in (asr.TOKEN_CAP_ENV, asr.MASK_LANGS_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(asr, "_mlx_said", set())
    state = SimpleNamespace(mlx=[], warmup=[], mlx_langs=[], ct2_langs=[], ct2=None)

    def mlx_transcribe(audio, **kw):
        if "temperature" not in kw:
            state.warmup.append(kw)
            return {"segments": [], "language": None}
        state.mlx.append(kw)
        lang = kw.get("language") or (state.mlx_langs.pop(0) if state.mlx_langs else "es")
        return {"segments": [{"no_speech_prob": 0.1, "compression_ratio": 1.2,
                              "avg_logprob": -0.3, "text": "hola", "tokens": [1, 2, 3]}],
                "language": lang}

    mlx = ModuleType("mlx_whisper")
    mlx.transcribe = mlx_transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", mlx)

    def whisper_model(*args, **kwargs):
        state.ct2 = FakeCT2Model(state.ct2_langs)
        return state.ct2

    fw = ModuleType("faster_whisper")
    fw.WhisperModel = whisper_model
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    yield state
    assert getattr(sys.modules.get("mlx.core"), "__file__", None) is None, "不许 import 真的 mlx.core"


def ct2(language="es", **kw):
    return asr.Transcriber("small", device="cpu", compute_type="int8", language=language, **kw)


def mlx(language="es", **kw):
    return asr.MLXTranscriber("large-v3", language=language, **kw)


def test_whisper_cap_is_off_by_default_and_calls_are_unchanged(whisper):
    """开关默认关：两个后端传的参数和改动前一个不多一个不少。"""
    m, c = mlx(), ct2()
    assert m.token_cap is None and c.token_cap is None
    m.transcribe(PCM)
    c.transcribe(PCM)
    assert set(whisper.mlx[-1]) == MLX_KEYS_BEFORE
    assert set(whisper.ct2.calls[-1]) == CT2_KEYS_BEFORE


def test_mlx_cap_is_sample_len_112(whisper, monkeypatch):
    """mlx-whisper 的上限叫 sample_len（经 decode_options 进 DecodingOptions；不传是 224）。"""
    monkeypatch.setenv(asr.TOKEN_CAP_ENV, "on")
    m = mlx()
    assert m.token_cap == asr.DECODE_TOKEN_CAP == 112
    m.transcribe(PCM)
    kw = whisper.mlx[-1]
    assert kw["sample_len"] == 112
    assert "max_new_tokens" not in kw           # ct2 的名字传给 mlx 的 DecodingOptions 会 TypeError
    assert set(kw) == MLX_KEYS_BEFORE | {"sample_len"}
    assert all("sample_len" not in w for w in whisper.warmup)    # 预热那次不受影响


def test_ct2_cap_is_max_new_tokens_112(whisper, monkeypatch):
    """faster-whisper 的上限叫 max_new_tokens；传 sample_len 会 TypeError（假模型的签名和真的一样严）。"""
    monkeypatch.setenv(asr.TOKEN_CAP_ENV, "on")
    c = ct2(hotwords="Producto Demo")
    assert c.token_cap == 112
    c.transcribe(PCM)
    kw = whisper.ct2.calls[-1]
    assert kw["max_new_tokens"] == 112
    assert "sample_len" not in kw
    assert kw["initial_prompt"] == "Producto Demo"         # 热词照旧
    with pytest.raises(TypeError):
        whisper.ct2.transcribe(None, sample_len=112)


def test_cap_also_bounds_the_relabel_rerun(whisper, monkeypatch):
    """检测跑偏后的整段重转（开关关着的语言屏蔽）也受同一个上限：复读死循环在重转里一样会发生。"""
    monkeypatch.setenv(asr.TOKEN_CAP_ENV, "on")
    whisper.mlx_langs[:] = ["ko"]
    whisper.ct2_langs[:] = ["ko"]
    m, c = mlx(language="es,en"), ct2(language="es,en")
    rm, rc = m.transcribe(PCM), c.transcribe(PCM)
    assert [kw["language"] for kw in whisper.mlx] == [None, "es"]
    assert [kw["sample_len"] for kw in whisper.mlx] == [112, 112]
    assert [kw["language"] for kw in whisper.ct2.calls] == [None, "es"]
    assert [kw["max_new_tokens"] for kw in whisper.ct2.calls] == [112, 112]
    assert rm.relabeled_from == rc.relabeled_from == "ko"


@pytest.mark.parametrize("value", ["on", "ON", " 1 ", "true", "Yes"])
def test_cap_switch_on_words(value):
    assert asr.decode_switches(environ={asr.TOKEN_CAP_ENV: value})[0] == 112


@pytest.mark.parametrize("value", ["", "off", "0", "no", "false", "112", "enable", "开"])
def test_cap_switch_anything_else_is_off(value):
    """默认关，写错也是关；开关只认开或关，不认数字（上限的数写在代码里，评测改要走 token_cap 参数）。"""
    assert asr.decode_switches(environ={asr.TOKEN_CAP_ENV: value})[0] is None


def test_explicit_cap_overrides_the_switch_and_is_checked():
    on = {asr.TOKEN_CAP_ENV: "on"}
    assert asr.decode_switches(token_cap=0, environ=on)[0] is None
    assert asr.decode_switches(token_cap=64, environ={})[0] == 64
    assert asr.decode_switches(token_cap=asr.TOKEN_CAP_MAX, environ={})[0] == asr.TOKEN_CAP_MAX
    for bad in (-1, True, 1.5, "112", asr.TOKEN_CAP_MAX + 1):
        with pytest.raises(ValueError):
            asr.decode_switches(token_cap=bad, environ={})


def test_cap_fits_under_faster_whisper_longest_prompt():
    """faster-whisper 在「提示长度 + max_new_tokens > 448」时直接报错。提示最长是 1（sot_prev）+ 223（热词
    截到 max_length // 2 - 1）+ 3（起始 token）：112 加上去还有余量；而且确实比 mlx 不传时的 224 小。"""
    assert asr.WHISPER_MAX_LENGTH == 448
    assert asr.LONGEST_PROMPT_TOKENS == 227
    assert asr.LONGEST_PROMPT_TOKENS + asr.DECODE_TOKEN_CAP <= asr.WHISPER_MAX_LENGTH
    assert asr.LONGEST_PROMPT_TOKENS + asr.TOKEN_CAP_MAX == asr.WHISPER_MAX_LENGTH
    assert asr.DECODE_TOKEN_CAP < asr.WHISPER_MAX_LENGTH // 2


def test_cap_switch_is_announced_once_in_the_terminal(whisper, monkeypatch, capsys):
    monkeypatch.setenv(asr.TOKEN_CAP_ENV, "on")
    mlx()
    ct2()
    out = capsys.readouterr().out
    assert out.count("[信息] 识别生成上限：每段最多解码 112 个 token") == 1
