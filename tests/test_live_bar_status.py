"""底部大字幕的翻译状态、滞后数，页脚的字幕等待，和字幕字号滑块的持久化（2026-10-15）。

页面怎么画由 tests/live-bar.test.mjs 钉着（node:vm 里真跑 app.js）；这里钉服务端这一侧：
  * caption 消息带 segment_ms，页面用 ts − e2e_ms − segment_ms 算「这一句最早一个词是多久以前说的」；
    片段时长不知道就不发这个键（页面宁可不写）；手机的白名单照旧不放这两个内部耗时；
  * 字幕等待（说出这一句 → 中控读到的那一版字幕上屏）进遥测，页脚统计行的第一个数；
    违禁词那一档 detect_worst 一个字没动（CLAUDE.md 第六条）；
  * 字幕字号存进 settings.json：合法值才存，坏值不存，重启读得回，没存过就不放这个键（界面保持今天的默认）；
  * 三处写着字号范围的地方（滑块、服务端、页面模块）是同一对数。
"""
import re
from pathlib import Path

import pytest

from app import settings, viewer
from app.telemetry import Telemetry
from tests.helpers import run
from tests.test_pipeline_decoupling import FastTranslator, make_pipeline, result

ROOT = Path(__file__).resolve().parent.parent


# ---- 字号：合法值才认 --------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (34, 34), (20, 20), (60, 60), ("40", 40), (41.4, 41), (41.5, 42), ("34.0", 34),
    (19, None), (61, None), (0, None), (-5, None), (1e9, None),
    (None, None), ("", None), ("abc", None), (True, None), (False, None),
    (float("nan"), None), (float("inf"), None), ([34], None), ({"v": 34}, None),
])
def test_only_sizes_inside_the_slider_range_are_accepted(value, expected):
    assert settings.normalize_caption_size(value) == expected


def test_the_size_range_is_the_same_in_the_slider_the_server_and_the_page_module():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    slider = re.search(r'<input type="range" id="font-size" min="(\d+)" max="(\d+)"', html)
    assert slider, "index.html 里找不到 #font-size"
    ui = (ROOT / "web" / "live-ui.js").read_text(encoding="utf-8")
    page = (int(re.search(r"var CAPTION_SIZE_MIN = (\d+);", ui).group(1)),
            int(re.search(r"var CAPTION_SIZE_MAX = (\d+);", ui).group(1)))
    assert (int(slider.group(1)), int(slider.group(2))) == page
    assert page == (settings.CAPTION_SIZE_MIN, settings.CAPTION_SIZE_MAX)


def test_the_saved_size_reads_back_and_a_damaged_one_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    assert settings.saved_caption_size() is None                 # 没存过
    settings.save_setting("caption_size", 44)
    assert settings.saved_caption_size() == 44
    for bad in ("big", 999, None, True, [44]):                   # 设置文件被手改坏了
        settings.save_setting("caption_size", bad)
        assert settings.saved_caption_size() is None, bad


# ---- 字号：随 Pipeline 存取 ---------------------------------------------------------------------

def test_a_fresh_install_has_no_caption_size_so_the_page_keeps_todays_defaults(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    assert "caption_size" not in server.config


def test_releasing_the_slider_saves_the_size_and_tells_every_open_page(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    run(p.handle_control({"type": "set_caption_size", "value": 44}))
    assert settings.load_settings()["caption_size"] == 44
    assert server.config["caption_size"] == 44
    assert server.of_type("config") == [{"type": "config", "caption_size": 44}]


def test_a_size_outside_the_range_is_ignored_and_nothing_is_written(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    for bad in (5, 200, "huge", None, True, [40], {"v": 40}):
        assert p.handle_control({"type": "set_caption_size", "value": bad}) is None
    assert "caption_size" not in settings.load_settings()
    assert "caption_size" not in server.config
    assert server.of_type("config") == []


def test_the_size_survives_a_restart_which_is_what_a_port_change_used_to_lose(monkeypatch, tmp_path):
    first, _ = make_pipeline(monkeypatch, tmp_path)
    run(first.handle_control({"type": "set_caption_size", "value": "48"}))
    second, server = make_pipeline(monkeypatch, tmp_path)     # 同一份 settings.json，新进程
    assert server.config["caption_size"] == 48


def test_a_damaged_saved_size_does_not_reach_the_page(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    settings.save_setting("caption_size", 9000)
    _, server = make_pipeline(monkeypatch, tmp_path)
    assert "caption_size" not in server.config


def test_changing_the_size_is_not_audit_evidence(monkeypatch, tmp_path):
    """界面偏好不进合规审计：审计的记录类型和字段不为它加一个。"""
    p, _ = make_pipeline(monkeypatch, tmp_path)
    assert p.audit is None
    run(p.handle_control({"type": "set_caption_size", "value": 30}))     # 没有审计文件也不抛
    assert p.audit is None


# ---- caption 消息：片段时长 ---------------------------------------------------------------------

def test_the_caption_carries_the_segment_length_the_page_needs_for_how_long_ago(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, translator=FastTranslator())
    monkeypatch.setattr("app.pipeline.time.time", lambda: 1000.0)
    run(p._emit_original(result("hola amigas"), audio_end_ts=992.0, asr_ms=1100, segment_ms=6400))
    (caption,) = server.of_type("caption")
    assert caption["segment_ms"] == 6400 and caption["e2e_ms"] == 8000 and caption["ts"] == 1000.0
    # 页面的算法（web/live-ui.js captionSpokenAt）：ts − e2e − segment = 最早一个词说出口的时刻
    assert caption["ts"] * 1000 - caption["e2e_ms"] - caption["segment_ms"] == (992.0 - 6.4) * 1000


def test_an_unknown_segment_length_is_left_out_not_made_up(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, translator=FastTranslator())
    run(p._emit_original(result("hola amigas"), audio_end_ts=100.0, asr_ms=1100))
    (caption,) = server.of_type("caption")
    assert "segment_ms" not in caption and "e2e_ms" in caption


def test_the_phone_still_gets_neither_internal_timing():
    msg = {"type": "caption", "id": 1, "ts": 1.0, "original": "hola", "translated": None,
           "translate_state": "pending", "asr_ms": 1100, "e2e_ms": 2300, "segment_ms": 6400}
    out = viewer.filter_payload(msg)
    assert out["original"] == "hola"
    assert not {"asr_ms", "e2e_ms", "segment_ms"} & set(out)
    assert viewer.filter_payload({"type": "config", "caption_size": 40}) is None   # 字号只在电脑页


# ---- 字幕等待 -----------------------------------------------------------------------------------

def test_the_caption_wait_is_the_segment_plus_everything_up_to_the_translation(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, translator=FastTranslator())
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.pipeline.time.time", lambda: clock["now"])

    async def scenario():
        job = await p._emit_original(result("hola amigas"), audio_end_ts=992.0, asr_ms=1100, segment_ms=6000)
        assert p.telemetry.snapshot()["caption_wait"]["n"] == 0      # 原文上屏时还没有「读到译文」这回事
        clock["now"] = 1003.0                                        # 译文又等了 3 秒
        await p._translate_and_update(job)

    run(scenario())
    snap = p.telemetry.snapshot()
    assert snap["caption_wait"]["p50"] == pytest.approx(6000 + 11000)       # 片段 6 s + 音频结束后 11 s
    (update,) = server.of_type("caption_update")
    assert update["e2e_translated_ms"] == 11000


def test_a_line_that_needs_no_translation_waits_exactly_as_long_as_the_alert_does(monkeypatch, tmp_path):
    p, _ = make_pipeline(monkeypatch, tmp_path, translator=None)     # 没有翻译引擎：屏幕上就是原文
    monkeypatch.setattr("app.pipeline.time.time", lambda: 1000.0)
    run(p._emit_original(result("hola amigas"), audio_end_ts=996.0, asr_ms=900, segment_ms=5000))
    snap = p.telemetry.snapshot()
    assert snap["caption_wait"]["p50"] == pytest.approx(9000)
    assert snap["detect_worst"]["p50"] == pytest.approx(snap["caption_wait"]["p50"])


def test_a_failed_or_skipped_translation_adds_no_caption_wait_sample(monkeypatch, tmp_path):
    class Refusing:
        async def translate(self, text, target, source="auto", glossary=None):
            return None

    p, _ = make_pipeline(monkeypatch, tmp_path, translator=Refusing())

    async def scenario():
        job = await p._emit_original(result("hola amigas"), audio_end_ts=100.0, asr_ms=900, segment_ms=5000)
        await p._translate_and_update(job)

    run(scenario())
    assert p.telemetry.snapshot()["caption_wait"] == {"p50": None, "p95": None, "n": 0}


def test_the_alert_latency_figure_is_untouched(monkeypatch, tmp_path):
    """违禁词延迟仍是 片段时长 + e2e（CLAUDE.md 第六条的 KPI），字幕等待是另一条序列。"""
    t = Telemetry()
    t.record_asr(1000, 2000, 5000)
    t.record_caption_wait(12000)
    snap = t.snapshot()
    assert snap["detect_worst"]["p50"] == 7000
    assert snap["caption_wait"]["p50"] == 12000
    t.reset()                                              # 每场重新计数
    assert t.snapshot()["caption_wait"]["n"] == 0


def test_the_demo_does_not_invent_timings():
    """演示模式的假字幕没有真实的音频片段：不带 e2e_ms / segment_ms，页面就不写「约 N 秒前」。"""
    src = (ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
    demo = src[src.index("async def _run_demo_captions"):]
    demo = demo[:demo.index("await asyncio.sleep(2.4)")]
    assert "segment_ms" not in demo and "e2e_ms" not in demo
