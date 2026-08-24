"""Hy-MT2 引擎：官方术语格式 + 手动套用 chat template。"""
from app.translator import OllamaGemmaTranslator, OllamaHyMT2Translator, _as_pairs


def test_terminology_uses_the_official_line_format():
    """Hy-MT2 官方的术语格式是逐行 "X translates to Y"，不是紧凑串。"""
    p = OllamaHyMT2Translator()._prompt(
        "hola", "zh-CN", "es", [("las gotas", "D3 K2 维生素滴剂")])
    assert p.startswith("Reference the following translations:\n")
    assert "las gotas translates to D3 K2 维生素滴剂" in p
    assert "hola" in p


def test_no_glossary_means_no_reference_block():
    p = OllamaHyMT2Translator()._prompt("hola", "zh-CN", "es", None)
    assert "Reference the following" not in p
    assert p.startswith("Translate the following text into Simplified Chinese")


def test_chat_markers_wrap_the_prompt():
    """官方 GGUF 注册给 Ollama 的模板是坏的（`{{ if .Prompt }}` 里没有
    `{{ .Prompt }}`），模型收不到用户文本，只会吐乱码。所以我们用 raw 模式
    自己拼对话标记——这几个常量错了就会静默退化成乱码。"""
    t = OllamaHyMT2Translator()
    assert t._BOS.startswith("<") and t._USER and t._ASSISTANT
    assert t._OPTIONS["stop"]


def test_both_engines_accept_pairs_and_the_legacy_string():
    """管线只传一种形态（词对元组）；旧的紧凑串也要继续能用。"""
    pairs = [("a", "甲"), ("b", "乙")]
    assert _as_pairs(pairs) == pairs
    assert _as_pairs("a = 甲; b = 乙") == pairs
    assert _as_pairs("") == []
    assert _as_pairs(None) == []

    g = OllamaGemmaTranslator()._prompt("x", "zh-CN", "es", pairs)
    assert "a = 甲; b = 乙" in g
    h = OllamaHyMT2Translator()._prompt("x", "zh-CN", "es", "a = 甲; b = 乙")
    assert "a translates to 甲" in h


def test_the_two_hymt2_tiers_are_distinguishable():
    """两档的模型不同、质量不同，界面和日志必须报得出是哪一档。"""
    from app import translator

    small = translator.create_translator("hymt2")
    big = translator.create_translator("hymt2-7b")
    assert small.name == "hymt2" and big.name == "hymt2-7b"
    assert small.inner.model != big.inner.model
    assert "7B" in big.inner.model


def test_auto_prefers_hymt2_then_gemma_then_google(monkeypatch):
    from app import translator

    def resolve(models):
        monkeypatch.setattr(translator, "_ollama_models", lambda: models)
        tr = translator.create_translator("auto")
        name = tr.name if tr else "none"
        return name

    assert resolve(["hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M", "translategemma:4b"]) == "hymt2"
    # 7B 也装了也不自动选它：它把识别拖慢 2.3 倍，而识别在报警路径上。
    # 想用得显式指定（见 create_translator 里的实测记录）。
    assert resolve(["hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M",
                    "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M"]) == "hymt2"
    assert resolve(["translategemma:4b"]) == "gemma"
    assert resolve([]) == "google"


def test_special_tokens_never_reach_the_caption():
    """模型残留标记绝不能出现在字幕上。

    这里的每个样本都来自实盘。第一版兜底只匹配 <...hy_...>，88 条字幕里仍有
    12 条（13.6%）带着标记上了屏——因为模型吐出来的多数**没有尖括号**，
    就是一个全角竖线加 token 名。括号变体也不止一种（｜ U+FF5C 和 ｠ U+FF60）。
    """
    from app.translator import _strip_special

    # 无尖括号（实盘里最常见的形态）
    assert _strip_special("由于太受欢迎而售罄。｜hy_begin▁of▁sentence") == "由于太受欢迎而售罄。"
    assert _strip_special("你实际上是在为自己支付30%的费用。｜hy_User") == "你实际上是在为自己支付30%的费用。"
    # 带尖括号，两种竖线变体
    assert _strip_special("索菲亚！<｠hy_end▁of▁sentence｠>") == "索菲亚！"
    assert _strip_special("你好<｜hy_end▁of▁sentence｜>") == "你好"
    assert _strip_special("你好<｜hy_place▁holder▁no▁2｜>世界") == "你好世界"


def test_strip_leaves_ordinary_captions_alone():
    """字幕里本来就有尖括号和百分号，不能顺手吃掉。"""
    from app.translator import _strip_special

    for text in ("价格 <20 美元", "a <b> c", "含 30% 蛋白质",
                 "订单满 30 美元就送滴剂", "Se toman dos al día."):
        assert _strip_special(text) == text
