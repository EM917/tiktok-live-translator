"""译文串台：本地模型偶尔把一个词写成别的文字（韩/日/泰文）。

2026-09-19 上屏的一条是「好的，在32号에도你有测试项。」；回看 9 月 15–19 日的
7212 条译文，这样的有 5 条，全部出自本地 1.8B。下面的用例原样取自审计日志。"""
import json

import pytest

from app.translator import (RETRY_SEED, RETRY_TEMPERATURE, OllamaHyMT2Translator,
                            foreign_script_runs, strip_foreign_script)
from tests.helpers import run

REAL = [
    ("Ok, en el 32 también tienes testemis.", "好的，在32号에도你有测试项。", ["에도"]),
    ("ustedes los tienen que aplicar. ¡Dos de mis favoritos!", "你们必须去申请。是我最喜欢的 두 가지！", ["두", "가지"]),
    ("Es lo único que va a pasar, que te va a dar mucha energía.", "这就是唯一会发生的こと，它会给你带来很多能量。", ["こと"]),
    ("Esto lo puede hacer con agua fría, con agua caliente.", "可以用冷水、热水、牛奶或冰块来ทำ这样的事。", ["ทำ"]),
]


@pytest.mark.parametrize("source,translated,runs", REAL)
def test_the_real_cases_are_detected(source, translated, runs):
    assert foreign_script_runs(source, translated, "zh-CN") == runs


def test_ordinary_translations_are_clean():
    # 拉丁写法的品牌名、数字、表情、全角标点都不算
    for out in ("今天是 35 美元，Healthy-Life 牌。", "第32条中我们有All Natural。", "非常感谢 🥳💕", "D3 K2 维生素滴剂"):
        assert foreign_script_runs("Son 35 dólares hoy", out, "zh-CN") == []


def test_a_script_present_in_the_source_is_not_foreign():
    """主播真念了个韩文/日文品牌名，译文保留它不算串台。"""
    assert foreign_script_runs("la marca 무신사 es coreana", "무신사 这个牌子是韩国的", "zh-CN") == []


def test_the_target_language_keeps_its_own_script():
    assert foreign_script_runs("hola", "こんにちは", "ja") == []
    assert foreign_script_runs("hola", "안녕하세요", "ko") == []
    assert foreign_script_runs("hola", "привет", "ru") == []
    assert foreign_script_runs("hola", "你好 안녕", "en") == ["你好", "안녕"]


def test_stripping_removes_only_the_foreign_characters():
    assert strip_foreign_script(REAL[0][0], REAL[0][1], "zh-CN") == "好的，在32号你有测试项。"
    assert strip_foreign_script("Son 35", "今天是 35 美元", "zh-CN") == "今天是 35 美元"


# ---- translate()：发现串台换采样重译一次 ----

class FakeResp:
    def __init__(self, payload, status=200):
        self.status, self._payload = status, payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, answers):
        self.answers = list(answers)
        self.options = []          # 每次生成请求的 options

    def post(self, url, data=None, **kw):
        if url.endswith("/api/show"):
            return FakeResp({"template": "{{ .Prompt }}"})
        self.options.append(json.loads(data)["options"])
        return FakeResp({"response": self.answers.pop(0), "done_reason": "stop"})


@pytest.fixture(autouse=True)
def _clear_cache():
    from app import translator
    translator._RAW_MODE.clear()
    yield
    translator._RAW_MODE.clear()


async def _wrap(session):
    return session


def make(answers):
    session = FakeSession(answers)
    tr = OllamaHyMT2Translator()
    tr.session = lambda: _wrap(session)
    return tr, session


def test_a_clean_translation_costs_one_request():
    tr, session = make(["好的，在32号你也有测试项。"])
    assert run(tr.translate("Ok, en el 32 también tienes testemis.", "zh-CN", "es")) == "好的，在32号你也有测试项。"
    assert len(session.options) == 1 and session.options[0]["temperature"] == 0


def test_a_mixed_translation_is_retried_once_with_other_sampling():
    tr, session = make(["好的，在32号에도你有测试项。", "好的，32号里你也有测试项。"])
    out = run(tr.translate("Ok, en el 32 también tienes testemis.", "zh-CN", "es"))
    assert out == "好的，32号里你也有测试项。"
    assert len(session.options) == 2
    # 默认 temperature=0 是确定性的：重译必须换采样，否则只会拿回同一句
    assert session.options[1]["temperature"] == RETRY_TEMPERATURE
    assert session.options[1]["seed"] == RETRY_SEED


def test_when_the_retry_is_mixed_too_the_foreign_characters_are_removed():
    tr, session = make(["好的，在32号에도你有测试项。", "好的，在32号에你有测试项。"])
    out = run(tr.translate("Ok, en el 32 también tienes testemis.", "zh-CN", "es"))
    assert out == "好的，在32号你有测试项。"
    assert len(session.options) == 2          # 只重译一次，不循环


def test_a_failed_retry_falls_back_to_the_stripped_first_answer():
    tr, session = make(["好的，在32号에도你有测试项。", ""])
    assert run(tr.translate("Ok, en el 32 también tienes testemis.", "zh-CN", "es")) == "好的，在32号你有测试项。"
