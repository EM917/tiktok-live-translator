"""弹幕留意的模型复核（PR-5，spec §5.2–§5.9、§5.11、§5.13 的云端部分）：Claude Haiku 5.5 当第二道。

规则层（app/comment_guard.py）已经立即出了判定；这一层只给规则拿不准的弹幕补一句意见，影子期**只加不减**：
能把琥珀升红、能把 note/线索升成琥珀，**不能撤红、不能降级**（用户没有标注集，模型否决的准确度无从验证；
产品一贯漏报代价远高于误报，CLAUDE.md 第六条）。模型升出来的红条不自动加入关注名单（spec I14，
自动加入只看规则的明确命中，见 pipeline_guard._track_marks）。

* **打码**（§5.3）：电话、数字合计 >= 7 位的串（含西语数字词；数字之间隔着任何标点、符号、空白都算，不列举）、
  链接和邮箱、@账号一律在送出去之前换掉；
  不发昵称、uid、handle、主播名、时间戳、主播字幕。`mask_for_model` 记下每处替换在原文里的位置，
  证据片段校验和高亮都靠它映射回原文。
* **路由**（§5.2）：规则层给的 route（confirm / upgrade）加上优先级（P1 / P2）。同句缓存、同一个人
  30 秒 1 条、队列满了先丢最旧的 P2，都在 `ReviewPolicy` 里，纯逻辑、时钟由调用方传，测试里不等真时间。
* **合并**（§5.6）：`decide` 是纯函数，输入规则那一条的摘要和模型的判定，输出怎么改标注。
* **闸门**（§5.8 云端部分）：识别积压 >= 10 秒（健康等级不是 ok）就暂停送审，回到 ok 满 30 秒再恢复；
  没密钥、断网、冷却、密钥被拒、模型找不到：只用规则。Claude 后端不走熔断（它不占显存）。
* **ClaudeReviewer**：和 ClaudeTranslator 一样裸 aiohttp，不引入官方 SDK——requirements 一变，所有用户
  下次启动都要重装一遍依赖，这个先例（translator.py）更重要；偏离 spec §5.9 的「官方 SDK」，理由写进 PR。
  密钥用 `translator.api_key("ANTHROPIC_API_KEY")`（不绑工作区的密钥另带工作区 ID，请求头和字幕引擎是同一份
  `ClaudeTranslator.auth_headers`），请求规则抄 ClaudeTranslator.TUNED 里 Haiku 5.5 那一行
  （关思考、effort low；tests/test_comment_review.py 钉着两边一致）。回应的 stop_reason 和块的读法也复用
  `ClaudeTranslator.read_message`。**只记状态码，不记对方原话**。

这个文件没有界面文字：状态只发代码（pipeline_guard.GuardRuntime.state），文案在 web/guard.js。
审计只记计数、模型调用和闸门转折，不记弹幕原文、昵称、账号（spec §8，写在 pipeline_guard 里）。
"""
import asyncio
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from collections import OrderedDict, deque
from pathlib import Path

from . import comment_guard as cg
from .translator import ClaudeTranslator, anthropic_workspace, api_key, says_workspace_missing

GUARD_CLAUDE_MODEL = "claude-haiku-5-5"
MODEL_ENV = "TLT_GUARD_CLAUDE_MODEL"       # 评估用：覆盖复核用的模型（没调过的模型发通用最小请求体）。
#   tools/guard_review_eval.py 的价格表只有 Haiku 5.5 的，别的模型它拒绝运行（花费上限算不准）
KEY_NAME = "ANTHROPIC_API_KEY"
URL = ClaudeTranslator.URL
API_VERSION = ClaudeTranslator.API_VERSION

ROOT = Path(__file__).resolve().parent.parent
# 提示词是数据文件，改了要升版本号（prompt-v2.txt），哈希写进 session_start。路径不跟 cg.DEFAULTS_DIR 走：
# 测试把词表目录指到夹具，提示词仍然读仓库里这一份
PROMPT_VERSION = "v1"
PROMPT_FILE = ROOT / "comment_guard" / "defaults" / ("prompt-%s.txt" % PROMPT_VERSION)

LABELS = ("insult", "threat", "sexual", "fraud_claim", "boycott_call", "report_threat", "promo",
          "impersonation", "complaint", "question", "friendly", "neutral")
TARGETS = ("streamer", "viewer", "seller_or_product", "self", "none", "unclear")
# 模型标签 -> 守卫类别（§5.6）；这 8 个合称 ATTACK
ATTACK_CATS = {"insult": "insult", "threat": "threat", "sexual": "sexual", "fraud_claim": "accuse_claim",
               "boycott_call": "accuse_call", "report_threat": "accuse_report", "promo": "promo_pull",
               "impersonation": "promo_scam"}
# 非攻击标签 -> 界面上的模型意见代码（web/guard.js::guardReviewText）
OPINION_RV = {"question": "question", "complaint": "complaint", "friendly": "friendly", "neutral": "neutral"}
# 类别分三家：规则自己的类别和模型标签同家就用规则的，跨家才用模型的（§5.6）
FAMILY = {"insult": "A", "threat": "A", "sexual": "A", "accuse_claim": "B", "accuse_call": "B",
          "accuse_report": "B", "promo_pull": "D", "promo_scam": "D", "promo_link": "D", "promo_contact": "D"}
# 确认型里只有这四种琥珀可以被模型升红。pending（词条没经用户确认）和 self（第一人称自指）只写模型意见：
# 用户决定 7——没确认的词条只出琥珀、不标红，模型不能绕过这一道
PROMOTE_AMB = frozenset(("maybe_question", "grievance", "quote", "accumulated"))

REVIEW_SCHEMA_CLAUDE = {
    "type": "object", "additionalProperties": False, "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["k", "label", "target", "sure", "evidence"],
        "properties": {
            "k": {"type": "string"},
            "label": {"type": "string", "enum": list(LABELS)},
            "target": {"type": "string", "enum": list(TARGETS)},
            "sure": {"type": "boolean"},
            # 结构化输出不支持 maxLength（SDK 会替调用方去掉再在客户端校验；裸 HTTP 带着它会 400）：
            # 120 字的上限在 validate 里卡
            "evidence": {"type": "string"}}}}}}
EVIDENCE_MAX = 120

# 请求规则：抄 ClaudeTranslator.TUNED 里 Haiku 5.5 那一行（关思考、effort low），tests 钉着一致。
# 其它模型（TLT_GUARD_CLAUDE_MODEL 指定）发通用最小请求体：不写 thinking、不写 effort，给思考留够 max_tokens
TUNED = {GUARD_CLAUDE_MODEL: {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}}
MAX_TOKENS_TUNED = 1024
MAX_TOKENS_GENERIC = 2048

HEALTH_PAUSE_SEC = 10.0         # 识别积压到这么多秒（健康等级不是 ok）就暂停送审；等于 pipeline.AUDIO_BACKLOG_WARN_SEC
HEALTH_RESUME_SEC = 30.0        # 回到 ok 之后再持续这么久才恢复

MASK_NUM, MASK_LINK, MASK_USER = "#NUM#", "[link]", "@usuario"
SEND_MAX_CHARS = 280            # 打码之后截到这么多字
MIN_MASK_DIGITS = 7             # 数字（含西语数字词）合计这么多个就整串打码，不管上下文是不是金额、日期、单号
PREV_MAX_SEC = 120.0            # 上下文只带同一个人 2 分钟内的前一条


def load_prompt(path=None):
    """(提示词全文, 哈希前 12 位)；读不到返回 (None, None)——此时不能送审。"""
    try:
        text = Path(path or PROMPT_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None, None
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------------------------------
# 打码（§5.3）
# ------------------------------------------------------------------------------------------

_DIGIT_WORDS = "cero|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve"
# 数字或西语数字词，之间最多隔 3 个分隔符；合计 >= 7 个就整串打码。比规则层的电话识别（_PHONE_RE、
# _SPELLED_RE）宽：规则层要少误报，这里宁可多打。分隔符是「任何不是字母、数字的字符」（\W 加下划线：空白、
# 标点、符号、表情、隐形字符都算）：之前只列了 空白 - . , ( ) / 这几个，换成 : _ | * ~ + 就整串漏过去，
# 列举永远列不完。字母隔开的（1a2b3c…）不是能用的号码，不算
_NUM_SEP = r"[\W_]{0,3}"
# 数字词前后不能紧挨着字母或数字；下划线算分隔符，所以不用 \b（\b 把 _ 当字母，cuatro_cinco 里的 cinco 就认不出来）
_NUM_TOKEN = r"(?:\d|(?<![^\W_])(?:" + _DIGIT_WORDS + r")(?![^\W_]))"
# 起止都是数字或数字词（不吞结尾的分隔符，打码之后词与词之间的空格还在）
_NUM_RE = re.compile(r"(?:\+\s*)?" + _NUM_TOKEN + r"(?:" + _NUM_SEP + _NUM_TOKEN + r"){%d,}"
                     % (MIN_MASK_DIGITS - 1))
_TOKENS = {"link": MASK_LINK, "user": MASK_USER, "num": MASK_NUM}
_KIND_RANK = {"link": 3, "user": 2, "num": 1}
_SAFE_CHAR = {ord("<"): "\u2039", ord(">"): "\u203a"}


def _plain(chunk):
    """送出去的原样文字：<> 换成 ‹›（弹幕里的 </comment> 变不成标签），空白和控制字符各换成一个空格。
    逐字符一换一，长度不变——证据映射回原文靠这个。"""
    out = []
    for c in chunk:
        if c in "<>":
            out.append(_SAFE_CHAR[ord(c)])
        elif c.isspace() or unicodedata.category(c) == "Cc":
            out.append(" ")
        else:
            out.append(c)
    return "".join(out)


class MaskedText:
    """一条弹幕打码后的样子。segments：[(m_起, m_止, o_起, o_止, 种类)]，种类 None 是原样保留的一段
    （一换一，位置线性对应），"link" / "user" / "num" 是被换成记号的一段。"""

    def __init__(self, original, segments, text):
        self.original = original
        self.segments = segments
        self.text = text

    @property
    def spans(self):
        """每处替换在原文里的位置：[(o_起, o_止, 种类)]。"""
        return [(o0, o1, kind) for _, _, o0, o1, kind in self.segments if kind]

    def __iter__(self):
        yield self.text
        yield self.spans

    def to_original(self, start, end):
        """打码文本里的 [start, end) -> 原文里对应的 (o_起, o_止)。落在记号上的，取整处替换对应的那一段。"""
        o_start = o_end = None
        for m0, m1, o0, o1, kind in self.segments:
            if o_start is None and start < m1:
                o_start = o0 if kind else o0 + max(0, start - m0)
            if end <= m1 and end > m0:
                o_end = o1 if kind else o0 + (end - m0)
                break
        if o_start is None or o_end is None or o_end <= o_start:
            return None
        return o_start, o_end

    def evidence_in(self, evidence):
        """模型给的证据片段（NFKC、小写、压空白之后）在送出去的打码文本里的位置，换算成原文片段。
        返回 None：不是打码文本的子串（模型编的，或者引用了没送出去的内容）；返回 ""：没有证据。
        用打码文本比对而不是原文：引用 #NUM#、[link]、@usuario 的有效证据不能被误清掉。"""
        ev = cg.light(evidence or "")
        if not ev:
            return ""
        lt, lo, hi = cg.light_map(self.text)
        at = lt.find(ev)
        if at < 0:
            return None
        pos = self.to_original(lo[at], hi[at + len(ev) - 1])
        if pos is None:
            return None
        return self.original[pos[0]:pos[1]].strip()[:EVIDENCE_MAX]


def _find_spans(light_text):
    """light 文本上要打码的位置 [(起, 止, 种类)]。"""
    found = [(a, b, "link") for a, b in cg.url_spans(light_text)]
    found += [(a, b, "link") for a, b in cg.email_spans(light_text)]
    found += [(a, b, "user") for a, b in cg.handle_spans(light_text)]
    found += [(m.start(), m.end(), "num") for m in _NUM_RE.finditer(light_text)]
    return found


def mask(text, limit=SEND_MAX_CHARS):
    """打码，返回 MaskedText。文字先过 light_map（NFKC、小写、去隐形字符、压空白——规则层认链接和电话的
    那个形态），在上面找出该打码的位置，再换算回原文，从原文生成要送出去的文字（保留原来的大小写）。"""
    original = text if isinstance(text, str) else ""
    lt, lo, hi = cg.light_map(original[:cg.MAX_CHARS])
    if hi:
        original = original[:hi[-1]]      # light_map 数过 MAX_CHARS 就不再往后看；没看的部分不能原样送出去
    else:
        original = ""
    spans = []
    for a, b, kind in _find_spans(lt):
        if b > a:
            spans.append((lo[a], hi[b - 1], kind))
    spans.sort()
    merged = []
    for o0, o1, kind in spans:
        if merged and o0 < merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], o1),
                          prev[2] if _KIND_RANK[prev[2]] >= _KIND_RANK[kind] else kind)
        else:
            merged.append((o0, o1, kind))
    lead = len(original) - len(original.lstrip())        # 开头的空白不送
    segments, parts, m_pos, o_pos = [], [], 0, lead

    def room():
        return limit - m_pos

    for o0, o1, kind in merged + [(len(original), len(original), None)]:
        if o0 > o_pos:
            chunk = _plain(original[o_pos:o0])
            take = min(len(chunk), room())
            if take > 0:
                parts.append(chunk[:take])
                segments.append((m_pos, m_pos + take, o_pos, o_pos + take, None))
                m_pos += take
            if take < len(chunk):
                break                               # 满了：后面的一概不要
        if kind is None:
            break
        token = _TOKENS[kind]
        if len(token) > room():
            break                                   # 记号放不下就整个不要，不留 "#NU" 这种半截
        parts.append(token)
        segments.append((m_pos, m_pos + len(token), o0, o1, kind))
        m_pos += len(token)
        o_pos = o1
    return MaskedText(original, segments, "".join(parts).rstrip())


def mask_for_model(text):
    """spec §5.3 的接口：(打码后的文字, 替换位置)。要映射证据请用 mask() 返回的 MaskedText。"""
    m = mask(text)
    return m.text, m.spans


def _without_tokens(masked_text):
    """把记号换成一个字母 x：不会把相邻的数字接成一串，也不会留下链接或账号的样子。"""
    return masked_text.replace(MASK_USER, " x ").replace(MASK_LINK, " x ").replace(MASK_NUM, " x ")


def leaks(masked_text):
    """打码后的文字里还认得出链接、邮箱、电话或 @账号吗（认得出就是打码漏了，不能送）。"""
    lt = cg.light(_without_tokens(masked_text))
    return bool(cg.url_hits(lt) or cg.email_hits(lt) or cg.handle_spans(lt) or _NUM_RE.search(lt))


def reviewable(masked):
    """打码之后值不值得送：除记号以外至少 2 个字母（整条是个电话号码或链接，没有什么可判的），而且没漏。"""
    letters = sum(c.isalpha() for c in _without_tokens(masked.text))
    return letters >= 2 and not leaks(masked.text)


# ------------------------------------------------------------------------------------------
# 路由与提示词的属性（§5.2、§5.4）
# ------------------------------------------------------------------------------------------

def priority(result):
    """规则层一条判定要不要送、排哪一档：None 不送；"p1" 确认型和有词表命中的升级型；
    "p2" 只命中负面线索（lead）。route 本身是规则层按 §5.2 的不送清单算好的。"""
    route = result.get("route")
    if route not in ("confirm", "upgrade"):
        return None
    return "p2" if result.get("lvl") == "lead" else "p1"


def hint_of(cats, sub):
    """提示词里的 hint：命中的词表。辱骂写「insult_档位」（insult_medium、insult_target），其余写类别本身。"""
    if not cats:
        return ""
    cat = cats[0]
    if cat == "insult" and sub:
        return "insult_%s" % re.sub(r"[^a-z0-9]+", "_", str(sub).lower()).strip("_")
    return cat


def flags_of(result):
    """提示词里的 flags：规则层拿不准的地方。"""
    tags = set(result.get("tags") or ())
    out = [f for f in ("maybe_question", "quote", "vocative", "self") if f in tags]
    if result.get("lvl") == "lead":
        out.append("cue")
    return out


def build_user_message(items):
    """一批的 user 消息。items：[{"k", "text"（打码后）, "hint", "flags", "prev"（打码后，可空）}]。
    k 用短编号；attribute 的值都来自我们自己的词汇表，弹幕正文里的 <> 已经换掉，伪造不了标签。"""
    lines = []
    for it in items:
        flags = list(it.get("flags") or ())
        if it.get("prev"):
            flags.append("prev")
        attrs = 'k="%s"' % it["k"]
        if it.get("hint"):
            attrs += ' hint="%s"' % it["hint"]
        if flags:
            attrs += ' flags="%s"' % " ".join(flags)
        lines.append("<comment %s>%s</comment>" % (attrs, it["text"]))
        if it.get("prev"):
            lines.append('<prev k="%s">%s</prev>' % (it["k"], it["prev"]))
    return "\n".join(lines)


def review_key(masked, hint="", flags=(), prev_masked=None):
    """同句缓存和同一批里「跟着用答案」的键：模型这一条看到的一切——送出去的打码文本（问号、¿、表情、大小写
    原样）、hint、flags、同一个人的前一条——的短哈希。**不拿 cg.fold_key 当键**：fold 会丢掉问号和表情，
    而「问句还是断言」正是提示词要模型区分的东西（`¿es falso?` 是 question，`es falso 🤡` 是 fraud_claim），
    键一样就会把一句的判定用到另一句头上。route 不进键：模型看不到它，合并（decide）是每条自己算的。"""
    prev = prev_masked.text if prev_masked is not None else ""
    item = {"k": "0", "text": masked.text, "hint": hint or "", "flags": list(flags or ()), "prev": prev}
    return hashlib.sha1(build_user_message([item]).encode("utf-8")).hexdigest()[:16]


def counts_as_invalid(result):
    """session_end.review_invalid 数哪些调用：模型的回应不合格（outcome == invalid：解析不了、k 对不上、形状不对），
    或者接口认为我们的请求有问题（400 一类，会累计到「请求被拒」而停用）。不数 429（限流）、401 / 403（密钥被拒）、
    404（模型找不到）：它们是别的状态，在 comment_guard_review 的 outcome 和闸门记录里看得到。"""
    if result.outcome == "invalid":
        return True
    return result.outcome == "http_4xx" and result.http not in (401, 403, 404, 429)


# ------------------------------------------------------------------------------------------
# 校验（§5.5）与合并（§5.6）
# ------------------------------------------------------------------------------------------

class Verdict:
    """模型对一条弹幕的判定（已校验）。evidence 是通过校验的模型原话（打码文本的子串）或 ""。"""

    __slots__ = ("label", "target", "sure", "evidence")

    def __init__(self, label, target, sure, evidence=""):
        self.label, self.target, self.sure, self.evidence = label, target, bool(sure), evidence or ""

    @property
    def cat(self):
        return ATTACK_CATS.get(self.label)

    @property
    def attack(self):
        """合并规则里的「模型 ATTACK 且 sure」。目标是说话人自己的不算：提示词把自嘲排除在辱骂之外，
        模型给出「辱骂，目标是自己」是自相矛盾，当拿不准。"""
        return bool(self.sure and self.label in ATTACK_CATS and self.target != "self")

    def __repr__(self):
        return "Verdict(%s, %s, sure=%s)" % (self.label, self.target, self.sure)


def validate(raw_text, keys):
    """模型回的 JSON 文本 -> ({k: Verdict}, 丢掉的条数)。解析不了、不是 {"results": [...]} 返回 (None, 0)
    ——整批算「未复核」。k 不在本批里、重复出现（重复的两条都丢）、字段不合要求的，那一条丢掉。
    evidence 不在这里校验（要拿打码文本比，见 MaskedText.evidence_in）。"""
    try:
        data = json.loads(raw_text)
    except (TypeError, ValueError):
        return None, 0
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None, 0
    expected = set(keys)
    seen, dup, out, dropped = set(), set(), {}, 0
    for row in rows:
        k = row.get("k") if isinstance(row, dict) else None
        if not isinstance(k, str) or k not in expected:
            dropped += 1
            continue
        if k in seen:
            dup.add(k)
            continue
        seen.add(k)
        label, target, sure = row.get("label"), row.get("target"), row.get("sure")
        evidence = row.get("evidence", "")
        if label not in LABELS or target not in TARGETS or not isinstance(sure, bool) \
                or not isinstance(evidence, str):
            dropped += 1
            continue
        out[k] = Verdict(label, target, sure, evidence[:EVIDENCE_MAX])
    for k in dup:
        if out.pop(k, None) is not None:
            dropped += 1
    return out, dropped


class Decision:
    """合并规则的结果。promote：None / "amber_to_red" / "note_to_amber"；agree / disagree 是影子期一致性计数。"""

    __slots__ = ("rv", "promote", "cats", "agree", "disagree", "evidence")

    def __init__(self):
        self.rv = None
        self.promote = None
        self.cats = None
        self.agree = False
        self.disagree = None
        self.evidence = False       # 要不要把证据片段挂在这条标注上


def pick_cats(rule_cats, model_cat):
    if rule_cats and FAMILY.get(rule_cats[0]) is not None and FAMILY.get(rule_cats[0]) == FAMILY.get(model_cat):
        return list(rule_cats)
    return [model_cat]


def decide(entry, verdict):
    """spec §5.6 的合并表。entry 是规则那一条的摘要（route / lvl / amb / cats），verdict 是模型的判定。
    纯函数：不碰任何状态，只说「怎么改」。从不撤红、从不降级。

    确认型（琥珀）：模型 ATTACK 且 sure -> 升红（amb 在 PROMOTE_AMB 里时），写「同意规则」；非攻击 ->
    琥珀保留，写模型意见；拿不准 -> 琥珀保留，写「拿不准」。
    升级型（note / 线索）：模型 ATTACK 且 sure -> 升为琥珀（amb 写 review，类别见 pick_cats）；
    其它保持隐藏，什么都不显示。"""
    d = Decision()
    route = entry.get("route")
    if not verdict.sure or (verdict.label in ATTACK_CATS and verdict.target == "self"):
        kind = "unsure"
    elif verdict.label in ATTACK_CATS:
        kind = "attack"
    else:
        kind = "opinion"
    if route == "confirm":
        if kind == "attack":
            d.rv, d.agree, d.evidence = "agree", True, True
            if entry.get("lvl") == "amber" and entry.get("amb") in PROMOTE_AMB:
                d.promote = "amber_to_red"
        elif kind == "unsure":
            d.rv = "unsure"
        else:
            d.rv, d.disagree = OPINION_RV[verdict.label], verdict.label
    elif route == "upgrade" and kind == "attack":
        d.promote, d.evidence = "note_to_amber", True
        d.cats = pick_cats(entry.get("cats") or [], verdict.cat)
    return d


# ------------------------------------------------------------------------------------------
# 队列与限频（§5.2 去重与配额、§5.11）
# ------------------------------------------------------------------------------------------

class ReviewQueue:
    """候选队列：满了先丢最旧的 P2，再丢最旧的 P1（来的是 P2 而队里全是 P1，就丢来的这一条）。"""

    def __init__(self, maxlen):
        self.maxlen = maxlen
        self._items = []

    def __len__(self):
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    def clear(self):
        self._items = []

    def push(self, entry):
        """放进去，返回被挤掉的候选（可能是 entry 自己）。"""
        evicted = []
        if len(self._items) >= self.maxlen:
            victim = next((e for e in self._items if e["prio"] == "p2"), None)
            if victim is None:
                if entry["prio"] == "p2":
                    return [entry]
                victim = self._items[0]
            self._items.remove(victim)
            evicted.append(victim)
        self._items.append(entry)
        return evicted

    def pop_first(self):
        return self._items.pop(0) if self._items else None

    def drain(self):
        items, self._items = self._items, []
        return items


class ReviewPolicy:
    """限频、缓存、健康闸的纯逻辑。时钟都由调用方传（`now`），测试里不等真时间。常量都是类属性，测试可以调小。"""

    BATCH_MAX = 8                   # 一次最多问几条
    BATCH_MIN = 4                   # 连续撞 max_tokens 之后降到这个
    MAX_TOKENS_STREAK = 3
    CALLS_PER_MIN = 10
    CALLS_PER_SESSION = 600
    QUEUE_MAX = 40
    PER_USER_SEC = 30.0             # 同一个人 30 秒内最多送 1 条
    USER_TABLE_MAX = 2000
    CACHE_MAX = 2000
    CACHE_TTL_SEC = 1800.0
    MAX_AGE_SEC = 60.0              # 排了这么久还没送出去的，弹幕早滚走了，记「未复核」
    HEALTH_PAUSE_SEC = HEALTH_PAUSE_SEC
    HEALTH_RESUME_SEC = HEALTH_RESUME_SEC

    def __init__(self):
        self.reset()

    def reset(self):
        """换场：全部清空（同句缓存也按场，不把上一个主播的判定带到下一个）。"""
        self._users = OrderedDict()
        self._cache = OrderedDict()
        self._calls = deque()
        self.calls_total = 0
        self.batch_limit = self.BATCH_MAX
        self.paused = False
        self.lag_sec = 0.0
        self._ok_since = None

    # ---- 同一个人
    def user_allowed(self, ukey, now):
        """同一个人 30 秒内最多送 1 条。没有账号键（连昵称都没有）的不限。通过就记一笔。"""
        if not ukey:
            return True
        last = self._users.get(ukey)
        if last is not None and now - last < self.PER_USER_SEC:
            return False
        self._users[ukey] = now
        self._users.move_to_end(ukey)
        while len(self._users) > self.USER_TABLE_MAX:
            self._users.popitem(last=False)
        return True

    # ---- 同句缓存
    def cache_get(self, key, now):
        hit = self._cache.get(key)
        if hit is None:
            return None
        if now - hit[0] > self.CACHE_TTL_SEC:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return hit[1]

    def cache_put(self, key, verdict, now):
        self._cache[key] = (now, verdict)
        self._cache.move_to_end(key)
        while len(self._cache) > self.CACHE_MAX:
            self._cache.popitem(last=False)

    # ---- 调用额度
    def call_allowed(self, now):
        while self._calls and now - self._calls[0] >= 60.0:
            self._calls.popleft()
        return len(self._calls) < self.CALLS_PER_MIN and self.calls_total < self.CALLS_PER_SESSION

    def note_call(self, now):
        self._calls.append(now)
        self.calls_total += 1

    def note_max_tokens_streak(self, streak):
        if streak >= self.MAX_TOKENS_STREAK:
            self.batch_limit = self.BATCH_MIN

    # ---- 识别健康闸
    def update_health(self, now, backlog_sec):
        """积压 >= 10 秒（健康等级不是 ok）就暂停；回到 ok 之后持续满 30 秒才恢复。
        返回转折："paused" / "resumed" / None。"""
        backlog = float(backlog_sec or 0.0)
        if backlog >= self.HEALTH_PAUSE_SEC:
            self.lag_sec = backlog
            self._ok_since = None
            if not self.paused:
                self.paused = True
                return "paused"
            return None
        if self.paused:
            if self._ok_since is None:
                self._ok_since = now
            if now - self._ok_since >= self.HEALTH_RESUME_SEC:
                self.paused = False
                self._ok_since = None
                return "resumed"
        return None


# ------------------------------------------------------------------------------------------
# Claude 后端（§5.9）
# ------------------------------------------------------------------------------------------

class ReviewResult(dict):
    """一次 review 的结果：{cid: Verdict}，加上这次调用的情况（审计、费用、评估都读它）。
    outcome：ok / timeout / http_4xx / http_5xx / refusal / max_tokens / invalid / skipped。"""

    outcome = "ok"
    ms = 0
    n = 0                          # 这一批问了几条
    valid = 0                      # 其中判定通过校验的条数
    evidence_cleared = 0           # 其中证据片段不是打码文本的子串、被清空的条数
    in_tokens = out_tokens = cache_read_tokens = cache_write_tokens = None
    refusal_category = None
    http = None


def request_extras(model):
    """这个模型的专门请求字段（深拷贝）。只给逐条核对过的模型加（见 TUNED 的注释）。"""
    return json.loads(json.dumps(TUNED.get(model) or {}))


def guard_model():
    return (os.environ.get(MODEL_ENV) or "").strip() or GUARD_CLAUDE_MODEL


class ClaudeReviewer:
    """Claude 复核后端。review() 永不抛（除了取消）：出任何错这一批都记「未复核」，状态见 status()。

    状态机（spec §5.9 的错误表，从最具体的往下接）：
      提示词文件读不到 -> prompt_missing：不送（密钥没问题，所以不叫 no_key）；
      401 / 403  -> key_rejected：停用，直到密钥被改（密钥的指纹变了才重新试，见 GuardRuntime.configure_reviewer）；
      404        -> model_missing：本场停用；
      429        -> cooldown：按 retry-after 冷却，最长 300 秒；
      5xx / 连不上 / 超时 -> 连续 3 次暂停 120 秒（offline）；
      其它 4xx（含 400）-> 那一批记 http_4xx；连续 3 次本场停用（invalid，这是我们的 bug）。
    refusal（HTTP 200 + stop_reason）：整批未复核，记 stop_details.category，不重试，不传 fallbacks。
    max_tokens：整批未复核，连续 3 次把每批上限从 8 降到 4（见 batch_limit_hint）。"""

    name = "claude"
    TIMEOUT_SEC = 8.0
    TIMEOUT_GRACE_SEC = 1.0         # aiohttp 自己的总时限先到；这一道是兜底（假会话、卡住的连接也不会拖住复核任务）
    OFFLINE_STREAK = 3
    OFFLINE_PAUSE_SEC = 120.0
    INVALID_STREAK = 3
    RATE_LIMIT_DEFAULT_SEC = 30.0
    RATE_LIMIT_MAX_SEC = 300.0
    workspace = None                    # 没走 __init__ 的替身也有（同 ClaudeTranslator.workspace_id）
    probe_workspace_missing = False

    def __init__(self, key, model=None, prompt=None, clock=time.monotonic, url=None, workspace=None):
        self.api_key = key
        self.workspace = workspace      # 工作区 ID（translator.WORKSPACE_ENV）；没填是 None，请求里不带这个头
        self.probe_workspace_missing = False
        self.model = model or guard_model()
        self.prompt, self.prompt_hash = (prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]) \
            if prompt is not None else load_prompt()
        self._clock = clock
        self.url = url or URL
        self._session = None
        self.key_fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        self.key_rejected = None        # HTTP 状态码；跨场保持（同一把密钥不再试）
        self.new_session()

    def new_session(self):
        """换场：本场才有效的状态清掉（冷却时钟、连续失败计数、本场停用）。密钥被拒不清。"""
        self.model_missing = False
        self.invalid_locked = False
        self.cooldown_until = 0.0
        self.offline_until = 0.0
        self.fail_streak = 0
        self.invalid_streak = 0
        self.max_tokens_streak = 0
        self.calls = 0

    # ---- 状态
    def status(self, now=None):
        """(状态代码, 附带数字)。claude 表示可以送。"""
        now = self._clock() if now is None else now
        if self.prompt is None:
            return "prompt_missing", {}         # 密钥在、提示词文件读不到：不是密钥的问题，别让人去重填密钥
        if self.key_rejected is not None:
            return "key_rejected", {"http": self.key_rejected}
        if self.model_missing:
            return "model_missing", {}
        if self.invalid_locked:
            return "invalid", {}
        if now < self.cooldown_until:
            return "cooldown", {"wait_min": max(1, int(math.ceil((self.cooldown_until - now) / 60.0)))}
        if now < self.offline_until:
            return "offline", {}
        return "claude", {}

    def blocked(self, now=None):
        """不能送的原因代码，可以送返回 None。"""
        code = self.status(now)[0]
        return None if code == "claude" else code

    # ---- 请求
    def _headers(self):
        return {**ClaudeTranslator.auth_headers(self.api_key, self.workspace), "content-type": "application/json"}

    def build_body(self, user_message):
        """这一批的请求体。评估工具直接调它，测的就是要发版的这一份。
        没有 temperature / top_p / top_k（Haiku 5.5 上非默认值 400）、没有预填（最后一条是 user）、
        没有 fallbacks（Haiku 5.5 没有服务端兜底）、没有 budget_tokens（400）。"""
        extra = request_extras(self.model)
        output_config = dict(extra.get("output_config") or {})
        output_config["format"] = {"type": "json_schema", "schema": json.loads(json.dumps(REVIEW_SCHEMA_CLAUDE))}
        body = {
            "model": self.model,
            "max_tokens": MAX_TOKENS_TUNED if extra else MAX_TOKENS_GENERIC,
            # 最小可缓存前缀 512 token：照样写 cache_control。调用稀疏，缓存多半是冷的，不靠它省钱
            "system": [{"type": "text", "text": self.prompt, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user_message}],
            "output_config": output_config,
        }
        if "thinking" in extra:
            body["thinking"] = extra["thinking"]
        return body

    async def session(self):
        import aiohttp  # 延迟导入：同 BaseTranslator

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.TIMEOUT_SEC))
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def probe_key(self):
        """不花 token 的探针：GET /v1/models/{模型}，返回 HTTP 状态码；连不上抛异常（自检限时 5 秒）。
        回 400 时只记一个是或否：是不是「密钥没绑工作区」（probe_workspace_missing，自检那一行据此说怎么办），
        原话不留。"""
        from urllib.parse import quote

        url = self.url.rsplit("/", 1)[0] + "/models/" + quote(self.model, safe="")
        self.probe_workspace_missing = False
        session = await self.session()
        async with session.get(url, headers=ClaudeTranslator.auth_headers(self.api_key, self.workspace)) as resp:
            if resp.status == 400 and not self.workspace:
                self.probe_workspace_missing = says_workspace_missing(await ClaudeTranslator._error_summary(resp))
            return resp.status

    async def _post(self, body):
        session = await self.session()
        async with session.post(self.url, data=json.dumps(body), headers=self._headers()) as resp:
            if resp.status != 200:
                return resp.status, None, self._retry_after(resp) if resp.status == 429 else None
            return 200, await resp.json(content_type=None), None

    def _retry_after(self, resp):
        headers = getattr(resp, "headers", None) or {}
        try:
            raw = next((v for k, v in headers.items() if str(k).lower() == "retry-after"), None)
            if raw is not None:
                return min(max(float(raw), 1.0), self.RATE_LIMIT_MAX_SEC)
        except (TypeError, ValueError, AttributeError):
            pass
        return self.RATE_LIMIT_DEFAULT_SEC

    # ---- 一批
    async def review(self, batch):
        """batch：[{"cid", "masked": MaskedText, "prev_masked": MaskedText | None, "hint", "flags"}]。
        返回 ReviewResult（{cid: Verdict}）。永不抛（取消除外）。"""
        result = ReviewResult()
        result.n = len(batch)
        if not batch or self.prompt is None:
            result.outcome = "skipped"          # 没有提示词就不能送（系统提示是空的）
            return result
        keys = {}
        items = []
        for i, e in enumerate(batch):
            k = str(i + 1)
            keys[k] = e
            items.append({"k": k, "text": e["masked"].text, "hint": e.get("hint"), "flags": e.get("flags"),
                          "prev": e["prev_masked"].text if e.get("prev_masked") else ""})
        body = self.build_body(build_user_message(items))
        t0 = time.perf_counter()
        self.calls += 1
        try:
            status, data, retry = await asyncio.wait_for(self._post(body), self.TIMEOUT_SEC + self.TIMEOUT_GRACE_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 超时、连不上、回应读不了：没有拿到回应，算同一类（只记「没回应」，不记异常原文）
            result.ms = int((time.perf_counter() - t0) * 1000)
            result.outcome = "timeout"
            self._note_unreachable()
            return result
        result.ms = int((time.perf_counter() - t0) * 1000)
        if status != 200:
            result.http = status
            result.outcome = "http_5xx" if status >= 500 else "http_4xx"
            self._note_http_error(status, retry)
            return result
        self.fail_streak = 0
        self._read_usage(result, data)
        text, stop, category = ClaudeTranslator.read_message(data)
        if stop == "refusal":
            result.outcome, result.refusal_category = "refusal", category
            return result
        if stop == "max_tokens":
            result.outcome = "max_tokens"
            self.max_tokens_streak += 1
            return result
        self.max_tokens_streak = 0
        self.invalid_streak = 0
        verdicts, _ = validate(text, keys)
        if verdicts is None:
            result.outcome = "invalid"
            return result
        for k, verdict in verdicts.items():
            entry = keys[k]
            if entry["masked"].evidence_in(verdict.evidence) is None:
                verdict.evidence = ""       # 证据不是送出去的打码文本的子串（模型编的）：清空，判定保留
                result.evidence_cleared += 1
            result[entry["cid"]] = verdict
        result.valid = len(result)
        return result

    @staticmethod
    def _read_usage(result, data):
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return

        def num(name):
            value = usage.get(name)
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        base, read, write = num("input_tokens"), num("cache_read_input_tokens"), num("cache_creation_input_tokens")
        if base is not None:
            result.in_tokens = base + (read or 0) + (write or 0)       # 全部输入 token（缓存读写也算）
        result.out_tokens = num("output_tokens")
        result.cache_read_tokens = read
        result.cache_write_tokens = write

    # ---- 失败的记账
    def _note_unreachable(self):
        self.fail_streak += 1
        if self.fail_streak >= self.OFFLINE_STREAK:
            self.offline_until = self._clock() + self.OFFLINE_PAUSE_SEC
            self.fail_streak = 0

    def _note_http_error(self, status, retry):
        if status in (401, 403):
            self.key_rejected = status
        elif status == 404:
            self.model_missing = True
        elif status == 429:
            self.cooldown_until = self._clock() + (retry or self.RATE_LIMIT_DEFAULT_SEC)
        elif status >= 500:
            self._note_unreachable()
        else:
            self.invalid_streak += 1
            if self.invalid_streak >= self.INVALID_STREAK:
                self.invalid_locked = True


def make_reviewer(clock=time.monotonic, existing=None):
    """按当前密钥造 Claude 复核后端。没有密钥返回 None。同一把密钥、同一个工作区 ID 且模型没变就沿用 existing
    （它记着这把密钥被拒过）。提示词读不到：照样造，但它的状态是 prompt_missing（不送、不联网），和「没有密钥」
    分开说——密钥是好的，让人去重填密钥是错的方向。"""
    key = api_key(KEY_NAME)
    if not key:
        return None
    model = guard_model()
    workspace = anthropic_workspace()
    if existing is not None and getattr(existing, "api_key", None) == key and existing.model == model \
            and getattr(existing, "workspace", None) == workspace and existing.prompt is not None:
        return existing
    return ClaudeReviewer(key, model=model, clock=clock, workspace=workspace)
