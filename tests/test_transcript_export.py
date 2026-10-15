"""字幕记录的三种导出（app/transcript_export.py）：Markdown、CSV、SRT。

黄金文件风格：tests/fixtures/transcript/session.jsonl 是一场虚构的小直播（主播 luna_demo，
五分钟不到），旁边的 session.zh.md / session.en.md / session.srt 是人工看过的期望输出；CSV 带
BOM 和 CRLF，写成文件会被 git 的换行转换动手脚，所以期望值直接写在下面的字节串里。
改了导出格式要同时改这些期望，并且要自己看一遍，不是跑个脚本重新生成就算数。

时间不依赖运行测试的机器所在时区：夹具里的时间都带 +00:00，导出显示的是记录时的钟面时间。
"""
import csv
import io
import json
import os
from pathlib import Path

import pytest

from app import i18n
from app import transcript_export as ex
from app.transcript import read_transcript

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "transcript"
SESSION = FIXTURES / "session.jsonl"

GOLDEN_CSV = (
    "time,kind,user,original,translation\r\n"
    '2026-10-15 20:00:03,caption,,"Hola a todos, bienvenidos a mi directo.",大家好，欢迎来到我的直播间。\r\n'
    '2026-10-15 20:00:05,comment,ana_demo,"Hola Luna, te veo muy bien!",你好 Luna，你看起来气色很好！\r\n'
    '2026-10-15 20:00:12,caption,,"Hoy tenemos 20% de descuento, solo hoy.",今天全场八折，仅限今天。\r\n'
    "2026-10-15 20:00:14,comment,'=oferta_demo,"
    '"\'=HYPERLINK(""http://example.invalid"",""gratis"")",\r\n'
    '2026-10-15 20:00:21,caption,,"Gracias por el *regalo*,\nde corazón.",\r\n'
    "2026-10-15 20:00:22,comment,pedro_demo,'+34 600 000 000,\r\n"
    "2026-10-15 20:00:58,caption,,1. Primero el sérum.,1. 先说精华液。\r\n"
)


def golden(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def make(tmp_path, *records, name="transcript-20261015-200000-luna_demo.jsonl"):
    """把给定的记录（dict）写成一个记录文件，返回路径。"""
    path = tmp_path / name
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return path


def start(at="2026-10-15T20:00:00.000+00:00", **kw):
    return dict({"type": "transcript_start", "v": 1, "started_at": at, "streamer": "luna_demo",
                 "target_lang": "zh-CN"}, **kw)


def cap(seq, at, text="hola", translated=None):
    out = [{"type": "caption", "seq": seq, "time": at, "src_lang": "es", "original": text}]
    if translated:
        out.append({"type": "translation", "seq": seq, "time": at, "translated": translated,
                    "quality": "fast"})
    return out


def srt_of(tmp_path, *records):
    return ex.render_srt(read_transcript(make(tmp_path, *records)))


# ---- 黄金文件 --------------------------------------------------------------------------------

def test_markdown_matches_the_reviewed_chinese_golden_file():
    assert ex.render_markdown(read_transcript(SESSION)) == golden("session.zh.md")


def test_markdown_headings_follow_the_interface_language():
    with i18n.use(i18n.EN):
        out = ex.render_markdown(read_transcript(SESSION))
    assert out == golden("session.en.md")
    # 小标题是英文了，内容（字幕、译文、昵称）原样不动
    assert "## Captions" in out and "## Comments" in out and "大家好，欢迎来到我的直播间。" in out


def test_captions_and_comments_sit_in_separate_sections():
    """分节而不是穿插：主播的话连成一条线，弹幕不去打断它。"""
    out = ex.render_markdown(read_transcript(SESSION))
    captions, comments = out.split("## 弹幕")
    assert "Hola a todos" in captions and "Hola Luna" not in captions
    assert "Hola Luna" in comments and "Hola a todos" not in comments


def test_srt_matches_the_reviewed_golden_file():
    assert ex.render_srt(read_transcript(SESSION)) == golden("session.srt")


def test_csv_matches_the_reviewed_golden_bytes():
    assert ex.render_csv(read_transcript(SESSION)) == GOLDEN_CSV


# ---- CSV：Excel 能直接打开 -------------------------------------------------------------------

def test_the_csv_file_starts_with_exactly_one_utf8_bom(tmp_path):
    result = ex.export_transcript(make(tmp_path, start(), *cap(1, "2026-10-15T20:00:03.000+00:00",
                                                            "Hola", "你好")))
    raw = result.paths[1].read_bytes()
    assert result.paths[1].suffix == ".csv"
    assert raw.startswith(b"\xef\xbb\xbf") and not raw.startswith(b"\xef\xbb\xbf\xef\xbb\xbf")
    assert raw[3:].startswith(b"time,kind,user,original,translation\r\n")
    assert "你好" in raw.decode("utf-8")            # 中文是 UTF-8 字节，不是转义


def test_the_csv_round_trips_through_a_csv_reader(tmp_path):
    result = ex.export_transcript(SESSION_COPY(tmp_path))
    text = result.paths[1].read_bytes().decode("utf-8-sig")
    table = list(csv.reader(io.StringIO(text, newline="")))
    assert table[0] == ["time", "kind", "user", "original", "translation"]
    assert len(table) == 1 + 4 + 3
    assert table[1] == ["2026-10-15 20:00:03", "caption", "", "Hola a todos, bienvenidos a mi directo.",
                        "大家好，欢迎来到我的直播间。"]
    assert table[4][1:4] == ["comment", "'=oferta_demo", "'=HYPERLINK(\"http://example.invalid\",\"gratis\")"]
    assert table[5][3] == "Gracias por el *regalo*,\nde corazón."      # 换行留在引号里，行数不乱


def SESSION_COPY(tmp_path):
    path = tmp_path / "transcript-20261015-200000-luna_demo.jsonl"
    path.write_bytes(SESSION.read_bytes())
    return path


@pytest.mark.parametrize("text", ["=1+1", "+34 600", "-2+3", "@SUM(A1)", "\t=cmd", "\r=cmd"])
def test_cells_that_excel_would_run_as_formulas_get_a_leading_apostrophe(tmp_path, text):
    """弹幕和昵称是陌生人写的：不补的话，一条「=HYPERLINK(…)」会在中控打开 CSV 时被执行。"""
    path = make(tmp_path, start(), {"type": "comment", "id": "c1", "time": "2026-10-15T20:00:05.000+00:00",
                                    "user": text, "text": text},
                {"type": "comment_translation", "id": "c1", "translated": text})
    rows = list(csv.reader(io.StringIO(ex.render_csv(read_transcript(path)), newline="")))
    assert rows[1][2:] == ["'" + text] * 3


def test_ordinary_text_is_not_touched_by_the_formula_guard(tmp_path):
    path = make(tmp_path, start(), *cap(1, "2026-10-15T20:00:03.000+00:00", "Hola = adiós - 3 + 4", "你好 @ 你"))
    rows = list(csv.reader(io.StringIO(ex.render_csv(read_transcript(path)), newline="")))
    assert rows[1][3:] == ["Hola = adiós - 3 + 4", "你好 @ 你"]


def test_csv_rows_are_in_time_order_even_when_the_two_streams_wrote_slightly_out_of_order(tmp_path):
    path = make(tmp_path, start(),
                *cap(1, "2026-10-15T20:00:10.000+00:00", "segunda"),
                {"type": "comment", "id": "c1", "time": "2026-10-15T20:00:09.900+00:00", "user": "ana_demo",
                 "text": "primera"})
    rows = list(csv.reader(io.StringIO(ex.render_csv(read_transcript(path)), newline="")))
    assert [r[3] for r in rows[1:]] == ["primera", "segunda"]


# ---- SRT：时间、顺序、边界 --------------------------------------------------------------------

def test_an_srt_cue_shows_the_translation_line_first_then_the_original(tmp_path):
    out = srt_of(tmp_path, start(), *cap(1, "2026-10-15T20:00:03.200+00:00", "Hola", "你好"))
    assert out == "1\n00:00:03,200 --> 00:00:08,200\n你好\nHola\n"


def test_a_caption_without_a_translation_is_just_the_original(tmp_path):
    out = srt_of(tmp_path, start(), *cap(1, "2026-10-15T20:00:03.000+00:00", "Hola"))
    assert out.splitlines()[2:] == ["Hola"]


def test_cues_run_to_the_next_caption_but_never_longer_than_eight_seconds(tmp_path):
    out = srt_of(tmp_path, start(),
                 *cap(1, "2026-10-15T20:00:10.000+00:00", "uno"),
                 *cap(2, "2026-10-15T20:00:12.500+00:00", "dos"),      # 离下一条 2.5 秒：到它为止
                 *cap(3, "2026-10-15T20:00:40.000+00:00", "tres"))     # 离下一条很远：最长 8 秒
    times = [line for line in out.splitlines() if "-->" in line]
    assert times == ["00:00:10,000 --> 00:00:12,500",
                     "00:00:12,500 --> 00:00:20,500",
                     "00:00:40,000 --> 00:00:45,000"]       # 最后一条固定 5 秒


def test_srt_times_count_from_the_start_of_the_session_not_from_the_first_caption(tmp_path):
    out = srt_of(tmp_path, start("2026-10-15T20:00:00.000+00:00"), *cap(1, "2026-10-15T21:02:05.500+00:00", "tarde"))
    assert "01:02:05,500 --> 01:02:10,500" in out


def test_without_a_header_the_first_caption_is_time_zero(tmp_path):
    out = srt_of(tmp_path, *cap(1, "2026-10-15T20:00:30.000+00:00", "uno"),
                 *cap(2, "2026-10-15T20:00:34.000+00:00", "dos"))
    assert [line for line in out.splitlines() if "-->" in line][0] == "00:00:00,000 --> 00:00:04,000"


def test_a_clock_that_jumps_back_never_makes_the_subtitle_time_run_backwards(tmp_path):
    out = srt_of(tmp_path, start(),
                 *cap(1, "2026-10-15T20:00:20.000+00:00", "uno"),
                 *cap(2, "2026-10-15T20:00:05.000+00:00", "dos"))      # 墙钟被往回拨了
    starts = [line.split(" --> ")[0] for line in out.splitlines() if "-->" in line]
    assert starts == sorted(starts) and starts[1] == "00:00:20,000"
    assert all(line.split(" --> ")[1] > line.split(" --> ")[0] for line in out.splitlines() if "-->" in line)


def test_multiline_text_cannot_split_a_cue_in_two(tmp_path):
    """SRT 里一个空行就是一条字幕的结束：原文里的换行和空行必须压平。"""
    out = srt_of(tmp_path, start(), *cap(1, "2026-10-15T20:00:03.000+00:00", "uno\n\ndos\n", "一\n\n二"))
    blocks = out.strip().split("\n\n")
    assert len(blocks) == 1 and blocks[0].splitlines()[2:] == ["一 二", "uno dos"]


def test_a_translation_identical_to_the_original_is_not_written_twice(tmp_path):
    out = srt_of(tmp_path, start(), *cap(1, "2026-10-15T20:00:03.000+00:00", "OK", "OK"))
    assert out.splitlines()[2:] == ["OK"]


def test_an_empty_session_gives_an_empty_srt_and_a_header_only_csv(tmp_path):
    tr = read_transcript(make(tmp_path, start()))
    assert ex.render_srt(tr) == ""
    assert ex.render_csv(tr) == "time,kind,user,original,translation\r\n"
    md = ex.render_markdown(tr)
    assert "（没有）" in md and md.count("（没有）") == 2 and md.endswith("\n") and not md.endswith("\n\n")


# ---- Markdown：转义、跨零点、没有主播名 ---------------------------------------------------------

def test_markdown_formatting_characters_in_speech_and_comments_are_escaped(tmp_path):
    path = make(tmp_path, start(),
                *cap(1, "2026-10-15T20:00:03.000+00:00", "# titulo [link](http://x) <b>fuerte</b> `code` a_b_c", "- 一 | 二 ~ 三"),
                {"type": "comment", "id": "c1", "time": "2026-10-15T20:00:05.000+00:00",
                 "user": "*gran*_fan", "text": "> cita\n\n# otra"})
    out = ex.render_markdown(read_transcript(path))
    # 方括号转义之后 [x](y) 不成链接；行首的 # - 会变成标题和列表，也转义
    assert "> \\# titulo \\[link\\](http://x) \\<b\\>fuerte\\</b\\> \\`code\\` a\\_b\\_c" in out
    assert "\\- 一 \\| 二 \\~ 三" in out
    assert "**20:00:05** · \\*gran\\*\\_fan" in out
    assert "> \\> cita # otra" in out                      # 换行压平之后 # 在行中间，无害
    assert "\n# " not in out.replace("\n# @luna", "")      # 除了标题，没有哪一行能以 # 开头


def test_a_session_across_midnight_gets_a_date_line_where_the_day_changes(tmp_path):
    path = make(tmp_path, start("2026-10-15T23:58:00.000+00:00"),
                *cap(1, "2026-10-15T23:59:30.000+00:00", "antes"),
                *cap(2, "2026-10-16T00:00:30.000+00:00", "despues"))
    out = ex.render_markdown(read_transcript(path))
    assert out.index("**23:59:30**") < out.index("### 2026-10-16") < out.index("**00:00:30**")
    assert out.count("### 20") == 1
    assert "2026-10-15 23:58:00" in out                       # 头部写完整的开始日期时间


def test_a_session_without_a_streamer_gets_a_plain_title(tmp_path):
    path = make(tmp_path, start(streamer=""))
    assert ex.render_markdown(read_transcript(path)).startswith("# 这一场的字幕记录\n")
    with i18n.use(i18n.EN):
        assert ex.render_markdown(read_transcript(path)).startswith("# Session transcript\n")


def test_counts_use_the_singular_in_english(tmp_path):
    path = make(tmp_path, start(), *cap(1, "2026-10-15T20:00:03.000+00:00", "hola"))
    with i18n.use(i18n.EN):
        assert "- 1 caption, 0 comments" in ex.render_markdown(read_transcript(path))


def test_a_session_that_was_never_closed_has_no_end_line(tmp_path):
    path = make(tmp_path, start(), *cap(1, "2026-10-15T20:00:03.000+00:00", "hola"))
    out = ex.render_markdown(read_transcript(path))
    assert "- 开始：2026-10-15 20:00:00" in out and "结束" not in out


# ---- 导出到文件 ------------------------------------------------------------------------------

def test_export_writes_three_files_next_to_the_transcript(tmp_path):
    path = SESSION_COPY(tmp_path)
    result = ex.export_transcript(path)
    assert [p.name for p in result.paths] == [
        "transcript-20261015-200000-luna_demo.md", "transcript-20261015-200000-luna_demo.csv",
        "transcript-20261015-200000-luna_demo.srt"]
    assert all(p.parent == path.parent and p.exists() for p in result.paths)
    assert result.folder == path.parent and (result.captions, result.comments) == (4, 3)
    assert result.paths[0].read_text(encoding="utf-8") == golden("session.zh.md")
    assert result.paths[2].read_text(encoding="utf-8") == golden("session.srt")
    assert path.read_bytes() == SESSION.read_bytes()          # 记录本身一个字节不动
    assert not list(tmp_path.glob("*.part"))


def test_a_handle_with_dots_keeps_its_whole_name_in_the_export_files(tmp_path):
    path = make(tmp_path, start(streamer="luna.demo"), name="transcript-20261015-200000-luna.demo.jsonl")
    assert [p.name for p in ex.export_transcript(path).paths] == [
        "transcript-20261015-200000-luna.demo.md", "transcript-20261015-200000-luna.demo.csv",
        "transcript-20261015-200000-luna.demo.srt"]


def test_exporting_again_replaces_the_previous_export(tmp_path):
    path = SESSION_COPY(tmp_path)
    ex.export_transcript(path)
    with path.open("ab") as fh:                     # 这一场又多记了一条
        fh.write(json.dumps({"type": "caption", "seq": 5, "time": "2026-10-15T20:01:10.000+00:00",
                             "src_lang": "es", "original": "Adios"}).encode("utf-8") + b"\n")
    result = ex.export_transcript(path)
    assert result.captions == 5 and "Adios" in result.paths[0].read_text(encoding="utf-8")
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_exporting_a_session_that_is_still_being_recorded_works(tmp_path):
    """进行中的那一场（没有 transcript_end，最后一行可能只写了一半）也能导出。"""
    path = make(tmp_path, start(), *cap(1, "2026-10-15T20:00:03.000+00:00", "Hola", "你好"))
    with path.open("ab") as fh:
        fh.write(b'{"type":"caption","seq":2,"time":"2026-10-15T20:00')
    result = ex.export_transcript(path)
    assert (result.captions, result.comments) == (1, 0)


def test_a_failure_halfway_does_not_leave_a_half_written_file_over_the_old_export(tmp_path, monkeypatch):
    path = SESSION_COPY(tmp_path)
    ex.export_transcript(path)
    before = path.with_suffix(".csv").read_bytes()
    real_replace = os.replace

    def replace(src, dst):
        if str(dst).endswith(".csv"):
            raise PermissionError(13, "Permission denied", str(dst))     # 比如 Excel 正开着它
        return real_replace(src, dst)

    monkeypatch.setattr(ex.os, "replace", replace)
    with pytest.raises(PermissionError):
        ex.export_transcript(path)
    assert path.with_suffix(".csv").read_bytes() == before
    assert not list(tmp_path.glob("*.part"))


def test_exporting_a_missing_file_raises_oserror_for_the_caller_to_report(tmp_path):
    with pytest.raises(OSError):
        ex.export_transcript(tmp_path / "transcript-20261015-200000-gone.jsonl")
