"""审计文件关掉之后才到的记录：不再静默消失（专家审查 B5；计划审查 P8）。

以前 AuditLog._write 见文件已关直接 return：旧任务过了停止的宽限期才写的译文、重译结果就这么没了，
write_failures 还是 0（asyncio/exp_audit_after_close.py）。现在计数、终端打一行（每场最多一行），本进程
下一场的 session_start 带上 late_records_prev；不追加进已经写了 session_end 的旧文件。

后半是审计格式的字节对比（计划 cross_cutting 7）：已有类型的记录输出逐字节不变，只在新情况下多出新键。
"""
import json

from app.audit import AuditLog

ROOM = "https://www.tiktok.com/@ficticia_tarde/live"


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def old_encode(record):
    """改动前 _write 对正常记录的编码（原样抄下来）。"""
    return (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def test_a_write_after_close_is_counted_and_said_once(tmp_path, capsys):
    a = AuditLog(room_url=ROOM, log_dir=tmp_path / "a")
    a.close(reason="user_stop")
    closed = a.path.read_bytes()
    a.translation(5, "晚到的译文", 1200.0, True, engine="claude")
    a.translation_strong(5, "晚到的重译", 3000.0, True, "claude-sonnet-5-5", "manual")
    assert a.path.read_bytes() == closed                 # 不追加进已经写了 session_end 的文件
    assert a.late_records == 2
    out = capsys.readouterr().out
    assert out.count("这一场的审计文件已经关闭") == 1        # 每场最多一行
    assert "translation" in out and "晚到的译文" not in out  # 只说记录类型，不说内容


def test_each_closed_session_says_it_once_on_its_own(tmp_path, capsys):
    first = AuditLog(room_url=ROOM, log_dir=tmp_path / "1")
    first.close(reason="user_stop")
    second = AuditLog(room_url=ROOM, log_dir=tmp_path / "2")
    second.close(reason="user_stop")
    first.translation(1, "x", 1.0, True)
    second.translation(1, "y", 1.0, True)
    first.translation(2, "z", 1.0, True)
    assert capsys.readouterr().out.count("这一场的审计文件已经关闭") == 2
    assert (first.late_records, second.late_records) == (2, 1)


def test_an_audit_that_never_opened_is_not_a_late_write(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    a = AuditLog(room_url=ROOM, log_dir=blocker / "logs")     # 建不了目录：open_error
    assert a.path is None and a.open_error
    a.translation(1, "x", 1.0, True)
    assert a.late_records == 0
    assert "已经关闭" not in capsys.readouterr().out


def test_a_write_racing_the_close_is_counted_too(tmp_path):
    """等锁的工夫里文件被关了：同样计数，不抛。"""
    a = AuditLog(room_url=ROOM, log_dir=tmp_path)
    a._lock.acquire()
    import threading
    t = threading.Thread(target=a.translation, args=(1, "x", 1.0, True))
    t.start()
    a._fh, fh = None, a._fh                              # close() 在锁里做的那一步
    a._closed = True
    a._lock.release()
    t.join(2)
    fh.close()
    assert a.late_records == 1


# ---- 下一场的 session_start 带上 late_records_prev（按管线算）----

def _pipeline(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app import pipeline as pipeline_mod
    from app import settings
    from app.pipeline import Pipeline
    from app.server import CaptionServer

    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False,
        cookies_browser="none")
    p = Pipeline(args, CaptionServer(port=0))

    async def noop(*a, **k):
        return None

    async def no_facts():
        return {}

    p._login_source = noop
    p._ffmpeg_session_facts = no_facts                   # 这里不关心 ffmpeg，不起子进程
    return p


def _open_session(p):
    import asyncio

    async def scenario():
        await p._begin_session(ROOM)
        if p._stats_task is not None:
            p._stats_task.cancel()
        return p.audit

    return asyncio.run(scenario())


def test_the_next_session_start_reports_the_late_records_once(monkeypatch, tmp_path):
    p = _pipeline(monkeypatch, tmp_path)
    first = _open_session(p)
    first.close(reason="user_stop")
    first.translation(9, "晚到", 1.0, True)              # 旧任务过了宽限期才写
    first.translation(10, "晚到", 1.0, True)
    second = _open_session(p)
    second.close(reason="user_stop")
    third = _open_session(p)
    third.close(reason="user_stop")
    assert rows(second.path)[0]["late_records_prev"] == 2
    assert "late_records_prev" not in rows(third.path)[0]   # 报过一次就不再报
    assert "late_records_prev" not in rows(first.path)[0]


def test_late_records_from_another_pipeline_are_not_reported(monkeypatch, tmp_path):
    other = AuditLog(room_url=ROOM, log_dir=tmp_path / "other")
    other.close()
    other.translation(1, "x", 1.0, True)
    p = _pipeline(monkeypatch, tmp_path)
    mine = _open_session(p)
    mine.close()
    assert "late_records_prev" not in rows(mine.path)[0]


def test_a_session_start_without_late_records_is_byte_for_byte_the_old_one(tmp_path):
    a = AuditLog(room_url=ROOM, log_dir=tmp_path, extra={"app_version": "9.9.9"})
    a.close()
    line = a.path.read_bytes().split(b"\n")[0] + b"\n"
    assert line == old_encode(rows(a.path)[0])
    assert list(rows(a.path)[0]) == ["app_version", "type", "room_url", "started_at", "code_commit",
                                     "glossary_hash", "vocative_hash"]


# ---- viewer_action 的 refused（顺延事项：S7 第 3 条的另一半）----

def test_viewer_action_without_refused_is_byte_for_byte_the_old_record(tmp_path):
    a = AuditLog(room_url=ROOM, log_dir=tmp_path)
    a.viewer_action("retranslate", 12, "192.168.1.20")
    a.close()
    raw = a.path.read_bytes().split(b"\n")
    (row,) = [r for r in rows(a.path) if r["type"] == "viewer_action"]
    old = old_encode({"type": "viewer_action", "at": row["at"], "action": "retranslate", "id": 12,
                      "ip": "192.168.1.20"})
    assert old in [line + b"\n" for line in raw]


def test_viewer_action_can_carry_why_it_was_refused(tmp_path):
    a = AuditLog(room_url=ROOM, log_dir=tmp_path)
    a.viewer_action("retranslate", 12, "192.168.1.20", refused="lagging")
    a.close()
    (row,) = [r for r in rows(a.path) if r["type"] == "viewer_action"]
    assert list(row) == ["type", "at", "action", "id", "ip", "refused"]
    assert row["refused"] == "lagging"
