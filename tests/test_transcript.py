"""字幕记录的写入与读取（app/transcript.py）：一场一个文件、写一条落一条、崩溃只丢半行、
写失败绝不抛给直播。导出格式见 tests/test_transcript_export.py，接进 Pipeline 见
tests/test_pipeline_transcript.py。

全部用夹具写临时目录，不碰真实的 transcripts/（tests/conftest.py 的 autouse 夹具也兜着）。
"""
import errno
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from app import transcript as tr
from app.transcript import (TranscriptWriter, latest_transcript, list_transcripts, read_transcript,
                            safe_handle)

T0 = 1_760_000_000          # 2025-10-09 的某个时刻；只用来排顺序，不比较钟面


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def writer(tmp_path, streamer="luna_demo", now=T0, **kw):
    return TranscriptWriter(streamer, "zh-CN", directory=tmp_path, now=now, **kw)


# ---- 文件名与文件头 ---------------------------------------------------------------------------

def test_the_file_is_named_by_start_time_and_streamer(tmp_path):
    w = writer(tmp_path)
    stamp = datetime.fromtimestamp(T0).strftime("%Y%m%d-%H%M%S")
    assert w.path == tmp_path / "transcript-{}-luna_demo.jsonl".format(stamp)
    assert w.path.exists()


def test_the_header_is_the_first_record_and_names_the_streamer_and_target(tmp_path):
    w = writer(tmp_path)
    head = rows(w.path)[0]
    assert (head["type"], head["v"], head["streamer"], head["target_lang"]) == \
        ("transcript_start", tr.FORMAT_VERSION, "luna_demo", "zh-CN")
    assert datetime.fromisoformat(head["started_at"]).timestamp() == pytest.approx(T0, abs=0.001)


def test_two_sessions_started_in_the_same_second_get_two_files(tmp_path):
    a, b = writer(tmp_path), writer(tmp_path)
    assert a.path != b.path and b.path.name.endswith("-luna_demo-2.jsonl")
    a.caption(1, T0, "es", "primera")
    b.caption(1, T0, "es", "segunda")
    assert [r["original"] for r in rows(a.path)[1:]] == ["primera"]
    assert [r["original"] for r in rows(b.path)[1:]] == ["segunda"]


@pytest.mark.parametrize("streamer, expected", [
    ("luna_demo", "luna_demo"),
    ("luna.demo-2", "luna.demo-2"),
    ("", "stream"),                          # 直接流地址没有主播名
    (None, "stream"),
    ("../../etc/passwd", "_.._etc_passwd"),   # 路径分隔符不进文件名，开头的点也去掉
    ("..", "stream"),
    ("a b\\c:d", "a_b_c_d"),
    ("x" * 100, "x" * 40),
])
def test_the_streamer_in_the_file_name_cannot_escape_the_folder(tmp_path, streamer, expected):
    assert safe_handle(streamer) == expected
    w = writer(tmp_path, streamer=streamer)
    assert w.path.parent == tmp_path and w.path.name.endswith("-{}.jsonl".format(expected))


# ---- 写一条落一条 -----------------------------------------------------------------------------

def test_records_are_on_disk_before_the_session_is_closed(tmp_path):
    """崩溃安全的前提：不等 close()、不等缓冲区刷新，写过的就在盘上。"""
    w = writer(tmp_path)
    w.caption(1, T0 + 1, "es", "Hola a todos")
    w.comment("c1", T0 + 2, "ana_demo", "hola!")
    on_disk = rows(w.path)             # 句柄还开着，另开一个读
    assert [r["type"] for r in on_disk] == ["transcript_start", "caption", "comment"]
    assert w._fh is not None


def test_records_keep_the_order_they_were_written_in(tmp_path):
    w = writer(tmp_path)
    w.caption(1, T0 + 1, "es", "uno")
    w.comment("c1", T0 + 2, "ana_demo", "dos")
    w.translation(1, T0 + 3, "一", strong=False)
    w.caption(2, T0 + 4, "es", "tres")
    w.comment_translation("c1", "二")
    w.close(T0 + 5)
    assert [r["type"] for r in rows(w.path)] == [
        "transcript_start", "caption", "comment", "translation", "caption",
        "comment_translation", "transcript_end"]
    parsed = read_transcript(w.path)
    assert [(kind, getattr(item, "original", None) or item.text) for kind, item in parsed.entries] == \
        [("caption", "uno"), ("comment", "dos"), ("caption", "tres")]


def test_every_record_carries_the_fields_the_export_needs(tmp_path):
    w = writer(tmp_path)
    w.caption(7, T0 + 1, "es", "Hola")
    w.translation(7, T0 + 2, "你好", strong=True)
    w.comment("c9", T0 + 3, "ana_demo", "buenas")
    w.comment_translation("c9", "你好呀")
    caption, translation, comment, comment_tr = rows(w.path)[1:]
    assert set(caption) == {"type", "seq", "time", "src_lang", "original"}
    assert (caption["seq"], caption["src_lang"], caption["original"]) == (7, "es", "Hola")
    assert set(translation) == {"type", "seq", "time", "translated", "quality"}
    assert (translation["seq"], translation["translated"], translation["quality"]) == (7, "你好", "strong")
    assert set(comment) == {"type", "id", "time", "user", "text"}
    assert (comment["id"], comment["user"], comment["text"]) == ("c9", "ana_demo", "buenas")
    assert comment_tr == {"type": "comment_translation", "id": "c9", "translated": "你好呀"}


def test_a_caption_time_is_an_iso_local_time_with_offset_and_milliseconds(tmp_path):
    w = writer(tmp_path)
    w.caption(1, 1_760_000_003.2, "es", "Hola")
    stamp = rows(w.path)[1]["time"]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.utcoffset() is not None
    assert parsed.timestamp() == pytest.approx(1_760_000_003.2, abs=0.001)
    assert stamp.split(".")[1][:3].isdigit()


def test_a_fast_translation_then_a_strong_one_both_land_and_the_strong_one_wins(tmp_path):
    w = writer(tmp_path)
    w.caption(1, T0 + 1, "es", "Hoy hay descuento")
    w.translation(1, T0 + 2, "今天有折扣", strong=False)
    w.translation(1, T0 + 9, "今天全场有折扣", strong=True)
    assert [r["quality"] for r in rows(w.path) if r["type"] == "translation"] == ["fast", "strong"]
    cap = read_transcript(w.path).captions[0]
    assert (cap.translated, cap.quality) == ("今天全场有折扣", "strong")


def test_a_late_fast_translation_never_replaces_a_strong_one_when_read_back(tmp_path):
    """界面上的规则（Pipeline._publish_translation）是低等级不覆盖高等级；读回来时同样守着，
    即使文件里有一条先强后快的记录。"""
    w = writer(tmp_path)
    w.caption(1, T0 + 1, "es", "Hoy hay descuento")
    w.translation(1, T0 + 2, "强译", strong=True)
    w.translation(1, T0 + 3, "快译", strong=False)
    cap = read_transcript(w.path).captions[0]
    assert (cap.translated, cap.quality) == ("强译", "strong")


def test_comments_and_their_translations_are_read_back_together(tmp_path):
    w = writer(tmp_path)
    w.comment("c1", T0 + 1, "ana_demo", "hola amiga")
    w.comment_translation("c1", "你好朋友")
    w.comment_translation("c1", "你好，朋友")             # 重译：后写的算
    (c,) = read_transcript(w.path).comments
    assert (c.id, c.user, c.text, c.translated) == ("c1", "ana_demo", "hola amiga", "你好，朋友")


def test_a_translation_for_a_caption_this_session_never_wrote_is_not_recorded(tmp_path):
    """上一场迟到的译文（序号在 Pipeline 里不换场归零，不会撞号，但也不该串进这一场）。"""
    w = writer(tmp_path)
    w.caption(5, T0 + 1, "es", "cinco")
    w.translation(4, T0 + 2, "四", strong=False)
    w.comment_translation("old-comment", "旧弹幕")
    assert [r["type"] for r in rows(w.path)] == ["transcript_start", "caption"]


def test_empty_text_is_not_recorded(tmp_path):
    w = writer(tmp_path)
    w.caption(1, T0, "es", "")
    w.comment("c1", T0, "ana_demo", "")
    w.comment("", T0, "ana_demo", "hola")
    w.translation(1, T0, "", strong=False)
    assert [r["type"] for r in rows(w.path)] == ["transcript_start"]


def test_close_writes_the_end_record_once_and_is_idempotent(tmp_path):
    w = writer(tmp_path)
    w.close(T0 + 60)
    w.close(T0 + 90)
    w.caption(1, T0 + 100, "es", "tarde")             # 关了之后再记：空操作，不抛
    types = [r["type"] for r in rows(w.path)]
    assert types == ["transcript_start", "transcript_end"]
    assert read_transcript(w.path).ended is not None


def test_text_that_cannot_be_encoded_is_replaced_not_fatal(tmp_path):
    w = writer(tmp_path)
    w.caption(1, T0, "es", "hola \ud800 mundo")          # 单独的代理字符，utf-8 编不出来
    w.caption(2, T0 + 1, "es", "siguiente")
    parsed = read_transcript(w.path)
    assert [c.original for c in parsed.captions][1] == "siguiente"
    assert len(parsed.captions) == 2


# ---- 崩溃留下的文件也能读 ----------------------------------------------------------------------

def test_a_file_that_was_never_closed_reads_back_everything_that_was_written(tmp_path):
    w = writer(tmp_path)
    w.caption(1, T0 + 1, "es", "Hola")
    w.translation(1, T0 + 2, "你好", strong=False)
    w.comment("c1", T0 + 3, "ana_demo", "hola")
    parsed = read_transcript(w.path)            # 模拟进程被杀：没有 transcript_end
    assert parsed.ended is None
    assert [(c.original, c.translated) for c in parsed.captions] == [("Hola", "你好")]
    assert [c.text for c in parsed.comments] == ["hola"]


def test_a_half_written_last_line_is_skipped_and_everything_before_it_survives(tmp_path):
    w = writer(tmp_path)
    w.caption(1, T0 + 1, "es", "Hola")
    w.caption(2, T0 + 2, "es", "Adiós")
    with w.path.open("ab") as fh:                # 崩溃时正好写到一半：没有换行，JSON 不完整
        fh.write('{"type":"caption","seq":3,"time":"2025-10-09T01:53'.encode("utf-8"))
    parsed = read_transcript(w.path)
    assert [c.original for c in parsed.captions] == ["Hola", "Adiós"]


def test_a_line_cut_in_the_middle_of_a_multibyte_character_is_skipped(tmp_path):
    w = writer(tmp_path)
    w.caption(1, T0 + 1, "es", "Hola")
    line = json.dumps({"type": "translation", "seq": 1, "translated": "你好世界"},
                      ensure_ascii=False).encode("utf-8")
    with w.path.open("ab") as fh:
        fh.write(line[:-4])                       # 切在「世」或「界」的中间
    assert read_transcript(w.path).captions[0].translated == ""


def test_junk_lines_are_ignored_without_hiding_the_good_ones(tmp_path):
    w = writer(tmp_path)
    w.caption(1, T0 + 1, "es", "Hola")
    junk = (b"\n\nnot json at all\n[1, 2, 3]\n\"just a string\"\n\xff\xfe\n"
            b'{"type": "from_the_future", "x": 1}\n'
            b'{"type": "caption", "seq": "one", "original": "bad seq", "time": "x"}\n')
    w._fh.write(junk)                    # 经写入器自己的句柄写：另开句柄追加会被它下一次写覆盖
    w._pos += len(junk)
    w.caption(2, T0 + 2, "es", "Adiós")
    assert [c.original for c in read_transcript(w.path).captions] == ["Hola", "Adiós"]


def test_a_translation_whose_caption_line_was_lost_survives_as_its_own_row(tmp_path):
    """原文那一行没写进去（写失败过）、译文写进去了：宁可留一行只有译文的字幕，不丢。"""
    w = writer(tmp_path)
    w._write({"type": "translation", "seq": 3, "time": tr._iso(T0 + 4), "translated": "孤零零的译文",
              "quality": "fast"})
    (cap,) = read_transcript(w.path).captions
    assert (cap.seq, cap.original, cap.translated) == (3, "", "孤零零的译文")


def test_an_unknown_quality_counts_as_fast_and_duplicate_captions_are_kept_once(tmp_path):
    w = writer(tmp_path)
    w.caption(1, T0 + 1, "es", "Hola")
    w._write({"type": "caption", "seq": 1, "time": tr._iso(T0 + 2), "src_lang": "es", "original": "otra vez"})
    w._write({"type": "translation", "seq": 1, "time": tr._iso(T0 + 3), "translated": "你好",
              "quality": "ultra"})
    (cap,) = read_transcript(w.path).captions
    assert (cap.original, cap.translated, cap.quality) == ("Hola", "你好", "fast")


# ---- 写失败绝不影响直播 ------------------------------------------------------------------------

class BrokenFile:
    def __init__(self, error=None):
        self.error = error or OSError(errno.ENOSPC, "No space left on device")
        self.calls = 0

    def write(self, data):
        self.calls += 1
        raise self.error

    def truncate(self, size=None):
        raise self.error

    def seek(self, pos):
        raise self.error

    def fileno(self):
        raise self.error

    def close(self):
        pass


def test_a_failing_disk_never_raises_and_warns_once_in_chinese(tmp_path, capsys):
    w = writer(tmp_path)
    capsys.readouterr()
    real = w._fh
    broken = BrokenFile()
    w._fh = broken
    for i in range(5):
        w.caption(i, T0 + i, "es", "texto {}".format(i))
        w.comment("c{}".format(i), T0 + i, "ana_demo", "hola")
    w.close()
    real.close()
    out = capsys.readouterr().out
    assert out.count("[警告] 字幕记录写入失败") == 1
    assert "直播不受影响" in out and "No space left" in out
    assert w.failed and w.write_failures >= 10


def test_a_short_write_is_rolled_back_so_no_half_line_stays_in_the_file(tmp_path, capsys):
    w = writer(tmp_path)
    real = w._fh

    class Short:
        """只写进去一半就返回（磁盘快满时 write 可以少写）。"""

        def __init__(self):
            self.armed = True

        def write(self, data):
            if self.armed:
                self.armed = False
                real.write(data[: len(data) // 2])
                return len(data) // 2
            return real.write(data)

        def truncate(self, size):
            return real.truncate(size)

        def seek(self, pos):
            return real.seek(pos)

        def fileno(self):
            return real.fileno()

        def close(self):
            real.close()

    w._fh = Short()
    w.caption(1, T0 + 1, "es", "se corta")
    w.caption(2, T0 + 2, "es", "sigue bien")
    w.close()
    text = w.path.read_bytes().decode("utf-8")
    assert text.endswith("\n") and all(json.loads(line) for line in text.splitlines())
    assert [c.original for c in read_transcript(w.path).captions] == ["sigue bien"]
    assert "[警告] 字幕记录写入失败" in capsys.readouterr().out


def test_a_folder_that_cannot_be_created_gives_a_writer_that_does_nothing(tmp_path, capsys):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    w = TranscriptWriter("luna_demo", "zh-CN", directory=blocker / "sub", now=T0)
    assert w.path is None
    w.caption(1, T0, "es", "Hola")
    w.comment("c1", T0, "ana_demo", "hola")
    w.translation(1, T0, "你好")
    w.close()
    assert capsys.readouterr().out.count("[警告] 字幕记录写入失败") == 1


def test_a_value_json_cannot_serialise_is_a_logged_failure_not_an_exception(tmp_path, capsys):
    w = writer(tmp_path)
    capsys.readouterr()
    w.caption(1, T0, "es", object())                 # 序列化不了：这一条记不下，但不抛
    w.caption(2, T0 + 1, "es", "sigue")
    assert w.write_failures == 1
    assert "[警告] 字幕记录写入失败" in capsys.readouterr().out
    assert [c.original for c in read_transcript(w.path).captions] == ["sigue"]


# ---- 找最近一场 -------------------------------------------------------------------------------

def recorded(tmp_path, streamer="luna_demo", now=T0, text="hola"):
    """一场听到了一条字幕的记录（收好尾）。"""
    w = writer(tmp_path, streamer=streamer, now=now)
    w.caption(1, now + 1, "es", text)
    w.close()
    return w


def test_the_latest_transcript_is_the_one_that_started_last(tmp_path):
    first = recorded(tmp_path, streamer="luna_demo", now=T0)
    second = recorded(tmp_path, streamer="mia_demo", now=T0 + 3600)
    third = recorded(tmp_path, streamer="luna_demo", now=T0 + 7200)
    (tmp_path / "notes.txt").write_text("not a transcript", encoding="utf-8")
    (tmp_path / "transcript-20251009-000000-x.md").write_text("an export", encoding="utf-8")
    assert list_transcripts(tmp_path) == [first.path, second.path, third.path]
    assert latest_transcript(tmp_path) == third.path
    assert tr.count_transcripts(tmp_path) == 3


def test_there_is_no_latest_transcript_in_an_empty_or_missing_folder(tmp_path):
    assert latest_transcript(tmp_path) is None
    assert latest_transcript(tmp_path / "missing") is None
    assert list_transcripts(tmp_path / "missing") == []


# ---- 空的一场不算一场 -------------------------------------------------------------------------
# 每次开始监听都先建文件；房间不给流地址、开错房间、马上停止，都会留下只有文件头（和收尾）的
# 文件。程序从不删记录（README/FAQ 都这么承诺），所以靠「找」的时候绕开它们。

def test_a_file_with_only_a_header_and_an_end_record_has_no_content(tmp_path):
    w = writer(tmp_path)
    assert not tr.has_content(w.path)                     # 只有文件头（崩溃留下的也是这样）
    w.close()
    assert not tr.has_content(w.path)                     # 文件头加收尾


def test_a_caption_a_comment_or_a_lone_translation_is_content(tmp_path):
    with_caption = writer(tmp_path, now=T0)
    with_caption.caption(1, T0, "es", "hola")
    with_comment = writer(tmp_path, now=T0 + 1)
    with_comment.comment("c1", T0, "ana_demo", "hola")
    lone = tmp_path / "transcript-20251009-000000-x.jsonl"        # 字幕行没写进去、只剩译文
    lone.write_text('{"type":"translation","seq":3,"time":"2025-10-09T00:00:00.000+00:00",'
                    '"translated":"你好","quality":"fast"}\n', encoding="utf-8")
    assert tr.has_content(with_caption.path) and tr.has_content(with_comment.path)
    assert tr.has_content(lone)


def test_has_content_ignores_garbage_empty_values_and_odd_types(tmp_path):
    path = tmp_path / "transcript-20251009-000000-x.jsonl"
    path.write_bytes(b'{"type":"transcript_start","v":1}\n'
                     b'not json at all\n'
                     b'\xff\xfe broken bytes\n'
                     b'[1, 2, 3]\n'
                     b'{"type":["caption"],"original":"x"}\n'     # 类型不是字符串
                     b'{"type":"caption","seq":1,"original":""}\n'  # 没有文字
                     b'{"type":"comment","id":"c1","text":""}\n'
                     b'{"type":"comment_translation","id":"c1","translated":"x"}\n'  # 单独的弹幕译文不算
                     b'{"type":"transcript_end"}\n')
    assert not tr.has_content(path)
    with path.open("ab") as fh:
        fh.write(b'{"type":"caption","seq":2,"original":"hola"}\n')
    assert tr.has_content(path)


def test_has_content_stops_at_the_first_record_instead_of_parsing_the_whole_file(tmp_path, monkeypatch):
    """数「存了几场」要把每个文件都看一遍：有内容的文件读到第一条字幕就该收手，不能整份解析。"""
    w = writer(tmp_path)
    for seq in range(1, 2001):
        w.caption(seq, T0 + seq, "es", "linea {}".format(seq))
    parsed = []
    real_loads = json.loads

    def counting_loads(*a, **k):
        parsed.append(1)
        return real_loads(*a, **k)

    monkeypatch.setattr(tr.json, "loads", counting_loads)
    assert tr.has_content(w.path)
    assert len(parsed) == 2                               # 文件头 + 第一条字幕


def test_a_file_that_cannot_be_read_is_not_hidden(tmp_path):
    """读不了就说不准有没有内容：留在列表里，让导出去报那个具体的错，别悄悄略过。"""
    w = writer(tmp_path)
    w.caption(1, T0, "es", "hola")
    w.path.chmod(0)
    try:
        if os.access(w.path, os.R_OK):
            pytest.skip("this account can read files whatever their mode")
        assert tr.has_content(w.path)
        assert latest_transcript(tmp_path) == w.path
    finally:
        w.path.chmod(0o644)


def test_latest_and_count_skip_sessions_that_never_recorded_anything(tmp_path):
    real = recorded(tmp_path, now=T0)
    empty_closed = writer(tmp_path, streamer="mia_demo", now=T0 + 3600)       # 开了就关：文件头加收尾
    empty_closed.close()
    crashed = writer(tmp_path, streamer="zoe_demo", now=T0 + 7200)             # 崩溃：只有文件头
    assert list_transcripts(tmp_path) == [real.path, empty_closed.path, crashed.path]   # 文件夹里都还在
    assert latest_transcript(tmp_path) == real.path
    assert tr.count_transcripts(tmp_path) == 1
    assert tr.list_transcripts(tmp_path, recorded_only=True) == [real.path]
    assert empty_closed.path.exists() and crashed.path.exists()                 # 没有谁被删


def test_a_session_counts_the_moment_it_hears_something(tmp_path):
    live = writer(tmp_path)
    assert latest_transcript(tmp_path) is None and tr.count_transcripts(tmp_path) == 0
    live.comment("c1", T0, "ana_demo", "hola")
    assert latest_transcript(tmp_path) == live.path and tr.count_transcripts(tmp_path) == 1


def test_a_folder_of_nothing_but_empty_sessions_has_no_latest_transcript(tmp_path):
    for n in range(3):
        writer(tmp_path, now=T0 + n).close()
    assert latest_transcript(tmp_path) is None
    assert tr.count_transcripts(tmp_path) == 0


def test_the_folder_is_called_transcripts_and_git_ignores_it():
    """个人数据（含观众昵称和弹幕），不入库，也不能挡住一键更新（工作区要保持干净）。"""
    assert tr.TRANSCRIPT_DIR.name == "transcripts"
    ignore = (Path(tr.__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "transcripts/" in ignore.splitlines()
