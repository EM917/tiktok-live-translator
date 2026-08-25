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
        # 词表可以是紧凑串，也可以是词对列表（见 _as_pairs）。列表不可哈希，
        # 直接拿来做缓存键会抛 TypeError 把这次翻译整个打掉——调用方少写一个
        # tuple() 就会踩到。这里统一成可哈希形式。
        key = (text.strip(), target, source,
               tuple(map(tuple, glossary)) if isinstance(glossary, list)
               else (glossary or ""))
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
    # 常驻显存的时长。按需调用的实例会把它设成 0，用完立刻卸载。
    keep_alive = None

    async def translate(self, text, target, source="auto", glossary=None):
        body = {
            "model": self.model,
            "prompt": (self._BOS + self._USER
                       + self._prompt(text, target, source, glossary)
                       + self._ASSISTANT),
            "raw": True,          # 见上：自带模板是坏的，我们自己拼
            "stream": False,
            "options": self._OPTIONS,
            "keep_alive": (self.keep_alive if self.keep_alive is not None
                           else os.environ.get("OLLAMA_KEEP_ALIVE", "30m")),
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


# 兜底清理。这个正则被实测放宽过两次，每次都是模型吐出了没预料到的写法：
#   1. 第一版只匹配 <...hy_...>，实盘 88 条里漏了 12 条（13.6%）——模型
#      多数时候不带尖括号，就是一个全角竖线加 token 名：
#          「由于太受欢迎而售罄。｜hy_begin▁of▁sentence」
#   2. 分隔符也不止下划线：实测出现过 `｜hy-Assistant`（连字符）。
# 教训是别按「已知的几种写法」列举，而是认 hy 前缀加分隔符这个形状。
# 正常字幕里不会出现 hy_xxx / hy-xxx。
_SPECIAL_RE = re.compile(r"[<｜｠]*\s*hy[-_][A-Za-z0-9▁_-]+\s*[｜｠>]*")


def _strip_special(text):
    return _SPECIAL_RE.sub("", text).strip()


_DIGITS_RE = re.compile(r"\d+")


def looks_like_a_reply(source, translated):
    """译文看起来不是在翻译，而是在**回话**。

    实测的失败模式：Hy-MT2 7B 遇到「较长 + 句中带疑问」的输入时，会把它当成
    对话轮次去回答，而不是翻译。确定性复现，temperature 0 三次一致：
        源：…y ¿qué hago? ¿Me ven mis colegas? ¿No se van a creer que estoy loca?
        出：没关系，长时间坐着确实不太好。你可以在工作间隙适当活动一下…
    这在合规工具里是最坏的一类错误——凭空生成主播没说过的话，而且按质量等级
    规则它会覆盖掉本来正确的快译并留在记录里。

    加 system 角色钉不住（实测反而更像回话），所以只能事后检出。用结构信号，
    不猜语义：
      * 源文有问句而译文一个都没有 —— 回答会把问句吃掉
      * 源文里的数字在译文里消失 —— 价格和促销条件绝不能丢
    这只挡得住这一类，不是通用的幻觉检测；但它挡住的正是实测见过的那一类。
    """
    if not source or not translated:
        return False
    if "?" in source or "？" in source:
        if "?" not in translated and "？" not in translated:
            return True
    want = set(_DIGITS_RE.findall(source))
    if want and not (want & set(_DIGITS_RE.findall(translated))):
        return True
    return False


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


def _unload_siblings(keep_model):
    """把本机 Ollama 里**没在用的**同族模型从显存里卸掉。

    起因是一次真实故障：默认档从 7B 改回 1.8B 后，用户重启了程序，但 Ollama
    的 keep_alive=30m 让 7B 又占了半小时显存——1.8B(1.4G) + 7B(5.3G) 加上
    Whisper large-v3(~3G)，18 GB 的机器只剩 20% 空闲并开始换页，识别从 1.4 秒
    掉到 2.2 秒、音频积压到 20 秒。keep_alive 在只有一个模型时没问题，有了
    多档就必须自己清场。只卸载不删除，下次要用会自动重新载入。
    """
    import urllib.request

    base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    for model in (HYMT2_SMALL, HYMT2_LARGE, "translategemma:4b"):
        if model == keep_model:
            continue
        if not _ollama_has(model.split("/")[-1].split(":")[0]):
            continue
        body = json.dumps({"model": model, "keep_alive": 0}).encode()
        req = urllib.request.Request(
            base + "/api/generate", data=body,
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass          # 卸不掉不影响功能，只是内存紧一点


def strong_model():
    """当前本机可用的最强本地翻译引擎；没有则返回 None。"""
    if _ollama_has_hymt2(large=True):
        return HYMT2_LARGE
    if _ollama_has_hymt2():
        return HYMT2_SMALL
    return None


def create_strong_translator():
    """按需调用最强模型的实例，**用完立刻卸载**。

    不做成常驻的「专注模式」是实测决定的：7B 常驻会和 Whisper large-v3 抢
    统一内存，把识别从 1.4 秒拖到 3.2 秒，而识别在违禁词报警的链路上
    （报警延迟 6.8 秒 → 10.6 秒）。而装卸其实很便宜——载入 1.9 秒、
    单次调用全程 2.3 秒，keep_alive=0 调完即从显存消失。

    所以代价只落在需要它的那一次调用上，其余时间对识别零影响。这也是为什么
    这个功能按「哪一句」触发，而不是按「哪一段时间」。
    """
    model = strong_model()
    if model is None:
        return None
    tr = OllamaHyMT2Translator()
    tr.model = model
    tr.name = "strong"
    tr.keep_alive = 0
    return tr


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
        # 默认用 1.8B，**不是** 7B——这条是发布当天被实盘推翻后改回来的。
        # 7B 的词表遵从率确实高 17pp，但它在 18 GB 统一内存里和 Whisper
        # large-v3 抢资源，实测识别中位从 1.4s 涨到 3.2s（92 段，四分段
        # 分别是 3300/3224/3314/2850ms——是稳态争抢，不是跑久了变热）。
        # 识别在报警路径上，违禁词报警延迟因此从 6.8s 涨到 10.6s，
        # 而报警延迟正是这个工具存在的理由。翻译准确度是次要目标。
        # 另外 7B 会对某些句子直接返回空（实盘 90 条里 2 条），
        # 换温度、换 seed、去掉词表都救不回来，而 1.8B 同样两句翻得好好的。
        # 想要那 17pp 的可以显式 --translator hymt2-7b，代价写在 README 里。
        if _ollama_has_hymt2():
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
        _unload_siblings(inner.model)
    elif name == "hymt2-7b":
        inner = OllamaHyMT2Translator()
        inner.model = HYMT2_LARGE
        inner.name = "hymt2-7b"      # 两档要能在界面和日志里分得出来
        _unload_siblings(inner.model)
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
