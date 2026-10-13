# i18n: done
"""弹幕留意的接线（PR-3，spec §4.7、§6、§8）：把规则层（app/comment_guard.py）和关注名单
（app/comment_marks.py）挂到弹幕读取协程上。没有界面；`comment_guard.enabled` 默认关。

**对识别零影响靠结构保证，不靠运气**（spec I1/I2）：守卫只出现在这些地方——`on_items`（弹幕读取
协程）、开场/收尾（`_begin_session` / `_end_session`）、控制消息、统计循环。识别、违禁词检测、
审计的热路径（`asr_worker`、`translation_worker`、`_emit_original`、`_translate_and_update`）里
没有一处引用守卫，tests/test_comment_guard_pipeline.py 用 AST 钉着这一条。复核（PR-5 的 Claude）
是守卫自己的 asyncio 任务，惰性启动、永不向外抛异常；`flush` 只往队列里放候选、从不等它。

**永不抛、永不阻塞**（I4）：`observe()` 逐条 try/except，出任何错都把这一批原样交给 `accept`；
`flush()` 同理。守卫不碰通知和声音（I5）：这个模块不 import alert_notify / window_attention。

**消息**（spec §6）：规则判定随 `comment` 消息立即到（`guard` 字段）；之后只有标注变了才发
`comment_guard`（每个 cid 每秒至多 1 条）；`guard_event` 是聚合后的事件，全局每秒至多 1 条；
`guard_mark` 是名单的增删。三种消息都只发本机页面，手机同看一律不发（用户决定 8，app/viewer.py）。

**审计**（spec §8）：只记计数、不记弹幕原文、昵称、账号。`session_start.comment_guard` 在开场写，
`session_end.comment_guard` 在收尾写；两处都只用守卫在 `bind` 时拿到的**本场自己的** audit
（CLAUDE.md 第七条 my_audit）：晚到的旧任务关不掉新一场的守卫，也写不进新一场的审计。

界面文字只在文件末尾 `guard_notice` 里（关注名单损坏、名单已满、规则文件被改）；别的地方只出代码。
"""
import asyncio
import os
import time
from collections import OrderedDict, deque

from . import comment_guard as cg
from .comment_marks import MAX_VIEWERS, SESSION_PREFIX, MarkStore, clean_key
from .comments import CommentTranslator
from .i18n import L
from .settings import load_settings

ENV_SWITCH = "TLT_COMMENT_GUARD"            # =off：应急总开关，界面坏了也能整体关掉
SETTINGS_KEY = "comment_guard"              # settings.json 里的键：{enabled, backend, weak}
BACKENDS = ("none", "claude")               # 用户决定 6：直播时只用 Claude Haiku 5.5，没有本地模型
GUARD_CONTROL_TYPES = frozenset(("guard_set", "guard_mark", "guard_dismiss"))
UID_SOURCES = ("id", "id_str", "sec_uid", "handle")
RV_LABELS = ("agree", "question", "complaint", "friendly", "neutral", "unsure", "skipped")
SHOWN_LEVELS = ("alert", "amber", "note")   # 带 guard 字段的档位；lead 只决定送不送模型


def env_disabled():
    return os.environ.get(ENV_SWITCH, "").strip().lower() in ("off", "0", "false", "no")


def normalize_settings(raw):
    """settings.json 里的 comment_guard -> {enabled, backend, weak}。类型不对的键按默认：
    enabled 只认真正的 True（PR-3 默认 False，PR-4 才改默认），backend 只认 BACKENDS。"""
    raw = raw if isinstance(raw, dict) else {}
    backend = raw.get("backend")
    return {"enabled": raw.get("enabled") is True,
            "backend": backend if backend in BACKENDS else "none",
            "weak": raw.get("weak") is True}


def _short(value, limit):
    """非空字符串，截到 limit；其它一律 None。"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _rules_fingerprint():
    """词表文件的 (名字, mtime, 大小) 清单：直播中被改了就提示一次（不热加载）。只做 stat。"""
    out = []
    for directory in (cg.DEFAULTS_DIR, cg.LOCAL_DIR):
        try:
            with os.scandir(str(directory)) as it:
                for entry in sorted(it, key=lambda e: e.name):
                    if entry.name.endswith(".txt") and entry.is_file():
                        st = entry.stat()
                        out.append((directory.name + "/" + entry.name, st.st_mtime_ns, st.st_size))
            for sub in ("trusted",):
                tdir = directory / sub
                if tdir.is_dir():
                    for name in sorted(os.listdir(str(tdir))):
                        st = (tdir / name).stat()
                        out.append((directory.name + "/" + sub + "/" + name, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return tuple(out)


class GuardRuntime:
    """守卫的运行态：规则引擎、关注名单、事件聚合、计数。全部在事件循环线程里用，磁盘 IO 在
    MarkStore 里放线程池。一个 Pipeline 一个，跨场次复用；`begin` 换场时全部重置。"""

    # 节流（spec §6）：守卫消息只在变化时发，免得冲场把页面的发送缓冲塞满
    CID_MSG_GAP_SEC = 1.0           # 同一个 cid 的 comment_guard 至少隔这么久
    EVENT_MSG_GAP_SEC = 1.0         # guard_event 全局至少隔这么久
    STATE_MSG_GAP_SEC = 2.0         # config.guard_state 至少隔这么久
    EVENT_MERGE_SEC = 60.0          # 同类事件 60 秒内合成一条
    EVENT_MAX = 50
    SHOWN_MAX = 1000                # accept 的内容去重表同量级
    ANN_MAX = 500
    ACCOUNTS_MAX = 500
    MAX_NICK = 64
    SAMPLE_TEXT = 120
    REVIEW_QUEUE_MAX = 50
    REVIEW_BATCH = 8

    def __init__(self, clock=time.time, mono=time.monotonic, marks=None):
        self._clock = clock
        self._mono = mono
        self.cfg = normalize_settings(None)
        self.marks = marks if marks is not None else MarkStore(clock=clock)
        self.reviewer = None            # PR-5 的 ClaudeReviewer 挂在这里；PR-3 没有，只用规则
        self.enabled = False            # observe 看这个：bind 之后才为真
        self.audit = None               # 本场自己的 audit（my_audit）
        self.rules = None
        self.engine = None
        self.budget = None
        self.streamer = ""
        self.notices = []               # 待发的提示代码（pipeline_guard.guard_notice 换成文字）
        self.rules_fp = ()
        self._token = 0
        self._open = False              # begin 过、还没 end
        self._ready = False             # begin 成功、可以 bind
        self._session_enabled = False
        self._env_off = False
        self._since = clock()
        self._last_status = None
        self._broadcast = None
        self._flush_timer = None
        self._flush_task = None
        self._review_task = None
        self._review_event = None
        self._reset_session()

    # ------------------------------------------------------------------------------------
    # 设置与状态
    # ------------------------------------------------------------------------------------
    def effective_enabled(self):
        if self._open:
            return self._session_enabled
        return self.cfg["enabled"] and not env_disabled()

    def status_code(self):
        """config.guard_state.status：只发代码，文案在前端。PR-3 没有复核后端：选了 Claude 也只用规则，
        状态写 no_sdk（缺少 Claude 组件，只用规则），不假装在复核。"""
        if env_disabled():
            return "env_off"
        if not self.effective_enabled():
            return "off"
        if self.cfg["backend"] == "claude":
            return "no_sdk" if self.reviewer is None else "claude"
        return "rules"

    def state(self):
        status = self.status_code()
        enabled = self.effective_enabled()
        if (status, enabled) != self._last_status:
            self._last_status = (status, enabled)
            self._since = self._clock()
        rules = None
        if self.rules is not None:
            rules = {"count": self.rules.count,
                     "warnings": sum(w.get("n", 1) if w.get("reason") == "more" else 1
                                     for w in self.rules.warnings)}
        return {"enabled": enabled, "backend": self.cfg["backend"], "status": status,
                "since": self._since, "trip": None, "rules": rules,
                "n_marked": len(self.marks), "unreviewed": self.unreviewed,
                "weak": self.cfg["weak"]}

    def preview_rules(self):
        """开着但还没开播时，设置行也要能显示「规则 N 条已生效」。同步、毫秒级；开场仍会重新载入。"""
        if self.rules is None:
            self.rules = cg.load_rules()
        return self.rules

    # ------------------------------------------------------------------------------------
    # 换场
    # ------------------------------------------------------------------------------------
    def _reset_session(self):
        self.counts = {}
        self.amber_by_reason = {}
        self.observed = 0
        self.observe_ms_max = 0.0
        self.errors = 0
        self.marked_auto = self.marked_manual = self.unmarked_auto = self.dismissed = 0
        self.guard_msgs = 0
        self.unreviewed = 0
        self.events = OrderedDict()          # 事件 id -> 事件（最近更新的在后）
        self._ev_index = {}                  # (类别, 人) -> (事件 id, 最近时间)
        self._ev_seq = 0
        self._ev_dirty = OrderedDict()       # 还没发给页面的事件 id（先变脏的在前）
        self._ev_sent = set()
        self._last_ev_msg = float("-inf")
        self._pending_cg = OrderedDict()     # cid -> guard
        self._cg_sent = OrderedDict()        # cid -> 上次发的时刻
        self._pending_marks = OrderedDict()  # 主键 -> 记录 | None
        self._pending_dismiss = []
        self._shown = OrderedDict()          # (昵称, 文本前 300 字) -> {cid, ts}：accept 会留下哪一条
        self._ann = OrderedDict()            # cid -> 当前的 guard
        self._accounts = OrderedDict()       # 账号键 -> 昵称、sec、handle：加入名单时补全
        self._state_dirty = False
        self._last_state_msg = float("-inf")
        self._review_q = deque(maxlen=self.REVIEW_QUEUE_MAX)
        self._rules_notified = None

    async def close_session(self):
        """收掉上一场（换场、重复开场）：名单写完盘，不再 observe，取消定时器和复核任务。"""
        self.enabled = False
        self._ready = False
        self._cancel_flush_timer()
        await self._stop_review()
        if self._open:
            await self.marks.flush_now()
        self._open = False
        self.audit = None

    async def begin(self, streamer):
        """开场（_begin_session 算出 streamer 之后）：词表、阈值、可信名单、关注名单都在这里载入；
        flood 状态、事件、名单按主播重置。返回要并进 session_start.comment_guard 的字典。
        从不抛：出任何错这一场就不启用守卫，弹幕照常显示。"""
        try:
            await self.close_session()
            self._token += 1
            self._reset_session()
            self.streamer = (streamer or "").strip().lower()
            self._env_off = env_disabled()
            self._session_enabled = bool(self.cfg["enabled"] and not self._env_off)
            self._open = True
            self.rules = self.engine = self.budget = None
            self.rules_fp = ()
            if not self._session_enabled:
                self.marks.clear()
                info = {"enabled": False}
                if self._env_off:
                    info["env_off"] = True
                return info
            loop = asyncio.get_running_loop()
            self.rules = await loop.run_in_executor(None, cg.load_rules)
            trusted = await loop.run_in_executor(None, cg.load_trusted, self.streamer)
            code = await self.marks.load(self.streamer)
            if code:
                self.notices.append(code)
            self.engine = cg.CommentGuard(self.rules, streamer=self.streamer, trusted=trusted)
            self.budget = cg.CostBudget(self.rules.thresholds)
            self.rules_fp = _rules_fingerprint()
            self._ready = True
            return self._session_start_record()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 留意规则载入出错，本场不启用：{}".format(exc))
            self._session_enabled = False
            self._ready = False
            self.enabled = False
            return {"enabled": False, "error": "begin_failed"}

    def bind(self, audit):
        """audit 建好之后调：记住本场自己的 audit，守卫从这一刻开始 observe。"""
        self.audit = audit
        self.enabled = bool(self._ready and self._open)

    async def end(self, owner_audit, broadcast=None):
        """收尾。owner_audit 是调用方会话自己的 audit：不是守卫这一场的就什么都不做（返回 None）——
        晚死的旧任务关不掉新一场的守卫，也拿不到新一场的计数（CLAUDE.md 第七条）。
        返回要并进 session_end.comment_guard 的字典；这一场守卫没开就返回 None。"""
        if not self._open or (owner_audit is not None and self.audit is not owner_audit):
            return None
        record = None
        try:
            was_on = self._session_enabled and self._ready
            self.enabled = False
            self._cancel_flush_timer()
            await self._stop_review()
            if was_on:
                if broadcast is not None:
                    await self.flush(broadcast, final=True)
                await self.marks.flush_now()
                record = self._session_end_record()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 留意收尾出错：{}".format(exc))
        self._open = False
        self._ready = False
        self.audit = None
        return record

    # ------------------------------------------------------------------------------------
    # 审计记录（只有计数和代码）
    # ------------------------------------------------------------------------------------
    def _session_start_record(self):
        rules = self.rules
        keep = ("src", "file", "section", "line", "reason", "n", "by_reason")
        return {"enabled": True, "backend": self.cfg["backend"], "model": None,
                "rules_hash": rules.hash, "rules_count": rules.count,
                "rules_pending": rules.pending_count,
                "warnings": [{k: w[k] for k in keep if k in w} for w in rules.warnings],
                "thresholds": dict(rules.thresholds), "prompt_hash": None, "bench_pass_hash": None}

    def _session_end_record(self):
        return {"counts": {c: dict(v) for c, v in sorted(self.counts.items())},
                "amber_by_reason": dict(sorted(self.amber_by_reason.items())),
                "observed": self.observed,
                "reviews": 0, "unreviewed": self.unreviewed, "review_invalid": 0, "review_agree": 0,
                "review_disagree": {}, "review_promoted": {"amber_to_red": 0, "note_to_amber": 0},
                "trips": 0, "budget_trips": self.budget.trips if self.budget else 0,
                "observe_ms_max": round(self.observe_ms_max, 1),
                "marked_auto": self.marked_auto, "marked_manual": self.marked_manual,
                "unmarked_auto": self.unmarked_auto, "dismissed": self.dismissed,
                "guard_msgs": self.guard_msgs}

    # ------------------------------------------------------------------------------------
    # 逐批判定（同步、纯 CPU，在 accept 之前）
    # ------------------------------------------------------------------------------------
    def observe(self, items, now=None):
        """给一批弹幕加上守卫的标注，返回新的列表（不改传进来的字典）。永不抛：整批出错就原样返回，
        单条出错这一条原样返回。`now` 是真实时间戳（刷屏窗口靠它过期，CLAUDE.md 第七条）。"""
        if not self.enabled or not isinstance(items, list):
            return items
        try:
            now = float(self._clock() if now is None else now)
            out = []
            for item in items:
                try:
                    out.append(self._observe_one(item, now))
                except Exception:
                    self.errors += 1
                    out.append(item)
            return out
        except Exception:
            self.errors += 1
            return items

    def _observe_one(self, item, now):
        if not isinstance(item, dict):
            return item
        cid, text = item.get("id"), item.get("text")
        if not isinstance(cid, str) or not cid or not isinstance(text, str):
            return item
        nick = item.get("user") if isinstance(item.get("user"), str) else ""
        nick = nick[:self.MAX_NICK]
        uid = _short(item.get("uid"), 96)
        uid_src = item.get("uid_src") if item.get("uid_src") in UID_SOURCES else None
        sec, handle = _short(item.get("sec"), 80), _short(item.get("handle"), 64)
        role = item.get("role") if item.get("role") in cg.ROLES_EXEMPT else None
        ok = bool(uid) and cg.uid_ok(uid_src)
        ukey = uid or (SESSION_PREFIX + nick if nick else None)
        keys = [k for k in (uid, sec, "h:" + handle.lower() if handle else None) if k]

        mono = self._mono()
        t0 = time.perf_counter()
        r = self.engine.judge(text, user=ukey, ts=now, cid=cid, role=role, mention=item.get("mention") is True,
                              structure_only=self.budget.structure_only(mono), handle=handle, keys=tuple(keys))
        ms = (time.perf_counter() - t0) * 1000.0
        self.budget.charge(mono, ms)
        if ms > self.observe_ms_max:
            self.observe_ms_max = ms
        if "dup_id" in r["tags"]:
            return item                      # 连接重放的历史评论：accept 也会按 id 丢掉
        self.observed += 1
        if ukey:
            self._remember_account(ukey, nick, sec, handle)
        flood = list(self.engine.last_flood)

        # accept 会按 (昵称, 文本) 60 秒去重：被它吞掉的重复，标注要打在最近一条已显示的那条上
        shown_key = (nick, text.strip()[:CommentTranslator.MAX_TEXT])
        prev = self._shown.get(shown_key)
        if prev is not None and now - prev["ts"] < CommentTranslator.DEDUPE_CONTENT_SEC:
            display_cid = prev["cid"]
        else:
            display_cid = cid
            self._shown[shown_key] = {"cid": cid, "ts": now}
            self._shown.move_to_end(shown_key)
            while len(self._shown) > self.SHOWN_MAX:
                self._shown.popitem(last=False)

        lvl = r["lvl"]
        cat = (r["cats"] or [None])[0]
        if lvl in SHOWN_LEVELS:
            row = self.counts.setdefault(cat or "other", {"alert": 0, "amber": 0, "note": 0})
            row[lvl] += 1
            if lvl == "amber":
                reason = r["amb"] or "other"
                self.amber_by_reason[reason] = self.amber_by_reason.get(reason, 0) + 1

        self._track_marks(r, keys, uid, uid_src, ok, role, sec, handle, nick, now)

        new = dict(item)
        if uid:
            new["uid_ok"] = ok
        guard = None
        if lvl in SHOWN_LEVELS:
            guard = {"lvl": lvl, "amb": r["amb"], "cats": list(r["cats"])[:4], "sub": r["sub"],
                     "tier": r["tier"], "src": "rule", "rv": None, "ev": None,
                     "why": list(r["why"])[:8]}
        sample = {"cid": display_cid, "user": nick, "text": text.strip()[:self.SAMPLE_TEXT],
                  "uid": uid, "uid_src": uid_src, "handle": handle}
        ev_id = self._upsert_events(r, flood, ukey, cid, sample, now)
        if guard is not None:
            guard["ev"] = ev_id
            new["guard"] = guard
            self._ann[cid] = guard
            self._ann.move_to_end(cid)
            while len(self._ann) > self.ANN_MAX:
                self._ann.popitem(last=False)
            if display_cid != cid and lvl in ("alert", "amber"):
                old = self._ann.get(display_cid)
                if old is None or cg.LVL_RANK[lvl] > cg.LVL_RANK[old["lvl"]]:
                    self._ann[display_cid] = guard
                    self._pending_cg[display_cid] = guard
        if r["route"]:
            self._queue_review(cid, r["route"], text, now)
        return new

    def _remember_account(self, ukey, nick, sec, handle):
        self._accounts[ukey] = {"nick": nick, "sec": sec, "handle": handle}
        self._accounts.move_to_end(ukey)
        while len(self._accounts) > self.ACCOUNTS_MAX:
            self._accounts.popitem(last=False)

    # ------------------------------------------------------------------------------------
    # 关注名单：高亮的人又发言了、自动加入
    # ------------------------------------------------------------------------------------
    def _track_marks(self, r, keys, uid, uid_src, ok, role, sec, handle, nick, now):
        marks = self.marks
        primary = marks.match(keys, nick if not uid else None)
        if primary is not None:
            # 名单里的人又发言了：last_seen / 条数只在内存里更新（页面自己按弹幕现算，刷新靠统计循环
            # 悄悄补进 config）；只有键或昵称有了新东西才发一条 guard_mark
            if marks.touch(primary, keys, nick, now):
                self._pending_marks[primary] = marks.entry(primary)
            return
        # 自动加入只认用户确认过的红条档词条的明确命中，且账号键稳定、不是主播/房管（I14）；
        # 链接、电话、刷屏、琥珀、模型意见一律不加人
        if not (ok and not role and cg.auto_markable(r, uid_src)):
            return
        add_keys = [uid] + ([sec] if sec and sec != uid else [])
        status, primary, evicted = marks.add(add_keys, nick, handle, by="auto", cat=(r["cats"] or [None])[0],
                                             ts=now)
        for gone in evicted:
            self.unmarked_auto += 1
            self._pending_marks[gone] = None
        if status == "added":
            self.marked_auto += 1
            self._pending_marks[primary] = marks.entry(primary)
            self._state_dirty = True

    def mark(self, key, on, nick="", handle=None, now=None):
        """中控在页面上加入/移出关注名单（只有本机页面发得出 guard_mark）。返回状态代码：
        ok / full / bad_key / disabled。"""
        if not self.enabled:
            return "disabled"
        key = clean_key(key)
        if key is None:
            return "bad_key"
        now = self._clock() if now is None else now
        if on:
            meta = self._accounts.get(key) or {}
            keys = [key]
            if meta.get("sec") and meta["sec"] != key:
                keys.append(meta["sec"])
            status, primary, evicted = self.marks.add(
                keys, _short(nick, self.MAX_NICK) or meta.get("nick") or "",
                _short(handle, 64) or meta.get("handle"), by="manual", ts=now)
            for gone in evicted:
                self.unmarked_auto += 1
                self._pending_marks[gone] = None
            if status == "full":
                return "full"
            if status == "bad_key":
                return "bad_key"
            if status in ("added", "upgraded"):
                self.marked_manual += 1
            self._pending_marks[primary] = self.marks.entry(primary)
        else:
            primary, entry = self.marks.remove(key)
            if primary is not None:
                if entry["by"] == "auto":
                    self.unmarked_auto += 1
                self._pending_marks[primary] = None
        self._state_dirty = True
        return "ok"

    def dismiss(self, ev_id):
        """抽屉里「忽略」一个事件：从事件表里拿掉，并告诉页面。"""
        if not self.enabled or not isinstance(ev_id, str) or ev_id not in self.events:
            return False
        del self.events[ev_id]
        self._ev_dirty.pop(ev_id, None)
        self._ev_sent.discard(ev_id)
        self.dismissed += 1
        self._pending_dismiss.append(ev_id)
        return True

    # ------------------------------------------------------------------------------------
    # 事件聚合（spec §6：同类事件 60 秒内合成一条，同一个 id 持续更新计数）
    # ------------------------------------------------------------------------------------
    def _upsert_events(self, r, flood, ukey, cid, sample, now):
        own = None
        by_kind = {}
        for fe in flood:
            by_kind[fe["kind"]] = fe
            if fe["kind"] == "brigade" and fe["lvl"] == "amber":
                eid = "b" + str(fe["ev"])
                accounts = [self._account_entry(k) for k in (fe.get("accounts") or ())]
                self._put_event(eid, {"kind": "brigade", "cat": "flood_brigade", "lvl": "amber",
                                      "amb": "brigade", "n": fe["n"], "accounts": accounts,
                                      "window_sec": fe["window"], "first_ts": fe.get("first_ts") or now,
                                      "ts": now, "sample": sample, "rv": None}, now, count_up=False)
                if (r["cats"] or [None])[0] == "flood_brigade":
                    own = eid
        lvl = r["lvl"]
        if lvl not in ("alert", "amber") or own is not None:
            return own
        cat = (r["cats"] or ["other"])[0] or "other"
        flood_kind = {"flood_user": "user_burst", "flood_repeat": "same_text"}.get(cat)
        fe = by_kind.get(flood_kind) if flood_kind else None
        who = ukey or "c:" + cid
        slot = (cat, who)
        idx = self._ev_index.get(slot)
        if idx is not None and now - idx[1] <= self.EVENT_MERGE_SEC and idx[0] in self.events:
            eid = idx[0]
        else:
            self._ev_seq += 1
            eid = "g%d" % self._ev_seq
        self._ev_index[slot] = (eid, now)
        if len(self._ev_index) > 4 * self.EVENT_MAX:
            self._ev_index = {k: v for k, v in self._ev_index.items() if now - v[1] <= self.EVENT_MERGE_SEC}
        fields = {"kind": "flood" if fe is not None else "comment", "cat": cat, "lvl": lvl, "amb": r["amb"],
                  "n": fe["n"] if fe is not None else 1, "accounts": [],
                  "window_sec": fe["window"] if fe is not None else int(self.EVENT_MERGE_SEC),
                  "first_ts": now, "ts": now, "sample": sample, "rv": None}
        self._put_event(eid, fields, now, count_up=fe is None)
        return eid

    def _put_event(self, eid, fields, now, count_up):
        ev = self.events.get(eid)
        if ev is None:
            ev = dict(fields, id=eid)
        else:
            n = ev["n"] + 1 if count_up else max(ev["n"], fields["n"])
            first = min(ev["first_ts"], fields["first_ts"])
            if cg.LVL_RANK[fields["lvl"]] >= cg.LVL_RANK[ev["lvl"]]:
                ev.update(fields)        # 档位不低于旧的：类别、理由、样例都换成最新这条
            else:
                ev["window_sec"] = fields["window_sec"]
            ev["n"], ev["first_ts"], ev["ts"] = n, first, now
        self.events[eid] = ev
        self.events.move_to_end(eid)
        self._ev_dirty[eid] = True
        while len(self.events) > self.EVENT_MAX:
            old, _ = self.events.popitem(last=False)
            self._ev_dirty.pop(old, None)
            self._ev_sent.discard(old)

    def _account_entry(self, ukey):
        meta = self._accounts.get(ukey) or {}
        nick = meta.get("nick") or (ukey[len(SESSION_PREFIX):] if ukey.startswith(SESSION_PREFIX) else "")
        return {"user": nick, "uid": ukey}

    # ------------------------------------------------------------------------------------
    # 复核（PR-5 接 Claude）：守卫自己的任务，flush 只往队列里放候选、从不等它（I2）
    # ------------------------------------------------------------------------------------
    def _queue_review(self, cid, route, text, now):
        if self.reviewer is None:
            self.unreviewed += 1
            return
        self._review_q.append({"cid": cid, "route": route, "text": text[:CommentTranslator.MAX_TEXT],
                               "ts": now, "token": self._token})

    def _ensure_review_task(self):
        if self.reviewer is None or not self._review_q:
            return
        if self._review_task is None or self._review_task.done():
            self._review_event = asyncio.Event()
            self._review_task = asyncio.ensure_future(self._review_loop())
        self._review_event.set()

    async def _stop_review(self):
        """取消复核任务并等它真的收掉（最多 1 秒）：永不返回的复核不能挂住收尾，也不留下悬着的任务。"""
        task = self._review_task
        self._review_task = None
        self._review_q.clear()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=1.0)

    async def _review_loop(self):
        """惰性启动，永不向外抛异常。结果回来时如果已经换场就丢掉；影子期只往旁边写一句模型意见
        （rv），不改红条/琥珀（spec §5.6 的子集，升降级放到 PR-5）。"""
        while True:
            try:
                while not self._review_q:
                    self._review_event.clear()
                    await self._review_event.wait()
                batch = [self._review_q.popleft() for _ in range(min(self.REVIEW_BATCH, len(self._review_q)))]
                token = self._token
                results = await self.reviewer.review(batch)
                if token != self._token or not isinstance(results, dict):
                    continue
                for cid, label in results.items():
                    self._apply_review(cid, label)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("[弹幕] 复核出错：{}".format(exc))
                await asyncio.sleep(1.0)

    def _apply_review(self, cid, label):
        current = self._ann.get(cid)
        if current is None or label not in RV_LABELS:
            return
        updated = dict(current, rv=label)
        self._ann[cid] = updated
        self._pending_cg[cid] = updated

    # ------------------------------------------------------------------------------------
    # 发出去
    # ------------------------------------------------------------------------------------
    def _pick_event(self):
        best, best_key = None, None
        for pos, eid in enumerate(self._ev_dirty):
            ev = self.events.get(eid)
            if ev is None:
                continue
            key = (eid in self._ev_sent, -cg.LVL_RANK[ev["lvl"]], pos)
            if best_key is None or key < best_key:
                best, best_key = eid, key
        return best

    def _collect(self, now, final=False):
        msgs = []
        for ev_id in self._pending_dismiss:
            msgs.append({"type": "guard_event", "ev": {"id": ev_id, "dismissed": True}})
        self._pending_dismiss = []
        for cid in list(self._pending_cg):
            if final or now - self._cg_sent.get(cid, float("-inf")) >= self.CID_MSG_GAP_SEC:
                g = self._pending_cg.pop(cid)
                msgs.append({"type": "comment_guard", "id": cid,
                             "guard": dict(g, cats=list(g.get("cats") or ()), why=list(g.get("why") or ()))})
                self._cg_sent[cid] = now
                self._cg_sent.move_to_end(cid)
                while len(self._cg_sent) > self.ANN_MAX:
                    self._cg_sent.popitem(last=False)
        for primary in list(self._pending_marks):
            msgs.append({"type": "guard_mark", "key": primary, "entry": self._pending_marks.pop(primary)})
        while self._ev_dirty and (final or now - self._last_ev_msg >= self.EVENT_MSG_GAP_SEC):
            eid = self._pick_event()
            if eid is None:
                self._ev_dirty.clear()
                break
            self._ev_dirty.pop(eid, None)
            self._ev_sent.add(eid)
            self._last_ev_msg = now
            msgs.append({"type": "guard_event", "ev": dict(self.events[eid])})
            if not final:
                break                   # 平时全局每秒至多 1 条；收尾才把剩下的一次发完
        if self._state_dirty and (final or now - self._last_state_msg >= self.STATE_MSG_GAP_SEC):
            self._state_dirty = False
            self._last_state_msg = now
            msgs.append({"type": "config", "guard_state": self.state()})
        return msgs

    def _wants_flush(self):
        return bool(self._pending_cg or self._pending_marks or self._ev_dirty or self._state_dirty
                    or self._pending_dismiss)

    async def flush(self, broadcast, final=False):
        """把攒着的守卫消息发出去（节流见类常量），再把复核候选交给复核任务。永不抛。
        没开、没有待发的消息就立刻返回，零开销。"""
        if not (self.enabled or final):
            return
        if not final and self._review_q:
            self._ensure_review_task()          # 候选交给复核任务（只是放进队列叫醒它，不等它）
        if not self._wants_flush():
            return
        try:
            self._broadcast = broadcast
            msgs = self._collect(self._clock(), final=final)
        except Exception as exc:
            print("[弹幕] 留意消息整理出错：{}".format(exc))
            return
        for msg in msgs:
            try:
                await broadcast(msg)
                self.guard_msgs += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("[弹幕] 留意消息发送出错：{}".format(exc))
        if not final and self._wants_flush():
            self._arm_flush_timer(self.CID_MSG_GAP_SEC)

    def _arm_flush_timer(self, delay):
        """被节流压住的消息，隔一秒再发一次，不必等下一批弹幕或下一跳统计。"""
        if self._flush_timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_timer = loop.call_later(delay, self._flush_timer_fire)

    def _flush_timer_fire(self):
        self._flush_timer = None
        if self.enabled and self._broadcast is not None:
            self._flush_task = asyncio.ensure_future(self.flush(self._broadcast))

    def _cancel_flush_timer(self):
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_timer = None


class CommentGuardMixin:
    """Pipeline 的弹幕留意接线。方法都不抛：守卫是附加功能，绝不能拖累弹幕、识别和审计。"""

    def _init_comment_guard(self):
        """Pipeline.__init__ 调：读设置、造守卫。默认关（PR-3）；关着时开场不载词表、不读写任何文件。"""
        guard = self.guard = GuardRuntime()
        guard.cfg = normalize_settings(load_settings().get(SETTINGS_KEY))
        if guard.effective_enabled():
            try:
                guard.preview_rules()
            except Exception as exc:
                print("[弹幕] 留意规则预载出错：{}".format(exc))
        config = self.server.config
        config["guard_state"] = guard.state()
        config["guard_events"] = {}
        config["guard_marked"] = {}

    async def _on_comment_items(self, items):
        """弹幕读取协程的 on_items：守卫在 accept 之前看一遍（accept 按 (昵称, 文本) 去重并截到
        300 字，挂在后面就看不到同人连发和第 300 字之后的链接、电话），再交给 accept 显示。"""
        guard = getattr(self, "guard", None)
        if guard is not None and guard.enabled:
            try:
                items = guard.observe(items, now=time.time())
            except Exception as exc:
                print("[弹幕] 留意规则出错: {}".format(exc))
        await self.comments.accept({"type": "viewer_comments", "items": items})
        if guard is not None:
            try:
                await guard.flush(self.server.broadcast)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("[弹幕] 留意消息发送出错: {}".format(exc))

    # ---- 换场 ----
    async def _begin_comment_guard(self, streamer):
        """_begin_session 里、建审计之前：载入规则和关注名单。返回并进 session_start 的字典。"""
        guard = getattr(self, "guard", None)
        if guard is None:
            return None
        return await guard.begin(streamer)

    async def _bind_comment_guard(self, audit):
        """审计建好之后：守卫记住本场自己的 audit 并开始 observe；把状态、事件表（清空）、名单发给页面。"""
        guard = getattr(self, "guard", None)
        if guard is None:
            return
        try:
            guard.bind(audit)
            if guard.enabled:
                rules = guard.rules
                print("[弹幕] 弹幕留意已启用：规则 {} 条生效（{} 条待确认），关注名单 {} 人".format(
                    rules.count, rules.pending_count, len(guard.marks)))
            elif guard.cfg["enabled"] and guard._env_off:
                print("[弹幕] 已用 {}=off 关闭弹幕留意".format(ENV_SWITCH))
            await self._publish_comment_guard_config()
            for code in guard.notices:
                text = guard_notice(code)
                print("[弹幕] " + text)
                await self.server.broadcast({"type": "notice", "text": text})
            guard.notices = []
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 留意启动出错：{}".format(exc))

    async def _publish_comment_guard_config(self, reset=True):
        """把状态（reset=True 时连同清空的事件表和这个主播的名单）写进 server.config 并广播；
        和已有的一致就不发（默认关时开场零消息）。收尾时 reset=False：只更新状态，页面上这一场的
        事件和名单留着，到下一场开场才换。"""
        guard = self.guard
        config = self.server.config
        new = {"guard_state": guard.state()}
        if reset:
            new["guard_events"] = {}
            new["guard_marked"] = dict(guard.marks.snapshot()) if guard.enabled else {}
        if all(config.get(k) == v for k, v in new.items()):
            return
        config.update(new)
        await self.server.broadcast(dict(new, type="config"))

    async def _end_comment_guard(self, my_audit):
        """_end_session 里、关审计之前：返回并进 session_end 的字典，或 None。
        只在「我还是当前会话」时才会被调用，守卫那边再按 audit 对一遍。"""
        guard = getattr(self, "guard", None)
        if guard is None:
            return None
        owner = my_audit if my_audit is not None else getattr(self, "audit", None)
        try:
            record = await guard.end(owner, broadcast=self.server.broadcast)
            if not guard._open:
                await self._publish_comment_guard_config(reset=False)
            return record
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 留意收尾出错: {}".format(exc))
            return None

    async def _guard_tick(self):
        """统计循环每一跳调一次：把被节流压住的消息发掉，查词表文件有没有被改。自带保护——
        _stats_loop 遇到异常会整个退出。"""
        guard = getattr(self, "guard", None)
        if guard is None:
            return
        try:
            await guard.flush(self.server.broadcast)
            if guard.enabled:
                # 名单里的人 last_seen / 条数的变化不广播（页面自己按弹幕现算），这里悄悄补进 config，
                # 刷新后的 hello 拿到的是新的
                self.server.config["guard_marked"] = dict(guard.marks.snapshot())
            await self._check_guard_rules_changed()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 留意定时检查出错：{}".format(exc))

    async def _check_guard_rules_changed(self):
        """直播中词表被改了：新内容要「停止→开始」后才生效（不热加载），每次变化说一次。
        改回本场载入的那份不提示。"""
        guard = self.guard
        if not guard.enabled:
            return
        fp = _rules_fingerprint()
        if fp == guard.rules_fp:
            guard._rules_notified = None
            return
        if fp == guard._rules_notified:
            return
        guard._rules_notified = fp
        text = guard_notice("rules_changed")
        print("[弹幕] " + text)
        await self.server.broadcast({"type": "notice", "text": text})

    # ---- 控制消息（只有本机页面发得出来；手机入站只认 retranslate）----
    async def _handle_guard_control(self, msg):
        guard = getattr(self, "guard", None)
        if guard is None:
            return None
        mtype = msg.get("type")
        try:
            if mtype == "guard_set":
                await self._guard_set(msg)
            elif mtype == "guard_mark":
                code = guard.mark(msg.get("key"), bool(msg.get("on")), nick=msg.get("nick") or "",
                                  handle=msg.get("handle"))
                if code == "full":
                    text = guard_notice("marks_full")
                    print("[弹幕] " + text)
                    await self.server.broadcast({"type": "notice", "text": text})
                await guard.flush(self.server.broadcast)
            elif mtype == "guard_dismiss":
                if guard.dismiss(msg.get("ev_id")):
                    await guard.flush(self.server.broadcast)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 处理 {} 出错: {}".format(mtype, exc))
        return None

    async def _guard_set(self, msg):
        """设置里的开关和模型复核选择。只在待机时改：直播中这一场的开关已经定了（同违禁词警示），
        设置分组在直播中也是整块隐藏的。只在改了的时候写一次 settings.json。`key` 先不处理（PR-5）。"""
        guard = self.guard
        if self._stream_active() or (guard._open and getattr(self, "audit", None) is not None):
            print("[弹幕] 直播进行中，弹幕留意的设置要停止后再改")
            return
        new = dict(guard.cfg)
        if isinstance(msg.get("enabled"), bool):
            new["enabled"] = msg["enabled"]
        if msg.get("backend") in BACKENDS:
            new["backend"] = msg["backend"]
        if isinstance(msg.get("weak"), bool):
            new["weak"] = msg["weak"]
        if new == guard.cfg:
            return
        guard.cfg = new
        self._save_setting(SETTINGS_KEY, dict(new))
        if guard.effective_enabled() and guard.rules is None:
            loop = asyncio.get_running_loop()
            guard.rules = await loop.run_in_executor(None, cg.load_rules)
        await self._publish_comment_guard_config(reset=False)       # 只是状态变了：上一场的事件和名单不动


def guard_notice(code):
    """提示文字（界面文字都在这里；守卫的其它地方只出代码）。只写观察到的事和能做的事。"""
    if code == "marks_corrupt":
        return L("关注名单文件读不了，已另存一份备份，先按空名单继续",
                 "The marked-viewers file couldn’t be read. A backup was saved and the list "
                 "starts empty.")
    if code == "marks_full":
        return L("关注名单已满（{} 人），先移出几位再加入",
                 "The marked-viewers list is full ({} viewers). Unmark someone first.").format(MAX_VIEWERS)
    if code == "rules_changed":
        return L("弹幕留意的规则文件已修改，点「停止」再「开始翻译」后生效",
                 "The Comment Review rules changed. Click Stop, then Start, to apply them.")
    return ""
