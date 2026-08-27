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


class AuditLog:
    def __init__(self, room_url="", log_dir=None, extra=None):
        self._lock = threading.Lock()
        self._fh = None
        directory = Path(log_dir) if log_dir else LOG_DIR
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path = directory / "session-{}.jsonl".format(stamp)
            self._fh = self.path.open("a", encoding="utf-8")
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

    def dropped_audio(self, queue_depth=None):
        """识别跟不上时丢掉的音频段。漏报的第四种成因——这一段压根没进 ASR，
        不记下来事后就无法归因。"""
        self._write({
            "type": "audio_dropped",
            "at": datetime.now().isoformat(timespec="milliseconds"),
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
