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
"""
import asyncio
import re
import time
from collections import OrderedDict

from .i18n import L
from .translator import CachedTranslator, ClaudeTranslator, engine_is_claude

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
                 session=None, claude_factory=None):
        """broadcast: async fn(msg)。translator/target/glossary：零参可调用
        （getter）——引擎能在界面里热切换、目标语言能改，弹幕翻译不能拿着
        构造时刻的快照用一整场。busy：零参可调用 -> bool，字幕翻译排队/在途
        时返回 True，弹幕翻译据此让路。session：零参可调用，返回「这一场」的
        标识（管线传审计对象），「弹幕只显示原文」的提示每场只发一次。
        claude_factory：零参可调用，造弹幕专用的 Claude 实例（默认 Haiku 5.5，见
        _engine_for）；测试用它换成不联网的替身。
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
            accepted += 1
            await self._enqueue(cid, user, text, now)
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

    async def _enqueue(self, cid, user, text, ts):
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
        await self._broadcast({"type": "comment", "id": cid, "user": user,
                               "text": text, "ts": ts, "translated": None,
                               "state": state})
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
        """关掉弹幕专用 Claude 实例的连接（如果建过）。"""
        engine, self._claude_engine, self._claude_for = self._claude_engine, None, None
        if engine is not None:
            try:
                await engine.close()
            except Exception:
                pass

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
