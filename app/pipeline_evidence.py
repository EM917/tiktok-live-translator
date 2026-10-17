"""识别证据音频的 Pipeline 侧胶水。存什么、怎么存、上限怎么守在 app/asr_evidence.py；这里只把
Pipeline 手头的东西（这一场的会话状态、事件循环、识别输出、命中）接过去。

规矩（和 app/asr_evidence.py 文件头一致）：
  * 证据跟着「这一场自己的会话状态」走（Pipeline._new_session_state），不读 self.audit——晚到的
    旧任务写不进下一场的目录（CLAUDE.md 第七条）；
  * 每一步都包在 try 里：证据出任何错，识别、违禁词检测、审计、字幕、翻译一概不受影响；
  * 这个文件不产出界面文字，终端行一律中文。
"""
import asyncio

from . import asr_evidence


class AsrEvidenceMixin:
    def _evidence_round(self, sess):
        """拉流每一轮开始时调。这一场第一次时建证据记录（读一次设置、开场清一次上限），之后每轮换
        一个新的环形缓冲。证据关着、这一场没有审计文件时返回 None：不建缓冲，零开销。"""
        if not isinstance(sess, dict):
            return None
        if "evidence" not in sess:
            sess["evidence"] = self._new_evidence(sess)
        recorder = sess["evidence"]
        if recorder is not None:
            recorder.start_round()
        return recorder

    @staticmethod
    def _new_evidence(sess):
        path = getattr(sess.get("audit"), "path", None)
        if not path:
            return None
        try:
            cfg = asr_evidence.load_config()
            if not cfg["on"]:
                return None
            recorder = asr_evidence.EvidenceRecorder(path, cfg)
            recorder.prune_soon(asyncio.get_running_loop())
        except Exception as exc:
            print("[警告] 识别证据音频没开起来（{}），这一场不存；识别、检测和字幕不受影响"
                  .format(str(exc)[:160]))
            return None
        # 不打具体路径：目录名带开场时刻，打出来每场都不同，也没有新信息（审计旁边的 asr-evidence/）
        print("[信息] 识别证据音频：只存本机审计日志旁的 asr-evidence/（上限 {} MB / {} 个文件 / {} 天）"
              .format(cfg["max_mb"], cfg["max_files"], cfg["days"]))
        return recorder

    @staticmethod
    def _evidence_cut_meta(recorder, meta, segments):
        """切出一段时给它补上在环形缓冲里的位置（一次吐出好几段时对不上，不补）。"""
        if recorder is None or len(segments) != 1:
            return meta
        try:
            return recorder.cut_meta(meta, len(segments[0]))
        except Exception:
            return meta

    def _evidence_after_segment(self, seq, result, risk, hits, audio_end_ts, meta, sess):
        """识别出一段之后：记住它（手动重译要回头找），该存的交给线程池去写。"""
        recorder = sess.get("evidence") if isinstance(sess, dict) else None
        if recorder is None:
            return
        try:
            recorder.remember(seq, meta, result, risk, hits, audio_end_ts)
            recorder.save(seq, recorder.reasons_for(seq, risk, hits),
                          asyncio.get_running_loop())
        except Exception as exc:
            self._evidence_warn(exc)

    def _evidence_retranslate(self, seq):
        """中控在桌面点了「重译」：这是线上唯一能留住「通顺的错句」证据的信号（风险规则标不出这类段），
        在环形缓冲里找这一段存下来。找不到（超出缓冲、上一轮流）只计数，不报错。"""
        sess = getattr(self, "_session_state", None)
        recorder = sess.get("evidence") if isinstance(sess, dict) else None
        if recorder is None:
            return
        try:
            recorder.save(seq, ["retranslate"], asyncio.get_running_loop())
        except Exception as exc:
            self._evidence_warn(exc)

    def _evidence_warn(self, exc):
        if not getattr(self, "_evidence_warned", False):
            self._evidence_warned = True
            print("[警告] 识别证据音频出错（{}），识别、检测和字幕不受影响".format(str(exc)[:160]))

    @staticmethod
    def _evidence_summary(sess):
        """session_end.asr_risk.evidence：这一场存了多少、跳过多少（只有数字）。顺手放掉环形缓冲。
        这一场一轮流都没跑起来时没有这一栏。"""
        if not isinstance(sess, dict) or "evidence" not in sess:
            return None
        recorder = sess["evidence"]
        if recorder is None:
            return {"on": False}
        try:
            return recorder.summary()
        finally:
            recorder.close()
