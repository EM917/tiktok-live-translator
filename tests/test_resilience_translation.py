"""韧性补丁（翻译组）：翻译引擎、本地模型、弹幕翻译在上游出问题时的检测、提示、恢复和证据。

每一条都对应一个确认过的缺口：失败时只回 None、原因当场丢掉；自检报绿；直播中下载
几个 GB 的模型；报警翻译一直叫一个被删掉的模型；弹幕把 Google 免费接口打到 429……
测试一律不联网、不起真实子进程、不等真实的 120 秒冷却。
"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from app import comments as comments_mod
from app import localmodel, selfcheck, translator
from app.audit import AuditLog
from app.comments import CommentTranslator
from app.pipeline import AUDIO_BACKLOG_WARN_SEC, Pipeline
from app.telemetry import Telemetry
from app.translator import (HYMT2_LARGE, HYMT2_SMALL, CachedTranslator,
                            ClaudeTranslator, DeepLTranslator, GoogleWebTranslator,
                            OllamaGemmaTranslator, OllamaHyMT2Translator, OpenAITranslator)
from tests.helpers import run

GUESSED_CAUSES = ("年龄", "限流", "封禁", "多半")


def plain(text):
    """给中控看的文字只写观察和能做的事，不贴原因标签（CLAUDE.md 第八条）。"""
    for word in GUESSED_CAUSES:
        assert word not in text, text
    return text


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------

class Server:
    def __init__(self):
        self.config = {}
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(msg)

    async def status(self, state, detail="", command=None):
        self.sent.append({"type": "status", "state": state, "detail": detail})

    def of(self, kind):
        return [m for m in self.sent if m.get("type") == kind]


class FakeResp:
    def __init__(self, status, payload=None, text=None):
        self.status = status
        self._payload = payload
        self._text = text

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


class FakeSession:
    """按顺序回应；记下每次请求的地址。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, url):
        self.calls.append(url)
        return self.responses.pop(0)

    def post(self, url, data=None, headers=None, **kw):
        return self._next(url)

    def get(self, url, params=None, headers=None, **kw):
        return self._next(url)


def attach(engine, session):
    async def get_session():
        return session

    engine.session = get_session


class Fast:
    """常驻快速引擎的替身。"""

    def __init__(self, name, model=None, out="译文"):
        self.name = name
        self.model = model
        self.out = out
        self.calls = 0
        self.closed = False

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls += 1
        return self.out

    async def close(self):
        self.closed = True


class Strong:
    def __init__(self, result=None, raises=False, model=HYMT2_LARGE):
        self.result = result
        self.raises = raises
        self.model = model
        self.name = "strong"
        self.last_error = None
        self.calls = 0
        self.closed = False

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls += 1
        if self.raises:
            raise RuntimeError("超时")
        return self.result

    async def close(self):
        self.closed = True


class ScriptedEngine:
    """按脚本回应的引擎，和真实引擎一样维护 last_error / fail_streak / 冷却。
    脚本项：("ok", 译文) 或 ("http", 状态码, 原话, 冷却秒数)。"""

    def __init__(self, name, model=None, script=()):
        self.name = name
        self.model = model
        self.script = list(script)
        self.last_error = None
        self.fail_streak = 0
        self._cooldown_until = 0.0
        self.calls = 0
        self.closed = False

    @property
    def cooldown_until(self):
        return self._cooldown_until

    async def translate(self, text, target, source="auto", glossary=None):
        if time.monotonic() < self._cooldown_until:
            return None
        self.calls += 1
        step = self.script.pop(0) if self.script else ("ok", "译文")
        if step[0] == "ok":
            self.last_error, self.fail_streak = None, 0
            return step[1]
        _kind, status, said, cooldown = (tuple(step) + (None, 0))[:4]
        self.last_error = (status, said)
        self.fail_streak += 1
        if cooldown:
            self._cooldown_until = time.monotonic() + cooldown
        return None

    async def close(self):
        self.closed = True


async def noop(*a, **k):
    return None


@pytest.fixture
def settings_file(monkeypatch, tmp_path):
    from app import settings
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_FILE", path)
    return path


def bare_pipeline(tmp_path, translator_obj=None, engine="hymt2", audit=True):
    p = Pipeline.__new__(Pipeline)
    p.server = Server()
    p.args = SimpleNamespace(translator=engine, translator_note=None, target="zh-CN")
    p.translator = translator_obj
    p.target = "zh-CN"
    p.glossary = None
    p.telemetry = Telemetry()
    p._quality = {}
    p._bg_tasks = set()
    p._stream_task = None
    p._stats_task = None
    p._strong = None
    p._strong_missing = False
    p.audit = AuditLog(room_url="https://www.tiktok.com/@x/live",
                       log_dir=tmp_path) if audit else None
    p.run_selfcheck = noop
    return p


def full_pipeline(monkeypatch, tmp_path, translator_obj=None):
    from app import pipeline as pipeline_mod
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5,
        context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None,
        comments=False)
    p = Pipeline(args, Server())
    p.translator = translator_obj
    return p


def rows(audit, kind=None):
    with open(audit.path, encoding="utf-8") as fh:
        out = [json.loads(line) for line in fh if line.strip()]
    return [r for r in out if kind is None or r["type"] == kind]


def job(seq, text="hola amigos"):
    return {"id": seq, "text": text, "lang": "es", "target": "zh-CN",
            "audio_end_ts": time.time()}


async def drain(p):
    for _ in range(50):
        pending = [t for t in list(p._bg_tasks) if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# 17. Ollama 在跑但生成失败：原因要留下、要说出来、自检不能报绿
# ---------------------------------------------------------------------------

def test_hymt2_keeps_the_status_and_ollamas_own_words(monkeypatch):
    tr = OllamaHyMT2Translator()
    monkeypatch.setattr(translator, "_RAW_MODE", {tr.model: False})
    said = "model 'hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M' not found, try pulling it first"
    attach(tr, FakeSession([FakeResp(404, {"error": said}), FakeResp(404, {"error": said}),
                            FakeResp(200, {"response": "你好"})]))
    assert run(tr.translate("hola", "zh-CN")) is None
    assert tr.last_error == (404, said) and tr.fail_streak == 1
    assert run(tr.translate("hola", "zh-CN")) is None
    assert tr.fail_streak == 2
    assert run(tr.translate("hola", "zh-CN")) == "你好"
    assert tr.last_error is None and tr.fail_streak == 0


def test_gemma_error_text_loses_signed_url_queries():
    tr = OllamaGemmaTranslator()
    said = "llama runner terminated, see https://example.invalid/log?sig=SECRET123 for details"
    attach(tr, FakeSession([FakeResp(500, {"error": said})]))
    assert run(tr.translate("hola", "zh-CN")) is None
    status, text = tr.last_error
    assert status == 500
    assert "SECRET123" not in text and "https://example.invalid/log" in text


def test_the_cache_wrapper_passes_engine_health_through():
    inner = OllamaGemmaTranslator()
    inner.last_error, inner.fail_streak, inner._cooldown_until = (500, "x"), 2, 123.0
    wrapped = CachedTranslator(inner)
    assert (wrapped.last_error, wrapped.fail_streak, wrapped.cooldown_until) == \
        ((500, "x"), 2, 123.0)


def test_model_listed_matches_the_name_generation_needs():
    names = ["hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M", "translategemma:latest"]
    assert translator.model_listed("hf.co/tencent/hy-mt2-1.8b-gguf:q4_k_m", names)
    assert translator.model_listed("translategemma", names)
    assert not translator.model_listed(HYMT2_LARGE, names)
    assert not translator.model_listed("translategemma:4b", names)
    assert not translator.model_listed("hf.co/tencent/Hy-MT2-1.8B-GGUF:Q8_0", names)


def test_selfcheck_fails_when_ollama_runs_without_the_configured_model(monkeypatch):
    monkeypatch.setattr(selfcheck, "_ollama_tags", lambda: [HYMT2_SMALL])
    seven = SimpleNamespace(name="hymt2-7b", inner=SimpleNamespace(model=HYMT2_LARGE))
    c = run(selfcheck.check_translator(SimpleNamespace(translator="hymt2-7b"), seven))
    assert c["level"] == "fail"
    assert HYMT2_LARGE in c["detail"] and "违禁词报警不受影响" in c["detail"]
    plain(c["detail"] + c["fix"])

    gemma = SimpleNamespace(name="gemma", inner=SimpleNamespace(model="translategemma:4b"))
    assert run(selfcheck.check_translator(SimpleNamespace(translator="gemma"),
                                          gemma))["level"] == "fail"
    monkeypatch.setattr(selfcheck, "_ollama_tags", lambda: ["translategemma:4b"])
    assert run(selfcheck.check_translator(SimpleNamespace(translator="gemma"),
                                          gemma))["level"] == "ok"


@pytest.mark.parametrize("engine,env,listed,wanted", [
    ("gemma", None, ["translategemma:12b"], "translategemma:4b"),
    ("gemma", "translategemma:12b", ["translategemma:4b"], "translategemma:12b"),
    ("hymt2", None, ["hf.co/tencent/Hy-MT2-1.8B-GGUF:Q8_0"], HYMT2_SMALL),
    ("hymt2-7b", None, [HYMT2_SMALL, "hf.co/tencent/Hy-MT2-7B-GGUF:Q8_0"], HYMT2_LARGE),
])
def test_another_tag_of_the_model_does_not_count_as_the_model(
        monkeypatch, tmp_path, settings_file, engine, env, listed, wanted):
    """自检、备模型、换引擎对「本机有没有这个模型」必须给同一个答案：生成时用的那个名字。
    以前后两处按子串认：自检红着说会自动下载，实际一个字节都不下，换引擎也照换。"""
    monkeypatch.delenv("OLLAMA_HYMT2_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_TRANSLATE_MODEL", raising=False)
    if env:
        monkeypatch.setenv("OLLAMA_TRANSLATE_MODEL", env)
    monkeypatch.setattr(translator, "_ollama_models_or_none", lambda: list(listed))
    pulls, built = [], []

    async def running(timeout=2):
        return True

    async def pull(model, on_progress=None):
        pulls.append(model)
        return True, None

    monkeypatch.setattr(localmodel, "is_running", running)
    monkeypatch.setattr(localmodel, "pull", pull)
    monkeypatch.setattr("app.pipeline.create_translator", lambda name: built.append(name))

    inner = OllamaGemmaTranslator() if engine == "gemma" else OllamaHyMT2Translator()
    if engine == "hymt2-7b":
        inner.model, inner.name = HYMT2_LARGE, "hymt2-7b"
    assert inner.model == wanted
    engine_obj = CachedTranslator(inner)
    check = run(selfcheck.check_translator(SimpleNamespace(translator=engine), engine_obj))
    assert check["level"] == "fail" and wanted in check["detail"]

    async def idle():
        p = bare_pipeline(tmp_path, engine_obj, engine=engine, audit=False)
        await p.ensure_local_translator()

    run(idle())
    assert pulls == [wanted]                           # 自检说会下载的，这里真的下载

    current = Fast("google")

    async def live():
        p = bare_pipeline(tmp_path, current, engine="google", audit=False)
        p._stream_task = asyncio.get_running_loop().create_future()      # 直播中
        await p.set_engine(engine)
        p._stream_task.cancel()
        return p

    p = run(live())
    assert p.translator is current and built == []   # 模型不在：本场不换
    assert p._engine_pending == engine and p._pull_deferred == wanted


def test_picking_a_missing_local_model_mid_live_keeps_the_current_engine(
        monkeypatch, tmp_path, settings_file):
    from app.settings import load_settings

    current = Fast("hymt2", model=HYMT2_SMALL)
    built = []
    monkeypatch.setattr("app.pipeline.create_translator", lambda name: built.append(name))
    monkeypatch.setattr(translator, "_ollama_models_or_none", lambda: [HYMT2_SMALL])

    async def scenario():
        p = bare_pipeline(tmp_path, current, engine="hymt2")
        p._stream_task = asyncio.get_running_loop().create_future()      # 直播中
        await p.set_engine("hymt2-7b")
        p._stream_task.cancel()
        return p

    p = run(scenario())
    assert p.translator is current and built == [] and not current.closed
    assert p.args.translator == "hymt2-7b" and load_settings()["translator"] == "hymt2-7b"
    assert p._engine_pending == "hymt2-7b" and p._pull_deferred == HYMT2_LARGE
    notices = p.server.of("notice")
    assert len(notices) == 1
    text = plain(notices[0]["text"])
    assert "Hy-MT2 7B" in text and "本地 Hy-MT2 1.8B" in text and "停止后自动下载" in text
    assert p.args.translator_note == text
    assert [r["state"] for r in rows(p.audit, "model_pull")] == ["deferred"]


def test_picking_a_missing_local_model_while_idle_downloads_then_switches(
        monkeypatch, tmp_path, settings_file):
    state = {"large": False, "pulls": []}
    monkeypatch.setattr(translator, "_ollama_models_or_none",
                        lambda: [HYMT2_SMALL] + ([HYMT2_LARGE] if state["large"] else []))

    async def running(timeout=2):
        return True

    async def pull(model, on_progress=None):
        state["pulls"].append(model)
        state["large"] = True
        return True, None

    monkeypatch.setattr(localmodel, "is_running", running)
    monkeypatch.setattr(localmodel, "pull", pull)
    old, new = Fast("google"), Fast("hymt2-7b", model=HYMT2_LARGE)
    monkeypatch.setattr("app.pipeline.create_translator", lambda name: new)

    async def scenario():
        p = bare_pipeline(tmp_path, old, engine="google", audit=False)
        await p.set_engine("hymt2-7b")
        assert p.translator is old                     # 模型没下好之前不换
        await drain(p)
        return p

    p = run(scenario())
    assert state["pulls"] == [HYMT2_LARGE]
    assert p.translator is new and old.closed and p._engine_pending is None
    assert any("本地翻译已就绪" in m["detail"] for m in p.server.of("status"))


def test_ollama_errors_in_a_row_are_reported_once_until_a_success(monkeypatch, tmp_path):
    said = "model 'hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M' not found"
    engine = ScriptedEngine("hymt2", model=HYMT2_SMALL,
                            script=[("http", 404, said)] * 4 + [("ok", "你好")]
                            + [("http", 500, "llama runner process has terminated")] * 3)
    provisioned = []

    async def provision():
        provisioned.append(1)

    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="hymt2")
        p._heal_local_engine = noop
        p._provision_then_check = provision
        for seq in range(1, 9):
            await p._translate_and_update(job(seq, "frase numero %d" % seq))
            await asyncio.sleep(0)                     # 让后台任务跑一轮
        await drain(p)
        return p

    p = run(scenario())
    incidents = p.server.of("incident")
    assert [m["level"] for m in incidents] == ["warn", "clear", "warn"]
    assert incidents[0]["id"] == Pipeline.ENGINE_INCIDENT
    assert "HTTP 404" in incidents[0]["text"] and said in incidents[0]["text"]
    assert "HTTP 500" in incidents[2]["text"]
    plain(incidents[0]["text"])
    errors = rows(p.audit, "translation_engine_error")
    assert [(r["engine"], r["model"], r["status"], r["error"]) for r in errors] == [
        ("hymt2", HYMT2_SMALL, 404, said),
        ("hymt2", HYMT2_SMALL, 500, "llama runner process has terminated")]
    assert p.translator is engine                      # 不自动换引擎，更不换到远程
    assert provisioned == [1, 1]                       # 模型不在了会安排停止后重新下载


# ---------------------------------------------------------------------------
# 18. 报警上下文翻译：强模型没译出来要换常驻引擎再译，并记审计
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raises", [False, True])
def test_alert_translation_falls_back_to_the_resident_engine(tmp_path, raises):
    strong = Strong(result=None, raises=raises)
    if not raises:
        strong.last_error = (404, "model not found")
    fast = Fast("hymt2", model=HYMT2_SMALL, out="这句话宣称能治癌症")

    async def scenario():
        p = bare_pipeline(tmp_path, fast)
        p._strong = strong
        await p._translate_alert([5, 6], "esto cura el cancer", "es")
        await drain(p)
        return p

    p = run(scenario())
    updates = p.server.of("alert_update")
    assert [u["alert_id"] for u in updates] == [5, 6]
    assert all(u["context_zh"] == "这句话宣称能治癌症" and not u["failed"] for u in updates)
    assert strong.calls == 1 and fast.calls == 1
    assert p._strong is None                           # 下一条报警重新探测
    assert strong.closed is True                       # 丢下的实例没人在用：关掉连接
    (record,) = rows(p.audit, "alert_translation")
    assert record["alert_ids"] == [5, 6] and record["ok"] is True
    assert record["fallback"] is True and record["model"] == HYMT2_SMALL
    assert record["error"] == (None if raises else "HTTP 404：model not found")
    assert not rows(p.audit, "translation_strong")     # 报警编号不能冒充字幕 seq


def test_after_a_strong_failure_the_next_alert_probes_again_off_the_loop(
        monkeypatch, tmp_path):
    probes = []

    def create_strong():
        probes.append(threading.get_ident())
        return Strong(result="重新探测到的强模型译文", model="fresh-7b")

    monkeypatch.setattr(translator, "create_strong_translator", create_strong)

    async def scenario():
        p = bare_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL, out="快译"))
        p._strong = Strong(result=None)
        await p._translate_alert([1], "esto cura el cancer", "es")
        await p._translate_alert([2], "esto cura el cancer", "es")
        return p, threading.get_ident()

    p, loop_thread = run(scenario())
    assert len(probes) == 1 and probes[0] != loop_thread
    assert p.server.of("alert_update")[-1]["context_zh"] == "重新探测到的强模型译文"


def test_each_session_starts_without_the_previous_strong_model(monkeypatch, tmp_path):
    p = full_pipeline(monkeypatch, tmp_path)
    p._provision_then_check = noop
    old = p._strong = Strong(result="上一场的")

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@x/live")
        assert p._strong is None and p._strong_missing is False
        assert await wait_for(lambda: old.closed)     # 没人在用：旧实例的连接关掉
        await p._end_session()

    run(scenario())


def test_a_dropped_strong_model_stays_open_until_its_last_user_is_done(tmp_path):
    """报警翻译失败清掉 self._strong 时，重译可能正拿着同一个实例等译文：马上关会把它
    半路掐断，还会记成一次「强模型失败」；等它用完再关。"""
    async def scenario():
        gate = asyncio.Event()

        class SharedStrong(Strong):
            async def translate(self, text, target, source="auto", glossary=None):
                self.calls += 1
                if self.calls == 1:                    # 重译：等在半路
                    await gate.wait()
                    return "强译"
                return None                            # 报警：强模型没给出译文

        strong = SharedStrong()
        p = bare_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL, out="快译"))
        p._strong = strong
        p._recent = {7: job(7)}
        p._strong_inflight = set()
        p._publish_translation = noop
        redo = asyncio.ensure_future(p.retranslate(7))
        assert await wait_for(lambda: strong.calls == 1)
        await p._translate_alert([1], "esto cura el cancer", "es")
        await drain(p)
        assert p._strong is None and strong.closed is False
        gate.set()
        await redo
        await drain(p)
        return p, strong

    p, strong = run(scenario())
    assert strong.closed is True
    assert [r["ok"] for r in rows(p.audit, "translation_strong")] == [True]


def test_closing_a_dropped_strong_model_never_costs_the_alert_its_reply():
    """关连接是顺手的清理，跑在报警翻译的 finally 里：它出了错（这里是半成品实例没有
    _bg_tasks），报警框也必须收到回话，否则永远停在「翻译中…」。"""
    p = Pipeline.__new__(Pipeline)
    p.server = Server()
    p.glossary = None
    p.target = "zh-CN"
    p.translator = None
    p._strong = Strong(result=None)
    run(p._translate_alert([7], "esto cura el cancer", "es"))
    (update,) = p.server.of("alert_update")
    assert update["failed"] is True and update["why"] == "模型没有返回译文"
    assert p._strong is None


def test_alert_translation_that_fails_everywhere_still_answers_the_page(tmp_path):
    async def scenario():
        p = bare_pipeline(tmp_path, None)
        p._strong = Strong(result=None)
        await p._translate_alert([9], "esto cura el cancer", "es")
        return p

    p = run(scenario())
    (update,) = p.server.of("alert_update")
    assert update["failed"] is True and update["why"] == "模型没有返回译文"
    (record,) = rows(p.audit, "alert_translation")
    assert record["ok"] is False and record["fallback"] is False


# ---------------------------------------------------------------------------
# 19. 弹幕只用常驻本地模型翻；429 冷却要说一次、记一次
# ---------------------------------------------------------------------------

def make_comments(holder, session=None):
    sent = []

    async def broadcast(msg):
        sent.append(msg)

    ct = CommentTranslator(broadcast=broadcast, translator=lambda: holder["tr"],
                           target=lambda: "zh-CN", glossary=lambda: None, session=session)
    return ct, sent


async def wait_for(cond, limit=200):
    for _ in range(limit):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


@pytest.mark.parametrize("name", ["google", "deepl", "claude", "openai", "hymt2-7b"])
def test_comments_are_never_sent_to_a_non_resident_engine(name):
    engine = Fast(name)
    holder = {"tr": engine, "session": object()}
    ct, sent = make_comments(holder, session=lambda: holder["session"])

    async def scenario():
        await ct.accept({"items": [{"id": "a", "user": "u", "text": "hola amigos"},
                                   {"id": "b", "user": "u", "text": "que precio tiene"}]})
        holder["session"] = object()                   # 下一场
        await ct.accept({"items": [{"id": "c", "user": "u", "text": "buenas noches"}]})
        await asyncio.sleep(0)

    run(scenario())
    assert engine.calls == 0
    assert {m["id"]: m["state"] for m in sent if m["type"] == "comment"} == {
        "a": "skipped", "b": "skipped", "c": "skipped"}
    hints = [m["text"] for m in sent if m["type"] == "notice"]
    expected = comments_mod.original_only_hint(engine)
    assert hints == [expected] * 2                           # 每场一次
    if name == "hymt2-7b":                                   # 7B 是本地模型，不能说成不是
        assert expected == comments_mod.LARGE_MODEL_HINT
        assert "不是本地模型" not in expected and "7B" in expected
    else:
        assert expected == comments_mod.REMOTE_ENGINE_HINT
    plain(expected)


@pytest.mark.parametrize("name", ["hymt2", "gemma"])
def test_comments_are_translated_by_the_resident_local_model(name):
    engine = Fast(name, out="你好朋友")
    ct, sent = make_comments({"tr": engine})
    ct.BATCH_WINDOW_SEC = 0.01

    async def scenario():
        await ct.accept({"items": [{"id": "a", "user": "u", "text": "hola amigos"}]})
        assert await wait_for(lambda: any(m["type"] == "comment_update" for m in sent))

    run(scenario())
    assert engine.calls == 1
    assert [m["state"] for m in sent if m["type"] == "comment_update"] == ["ok"]
    assert not [m for m in sent if m["type"] == "notice"]


def test_a_batch_queued_before_a_switch_to_a_remote_engine_is_not_sent():
    local, remote = Fast("hymt2"), Fast("google")
    holder = {"tr": local}
    ct, sent = make_comments(holder)
    ct.BATCH_WINDOW_SEC = 0.05

    async def scenario():
        await ct.accept({"items": [{"id": "a", "user": "u", "text": "hola amigos"}]})
        holder["tr"] = remote                          # 攒批窗口内换成了远程引擎
        assert await wait_for(lambda: any(m["type"] == "comment_update" for m in sent))

    run(scenario())
    assert local.calls == 0 and remote.calls == 0
    assert [m["state"] for m in sent if m["type"] == "comment_update"] == ["skipped"]


def test_the_comment_hint_is_repeated_in_a_session_only_when_its_wording_changes():
    holder = {"tr": None}
    ct, sent = make_comments(holder, session=lambda: "同一场")

    async def scenario():
        for cid, name in (("a", "google"), ("b", "deepl"), ("c", "hymt2-7b"),
                          ("d", "hymt2-7b")):
            holder["tr"] = Fast(name)
            await ct.accept({"items": [{"id": cid, "user": "u",
                                        "text": "hola amigos " + cid}]})

    run(scenario())
    assert [m["text"] for m in sent if m["type"] == "notice"] == [
        comments_mod.REMOTE_ENGINE_HINT, comments_mod.LARGE_MODEL_HINT]


def test_the_pipeline_scopes_the_comment_hint_to_the_session(monkeypatch, tmp_path):
    p = full_pipeline(monkeypatch, tmp_path, Fast("google"))

    async def scenario():
        p.audit = object()
        for cid in ("a", "b"):
            await p.comments.accept({"items": [{"id": cid, "user": "u",
                                                "text": "hola amigos " + cid}]})
        p.audit = object()                             # 新的一场
        await p.comments.accept({"items": [{"id": "c", "user": "u", "text": "que tal"}]})

    run(scenario())
    assert len([m for m in p.server.of("notice")
                if m["text"] == comments_mod.REMOTE_ENGINE_HINT]) == 2


def test_google_exposes_its_cooldown():
    tr = GoogleWebTranslator()
    attach(tr, FakeSession([FakeResp(429)]))
    assert run(tr.translate("hola", "zh-CN")) is None
    assert tr.cooldown_until > time.monotonic() + 100
    assert tr.last_error == (429, None)
    assert CachedTranslator(tr).cooldown_until == tr.cooldown_until


def test_a_cooldown_is_announced_once_until_a_real_success(tmp_path):
    inner = GoogleWebTranslator()
    session = FakeSession([FakeResp(429), FakeResp(200, [[["你好", "hola", None]]]),
                           FakeResp(429)])
    attach(inner, session)
    engine = CachedTranslator(inner)

    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="google")
        p._heal_local_engine = noop
        await p._translate_and_update(job(1, "hola"))      # 429：说一次
        await p._translate_and_update(job(2, "otra"))      # 冷却中：不重复
        inner._cooldown_until = 0.0                       # 冷却到期（不真等 120 秒）
        await p._translate_and_update(job(3, "hola"))      # 真的成功一次
        await p._translate_and_update(job(4, "nueva"))     # 又 429：再说一次
        return p

    p = run(scenario())
    notices = [plain(m["text"]) for m in p.server.of("notice")]
    assert len(notices) == 2 and "HTTP 429" in notices[0] and "120" in notices[0]
    assert [(r["engine"], r["status"], r["seconds"])
            for r in rows(p.audit, "translation_cooldown")] == [("google", 429, 120)] * 2
    assert len(session.calls) == 3


def test_a_cache_hit_during_a_cooldown_does_not_rearm_the_notice(tmp_path):
    inner = GoogleWebTranslator()
    session = FakeSession([FakeResp(200, [[["你好", "hola", None]]]), FakeResp(429)])
    attach(inner, session)
    engine = CachedTranslator(inner)

    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="google")
        p._heal_local_engine = noop
        await p._translate_and_update(job(1, "hola"))      # 成功，进缓存
        await p._translate_and_update(job(2, "otra"))      # 429
        await p._translate_and_update(job(3, "hola"))      # 缓存命中：不算恢复
        await p._translate_and_update(job(4, "mas"))       # 仍在冷却
        return p

    p = run(scenario())
    assert len(p.server.of("notice")) == 1
    assert len(rows(p.audit, "translation_cooldown")) == 1
    assert len(session.calls) == 2


# ---------------------------------------------------------------------------
# 29. 直播中不下载模型；停止后补上；下载留审计
# ---------------------------------------------------------------------------

@pytest.fixture
def ollama(monkeypatch):
    state = {"running": True, "installed": True, "small": False, "large": False,
             "gemma": False, "pulls": [], "result": (True, None), "gate": None,
             "progress": False}

    async def is_running(timeout=2):
        return state["running"]

    async def start(timeout=25):
        state["running"] = True
        return True

    async def pull(model, on_progress=None):
        state["pulls"].append(model)
        if state["progress"] and on_progress:
            on_progress(50.0, 550.0, 1100.0)
            for _ in range(3):
                await asyncio.sleep(0)                 # 让进度提示先显示出来
        if state["gate"] is not None:
            await state["gate"].wait()
        ok, error = state["result"]
        if ok:
            state["large" if model == HYMT2_LARGE else "small"] = True
        return ok, error

    monkeypatch.setattr(localmodel, "is_running", is_running)
    monkeypatch.setattr(localmodel, "is_installed", lambda: state["installed"])
    monkeypatch.setattr(localmodel, "start", start)
    monkeypatch.setattr(localmodel, "pull", pull)
    def listed():                       # /api/tags：按生成时真正用的名字列出已有模型
        return [m for m, key in ((HYMT2_SMALL, "small"), (HYMT2_LARGE, "large"),
                                 ("translategemma:4b", "gemma")) if state[key]]

    monkeypatch.setattr(translator, "_ollama_models_or_none", listed)
    return state


def test_no_model_download_while_live_and_it_runs_after_the_session(ollama, tmp_path):
    async def scenario():
        p = bare_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL), engine="hymt2")
        session_audit = p.audit
        release = asyncio.Event()

        async def session():
            await p.ensure_local_translator()          # 开播时的备模型
            await p.ensure_local_translator()          # 同一场里自愈又来问：不重复提示
            await release.wait()
            await p._end_session(session_audit)        # 直播结束收尾

        p._stream_task = asyncio.ensure_future(session())
        assert await wait_for(lambda: getattr(p, "_pull_deferred", None))
        await asyncio.sleep(0)
        assert ollama["pulls"] == []                   # 直播中一个字节都不下
        release.set()
        await p._stream_task
        await drain(p)
        return p, session_audit

    p, session_audit = run(scenario())
    assert ollama["pulls"] == [HYMT2_SMALL]
    deferred = [m["text"] for m in p.server.of("notice") if "停止后自动下载" in m["text"]]
    assert len(deferred) == 1 and "Hy-MT2 1.8B" in plain(deferred[0])
    assert [r["state"] for r in rows(session_audit, "model_pull")] == ["deferred"]
    assert any("本地翻译已就绪" in m["detail"] for m in p.server.of("status"))


def test_a_download_already_running_is_left_alone_and_noted_in_the_session(ollama, tmp_path):
    async def scenario():
        p = bare_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL), engine="hymt2",
                          audit=False)
        ollama["gate"] = asyncio.Event()
        startup = asyncio.ensure_future(p.ensure_local_translator())    # 启动时，不在直播
        assert await wait_for(lambda: ollama["pulls"] == [HYMT2_SMALL])
        p.audit = AuditLog(room_url="https://www.tiktok.com/@x/live", log_dir=tmp_path)
        p._stream_task = asyncio.get_running_loop().create_future()     # 下载中开播
        await p.ensure_local_translator()
        assert ollama["pulls"] == [HYMT2_SMALL]        # 没起第二个，也没推迟
        ollama["gate"].set()
        await startup
        p._stream_task.cancel()
        return p

    p = run(scenario())
    assert [r["state"] for r in rows(p.audit, "model_pull")] == ["in_progress", "done"]
    assert not [m for m in p.server.of("notice") if "停止后自动下载" in m["text"]]


# ---------------------------------------------------------------------------
# 30. 识别积压或强模型正忙时，报警翻译不再叫 7B
# ---------------------------------------------------------------------------

def test_alert_translation_skips_the_strong_model_while_recognition_is_behind(
        monkeypatch, tmp_path):
    probes = []
    monkeypatch.setattr(translator, "create_strong_translator",
                        lambda: probes.append(1) or Strong(result="强译"))
    fast = Fast("hymt2", model=HYMT2_SMALL, out="快译")

    async def scenario():
        p = bare_pipeline(tmp_path, fast)
        p.telemetry.set_backlog(AUDIO_BACKLOG_WARN_SEC + 5)
        await p._translate_alert([1], "esto cura el cancer", "es")
        return p

    p = run(scenario())
    assert probes == [] and fast.calls == 1
    assert p.server.of("alert_update")[0]["context_zh"] == "快译"
    (record,) = rows(p.audit, "alert_translation")
    assert record["downgraded_for_backlog"] is True and record["model"] == HYMT2_SMALL


def test_a_second_alert_does_not_queue_behind_a_running_strong_call(tmp_path):
    fast = Fast("hymt2", model=HYMT2_SMALL, out="快译")

    async def scenario():
        gate = asyncio.Event()

        class SlowStrong(Strong):
            async def translate(self, *a, **k):
                self.calls += 1
                await gate.wait()
                return "强译"

        strong = SlowStrong()
        p = bare_pipeline(tmp_path, fast)
        p._strong = strong
        first = asyncio.ensure_future(p._translate_alert([1], "esto cura el cancer", "es"))
        assert await wait_for(lambda: strong.calls == 1)
        await p._translate_alert([2], "adelgaza sin dieta", "es")
        assert fast.calls == 1 and strong.calls == 1
        gate.set()
        await first
        assert p._alert_strong_busy is False
        return p

    p = run(scenario())
    assert {u["alert_id"]: u["context_zh"] for u in p.server.of("alert_update")} == {
        1: "强译", 2: "快译"}
    records = {r["alert_ids"][0]: r for r in rows(p.audit, "alert_translation")}
    assert records[2]["downgraded_for_busy"] is True and records[2]["model"] == HYMT2_SMALL
    assert records[1]["downgraded_for_busy"] is False and records[1]["model"] == HYMT2_LARGE


def test_an_alert_that_arrives_while_the_strong_model_is_probed_uses_the_resident_one(
        monkeypatch, tmp_path):
    """每场第一条报警时 self._strong 是空的，得先去线程池里探测：这期间到的第二条报警也要
    看得见「强模型已被占住」，否则两条都去叫 7B。"""
    entered, release = threading.Event(), threading.Event()
    made = []

    def create_strong():
        made.append(1)
        entered.set()
        release.wait(2)                 # 探测要一会儿；上限 2 秒，出错时测试不至于卡死
        return Strong(result="强译")

    monkeypatch.setattr(translator, "create_strong_translator", create_strong)
    fast = Fast("hymt2", model=HYMT2_SMALL, out="快译")

    async def scenario():
        p = bare_pipeline(tmp_path, fast)
        first = asyncio.ensure_future(p._translate_alert([1], "esto cura el cancer", "es"))
        try:
            assert await wait_for(entered.is_set)
            await p._translate_alert([2], "adelgaza sin dieta", "es")
        finally:
            release.set()
        await first
        return p

    p = run(scenario())
    assert made == [1] and fast.calls == 1
    assert {u["alert_id"]: u["context_zh"] for u in p.server.of("alert_update")} == {
        1: "强译", 2: "快译"}
    records = {r["alert_ids"][0]: r for r in rows(p.audit, "alert_translation")}
    assert records[2]["downgraded_for_busy"] is True and records[2]["model"] == HYMT2_SMALL
    assert records[1]["downgraded_for_busy"] is False and records[1]["model"] == HYMT2_LARGE


def test_when_the_strongest_model_is_the_resident_one_that_instance_is_used(tmp_path):
    strong = Strong(result="按需实例", model=HYMT2_SMALL)
    fast = Fast("hymt2", model=HYMT2_SMALL, out="常驻实例")

    async def scenario():
        p = bare_pipeline(tmp_path, fast)
        p._strong = strong
        await p._translate_alert([1], "esto cura el cancer", "es")
        return p

    p = run(scenario())
    assert strong.calls == 0 and fast.calls == 1
    assert p.server.of("alert_update")[0]["context_zh"] == "常驻实例"


# ---------------------------------------------------------------------------
# 31. 远程引擎拒绝密钥/模型：暂停、说清状态码、记审计，只退到本地引擎
# ---------------------------------------------------------------------------

def test_a_rejected_deepl_key_pauses_requests(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "old-key:fx")
    tr = DeepLTranslator()
    paths = []

    async def api(method, path, form=None, body=None):
        paths.append(path)
        return 403, {"message": "Forbidden"}

    tr._api = api
    assert run(tr.translate("hola", "zh-CN")) is None
    assert tr.last_error == (403, None) and tr.quota_exhausted is False
    assert tr.cooldown_until > time.monotonic() + 100
    assert run(tr.translate("otra", "zh-CN")) is None
    assert paths == ["/v2/translate"]


@pytest.mark.parametrize("cls,env,status", [
    (ClaudeTranslator, "ANTHROPIC_API_KEY", 401),
    (ClaudeTranslator, "ANTHROPIC_API_KEY", 404),
    (OpenAITranslator, "OPENAI_API_KEY", 403),
])
def test_paid_api_rejections_pause_requests_and_keep_no_body(monkeypatch, cls, env, status):
    monkeypatch.setenv(env, "sk-test-key")
    tr = cls()
    session = FakeSession([FakeResp(status, {"error": {
        "message": "Incorrect API key provided: sk-te****-key"}})])
    attach(tr, session)
    assert run(tr.translate("hola", "zh-CN")) is None
    assert tr.last_error == (status, None)             # 对方原话里可能有密钥片段：不留
    assert tr.cooldown_until > time.monotonic() + 100
    assert run(tr.translate("hola", "zh-CN")) is None
    assert len(session.calls) == 1


def test_a_rejected_key_is_reported_once_and_never_falls_back_to_google(
        monkeypatch, tmp_path, settings_file):
    engine = ScriptedEngine("deepl", script=[("http", 403, None, 120)])
    google = Fast("google")
    built = []
    monkeypatch.setattr("app.pipeline.create_translator",
                        lambda name: built.append(name) or google)

    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="deepl")
        p._heal_local_engine = noop
        await p._translate_and_update(job(1))
        await p._translate_and_update(job(2))          # 冷却中
        return p

    p = run(scenario())
    assert p.translator is engine and google.closed and built == ["auto"]
    (incident,) = p.server.of("incident")
    assert incident["level"] == "error"
    assert "HTTP 403" in plain(incident["text"]) and "重新填写密钥" in incident["text"]
    assert [(r["engine"], r["status"], r["model"], r["error"])
            for r in rows(p.audit, "translation_engine_error")] == [("deepl", 403, None, None)]
    assert p.args.translator_note is None


def test_the_rejected_key_banner_stops_promising_retries_once_the_session_ends(
        monkeypatch, tmp_path, settings_file):
    """合并时发现的接缝：「程序每 N 秒再试一次」只在监听时成立。stream 组的收尾只撤它自己
    那几条提示，这一条原样留在停下来的界面上。收尾时改成已经发生的事和下一步。"""
    engine = ScriptedEngine("deepl", script=[("http", 403, None, 120)])
    monkeypatch.setattr("app.pipeline.create_translator", lambda name: Fast("google"))

    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="deepl")
        p._heal_local_engine = noop
        await p._translate_and_update(job(1))
        await p._end_session(p.audit, "user_stop")
        return p

    p = run(scenario())
    banners = [m for m in p.server.of("incident") if m["id"] == Pipeline.ENGINE_INCIDENT]
    assert "秒再试一次" in banners[0]["text"]
    last = banners[-1]
    assert last["level"] == "error" and "再试一次" not in last["text"]
    assert "下一场开始后会再试" in last["text"]
    assert "HTTP 403" in plain(last["text"]) and "重新填写密钥" in last["text"]
    assert p._engine_rejection is None


def test_a_rejected_key_switches_to_a_local_model_when_there_is_one(
        monkeypatch, tmp_path, settings_file):
    engine = ScriptedEngine("claude", model="claude-haiku-4-5-20251001",
                            script=[("http", 401, None, 120)])
    local = Fast("hymt2", model=HYMT2_SMALL, out="本地译文")
    monkeypatch.setattr("app.pipeline.create_translator", lambda name: local)

    async def scenario():
        p = bare_pipeline(tmp_path, engine, engine="claude")
        p._heal_local_engine = noop
        await p._translate_and_update(job(1))
        return p

    p = run(scenario())
    assert p.translator is local and engine.closed
    assert p.args.translator == "claude"                # 下拉框里的选择不动
    assert p.server.of("caption_update")[-1]["translated"] == "本地译文"
    (notice,) = p.server.of("notice")
    assert "Claude 返回 HTTP 401" in plain(notice["text"])
    assert "本地 Hy-MT2 1.8B" in notice["text"]
    assert not p.server.of("incident")
    assert [(r["engine"], r["status"]) for r in rows(p.audit, "translation_engine_error")] \
        == [("claude", 401)]


def test_picking_an_engine_clears_the_engine_banner(monkeypatch, tmp_path, settings_file):
    monkeypatch.setattr("app.pipeline.create_translator", lambda name: Fast("deepl"))

    async def scenario():
        p = bare_pipeline(tmp_path, ScriptedEngine("deepl"), engine="deepl")
        p._engine_incident_up = True
        p._engine_error_mark = (p.translator, p.audit)
        await p.set_engine("deepl", key="new-key:fx")
        return p

    p = run(scenario())
    assert [m["level"] for m in p.server.of("incident")] == ["clear"]
    assert p._engine_error_mark is None


def test_selfcheck_names_a_rejected_deepl_key(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "old-key:fx")
    tr = DeepLTranslator()

    async def api(method, path, form=None, body=None):
        assert (method, path) == ("GET", "/v2/usage")
        return 403, {"message": "Forbidden"}

    tr._api = api
    c = run(selfcheck._check_deepl(
        SimpleNamespace(translator="deepl", target="zh-CN", source="es"), tr))
    assert c["level"] == "fail" and "HTTP 403" in c["detail"]
    assert "已就绪" not in c["detail"] and "重新填写密钥" in c["fix"]


def test_deepl_glossary_warning_no_longer_guesses_a_cause(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "key:fx")
    tr = DeepLTranslator()

    async def api(method, path, form=None, body=None):
        if path == "/v2/usage":
            return 200, {"character_count": 1, "character_limit": 10}
        return 456, {"message": "Too many glossaries"}

    tr._api = api
    c = run(selfcheck._check_deepl(
        SimpleNamespace(translator="deepl", target="zh-CN", source="es"), tr))
    assert c["level"] == "warn"
    plain(c["detail"] + c["fix"])


# ---------------------------------------------------------------------------
# 32. 界面里填的 Claude/OpenAI 密钥不再被自检报红；有密钥时真问一次接口
# ---------------------------------------------------------------------------

def test_selfcheck_accepts_a_key_saved_from_the_panel(monkeypatch, settings_file):
    from app.settings import save_setting

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    save_setting("api_keys", {"OPENAI_API_KEY": "sk-from-the-panel"})
    tr = OpenAITranslator()

    async def probe():
        return 200

    tr.probe_key = probe
    c = run(selfcheck.check_translator(SimpleNamespace(translator="openai"),
                                       CachedTranslator(tr)))
    assert c["level"] == "ok" and "环境变量" not in c["detail"]


@pytest.mark.parametrize("cls,env,status,level", [
    (OpenAITranslator, "OPENAI_API_KEY", 401, "fail"),
    (ClaudeTranslator, "ANTHROPIC_API_KEY", 403, "fail"),
    (ClaudeTranslator, "ANTHROPIC_API_KEY", 404, "fail"),
    (OpenAITranslator, "OPENAI_API_KEY", 404, "warn"),
    (ClaudeTranslator, "ANTHROPIC_API_KEY", 200, "ok"),
])
def test_selfcheck_asks_the_paid_api_once(monkeypatch, settings_file, cls, env, status, level):
    monkeypatch.setenv(env, "sk-test-key")
    tr = cls()
    session = FakeSession([FakeResp(status, {})])
    attach(tr, session)
    c = run(selfcheck.check_translator(SimpleNamespace(translator=tr.name), tr))
    assert c["level"] == level
    assert len(session.calls) == 1 and "/models" in session.calls[0]
    if status == 404 and cls is ClaudeTranslator:
        assert tr.MODEL in c["detail"]
    elif status != 200:
        assert "HTTP {}".format(status) in c["detail"]
    plain(c["detail"] + c["fix"])


def test_selfcheck_without_any_key_says_so(monkeypatch, settings_file):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = run(selfcheck.check_translator(SimpleNamespace(translator="claude"),
                                       SimpleNamespace(name="claude")))
    assert c["level"] == "fail" and "还没有密钥" in c["detail"]
    assert "环境变量" not in c["detail"]


def test_selfcheck_probe_that_cannot_connect_is_a_warning(monkeypatch, settings_file):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    tr = OpenAITranslator()

    async def probe():
        raise OSError("unreachable")

    tr.probe_key = probe
    c = run(selfcheck.check_translator(SimpleNamespace(translator="openai"), tr))
    assert c["level"] == "warn" and "OSError" in c["detail"]


# ---------------------------------------------------------------------------
# 33. 模型下载失败：带上 Ollama 的原话，替换停住的进度，记审计
# ---------------------------------------------------------------------------

class _Content:
    def __init__(self, lines):
        self._lines = [ln if isinstance(ln, bytes) else json.dumps(ln).encode()
                       for ln in lines]

    def __aiter__(self):
        self._it = iter(self._lines)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _PullResp:
    def __init__(self, status, lines=(), body=""):
        self.status = status
        self.content = _Content(lines)
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _client_session(resp=None, boom=None):
    class Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, data=None):
            if boom is not None:
                raise boom
            return resp

    return Session


def test_pull_returns_ollamas_own_words(monkeypatch):
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", _client_session(_PullResp(200, [
        {"status": "pulling manifest"},
        {"error": "pull model manifest: 412: see https://hf.co/api/x?token=SECRET987"}])))
    ok, error = run(localmodel.pull("hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M"))
    assert ok is False and "412" in error
    assert "SECRET987" not in error and "https://hf.co/api/x" in error

    monkeypatch.setattr(aiohttp, "ClientSession", _client_session(
        _PullResp(500, body='{"error": "no space left on device"}')))
    assert run(localmodel.pull("m")) == (False, "HTTP 500：no space left on device")

    monkeypatch.setattr(aiohttp, "ClientSession",
                        _client_session(boom=OSError("Cannot connect to 127.0.0.1:11434")))
    ok, error = run(localmodel.pull("m"))
    assert ok is False and error.startswith("OSError") and "127.0.0.1:11434" in error

    seen = []
    monkeypatch.setattr(aiohttp, "ClientSession", _client_session(_PullResp(200, [
        {"total": 100, "completed": 50}, {"status": "success"}])))
    assert run(localmodel.pull("m", on_progress=lambda *a: seen.append(a))) == (True, None)
    assert seen == [(50.0, 50 / 1e6, 100 / 1e6)]


def test_a_failed_download_replaces_the_progress_with_ollamas_words(ollama, tmp_path):
    ollama["result"] = (False, "pull model manifest: file does not exist")
    ollama["progress"] = True

    async def scenario():
        p = bare_pipeline(tmp_path, Fast("google"), engine="auto")
        await p.ensure_local_translator()
        await drain(p)
        return p

    p = run(scenario())
    details = [m["detail"] for m in p.server.of("status")]
    assert any("50%" in d for d in details)            # 进度确实显示过
    last = plain(details[-1])
    assert "下载失败" in last and "file does not exist" in last
    assert "Google 免费接口" in last and "%" not in last
    assert [(r["state"], r["error"]) for r in rows(p.audit, "model_pull")] == [
        ("start", None), ("failed", "pull model manifest: file does not exist")]
    assert p.translator.name == "google"               # 失败不换引擎


def test_a_progress_note_scheduled_just_before_a_failure_does_not_cover_it(
        ollama, tmp_path, monkeypatch):
    """进度回调刚排上提示、下载就失败了（中间没让出事件循环）：迟到的百分比不能盖掉失败原因。"""
    async def pull(model, on_progress=None):
        on_progress(63.0, 700.0, 1100.0)
        return False, "pull model manifest: file does not exist"

    monkeypatch.setattr(localmodel, "pull", pull)

    async def scenario():
        p = bare_pipeline(tmp_path, Fast("google"), engine="auto", audit=False)
        await p.ensure_local_translator()
        await drain(p)
        return p

    details = [m["detail"] for m in run(scenario()).server.of("status")]
    assert "下载失败" in details[-1] and "file does not exist" in details[-1]
    assert not [d for d in details if "%" in d]


def test_auto_mode_downloads_again_when_the_model_in_use_was_removed(ollama, tmp_path):
    """auto 正在用 1.8B、它被 ollama rm 掉而 7B 还在：以前算「有本地模型」什么都不做。"""
    ollama["large"] = True

    async def scenario():
        p = bare_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL), engine="auto",
                          audit=False)
        await p.ensure_local_translator()
        return p

    run(scenario())
    assert ollama["pulls"] == [HYMT2_SMALL]


# ---------------------------------------------------------------------------
# 34. 找 Ollama（可能跑 Spotlight）不许在事件循环上做
# ---------------------------------------------------------------------------

def test_heal_and_provisioning_look_for_ollama_off_the_event_loop(monkeypatch, tmp_path):
    seen = []

    def is_installed():
        seen.append(threading.get_ident())
        return False

    async def not_running(timeout=2):
        return False

    monkeypatch.setattr(localmodel, "is_installed", is_installed)
    monkeypatch.setattr(localmodel, "is_running", not_running)

    async def scenario():
        p = bare_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL), engine="hymt2")
        await p._heal_local_engine()
        await p.ensure_local_translator()
        return threading.get_ident()

    loop_thread = run(scenario())
    assert len(seen) == 2 and loop_thread not in seen


def test_start_looks_for_the_app_off_the_event_loop(monkeypatch):
    seen = []

    async def not_running(timeout=2):
        return False

    monkeypatch.setattr(localmodel, "is_running", not_running)
    monkeypatch.setattr(localmodel, "find_binary",
                        lambda: seen.append(("find", threading.get_ident())))
    monkeypatch.setattr(localmodel, "_mac_app_exists",
                        lambda: seen.append(("spotlight", threading.get_ident())) or False)

    async def scenario():
        return threading.get_ident(), await localmodel.start(timeout=0.1)

    loop_thread, started = run(scenario())
    assert started is False
    assert [kind for kind, _ in seen] == ["find", "spotlight"]
    assert all(ident != loop_thread for _, ident in seen)


def test_a_spotlight_hit_is_remembered_and_a_miss_is_not(monkeypatch):
    import subprocess

    calls = []
    answer = {"stdout": b"/Volumes/Tools/Ollama.app\n"}

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(stdout=answer["stdout"])

    monkeypatch.setattr(localmodel.sys, "platform", "darwin")
    monkeypatch.setattr(localmodel, "find_binary", lambda: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    monkeypatch.setattr(localmodel, "_SPOTLIGHT_FOUND", False)
    answer["stdout"] = b""
    assert localmodel._mac_app_exists() is False
    assert localmodel._mac_app_exists() is False
    assert len(calls) == 2                             # 没找到的不记，下次还查

    answer["stdout"] = b"/Volumes/Tools/Ollama.app\n"
    assert localmodel._mac_app_exists() is True
    assert localmodel._mac_app_exists() is True
    assert len(calls) == 3                             # 找到过一次就不再跑 mdfind
