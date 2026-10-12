"""pipeline.py、pipeline_engine.py、pipeline_disk.py 的英文场景（spec §12.1 G7 / G8）。

前半是会话生命周期、健康提示、持续提示、审计类提示（提交 M5a）；后半是翻译引擎的回退与报错、
额度、HTTP 429、报警译文的 why、一键更新暂停、演示模式、本地模型下载、磁盘删除（提交 M5b）。

G7：真的 Pipeline 加记录用的 CaptionServer（tests/test_resilience_stream.py 的 make_pipeline），在
i18n.use("en") 下跑完一段真实流程，这两个提交拥有的消息类型（status / notice / incident /
health / comment_source / alert_update）里，界面文字渲染成英文后没有中文。输入挑得不经过别的
模块的文字：自检项的名字用成对的替身，拿不到流地址时没有浏览器观察（advice 为空），解析失败用
ASCII 的 ResolveError，引擎名换成 ASCII 的替身（translator.engine_label 由它自己的迁移提交写英文）。
别的模块（selfcheck、resolver、browser_login、translator、updater、diskspace……）的句子由各自的
迁移提交补，跨模块组合留给收紧闸的提交。

G8：同一段流程在中文、英文下各跑一次，审计 JSONL 与终端输出逐字节相同（不变量 2），
借用 tests/test_i18n_backend_en.py 的骨架。

模板写坏时这里直接抛（NET="strict"），不像生产里那样退回中文、只打一行警告。
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import app.asr
from app import i18n
from app import pipeline as pipeline_mod
from app import resolver as resolver_mod
from app import settings as settings_mod
from app.i18n import CJK, L
from app.pipeline import LIVE_ENDED_NOTE, Pipeline, _ASRSlot
from app.resolver import ResolveError
from tests.helpers import run
from tests.test_i18n_backend_en import assert_language_independent, run_in_both_languages
from tests.test_resilience_stream import (DIRECT, ROOM, T0, audit_rows, make_pipeline, of_type,
                                          scripted_resolve)

EN = i18n.EN
OWNED = ("status", "notice", "incident", "health", "comment_source", "alert_update")


@pytest.fixture(autouse=True)
def _broken_templates_raise(monkeypatch):
    monkeypatch.setattr(i18n, "NET", "strict")


def english_ui(messages, types=OWNED):
    """这些类型的消息里，每一句界面文字渲染成英文：[(类型, 路径, 英文)]。"""
    out = []
    for msg in messages:
        if msg.get("type") not in types:
            continue
        for path, value in i18n.ui_values(msg):
            if isinstance(value, str) and value:
                out.append((msg["type"], path, i18n.render(value, EN)))
    return out


def assert_no_chinese(texts):
    bad = [t for t in texts if CJK.search(t[2])]
    assert not bad, bad


def statuses_en(server):
    return [i18n.render(d, EN) for _, d in server.statuses]


def incidents_en(server, key):
    return [i18n.render(m["text"], EN) for m in server.incidents(key) if m["level"] != "clear"]


# ---- 固定句子与拼进别的句子里的片段 ----------------------------------------------------------

def test_fixed_texts_and_fragments_read_as_english():
    assert i18n.render(LIVE_ENDED_NOTE, EN) == (
        "The stream has ended. Scroll down to review this session’s captions, or enter a new "
        "live link above.")
    assert [i18n.render(Pipeline._duration_text(s), EN) for s in (30, 300, 7260)] == [
        "30 sec", "5 min", "2 hr 1 min"]
    assert [Pipeline._duration_text(s) for s in (30, 300, 7260)] == ["30 秒", "5 分钟", "2 小时 1 分钟"]
    p = Pipeline.__new__(Pipeline)
    assert i18n.render(p._gap_lead({"diverged": True}), EN) == (
        "This computer just resumed after sleep or suspension")
    assert i18n.render(p._gap_lead({"sec": 300}), EN) == "The app wasn’t running for about 5 min"


def test_audit_texts_read_as_english():
    gap = {"from": "2026-09-29T14:01:02", "to": "2026-09-29T14:03:04",
           "lost_records": 3, "retained_records": 2}
    audit = SimpleNamespace(last_error="OSError 28", last_gap=gap)
    assert i18n.render(Pipeline._audit_failing_text(audit), EN) == (
        "Can’t write to the audit log (OSError 28). Alerts still show, but nothing is being "
        "recorded right now. The app keeps retrying and holds alert records in memory until it "
        "can write them.")
    assert "(no error details)" in i18n.render(Pipeline._audit_failing_text(SimpleNamespace()), EN)
    assert i18n.render(Pipeline._audit_recovered_text(audit), EN) == (
        "The audit log couldn’t be written from 14:01:02 to 14:03:04. Records lost: 3. Records "
        "written afterward (session header and alerts): 2. Writing has resumed, and an audit_gap "
        "record in the log marks this period.")
    unwritten = Pipeline._audit_unwritten_text({"since": "2026-09-29T14:01:02", "lost": 4,
                                                "retained": 1})
    assert i18n.render(unwritten, EN).startswith(
        "The audit log couldn’t be written from 14:01:02 until this session ended "
        "(no error details). Records lost: 4.")
    assert not CJK.search(i18n.render(unwritten, EN))


# ---- 一场从开始到放弃：状态行、提示、弹幕状态 -------------------------------------------------

def test_a_session_that_loses_the_network_and_gives_up_is_english(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.NETWORK_GIVE_UP_SEC = 60.0

    async def unreachable():
        return False, "timeout"

    async def fake_session(media, *a, **k):
        return True, 60.0

    scripted_resolve(monkeypatch, ["http://cdn/a.flv"])
    monkeypatch.setattr(resolver_mod, "tiktok_reachable", unreachable)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    with i18n.use(EN):
        run(p._run_stream_inner(ROOM))
    assert_no_chinese(english_ui(server.messages))
    texts = statuses_en(server)
    assert texts[0] == "Getting the stream URL…"
    assert texts[1].startswith("Loading speech model ") and "It downloads the first time only" in texts[1]
    assert texts[2:4] == ["Connecting to the stream audio…",
                          "The stream was interrupted. Reconnecting in 2 sec (attempt 1 of 6)…"]
    assert texts[4].startswith("This computer can’t reach www.tiktok.com. Nothing has been "
                               "monitored for ")
    assert texts[-1] == ("This computer couldn’t reach www.tiktok.com for over 1 min. Monitoring "
                         "stopped. When the network is back, click Start to start again.")
    notices = [t for kind, _, t in english_ui(server.messages) if kind == "notice"]
    assert notices == ["The banned-term list is empty, so no alerts will be raised this session. "
                       "Edit banned_terms.txt, then start again."]
    comments = [t for kind, _, t in english_ui(server.messages) if kind == "comment_source"]
    assert comments == ["Turned off with --no-comments"]


def test_reconnects_that_never_get_audio_end_with_an_english_error(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def empty_round(media, *a, **k):
        return False, 0.0

    scripted_resolve(monkeypatch, ["http://cdn/dead.flv"] * 6)
    monkeypatch.setattr(p, "_stream_session", empty_round)
    with i18n.use(EN):
        run(p._run_stream_inner(ROOM))
    assert_no_chinese(english_ui(server.messages))
    assert server.statuses[-1][0] == "error"
    assert statuses_en(server)[-1] == (
        "The stream was interrupted several times and couldn’t reconnect. Check that the stream "
        "is still live and your network is working, then click Start.")


def test_an_ended_answer_right_after_sleep_is_rechecked_in_english(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []

    async def fake_session(media, *a, sess=None, **k):
        sessions.append(media)
        if len(sessions) == 1:
            sess["gap"] = {"from": T0, "to": T0 + 1390, "sec": 1380.0, "diverged": True}
        return True, 60.0

    async def probe(url):
        return 2, ""

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("offline", kind="offline", status=4),
                                   "http://cdn/b.flv",
                                   ResolveError("offline", kind="offline", status=4)])
    monkeypatch.setattr(resolver_mod, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    with i18n.use(EN):
        run(p._run_stream_inner(ROOM))
    assert_no_chinese(english_ui(server.messages))
    texts = statuses_en(server)
    assert [t for t in texts if "Checking again" in t] == [
        "This computer just resumed after sleep or suspension. TikTok’s API returned stream "
        "status 4 (ended). Checking again in 30 sec before deciding…"]
    assert server.statuses[-1][0] == "ended" and texts[-1] == i18n.render(LIVE_ENDED_NOTE, EN)


def test_a_direct_url_that_stops_serving_after_sleep_is_english(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []

    async def fake_session(media, *a, sess=None, **k):
        sessions.append(media)
        sess["gap"] = {"from": T0, "to": T0 + 1390, "sec": 1380.0, "diverged": True}
        return True, 60.0

    async def dead(url, timeout=8):
        return False

    async def passthrough(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    monkeypatch.setattr(resolver_mod, "resolve_stream_url", passthrough)
    monkeypatch.setattr(resolver_mod, "_media_url_works", dead)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    with i18n.use(EN):
        run(p._run_stream_inner(DIRECT))
    assert_no_chinese(english_ui(server.messages))
    texts = statuses_en(server)
    assert texts[texts.index("Connecting to the stream audio…") + 1] == (
        "This computer just resumed after sleep or suspension. This stream URL isn’t returning "
        "data right now. Trying again in 2 sec (attempt 1 of 4)…")
    assert texts[-1] == ("This stream URL isn’t returning data anymore. Monitoring stopped. "
                         "Enter a live link or a new stream URL to continue.")


# ---- 持续提示：电脑休眠、音频到达率、设置文件损坏、审计 ------------------------------------------

def _gap_pipeline(monkeypatch, tmp_path):
    from app.audit import AuditLog
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    sess["clock"] = (T0, 500.0)
    return p, server, sess


def test_clock_gaps_and_audio_events_are_english(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)
    with i18n.use(EN):
        run(p._check_clock_gap(10, now=(T0 + 1390.0, 510.0)))       # 单调时钟停住：休眠
        run(p._check_clock_gap(10, now=(T0 + 1460.0, 570.0)))       # 两个钟一起多走：没运行
        run(p._on_audio_events(sess, [("low", {"audio_sec": 20.0, "wall_sec": 60.0}),
                                      ("quiet", {"audio_sec": 45.0})]))
    assert_no_chinese(english_ui(server.messages))
    first, second = incidents_en(server, "session:clock_gap")
    assert first.endswith(" This computer was asleep or suspended for about 23 min. The app "
                          "wasn’t running, so that part of the stream wasn’t monitored. During "
                          "streams, keep the computer plugged in with the lid open.")
    assert second.endswith(" The app wasn’t running for about 60 sec, so that part of the "
                           "stream wasn’t monitored. During streams, keep the computer plugged "
                           "in and don’t let it sleep. This has happened 2 times this session.")
    assert incidents_en(server, "session:audio_rate") == [
        "Only 20 sec of stream audio arrived in the last minute. The missing audio wasn’t "
        "checked."]
    assert incidents_en(server, "session:quiet_audio") == [
        "45 sec of stream audio arrived, but its volume stayed below the speech recognition "
        "threshold, so none of it was transcribed. Monitoring continues."]
    p.audit.close()


def test_audit_trouble_during_a_session_is_english(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    class FakeAudit:
        path = tmp_path / "logs" / "session-x.jsonl"
        failing = True
        last_error = "OSError 28"
        last_gap = {"from": "2026-09-29T14:01:02", "to": "2026-09-29T14:03:04",
                    "lost_records": 3, "retained_records": 2}

        def detached(self):
            return True

    audit = FakeAudit()
    p.audit = audit
    p._audit_watch = {"audit": audit, "failing": False, "detached": False,
                      "disk_low": False, "errored": False}
    monkeypatch.setattr(Pipeline, "_log_free_bytes", staticmethod(lambda _a: 512 * 1024 ** 2))
    with i18n.use(EN):
        run(p._check_audit_health())
        audit.failing = False
        run(p._check_audit_health())
    assert_no_chinese(english_ui(server.messages))
    assert incidents_en(server, "session:audit-moved") == [
        "This session’s audit file is no longer where it was ({}). New records go to the moved "
        "file, and if that file was deleted, they’re lost. Click Stop, then Start, to create a "
        "new audit file.".format(audit.path)]
    assert incidents_en(server, "session:disk-low") == [
        "Only 0.5 GB of storage is available. When the disk is full, the audit log can’t be "
        "written. Free up some storage."]
    written = incidents_en(server, "session:audit-write")
    assert written[0].startswith("Can’t write to the audit log (OSError 28).")
    assert written[1].startswith("The audit log couldn’t be written from 14:01:02 to 14:03:04.")


def test_a_damaged_settings_file_is_explained_in_english(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(settings_mod, "take_corrupt_notice", lambda: "settings.json.bad-1")
    with i18n.use(EN):
        run(p._announce_settings_backup())
    assert incidents_en(server, "settings-corrupt") == [
        "The settings file was damaged and was backed up as settings.json.bad-1. Choose the "
        "translation engine and enter the API key again."]


# ---- 健康提示：积压三档、识别卡住 --------------------------------------------------------------

def test_health_levels_and_a_stalled_recognition_call_are_english(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    with i18n.use(EN):
        run(p._announce_health("lagging", 12.0))
        run(p._announce_health("degraded", 35.0))
        p.telemetry.audio_segments_dropped = 1
        run(p._announce_health("degraded", 70.0))
        p.telemetry.audio_segments_dropped = 3
        run(p._announce_health("degraded", 80.0))
        run(p._announce_health("ok", 0.0))
        p._asr_inflight = (100.0,)
        run(p._check_asr_stall({"audio_segments_dropped": 2, "audio_backlog_sec": 40.0},
                               now=170.0))
    assert_no_chinese(english_ui(server.messages))
    health = [i18n.render(m["text"], EN) for m in server.messages if m["type"] == "health"]
    assert health == [
        "⚠️ Speech recognition is falling behind (12 sec backlog). Alerts will be delayed.",
        "🔴 Detection degraded: recognition is 35 sec behind and still processing. Once the "
        "backlog passes 60 sec, the oldest audio starts being dropped.",
        "🔴 Detection degraded: recognition is 70 sec behind. 1 audio segment older than 60 sec "
        "was dropped and wasn’t checked for banned terms.",
        "🔴 Detection degraded: recognition is 80 sec behind. 3 audio segments older than 60 sec "
        "were dropped and weren’t checked for banned terms.",
        "✅ Speech recognition caught up. Detection is back to normal.",
        "🔴 One audio segment has been in speech recognition for 70 sec without a result. Audio "
        "older than 60 sec will be dropped, so banned terms in it may be missed. Segments "
        "dropped this session: 2. If this lasts several minutes, quit and reopen the app. Stop "
        "and Start don’t reload the speech model."]


# ---- 识别模型：加载失败、连续出错恢复不了 ------------------------------------------------------

def test_speech_model_failures_are_english(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    config = {"backend": "ct2", "model": "small", "device": "cpu"}

    async def scenario():
        loop = asyncio.get_running_loop()
        got = await p._on_model_load_failed(OSError("cache unreadable"), config, loop)
        slot = _ASRSlot(object(), config=None, audit=None)
        switched = await p._on_asr_failures(slot, "boom", 3, 5.0, None, loop)
        return got, switched

    with i18n.use(EN):
        assert run(scenario()) == (None, False)
    assert_no_chinese(english_ui(server.messages))
    assert statuses_en(server)[-1] == (
        "The speech model (ct2/small) didn’t load. Check your network connection and available "
        "storage, then click Start to try again. If this keeps happening, quit and reopen the "
        "app.\nDetails: cache unreadable")
    failing = ("Speech recognition failed 3 times in a row, and audio from that time wasn’t "
               "checked for banned terms. The app can’t recover on its own. Quit and reopen the "
               "app. If it still fails, report the problem.")
    assert incidents_en(server, "session:asr-failing") == [failing]
    assert [i18n.render(m["text"], EN) for m in server.messages if m["type"] == "health"] == [
        "🔴 " + failing]


def test_a_failed_model_load_ends_the_session_with_an_english_status(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    def broken(**kw):
        raise OSError("model cache unreadable")

    monkeypatch.setattr(app.asr, "create_transcriber", broken)
    scripted_resolve(monkeypatch, ["http://cdn/a.flv"])
    with i18n.use(EN):
        run(p._run_stream_inner(ROOM))
    assert_no_chinese(english_ui(server.messages))
    assert server.statuses[-1][0] == "error"
    assert statuses_en(server)[-1].startswith("The speech model (")
    # 与自检那一行的修复说法逐字相同（tests/test_i18n_selfcheck_en.py 的 MODEL_LOAD_FIX_EN）
    assert ("Check your network connection and available storage, then click Start to try "
            "again. If this keeps happening, quit and reopen the app.") in statuses_en(server)[-1]
    assert of_type(audit_rows(tmp_path), "session_end")[0]["reason"] == "model_load_failed"


def test_the_catch_all_keeps_the_english_an_exception_carries(monkeypatch, tmp_path):
    """_run_stream 的兜底用 of(exc)：raise X(L(...)) 带来的英文不能在 .format 里丢掉。
    缺 ffmpeg 时 audio.py 抛的就是这样一个 RuntimeError，这里是它到界面的唯一出口
    （双击启动不带地址时，main.py 只在终端提醒缺组件，窗口照常打开）。"""
    from app import audio

    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: None)

    async def inner(url):
        async for _ in audio.FFmpegAudioSource("http://cdn/a.flv").frames():
            pass

    p._run_stream_inner = inner
    with i18n.use(EN):
        run(p._run_stream(ROOM))
    state, detail = server.statuses[-1]
    assert state == "error"
    assert str(detail) == "内部错误，已停止：缺少音频组件 ffmpeg——请关闭程序后重新打开，会自动补装"
    assert i18n.render(detail, EN) == (
        "An internal error stopped monitoring. Details: Audio (ffmpeg) is missing. Quit and "
        "reopen the app to install it automatically.")
    assert_no_chinese(english_ui(server.messages))


# ---- 开始、换主播、停止、地址不对 --------------------------------------------------------------

def test_start_switch_and_stop_acks_are_english(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        async def fake_start(url, media=None):
            return None

        real_start = p.start_stream
        p.start_stream = fake_start
        await p._start_with_ack("https://www.tiktok.com/@alice/live")
        p._stream_task = asyncio.ensure_future(asyncio.Event().wait())
        await asyncio.sleep(0)
        server.config["room_url"] = "https://www.tiktok.com/@alice/live"
        await p._start_with_ack("https://www.tiktok.com/@bob/live")
        p.start_stream = real_start
        await p.stop_stream()
        await p.handle_control({"type": "start", "url": "ftp://example.com/live"})

    with i18n.use(EN):
        run(scenario())
    assert_no_chinese(english_ui(server.messages))
    assert statuses_en(server) == [
        "Connecting…",
        "Switching to @bob: stopping @alice, then connecting…",
        "Stopping…",
        "Stopped. Enter a live link to start again.",
        "This address isn’t valid. Enter a live link that starts with http:// or https://."]
    assert [d for _, d in server.statuses][:3] == [
        "已收到指令，正在连接…", "正在切换到 @bob：先停止 @alice 的监听，再连接新的直播间…", "正在停止…"]


# ---- G8：审计与终端和界面语言无关 --------------------------------------------------------------

async def _sleep_audio_and_health(p):
    sess = p._session_state = p._new_session_state(p.audit)
    sess["clock"] = (T0, 500.0)
    await p._check_clock_gap(10, now=(T0 + 1390.0, 510.0))
    await p._check_clock_gap(10, now=(T0 + 1460.0, 570.0))
    await p._on_audio_events(sess, [("low", {"audio_sec": 20.0, "wall_sec": 60.0}),
                                    ("quiet", {"audio_sec": 45.0})])
    await p._announce_health("lagging", 12.0)
    await p._announce_health("ok", 0.0)
    await p._incident("session:audit-write", "error",
                      Pipeline._audit_failing_text(SimpleNamespace(last_error="disk full")))


def test_audit_and_terminal_of_these_messages_do_not_depend_on_the_ui_language(tmp_path, capsys):
    results = run_in_both_languages(_sleep_audio_and_health, tmp_path, capsys)
    assert_language_independent(results)
    zh_audit, zh_out, zh_got = results[i18n.ZH]
    assert "电脑休眠或挂起了约 23 分钟" in zh_out and "[提示] 本场第 2 次：" in zh_out
    assert "[健康] ✅ 识别已追上，检测恢复正常" in zh_out
    assert '"type": "clock_gap"' in "\n".join(zh_audit)
    _, _, en_got = results[i18n.EN]
    texts = [value for msg in en_got for _, value in i18n.ui_values(msg)]
    assert texts and not [t for t in texts if CJK.search(t)]
    assert zh_got[0]["text"] != en_got[0]["text"]


# =========================================================================================
# 提交 M5b：翻译引擎、额度、429、报警译文的 why、一键更新暂停、演示、本地模型下载、磁盘删除
# =========================================================================================

LABELS = {"deepl": "DeepL", "claude": "Claude", "openai": "OpenAI", "google": "Google",
          "hymt2": "Local Hy-MT2 1.8B"}


@pytest.fixture
def ascii_engine_labels(monkeypatch):
    """引擎的显示名由 translator.py 自己的迁移提交写英文；这里换成 ASCII，只看本提交的句子。"""
    from app import translator
    monkeypatch.setattr(translator, "engine_label", lambda name: LABELS.get(name, str(name)))


class FakeEngine:
    def __init__(self, name, status=None, said="", cooldown=0.0, streak=0, model=None,
                 raises=False):
        self.name, self.model = name, model
        self.last_error = (status, said) if status else None
        self.cooldown_until = time.monotonic() + cooldown if cooldown else 0.0
        self.fail_streak = streak
        self.raises = raises
        self.closed = False

    async def translate(self, text, target, source="auto", glossary=None):
        if self.raises:
            raise RuntimeError("timeout")
        return None

    async def close(self):
        self.closed = True


def notices_en(server):
    return [i18n.render(m["text"], EN) for m in server.messages if m.get("type") == "notice"]


def test_engine_trouble_is_explained_in_english(monkeypatch, tmp_path, ascii_engine_labels):
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline_mod, "create_translator", lambda name: None)

    async def scenario():
        p.translator = FakeEngine("deepl", status=429, cooldown=30.0)
        await p._note_engine_failure(p.translator)
        p.translator = FakeEngine("claude", status=401)
        await p._note_engine_failure(p.translator)
        await p._settle_engine_incident()
        p.translator = FakeEngine("openai", status=404, model="gpt-x")
        await p._note_engine_failure(p.translator)
        p.translator = FakeEngine("hymt2", status=500, said="model not found", streak=3)
        await p._note_engine_failure(p.translator)

    with i18n.use(EN):
        run(scenario())
    assert_no_chinese(english_ui(server.messages))
    assert notices_en(server) == [
        "DeepL returned HTTP 429. Requests are paused for 30 sec, then retried automatically. "
        "Meanwhile, captions show the original text. Banned-term alerts aren’t affected."]
    assert incidents_en(server, Pipeline.ENGINE_INCIDENT) == [
        "Claude returned HTTP 401. Captions show the original text (banned-term alerts aren’t "
        "affected). The app tries again every 120 sec. In Settings > Translation Engine, you "
        "can re-enter the API key or choose another engine.",
        "Claude returned HTTP 401. Captions after that showed only the original text "
        "(banned-term alerts weren’t affected). The app tries again when the next session "
        "starts. In Settings > Translation Engine, you can re-enter the API key or choose "
        "another engine.",
        "OpenAI returned HTTP 404 (model gpt-x). Captions show the original text (banned-term "
        "alerts aren’t affected). The app tries again every 120 sec. In Settings > Translation "
        "Engine, you can choose another engine.",
        "Local Hy-MT2 1.8B returned no translation 3 times in a row. Ollama returned HTTP 500: "
        "model not found. Captions show the original text (banned-term alerts aren’t "
        "affected). You can choose another engine in Settings > Translation Engine."]


def test_engine_fallbacks_are_explained_in_english(monkeypatch, tmp_path, ascii_engine_labels):
    p, server = make_pipeline(monkeypatch, tmp_path)
    made = []

    def fake_create(name):
        made.append(name)
        return FakeEngine(["hymt2", "google", "hymt2"][len(made) - 1])

    monkeypatch.setattr(pipeline_mod, "create_translator", fake_create)

    async def scenario():
        p.translator = FakeEngine("claude", status=403)
        await p._note_engine_failure(p.translator)          # 本机有本地模型：本场改用它
        p.translator = old = FakeEngine("deepl")
        await p._quota_fallback(old)                        # 额度用完，只有 Google
        p.translator = old = FakeEngine("deepl")
        await p._quota_fallback(old)                        # 额度用完，改用本地引擎

    with i18n.use(EN):
        run(scenario())
    assert_no_chinese(english_ui(server.messages))
    assert notices_en(server) == [
        "Claude returned HTTP 403. This session switched to Local Hy-MT2 1.8B to keep "
        "translating. To switch back, re-enter the API key in Settings > Translation Engine, "
        "then choose Claude again.",
        "The DeepL free quota is used up, and no local model is available on this computer, so "
        "the app switched to Google (free) to keep translating. Captions are now sent to "
        "Google. After you upgrade or renew DeepL, choose DeepL again to switch back.",
        "The DeepL free quota is used up, so this session switched to a local engine (hymt2) to "
        "keep translating. After you upgrade or renew DeepL, choose DeepL again to switch back."]
    assert i18n.render(p.args.translator_note, EN) == notices_en(server)[-1]


def test_choosing_an_engine_that_is_not_ready_is_english(monkeypatch, tmp_path,
                                                         ascii_engine_labels):
    from app.translator import HYMT2_LARGE
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(Pipeline, "_stream_active", lambda self: True)

    def refuse(name):
        raise RuntimeError(L("缺少密钥", "The API key is missing."))

    monkeypatch.setattr(pipeline_mod, "create_translator", refuse)

    async def scenario():
        p.translator = FakeEngine("deepl")
        await p._keep_engine_until_model("hymt2-7b", HYMT2_LARGE)
        p.translator = None
        await p._keep_engine_until_model("hymt2-7b", HYMT2_LARGE)
        await p.set_engine("deepl")                         # 建引擎抛出的界面文字原样带英文

    with i18n.use(EN):
        run(scenario())
    assert_no_chinese(english_ui(server.messages))
    assert notices_en(server) == [
        "Ollama on this computer doesn’t have Hy-MT2 7B yet. This session keeps using DeepL. It "
        "downloads automatically after monitoring stops.",
        "Ollama on this computer doesn’t have Hy-MT2 7B yet. This session continues without "
        "translation. It downloads automatically after monitoring stops.",
        "The API key is missing."]


def test_alert_translation_reasons_are_short_english_fragments(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def no_strong():
        return None

    monkeypatch.setattr(p, "_strong_translator", no_strong)

    async def scenario():
        p.translator = None
        await p._translate_alert([1], "hola amigos", "es")          # 没有引擎
        p.telemetry.set_backlog(20.0)
        await p._translate_alert([2], "hola amigos", "es")          # 识别在积压
        p.telemetry.set_backlog(0.0)
        p.translator = FakeEngine("hymt2", raises=True)
        await p._translate_alert([3], "hola amigos", "es")          # 译的时候出错

    with i18n.use(EN):
        run(scenario())
    assert_no_chinese(english_ui(server.messages))
    whys = [i18n.render(m["why"], EN) for m in server.messages if m["type"] == "alert_update"]
    assert whys == ["no translation engine is available",
                    "skipped while speech recognition is behind",
                    "translation timed out or failed"]


def test_update_pause_and_resume_are_english(monkeypatch, tmp_path):
    from app.provenance import app_version
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(Pipeline, "_stream_active", lambda self: True)
    started = []

    async def fake_start(url, media=None):
        started.append(url)

    p.start_stream = fake_start
    server.config["room_url"] = ROOM

    class Updater:
        _applying = False

        async def apply(self, live, pause, resume, before_restart):
            await pause("1.0.0", "1.1.0", pip_minutes=10)
            raise RuntimeError("boom")

    p.updater = Updater()

    async def scenario():
        await p._apply_update()
        await p._pause_for_update("1.0.0", "1.1.0")
        settings_mod.save_setting("resume_after_update", {"url": ROOM, "at": time.time()})
        await p.resume_after_update()

    with i18n.use(EN):
        run(scenario())
    assert_no_chinese(english_ui(server.messages))
    texts = statuses_en(server)
    assert texts[0] == ("Updating. Monitoring is paused while the new version’s components "
                        "install (up to about 10 min). Then the app restarts and resumes "
                        "monitoring.")
    assert "Updating. Monitoring is paused and resumes in about 1 minute." in texts
    assert texts[-1] == "Updated to v{}. Resuming monitoring @bellaallnatural…".format(
        app_version())
    assert incidents_en(server, "update") == [
        "The update didn’t finish: The app hit an error during the update (RuntimeError)."]
    assert started == [ROOM]


def test_demo_terms_brands_and_retranslate_notes_are_english(monkeypatch, tmp_path):
    from app import glossary
    p, server = make_pipeline(monkeypatch, tmp_path)
    terms = tmp_path / "terms.txt"
    terms.write_text("uno\n", encoding="utf-8")
    p.detector = SimpleNamespace(source_path=str(terms), source_mtime=0, source_hash="old")
    monkeypatch.setattr(glossary, "BRAND_DIR", tmp_path / "brands")

    def cannot_open(path):
        raise OSError("no opener")

    p._brand_dir_opener = cannot_open

    async def no_strong():
        return None

    monkeypatch.setattr(p, "_strong_translator", no_strong)
    p._recent[7] = {"id": 7, "text": "hola", "lang": "es", "target": "zh-CN"}

    async def scenario():
        demo = asyncio.ensure_future(p.run_demo())
        for _ in range(3):
            await asyncio.sleep(0)
        demo.cancel()
        try:
            await demo
        except asyncio.CancelledError:
            pass
        await p._check_terms_changed()
        await p._open_brands_dir()
        await p.retranslate(7)

    with i18n.use(EN):
        run(scenario())
    assert_no_chinese(english_ui(server.messages))
    assert statuses_en(server)[:2] == [
        "Starting demo mode…",
        "Demo mode: built-in sample lines simulate live captions (not connected to a real "
        "stream)"]
    assert notices_en(server) == [
        "The banned-term list changed. Click Stop, then Start, to apply it.",
        "Couldn’t open the folder: {}".format(tmp_path / "brands"),
        "No local model is available, so this can’t be retranslated. See the Translation Engine "
        "row in Settings > Startup Check."]


def test_local_model_download_notes_are_english(monkeypatch, tmp_path):
    from app import localmodel
    from app.translator import HYMT2_LARGE, HYMT2_SMALL
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.translator = None
    p.args.translator = "hymt2"

    async def failing_pull(model, on_progress=None):
        on_progress(50.0, 550.0, 1100.0)
        for _ in range(3):
            await asyncio.sleep(0)                  # 让进度那一条先发出去
        return False, None

    async def not_running(timeout=2):
        return False

    async def nothing():
        return None

    monkeypatch.setattr(localmodel, "pull", failing_pull)
    monkeypatch.setattr(localmodel, "is_running", not_running)
    monkeypatch.setattr(localmodel, "is_installed", lambda: True)
    monkeypatch.setattr(p, "_provision_then_check", nothing)

    async def scenario():
        await p._defer_pull(HYMT2_LARGE)
        assert await p._pull_model(HYMT2_SMALL) is False
        p.translator = FakeEngine("hymt2")
        await p._heal_local_engine()

    with i18n.use(EN):
        run(scenario())
    assert_no_chinese(english_ui(server.messages))
    assert statuses_en(server) == [
        "The local translation model Hy-MT2 7B isn’t downloaded yet. It downloads "
        "automatically after monitoring stops.",
        "Preparing the local translation model (about 1.1 GB, only needed once)…",
        "Downloading the local translation model… 50% (550 of 1100 MB, only needed once)",
        "Couldn’t download the local translation model: Ollama gave no details. The app "
        "continues without translation, and tries again after the next session stops."]
    assert notices_en(server) == [
        "Ollama, which the translation engine uses, isn’t running. Starting it…"]


def test_disk_delete_notes_are_english(monkeypatch, tmp_path):
    from app import diskspace
    p, server = make_pipeline(monkeypatch, tmp_path)
    results = iter([(1_200_000, ["a"], ["b: skipped", "c: skipped"]),
                    (2_400_000, ["a", "b"], [])])

    async def fake_delete(ids, **kw):
        return next(results)

    async def no_models():
        return []

    async def nothing():
        return None

    monkeypatch.setattr(diskspace, "delete", fake_delete)
    monkeypatch.setattr(p, "_ollama_models", no_models)
    monkeypatch.setattr(p, "_publish_disk", nothing)

    async def scenario():
        server.config["status"] = {"state": "live"}
        await p._disk_delete(["a"])
        server.config["status"] = {"state": "idle"}
        await p._disk_delete(["a"])
        await p._disk_delete(["a", "b"])

    with i18n.use(EN):
        run(scenario())
    assert_no_chinese(english_ui(server.messages))
    assert notices_en(server) == [
        "Files can’t be deleted during a stream. Click Stop first.",
        "Deleted 1 item and freed {}. Not deleted: b: skipped; c: skipped".format(
            diskspace.human(1_200_000)),
        "Deleted 2 items and freed {}.".format(diskspace.human(2_400_000))]


# ---- G8（M5b）：引擎出错与回退写进审计和终端的，与界面语言无关 --------------------------------

def _engine_trouble_in(lang, monkeypatch, tmp_path, capsys):
    from app.audit import AuditLog
    (tmp_path / lang).mkdir()
    p, server = make_pipeline(monkeypatch, tmp_path / lang)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / lang / "logs")
    monkeypatch.setattr(pipeline_mod, "create_translator", lambda name: None)

    async def scenario():
        p.translator = FakeEngine("deepl", status=429, cooldown=30.0)
        await p._note_engine_failure(p.translator)
        p.translator = FakeEngine("claude", status=401)
        await p._note_engine_failure(p.translator)
        await p._settle_engine_incident()

    i18n._bad_once.clear()
    capsys.readouterr()
    with i18n.use(lang):
        run(scenario())
    p.audit.close(reason="user_stop")
    rows = [json.dumps({k: v for k, v in json.loads(line).items()
                        if k not in ("at", "ts", "started_at", "ended_at")}, ensure_ascii=False)
            for line in p.audit.path.read_text(encoding="utf-8").splitlines()]
    return rows, capsys.readouterr().out, server


def test_engine_audit_and_terminal_do_not_depend_on_the_ui_language(
        monkeypatch, tmp_path, capsys, ascii_engine_labels):
    zh_rows, zh_out, _ = _engine_trouble_in(i18n.ZH, monkeypatch, tmp_path, capsys)
    en_rows, en_out, en_server = _engine_trouble_in(i18n.EN, monkeypatch, tmp_path, capsys)
    assert en_rows == zh_rows and en_out == zh_out
    assert "[警告] DeepL 返回 HTTP 429，程序暂停请求 30 秒后自动重试" in zh_out
    kinds = [json.loads(r)["type"] for r in zh_rows]
    assert "translation_cooldown" in kinds and "translation_engine_error" in kinds
    assert "returned HTTP" not in zh_out and "returned HTTP" not in "\n".join(zh_rows)
    assert incidents_en(en_server, Pipeline.ENGINE_INCIDENT)[0].startswith(
        "Claude returned HTTP 401.")
