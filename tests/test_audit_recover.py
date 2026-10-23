"""启动时给上一次没写结尾的审计补一条 session_end（专家审查 B5，app/audit_recover.py）。

只追加、原有字节一个不动；15 分钟内还在写的、端口上有本程序在跑时、D16 范围外的历史文件都不碰；
补过的再启动不重复补。
"""
import json
import os
import time

import pytest

from app import audit_recover as ar

NOW = 1_800_000_000.0                 # 固定的「现在」：mtime 都按它摆
SINCE = NOW - 7 * 86400               # 这个功能上线的时刻（D16 的范围起点）
OLD = NOW - 3600                      # 一小时前：早于 15 分钟、晚于上线时刻


def write(path, records, tail=b"", mtime=OLD):
    data = b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8") for r in records) + tail
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return data


START = {"type": "session_start", "room_url": "https://www.tiktok.com/@ficticia_rec/live"}
SEG = {"type": "segment", "seq": 1, "text": "hola"}
TRANS = {"type": "translation", "seq": 1, "translated": "你好", "ok": True}


def lines(path):
    return path.read_bytes().split(b"\n")


def recover(tmp_path, **kw):
    kw.setdefault("since", SINCE)
    kw.setdefault("now", NOW)
    return ar.recover_unclosed(tmp_path, **kw)


def test_a_file_without_an_end_gets_one_appended_and_keeps_every_original_byte(tmp_path):
    f = tmp_path / "session-20260101-120000.jsonl"
    before = write(f, [START, SEG, TRANS])
    report = recover(tmp_path)
    assert report.recovered == [f.name]
    after = f.read_bytes()
    assert after.startswith(before)                     # 只追加
    added = json.loads(after[len(before):].decode("utf-8"))
    assert added == {"type": "session_end",
                     "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(OLD)),
                     "reason": "unclean_exit", "recovered": True,
                     "recovered_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW))}
    assert after.endswith(b"\n") and after.count(b"\n") == before.count(b"\n") + 1


@pytest.mark.parametrize("last", [{"type": "session_end", "reason": "user_stop"},
                                  {"type": "window_closed", "at": "2026-01-01T12:00:00.000"}])
def test_a_file_that_ends_with_session_end_or_window_closed_is_not_touched(tmp_path, last):
    f = tmp_path / "session-20260101-120000.jsonl"
    before = write(f, [START, SEG, last])
    report = recover(tmp_path)
    assert report.recovered == []
    assert f.read_bytes() == before and f.stat().st_mtime == OLD


def test_an_empty_file_or_one_with_only_half_a_line_is_not_touched(tmp_path):
    empty = tmp_path / "session-20260101-120000.jsonl"
    write(empty, [])
    half = tmp_path / "session-20260101-120001.jsonl"
    write(half, [], tail=b'{"type": "session_st')
    assert recover(tmp_path).recovered == []
    assert empty.read_bytes() == b"" and half.read_bytes() == b'{"type": "session_st'


def test_a_half_written_last_line_is_kept_and_the_new_record_gets_its_own_line(tmp_path):
    """崩溃留下的半行：先补一个换行，新记录不和它粘成一行坏 JSON。半行本身一个字节不动。"""
    f = tmp_path / "session-20260101-120000.jsonl"
    half = b'{"type": "translation", "seq": 2, "transl'
    before = write(f, [START, SEG], tail=half)
    assert recover(tmp_path).recovered == [f.name]
    after = f.read_bytes()
    assert after.startswith(before)
    rows = lines(f)
    assert rows[-1] == b""                               # 文件以换行结尾
    assert rows[-3] == half                              # 半行原样留着，自成一行
    for raw in rows[:-3] + [rows[-2]]:                   # 除了那半行，每一行都是合法 JSON
        json.loads(raw)
    assert json.loads(rows[-2])["reason"] == "unclean_exit"


def test_the_last_complete_record_decides_even_after_a_half_line(tmp_path):
    """半行前面那一条已经是 session_end：不补（半行是关文件之后不会有的，但也不能因此补出第二条结尾）。"""
    f = tmp_path / "session-20260101-120000.jsonl"
    before = write(f, [START, {"type": "session_end", "reason": "offline"}], tail=b'{"type"')
    assert recover(tmp_path).recovered == []
    assert f.read_bytes() == before


def test_a_file_written_in_the_last_15_minutes_is_not_touched(tmp_path):
    """另一个 --no-open 实例可能正在写它：活着的场次每 5 分钟至少写一条，不会比 15 分钟更旧。"""
    f = tmp_path / "session-20260101-120000.jsonl"
    before = write(f, [START, SEG], mtime=NOW - ar.QUIET_SEC + 30)
    report = recover(tmp_path)
    assert report.recovered == [] and report.skipped_recent == 1
    assert f.read_bytes() == before


def test_nothing_is_touched_while_the_app_answers_on_its_port(tmp_path):
    f = tmp_path / "session-20260101-120000.jsonl"
    before = write(f, [START, SEG])
    report = recover(tmp_path, others_running=lambda: True)
    assert report.blocked and report.recovered == []
    assert f.read_bytes() == before
    report = recover(tmp_path, others_running=lambda: False)
    assert report.recovered == [f.name]


def test_a_port_probe_that_fails_counts_as_someone_running(tmp_path):
    f = tmp_path / "session-20260101-120000.jsonl"
    before = write(f, [START, SEG])

    def broken():
        raise OSError("probe")

    report = recover(tmp_path, others_running=broken)
    assert report.blocked and f.read_bytes() == before


def test_history_from_before_the_feature_shipped_is_not_touched(tmp_path):
    """D16（用户 2026-10-22）：只补上线之后产生的文件，历史的一个字节都不动。"""
    old = tmp_path / "session-20250101-120000.jsonl"
    before = write(old, [START, SEG, TRANS], mtime=SINCE - 1)
    new = tmp_path / "session-20260101-120000.jsonl"
    write(new, [START, SEG])
    report = recover(tmp_path)
    assert report.recovered == [new.name] and report.skipped_before == 1
    assert old.read_bytes() == before and old.stat().st_mtime == SINCE - 1


def test_a_second_start_does_not_append_a_second_end(tmp_path):
    f = tmp_path / "session-20260101-120000.jsonl"
    write(f, [START, SEG])
    assert recover(tmp_path).recovered == [f.name]
    once = f.read_bytes()
    os.utime(f, (OLD, OLD))
    assert recover(tmp_path).recovered == []
    assert f.read_bytes() == once


def test_without_a_since_nothing_is_recovered(tmp_path):
    f = tmp_path / "session-20260101-120000.jsonl"
    before = write(f, [START, SEG])
    report = recover(tmp_path, since=None)
    assert report.disabled and f.read_bytes() == before


def test_only_session_files_are_looked_at(tmp_path):
    other = tmp_path / "bootstrap-20260101.jsonl"
    before = write(other, [START, SEG])
    assert recover(tmp_path).scanned == 0
    assert other.read_bytes() == before


def test_a_long_tail_is_read_back_to_the_last_complete_record(tmp_path, monkeypatch):
    """最后一条比读末尾的那一截还长：整份读一遍再判断，不误判成「一条都没有」。"""
    monkeypatch.setattr(ar, "TAIL_BYTES", 64)
    f = tmp_path / "session-20260101-120000.jsonl"
    long_end = {"type": "session_end", "reason": "offline", "pad": "x" * 500}
    before = write(f, [START, long_end])
    assert recover(tmp_path).recovered == []
    assert f.read_bytes() == before
    g = tmp_path / "session-20260101-120001.jsonl"
    write(g, [START, dict(SEG, pad="y" * 500)])
    assert recover(tmp_path).recovered == [g.name]


@pytest.mark.skipif(ar.fcntl is None, reason="没有 flock 的平台（Windows）")
def test_a_file_another_process_holds_the_lock_on_is_skipped(tmp_path):
    f = tmp_path / "session-20260101-120000.jsonl"
    before = write(f, [START, SEG])
    with open(f, "ab") as holder:
        ar.fcntl.flock(holder.fileno(), ar.fcntl.LOCK_EX)
        report = recover(tmp_path)
        assert report.recovered == [] and report.skipped_locked == 1
    assert f.read_bytes() == before


# ---- D16 的范围起点：第一次跑到时记进 settings ----

class FakeSettings:
    def __init__(self, data=None, writable=True):
        self.data = dict(data or {})
        self.writable = writable

    def load(self):
        return dict(self.data)

    def save(self, key, value):
        if self.writable:          # app/settings.save_setting 写不进去时静默忽略
            self.data[key] = value


def test_the_first_run_records_now_and_recovers_nothing_older(tmp_path):
    s = FakeSettings()
    old = tmp_path / "session-20260101-120000.jsonl"
    before = write(old, [START, SEG])
    report = ar.recover_at_startup(tmp_path, s.load, s.save, NOW)
    assert s.data[ar.SINCE_KEY] == NOW
    assert report.recovered == [] and report.skipped_before == 1
    assert old.read_bytes() == before


def test_later_runs_keep_the_first_moment(tmp_path):
    s = FakeSettings({ar.SINCE_KEY: SINCE})
    assert ar.recover_since(s.load, s.save, NOW) == SINCE
    assert s.data[ar.SINCE_KEY] == SINCE


@pytest.mark.parametrize("bad", [None, "yesterday", True, -1, float("nan")])
def test_a_missing_or_broken_moment_is_recorded_afresh(bad):
    s = FakeSettings({ar.SINCE_KEY: bad} if bad is not None else {})
    assert ar.recover_since(s.load, s.save, NOW) == NOW
    assert s.data[ar.SINCE_KEY] == NOW


def test_when_the_moment_cannot_be_saved_nothing_is_recovered(tmp_path):
    s = FakeSettings(writable=False)
    f = tmp_path / "session-20260101-120000.jsonl"
    before = write(f, [START, SEG])
    report = ar.recover_at_startup(tmp_path, s.load, s.save, NOW)
    assert report.disabled and f.read_bytes() == before


def test_the_module_holds_no_chinese_literals():
    """新模块不放中文字面量（计划 cross_cutting 6）：要打印的中文交给 main.py。"""
    import ast
    import re

    tree = ast.parse(open(ar.__file__, encoding="utf-8").read())
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)}
    cjk = re.compile(r"[一-鿿]")
    offenders = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                 and isinstance(n.value, str) and id(n) not in docs and cjk.search(n.value)]
    assert offenders == []
