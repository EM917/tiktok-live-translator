"""审计日志必须能回答「这一场、这一条，到底是哪个引擎翻的」。

起因是 2026-08-26 的排查：settings 里写着 deepl，实际整场跑的是本地 1.8B，
而日志里没有任何一个字段能说明这一点，只能靠延迟指纹（截距 21ms、19ms/字）
反推。做引擎对比实验之前，这个问题必须从「猜」变成「读一行」。
"""
import asyncio
import json
import time

from app.audit import AuditLog
from app.telemetry import Telemetry


def _rows(path):
    return [json.loads(line) for line in open(path, encoding="utf-8")]


def test_session_start_carries_runtime_provenance(tmp_path):
    log = AuditLog(room_url="https://www.tiktok.com/@real/live", log_dir=tmp_path,
                   extra={"app_version": "0.14.2",
                          "streamer": "real",
                          "source_requested": "es", "source_active": "es",
                          "translator_requested": "deepl",
                          "translator_active": "hymt2",
                          "profile_hash": None})
    log.close()
    start = _rows(log.path)[0]
    # requested 与 active 并排——0826 正是这两者不一致而无从发现
    assert start["translator_requested"] == "deepl"
    assert start["translator_active"] == "hymt2"
    assert start["source_active"] == "es"
    assert start["app_version"] == "0.14.2"
    assert start["profile_hash"] is None          # profile 未落地时的占位
    assert start["code_commit"]                   # 原有字段不受 extra 影响


def test_extra_cannot_overwrite_core_fields(tmp_path):
    """extra 撞上骨架字段名时骨架赢——审计核心不能被手滑的键名静默改写。"""
    log = AuditLog(room_url="https://www.tiktok.com/@real/live", log_dir=tmp_path,
                   extra={"room_url": "https://evil/live", "code_commit": "fake"})
    log.close()
    start = _rows(log.path)[0]
    assert start["room_url"] == "https://www.tiktok.com/@real/live"
    assert start["code_commit"] != "fake"


def test_translation_records_the_engine(tmp_path):
    log = AuditLog(room_url="https://www.tiktok.com/@real/live", log_dir=tmp_path)
    log.translation(7, "你好", 320.0, True, engine="deepl")
    log.translation(8, None, 10000.0, False)      # 旧调用形态也不能炸
    log.close()
    rows = [r for r in _rows(log.path) if r["type"] == "translation"]
    assert rows[0]["engine"] == "deepl"
    assert rows[1]["engine"] is None


def test_fast_translation_path_passes_its_engine(tmp_path):
    """管线的快译路径要把**当时用的**引擎写进日志。"""
    from app.pipeline import Pipeline

    class FakeServer:
        config = {}

        async def broadcast(self, msg):
            pass

    class Tr:
        name = "deepl"

        async def translate(self, *a, **k):
            return "你好"

    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p.glossary = None
    p.telemetry = Telemetry()
    p.translator = Tr()
    p._quality = {}
    p.audit = AuditLog(room_url="https://www.tiktok.com/@real/live",
                       log_dir=tmp_path)
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        loop.run_until_complete(p._translate_and_update(
            {"id": 1, "text": "hola", "lang": "es", "target": "zh-CN",
             "audio_end_ts": time.time()}))
    finally:
        loop.close()
    p.audit.close()
    row = [r for r in _rows(p.audit.path) if r["type"] == "translation"][0]
    assert row["engine"] == "deepl"
    assert row["ok"] is True


# ---- Claude 请求超时、连不上、回 5xx：translation_engine_error 带 kind（专家审查 B7）----
#
# 以前 translator 里 except Exception: return None，last_error 不动、不打印；管线的 _note_engine_failure 见
# last_error 为空直接返回，5xx 又不在任何一条规则里——审计里只有 ok=false。现网 10-01 起 Claude 字幕失败 19 条，
# 8 条约 10 秒（超时），一条都没有类别。现在记一个类别，只记类别，不猜原因。

import json as _json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from app.translator import CachedTranslator, ClaudeTranslator  # noqa: E402

ROOM = "https://www.tiktok.com/@ficticia_motor/live"


class _Resp:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload
        self.headers = {}

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return _json.dumps(self._payload) if self._payload is not None else ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Raises:
    """aiohttp 在进 async with 的那一刻抛（连接、发请求、等回应头都在这一步）。"""

    def __init__(self, exc):
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.posts = 0

    def post(self, url, data=None, headers=None, **kw):
        self.posts += 1
        return self.outcomes.pop(0)


def _ok(text="你好"):
    return _Resp(200, {"type": "message", "role": "assistant", "model": "claude-haiku-5-5",
                       "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                       "stop_details": None})


def _connector_error():
    import aiohttp

    key = SimpleNamespace(host="api.example.invalid", port=443, ssl=True)
    return aiohttp.ClientConnectorError(key, OSError(61, "Connection refused"))


def _disconnected():
    import aiohttp

    return aiohttp.ServerDisconnectedError()


class _Server:
    def __init__(self):
        self.config = {}
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(msg)


def _claude_pipeline(monkeypatch, tmp_path, outcomes):
    from app.pipeline import Pipeline

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.delenv("CLAUDE_TRANSLATE_MODEL", raising=False)
    inner = ClaudeTranslator()
    session = _Session(outcomes)

    async def get_session():
        return session

    inner.session = get_session
    p = Pipeline.__new__(Pipeline)
    p.server = _Server()
    p.glossary = None
    p.telemetry = Telemetry()
    p.translator = CachedTranslator(inner)
    p._quality = {}
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path)
    return p, session


def _translate(p, *texts):
    async def scenario():
        for n, text in enumerate(texts, 1):
            await p._translate_and_update({"id": n, "text": text, "lang": "es", "target": "zh-CN",
                                           "audio_end_ts": time.time()})
    asyncio.run(scenario())
    p.audit.close(reason="user_stop")
    return [r for r in _rows(p.audit.path) if r["type"] == "translation_engine_error"]


@pytest.mark.parametrize("outcome, kind, status, error", [
    (lambda: _Raises(asyncio.TimeoutError()), "timeout", None, "TimeoutError"),
    (lambda: _Raises(_connector_error()), "connect", None, "ClientConnectorError"),
    (lambda: _Raises(_disconnected()), "connect", None, "ServerDisconnectedError"),
    (lambda: _Resp(503, {"error": {"type": "api_error", "message": "x"}}), "http", 503, None),
    (lambda: _Resp(529, {"error": {"type": "overloaded_error", "message": "x"}}), "http", 529, None),
    (lambda: _Raises(ValueError("odd")), "other", None, "ValueError"),
])
def test_each_failure_kind_leaves_one_record(monkeypatch, tmp_path, capsys, outcome, kind, status, error):
    p, _session = _claude_pipeline(monkeypatch, tmp_path, [outcome(), outcome()])
    rows = _translate(p, "hola", "adios")
    assert len(rows) == 1                               # 同一种这一场只记一次
    (row,) = rows
    assert row == {"type": "translation_engine_error", "at": row["at"], "engine": "claude",
                   "model": "claude-haiku-5-5", "status": status, "error": error, "kind": kind}
    out = capsys.readouterr().out
    assert out.count("这一句显示原文；同一种情况这一场不再打印") == 1
    assert "odd" not in out and "Connection refused" not in out    # 只有类型名，不带异常原文


def test_different_kinds_each_get_a_record(monkeypatch, tmp_path):
    p, _session = _claude_pipeline(monkeypatch, tmp_path, [
        _Raises(asyncio.TimeoutError()), _Resp(503, {}), _Raises(asyncio.TimeoutError()),
        _Raises(_connector_error())])
    rows = _translate(p, "a", "b", "c", "d")
    assert [r["kind"] for r in rows] == ["timeout", "http", "connect"]


def test_a_success_in_between_lets_the_same_kind_be_recorded_again(monkeypatch, tmp_path):
    p, _session = _claude_pipeline(monkeypatch, tmp_path, [
        _Raises(asyncio.TimeoutError()), _ok(), _Raises(asyncio.TimeoutError())])
    rows = _translate(p, "a", "b", "c")
    assert [r["kind"] for r in rows] == ["timeout", "timeout"]


def test_a_cache_hit_is_not_a_success(monkeypatch, tmp_path):
    """缓存命中不发请求：引擎还记着上一次超时，同一种不重记（和这个类型原来的规则一致）。"""
    p, session = _claude_pipeline(monkeypatch, tmp_path, [
        _ok("你好"), _Raises(asyncio.TimeoutError()), _Raises(asyncio.TimeoutError())])
    rows = _translate(p, "hola", "adios", "hola", "otra")
    assert session.posts == 3                           # 第三句是缓存命中
    assert [r["kind"] for r in rows] == ["timeout"]


def test_the_failure_is_cleared_by_a_200_and_by_a_4xx(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    tr = ClaudeTranslator()
    session = _Session([_Raises(asyncio.TimeoutError()), _ok(), _Resp(503, {}), _Resp(401, {})])

    async def get_session():
        return session

    tr.session = get_session

    async def scenario():
        await tr.translate("a", "zh-CN")
        assert tr.last_failure == {"kind": "timeout", "status": None, "error": "TimeoutError"}
        await tr.translate("b", "zh-CN")
        assert tr.last_failure is None
        await tr.translate("c", "zh-CN")
        assert tr.last_failure == {"kind": "http", "status": 503, "error": None}
        await tr.translate("d", "zh-CN")
        assert tr.last_failure is None and tr.last_error == (401, None)

    asyncio.run(scenario())
    assert CachedTranslator(tr).last_failure is None


@pytest.mark.parametrize("status", [401, 403])
def test_existing_rejection_records_are_byte_for_byte_unchanged(monkeypatch, tmp_path, status):
    """已有的 translation_engine_error（401/403）不带 kind，逐字节和以前一样。"""
    p, _session = _claude_pipeline(monkeypatch, tmp_path, [
        _Resp(status, {"error": {"type": "authentication_error", "message": "sk-an****key"}})])
    monkeypatch.setattr(type(p), "_rejection_fallback", _no_fallback, raising=False)

    async def no_incident(*a, **k):
        return None

    p._incident = no_incident
    (row,) = _translate(p, "hola")
    raw = [line + b"\n" for line in p.audit.path.read_bytes().split(b"\n") if b"engine_error" in line]
    old = (_json.dumps({"type": "translation_engine_error", "at": row["at"], "engine": "claude",
                        "model": "claude-haiku-5-5", "status": status, "error": None},
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    assert raw == [old]


async def _no_fallback(self, old, what, status):
    return None
