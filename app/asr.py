"""faster-whisper 语音识别封装（同步接口，由 Pipeline 放进线程池执行）。

精度策略：
  * beam search（默认 beam_size=5）代替贪心解码；
  * 滚动上下文：把最近识别出的文本作为 initial_prompt 传给下一段，
    帮助模型接住被切断的句子和领域词汇（可用 --no-context 关闭）;
  * 质量过滤：压缩比过高（复读机式垃圾）或平均置信度过低（多为背景音乐
    误识别）的段直接丢弃，宁缺毋滥。
"""
import string

import numpy as np

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
                       language=None, beam_size=5, use_context=True):
    """backend: ct2（faster-whisper，CPU/CUDA）或 mlx（Apple GPU）。auto 优先 mlx。"""
    if backend == "auto":
        try:
            import mlx_whisper  # noqa: F401
            backend = "mlx"
        except ImportError:
            backend = "ct2"
    if backend == "mlx":
        return MLXTranscriber(model_size, language=language, use_context=use_context)
    return Transcriber(model_size, device=device, compute_type=compute_type,
                       language=language, beam_size=beam_size, use_context=use_context)


class _FilterMixin:
    """两个后端共用的质量过滤 + 滚动上下文。"""

    def _fold(self, seg_iter, detected_lang):
        parts = []
        logprobs = []
        for no_speech, comp_ratio, avg_lp, text in seg_iter:
            if no_speech is not None and no_speech > 0.85:
                continue
            if comp_ratio is not None and comp_ratio > 2.4:   # 复读机式重复
                continue
            if avg_lp is not None and avg_lp < -1.6:          # 置信度过低（背景音乐）
                continue
            text = text.strip()
            if text:
                parts.append(text)
                if avg_lp is not None:
                    logprobs.append(avg_lp)
        text = " ".join(parts).strip()
        normalized = text.lower().strip(string.punctuation + string.whitespace + "！。，、？")
        if normalized.replace(" ", "") in {h.replace(" ", "") for h in _HALLUCINATIONS}:
            mean_logprob = sum(logprobs) / len(logprobs) if logprobs else -10.0
            if mean_logprob < -0.6:
                return "", detected_lang
        if text:
            self._context = (self._context + " " + text).strip()[-400:]
        return text, detected_lang


class Transcriber(_FilterMixin):
    """faster-whisper（CTranslate2）后端：CPU / CUDA。"""

    def __init__(self, model_size="large-v3-turbo", device="auto", compute_type="auto",
                 language=None, beam_size=5, use_context=True):
        from faster_whisper import WhisperModel

        self.language = language
        self.beam_size = beam_size
        self.use_context = use_context
        self._context = ""
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, pcm):
        """输入 16 kHz mono s16le PCM，返回 (文本, 识别到的语言代码)。"""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        kwargs = {}
        if self.use_context and self._context:
            kwargs["initial_prompt"] = self._context[-200:]
        segments, info = self.model.transcribe(
            audio,
            language=self.language,
            vad_filter=True,
            beam_size=self.beam_size,
            condition_on_previous_text=False,
            **kwargs
        )
        return self._fold(
            ((s.no_speech_prob, s.compression_ratio, s.avg_logprob, s.text) for s in segments),
            info.language,
        )


class MLXTranscriber(_FilterMixin):
    """mlx-whisper 后端：跑在 Apple Silicon GPU 上，large-v3 也能数倍实时。"""

    def __init__(self, model_size="large-v3", language=None, use_context=True):
        import mlx_whisper  # 提前失败好过跑到一半失败

        self._mlx = mlx_whisper
        self.repo = _MLX_REPOS.get(model_size, model_size)  # 允许直接给 HF 仓库名
        self.language = language
        self.use_context = use_context
        self._context = ""
        # 预热一次：触发模型下载/编译，让第一段真实音频不用等
        self._mlx.transcribe(np.zeros(16000, dtype=np.float32),
                             path_or_hf_repo=self.repo, language=language, fp16=True)

    def transcribe(self, pcm):
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        kwargs = {}
        if self.use_context and self._context:
            kwargs["initial_prompt"] = self._context[-200:]
        out = self._mlx.transcribe(
            audio,
            path_or_hf_repo=self.repo,
            language=self.language,
            condition_on_previous_text=False,
            fp16=True,
            **kwargs
        )
        return self._fold(
            ((s.get("no_speech_prob"), s.get("compression_ratio"),
              s.get("avg_logprob"), s.get("text", "")) for s in out.get("segments", [])),
            out.get("language"),
        )
