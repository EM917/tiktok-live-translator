"""翻译引擎。auto：本地 Ollama 里有 TranslateGemma 就用它（完全本地），否则退回
Google 网页免费接口；也可显式指定 gemma / google / claude / openai / none。"""
import json
import os
import re
from collections import OrderedDict

TRANSLATOR_CHOICES = ["auto", "hymt2", "hymt2-7b", "gemma",
                      "google", "claude", "openai", "none"]

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

    async def translate(self, text, target, source="auto", glossary=None):
        key = (text.strip(), target, source, glossary or "")
        if key in self._cache:
            self._cache.move_to_end(key)
            self.hits += 1
            return self._cache[key]
        self.misses += 1
        out = await self.inner.translate(text, target, source=source,
                                         glossary=glossary)
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

    async def translate(self, text, target, source="auto", glossary=None):
        raise NotImplementedError


class GoogleWebTranslator(BaseTranslator):
    """Google 翻译网页版免费接口（无需密钥）。注意：会把字幕文本发给 Google。"""

    name = "google"
    URL = "https://translate.googleapis.com/translate_a/single"
    COOLDOWN_SEC = 120

    def __init__(self):
        super().__init__()
        self._cooldown_until = 0.0

    async def translate(self, text, target, source="auto", glossary=None):
        import time

        # 免费接口无法接受术语指令，忽略 glossary（靠译文后的兜底替换保证一致）
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

    async def translate(self, text, target, source="auto", glossary=None):
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

    async def translate(self, text, target, source="auto", glossary=None):
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

    def _prompt(self, text, target, source, glossary=None):
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
        # 词表必须留在**指令区**。实测教训：把 "Use these exact translations: …"
        # 拼在正文前面，TranslateGemma 会把这行指令当成源文一起翻译，
        # 译文里就冒出「使用这些精确的翻译： 滴剂 = D3 K2 维生素滴剂」这种东西。
        # 它是纯翻译模型——给它什么就翻什么，指令只认 head 这一段。
        # 实测（2026-08-24，日志里 175 句真实西语、269 个待检术语，判据是
        # 词表要求的中文有没有出现在译文里）：
        #   Keep these terms exactly as given  46.1%   ← 现用
        #   You MUST use these exact translations  48.7%（n=269 时标准误约 3pp，是噪声）
        #   Glossary (mandatory)               38.3%   ← 明显更差，别用
        # 也就是说**换措辞救不了**：这类纯翻译模型对指令区术语表的遵从率就在
        # 五成上下，短句上尤其容易整条无视（"la limpieza" 在长句里认、
        # 单独一句就翻回「清洁」）。glossary.apply() 只能兜住译文里还残留西语
        # 的情况，模型「翻错但翻得像模像样」时救不了。
        # 想再往上走要换有原生术语干预的模型，不是继续调这句话。
        pairs = _as_pairs(glossary)
        if pairs:
            head += (" Keep these terms exactly as given: "
                     + "; ".join("{} = {}".format(es, zh) for es, zh in pairs) + ".")
        return head + "\n\n" + text

    # 选项必须逐次完全一致：Ollama 一旦发现 options 变了就会重新加载模型，
    # 实测会带来 ~4 秒停顿。所以这里定成常量，不做动态调整。
    _OPTIONS = {"temperature": 0, "num_predict": 200}

    async def translate(self, text, target, source="auto", glossary=None):
        body = {
            "model": self.model,
            "prompt": self._prompt(text, target, source, glossary),
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


class OllamaHyMT2Translator(BaseTranslator):
    """本地 Ollama + 腾讯 Hy-MT2-1.8B（Apache 2.0）。

    引进它的理由只有一个，而且是实测出来的：TranslateGemma 对指令区术语表的
    遵从率只有 46.1%（tools/bench_glossary.py，175 句真实西语 / 269 个术语），
    而换措辞救不了——试过的三种写法落在 38.3%~48.7%，彼此在噪声范围内。
    我们最在乎的恰恰是术语（商品名、价格、促销条件），所以要找的是**原生支持
    术语干预**的模型，而不是继续调那句提示词。

    Hy-MT2 官方给了术语格式，这里照搬：
        Reference the following translations:
        {src} translates to {tgt}

        Translate the following text into {lang}. Note that you must ONLY
        output the translated result without any additional explanation:
        {text}

    官方还提供了 [Background Information] 上下文格式，这里**故意不用**：
    背景信息要进 prefill，而 prefill 是本项目翻译耗时的主要固定成本
    （92 token → 35 token 指令实测 494ms → 344ms）；而且滚动上下文在识别侧
    实测过是净亏。要用也该单独立项测，不能和术语干预打包。
    """

    name = "hymt2"
    TIMEOUT = 60

    # 官方 GGUF 里注册给 Ollama 的 chat template 是坏的——`{{ if .Prompt }}` 块里
    # 根本没有 `{{ .Prompt }}`，末尾还留着 `onse }}` 这种被截断的残片。结果是
    # 用户的文本压根没进模型，输出是 `[{ "i": 0, "k": 0, ... }]` 这样的乱码。
    # 所以这里用 raw 模式，按官方 chat_template.jinja 自己拼对话标记。
    # 这样用户只要 ollama pull 就能用，不需要额外 ollama create 一个修好的版本。
    _BOS = "<｜hy_begin▁of▁sentence｜>"
    _USER = "<｜hy_User｜>"
    _ASSISTANT = "<｜hy_Assistant｜>"
    # 两种全角竖线都要列进去：实测模型有时吐的是 ｠（U+FF60）而不是 ｜（U+FF5C），
    # 只挡一种的话另一种会原样漏进字幕（真的在直播里出现过：
    # 「索菲亚！<｠hy_end▁of▁sentence｠>」）。
    _STOP = ["<｜hy_end▁of▁sentence｜>", "<｜hy_place▁holder▁no▁2｜>",
             "<｠hy_end▁of▁sentence｠>", "<｠hy_place▁holder▁no▁2｠>"]

    def __init__(self):
        super().__init__()
        base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        self.url = base + "/api/generate"
        self.model = os.environ.get(
            "OLLAMA_HYMT2_MODEL", "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M")

    def _prompt(self, text, target, source, glossary=None):
        lang = LANG_NAMES.get(target, target)
        head = ""
        if glossary:
            lines = "\n".join("{} translates to {}".format(es, zh)
                               for es, zh in _as_pairs(glossary))
            if lines:
                head = "Reference the following translations:\n" + lines + "\n\n"
        return (head + "Translate the following text into {lang}. Note that you "
                "must ONLY output the translated result without any additional "
                "explanation:\n{text}".format(lang=lang, text=text))

    _OPTIONS = {"temperature": 0, "num_predict": 200, "stop": _STOP}

    async def translate(self, text, target, source="auto", glossary=None):
        body = {
            "model": self.model,
            "prompt": (self._BOS + self._USER
                       + self._prompt(text, target, source, glossary)
                       + self._ASSISTANT),
            "raw": True,          # 见上：自带模板是坏的，我们自己拼
            "stream": False,
            "options": self._OPTIONS,
            "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "30m"),
        }
        try:
            session = await self.session()
            async with session.post(self.url, data=json.dumps(body)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            return _strip_special(data.get("response") or "") or None
        except Exception:
            return None


# 兜底清理：停止词列表只能挡已知写法，模型偶尔会用别的括号变体。字幕是给人看的，
# 任何 <...hy_...> 的残留都不该出现在屏幕上。
_SPECIAL_RE = re.compile(r"<[^<>]{0,8}hy_[^<>]{0,40}>")


def _strip_special(text):
    return _SPECIAL_RE.sub("", text).strip()


def _as_pairs(glossary):
    """把词表统一成 (西语, 中文) 列表。

    历史上各引擎收的是一行紧凑串（"a = b; c = d"），新引擎要逐行格式。
    两种都收，省得调用方为不同引擎准备不同参数。"""
    if not glossary:
        return []
    if isinstance(glossary, str):
        out = []
        for chunk in glossary.split("; "):
            if " = " in chunk:
                es, zh = chunk.split(" = ", 1)
                out.append((es.strip(), zh.strip()))
        return out
    return list(glossary)


def _ollama_models():
    """启动时的一次性同步探测：Ollama 在跑的话，返回已拉取的模型名列表。"""
    import urllib.request

    base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as resp:
            data = json.load(resp)
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []


def _ollama_has(marker):
    return any(marker.lower() in name.lower() for name in _ollama_models())


def _ollama_has_gemma():
    return _ollama_has("translategemma")


HYMT2_SMALL = "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M"
HYMT2_LARGE = "hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M"


def _ollama_has_hymt2(large=False):
    return _ollama_has("hy-mt2-7b" if large else "hy-mt2-1.8b")


def create_translator(name):
    """返回带重复句缓存的翻译引擎（none 除外）。"""
    if name == "auto":
        # 顺序由实测定（tools/bench_glossary.py，280 个术语取自真实直播字幕，
        # 判据是词表要求的中文有没有出现在译文里）：
        #
        #                     总计    多词短语   商品名   中位延迟
        #   TranslateGemma 4B 47.9%    25.8%    64.4%     832ms
        #   Hy-MT2 1.8B       66.1%    65.0%    66.9%     335ms
        #   Hy-MT2 7B         83.2%    84.2%    82.5%    1439ms
        #
        # 多词短语那一列是关键：价格和促销条件都是短语（"lo tenemos en
        # especial"、"hacen una orden de"），审计翻出的 critical 错误全在这里。
        #
        # 延迟不参与排序。违禁词报警走识别原文，从不等翻译；翻译只要别把队列
        # 堵上就行，而切段是 9 秒，7B 的 P95 才 2.3 秒。
        #
        # 装了 7B 就用 7B：4.6 GB 是用户自己拉的，那本身就是一次选择。
        if _ollama_has_hymt2(large=True):
            name = "hymt2-7b"
        elif _ollama_has_hymt2():
            name = "hymt2"
        elif _ollama_has_gemma():
            name = "gemma"
        else:
            name = "google"
        print("[信息] 翻译引擎 auto → {}".format(name))
    if name == "none":
        return None
    if name == "hymt2":
        inner = OllamaHyMT2Translator()
    elif name == "hymt2-7b":
        inner = OllamaHyMT2Translator()
        inner.model = HYMT2_LARGE
        inner.name = "hymt2-7b"      # 两档要能在界面和日志里分得出来
    elif name == "gemma":
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
