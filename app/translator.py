"""翻译引擎。auto：本地 Ollama 里有 TranslateGemma 就用它（完全本地），否则退回
Google 网页免费接口；也可显式指定 gemma / google / claude / openai / none。"""
import asyncio
import hashlib
import json
import os
import re
from collections import OrderedDict

def api_key(name):
    """取密钥：环境变量优先，其次界面里填的（存在 settings.json）。

    界面这条路是为了不逼用户开终端——这个工具的用户里有连终端是什么都不知道
    的人，而「设个环境变量」对他们等于这个功能不存在。
    settings.json 已在 .gitignore 里，密钥不会被提交；它也从不出现在
    发给页面的任何消息里（只回传打码后的尾四位）。
    """
    val = os.environ.get(name)
    if val:
        return val.strip()
    from .settings import load_settings
    return str(load_settings().get("api_keys", {}).get(name, "")).strip() or None


def mask_key(val):
    """给界面看的形态：只留尾四位，其余打码。"""
    if not val:
        return ""
    return "…" + val[-4:] if len(val) > 4 else "…"


TRANSLATOR_CHOICES = ["auto", "hymt2", "hymt2-7b", "gemma",
                      "deepl", "google", "claude", "openai", "none"]

# 需要密钥的引擎 -> 密钥所在的环境变量名（settings.json 里也用同名字段）
ENGINE_KEY_ENV = {"deepl": "DEEPL_API_KEY", "claude": "ANTHROPIC_API_KEY",
                  "openai": "OPENAI_API_KEY"}


def restore_engine(cli_value, saved, key_lookup=None):
    """启动时决定翻译引擎：命令行显式指定 > 界面上次的选择 > auto。
    返回 (引擎名, 警告文案或 None)。

    界面切换引擎时会 save_setting("translator", ...)，但曾经没有任何地方把它
    读回来——用户在页面选了 DeepL，重启后被静默重置回 auto。实测一整场直播
    （2026-08-26）就这样跑在了 1.8B 上，而用户一直按 DeepL 的预期看译文。

    密钥缺失时必须回落 auto 而不是照单恢复：需要密钥的引擎在构造时抛
    RuntimeError，启动路径据此直接退出——而重新填密钥恰恰得先把界面打开，
    照单恢复等于把用户锁在门外。
    """
    if cli_value:
        return cli_value, None
    name = str(saved or "").strip()
    if name not in TRANSLATOR_CHOICES:
        return "auto", None          # 没存过，或 settings.json 被改坏
    env = ENGINE_KEY_ENV.get(name)
    if env and not (key_lookup or api_key)(env):
        return "auto", ("上次选的翻译引擎 {} 还没有密钥，本次先用 auto——"
                        "在页面的「翻译引擎」里重新填一次即可".format(name))
    return name, None

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


class DeepLTranslator(BaseTranslator):
    """DeepL API。需要环境变量 DEEPL_API_KEY。

    免费额度的 key 以 `:fx` 结尾，走的是另一个域名——这里按后缀自动分辨，
    免得用户填对了 key 却因为域名不对一直 403。

    **注意字幕会发送给 DeepL**，与 Google 那条一样：我们监听的是别人的直播
    内容，这一点要由业务侧决定能不能接受。

    词表走 DeepL 的原生术语表（见 _ensure_glossary）。60 句真实字幕实测：

        不挂术语表   词表遵从 26.5%   中位 388ms
        挂上术语表   词表遵从 91.8%   中位 542ms
        本地 7B 对照 词表遵从 87.4%   中位 2017ms

    差的那 65 个百分点几乎全是商品名。术语表是这条引擎能不能用的分水岭。
    """

    name = "deepl"
    # DeepL 的目标语言代码与我们 UI 的差异
    _LANGS = {"zh-CN": "ZH-HANS", "zh-TW": "ZH-HANT", "zh": "ZH",
              "en": "EN-US", "pt": "PT-BR"}
    # 术语表接口只认不带地区的语言码。中文在那里只有一个 `zh`，而 glossary.txt
    # 里的译法是简体——实测把它挂到 ZH-HANT 上会把简体词塞进繁体译文
    # （"quiero las gotas" → "我想要维生素滴剂"），所以繁体目标不挂原生表，
    # 只留译后兜底替换。
    _GLOSSARY_TARGET = {"zh-CN": "zh", "zh": "zh"}
    # 只删我们自己建的表，用户在 DeepL 后台自建的不碰
    GLOSSARY_PREFIX = "tlt-"

    def __init__(self):
        super().__init__()
        self.api_key = api_key("DEEPL_API_KEY")
        if not self.api_key:
            raise RuntimeError("还没有填 DeepL 密钥——在页面上的「翻译引擎」里填一次即可")
        self.host = ("https://api-free.deepl.com" if self.api_key.strip().endswith(":fx")
                     else "https://api.deepl.com")
        self.host = os.environ.get("DEEPL_URL", self.host).rstrip("/")
        self.url = self.host + "/v2/translate"
        self._cooldown_until = 0.0
        # (源语言, 目标语言) -> 术语表 id；值为 None 表示试过建不起来，不再重试
        self._glossary_ids = {}
        self._glossary_source = None
        # 锁延迟到协程里建：Python 3.9 的 asyncio.Lock() 会绑到构造时的事件
        # 循环，而引擎对象是在管线线程之外建的，绑错了会在第一次用时炸。
        self._glossary_lock = None

    # ---- 原生术语表 ---------------------------------------------------

    @staticmethod
    def glossary_tsv(entries):
        """把 glossary.txt 压成 DeepL 要的 TSV：一行一个「西语写法<TAB>中文」。

        每个变体都单独占一行——DeepL 只做字面匹配，`la limpieza` 和
        `la limpiecita` 对它是两个词。源词重复会让整表创建失败，所以按小写
        去重，先出现的赢（与 glossary.txt 的优先级一致）。
        """
        seen, rows = set(), []
        for variants, zh in entries:
            zh = (zh or "").strip()
            if not zh or "\t" in zh or "\n" in zh:
                continue
            for v in variants:
                v = (v or "").strip()
                if not v or "\t" in v or "\n" in v or v.lower() in seen:
                    continue
                seen.add(v.lower())
                rows.append(v + "\t" + zh)
        return "\n".join(rows)

    def glossary_name(self, source, target, tsv):
        """表名里带词表内容的指纹——改了 glossary.txt 就是另一个名字，
        下次启动自动重建，不会拿着过期的表继续用。"""
        fp = hashlib.sha256(tsv.encode("utf-8")).hexdigest()[:12]
        return "{}{}-{}-{}".format(self.GLOSSARY_PREFIX, source, target, fp)

    async def _ensure_glossary(self, source, target):
        """拿到这对语言的术语表 id；建不起来就返回 None，且不再重试。

        **免费版只允许同时存在 1 个术语表**——实测建第 2 个直接 456
        "Too many glossaries"。所以顺序必须是先删我们自己的旧表再建新表，
        而不是先建后删。也正因为只有一个槽位，源语言认准第一次见到的那个：
        直播里偶尔蹦出一句被判成英语的字幕，不能让它把西语表挤掉。
        """
        gtarget = self._GLOSSARY_TARGET.get(target)
        if not gtarget:
            return None
        if self._glossary_source is None:
            self._glossary_source = source
        elif self._glossary_source != source:
            return None
        key = (source, gtarget)
        if key in self._glossary_ids:
            return self._glossary_ids[key]
        if self._glossary_lock is None:
            self._glossary_lock = asyncio.Lock()
        async with self._glossary_lock:
            if key in self._glossary_ids:
                return self._glossary_ids[key]
            gid = None
            try:
                gid = await self._build_glossary(source, gtarget)
            except Exception as exc:
                print("[警告] DeepL 术语表建不起来（{}），"
                      "本次按无术语表翻译——商品名会被直译".format(exc))
            self._glossary_ids[key] = gid
            return gid

    async def _build_glossary(self, source, gtarget):
        from .glossary import load as load_glossary

        tsv = self.glossary_tsv(load_glossary().entries)
        if not tsv:
            return None
        want = self.glossary_name(source, gtarget, tsv)
        status, data = await self._api("GET", "/v2/glossaries")
        mine = [g for g in (data.get("glossaries") or [])
                if str(g.get("name", "")).startswith(self.GLOSSARY_PREFIX)]
        for g in mine:
            if g.get("name") == want and g.get("ready"):
                return g.get("glossary_id")
        for g in mine:                      # 腾出唯一的槽位
            await self._api("DELETE", "/v2/glossaries/" + str(g.get("glossary_id")))
        status, made = await self._api("POST", "/v2/glossaries", form={
            "name": want, "source_lang": source, "target_lang": gtarget,
            "entries": tsv, "entries_format": "tsv"})
        if status >= 400 or not made.get("glossary_id"):
            raise RuntimeError("HTTP {} {}".format(status, made))
        print("[信息] DeepL 术语表已就绪：{} 条（{}→{}）".format(
            made.get("entry_count") or tsv.count("\n") + 1, source, gtarget))
        return made["glossary_id"]

    # ---- HTTP ---------------------------------------------------------

    async def _api(self, method, path, form=None, body=None):
        """DeepL 接口的薄封装，返回 (状态码, 解析后的 JSON)。

        单独成一层是为了让测试能整体替换掉它——DeepL 的术语表逻辑有先删后建、
        指纹复用这些容易写反的分支，那些必须能在不联网的情况下测。
        """
        session = await self.session()
        headers = {"Authorization": "DeepL-Auth-Key " + self.api_key}
        kw = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            kw["data"] = json.dumps(body)
        elif form is not None:
            kw["data"] = form
        async with session.request(method, self.host + path,
                                   headers=headers, **kw) as resp:
            status, text = resp.status, await resp.text()
        try:
            return status, (json.loads(text) if text.strip() else {})
        except ValueError:
            return status, {"message": text[:200]}

    async def translate(self, text, target, source="auto", glossary=None):
        import time

        # 额度用尽或被限流时冷却，别在每条字幕上继续撞
        if time.time() < self._cooldown_until:
            return None
        body = {"text": [text],
                "target_lang": self._LANGS.get(target, target.upper())}
        gid = None
        if source and source != "auto":
            body["source_lang"] = source.upper()
            gid = await self._ensure_glossary(source.lower(), target)
            if gid:
                body["glossary_id"] = gid
        try:
            status, data = await self._api("POST", "/v2/translate", body=body)
            if status in (429, 456):
                self._cooldown_until = time.time() + 120
                print("[警告] DeepL {}（{}），暂停 120 秒".format(
                    status,
                    "本月额度已用尽" if status == 456 else "被限流"))
                return None
            if status == 400 and gid:
                # 表可能被人在 DeepL 后台删了。丢掉缓存、这次先不带表翻，
                # 下一句会重建——不能因为术语表没了就整条引擎哑掉。
                self._glossary_ids.pop((source.lower(),
                                        self._GLOSSARY_TARGET.get(target)), None)
                retry = {k: v for k, v in body.items() if k != "glossary_id"}
                status, data = await self._api("POST", "/v2/translate", body=retry)
            if status != 200:
                return None
            items = data.get("translations") or []
            return (items[0].get("text") or "").strip() or None if items else None
        except Exception:
            return None


class ClaudeTranslator(BaseTranslator):
    """通过 Anthropic API（Claude Haiku）翻译，需要环境变量 ANTHROPIC_API_KEY。"""

    name = "claude"
    URL = "https://api.anthropic.com/v1/messages"
    MODEL = os.environ.get("CLAUDE_TRANSLATE_MODEL", "claude-haiku-4-5-20251001")

    def __init__(self):
        super().__init__()
        self.api_key = api_key("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("还没有填 Claude 密钥——在页面上的「翻译引擎」里填一次即可")

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
        self.api_key = api_key("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("还没有填 OpenAI 密钥——在页面上的「翻译引擎」里填一次即可")
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

    # temperature 必须逐次一致：Ollama 一旦发现 options 变了就会重新加载模型，
    # 实测会带来 ~4 秒停顿。num_predict 是例外——它按源文长度算（见
    # predict_cap），实测改动它不触发重载。
    _OPTIONS = {"temperature": 0}

    async def translate(self, text, target, source="auto", glossary=None):
        body = {
            "model": self.model,
            "prompt": self._prompt(text, target, source, glossary),
            "stream": False,
            "options": dict(self._OPTIONS, num_predict=predict_cap(text)),
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


# 模型名 -> 要不要 raw 模式。同一个模型每进程只问 Ollama 一次。
_RAW_MODE = {}


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

    # 1.8B 的 GGUF 里注册给 Ollama 的 chat template 是坏的——`{{ if .Prompt }}`
    # 块里根本没有 `{{ .Prompt }}`，末尾还留着 `onse }}` 这种被截断的残片。结果
    # 是用户的文本压根没进模型，输出是 `[{ "i": 0, "k": 0, ... }]` 这样的乱码。
    # 对那一档只能用 raw 模式，按官方 chat_template.jinja 自己拼对话标记。
    #
    # **但 7B 的模板是好的，而且用的是另一套标记**（`<|startoftext|>`、
    # `<|extra_0|>`）。把下面这套 1.8B 的标记套给它，等于往正文里塞了一串它
    # 不认识的普通文本，用户轮次从来没有被闭合——于是它一直以为在聊天：
    #   "¿Cómo se toman las gotitas?"  →  "服用滴剂的方法如下：首先，用温水…"
    #   "¿Podemos hacer un giveaway?"  →  "好的，那我们开始吧。请告诉我您希望…"
    # 一整场 567 段里 7.8% 只译出片段、7.1% 变成了回话、19.2% 换上了「您」的
    # 客服口吻。换回它自己的模板后这四类样例全部译对。
    # 所以 raw 模式是**按模型判定**的，判据就是问 Ollama 要模板看看坏没坏——
    # 哪天官方把 1.8B 的模板修好了，这里会自动跟着改用模板路径。
    _BOS = "<｜hy_begin▁of▁sentence｜>"
    _USER = "<｜hy_User｜>"
    _ASSISTANT = "<｜hy_Assistant｜>"
    # 两种全角竖线都要列进去：实测模型有时吐的是 ｠（U+FF60）而不是 ｜（U+FF5C），
    # 只挡一种的话另一种会原样漏进字幕（真的在直播里出现过：
    # 「索菲亚！<｠hy_end▁of▁sentence｠>」）。
    _STOP = ["<｜hy_end▁of▁sentence｜>", "<｜hy_place▁holder▁no▁2｜>",
             "<｠hy_end▁of▁sentence｠>", "<｠hy_place▁holder▁no▁2｠>",
             # 7B 走错模板时吐的是这个，名字是 message 不是 sentence——
             # 只列 sentence 的话它会原样漏进字幕（实测：「价格应该是57.20
             # 才对。<｠end▁of▁message」）。
             "<｜end▁of▁message｜>", "<｠end▁of▁message｠>", "<end▁of▁message"]

    def __init__(self):
        super().__init__()
        base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        self.url = base + "/api/generate"
        self.model = os.environ.get(
            "OLLAMA_HYMT2_MODEL", "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M")

    async def _needs_raw(self):
        """这个模型的自带模板能不能用。问一次就记住。

        判据是模板里有没有 `{{ .Prompt }}`——没有就意味着用户的文本根本不会
        进到模型里，只能自己拼标记。1.8B 的模板缺这一段，7B 的不缺。

        问不到（Ollama 不通、接口变了）时按 raw 处理：那是这个改动之前的行为，
        对模板确实坏掉的档位是唯一能出正确结果的路，宁可保守。
        """
        cached = _RAW_MODE.get(self.model)
        if cached is not None:
            return cached
        need = True
        try:
            session = await self.session()
            async with session.post(self.url.replace("/api/generate", "/api/show"),
                                    data=json.dumps({"model": self.model})) as resp:
                if resp.status == 200:
                    tpl = (await resp.json()).get("template") or ""
                    need = "{{ .Prompt }}" not in tpl
        except Exception:
            pass
        _RAW_MODE[self.model] = need
        return need

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

    _OPTIONS = {"temperature": 0, "stop": _STOP}
    # 常驻显存的时长。按需调用的实例会把它设成 0，用完立刻卸载。
    keep_alive = None

    async def translate(self, text, target, source="auto", glossary=None):
        opts = dict(self._OPTIONS, num_predict=predict_cap(text))
        prompt = self._prompt(text, target, source, glossary)
        raw = await self._needs_raw()
        body = {
            "model": self.model,
            "prompt": ((self._BOS + self._USER + prompt + self._ASSISTANT)
                       if raw else prompt),
            "raw": raw,           # 见上：只在自带模板坏掉时才自己拼标记
            "stream": False,
            "options": opts,
            "keep_alive": (self.keep_alive if self.keep_alive is not None
                           else os.environ.get("OLLAMA_KEEP_ALIVE", "30m")),
        }
        try:
            session = await self.session()
            async with session.post(self.url, data=json.dumps(body)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            if data.get("done_reason") == "length":
                # 撞到长度上限。实测 70 句真实翻译无一撞到，所以撞了就说明
                # 模型正在加料——直接判失败，让调用方保留原来那版译文。
                print("[警告] 译文撞到长度上限（疑似模型在加料），已丢弃")
                return None
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
# 全角竖线是这个模型词表里的分隔符，正常字幕里不会出现。实测残留过 `ａ｜>`
# 这种只剩半截的写法——它不含 hy_ 前缀，上面那条正则拦不住。
_BAR_RE = re.compile(r"[｜｠]+>?")
# 3. 还有不带 hy 前缀的：`<｠end▁of▁message`。这一类的形状是「▁ 连接的
#    token 名」，正常中文字幕里不会出现 ▁（U+2581）。
_TOKEN_RE = re.compile(r"[<｜｠]*\s*[A-Za-z]+(?:▁[A-Za-z]+)+\s*[｜｠>]*")
# 半角片假名连成一串也是词表残留（实测出现过 `<ｯｯｯｯ｝`）。
# 中文字幕里不会出现这种东西；要求连续三个以上，避免误伤偶发的单字符。
_KANA_RE = re.compile(r"[<>{}｛｝]?[ｦ-ﾟ]{3,}[<>{}｛｝]?")


def _strip_special(text):
    out = _SPECIAL_RE.sub("", text)
    out = _TOKEN_RE.sub("", out)
    return _KANA_RE.sub("", _BAR_RE.sub("", out)).strip()


_DIGITS_RE = re.compile(r"\d+")
_CN_NUM_RE = re.compile(r"[一二三四五六七八九十百千万两半]")


# 中文译文正常只有西语原文的三到六成长。实测 1959 对真实译文：
# 中位 0.35x、P95 0.52x、P99 0.66x，而唯一一条捏造的是 5.9x。
# 阈值放在 1.5x：这 1959 对里零误判，离捏造那条还差着四倍。
MAX_LENGTH_RATIO = 1.5

# 生成阶段的硬上限：按源文长度限制能吐多少 token。
#
# 这是比事后检查更强的一层——捏造在物理上就吐不出来，而不是吐出来再判掉。
# 实测 70 句真实翻译，输出 token / 源文字符的比值最大 0.364（中位 0.22）；
# 上限取 0.8，留 2.2 倍余量，那 70 句**零截断**。而捏造那条的比值约 3.6，
# 一定会撞上限。
#
# 于是「撞上限」本身成了一个近乎确定的信号：正常翻译从不撞它。
TOKENS_PER_SOURCE_CHAR = 0.8
MIN_PREDICT = 48          # 短句的地板，别把正常的短译文卡掉
MAX_PREDICT = 200


def predict_cap(text):
    return max(MIN_PREDICT, min(MAX_PREDICT,
                                int(len(text or "") * TOKENS_PER_SOURCE_CHAR)))
_MIN_LEN_FOR_RATIO = 12      # 太短的源文比值没意义（"Nada." 译成一句话很正常）


def looks_fabricated(source, translated):
    """译文看起来不是在翻译，而是在**回话**。

    实测到的两种：

    1. 「较长 + 句中带疑问」的输入会被当成对话轮次去回答。确定性复现，
       temperature 0 三次一致：
           源：…¿qué hago? ¿Me ven mis colegas? ¿No se van a creer que estoy loca?
           出：没关系，长时间坐着确实不太好。你可以在工作间隙适当活动一下…

    2. 短句后面接一整段与原文毫无关系的内容：
           源：Sigan dando el micrófono.（25 字符）
           出：请继续把麦克风交给他们。ａ｜>我是李明，来自北京。我是一名教师…（147 字符）
       前半句其实翻对了，后面整段是凭空生成的。
    这在合规工具里是最坏的一类错误——凭空生成主播没说过的话，而且按质量等级
    规则它会覆盖掉本来正确的快译并留在记录里。

    加 system 角色钉不住（实测反而更像回话），所以只能事后检出。用结构信号，
    不猜语义：
      * 源文有问句而译文一个都没有 —— 回答会把问句吃掉
      * 源文里的数字在译文里消失 —— 价格和促销条件绝不能丢
      * 译文比源文长出太多 —— 中文本来就比西语短，长出一大截只可能是加料
    这不是通用的幻觉检测，但每一条都由实盘见过的失败模式反推出来，
    且阈值取自真实分布而非拍脑袋。
    """
    if not source or not translated:
        return False
    # 问句：只在源文**以疑问为主**时才管（两个以上问号）。源文里只有一个问句、
    # 其余是陈述时，译文合并掉问号是正常的中文表达——实测按一个问号判会把
    # 大量正常译文误判成捏造。
    if source.count("?") + source.count("？") >= 2:
        if "?" not in translated and "？" not in translated:
            return True
    # 数字：只在译文里**一个数字符号都没有**时才算丢。中文会把数字换个写法
    # （pacto 3 → 第三个约定、20 millones → 2000万），逐个比对数值会把
    # 这些正常译文全判成错。
    if _DIGITS_RE.search(source) and not _DIGITS_RE.search(translated) \
            and not _CN_NUM_RE.search(translated):
        return True
    if (len(source) >= _MIN_LEN_FOR_RATIO
            and len(translated) > len(source) * MAX_LENGTH_RATIO):
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
    elif name == "deepl":
        inner = DeepLTranslator()
    elif name == "google":
        inner = GoogleWebTranslator()
    elif name == "claude":
        inner = ClaudeTranslator()
    elif name == "openai":
        inner = OpenAITranslator()
    else:
        raise ValueError("未知的翻译引擎: " + str(name))
    return CachedTranslator(inner)
