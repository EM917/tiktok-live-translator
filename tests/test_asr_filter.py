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
