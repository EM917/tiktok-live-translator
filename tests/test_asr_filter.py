"""asr._FilterMixin：质量过滤 + 幻觉丢弃 + 滚动上下文（纯逻辑，两后端共用）。"""
from app.asr import _FilterMixin


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
