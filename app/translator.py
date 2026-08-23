"""翻译引擎。auto：本地 Ollama 里有 TranslateGemma 就用它（完全本地），否则退回
Google 网页免费接口；也可显式指定 gemma / google / claude / openai / none。"""
import json
import os
from collections import OrderedDict

TRANSLATOR_CHOICES = ["auto", "gemma", "google", "claude", "openai", "none"]

LANG_NAMES = {
    "zh-CN": "Simplified Chinese", "zh-TW": "Traditional Chinese", "zh": "Chinese",
    "en": "English", "ja": "Japanese", "ko": "Korean", "es": "Spanish",
    "fr": "French", "de": "German", "ru": "Russian", "pt": "Portuguese",
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "ar": "Arabic",
}


class CachedTranslator:
    """给任意翻译引擎套一层重复句缓存。

    带货直播的话术重复率极高（"envío gratis"、"solo por hoy"、口头禅、
    以及主播念同一段广告词），命中即 0ms 返回。缓存只存成功结果——失败
    不缓存，否则一次网络抖动会把这句话永久钉死成「翻译失败」。
    """

    def __init__(self, inner, capacity=512):
        self.inner = inner
        self.capacity = capacity
        self._cache = OrderedDict()
        self.hits = 0
        self.misses = 0

    @property
    def name(self):
        return self.inner.name

    async def translate(self, text, target, source="auto"):
        key = (text.strip(), target, source)
        if key in self._cache:
            self._cache.move_to_end(key)
            self.hits += 1
            return self._cache[key]
        self.misses += 1
        out = await self.inner.translate(text, target, source=source)
        if out:
            self._cache[key] = out
            if len(self._cache) > self.capacity:
                self._cache.popitem(last=False)
        return out

    async def close(self):
        await self.inner.close()


class BaseTranslator:
    name = "base"
    TIMEOUT = 10  # 秒

    def __init__(self):
        self._session = None

    async def session(self):
        import aiohttp  # 延迟导入：让 --doctor 在依赖未装时也能运行

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.TIMEOUT)
            )
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def translate(self, text, target, source="auto"):
        raise NotImplementedError


class GoogleWebTranslator(BaseTranslator):
    """Google 翻译网页版免费接口（无需密钥）。注意：会把字幕文本发给 Google。"""

    name = "google"
    URL = "https://translate.googleapis.com/translate_a/single"
    COOLDOWN_SEC = 120

    def __init__(self):
        super().__init__()
        self._cooldown_until = 0.0

    async def translate(self, text, target, source="auto"):
        import time

        # 免费接口会按 IP 限流（429）：冷却期间直接跳过，别在每条字幕上
        # 继续撞——既是无谓请求，也会加重限流
        if time.time() < self._cooldown_until:
            return None
        params = {"client": "gtx", "sl": source or "auto", "tl": target, "dt": "t", "q": text}
        try:
            session = await self.session()
            async with session.get(self.URL, params=params) as resp:
                if resp.status == 429:
                    self._cooldown_until = time.time() + self.COOLDOWN_SEC
                    print("[警告] Google 翻译接口被限流（429），"
                          "暂停请求 {} 秒后自动恢复".format(self.COOLDOWN_SEC))
                    return None
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
            if not data or not data[0]:
                return None
            return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip() or None
        except Exception:
            return None


class ClaudeTranslator(BaseTranslator):
    """通过 Anthropic API（Claude Haiku）翻译，需要环境变量 ANTHROPIC_API_KEY。"""

    name = "claude"
    URL = "https://api.anthropic.com/v1/messages"
    MODEL = os.environ.get("CLAUDE_TRANSLATE_MODEL", "claude-haiku-4-5-20251001")

    def __init__(self):
        super().__init__()
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("使用 --translator claude 需要设置环境变量 ANTHROPIC_API_KEY")

    async def translate(self, text, target, source="auto"):
        lang = LANG_NAMES.get(target, target)
        body = {
            "model": self.MODEL,
            "max_tokens": 512,
            "system": ("You are a translation engine for live-stream subtitles. "
                       "Translate the user's text into {lang}. "
                       "Output ONLY the translation, nothing else.").format(lang=lang),
            "messages": [{"role": "user", "content": text}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            session = await self.session()
            async with session.post(self.URL, data=json.dumps(body), headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            return (data.get("content") or [{}])[0].get("text", "").strip() or None
        except Exception:
            return None


class OpenAITranslator(BaseTranslator):
    """通过 OpenAI 兼容接口翻译，需要环境变量 OPENAI_API_KEY（可选 OPENAI_BASE_URL / OPENAI_MODEL）。"""

    name = "openai"

    def __init__(self):
        super().__init__()
        self.api_key = os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("使用 --translator openai 需要设置环境变量 OPENAI_API_KEY")
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.url = base + "/chat/completions"
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    async def translate(self, text, target, source="auto"):
        lang = LANG_NAMES.get(target, target)
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system",
                 "content": ("You are a translation engine for live-stream subtitles. "
                             "Translate the user's text into {lang}. "
                             "Output ONLY the translation.").format(lang=lang)},
                {"role": "user", "content": text},
            ],
        }
        headers = {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}
        try:
            session = await self.session()
            async with session.post(self.url, data=json.dumps(body), headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            return data["choices"][0]["message"]["content"].strip() or None
        except Exception:
            return None


# TranslateGemma 用的语言代码与我们 UI 代码的差异映射
_GEMMA_CODES = {"zh-CN": "zh-Hans", "zh-TW": "zh-Hant"}


class OllamaGemmaTranslator(BaseTranslator):
    """本地 Ollama + TranslateGemma（Google 开源翻译专用模型）：完全离线、免费。"""

    name = "gemma"
    TIMEOUT = 60  # 首次调用要把模型载入 GPU，会慢几秒

    def __init__(self):
        super().__init__()
        base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        self.url = base + "/api/generate"
        self.model = os.environ.get("OLLAMA_TRANSLATE_MODEL", "translategemma:4b")

    def _prompt(self, text, target, source):
        """短指令。实测（M 系列 + translategemma:4b，6 句带货话术中位数）：
        原来 92 token 的长指令总耗时 494ms（预填充 244ms），换成这版 35 token
        的短指令后 344ms（预填充 84ms），译文质量无差别——指令每句都要重新
        预填充一遍，是纯粹的固定成本。再往下砍收益趋零（预填充有 ~80ms 地板），
        砍成 "es→zh:" 这种标签式反而会让模型把提示词回显出来。"""
        tgt_name = LANG_NAMES.get(target, target)
        if source and source != "auto":
            src_name = LANG_NAMES.get(source, source)
            head = ("Translate this {sn} text into {tn}. "
                    "Output only the translation.").format(sn=src_name, tn=tgt_name)
        else:
            head = ("Translate this text into {tn}. "
                    "Output only the translation.").format(tn=tgt_name)
        return head + "\n\n" + text

    # 选项必须逐次完全一致：Ollama 一旦发现 options 变了就会重新加载模型，
    # 实测会带来 ~4 秒停顿。所以这里定成常量，不做动态调整。
    _OPTIONS = {"temperature": 0, "num_predict": 200}

    async def translate(self, text, target, source="auto"):
        body = {
            "model": self.model,
            "prompt": self._prompt(text, target, source),
            "stream": False,
            "options": self._OPTIONS,
            # 常驻显存：默认空闲 5 分钟就卸载，主播放一段音乐回来后
            # 第一句要多等约 5 秒重新载入
            "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "30m"),
        }
        try:
            session = await self.session()
            async with session.post(self.url, data=json.dumps(body)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            return (data.get("response") or "").strip() or None
        except Exception:
            return None


def _ollama_has_gemma():
    """启动时的一次性同步探测：Ollama 是否在跑且已拉取 translategemma。"""
    import urllib.request

    base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as resp:
            data = json.load(resp)
        return any(m.get("name", "").startswith("translategemma")
                   for m in data.get("models", []))
    except Exception:
        return False


def create_translator(name):
    """返回带重复句缓存的翻译引擎（none 除外）。"""
    if name == "auto":
        name = "gemma" if _ollama_has_gemma() else "google"
        print("[信息] 翻译引擎 auto → {}".format(name))
    if name == "none":
        return None
    if name == "gemma":
        inner = OllamaGemmaTranslator()
    elif name == "google":
        inner = GoogleWebTranslator()
    elif name == "claude":
        inner = ClaudeTranslator()
    elif name == "openai":
        inner = OpenAITranslator()
    else:
        raise ValueError("未知的翻译引擎: " + str(name))
    return CachedTranslator(inner)
