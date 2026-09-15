"""审计日志：每一段音频的识别结果逐条落盘（JSONL）。

合规监听场景里，事后要能回答「昨天主播明明说了某个违禁词，为什么没报警」。
这有三种完全不同的原因，只有把被过滤掉的候选也记下来才能区分：
  A. Whisper 根本没听出来       —— raw 里也没有
  B. 听出来了但被质量过滤丢掉   —— rejected 里有，附原因
  C. ASR 正确但检测器没匹配上   —— text 里有、hits 为空
写入失败一律静默忽略：日志不能拖累实时链路。
"""
import json
import threading
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def _open_new(directory, stamp):
    """独占创建审计文件。同一秒起两场（双击「开始」、一秒内换主播）会得到同一个
    stamp：以前用追加模式，第二场写进第一场的文件，provenance 把两场混成一场归到
    第一个主播名下，按会话切语料的工具全部错归属。冲突就加 -2/-3 后缀。"""
    for n in range(1, 100):
        name = "session-{}{}.jsonl".format(stamp, "" if n == 1 else "-{}".format(n))
        path = directory / name
        try:
            return path, path.open("x", encoding="utf-8")
        except FileExistsError:
            continue
    raise OSError("同一秒内已有 99 个审计文件")


class AuditLog:
    def __init__(self, room_url="", log_dir=None, extra=None):
        self._lock = threading.Lock()
        self._fh = None
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
        except OSError:
            self.path = None

    def _write(self, record):
        if self._fh is None:
            return
        try:
            with self._lock:
                self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._fh.flush()      # 崩溃也不能丢最后几条
        except (OSError, ValueError):
            pass

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

    def close(self):
        if self._fh is not None:
            try:
                self._write({"type": "session_end",
                             "ended_at": datetime.now().isoformat(timespec="seconds")})
                self._fh.close()
            except (OSError, ValueError):
                pass
            self._fh = None
