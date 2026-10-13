"""弹幕留意的规则层（PR-2）：纯函数 + 刷屏引擎。不接线、没有界面文字、不碰模型和网络。

这一层只回答一件事：一条弹幕（加上同一个人和别的账号最近发过什么）看起来像不像
辱骂、扬言举报/号召不买、刷屏、引流。规则判定同步完成，不等任何模型。

* 文本两种形态：`light()` 保留 `? ¿ @ #`、数字和表情，链接/电话/问号/表情都在它上面判；
  `fold()` 在它之上再折叠拼写漂移，词表匹配用 fold 后的文本。`detector.normalize()`
  会抹掉 `?¿@#` 和表情，所以问句和结构信号必须在它之前判。
* 词表匹配复用 `app.detector.BannedTermDetector` 的公开接口（一段一个实例，每条弹幕先
  `reset_state()` 再 `scan(folded, ts=0)`），模糊档全关；detector.py 一行不改。
* 词表分三层（见 comment_guard/README.md）：红条档（用户确认过的词条单独命中就标红；没确认的写在
  `[段名:pending]` 里，命中只出琥珀）、线索档（只记 note，不上界面）、豁免档（不计分）。
  词表是业务判断，agent 只产出候选，出厂词表里的红条档一律是 `:pending`。
* 结果的形状固定（`CommentGuard.judge` 的返回值）：
    {"lvl": "alert"|"amber"|"note"|"lead"|None, "amb": None|"maybe_question"|"grievance"|
     "quote"|"accumulated"|"question_link"|"brigade"|"pending"|"self", "cats": [...], "sub": ...,
     "why": [...], "tier": "red"|"clue"|"struct"|None, "tags": [...],
     "route": None|"confirm"|"upgrade"}
  `lvl` 里 alert 是红条，amber 是拿不准（不算红条、不自动加入），note 不上界面只计数，
  lead 是只命中负面线索、只决定送不送模型。`tier` 说命中来自红条档词表、线索档，还是结构信号
  （链接/电话/刷屏）；自动加入关注名单只认 tier == "red" 且没有不确定标记（`auto_markable`）。
  `amb == "pending"`：命中的是还没经用户确认的红条档词条（`tier` 仍是 "red"，`tags` 含
  "pending"，`route == "confirm"`）；同一条弹幕里既有已确认命中又有待确认命中时按已确认的出红条。
  `amb == "self"`：强词出现在第一人称自指的句子里（que pendeja soy），降为琥珀，不自动加入。
* 刷屏要传真实时间戳（`judge(..., ts=...)` 的 ts 是必填关键字，必须是有限的 int / float）：不传会让
  滑动窗口永不过期，传了坏值在改任何状态之前就抛。窗口计数只算 ts - 窗口 <= t <= ts 的条目。
* 内存有界：每个用户的时间戳只留 120 秒、每人最多 200 条，用户表满 3000 清掉最旧的 500，
  多账号同句窗口最多 1000 条（每条最多回看 500 条、比较只取前 64 个词），多账号事件最多 50 个
  （每个最多记 2000 个账号），id 去重 LRU 2000，累计表同样 3000/500，体检警告最多 50 条。
* 被整条豁免的弹幕（互动词、正向表情、主播本人、房管、trusted）不进任何状态；主播本人按 `handle`
  字段比，不按昵称比。

界面文字不在这里：只输出代码（类别 id、琥珀理由、`why` 里的词表条目），中英文案由
PR-4 的 web/guard.js 和 PR-3 的 pipeline_guard.py 负责。词表体检的警告同样只给代码
（`Rules.warnings`），不带文字。
"""
import hashlib
import math
import re
import unicodedata
from collections import OrderedDict, deque
from itertools import islice
from pathlib import Path

from .detector import BannedTermDetector, normalize, read_terms

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_DIR = ROOT / "comment_guard" / "defaults"
LOCAL_DIR = ROOT / "comment_guard" / "local"

MAX_CHARS = 1000            # comment_worker 给的全文上限；结构正则在这上面跑
HEAD_WORDS = 60             # 长文：词表只匹配头 60 个词 + 尾 20 个词
TAIL_WORDS = 20
BREAK_WORD = "zzbreak"      # 头尾之间的断点词，防止拼出假短语
SEEN_IDS = 2000
USER_TABLE = 3000
USER_TABLE_DROP = 500
USER_HISTORY = 120          # 每个用户的时间戳只留这么多秒
USER_HISTORY_MAX = 200
RECENT_MAX = 1000           # 多账号同句窗口里最多记这么多条
RECENT_SCAN = 500           # 每条弹幕最多回看这么多条：一次冲场不能把单条判定拖慢
ROLES_EXEMPT = ("anchor", "mod")

# ------------------------------------------------------------------------------------------
# 文本的两种形态
# ------------------------------------------------------------------------------------------

_INVISIBLE = {ord(c): None for c in (
    "\u00ad\u034f\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064"
    "\ufeff\u20e3\u202a\u202b\u202c\u202d\u202e")}
_INVISIBLE.update({cp: None for cp in range(0xFE00, 0xFE10)})            # 变体选择符
_INVISIBLE.update({cp: None for cp in range(0xE0100, 0xE01F0)})          # 变体选择符补充
_INVISIBLE.update({cp: None for cp in range(0xE0000, 0xE0080)})          # 标签字符
# 韩文填充符、盲文空白、蒙古文元音分隔符、组合用的圈、高棉固有元音：看不见，却能把一个词劈成两个
_INVISIBLE.update({cp: None for cp in (0x3164, 0x115F, 0x1160, 0xFFA0, 0x2800, 0x180E,
                                       0x0488, 0x0489, 0x17B4, 0x17B5)})
# Unicode 类别 Cf（格式字符，如双向隔离符 U+2066..2069）和 Cs（孤立代理码点）一律删掉
_DROP_CATEGORIES = frozenset(("Cf", "Cs"))
# 西里尔/希腊同形字 -> 拉丁（写成转义：字面字符和拉丁字母肉眼分不出）
_CONFUSABLE = {
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p",
    "\u0441": "c", "\u0443": "y", "\u0445": "x", "\u0456": "i",
    "\u0455": "s", "\u0458": "j", "\u04bb": "h", "\u04cf": "l",
    "\u03b1": "a", "\u03bf": "o", "\u03c1": "p", "\u03bd": "v",
    "\u03b9": "i", "\u03ba": "k", "\u03c4": "t",
}
_LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
_SLANG = {"q": "que", "k": "que", "ke": "que", "xq": "porque", "pq": "porque", "porq": "porque",
          "xk": "porque", "pk": "porque", "tmb": "tambien", "tb": "tambien", "aki": "aqui",
          # 英语脏话的常见拼法变体（f*ck 这种带遮挡符的另走 CommentGuard._unmask）
          "fvck": "fuck", "fuk": "fuck", "fck": "fuck", "phuck": "fuck"}
_APOS = re.compile(r"(?<=\w)['\u2019`\u00b4](?=\w)")
_REPEAT = re.compile(r"([a-z])\1{2,}")
_SPACED = re.compile(r"(?<![^\s])(?:[a-z][\s.\-_*]){3,}[a-z](?![^\s])")
_SPACE = re.compile(r"\s+")
_LONGDIGITS = re.compile(r"\d{4,}")


def light(text):
    """轻折叠：NFKC、去零宽/格式字符/变体选择符/keycap/孤立代理码点、小写、同形字还原、压空白。

    保留 `? ¿ @ #`、数字和表情：问句、链接、电话、@账号、表情都在它上面判。
    NFKC 之后再截一次 MAX_CHARS：有的字符（U+FDFA）展开后是 18 个，不截的话一条 1000 字的
    弹幕会变成 18000 字，既超过 I3 的 20 ms，也让刷屏表里的键涨到 18 KB。"""
    t = unicodedata.normalize("NFKC", text or "")[:MAX_CHARS].translate(_INVISIBLE).lower()
    t = "".join(_CONFUSABLE.get(c, c) for c in t)
    if not t.isascii():
        t = "".join(c for c in t if unicodedata.category(c) not in _DROP_CATEGORIES)
    return _SPACE.sub(" ", t).strip()


def _join_spaced(match):
    return re.sub(r"[\s.\-_*]", "", match.group(0))


def _leet_token(tok):
    letters = sum(c.isalpha() for c in tok)
    leets = sum(c in _LEET for c in tok)
    if letters >= 2 and leets >= 1 and letters + leets >= 4 and not _LONGDIGITS.search(tok):
        return "".join(_LEET.get(c, c) for c in tok)
    return tok


def _join_letter_runs(tokens):
    """>= 4 个连续的单字母 token 拼回一个词（p u t a -> puta）。`S M L XL` 这种不到 4 个单字母的不动。"""
    out, run = [], []
    for tok in tokens + [""]:
        if len(tok) == 1 and "a" <= tok <= "z":
            run.append(tok)
            continue
        if run:
            out.extend(["".join(run)] if len(run) >= 4 else run)
            run = []
        if tok:
            out.append(tok)
    return out


def fold(text):
    """给词表匹配用的折叠：light -> 去撇号 -> ñ 写成 n -> 同一字母连续 3 个以上压成 1 个 ->
    连续单字母拼回 -> leet 还原 -> detector.normalize() -> 再拼一遍单字母 -> 俚语展开。

    弹幕是手机打字，常省 ñ，和违禁词那边（语音识别，保留 ñ）相反，所以两边（输入和词条）
    都写成 n。normalize 是 app.detector 的那个，原样调用。

    单字母拼回做两遍：第一遍在 normalize 之前，认空格 . - _ * 隔开的；表情、逗号、斜杠、竖线、波浪线
    隔开的（p🔥u🔥t🔥a、p,u,t,a）要等 normalize 把它们变成空格之后才看得出来，所以 normalize 之后
    再把 >= 4 个连续的单字母 token 拼一遍。"""
    t = _APOS.sub("", light(text)).replace("ñ", "n")
    t = _REPEAT.sub(r"\1", t)                                  # holaaaa -> hola；ll rr cc 双写不动
    t = _SPACED.sub(_join_spaced, t)                           # e s t a f a -> estafa
    t = " ".join(_leet_token(tok) for tok in t.split(" "))     # h1j0 d3 put4 -> hijo de puta
    return " ".join(_SLANG.get(tok, tok) for tok in _join_letter_runs(normalize(t).split()))


def fold_key(text):
    """折叠后文本的短哈希：同一句话（拼写漂移之后）的缓存键，给 PR-5 的同句缓存用。"""
    return hashlib.sha1(fold(text).encode("utf-8")).hexdigest()[:16]


def clip_words(folded):
    """长文：只留头 60 + 尾 20 个词，中间插一个断点词，防止拼出假短语。"""
    toks = folded.split()
    if len(toks) <= HEAD_WORDS + TAIL_WORDS:
        return folded
    return " ".join(toks[:HEAD_WORDS] + [BREAK_WORD] + toks[-TAIL_WORDS:])


# ------------------------------------------------------------------------------------------
# 结构信号：问句、链接、电话、@账号（都在 light 文本上判）
# ------------------------------------------------------------------------------------------

# 没有问号时，只有真正的疑问词起头才当问句。hay / tiene / tienen / dan / pueden / puedo / sera /
# alguien / se puede 这些不是疑问词（「hay puros estafadores aqui」是陈述句），感叹的
# cuanta estafa / what a scam 也不是：它们走 _QWEAK，不再算问句，带 maybe_question 标记
_QSTART = re.compile(
    r"^(?:como|donde|cual|cuales|quien|quienes|cuando|"
    r"is it|is this|is she|are they|are you|does it|do they|do you|can i|can you|how|why|"
    r"where|who|when|what(?!\s+an?\b))\b")
_QCUANTO = re.compile(r"^(?:cuanto|cuanta|cuantos|cuantas)\b")
# cuanto cuesta / cuantos dias dura 是问句；cuanta estafa / cuanto ladron junto 是感叹
_QCUANTO_ASK = re.compile(
    r"^(?:cuanto|cuanta|cuantos|cuantas)\s+(?:cuesta|cuestan|sale|salen|vale|valen|es|son|dura|duran|"
    r"tiempo|dias|meses|horas|semanas|minutos|falta|faltan|trae|vienen|viene|me|nos|le|les|se|hay|"
    r"mide|pesa|llega|tarda|tardan|cobran|cobra)\b")
_QWEAK = re.compile(
    r"^(?:hay|dan|tiene|tienen|tienes|pueden|me pueden|puedo|sera|seria|alguien|se puede|"
    r"what an?)\b")
# 句尾追问：neta 只认句尾，句首的「neta es fraude」是断言
_TAIL_QUESTION = re.compile(r"\b(?:verdad|cierto|neta|o que|no)$")
_CHOICE_SPLIT = ("o", "or")
_ANTONYMS = frozenset((
    "original", "originales", "real", "reales", "autentico", "autentica", "autenticos",
    "autenticas", "verdadero", "verdadera", "verdaderos", "verdaderas", "legit", "legitimo",
    "legitima", "genuine", "authentic"))
_COPULA_START = re.compile(r"^(?:no )?(?:es|son)\b")
_RHETORICAL = re.compile(
    r"\b(?:nos|me|les|los)?\s*(?:estafan|estafar|roban|robar|enganan|enganar|mienten|mentir|"
    r"venden|cobran|quieren)\b")
_VICTIM = re.compile(r"\bme\s+(?:estafaron|robaron|enganaron|mintieron|timaron|vendieron)\b")
_QUOTE = re.compile(
    r"\b(?:dicen que|dice que|dijeron que|dijo que|me dijeron|me dijo|escuche que|oi que|lei que|"
    r"segun dicen|se dice que|comentan que|they say|people say|i heard|i read that|someone said|"
    r"he said|she said)\b")
_SECOND_PERSON = re.compile(r"\b(?:tu|tus|te|ti|contigo|eres|estas|usted|ustedes|vos|vienes|haces)\b")
# 强词所在的句子在说自己（que pendeja soy / soy una idiota）且没有第二人称标记：降为琥珀
_SELF_STRONG = re.compile(r"\b(?:soy|me siento|i am|im|i m)\b")
_SECOND_EN = re.compile(r"\b(?:you|youre|your|yourself|ur|u)\b")
_SELFREF = re.compile(
    r"\b(?:estoy|estaba|estuve|me siento|me veo|me puse|me puso|yo|soy|fui|mi|mis|quiero bajar|"
    r"necesito bajar)\b")


def weak_question_start(light_text, folded):
    """没有问号、不以疑问词起头，但以 hay / tienen / dan / pueden / alguien / 感叹的 cuanta…
    起头的短句：看起来像问句，也可能是陈述。不当问句（不进 question_exempt），有指称词时带
    maybe_question 标记出琥珀，交模型确认。"""
    if "?" in light_text or "\u00bf" in light_text:
        return False
    toks = folded.split()
    if not toks or len(toks) > 8:
        return False
    if _QWEAK.match(folded):
        return True
    return bool(_QCUANTO.match(folded)) and len(toks) > 1 and not _QCUANTO_ASK.match(folded)


def is_question(light_text, folded, tail=True):
    """问句：有 ? / ¿，或疑问词开头的短句，或（tail=True 时）句尾追问：verdad / o no / o que /
    cierto / neta / si o no / no 结尾。「es una estafa」和「¿es una estafa?」去掉标点后一模一样，
    所以问号必须在 normalize 之前认。链接的问句豁免只认前两种（tail=False）：不能让一句引流话术
    靠结尾加个「no」就躲过去。"""
    if "?" in light_text or "\u00bf" in light_text:
        return True
    toks = folded.split()
    if toks and len(toks) <= 8 and (_QSTART.match(folded) or (
            _QCUANTO.match(folded) and not weak_question_start(light_text, folded))):
        return True
    return tail and bool(_TAIL_QUESTION.search(folded))


def is_choice_question(folded):
    """选择问句「X o Y」：两边都 <= 4 个词，且句里有指称词的反义词（original / real /
    authentic 一类）：'son originales o son falsos'、'es pirata o original'。

    比「有一边含指称词」收得更窄：'son ladrones o estafadores' 两边都是指称词、没有反义词，
    是指控不是在问，不能因为中间有个 o 就整条不显示。"""
    toks = folded.split()
    if not (_ANTONYMS & set(toks)):
        return False
    for i, tok in enumerate(toks):
        if tok in _CHOICE_SPLIT and 0 < i < len(toks) - 1 and i <= 4 and len(toks) - i - 1 <= 4:
            return True
    return False


_TLDS = ("com|net|org|mx|shop|store|link|ly|me|io|co|tv|app|info|biz|xyz|site|online|cc|gt|hn|sv|cr")
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s]+|"
    r"\b(?:wa\.me|t\.me|bit\.ly|tinyurl\.com|linktr\.ee|cutt\.ly|ig\.me|fb\.me|youtu\.be|"
    r"vm\.tiktok\.com|instagram\.com|facebook\.com|tiktok\.com|onlyfans\.com|chat\.whatsapp\.com|"
    r"discord\.gg)/?[^\s]*|"
    r"\b[a-z0-9][a-z0-9\-]{1,}\.(?:com|net|org|mx|shop|store|link|ly|tv|app|info|biz|xyz|site|"
    r"online|cc|gt|hn|sv|cr)(?:/[^\s]*)?(?![a-z0-9])")
_EMAIL_RE = re.compile(r"[a-z0-9][a-z0-9._%+\-]*@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\d])(\+?\d(?:[\s\-.()]{0,2}\d){7,13})(?![\d])")
_SPELLED_RE = re.compile(
    r"(?:\b(?:cero|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve)\b[\s,.\-]*){8,}")
_HANDLE_RE = re.compile(r"(?<![\w.])@([a-z0-9_.]{3,24})")
# 账号标签：@账号只有被这些词引出时才算引流的载荷；句首或句中呼叫朋友的「@名字 ……」不算
_HANDLE_LABELS = (r"(?:ig|insta|instagram|fb|face|facebook|tt|tiktok|tik tok|snap|snapchat|telegram|"
                  r"tele|tg|ws|wsp|wa|whatsapp|wasap|cuenta|perfil|usuario|pagina|page|"
                  r"sigueme en|siganme en|buscame como|me encuentras como|encuentrame como)")
_HANDLE_LABEL_RE = re.compile(r"\b" + _HANDLE_LABELS + r"\s*[:\-]\s*@?([a-z0-9_.]{3,24})")
_HANDLE_LABEL_AT_RE = re.compile(r"\b" + _HANDLE_LABELS + r"\s+@([a-z0-9_.]{3,24})")
# 第一人称的账号标签（mi ig @x / sigueme en @x / buscame como @x）：这本身就是拉人关注，不需要
# 再有别的话术
_SELF_HANDLE_RE = re.compile(
    r"\b(?:mi\s+(?:ig|insta|instagram|fb|facebook|tiktok|tt|cuenta|perfil|pagina|page)|"
    r"sigueme en|siganme en|buscame como|me encuentras como|encuentrame como)\s*[:\-]?\s*@([a-z0-9_.]{3,24})")
# 起点限定在数字串开头，否则长数字串/长点串上是平方级回溯
_MONEY_RE = re.compile(
    r"\$\s?\d[\d.,]*|(?<![\d.,])\d[\d.,]*\s?(?:pesos|mxn|usd|dolares|lempiras|quetzales|colones)\b")
_LONGNUM_RE = re.compile(r"\d{14,}")
# 只认合理的日期：日 <= 31、月 <= 12、年 2 或 4 位，而且不处在更长的数字串组里
# （55-12-34-56-78 是电话，不是 12-34-56 这个「日期」）
_DATE_RE = re.compile(
    r"(?<!\d)(?<!\d[/\-])(?:0?[1-9]|[12]\d|3[01])[/\-](?:0?[1-9]|1[0-2])[/\-](?:\d{4}|\d{2})"
    r"(?!\d)(?![/\-]\d)")
# 号码前面的单号标签：「mi numero de guia es 5512345678」。短标签 no / nu 要左边界，否则「telefono」
# 「uno」里的 no 会把电话当成单号
_ORDERNO_RE = re.compile(
    r"(?:(?:pedido|guia|orden|folio|rastreo|seguimiento|tracking|ticket|factura|order|numero de)"
    r"(?:\s+(?:es|is|son|fue))?|#|(?<![a-z])n[ou]\.?)\s*:?\s*#?\s*$")
_ORDER_CTX = re.compile(
    r"\b(?:pedido|pedir|ordenar|comprar|compre|compro|quiero|apartar|aparto|envio|enviar)\b")
_URL_BRACKET_DOT = re.compile(r"\s*[\[({]\s*(?:\.|dot|punto)\s*[\])}]\s*")
_URL_PUNTO = re.compile(r"\s+(?:punto|dot)\s+(" + _TLDS + r")\b")
_URL_SPACED_DOT = re.compile(r"(?<=[a-z0-9])(?:\s+\.\s*|\s*\.\s+)(?=(?:" + _TLDS + r")\b)")
_URL_COMMA = re.compile(r"(?<=[a-z0-9]),\s*(?=(?:com|net|org|mx|shop|store|link)\b)")
_URL_SCHEME = re.compile(r"\b(https?)\s*:\s*/\s*/\s*")
_URL_WA = re.compile(r"\b(wa|t|ig|fb)\s*\.\s*me\s*/\s*")
_URL_TRAIL = ".,;:!?)]}>'\"»"


def _join_single_chars(t):
    """>= 2 个连续的单字符 token（字母 . : /）拼回去：`w a . m e /` -> `wa.me/`，`h t t p s : / /`
    -> `https://`。只给链接识别用，不影响别处。"""
    out, run = [], []
    for tok in t.split(" ") + [""]:
        if len(tok) == 1 and (tok.isalpha() or tok in ".:/"):
            run.append(tok)
            continue
        if run:
            out.append("".join(run) if len(run) >= 2 else run[0])
            run = []
        if tok:
            out.append(tok)
    return " ".join(out)


def _url_normalize(t):
    t = _URL_BRACKET_DOT.sub(".", t)
    t = _URL_PUNTO.sub(r".\1", t)
    t = _URL_SPACED_DOT.sub(".", t)
    t = _URL_COMMA.sub(".", t)
    t = re.sub(r"\bhxxp", "http", t)
    t = _URL_SCHEME.sub(r"\1://", t)
    return _URL_WA.sub(r"\1.me/", t)


def _phone_text(light_text):
    return _LONGNUM_RE.sub(" ", _DATE_RE.sub(" ", _MONEY_RE.sub(" ", _EMAIL_RE.sub(" ", light_text))))


def phone_hits(light_text):
    """电话号码候选：8~12 位数字（带 + 可到 13 位），排除金额/日期/单号上下文，14 位以上无分隔
    的当快递单号。另认连续 >= 8 个西语数字词。返回数字串列表（读法绕写返回 "spelled"）。"""
    t = _phone_text(light_text)
    hits = []
    for m in _PHONE_RE.finditer(t):
        digits = re.sub(r"\D", "", m.group(1))
        # 13 位：带 + 的国际号，或墨西哥手机的 044 / 045 前缀；其余 13 位以上当快递单号
        if not (8 <= len(digits) <= 12 or (len(digits) == 13 and (
                m.group(1).startswith("+") or digits.startswith(("044", "045"))))):
            continue
        # 单号上下文：只看号码前 16 个字符，但左边界（「telefono」里的 no 不是「no.」）要看得到更前面
        if _ORDERNO_RE.search(t, max(0, m.start() - 16), m.start()):
            continue
        hits.append(digits)
    if _SPELLED_RE.search(normalize(t)):
        hits.append("spelled")
    return hits


def email_hits(light_text):
    """邮箱地址：不是链接，是联系方式（单独 note，和 offer / pull 同现红）。"""
    return _EMAIL_RE.findall(light_text)


def url_hits(light_text):
    """链接候选（含 `punto com`、`[.]`、`[dot]`、` . com`、`,com`、`hxxp`、`wa . me /`、拆字母的
    `h t t p s :` 这类绕写）。邮箱地址先拿掉，不当链接。不做没有点的 `mitienda com`。"""
    t = _url_normalize(_EMAIL_RE.sub(" ", light_text))
    found = [m.group(0) for m in _URL_RE.finditer(t)]
    joined = _url_normalize(_join_single_chars(t))
    if joined != t:
        for m in _URL_RE.finditer(joined):
            if m.group(0) not in found:
                found.append(m.group(0))
    return found


def handle_hits(light_text, streamer="", labeled_only=True):
    """@账号。默认只返回被账号标签引出的（`ig: x`、`mi ig @x`、`sigueme en @x`、`mi cuenta @x`）：
    引流的载荷只认这种；句首或句中呼叫朋友的「@名字 ……」不算。labeled_only=False 返回所有 @。
    主播自己不算。"""
    own = (streamer or "").lower()
    found = _HANDLE_LABEL_RE.findall(light_text) + _HANDLE_LABEL_AT_RE.findall(light_text)
    if not labeled_only:
        found += _HANDLE_RE.findall(light_text)
    return [h for h in found if h.rstrip(".") != own]


def self_handle_hits(light_text, streamer=""):
    """第一人称账号标签引出的 @账号（mi ig @x、sigueme en @x）：拉人关注，自己就是话术。"""
    own = (streamer or "").lower()
    return [h for h in _SELF_HANDLE_RE.findall(light_text) if h.rstrip(".") != own]


def _split_url(url):
    """链接 -> (host, path)：去协议、尾随标点、用户信息、端口；host 小写。"""
    u = url.strip().rstrip(_URL_TRAIL)
    u = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", u)
    host, sep, path = u.partition("/")
    host = host.split("?")[0].split("#")[0].rsplit("@", 1)[-1].split(":")[0].strip(".")
    return host, ("/" + path) if sep else ""


def _host_matches(host, path, entry):
    """豁免名单里的一条是否盖住这个链接：host == 条目，或 host 以 `.条目` 结尾；条目带路径时还要
    路径以它开头。按 host 比，不按子串比：`tienda-marcaejemplo.com`、`marcaejemplo.com.ofertas.shop`
    都不是 `marcaejemplo.com`。"""
    eh, ep = _split_url(entry)
    if not eh or "." not in eh or eh.startswith("@"):
        return False
    if not (host == eh or host.endswith("." + eh)):
        return False
    return not ep or path.startswith(ep)


_OWN_HOSTS = frozenset(("tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com"))


# ------------------------------------------------------------------------------------------
# 词表：格式、档位、加载
# ------------------------------------------------------------------------------------------

RED, CLUE, EXEMPT = "red", "clue", "exempt"
STRUCT = "struct"
# 红条档的段名后面可以加 `:pending`：`[threat:pending]` = 词条还没经用户确认，命中只出琥珀
# （amb == "pending"，不标红，不自动加入）；用户确认一条，就把它从 `[x:pending]` 挪到 `[x]`
PENDING = ":pending"

# 文件 -> {段名: 档位}。认不出的段名在体检里报 unknown_section，不静默忽略。
# 红条档的每个段另有一个 `:pending` 版本（见文件末尾的循环）
SECTIONS = {
    "insult": {"strong": RED, "threat": RED, "sexual": RED, "target": RED,
               "medium": CLUE, "watch": CLUE, "sexual-soft": CLUE, "exempt-phrases": EXEMPT},
    "accuse": {"call": RED, "report": RED, "assert": RED, "assert-frame": RED,
               "institution": CLUE, "negative": CLUE, "exempt-phrases": EXEMPT},
    "promo": {"pull": RED, "scam-strong": RED, "offer": CLUE, "lure": CLUE, "scam-weak": CLUE,
              "exempt-phrases": EXEMPT, "ask": EXEMPT},
    "interact": {"words": EXEMPT, "places": EXEMPT, "positive": EXEMPT, "positive-emoji": EXEMPT},
    "cues": {"neg-emoji": CLUE, "irony": CLUE},
}
for _secs in SECTIONS.values():
    for _sec in [name for name, tier in _secs.items() if tier == RED]:
        _secs[_sec + PENDING] = RED
LEX_FILES = tuple(SECTIONS)
LOCAL_LEX_FILES = ("insult", "accuse", "promo")


def is_pending(sec):
    return sec.endswith(PENDING)


def base_sec(sec):
    """`threat:pending` -> `threat`。"""
    return sec[:-len(PENDING)] if sec.endswith(PENDING) else sec


# 交给 BannedTermDetector 匹配的段（其余段是词表、短语或正则，另有专用的匹配方式）
MATCHED = frozenset(
    [("insult", s) for s in ("strong", "threat", "sexual")]
    + [("accuse", s) for s in ("call", "report", "assert")]
    + [("promo", s) for s in ("pull", "scam-strong")]
    + [(f, s + PENDING) for f, s in
       [("insult", "strong"), ("insult", "threat"), ("insult", "sexual"),
        ("accuse", "call"), ("accuse", "report"), ("accuse", "assert"),
        ("promo", "pull"), ("promo", "scam-strong")]]
    + [("insult", "medium"), ("insult", "watch"), ("insult", "sexual-soft"),
       ("accuse", "institution"), ("accuse", "negative"),
       ("promo", "offer"), ("promo", "lure"), ("promo", "scam-weak"),
       ("cues", "irony")])
# 能写 `re:` 正则的段：交给检测器匹配的段，加上 promo 的 ask。别的段（exempt-phrases、interact、
# target、assert-frame、cues/neg-emoji）是词表、短语或表情，写了 re: 也用不上，体检报 regex_not_supported
REGEX_SECTIONS = MATCHED | {("promo", "ask")}
# 只有这两段允许检测器的「变体」档（单复数/阴阳性）：其余段变体档会把 perro 当 perra、zorro 当 zorra
VARIANT_OK = frozenset((("insult", "strong"), ("accuse", "assert"),
                        ("insult", "strong" + PENDING), ("accuse", "assert" + PENDING)))
# 同一词条写在多个段里时取最严的档：已确认的红条档 > 待确认的红条档 > 线索档；同档里靠前的更严
_STRICT_ORDER = {
    "insult": ("threat", "strong", "sexual", "target", "medium", "sexual-soft", "watch"),
    "accuse": ("call", "report", "assert", "assert-frame", "institution", "negative"),
    "promo": ("scam-strong", "pull", "offer", "lure", "scam-weak"),
}
_TRAILING_COMMENT = re.compile(r"\s#")


def _strictness(file, sec):
    order = _STRICT_ORDER.get(file, ())
    base = base_sec(sec)
    idx = order.index(base) if base in order else len(order)
    if SECTIONS[file][sec] == RED:
        return (1 if is_pending(sec) else 0, idx)
    return (2, idx)


DEFAULT_THRESHOLDS = {
    "user_burst_note": (4, 10),          # 条数/秒
    "user_burst_alert": (6, 10),
    "user_burst_alert_long": (12, 60),
    "same_text_note": (3, 60),
    "same_text_alert": (5, 60),
    "brigade_note": (3, 20),             # 账号数/秒
    "brigade_alert": (5, 20),
    "brigade_min_words": 3,
    "brigade_jaccard": 0.8,
    "accumulate": (2, 600),              # 10 分钟内第 2 次
    "accumulate_red": False,             # 红条档开关：累计结果直接标红，默认关，交用户确认
    "caps_lead": (0.6, 3),               # 全大写字母占比/至少几个词
    "budget_ms": 50,                     # 滚动 1 秒累计超过这么多毫秒就进结构模式
    "budget_window_sec": 1.0,
    "budget_structure_sec": 5.0,
}
_PAIR_KEYS = frozenset(k for k, v in DEFAULT_THRESHOLDS.items() if isinstance(v, tuple))
_FLOAT_KEYS = frozenset(("brigade_jaccard", "budget_window_sec", "budget_structure_sec"))
_INT_KEYS = frozenset(("brigade_min_words", "budget_ms"))


MAX_WARNINGS = 50           # Rules.warnings 最多留这么多条，其余按 reason 聚合成一条 "more"


def _section_header(line):
    """`[` 开头的行一律是段头，绝不当词条：去空白、转小写、`_` 写成 `-`、全角括号也认。
    返回 ("header", 段名) | ("bad", None)（缺 `]`，如 `[medium` 或 `[medium]:`）| (None, None)。"""
    t = unicodedata.normalize("NFKC", line).strip()
    if not t.startswith("["):
        return None, None
    if not t.endswith("]"):
        return "bad", None
    name = re.sub(r"\s*:\s*", ":", t[1:-1].strip().lower().replace("_", "-"))
    return "header", name


def _has_alnum(text):
    return any(c.isalnum() for c in text)


def _phrase_re(phrases):
    pats = [r"\b" + re.escape(p).replace(r"\ ", r"\s+") + r"\b"
            for p in sorted(phrases, key=len, reverse=True)]
    return re.compile("|".join(pats)) if pats else None


def _entry_key(raw):
    """词条加载时统一折叠一遍（Estafa / estáfa 写哪种都等价）；re: 条目原样（前缀统一写成小写
    `re:`）；只有符号/表情的条目按字面处理（detector 的 normalize 会把它们抹成空）。返回 (种类, 键)。

    只有 Unicode 类别 So / Sk / Sm 的非 ASCII 符号（表情、手势）才按字面子串匹配；纯 ASCII 标点
    （`!!!` `?` `.` `-` `====`）或别的标点一律是 "empty"：它们会匹配几乎每一条弹幕。"""
    raw = raw.strip()
    if raw.lower().startswith("re:"):
        return "re", "re:" + raw[3:].strip()
    if not _has_alnum(raw):
        t = light(raw)
        if t and all(c == " " or (not c.isascii() and unicodedata.category(c) in ("So", "Sk", "Sm"))
                     for c in t):
            return "emoji", t
        return "empty", ""
    return "word", fold(raw)


def _regex_problem(key):
    """`re:` 条目的问题代码：写错了 -> invalid_regex；能匹配空串、或者对一组各不相同的普通弹幕都
    匹配（`re:`、`re:.*`、`re:x|`、`re:\\b`）-> matches_empty。没问题返回 None。"""
    try:
        rx = re.compile(key[3:].strip(), re.I)
    except re.error:
        return "invalid_regex"
    if rx.search("") or all(rx.search(probe) for probe in _REGEX_PROBES):
        return "matches_empty"
    return None


_REGEX_PROBES = ("a", "x y", "hola buenas tardes", "1 2", "zz")


def _parse_pair(value):
    left, sep, right = value.partition("/")
    if not sep:
        raise ValueError(value)
    a, b = left.strip(), right.strip()
    return (float(a) if "." in a else int(a)), (float(b) if "." in b else int(b))


def _parse_threshold(key, value):
    value = value.strip()
    if key in _PAIR_KEYS:
        a, b = _parse_pair(value)
        if not (math.isfinite(a) and math.isfinite(b)):
            raise ValueError(value)
        if key == "caps_lead":                       # 大写字母占比 (0, 1]，至少 1 个词
            if not (0 < a <= 1 and b >= 1):
                raise ValueError(value)
        elif not (a >= 1 and b > 0):                 # 条数 / 账号数 >= 1，窗口 > 0 秒
            raise ValueError(value)
        return a, b
    if key == "accumulate_red":
        low = value.lower()
        if low in ("on", "true", "yes", "1"):
            return True
        if low in ("off", "false", "no", "0"):
            return False
        raise ValueError(value)
    if key in _FLOAT_KEYS:
        v = float(value)
        if not math.isfinite(v) or v <= 0 or (key == "brigade_jaccard" and v > 1):
            raise ValueError(value)
        return v
    if key in _INT_KEYS:
        v = int(value)
        if v < 1:
            raise ValueError(value)
        return v
    raise KeyError(key)


class Rules:
    """一次载入的词表 + 阈值 + 本机叠加（只读，载入后不再变）。

    属性：
      warnings    词表体检：[{"src", "file", "section", "line", "entry", "reason"}]，只有代码；
      counts      {(文件, 段): 生效条数}（待确认的红条档段名带 `:pending`）；count 是红条档（含待确认的）
                  + 线索档的合计（「规则 N 条已生效」），pending_count 是其中待确认的条数；
      hash        生效词条 + 阈值的指纹（审计里「当时为什么标红」靠它还原）；
      thresholds  合并后的阈值。
    """

    def __init__(self):
        self.warnings = []
        self._overflow = {}
        self.counts = {}
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        self.entries = {}        # (文件, 段) -> [折叠后的词条]；re: 条目原样；表情原样
        self.originals = {}      # (文件, 段) -> {折叠后的词条: 文件里的原写法}
        self.matchers = {}       # (文件, 段) -> BannedTermDetector
        self.emoji = {}          # (文件, 段) -> 表情集合（只有符号/表情的词条）
        self.exempt = {}         # 类别 -> 豁免短语的正则（盖住就当没命中）
        self.target_words = ()
        self.frame_words = ()
        self.target_words_pending = ()
        self.frame_words_pending = ()
        self.ask_res = ()
        self.interaction = frozenset()
        self.positive_re = None
        self.positive_emoji = frozenset()
        self.competitors = None
        self.competitor_terms = ()
        self.local_exempt = {}     # 类别 -> 本机 exempt.txt 加进来的豁免短语（折叠后），只给指纹用
        self.mine = ()
        self.exempt_domains = ()
        self.hash = "?"
        self.count = 0
        self.pending_count = 0
        self.mask_words = {}     # 字数 -> [strong / assert 段里 >= 4 个字母的单词条]，给 p*ta 这类遮挡符还原用
        self.local_dir = None
        # 下面几个由 _finish 按词表编译；目标词、断言词是空的，对应的句式就是 None
        self._frames = None
        self._frames_pending = None
        self.target_vocative = None
        self.target_vocative_pending = None
        self.neg_re = None
        self.frame_re = None
        self.frame_re_pending = None

    # 体检警告：只给代码。最多留 MAX_WARNINGS 条：本机文件忘了写第一个 [段名] 会让几千行词条各出一条
    # 警告，写进会话开头的审计行就是几百 KB；多出来的按 reason 聚合成最后一条 {"reason": "more", ...}
    def _warn(self, src, file, section, line, entry, reason):
        if len(self.warnings) >= MAX_WARNINGS:
            self._overflow[reason] = self._overflow.get(reason, 0) + 1
            return
        self.warnings.append({"src": src, "file": file, "section": section, "line": line,
                              "entry": entry, "reason": reason})


def _read_sections(path, file, src, rules):
    """读一个词表文件 -> ({段名: [(行号, 原文)]}, [(行号, 要关掉的原文)])。

    复用 detector.read_terms：BOM、非 UTF-8、读不了都已处理，从不抛异常。`-词条` 只在本机
    叠加文件里有意义（关掉一条默认）；`[段名]` 切换后面条目的档位；注释只能整行写。"""
    info = read_terms(path)
    if info.read_error:
        rules._warn(src, file, None, None, None, "read_error")
    if info.decode_error:
        rules._warn(src, file, None, None, None, "decode_error")
    for number in info.skipped_lines:
        rules._warn(src, file, None, number, None, "skipped_line")
    sections, removals, current = OrderedDict(), [], None
    known = SECTIONS.get(file, {})
    for number, line in info.entries:
        kind, name = _section_header(line)
        if kind == "bad":
            # 缺 ] 的行不当词条，也不能让后面的词条悄悄落进前一段：后面的词条跳过
            rules._warn(src, file, None, number, line, "bad_section")
            current = "?bad"
            sections.setdefault(current, [])
            continue
        if kind == "header":
            current = name
            if is_pending(current) and current not in known:
                # `:pending` 只允许加在红条档的段名上；加在别的段上按普通段处理
                if base_sec(current) in known:
                    rules._warn(src, file, current, number, line, "pending_not_red_tier")
                    current = base_sec(current)
            if current not in known:
                rules._warn(src, file, current, number, line, "unknown_section")
            sections.setdefault(current, [])
            continue
        if src == "local" and line.startswith("-") and len(line) > 1:
            removals.append((number, line[1:].strip()))
            continue
        if current is None:
            rules._warn(src, file, None, number, line, "entry_before_section")
            continue
        sections[current].append((number, line))
    return sections, removals


def _merge_threshold_file(path, rules, src):
    info = read_terms(path)
    for number, line in info.entries:
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in DEFAULT_THRESHOLDS:
            rules._warn(src, "thresholds", None, number, line, "unknown_threshold")
            continue
        try:
            rules.thresholds[key] = _parse_threshold(key, value)
        except (ValueError, KeyError):
            rules._warn(src, "thresholds", None, number, line, "bad_threshold")


def _simple_lines(path):
    return [line for _, line in read_terms(path).entries]


def load_rules(defaults_dir=None, local_dir=None):
    """载入词表：`defaults_dir`（入库，程序直读、不复制）+ `local_dir`（本机叠加，可不存在）。

    本机文件 insult/accuse/promo.txt 同名同格式：普通行 = 追加到所在的段；`-词条` = 关掉
    一条默认（按折叠形式匹配，以后版本里同名词条仍保持关掉；一条也没关掉就报 removal_unmatched）。
    exempt.txt = 本机豁免短语，competitors.txt / mine.txt / exempt_domains.txt 每行一条，
    thresholds.txt 是 `key: value`。本机目录不存在、文件缺失都不报错；出厂词表目录或文件缺失
    会报 defaults_missing / missing_file（打包出错和「红条档全部待确认」要能分得出来）。
    agent 不碰 local/。"""
    rules = Rules()
    defaults_dir = Path(defaults_dir) if defaults_dir is not None else DEFAULTS_DIR
    local_path = Path(local_dir) if local_dir is not None else LOCAL_DIR
    rules.local_dir = local_path

    if not defaults_dir.is_dir():
        rules._warn("default", None, None, None, None, "defaults_missing")
    else:
        for name in LEX_FILES + ("thresholds",):
            if not (defaults_dir / (name + ".txt")).is_file():
                rules._warn("default", name, None, None, None, "missing_file")
    parsed = {}
    for file in LEX_FILES:
        sections, _ = _read_sections(defaults_dir / (file + ".txt"), file, "default", rules)
        parsed[file] = [(sec, number, raw, "default")
                        for sec, items in sections.items() for number, raw in items]
    removals = {}
    for file in LOCAL_LEX_FILES:
        sections, rem = _read_sections(local_path / (file + ".txt"), file, "local", rules)
        parsed[file] += [(sec, number, raw, "local")
                         for sec, items in sections.items() for number, raw in items]
        removals[file] = {}
        for number, raw in rem:
            removals[file].setdefault(_entry_key(raw)[1], (number, raw))
    _merge_threshold_file(defaults_dir / "thresholds.txt", rules, "default")
    if local_path.is_dir():
        _merge_threshold_file(local_path / "thresholds.txt", rules, "local")

    # 折叠、去掉被本机关掉的默认词条、同一词条多段取最严
    per_section = {}           # (文件, 段) -> OrderedDict(键 -> (种类, 原写法, 行号, 来源))
    removed = set()
    for file in LEX_FILES:
        tiers = SECTIONS[file]
        best = {}              # 键 -> (排名, 段)
        staged = []
        for sec, number, raw, src in parsed[file]:
            if sec not in tiers:
                continue
            kind, key = _entry_key(raw)
            if src == "default" and key in removals.get(file, ()):
                removed.add((file, key))
                continue
            if kind == "empty" or (kind == "word" and not key):
                rules._warn(src, file, sec, number, raw, "empty")
                continue
            if kind == "re":
                if (file, sec) not in REGEX_SECTIONS:
                    rules._warn(src, file, sec, number, raw, "regex_not_supported")
                    continue
                problem = _regex_problem(key)
                if problem:
                    rules._warn(src, file, sec, number, raw, problem)
                    continue
            if kind != "re" and _TRAILING_COMMENT.search(raw):
                # 行尾的 # 说明不算注释（和违禁词表同一条规则）：折叠会把 # 抹掉，词条悄悄变成
                # 「词 + 说明」，永远匹配不上——所以这里报出来
                rules._warn(src, file, sec, number, raw, "trailing_comment")
            staged.append((sec, number, raw, src, kind, key))
            if tiers[sec] != EXEMPT and kind != "re":
                r = _strictness(file, sec)
                if key not in best or r < best[key][0]:
                    best[key] = (r, sec)
        for sec, number, raw, src, kind, key in staged:
            if tiers[sec] != EXEMPT and kind != "re" and best[key][1] != sec:
                continue
            slot = per_section.setdefault((file, sec), OrderedDict())
            slot.setdefault(key, (kind, raw, number, src))
    for file, items in removals.items():
        for key, (number, raw) in items.items():
            if key and (file, key) not in removed:
                rules._warn("local", file, None, number, raw, "removal_unmatched")

    _build_sections(rules, per_section)
    # defaults/exempt_domains.txt 目前不存在：TikTok Shop 商品链接的写法要用户贴一条自家店里的
    # 商品链接核对后才能写进去（不凭记忆写）。机制先留好，有这个文件就读
    rules.exempt_domains = tuple(x.lower() for x in _simple_lines(defaults_dir / "exempt_domains.txt"))
    _load_local_extras(rules, local_path)
    _finish(rules)
    return rules


def _build_sections(rules, per_section):
    positive_words, interaction, neg_emoji = [], set(), set()
    for (file, sec), items in per_section.items():
        originals, plain, emoji_set, lines = {}, [], set(), []
        for key, (kind, raw, number, src) in items.items():
            originals[key] = raw
            if kind == "emoji":
                emoji_set.add(key)
                continue
            plain.append(key)
            lines.append((number, src))
        rules.originals[(file, sec)] = originals
        if emoji_set:
            rules.emoji[(file, sec)] = frozenset(emoji_set)
        alive = len(plain) + len(emoji_set)
        if (file, sec) in MATCHED:
            det = BannedTermDetector(plain, window_sec=1.0, cooldown_sec=0.0, min_fuzzy_len=99,
                                     line_numbers=[n for n, _ in lines])
            for w in det.load_warnings:
                # 词表体检（死正则、行尾 # 说明……）：只留代码；条目是词表数据
                src = items[w["entry"]][3] if w["entry"] in items else "default"
                rules._warn(src, file, sec, w["line"], w["entry"], w["reason"])
            alive = det.effective_count + len(emoji_set)
            if det.enabled:
                rules.matchers[(file, sec)] = det
        elif (file, sec) == ("interact", "words") or (file, sec) == ("interact", "places"):
            interaction.update(k for k in plain if not k.startswith("re:"))
        elif (file, sec) == ("interact", "positive"):
            positive_words += [k for k in plain if not k.startswith("re:")]
        elif (file, sec) == ("interact", "positive-emoji"):
            rules.positive_emoji = frozenset(emoji_set)
        elif (file, sec) == ("cues", "neg-emoji"):
            neg_emoji |= emoji_set
        elif sec == "exempt-phrases":
            rules.exempt[file] = _phrase_re([k for k in plain if not k.startswith("re:")])
        elif sec == "ask":
            ask = []
            for key in plain:
                try:
                    ask.append(re.compile(key[3:].strip() if key.startswith("re:") else key))
                except re.error:
                    rules._warn(items[key][3], file, sec, items[key][2], items[key][1],
                                "invalid_regex")
            rules.ask_res = tuple(ask)
        elif sec == "target":
            rules.target_words = tuple(k for k in plain if not k.startswith("re:"))
        elif sec == "target" + PENDING:
            rules.target_words_pending = tuple(k for k in plain if not k.startswith("re:"))
        elif sec == "assert-frame":
            rules.frame_words = tuple(k for k in plain if not k.startswith("re:"))
        elif sec == "assert-frame" + PENDING:
            rules.frame_words_pending = tuple(k for k in plain if not k.startswith("re:"))
        rules.entries[(file, sec)] = plain + sorted(emoji_set)
        rules.counts[(file, sec)] = alive
    rules.interaction = frozenset(interaction)
    rules.positive_re = _phrase_re(positive_words)
    if neg_emoji:
        rules.emoji[("cues", "neg-emoji")] = frozenset(neg_emoji)


def _load_local_extras(rules, local_path):
    if not local_path.is_dir():
        return
    # exempt.txt：本机豁免短语（行业词、主播口头禅）。可用 [insult] / [accuse] / [promo] 分段，
    # 不分段就三类都适用
    info = read_terms(local_path / "exempt.txt")
    scoped, current = {"insult": [], "accuse": [], "promo": []}, None
    for number, line in info.entries:
        kind, name = _section_header(line)
        if kind == "bad":
            rules._warn("local", "exempt", None, number, line, "bad_section")
            current = "?"
            continue
        if kind == "header":
            current = name
            if current not in scoped:
                rules._warn("local", "exempt", current, number, line, "unknown_section")
                current = "?"
            continue
        if current == "?":
            continue
        if line.lower().startswith("re:"):
            rules._warn("local", "exempt", current, number, line, "regex_not_supported")
            continue
        key = fold(line)
        if not key:
            rules._warn("local", "exempt", current, number, line, "empty")
            continue
        for cat in ((current,) if current else scoped):
            scoped[cat].append(key)
    rules.local_exempt = {cat: sorted(set(phrases)) for cat, phrases in scoped.items() if phrases}
    for cat, phrases in scoped.items():
        if not phrases:
            continue
        have = [p for p in rules.originals.get((cat, "exempt-phrases"), ())]
        rules.exempt[cat] = _phrase_re(have + phrases)
    comp = [fold(x) for x in _simple_lines(local_path / "competitors.txt")]
    comp = [c for c in comp if c]
    rules.competitor_terms = tuple(sorted(set(comp)))
    rules.competitors = _phrase_re(comp) if comp else None
    rules.mine = tuple(x.lower() for x in _simple_lines(local_path / "mine.txt"))
    rules.exempt_domains += tuple(x.lower()
                                  for x in _simple_lines(local_path / "exempt_domains.txt"))


def _finish(rules):
    rules.count = sum(n for (file, sec), n in rules.counts.items()
                      if SECTIONS[file][sec] in (RED, CLUE) and file != "cues")
    rules.pending_count = sum(n for (file, sec), n in rules.counts.items() if is_pending(sec))
    h = hashlib.sha256()
    for key in sorted(rules.entries):
        for entry in sorted(rules.entries[key]):
            h.update(("%s/%s:%s\n" % (key[0], key[1], entry)).encode("utf-8"))
    for key in sorted(rules.thresholds):
        h.update(("%s=%r\n" % (key, rules.thresholds[key])).encode("utf-8"))
    # 本机叠加里同样会改变判定的几份：豁免短语、同行名单、自家号码/账号/域名、豁免域名。不放进指纹
    # 的话，两场用了不同本机文件的会话会记下同一个 rules_hash，审计就还原不出「当时为什么标红」。
    # trusted 名单不进（它按主播在开场时读，不属于词表）
    for cat in sorted(rules.local_exempt):
        for phrase in rules.local_exempt[cat]:
            h.update(("exempt-local/%s:%s\n" % (cat, phrase)).encode("utf-8"))
    for name, items in (("competitors", rules.competitor_terms), ("mine", rules.mine),
                        ("exempt_domains", rules.exempt_domains)):
        for item in sorted(set(items)):
            h.update(("%s:%s\n" % (name, item)).encode("utf-8"))
    rules.hash = h.hexdigest()[:12]
    if rules._overflow:
        rules.warnings.append({"reason": "more", "n": sum(rules._overflow.values()),
                               "by_reason": dict(sorted(rules._overflow.items()))})
    words = set(_MASK_EXTRA_WORDS)
    for key in (("insult", "strong"), ("insult", "strong" + PENDING),
                ("accuse", "assert"), ("accuse", "assert" + PENDING)):
        words.update(x for x in rules.entries.get(key, ()) if x.isalpha() and len(x) >= 4)
    rules.mask_words = {}
    for word in sorted(words):
        rules.mask_words.setdefault(len(word), []).append(word)
    # 目标词/断言框架词的正则：词表是空的就没有这些句式。已确认和待确认的各编一份
    rules._frames, rules.target_vocative = _target_frames(rules.target_words)
    rules._frames_pending, rules.target_vocative_pending = _target_frames(rules.target_words_pending)
    singles = [x for sec in ("assert", "assert" + PENDING)
               for x in rules.entries.get(("accuse", sec), ())
               if " " not in x and not x.startswith("re:")]
    allw = "|".join(re.escape(x) for x in singles + list(rules.frame_words)
                    + list(rules.frame_words_pending))
    # 否认式整体剪掉，不计分：断言词前面带 no/nunca/ni/nada/sin/not（中间可隔 es/hay/haya……）、
    # 「cómo va a haber estafa」这种反问式否认；担心、求证、替主播辩护的说法同理：espero que no sea、
    # no creo que sea、ojala no sea、dudo que sea、tenia miedo de que fuera、pense que era……pero、
    # dicen que es ……pero；英语 not a scam / isnt a scam / never a scam。否认和确认状态无关
    rules.neg_re = None
    if allw:
        neg = r"(?:no|nunca|ni|jamas|tampoco|nada|sin|not|isnt|wasnt|arent|werent|never|aint|dudo que|dudo|descarto)"
        cop = (r"(?:es|son|fue|fueron|soy|somos|eres|seria|hay|haya|existe|is|are|was|va a haber|habra|"
               r"tiene|tienen|hablando con|hablo de|sea|sean|fuera|fuese|parece|parezca|creo que sea|"
               r"creo que sean|creo que es|creo que son|quiero que sea|vaya a ser|vaya a haber|me parece)")
        art = r"(?:una?|an?|the|ningun[a]?|nada de|de)"
        refl = r"(?:me|te|nos|le|les|se|lo|la|los)"
        rules.neg_re = re.compile(
            r"\b" + neg + r"\s+(?:" + refl + r"\s+)?(?:" + cop + r"\s+)*(?:" + art + r"\s+)?(?:" + allw + r")\b|"
            r"\bcomo\s+(?:va a|puede|podria)\s+(?:haber|ser)\s+(?:una?\s+)?(?:" + allw + r")\b|"
            r"\b(?:pense|creia|crei|imagine|suponia|temia|temi|tenia miedo de|tengo miedo de|miedo de|"
            r"miedo a)\s+que\s+(?:(?:es|era|fuera|fuese|seria|sea|sean|son|eran|fueran)\s+)?"
            r"(?:una?\s+)?(?:" + allw + r")\b|"
            r"\b(?:dicen|decian|dijeron|dice|escuche|lei|oi)\s+que\s+(?:(?:es|era|son|eran|fuera|seria)\s+)?"
            r"(?:una?\s+)?(?:" + allw + r")\b(?=.*\b(?:pero|aunque|sin embargo)\b)|"
            r"\b(?:cuidado|ojo|aguas|alerta)\s+con\s+(?:las?|los|el)\s+(?:" + allw + r")\s+(?:en|por|de)\s+"
            r"(?:tiktok|redes|internet|linea|ahi|la red|facebook|instagram|whatsapp)\b|"
            r"\b(?:posible|presunto|presunta|alerta de|alerta por|proteccion contra|seguro contra|"
            r"prevencion de)\s+(?:" + allw + r")\b|"
            r"\b(?:ladrones?|ladrona|thieves|thief)\s+(?:se llevaron|se llevo|se robaron|robaron|took|stole)\s+"
            r"(?:mi|my|el|la|los|las|de|del)?\s*(?:paquete|package|pedido|envio|caja|box|entrega|delivery)\b")
    # falso/fake/chafa 这类词只在「说某物是假的」的句式里才算断言
    rules.frame_re = _frame_regex(rules.frame_words)
    rules.frame_re_pending = _frame_regex(rules.frame_words_pending)


_DEGREE = r"(?:(?:muy|tan|bien|re|una|un|so|really|very|too|such a|a|an)\s+)*"
# 句式前面有否定（no / nunca / jamas / ni / tampoco，可隔 tu|te|me，紧贴句式词或隔一个词）：
# 「no eres fea」「ya no estas gorda」是安慰，不是骂
_NEG_BEFORE = re.compile(r"\b(?:no|nunca|jamas|ni|tampoco)\s+(?:(?:tu|te|me)\s+)?(?:\w+\s+)?$")
# 条件句里的泛指「你」：「si estas operada puedes usar la faja」「if you are fat this works」
_COND_BEFORE = re.compile(r"\b(?:si|cuando|aunque|if|when)\s+(?:\w+\s+)?$")


def _target_frames(words):
    """目标词的第二人称句式。「estas」折叠之后分不出「你是」和「这些」，所以裸 estas 只认单数的
    目标词（estas feas manchas 的 feas 是复数，不算）；英语不认裸 ur（ur fat burner），要 you are /
    u r / youre，或者 ur 后面跟 so / such / very / really / too。"""
    w = "|".join(re.escape(x) for x in words)
    if not w:
        return None, None
    wset = set(words)
    singular = "|".join(re.escape(x) for x in words if not (x.endswith("s") and x[:-1] in wset))
    frames = [
        re.compile(r"\b(?:eres|te ves|pareces|fuiste|you are|youre|you re|u r|u are|"
                   r"ur(?=\s+(?:so|such|very|really|too)\b))\s+" + _DEGREE + r"(" + w + r")\b"),
        re.compile(r"\bque\s+(" + w + r")\s+(?:eres|estas|te ves)\b"),
        re.compile(r"\bpinche\s+(" + w + r")\b"),
        re.compile(r"\b(" + w + r")\s+(?:de mierda|asquerosa|inutil)\b"),
    ]
    if singular:
        frames.insert(1, re.compile(r"\bestas\s+" + _DEGREE + r"(" + singular + r")\b"))
    return frames, re.compile(r"^(" + w + r")$")


def _first_target(frames, text):
    """第一个没被否定、也不在条件句里的句式命中；没有返回 None。"""
    for rx in frames:
        for m in rx.finditer(text):
            before = text[:m.start()]
            if _NEG_BEFORE.search(before) or _COND_BEFORE.search(before):
                continue
            return m
    return None


def _frame_regex(words):
    fw = "|".join(re.escape(x) for x in words)
    if not fw:
        return None
    return re.compile(
        r"\b(?:es|son|esta|estan|esto es|eso es|todo es|todo esta|producto|productos|puro|pura|"
        r"marca|cuenta|live|medicina|vitaminas?|suplemento|gotas|vende|venden|un|its|it is|"
        r"this is|that is|thats|is|are|so|totally|completely)\s+"
        r"(?:(?:muy|re|100|cien|completamente|totalmente|todo)\s+)*(" + fw + r")\b")


def load_trusted(streamer, local_dir=None):
    """本机文件 local/trusted/<主播>.txt：一行一个 uid，该主播本人、房管、助理的账号。
    主播名不合规（空、带路径符、带 NUL）或文件读不了，一律返回空集合，不抛。"""
    base = Path(local_dir) if local_dir is not None else LOCAL_DIR
    name = str(streamer or "").strip().lower()
    if not name or "/" in name or "\\" in name or "\x00" in name or name.startswith("."):
        return frozenset()
    try:
        return frozenset(x.lower() for x in _simple_lines(base / "trusted" / (name + ".txt")))
    except (OSError, ValueError):
        return frozenset()


# ------------------------------------------------------------------------------------------
# 刷屏引擎（有状态；必须传真实 ts）
# ------------------------------------------------------------------------------------------

BRIGADE_GAP = 20.0          # 多账号事件：最后一条相似弹幕之后，再过「窗口 + 这么多秒」没有新的才算结束
BRIGADE_CLUSTERS = 50       # 同时存在的事件最多这么多个
BRIGADE_USERS = 2000        # 一个事件最多记这么多个账号
BRIGADE_KEYS = 8            # 一个事件最多记这么多个不同的句子（近似句要和其中任一条比）
BRIGADE_TOKENS = 64         # 近似句比较只取前 64 个词：1000 字的长弹幕不能把每次比较拖慢


def _check_ts(ts):
    """时间戳必须是有限的 int / float（bool 不算）：在改任何状态之前检查，免得一个坏值留在用户的
    历史里，让这个用户接下来的每条判定都抛异常。"""
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise TypeError("ts must be a finite int or float, got %s" % type(ts).__name__)
    if not math.isfinite(ts):
        raise ValueError("ts must be finite, got %r" % (ts,))


class FloodEngine:
    """C1 单人高频、C3 同人同句、C2 多账号同句。纯内存、内存有界。

    feed() 返回这一条弹幕触发的刷屏事件：
      {"kind": "user_burst"|"same_text"|"brigade", "lvl": "alert"|"amber"|"note",
       "n": 条数或账号数, "window": 秒, "ev": 多账号事件的稳定 id（其余为 None）}
    多账号事件另有 first_ts、last_ts 和 accounts（前 20 个账号），C2 在 v1 里只到琥珀（一个事件、
    账号数原地增长）：主播喊口令时观众会齐刷同一句。

    ts 要传调用方的时钟读数（PR-3 传 time.time()）：窗口计数只算 ts - window <= t <= ts 的条目，
    系统时钟被往回拨时，比 ts 更晚的旧条目不会被算进去。"""

    def __init__(self, thresholds=None):
        self.th = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            self.th.update(thresholds)
        self.users = OrderedDict()        # 用户 -> deque[(ts, 句子键)]
        self.recent = deque(maxlen=RECENT_MAX)    # (ts, 用户, key, 词集合)
        self.clusters = OrderedDict()     # 多账号事件 id -> 事件记录
        self._ev_seq = 0

    def feed(self, user, key, toks, ts, question=False, mute_all=False, mute_nonburst=False,
             exact_key=None):
        """key 是去掉数字的句子键（多账号同句用），exact_key 是不去数字的（同人同句用，不传就是 key）。

        mute_all（互动词、正向表情整条豁免）的弹幕直接返回：它不进这个用户的历史，免得「yo yo yo
        yo yo yo」之后的第一句正常提问被当成单人高频。"""
        _check_ts(ts)
        th = self.th
        if mute_all:
            return []
        exact = key if exact_key is None else exact_key
        dq = self.users.get(user)
        if dq is None:
            dq = self.users[user] = deque(maxlen=USER_HISTORY_MAX)
            if len(self.users) > USER_TABLE:
                for old in list(self.users)[:USER_TABLE_DROP]:
                    self.users.pop(old, None)
        else:
            self.users.move_to_end(user)
        while dq and dq[0][0] < ts - USER_HISTORY:
            dq.popleft()
        dq.append((ts, exact))
        out = []

        def count(window):
            return sum(1 for t, _ in dq if ts - window <= t <= ts)

        n_alert, w_alert = th["user_burst_alert"]
        n_long, w_long = th["user_burst_alert_long"]
        n_note, w_note = th["user_burst_note"]
        c_alert, c_long, c_note = count(w_alert), count(w_long), count(w_note)
        if c_alert >= n_alert:
            out.append({"kind": "user_burst", "lvl": "alert", "n": c_alert,
                        "window": w_alert, "ev": None})
        elif c_long >= n_long:
            out.append({"kind": "user_burst", "lvl": "alert", "n": c_long,
                        "window": w_long, "ev": None})
        elif c_note >= n_note:
            out.append({"kind": "user_burst", "lvl": "note", "n": c_note,
                        "window": w_note, "ev": None})
        if mute_nonburst:
            return out

        n_alert, w_alert = th["same_text_alert"]
        n_note, w_note = th["same_text_note"]
        same_alert = sum(1 for t, k in dq if ts - w_alert <= t <= ts and k == exact)
        same_note = sum(1 for t, k in dq if ts - w_note <= t <= ts and k == exact)
        if same_alert >= n_alert:
            # 问句（被冷落的顾客每隔十几秒问一次价）最多到 note
            out.append({"kind": "same_text", "lvl": "note" if question else "alert",
                        "n": same_alert, "window": w_alert, "ev": None})
        elif same_note >= n_note:
            out.append({"kind": "same_text", "lvl": "note", "n": same_note,
                        "window": w_note, "ev": None})

        if key and toks:
            ev = self._brigade(user, key, toks, ts)
            if ev:
                out.append(ev)
        return out

    def _brigade(self, user, key, toks, ts):
        """多账号同句。事件 id 在第一次到 brigade_note 时分配，之后相似的弹幕并进同一个事件
        （账号数原地增长、id 不变）；最后一条相似弹幕之后窗口 + BRIGADE_GAP 秒没有新的，事件结束。
        id 不是句子的哈希：同一句口令隔几分钟再刷一波，是新事件、新 id。"""
        th = self.th
        n_note, window = th["brigade_note"]
        n_alert, _ = th["brigade_alert"]
        self.recent.append((ts, user, key, toks))
        self._expire_clusters(ts, window)
        cl = self._find_cluster(key, toks)
        if cl is None:
            found, first = OrderedDict(), ts
            for t, u, k, tk in islice(reversed(self.recent), RECENT_SCAN):
                if t < ts - window:
                    break
                if t > ts:
                    continue                  # 系统时钟被往回拨过：比现在还晚的旧条目不算
                if self._similar(key, toks, k, tk):
                    found.setdefault(u, None)
                    first = min(first, t)
            if len(found) < n_note:
                return None
            users = OrderedDict.fromkeys(reversed(list(found)))      # 先到的账号排前面
            self._ev_seq += 1
            ev = "%s%d" % (hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:6], self._ev_seq)
            cl = self.clusters[ev] = {"ev": ev, "first_ts": first, "last_ts": ts, "users": users,
                                      "keys": [(key, toks)]}
            while len(self.clusters) > BRIGADE_CLUSTERS:
                self.clusters.popitem(last=False)
        else:
            cl["last_ts"] = ts
            if len(cl["users"]) < BRIGADE_USERS:
                cl["users"].setdefault(user, None)
            if len(cl["keys"]) < BRIGADE_KEYS and all(key != k for k, _ in cl["keys"]):
                cl["keys"].append((key, toks))
        n = len(cl["users"])
        return {"kind": "brigade", "lvl": "amber" if n >= n_alert else "note",
                "n": n, "window": window, "ev": cl["ev"], "first_ts": cl["first_ts"],
                "last_ts": cl["last_ts"], "accounts": list(islice(cl["users"], 20))}

    def _expire_clusters(self, ts, window):
        stale = [ev for ev, cl in self.clusters.items()
                 if abs(ts - cl["last_ts"]) > window + BRIGADE_GAP]
        for ev in stale:
            del self.clusters[ev]

    def _find_cluster(self, key, toks):
        for cl in self.clusters.values():
            for k, tk in cl["keys"]:
                if self._similar(key, toks, k, tk):
                    return cl
        return None

    def _similar(self, key, toks, key2, toks2):
        if key == key2:
            return True
        th = self.th
        la, lb = len(toks), len(toks2)
        if la < th["brigade_min_words"] or lb < th["brigade_min_words"]:
            return False
        jaccard = th["brigade_jaccard"]
        # Jaccard 的上界是 min / max：词数相差太多的直接跳过，省掉集合运算
        if min(la, lb) < jaccard * max(la, lb):
            return False
        inter = len(toks & toks2)
        return inter / float(la + lb - inter) >= jaccard


class CostBudget:
    """规则判定的预算：滚动 1 秒累计超过 budget_ms 就进「结构模式」一段时间（judge 的
    structure_only=True：只跑链接、电话、刷屏）。时钟由调用方传，所以可以注入假时钟测试。

    now 要传**单调时钟**（time.monotonic()）：挂钟会被对时、合盖休眠改写，往回拨之后「花费」和
    「结构模式到期时间」都会对不上。"""

    def __init__(self, thresholds=None):
        th = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            th.update(thresholds)
        self.limit_ms = th["budget_ms"]
        self.window = th["budget_window_sec"]
        self.hold = th["budget_structure_sec"]
        self.spent = deque()
        self.since = 0.0
        self.until = 0.0
        self.trips = 0

    def structure_only(self, now):
        # 时钟被往回拨到进入结构模式之前，就不算还在结构模式里（否则会一直降级到时钟追回来）
        return self.since <= now < self.until

    def charge(self, now, ms):
        self.spent.append((now, ms))
        while self.spent and (self.spent[0][0] < now - self.window or self.spent[0][0] > now):
            self.spent.popleft()
        if sum(m for _, m in self.spent) > self.limit_ms and not self.since <= now < self.until:
            self.since, self.until = now, now + self.hold
            self.trips += 1


# ------------------------------------------------------------------------------------------
# 单条判定
# ------------------------------------------------------------------------------------------

LVL_RANK = {"alert": 4, "amber": 3, "note": 2, "lead": 1, None: 0}
TIER_RANK = {RED: 3, STRUCT: 2, CLUE: 1, None: 0}
# 同级同档时谁当主类别：人身威胁排最前
CAT_ORDER = ("threat", "insult", "sexual", "accuse_call", "accuse_report", "accuse_claim",
             "promo_scam", "promo_pull", "promo_link", "promo_contact", "flood_repeat",
             "flood_user", "flood_brigade")
UNCERTAIN_TAGS = frozenset(("maybe_question", "grievance", "quote", "vocative", "self",
                            "accumulated", "rhetorical_question", "question_exempt", "pending"))


# 自动加入关注名单还要账号键稳定：只有 uid_src 是这三种的才行（item["uid_src"]，见 spec §3.1）。
# 只有 handle 或昵称的人刷屏照常计数，但不自动加入
UID_OK_SRC = ("id", "id_str", "sec_uid")


def uid_ok(uid_src):
    return uid_src in UID_OK_SRC


def auto_markable(result, uid_src=None):
    """这条判定能不能自动加入关注名单：必须是红条档词条的明确命中（tier == "red"），
    没有任何不确定标记（含还没确认的词条 pending）。链接、电话组合、刷屏、琥珀、note、模型意见都不加人。

    传了 uid_src 就把账号键稳定也算进来（spec I14）：uid_src 不在 UID_OK_SRC 里（只有 handle 或昵称）
    就返回 False。不传时只看判定本身——调用方自己负责 uid_ok。"""
    if uid_src is not None and not uid_ok(uid_src):
        return False
    return bool(result and result["lvl"] == "alert" and result["tier"] == RED
                and not (set(result["tags"]) & UNCERTAIN_TAGS))


def _finding(cat, sub, tier, lvl, why, amb=None, tags=(), route=None, pending=False):
    f = {"cat": cat, "sub": sub, "tier": tier, "lvl": lvl, "amb": amb,
         "why": list(why), "tags": list(tags), "route": route}
    return _as_pending(f) if pending else f


def _as_pending(f):
    """待确认的红条档词条命中：红条和琥珀一律出琥珀（amb == "pending"，route == "confirm"），
    note 仍是 note；tier 仍是 "red"（来源是红条档词表），tags 带上 "pending"。"""
    f = dict(f)
    f["tags"] = list(f["tags"])
    if "pending" not in f["tags"]:
        f["tags"].append("pending")
    if f["lvl"] in ("alert", "amber"):
        f["lvl"], f["amb"], f["route"] = "amber", "pending", "confirm"
    return f


def _result(findings=(), extra_tags=()):
    tags = list(extra_tags)
    if not findings:
        return {"lvl": None, "amb": None, "cats": [], "sub": None, "why": [], "tier": None,
                "tags": tags, "route": None}
    best = max(findings, key=lambda f: (
        LVL_RANK[f["lvl"]], TIER_RANK[f["tier"]],
        -(CAT_ORDER.index(f["cat"]) if f["cat"] in CAT_ORDER else len(CAT_ORDER))))
    same = [f for f in findings if f["lvl"] == best["lvl"]]
    same.sort(key=lambda f: (-TIER_RANK[f["tier"]],
                             CAT_ORDER.index(f["cat"]) if f["cat"] in CAT_ORDER else len(CAT_ORDER)))
    cats = []
    for f in same:
        if f["cat"] and f["cat"] not in cats:
            cats.append(f["cat"])
    why = []
    for f in same:
        for item in f["why"]:
            if item not in why:
                why.append(item)
    for f in same:
        for tag in f["tags"]:
            if tag not in tags:
                tags.append(tag)
    return {"lvl": best["lvl"], "amb": best["amb"], "cats": cats, "sub": best["sub"],
            "why": why[:6], "tier": best["tier"], "tags": tags, "route": best["route"]}


_PUTA_KEEP = re.compile(r"(?:hij[oa]s?\s+de(?:\s+(?:la|tu|su))?|\btu|\bsu|\ba\s+tu)\s*$")
# 子句边界：标点后面跟空白或结尾（`p.u.t.a` 里的点后面是字母，不算边界）。否认式只在一个子句里算：
# 「no, es una estafa」的 no 是应答，不是在否认后面那句
_CLAUSE_SPLIT = re.compile(r"[,.;:!?\u00bf\u00a1]+(?=\s|$)")
# 词内遮挡符：一个遮挡符代表一个任意字母（p*ta、est*fa、p*nd*ja、f*ck）
_MASK_CHARS = frozenset("*#%-_.@$+")
_MASK_HINT = re.compile(r"[a-z\u00f1][*#%\-_.@$+]+[a-z\u00f1*#%]|[a-z\u00f1][*#%]|[*#%][a-z\u00f1]")
_MASK_EXTRA_WORDS = ("fuck",)
_MASK_PUNCT = "!?\u00a1\u00bf,;:()\"'[]{}<>\u2026"


def _as_text(text):
    if text is None:
        return ""
    return text if isinstance(text, str) else str(text)


def _hashable(x):
    try:
        hash(x)
    except TypeError:
        return str(x)
    return x


def _unmask_token(tok, words):
    core = tok.strip(_MASK_PUNCT).strip(".-_")
    if len(core) < 4 or not all(c.isalpha() or c in _MASK_CHARS for c in core):
        return tok
    masks = sum(c in _MASK_CHARS for c in core)
    if not masks or len(core) - masks < 2:
        return tok
    cands = words.get(len(core))
    if not cands:
        return tok
    rx = re.compile("".join("." if c in _MASK_CHARS else re.escape(c) for c in core) + "$")
    found = [w for w in cands if rx.match(w)]
    return " ".join(found) if found else tok


class CommentGuard:
    """单条判定 + 刷屏/累计状态。

    judge(text, user=..., ts=...) 永远返回 §4.6 形状的字典；内部是纯 CPU。参数类型不对（ts 不是有限
    的数）在改任何状态之前就抛；text 不是 str 就转成 str，user / cid 不可哈希就转成 str，调用方
    （PR-3 的 observe）仍要自己 try/except。`streamer` 是本场主播（@主播 不算引流，指向本场主播
    自己的 tiktok.com 链接豁免）；`trusted` 是该主播本人/房管/助理的 uid。"""

    def __init__(self, rules, streamer="", trusted=()):
        self.rules = rules
        self.streamer = (streamer or "").lower()
        self.trusted = frozenset(str(t).lower() for t in trusted)
        self.flood = FloodEngine(rules.thresholds)
        self.last_flood = []                  # 最近一次 judge 触发的刷屏事件（给事件聚合用）
        self._seen = OrderedDict()
        self._acc = OrderedDict()             # 用户 -> deque[ts]：insult 线索档命中的累计

    # ---- 词表命中（工具也用它：碰撞计数要和真判定走同一条路径）
    def _scan(self, key, folded):
        det = self.rules.matchers.get(key)
        if det is None:
            return []
        det.reset_state()
        hits = det.scan(folded, ts=0.0)
        if key not in VARIANT_OK:
            hits = [h for h in hits if h["tier"] == "exact"]
        hits.sort(key=lambda h: h["tier"] != "exact")        # 原样命中排前面，why 里先看到它
        return [h["term"] for h in hits]

    def lex_hits(self, text):
        """这条文本在每个词表段上的有效命中：{(文件, 段): [词条]}，再加几个标志。

        「有效」指经过和真判定一样的预处理：豁免短语先盖住，断言词的否认式整体剪掉，目标词
        只在「指向对方」的句式里算。待确认的红条档命中在 `段名:pending` 的键下。"""
        light_text, src, full_fold = self._prep(_as_text(text))
        return self._lex_hits(light_text, clip_words(full_fold), src)

    def _prep(self, text):
        """(light 文本, 去掉遮挡符之后的 light 文本, fold 文本)。链接、电话、问号看 light 文本；
        词表匹配看 fold 文本；p*ta 这类词内遮挡符只在词表匹配那一路还原。"""
        light_text = light(text)
        src = self._unmask(light_text)
        return light_text, src, fold(src)

    def _unmask(self, light_text):
        words = self.rules.mask_words
        if not words or not _MASK_HINT.search(light_text):
            return light_text
        return " ".join(_unmask_token(tok, words) for tok in light_text.split(" "))

    def _lex_hits(self, light_text, folded, fold_src=None):
        rules, out = self.rules, {}
        flags = {"target_vocative": False, "target_vocative_pending": False, "ask": False}

        def strip(file, text):
            rx = rules.exempt.get(file)
            if not rx:
                return text
            if file != "insult":
                return rx.sub(" ", text)
            # 「puta madre」是感叹，但「hijo de la / tu puta madre」「tu puta madre」「vete a tu puta
            # madre」是骂人：这些情形前面的词不能让它被整个盖住
            return rx.sub(lambda m: m.group(0) if "puta madre" in m.group(0)
                          and _PUTA_KEEP.search(text[:m.start()]) else " ", text)

        def scan(file, secs, text):
            for sec in secs:
                terms = self._scan((file, sec), text)
                if terms:
                    out[(file, sec)] = terms

        fi = strip("insult", folded)
        scan("insult", ("strong", "threat", "sexual", "strong" + PENDING, "threat" + PENDING,
                        "sexual" + PENDING, "medium", "watch", "sexual-soft"), fi)
        for sec, frames, voc, flag in (
                ("target", rules._frames, rules.target_vocative, "target_vocative"),
                ("target" + PENDING, rules._frames_pending, rules.target_vocative_pending,
                 "target_vocative_pending")):
            if not frames:
                continue
            m = _first_target(frames, fi)
            if m:
                out[("insult", sec)] = [m.group(1)]
                continue
            m = voc.match(fi.strip())
            if m:
                out[("insult", sec)] = [m.group(1)]
                flags[flag] = True

        fa = strip("accuse", folded)
        if rules.neg_re is not None:
            src = fold_src if fold_src is not None else light_text
            if _CLAUSE_SPLIT.search(src):
                # 否认式在保留标点的子句里判：「no, es una estafa」的 no 不否认后面那句
                parts = [rules.neg_re.sub(" ", strip("accuse", fold(c)))
                         for c in _CLAUSE_SPLIT.split(src) if c.strip()]
                fa = clip_words(" ".join(_join_letter_runs(" ".join(parts).split())))
            else:
                fa = rules.neg_re.sub(" ", fa)
        scan("accuse", ("assert", "call", "report", "assert" + PENDING, "call" + PENDING,
                        "report" + PENDING, "institution", "negative"), fa)
        for sec, rx in (("assert-frame", rules.frame_re), ("assert-frame" + PENDING, rules.frame_re_pending)):
            if rx is not None:
                m = rx.search(fa)
                if m:
                    out[("accuse", sec)] = [m.group(1)]

        fp = strip("promo", folded)
        scan("promo", ("pull", "scam-strong", "pull" + PENDING, "scam-strong" + PENDING, "offer",
                       "lure", "scam-weak"), fp)
        flags["ask"] = any(rx.search(folded) for rx in rules.ask_res)

        terms = self._scan(("cues", "irony"), folded)
        if terms:
            out[("cues", "irony")] = terms
        for key, chars in rules.emoji.items():
            if key[0] == "interact":
                continue
            found = sorted(c for c in chars if c in light_text)
            if found:
                out.setdefault(key, []).extend(found)
        out["_flags"] = flags
        return out

    # ---- 辅助
    def _exempt_user(self, user, role, handle=None, keys=()):
        if role in ROLES_EXEMPT:
            return True
        # 主播自己的账号：比 handle 字段（不分大小写）。不按昵称比——任何人都能把昵称改成主播名
        # 来躲检测
        if self.streamer and isinstance(handle, str) and handle.strip().lstrip("@").lower() == self.streamer:
            return True
        if self.trusted:
            if isinstance(keys, str):
                keys = (keys,)
            for key in (user, *(keys or ())):
                if key is not None and str(key).lower() in self.trusted:
                    return True
        return False

    def _remember_id(self, cid):
        if cid in self._seen:
            return True
        self._seen[cid] = True
        while len(self._seen) > SEEN_IDS:
            self._seen.popitem(last=False)
        return False

    def _link_exempt(self, url):
        """本场主播自己的页面，或本机 mine.txt / exempt_domains.txt 里的域名。按 host 比，不按子串比：
        外站链接里恰好夹着 `tiktok.com/@主播`、或域名只是长得像（tienda-marcaejemplo.com）都不豁免。"""
        host, path = _split_url(url)
        if self.streamer and host in _OWN_HOSTS and re.match(
                r"/@" + re.escape(self.streamer) + r"(?![a-z0-9_.])", path):
            return True
        rules = self.rules
        return any(_host_matches(host, path, entry) for entry in rules.mine + rules.exempt_domains)

    def _accumulate(self, user, ts):
        """insult 线索档命中的累计：返回这是不是 10 分钟内的第 N 次（N = accumulate 的条数）。"""
        need, window = self.rules.thresholds["accumulate"]
        dq = self._acc.get(user)
        if dq is None:
            dq = self._acc[user] = deque(maxlen=20)
            if len(self._acc) > USER_TABLE:
                for old in list(self._acc)[:USER_TABLE_DROP]:
                    self._acc.pop(old, None)
        else:
            self._acc.move_to_end(user)
        while dq and dq[0] < ts - window:
            dq.popleft()
        dq.append(ts)
        return len(dq) >= need

    # ---- 主入口
    def judge(self, text, *, user=None, ts, cid=None, role=None, mention=False,
              structure_only=False, handle=None, keys=()):
        """一条弹幕的判定。

        user：稳定的用户键（uid；只有 handle/昵称时由调用方传对应的键）；没有就不数刷屏，
        免得把所有匿名弹幕当成同一个人。ts：真实时间戳（必填关键字——不传会让滑动窗口永不过期；
        必须是有限的 int / float，否则在改任何状态之前抛 TypeError / ValueError）。
        cid：弹幕 id，守卫自己按 id 去重（TikTokLive 连上时会把历史评论推两遍）。
        role：anchor/mod 整条豁免。structure_only：只跑链接、电话、刷屏（超预算时的降级模式）。
        handle：这个人的 handle 字段（PR-1 的 item["handle"]），等于本场主播的 handle 就整条豁免；
        keys：这个人的其它账号键（uid、sec、handle……），trusted 名单命中 user 或其中任一都整条豁免。
        """
        _check_ts(ts)
        self.last_flood = []
        user, cid = _hashable(user), _hashable(cid)
        if cid is not None and self._remember_id(cid):
            return _result(extra_tags=["dup_id"])
        if self._exempt_user(user, role, handle, keys):
            return _result(extra_tags=["trusted"])
        text = _as_text(text)[:MAX_CHARS]
        light_text, src, full_fold = self._prep(text)
        if not light_text:
            return _result()
        folded = clip_words(full_fold)
        # 问号只认链接以外的：链接的查询串（evil.com/?x=1）里的 ? 不是问句，不然每条链接后面加个
        # ?x=1 就从红条降成琥珀了
        q_text = _URL_RE.sub(" ", light_text) if "?" in light_text else light_text
        plain_q = is_question(q_text, full_fold, tail=False)          # 链接的问句豁免只认这一种
        question = plain_q or is_question(q_text, full_fold) or is_choice_question(full_fold)
        weak_q = not question and weak_question_start(q_text, full_fold)
        quote = bool(_QUOTE.search(folded))
        findings = []
        hits = {} if structure_only else self._lex_hits(light_text, folded, src)
        flags = hits.pop("_flags", {})

        if not structure_only:
            findings += self._insult(hits, flags, folded, quote)
            findings += self._accuse(hits, folded, question, quote, weak_q)
        findings += self._promo(hits, flags, light_text, folded, plain_q, structure_only)
        if not structure_only:
            lead = self._lead(hits, text, mention)
            if lead:
                findings.append(lead)
        if user:
            findings += self._flood(user, light_text, full_fold, ts, question)
        if not structure_only:
            self._accumulate_step(findings, user, ts)
        result = _result(findings)
        if result["route"] and sum(c.isalpha() for c in light_text) < 2:
            result["route"] = None            # 字母少于 2 个的不送审
        return result

    # ---- A 辱骂
    def _insult(self, hits, flags, folded, quote):
        out = []

        def why(key):
            return ["insult/%s:%s" % (base_sec(key), t) for t in hits[("insult", key)][:3]]

        selfy = bool(_SELF_STRONG.search(folded)) and not (
            _SECOND_PERSON.search(folded) or _SECOND_EN.search(folded))

        def red(cat, sub, key):
            # 红条档命中；像在转述别人的话就降为琥珀；待确认的词条一律出琥珀（_as_pending）
            pend = is_pending(key)
            if selfy and sub == "strong":
                return _finding(cat, sub, RED, "amber", why(key), amb="self", tags=["self"],
                                route="confirm", pending=pend)
            if quote:
                return _finding(cat, sub, RED, "amber", why(key), amb="quote", tags=["quote"],
                                route="confirm", pending=pend)
            return _finding(cat, sub, RED, "alert", why(key), pending=pend)

        for sec, cat in (("strong", "insult"), ("threat", "threat"), ("sexual", "sexual")):
            for key in (sec, sec + PENDING):
                if ("insult", key) in hits:
                    out.append(red(cat, sec, key))
        if ("insult", "sexual-soft") in hits:
            out.append(_finding("sexual", "sexual-soft", CLUE, "note", why("sexual-soft")))
        if ("insult", "medium") in hits:
            # 中档词 + 第二人称：规则只记 note，请模型判断是骂还是亲昵
            targeted = bool(_SECOND_PERSON.search(folded))
            out.append(_finding("insult", "medium", CLUE, "note", why("medium"),
                                tags=["medium_targeted"] if targeted else (),
                                route="upgrade" if targeted else None))
        if ("insult", "watch") in hits:
            out.append(_finding("insult", "watch", CLUE, "note", why("watch")))
        for key, voc in (("target", "target_vocative"), ("target" + PENDING, "target_vocative_pending")):
            if ("insult", key) not in hits:
                continue
            pend = is_pending(key)
            if flags.get(voc):
                # 单独一个词（「gorda」）：可能是骂也可能是昵称
                out.append(_finding("insult", "target", RED, "note", why(key),
                                    tags=["vocative"], route="upgrade", pending=pend))
            elif _SELFREF.search(folded):
                # 顾客自述（estoy muy gorda）：减重品类里这是目标词最常见的用法
                out.append(_finding("insult", "target", RED, "note", why(key),
                                    tags=["self"], route="upgrade", pending=pend))
            else:
                out.append(red("insult", "target", key))
        return out

    # ---- B 假货、骗子、举报
    def _accuse(self, hits, folded, question, quote, weak_q=False):
        subs = (("call", "accuse_call"), ("report", "accuse_report"), ("assert", "accuse_claim"),
                ("assert-frame", "accuse_claim"))
        reds = [(sec, cat, hits[("accuse", key)], is_pending(key))
                for sec, cat in subs for key in (sec, sec + PENDING) if ("accuse", key) in hits]
        reds.sort(key=lambda r: r[3])             # 已确认的排前面（sort 稳定）
        if not reds:
            out = []
            if question:
                return out
            for sec in ("institution", "negative"):
                if ("accuse", sec) in hits:
                    out.append(_finding("accuse_claim", sec, CLUE, "note",
                                        ["accuse/%s:%s" % (sec, t) for t in hits[("accuse", sec)][:3]]))
            return out[:1]
        sec0, cat0, _, pend0 = reds[0]
        why = ["accuse/%s:%s" % (sec, t) for sec, _, terms, _ in reds for t in terms[:2]]
        if question:
            # 问句不计分，但句里有指称词：作为升级型送模型（没有模型时不显示）
            tag = "rhetorical_question" if _RHETORICAL.search(folded) else "question_exempt"
            return [_finding(cat0, sec0, RED, "note", why, tags=[tag], route="upgrade",
                             pending=pend0)]
        tags = []
        if _VICTIM.search(folded):
            tags.append("grievance")
        if quote:
            tags.append("quote")
        if (_COPULA_START.match(folded) and len(folded.split()) <= 6) or weak_q:
            tags.append("maybe_question")
        out = []
        for sec, cat, terms, pend in reds:
            w = ["accuse/%s:%s" % (sec, t) for t in terms[:2]]
            if tags:
                out.append(_finding(cat, sec, RED, "amber", w, amb=tags[0], tags=tags,
                                    route="confirm", pending=pend))
            else:
                out.append(_finding(cat, sec, RED, "alert", w, pending=pend))
        return out

    # ---- D 引流
    def _promo(self, hits, flags, light_text, folded, plain_q, structure_only):
        rules = self.rules
        out = []
        urls = [u for u in url_hits(light_text) if not self._link_exempt(u)]
        phones = phone_hits(light_text)
        emails = email_hits(light_text)
        handles = handle_hits(light_text, self.streamer)                          # 被账号标签引出的
        any_handles = handle_hits(light_text, self.streamer, labeled_only=False)  # 所有 @
        self_handles = self_handle_hits(light_text, self.streamer)               # mi ig @x 这类
        if rules.mine:
            phones = [p for p in phones if not any(m in p for m in rules.mine)]
            own = {m.lstrip("@") for m in rules.mine}
            handles = [h for h in handles if h not in own]
            any_handles = [h for h in any_handles if h not in own]
            self_handles = [h for h in self_handles if h not in own]
            emails = [e for e in emails if e not in rules.mine]
        pulls = hits.get(("promo", "pull"), [])
        scams = hits.get(("promo", "scam-strong"), [])
        pulls_p = hits.get(("promo", "pull" + PENDING), [])       # 待确认的：只出琥珀
        scams_p = hits.get(("promo", "scam-strong" + PENDING), [])
        offers = hits.get(("promo", "offer"), [])
        lures = hits.get(("promo", "lure"), [])
        weak = sorted(set(hits.get(("promo", "scam-weak"), [])))
        payload = bool(phones or handles or emails)
        cue = bool(offers or pulls or self_handles)
        # 顾客向主播要联系方式（ask）：offer / lure / 电话 / 链接的 note 和琥珀不报，只说 WhatsApp
        # 这个词绝不报。但 pull、scam-strong、同行名单照判——它们在 ask 之前，不能靠后面
        # 接一句「donde esta el link」躲过去
        ask = bool(flags.get("ask")) and not (urls or payload or scams or scams_p)

        if urls:
            if plain_q:
                # 问句里的链接：不送模型（打码后模型看不到域名）
                out.append(_finding("promo_link", "link", STRUCT, "amber", ["promo/link"],
                                    amb="question_link"))
            else:
                out.append(_finding("promo_link", "link", STRUCT, "alert", ["promo/link"]))
        if scams:
            out.append(_finding("promo_scam", "scam-strong", RED, "alert",
                                ["promo/scam-strong:" + scams[0]]))
        if scams_p:
            out.append(_finding("promo_scam", "scam-strong", RED, "alert",
                                ["promo/scam-strong:" + scams_p[0]], pending=True))
        if pulls:
            out.append(_finding("promo_pull", "pull", RED, "alert", ["promo/pull:" + pulls[0]]))
        if pulls_p:
            out.append(_finding("promo_pull", "pull", RED, "alert", ["promo/pull:" + pulls_p[0]],
                                pending=True))
        if weak:
            reason = "promo/scam-weak:" + ",".join(weak[:3])
            if len(weak) >= 2 or urls or payload or cue:
                out.append(_finding("promo_scam", "scam-weak", STRUCT, "alert", [reason]))
            else:
                out.append(_finding("promo_scam", "scam-weak", CLUE, "note", [reason]))
        if payload and cue:
            # 顾客留号码下单是常见行为，规则分不出它和同行引流：有下单语境词就只记 note
            ordering = bool(phones) and _ORDER_CTX.search(folded) and not (urls or scams or pulls
                                                                           or lures)
            if ordering:
                out.append(_finding("promo_contact", "phone", STRUCT, "note",
                                    ["promo/phone", "promo/order_context"]))
            else:
                out.append(_finding("promo_contact", "contact", STRUCT, "alert",
                                    ["promo/contact+cue"]))
        elif phones and any_handles:
            # 电话 + @账号（任何 @，包括呼叫朋友的那种）同现：一份联系方式清单
            out.append(_finding("promo_contact", "contact", STRUCT, "alert",
                                ["promo/phone+handle"]))
        elif phones:
            out.append(_finding("promo_contact", "phone", STRUCT, "note", ["promo/phone"]))
        elif emails:
            out.append(_finding("promo_contact", "email", STRUCT, "note", ["promo/email"]))
        # 单独的 @账号只记 1 分：不到 note
        if not (payload or urls or scams or pulls or scams_p or pulls_p or ask):
            if offers:
                out.append(_finding("promo_contact", "offer", CLUE, "note",
                                    ["promo/offer:" + offers[0]]))
            elif lures:
                out.append(_finding("promo_pull", "lure", CLUE, "note",
                                    ["promo/lure:" + lures[0]]))
        if (rules.competitors is not None and not structure_only and not plain_q
                and (lures or cue) and rules.competitors.search(folded)):
            out.append(_finding("promo_pull", "competitor", STRUCT, "alert", ["promo/competitor"]))
        return out

    # ---- 负面线索（只决定送不送模型）
    def _lead(self, hits, text, mention):
        why, sub = [], None
        if ("cues", "neg-emoji") in hits:
            why.append("cues/neg-emoji:" + hits[("cues", "neg-emoji")][0])
            sub = "emoji"
        if ("cues", "irony") in hits:
            why.append("cues/irony:" + hits[("cues", "irony")][0])
            sub = sub or "irony"
        ratio, words = self.rules.thresholds["caps_lead"]
        letters = [c for c in text if c.isalpha()]
        if len(letters) >= 4 and len(text.split()) >= words:
            if sum(c.isupper() for c in letters) / float(len(letters)) >= ratio:
                why.append("cues/caps")
                sub = sub or "caps"
        if not why:
            return None
        return _finding(None, sub, CLUE, "lead", why, tags=["mention"] if mention else (),
                        route="upgrade")

    # ---- C 刷屏
    def _flood(self, user, light_text, full_fold, ts, question):
        rules = self.rules
        emoji_only = not any(c.isalnum() for c in light_text)
        positive_emoji = emoji_only and bool(rules.positive_emoji) and all(
            c.isspace() or c in rules.positive_emoji for c in light_text)
        interaction = full_fold in rules.interaction or positive_emoji
        positive = positive_emoji or bool(
            rules.positive_re and rules.positive_re.search(full_fold))
        # 同人同句（C3）按 fold 后的原句比：「quiero el 1 / quiero el 2」是不同的句子。多账号同句（C2）
        # 才去掉数字再比：「participo en el sorteo 3 / 4 / 5」是同一句口令；去完没剩东西（纯数字）
        # 就用原文
        exact = full_fold or "e:" + _SPACE.sub("", light_text)    # 只有表情的弹幕：键不能是空串
        key = _SPACE.sub(" ", re.sub(r"\d+", " ", full_fold)).strip() or exact
        toks = frozenset(islice(key.split(), BRIGADE_TOKENS))
        events = self.flood.feed(user, key, toks, ts, question=question,
                                 mute_all=interaction, mute_nonburst=positive, exact_key=exact)
        self.last_flood = events
        out = []
        for ev in events:
            cat = {"user_burst": "flood_user", "same_text": "flood_repeat",
                   "brigade": "flood_brigade"}[ev["kind"]]
            why = ["flood/%s:n=%d,window=%d" % (ev["kind"], ev["n"], ev["window"])]
            if ev["lvl"] == "amber":
                out.append(_finding(cat, ev["kind"], STRUCT, "amber", why, amb="brigade"))
            else:
                out.append(_finding(cat, ev["kind"], STRUCT, ev["lvl"], why))
        return out

    # ---- 累计（只在 insult 线索档之间算）
    def _accumulate_step(self, findings, user, ts):
        if not user:
            return
        hit = [f for f in findings if f["cat"] in ("insult", "sexual") and f["tier"] == CLUE
               and f["sub"] in ("medium", "watch", "sexual-soft")]
        if not hit or any(f["tags"] and set(f["tags"]) & {"vocative", "self"} for f in hit):
            return
        if not self._accumulate(user, ts):
            return
        f = hit[0]
        f["tags"] = list(f["tags"]) + ["accumulated"]
        if self.rules.thresholds["accumulate_red"]:
            f["lvl"], f["route"] = "alert", None
        else:
            f["lvl"], f["amb"], f["route"] = "amber", "accumulated", "confirm"
