"""识别风险信号接进管线（C1）：只记数，不改行为。

钉住：每段审计多一个 asr 子对象（只有数字和 ASCII 代码，NaN 写成 null）；切段器的切法经队列的
第三个元素到达审计；老的假切段器没有 last_cut 时不崩；字幕消息的键、检测器收到的 (text, ts)、
翻译任务在总闸开或关时逐次相同；报警消息和报警审计不带任何 asr 键；session_end.asr_risk 只记这一场
自己的段；asr_config 记下规则版本、阈值和提示词指纹；中英两种界面下审计和终端逐字节相同（G8）。
全程不联网、不起 ffmpeg、不加载模型。
"""
import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

import app.audio
import app.segmenter
from app import i18n
from app import pipeline as pipeline_mod
from app.asr import ASRResult
from app.audio import FRAME_BYTES
from app.audit import AuditLog
from app.pipeline import Pipeline
from tests.helpers import run

ROOM = "https://www.tiktok.com/@bellaallnatural/live"
CAPTION_KEYS = {"type", "id", "ts", "original", "translated", "translate_state", "src_lang",
                "target_lang", "asr_ms", "e2e_ms"}
JOB_KEYS = {"id", "text", "lang", "target", "audio_end_ts", "segment_ms"}
LOUD = (np.ones(FRAME_BYTES // 2, dtype=np.int16) * 3000).tobytes()


class StubServer:
    def __init__(self):
        self.config = {}
        self.messages = []

    async def status(self, state, detail="", command=None):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)

    def of_type(self, t):
        return [m for m in self.messages if m.get("type") == t]


def make_pipeline(monkeypatch, tmp_path, terms=(), translator=None):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("\n".join(terms), encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None,
    )
    server = StubServer()
    p = Pipeline(args, server)
    p.translator = translator
    p.alerts_enabled = True
    return p, server


def rows(path, kind=None):
    out = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in out if kind is None or r.get("type") == kind]


def result(text, raw=None, lp=-0.25, cr=1.2):
    r = ASRResult(text=text, language="es", raw_text=text if raw is None else raw, rejected=[])
    r.signals = {"lp": lp, "nsp": 0.01, "cr": cr, "tok": 9, "t": 0.0, "calls": 1, "win": 1}
    return r


class LargeV3:
    """按顺序吐出安排好的结果；backend / model_size 和真的 MLXTranscriber 一样。"""

    backend = "mlx"
    model_size = "large-v3"

    def __init__(self, results):
        self.results = list(results)

    def transcribe(self, pcm):
        return self.results.pop(0)


def install_source(monkeypatch, frames):
    class FakeSource:
        def __init__(self, media, denoise_model=None):
            pass

        async def frames(self):
            for frame in frames:
                yield frame

        def stderr_tail(self):
            return ""

        async def stop(self):
            pass

    monkeypatch.setattr(app.audio, "FFmpegAudioSource", FakeSource)


def stream_round(p, transcriber, sess):
    async def go():
        loop = asyncio.get_running_loop()
        return await p._stream_session("http://cdn/s.flv", transcriber, None, loop, sess=sess)
    return run(go())


# ---- 审计：asr 子对象、NaN ------------------------------------------------------------------

def test_a_stream_round_writes_the_cut_and_signals_into_every_segment(monkeypatch, tmp_path):
    """真的切段器：9 秒硬切一段（只识别出 1 个词，标 few_words），flush 再吐 0.5 秒一段。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    install_source(monkeypatch, [LOUD] * 95)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._new_session_state(p.audit)
    stream_round(p, LargeV3([result("hola"), result("si")]), sess)
    p.audit.close()
    segs = rows(p.audit.path, "segment")
    assert [s["asr"]["cut"] for s in segs] == ["max", "flush"]
    first = segs[0]["asr"]
    assert first["ms"] == 9000 and first["voiced"] == 1.0 and first["words"] == 1
    assert first["wpvs"] == 0.111 and first["flags"] == ["few_words"]
    assert segs[1]["asr"]["flags"] == [] and segs[1]["asr"]["ms"] == 500
    # 字幕消息的键和以前完全一样：C1 只记数
    assert [set(c) for c in server.of_type("caption")] == [CAPTION_KEYS, CAPTION_KEYS]
    assert sess["asr_risk"].summary()["segments"] == 2
    assert sess["asr_risk"].summary()["flagged"] == 1


def test_an_old_fake_segmenter_without_last_cut_still_works(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    install_source(monkeypatch, [b"\x00" * FRAME_BYTES] * 16)

    class OldSegmenter:                       # 只有 feed / flush，同 tests/test_bug_sweep2.py
        def __init__(self, *a, **k):
            self.n = 0

        def feed(self, frame):
            self.n += 1
            return [frame * 8] if self.n % 8 == 0 else []

        def flush(self):
            return []

    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", OldSegmenter)

    class Plain:                              # 假识别器：不带 signals、不带 backend
        def transcribe(self, pcm):
            return SimpleNamespace(text="hola que tal", raw_text="hola que tal", language="es",
                                   rejected=[])

    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    stream_round(p, Plain(), p._new_session_state(p.audit))
    p.audit.close()
    segs = rows(p.audit.path, "segment")
    assert len(segs) == 2
    assert all(s["asr"]["cut"] is None and s["asr"]["lp"] is None for s in segs)
    assert len(server.of_type("caption")) == 2


def test_nan_is_written_as_null_and_the_line_is_strict_json(monkeypatch, tmp_path):
    p, _ = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    run(p._emit_original(result("!x", lp=float("nan")), audio_end_ts=100.0, asr_ms=900,
                         segment_ms=2600, asr_meta={"backend": "mlx", "model": "large-v3"}))
    p.audit.close()
    line = [ln for ln in p.audit.path.read_text(encoding="utf-8").splitlines()
            if '"segment"' in ln][0]
    assert "NaN" not in line
    rec = json.loads(line, parse_constant=lambda c: pytest.fail("non-finite " + c))
    assert rec["asr"]["lp"] is None and "nan_lp" in rec["asr"]["flags"]


def test_the_audit_writer_turns_any_non_finite_float_into_null(tmp_path):
    """审计写入本身也兜住（app/audit.py）：别的记录里漏进来的 NaN / inf 也不会写出非法 JSON。
    正常的记录逐字节和以前相同。"""
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path)
    audit._write({"type": "probe", "a": float("nan"), "b": [1.5, float("inf")],
                  "c": {"d": float("-inf"), "e": "ñ"}})
    normal = {"type": "probe2", "x": 1.25, "t": "texto ñ"}
    audit._write(normal)
    audit.close()
    lines = audit.path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[1]) == {"type": "probe", "a": None, "b": [1.5, None],
                                    "c": {"d": None, "e": "ñ"}}
    assert lines[2] == json.dumps(normal, ensure_ascii=False)


def test_a_segment_record_without_asr_is_unchanged(tmp_path):
    audit = AuditLog(room_url=ROOM, log_dir=tmp_path)
    r = ASRResult(text="hola", language="es", raw_text="hola", rejected=[])
    audit.segment(1, r, 10.0, 120, [])
    audit.segment(2, r, 11.0, 120, [], asr={"v": 1, "lp": -0.2})
    audit.close()
    a, b = rows(audit.path, "segment")
    assert "asr" not in a
    assert list(b)[-1] == "asr" and b["asr"] == {"v": 1, "lp": -0.2}


# ---- 行为不变 -------------------------------------------------------------------------------

class FastTranslator:
    async def translate(self, text, target, source="auto", glossary=None):
        return "[zh] " + text


def _emit_series(monkeypatch, tmp_path, env):
    """同一串段（含被标记的、命中违禁词的）走一遍 _emit_original，记下检测器收到的东西。"""
    if env is None:
        monkeypatch.delenv("TLT_ASR_SAFEGUARDS", raising=False)
    else:
        monkeypatch.setenv("TLT_ASR_SAFEGUARDS", env)
    p, server = make_pipeline(monkeypatch, tmp_path, terms=["cura el cancer"],
                              translator=FastTranslator())
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._new_session_state(p.audit)
    p._session_state = sess
    scanned = []
    real_scan = p.detector.scan

    def spy(text, ts=None):
        scanned.append((text, ts))
        return real_scan(text, ts=ts)

    p.detector.scan = spy
    # 停顿切：词数那一条不看，四段各自只命中想要的那一条
    meta = {"cut": "pause", "frames": 40, "speech_frames": 30, "backend": "mlx",
            "model": "large-v3"}
    series = [result("hola a todos como estan hoy"),
              result("cura el cancer", lp=-1.3),                 # 命中、也被标记
              result("", raw="¡Suscríbete al canal!"),           # 被过滤器拦掉的整段套话
              result("tres por cien", cr=2.2)]
    jobs = []

    async def go():
        for i, r in enumerate(series):
            jobs.append(await p._emit_original(r, audio_end_ts=100.0 + i, asr_ms=800,
                                               segment_ms=9000, asr_meta=meta, sess=sess))

    run(go())
    p.audit.close()
    return p, server, scanned, jobs, sess


def _strip_volatile(msg):
    return {k: v for k, v in msg.items() if k not in ("ts", "e2e_ms", "asr_ms")}


def test_detector_captions_and_jobs_are_the_same_with_the_master_switch_on_or_off(
        monkeypatch, tmp_path):
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    _, on_server, on_scanned, on_jobs, on_sess = _emit_series(monkeypatch, tmp_path / "on", None)
    _, off_server, off_scanned, off_jobs, off_sess = _emit_series(monkeypatch, tmp_path / "off",
                                                                  "off")
    assert on_scanned == off_scanned == [
        ("hola a todos como estan hoy", 100.0), ("cura el cancer", 101.0),
        ("¡Suscríbete al canal!", 102.0), ("tres por cien", 103.0)]
    on_caps = [_strip_volatile(c) for c in on_server.of_type("caption")]
    assert on_caps == [_strip_volatile(c) for c in off_server.of_type("caption")]
    assert all(set(c) == CAPTION_KEYS for c in on_server.of_type("caption"))
    assert [j and set(j) for j in on_jobs] == [JOB_KEYS, JOB_KEYS, None, JOB_KEYS]
    assert on_jobs == off_jobs
    assert on_sess["asr_risk"].summary()["flagged"] == 3
    assert off_sess["asr_risk"].summary() == {
        "v": 1, "on": False, "segments": 4, "shown": 3, "flagged": 0, "shown_flagged": 0,
        "nan_lp": 0, "by_flag": {}}


def test_alerts_never_carry_the_asr_risk(monkeypatch, tmp_path):
    """角标不是报警，也不挂在报警上（CLAUDE.md 第六条）：报警消息和报警审计里没有 asr 的任何键，
    同一段既命中又被标记时，报警和总闸关着时一样。"""
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    p_on, on_server, *_ = _emit_series(monkeypatch, tmp_path / "on", None)
    p_off, off_server, *_ = _emit_series(monkeypatch, tmp_path / "off", "off")
    on_alerts, off_alerts = on_server.of_type("alert"), off_server.of_type("alert")
    assert len(on_alerts) == 1
    assert not [k for k in on_alerts[0] if "asr" in k]
    assert [_strip_volatile(a) for a in on_alerts] == [_strip_volatile(a) for a in off_alerts]
    audit_alert = rows(p_on.audit.path, "alert")[0]
    assert not [k for k in audit_alert if "asr" in k]
    seg = [r for r in rows(p_on.audit.path, "segment") if r["text"] == "cura el cancer"][0]
    assert seg["asr"]["flags"] == ["low_conf"]
    off_seg = [r for r in rows(p_off.audit.path, "segment") if r["text"] == "cura el cancer"][0]
    assert "flags" not in off_seg["asr"] and off_seg["asr"]["lp"] == -1.3


def test_a_broken_risk_score_never_stops_detection_or_captions(monkeypatch, tmp_path, capsys):
    from app import asr_risk
    p, server = make_pipeline(monkeypatch, tmp_path, terms=["cura el cancer"])

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(asr_risk, "assess", boom)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    run(p._emit_original(result("cura el cancer"), audio_end_ts=100.0, asr_ms=800))
    run(p._emit_original(result("otra vez cura el cancer"), audio_end_ts=140.0, asr_ms=800))
    p.audit.close()
    assert len(server.of_type("alert")) == 2 and len(server.of_type("caption")) == 2
    segs = rows(p.audit.path, "segment")
    assert len(segs) == 2 and all("asr" not in s for s in segs)
    out = capsys.readouterr().out
    assert out.count("[警告] 识别风险分出错") == 1      # 一个进程只说一次


# ---- 会话：session_end 与 asr_config -----------------------------------------------------------

def test_session_end_carries_only_this_sessions_counts(monkeypatch, tmp_path):
    from app.resolver import ResolveError
    from tests.test_resilience_stream import make_pipeline as stream_pipeline
    from tests.test_resilience_stream import scripted_resolve
    p, server = stream_pipeline(monkeypatch, tmp_path)
    old_audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "old")
    old_sess = p._new_session_state(old_audit)
    meta = {"cut": "max", "frames": 90, "speech_frames": 90, "backend": "mlx", "model": "large-v3"}

    async def fake_session(media, slot, denoise, loop, sess=None):
        await p._emit_original(result("hola"), 100.0, 800, segment_ms=9000, asr_meta=meta,
                               sess=sess)
        await p._emit_original(result("hola a todos como estan hoy les traigo algo muy bonito"),
                               109.0, 800, segment_ms=9000, asr_meta=meta, sess=sess)
        # 晚到的旧任务拿着上一场的会话状态：它的段不算进这一场
        await p._emit_original(result("x", lp=-2.0), 110.0, 800, segment_ms=9000,
                               asr_meta=meta, sess=old_sess)
        return True, 18.0

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    logs = sorted((tmp_path / "logs").glob("session-*.jsonl"))
    end = rows(logs[-1], "session_end")[0]
    assert end["asr_risk"] == {"v": 1, "on": True, "segments": 2, "shown": 2, "flagged": 1,
                               "shown_flagged": 1, "nan_lp": 0, "by_flag": {"few_words": 1}}
    assert old_sess["asr_risk"].summary()["segments"] == 1
    config = rows(logs[-1], "asr_config")[0]
    assert config["asr_risk"]["v"] == 1 and config["asr_risk"]["on"] is True
    assert config["asr_risk"]["lp_below"] == -0.8
    assert config["language"] == "es" and config["temperature"] == 0.0
    assert "prompt_sha16" in config
    old_audit.close()


def test_the_asr_config_fingerprints_the_prompt_instead_of_copying_it(monkeypatch, tmp_path):
    p, _ = make_pipeline(monkeypatch, tmp_path)
    t = SimpleNamespace(backend="mlx", model_size="large-v3", device="gpu",
                        compute_type="float16", hotwords="Bella All Natural, limpieza facial")
    record = p._asr_config_record({"backend": "mlx", "model": "large-v3", "device": "gpu",
                                   "compute_type": "float16", "language": "es,en",
                                   "temperature": 0.0, "hotwords": t.hotwords}, t)
    assert record["prompt_sha16"] and len(record["prompt_sha16"]) == 16
    assert "Bella" not in json.dumps(record, ensure_ascii=False)
    assert record["language"] == "es,en"


def test_the_master_switch_is_recorded_in_the_asr_config(monkeypatch, tmp_path):
    monkeypatch.setenv("TLT_ASR_SAFEGUARDS", "off")
    p, _ = make_pipeline(monkeypatch, tmp_path)
    record = p._asr_config_record({"backend": "ct2", "model": "small"}, object())
    assert record["asr_risk"]["on"] is False and record["prompt_sha16"] is None


# ---- G8：中英两种界面下审计和终端逐字节相同 ------------------------------------------------------

TIME_KEYS = ("at", "ts", "started_at", "ended_at", "e2e_ms", "asr_ms")


def _g8_run(lang, monkeypatch, tmp_path, capsys):
    (tmp_path / lang).mkdir()
    i18n._bad_once.clear()
    capsys.readouterr()
    with i18n.use(lang):
        p, server, scanned, jobs, sess = _emit_series(monkeypatch, tmp_path / lang, None)
    out = capsys.readouterr().out
    lines = [json.dumps({k: v for k, v in json.loads(ln).items() if k not in TIME_KEYS},
                        ensure_ascii=False)
             for ln in p.audit.path.read_text(encoding="utf-8").splitlines()]
    return lines, out


def test_g8_the_asr_audit_and_terminal_do_not_depend_on_the_ui_language(
        monkeypatch, tmp_path, capsys):
    zh_lines, zh_out = _g8_run(i18n.ZH, monkeypatch, tmp_path, capsys)
    en_lines, en_out = _g8_run(i18n.EN, monkeypatch, tmp_path, capsys)
    assert zh_lines == en_lines
    assert zh_out == en_out
    assert sum('"asr": {' in ln for ln in zh_lines) == 4
