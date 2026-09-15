"""审计日志：每一段音频的识别结果逐条落盘（JSONL）。

合规监听场景里，事后要能回答「昨天主播明明说了某个违禁词，为什么没报警」。
这有三种完全不同的原因，只有把被过滤掉的候选也记下来才能区分：
  A. Whisper 根本没听出来       —— raw 里也没有
  B. 听出来了但被质量过滤丢掉   —— rejected 里有，附原因
  C. ASR 正确但检测器没匹配上   —— text 里有、hits 为空
写入失败不抛给实时链路（日志不能拖累报警），但也不再静默：见 AuditLog._write。
"""
import json
import os
import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# 写不进去时先留在内存里、能写了再补写的记录类型：会话头、报警、会话尾。
# 逐段字幕和译文只计数不留——几个小时的磁盘满不能把内存吃光。
RETAIN_TYPES = ("session_start", "alert", "session_end")
RETAIN_MAX = 500


def _open_new(directory, stamp):
    """独占创建审计文件。同一秒起两场（双击「开始」、一秒内换主播）会得到同一个
    stamp：以前用追加模式，第二场写进第一场的文件，provenance 把两场混成一场归到
    第一个主播名下，按会话切语料的工具全部错归属。冲突就加 -2/-3 后缀。

    无缓冲的二进制句柄：每条记录要么整行落盘、要么一个字节都不留（见
    AuditLog._append_locked）。文本句柄在磁盘满时会把一部分记录留在 Python 的
    缓冲区里、丢掉另一部分，事后既数不清丢了几条，补写报警时还可能写出重复的。"""
    for n in range(1, 100):
        name = "session-{}{}.jsonl".format(stamp, "" if n == 1 else "-{}".format(n))
        path = directory / name
        try:
            return path, path.open("xb", buffering=0)
        except FileExistsError:
            continue
    raise OSError("同一秒内已有 99 个审计文件")


def clean_error(exc, limit=200):
    """错误文字写进审计或界面之前去掉 URL 的查询串（签名地址、token 常在里面）。"""
    text = exc if isinstance(exc, str) else str(exc)
    text = re.sub(r"(\w+://[^\s?#'\"]*)[?#][^\s'\"]*", r"\1", text)
    return text[:limit]


def _now_ms():
    return datetime.now().isoformat(timespec="milliseconds")


class AuditLog:
    def __init__(self, room_url="", log_dir=None, extra=None):
        self._lock = threading.Lock()
        self._fh = None
        self._pos = 0                    # 已整行落盘的字节数（写到一半失败时截回这里）
        self._torn = False               # 截回失败、文件里留着半行
        # 写入失败的状态。以前 _write 把 OSError 吞掉就完了：磁盘满的那段时间报警
        # 照常上屏，审计里一条没有，界面和自检都不知道。
        self.open_error = None           # 文件根本没建起来的原因（此时 path 为 None）
        self.failing = False             # 此刻是否写不进去
        self.failing_since = None
        self.last_error = ""
        self.write_failures = 0          # 本场累计写失败的记录条数
        self.lost_records = 0            # 其中没留在内存里、确定丢了的条数
        self.last_gap = None             # 最近一次恢复时写下的 audit_gap
        self._lost_in_outage = 0
        self._retained = deque()         # 等补写的会话头/报警/会话尾（编码好的整行）
        # 每次「开始写不进去」调一次，不是每条都调。可能在事件循环之外的线程里被调用，
        # 调用方自己切回循环（call_soon_threadsafe）
        self.on_write_error = None
        directory = Path(log_dir) if log_dir else LOG_DIR
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path, self._fh = _open_new(directory, stamp)
            # 记下这一场是哪个版本、哪份词表跑的。事后拿数字回来复盘时，
            # 「这个数是哪几个主播、哪个 commit、哪份词表产生的」要答得出来。
            # extra 是调用方掌握、这里拿不到的运行时事实（引擎、语言等）——
            # 2026-08-26 排查时正因为没记这些，只能靠延迟指纹反推那一场
            # 到底是 DeepL 还是本地 1.8B 在翻。
            from .provenance import code_commit, file_hash
            root = Path(__file__).resolve().parent.parent
            # 核心字段放后面：extra 与其撞名时核心字段赢。审计的骨架字段
            # 不能被调用方一个手滑的键名静默改写
            self._write(dict(extra or {},
                             **{"type": "session_start", "room_url": room_url,
                                "started_at": datetime.now().isoformat(timespec="seconds"),
                                "code_commit": code_commit(),
                                "glossary_hash": file_hash(root / "glossary.txt"),
                                "vocative_hash": file_hash(root / "app" / "vocative.py")}))
        except OSError as exc:
            if self._fh is None:
                self.path = None
                self.open_error = clean_error(exc)

    def _write(self, record):
        """写一条。失败不抛给调用方（实时链路不能被日志拖住），但要记下来：
          - failing / failing_since / last_error / write_failures 供管线每 10 秒查看；
          - 开始写不进去的那一刻调一次 on_write_error；
          - 会话头、报警、会话尾留在内存里（最多 RETAIN_MAX 条），其余只计数；
          - 之后第一次能写时先补一条 audit_gap（起止时间、丢了几条），再按原顺序补写
            留下的记录，最后才是这一条。
        每条都是整行直接落盘（无缓冲），崩溃也不丢最后几条。"""
        if self._fh is None:
            return
        try:
            data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        except ValueError:
            return
        started = None
        with self._lock:
            if self._fh is None:
                return
            try:
                if self.failing or self._retained:
                    self._drain_locked()
                self._append_locked(data)
            except (OSError, ValueError) as exc:
                started = self._note_failure_locked(record, data, exc)
        callback = self.on_write_error
        if started is not None and callback is not None:
            try:
                callback(started)
            except Exception:
                pass

    def _append_locked(self, data):
        """整行写进去，或者一个字节都不留。写到一半失败（磁盘只剩几个字节）就把文件
        截回这一行之前：半行 JSON 会让逐行读日志的工具在这里断掉。"""
        fh = self._fh
        if self._torn:
            data = b"\n" + data          # 上次没截回去的半行，先把它隔成单独一行
        view = memoryview(data)
        done = 0
        try:
            while done < len(data):
                n = fh.write(view[done:])
                if not n:
                    raise OSError("审计文件写入返回 0 字节")
                done += n
        except (OSError, ValueError):
            if done:
                try:
                    fh.truncate(self._pos)
                    fh.seek(self._pos)
                except (OSError, ValueError):
                    self._torn = True
                    try:
                        self._pos = fh.tell()
                    except (OSError, ValueError):
                        pass
            raise
        self._pos += done
        self._torn = False

    def _drain_locked(self):
        """写不进去之后第一次能写：先记 audit_gap，再补写留在内存里的记录。
        任何一步写不进去就抛出，留到下一条再试。"""
        if self.failing:
            now = _now_ms()
            gap = {"type": "audit_gap", "at": now, "from": self.failing_since, "to": now,
                   "lost_records": self._lost_in_outage,
                   "retained_records": len(self._retained),
                   "error": self.last_error}
            self._append_locked((json.dumps(gap, ensure_ascii=False) + "\n").encode("utf-8"))
            self.failing = False
            self.failing_since = None
            self._lost_in_outage = 0
            self.last_gap = gap
        while self._retained:
            self._append_locked(self._retained[0])
            self._retained.popleft()

    def _note_failure_locked(self, record, data, exc):
        """记一次写失败。只在「这一刻开始写不进去」时返回信息（给回调），否则 None。"""
        self.write_failures += 1
        self.last_error = clean_error(exc)
        started = not self.failing
        if started:
            self.failing = True
            self.failing_since = _now_ms()
        if record.get("type") in RETAIN_TYPES and len(self._retained) < RETAIN_MAX:
            self._retained.append(data)
        else:
            self._lost_in_outage += 1
            self.lost_records += 1
        if started:
            return {"since": self.failing_since, "error": self.last_error}
        return None

    def detached(self):
        """审计文件还在不在它的路径上。logs/ 在访达里被移走或删掉时写入并不报错，
        进的是路径上已经没有的那个文件——删掉的话关闭时就全没了。
        Windows 上打开着的文件删不掉也改不了名，只看路径在不在。"""
        fh, path = self._fh, getattr(self, "path", None)
        if fh is None or path is None:
            return False
        try:
            if os.name == "nt":
                return not os.path.exists(str(path))
            opened = os.fstat(fh.fileno())
            try:
                current = os.stat(str(path))
            except FileNotFoundError:
                return True
            return (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        except (OSError, ValueError):
            return False

    def segment(self, seq, result, audio_end_ts, asr_ms, hits):
        """一段音频的完整记录：接受的文本、被丢弃的候选及原因、命中的违禁词。"""
        self._write({
            "type": "segment",
            "seq": seq,
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "audio_end_ts": round(audio_end_ts, 3),
            "asr_ms": round(asr_ms, 1),
            "language": result.language,
            "text": result.text,
            "raw_text": result.raw_text,
            "rejected": result.rejected,
            "hits": hits,
        })

    def translation(self, seq, translated, translate_ms, ok, engine=None):
        """译文是后到的，单独记一条，按 seq 与上面的 segment 对应。

        不合并进 segment 是因为 segment 必须在识别一出来就落盘——报警证据
        不能等翻译。但审计只有西语原文是残的：事后复查一条报警时，中控要看
        的是「这句被翻成了什么」。翻译失败也记，否则日志里会静默缺一条。

        engine 记的是**这一条**实际用的引擎：会话中途可以在界面里换引擎，
        只看 session_start 会把换挡后的译文归到旧引擎头上。强译那边的
        translation_strong 从第一天就带 model 字段，快译缺这个，做引擎
        对比时快译的归属只能靠猜。"""
        self._write({
            "type": "translation",
            "seq": seq,
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "translated": translated,
            "translate_ms": round(translate_ms, 1),
            "ok": bool(ok),
            "engine": engine,
        })

    def translation_strong(self, seq, translated, translate_ms, ok, model,
                           trigger):
        """用最强模型重译的结果，**单独一种记录类型**。

        不复用上面的 translation：同一个 seq 会同时存在快译和强译两条，若类型
        相同就无法从日志判断哪条是哪个模型翻的——而这正是事后复核最需要区分的
        东西。收工后的批量重译工具写的也是这个类型，两条路径保持一致。

        trigger 说明这次重译是谁发起的：banned_term（命中违禁词自动升级）
        或 manual（中控点了「重译」）。"""
        self._write({
            "type": "translation_strong",
            "seq": seq,
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "translated": translated,
            "translate_ms": round(translate_ms, 1),
            "ok": bool(ok),
            "model": model,
            "trigger": trigger,
        })

    def resolve(self, record):
        """流地址解析的一次尝试：第几次、成没成、走了哪几层、各层结果与耗时。

        record 由 Pipeline._log_resolve 组好（type=resolve）。这条记录存在的
        理由：2026-09-06 一场直播前两次解析失败、第三次才成功，事后查不到
        任何证据——程序 stdout 指向 /dev/null，会话日志又只记开始/结束。
        只能靠「只有一个会话文件」+「重试循环只对一种错误生效」+「秒数
        对得上」倒推，换个失败模式就推不出来了。"""
        self._write(dict(record, at=datetime.now().isoformat(timespec="milliseconds")))

    def dropped_audio(self, queue_depth=None):
        """识别跟不上时丢掉的音频段。漏报的第四种成因——这一段压根没进 ASR，
        不记下来事后就无法归因。"""
        self._write({
            "type": "audio_dropped",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "queue_depth": queue_depth,
        })

    def asr_failed(self, segment_ms, error, queue_depth=None):
        """识别本身抛异常、这段音频没有进检测器——漏报的第五种成因。以前只有
        终端一行 print，打包运行时 stdout 指向 /dev/null，事后无从归因。"""
        self._write({
            "type": "asr_failed",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "segment_ms": round(segment_ms, 1),
            "error": error,
            "queue_depth": queue_depth,
        })

    def asr_overrun(self, asr_ms, segment_ms):
        """识别耗时超过音频时长的调用——复读跑飞的痕迹，事后排查丢段用。"""
        self._write({
            "type": "asr_overrun",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "asr_ms": round(asr_ms, 1),
            "segment_ms": round(segment_ms, 1),
        })

    def comment_source(self, state, detail="", raw=""):
        """弹幕连接状态的变化（连上、断开、被拒、组件更新）。弹幕不在报警链路上，
        记下来是为了事后答得出「弹幕是哪一刻坏的、之前几场好不好」——2026-09-14
        弹幕连接每次 HTTP 400 时，会话日志里一条弹幕状态都没有，这两个问题都答不上来。"""
        self._write({
            "type": "comment_source",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "state": state,
            "detail": (detail or "")[:300],
            # 服务端给的原始原因（握手被拒时的 Handshake-Msg、状态码）。面板上只有
            # 中文说明，事后要分清「这次 400」和「另一种 400」只能靠这一栏
            "raw": (raw or "")[:300],
        })

    def alert(self, hit):
        self._write({"type": "alert",
                     "at": datetime.now().isoformat(timespec="milliseconds"),
                     **hit})

    def ui_client_dropped(self, reason, buffered_bytes=None, clients_left=None):
        """一个界面页面收不下消息，被服务端断开（页面会自己重连并补回报警）。
        留痕是为了事后答得出「那段时间屏幕上的报警为什么晚到」。"""
        self._write({"type": "ui_client_dropped", "at": _now_ms(), "reason": reason,
                     "buffered_bytes": buffered_bytes, "clients_left": clients_left})

    def window_closed(self):
        """中控关掉了程序窗口（正在监听时要先确认），监听随之停止。先于停止流程写下：
        收尾要等识别线程和弹幕子进程，进程可能等不到 session_end 就退出。"""
        self._write({"type": "window_closed", "at": _now_ms()})

    def close(self):
        if self._fh is not None:
            try:
                self._write({"type": "session_end",
                             "ended_at": datetime.now().isoformat(timespec="seconds")})
                self._fh.close()
            except (OSError, ValueError):
                pass
            self._fh = None
