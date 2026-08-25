"""raw 模式必须按模型判定，不能一刀切。

起因是一次实盘复核：7B 在 567 段里 7.8% 只译出片段、7.1% 变成了回话
（"¿Cómo se toman las gotitas?" → "服用滴剂的方法如下：首先，用温水…"），
还漏出过 `<｠end▁of▁message`。根因是我们把 1.8B 的对话标记套给了 7B——
1.8B 的自带模板确实是坏的（`{{ if .Prompt }}` 里没有 `{{ .Prompt }}`），
但 7B 的是好的，而且用的是另一套标记（`<|startoftext|>`、`<|extra_0|>`）。
对 7B 来说我们拼的标记只是普通文本，用户轮次从来没有被闭合，于是它一直
以为在聊天。
"""
import asyncio

import pytest

from app.translator import OllamaHyMT2Translator, _strip_special


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FakeResp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """记录 POST 过的地址，好确认 /api/show 只问一次。"""

    def __init__(self, template="", status=200, boom=False):
        self.template, self.status, self.boom = template, status, boom
        self.calls = []

    def post(self, url, data=None, **kw):
        self.calls.append(url)
        if self.boom:
            raise RuntimeError("Ollama 不通")
        return FakeResp(self.status, {"template": self.template})


@pytest.fixture(autouse=True)
def _clear_cache():
    from app import translator
    translator._RAW_MODE.clear()
    yield
    translator._RAW_MODE.clear()


def _make(session):
    tr = OllamaHyMT2Translator()
    tr.session = lambda: _wrap(session)
    return tr


async def _wrap(session):
    return session


def test_a_broken_template_means_we_assemble_the_markers_ourselves():
    """1.8B：`{{ if .Prompt }}` 块里没有 `{{ .Prompt }}`，用户文本进不去模型。"""
    broken = "{{ if .Prompt }}<｜hy_place▁holder▁no▁8｜>{{ end }}onse }}"
    assert run(_make(FakeSession(broken))._needs_raw()) is True


def test_a_working_template_is_used_instead_of_our_markers():
    """7B：模板完好，套我们的标记反而会让它以为在聊天。"""
    ok = "{{ if .Prompt }}<|startoftext|>{{ .Prompt }}<|extra_0|>{{ end }}{{ .Response }}"
    assert run(_make(FakeSession(ok))._needs_raw()) is False


def test_the_template_is_only_fetched_once_per_model():
    """每句字幕都去问一次 Ollama 是白白加一次往返。"""
    s = FakeSession("{{ .Prompt }}")
    tr = _make(s)
    run(tr._needs_raw())
    run(tr._needs_raw())
    assert len([c for c in s.calls if "show" in c]) == 1


def test_an_unreachable_ollama_falls_back_to_raw():
    """问不到就按改动之前的行为走：模板真坏的那档只有 raw 能出正确结果。"""
    assert run(_make(FakeSession(boom=True))._needs_raw()) is True
    from app import translator
    translator._RAW_MODE.clear()
    assert run(_make(FakeSession("", status=404))._needs_raw()) is True


def test_the_end_of_message_marker_is_stripped():
    """实测漏进过字幕：「价格应该是57.20才对。<｠end▁of▁message」。
    它不带 hy 前缀，原来的正则拦不住。"""
    assert _strip_special("价格应该是57.20才对。<｠end▁of▁message") == "价格应该是57.20才对。"
    assert _strip_special("嗨<end▁of▁message") == "嗨"


def test_stripping_does_not_touch_ordinary_subtitles():
    for good in ("正常的中文译文，不该被动", "价格是 57.20 美元", "D3 K2 维生素滴剂"):
        assert _strip_special(good) == good
