"""识别风险标记 v1：一段识别结果「像不像出了大问题」。纯函数，不做 IO，不写界面文字。

来历（2026-10-16，10-14 rec_01 的 608 段 large-v3 离线重跑 + 线上 20 场审计，没有人工真值）：
  * 能用的只有几条「粗故障」信号：9 秒硬切里有声很久却只识别出几个词（套话顶替整段真话的那类，
    10-14 报告里挑出的 8 段全中——但那 8 段本来就是按词数挑的，这一条在它们上面是循环论证）、
    avg_logprob 很低、avg_logprob 是 NaN、压缩比偏高（复读）、整段只有片尾套话；
  * 通顺的错句（20:47 那次「ponen la oficina」一类）抓不到：rec_01 上和它同类的 135 段，0 段被标。
    所以「没被标」**不等于识别对了**，界面、文档都不能这么暗示；
  * 常数是看着 rec_01 定的，现在冻结成 RISK_VERSION = 1：之后只在新场次上评估、不重调，
    要改就升版本并换一批没参与调参的场次去评（CLAUDE.md 第四条）。

只加不改：违禁词检测仍然先在 raw_text 上扫、输入一个字不动；过滤（asr._fold）和解码参数一个不改。
这里算出的东西只进审计（每段的 asr 子对象）、证据音频的触发条件、以及默认关着的界面角标。

审计里只放数字和 ASCII 代码，不放文字，也不经过 L()：中英两种界面下审计逐字节相同（G8）。
"""
import hashlib
import math
import os

from .asr import _is_all_hallucination, _norm_for_hallucination

RISK_VERSION = 1

# ---- 冻结的常数（v1）。改任何一个都要同时把 RISK_VERSION 升上去，tests/test_asr_risk.py 钉着 ----
# avg_logprob：低于 −0.8 记 low_conf（严格小于）。连续分 s_lp = clamp((−0.5 − lp) / 0.6)，lp=−0.8 时 0.5。
# rec_01 上 −0.8 标 6 段（1.0%）、和 turbo 写得一样的段 0 段被标；−0.6 就标 21 段、其中 4 段两模型一致
LP_FLAG_BELOW = -0.8
LP_SCORE_ZERO = -0.5
LP_SCORE_SPAN = 0.6
# 只有这些 (backend, model) 上 lp 两条（low_conf、nan_lp）才生效：阈值是在 mlx large-v3 上看的，
# turbo 和 ct2 的 logprob 分布没量过，套过去只会乱标
LP_MODELS = (("mlx", "large-v3"),)
# 每有声秒的词数（raw_text 按空白切的词数 ÷ 有声秒数）：9 秒硬切、有声帧 ≥ 60%、段长 ≥ 8.5 秒时，
# 低于 1.0 记 few_words。连续分 s_words = clamp((1.5 − wpvs) / 1.0)，wpvs=1.0 时 0.5。
# 词数用 raw_text（过滤前）：套话顶替的那几段，raw_text 里就只剩套话
WORDS_FLAG_BELOW = 1.0
WORDS_SCORE_ZERO = 1.5
WORDS_SCORE_SPAN = 1.0
WORDS_CUT = "max"
WORDS_MIN_VOICED = 0.6
WORDS_MIN_MS = 8500
# 压缩比：高于 2.0 记 loop（复读）。连续分 s_loop = clamp((cr − 1.8) / 0.4)，cr=2.0 时 0.5。
# 过滤器自己在 2.4 才丢（asr._fold），2.0–2.4 之间的复读会上屏
CR_FLAG_ABOVE = 2.0
CR_SCORE_ZERO = 1.8
CR_SCORE_SPAN = 0.4
# 有声秒 = 有声帧数 × 帧长。和 app/audio.py 的 FRAME_SEC 是同一个数（测试钉着）；不 import 它，
# 免得这个纯函数模块拖进拉流那一套
FRAME_SEC = 0.1

# 会挂界面角标的标记（还要这段有字幕，见 assess）
CHIP_FLAGS = ("low_conf", "nan_lp", "few_words", "loop")
# 只记审计（和证据）、不挂角标的：
#   outro_only —— 整段只有片尾套话。按当前过滤器（82e7446）整段套话里有片尾那一层的不看置信度直接拦，
#                 还能上屏的只剩「主播也真会说、模型把握也不低」的；长段上的顶替 few_words 已经能抓到，
#                 短段上一句很有把握的「¡Gracias!」挂「识别不确定」多半是冤枉。rec_01 上它对角标的贡献是 0；
#   relabel    —— 检测语言不在允许列表里、整段重转过（rec_01 上 3 段里 1 段被判输）
AUDIT_ONLY_FLAGS = ("outro_only", "relabel")
# 存证据音频的标记：v1 规则本身（角标那几条 + 整段套话），不管字幕有没有上屏
EVIDENCE_FLAGS = CHIP_FLAGS + ("outro_only",)

# 总闸：TLT_ASR_SAFEGUARDS=off 关掉风险标记、证据音频和角标；每段的识别数字照记（只记数，不改行为）
MASTER_SWITCH_ENV = "TLT_ASR_SAFEGUARDS"
_OFF_WORDS = ("off", "0", "false", "no")


def master_switch_on(environ=None):
    """总闸开着吗。没设或设成别的值都算开；只认 off / 0 / false / no（不分大小写）。"""
    value = (environ if environ is not None else os.environ).get(MASTER_SWITCH_ENV, "")
    return str(value).strip().lower() not in _OFF_WORDS


def constants():
    """写进 asr_config 的那一版规则：阈值改了，从审计里看得出这一场是哪一版。"""
    return {"v": RISK_VERSION,
            "lp_below": LP_FLAG_BELOW, "lp_zero": LP_SCORE_ZERO, "lp_span": LP_SCORE_SPAN,
            "lp_models": ["{}/{}".format(b, m) for b, m in LP_MODELS],
            "wpvs_below": WORDS_FLAG_BELOW, "wpvs_zero": WORDS_SCORE_ZERO,
            "wpvs_span": WORDS_SCORE_SPAN, "cut": WORDS_CUT, "voiced_min": WORDS_MIN_VOICED,
            "ms_min": WORDS_MIN_MS,
            "cr_above": CR_FLAG_ABOVE, "cr_zero": CR_SCORE_ZERO, "cr_span": CR_SCORE_SPAN,
            "chip": list(CHIP_FLAGS), "audit_only": list(AUDIT_ONLY_FLAGS)}


def config_record(enabled=True):
    """asr_config 里的 asr_risk 一栏。总闸关着时 on=false：这一场的段没有 risk / flags。"""
    return dict(constants(), on=bool(enabled))


def prompt_sha16(prompt):
    """热词提示的指纹（sha256 前 16 位）。换提示词 logprob 的分布就会动，阈值不能跨提示词比，
    所以审计要记是哪一份；只记指纹，不记提示词本身（里面是主播和品牌名）。没有提示是 None。"""
    if not prompt:
        return None
    return hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()[:16]


def _finite(value):
    """有限的 float 原样返回，NaN / inf / 不是数都是 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _round(value, digits=3):
    value = _finite(value)
    return None if value is None else round(value, digits)


def _clamp(value):
    return max(0.0, min(1.0, value))


class Risk:
    """assess 的结果。audit 是写进审计的 asr 子对象；flags 是命中的标记代码；chip 是这段字幕
    该不该挂角标（角标开关另说）；evidence 是该不该存证据音频。"""

    __slots__ = ("audit", "flags", "chip", "evidence", "score", "shown", "nan")

    def __init__(self, audit, flags, chip, evidence, score, shown, nan=False):
        self.audit = audit
        self.flags = flags
        self.chip = chip
        self.evidence = evidence
        self.score = score
        self.shown = shown
        self.nan = nan          # avg_logprob 是 NaN / inf（审计里写成 null，这里留个记号）


def assess(result, segment_ms=None, cut_meta=None, backend=None, model=None, enabled=True):
    """算一段的风险。result 是 ASRResult（或只有 text / raw_text 的替身：测试和老代码里的假识别器
    不带 signals，取不到的一律当没有）。cut_meta 是切段器的 last_cut，没有是 None。

    enabled=False（总闸关着）时只记数字，不出 risk / flags，也不触发角标和证据。"""
    signals = getattr(result, "signals", None) or {}
    raw_text = getattr(result, "raw_text", "") or ""
    text = getattr(result, "text", "") or ""
    meta = cut_meta if isinstance(cut_meta, dict) else {}

    ms = _finite(segment_ms)
    cut = meta.get("cut") if isinstance(meta.get("cut"), str) else None
    frames = meta.get("frames")
    speech = meta.get("speech_frames")
    voiced = None
    voiced_sec = None
    if isinstance(frames, int) and isinstance(speech, int) and frames > 0:
        voiced = speech / float(frames)
        voiced_sec = speech * FRAME_SEC
    words = len(raw_text.split())
    wpvs = words / voiced_sec if voiced_sec else None

    lp_raw = signals.get("lp")
    lp_raw = None if isinstance(lp_raw, bool) or not isinstance(lp_raw, (int, float)) else float(lp_raw)
    lp = _finite(lp_raw)
    is_nan = lp_raw is not None and lp is None
    cr = _finite(signals.get("cr"))

    record = {"v": RISK_VERSION,
              "ms": None if ms is None else int(round(ms)),
              "cut": cut,
              "voiced": _round(voiced),
              "words": words,
              "wpvs": _round(wpvs),
              "lp": _round(lp),
              "nsp": _round(signals.get("nsp")),
              "cr": _round(cr),
              "tok": signals.get("tok") if isinstance(signals.get("tok"), int) else None,
              "t": _round(signals.get("t")),
              "calls": signals.get("calls") if isinstance(signals.get("calls"), int) else None,
              "win": signals.get("win") if isinstance(signals.get("win"), int) else None}
    if not enabled:
        return Risk(record, [], False, False, None, bool(text), is_nan)

    flags, scores = [], [0.0]
    lp_active = (backend, model) in LP_MODELS
    if lp_active and lp is not None:
        scores.append(_clamp((LP_SCORE_ZERO - lp) / LP_SCORE_SPAN))
        if lp < LP_FLAG_BELOW:
            flags.append("low_conf")
    if lp_active and is_nan:
        scores.append(1.0)
        flags.append("nan_lp")
    if (cut == WORDS_CUT and voiced is not None and voiced >= WORDS_MIN_VOICED
            and ms is not None and ms >= WORDS_MIN_MS and wpvs is not None):
        scores.append(_clamp((WORDS_SCORE_ZERO - wpvs) / WORDS_SCORE_SPAN))
        if wpvs < WORDS_FLAG_BELOW:
            flags.append("few_words")
    if cr is not None:
        scores.append(_clamp((cr - CR_SCORE_ZERO) / CR_SCORE_SPAN))
        if cr > CR_FLAG_ABOVE:
            flags.append("loop")
    # 和 _fold 用同一对函数判「整段都是套话」：表改了两边一起变
    if raw_text.strip() and _is_all_hallucination(_norm_for_hallucination(raw_text)):
        scores.append(1.0)
        flags.append("outro_only")
    if getattr(result, "relabeled_from", ""):
        flags.append("relabel")

    score = round(max(scores), 3)
    record["risk"] = score
    record["flags"] = flags
    chip = bool(text) and any(f in CHIP_FLAGS for f in flags)
    evidence = any(f in EVIDENCE_FLAGS for f in flags)
    return Risk(record, flags, chip, evidence, score, bool(text), is_nan)


class RiskCounter:
    """一场的计数，写进 session_end.asr_risk。只有数字，挂在这一场自己的会话状态上
    （Pipeline._new_session_state），晚到的旧任务数不进下一场（CLAUDE.md 第七条，my_audit 的道理）。

      segments       这一场算过风险的段数
      shown          其中有字幕的（text 非空）
      flagged        命中 v1 规则（EVIDENCE_FLAGS，含整段套话）的段数，不管有没有上屏
      shown_flagged  会挂角标的段数（有字幕、且命中 CHIP_FLAGS）：角标率 = shown_flagged / shown
      by_flag        每个标记代码各命中几段（含只记审计的 relabel）
      nan_lp         avg_logprob 不是有限数的段（不管模型，NaN 本身就值得数）
    证据音频的计数（evidence_*）由 app/asr_evidence.py 往 extra 里加。"""

    def __init__(self, enabled=True):
        self.enabled = bool(enabled)
        self.segments = 0
        self.shown = 0
        self.flagged = 0
        self.shown_flagged = 0
        self.nan_lp = 0
        self.by_flag = {}
        self.extra = {}

    def add(self, risk):
        if risk is None:
            return
        self.segments += 1
        if risk.shown:
            self.shown += 1
        if risk.evidence:
            self.flagged += 1
        if risk.chip:
            self.shown_flagged += 1
        if risk.nan:
            self.nan_lp += 1
        for flag in risk.flags:
            self.by_flag[flag] = self.by_flag.get(flag, 0) + 1

    def summary(self):
        out = {"v": RISK_VERSION, "on": self.enabled, "segments": self.segments,
               "shown": self.shown, "flagged": self.flagged,
               "shown_flagged": self.shown_flagged, "nan_lp": self.nan_lp,
               "by_flag": dict(sorted(self.by_flag.items()))}
        out.update(self.extra)
        return out
