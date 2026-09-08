"""tools/diagnose_room.py：拿不到流地址时的诊断顺序——先日志、再配对、不猜原因。

这些规则是 2026-09-05/06 一天之内三次误判（年龄→限流→被我们打坏）换来的，
测试把它们钉死：0 段的会话必须被算进去、对照必须是最近成功过的房间、
配对结论的每个分支都不出现原因标签、pytest 里绝不联网。"""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("diagnose_room", ROOT / "tools" / "diagnose_room.py")
dr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dr)


def _session(dirpath, name, streamer, segments, started, resolves=()):
    lines = [json.dumps({"type": "session_start", "streamer": streamer,
                         "room_url": "https://www.tiktok.com/@{}/live".format(streamer),
                         "started_at": started})]
    lines += [json.dumps(dict(r, type="resolve")) for r in resolves]
    lines += [json.dumps({"type": "segment", "seq": i, "text": "hola"}) for i in range(segments)]
    lines.append(json.dumps({"type": "session_end"}))
    (dirpath / "session-{}.jsonl".format(name)).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_history_counts_zero_segment_sessions(tmp_path):
    """0 段的会话就是「从来没成功过」的证据，绝不能像 corpus() 那样过滤掉。"""
    _session(tmp_path, "1", "gated", 0, "2026-09-05T10:16:07")
    _session(tmp_path, "2", "gated", 0, "2026-09-05T13:17:45",
             resolves=[{"attempt": 1, "of": 3, "ok": False, "kind": "browser_only", "ms": 43000,
                        "layers": [{"layer": "官方接口", "outcome": "browser_only", "ms": 5000}]}])
    _session(tmp_path, "3", "bella", 970, "2026-09-04T14:53:52")
    h = dr.history_by_streamer(tmp_path)
    assert h["gated"]["sessions"] == 2 and h["gated"]["ok_sessions"] == 0
    assert h["gated"]["last_started"] == "2026-09-05T13:17:45"
    assert h["gated"]["last_resolve"][0]["kind"] == "browser_only"
    assert h["bella"]["ok_sessions"] == 1 and h["bella"]["segments"] == 970


def test_history_falls_back_to_room_url_for_streamer(tmp_path):
    p = tmp_path / "session-x.jsonl"
    p.write_text(json.dumps({"type": "session_start",
                             "room_url": "https://www.tiktok.com/@daisy/live",
                             "started_at": "t"}) + "\n", encoding="utf-8")
    assert "daisy" in dr.history_by_streamer(tmp_path)


def test_pick_control_prefers_latest_streamer_whose_last_session_worked(tmp_path):
    _session(tmp_path, "1", "gated", 0, "2026-09-06T22:14:01")
    _session(tmp_path, "2", "bella", 970, "2026-09-04T14:53:52")
    _session(tmp_path, "3", "daisy", 2672, "2026-08-27T19:22:19")
    _session(tmp_path, "4", "flaky", 500, "2026-09-01T10:00:00")
    _session(tmp_path, "5", "flaky", 0, "2026-09-06T09:00:00")     # 最近一场失败：不当对照
    h = dr.history_by_streamer(tmp_path)
    assert dr.pick_control(h, exclude="gated") == "bella"
    assert dr.pick_control(h, exclude="bella") == "daisy"
    assert dr.pick_control({"gated": h["gated"]}, exclude="gated") is None


def test_classify_observations():
    withheld = {"status_code": 4003110, "data": {"prompts": ""}}
    assert dr.classify(2, withheld)[0] == "withheld"
    assert dr.classify(2, {"status_code": 0, "data": {"prompts": ""}})[0] == "withheld"
    ok = {"status_code": 0, "data": {"status": 2, "stream_url": {"flv_pull_url": {"HD1": "u"}}}}
    assert dr.classify(2, ok)[0] == "ok"
    assert dr.classify(4, ok)[0] == "offline"                  # 房间状态接口先说没播
    assert dr.classify(2, {"status_code": 0, "data": {"status": 4}})[0] == "offline"
    assert dr.classify(2, None)[0] == "error"
    assert dr.classify(2, {"status_code": 0, "data": {"status": 2}})[0] == "error"
    assert dr.classify(2, {"status_code": 500, "data": {}})[0] == "error"


@pytest.mark.parametrize("target,control", [
    (("withheld", "x"), ("ok", "y")),
    (("withheld", "x"), ("withheld", "y")),
    (("withheld", "x"), ("offline", "y")),
    (("ok", "x"), ("ok", "y")),
    (("offline", "x"), ("ok", "y")),
    (("error", "x"), ("ok", "y")),
    (("withheld", "x"), ("error", "y")),
    (("withheld", "x"), None),
])
def test_verdict_never_names_a_cause(target, control):
    """结论只能描述观察和可做的事。这些词每一个都曾被当成原因写进过代码或对话。"""
    v = dr.verdict(target, control)
    for banned in ("年龄限制", "被限流", "IP 限流", "封禁", "打坏", "多半"):
        assert banned not in v.replace("不要贴年龄/限流/封禁之类的标签", ""), (banned, v)


def test_verdict_rules():
    assert "房间维度" in dr.verdict(("withheld", ""), ("ok", ""))
    assert "别怪目标房间" in dr.verdict(("withheld", ""), ("withheld", ""))
    assert "不能下任何结论" in dr.verdict(("withheld", ""), None)
    assert "配对无效" in dr.verdict(("withheld", ""), ("error", "x"))
    assert "解析之后" in dr.verdict(("ok", ""), ("ok", ""))
    assert "没开播" in dr.verdict(("offline", "status=4"), ("ok", ""))


def test_probe_refuses_to_touch_the_network_under_pytest():
    with pytest.raises(RuntimeError):
        asyncio.run(dr.probe("anyone"))


def test_history_only_mode_makes_no_requests(tmp_path, capsys, monkeypatch):
    _session(tmp_path, "1", "gated", 0, "2026-09-05T10:16:07")

    async def boom(user):
        raise AssertionError("不该联网")

    monkeypatch.setattr(dr, "probe", boom)
    assert dr.main(["@gated", "--history-only", "--logs", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "一次字幕都没出过" in out
