"""字幕引擎是 Claude 时，「重译」和报警上下文翻译改用 Claude Sonnet 5.5（2026-10-13 用户决定）。

字幕引擎是本地模型时一切照旧（本机最强的本地模型，按需载入、用完卸载）。这里管三件事：
  1. 选哪个强模型（Pipeline._strong_translator）：看字幕引擎，引擎中途换了就跟着换；
  2. 报警上下文翻译：强模型是 Claude 时不再为「识别积压 / 另一条报警占着」让路——那两条规矩
     是怕本地模型和 Whisper 抢显存，Claude 走网络，没有要让的路；
  3. 点「重译」没有开始或没成功时，在被点的那条字幕旁边说清楚（retranslate_note），
     只写观察到的事实、不猜原因（CLAUDE.md 第八条，英文同样适用）。

测试一律用假 HTTP 层和替身，不联网、不载入模型、不开浏览器。
"""
import asyncio

import pytest

from app import i18n, translator
from app.pipeline import (AUDIO_BACKLOG_WARN_SEC, QUALITY_FAST, QUALITY_STRONG, RECENT_KEEP)
from app.translator import (HYMT2_LARGE, HYMT2_SMALL, CachedTranslator, ClaudeTranslator,
                            create_claude_strong_translator)
from tests import i18n_rules
from tests.helpers import run
from tests.test_claude_translator import RecordingSession, Resp, attach, message, text_block
from tests.test_resilience_translation import (Fast, Strong, bare_pipeline, drain, job, rows,
                                               wait_for)

EN = i18n.EN


def claude_engine(monkeypatch, out="快译"):
    """字幕引擎：包着一个真的 ClaudeTranslator，HTTP 层换成假的。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.delenv("CLAUDE_TRANSLATE_MODEL", raising=False)
    inner = ClaudeTranslator()
    attach(inner, RecordingSession([Resp(200, message(text_block(out))) for _ in range(20)]))
    return CachedTranslator(inner), inner


def retranslate_pipeline(tmp_path, engine, strong=None, quality=None):
    p = bare_pipeline(tmp_path, engine)
    p._recent = {7: job(7, "pague primero y le enviamos")}
    p._strong_inflight = set()
    if quality:
        p._quality[7] = quality
    if strong is not None:
        p._strong = strong
    return p


def of(p, kind):
    return p.server.of(kind)


def en(text):
    return i18n.render(text, EN)


def check_rule8(text):
    """给中控看的话只写观察和能做的事：中文、英文两边都不贴原因标签，英文还有因果词一级。"""
    for word in ("年龄", "限流", "封禁", "多半", "可能是", "因为", "原因是"):
        assert word not in text, text
    english = en(text)
    assert not i18n_rules.EN_LABELS.search(english), english
    assert not i18n_rules.EN_CAUSAL.search(english), english
    assert not i18n.CJK.search(english), english


# ---------------------------------------------------------------------------
# 1. 选哪个强模型
# ---------------------------------------------------------------------------

def spy_factories(monkeypatch, claude_result="claude-strong", local_result="local-strong"):
    calls = []

    def local():
        calls.append("local")
        return Strong(result=local_result, model=HYMT2_LARGE)

    def claude():
        calls.append("claude")
        return Strong(result=claude_result, model="claude-sonnet-5-5")

    monkeypatch.setattr(translator, "create_strong_translator", local)
    monkeypatch.setattr(translator, "create_claude_strong_translator", claude)
    return calls


def test_a_claude_subtitle_engine_gets_the_claude_strong_model_and_never_probes_ollama(
        monkeypatch, tmp_path):
    calls = spy_factories(monkeypatch)
    p = bare_pipeline(tmp_path, Fast("claude", model="claude-haiku-5-5"))

    async def scenario():
        first = await p._strong_translator()
        again = await p._strong_translator()
        return first, again

    first, again = run(scenario())
    assert calls == ["claude"]                      # 没有碰本地那条（它会同步探测 Ollama）
    assert first is again and first.model == "claude-sonnet-5-5"


@pytest.mark.parametrize("name", ["hymt2", "hymt2-7b", "gemma", "google", "deepl", "openai"])
def test_every_other_engine_keeps_the_local_strong_model(monkeypatch, tmp_path, name):
    calls = spy_factories(monkeypatch)
    p = bare_pipeline(tmp_path, Fast(name, model="x"))
    assert run(p._strong_translator()).model == HYMT2_LARGE
    assert calls == ["local"]


def test_the_strong_model_follows_the_subtitle_engine_when_it_changes_mid_session(
        monkeypatch, tmp_path):
    calls = spy_factories(monkeypatch)
    p = bare_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL))

    async def scenario():
        seen = [await p._strong_translator()]
        p.translator = Fast("claude", model="claude-haiku-5-5")      # 界面里换成 Claude
        seen.append(await p._strong_translator())
        p.translator = Fast("hymt2", model=HYMT2_SMALL)              # 密钥被拒，回退到本地
        seen.append(await p._strong_translator())
        return seen

    local1, claude1, local2 = run(scenario())
    assert calls == ["local", "claude", "local"]
    assert [local1.model, claude1.model, local2.model] == [
        HYMT2_LARGE, "claude-sonnet-5-5", HYMT2_LARGE]
    assert local1 is not local2 and local1.closed is True       # 换下来的实例没人在用：关掉


def test_refilling_the_claude_key_gives_a_fresh_strong_instance_and_forgets_missing(
        monkeypatch, tmp_path):
    """填了新密钥 = 新的字幕引擎对象；强模型实例（带着旧密钥和 401 冷却）和「本场没有强模型」
    的记号一起作废。"""
    made = []

    def claude():
        made.append(1)
        return None if len(made) == 1 else Strong(result="x", model="claude-sonnet-5-5")

    monkeypatch.setattr(translator, "create_claude_strong_translator", claude)
    old = Fast("claude", model="claude-haiku-5-5")
    p = bare_pipeline(tmp_path, old)

    async def scenario():
        assert await p._strong_translator() is None
        assert await p._strong_translator() is None            # 本场探测过没有：不重探
        assert len(made) == 1
        p.translator = Fast("claude", model="claude-haiku-5-5")     # 重填密钥
        return await p._strong_translator()

    assert run(scenario()).model == "claude-sonnet-5-5" and len(made) == 2


def test_a_cached_wrapper_is_looked_through_to_the_engine_inside(monkeypatch, tmp_path):
    calls = spy_factories(monkeypatch)
    wrapped, _inner = claude_engine(monkeypatch)
    p = bare_pipeline(tmp_path, wrapped)
    run(p._strong_translator())
    assert calls == ["claude"]


# ---------------------------------------------------------------------------
# 2. 点「重译」：真的走 Sonnet 5.5 的请求，字幕引擎一次都不用
# ---------------------------------------------------------------------------

def test_retranslate_with_a_claude_engine_sends_the_sonnet_request_and_publishes(
        monkeypatch, tmp_path):
    wrapped, subtitle = claude_engine(monkeypatch)
    big = create_claude_strong_translator()
    session = RecordingSession([Resp(200, message(text_block("先付款，再发货")))])
    attach(big, session)
    sub_session = subtitle._session = RecordingSession([])
    p = retranslate_pipeline(tmp_path, wrapped, strong=big, quality=QUALITY_FAST)
    p._for_translation = lambda text: (text, None)

    async def scenario():
        await p._strong_translator()          # 先对一遍引擎种类：已有的实例要保留
        await p.retranslate(7)

    run(scenario())
    assert p._strong is big
    (post,) = session.posts
    assert post["body"]["model"] == "claude-sonnet-5-5"
    assert post["body"]["thinking"] == {"type": "between_tools"}
    assert post["body"]["messages"] == [{"role": "user", "content": "pague primero y le enviamos"}]
    assert sub_session.posts == []
    final = [m for m in of(p, "caption_update") if m.get("translated")][-1]
    assert final["translated"] == "先付款，再发货" and final["strong"] is True
    assert final["quality"] == QUALITY_STRONG and final["strong_state"] == "ok"
    assert not of(p, "retranslate_note")
    (record,) = rows(p.audit, "translation_strong")
    assert record["model"] == "claude-sonnet-5-5" and record["ok"] is True
    assert record["trigger"] == "manual"


def test_a_local_engine_retranslates_exactly_as_before(monkeypatch, tmp_path):
    spy_factories(monkeypatch, local_result="本地强译")
    p = retranslate_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL), quality=QUALITY_FAST)
    p._for_translation = lambda text: (text, None)
    run(p.retranslate(7))
    assert [m for m in of(p, "caption_update") if m.get("translated")][-1]["translated"] == "本地强译"
    (record,) = rows(p.audit, "translation_strong")
    assert record["model"] == HYMT2_LARGE


# ---------------------------------------------------------------------------
# 3. 报警上下文翻译
# ---------------------------------------------------------------------------

def test_alert_context_uses_sonnet_even_while_recognition_is_behind(monkeypatch, tmp_path):
    """识别积压时本地强模型会让路（怕和 Whisper 抢内存）；Claude 走网络，不让。"""
    calls = spy_factories(monkeypatch, claude_result="强译")
    fast = Fast("claude", model="claude-haiku-5-5", out="快译")

    async def scenario():
        p = bare_pipeline(tmp_path, fast)
        p.telemetry.set_backlog(AUDIO_BACKLOG_WARN_SEC + 5)
        await p._translate_alert([1], "esto cura el cancer", "es")
        return p

    p = run(scenario())
    assert calls == ["claude"] and fast.calls == 0
    assert of(p, "alert_update")[0]["context_zh"] == "强译"
    (record,) = rows(p.audit, "alert_translation")
    assert record["model"] == "claude-sonnet-5-5"
    assert record["downgraded_for_backlog"] is False and record["downgraded_for_busy"] is False


def test_two_alerts_at_once_both_use_sonnet(monkeypatch, tmp_path):
    spy_factories(monkeypatch)
    fast = Fast("claude", model="claude-haiku-5-5", out="快译")

    async def scenario():
        gate = asyncio.Event()

        class SlowStrong(Strong):
            async def translate(self, text, *a, **k):
                self.calls += 1
                await gate.wait()
                return "强译：" + text[:5]

        strong = SlowStrong(model="claude-sonnet-5-5")
        p = bare_pipeline(tmp_path, fast)
        p._strong = strong
        first = asyncio.ensure_future(p._translate_alert([1], "esto cura el cancer", "es"))
        assert await wait_for(lambda: strong.calls == 1)
        second = asyncio.ensure_future(p._translate_alert([2], "adelgaza sin dieta", "es"))
        assert await wait_for(lambda: strong.calls == 2)
        gate.set()
        await asyncio.gather(first, second)
        return p

    p = run(scenario())
    assert fast.calls == 0
    assert {u["alert_id"] for u in of(p, "alert_update")} == {1, 2}
    assert all(r["downgraded_for_busy"] is False for r in rows(p.audit, "alert_translation"))


def test_a_failed_claude_strong_keeps_its_instance_and_the_resident_engine_answers(
        monkeypatch, tmp_path):
    """本地强模型失败要丢掉实例重新探测（模型可能被删了）。Claude 的实例带着自己的 429 冷却，
    丢了等于忘了刚被限速，下一条报警又去撞一次——不丢。"""
    calls = spy_factories(monkeypatch)
    strong = Strong(result=None, model="claude-sonnet-5-5")
    strong.last_error = (429, None)
    fast = Fast("claude", model="claude-haiku-5-5", out="快译")

    async def scenario():
        p = bare_pipeline(tmp_path, fast)
        p._strong = strong
        await p._translate_alert([1], "esto cura el cancer", "es")
        await p._translate_alert([2], "esto cura el cancer", "es")
        await drain(p)
        return p

    p = run(scenario())
    assert p._strong is strong and calls == []
    assert strong.calls == 2 and fast.calls == 2
    assert [u["context_zh"] for u in of(p, "alert_update")] == ["快译", "快译"]
    first = rows(p.audit, "alert_translation")[0]
    assert first["fallback"] is True and first["error"] == "HTTP 429"


def test_a_local_strong_that_fails_is_still_dropped_for_a_fresh_probe(monkeypatch, tmp_path):
    calls = spy_factories(monkeypatch, local_result="重新探测到的")
    fast = Fast("hymt2", model=HYMT2_SMALL, out="快译")

    async def scenario():
        p = bare_pipeline(tmp_path, fast)
        p._strong = Strong(result=None)
        await p._translate_alert([1], "esto cura el cancer", "es")
        await p._translate_alert([2], "esto cura el cancer", "es")
        return p

    p = run(scenario())
    assert calls == ["local"]
    assert of(p, "alert_update")[-1]["context_zh"] == "重新探测到的"


# ---------------------------------------------------------------------------
# 4. 点了重译没有开始 / 没成功：在被点的那条字幕旁边说清楚
# ---------------------------------------------------------------------------

def notes(p):
    return of(p, "retranslate_note")


def only_note(p):
    (note,) = notes(p)
    assert note["id"] == 7
    check_rule8(note["text"])
    return note["text"]


def test_a_caption_without_a_saved_original_says_so(tmp_path):
    p = retranslate_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL))
    p._recent.clear()
    run(p.retranslate(7))
    text = only_note(p)
    assert text == "这条字幕没有可重译的原文记录（程序只保留最近 {} 条需要翻译的字幕）".format(RECENT_KEEP)
    assert en(text) == ("This caption has no saved original to retranslate. The app keeps only "
                        "the latest {} captions that needed translation.".format(RECENT_KEEP))
    assert [m.get("strong_state") for m in of(p, "caption_update")] == ["failed"]   # 按钮与手机


def test_no_local_model_says_so_where_the_caption_is(monkeypatch, tmp_path):
    spy_factories(monkeypatch, local_result=None)
    monkeypatch.setattr(translator, "create_strong_translator", lambda: None)
    p = retranslate_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL))
    run(p.retranslate(7))
    assert only_note(p) == "没有可用的本地模型，无法重译（见首页自检的「翻译引擎」一项）"
    assert en(notes(p)[0]["text"]) == (
        "No local model is available, so this can’t be retranslated. See the Translation Engine "
        "row in Settings > Startup Check.")
    assert not of(p, "notice")                        # 不再是页脚那一行
    assert [m.get("strong_state") for m in of(p, "caption_update")] == ["failed"]


def test_a_claude_engine_without_a_key_does_not_blame_a_missing_local_model(
        monkeypatch, tmp_path):
    monkeypatch.setattr(translator, "create_claude_strong_translator", lambda: None)
    p = retranslate_pipeline(tmp_path, Fast("claude", model="claude-haiku-5-5"))
    run(p.retranslate(7))
    text = only_note(p)
    assert "本地模型" not in text and "Claude" in text
    assert en(text) == ("Retranslating needs a Claude API key, and none is available. Enter one "
                        "in Settings > Translation Engine.")


def test_an_already_retranslated_caption_answers_the_click(tmp_path):
    strong = Strong(result="x")
    p = retranslate_pipeline(tmp_path, Fast("hymt2"), strong=strong, quality=QUALITY_STRONG)
    run(p.retranslate(7))
    assert en(only_note(p)) == "This caption is already retranslated."
    assert strong.calls == 0 and not of(p, "caption_update")      # 不是失败，按钮不变


def test_a_caption_already_being_retranslated_answers_the_click(tmp_path):
    strong = Strong(result="x")
    p = retranslate_pipeline(tmp_path, Fast("hymt2"), strong=strong, quality=QUALITY_FAST)
    p._strong_inflight.add(7)
    run(p.retranslate(7))
    assert en(only_note(p)) == "This caption is already being retranslated."
    assert strong.calls == 0


class Flaky(Strong):
    """按脚本失败的强模型：像真引擎那样维护 last_error / cooldown_until。"""

    def __init__(self, result=None, error=None, cooldown=0.0, refuse=None, model="claude-sonnet-5-5"):
        super().__init__(result=result, model=model)
        self._error, self._cooldown, self._refuse = error, cooldown, refuse

    @property
    def cooldown_until(self):
        import time
        return time.monotonic() + self._cooldown if self._cooldown else 0.0

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls += 1
        if self._error:
            self.last_error = self._error
        if self._refuse is not None:
            translator.note_refusal(self._refuse or None)
        return self.result


def failed_retranslate(tmp_path, strong, quality=QUALITY_FAST, trigger="manual", monkeypatch=None,
                       engine=None):
    p = retranslate_pipeline(tmp_path, engine or Fast("claude", model="claude-haiku-5-5"),
                             strong=strong, quality=quality)
    p._for_translation = lambda text: (text, None)
    run(p.retranslate(7, trigger))
    return p


@pytest.mark.parametrize("flaky,zh,english", [
    (dict(refuse="general_harms"),
     "重译没有成功：Claude 拒绝翻译这一条（接口返回 refusal），原译文保持不变",
     "Couldn’t retranslate: Claude declined to translate this caption (the API returned a "
     "refusal). The current translation is unchanged."),
    (dict(error=(500, None)),
     "重译没有成功：Claude 返回 HTTP 500，原译文保持不变",
     "Couldn’t retranslate: Claude returned HTTP 500. The current translation is unchanged."),
    # 400/402 带着接口自己的说明：照抄，不解释
    (dict(error=(400, "invalid_request_error: thinking.type.between_tools is not supported.")),
     "重译没有成功：Claude 返回 HTTP 400（invalid_request_error: thinking.type.between_tools is "
     "not supported），原译文保持不变",
     "Couldn’t retranslate: Claude returned HTTP 400 (invalid_request_error: "
     "thinking.type.between_tools is not supported). The current translation is unchanged."),
    (dict(error=(429, None), cooldown=30.0),
     "重译没有成功：Claude 返回 HTTP 429，程序暂停请求 30 秒，原译文保持不变",
     "Couldn’t retranslate: Claude returned HTTP 429. Requests are paused for 30 sec. The "
     "current translation is unchanged."),
    (dict(),
     "重译没有成功：没有拿到译文，原译文保持不变",
     "Couldn’t retranslate: no translation came back. The current translation is unchanged."),
])
def test_a_failed_retranslate_reports_what_was_observed(tmp_path, flaky, zh, english):
    p = failed_retranslate(tmp_path, Flaky(**flaky))
    text = only_note(p)
    assert text == zh and en(text) == english
    assert [m.get("strong_state") for m in of(p, "caption_update")] == ["pending", "failed"]
    assert not [m for m in of(p, "caption_update") if m.get("translated")]    # 屏幕上那一版不动


def test_a_retranslate_that_fails_on_a_caption_with_no_translation_says_so(tmp_path):
    p = failed_retranslate(tmp_path, Flaky(error=(500, None)), quality=0)
    text = only_note(p)
    assert text.endswith("这一条仍然没有译文")
    assert en(text).endswith("This caption still has no translation.")
    assert of(p, "caption_update")[-1]["translate_state"] == "failed"


def test_a_stale_http_error_is_not_blamed_for_a_failure_that_left_no_new_response(tmp_path):
    """连不上时 last_error 不变：上一次的状态码不能算到这一次头上。"""
    strong = Flaky()
    strong.last_error = (503, None)               # 早先某次留下的
    p = failed_retranslate(tmp_path, strong)
    assert en(only_note(p)).startswith("Couldn’t retranslate: no translation came back.")


def test_a_discarded_lookalike_is_reported_as_discarded(tmp_path):
    p = retranslate_pipeline(tmp_path, Fast("claude", model="claude-haiku-5-5"),
                             strong=Strong(result="好的，没问题。我来回答你的问题：" * 8),
                             quality=QUALITY_FAST)
    p._for_translation = lambda text: (text, None)
    run(p.retranslate(7))
    text = only_note(p)
    assert text == "重译没有成功：结果不像译文，已丢弃，原译文保持不变"
    assert en(text) == ("Couldn’t retranslate: the result didn’t look like a translation, so it "
                        "was discarded. The current translation is unchanged.")


def test_a_local_model_failure_names_the_local_model_not_claude(monkeypatch, tmp_path):
    p = failed_retranslate(tmp_path, Flaky(error=(500, "boom"), model=HYMT2_LARGE),
                           engine=Fast("hymt2", model=HYMT2_SMALL))
    # Ollama 的原话也照抄（和引擎横幅一样）
    assert en(only_note(p)) == ("Couldn’t retranslate: the local model returned HTTP 500 (boom). "
                                "The current translation is unchanged.")


@pytest.mark.parametrize("trigger", ["viewer", "banned_term"])
def test_only_the_desktop_click_gets_the_note(tmp_path, trigger):
    """手机点的、报警自动触发的：桌面上没人点过，不冒提示。手机靠按钮上的失败状态。"""
    p = failed_retranslate(tmp_path, Flaky(error=(500, None)), trigger=trigger)
    assert notes(p) == []
    assert of(p, "caption_update")[-1]["strong_state"] == "failed"


@pytest.mark.parametrize("trigger", ["viewer", "banned_term"])
def test_a_refused_start_from_a_phone_still_marks_the_button_failed(monkeypatch, tmp_path, trigger):
    monkeypatch.setattr(translator, "create_strong_translator", lambda: None)
    p = retranslate_pipeline(tmp_path, Fast("hymt2", model=HYMT2_SMALL))
    run(p.retranslate(7, trigger))
    assert notes(p) == []
    assert [m.get("strong_state") for m in of(p, "caption_update")] == ["failed"]


def test_every_retranslate_note_is_a_registered_ui_message_type():
    assert i18n.UI_FIELDS["retranslate_note"] == ("text",)


def test_the_phone_never_receives_a_retranslate_note():
    from app import viewer
    assert "retranslate_note" not in viewer.ALLOW
