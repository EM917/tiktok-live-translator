"""断断续续时说清「这场实际听到了多少」（2026-10-19，改进清单第 4 条；app/stream_watch.py coverage、
app/pipeline_stream.py _check_coverage）。

  * 跨轮滚动覆盖率：最近 5 分钟收到的直播音频秒数 ÷ min(300, 从第一帧起过了多久)；不满 2 分钟不评；
    分母照回放的口径（拿 300 当分母，开场两分钟必然低于四成，一开场就会误报）；
  * 低于 70% 连续两次（20 秒）才报，回到 85% 以上才撤，中间不来回闪；跨阈值时写一条 coverage；
  * 页面顶部沿用 session:audio_rate 这个 id，同一件事只有一条；「本机连不上 TikTok」或「已 N 分钟
    没收到直播音频」挂着时不出；断流时不再把它撤掉；
  * 随 stats 推给页面的数字（角标的字在 web/live-ui.js）；每 5 分钟一条 stream_summary；stream_break
    带 coverage_5m；session_end 带 coverage；
  * 文字中英逐字、计数分单复数、不出现第八条的禁用词；时钟跳变那段不算「没收到」。
"""
import pytest

from app import i18n
from app.audit import AuditLog
from app.stream_watch import StreamWatch
from tests import i18n_rules as rules
from tests.helpers import run
from tests.test_resilience_stream import CAUSE_LABELS, ROOM, make_pipeline, of_type, rows_of

EN = i18n.EN
T0 = 1790000000.0


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def feed(watch, start, seconds, every=1.0):
    """从单调时刻 start 起，每 every 秒收到 1 秒直播音频（按 0.1 秒一帧喂），共 seconds 秒的时间。"""
    t = start
    while t < start + seconds:
        for k in range(int(every * 10) if every <= 1 else 10):
            watch.note_audio(0.1, t + k * 0.1, T0 + t)
        t += every


# ---- 计数 ---------------------------------------------------------------------------------

def test_not_rated_before_two_minutes_and_the_denominator_is_the_time_since_the_first_frame():
    mono = Clock()
    watch = StreamWatch(60.0, mono=mono)
    assert watch.coverage(1000.0) is None
    feed(watch, 1000.0, 60)                       # 第一分钟一直有声
    assert watch.coverage(1119.0) is None          # 不满 2 分钟不评
    ratio, span, audio, breaks = watch.coverage(1120.0)
    assert span == 120.0 and audio == pytest.approx(60.0) and ratio == pytest.approx(0.5)


def test_the_window_rolls_after_five_minutes_and_counts_breaks_inside_it():
    watch = StreamWatch(60.0, mono=Clock())
    feed(watch, 1000.0, 600)                       # 十分钟一直有声
    watch.note_break(1350.0)
    watch.note_break(1550.0)
    ratio, span, audio, breaks = watch.coverage(1600.0)
    assert span == 300.0 and ratio == pytest.approx(1.0) and breaks == 2
    ratio, span, audio, breaks = watch.coverage(1900.0)   # 后五分钟没声音
    assert ratio == pytest.approx(0.0, abs=0.01) and breaks == 0


def test_time_the_app_was_not_running_is_not_counted_as_missing():
    watch = StreamWatch(60.0, mono=Clock())
    feed(watch, 1000.0, 200)
    watch.shift(600)                               # 单调时钟多走了 600 秒（Windows 睡眠）
    ratio, span, audio, breaks = watch.coverage(1800.0)
    assert ratio == pytest.approx(1.0, abs=0.02)


# ---- 提示、审计、角标的数字 ---------------------------------------------------------------

def _session(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    return p, server, sess


def _low(watch):
    """开播 5 分钟，只有头 90 秒有声。"""
    feed(watch, 1000.0, 90)
    watch.cov_start = 1000.0


def test_low_twice_raises_the_notice_and_back_above_85_takes_it_down(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    watch = sess["watch"]
    _low(watch)
    sess["break_no"] = 7
    run(p._check_coverage(sess, 1295.0))
    assert "session:audio_rate" not in (server.config.get("incidents") or {})   # 一次不算
    run(p._check_coverage(sess, 1300.0))
    text = server.config["incidents"]["session:audio_rate"]["text"]
    assert str(text) == "近 5 分钟只收到 30% 的直播音频，缺的部分没有经过检测。本场已中断 7 次。"
    assert i18n.render(text, EN) == ("Only 30% of stream audio arrived in the last 5 min. The missing "
                                     "audio wasn’t checked. 7 interruptions this session.")
    stats = p._coverage_stats()
    assert stats == {"coverage_pct": 30, "coverage_window_min": 5, "coverage_low": True, "breaks": 7}
    # 回升到 75%：还在回差里，不撤（数字跟着更新）
    feed(watch, 1300.0, 225)
    run(p._check_coverage(sess, 1525.0))
    assert str(server.config["incidents"]["session:audio_rate"]["text"]).startswith(
        "近 5 分钟只收到 75% 的直播音频")
    feed(watch, 1525.0, 35)
    run(p._check_coverage(sess, 1560.0))
    assert "session:audio_rate" not in server.config["incidents"]
    rows = of_type(rows_of(p.audit.path), "coverage")
    assert [r["state"] for r in rows] == ["low", "ok"]
    assert rows[0]["window_sec"] == 300.0 and rows[0]["audio_sec"] == pytest.approx(90.0)


def test_one_interruption_reads_as_one(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    text = p._coverage_text(40, 180.0, 1)
    assert str(text) == "近 3 分钟只收到 40% 的直播音频，缺的部分没有经过检测。本场已中断 1 次。"
    assert i18n.render(text, EN) == ("Only 40% of stream audio arrived in the last 3 min. The missing "
                                     "audio wasn’t checked. 1 interruption this session.")


@pytest.mark.parametrize("louder", ["session:network", "session:no_audio"])
def test_a_louder_notice_wins_and_the_coverage_line_stays_away(monkeypatch, tmp_path, louder):
    """同一件事只出一条：本机连不上 TikTok、已 N 分钟没收到直播音频，都比覆盖率要紧。"""
    p, server, sess = _session(monkeypatch, tmp_path)
    _low(sess["watch"])
    run(p._check_coverage(sess, 1295.0))
    run(p._check_coverage(sess, 1300.0))
    assert "session:audio_rate" in server.config["incidents"]
    run(p._incident(louder, "warn", "x"))
    run(p._check_coverage(sess, 1305.0))
    assert "session:audio_rate" not in server.config["incidents"]
    assert p._coverage_stats()["coverage_low"] is True          # 角标的数字照给


def test_a_break_no_longer_takes_the_coverage_line_down(monkeypatch, tmp_path):
    """以前每轮断流都撤一次「音频偏少」；改成跨轮之后那一句会把跨轮的提示撤掉，删了。"""
    import app.audio
    from tests.test_live_stall import Silence, _round, scripted_source

    p, server, sess = _session(monkeypatch, tmp_path)
    _low(sess["watch"])
    run(p._check_coverage(sess, 1295.0))
    run(p._check_coverage(sess, 1300.0))
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", scripted_source(live=5))
    _round(p, sess, Silence())
    assert "session:audio_rate" in server.config["incidents"]
    brk = of_type(rows_of(p.audit.path), "stream_break")[0]
    assert brk["coverage_5m"] == pytest.approx(0.3, abs=0.001)


def test_a_summary_lands_in_the_audit_every_five_minutes(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    sess["break_no"] = 3

    class Comments:
        _connect_times = [1.0, 2.0]

    p.comment_source = Comments()
    p._maybe_stream_summary(sess, 1000.0)
    p._maybe_stream_summary(sess, 1299.0)
    p._maybe_stream_summary(sess, 1300.0)
    p._maybe_stream_summary(sess, 1400.0)
    rows = of_type(rows_of(p.audit.path), "stream_summary")
    assert len(rows) == 1
    assert rows[0]["breaks"] == 3 and rows[0]["comment_connects_hour"] == 2
    assert "monitored_sec" in rows[0] and "coverage" in rows[0]


def test_the_coverage_words_name_no_cause():
    from app.pipeline import Pipeline

    for n in (0, 1, 5):
        text = Pipeline._coverage_text(Pipeline.__new__(Pipeline), 35, 300.0, n)
        zh, en = str(text), i18n.render(text, EN)
        for label in CAUSE_LABELS:
            assert label not in zh
        assert not rules.ZH_LABELS.search(zh) and not rules.ZH_CAUSAL.search(zh)
        assert not rules.EN_LABELS.search(en) and not rules.EN_CAUSAL.search(en)
