"""自检报的必须是管线真正在用的引擎。

真实故障：默认档从 7B 改回 1.8B 时，只改了 create_translator，而 check_translator
里自己复制的那份 auto 选择逻辑没跟上。结果程序实际跑着 1.8B，面板却写「本地
Hy-MT2 7B」。自检报错东西比不自检更糟——它会让人相信一个错误的事实。
"""
import asyncio
from types import SimpleNamespace

from app import selfcheck


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FakeInner:
    def __init__(self, model):
        self.model = model


class FakeTranslator:
    def __init__(self, name, model=""):
        self.name = name
        self.inner = FakeInner(model)


def check(engine_name, model="", configured="auto", reachable=True, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(selfcheck, "_ollama_reachable", lambda: reachable)
    return run(selfcheck.check_translator(
        SimpleNamespace(translator=configured),
        FakeTranslator(engine_name, model)))


def test_reports_the_tier_the_pipeline_actually_built(monkeypatch):
    """auto 选了 1.8B 就必须说 1.8B，不能自己再推导一遍。"""
    c = check("hymt2", "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M", monkeypatch=monkeypatch)
    assert "1.8B" in c["detail"] and "7B" not in c["detail"]
    assert c["level"] == "ok"


def test_reports_7b_when_7b_is_what_is_running(monkeypatch):
    c = check("hymt2-7b", "hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M", monkeypatch=monkeypatch)
    assert "7B" in c["detail"]
    assert c["level"] == "ok"


def test_local_engine_with_ollama_down_is_a_failure(monkeypatch):
    """配了本地引擎但 Ollama 没开——这正是「配置看着对，其实不工作」。"""
    c = check("hymt2", "…1.8B…", reachable=False, monkeypatch=monkeypatch)
    assert c["level"] == "fail"
    assert c["fix"]


def test_google_is_a_warning_with_a_way_out(monkeypatch):
    """退回网络翻译要给出路，但那条出路里不该再有 ollama pull。

    模型现在由程序自己通过 Ollama 的 HTTP 接口拉取——用户装完 Ollama 就完事了。
    提示里再出现一行要敲的命令，等于把不会用终端的人重新挡在门外。
    """
    c = check("google", monkeypatch=monkeypatch)
    assert c["level"] == "warn"
    assert "ollama.com" in c["fix"]
    assert "ollama pull" not in c["fix"]
    assert "违禁词报警不受影响" in c["detail"]      # 别让人以为报警也废了


def test_no_translator_object_does_not_guess():
    """引擎还没建起来时，只说「还没初始化」，不能假装知道是哪一个。"""
    c = run(selfcheck.check_translator(SimpleNamespace(translator="auto"), None))
    assert c["level"] == "warn"
    assert "尚未初始化" in c["detail"]


def test_translation_off_on_purpose_is_ok():
    c = run(selfcheck.check_translator(SimpleNamespace(translator="none"), None))
    assert c["level"] == "ok"
