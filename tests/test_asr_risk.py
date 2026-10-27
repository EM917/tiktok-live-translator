"""识别风险标记 v1（app/asr_risk.py）与识别器往外给的数（ASRResult.signals / windows）。

钉住的是：冻结的常数（改一个就得升 RISK_VERSION）、各阈值的边界、NaN、整段套话只记不挂角标、
lp 那两条只对 mlx large-v3 生效、审计子对象里只有数字和 ASCII 代码、过滤（_fold）和主解码的
参数一个不变。全程不加载模型：mlx_whisper / faster_whisper 都是塞进 sys.modules 的假模块。
"""
import json
import math
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app import asr, asr_risk
from app.asr import ASRResult
from app.asr_risk import assess

LV3 = ("mlx", "large-v3")


def res(text="hola amigos como estan", raw=None, lp=-0.2, cr=1.3, nsp=0.01, relabeled=""):
    r = ASRResult(text=text, language="es", raw_text=text if raw is None else raw, rejected=[],
                  relabeled_from=relabeled)
    r.signals = {"lp": lp, "nsp": nsp, "cr": cr, "tok": 12, "t": 0.0, "calls": 1, "win": 1}
    return r


def hard_cut(speech_frames=90, frames=90):
    return {"cut": "max", "frames": frames, "speech_frames": speech_frames}


# ---- 冻结的常数 -----------------------------------------------------------------------------

def test_the_v1_constants_are_frozen():
    """改了任何一个阈值，这里必须一起改，而且 RISK_VERSION 要升（CLAUDE.md 第四条：
    rec_01 已经用来调过参，之后只评估、不重调；要改就升版本、换没看过的场次评）。"""
    assert asr_risk.RISK_VERSION == 1
    assert asr_risk.constants() == {
        "v": 1, "lp_below": -0.8, "lp_zero": -0.5, "lp_span": 0.6, "lp_models": ["mlx/large-v3"],
        "wpvs_below": 1.0, "wpvs_zero": 1.5, "wpvs_span": 1.0, "cut": "max", "voiced_min": 0.6,
        "ms_min": 8500, "cr_above": 2.0, "cr_zero": 1.8, "cr_span": 0.4,
        "chip": ["low_conf", "nan_lp", "few_words", "loop"], "audit_only": ["outro_only", "relabel"]}
    assert asr_risk.config_record(True)["on"] is True
    assert asr_risk.config_record(False)["on"] is False


def test_frame_length_matches_the_audio_source():
    from app.audio import FRAME_SEC
    assert asr_risk.FRAME_SEC == FRAME_SEC


# ---- avg_logprob ---------------------------------------------------------------------------

def test_low_confidence_is_strictly_below_minus_point_eight():
    assert "low_conf" not in assess(res(lp=-0.8), 4000, None, *LV3).flags
    risk = assess(res(lp=-0.8001), 4000, None, *LV3)
    assert risk.flags == ["low_conf"] and risk.chip and risk.evidence
    assert risk.audit["lp"] == -0.8 and risk.audit["risk"] == 0.5
    assert assess(res(lp=-0.2), 4000, None, *LV3).flags == []


@pytest.mark.parametrize("backend, model", [("mlx", "large-v3-turbo"), ("ct2", "large-v3"),
                                            ("ct2", "large-v3-turbo"), (None, None)])
def test_the_logprob_rules_only_apply_to_mlx_large_v3(backend, model):
    """阈值是在 mlx large-v3 上看的：别的模型 lp 照记，但不据此标记。"""
    risk = assess(res(lp=-1.5), 4000, None, backend, model)
    assert risk.flags == [] and not risk.chip and risk.audit["lp"] == -1.5
    nan = assess(res(lp=float("nan")), 4000, None, backend, model)
    assert nan.flags == [] and nan.audit["lp"] is None and nan.nan


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_logprob_is_null_in_the_audit_and_flagged(bad):
    risk = assess(res(text="!x", lp=bad), 2600, None, *LV3)
    assert risk.audit["lp"] is None
    assert "nan_lp" in risk.flags and risk.chip and risk.nan
    json.dumps(risk.audit, allow_nan=False)        # 严格 JSON 写得出来


# ---- 9 秒硬切里有声很久却只有几个词 -----------------------------------------------------------

def words(n):
    return " ".join("palabra{}".format(i) for i in range(n))


def test_few_words_boundary_on_words_per_voiced_second():
    # 90 帧全有声 = 9 秒有声：9 个词正好 1.0 词/秒，不标；8 个词 0.889，标
    at_one = assess(res(text=words(9)), 9000, hard_cut(), *LV3)
    assert at_one.audit["wpvs"] == 1.0 and "few_words" not in at_one.flags
    below = assess(res(text=words(8)), 9000, hard_cut(), *LV3)
    assert below.flags == ["few_words"] and below.chip
    assert below.audit["cut"] == "max" and below.audit["voiced"] == 1.0 and below.audit["words"] == 8


def test_few_words_needs_a_hard_cut_enough_voice_and_a_full_length_segment():
    # 有声帧 54/90 = 0.6 正好够；53/90 不够
    assert "few_words" in assess(res(text="hola"), 9000, hard_cut(54), *LV3).flags
    assert "few_words" not in assess(res(text="hola"), 9000, hard_cut(53), *LV3).flags
    # 段长 8500 ms 够，8499 不够
    assert "few_words" in assess(res(text="hola"), 8500, hard_cut(), *LV3).flags
    assert "few_words" not in assess(res(text="hola"), 8499, hard_cut(), *LV3).flags
    # 停顿切、flush、切段器没报（老的假切段器）都不看这一条
    for meta in ({"cut": "pause", "frames": 90, "speech_frames": 90},
                 {"cut": "flush", "frames": 90, "speech_frames": 90}, None, {}):
        risk = assess(res(text="hola"), 9000, meta, *LV3)
        assert "few_words" not in risk.flags
    assert assess(res(text="hola"), 9000, None, *LV3).audit["cut"] is None


def test_few_words_also_applies_to_other_models():
    """词数这一条看的是文字和有声时长，不看模型自己的 logprob：turbo / ct2 上照样生效。"""
    assert assess(res(text="hola"), 9000, hard_cut(), "ct2", "large-v3-turbo").flags == ["few_words"]


def test_words_are_counted_on_the_raw_text():
    """套话顶替的那几段，raw_text 里就只剩套话：词数用过滤前的 raw_text。"""
    risk = assess(res(text="", raw=words(20)), 9000, hard_cut(), *LV3)
    assert risk.audit["words"] == 20 and "few_words" not in risk.flags
    assert not risk.chip                     # 没有字幕就没有角标


# ---- 复读、整段套话、重转 ---------------------------------------------------------------------

def test_loop_is_strictly_above_two():
    assert "loop" not in assess(res(cr=2.0), 4000, None, *LV3).flags
    risk = assess(res(cr=2.01), 4000, None, "ct2", "small")
    assert risk.flags == ["loop"] and risk.chip


def test_an_outro_only_segment_is_recorded_but_never_gets_the_chip():
    """整段套话只进审计和证据，不进角标（design §3.4）：还能上屏的整段套话多半是模型很有把握的
    一句，挂「识别不确定」是冤枉。"""
    risk = assess(res(text="¡Gracias por ver el video!"), 2600, None, *LV3)
    assert risk.flags == ["outro_only"]
    assert risk.evidence and not risk.chip


def test_outro_uses_the_same_table_as_the_filter(monkeypatch):
    """和 _fold 用同一对函数：换一张表，两边一起变。"""
    monkeypatch.setattr(asr, "_HALLUCINATION_KEYS", {"holaamigos"})
    assert assess(res(text="Hola amigos."), 3000, None, *LV3).flags == ["outro_only"]
    assert "outro_only" not in assess(res(text="¡Gracias por ver el video!"), 3000, None, *LV3).flags

    class Folder(asr._FilterMixin):
        _context = ""

    folded = Folder()._fold([(0.0, 1.0, -0.9, "Hola amigos.")], "es")
    assert folded.text == "" and folded.rejected[0]["reason"] == "hallucination"


def test_relabel_is_audit_only():
    risk = assess(res(relabeled="ko"), 4000, None, *LV3)
    assert risk.flags == ["relabel"] and not risk.chip and not risk.evidence


def test_no_caption_means_no_chip_even_when_flagged():
    risk = assess(res(text="", raw="!", lp=-1.3), 2600, None, *LV3)
    assert "low_conf" in risk.flags and risk.evidence and not risk.chip and not risk.shown


# ---- 审计子对象 -------------------------------------------------------------------------------

def test_the_audit_object_holds_only_numbers_and_ascii_codes():
    risk = assess(res(text="¡Suscríbete al canal!", lp=float("nan"), relabeled="ko"), 9000,
                  hard_cut(70), *LV3)
    audit = risk.audit
    assert list(audit) == ["v", "ms", "cut", "voiced", "words", "wpvs", "lp", "nsp", "cr", "tok",
                           "t", "calls", "win", "risk", "flags"]
    text = json.dumps(audit, ensure_ascii=False, allow_nan=False)
    assert text.isascii()                    # 不带识别文字，也没有中文
    for key, value in audit.items():
        if key == "flags":
            assert all(isinstance(f, str) and f.isascii() for f in value)
        elif key == "cut":
            assert value in ("max", "pause", "flush", None)
        else:
            assert value is None or isinstance(value, (int, float)), (key, value)


def test_a_result_without_signals_still_gets_a_record():
    """测试和老代码里的假识别器返回不带 signals 的 SimpleNamespace：取不到的一律是 null。"""
    risk = assess(SimpleNamespace(text="hola", raw_text="hola", language="es"), 3000, None, *LV3)
    assert risk.audit["lp"] is None and risk.audit["tok"] is None and risk.flags == []


def test_switched_off_keeps_the_numbers_but_flags_nothing():
    risk = assess(res(lp=-1.5, cr=3.0), 9000, hard_cut(), *LV3, enabled=False)
    assert "risk" not in risk.audit and "flags" not in risk.audit
    assert risk.audit["lp"] == -1.5
    assert not risk.chip and not risk.evidence and risk.flags == []


@pytest.mark.parametrize("value, on", [(None, True), ("", True), ("on", True), ("1", True),
                                       ("off", False), ("OFF", False), (" 0 ", False),
                                       ("false", False), ("no", False)])
def test_the_master_switch(value, on):
    env = {} if value is None else {"TLT_ASR_SAFEGUARDS": value}
    assert asr_risk.master_switch_on(env) is on


def test_prompt_fingerprint():
    assert asr_risk.prompt_sha16(None) is None and asr_risk.prompt_sha16("") is None
    fp = asr_risk.prompt_sha16("Allura Belt Nalla, limpieza")
    assert len(fp) == 16 and fp == asr_risk.prompt_sha16("Allura Belt Nalla, limpieza")
    assert fp != asr_risk.prompt_sha16("otra cosa")


def test_the_session_counter_only_holds_numbers():
    counter = asr_risk.RiskCounter()
    for r in (assess(res(lp=-1.2), 4000, None, *LV3),                     # 上屏、挂角标
              assess(res(text="", raw="!", lp=float("nan")), 2600, None, *LV3),   # 被拦、标记
              assess(res(text="¡Gracias por ver el video!"), 2600, None, *LV3),   # 只记审计
              assess(res(), 4000, None, *LV3)):                            # 没事
        counter.add(r)
    counter.add(None)                                                      # 算不出风险分的段不计
    assert counter.summary() == {
        "v": 1, "on": True, "segments": 4, "shown": 3, "flagged": 3, "shown_flagged": 1,
        "nan_lp": 1, "by_flag": {"low_conf": 1, "nan_lp": 1, "outro_only": 1}}
    json.dumps(counter.summary(), allow_nan=False)


# ---- 识别器往外给的数：mlx -------------------------------------------------------------------

def _mlx_seg(text="hola amigos", lp=-0.3, nsp=0.02, cr=1.2, seek=0, start=0.0, end=4.0, t=0.0):
    return {"seek": seek, "start": start, "end": end, "text": " " + text, "tokens": [1, 2, 3, 4],
            "temperature": t, "avg_logprob": lp, "compression_ratio": cr, "no_speech_prob": nsp}


class FakeMLX:
    """假的 mlx_whisper：按顺序吐出安排好的输出，记下每次真实调用的参数（预热那次不带 temperature）。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def transcribe(self, audio, **kw):
        if "temperature" not in kw:
            return {"segments": [], "language": None, "text": ""}
        self.calls.append(kw)
        return self.outputs.pop(0)


def install_mlx(monkeypatch, outputs):
    fake = FakeMLX(outputs)
    module = ModuleType("mlx_whisper")
    module.transcribe = fake.transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)
    return fake


def test_mlx_signals_come_from_the_decoder_segments(monkeypatch):
    fake = install_mlx(monkeypatch, [{"language": "es", "text": "hola amigos que tal", "segments": [
        _mlx_seg("hola amigos", lp=-0.31, nsp=0.02, cr=1.1, start=0.0, end=4.0),
        _mlx_seg("que tal", lp=-0.31, nsp=0.05, cr=1.4, start=4.0, end=7.5)]}])
    t = asr.MLXTranscriber("large-v3", language="es", hotwords="Allura")
    result = t.transcribe(b"\x00\x00" * 16000)
    assert result.text == "hola amigos que tal"
    assert result.signals == {"lp": -0.31, "nsp": 0.05, "cr": 1.4, "tok": 8, "t": 0.0,
                              "calls": 1, "win": 1}
    assert [w["text"] for w in result.windows] == ["hola amigos", "que tal"]
    assert result.windows[1]["start"] == 4.0 and result.windows[1]["end"] == 7.5
    # 主解码的参数钉住：温度 0、不接上一段、fp16、热词提示，没有 sample_len 之类多出来的参数
    assert len(fake.calls) == 1
    kw = fake.calls[0]
    assert set(kw) == {"path_or_hf_repo", "language", "temperature",
                       "condition_on_previous_text", "fp16", "initial_prompt"}
    assert kw["temperature"] == 0.0 and kw["condition_on_previous_text"] is False
    assert kw["fp16"] is True and kw["initial_prompt"] == "Allura" and kw["language"] == "es"


def test_mlx_relabel_takes_the_second_pass_and_keeps_the_first(monkeypatch):
    install_mlx(monkeypatch, [
        {"language": "ko", "text": "감사합니다", "segments": [_mlx_seg("감사합니다", lp=-0.9)]},
        {"language": "es", "text": "gracias a todos", "segments": [_mlx_seg("gracias a todos", lp=-0.4)]}])
    t = asr.MLXTranscriber("large-v3", language="es,en")
    result = t.transcribe(b"\x00\x00" * 16000)
    assert result.relabeled_from == "ko"
    assert result.signals["calls"] == 2 and result.signals["lp"] == -0.4
    assert result.windows[0]["first_call"] == {"language": "ko", "text": "감사합니다"}


def test_mlx_nan_and_several_windows(monkeypatch):
    install_mlx(monkeypatch, [{"language": "es", "text": "!", "segments": [
        _mlx_seg("!", lp=float("nan"), seek=0), _mlx_seg("ya", lp=-0.2, seek=3000)]}])
    t = asr.MLXTranscriber("large-v3", language="es")
    result = t.transcribe(b"\x00\x00" * 16000)
    assert math.isnan(result.signals["lp"]) and result.signals["win"] == 2


def test_the_filter_is_unchanged_by_the_signals(monkeypatch):
    """_fold 的输入输出逐条不变：同一份解码输出，挂不挂 signals，text / raw_text / rejected 都一样。"""
    out = {"language": "es", "text": "x", "segments": [
        _mlx_seg("¡Suscríbete al canal!", lp=-0.4), _mlx_seg("tres por cien", lp=-1.7)]}
    install_mlx(monkeypatch, [dict(out)])
    t = asr.MLXTranscriber("large-v3", language="es")
    result = t.transcribe(b"\x00\x00" * 16000)

    class Folder(asr._FilterMixin):
        _context = ""

    expect = Folder()._fold(((s["no_speech_prob"], s["compression_ratio"], s["avg_logprob"],
                              s["text"]) for s in out["segments"]), "es")
    assert (result.text, result.raw_text, result.rejected) == (expect.text, expect.raw_text,
                                                               expect.rejected)
    assert result.signals["lp"] == -1.7


def test_broken_segments_only_lose_the_signals(monkeypatch):
    """子段里有怪东西（tokens 不是序列、时间戳不是数）：识别结果照旧，只是少几个数。"""
    seg = _mlx_seg("hola amigos")
    seg["tokens"], seg["start"] = 7, "?"
    install_mlx(monkeypatch, [{"language": "es", "text": "hola amigos", "segments": [seg]}])
    t = asr.MLXTranscriber("large-v3", language="es")
    result = t.transcribe(b"\x00\x00" * 16000)
    assert result.text == "hola amigos"
    assert result.signals["tok"] == 0 and result.windows[0]["start"] is None
    assert result.signals["lp"] == -0.3


def test_signal_extraction_errors_never_reach_the_caller(monkeypatch):
    """_decode_signals 自己抛错：结果照旧，signals / windows 是空的。"""
    install_mlx(monkeypatch, [{"language": "es", "text": "hola", "segments": [_mlx_seg("hola")]}])

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(asr, "_decode_signals", boom)
    t = asr.MLXTranscriber("large-v3", language="es")
    result = t.transcribe(b"\x00\x00" * 16000)
    assert result.text == "hola" and result.signals == {} and result.windows == []


# ---- 识别器往外给的数：ct2 -------------------------------------------------------------------

def test_ct2_signals_come_from_the_segment_objects(monkeypatch):
    seg = SimpleNamespace(seek=0, start=0.0, end=3.0, text=" hola", tokens=[5, 6],
                          temperature=0.0, avg_logprob=-0.5, compression_ratio=1.0,
                          no_speech_prob=0.1)

    class Model:
        def transcribe(self, audio, **kw):
            return [seg], SimpleNamespace(language="es")

    fake = ModuleType("faster_whisper")
    fake.WhisperModel = lambda *a, **k: Model()
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    t = asr.Transcriber("small", device="cpu", compute_type="int8", language="es")
    result = t.transcribe(b"\x00\x00" * 16000)
    assert result.text == "hola"
    assert result.signals == {"lp": -0.5, "nsp": 0.1, "cr": 1.0, "tok": 2, "t": 0.0,
                              "calls": 1, "win": 1}
