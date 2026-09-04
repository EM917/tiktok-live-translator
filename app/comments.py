"""观众弹幕（评论区）翻译 —— 只翻译、只显示，不进检测/审计链路。

弹幕来自 Chrome 插件对 TikTok 直播页评论区的抓取（浏览器以 "view" 身份连接，
详见 app/server.py 的 `_classify_origin`），与字幕（主播语音）是完全独立的
两条链路：弹幕不碰 queue/trans_queue/asr_pool/detector/audit，也不参与
`run_workers()` 的 gather——它是 Pipeline 级的独立协程，跨场次常驻，出错
只记日志，绝不影响直播翻译主链路（这个产品的 KPI 是违禁词召回和检测延迟，
弹幕翻译再怎么出错都不该波及那两样）。

铁律：只用常驻快速引擎（Pipeline.translator），绝不触发
`create_strong_translator()` —— 那是每次调用临时装载 5.3 GB 的 7B 模型，
弹幕这种锦上添花的功能不配抢这份显存，尤其是在直播 ASR 正跑着的时候。
字幕翻译永远优先：弹幕 worker 每轮攒批后都要等字幕翻译空闲，最多等
`IDLE_WAIT_MAX_SEC` 防止在字幕持续繁忙时弹幕被活活饿死。
"""
import asyncio
import re
import time
from collections import OrderedDict

# 「有没有可译内容」的粗判：至少要有 2 个字母（含各语言字母，排除数字和下划线）。
# 纯 emoji / 数字 / 标点的弹幕（"😂😂😂"、"1111"）翻不出东西，白白占用引擎。
_ALPHA_RE = re.compile(r"[^\W\d_]", re.UNICODE)
# CJK 统一表意文字（含扩展 A）：目标语言是中文、原文已经是中文时不用再翻一遍。
_CJK_RE = re.compile(r"[㐀-鿿]")
# 批量翻译回来的行首编号，如 "1. "、"2、"、"3)"、"4：" —— 翻译引擎经常会把
# 我们喂进去的编号原样保留甚至换个符号，统一在这里剥掉。
_LEADING_NUM_RE = re.compile(r"^\s*\d+\s*[.。、)）:：]\s*")


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
    return "\n".join("{}. {}".format(i + 1, t) for i, t in enumerate(texts))


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
    # 去重窗口：最近 500 个 id 精确去重（插件断线重连、MutationObserver 与
    # 兜底扫描重复命中同一条），同一 (user, text) 60 秒内只收一次
    # （虚拟列表复用节点时，兜底扫描会把同一条老评论当新的再报一次）。
    DEDUPE_ID_HISTORY = 500
    DEDUPE_CONTENT_SEC = 60.0

    def __init__(self, broadcast, translator, target, glossary=None, busy=None):
        """broadcast: async fn(msg)。translator/target/glossary：零参可调用
        （getter）——引擎能在界面里热切换、目标语言能改，弹幕翻译不能拿着
        构造时刻的快照用一整场。busy：零参可调用 -> bool，字幕翻译排队/在途
        时返回 True，弹幕翻译据此让路。
        """
        self._broadcast = broadcast
        self._translator = translator
        self._target = target
        self._glossary = glossary if glossary is not None else (lambda: None)
        self._busy = busy if busy is not None else (lambda: False)
        self._queue = asyncio.Queue()
        self._worker_task = None
        self._seen_ids = OrderedDict()      # id -> True，LRU，最多 DEDUPE_ID_HISTORY 个
        self._seen_content = {}             # (user, text) -> 上次接受的时间戳

    # ---- 入站：Chrome 插件发来的一批观众评论 ----
    async def accept(self, data):
        """校验、去重、广播、入队。永不抛异常——弹幕这条链路出错只能吞掉，
        绝不能把异常甩回 server._ws 那个 WebSocket 消息循环里。
        """
        try:
            return await self._accept(data)
        except Exception as exc:
            print("[警告] 处理观众评论失败: {}".format(exc))
            return 0

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
        if translator is None:
            state = "skipped"          # engine=none：只显示原文
        elif not needs_translation(text, target):
            state = "same"             # 已是目标语言，或没有可译内容
        else:
            state = "pending"
        await self._broadcast({"type": "comment", "id": cid, "user": user,
                               "text": text, "ts": ts, "translated": None,
                               "state": state})
        if state != "pending":
            return
        if self._queue.qsize() >= self.MAX_QUEUE:
            try:
                dropped = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                dropped = None
            if dropped is not None:
                await self._broadcast({"type": "comment_update", "id": dropped["id"],
                                       "translated": None, "state": "dropped"})
        await self._queue.put({"id": cid, "user": user, "text": text, "ts": ts})
        self._ensure_worker()

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
        first = await self._queue.get()
        batch = [first]
        # 攒批：先等一个固定窗口，再一口气把队列里已有的取走。不用
        # wait_for(queue.get(), 剩余时间)——超时取消 get() 时若恰好有 put()
        # 落在同一刻，那条弹幕会被静默吞掉（Python 3.12 之前的已知竞态），
        # 表现为界面上永远停在「翻译中…」。多等 0.3 秒对弹幕无所谓。
        await asyncio.sleep(self.BATCH_WINDOW_SEC)
        while len(batch) < self.BATCH_MAX:
            try:
                batch.append(self._queue.get_nowait())
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
        if translator is None:
            for item in batch:
                await self._safe_broadcast({"type": "comment_update", "id": item["id"],
                                            "translated": None, "state": "failed"})
            return
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

    async def _translate_many(self, translator, texts, target, glossary):
        joined = "\n".join(texts)
        hint = tuple(glossary.translation_pairs(joined)) if glossary else None
        raw = await translator.translate(build_batch(texts), target, source="auto",
                                          glossary=hint)
        parsed = parse_batch(raw, len(texts)) if raw else None
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
