# i18n: done
"""把一场字幕记录（app/transcript.py 写下的 JSONL）导出成三种文件，放在记录文件旁边：

  *.md   双语 Markdown：给人读。字幕和弹幕**分成两节**，不按时间穿插。
         理由：主播的话是连续的叙述，一小时只有几百句；弹幕一小时能有几千条，大多是「hola」
         和表情。穿插起来，弹幕会把那条叙述冲得没法读。想看「主播说这句话时观众在说什么」，
         用 CSV——它把两种内容按时间排在同一张表里，用 kind 列区分，筛选或排序都行。
  *.csv  time, kind, user, original, translation。UTF-8 带 BOM（Excel 双击打开中文才不乱码）。
         单元格以 = + - @ 开头时前面补一个 '：弹幕和昵称是陌生人写的，
         不补的话 Excel 会把「=HYPERLINK(…)」之类当公式执行（CSV 注入）。
  *.srt  字幕文件。时间取自字幕出现的时刻，从这一场开始算起：每条先写译文，再写原文；
         每条显示到下一条出现为止，最长 8 秒（主播停顿时别让旧字幕一直挂着）。
         只有字幕，没有弹幕。

重复导出同一场会覆盖上次的导出文件，不会碰 JSONL 本身。Markdown 的小标题跟界面语言走（此刻是
哪种就写哪种）；CSV 的列名固定是英文，方便脚本读。
"""
import csv
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import i18n
from .i18n import L, LN
from .transcript import read_transcript

EXPORT_SUFFIXES = (".md", ".csv", ".srt")
CSV_HEADER = ("time", "kind", "user", "original", "translation")
SRT_MAX_CUE_SEC = 8.0
SRT_LAST_CUE_SEC = 5.0

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~])")
_MD_LINE_START = re.compile(r"^(#|[-+=]|(\d+)([.)]))")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class ExportResult:
    paths: list          # 写出的三个文件
    folder: Path
    captions: int
    comments: int


def _flat(text):
    """压成一行：Markdown 和 SRT 里的换行会拆坏版式（SRT 里的空行还会把一条字幕截成两条）。"""
    return " ".join(str(text).split())  # i18n: data（字幕和弹幕的原文，不是界面文字）


def _md(text, inline=False):
    """放进 Markdown 正文的文字：转义会被当成格式的字符。主播的话和观众的弹幕都不可信。
    inline=True 表示它出现在一行的中间（昵称、语言码），不用防「行首的 # - 1.」。"""
    flat = _MD_SPECIAL.sub(r"\\\1", _flat(text))
    match = None if inline else _MD_LINE_START.match(flat)
    if match:
        if match.group(2):                       # "3. xxx" 会变成编号列表：转义点
            return flat[:len(match.group(2))] + "\\" + flat[len(match.group(2)):]
        return "\\" + flat
    return flat


def _cell(text):
    text = str(text)
    return "'" + text if text.startswith(_FORMULA_START) else text


def _clock(moment):
    return moment.strftime("%H:%M:%S")


def _stamp(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def render_markdown(tr):
    t = i18n.text
    title = "@" + tr.streamer if tr.streamer else t(L("这一场的字幕记录", "Session transcript"))
    lines = ["# " + _md(title), ""]
    if tr.started is not None:
        lines.append("- " + t(L("开始：", "Started: ")) + _stamp(tr.started))
    if tr.ended is not None:
        lines.append("- " + t(L("结束：", "Ended: ")) + _stamp(tr.ended))
    if tr.target_lang:
        lines.append("- " + t(L("译成：", "Translated to: ")) + _md(tr.target_lang, inline=True))
    counts = [LN(len(tr.captions), "{} 条字幕", "1 caption", "{} captions").format(len(tr.captions)),
              LN(len(tr.comments), "{} 条弹幕", "1 comment", "{} comments").format(len(tr.comments))]
    lines.append("- " + t(L("、", ", ").join(counts)))
    lines.append("")
    _section(lines, t(L("字幕", "Captions")), tr.captions, tr.started, show_lang=True)
    _section(lines, t(L("弹幕", "Comments")), tr.comments, tr.started, show_lang=False)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"  # i18n: data（lines 里的小标题已按界面语言渲染成普通 str）


def _section(lines, heading, items, started, show_lang):
    lines.append("## " + heading)
    lines.append("")
    if not items:
        lines.append(i18n.text(L("（没有）", "(none)")))
        lines.append("")
        return
    day = started.date() if started is not None else items[0].time.date()
    for item in items:
        if item.time.date() != day:              # 过了零点：加一行日期，免得只看钟点读岔
            day = item.time.date()
            lines += ["### " + day.isoformat(), ""]
        who = item.lang if show_lang else item.user
        lines.append("**" + _clock(item.time) + "**" + (" · " + _md(who, inline=True) if who else ""))
        lines.append("")
        original = item.original if show_lang else item.text
        if original:
            lines += ["> " + _md(original), ""]
        if item.translated:
            lines += [_md(item.translated), ""]


def render_csv(tr):
    buf = io.StringIO(newline="")
    out = csv.writer(buf, lineterminator="\r\n")
    out.writerow(CSV_HEADER)
    # 按时间排：字幕和弹幕是两条各自打时间戳的链路，写入先后偶尔差几十毫秒。sorted 是稳定排序，
    # 同一时刻的保持写入顺序
    for kind, item in sorted(tr.entries, key=lambda entry: entry[1].epoch):
        if kind == "caption":
            out.writerow([_stamp(item.time), "caption", "", _cell(item.original),
                          _cell(item.translated)])
        else:
            out.writerow([_stamp(item.time), "comment", _cell(item.user), _cell(item.text),
                          _cell(item.translated)])
    return buf.getvalue()


def _srt_time(ms):
    ms = max(0, int(ms))
    return "{:02d}:{:02d}:{:02d},{:03d}".format(ms // 3600000, ms // 60000 % 60,
                                               ms // 1000 % 60, ms % 1000)


def render_srt(tr):
    caps = [c for c in tr.captions if c.original or c.translated]
    if not caps:
        return ""
    origin = tr.started_epoch if tr.started_epoch is not None else caps[0].epoch
    starts, prev = [], 0
    for cap in caps:
        prev = max(prev, round((cap.epoch - origin) * 1000))    # 时钟回拨也不让时间倒着走
        starts.append(prev)
    blocks = []
    for i, cap in enumerate(caps):
        if i + 1 < len(caps):
            end = min(starts[i + 1], starts[i] + round(SRT_MAX_CUE_SEC * 1000))
        else:
            end = starts[i] + round(SRT_LAST_CUE_SEC * 1000)
        end = max(end, starts[i] + 1)
        text = [_flat(x) for x in (cap.translated, cap.original) if x]
        if len(text) == 2 and text[0] == text[1]:
            text.pop()
        blocks.append("{}\n{} --> {}\n{}\n".format(i + 1, _srt_time(starts[i]), _srt_time(end),
                                                  "\n".join(text)))  # i18n: data
    return "\n".join(blocks)  # i18n: data（SRT 里只有字幕原文和译文）


def _write_atomic(path, data):
    """先写 .part 再改名：导出到一半出错，不会留下半个文件顶替上一次完整的导出。"""
    part = path.with_name(path.name + ".part")
    try:
        part.write_bytes(data)
        os.replace(str(part), str(path))
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def export_transcript(path):
    """读 path（一个 transcript-*.jsonl），在它旁边写出 .md / .csv / .srt。
    读不了、写不了就抛 OSError，由调用方告诉用户。"""
    path = Path(path)
    tr = read_transcript(path)
    outputs = ((".md", render_markdown(tr).encode("utf-8", errors="replace")),
               (".csv", render_csv(tr).encode("utf-8-sig", errors="replace")),
               (".srt", render_srt(tr).encode("utf-8", errors="replace")))
    written = []
    for suffix, data in outputs:
        target = path.with_suffix(suffix)
        _write_atomic(target, data)
        written.append(target)
    return ExportResult(written, path.parent, len(tr.captions), len(tr.comments))
