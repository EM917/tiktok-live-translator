"""字幕记录：每一场直播的字幕（原文、译文）和观众弹幕，边听边存成一个本地文件。

给中控事后翻看、整理用的，和审计日志（app/audit.py）是两回事：
  * 审计日志是合规证据，只有代码和数字，**弹幕内容不进审计**，永远是中文；
  * 字幕记录是内容本身——主播说了什么、译成了什么、观众发了什么——只留在这台电脑上，
    放在程序文件夹的 transcripts/ 里（.gitignore 已忽略）。两边互不写对方的东西。

文件格式：JSONL，一行一条记录，写一条就落盘一条（无缓冲），程序中途崩溃只会丢掉正在写的那
半行，之前的都在。读取时认不出的行（崩溃留下的半行、被人手改坏的行）一律跳过，不让它拖累
整个文件。记录类型：
  transcript_start  v、started_at、streamer、target_lang（文件头）
  caption           seq、time、src_lang、original
  translation       seq、time、translated、quality（fast / strong）
  comment           id、time、user、text
  comment_translation  id、translated
  transcript_end    ended_at（正常收尾才有；崩溃的文件没有，不影响读取）
time 是带时区偏移的本地时间（ISO 8601，毫秒），夏令时换挡也对得上。

**写失败绝不能影响直播**：写不进去（磁盘满、目录没权限）只在终端打一次中文警告，之后这一场
的记录静默停下，识别、检测、审计、翻译一概不受牵连——这个文件是锦上添花，不在两个 KPI
的关键路径上。所有写入都发生在事件循环线程里，不加锁。
"""
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TRANSCRIPT_DIR = Path(__file__).resolve().parent.parent / "transcripts"

FILE_PREFIX = "transcript-"
FILE_SUFFIX = ".jsonl"
FORMAT_VERSION = 1

# 一场里最多记住多少个弹幕 id 来对上译文：译文几秒内就回来，不需要记一整场
COMMENT_IDS_KEPT = 5000

_STAMP = re.compile(r"^transcript-(\d{8}-\d{6})")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _iso(ts=None):
    """epoch 秒 -> 带偏移的本地时间字符串（毫秒）。认不出的时间用此刻。"""
    try:
        moment = datetime.fromtimestamp(float(ts)) if ts is not None else datetime.now()
    except (TypeError, ValueError, OverflowError, OSError):
        moment = datetime.now()
    return moment.astimezone().isoformat(timespec="milliseconds")


def safe_handle(streamer):
    """主播名放进文件名之前清洗：只留字母数字和 . _ -，不许出现路径分隔符，也不许以点开头
    （隐藏文件、..）。取不到主播名（直接流地址）就写 stream。"""
    cleaned = _UNSAFE.sub("_", str(streamer or "")).lstrip(".")[:40]
    return cleaned or "stream"


class TranscriptWriter:
    """一场的字幕记录。构造时就把文件建好、文件头写进去；**构造和所有记录方法都不抛异常**。

    owner 是这一场的审计对象（Pipeline 用它认「这份记录是不是我这一场的」：晚死的旧任务只许关
    自己的，不许关下一场的，同 CLAUDE.md 第七条的 my_audit 模式）。建不起来时 path 为 None，
    之后的记录方法都是空操作。"""

    def __init__(self, streamer="", target_lang="", owner=None, directory=None, now=None):
        self.owner = owner
        self.path = None
        self.failed = False                  # 开过的文件后来写失败了
        self.write_failures = 0
        self._fh = None
        self._pos = 0                        # 已整行落盘的字节数（写到一半失败时截回这里）
        self._warned = False
        self._seqs = set()                   # 本场写过原文的字幕序号：译文凭它对号
        self._cids = OrderedDict()           # 本场写过的弹幕 id（有上限）
        self.streamer = str(streamer or "")
        try:
            folder = Path(directory) if directory else TRANSCRIPT_DIR
            folder.mkdir(parents=True, exist_ok=True)
            moment = datetime.fromtimestamp(now) if now is not None else datetime.now()
            self.path, self._fh = _open_new(folder, moment.strftime("%Y%m%d-%H%M%S"),
                                            safe_handle(streamer))
        except Exception as exc:             # 目录不可写、盘满、路径非法……
            self.path = None
            self._fh = None
            self._warn(exc)
            return
        self._write({"type": "transcript_start", "v": FORMAT_VERSION,
                     "started_at": _iso(now), "streamer": self.streamer,
                     "target_lang": str(target_lang or "")})

    # ---- 记录 ----
    def caption(self, seq, ts, lang, original):
        """一条字幕的原文（在广播给界面的同一刻写）。没有文字不记。"""
        if not original:
            return
        self._seqs.add(seq)
        self._write({"type": "caption", "seq": seq, "time": _iso(ts),
                     "src_lang": str(lang or ""), "original": original})

    def translation(self, seq, ts, translated, strong=False):
        """一条字幕的译文。strong=True 是「重译」换上的强模型版本，导出时它盖过快译。
        对不上本场某条字幕的（上一场迟到的译文）不记，免得串场。"""
        if not translated or seq not in self._seqs:
            return
        self._write({"type": "translation", "seq": seq, "time": _iso(ts),
                     "translated": translated, "quality": "strong" if strong else "fast"})

    def comment(self, cid, ts, user, text):
        """一条观众弹幕（含昵称）。内容只进这个文件，不进审计。"""
        if not cid or not text:
            return
        self._cids[cid] = True
        while len(self._cids) > COMMENT_IDS_KEPT:
            self._cids.popitem(last=False)
        self._write({"type": "comment", "id": cid, "time": _iso(ts),
                     "user": str(user or ""), "text": text})

    def comment_translation(self, cid, translated):
        """一条弹幕的译文（重译会再记一次，后写的算）。"""
        if not translated or cid not in self._cids:
            return
        self._write({"type": "comment_translation", "id": cid, "translated": translated})

    def close(self, ts=None):
        """收尾：写一条 transcript_end 再关文件。可以重复调用。"""
        if self._fh is None:
            return
        self._write({"type": "transcript_end", "ended_at": _iso(ts)})
        fh, self._fh = self._fh, None
        try:
            os.fsync(fh.fileno())            # 收尾时多做一步：断电前尽量落到盘上
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    # ---- 内部 ----
    def _write(self, record):
        if self._fh is None:
            return
        try:
            data = (json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    + "\n").encode("utf-8", errors="replace")
            written = self._fh.write(data)
            if written is not None and written != len(data):
                raise OSError("short write")
            self._pos += len(data)
        except Exception as exc:             # OSError、文件已关、记录里有写不了的东西……
            self.write_failures += 1
            self.failed = True
            self._rewind()
            self._warn(exc)

    def _rewind(self):
        """写到一半失败：把文件截回上一条完整记录的末尾，别留半行。"""
        try:
            self._fh.truncate(self._pos)
            self._fh.seek(self._pos)         # 文件不是追加模式打开的：位置也要回到末尾
        except Exception:
            pass

    def _warn(self, exc):
        if self._warned:
            return
        self._warned = True
        print("[警告] 字幕记录写入失败，这一场之后的记录可能不完整（直播不受影响）：{}".format(exc))


def _open_new(folder, stamp, handle):
    """独占创建记录文件。同一秒起两场会得到同一个名字：冲突就加 -2/-3 后缀（同 audit._open_new）。
    无缓冲的二进制句柄：每条记录整行写出，崩溃时最多丢正在写的那一行。"""
    for n in range(1, 100):
        name = "{}{}-{}{}{}".format(FILE_PREFIX, stamp, handle,
                                    "" if n == 1 else "-{}".format(n), FILE_SUFFIX)
        path = folder / name
        try:
            return path, path.open("xb", buffering=0)
        except FileExistsError:
            continue
    raise OSError("同一秒内已有 99 个字幕记录文件")  # i18n: terminal


# ---- 读 ----------------------------------------------------------------------------

@dataclass
class Caption:
    seq: int
    time: datetime              # 记录时的本地钟面时间（不带时区），导出时直接显示
    epoch: float                # 同一时刻的 epoch 秒，算间隔用
    lang: str = ""
    original: str = ""
    translated: str = ""
    quality: str = ""           # "" / "fast" / "strong"


@dataclass
class Comment:
    id: str
    time: datetime
    epoch: float
    user: str = ""
    text: str = ""
    translated: str = ""


@dataclass
class Transcript:
    path: Path
    streamer: str = ""
    target_lang: str = ""
    started: datetime = None
    started_epoch: float = None
    ended: datetime = None
    version: int = FORMAT_VERSION
    captions: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    entries: list = field(default_factory=list)   # 字幕和弹幕合在一起，按写入先后（就是时间先后）


_RANK = {"": 0, "fast": 1, "strong": 2}


def _parse_time(value):
    """ISO 字符串 -> (钟面时间, epoch 秒)；认不出返回 None。"""
    try:
        moment = datetime.fromisoformat(str(value))
        return moment.replace(tzinfo=None), moment.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def read_transcript(path):
    """读一个记录文件。**宽容地读**：坏行、崩溃留下的半行、不认识的记录类型都跳过。
    文件打不开才抛 OSError（调用方要告诉用户）。

    译文挂到同序号的字幕上：同一条字幕有多条译文时，等级高的盖过低的，同级后到的盖过先到的
    （与界面上 Pipeline._publish_translation 的规则一致）。对不上字幕的译文：字幕行没写进去
    （写失败过）时仍保留成一条只有译文的字幕；对不上弹幕的弹幕译文丢掉。"""
    path = Path(path)
    out = Transcript(path=path)
    by_seq, by_cid = {}, {}
    last = None                                  # 最近一条有效时间，坏时间的记录借用它
    with path.open("rb") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            stamp = _parse_time(rec.get("time") or rec.get("started_at")) or last
            if kind == "transcript_start":
                out.streamer = str(rec.get("streamer") or "")
                out.target_lang = str(rec.get("target_lang") or "")
                if isinstance(rec.get("v"), int):
                    out.version = rec["v"]
                if stamp is not None:
                    out.started, out.started_epoch = stamp
            elif kind == "transcript_end":
                ended = _parse_time(rec.get("ended_at"))
                if ended is not None:
                    out.ended = ended[0]
            elif kind == "caption":
                seq = _int(rec.get("seq"))
                original = str(rec.get("original") or "")
                if seq is None or not original or seq in by_seq or stamp is None:
                    continue
                item = Caption(seq, stamp[0], stamp[1], str(rec.get("src_lang") or ""), original)
                by_seq[seq] = item
                out.captions.append(item)
                out.entries.append(("caption", item))
            elif kind == "translation":
                _add_translation(out, by_seq, rec, stamp)
            elif kind == "comment":
                cid = str(rec.get("id") or "")
                text = str(rec.get("text") or "")
                if not cid or not text or cid in by_cid or stamp is None:
                    continue
                item = Comment(cid, stamp[0], stamp[1], str(rec.get("user") or ""), text)
                by_cid[cid] = item
                out.comments.append(item)
                out.entries.append(("comment", item))
            elif kind == "comment_translation":
                item = by_cid.get(str(rec.get("id") or ""))
                if item is not None and rec.get("translated"):
                    item.translated = str(rec["translated"])
            if stamp is not None:
                last = stamp
    if out.started is None and out.entries:
        first = out.entries[0][1]
        out.started, out.started_epoch = first.time, first.epoch
    return out


def _int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _add_translation(out, by_seq, rec, stamp):
    seq = _int(rec.get("seq"))
    translated = str(rec.get("translated") or "")
    if seq is None or not translated:
        return
    quality = rec.get("quality") if rec.get("quality") in _RANK else "fast"
    item = by_seq.get(seq)
    if item is None:
        if stamp is None:
            return
        item = Caption(seq, stamp[0], stamp[1])
        by_seq[seq] = item
        out.captions.append(item)
        out.entries.append(("caption", item))
    if _RANK[quality] >= _RANK[item.quality]:
        item.translated, item.quality = translated, quality


# ---- 找 ----------------------------------------------------------------------------

def list_transcripts(directory=None):
    """文件夹里的记录文件，从旧到新（按文件名里的开始时间，同一秒再按修改时间）。"""
    folder = Path(directory) if directory else TRANSCRIPT_DIR
    found = []
    try:
        for entry in folder.iterdir():
            match = _STAMP.match(entry.name)
            if match and entry.name.endswith(FILE_SUFFIX) and entry.is_file():
                try:
                    mtime = entry.stat().st_mtime_ns
                except OSError:
                    mtime = 0
                found.append((match.group(1), mtime, entry.name, entry))
    except OSError:
        return []
    found.sort(key=lambda item: item[:3])
    return [item[3] for item in found]


def latest_transcript(directory=None):
    """最近开始的一场的记录文件；一场都没有返回 None。"""
    found = list_transcripts(directory)
    return found[-1] if found else None


def count_transcripts(directory=None):
    return len(list_transcripts(directory))
