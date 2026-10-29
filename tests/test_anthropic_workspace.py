"""不绑工作区的 Anthropic 密钥（2026-10-29 用户在 v0.19.0 上实测）：新版 Claude Console 里建密钥时没选工作区，
每句翻译都回 HTTP 400 invalid_request_error，红色横幅把接口的英文原话照抄给了中控。

现在：「设置 > 翻译引擎」选 Claude 时可以填工作区 ID（api_keys 里的 ANTHROPIC_WORKSPACE_ID，环境变量优先），
填了每个 Anthropic 请求都带 anthropic-workspace-id 头；没填、接口又回这一种 400 时，横幅、自检、重译失败的
提示换成成对的一句，说接口说了什么、能做哪两件事。密钥不进任何输出；工作区 ID 不进审计。

真的走 HTTP：对面是本机临时端口上的假 Anthropic。请求里没有 anthropic-workspace-id 就回用户看到的那一句 400
（原话照抄），有就回一条正常的 message。不联网。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app import comment_redo, i18n, selfcheck
from app import comment_review as cr
from app import pipeline as pipeline_mod
from app import settings as settings_mod
from app import translator as T
from app.i18n import CJK, EN, ZH
from app.translator import CachedTranslator, ClaudeTranslator
from tests.test_resilience_stream import make_pipeline

KEY = "sk-ant-api03-workspace-test-key-0123456789"
WS = "wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ"
# 用户 2026-10-29 在横幅上看到的原话（只差末尾那半句，照抄）
SAID = ("This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id "
        "header with the ID of the workspace to use. Add the header, or use an API key.")


def fake_anthropic(seen, always_400=False):
    """POST /v1/messages 和 GET /v1/models/{id}：密钥不对回 401；没带工作区头（或 always_400）回那一句 400。"""
    def gate(request):
        seen.append({"method": request.method, "path": request.path, "headers": dict(request.headers)})
        if request.headers.get("x-api-key") != KEY:
            return web.json_response({"type": "error", "error": {"type": "authentication_error",
                                                                 "message": "invalid x-api-key"}}, status=401)
        if always_400 or T.WORKSPACE_HEADER not in request.headers:
            return web.json_response({"type": "error", "request_id": "req_test",
                                      "error": {"type": "invalid_request_error", "message": SAID}}, status=400)
        return None

    async def messages(request):
        return gate(request) or web.json_response({
            "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "stop_reason": "end_turn",
            "stop_details": None, "content": [{"type": "text", "text": "你好"}],
            "usage": {"input_tokens": 10, "output_tokens": 2}})

    async def model(request):
        return gate(request) or web.json_response({"type": "model", "id": request.match_info["model"]})

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    app.router.add_get("/v1/models/{model}", model)
    return app


def serve(body, always_400=False):
    """起假 Anthropic，跑 body(base, seen)，关掉。seen 是收到的每个请求（方法、路径、请求头）。"""
    seen = []

    async def go():
        server = TestServer(fake_anthropic(seen, always_400), host="127.0.0.1")
        await server.start_server()
        try:
            return await body("http://127.0.0.1:%d" % server.port, seen)
        finally:
            await server.close()

    return asyncio.run(go()), seen


def claude_at(base, **kw):
    tr = ClaudeTranslator(**kw)
    tr.URL = base + "/v1/messages"
    return tr


@pytest.fixture
def key_only(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.delenv("CLAUDE_TRANSLATE_MODEL", raising=False)


@pytest.fixture(autouse=True)
def _strict_templates(monkeypatch):
    monkeypatch.setattr(i18n, "NET", "strict")


def both(text):
    return i18n.render(text, ZH), i18n.render(text, EN)


# ---- 请求头 ----------------------------------------------------------------------------------------------------

def test_without_a_workspace_id_no_header_is_sent_and_the_400_is_recognised(key_only):
    async def body(base, seen):
        tr = claude_at(base)
        try:
            out = await tr.translate("hola amigos", "zh-CN", source="es")
            status = await tr.probe_key()
            return tr, out, status
        finally:
            await tr.close()

    (tr, out, status), seen = serve(body)
    assert out is None and status == 400
    assert [r["method"] for r in seen] == ["POST", "GET"]
    assert all(T.WORKSPACE_HEADER not in {k.lower() for k in r["headers"]} for r in seen)
    code, said = tr.last_error
    assert code == 400 and said.startswith("invalid_request_error: This API key is not scoped to a workspace")
    assert T.workspace_missing(tr, said) and T.workspace_missing(CachedTranslator(tr), said)
    assert tr.probe_workspace_missing is True


def test_a_saved_workspace_id_goes_on_every_request(key_only):
    settings_mod.save_setting("api_keys", {"ANTHROPIC_API_KEY": KEY, T.WORKSPACE_ENV: WS})

    async def body(base, seen):
        tr = claude_at(base)
        try:
            return tr, await tr.translate("hola", "zh-CN", source="es"), await tr.probe_key()
        finally:
            await tr.close()

    (tr, out, status), seen = serve(body)
    assert (out, status) == ("你好", 200)
    assert [r["headers"].get(T.WORKSPACE_HEADER) for r in seen] == [WS, WS]
    assert tr.last_error is None and tr.probe_workspace_missing is False


def test_the_environment_variable_wins_over_the_saved_workspace_id(key_only, monkeypatch):
    settings_mod.save_setting("api_keys", {T.WORKSPACE_ENV: "wrkspc_saved"})
    monkeypatch.setenv(T.WORKSPACE_ENV, " " + WS + " ")
    assert T.anthropic_workspace() == WS
    assert ClaudeTranslator.auth_headers(KEY, WS) == {"x-api-key": KEY, "anthropic-version": "2023-06-01",
                                                      T.WORKSPACE_HEADER: WS}
    assert T.WORKSPACE_HEADER not in ClaudeTranslator.auth_headers(KEY, None)


def test_a_400_that_mentions_the_header_while_a_workspace_id_was_sent_is_quoted_as_before(key_only, monkeypatch):
    """带了工作区 ID 还 400：ID 填错了还是别的什么，接口没说，就不认成「没绑工作区」，照旧照抄原话。"""
    monkeypatch.setenv(T.WORKSPACE_ENV, WS)

    async def body(base, seen):
        tr = claude_at(base)
        try:
            await tr.translate("hola", "zh-CN", source="es")
            return tr, await tr.probe_key()
        finally:
            await tr.close()

    (tr, status), seen = serve(body, always_400=True)
    assert status == 400 and seen[0]["headers"].get(T.WORKSPACE_HEADER) == WS
    assert not T.workspace_missing(tr, tr.last_error[1]) and tr.probe_workspace_missing is False
    assert T.said_for_ui(tr, tr.last_error[1]) == tr.last_error[1].rstrip(" .。")


def test_only_claude_and_only_this_error_count():
    claude = SimpleNamespace(name="claude", workspace_id=None)
    said = "invalid_request_error: " + SAID
    assert T.workspace_missing(claude, said)
    assert not T.workspace_missing(SimpleNamespace(name="openai", workspace_id=None), said)
    assert not T.workspace_missing(claude, "invalid_request_error: max_tokens: too large")
    assert not T.workspace_missing(claude, "billing_error: credit balance is too low")
    assert not T.workspace_missing(claude, None)


# ---- 弹幕留意的复核：同一个工作区 ID、同一份请求头 -------------------------------------------------------------------

def test_the_comment_reviewer_sends_the_same_header_and_is_rebuilt_when_the_id_changes(key_only, monkeypatch):
    first = cr.make_reviewer()
    assert first.workspace is None and cr.make_reviewer(existing=first) is first
    monkeypatch.setenv(T.WORKSPACE_ENV, WS)
    second = cr.make_reviewer(existing=first)
    assert second is not first and second.workspace == WS
    assert second._headers()[T.WORKSPACE_HEADER] == WS
    assert T.WORKSPACE_HEADER not in first._headers()

    async def body(base, seen):
        out = []
        for reviewer in (cr.ClaudeReviewer(KEY, url=base + "/v1/messages"),
                         cr.ClaudeReviewer(KEY, url=base + "/v1/messages", workspace=WS)):
            try:
                out.append((await reviewer.probe_key(), reviewer.probe_workspace_missing,
                            await selfcheck._comment_review_note(reviewer)))
            finally:
                await reviewer.close()
        return out

    ((bad, bad_flag, bad_note), (ok, ok_flag, ok_note)), seen = serve(body)
    assert (bad, bad_flag, ok, ok_flag) == (400, True, 200, False)
    assert [r["headers"].get(T.WORKSPACE_HEADER) for r in seen] == [None, None, WS, WS]
    zh, en = both(bad_note[0])
    assert zh == ("Claude 接口返回 HTTP 400：Anthropic 说这把密钥没有绑定工作区，请求里要带工作区 ID，只用规则")
    assert en == ("The Claude API returned HTTP 400: Anthropic says this API key isn’t scoped to a workspace, "
                  "so requests need a workspace ID. Rules only")
    fix_zh, fix_en = both(bad_note[1])
    assert "wrkspc_" in fix_zh and "「设置 > 翻译引擎」" in fix_zh and "Default" in fix_zh
    assert "wrkspc_" in fix_en and "Settings > Translation Engine" in fix_en and not CJK.search(fix_en)
    assert "key verified" in i18n.render(ok_note[0], EN)
    assert KEY not in json.dumps([zh, en, fix_zh, fix_en], ensure_ascii=False)


# ---- 启动自检的「翻译引擎」一行 -------------------------------------------------------------------------------------

def test_the_selfcheck_row_says_what_anthropic_said_and_what_to_do(key_only):
    async def body(base, seen):
        tr = claude_at(base)
        try:
            return await selfcheck._check_paid_api("claude", "ANTHROPIC_API_KEY", CachedTranslator(tr))
        finally:
            await tr.close()

    row, seen = serve(body)
    assert row["level"] == selfcheck.FAIL
    detail, detail_en = both(row["detail"])
    fix, fix_en = both(row["fix"])
    assert detail == "Claude 接口返回 HTTP 400：Anthropic 说这把密钥没有绑定工作区，请求里要带工作区 ID"
    assert detail_en == ("The Claude API returned HTTP 400: Anthropic says this API key isn’t scoped to a "
                         "workspace, so requests need a workspace ID.")
    assert fix == ("在 Claude Console 新建一把选了工作区（例如 Default）的密钥、在「设置 > 翻译引擎」里重新填写；"
                   "或者在「设置 > 翻译引擎」里填上工作区 ID（wrkspc_ 开头，Console 的 Workspaces 页面上有）")
    assert not CJK.search(detail_en + fix_en)
    assert KEY not in json.dumps(row, ensure_ascii=False) + detail_en + fix_en


# ---- 直播中的横幅、审计、终端 ----------------------------------------------------------------------------------------

class Audit:
    def __init__(self):
        self.calls = []

    def translation_engine_error(self, *a, **k):
        self.calls.append((a, k))


def failed_claude(base):
    """对着假 Anthropic 译一句、失败一次的 Claude 引擎（管线里那样包一层缓存）。"""
    async def go():
        tr = claude_at(base)
        try:
            await tr.translate("hola", "zh-CN", source="es")
        finally:
            await tr.close()
        return CachedTranslator(tr)
    return go()


def test_the_banner_explains_it_on_the_first_400_and_the_audit_keeps_the_original(
        key_only, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(T, "engine_label", lambda name: {"claude": "Claude"}.get(name, str(name)))
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)          # make_pipeline 换了设置文件，密钥还在环境变量里
    audit = p.audit = Audit()

    async def body(base, seen):
        p.translator = await failed_claude(base)
        assert p.translator.fail_streak == 1                # 只失败了一次：别的 400 要连续 3 次才说
        await p._note_engine_failure(p.translator)

    serve(body)
    shown = [m["text"] for m in server.incidents(pipeline_mod.Pipeline.ENGINE_INCIDENT) if m["level"] != "clear"]
    assert len(shown) == 1
    zh, en = both(shown[0])
    assert zh == ("Claude 返回 HTTP 400：Anthropic 说这把密钥没有绑定工作区，请求里要带工作区 ID。"
                  "字幕先显示原文（违禁词报警不受影响）。点「停止」后，可以在 Claude Console 新建一把选了工作区"
                  "（例如 Default）的密钥、在「设置 > 翻译引擎」里重新填写；或者在「设置 > 翻译引擎」里填上工作区 ID"
                  "（wrkspc_ 开头，Console 的 Workspaces 页面上有）")
    assert en == ("Claude returned HTTP 400: Anthropic says this API key isn’t scoped to a workspace, so requests "
                  "need a workspace ID. Captions show the original text (banned-term alerts aren’t affected). "
                  "After you click Stop, you can create a key with a workspace selected (for example, Default) in "
                  "the Claude Console and enter it in Settings > Translation Engine, or enter the workspace ID in "
                  "Settings > Translation Engine. It starts with wrkspc_ and is listed on the Workspaces page in "
                  "the Console.")
    assert "This API key" not in zh and "This API key" not in en       # 接口原话不再照抄进横幅
    # 审计照旧写接口原话（证据），终端也是；两处都没有密钥
    ((args, kwargs),) = audit.calls
    assert args[:2] == ("claude", 400) and kwargs["error"].startswith("invalid_request_error: This API key")
    out = capsys.readouterr().out
    assert "[警告] Claude 返回 HTTP 400" in out and "invalid_request_error" in out
    blob = json.dumps([m for m in server.messages], ensure_ascii=False, default=str) + out + repr(audit.calls)
    assert KEY not in blob and KEY[-12:] not in blob


def test_a_set_workspace_id_never_reaches_the_audit(key_only, monkeypatch, tmp_path, capsys):
    """带了工作区 ID 还 400：连续 3 次之后照旧照抄原话挂横幅；审计、终端、页面消息里都没有工作区 ID 和密钥。"""
    monkeypatch.setattr(T, "engine_label", lambda name: {"claude": "Claude"}.get(name, str(name)))
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv(T.WORKSPACE_ENV, WS)
    audit = p.audit = Audit()

    async def body(base, seen):
        tr = claude_at(base)
        p.translator = CachedTranslator(tr)
        try:
            for _ in range(pipeline_mod.Pipeline.ENGINE_ERROR_STREAK):
                await tr.translate("hola", "zh-CN", source="es")
                await p._note_engine_failure(p.translator)
        finally:
            await tr.close()
        return seen

    seen, _ = serve(body, always_400=True)
    assert all(r["headers"].get(T.WORKSPACE_HEADER) == WS for r in seen)
    shown = [m["text"] for m in server.incidents(pipeline_mod.Pipeline.ENGINE_INCIDENT) if m["level"] != "clear"]
    assert len(shown) == 1 and "This API key is not scoped" in i18n.render(shown[0], EN)
    blob = repr(audit.calls) + capsys.readouterr().out + json.dumps(server.messages, ensure_ascii=False,
                                                                    default=str)
    assert WS not in blob and KEY not in blob


# ---- 重译、弹幕重译失败的提示 -------------------------------------------------------------------------------------

def test_retranslate_failures_use_the_paired_line_instead_of_the_original(key_only, monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)

    async def body(base, seen):
        strong = claude_at(base, model=ClaudeTranslator.STRONG_MODEL, role="strong")
        strong.name = "strong"                                       # 同 create_claude_strong_translator
        try:
            await strong.translate("hola", "zh-CN", source="es")
        finally:
            await strong.close()
        p.translator = await failed_claude(base)
        return strong

    strong, _ = serve(body)
    redo = both(comment_redo.failure_text(strong, None, None, False, had=False))
    caption = both(p._retranslate_failure_text(strong, None, None, False, True))
    assert redo == ("弹幕重译没有成功：Claude Sonnet 5.5 返回 HTTP 400（Anthropic 说这把密钥没有绑定工作区，"
                    "请求里要带工作区 ID），这条弹幕仍显示原文",
                    "Couldn’t retranslate this comment: Claude Sonnet 5.5 returned HTTP 400 (Anthropic says "
                    "this API key isn’t scoped to a workspace, so requests need a workspace ID). The comment "
                    "still shows the original text.")
    assert caption[0].startswith("重译没有成功：Claude Sonnet 5.5 返回 HTTP 400（Anthropic 说这把密钥没有绑定工作区")
    assert caption[1].startswith("Couldn’t retranslate: Claude Sonnet 5.5 returned HTTP 400 (Anthropic says")
    assert not CJK.search(redo[1] + caption[1])


# ---- 设置：存、清、不像的不存 -----------------------------------------------------------------------------------------

class Engine:
    def __init__(self, name):
        self.name = name

    async def close(self):
        pass


def settings_page(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    made = []
    monkeypatch.setattr(pipeline_mod, "create_translator", lambda name: made.append(name) or Engine(name))
    return p, server, made


def engine_messages(server):
    return [m for m in server.messages if m.get("type") == "engine"]


def test_saving_a_workspace_id_stores_it_next_to_the_key_and_shows_it_in_full(monkeypatch, tmp_path):
    p, server, made = settings_page(monkeypatch, tmp_path)
    asyncio.run(p.set_engine("claude", " " + KEY + " ", " " + WS + " "))
    assert settings_mod.load_settings()["api_keys"] == {"ANTHROPIC_API_KEY": KEY, T.WORKSPACE_ENV: WS}
    assert made == ["claude"]
    info = engine_messages(server)[-1]
    assert info["workspace_id"] == WS and info["keys"]["ANTHROPIC_API_KEY"] == "…" + KEY[-4:]
    assert KEY not in json.dumps(server.messages, ensure_ascii=False, default=str) + repr(server.config)

    asyncio.run(p.set_engine("claude", None, ""))                    # 清空：不再带
    assert settings_mod.load_settings()["api_keys"] == {"ANTHROPIC_API_KEY": KEY}
    assert engine_messages(server)[-1]["workspace_id"] == ""

    settings_mod.save_setting("api_keys", {"ANTHROPIC_API_KEY": KEY, T.WORKSPACE_ENV: WS})
    asyncio.run(p.set_engine("deepl", None, ""))                     # 别的引擎：页面发了也不动它
    asyncio.run(p.set_engine("claude"))                              # 没发这个字段：不动
    assert settings_mod.load_settings()["api_keys"][T.WORKSPACE_ENV] == WS


@pytest.mark.parametrize("bad", ["Default", "wrkspc_", "wrkspc_01 02", "https://platform.claude.com/settings/workspaces",
                                 "wrkspc_01\nX-Evil: 1"])
def test_something_that_is_not_a_workspace_id_saves_nothing(monkeypatch, tmp_path, bad):
    p, server, made = settings_page(monkeypatch, tmp_path)
    asyncio.run(p.set_engine("claude", KEY, bad))
    assert "api_keys" not in settings_mod.load_settings() and made == []     # 密钥也没存、引擎也没换
    (notice,) = [m["text"] for m in server.messages if m.get("type") == "notice"]
    zh, en = both(notice)
    assert zh == ("工作区 ID 是 wrkspc_ 开头的一串字母和数字（Claude Console 的 Workspaces 页面上有），"
                  "这次没有保存，请检查后再点「保存」")
    assert en == ("A workspace ID starts with wrkspc_ followed by letters and numbers. It’s listed on the "
                  "Workspaces page in the Claude Console. Nothing was saved. Check it and click Save again.")
