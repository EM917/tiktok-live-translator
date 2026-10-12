"""Claude 引擎升级到 Haiku 5.5：请求体、读回应、拒答。

接口规则来自 claude-api skill 的迁移指南「Migrating to Claude Haiku 5.5」与 Thinking 表：
思考默认开（adaptive，effort 默认 medium）；{"type": "disabled"} 只在 effort ≤ high 时被接受；
非默认的 temperature/top_p/top_k 400；没有 budget_tokens；不能预填；拒答没有服务端兜底
（fallbacks 列表形式 400）。回应可以以 thinking 块开头，拒答是 HTTP 200 + stop_reason "refusal"。

测试一律用假 HTTP 层，不发任何真实请求。
"""
import json
import time

import pytest

from app.translator import (CachedTranslator, ClaudeTranslator, begin_translate_call,
                            translate_call_outcome)
from tests.helpers import run

FORBIDDEN_KEYS = ("temperature", "top_p", "top_k", "budget_tokens", "fallbacks")


class Resp:
    def __init__(self, status, payload=None, text=None, headers=None):
        self.status = status
        self._payload = payload
        self._text = text
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        if self._text is not None:
            return self._text
        return json.dumps(self._payload) if self._payload is not None else ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class RecordingSession:
    """按顺序回应，并把每次请求的地址、请求体（解析成 dict）、请求头都记下来。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.gets = []

    def post(self, url, data=None, headers=None, **kw):
        self.posts.append({"url": url, "body": json.loads(data), "headers": dict(headers or {})})
        return self.responses.pop(0)

    def get(self, url, params=None, headers=None, **kw):
        self.gets.append({"url": url, "headers": dict(headers or {})})
        return self.responses.pop(0)


def attach(engine, session):
    async def get_session():
        return session

    engine.session = get_session


def message(*blocks, stop="end_turn", **extra):
    return dict({"type": "message", "role": "assistant", "model": "claude-haiku-5-5",
                 "content": list(blocks), "stop_reason": stop, "stop_details": None,
                 "usage": {"input_tokens": 60, "output_tokens": 12}}, **extra)


def text_block(text):
    return {"type": "text", "text": text}


def thinking_block():
    # Haiku 5.5 默认不回思考原文：thinking 字段是空串，只有签名
    return {"type": "thinking", "thinking": "", "signature": "sig"}


def make(monkeypatch, model=None):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    if model is None:
        monkeypatch.delenv("CLAUDE_TRANSLATE_MODEL", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_TRANSLATE_MODEL", model)
    return ClaudeTranslator()


def keys_anywhere(obj):
    """请求体里所有层级出现过的键。"""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= keys_anywhere(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= keys_anywhere(v)
    return out


def sent_body(monkeypatch, model=None, response=None):
    tr = make(monkeypatch, model)
    session = RecordingSession([Resp(200, response or message(text_block("你好")))])
    attach(tr, session)
    out = run(tr.translate("hola amigos", "zh-CN", source="es"))
    (post,) = session.posts
    return tr, out, post


# ---------------------------------------------------------------------------
# 默认模型与请求体
# ---------------------------------------------------------------------------

def test_the_default_model_is_haiku_5_5_with_no_date_suffix(monkeypatch):
    tr = make(monkeypatch)
    assert tr.model == "claude-haiku-5-5"
    assert ClaudeTranslator.DEFAULT_MODEL == ClaudeTranslator.MODEL == "claude-haiku-5-5"


def test_haiku_5_5_turns_thinking_off_at_an_effort_that_accepts_it(monkeypatch):
    tr, out, post = sent_body(monkeypatch)
    body = post["body"]
    assert out == "你好"
    assert body["model"] == "claude-haiku-5-5"
    assert body["thinking"] == {"type": "disabled"}
    # disabled 只在 effort 为 low/medium/high 时被接受，xhigh/max 会 400
    assert body["output_config"]["effort"] in ("low", "medium", "high")
    assert body["output_config"] == {"effort": "low"}
    assert body["max_tokens"] == ClaudeTranslator.MAX_TOKENS_NO_THINKING
    # 以 user 轮结尾：预填会 400
    assert body["messages"] == [{"role": "user", "content": "hola amigos"}]
    assert "Simplified Chinese" in body["system"]
    assert not keys_anywhere(body) & set(FORBIDDEN_KEYS)
    assert post["url"] == "https://api.anthropic.com/v1/messages"
    assert post["headers"]["anthropic-version"] == "2023-06-01"
    assert post["headers"]["x-api-key"] == "sk-ant-test-key"
    assert not any(h.lower() == "anthropic-beta" for h in post["headers"])


def test_every_tuned_model_that_disables_thinking_keeps_effort_at_high_or_below():
    """把「disabled 只在 effort ≤ high 时被接受」钉在表上：以后有人往 TUNED 里加一行
    或把 effort 调成 xhigh，这里先红，而不是直播里每一句都 400。"""
    for model, extra in ClaudeTranslator.TUNED.items():
        assert not keys_anywhere(extra) & set(FORBIDDEN_KEYS), model
        if (extra.get("thinking") or {}).get("type") == "disabled":
            assert (extra.get("output_config") or {}).get("effort") in ("low", "medium", "high"), model


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5"])
def test_disabled_thinking_is_never_sent_to_models_that_reject_it(monkeypatch, model):
    """Opus 5.5 和 Sonnet 5.5 对 {"type": "disabled"} 一律 400：TUNED 不能泛化成「新模型都关思考」。"""
    assert model not in ClaudeTranslator.TUNED
    _tr, _out, post = sent_body(monkeypatch, model)
    body = post["body"]
    assert body["model"] == model
    assert "thinking" not in body and "output_config" not in body
    assert not keys_anywhere(body) & set(FORBIDDEN_KEYS)
    # 这些模型默认开思考，思考算进 max_tokens
    assert body["max_tokens"] == ClaudeTranslator.MAX_TOKENS_MAY_THINK


@pytest.mark.parametrize("model", ["claude-haiku-4-5-20251001", "claude-haiku-4-5"])
def test_an_older_model_from_the_environment_gets_the_plain_request(monkeypatch, model):
    """effort 在 Haiku 4.5 上会报错；disabled 在 Haiku 4.5 上收不收，手头的文档没写——都不发。"""
    tr, out, post = sent_body(monkeypatch, model, message(text_block("你好")))
    body = post["body"]
    assert tr.model == model and body["model"] == model
    assert set(body) == {"model", "max_tokens", "system", "messages"}
    assert out == "你好"


def test_a_blank_environment_override_falls_back_to_the_default(monkeypatch):
    assert make(monkeypatch, "   ").model == "claude-haiku-5-5"


def test_the_request_body_is_a_copy_of_the_tuned_table(monkeypatch):
    tr = make(monkeypatch)
    body = tr._body("hola", "zh-CN")
    body["thinking"]["type"] = "adaptive"
    body["output_config"]["effort"] = "max"
    assert ClaudeTranslator.TUNED["claude-haiku-5-5"] == {
        "thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}


def test_the_probe_asks_about_the_model_actually_in_use(monkeypatch):
    tr = make(monkeypatch, "claude-haiku-4-5")
    session = RecordingSession([Resp(200, {})])
    attach(tr, session)
    assert run(tr.probe_key()) == 200
    assert session.gets[0]["url"].endswith("/v1/models/claude-haiku-4-5")


# ---------------------------------------------------------------------------
# 读回应：先看 stop_reason，再按 type 取文本
# ---------------------------------------------------------------------------

def test_a_response_that_starts_with_a_thinking_block_still_yields_the_text(monkeypatch):
    _tr, out, _post = sent_body(monkeypatch, response=message(thinking_block(),
                                                              text_block("先付款，再发货")))
    assert out == "先付款，再发货"


def test_text_blocks_are_joined_and_other_blocks_ignored():
    data = message(thinking_block(), text_block("满 30 美元 "), {"type": "redacted_thinking"},
                   text_block("包邮"))
    assert ClaudeTranslator.read_message(data) == ("满 30 美元 包邮", "end_turn", None)


@pytest.mark.parametrize("data", [None, [], {}, {"content": None}, {"content": [{"type": "text"}]}])
def test_odd_responses_give_no_translation_instead_of_raising(data):
    assert ClaudeTranslator.read_message(data)[0] is None


@pytest.mark.parametrize("content", [[], [text_block("这款产品可以")]])
def test_a_refusal_is_no_translation_even_with_half_a_sentence(monkeypatch, content):
    """拒答前吐出的半句不是完整译文：当成译文发出去，中控看到的是被截断的意思。"""
    tr = make(monkeypatch)
    refusal = dict(message(*content, stop="refusal"),
                   stop_details={"type": "refusal", "category": "general_harms",
                                 "explanation": "x"})
    session = RecordingSession([Resp(200, refusal), Resp(200, refusal),
                                Resp(200, message(text_block("你好")))])
    attach(tr, session)
    cached = CachedTranslator(tr)

    async def ask(text):
        begin_translate_call()
        out = await cached.translate(text, "zh-CN")
        return out, translate_call_outcome()

    # 这一次调用的结局按任务记（管线据此判断「这一句」是不是拒答），类别照抄接口
    assert run(ask("hola")) == (None, ("refusal", "general_harms"))
    assert tr.refusals == 1
    # 拒答是 HTTP 200：不记 HTTP 错误、不冷却
    assert tr.last_error is None and tr.fail_streak == 0
    assert tr.cooldown_until <= time.monotonic()
    # 不进缓存：同一句再问一次
    assert run(ask("hola")) == (None, ("refusal", "general_harms"))
    assert len(session.posts) == 2 and tr.refusals == 2
    # 下一次调用不是拒答：结局清空
    assert run(ask("adiós")) == ("你好", None)


def test_a_refusal_without_a_category_keeps_it_empty():
    data = message(stop="refusal")
    assert ClaudeTranslator.read_message(data) == (None, "refusal", None)


def test_hitting_max_tokens_is_no_translation(monkeypatch, capsys):
    _tr, out, _post = sent_body(monkeypatch, response=message(thinking_block(),
                                                              text_block("半句"),
                                                              stop="max_tokens"))
    assert out is None
    assert "max_tokens" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 400：请求体配错时留下接口的原话
# ---------------------------------------------------------------------------

def test_a_400_keeps_the_apis_own_words_and_says_so_once(monkeypatch, capsys):
    tr = make(monkeypatch)
    said = {"type": "error", "error": {"type": "invalid_request_error",
                                       "message": "temperature: unsupported value"}}
    session = RecordingSession([Resp(400, said), Resp(400, said)])
    attach(tr, session)
    assert run(tr.translate("hola", "zh-CN")) is None
    assert tr.last_error == (400, "invalid_request_error: temperature: unsupported value")
    assert run(tr.translate("hola", "zh-CN")) is None
    assert len(session.posts) == 2                     # 400 不冷却
    out = capsys.readouterr().out
    assert out.count("HTTP 400") == 1 and "claude-haiku-5-5" in out
    assert "sk-ant-test-key" not in out


def test_a_402_keeps_the_apis_own_words_too(monkeypatch, capsys):
    """402 是 billing_error：说的是账务，不带密钥。原话留下，管线照抄给中控。"""
    tr = make(monkeypatch)
    said = {"type": "error", "error": {"type": "billing_error",
                                       "message": "Your credit balance is too low."}}
    session = RecordingSession([Resp(402, said)])
    attach(tr, session)
    assert run(tr.translate("hola", "zh-CN")) is None
    assert tr.last_error == (402, "billing_error: Your credit balance is too low.")
    assert tr.cooldown_until <= time.monotonic()        # 不冷却：管线说一次，中控自己定
    out = capsys.readouterr().out
    assert "HTTP 402" in out and "sk-ant-test-key" not in out


@pytest.mark.parametrize("headers,wait", [({"retry-after": "7"}, 7), ({"Retry-After": "0"}, 1),
                                          ({"retry-after": "9000"}, 120), ({}, 30),
                                          ({"retry-after": "soon"}, 30)])
def test_a_429_pauses_for_what_the_api_asks_within_limits(monkeypatch, capsys, headers, wait):
    """429 带 retry-after：按它暂停（夹在 1 到 120 秒之间），没给或读不懂就停 30 秒。
    暂停期间不发请求——管线的 429 提示和 translation_cooldown 审计要靠这个冷却才走得到。"""
    tr = make(monkeypatch)
    said = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    session = RecordingSession([Resp(429, said, headers=headers)])
    attach(tr, session)
    t0 = time.monotonic()
    assert run(tr.translate("hola", "zh-CN")) is None
    assert tr.last_error == (429, None)                 # 429 不留原话
    assert tr.cooldown_until - t0 == pytest.approx(wait, abs=0.5)
    assert run(tr.translate("otra", "zh-CN")) is None
    assert len(session.posts) == 1
    assert "HTTP 429" in capsys.readouterr().out


def test_a_401_still_keeps_no_body(monkeypatch, capsys):
    tr = make(monkeypatch)
    session = RecordingSession([Resp(401, {"error": {"type": "authentication_error",
                                                     "message": "invalid x-api-key sk-an****key"}})])
    attach(tr, session)
    assert run(tr.translate("hola", "zh-CN")) is None
    assert tr.last_error == (401, None)
    assert "sk-an" not in capsys.readouterr().out
