"""违禁词检测：挂在语音识别输出上，完全不依赖翻译。

设计取向由业务决定——**漏报的代价远高于误报**，所以这里一律偏向召回：
  * 扫的是 ASR 的 raw_text（含被质量过滤丢掉的部分），不是干净字幕；
  * 按**时间**做滑动窗口，而不是「最近 N 段」——切段长度以后还会调，
    用段数定义会让行为跟着变；
  * 三级命中（精确 / 变体 / 模糊），宁可多报。

不用 LLM：词表匹配足够快（微秒级），且结果可解释、可审计。
"""
import difflib
import re
import time
import unicodedata
from collections import deque

# 三级命中，按可信度从高到低
TIER_EXACT = "exact"        # 🔴 原样命中
TIER_VARIANT = "variant"    # 🟠 形态变化（单复数、阴阳性、动词变位）
TIER_FUZZY = "fuzzy"        # 🟡 ASR 可能听错一两个字母

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize(text):
    """归一化：去重音、转小写、去标点、压空白。

    西语 ASR 的重音符号很不稳定（sí/si、está/esta），必须先抹平；
    否则词表里写 "prohibido" 就匹配不到识别成 "prohíbido" 的输出。

    但 **ñ 要保留**：它在西语里是独立字母，不是 n 加个符号。把它并成 n 会
    制造语义完全不同的碰撞——最典型的是 año（年，高频口语）↔ ano（粗俗词，
    很可能出现在合规词表里），主播每说一次「一年」就误报一次，几轮下来中控
    就不再相信报警了。Whisper 对 ñ 的输出相当稳定，保留它几乎不损召回。
    """
    if not text:
        return ""
    text = text.lower().replace("ñ", "\x00")     # 占位，躲开下面的 NFD 分解
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("\x00", "ñ")
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _morph_variants(token):
    """西语常见形态变化的粗略还原（够用即可，不追求语言学严谨）。"""
    out = {token}
    for suffix, base in (("es", ""), ("s", ""), ("as", "a"), ("os", "o")):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            out.add(token[: len(token) - len(suffix)] + base)
    if token.endswith("a") and len(token) >= 4:      # 阴阳性
        out.add(token[:-1] + "o")
    if token.endswith("o") and len(token) >= 4:
        out.add(token[:-1] + "a")
    for dim in ("ito", "ita", "itos", "itas"):       # 指小词
        if token.endswith(dim) and len(token) - len(dim) >= 3:
            out.add(token[: len(token) - len(dim)])
    return out


def _stem_tokens(tokens):
    """把每个词映射到它的「词干候选集」，用于变体级匹配。"""
    return [_morph_variants(t) for t in tokens]


class BannedTermDetector:
    """扫描识别文本里的违禁词。线程/协程内直接调用即可，无 IO。"""

    def __init__(self, terms, window_sec=12.0, cooldown_sec=30.0,
                 fuzzy_ratio=0.86, min_fuzzy_len=5):
        # 有些违规不是固定词而是**模式**——平台指南里的真实违规案例
        # "Pasé de 97 kilos a 82"（我从 97 公斤降到 82）就是具体数字的体重变化，
        # 换个数字就是新的一句话，词表穷举不完。以 `re:` 开头的条目按正则处理。
        self.window_sec = window_sec
        self.cooldown_sec = cooldown_sec
        self.fuzzy_ratio = fuzzy_ratio
        self.min_fuzzy_len = min_fuzzy_len
        self.terms = []
        self.patterns = []
        for raw in terms:
            raw = raw.strip()
            if raw.lower().startswith("re:"):
                expr = raw[3:].strip()
                try:
                    self.patterns.append({"raw": raw, "re": re.compile(expr, re.I)})
                except re.error as exc:
                    print("[警告] 违禁词表里的正则无效，已跳过：{}（{}）".format(raw, exc))
                continue
            norm = normalize(raw)
            if not norm:
                continue
            tokens = norm.split()
            self.terms.append({
                "raw": raw,
                "norm": norm,
                "tokens": tokens,
                "stems": _stem_tokens(tokens),
            })
        self._window = deque()      # [(ts, normalized_text)]
        self._last_hit = {}         # term.raw -> ts，命中冷却，避免刷屏

    def reset_state(self):
        """换直播间时清空滑动窗口与命中冷却（词表保留）。

        不清的话有两个真实后果：上一场的冷却会把新一场同一个词的报警直接吞掉
        （真漏报），上一场残留的文本还会混进新一场第一条报警的上下文里，让审计
        证据失真。"""
        self._window.clear()
        self._last_hit.clear()

    @property
    def enabled(self):
        return bool(self.terms or self.patterns)

    @property
    def count(self):
        return len(self.terms) + len(self.patterns)

    def scan(self, text, ts=None):
        """喂入一段识别文本，返回本次新命中的列表。

        命中判定在「最近 window_sec 秒的拼接文本」上做——短语被切在两段之间
        （切得越短越常见）时仍然能命中。
        """
        if not self.enabled:
            return []
        ts = time.time() if ts is None else ts
        norm = normalize(text)
        if norm:
            self._window.append((ts, norm))
        cutoff = ts - self.window_sec
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()
        if not self._window:
            return []

        haystack = " ".join(chunk for _, chunk in self._window)
        tokens = haystack.split()
        stems = _stem_tokens(tokens)

        hits = []
        # 正则条目直接在归一化后的窗口文本上匹配
        for pat in self.patterns:
            m = pat["re"].search(haystack)
            if not m:
                continue
            last = self._last_hit.get(pat["raw"], 0)
            if ts - last < self.cooldown_sec:
                continue
            self._last_hit[pat["raw"]] = ts
            hits.append({"term": pat["raw"], "tier": TIER_EXACT, "ts": ts,
                         "matched": m.group(0), "context": haystack[-200:]})
        for term in self.terms:
            tier = self._match(term, haystack, tokens, stems)
            if not tier:
                continue
            last = self._last_hit.get(term["raw"], 0)
            if ts - last < self.cooldown_sec:      # 同一个词短时间内不重复报警
                continue
            self._last_hit[term["raw"]] = ts
            hits.append({
                "term": term["raw"],
                "tier": tier,
                "ts": ts,
                "context": haystack[-200:],
            })
        return hits

    def _match(self, term, haystack, tokens, stems):
        n = len(term["tokens"])
        if n == 0 or len(tokens) < n:
            return None
        # 精确级：按词序列整词匹配。不用子串——否则短词会误伤
        #（"cura" 会命中 "curación"）；词形变化交给下面的变体级处理
        for i in range(len(tokens) - n + 1):
            if tokens[i:i + n] == term["tokens"]:
                return TIER_EXACT
        # 变体级：逐个词比对词干候选集有没有交集
        for i in range(len(tokens) - n + 1):
            if all(term["stems"][j] & stems[i + j] for j in range(n)):
                return TIER_VARIANT
        # 模糊级：ASR 听错一两个字母（词太短时不做，否则误报爆炸）
        if len(term["norm"].replace(" ", "")) >= self.min_fuzzy_len:
            for i in range(len(tokens) - n + 1):
                window = " ".join(tokens[i:i + n])
                if difflib.SequenceMatcher(None, term["norm"], window).ratio() >= self.fuzzy_ratio:
                    return TIER_FUZZY
        return None


def load_terms(path):
    """从词表文件读取违禁词：一行一个，`#` 开头是注释，空行忽略。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    terms = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms
