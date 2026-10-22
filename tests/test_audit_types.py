"""审计记录里混进 json 认不出的类型：不能结束整场，也不能被报成「审计写不进去」。

专家审查 B2（2026-10-21，10-22 补核成立）：写入只接 ValueError，np.float32、np.int64、set、bytes、
自定义对象都抛 TypeError，从 AuditLog._write 冒到拉流那一轮，一段坏记录就让整场以 internal_error
结束；写失败计数还是 0。close() 有同一个缺口：session_end 带上这样的字段时抛出、文件不关、
session_end 也不写。

改成：认得出的换成能写的值（_json_default），实在编不出来的计进 serialize_failures、原位置写一条
audit_record_error 占位——不碰 failing，不补 audit_gap。正常记录的输出必须和以前逐字节相同。
"""
import json
from types import SimpleNamespace

import numpy as np

from app.audit import AuditLog, _finite_json
from app.i18n import L

ROOM = "https://www.tiktok.com/@ficticia_audit/live"


def rows(audit):
    return [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()]


def of_type(audit, kind):
    return [r for r in rows(audit) if r["type"] == kind]


def old_encode(record):
    """改动前 AuditLog._write 的编码（原样抄下来）：正常记录的输出必须和它逐字节相同。"""
    try:
        return (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except ValueError:
        return (json.dumps(_finite_json(record), ensure_ascii=False, allow_nan=False)
                + "\n").encode("utf-8")


class Secret:
    """自定义对象：str() 里带签名地址。认不出的类型只许记类型名，不许借 str(obj) 进审计。"""

    def __str__(self):
        return "https://pull-flv.example.invalid/s.flv?expire=1&sign=NO-LEAK"


def test_each_odd_type_lands_on_disk_as_something_readable(tmp_path):
    a = AuditLog(room_url=ROOM, log_dir=tmp_path)
    a._write({"type": "probe", "v": np.float32(1.5)})
    a._write({"type": "probe", "v": np.int64(3)})
    a._write({"type": "probe", "v": {2, 1}})
    a._write({"type": "probe", "v": {1, "a"}})             # 元素类型混杂：按 repr 排，不抛
    a._write({"type": "probe", "v": b"xyz"})
    a._write({"type": "probe", "v": Secret()})
    a._write({"type": "probe", "v": np.float32("nan")})
    a._write({"type": "probe", "v": np.bool_(True)})
    a.close(reason="user_stop")
    values = [r["v"] for r in of_type(a, "probe")]
    assert values == [1.5, 3, [1, 2], ["a", 1], "<bytes 3>", "<Secret>", None, True]
    raw = a.path.read_text(encoding="utf-8")
    assert "NO-LEAK" not in raw and "sign=" not in raw    # 只有类型名
    assert a.serialize_failures == 0                       # 都换成了能写的值，不算编不出来
    assert a.write_failures == 0 and a.lost_records == 0 and not a.failing
    assert "serialize_failures" not in of_type(a, "session_end")[0]


def test_nan_together_with_a_set_is_written_too(tmp_path):
    """_finite_json 那一路的第二次 json.dumps 也带 default：NaN 加 set 不再抛 TypeError。"""
    a = AuditLog(room_url=ROOM, log_dir=tmp_path)
    a._write({"type": "probe", "x": float("nan"), "s": {3, 1}, "n": np.int64(2)})
    a.close()
    (row,) = of_type(a, "probe")
    assert row == {"type": "probe", "x": None, "s": [1, 3], "n": 2}


def test_a_segment_whose_rejections_carry_a_set_no_longer_raises(tmp_path):
    """exp_bad_record_kills_round.py 的那一段：rejected 里有个 set。以前 TypeError 冒出 _write，整场结束。"""
    a = AuditLog(room_url=ROOM, log_dir=tmp_path)
    result = SimpleNamespace(language="es", text="hola", raw_text="hola",
                             rejected=[{"text": "x", "reason": {"odd"}}])
    a.segment(3, result, audio_end_ts=12.0, asr_ms=80.0, hits=[])
    a.close(reason="offline")
    (seg,) = of_type(a, "segment")
    assert seg["rejected"] == [{"text": "x", "reason": ["odd"]}]
    assert a.write_failures == 0


def test_a_record_that_still_cannot_be_encoded_leaves_a_placeholder_not_an_outage(tmp_path):
    """键不是字符串、循环引用：认不出的类型换完了还是编不出来。计 serialize_failures、写占位，
    failing 保持 False——不能下一跳就挂出红色「审计写不进去」、补一条 audit_gap。"""
    a = AuditLog(room_url=ROOM, log_dir=tmp_path)
    loop = []
    loop.append(loop)
    a._write({"type": "probe", "keys": {(1, 2): "tuple key"}})
    a._write({"type": "translation", "seq": 1, "loop": loop})
    a._write({"type": "probe_after", "ok": True})          # 下一条照常写，前面不冒 audit_gap
    assert not a.failing and a.write_failures == 0 and a.lost_records == 0
    assert a.unwritten() is None
    a.close(reason="user_stop")
    types = [r["type"] for r in rows(a)]
    assert types == ["session_start", "audit_record_error", "audit_record_error", "probe_after",
                     "session_end"]
    first, second = of_type(a, "audit_record_error")
    assert first["orig_type"] == "probe" and first["bad_fields"] == ["dict"]
    assert second["orig_type"] == "translation" and second["bad_fields"] == ["list"]
    assert "at" in first
    assert "tuple key" not in a.path.read_text(encoding="utf-8")
    assert "audit_gap" not in types
    assert a.serialize_failures == 2
    assert of_type(a, "session_end")[0]["serialize_failures"] == 2


def test_close_with_bad_fields_still_writes_session_end_and_closes_the_file(tmp_path):
    a = AuditLog(room_url=ROOM, log_dir=tmp_path)
    a.close(reason="offline", budget=np.int64(7), tags={"b", "a"}, blob=b"\x00\x01",
            who=Secret(), odd={(1,): "x"})
    assert a._fh is None                                     # 文件关上了
    (end,) = of_type(a, "session_end")
    assert end["reason"] == "offline"
    assert end["budget"] == 7 and end["tags"] == ["a", "b"] and end["blob"] == "<bytes 2>"
    assert end["who"] == "<Secret>"
    assert end["odd"] == "<dict>"                            # 编不出来的字段换成类型名，session_end 照写
    assert end["serialize_failures"] == 1                    # session_end 自己这一条也算进去
    assert not a.failing and a.write_failures == 0


def test_close_from_end_fields_with_bad_values(tmp_path):
    """汇总（end_fields）里带了编不出来的值：session_end 照写。"""
    a = AuditLog(room_url=ROOM, log_dir=tmp_path)
    a.end_fields = lambda: {"asr_risk": {"n": np.int64(3), "flags": {"x"}}}
    a.close(reason="user_stop")
    (end,) = of_type(a, "session_end")
    assert end["asr_risk"] == {"n": 3, "flags": ["x"]}
    assert a._fh is None


def test_close_always_releases_the_file_handle(tmp_path, monkeypatch):
    """写 session_end 时出了什么错，文件也要在 finally 里关上（以前 TypeError 时文件一直开着）。"""
    a = AuditLog(room_url=ROOM, log_dir=tmp_path)
    fh = a._fh

    def boom(record):
        raise TypeError("unexpected")

    monkeypatch.setattr(a, "_write", boom)
    a.close(reason="user_stop")
    assert a._fh is None and fh.closed


def test_a_bad_session_start_extra_keeps_the_header(tmp_path):
    """会话头不换成占位：按会话切审计的工具认 session_start，只把编不出来的字段换成类型名。"""
    a = AuditLog(room_url=ROOM, log_dir=tmp_path,
                 extra={"engine_opts": {("k",): 1}, "rate": np.float32(0.5)})
    a.close(reason="user_stop")
    (start,) = of_type(a, "session_start")
    assert start["engine_opts"] == "<dict>" and start["rate"] == 0.5
    assert start["room_url"] == ROOM
    assert a.serialize_failures == 1


def test_normal_records_are_byte_identical_to_the_old_encoder(tmp_path):
    """G8 和现有对比都依赖这一点：json 认得的类型一律走老路，输出逐字节不变（含 NaN 换 null 那一路、
    中英成对的字符串、中文和 emoji）。"""
    written = []

    class Spy(AuditLog):
        def _write(self, record):
            written.append(record)
            super()._write(record)

    a = Spy(room_url=ROOM, log_dir=tmp_path, extra={"streamer": "ficticia_audit",
                                                     "translator_active": "hymt2"})
    result = SimpleNamespace(language="es", text="Hola a todos 👋", raw_text="hola a todos",
                             rejected=[{"text": "eh", "reason": "低于门限", "avg_logprob": float("nan")}])
    a.segment(1, result, audio_end_ts=10.12345, asr_ms=812.44, hits=[{"term": "x", "tier": 1}],
              asr={"risk": 0.2})
    a.translation(1, "大家好", 120.0, True, engine="hymt2")
    a.translation_strong(1, None, 2300.0, False, "fake-7b", "manual", error="refusal")
    a.stream_audio(300.0, 42, 1234.5)
    a.clock_gap(1700000000.0, 1700000100.0, 100.0, 100.0, 0.0, True)
    a.coverage("low", 0.4, 600.0, 240.0, 3)
    a._write({"type": "notice_probe", "text": L("改用本地引擎", "Switched to the local engine"),
              "inf": float("inf"), "n": None, "ok": False, "list": [1, 2.5, "三"]})
    a.close(reason="offline", silent=True, budget=3)
    expected = b"".join(old_encode(r) for r in written)
    assert a.path.read_bytes() == expected
    assert a.serialize_failures == 0
