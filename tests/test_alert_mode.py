"""违禁词警示模式（alerts_enabled）：每场可选、默认关闭的开关。

产品背景（CLAUDE.md 六）：这个产品只有两个 KPI——违禁词召回与检测延迟。这个
开关不是「要不要检测」，检测（BannedTermDetector.scan）永远跑，命中永远写审计；
开关只决定命中要不要广播给界面、要不要占强模型翻译、要不要触发系统通知。关闭
时证据不能因此丢——否则事后没法回答「那场命中为什么没报警」（CLAUDE.md 八：
只报观察，不猜原因；这里同理，状态要看得见，不能是猜出来的）。

默认关闭是产品负责人明确要求的（"不要默认开 默认是关闭的"），不是本改动自选。
"""
import asyncio
import json
from types import SimpleNamespace

from app import pipeline as pipeline_mod
from app import settings
from app.pipeline import Pipeline
from app.server import CaptionServer


def run(coro):
    return asyncio.run(coro)


def res(text):
    return SimpleNamespace(text=text, raw_text=text, language="es", rejected=[])


def records(path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


class RecordingServer(CaptionServer):
    def __init__(self):
        super().__init__(port=8765)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(dict(msg))
        await super().broadcast(msg)

    def of_type(self, t):
        return [m for m in self.messages if m.get("type") == t]


class QuietNotifier:
    """系统通知的替身：记下被叫了几次，绝不真的发通知（同
    tests/test_resilience_evidence.py 里的 QuietNotifier）。"""

    def __init__(self):
        self.alerts = 0

    def note_alert(self):
        self.alerts += 1
        return False

    def send(self):
        raise AssertionError("测试里不该真的发系统通知")


def make_pipeline(monkeypatch, tmp_path, terms=("cura el cancer",)):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("\n".join(terms), encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto",
        model=None, device="auto", compute_type="auto", denoise="off", banned_terms=None,
        comments=False)
    server = RecordingServer()
    p = Pipeline(args, server)
    p.translator = None
    p._alert_notifier = QuietNotifier()

    async def nothing(*a, **k):
        return None

    p._provision_then_check = nothing      # 后台备模型/自检不是这里要测的
    p._translate_alert = nothing           # 报警上下文的强模型翻译同理（按用例覆盖）
    return p, server


# ---------------------------------------------------------------------------
# 默认值：settings.json 里没有这个键时按关处理
# ---------------------------------------------------------------------------

def test_default_is_off_when_settings_has_no_key(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    assert p.alerts_enabled is False
    assert server.config["alerts_enabled"] is False


def test_construction_restores_a_previously_saved_true(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    settings.save_setting("alerts_enabled", True)
    p, server = make_pipeline(monkeypatch, tmp_path)
    assert p.alerts_enabled is True
    assert server.config["alerts_enabled"] is True


# ---------------------------------------------------------------------------
# start 消息：设置这一场的开关，并存进 settings.json
# ---------------------------------------------------------------------------

def test_start_message_with_alerts_true_sets_the_flag_and_saves_it(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    seen = {}

    async def fake_start(url, media=None):
        seen["url"] = url

    p.start_stream = fake_start
    run(p.handle_control({"type": "start", "url": "https://www.tiktok.com/@bella/live",
                          "alerts": True}))
    assert p.alerts_enabled is True
    assert server.config["alerts_enabled"] is True
    assert settings.load_settings()["alerts_enabled"] is True
    assert seen["url"] == "https://www.tiktok.com/@bella/live"


def test_start_message_with_alerts_false_sets_the_flag_and_saves_it(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    settings.save_setting("alerts_enabled", True)     # 上一场开着
    p.alerts_enabled = True

    async def fake_start(url, media=None):
        return None

    p.start_stream = fake_start
    run(p.handle_control({"type": "start", "url": "https://www.tiktok.com/@bella/live",
                          "alerts": False}))
    assert p.alerts_enabled is False
    assert settings.load_settings()["alerts_enabled"] is False


def test_start_message_without_the_field_defaults_to_off(monkeypatch, tmp_path):
    """老页面缓存或异常消息没带 alerts 字段：按关处理，绝不擅自当成开。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.alerts_enabled = True

    async def fake_start(url, media=None):
        return None

    p.start_stream = fake_start
    run(p.handle_control({"type": "start", "url": "https://www.tiktok.com/@bella/live"}))
    assert p.alerts_enabled is False
    assert settings.load_settings()["alerts_enabled"] is False


# ---------------------------------------------------------------------------
# 命中路径：关闭时只记审计，不报警、不占强模型、不触发通知、计数不变
# ---------------------------------------------------------------------------

def test_hit_with_alerts_off_is_suppressed_but_still_audited(monkeypatch, tmp_path, capsys):
    p, server = make_pipeline(monkeypatch, tmp_path)
    assert p.alerts_enabled is False   # 默认关闭，本用例不覆盖
    calls = {"n": 0}

    async def fake_translate_alert(*a, **k):
        calls["n"] += 1

    p._translate_alert = fake_translate_alert

    async def go():
        await p._begin_session("https://www.tiktok.com/@bella/live")
        await p._emit_original(res("esto cura el cancer"), audio_end_ts=100.0, asr_ms=100)
        path = p.audit.path
        await p._end_session()
        return path

    path = run(go())
    assert server.of_type("alert") == []            # 没有广播报警
    assert p._alert_notifier.alerts == 0             # 没触发系统通知
    assert calls["n"] == 0                           # 没占强模型翻译
    assert server.config["alerts_session"]["total"] == 0   # 计数不变

    rec = [r for r in records(path) if r["type"] == "alert"][0]
    assert rec["suppressed"] == "alerts_off"
    assert "alert_id" not in rec                     # 关闭时不发号，避免打乱下次开着时的序号语义

    out = capsys.readouterr().out
    assert "[提示] 本场违禁词警示关闭：命中只记入审计，不报警" in out
    assert "[警报]" not in out


def test_hit_with_alerts_on_behaves_as_before(monkeypatch, tmp_path, capsys):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.alerts_enabled = True
    calls = {"n": 0}

    async def fake_translate_alert(*a, **k):
        calls["n"] += 1

    p._translate_alert = fake_translate_alert

    async def go():
        await p._begin_session("https://www.tiktok.com/@bella/live")
        await p._emit_original(res("esto cura el cancer"), audio_end_ts=100.0, asr_ms=100)
        path = p.audit.path
        await p._end_session()
        return path

    path = run(go())
    alerts = server.of_type("alert")
    assert len(alerts) == 1
    assert alerts[0]["alert_id"] == 1
    assert alerts[0]["session_total"] == 1
    assert "suppressed" not in alerts[0]
    assert p._alert_notifier.alerts == 1
    assert calls["n"] == 1
    assert server.config["alerts_session"]["total"] == 1

    rec = [r for r in records(path) if r["type"] == "alert"][0]
    assert "suppressed" not in rec

    out = capsys.readouterr().out
    assert "[警报]" in out
    assert "警示关闭" not in out


# ---------------------------------------------------------------------------
# 状态可见：session_start 落审计、config 带上、开场广播一次
# ---------------------------------------------------------------------------

def test_session_start_records_and_broadcasts_the_mode_when_on(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.alerts_enabled = True

    run(p._begin_session("https://www.tiktok.com/@bella/live"))
    path = p.audit.path

    rec = [r for r in records(path) if r["type"] == "session_start"][0]
    assert rec["alerts_enabled"] is True
    assert server.config["alerts_enabled"] is True
    modes = server.of_type("alert_mode")
    assert modes[-1] == {"type": "alert_mode", "on": True}


def test_session_start_records_and_broadcasts_the_mode_when_off(monkeypatch, tmp_path, capsys):
    p, server = make_pipeline(monkeypatch, tmp_path)
    assert p.alerts_enabled is False

    run(p._begin_session("https://www.tiktok.com/@bella/live"))
    path = p.audit.path

    rec = [r for r in records(path) if r["type"] == "session_start"][0]
    assert rec["alerts_enabled"] is False
    assert server.config["alerts_enabled"] is False
    modes = server.of_type("alert_mode")
    assert modes[-1] == {"type": "alert_mode", "on": False}
    assert "[提示] 本场违禁词警示关闭：命中只记入审计，不报警" in capsys.readouterr().out
