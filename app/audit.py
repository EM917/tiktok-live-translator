"""审计日志：每一段音频的识别结果逐条落盘（JSONL）。

合规监听场景里，事后要能回答「昨天主播明明说了某个违禁词，为什么没报警」。
这有三种完全不同的原因，只有把被过滤掉的候选也记下来才能区分：
  A. Whisper 根本没听出来       —— raw 里也没有
  B. 听出来了但被质量过滤丢掉   —— rejected 里有，附原因
  C. ASR 正确但检测器没匹配上   —— text 里有、hits 为空
写入失败一律静默忽略：日志不能拖累实时链路。
"""
import json
import re
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


_URL_QUERY_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s?#'\"<>]*)[?#][^\s'\"<>]*")


def strip_url_queries(text, limit=300):
    """写进审计的错误文本：去掉网址里的 query。签名流地址的 sign/expire、HF 的
    token 都在 query 里——审计文件不能变成两周有效的拉流凭证。"""
    return _URL_QUERY_RE.sub(r"\1", str(text or ""))[:limit]


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
