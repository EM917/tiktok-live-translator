"""pipeline.py 的英文场景（spec §12.1 G7 / G8）：会话生命周期、健康提示、持续提示、审计类提示。

G7：真的 Pipeline 加记录用的 CaptionServer（tests/test_resilience_stream.py 的 make_pipeline），在
i18n.use("en") 下跑完一段真实流程，本提交拥有的消息类型（status / notice / incident / health /
comment_source）里，界面文字渲染成英文后没有中文。输入挑得不经过别的模块的文字：自检项的名字
用成对的替身，拿不到流地址时没有浏览器观察（advice 为空），解析失败用 ASCII 的 ResolveError。
别的模块（selfcheck、resolver、browser_login、translator……）的句子由各自的迁移提交补，
跨模块组合留给收紧闸的提交。

G8：同一段流程在中文、英文下各跑一次，审计 JSONL 与终端输出逐字节相同（不变量 2），
借用 tests/test_i18n_backend_en.py 的骨架。

模板写坏时这里直接抛（NET="strict"），不像生产里那样退回中文、只打一行警告。
"""
import asyncio
from types import SimpleNamespace

import pytest

import app.asr
from app import i18n
from app import resolver as resolver_mod
from app import settings as settings_mod
from app.i18n import CJK
from app.pipeline import LIVE_ENDED_NOTE, Pipeline, _ASRSlot
from app.resolver import ResolveError
from tests.helpers import run
from tests.test_i18n_backend_en import assert_language_independent, run_in_both_languages
from tests.test_resilience_stream import (DIRECT, ROOM, T0, audit_rows, make_pipeline, of_type,
                                          scripted_resolve)

EN = i18n.EN
OWNED = ("status", "notice", "incident", "health", "comment_source")


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
                          "The stream was interrupted. Reconnecting in 2 sec (attempt 1 of 5)…"]
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
        "45 sec of stream audio arrived, but it stayed below the speech recognition threshold, "
        "so none of it was transcribed. Monitoring continues."]
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
        "The speech model (ct2/small) didn’t load. Check your network connection and free "
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
    assert of_type(audit_rows(tmp_path), "session_end")[0]["reason"] == "model_load_failed"


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
