"""2026-09-09 全项目扫描在 tools/ 里修掉的 bug，每条一个回归用例。

共同点：输入缺失或异常时**静默给出结论**——回放 gate 在零语料上打 ✅、
「最近一场」选中候选文件、词表缺失按空表出报告、对照没在播被推成「本机有问题」。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


# ---- replay_alerts：空语料 / 空词表不许假通过 ----

def _gate(*extra, cwd=ROOT):
    return subprocess.run([PY, str(ROOT / "tools" / "replay_alerts.py"), *extra],
                          cwd=str(cwd), capture_output=True, text=True, timeout=120)


def test_replay_gate_refuses_an_empty_corpus(tmp_path):
    terms = tmp_path / "terms.txt"
    terms.write_text("curar\n", encoding="utf-8")
    r = _gate("--logs", str(tmp_path / "nologs"), "--terms", str(terms))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "语料为空" in r.stdout and "✅" not in r.stdout


def test_replay_gate_refuses_a_missing_term_list(tmp_path):
    r = _gate("--logs", str(tmp_path), "--terms", str(tmp_path / "nope.txt"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "词表为空" in r.stdout


# ---- retranslate_audit：只认会话日志 ----

def test_latest_log_ignores_candidate_and_queue_files(tmp_path, monkeypatch):
    from tools.retranslate_audit import latest_log
    (tmp_path / "logs").mkdir()
    for name in ("session-20260901-100000.jsonl", "training-candidates-20260902.jsonl",
                 "annotation-queue-20260903.jsonl"):
        (tmp_path / "logs" / name).write_text("{}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert latest_log() == os.path.join("logs", "session-20260901-100000.jsonl")


# ---- build_annotation_queue：半行容错、不覆盖旧队列 ----

def test_read_records_skips_a_torn_last_line(tmp_path):
    from tools.build_annotation_queue import _read_records
    p = tmp_path / "session-x.jsonl"
    p.write_text(json.dumps({"type": "segment", "seq": 1}) + "\n"
                 + '{"type": "segment", "seq": 2, "te', encoding="utf-8")
    assert [r["seq"] for r in _read_records(p)] == [1]


def test_next_queue_path_never_overwrites_and_sorts_last(tmp_path):
    from tools.annotate import latest_queue, results_path
    from tools.build_annotation_queue import next_queue_path
    first = next_queue_path(tmp_path, "20260909")
    first.write_text("", encoding="utf-8")
    second = next_queue_path(tmp_path, "20260909")
    assert first.name == "annotation-queue-20260909.jsonl"
    assert second.name == "annotation-queue-20260909_2.jsonl"
    second.write_text("", encoding="utf-8")
    assert latest_queue(tmp_path) == second                     # 新的排在最后
    assert results_path(second).name == "annotation-results-20260909_2.jsonl"


# ---- annotate：残留锁自动接管，活着的实例仍拒绝 ----

def test_stale_lock_is_taken_over_and_live_lock_is_refused(tmp_path):
    from tools.annotate import acquire_lock
    lock = tmp_path / "x.lock"
    lock.write_text("999999", encoding="utf-8")          # 不存在的 PID
    acquire_lock(lock)
    assert lock.read_text() == str(os.getpid())
    with pytest.raises(SystemExit):
        acquire_lock(lock)                                # 自己还活着 → 拒绝


# ---- collision_audit：词表读不到不许按空表出报告 ----

def test_collision_audit_exits_on_missing_terms(tmp_path, monkeypatch):
    from tools import collision_audit
    monkeypatch.setattr(sys, "argv", ["collision_audit.py", "--terms", str(tmp_path / "nope.txt"),
                                      "--logs", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        collision_audit.main()
    assert "词表为空或读不到" in str(exc.value)


# ---- diagnose_room：用户名查不到 / 对照没在播 ----

def test_diagnose_reports_unknown_username_honestly():
    import importlib.util
    spec = importlib.util.spec_from_file_location("dr", ROOT / "tools" / "diagnose_room.py")
    dr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dr)
    assert dr.classify("no_room", None)[0] == "no_room"
    assert "用户名" in dr.verdict(("no_room", ""), None)
    v = dr.verdict(("withheld", "x"), ("offline", "status=4"))
    assert "配对无效" in v and "本机" not in v                  # 对照没在播 ≠ 本机有问题
    assert "别怪目标房间" in dr.verdict(("withheld", "x"), ("withheld", "y"))


# ---- onboard_streamer：--glossary 真的被解析 ----

def test_glossary_arg_is_parsed_in_any_position():
    from tools.onboard_streamer import glossary_arg
    assert glossary_arg(["--glossary", "old.txt", "logs/x.jsonl"]) == "old.txt"
    assert glossary_arg(["logs/x.jsonl", "--glossary=old.txt"]) == "old.txt"
    assert glossary_arg(["logs/x.jsonl"]) is None
