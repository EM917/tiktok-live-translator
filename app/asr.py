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
import string
from dataclasses import dataclass, field
from typing import List

import numpy as np


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

# whisper 在静音/纯音乐段上常见的幻觉输出，直接丢弃
_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching", "you",
    "please subscribe", "subscribe", "bye", "so",
    "字幕由amara.org社区提供", "请不吝点赞订阅转发打赏支持明镜与点点栏目",
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
        normalized = text.lower().strip(string.punctuation + string.whitespace + "！。，、？")
        if normalized.replace(" ", "") in {h.replace(" ", "") for h in _HALLUCINATIONS}:
            mean_logprob = sum(logprobs) / len(logprobs) if logprobs else -10.0
            if mean_logprob < -0.6:
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
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, pcm):
        """输入 16 kHz mono s16le PCM，返回 (文本, 识别到的语言代码)。"""
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
        # 预热一次：触发模型下载/编译，让第一段真实音频不用等
        self._mlx.transcribe(np.zeros(16000, dtype=np.float32),
                             path_or_hf_repo=self.repo, language=language, fp16=True)

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
