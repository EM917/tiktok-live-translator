"""faster-whisper 语音识别封装（同步接口，由 Pipeline 放进线程池执行）。

精度策略：
  * beam search（默认 beam_size=5）代替贪心解码；
  * 滚动上下文：把最近识别出的文本作为 initial_prompt 传给下一段。
    **默认关闭**——本意是帮模型接住被切断的句子，实测却是净损害：模型自己的
    输出被喂回去后，一旦开始重复就自我强化，在 4 分钟真实西语直播上实测
    （mlx large-v3，同一段音频对照）：
        重复率 30–95%  → 2–6%
        词召回 35–78%  → 84–89%
        最慢单次识别 38.9s → 5.2s
    那个 38.9 秒是复读死循环：一个 4.5 秒的片段解码了 39 秒，生产中足以让
    音频队列溢出、丢段漏词。需要它的场景可用 --context 打开;
  * 质量过滤：压缩比过高（复读机式垃圾）或平均置信度过低（多为背景音乐
    误识别）的段直接丢弃，宁缺毋滥；
  * 两处省解码的改动（生成上限、语言屏蔽）带开关、默认关，见 DECODE_TOKEN_CAP 前的说明。
"""
import gc
import importlib
import math
import os
import re
import sys
import traceback
import unicodedata
from dataclasses import dataclass, field
from typing import List

import numpy as np


def _norm_for_hallucination(text):
    """比对幻觉短语用的归一化：去重音、去标点、压空白、转小写。"""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # 词内的点（amara.org、3.5）不是句号，先摘掉，免得拆句时把一句拆成两句：
    # 表里三条 amara.org 条目就因此从未命中过——2026-09-17 线上一条英文的
    # 「Subtitles by the Amara.org community」原样上了屏
    text = re.sub(r"(?<=\w)\.(?=\w)", "", text)
    # 保留句末标点（拆句要用），只去掉其它标点与首尾空白
    text = re.sub(r"[^\w\s.!?！？。]", " ", text)
    return " ".join(text.split()).strip()


def _hallucination_key(text):
    """逐句比对用的键：去空白与句末标点。表和识别文本两边都走同一条路，
    表里带 "!" 或 "." 的条目才不会成为永远比不中的死条目。"""
    return re.sub(r"[\s.!?！？。]", "", text)


def _is_subtitle_credit(normalized):
    """字幕组署名（amara.org 一族）：没有任何主播会亲口说这句，不看置信度直接丢。"""
    return "amaraorg" in normalized


# 2026-09-17 一天的会话里，显示出来的字幕有 40 条不是拉丁字母：韩文「감사합니다」14 条、
# 阿拉伯文「المترجمات لكثير من الاشتراك في القناة」14 条、俄文 5 条、日文 3 条……直播间只说
# 西语和英语。这些都是 Whisper 在无人声段吐出的训练集套话，靠一句句往表里加永远加不完，
# 所以按**文字系统**判：一段的文字系统在最近接受的字幕里一次都没出现过，就先扣下；
# 同一种文字连着来两段才放行——真换了语言的直播只损失第一段的显示，违禁词检测用的
# raw_text 不受影响。
_SCRIPT_NAMES = (("ARABIC", "arabic"), ("CYRILLIC", "cyrillic"), ("HANGUL", "hangul"),
                 ("HIRAGANA", "kana"), ("KATAKANA", "kana"), ("CJK", "cjk"),
                 ("THAI", "thai"), ("HEBREW", "hebrew"), ("LATIN", "latin"))
SCRIPT_HISTORY = 12      # 只看最近这么多条接受的字幕
SCRIPT_MIN_HISTORY = 6   # 历史不足这么多条时不判（开播头几段没有参照）


def _script_of(text):
    """一段文字的主要文字系统；没有字母就是 None。数字与标点不算。"""
    counts = {}
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        for key, label in _SCRIPT_NAMES:
            if key in name:
                counts[label] = counts.get(label, 0) + 1
                break
        else:
            counts["other"] = counts.get("other", 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


_URL_RE = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|net|org|es|mx|co|ar|gov|edu|info|tv)\b)",
                     re.IGNORECASE)
CREDIT_MAX_WORDS = 8


def _is_url_credit(text):
    """「Más información www.cdc.gov.ar」「www.albertosanagustin.com」这类带网址的短句：
    Whisper 训练集里的片尾署名，2026-09-17 一天里 5 条上了屏。带货主播念网址会说
    「punto com」，识别出来不会是一个带点的域名；长句里夹一个网址不算（可能真在念）。"""
    if not _URL_RE.search(text):
        return False
    return len(text.split()) <= CREDIT_MAX_WORDS


# 字母或数字（任意文字系统都算）。整段一个都没有——多为 Whisper 在音乐/
# 噪声段吐出的裸感叹号——就不是话：实测一场里 13 段 "!" 被当成字幕原样
# 翻译上屏，观感全是「翻错了」。数字要保留：价格和数量是合规要看的内容。
_WORD_RE = re.compile(r"[^\W_]", re.UNICODE)

_HALLUCINATION_KEYS = None


def _is_all_hallucination(normalized):
    """整段是否全部由幻觉套话构成。

    按句拆开逐句判定，因为 Whisper 常把好几句片尾语粘在一段里
    （"¡Gracias por ver el video! ¡Suscríbete al canal!"）——只比对整段
    的话这种组合就漏了过去，实测正是这样漏的。
    只有**每一句**都是幻觉才丢弃，所以主播真的在句子里说 gracias 不会被误杀。
    """
    global _HALLUCINATION_KEYS
    if _HALLUCINATION_KEYS is None:
        _HALLUCINATION_KEYS = _table_keys(_HALLUCINATIONS)
    parts = _sentence_keys(normalized)
    return bool(parts) and all(p in _HALLUCINATION_KEYS for p in parts)


_OUTRO_KEYS = None


def _has_youtube_outro(normalized):
    """有没有一句属于 _YOUTUBE_OUTROS（训练集的片尾/署名，主播几乎不会整段只说这句）。
    只在 _is_all_hallucination 已经成立时用：整段都是套话，又有一句属于这一层。"""
    global _OUTRO_KEYS
    if _OUTRO_KEYS is None:
        _OUTRO_KEYS = _table_keys(_YOUTUBE_OUTROS)
    return any(p in _OUTRO_KEYS for p in _sentence_keys(normalized))


def _table_keys(table):
    return {_hallucination_key(_norm_for_hallucination(h)) for h in table}


def _sentence_keys(normalized):
    """按句末标点拆句，每句取比对键；空句不算。"""
    return [_hallucination_key(p) for p in re.split(r"[.!?！？。]+", normalized or "")
            if p.strip()]


@dataclass
class ASRResult:
    """一段音频的识别结果。

    text     —— 通过质量过滤的文本，用于字幕展示；
    raw_text —— 含被过滤掉的部分，**违禁词检测用这个**：漏报的代价远高于误报，
                宁可扫到一句置信度低的疑似违禁词，也不要因为过滤而漏掉；
    rejected —— 被丢弃的候选及原因，用于审计（事后能区分「没听出来」和
                「听出来了但被过滤」——这是两个完全不同的问题）；
    relabeled_from —— 主播语言设了允许列表（如 "es,en"）时，检测到的语言不在
                列表里、整段用主语言重转过：这里留一笔被推翻的检测语言（如
                "ko"），供审计追溯；没发生过重转时是空字符串。打开语言屏蔽
                （MASK_LANGS_ENV）后，检测阶段就被屏蔽掉的语言也记在这里，含义
                不变；两者靠 signals 的 calls 区分：屏蔽是 1，重转是 2。
    """

    text: str = ""
    language: str = ""
    raw_text: str = ""
    rejected: List[dict] = field(default_factory=list)
    relabeled_from: str = ""
    # 识别器自己报的数，见 _decode_signals：lp（各子段里最小的 avg_logprob）、nsp / cr（最大的
    # no_speech_prob / compression_ratio）、tok、t、calls（解码了几遍，重转过是 2）、win（几个窗口）。
    # 只给识别风险分（app/asr_risk.py）和审计用，过滤（_fold）不看它，一条规则都不因此改。
    # 以前一个数都不往外给：线上 64,821 段没有一段记过 logprob，阈值只能在离线录音上猜
    signals: dict = field(default_factory=dict)
    # Whisper 吐出的各个子段（start / end / lp / nsp / cr / t / tok / text）。只进证据音频的索引
    # （logs/asr-evidence/），不进审计的 asr 子对象——审计那边只放数字
    windows: List[dict] = field(default_factory=list)

    def __iter__(self):
        """兼容旧的 `text, lang = transcribe(...)` 解包写法。"""
        return iter((self.text, self.language))

# Whisper 的温度回退：解码结果压缩比过高或置信度过低时，用更高的 temperature
# 把整段**重新解码**一遍，默认要试 6 档 (0, 0.2, 0.4, 0.6, 0.8, 1.0)。
# 音乐/噪声段几乎必然触发全部回退，实测（M 系列 mlx large-v3，真实直播音频）：
#     默认六档  最慢 25.1s | P95 21.7s | RTF 0.85
#     单档 0    最慢  5.8s | P95  5.6s | RTF 0.28
# 对「监听违禁词」这个用途，一次 25 秒的解码会让音频积压、检测落后，
# 代价远大于那点回退可能挽回的转写质量，所以默认只解码一次。
DEFAULT_TEMPERATURE = 0.0

# Whisper 在静音/纯音乐段上会吐出训练数据里的 YouTube 片尾套话。实测一场
# 西语带货直播里 21 段有文本的字幕中有 3 段是这种幻觉（14%）——而当时表里
# 只有英文和中文条目，西语的全部漏了过去。这类幻觉不只是脏字幕：它会污染
# 违禁词检测的上下文窗口，还白白占用翻译。
#
# 判定是**整段每一句都是**这些短语才丢弃（见 _is_all_hallucination），所以
# 不会误伤主播真的在句子里说「gracias」。
# 实测记录（bella.pcm 20 段 + s_purple23.0.pcm 23 段，2026-08）：
# 曾试过用 Silero VAD 的「人声占比」当闸门，先判定一段有没有人在说话，
# 没有就不送去识别——想法是幻觉都出在纯背景音乐段。测下来不成立：
# 四段确凿的幻觉（¡Suscríbete al canal!）人声占比是 100%/100%/53%/73.5%，
# VAD 把背景音乐当成了人声；43 段里没有一段低于 6%。
# 也就是说这道闸门一段都拦不住，还要每段多花 20–90ms。
# 真正挡住这些幻觉的是下面这张词表 + 逐句判定。
# 注意区分：废弃的是「我们自己在切段后再加一道 VAD 闸门」，不是 ct2 路径里
# 那个 vad_filter=True——后者是 faster-whisper 在解码内部按段裁剪，作用不同，
# 别顺手删掉。
#
# 表分两层。下面 _YOUTUBE_OUTROS 这一层只收四类：字幕组署名、整句片尾语里提到
# 「视频」的、「¡Suscríbete al canal!」这一句（及其别的语言版本）、YouTube 的小铃铛。
# 整段都是套话、且其中至少有一句属于这一层，就不看置信度直接丢。其余条目（thank you、
# nos vemos、suscríbete、dale like……）主播真会说——TikTok 有订阅功能，有主播把自己的
# 页面叫「mi canal」——仍然要置信度低于 -0.6 才丢；光一句「más videos」算不上片尾语，
# 也留在这边。
# 为什么要分层（2026-10-14 实测）：
#   - 本机 114 份会话日志里，按旧表有 276 段整段都是套话的字幕上了屏，前三名都属于
#     这一层（按去掉重音、不分大小写的整段文字计：「¡Gracias por ver el video!
#     ¡Suscríbete al canal!」67 段、「¡Gracias por ver este video!」58 段、「¡Gracias
#     por ver el video!」50 段）；
#   - 10-14 直播录音（13:20–14:40，608 段）离线重跑 large-v3，整段属于这一层的 9 段
#     置信度落在 -0.38 到 -0.70，大多贴着 -0.6，拦没拦住近乎抛硬币：4 段上了屏；
#   - 同样这 9 段音频，large-v3-turbo 有 7 段写出了一整段真话（「Otro producto
#     que yo recomiendo…」这类）——套话是顶替了真话，不是主播在说这句；
#   - 「¡Suscríbete al canal!」在日志里整段出现 283 次（上屏 33、被拒 250）。上屏的 33 次
#     逐条看过前后文：大多夹在讲价讲货中间，2 次落在道别处——那里就算主播在请大家
#     关注，屏幕上这句也多半不是原话。
# 丢的只是字幕：raw_text 原样保留，违禁词检测的输入一个字不少。
# 长尾变体是从同一批日志里捞出来补进来的，最多的是「¡Gracias por ver este video!
# ¡Suscríbete al canal para no perderte los próximos videos!」41 段，另有 8 段近似写法。
# 「suscríbete a mi canal / a nuestro canal」也是这次新补的，放在要看置信度的那层：
# 日志里整段出现 5 次，都夹在讲货中间。
_YOUTUBE_OUTROS = {
    # 字幕组署名（英 / 葡 / 法 / 西 / 中 / 俄 / 阿），及中文训练集里节目的订阅打赏套话
    "subtitles by the amara.org community", "subtitles by amara.org",
    "subtitled by the amara.org community",
    "legendas pela comunidade amara.org",
    "sous-titres realises par la communaute d'amara.org",
    "subtitulos realizados por la comunidad de amara.org",
    "字幕由amara.org社区提供", "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "субтитры сделал dimatorzok", "субтитры создавал dimatorzok",
    "المترجمات لكثير من الاشتراك في القناة",
    # 提到「视频」的片尾语
    "gracias por ver el video", "gracias por ver este video",
    "gracias por ver nuestro video", "muchas gracias por ver este video",
    "suscribete al canal para no perderte los proximos videos",
    "suscribete al canal para que no te pierdas los proximos videos",
    "suscribete para no perderte nuestros proximos videos",
    "suscribete al canal para mas videos",
    "si te gusto el video dale like suscribete y comparte",
    "si te ha gustado este video dale like suscribete y comparte",
    "si te ha gustado este video dale a like suscribete y comparte",
    "nos vemos en el proximo video", "다음 영상에서 만나요",
    # 「¡Suscríbete al canal!」及其别的语言版本、YouTube 的小铃铛
    "suscribete al canal", "suscribete y activa la campanita",
    "اشتركوا في القناة", "チャンネル登録お願いします",
}

_HALLUCINATIONS = _YOUTUBE_OUTROS | {
    # 英文
    "thank you", "thanks for watching", "thank you for watching", "you",
    "please subscribe", "subscribe", "bye", "so",
    "thanks for watching!", "see you next time",
    # 西语（带货直播的主力语种，之前完全没覆盖）
    "gracias por ver", "gracias por vernos",
    "suscribete", "suscribanse", "suscribete a mi canal", "suscribete a nuestro canal",
    "no olvides suscribirte", "no olviden suscribirse",
    "hasta la proxima", "nos vemos", "gracias por su atencion",
    "mas videos", "dale like y suscribete",
    "aplausos", "musica", "risas", "mas informacion", "obrigado", "obrigada",
    # 中文
    "谢谢观看", "请订阅",
    # 其它语言的同类套话（文字系统规则通常先扣下它们；这里是第二道）
    "감사합니다", "구독과 좋아요 부탁드립니다",
    "продолжение следует",
    "ご視聴ありがとうございました", "תודה רבה",
}


# 常用模型名 → MLX 社区仓库（Apple GPU 后端用）
_MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


def release_mlx_model():
    """放掉 mlx-whisper 缓存在类属性里的模型（ModelHolder 是类级缓存，新建一个
    MLXTranscriber 也还是同一份模型）。只看已经 import 过的模块：没加载过就没有
    可放的，不为此去 import mlx。返回是否真的找到了缓存。"""
    holder = getattr(sys.modules.get("mlx_whisper.transcribe"), "ModelHolder", None)
    if holder is None:
        return False
    holder.model = None
    holder.model_path = None
    # 模型对象里有引用环时，置 None 之后还要等 GC 才真正放掉；不先回收，clear_cache 时
    # 这些显存还被占着，清了等于没清
    gc.collect()
    core = sys.modules.get("mlx.core")
    for clear in (getattr(core, "clear_cache", None),
                  getattr(getattr(core, "metal", None), "clear_cache", None)):
        if callable(clear):
            try:
                clear()
                break
            except Exception:
                pass
    return True


# ---- MLX 缓冲缓存：可选上限 + 读数 ----
# 2026-09-17 实测（18 GB M3 Pro，mlx 0.32.1，large-v3 fp16，进程运行 28 分钟）：
# `footprint` 里监听进程 7.5 GB，其中 6948 MB 是 "IOAccelerator (graphics)"（Metal 缓冲），
# 其余不到 0.5 GB；权重约 3.1 GB。这些缓冲是 wired 统一内存，不能压缩也不能换出。
#
# 默认上限 256 MB，同一天停播后实测定的（tools/bench_mlx_cache.py：一个进程一个配置，同一段
# 130 秒西语话术逐段配对，识别耗时相对「不设上限」的变化与 95% 置信区间）：
#   等长段 23 段：256 MB −0.7% [−1.7, +0.2]；1024 MB +3.6% [+0.4, +5.2]；0 +5.7% [+4.2, +7.1]；
#                 再跑一次不设上限作对照 +0.6% [−0.9, +2.1]
#   变长段 2.5–9 秒 46 段：256 MB −0.5% [−2.2, +1.0]；512 MB +1.3% [−0.6, +5.5]；对照 −1.4% [−2.7, +0.4]
#   显卡缓冲：不设 4095–4305 MB，256 MB 时 3220 MB，0 时 2998 MB；模型常驻 2945 MB、峰值约 3760 MB 与上限无关。
# 不设上限时缓存随「段长种类」增长（等长 1142 MB，16 种长度 1353 MB）；真实直播段长连续变化，
# 线上 28 分钟攒到约 4 GB。256 MB 的差异落在对照组自身的波动里；完全不留缓存确实慢约 6%，所以不取 0。
# 局限：合成语音、单机、单次。上线后看审计里的 asr_memory 和各段 asr_ms 复核。
MLX_CACHE_ENV = "TLT_MLX_CACHE_MB"
MLX_CACHE_SETTING = "mlx_cache_limit_mb"
DEFAULT_MLX_CACHE_MB = 256
_NO_LIMIT_WORDS = ("off", "none", "unlimited")     # 明确写这几个词之一 = 不设上限（以前的行为）
_UNLIMITED = object()
_MB = 1024 * 1024
_mlx_said = set()         # 已经打过的提示：同一句话一个进程只说一次


def _say_once(text):
    if text not in _mlx_said:
        _mlx_said.add(text)
        print(text)


def _parse_cache_mb(raw, source):
    """非负整数（0 = 不留缓存），或 off / none / unlimited（不设上限，返回 _UNLIMITED）。
    没给返回 None；给了但两者都不是：提示一次，当作没给。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.lower() in _NO_LIMIT_WORDS:
            return _UNLIMITED
        if text.isascii() and text.isdigit():
            return int(text)
    elif isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    _say_once("[警告] {} 的值 {!r} 不是非负整数，也不是 off，已忽略".format(source, raw))
    return None


def mlx_cache_limit_mb():
    """要用的 MLX 缓冲缓存上限（MB）：环境变量 TLT_MLX_CACHE_MB，其次 settings.json 的
    mlx_cache_limit_mb，都没有就用 DEFAULT_MLX_CACHE_MB。写 off 的返回 None（不设上限）。不抛异常。"""
    value = None
    try:
        value = _parse_cache_mb(os.environ.get(MLX_CACHE_ENV), "环境变量 " + MLX_CACHE_ENV)
        if value is None:
            from .settings import load_settings
            value = _parse_cache_mb(load_settings().get(MLX_CACHE_SETTING),
                                    "settings.json 的 " + MLX_CACHE_SETTING)
    except Exception:
        value = None
    if value is _UNLIMITED:
        return None
    return DEFAULT_MLX_CACHE_MB if value is None else value


def _mlx_api(name):
    """mx.<name>，没有就找 mx.metal.<name>（旧版 mlx 放在那里）。只看已经 import 过的
    mlx.core，不为此去 import mlx。"""
    core = sys.modules.get("mlx.core")
    for owner in (core, getattr(core, "metal", None)):
        fn = getattr(owner, name, None)
        if callable(fn):
            return fn
    return None


def apply_mlx_cache_limit():
    """MLX 模型加载之后调：配置了上限就设上。返回实际设上的 MB 数，没设是 None。
    没配置时不碰 mlx 的任何接口。任何一步出错都只打一行，不影响识别。"""
    limit_mb = mlx_cache_limit_mb()
    if limit_mb is None:
        return None
    try:
        set_limit = _mlx_api("set_cache_limit")
        if set_limit is None:
            _say_once("[警告] 这个版本的 mlx 没有 set_cache_limit，MLX 缓冲缓存上限没有设")
            return None
        set_limit(limit_mb * _MB)
    except Exception as exc:
        _say_once("[警告] 设 MLX 缓冲缓存上限出错（{}），没有设".format(str(exc)[:120]))
        return None
    print("[信息] MLX 缓冲缓存上限：{} MB".format(limit_mb))
    return limit_mb


def mlx_memory_stats():
    """MLX 分配器的三个读数（MB）：在用、缓存、峰值。读的是计数器，不加锁、不触发计算。
    读不到的那一项是 None，不抛异常。"""
    out = {}
    for key, name in (("active_mb", "get_active_memory"),
                      ("cache_mb", "get_cache_memory"),
                      ("peak_mb", "get_peak_memory")):
        try:
            read = _mlx_api(name)
            out[key] = round(read() / float(_MB), 1) if read is not None else None
        except Exception:
            out[key] = None
    return out


def release_transcriber(transcriber):
    """放掉一个识别器占着的模型。换模型之前必须先调它：两个大模型同时驻留
    正是 2026-08-31 识别从 1.1 秒掉到 38-55 秒/段、丢 16 段音频的那类事故。"""
    release = getattr(transcriber, "release", None)
    if callable(release):
        try:
            release()
        except Exception:
            pass


def forget_exception_locals(exc):
    """清掉一个异常（连同它串着的 __cause__/__context__）回溯里各帧的局部变量。

    识别出错的异常要留着写审计、判断换不换模型，而它的回溯帧还连着模型：
    mlx_whisper/transcribe.py 里模型就是局部变量（model = ModelHolder.get_model(...)），
    faster-whisper 各帧的 self 也连着 WhisperModel。不清的话，清了 ModelHolder 模型也
    还在内存里，接着加载 CPU 模型就是两个模型同时驻留。还在执行的帧清不了，会跳过。"""
    stack, seen = [exc], set()
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        traceback.clear_frames(current.__traceback__)
        stack.extend((current.__cause__, current.__context__))


# 主播语言可以是一个 Whisper 语言码列表（逗号分隔，如 "es,en"）：意思不是强制
# 单一语种，而是「自动检测，但只在这些语言里选」——带货主播西语夹英语是常态，
# 不限制的逐段自动检测实测会把两成以上的段贴上无关语言标签（见 settings.py 的
# resolve_source 说明）。允许的语言码与界面下拉框一致（web/index.html #source-lang）。
SOURCE_LANG_CODES = {"es", "en", "ja", "ko", "pt", "fr", "de", "ru",
                     "ar", "th", "vi", "id", "zh"}
MAX_SOURCE_LANGS = 4     # 再多没有实际意义，还要控制「检测语言不对就整段重转一遍」的开销


def _parse_language_spec(language):
    """把 create_transcriber 收到的 language 拆成识别器要用的三样：

      forced  —— 直接传给 Whisper 的 language 参数；单一语种 / None / "auto" 时
                 原样返回，行为和改动前完全一致；
      allowed —— 允许的语言码（元组，保序）；不是逗号列表时是 None（不限制）；
      primary —— allowed 的第一个语言码：检测到的语言不在 allowed 里时，
                 重转这一段时强制用这个语言（见 Transcriber/MLXTranscriber.transcribe）。

    逗号列表的校验规则（没有一条会让识别报错——识别器不能因为一个写错的语言码
    就整个起不来）：
      * 只认 SOURCE_LANG_CODES 里的码，表外的码直接丢弃；
      * 去重，保留第一次出现的顺序；
      * 最多 MAX_SOURCE_LANGS 个，多出的从尾部截掉——首位是主语言，永远保留；
      * 校验完少于 2 个码：退化成「表里已经有的行为」而不是不限制列表——
        剩 1 个就当成单一语种（强制这个语言），一个都不剩就当成不限制的自动检测。
    """
    if not language or "," not in str(language):
        return language, None, None
    codes, seen = [], set()
    for raw in str(language).split(","):
        code = raw.strip().lower()
        if code and code in SOURCE_LANG_CODES and code not in seen:
            seen.add(code)
            codes.append(code)
    codes = codes[:MAX_SOURCE_LANGS]
    if len(codes) < 2:
        return (codes[0] if codes else None), None, None
    return None, tuple(codes), codes[0]


# ---- 识别长尾里两块可以省的解码（专家审查 M6）：两个开关，默认都关 ----
# 两项改的都是解码行为（项目规则四），所以各带一个开关、默认关：等离线配对评测做完、结果给用户看过、
# 用户点头才打开（D17）。评测要跑模型，归规则三管，另外排期；这里只有代码和离线单测。开关只认
# 环境变量（on / 1 / true / yes，不分大小写；没设或别的值都是关，写了 on、off 两组词以外的值
# 会在终端提示一次），识别器建好时读一次。
#
# 一、生成上限（TLT_ASR_TOKEN_CAP）。复读死循环会一直解码到上限才停：mlx-whisper 的上限叫 sample_len，
#   不传是 n_text_ctx // 2 = 224 个 token；faster-whisper（ct2）的叫 max_new_tokens，不传时一直到 448 减去
#   提示长度。两个参数名不一样，传错了 faster-whisper 直接报 TypeError（单测钉着）。
#   本机审计（132 份会话日志里记过 tok 的 4,800 段，只读日志、没跑模型，2026-10-23）：tok 到 112 及以上的
#   112 段全部因复读被丢，没有一段上屏、没有一段命中违禁词，其中 95 段停在 224；它们的识别 p50 是 6.7 秒，
#   全部段是 2.3 秒。上屏的段 tok 最多 94，命中违禁词的最多 66。
#   上限取 112：faster-whisper 最长的提示（热词最多 223 个 token，加 1 个 sot_prev 和 3 个起始 token）
#   再加 112 仍在 448 以内，不会撞上它「提示加上限超过 max_length」的报错（单测钉着）。
#   评测要看两件事：截短以后复读段的压缩比可能降到 2.4 以下，从「被丢」变成「上屏」；真话超过上限的段
#   会被截掉尾巴，违禁词检测用的 raw_text 也跟着少。
#
# 二、语言屏蔽（TLT_ASR_MASK_LANGS）。主播语言设了允许列表时，现在的做法是先自动检测，检测结果不在
#   列表里就用主语言整段重转一遍。这类段占全部段的 4%，却占识别超时的 47%（专家审查 M6）。打开后改成：
#   先单独做一次语言检测，在允许列表里挑概率最高的语言，只解码一遍。
#   - 代价（按 mlx-whisper 0.4.3、faster-whisper 1.2.1 的源码推的，没量过）：language=None 时两个后端
#     本来就先为检测单独编码一次、再为解码编码一次；把检测挪到外面、把选好的语言传进去，后端就跳过
#     自己那次检测，编码次数不变。多出来的是梅尔谱再算一遍，ct2 还要多跑一次 VAD（Silero，每段约
#     20–90 ms，见 _YOUTUBE_OUTROS 前的实测）。所以打开之前照样要量 p50 和超时率，mlx 和 ct2/CPU 分开过闸。
#   - 被屏蔽掉的语言照样填进 relabeled_from：asr_risk 的 relabel 标记、证据索引和审计 segment 的
#     relabeled_from 含义都不变，不新增字段。和重转靠 signals 的 calls 区分：屏蔽是 1，重转是 2。
#   - 和重转还有一处不同：重转一律用主语言，屏蔽用允许列表里概率最高的那个（并列时取列表里靠前的）。
#   - 检测这一步出任何错（比如 faster-whisper 1.1 之前没有公开的 detect_language）：同一原因提示
#     一次，这一段照旧走「先自动检测、不在列表里再重转」，识别不因此停，音频一段不少。
DECODE_TOKEN_CAP = 112
# faster-whisper 的 max_length 是 448；提示最长 1（sot_prev）+ 223（热词，max_length // 2 - 1）+ 3（起始）。
# 上限再大就可能撞它的报错；mlx 不传时是 224，比这还大也没有意义
WHISPER_MAX_LENGTH = 448
LONGEST_PROMPT_TOKENS = 1 + (WHISPER_MAX_LENGTH // 2 - 1) + 3
TOKEN_CAP_MAX = WHISPER_MAX_LENGTH - LONGEST_PROMPT_TOKENS
TOKEN_CAP_ENV = "TLT_ASR_TOKEN_CAP"
MASK_LANGS_ENV = "TLT_ASR_MASK_LANGS"
_ON_WORDS = ("on", "1", "true", "yes")
_SWITCH_OFF_WORDS = ("off", "0", "false", "no")
# 语言检测出错：不同原因各提示一次，最多提示这么多种（消息里每段不同的东西万一不止数字，也不至于每段一行）
MASK_ERROR_KINDS = 5
_MASK_ERROR_PREFIX = "mask_langs:"
_VARYING_NUMBER = re.compile(r"0x[0-9a-fA-F]+|\d+")


def _switch_on(name, environ=None):
    """开关开着吗。只认 on / 1 / true / yes（不分大小写）；没设或别的值都是关——默认关，写错也是关。
    写了 on/off 两组词以外的值（比如 TLT_ASR_TOKEN_CAP=112）：按关处理，终端提示一次，免得以为开了。"""
    value = (environ if environ is not None else os.environ).get(name, "")
    word = str(value).strip().lower()
    if word in _ON_WORDS:
        return True
    if word and word not in _SWITCH_OFF_WORDS:
        _say_once("[警告] 环境变量 {} 的值 {!r} 不是 on/1/true/yes，也不是 off/0/false/no，按关处理"
                  .format(name, value))
    return False


def decode_switches(token_cap=None, mask_langs=None, environ=None, allowed_langs=None):
    """识别器要用的 (生成上限, 是否屏蔽语言)。参数是 None 的看环境变量开关；评测脚本可以直接传值：
    token_cap 给 1..TOKEN_CAP_MAX 的整数就用这个数，给 0 是关；mask_langs 给 True / False。
    生成上限关着时是 None。直接传的值不对就报错（只有评测代码会传，界面和设置里没有这个口子）。
    allowed_langs 是识别器的主播语言允许列表（_parse_language_spec 的第二项）：只决定终端说哪一句——
    屏蔽只在设了列表时生效，没设列表时开关开着也照旧，终端就照实说这次不生效。"""
    if token_cap is None:
        token_cap = DECODE_TOKEN_CAP if _switch_on(TOKEN_CAP_ENV, environ) else None
    elif (isinstance(token_cap, bool) or not isinstance(token_cap, int)
          or not 0 <= token_cap <= TOKEN_CAP_MAX):
        raise ValueError("token_cap 要是 0 到 {} 的整数：{!r}".format(TOKEN_CAP_MAX, token_cap))  # i18n: terminal
    else:
        token_cap = token_cap or None
    mask_langs = _switch_on(MASK_LANGS_ENV, environ) if mask_langs is None else bool(mask_langs)
    if token_cap:
        _say_once("[信息] 识别生成上限：每段最多解码 {} 个 token".format(token_cap))
    if mask_langs and allowed_langs:
        _say_once("[信息] 识别语言屏蔽已打开：主播语言列表外的语言在检测阶段就屏蔽，不再整段重转")
    elif mask_langs:
        _say_once("[信息] 识别语言屏蔽开关开着，但这次识别没有主播语言列表（比如 es,en），不生效")
    return token_cap, mask_langs


def _mask_to_allowed(top, probs, allowed):
    """检测结果 top（各语言概率 probs）框进允许列表：返回 (解码用的语言, 被屏蔽掉的语言)。
    top 本来就在列表里时原样用它，第二项是空字符串——这时解码和让后端自己检测是同一个语言。
    不在列表里时取列表里概率最高的；并列（含都没有概率）时取列表里靠前的，主语言在第一位。"""
    if not top:
        raise ValueError("语言检测没有给出结果")  # i18n: terminal
    if top in allowed:
        return top, ""

    def score(code):
        value = probs.get(code) if isinstance(probs, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return float("-inf")
        return float(value)

    best = max(allowed, key=score)    # max 遇到并列返回最先的那个
    return best, top


def _warn_mask_error(exc):
    """语言屏蔽的检测出错时在终端说一声（这一段照旧走自动检测 + 重转）。出错的话多半每段都出，所以
    同一个原因一个进程只说一次：原因按「错误类型 + 消息」认，消息里的数字（形状、地址这类每段可能
    不同的）抹掉再比，换了原因的错照样能看到一次。最多说 MASK_ERROR_KINDS 种，再有新的只补一句。"""
    text = str(exc)
    key = "{}{}: {}".format(_MASK_ERROR_PREFIX, type(exc).__name__,
                            _VARYING_NUMBER.sub("#", text)[:120])
    if key in _mlx_said:
        return
    # 先拷一份再数：_say_once 可能正在别的线程里（run_in_executor 建识别器）往同一个集合里加句子
    kinds = sum(1 for said in tuple(_mlx_said) if said.startswith(_MASK_ERROR_PREFIX))
    if kinds >= MASK_ERROR_KINDS:
        _say_once("[警告] 识别前单独做的语言检测已经出过 {} 种错，再有别的错不逐条提示；出错的段照旧"
                  "先自动检测、不在主播语言里再整段重转".format(MASK_ERROR_KINDS))
        return
    _mlx_said.add(key)
    print("[警告] 识别前单独做的语言检测出错（{}: {}），改回先自动检测、不在主播语言里再"
          "整段重转".format(type(exc).__name__, text[:120]))


def create_transcriber(backend, model_size, device="auto", compute_type="auto",
                       language=None, beam_size=5, use_context=False,
                       temperature=DEFAULT_TEMPERATURE, hotwords=None,
                       token_cap=None, mask_langs=None):
    """backend: ct2（faster-whisper，CPU/CUDA）或 mlx（Apple GPU）。auto 优先 mlx。

    language 支持逗号列表形式（如 "es,en"，见 _parse_language_spec）；单一语种码
    和 "auto"/None 的行为不变。token_cap / mask_langs 见 decode_switches（默认看开关，开关默认关）。"""
    if backend == "auto":
        try:
            import mlx_whisper  # noqa: F401
            backend = "mlx"
        except ImportError:
            backend = "ct2"
    if backend == "mlx":
        return MLXTranscriber(model_size, language=language, use_context=use_context,
                              temperature=temperature, hotwords=hotwords,
                              token_cap=token_cap, mask_langs=mask_langs)
    return Transcriber(model_size, device=device, compute_type=compute_type,
                       language=language, beam_size=beam_size, use_context=use_context,
                       temperature=temperature, hotwords=hotwords,
                       token_cap=token_cap, mask_langs=mask_langs)


def _num(value):
    """识别器给的数转成 float；没有、转不了是 None。NaN / inf 原样留着：风险分要认出 NaN
    （10-14 录音 #355 只写出一个「!」，avg_logprob 就是 NaN），写审计时才换成 null。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _seg_field(seg, name):
    """mlx 的子段是 dict，faster-whisper 的是 Segment 对象：两种都按名字取。"""
    return seg.get(name) if isinstance(seg, dict) else getattr(seg, name, None)


def _odd_or(values, pick):
    """有非有限数（NaN / inf）就返回第一个，否则 pick(values)；空的是 None。
    min / max 遇到 NaN 的结果取决于顺序，不能直接用。"""
    if not values:
        return None
    odd = [v for v in values if not math.isfinite(v)]
    return odd[0] if odd else pick(values)


def _decode_signals(segments, calls):
    """从解码输出的子段里取风险分要的数，返回 (signals, windows)。

    同一个 30 秒窗口里的子段共用 avg_logprob / no_speech_prob / compression_ratio（mlx_whisper
    transcribe.py 的 new_segment），而段长不超过 9 秒，所以通常只有一个窗口；取最小 / 最大是为了
    万一跨窗口时取最可疑的那个。只读不改，和 _fold 看到的是同一份子段。"""
    windows, seeks = [], set()
    lps, nsps, crs, temps = [], [], [], []
    tok = 0
    for seg in segments:
        lp, nsp, cr, temp = (_num(_seg_field(seg, k)) for k in
                             ("avg_logprob", "no_speech_prob", "compression_ratio", "temperature"))
        tokens = _seg_field(seg, "tokens")
        try:
            n_tok = len(tokens) if tokens is not None else 0
        except TypeError:
            n_tok = 0
        seek = _seg_field(seg, "seek")
        if seek is not None:
            seeks.add(seek)
        text = _seg_field(seg, "text")
        windows.append({"start": _num(_seg_field(seg, "start")), "end": _num(_seg_field(seg, "end")),
                        "lp": lp, "nsp": nsp, "cr": cr, "t": temp, "tok": n_tok,
                        "text": text.strip() if isinstance(text, str) else ""})
        tok += n_tok
        for bucket, value in ((lps, lp), (nsps, nsp), (crs, cr), (temps, temp)):
            if value is not None:
                bucket.append(value)
    signals = {"lp": _odd_or(lps, min), "nsp": _odd_or(nsps, max), "cr": _odd_or(crs, max),
               "tok": tok, "t": _odd_or(temps, max), "calls": calls,
               # 没有 seek 字段（假识别器、老版本）时数不出窗口：有子段就当一个
               "win": len(seeks) if seeks else (1 if windows else 0)}
    return signals, windows


class _FilterMixin:
    """两个后端共用的质量过滤 + 滚动上下文。"""

    def set_hotwords(self, hotwords):
        """换成这一场的热词（app/pipeline.py _set_hotwords，专家审查 M7）。transcribe 每段现读 self.hotwords，
        赋值之后的下一段就用新的。

        热词变了（换了一位词表不同的主播，或者下一位没有词表）就把跟着上一位攒下的状态清掉：滚动上下文
        （--context 打开、这一场又没有热词时，它的最后 200 字会成为下一段的 initial_prompt）和文字系统历史
        （_script_outlier 拿它判离群）。以前热词进识别器的 key，热词一变就新建识别器，这两样自然是空的；
        换主播不再重建之后，不清的话上一位主播的话会带进下一位的头几段（b5-recog-input 第 1 轮审查）。
        热词没变（同一位主播停了再开、两位都没有词表）照旧留着，和以前复用识别器时一样。
        上一场在识别线程里还没跑完的那一段可能在清掉之后再往上下文里添一句，只影响下一段的提示。"""
        if hotwords != getattr(self, "hotwords", None):
            self._context = ""
            self._script_history = []
            self._script_pending = None
        self.hotwords = hotwords

    @staticmethod
    def _attach_signals(result, segments, calls, first_call=None):
        """把识别器报的数挂到结果上（见 ASRResult.signals）。出任何错都只是没有这些数：
        识别结果、过滤、违禁词检测的输入一个字不受影响。first_call 是重转之前那一遍的语言和
        文字，留给证据索引（被推翻的那一遍写了什么，事后要能看到）。"""
        try:
            result.signals, result.windows = _decode_signals(segments, calls)
            if first_call and result.windows:
                result.windows[0]["first_call"] = first_call
        except Exception:
            result.signals, result.windows = {}, []

    def _masked_language(self, audio):
        """语言屏蔽（见 DECODE_TOKEN_CAP 前的说明）：返回 (解码用的语言, 被屏蔽掉的语言)，被屏蔽的
        是空字符串表示检测结果本来就在允许列表里。没打开、没设允许列表时是 None，照旧让后端自己检测。
        检测出错也是 None：同一原因提示一次（_warn_mask_error），这一段走原来的「先自动检测、不在列表里
        再重转」，识别不停。"""
        if not (self.mask_langs and self.allowed_langs):
            return None
        try:
            top, probs = self._language_probs(audio)
            return _mask_to_allowed(top, probs, self.allowed_langs)
        except Exception as exc:
            _warn_mask_error(exc)
            return None

    def _script_outlier(self, text):
        """这段的文字系统在最近接受的字幕里一次都没出现过，且不是连着第二段同种文字。"""
        script = _script_of(text)
        if script is None:
            return False
        history = getattr(self, "_script_history", None)
        if history is None:
            history = self._script_history = []
        if len(history) < SCRIPT_MIN_HISTORY or script in history:
            self._script_pending = None
            return False
        if getattr(self, "_script_pending", None) == script:
            # 同一种文字连着第二段：当真换了语言，放行（并从此进入历史）
            self._script_pending = None
            return False
        self._script_pending = script
        return True

    def _remember_script(self, text):
        script = _script_of(text)
        if script is None:
            return
        history = getattr(self, "_script_history", None)
        if history is None:
            history = self._script_history = []
        history.append(script)
        del history[:-SCRIPT_HISTORY]

    def _fold(self, seg_iter, detected_lang):
        parts = []
        logprobs = []
        rejected = []
        all_parts = []          # 含被过滤掉的，供违禁词检测与审计使用
        for no_speech, comp_ratio, avg_lp, text in seg_iter:
            text = text.strip()
            if text:
                all_parts.append(text)
            reason = None
            if no_speech is not None and no_speech > 0.85:
                reason = "no_speech"
            elif comp_ratio is not None and comp_ratio > 2.4:   # 复读机式重复
                reason = "repetition"
            elif avg_lp is not None and avg_lp < -1.6:          # 置信度过低（背景音乐）
                reason = "low_confidence"
            elif text and _is_url_credit(text):                 # 带网址的片尾署名
                reason = "credit"
            elif text and self._script_outlier(text):           # 文字系统离群（见 _script_of）
                reason = "script_outlier"
            if reason:
                if text:
                    rejected.append({"text": text, "reason": reason})
                continue
            if text:
                parts.append(text)
                self._remember_script(text)
                if avg_lp is not None:
                    logprobs.append(avg_lp)
        text = " ".join(parts).strip()
        raw_text = " ".join(all_parts).strip()
        # 一个字母/数字都没有的段（裸 "!" 之类）不值得占一条字幕，更不该送去
        # 翻译。raw_text 原样保留——违禁词检测的输入不因此少一个字。
        if text and not _WORD_RE.search(text):
            rejected.append({"text": text, "reason": "punctuation"})
            return ASRResult(text="", language=detected_lang,
                             raw_text=raw_text, rejected=rejected)
        # 西语幻觉带重音和倒问叹号（¡Suscríbete!），必须先抹平才能比对
        normalized = _norm_for_hallucination(text)
        if _is_all_hallucination(normalized):
            mean_logprob = sum(logprobs) / len(logprobs) if logprobs else -10.0
            if (mean_logprob < -0.6 or _is_subtitle_credit(normalized)
                    or _has_youtube_outro(normalized)):
                if text:
                    rejected.append({"text": text, "reason": "hallucination"})
                return ASRResult(text="", language=detected_lang,
                                 raw_text=raw_text, rejected=rejected)
        if text:
            self._context = (self._context + " " + text).strip()[-400:]
        return ASRResult(text=text, language=detected_lang,
                         raw_text=raw_text, rejected=rejected)


class Transcriber(_FilterMixin):
    """faster-whisper（CTranslate2）后端：CPU / CUDA。"""

    def __init__(self, model_size="large-v3-turbo", device="auto", compute_type="auto",
                 language=None, beam_size=5, use_context=False,
                 temperature=DEFAULT_TEMPERATURE, hotwords=None,
                 token_cap=None, mask_langs=None):
        from faster_whisper import WhisperModel

        self.language, self.allowed_langs, self.primary_lang = _parse_language_spec(language)
        # M6 的两个开关（默认关），见 DECODE_TOKEN_CAP 前的说明
        self.token_cap, self.mask_langs = decode_switches(token_cap, mask_langs,
                                                          allowed_langs=self.allowed_langs)
        self.beam_size = beam_size
        self.use_context = use_context
        self.temperature = temperature
        # 静态热词（商品名等）。与滚动上下文不同：它不随时间漂移，
        # 不会把上一段的幻觉传染给下一段。transcribe 每段现读这个属性：管线每场开始时直接改它
        # （app/pipeline.py _set_hotwords，专家审查 M7），换主播不重建识别器，别在构造时把它烘焙进别处
        self.hotwords = hotwords
        self._context = ""
        # 实际生效的配置（CUDA 退回 CPU 后与请求的不同），审计记的是这一份
        self.backend = "ct2"
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        try:
            self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        except Exception as exc:
            if device != "cuda":
                raise
            # CUDA 组件缺失（cuBLAS/cuDNN 没装、驱动太旧）在这里才暴露。退回 CPU
            # 总比整机零识别强——这是合规监听器，识别不跑等于没人听
            print("[警告] CUDA 加载识别模型失败（{}），改用 CPU int8"
                  .format(str(exc)[:120]))
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
            self.device, self.compute_type = "cpu", "int8"

    def release(self):
        """放掉模型。之后再调 transcribe 会明确报错，而不是悄悄换一个模型。"""
        self.model = None
        gc.collect()      # 引用环里的模型对象要等 GC 才真正释放，换模型之前就要放干净

    def _language_probs(self, audio):
        """语言屏蔽用的检测：(检测结果, {语言: 概率})。用 faster-whisper 1.1 起公开的
        WhisperModel.detect_language，参数和 transcribe 在 language=None 时自己检测的那一步一致：
        同样先过默认参数的 VAD（transcribe 这里传了 vad_filter=True）、取前 30 秒、同一个阈值规则，
        所以检测结果和让 transcribe 自己检测时是同一个。纯英文模型不检测，和 transcribe 一样当英语。"""
        if getattr(getattr(self.model, "model", None), "is_multilingual", True) is False:
            return "en", {"en": 1.0}
        language, _probability, ranked = self.model.detect_language(audio=audio, vad_filter=True)
        return language, dict(ranked or ())

    def transcribe(self, pcm):
        """输入 16 kHz mono s16le PCM，返回 (文本, 识别到的语言代码)。"""
        if self.model is None:
            raise RuntimeError("识别模型已释放")
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        kwargs = {}
        if self.hotwords:
            kwargs["initial_prompt"] = self.hotwords
        elif self.use_context and self._context:
            kwargs["initial_prompt"] = self._context[-200:]
        if self.token_cap:
            # 生成上限（M6 开关，默认关）：ct2 的参数叫 max_new_tokens，不是 mlx 的 sample_len
            kwargs["max_new_tokens"] = self.token_cap
        language, relabeled_from = self.language, None
        masked = self._masked_language(audio)
        if masked is not None:
            # 语言屏蔽（M6 开关，默认关）：直接用框进允许列表的语言解码，后面的重转就不会触发
            language, relabeled_from = masked[0], masked[1] or None
        segments, info = self.model.transcribe(
            audio,
            language=language,
            vad_filter=True,
            beam_size=self.beam_size,
            temperature=self.temperature,
            condition_on_previous_text=False,
            **kwargs
        )
        segments = list(segments)
        detected_lang, calls = info.language, 1
        first_call = None
        # 主播语言设了允许列表（如 "es,en"）时：检测到的语言不在列表里，整段
        # 用主语言强制重转一遍——比如把误判成韩语的一段纠正回西语。这一步很少
        # 触发（只在检测跑偏时才会走到），比放一段错误语言标签的字幕流入
        # 后面的展示/翻译/脚本离群判定划算。
        if self.allowed_langs and detected_lang not in self.allowed_langs:
            relabeled_from, calls = detected_lang, 2
            first_call = {"language": detected_lang,
                          "text": " ".join(str(getattr(s, "text", "")).strip() for s in segments)}
            segments, info = self.model.transcribe(
                audio,
                language=self.primary_lang,
                vad_filter=True,
                beam_size=self.beam_size,
                temperature=self.temperature,
                condition_on_previous_text=False,
                **kwargs
            )
            segments = list(segments)
            detected_lang = info.language
        result = self._fold(
            ((s.no_speech_prob, s.compression_ratio, s.avg_logprob, s.text) for s in segments),
            detected_lang,
        )
        if relabeled_from:
            result.relabeled_from = relabeled_from
        self._attach_signals(result, segments, calls, first_call)
        return result


class MLXTranscriber(_FilterMixin):
    """mlx-whisper 后端：跑在 Apple Silicon GPU 上，large-v3 也能数倍实时。"""

    def __init__(self, model_size="large-v3", language=None, use_context=False,
                 temperature=DEFAULT_TEMPERATURE, hotwords=None,
                 token_cap=None, mask_langs=None):
        import mlx_whisper  # 提前失败好过跑到一半失败

        self._mlx = mlx_whisper
        self.repo = _MLX_REPOS.get(model_size, model_size)  # 允许直接给 HF 仓库名
        self.language, self.allowed_langs, self.primary_lang = _parse_language_spec(language)
        # M6 的两个开关（默认关），见 DECODE_TOKEN_CAP 前的说明
        self.token_cap, self.mask_langs = decode_switches(token_cap, mask_langs,
                                                          allowed_langs=self.allowed_langs)
        self.use_context = use_context
        self.temperature = temperature
        # 静态热词（商品名等）。与滚动上下文不同：它不随时间漂移，
        # 不会把上一段的幻觉传染给下一段。transcribe 每段现读这个属性：管线每场开始时直接改它
        # （app/pipeline.py _set_hotwords，专家审查 M7），换主播不重建识别器，别在构造时把它烘焙进别处
        self.hotwords = hotwords
        self._context = ""
        self.backend = "mlx"
        self.model_size = model_size
        self.device = "gpu"
        self.compute_type = "float16"
        # 预热一次：触发模型下载/编译，让第一段真实音频不用等。传解析后的
        # self.language（列表形式已拆成 None + allowed_langs），不能传原始
        # language——mlx-whisper 的 language 参数不认 "es,en" 这种逗号列表
        self._mlx.transcribe(np.zeros(16000, dtype=np.float32),
                             path_or_hf_repo=self.repo, language=self.language, fp16=True)
        # 模型在上面那一步加载。每个新的 MLX 模型都从这里过（按配置加载、换模型后重载），
        # 所以上限设在这里；实际设上的值记在对象上，asr_config 审计记的是这一份
        self.mlx_cache_limit_mb = apply_mlx_cache_limit()

    def release(self):
        release_mlx_model()

    def memory_stats(self):
        """{"active_mb", "cache_mb", "peak_mb"}，读不到的是 None。只读计数器，可以在识别
        线程之外随时调。"""
        return mlx_memory_stats()

    def _language_probs(self, audio):
        """语言屏蔽用的检测：(检测结果, {语言: 概率})。mlx-whisper 没有公开的检测入口，这里把
        transcribe 在 language=None 时自己做的那一步按 0.4.3 的源码原样搬出来：同一个缓存的模型
        （ModelHolder，fp16 和下面 transcribe 的调用一致）、同样的梅尔谱（补 30 秒静音、取前 30 秒）、
        同一个 model.detect_language、同样取概率最大的。所以检测结果和让 transcribe 自己检测时是同一个。
        用 import_module 取子模块：包里的 transcribe 这个名字是函数，不是子模块。"""
        mx = importlib.import_module("mlx.core")
        audio_mod = importlib.import_module("mlx_whisper.audio")
        holder = importlib.import_module("mlx_whisper.transcribe").ModelHolder
        model = holder.get_model(self.repo, mx.float16)
        if not model.is_multilingual:
            return "en", {"en": 1.0}       # 和 transcribe 一样：纯英文模型不检测
        mel = audio_mod.log_mel_spectrogram(audio, n_mels=model.dims.n_mels,
                                            padding=audio_mod.N_SAMPLES)
        segment = audio_mod.pad_or_trim(mel, audio_mod.N_FRAMES, axis=-2).astype(mx.float16)
        _, probs = model.detect_language(segment)
        return max(probs, key=probs.get), probs

    def transcribe(self, pcm):
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        kwargs = {}
        if self.hotwords:
            kwargs["initial_prompt"] = self.hotwords
        elif self.use_context and self._context:
            kwargs["initial_prompt"] = self._context[-200:]
        if self.token_cap:
            # 生成上限（M6 开关，默认关）：mlx 的参数叫 sample_len，经 decode_options 进 DecodingOptions
            kwargs["sample_len"] = self.token_cap
        language, relabeled_from = self.language, None
        masked = self._masked_language(audio)
        if masked is not None:
            # 语言屏蔽（M6 开关，默认关）：直接用框进允许列表的语言解码，后面的重转就不会触发
            language, relabeled_from = masked[0], masked[1] or None
        out = self._mlx.transcribe(
            audio,
            path_or_hf_repo=self.repo,
            language=language,
            temperature=self.temperature,
            condition_on_previous_text=False,
            fp16=True,
            **kwargs
        )
        detected_lang, calls = out.get("language"), 1
        first_call = None
        # 见 Transcriber.transcribe 里的同一段说明：允许列表框住了主播语言、
        # 检测结果又不在列表里时，整段用主语言强制重转一遍再用
        if self.allowed_langs and detected_lang not in self.allowed_langs:
            relabeled_from, calls = detected_lang, 2
            first_call = {"language": detected_lang, "text": str(out.get("text") or "").strip()}
            out = self._mlx.transcribe(
                audio,
                path_or_hf_repo=self.repo,
                language=self.primary_lang,
                temperature=self.temperature,
                condition_on_previous_text=False,
                fp16=True,
                **kwargs
            )
            detected_lang = out.get("language")
        result = self._fold(
            ((s.get("no_speech_prob"), s.get("compression_ratio"),
              s.get("avg_logprob"), s.get("text", "")) for s in out.get("segments", [])),
            detected_lang,
        )
        if relabeled_from:
            result.relabeled_from = relabeled_from
        self._attach_signals(result, out.get("segments", []), calls, first_call)
        return result
