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
    误识别）的段直接丢弃，宁缺毋滥。
"""
import gc
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
        _HALLUCINATION_KEYS = {_hallucination_key(_norm_for_hallucination(h))
                               for h in _HALLUCINATIONS}
    if not normalized:
        return False
    parts = [p for p in re.split(r"[.!?！？。]+", normalized) if p.strip()]
    if not parts:
        return False
    return all(_hallucination_key(p) in _HALLUCINATION_KEYS for p in parts)


@dataclass
class ASRResult:
    """一段音频的识别结果。

    text     —— 通过质量过滤的文本，用于字幕展示；
    raw_text —— 含被过滤掉的部分，**违禁词检测用这个**：漏报的代价远高于误报，
                宁可扫到一句置信度低的疑似违禁词，也不要因为过滤而漏掉；
    rejected —— 被丢弃的候选及原因，用于审计（事后能区分「没听出来」和
                「听出来了但被过滤」——这是两个完全不同的问题）。
    """

    text: str = ""
    language: str = ""
    raw_text: str = ""
    rejected: List[dict] = field(default_factory=list)

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
# 判定是**整段完全等于**这些短语才丢弃（见 _fold），所以不会误伤主播真的
# 在句子里说「gracias」。
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
_HALLUCINATIONS = {
    # 英文
    "thank you", "thanks for watching", "thank you for watching", "you",
    "please subscribe", "subscribe", "bye", "so",
    "thanks for watching!", "see you next time",
    "subtitles by the amara.org community", "subtitles by amara.org",
    "subtitled by the amara.org community",
    # 葡语 / 法语：同一个训练集署名，Whisper 在西语直播里也会吐
    "legendas pela comunidade amara.org",
    "sous-titres realises par la communaute d'amara.org",
    # 西语（带货直播的主力语种，之前完全没覆盖）
    "gracias por ver", "gracias por ver el video",
    "gracias por ver este video", "gracias por vernos",
    "suscribete", "suscribete al canal", "suscribanse",
    "no olvides suscribirte", "no olviden suscribirse",
    "hasta la proxima", "nos vemos", "gracias por su atencion",
    "subtitulos realizados por la comunidad de amara.org",
    "mas videos", "dale like y suscribete",
    # 中文
    "字幕由amara.org社区提供", "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "谢谢观看", "请订阅",
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


def create_transcriber(backend, model_size, device="auto", compute_type="auto",
                       language=None, beam_size=5, use_context=False,
                       temperature=DEFAULT_TEMPERATURE, hotwords=None):
    """backend: ct2（faster-whisper，CPU/CUDA）或 mlx（Apple GPU）。auto 优先 mlx。"""
    if backend == "auto":
        try:
            import mlx_whisper  # noqa: F401
            backend = "mlx"
        except ImportError:
            backend = "ct2"
    if backend == "mlx":
        return MLXTranscriber(model_size, language=language, use_context=use_context,
                              temperature=temperature, hotwords=hotwords)
    return Transcriber(model_size, device=device, compute_type=compute_type,
                       language=language, beam_size=beam_size, use_context=use_context,
                       temperature=temperature, hotwords=hotwords)


class _FilterMixin:
    """两个后端共用的质量过滤 + 滚动上下文。"""

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
            if reason:
                if text:
                    rejected.append({"text": text, "reason": reason})
                continue
            if text:
                parts.append(text)
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
            if mean_logprob < -0.6 or _is_subtitle_credit(normalized):
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
                 temperature=DEFAULT_TEMPERATURE, hotwords=None):
        from faster_whisper import WhisperModel

        self.language = language
        self.beam_size = beam_size
        self.use_context = use_context
        self.temperature = temperature
        # 静态热词（商品名等）。与滚动上下文不同：它不随时间漂移，
        # 不会把上一段的幻觉传染给下一段
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
        segments, info = self.model.transcribe(
            audio,
            language=self.language,
            vad_filter=True,
            beam_size=self.beam_size,
            temperature=self.temperature,
            condition_on_previous_text=False,
            **kwargs
        )
        return self._fold(
            ((s.no_speech_prob, s.compression_ratio, s.avg_logprob, s.text) for s in segments),
            info.language,
        )


class MLXTranscriber(_FilterMixin):
    """mlx-whisper 后端：跑在 Apple Silicon GPU 上，large-v3 也能数倍实时。"""

    def __init__(self, model_size="large-v3", language=None, use_context=False,
                 temperature=DEFAULT_TEMPERATURE, hotwords=None):
        import mlx_whisper  # 提前失败好过跑到一半失败

        self._mlx = mlx_whisper
        self.repo = _MLX_REPOS.get(model_size, model_size)  # 允许直接给 HF 仓库名
        self.language = language
        self.use_context = use_context
        self.temperature = temperature
        # 静态热词（商品名等）。与滚动上下文不同：它不随时间漂移，
        # 不会把上一段的幻觉传染给下一段
        self.hotwords = hotwords
        self._context = ""
        self.backend = "mlx"
        self.model_size = model_size
        self.device = "gpu"
        self.compute_type = "float16"
        # 预热一次：触发模型下载/编译，让第一段真实音频不用等
        self._mlx.transcribe(np.zeros(16000, dtype=np.float32),
                             path_or_hf_repo=self.repo, language=language, fp16=True)
        # 模型在上面那一步加载。每个新的 MLX 模型都从这里过（按配置加载、换模型后重载），
        # 所以上限设在这里；实际设上的值记在对象上，asr_config 审计记的是这一份
        self.mlx_cache_limit_mb = apply_mlx_cache_limit()

    def release(self):
        release_mlx_model()

    def memory_stats(self):
        """{"active_mb", "cache_mb", "peak_mb"}，读不到的是 None。只读计数器，可以在识别
        线程之外随时调。"""
        return mlx_memory_stats()

    def transcribe(self, pcm):
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        kwargs = {}
        if self.hotwords:
            kwargs["initial_prompt"] = self.hotwords
        elif self.use_context and self._context:
            kwargs["initial_prompt"] = self._context[-200:]
        out = self._mlx.transcribe(
            audio,
            path_or_hf_repo=self.repo,
            language=self.language,
            temperature=self.temperature,
            condition_on_previous_text=False,
            fp16=True,
            **kwargs
        )
        return self._fold(
            ((s.get("no_speech_prob"), s.get("compression_ratio"),
              s.get("avg_logprob"), s.get("text", "")) for s in out.get("segments", [])),
            out.get("language"),
        )
