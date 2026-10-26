"""只有一部分音频过了识别门限时要说出来（专家审查 M4，2026-10-21）。

切段器按固定的 RMS 300 判「有没有人声」：整段每一帧都低于它就不送去识别。以前只有「一段都没切出来」
（quiet，累计 120 秒）才挂提示；同一条音轨小 12 dB 时只送 127/300 秒去识别，界面上什么都不说。现在：

  * AudioFlowMeter 每 5 分钟（单调时钟，跨轮累计）比一次「送去识别的秒数 ÷ 收到的秒数」：低于 60% 报 gated，
    之后每个窗口没回到 75% 以上报 gated_more（数字跟着更新），回到以上报 gated_ok；60%–75% 之间来回不闪；
    窗口里收到的不到 60 秒不评；
  * 管线挂 session:gated_audio（warn），文字只写数字：近 N 分钟收到多少秒、送去识别多少秒、其余的音量一直低于
    识别门限；不写「音量太小」这类原因（第八条）。跨阈值时写一条 gated_audio 审计（low / ok），已有类型的
    记录不变；
  * 「一段都没送去识别」那条挂着时不出这一条；停止时和别的「正在进行」的提示一起撤掉；
  * 门限本身不改（改成自适应要按第四条离线配对评过再定）。

合成音轨：响的说话段、轻的说话段（0 dB 时过门限，小 12 dB 后不过）、短停顿，按 0.1 秒一帧喂真的切段器。
不联网、不起 ffmpeg、不加载模型。
"""
import asyncio

import numpy as np
import pytest

import app.audio
import app.segmenter
from app import i18n
from app.audio import FRAME_BYTES, SAMPLE_RATE, AudioFlowMeter
from app.audit import AuditLog
from app.pipeline import Pipeline
from app.segmenter import SilenceSegmenter
from tests import i18n_rules as rules
from tests.helpers import run
from tests.test_resilience_stream import CAUSE_LABELS, make_pipeline, of_type, rows_of

EN = i18n.EN
ROOM = "https://www.tiktok.com/@ficticia_gate/live"
FRAME_SAMPLES = FRAME_BYTES // 2
GATED = "session:gated_audio"


class StepClock:
    """每读一次走一帧（0.1 秒）：AudioFlowMeter.feed 每帧读一次钟，喂多少帧就过了多少秒。"""

    def __init__(self, t=1000.0, step=0.1):
        self.t = t
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def track(seconds, gain_db=0.0, seed=7):
    """合成的一段直播音频，返回 0.1 秒一帧的 PCM 列表。每 60 秒：响的说话 12 秒（RMS 约 1500）、三次 1 秒的
    停顿（RMS 约 100）、轻的说话 45 秒（RMS 约 700–1000）。0 dB 时轻的也过门限（300），整条几乎都送去识别；
    小 12 dB（×0.251）后轻的那 45 秒落到门限以下，只剩响的那几段过门限。"""
    rng = np.random.default_rng(seed)
    plan = [(1500, 6.0), (100, 1.0), (850, 22.0), (100, 1.0), (1500, 6.0), (100, 1.0), (800, 23.0)]
    scale = 10 ** (gain_db / 20.0)
    frames = []
    while len(frames) * 0.1 < seconds:
        for rms, sec in plan:
            for _ in range(int(round(sec * 10))):
                level = rms * rng.uniform(0.85, 1.15)
                pcm = rng.normal(0.0, level * scale, FRAME_SAMPLES)
                frames.append(np.clip(pcm, -32768, 32767).astype(np.int16).tobytes())
    return frames[:int(round(seconds * 10))]


def through_segmenter(frames, meter):
    """照 _stream_session 的 reader 那样喂：切段器切出来的段连同段长一起交给计数器。返回全部事件。"""
    seg = SilenceSegmenter()
    events = []
    for frame in frames:
        segments = seg.feed(frame)
        sent = sum(len(s) for s in segments) / 2.0 / SAMPLE_RATE
        events.extend(meter.feed(frame, segments=len(segments), sent_sec=sent))
    return events


def frames_at(meter, ratio, n):
    """直接按比例喂 n 帧：每帧 0.1 秒音频、其中 ratio 那么多「送去识别」。返回事件。"""
    events = []
    for _ in range(n):
        events.extend(meter.feed(b"\x00" * FRAME_BYTES, segments=1, sent_sec=0.1 * ratio))
    return events


def window(meter, ratio):
    """按比例一直喂到这个 5 分钟窗口结算为止（结算的那一帧算在这个窗口里）。返回这个窗口的事件。"""
    events = []
    start = meter._gate[0]
    for _ in range(20000):
        events.extend(frames_at(meter, ratio, 1))
        if start is None:
            start = meter._gate[0]
        elif meter._gate[0] != start:
            return events
    raise AssertionError("窗口没有结算")


def gate(events):
    return [(kind, data) for kind, data in events if kind.startswith("gated")]


def kinds(events):
    return [kind for kind, _ in gate(events)]


# ---- 计数器（app/audio.py AudioFlowMeter）------------------------------------------------------

def test_a_track_twelve_db_quieter_trips_the_gate_and_the_same_track_at_full_volume_does_not():
    loud = through_segmenter(track(305, 0.0), AudioFlowMeter(clock=StepClock()))
    assert gate(loud) == []
    quiet_meter = AudioFlowMeter(clock=StepClock())
    events = gate(through_segmenter(track(305, -12.0), quiet_meter))
    assert [k for k, _ in events] == ["gated"] and quiet_meter.gated
    data = events[0][1]
    assert set(data) == {"audio_sec", "sent_sec", "wall_sec", "ratio"}
    assert data["audio_sec"] == pytest.approx(300.0, abs=0.2)
    assert data["ratio"] < 0.6 and data["sent_sec"] == pytest.approx(data["ratio"] * data["audio_sec"],
                                                                      abs=0.2)
    assert not quiet_meter.quiet           # 一直有段切出来：「一段都没有」那条不响，以前就只剩沉默


def test_below_sixty_percent_flags_and_it_clears_only_above_seventy_five():
    meter = AudioFlowMeter(clock=StepClock())
    assert kinds(window(meter, 0.7)) == []                     # 回差区间里本来就没挂：不挂
    assert kinds(window(meter, 0.5)) == ["gated"]
    assert kinds(window(meter, 0.7)) == ["gated_more"]          # 回差区间：不撤，数字跟着更新
    assert kinds(window(meter, 0.58)) == ["gated_more"]
    assert kinds(window(meter, 0.72)) == ["gated_more"] and meter.gated
    events = gate(window(meter, 0.8))
    assert [k for k, _ in events] == ["gated_ok"] and not meter.gated
    assert events[0][1]["ratio"] == pytest.approx(0.8, abs=0.01)
    assert kinds(window(meter, 0.65)) == []                    # 撤了之后又落进回差区间：不挂


def test_a_window_with_too_little_audio_is_not_judged_and_the_window_spans_reconnects():
    meter = AudioFlowMeter(clock=StepClock(step=6.0))
    # 5 分钟里只到了约 5 秒（每帧之间钟走 6 秒）：送去识别的再少也不评，到达率另有提示
    assert kinds(window(meter, 0.0)) == [] and not meter.gated
    meter._clock.step = 0.1
    assert kinds(frames_at(meter, 0.3, 1000)) == []           # 100 秒：窗口还没满
    meter.start_round()                                       # 断流重连：窗口跨轮累计，不从零算
    assert kinds(window(meter, 0.3)) == ["gated"]


def test_the_heartbeat_and_the_other_meter_events_are_unchanged():
    """新加的是一组新事件；心跳、quiet / speech 的样子不变（它们写的审计逐字节不变）。"""
    meter = AudioFlowMeter(clock=StepClock())
    events = frames_at(meter, 1.0, 800)
    beats = [data for kind, data in events if kind == "heartbeat"]
    assert beats and set(beats[0]) == {"audio_sec", "wall_sec", "speech_frames", "segments"}
    quiet = AudioFlowMeter(clock=StepClock())
    events = []
    for _ in range(1201):
        events.extend(quiet.feed(b"\x00" * FRAME_BYTES))
    assert [k for k, _ in events if k == "quiet"] == ["quiet"]
    assert quiet.cut(1, 2.0) == [("speech", {})]


# ---- 管线：提示、审计 ------------------------------------------------------------------------

def _session(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    sess["meter"] = AudioFlowMeter(clock=StepClock())
    return p, server, sess


class Words:
    def transcribe(self, pcm):
        from types import SimpleNamespace
        return SimpleNamespace(text="hola", raw_text="hola", language="es", rejected=[])


def scripted(frames):
    class Source:
        STALL_SEC = 20.0

        def __init__(self, media, denoise_model=None):
            self.stopping = False
            self.on_stage = None

        async def frames(self):
            for frame in frames:
                yield frame

        def stderr_tail(self):
            return ""

        def stop_facts(self):
            return {}

        async def stop(self, urgent=False):
            pass

    return Source


def one_round(monkeypatch, p, sess, frames):
    monkeypatch.setattr(app.audio, "FFmpegAudioSource", scripted(frames))
    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", SilenceSegmenter)

    async def scenario():
        loop = asyncio.get_running_loop()
        return await p._stream_session("http://cdn/s.flv", Words(), None, loop, sess=sess)
    return run(scenario())


def test_a_quiet_stream_gets_the_notice_and_one_audit_row_with_numbers_only(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    one_round(monkeypatch, p, sess, track(305, -12.0))
    shown = server.config["incidents"][GATED]
    assert shown["level"] == "warn"
    (row,) = of_type(rows_of(p.audit.path), "gated_audio")
    assert list(row) == ["type", "at", "state", "audio_sec", "sent_sec", "wall_sec", "ratio"]
    assert row["state"] == "low" and row["ratio"] < 0.6
    text = shown["text"]
    zh, en = str(text), i18n.render(text, EN)
    sent = int(round(row["sent_sec"]))
    assert zh == ("近 5 分钟收到 300 秒直播音频，只有 {} 秒送去了识别，其余的音量一直低于识别门限；"
                  "程序继续监听".format(sent))
    assert en == ("In the last 5 min, 300 sec of stream audio arrived, but only {} sec was sent to "
                  "speech recognition. The rest stayed below the speech recognition threshold. "
                  "Monitoring continues.".format(sent))
    assert "session:quiet_audio" not in server.config["incidents"]


def test_full_volume_leaves_no_notice_and_no_new_audit_rows(monkeypatch, tmp_path):
    """正常音量：不挂提示，审计里没有 gated_audio；已有的音频记录照旧（键一个都没多）。"""
    p, server, sess = _session(monkeypatch, tmp_path)
    one_round(monkeypatch, p, sess, track(305, 0.0))
    assert GATED not in (server.config.get("incidents") or {})
    rows = rows_of(p.audit.path)
    assert of_type(rows, "gated_audio") == []
    beats = of_type(rows, "audio_heartbeat")
    assert beats and all(list(r) == ["type", "at", "audio_sec", "wall_sec", "speech_frames", "segments"]
                         for r in beats)
    finals = of_type(rows, "stream_audio")
    assert finals and all(list(r) == ["type", "at", "audio_sec", "segments_cut", "peak_rms", "final"]
                          for r in finals)


def _events(p, sess, *events):
    run(p._on_audio_events(sess, list(events)))


def _info(audio=300.0, sent=120.0):
    return {"audio_sec": audio, "sent_sec": sent, "wall_sec": 300.0, "ratio": round(sent / audio, 3)}


def test_the_notice_updates_while_low_and_clears_with_an_ok_row(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    _events(p, sess, ("gated", _info(sent=120.0)))
    assert "只有 120 秒" in str(server.config["incidents"][GATED]["text"])
    _events(p, sess, ("gated_more", _info(sent=210.0)))           # 回差区间里：数字更新，不撤
    assert "只有 210 秒" in str(server.config["incidents"][GATED]["text"])
    _events(p, sess, ("gated_ok", _info(sent=290.0)))
    assert GATED not in server.config["incidents"]
    levels = [m["level"] for m in server.incidents(GATED)]
    assert levels == ["warn", "warn", "clear"]                   # 一次都没在中途撤掉又挂上
    states = [r["state"] for r in of_type(rows_of(p.audit.path), "gated_audio")]
    assert states == ["low", "ok"]                               # 只记跨阈值的那两下


def test_the_nothing_passed_notice_wins_and_the_partial_one_comes_back_after_speech(monkeypatch,
                                                                                    tmp_path):
    """「一段都没送去识别」挂着时不出「只有一部分」；它挂出来时把「只有一部分」撤掉。切出语音之后，下一个
    窗口还没回升（gated_more）就重新挂出来。审计照记。"""
    p, server, sess = _session(monkeypatch, tmp_path)
    _events(p, sess, ("gated", _info(sent=100.0)))
    _events(p, sess, ("quiet", {"audio_sec": 121.0}))
    incidents = server.config["incidents"]
    assert "session:quiet_audio" in incidents and GATED not in incidents
    _events(p, sess, ("gated_more", _info(sent=0.0)))
    assert GATED not in server.config["incidents"]
    _events(p, sess, ("speech", {}), ("gated_more", _info(sent=150.0)))
    incidents = server.config["incidents"]
    assert "session:quiet_audio" not in incidents and "只有 150 秒" in str(incidents[GATED]["text"])
    assert [r["state"] for r in of_type(rows_of(p.audit.path), "gated_audio")] == ["low"]


def test_stopping_takes_the_notice_down(monkeypatch, tmp_path):
    p, server, sess = _session(monkeypatch, tmp_path)
    _events(p, sess, ("gated", _info()))
    assert GATED in Pipeline.ONGOING_INCIDENTS
    run(p._clear_ongoing_incidents())
    assert GATED not in server.config["incidents"]


def test_a_broken_audit_does_not_stop_the_notice(monkeypatch, tmp_path, capsys):
    p, server, sess = _session(monkeypatch, tmp_path)

    class Broken:
        def _write(self, record):
            raise OSError("磁盘满了")

    sess["audit"] = Broken()
    _events(p, sess, ("gated", _info()))
    assert GATED in server.config["incidents"]
    assert "记录识别门限比例出错" in capsys.readouterr().out


@pytest.mark.parametrize("sent", [0.0, 1.0, 59.0, 179.4])
def test_the_words_name_no_cause(sent):
    text = Pipeline._gated_text(_info(sent=sent))
    zh, en = str(text), i18n.render(text, EN)
    for label in CAUSE_LABELS + ("太小", "太低", "麦克风", "可能"):
        assert label not in zh
    assert not rules.ZH_LABELS.search(zh) and not rules.ZH_CAUSAL.search(zh)
    assert not rules.EN_LABELS.search(en) and not rules.EN_CAUSAL.search(en)
    assert not i18n.CJK.search(en)
    for word in ("too quiet", "too low", "microphone", "might", "may "):
        assert word not in en.lower()
