"""翻译引擎。auto：本地 Ollama 里有 TranslateGemma 就用它（完全本地），否则退回
Google 网页免费接口；也可显式指定 gemma / google / claude / openai / none。"""
import json
import os

TRANSLATOR_CHOICES = ["auto", "gemma", "google", "claude", "openai", "none"]

LANG_NAMES = {
    "zh-CN": "Simplified Chinese", "zh-TW": "Traditional Chinese", "zh": "Chinese",
    "en": "English", "ja": "Japanese", "ko": "Korean", "es": "Spanish",
    "fr": "French", "de": "German", "ru": "Russian", "pt": "Portuguese",
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "ar": "Arabic",
}


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

    async def translate(self, text, target, source="auto"):
        params = {"client": "gtx", "sl": source or "auto", "tl": target, "dt": "t", "q": text}
        try:
            session = await self.session()
            async with session.get(self.URL, params=params) as resp:
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
        tgt_code = _GEMMA_CODES.get(target, target)
        tgt_name = LANG_NAMES.get(target, target)
        if source and source != "auto":
            src_code = _GEMMA_CODES.get(source, source)
            src_name = LANG_NAMES.get(source, source)
            head = (
                "You are a professional {sn} ({sc}) to {tn} ({tc}) translator. "
                "Your goal is to accurately convey the meaning and nuances of the original {sn} text "
                "while adhering to {tn} grammar, vocabulary, and cultural sensitivities. "
                "Produce only the {tn} translation, without any additional explanations or commentary. "
                "Please translate the following {sn} text into {tn}:"
            ).format(sn=src_name, sc=src_code, tn=tgt_name, tc=tgt_code)
        else:
            head = (
                "You are a professional translator into {tn} ({tc}). "
                "Produce only the {tn} translation, without any additional explanations or commentary. "
                "Please translate the following text into {tn}:"
            ).format(tn=tgt_name, tc=tgt_code)
        return head + "\n\n\n" + text

    async def translate(self, text, target, source="auto"):
        body = {
            "model": self.model,
            "prompt": self._prompt(text, target, source),
            "stream": False,
            "options": {"temperature": 0},
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
    if name == "auto":
        name = "gemma" if _ollama_has_gemma() else "google"
        print("[信息] 翻译引擎 auto → {}".format(name))
    if name == "gemma":
        return OllamaGemmaTranslator()
    if name == "google":
        return GoogleWebTranslator()
    if name == "claude":
        return ClaudeTranslator()
    if name == "openai":
        return OpenAITranslator()
    if name == "none":
        return None
    raise ValueError("未知的翻译引擎: " + str(name))
