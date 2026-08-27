"""领域词表：一份数据，三处生效。

带货直播的翻译错误里，商品名/成分名是最容易修也最值得修的一类——通用模型没
见过 "Quema Lonja"、"moringa"、"sea moss"，换多大的模型都不会自动变对，但一张
几十条的词表就能钉死。三处注入：

  1. 识别热词  —— 拼成一句自然的西语提示喂给 Whisper 的 initial_prompt，
     减少「moringa 听成 morenga」这类错误。注意这跟被关掉的「滚动上下文」
     完全不同：滚动上下文把模型**自己的输出**喂回去，会传播错误、诱发复读；
     静态词表不随时间漂移，没有误差累积路径。
     这一处还顺带提升违禁词检测的召回——检测跑在识别原文上，原文听对了才检得到。
  2. 翻译提示 —— 只把**本句里出现过的**词条拼进提示词（全表塞进去会稀释
     注意力、撑爆小模型的上下文）。
  3. 译文兜底 —— 模型没照做时用规则强制替换，确定性地保证译法一致。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_FILE = ROOT / "glossary.txt"
GLOSSARY_EXAMPLE = ROOT / "glossary.example.txt"

# Whisper 的 prompt 上限约 244 token，留出余量只取前若干条
# 实测教训：99 个词的提示曾让 Whisper 凭空多识别出一个词（"Escutadora"）。
# Whisper 的 initial_prompt 是被当作「上文」续写的，越长越容易把提示里的词
# 硬塞进结果。只放最容易听错的专有名词，且宁短勿长。
MAX_ASR_TERMS = 18


class Glossary:
    def __init__(self, entries):
        # entries: [(西语变体列表, 中文译法)]，保持文件顺序（前面的优先级高）
        self.entries = entries

    @property
    def enabled(self):
        return bool(self.entries)

    def asr_prompt(self, limit=MAX_ASR_TERMS):
        """给 Whisper 的热词提示。写成自然的西语句子而不是逗号词表——
        Whisper 的 prompt 是当作「上文」续写的，自然句式的引导效果更好。"""
        names = []
        for variants, _zh in self.entries[:limit]:
            if variants:
                names.append(variants[0])
        if not names:
            return None
        return "Productos: " + ", ".join(names) + "."

    def matching(self, text):
        """本句里出现的词条，返回 (命中的西语写法, 中文, 该条全部变体)。
        只注入命中的，避免整表塞进提示词。

        **长的写法优先**：短写法被长写法包含时只保留长的。有些口语短语的意思
        取决于后面跟什么——`un montón de cosas` 是「很多东西」，而
        `estoy sentada un montón` 是「坐了很久」；`lo hago de vuelta` 是
        「再做一遍」，而 `de vuelta acá` 是「回到这里」。靠长短区分之后，
        两个意思可以各写一条，不会同时塞进提示词互相打架。
        """
        low = text.lower()
        hits = []
        for variants, zh in self.entries:
            for v in variants:
                if v.lower() in low:
                    hits.append((v, zh, variants))
                    break
        # 命中的写法之间互为子串时，只留最长的那个。
        # 只处理包含关系：交叠但不互含的情况（sentada un montón / un montón de
        # 同时出现在 "sentada un montón de veces" 里）按长短取舍反而会选错，
        # 那是语言本身的歧义。1044 段真实语料里没出现过，不为它增加复杂度。
        kept = []
        for hit in hits:
            v = hit[0].lower()
            if any(v != other[0].lower() and v in other[0].lower() for other in hits):
                continue
            kept.append(hit)
        return kept

    def translation_hint(self, text, limit=6):
        """本句命中的术语，拼成紧凑的一行交给翻译引擎的**指令区**。

        注意绝不能把它拼进正文——TranslateGemma 是纯翻译模型，正文里的任何
        文字都会被翻译，指令会原样出现在译文里（实测踩过）。"""
        hits = self.matching(text)[:limit]
        if not hits:
            return ""
        return "; ".join("{} = {}".format(es, zh) for es, zh, _ in hits)

    def translation_pairs(self, text, limit=6):
        """本句命中的术语，以 (西语, 中文) 列表返回。

        与 translation_hint 的区别只是形态：TranslateGemma 吃一行紧凑串，
        Hy-MT2 的官方术语格式是逐行的 "X translates to Y"。同一份命中结果，
        让各引擎按自己的模板拼，别让某一家的格式渗到词表里。"""
        return [(es, zh) for es, zh, _ in self.matching(text)[:limit]]

    def apply(self, source_text, translated):
        """译文兜底：源文里出现过的词条，若译文里没有对应中文，就把该词条的
        西语原样残留替换成中文。只做保守替换——不确定时宁可不动。"""
        if not translated:
            return translated
        for _es, zh, variants in self.matching(source_text):
            if zh in translated:
                continue          # 模型已经翻对了
            # 译文里残留了西语原词才替换。逐个变体试——源文里是 "la moringa"，
            # 译文里残留的往往是不带冠词的 "moringa"。长的先试，避免
            # 短变体先命中把长写法切碎。
            for v in sorted(variants, key=len, reverse=True):
                pattern = re.compile(r"(?<![\w])" + re.escape(v) + r"(?![\w])",
                                     re.IGNORECASE)
                if pattern.search(translated):
                    translated = pattern.sub(zh, translated)
                    break
        return translated


# 冠词槽位可以换成物主代词。词条写的是 `las gotitas`，主播嘴里常说的却是
# `tus gotitas`（实录 11 次）、`tu limpieza`（5 次）、`tu orden`（5 次）——
# 89 条词条里有 62 条以冠词开头，也就是七成条目对物主结构整个失效。
# **只换槽位、不去掉槽位**：裸的 `gotitas`、`limpieza` 仍然不命中。去掉槽位
# 会踩回上面 gotitas 那条的坑（普通词被绑成商品名，把整段拽偏）。
_DETERMINERS = ("el", "la", "los", "las", "un", "una", "unos", "unas")
_POSSESSIVES = ("tu", "tus", "su", "sus", "mi", "mis", "nuestro", "nuestra")


def _with_possessives(variant):
    """`las gotitas` → 同时也认 `tus gotitas` / `su gotitas` / …"""
    head, _, rest = variant.partition(" ")
    if not rest or head.lower() not in _DETERMINERS:
        return [variant]
    return [variant] + [d + " " + rest for d in _POSSESSIVES]


def parse(text):
    """解析词表文本。格式：`变体1 | 变体2 => 中文译法   # 备注`"""
    entries = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=>" not in line:
            continue
        left, right = line.split("=>", 1)
        seen, variants = set(), []
        for raw in left.split("|"):
            for v in _with_possessives(raw.strip()):
                if v and v.lower() not in seen:
                    seen.add(v.lower())
                    variants.append(v)
        zh = right.strip()
        if variants and zh:
            entries.append((variants, zh))
    return entries


PROFILE_DIR = ROOT / "profiles"


def profile_path(streamer):
    """主播专属词表的路径；首次使用时从同名 .example 生成可编辑副本。
    没有这个主播的 profile 就返回 None。"""
    safe = re.sub(r"[^\w.\-]", "", str(streamer or ""))
    if not safe:
        return None
    target = PROFILE_DIR / (safe + ".txt")
    if not target.exists():
        example = PROFILE_DIR / (safe + ".example.txt")
        if not example.exists():
            return None
        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            target.write_text(example.read_text(encoding="utf-8"),
                              encoding="utf-8")
        except OSError:
            return None
    return target


def _merge(profile_entries, global_entries):
    """profile 在前、且按变体去重：同一个西语写法两边都有时，profile 赢。

    为什么必须做到变体级：matching() 会收集**所有**命中的条目，同一变体
    两边各挂一个不同中文的话，两条都会进提示词互相打架。"""
    claimed = {v.lower() for variants, _ in profile_entries for v in variants}
    merged = list(profile_entries)
    for variants, zh in global_entries:
        rest = [v for v in variants if v.lower() not in claimed]
        if rest:
            merged.append((rest, zh))
    return merged


def load(path=None, streamer=None):
    """读取词表；首次运行从模板生成一份用户可编辑的副本（模板入库、副本不入库，
    这样用户编辑不会挡住一键更新）。

    传入 streamer（主播用户名）时，会把 profiles/<主播>.txt 合并进来且优先。
    商品知识是逐主播的：把 Bella 的品名装进全局表，Elisa 的直播里就会凭空
    冒出别家商品（实测发生过——「凭空多出 D3 K2」在盲评里被判成捏造）。
    """
    target = Path(path) if path else GLOSSARY_FILE
    if not target.exists() and target == GLOSSARY_FILE and GLOSSARY_EXAMPLE.exists():
        try:
            target.write_text(GLOSSARY_EXAMPLE.read_text(encoding="utf-8"),
                              encoding="utf-8")
        except OSError:
            pass
    try:
        entries = parse(target.read_text(encoding="utf-8"))
    except OSError:
        entries = []
    prof = profile_path(streamer)
    if prof is not None:
        try:
            entries = _merge(parse(prof.read_text(encoding="utf-8")), entries)
        except OSError:
            pass
    return Glossary(entries)


# 当前会话实际生效的词表（全局 + 主播 profile 合并后）。存在的理由：
# DeepL 的原生术语表在引擎内部自己建表，它不知道现在看的是哪个主播——
# 让它拿这份，而不是重新 load() 一份只有全局条目的。表名里带内容指纹，
# 换主播后指纹变化会自动触发重建，不会拿着上一个主播的表继续用。
_ACTIVE = None


def set_active(glossary):
    global _ACTIVE
    _ACTIVE = glossary


def active():
    """当前生效的词表；会话还没开始时退回全局表。"""
    return _ACTIVE if _ACTIVE is not None else load()


def misplaced_entries(entries):
    """全局词表里与某个主播 profile 模板重复的条目。

    它们是老版本全局模板的遗留：模板升级不会改用户已经复制出去的
    glossary.txt，这些条目会继续污染其他主播的直播。只检测、只提示，
    永不代改用户的词表。判据取精确档：中文完全一致且至少共享一个西语变体。"""
    fingerprints = {}          # (variant_lower, zh) -> streamer
    try:
        examples = sorted(PROFILE_DIR.glob("*.example.txt"))
    except OSError:
        examples = []
    for example in examples:
        streamer = example.name[:-len(".example.txt")]
        try:
            for variants, zh in parse(example.read_text(encoding="utf-8")):
                for v in variants:
                    fingerprints[(v.lower(), zh)] = streamer
        except OSError:
            continue
    out = []
    for variants, zh in entries:
        for v in variants:
            owner = fingerprints.get((v.lower(), zh))
            if owner:
                out.append((owner, v, zh))
                break
    return out
