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
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from .redact import strip_query

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


def _iso_from_epoch(ts):
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def strip_url_queries(text, limit=300):
    """写进审计的错误文本：去掉网址里的 query。签名流地址的 sign/expire、HF 的
    token 都在 query 里——审计文件不能变成两周有效的拉流凭证。规则在 redact.strip_query。"""
    return strip_query(text, limit)


def clean_error(exc, limit=200):
    """错误文字写进审计或界面之前去掉 URL 的查询串（签名地址、token 常在里面）。"""
    return strip_query(exc, limit)


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

    def unwritten(self):
        """此刻写不进去的这一段：从什么时候起、最近的错误、确定丢了几条、还有几条留在内存里
        等补写。没在失败时返回 None。文件关掉之后留在内存里的也就丢了——停止时的提示按这个说。"""
        with self._lock:
            if not self.failing:
                return None
            return {"since": self.failing_since, "error": self.last_error,
                    "lost": self._lost_in_outage, "retained": len(self._retained)}

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

    def translation_engine_error(self, engine, status, error=None, model=None):
        """翻译引擎的 HTTP 回应说明它这会儿用不了：本地 Ollama 连续几次非 200（没有
        这个模型、载入失败……），或远程引擎拒绝了密钥/模型（401/403/404）。translation
        记录里只有 ok=false，没有这一条就答不出「这一场的译文为什么整段是空的」。

        error 是 Ollama 的原话；远程引擎不记原话（它们的错误说明里可能带打码后的
        密钥片段）。同一个引擎在同一场里只记一次，中间成功过才会再记。"""
        self._write({
            "type": "translation_engine_error",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "engine": engine,
            "model": model,
            "status": status,
            "error": (error or "")[:200] or None,
        })

    def translation_cooldown(self, engine, status, seconds):
        """翻译接口回了 429、程序暂停请求的那一刻。暂停期间每条字幕都是 ok=false，
        有这一条才分得清「接口让等一会儿」和「引擎坏了」。中间成功过才会再记。"""
        self._write({
            "type": "translation_cooldown",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "engine": engine,
            "status": status,
            "seconds": seconds,
        })

    def alert_translation(self, alert_ids, model, ok, ms, fallback, why,
                          downgraded_for_backlog=False, downgraded_for_busy=False,
                          error=None):
        """报警上下文的中文翻译，一次扫描记一条。

        **不复用 translation_strong**：离线重译工具（tools/retranslate_audit.py）见到
        某个 seq 有 ok 的 translation_strong 就跳过这段，而报警编号和字幕 seq 是两套
        号——混用的话，第 N 条报警会挡住第 N 段字幕的重译。

        model 是最后给出（或没给出）译文的那个模型；fallback=真表示强模型没译出来或
        译文不像译文，改用了常驻引擎；downgraded_for_backlog / downgraded_for_busy
        表示一开始就没用强模型（识别正在积压 / 另一条报警正占着强模型）。error 是
        强模型那次失败时 Ollama 的回应（状态码和原话），没有就是 None。"""
        self._write({
            "type": "alert_translation",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "alert_ids": list(alert_ids),
            "model": model,
            "ok": bool(ok),
            "ms": round(ms, 1),
            "fallback": bool(fallback),
            "why": why or "",
            "downgraded_for_backlog": bool(downgraded_for_backlog),
            "downgraded_for_busy": bool(downgraded_for_busy),
            "error": (error or "")[:200] or None,
        })

    def model_pull(self, state, model, error=None):
        """本地翻译模型的下载。state：start / done / failed，以及 deferred（直播中不
        下载，停止后再下）和 in_progress（这一场开始时已有下载在跑）。下载和拉流抢
        同一条网络，事后要能把一段音频中断和它对上号——以前会话日志里没有下载的
        任何痕迹。下载本身跨场次，这条记在事件发生时正开着的那场里。"""
        self._write({
            "type": "model_pull",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "state": state,
            "model": model,
            "error": (error or "")[:300] or None,
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

    def asr_config(self, config):
        """这一场实际在听的识别配置（backend/model/device/compute_type/note，改用过
        CPU 时带上来龙去脉）。以前只 print 一行，打包运行时 stdout 指向 /dev/null，
        复盘「这场为什么漏报」时答不出当时是 GPU large-v3 还是 CPU turbo 在听。"""
        self._write(dict(config, type="asr_config",
                         at=datetime.now().isoformat(timespec="milliseconds")))

    def asr_memory(self, active_mb, cache_mb, peak_mb):
        """MLX 分配器的读数（MB）：在用、缓冲缓存、峰值；读不到的是 null。模型就绪时一条，
        之后每 5 分钟一条。2026-09-17 监听进程 7.5 GB 里有 6948 MB 是 Metal 缓冲，当时只能
        靠 `footprint` 从外面看；有了这条，缓存随时间怎么涨在会话日志里就能看到。"""
        self._write({
            "type": "asr_memory",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "active_mb": active_mb, "cache_mb": cache_mb, "peak_mb": peak_mb,
        })

    def asr_load_failed(self, backend, model, device, error):
        """识别模型没能加载：这一场一段都不会识别。以前只有界面上一句话。"""
        self._write({
            "type": "asr_load_failed",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "backend": backend, "model": model, "device": device,
            "error": strip_url_queries(error),
        })

    def asr_backend_fallback(self, from_, to, error, **extra):
        """识别出错后改用了另一套配置（to=None 表示没有可以安全改用的，没换）。"""
        self._write(dict(
            {k: (strip_url_queries(v) if k.endswith("error") else v)
             for k, v in extra.items()},
            type="asr_backend_fallback",
            at=datetime.now().isoformat(timespec="milliseconds"),
            **{"from": from_, "to": to, "error": strip_url_queries(error)}))

    def asr_stalled(self, inflight_sec, backlog_sec, dropped):
        """一段音频识别了很久还没返回。asr_overrun 要等调用返回才写、asr_failed 只在
        抛异常时写——调用一直不返回时，会话日志里只剩一条条 audio_dropped。"""
        self._write({
            "type": "asr_stalled",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "inflight_sec": round(inflight_sec, 1),
            "backlog_sec": round(backlog_sec, 1),
            "dropped": dropped,
        })

    def health(self, level, backlog_sec, reason="backlog", text="", dropped=None,
               asr_failed=None):
        """检测健康状态的变化（积压、识别出错、识别卡住、改用 CPU）。只在变化时写。
        dropped 是积压挤掉的段数，asr_failed 是识别出错没检测的段数，两者分开记。"""
        self._write({
            "type": "health",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "level": level, "reason": reason,
            "backlog_sec": round(backlog_sec or 0.0, 1),
            "dropped": dropped,
            "asr_failed": asr_failed,
            "text": (text or "")[:300],
        })

    def selfcheck(self, checks, summary, full=True):
        """自检结论。full=False 表示只记了等级有变化的那几行。自检本来就是为了抓静默
        降级，结论却只上界面、只 print——事后说不出这场跑的时候哪项能力是坏的。"""
        self._write({
            "type": "selfcheck",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "full": bool(full),
            "summary": summary,
            "checks": [{"name": c.get("name"), "level": c.get("level"),
                        "detail": strip_url_queries(c.get("detail"), 500)}
                       for c in checks],
        })

    def terms_changed(self, digest):
        """直播中违禁词表文件被改了（新内容要「停止→开始」后才生效）。"""
        self._write({
            "type": "terms_changed",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "hash": digest,
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

    # ---- 拉流这一侧的观察：断在哪、断了多久、程序有没有在跑、为什么结束 ----
    # 以前这些只打在终端上，而打包运行时 stdout 指向 /dev/null：事后对着一段没有字幕的
    # 时间，分不清是主播没说话、流断了、网断了，还是电脑睡着了。所有错误文本里的 URL
    # 先去掉 query（签名地址两周内拿着就能拉流）。

    def stream_break(self, reason, returncode=None, audio_sec=0.0, got_audio=False,
                     reconnect_no=0, stderr_tail="", error=None):
        """一轮拉流结束。reason：eof（读到流尾）/ stall（20 秒没有任何字节，看门狗断开）/
        clock_gap（时钟对账发现电脑休眠过，程序主动断开重连）/ cancelled（停止或换场）/
        error（这一轮内部出错）。returncode 是 ffmpeg 收尾后的退出码，原样记。"""
        rec = {
            "type": "stream_break",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "reason": reason,
            "returncode": returncode,
            "audio_sec": round(float(audio_sec or 0.0), 1),
            "got_audio": bool(got_audio),
            "reconnect_no": reconnect_no,
            "stderr_tail": strip_query(stderr_tail)[-300:],
        }
        if error:
            rec["error"] = strip_query(error, 200)
        self._write(rec)

    def stream_resumed(self, deaf_sec, reconnect_no=None):
        """断流之后重新收到第一帧音频：中间有多少秒什么都没听到。"""
        self._write({
            "type": "stream_resumed",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "deaf_sec": round(max(0.0, float(deaf_sec)), 1),
            "reconnect_no": reconnect_no,
        })

    def audio_heartbeat(self, audio_sec, wall_sec, speech_frames, segments):
        """每 60 秒一条：这段时间实际收到多少秒音频（wall_sec 是这个窗口经过的秒数，
        按单调时钟算，电脑睡着的时间不计）、多少帧超过识别门限、切出几段。
        用来分开「主播没说话」「音频没到」「程序没在跑」三种空白。"""
        self._write({
            "type": "audio_heartbeat",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "audio_sec": round(float(audio_sec), 1),
            "wall_sec": round(float(wall_sec), 1),
            "speech_frames": int(speech_frames),
            "segments": int(segments),
        })

    def stream_audio(self, audio_sec, segments_cut, peak_rms, final=False):
        """一轮拉流的累计：收到的音频秒数、切出送去识别的段数、帧 RMS 峰值。
        每 5 分钟一条、每轮结束一条（final=True）。"""
        self._write({
            "type": "stream_audio",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "audio_sec": round(float(audio_sec), 1),
            "segments_cut": int(segments_cut),
            "peak_rms": round(float(peak_rms), 1),
            "final": bool(final),
        })

    def clock_gap(self, from_ts, to_ts, gap_sec, wall_sec, mono_sec, clocks_diverged):
        """统计循环发现墙钟比预期多走了一大截：这段时间程序没有运行。
        clocks_diverged=True 表示墙钟走了、单调时钟没走（macOS/Linux 上电脑休眠或挂起时
        就是这样）；False 表示两个钟一起走多了（Windows 休眠、或事件循环被卡住）。
        时钟被往前拨也会触发，所以只叫 clock_gap，不叫「休眠」。"""
        self._write({
            "type": "clock_gap",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "from": _iso_from_epoch(from_ts),
            "to": _iso_from_epoch(to_ts),
            "gap_sec": round(float(gap_sec), 1),
            "wall_sec": round(float(wall_sec), 1),
            "mono_sec": round(float(mono_sec), 1),
            "clocks_diverged": bool(clocks_diverged),
        })

    def network_down(self, since_ts, why=""):
        """重连前的连通性探测（DNS + TCP 到 www.tiktok.com:443）失败。why 只有异常类名。"""
        self._write({
            "type": "network_down",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "since": _iso_from_epoch(since_ts),
            "why": strip_query(why, 120),
        })

    def network_up(self, down_sec):
        self._write({
            "type": "network_up",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "down_sec": round(float(down_sec), 1),
        })

    def host_wait(self, status, waited_sec, outcome, trigger="status", why=None):
        """重连时接口说房间不在播，但不是「已结束」（状态 4）：程序在等、每分钟问一次。
        outcome：started / live / ended / timeout；trigger=clock_gap 是电脑休眠过之后
        对「已结束」判定做的那次复查。status 是房间接口原样返回的值。"""
        rec = {
            "type": "host_wait",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "status": status,
            "waited_sec": int(round(float(waited_sec))),
            "outcome": outcome,
            "trigger": trigger,
        }
        if why:
            rec["why"] = strip_query(why, 120)
        self._write(rec)

    def internal_error(self, exc, tb=""):
        """直播任务里没预料到的异常。以前只有终端里的堆栈（打包运行时没人看得到），
        界面上那句「内部错误，已停止」刷新就没了。堆栈只留最后 2000 个字符。"""
        self._write({
            "type": "internal_error",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "exc_type": type(exc).__name__,
            "message": strip_query(str(exc), 300),
            "traceback": strip_query(tb)[-2000:],
        })

    def update_stop(self, from_version, to_version):
        """一键更新暂停了这场监听。session_end 说「为什么停」，这条说「从哪个版本更到
        哪个版本」；下一场 session_start 的 resumed_after 说「什么时候接上的」。以前
        审计里只剩一条光秃秃的 session_end，事后分不清是中控点了停止还是更新停的。"""
        self._write({
            "type": "update_stop",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "from_version": from_version,
            "to_version": to_version,
        })

    def component_updated(self, name, from_version, to_version, reason):
        """会话进行中后台升级了解析组件（yt-dlp / curl_cffi）。解析行为变了的时候，
        事后要答得出「升没升、升之前之后各是哪个版本」。"""
        self._write({
            "type": "component_updated",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "name": name,
            "from": from_version,
            "to": to_version,
            "reason": reason,
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

    def close(self, reason=None, **fields):
        """写 session_end 并关文件。reason：这一场为什么结束（offline / reconnect_exhausted /
        user_stop / internal_error …），fields 是这个原因附带的观察（如 silent、budget）。
        骨架字段优先：fields 里撞名的键盖不掉 type/ended_at/reason。"""
        if self._fh is not None:
            try:
                rec = {"type": "session_end",
                       "ended_at": datetime.now().isoformat(timespec="seconds")}
                if reason:
                    rec["reason"] = reason
                    for key, value in fields.items():
                        rec.setdefault(key, value)
                self._write(rec)
                self._fh.close()
            except (OSError, ValueError):
                pass
            self._fh = None
