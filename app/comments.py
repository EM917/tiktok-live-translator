# i18n: done
"""观众弹幕（评论区）翻译 —— 只翻译、只显示，不进检测/审计链路。

弹幕由程序自己经 TikTokLive 从直播间的评论 WebSocket 抓取（见
app/comment_source.py），与字幕（主播语音）是完全独立的两条链路：弹幕不碰 queue/trans_queue/asr_pool/detector/audit，也不参与
`run_workers()` 的 gather——它是 Pipeline 级的独立协程，跨场次常驻，出错
只记日志，绝不影响直播翻译主链路（这个产品的 KPI 是违禁词召回和检测延迟，
弹幕翻译再怎么出错都不该波及那两样）。

铁律：弹幕绝不触发 `create_strong_translator()` —— 那是每次调用临时装载 5.3 GB 的 7B 模型，
弹幕这种锦上添花的功能不配抢这份显存，尤其是在直播 ASR 正跑着的时候。弹幕只用两类引擎：
常驻的本地快速模型（hymt2、gemma）；以及字幕引擎是 Claude 时的 Claude Haiku 5.5
（2026-10-13 用户决定）——后者是**另建的一个实例**，不和字幕引擎共用：弹幕约 0.3 秒一批，
比字幕密得多，撞上 429 时暂停的只能是弹幕这个实例，字幕翻译不能跟着停。
字幕翻译永远优先：弹幕 worker 每轮攒批后都要等字幕翻译空闲，最多等
`IDLE_WAIT_MAX_SEC` 防止在字幕持续繁忙时弹幕被活活饿死。

弹幕重译（2026-10-14）：中控在某条弹幕上点「重译」时，字幕引擎是 Claude 才翻，用**另建的**
Sonnet 5.5 实例（第三个，和字幕、弹幕的 Haiku 各管各的冷却）；其它引擎一律不翻，仍然不碰
`create_strong_translator()`。账本、限额和给中控看的话在 app/comment_redo.py。
"""
import asyncio
import re
import time
from collections import OrderedDict

from . import comment_redo as redo
from .i18n import L
from .translator import (CachedTranslator, ClaudeTranslator, begin_translate_call,
                         engine_is_claude, looks_fabricated, translate_call_outcome)

# 「有没有可译内容」的粗判：至少要有 2 个字母（含各语言字母，排除数字和下划线）。
# 纯 emoji / 数字 / 标点的弹幕（"😂😂😂"、"1111"）翻不出东西，白白占用引擎。
_ALPHA_RE = re.compile(r"[^\W\d_]", re.UNICODE)
# CJK 统一表意文字（含扩展 A）：目标语言是中文、原文已经是中文时不用再翻一遍。
_CJK_RE = re.compile(r"[㐀-鿿]")                        # i18n: data
# 批量翻译回来的行首编号，如 "1. "、"2、"、"3)"、"4：" —— 翻译引擎经常会把
# 我们喂进去的编号原样保留甚至换个符号，统一在这里剥掉。
_LEADING_NUM_RE = re.compile(r"^\s*\d+\s*[.。、)）:：]\s*")   # i18n: data

# 弹幕只用这几种引擎翻：常驻的本地快速模型 hymt2（1.8B）、gemma，以及 claude。
#   * hymt2-7b 不算——弹幕绝不用 7B（见模块开头的铁律）；
#   * claude 用的是另建的实例（见 CommentTranslator._engine_for），模型固定 Haiku 5.5；
#   * 其余远程引擎不算。弹幕约 0.3 秒一批，比字幕（约 9 秒一条）密得多：打在 Google
#     免费接口上会让它回 429，整个引擎实例暂停 120 秒，字幕和（没有强模型时的）
#     报警上下文跟着一起没有译文；打在 DeepL 上是在花按字符计费的额度；OpenAI 同样按量计费，
#     而且接的可能是用户自己的本机服务，用量和能力都不知道。
# 其它引擎下弹幕只显示原文，每场提示一次。
COMMENT_ENGINES = ("hymt2", "gemma", "claude")
REMOTE_ENGINE_HINT = L("当前翻译引擎不翻译弹幕（只有本地 Hy-MT2 1.8B、本地 TranslateGemma 和 Claude 会），弹幕只显示原文",
                       "The current translation engine doesn’t translate comments. Only Local "
                       "Hy-MT2 1.8B, Local TranslateGemma and Claude do, so comments show the "
                       "original text.")
LARGE_MODEL_HINT = L("弹幕不用本地 Hy-MT2 7B 翻译，只显示原文",
                     "Comments aren’t translated with local Hy-MT2 7B. They show the original text.")
_NOT_YET = object()


def engine_allows_comments(translator):
    """这个引擎能不能拿来翻弹幕（见 COMMENT_ENGINES）。"""
    return translator is not None and getattr(translator, "name", None) in COMMENT_ENGINES


def original_only_hint(translator):
    """弹幕只显示原文时对中控说的那句话。7B 是本地模型，不能和其它引擎一样说成
    「不翻译弹幕的引擎」。"""
    if getattr(translator, "name", None) == "hymt2-7b":
        return LARGE_MODEL_HINT
    return REMOTE_ENGINE_HINT


def _comment_claude():
    """弹幕专用的 Claude 实例：Haiku 5.5，提示词按弹幕写。没有密钥时抛 RuntimeError。"""
    return ClaudeTranslator(model=ClaudeTranslator.COMMENT_MODEL, role="comment")


def _comment_claude_strong():
    """弹幕重译专用的 Claude 实例：Sonnet 5.5（和字幕重译同一档，见 ClaudeTranslator.TUNED），
    提示词按弹幕写。没有密钥时抛 RuntimeError。"""
    return ClaudeTranslator(model=ClaudeTranslator.STRONG_MODEL, role="comment_strong")


def needs_translation(text, target):
    """这段弹幕值不值得（也翻不翻得出）过一次翻译引擎。

    模块级函数：与实例状态无关，供测试直接调用。
    """
    text = text or ""
    # 至少要 2 个字母才值得过一次引擎——纯符号/单字符这类没有实际内容的弹幕
    # 翻译成本和收益都不成比例，直接当原文显示更快也更不容易出洋相。
    if len(_ALPHA_RE.findall(text)) < 2:
        return False
    if str(target or "").lower().startswith("zh") and _CJK_RE.search(text):
        return False
    return True


# 回话里才会出现的说法（模型在跟人说话：要上下文、说自己拿不准、提「翻译」这件事）。原文里本来就在说
# 翻译 / 上下文的弹幕不按这条判（「¿me puedes traducir?」译成「你能帮我翻译吗」是对的）
_META_REPLY_RE = re.compile(
    r"上下文|更多信息|请提供|请您提供|如果您能|不太确定|无法确定|无法翻译|确切含义|"   # i18n: data
    r"\bcontext\b|\bcould you\b|\bplease (?:provide|paste|share)\b|\bi(?:'m| am) not sure\b|"
    r"\btranslat", re.IGNORECASE)
_META_IN_SOURCE_RE = re.compile(r"tradu|translat|context|contexto", re.IGNORECASE)

# 短弹幕的译文长度上限：looks_fabricated 的长度比只管 20 字以上的原文，「Sii todo」这种两个词的
# 弹幕被当成对话、回一整句话（2026-10-13 实录，约 150 字符）它管不到
SHORT_REPLY_MAX = 60


def plausible_translation(text, out, target):
    """这条译文像不像是在翻译这条弹幕（而不是在回话、在问问题）。不像就当没译出来，只显示原文。

    用结构信号，不猜语义：
      * looks_fabricated 的几条（问句被吃掉、数字消失、比原文长出一大截）；
      * 译文里有只在回话里才出现的说法（要上下文、说拿不准、提「翻译」这件事）；
      * 原文很短、译文却长出一大截；
      * 目标语言是中文，原文不是中文，译文里却一个汉字都没有、还比原文多出好几个词——回话多半是
        英文的一句话（「Could you paste the comments?」）。只差一两个词的不算：品牌名、人名原样
        保留是正常译文。"""
    if not out:
        return False
    if looks_fabricated(text, out):
        return False
    if _META_REPLY_RE.search(out) and not _META_IN_SOURCE_RE.search(text):
        return False
    if len(out) > max(SHORT_REPLY_MAX, len(text) * 4):
        return False
    if str(target or "").lower().startswith("zh") and not _CJK_RE.search(text) \
            and not _CJK_RE.search(out) and len(out.split()) >= len(text.split()) + 3:
        return False
    return True


# 弹幕作者和主播的关系（comment_source._REL_FLAGS），页面上显示成「粉丝 / 互关 / 送过礼 / 订阅」
REL_TAGS = ("follower", "mutual", "gifter", "subscriber")


def _rel_tags(value):
    """抓取端给的关系标签：只留认得的、去重、保持顺序；不是列表就当没有。"""
    if not isinstance(value, list):
        return None
    out = [t for t in REL_TAGS if t in value]
    return out or None


def build_batch(texts):
    """把多条弹幕拼成带编号的一段文本，一次 translate() 调用搞定一批。"""
    return "\n".join("{}. {}".format(i + 1, t) for i, t in enumerate(texts))   # i18n: data


def parse_batch(output, n):
    """把批量翻译的输出按行拆回列表。

    两种情况都判定为「对不上」，返回 None，调用方据此改为逐条重译，而不是
    把错位的译文安给错的弹幕：行数对不上（引擎合并/拆分了行）；或者行数
    对了但某一行没带编号前缀——那通常意味着引擎没有老实按「每行一条」的
    格式回复（比如把说明文字和第一条译文粘在了一起），行数相等纯属巧合，
    内容对应关系已经不可信了。
    """
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if len(lines) != n:
        return None
    out = []
    for ln in lines:
        m = _LEADING_NUM_RE.match(ln)
        if not m:
            return None
        out.append(ln[m.end():].strip())
    return out


# ---- 弹幕留意（PR-3）：账号字段和守卫判定只做透传 ----
# 抓取端（comment_source）给的 uid / sec / handle，和守卫（pipeline_guard）加的 uid_ok / guard，
# 校验类型和长度后原样带进 `comment` 消息。**不带 marked**：关注高亮由页面按 config.guard_marked 现算，
# 缓存里不存，移出之后刷新才不会又亮回来。没有这些键就一个都不加，老消息逐字节不变。
MAX_UID = 96
MAX_SEC = 80
MAX_HANDLE = 64
WHY_ITEM_MAX = 200                  # why 里一条词表条目最多留这么长，再长的截断（不丢）
GUARD_LVLS = ("alert", "amber", "note", "lead")
GUARD_TIERS = ("red", "clue", "struct")
GUARD_SRC = ("rule", "review")


def _str_field(value, limit):
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _str_list(value, item_limit, max_items, clip=False):
    """字符串列表：非字符串、空串丢掉，最多 max_items 项。超过 item_limit 的项：clip=False 丢掉，
    clip=True 截断并以「…」结尾（给 `why` 用：出厂词表里最长的正则条目有 580 多字，丢掉就让红条
    没了依据）。"""
    if not isinstance(value, list):
        return None
    out = []
    for x in value:
        if not isinstance(x, str) or not x:
            continue
        if len(x) > item_limit:
            if not clip:
                continue
            x = x[:item_limit - 1] + "…"
        out.append(x)
    return out[:max_items]


def clean_guard(guard):
    """守卫判定只留 spec §6 列出的键，类型和长度不对的键整个丢掉；lvl 不对就整条丢掉。"""
    if not isinstance(guard, dict) or guard.get("lvl") not in GUARD_LVLS:
        return None
    out = {"lvl": guard["lvl"]}
    for key, limit in (("amb", 24), ("sub", 24), ("rv", 16), ("ev", 64), ("evidence", 300)):
        if key in guard:
            value = guard[key]
            out[key] = value if value is None else _str_field(value, limit)
    if "tier" in guard:
        out["tier"] = guard["tier"] if guard["tier"] in GUARD_TIERS else None
    if "src" in guard and guard["src"] in GUARD_SRC:
        out["src"] = guard["src"]
    cats = _str_list(guard.get("cats"), 24, 6)
    if cats is not None:
        out["cats"] = cats
    why = _str_list(guard.get("why"), WHY_ITEM_MAX, 8, clip=True)
    if why is not None:
        out["why"] = why
    return out


def account_fields(item):
    """一条弹幕里要透传给 `comment` 消息的账号字段和守卫判定（没有的不出现）。"""
    out = {}
    uid = _str_field(item.get("uid"), MAX_UID)
    if uid is not None:
        out["uid"] = uid
        if isinstance(item.get("uid_ok"), bool):
            out["uid_ok"] = item["uid_ok"]
    sec = _str_field(item.get("sec"), MAX_SEC)
    if sec is not None:
        out["sec"] = sec
    handle = _str_field(item.get("handle"), MAX_HANDLE)
    if handle is not None:
        out["handle"] = handle
    guard = clean_guard(item.get("guard"))
    if guard is not None:
        out["guard"] = guard
    return out


class CommentTranslator:
    """把观众弹幕攒批、排队、翻译，翻完广播出去。

    所有数字常量都放成类属性，方便测试把队列/批次调小、把等待调短，
    不用在真实值上等秒级的时间。
    """

    MAX_QUEUE = 40
    BATCH_MAX = 5
    BATCH_WINDOW_SEC = 0.3
    MAX_ITEMS_PER_MSG = 20
    MAX_TEXT = 300
    MAX_USER = 64
    MAX_ID = 64
    IDLE_WAIT_MAX_SEC = 3.0
    TRANSLATE_TIMEOUT_SEC = 20.0
    # 去重窗口：最近 500 个 id 精确去重（抓取端断线重连会把同一条再报一次），
    # 同一 (user, text) 60 秒内只收一次。
    DEDUPE_ID_HISTORY = 500
    DEDUPE_CONTENT_SEC = 60.0

    def __init__(self, broadcast, translator, target, glossary=None, busy=None,
                 session=None, claude_factory=None, claude_strong_factory=None):
        """broadcast: async fn(msg)。translator/target/glossary：零参可调用
        （getter）——引擎能在界面里热切换、目标语言能改，弹幕翻译不能拿着
        构造时刻的快照用一整场。busy：零参可调用 -> bool，字幕翻译排队/在途
        时返回 True，弹幕翻译据此让路。session：零参可调用，返回「这一场」的
        标识（管线传审计对象），「弹幕只显示原文」的提示每场只发一次。
        claude_factory：零参可调用，造弹幕专用的 Claude 实例（默认 Haiku 5.5，见
        _engine_for）；测试用它换成不联网的替身。claude_strong_factory 同理，造弹幕重译专用的
        实例（默认 Sonnet 5.5，见 _redo_engine_for）。
        """
        self._session = session if session is not None else (lambda: None)
        self._hinted_session = _NOT_YET
        self._hinted_text = None
        self._broadcast = broadcast
        self._translator = translator
        self._target = target
        self._glossary = glossary if glossary is not None else (lambda: None)
        self._busy = busy if busy is not None else (lambda: False)
        # 队列延迟到协程里建：Python 3.9 的 asyncio.Queue() 会绑到构造时的事件
        # 循环，循环外构造（测试、或将来在别的线程里搭 Pipeline）直接抛
        # RuntimeError——与 pipeline.py 里 asyncio.Lock 的处理一致。
        self._queue = None
        self._worker_task = None
        self._seen_ids = OrderedDict()      # id -> True，LRU，最多 DEDUPE_ID_HISTORY 个
        self._seen_content = {}             # (user, text) -> 上次接受的时间戳
        # 字幕引擎是 Claude 时，弹幕用的那个独立实例，以及它是照哪个字幕引擎对象建的
        self._claude_factory = claude_factory or _comment_claude
        self._claude_engine = None
        self._claude_for = None
        self._closing = set()               # 换下来的实例正在关连接的任务，保引用
        # 弹幕重译：最近 ORIGINAL_KEEP 条的原文、在途的 id、每分钟的次数（账本），以及重译专用
        # 的 Sonnet 实例和它是照哪个字幕引擎对象建的（换了对象就跟着重建，同 _claude_for）
        self._ledger = redo.RedoLedger()
        self._claude_strong_factory = claude_strong_factory or _comment_claude_strong
        self._redo_engine = None
        self._redo_for = None

    # ---- 入站：抓取端送来的一批观众评论 ----
    async def accept(self, data):
        """校验、去重、广播、入队。永不抛异常——弹幕这条链路出错只能吞掉，
        绝不能把异常甩回 server._ws 那个 WebSocket 消息循环里。
        """
        try:
            return await self._accept(data)
        except Exception as exc:
            print("[警告] 处理观众评论失败: {}".format(exc))
            return 0

    def _q(self):
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    async def _accept(self, data):
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return 0
        self._ensure_worker()
        now = time.time()
        accepted = 0
        for item in items[: self.MAX_ITEMS_PER_MSG]:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            text = item.get("text")
            if not isinstance(cid, str) or not cid or not isinstance(text, str):
                continue
            text = text.strip()[: self.MAX_TEXT]
            if not text:
                continue
            cid = cid[: self.MAX_ID]
            user = item.get("user")
            user = (user if isinstance(user, str) else "")[: self.MAX_USER]
            if cid in self._seen_ids:
                continue
            key = (user, text)
            last = self._seen_content.get(key)
            if last is not None and now - last < self.DEDUPE_CONTENT_SEC:
                continue
            self._remember(cid, key, now)
            self._ledger.remember(cid, text)
            accepted += 1
            await self._enqueue(cid, user, text, now, account_fields(item),
                                rel=_rel_tags(item.get("rel")))
        return accepted

    def _remember(self, cid, content_key, ts):
        self._seen_ids[cid] = True
        while len(self._seen_ids) > self.DEDUPE_ID_HISTORY:
            self._seen_ids.popitem(last=False)
        self._seen_content[content_key] = ts
        # 内容去重表顺手清理过期项，避免无界增长（弹幕量不大，够用）
        if len(self._seen_content) > self.DEDUPE_ID_HISTORY * 2:
            cutoff = ts - self.DEDUPE_CONTENT_SEC
            self._seen_content = {k: v for k, v in self._seen_content.items()
                                  if v >= cutoff}

    async def _enqueue(self, cid, user, text, ts, extra=None, rel=None):
        translator = self._translator()
        target = self._target()
        hint = None
        if translator is None:
            state = "skipped"          # engine=none：只显示原文
        elif not needs_translation(text, target):
            state = "same"             # 已是目标语言，或没有可译内容
        elif not engine_allows_comments(translator):
            state = "skipped"          # 不是常驻本地模型：只显示原文（见 COMMENT_ENGINES）
            hint = original_only_hint(translator)
        else:
            state = "pending"
        msg = {"type": "comment", "id": cid, "user": user, "text": text, "ts": ts,
               "translated": None, "state": state}
        if extra:
            msg.update(extra)                   # 守卫字段（弹幕留意）：同样只给电脑页
        if rel:
            msg["rel"] = rel                    # 只给电脑页；手机页的白名单里没有它（app/viewer.py）
        await self._broadcast(msg)
        if hint:
            await self._hint_original_only(hint)
        if state != "pending":
            return
        queue = self._q()
        if queue.qsize() >= self.MAX_QUEUE:
            try:
                dropped = queue.get_nowait()
            except asyncio.QueueEmpty:
                dropped = None
            if dropped is not None:
                await self._broadcast({"type": "comment_update", "id": dropped["id"],
                                       "translated": None, "state": "dropped"})
        await queue.put({"id": cid, "user": user, "text": text, "ts": ts})
        self._ensure_worker()

    async def _hint_original_only(self, text):
        """「弹幕只显示原文」同一场、同一句只说一次：每条弹幕都说一遍会把提示区刷爆。
        场内换了引擎、要说的话变了（远程引擎 ↔ 7B）才再说。"""
        session = self._session()
        if self._hinted_session is session and self._hinted_text == text:
            return
        self._hinted_session, self._hinted_text = session, text
        await self._safe_broadcast({"type": "notice", "text": text})

    def _ensure_worker(self):
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.ensure_future(self._worker())

    # ---- 后台 worker：攒批 -> 让路给字幕 -> 翻译 -> 广播 ----
    async def _worker(self):
        """惰性启动（首次 accept 时才建），永不主动退出、永不向外抛异常——
        单批翻译出错只影响那一批，worker 本身必须一直活着接后面的弹幕。
        （显式取消 —— 如测试收尾 —— 走的是 asyncio.CancelledError，
        不受这里的 except Exception 影响，会正常传播出去。）
        """
        while True:
            try:
                await self._process_one_batch()
            except Exception as exc:
                print("[警告] 弹幕翻译 worker 异常: {}".format(exc))

    async def _process_one_batch(self):
        queue = self._q()
        first = await queue.get()
        batch = [first]
        # 攒批：先等一个固定窗口，再一口气把队列里已有的取走。不用
        # wait_for(queue.get(), 剩余时间)——超时取消 get() 时若恰好有 put()
        # 落在同一刻，那条弹幕会被静默吞掉（Python 3.12 之前的已知竞态），
        # 表现为界面上永远停在「翻译中…」。多等 0.3 秒对弹幕无所谓。
        await asyncio.sleep(self.BATCH_WINDOW_SEC)
        while len(batch) < self.BATCH_MAX:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        # 字幕翻译永远优先：排队或在途时弹幕让路，但最多等这么久，防止字幕
        # 持续繁忙时弹幕被饿死——晚翻译总比不翻译强。
        waited = 0.0
        while self._busy() and waited < self.IDLE_WAIT_MAX_SEC:
            await asyncio.sleep(0.05)
            waited += 0.05
        try:
            await asyncio.wait_for(self._translate_batch(batch),
                                   timeout=self.TRANSLATE_TIMEOUT_SEC)
        except Exception as exc:
            print("[警告] 弹幕翻译失败: {}".format(exc))
            for item in batch:
                await self._safe_broadcast({"type": "comment_update", "id": item["id"],
                                            "translated": None, "state": "failed"})

    async def _translate_batch(self, batch):
        # 每批开头抓一次快照：引擎可能在这批翻译进行中被界面上的操作换掉，
        # 但这一批必须自始至终用同一个对象，行为才可预期。
        translator = self._translator()
        engine = self._engine_for(translator) if engine_allows_comments(translator) else None
        if engine is None:
            # 入队之后引擎被关掉，或被换成了不翻弹幕的引擎：这批不送出去，原样显示原文。
            # 引擎还是 Claude、却造不出弹幕那个实例（密钥没了）：当作这批翻译失败
            state = "failed" if translator is None or engine_allows_comments(translator) \
                else "skipped"
            for item in batch:
                await self._safe_broadcast({"type": "comment_update", "id": item["id"],
                                            "translated": None, "state": state})
            return
        translator = engine
        target = self._target()
        glossary = self._glossary()
        texts = [item["text"] for item in batch]
        if len(texts) == 1:
            outs = [await self._translate_one(translator, texts[0], target, glossary)]
        else:
            outs = await self._translate_many(translator, texts, target, glossary)
        for item, out in zip(batch, outs):
            if out and not plausible_translation(item["text"], out, target):
                print("[警告] 弹幕译文不像是在翻译这条弹幕（{} 字 → {} 字），只显示原文".format(
                    len(item["text"]), len(out)))
                out = None
            if out:
                await self._safe_broadcast({"type": "comment_update", "id": item["id"],
                                            "translated": out, "state": "ok"})
            else:
                await self._safe_broadcast({"type": "comment_update", "id": item["id"],
                                            "translated": None, "state": "failed"})

    def _engine_for(self, translator):
        """这一批弹幕实际交给哪个引擎翻；造不出来返回 None。

        本地引擎就是常驻的那个。字幕引擎是 Claude 时**另建一个实例**（模型固定 Haiku 5.5，
        提示词按弹幕写，套重复句缓存）：冷却、400 记录都挂在实例上，弹幕被 429 暂停
        不能连累字幕。字幕引擎换了对象（重填密钥）就跟着重建；换成别的引擎时先留着，
        下次又换回 Claude 且是同一个字幕引擎对象时接着用。"""
        inner = getattr(translator, "inner", translator)
        if getattr(inner, "name", None) != "claude":
            return translator
        if self._claude_engine is not None and self._claude_for is inner:
            return self._claude_engine
        self._close_later(self._claude_engine)
        self._claude_engine = self._claude_for = None
        try:
            self._claude_engine = CachedTranslator(self._claude_factory())
        except Exception as exc:
            print("[警告] 弹幕翻译没能建起 Claude 实例: {}".format(exc))
            return None
        self._claude_for = inner
        return self._claude_engine

    def _close_later(self, engine):
        """不等结果地关掉换下来的实例的连接；出什么错都只能放弃。"""
        if engine is None:
            return
        try:
            task = asyncio.ensure_future(engine.close())
        except Exception:
            return
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def close(self):
        """关掉弹幕专用 Claude 实例（翻译的、重译的）的连接，建过的话。"""
        engine, self._claude_engine, self._claude_for = self._claude_engine, None, None
        redo_engine, self._redo_engine, self._redo_for = self._redo_engine, None, None
        for one in (engine, redo_engine):
            if one is not None:
                try:
                    await one.close()
                except Exception:
                    pass

    # ---- 弹幕重译（见 app/comment_redo.py）----
    def _redo_engine_for(self, translator):
        """弹幕重译实际交给哪个引擎；造不出来（没有密钥）返回 None。

        另建一个 Sonnet 5.5 实例，不和字幕引擎、弹幕的 Haiku 共用：冷却、400 记录、拒答计数
        都挂在实例上，重译被 429 暂停不能连累字幕，也不能连累弹幕自己的翻译。不套
        CachedTranslator：重译就是要重新问一次，命中缓存等于白点。字幕引擎换了对象（重填密钥）
        就跟着重建。"""
        inner = getattr(translator, "inner", translator)
        if self._redo_engine is not None and self._redo_for is inner:
            return self._redo_engine
        self._close_later(self._redo_engine)
        self._redo_engine = self._redo_for = None
        try:
            self._redo_engine = self._claude_strong_factory()
        except Exception as exc:
            print("[警告] 弹幕重译没能建起 Claude 实例: {}".format(exc))
            return None
        self._redo_for = inner
        return self._redo_engine

    async def retranslate(self, cid):
        """中控在某条弹幕上点了「重译」（web/app.js 发 retranslate_comment）。永不抛异常。

        每一条不重译的出路都用 comment_redo 消息回话，页面把话显示在那条弹幕下面（文字见
        comment_redo.py）；译文出来了就用 comment_update 换掉那条弹幕的译文——服务端的留存
        和手机页都跟着换。手机发不出这个动作（app/viewer.py 的入站只认 retranslate）。"""
        try:
            await self._retranslate(cid)
        except Exception as exc:
            print("[警告] 弹幕重译出错: {}".format(exc))

    async def _retranslate(self, cid):
        if not isinstance(cid, str) or not cid:
            return                  # 不是弹幕 id：没有哪条弹幕可以回话
        cid = cid[: self.MAX_ID]
        if cid.startswith(redo.DEMO_ID_PREFIX):
            # --demo 的虚构弹幕：从来没进过账本，不能当成「挤出了最近 200 条」来答
            await self._redo_refused(cid, redo.note_demo())
            return
        text = self._ledger.original(cid)
        if text is None:
            await self._redo_refused(cid, redo.note_gone())
            return
        translator = self._translator()
        # 铁律：只有 Claude 能翻弹幕重译。别的引擎（尤其是本地 7B）到这里就停，不去叫强模型
        if not engine_is_claude(translator):
            await self._redo_refused(cid, redo.note_needs_claude(translator))
            return
        engine = self._redo_engine_for(translator)
        if engine is None:
            await self._redo_refused(cid, redo.note_no_key())
            return
        refused = self._ledger.begin(cid)
        if refused is not None:
            kind, wait = refused
            if kind == "busy":
                await self._redo_say(cid, redo.note_busy())          # 按钮已经是「重译中…」
            else:
                await self._redo_refused(cid, redo.note_full(wait))
            return
        try:
            await self._safe_broadcast({"type": "comment_redo", "id": cid, "redo_state": "pending"})
            out, why = await self._redo_translate(engine, text)
        except Exception as exc:
            print("[警告] 弹幕重译出错: {}".format(exc))
            out, why = None, redo.note_unknown()
        finally:
            self._ledger.finish(cid)
        if out:
            await self._safe_broadcast({"type": "comment_update", "id": cid,
                                        "translated": out, "state": "ok"})
            await self._safe_broadcast({"type": "comment_redo", "id": cid, "redo_state": "ok"})
        else:
            await self._redo_refused(cid, why)

    async def _redo_translate(self, engine, text):
        """用重译实例译一次。返回 (译文, None) 或 (None, 给中控的说明)。

        译文过的关和批量翻译一样：词表兜底替换，再过 plausible_translation（弹幕是陌生人的话，
        模型可能把它当成对自己说的，回一整句话）——不像译文就当没译出来，原译文留着。
        直接 await translate：拒答的结局记在本任务的上下文里（translator.translate_call_outcome），
        套 wait_for 会让它丢掉；实例自己的 HTTP 超时（BaseTranslator.TIMEOUT）兜住不返回的情况。"""
        target = self._target()
        begin_translate_call()
        error_before = getattr(engine, "last_error", None)
        out = await self._translate_one(engine, text, target, self._glossary())
        discarded = False
        if out and not plausible_translation(text, out, target):
            print("[警告] 弹幕重译的译文不像是在翻译这条弹幕（{} 字 → {} 字），已丢弃".format(
                len(text), len(out)))
            out, discarded = None, True
        if out:
            return out, None
        return None, redo.failure_text(engine, error_before, translate_call_outcome(), discarded)

    async def _redo_say(self, cid, text):
        """只说一句话，不动按钮的状态。"""
        await self._safe_broadcast({"type": "comment_redo", "id": cid, "text": text})

    async def _redo_refused(self, cid, text):
        """这次点击没有换出新译文：按钮标成失败，再在那条弹幕下面说明。"""
        await self._safe_broadcast({"type": "comment_redo", "id": cid, "redo_state": "failed",
                                    "text": text})

    async def _translate_many(self, translator, texts, target, glossary):
        joined = "\n".join(texts)                   # i18n: data（弹幕原文，喂给翻译引擎）
        hint = tuple(glossary.translation_pairs(joined)) if glossary else None
        raw = await translator.translate(build_batch(texts), target, source="auto",
                                          glossary=hint)
        parsed = parse_batch(raw, len(texts)) if raw else None
        if parsed is None and not raw and engine_is_claude(translator) \
                and getattr(translator, "fail_streak", 0) > 0:
            # 接口这一次回的是错（HTTP 非 200）：逐条重发是同样的结果，只会把一批的请求翻成
            # 六倍。拒答是 HTTP 200（fail_streak 为 0），不走这里——逐条重译正好把拒答的那
            # 一条隔离出来，其余照常翻
            return [None] * len(texts)
        if parsed is None:
            # 行数对不上：宁可逐条重译（顺序不变），也不能把译文安给错的弹幕
            return [await self._translate_one(translator, t, target, glossary)
                   for t in texts]
        return [self._apply_glossary(glossary, texts[i], parsed[i])
               for i in range(len(texts))]

    async def _translate_one(self, translator, text, target, glossary):
        hint = tuple(glossary.translation_pairs(text)) if glossary else None
        try:
            out = await translator.translate(text, target, source="auto", glossary=hint)
        except Exception:
            return None
        return self._apply_glossary(glossary, text, out)

    def _apply_glossary(self, glossary, text, out):
        if not out or not glossary:
            return out
        try:
            return glossary.apply(text, out)
        except Exception:
            return out

    async def _safe_broadcast(self, msg):
        try:
            await self._broadcast(msg)
        except Exception as exc:
            print("[警告] 弹幕消息广播失败: {}".format(exc))
