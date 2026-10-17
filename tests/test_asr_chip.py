"""「语音识别不确定」角标的服务端一侧（C3）：settings.json 的 asr_uncertain_chip，默认 false。

钉住：默认关着时被标记的字幕和以前逐字节相同；开着时只在「有字幕、命中会挂角标的标记」时多带
asr_flag（只放 CHIP_FLAGS 里的代码，整段套话和重转不挂）；总闸 TLT_ASR_SAFEGUARDS=off 时开着也不挂；
写错了提示一次、当关着；报警消息和报警审计永远不带它；手机白名单不放它；检测器的输入、翻译任务、
审计在开关开或关时逐次相同；中英两种界面下审计和终端逐字节相同（G8）。
"""
import json

import pytest

from app import i18n, viewer
from app.audit import AuditLog
from app.pipeline import Pipeline
from tests.helpers import run
from tests.test_asr_signals_pipeline import CAPTION_KEYS, ROOM, make_pipeline, result

PAUSE = {"cut": "pause", "frames": 40, "speech_frames": 30, "backend": "mlx", "model": "large-v3"}
HARD = {"cut": "max", "frames": 90, "speech_frames": 90, "backend": "mlx", "model": "large-v3"}


class FastTranslator:
    async def translate(self, text, target, source="auto", glossary=None):
        return "[zh] " + text


def _series(monkeypatch, tmp_path, chip=None, env=None):
    if env is None:
        monkeypatch.delenv("TLT_ASR_SAFEGUARDS", raising=False)
    else:
        monkeypatch.setenv("TLT_ASR_SAFEGUARDS", env)
    monkeypatch.setenv("TLT_ASR_EVIDENCE", "0")      # 这里只看字幕消息，不存证据
    p, server = make_pipeline(monkeypatch, tmp_path, terms=["cura el cancer"],
                              translator=FastTranslator())
    if chip is not None:
        (tmp_path / "settings.json").write_text(json.dumps({"asr_uncertain_chip": chip}),
                                                encoding="utf-8")
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._new_session_state(p.audit)
    p._session_state = sess
    scanned = []
    real_scan = p.detector.scan

    def spy(text, ts=None):
        scanned.append((text, ts))
        return real_scan(text, ts=ts)

    p.detector.scan = spy
    series = [(result("hola a todos como estan hoy"), PAUSE),
              (result("cura el cancer", lp=-1.3), PAUSE),           # 命中、也被标记
              (result("¡Gracias por ver el video!"), PAUSE),       # 整段套话：只记审计
              (result("tres por cien", cr=2.2), PAUSE),            # 复读
              (result("ya", raw="ya"), HARD),                      # 9 秒硬切只出 1 个词
              (result("", raw="!", lp=float("nan")), PAUSE)]       # 被拦掉、没有字幕
    jobs = []

    async def go():
        for i, (r, meta) in enumerate(series):
            jobs.append(await p._emit_original(r, audio_end_ts=100.0 + i, asr_ms=800,
                                               segment_ms=9000 if meta is HARD else 3000,
                                               asr_meta=meta, sess=sess))

    run(go())
    p.audit.close()
    return p, server, scanned, jobs


def captions(server):
    return [m for m in server.messages if m.get("type") == "caption"]


def test_the_chip_is_off_by_default_and_captions_are_unchanged(monkeypatch, tmp_path):
    _, server, _, _ = _series(monkeypatch, tmp_path)
    assert all(set(c) == CAPTION_KEYS for c in captions(server))
    assert Pipeline._asr_chip_setting() is False


def test_switched_on_only_flagged_shown_captions_get_the_chip(monkeypatch, tmp_path):
    _, server, _, _ = _series(monkeypatch, tmp_path, chip=True)
    by_text = {c["original"]: c for c in captions(server)}
    assert len(by_text) == 5                       # 被拦掉的那段本来就没有字幕
    assert "asr_flag" not in by_text["hola a todos como estan hoy"]
    assert by_text["cura el cancer"]["asr_flag"] == ["low_conf"]
    assert "asr_flag" not in by_text["¡Gracias por ver el video!"]   # 整段套话不挂
    assert by_text["tres por cien"]["asr_flag"] == ["loop"]
    assert by_text["ya"]["asr_flag"] == ["few_words"]
    for c in captions(server):
        assert set(c) - CAPTION_KEYS <= {"asr_flag"}


@pytest.mark.parametrize("value", ["yes", 1, "true", [True]])
def test_a_bad_value_is_ignored_with_one_warning(monkeypatch, tmp_path, capsys, value):
    monkeypatch.setattr(Pipeline, "_asr_chip_warned", False, raising=False)
    _, server, _, _ = _series(monkeypatch, tmp_path, chip=value)
    assert all("asr_flag" not in c for c in captions(server))
    Pipeline._asr_chip_setting()
    out = capsys.readouterr().out
    assert out.count("[警告] settings.json 的 asr_uncertain_chip") == 1


def test_the_master_switch_wins_over_the_setting(monkeypatch, tmp_path):
    _, server, _, _ = _series(monkeypatch, tmp_path, chip=True, env="off")
    assert all(set(c) == CAPTION_KEYS for c in captions(server))


def test_the_chip_never_reaches_alerts_or_the_audit(monkeypatch, tmp_path):
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    p_on, on_server, on_scanned, on_jobs = _series(monkeypatch, tmp_path / "on", chip=True)
    p_off, off_server, off_scanned, off_jobs = _series(monkeypatch, tmp_path / "off", chip=False)
    alerts = [m for m in on_server.messages if m.get("type") == "alert"]
    assert len(alerts) == 1 and not [k for k in alerts[0] if "asr" in k]
    strip = lambda m: {k: v for k, v in m.items() if k not in ("ts", "e2e_ms", "asr_ms")}  # noqa: E731
    assert [strip(a) for a in alerts] == [strip(m) for m in off_server.messages
                                         if m.get("type") == "alert"]
    # 检测器的输入、翻译任务和开关无关
    assert on_scanned == off_scanned and on_jobs == off_jobs
    # 审计里没有 asr_flag：角标只是字幕消息上的一个键；asr 子对象两边一样
    for path in (p_on.audit.path, p_off.audit.path):
        assert "asr_flag" not in path.read_text(encoding="utf-8")

    def segs(path):
        return [{k: v for k, v in json.loads(ln).items() if k not in ("at",)}
                for ln in path.read_text(encoding="utf-8").splitlines() if '"segment"' in ln]

    assert segs(p_on.audit.path) == segs(p_off.audit.path)


def test_the_phone_viewer_never_receives_the_chip():
    msg = {"type": "caption", "id": 3, "ts": 1.0, "original": "ya", "translated": None,
           "translate_state": "pending", "src_lang": "es", "target_lang": "zh-CN",
           "asr_ms": 900, "e2e_ms": 2000, "asr_flag": ["few_words"]}
    out = viewer.filter_payload(msg)
    assert out is not None and "asr_flag" not in out
    assert "asr_flag" not in viewer.ALLOW["caption"]
    assert "asr_flag" not in viewer.ALLOW["caption_update"]


TIME_KEYS = ("at", "ts", "started_at", "ended_at", "e2e_ms", "asr_ms")


def _g8(lang, monkeypatch, tmp_path, capsys):
    (tmp_path / lang).mkdir()
    i18n._bad_once.clear()
    capsys.readouterr()
    with i18n.use(lang):
        p, server, _, _ = _series(monkeypatch, tmp_path / lang, chip=True)
    out = capsys.readouterr().out
    audit = [json.dumps({k: v for k, v in json.loads(ln).items() if k not in TIME_KEYS},
                        ensure_ascii=False)
             for ln in p.audit.path.read_text(encoding="utf-8").splitlines()]
    flags = [c.get("asr_flag") for c in captions(server)]
    return audit, out, flags


def test_g8_with_the_chip_on_audit_terminal_and_codes_do_not_depend_on_the_ui_language(
        monkeypatch, tmp_path, capsys):
    zh = _g8(i18n.ZH, monkeypatch, tmp_path, capsys)
    en = _g8(i18n.EN, monkeypatch, tmp_path, capsys)
    assert zh == en
    assert zh[2] == [None, ["low_conf"], None, ["loop"], ["few_words"]]
