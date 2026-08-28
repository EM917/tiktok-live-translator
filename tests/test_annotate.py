"""标注工具的核心裁决逻辑：accept 与 manual 绝不混类、落盘原子、盲复标真的盲。"""

import pytest

from tools.annotate import (build_relabel_batch, compare_relabel,
                            effective_results, load_jsonl, make_record,
                            save_results_atomic)

ROW = {"id": 7, "cluster_id": "c1", "session": "s.jsonl", "seq": 42,
       "streamer": "bella", "bucket": "price_promo", "src": "cuesta 55",
       "fast": "价格是55", "fast_engine": "hymt2",
       "strong": "售价55美元", "strong_engine": "hf.co/tencent/Hy-MT2-7B",
       "fast_withheld": None, "strong_withheld": None}
STAMP = {"guide_hash": "g", "commit": "c", "queue_hash": "q"}


def test_accept_copies_reference_verbatim_and_ignores_client_text():
    """「接受参考」的 target 由服务端复制——客户端塞什么都不认，
    否则「接受」和「人工翻译」就混成一类了。"""
    rec = make_record(ROW, "accept_local", "客户端伪造的内容", STAMP)
    assert rec["target"] == "价格是55"
    assert rec["adopted_engine"] == "hymt2"
    rec2 = make_record(ROW, "accept_strong", None, STAMP)
    assert rec2["target"] == "售价55美元"


def test_accept_refuses_withheld_reference():
    row = dict(ROW, fast=None, fast_withheld="deepl")
    with pytest.raises(ValueError):
        make_record(row, "accept_local", None, STAMP)


def test_manual_requires_a_real_target():
    with pytest.raises(ValueError):
        make_record(ROW, "manual", "   ", STAMP)
    rec = make_record(ROW, "manual", " 55 美元一件 ", STAMP)
    assert rec["target"] == "55 美元一件"
    assert rec["adopted_engine"] is None


def test_gate_actions_carry_no_target():
    for a in ("asr_garbage", "context_required", "profile_only", "skip"):
        assert make_record(ROW, a, "残留文本", STAMP)["target"] is None


def test_every_record_carries_full_provenance():
    rec = make_record(ROW, "skip", None, STAMP)
    for key in ("queue_id", "cluster_id", "session", "seq", "streamer",
                "bucket", "src", "guide_hash", "commit", "queue_hash", "at"):
        assert rec.get(key) is not None or key in rec


def test_atomic_save_roundtrip_and_last_wins(tmp_path):
    p = tmp_path / "res.jsonl"
    r1 = make_record(ROW, "skip", None, STAMP)
    r2 = make_record(ROW, "manual", "改过了", STAMP)
    save_results_atomic(p, [r1, r2])
    eff = effective_results(load_jsonl(p))
    assert eff[7]["action"] == "manual"          # 重标后最后一条生效
    assert not list(tmp_path.glob("*.tmp"))      # 不留临时文件


def test_relabel_batch_is_deterministic_and_blind():
    done = [dict(make_record(dict(ROW, id=i), "manual", "t%d" % i, STAMP))
            for i in range(1, 121)]
    b1 = build_relabel_batch(done)
    b2 = build_relabel_batch(done)
    assert b1 == b2 and len(b1) == 20
    assert all(set(x) == {"blind_id", "queue_id"} for x in b1)   # 不带答案
    assert all(x["queue_id"] <= 100 for x in b1)                 # 只从前100抽


def test_manual_identical_to_reference_is_flagged_not_blocked():
    """复制参考后一字不改点 manual 是合法的人工确认，但必须打标——
    否则「1.8B 可直接当 gold 的比例」会把这类和真·人工翻译混在一起。"""
    rec = make_record(ROW, "manual", "价格是55", STAMP)
    assert rec["manual_equals_reference"] == "fast"
    rec2 = make_record(ROW, "manual", "自己写的", STAMP)
    assert rec2["manual_equals_reference"] is None


def test_relabel_batch_file_is_isolated_per_queue(tmp_path):
    """批次文件名必须带队列标识：固定名字时换一天重建队列，旧批次的
    queue_id 会指向完全不同的句子。"""
    from tools.annotate import relabel_batch_path

    a = relabel_batch_path(tmp_path / "annotation-queue-20260827.jsonl")
    b = relabel_batch_path(tmp_path / "annotation-queue-20260901.jsonl")
    assert a != b
    assert "20260827" in a.name and "20260901" in b.name


def test_second_instance_is_refused_by_lockfile(tmp_path, monkeypatch):
    """第二个实例的旧内存快照整文件重写会静默抹掉第一个实例已落盘的
    记录（实测复现过）——单实例锁宁可拒绝启动。"""
    import json as _json

    import pytest

    from tools.annotate import Annotator

    (tmp_path / "annotation-queue-20260827.jsonl").write_text(
        _json.dumps(dict(ROW, id=1)) + "\n", encoding="utf-8")
    first = Annotator(tmp_path)
    assert first.lock_path.exists()
    with pytest.raises(SystemExit):
        Annotator(tmp_path)
    first.lock_path.unlink()                     # 释放后可再启动
    Annotator(tmp_path)


def test_compare_flags_the_interesting_disagreements():
    a = {1: {"action": "accept_local", "target": "同"},
         2: {"action": "manual", "target": "x"},
         3: {"action": "context_required", "target": None}}
    b = {1: {"action": "accept_local", "target": "同"},
         2: {"action": "accept_local", "target": "y"},
         3: {"action": "manual", "target": "z"}}
    s = compare_relabel(a, b)
    assert s["n"] == 3 and s["action_agree"] == 1
    assert s["target_exact"] == 1
    assert s["manual_vs_accept"] == 1            # 2 号：manual ↔ accept
    assert s["gate_disagree"] == 1               # 3 号：门类动作翻转
    assert len(s["diffs"]) == 2
