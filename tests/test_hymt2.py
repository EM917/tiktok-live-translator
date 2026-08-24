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


def test_auto_prefers_hymt2_then_gemma_then_google(monkeypatch):
    from app import translator

    def resolve(models):
        monkeypatch.setattr(translator, "_ollama_models", lambda: models)
        tr = translator.create_translator("auto")
        name = tr.name if tr else "none"
        return name

    assert resolve(["hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M", "translategemma:4b"]) == "hymt2"
    assert resolve(["translategemma:4b"]) == "gemma"
    assert resolve([]) == "google"
