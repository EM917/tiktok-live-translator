# i18n: done
"""弹幕留意的接线（PR-3，spec §4.7、§6、§8）：把规则层（app/comment_guard.py）和关注名单
（app/comment_marks.py）挂到弹幕读取协程上。界面在 web/guard.js（PR-4）；`comment_guard.enabled` 从 PR-4
起默认开（只影响 settings.json 里没写过这个键的人；写过 false 的照旧关着）。

**对识别零影响靠结构保证，不靠运气**（spec I1/I2）：守卫只出现在这些地方——`on_items`（弹幕读取
协程）、开场/收尾（`_begin_session` / `_end_session`）、控制消息、统计循环。识别、违禁词检测、
审计的热路径（`asr_worker`、`translation_worker`、`_emit_original`、`_translate_and_update`）里
没有一处引用守卫，tests/test_comment_guard_pipeline.py 用 AST 钉着这一条。复核（PR-5 的 Claude Haiku 5.5，
app/comment_review.py）是守卫自己的 asyncio 任务，惰性启动、永不向外抛异常；`flush` 只往队列里放候选、从不等它。
复核只加不减（影子期）：能把琥珀升红、把 note/线索升成琥珀，不能撤红；模型升出来的红条不自动加入关注名单。

**永不抛、永不阻塞**（I4）：`observe()` 逐条 try/except，出任何错都把这一批原样交给 `accept`；
`flush()` 同理。守卫不碰通知和声音（I5）：这个模块不 import alert_notify / window_attention。

**消息**（spec §6）：规则判定随 `comment` 消息立即到（`guard` 字段）；之后只有标注变了才发
`comment_guard`（每个 cid 每秒至多 1 条）；`guard_event` 是聚合后的事件，全局每秒至多 1 条；
`guard_mark` 是名单的增删。三种消息都只发本机页面，手机同看一律不发（用户决定 8，app/viewer.py）。
`flush` 的收集和发送在一把锁里：评论读取、统计循环、控制消息、定时器都会调它，发送遇到慢页面会挂起，
不串行的话先收集的消息可能在后收集的之后才到页面。

**审计**（spec §8）：只记计数、不记弹幕原文、昵称、账号。`session_start.comment_guard` 在开场写，
`session_end.comment_guard` 在收尾写；两处都只用守卫在 `bind` 时拿到的**本场自己的** audit
（CLAUDE.md 第七条 my_audit）：晚到的旧任务关不掉新一场的守卫，也写不进新一场的审计。

界面文字只在文件末尾 `guard_notice` 里（关注名单损坏、读不了、存不进磁盘、名单已满、规则文件被改）；
别的地方只出代码。
"""
import asyncio
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

from . import comment_guard as cg
from . import comment_review as cr
from .comment_marks import MAX_VIEWERS, SESSION_PREFIX, MarkStore, clean_key
from .comments import CommentTranslator, account_fields
from .i18n import L
from .settings import load_settings

ENV_SWITCH = "TLT_COMMENT_GUARD"            # =off：应急总开关，界面坏了也能整体关掉
SETTINGS_KEY = "comment_guard"              # settings.json 里的键：{enabled, backend, weak}
BACKENDS = ("none", "claude")               # 用户决定 6：直播时只用 Claude Haiku 5.5，没有本地模型
# guard_reenable：熔断后「本场重新启用」（Claude 后端不占显存、不熔断，只有识别落后时的暂停，会自己恢复；
# 这条消息留着给以后的本地后端，现在收到了什么都不做）；
# guard_clear_auto：设置行里的「清空自动加入的」；打开规则文件夹是 open_guard_dir，在 pipeline.handle_control
GUARD_CONTROL_TYPES = frozenset(("guard_set", "guard_mark", "guard_dismiss", "guard_reenable",
                                 "guard_clear_auto"))
UID_SOURCES = ("id", "id_str", "sec_uid", "handle")
RV_LABELS = ("agree", "question", "complaint", "friendly", "neutral", "unsure", "skipped")
SHOWN_LEVELS = ("alert", "amber", "note")   # 带 guard 字段的档位；lead 只决定送不送模型


# --demo 的虚构弹幕流（停播时用来验界面；演示不连任何直播，主播、观众、账号全是虚构的）。
# 每条：(昵称, 账号, handle, 西语原文, 中文译文, 发完后等几秒)。几类情形都有：正常弹幕、号召举报（红条，
# 并自动加入关注名单，之后同一个人的弹幕会高亮）、没确认的词条（琥珀）、链接（红条）、多账号同一句（琥珀事件）。
# 账号 id 是 19 位的字符串，同真实账号一样超过 JS 的 2^53。内容是数据，不随界面语言变。
DEMO_STREAMER = "luna_demo"
DEMO_TRANSLATE_DELAY = 0.5          # 演示里「翻译中」停多久再补译文
DEMO_COMMENTS = (  # i18n: data
    ("Maria", "7100000000000000011", "maria_demo", "hola luna, cuánto cuesta el sérum?",
     "你好 Luna，这瓶精华多少钱？", 3.0),
    ("Caro", "7100000000000000012", "caro_demo", "ya compré dos y me llegaron rápido",
     "我已经买了两瓶，到货很快", 3.0),
    ("Rival Demo", "7100000000000000099", "rival_demo", "no compren, es una estafa, denuncien este live",
     "别买，这是骗局，大家举报这个直播间", 3.5),
    ("Lupita", "7100000000000000013", "lupita_demo", "es falso", "是假的", 3.0),
    ("Pedro", "7100000000000000014", "pedro_demo", "entra a mi live https://ejemplo.example.com/pedro",
     "来我的直播间 https://ejemplo.example.com/pedro", 3.5),
    ("Sofia", "7100000000000000015", "sofia_demo", "cuánto tarda el envío a Monterrey?",
     "寄到蒙特雷要多久？", 3.0),
    ("Rival Demo", "7100000000000000099", "rival_demo", "ya les dije lo que pienso", "我已经说了我的想法", 3.0),
    ("Cuenta A", "7100000000000000021", "cuenta_a_demo", "quiero uno ya mismo luna", "Luna，我现在就要一个", 1.2),
    ("Cuenta B", "7100000000000000022", "cuenta_b_demo", "quiero uno ya mismo luna", "Luna，我现在就要一个", 1.2),
    ("Cuenta C", "7100000000000000023", "cuenta_c_demo", "quiero uno ya mismo luna", "Luna，我现在就要一个", 1.2),
    ("Cuenta D", "7100000000000000024", "cuenta_d_demo", "quiero uno ya mismo luna", "Luna，我现在就要一个", 1.2),
    ("Cuenta E", "7100000000000000025", "cuenta_e_demo", "quiero uno ya mismo luna", "Luna，我现在就要一个", 3.0),
    ("Maria", "7100000000000000011", "maria_demo", "gracias luna, hasta pronto", "谢谢 Luna，回头见", 4.0),
)


def env_disabled():
    return os.environ.get(ENV_SWITCH, "").strip().lower() in ("off", "0", "false", "no")


def normalize_settings(raw):
    """settings.json 里的 comment_guard -> {enabled, backend, weak}。类型不对的键按默认：
    enabled 默认开（PR-4 起；没写过这个键的人，写了 false 的不变），backend 只认 BACKENDS。"""
    raw = raw if isinstance(raw, dict) else {}
    backend = raw.get("backend")
    enabled = raw.get("enabled")
    return {"enabled": enabled if isinstance(enabled, bool) else True,
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
    REVIEW_WINDOW_SEC = 2.0         # 攒批窗口：第一条候选进队后等这么久再一起送（凑满一批就不等）
    STOP_REVIEW_WAIT_SEC = 1.0      # 收尾时取消复核任务，最多等它这么久
    PREV_TEXT = 300                 # 同一个人的前一条弹幕，留这么多字（送审时才打码）
    PREV_USERS = 500

    def __init__(self, clock=time.time, mono=time.monotonic, marks=None, perf=time.perf_counter):
        self._clock = clock
        self._mono = mono
        self._perf = perf               # 量单条判定耗时的时钟；测试里换成假的，机器卡一下不会误触预算
        self.cfg = normalize_settings(None)
        self.marks = marks if marks is not None else MarkStore(clock=clock)
        self.reviewer = None            # 复核后端（comment_review.ClaudeReviewer）；None = 只用规则
        self.policy = cr.ReviewPolicy()     # 复核的限频、缓存、识别健康闸（纯逻辑，时钟由这里传）
        self.health_probe = None        # () -> (识别积压秒数, 识别是否在途)；Pipeline 在 _init_comment_guard 里接上
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
        self._flush_lock = None         # flush 的收集+发送串行化；在事件循环里惰性创建
        self._flush_lock_loop = None
        self._review_task = None
        self._review_event = None
        self._gate_seen = None          # 上次写进审计的复核闸门状态（(state, reason) 或 None），转折才再写
        self._reset_session()

    # ------------------------------------------------------------------------------------
    # 设置与状态
    # ------------------------------------------------------------------------------------
    def effective_enabled(self):
        if self._open:
            return self._session_enabled
        return self.cfg["enabled"] and not env_disabled()

    def status_code(self):
        """config.guard_state.status：只发代码，文案在前端。选了 Claude：没有密钥 no_key；识别落后时 paused；
        后端自己的状态（cooldown / key_rejected / model_missing / offline / invalid，见 ClaudeReviewer.status）；
        其余 claude。选「不用模型」是 rules。"""
        return self._status()[0]

    def _status(self):
        """(状态代码, 要并进 guard_state 的附带数字)。附带的键只在对应状态下才有：lag_sec（paused）、
        http（key_rejected）、wait_min（cooldown），平时 guard_state 的键集不变。"""
        if env_disabled():
            return "env_off", {}
        if not self.effective_enabled():
            return "off", {}
        if self.cfg["backend"] != "claude":
            return "rules", {}
        reviewer = self.reviewer
        if reviewer is None:
            return "no_key", {}
        if self.policy.paused:
            return "paused", {"lag_sec": int(round(self.policy.lag_sec))}
        status = getattr(reviewer, "status", None)
        if status is not None:
            code, extra = status(self._clock())
            if code != "claude":
                return code, extra
        return "claude", {}

    def state(self):
        status, extra = self._status()
        enabled = self.effective_enabled()
        if (status, enabled) != self._last_status:
            self._last_status = (status, enabled)
            self._since = self._clock()
        rules = None
        if self.rules is not None:
            rules = {"count": self.rules.count,
                     "warnings": sum(w.get("n", 1) if w.get("reason") == "more" else 1
                                     for w in self.rules.warnings)}
        out = {"enabled": enabled, "backend": self.cfg["backend"], "status": status,
               "since": self._since, "trip": None, "rules": rules,
               "n_marked": len(self.marks), "unreviewed": self.unreviewed,
               "weak": self.cfg["weak"],
               # 设置行里「关注名单 · 主播」写的是哪个主播：待机时就是上一场载入的那份；关着的那一场没有名单，空串
               "streamer": self.marks.streamer or ""}
        out.update(extra)
        return out

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
        self._review_q = cr.ReviewQueue(self.policy.QUEUE_MAX)
        self._inflight = []                  # 正在问模型的那一批候选（收尾时还没回来的，要记成「未复核」）
        self.policy.reset()
        self._prev = OrderedDict()           # 账号键 -> (时刻, 上一条弹幕原文)：送审时带同一个人的前一条做上下文
        self._gate_seen = None
        self.reviews = self.review_invalid = self.review_agree = 0
        self.review_disagree = {}
        self.review_promoted = {"amber_to_red": 0, "note_to_amber": 0}
        self._rules_notified = None
        self._marks_fail_told = False

    async def close_session(self):
        """收掉上一场（换场、重复开场）：名单写完盘，不再 observe，取消定时器和复核任务。"""
        self.enabled = False
        self._ready = False
        self._cancel_flush_timer()
        await self._stop_review()
        await self._close_reviewer()
        await self.marks.flush_now()        # 待机时在设置行里改过、当时没写成的，换场前再试一次，不让载入把它丢掉
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
            self.configure_reviewer()
            if self.reviewer is not None and hasattr(self.reviewer, "new_session"):
                self.reviewer.new_session()
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

    def _is_mine(self, token, owner_audit):
        """end() 的每个 await 之后都要问一句：这一场还是我入口时的那一场吗？换场（begin）会把
        token 加一、audit 换成新的；晚到的旧 end() 看到不是了，就不许再碰运行态（CLAUDE.md 第七条）。"""
        return self._open and self._token == token and (owner_audit is None or self.audit is owner_audit)

    async def end(self, owner_audit, broadcast=None):
        """收尾。owner_audit 是调用方会话自己的 audit：不是守卫这一场的就什么都不做（返回 None）——
        晚死的旧任务关不掉新一场的守卫，也拿不到新一场的计数（CLAUDE.md 第七条）。
        返回要并进 session_end.comment_guard 的字典；这一场守卫没开就返回 None。

        入口时在任何 await 之前先记下这一场的 token 和计数；每个 await 之后重新核对。收尾期间下一场
        已经开始（_stop_locked 的 3 秒宽限过了）：这个旧 end() 只把入口时记下的、属于旧一场的计数
        交回去，不碰 _open / _ready / audit / enabled，也不替下一场发消息、写名单。"""
        if not self._open or (owner_audit is not None and self.audit is not owner_audit):
            return None
        token = self._token
        was_on = self._session_enabled and self._ready
        record = self._session_end_record() if was_on else None
        try:
            self.enabled = False
            self._cancel_flush_timer()
            await self._stop_review()
            if was_on and self._is_mine(token, owner_audit):
                record = self._session_end_record()     # 收尾时还排着、还在问的候选这时已记成「未复核」
            await self._close_reviewer()
            if was_on and self._is_mine(token, owner_audit):
                if broadcast is not None:
                    await self.flush(broadcast, final=True, token=token)
                if self._is_mine(token, owner_audit):
                    await self.marks.flush_now()
                if self._is_mine(token, owner_audit):
                    self.poll_marks_health()
                    record = self._session_end_record()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 留意收尾出错：{}".format(exc))
        if not self._is_mine(token, owner_audit):
            return record                # 已经换场：新一场的运行态不是我的
        self._open = False
        self._ready = False
        self.audit = None
        return record

    def poll_marks_health(self):
        """关注名单写盘失败：每一段失败期只说一次（写成功了再失败才再说）。终端在打包后的程序里看不到，
        所以进 notices，由接线层发给页面。"""
        if self.marks.failing:
            if not self._marks_fail_told:
                self._marks_fail_told = True
                self.notices.append("marks_write_failed")
        else:
            self._marks_fail_told = False

    # ------------------------------------------------------------------------------------
    # 审计记录（只有计数和代码）
    # ------------------------------------------------------------------------------------
    def _session_start_record(self):
        rules = self.rules
        keep = ("src", "file", "section", "line", "reason", "n", "by_reason")
        model = prompt_hash = None
        if self.cfg["backend"] == "claude":
            # 选了 Claude：记下打算用的模型和提示词指纹（没有密钥时后端是 None，照样记，事后答得出「当时用的哪一版」）
            reviewer = self.reviewer
            model = getattr(reviewer, "model", None) or cr.guard_model()
            prompt_hash = getattr(reviewer, "prompt_hash", None) or cr.load_prompt()[1]
        return {"enabled": True, "backend": self.cfg["backend"], "model": model,
                "rules_hash": rules.hash, "rules_count": rules.count,
                "rules_pending": rules.pending_count,
                "warnings": [{k: w[k] for k in keep if k in w} for w in rules.warnings],
                "thresholds": dict(rules.thresholds), "prompt_hash": prompt_hash, "bench_pass_hash": None}

    def _session_end_record(self):
        return {"counts": {c: dict(v) for c, v in sorted(self.counts.items())},
                "amber_by_reason": dict(sorted(self.amber_by_reason.items())),
                "observed": self.observed,
                "reviews": self.reviews, "unreviewed": self.unreviewed, "review_invalid": self.review_invalid,
                "review_agree": self.review_agree, "review_disagree": dict(sorted(self.review_disagree.items())),
                "review_promoted": dict(self.review_promoted),
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
        t0 = self._perf()
        r = self.engine.judge(text, user=ukey, ts=now, cid=cid, role=role, mention=item.get("mention") is True,
                              structure_only=self.budget.structure_only(mono), handle=handle, keys=tuple(keys))
        ms = (self._perf() - t0) * 1000.0
        self.budget.charge(mono, ms)
        if ms > self.observe_ms_max:
            self.observe_ms_max = ms
        if "dup_id" in r["tags"]:
            return item                      # 连接重放的历史评论：accept 也会按 id 丢掉
        try:
            prev_text = self._take_prev(ukey, text, now)
        except Exception:
            prev_text = None
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
            try:
                self._queue_review(guard, r, cid, display_cid, text, now, ukey, sample, prev_text)
            except Exception as exc:
                # 复核这一路出任何错，都不能让规则已经做好的标注丢掉（外层的 except 会把这条原样放行，
                # 红条就没了）：只记一笔
                self.errors += 1
                print("[弹幕] 复核候选出错：{}".format(exc))
        return new

    def _take_prev(self, ukey, text, now):
        """同一个人 2 分钟内的前一条弹幕原文（送审时的上下文，打码在送审时做），并记下这一条给下一次用。"""
        if not ukey:
            return None
        old = self._prev.get(ukey)
        self._prev[ukey] = (now, text[:self.PREV_TEXT])
        self._prev.move_to_end(ukey)
        while len(self._prev) > self.PREV_USERS:
            self._prev.popitem(last=False)
        if old is not None and 0 <= now - old[0] <= cr.PREV_MAX_SEC:
            return old[1]
        return None

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
        ok / full / bad_key / disabled。待机时（没有在场）只能移出，改的是上一场载入的那份名单。"""
        if not self.enabled and (on or not self._marks_editable()):
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

    def _marks_editable(self):
        """名单现在能不能改：直播中（守卫开着）能；待机时也能——设置行里的「移出」「清空自动加入的」
        改的是上一场载入的那份（它还在内存里，主播名也合规）。关着的那一场（名单已清空）改不了。"""
        return self.enabled or (not self._open and self.marks.streamer is not None)

    def clear_auto(self):
        """设置行里的「清空自动加入的」：移出所有 by=auto 的人，手动加入的不动。返回移出了几个。"""
        if not self._marks_editable():
            return 0
        gone = self.marks.clear_auto()
        for primary in gone:
            self.unmarked_auto += 1
            self._pending_marks[primary] = None
        if gone:
            self._state_dirty = True
        return len(gone)

    def reenable(self):
        """熔断后的「本场重新启用」。Claude 后端不熔断（它不占显存，识别落后只暂停、自己恢复），所以现在没有
        东西可重新启用，什么都不做；消息和接口留着，给以后有会熔断的后端用。"""
        return False

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
    # 复核（PR-5，Claude Haiku 5.5）：守卫自己的任务，flush 只往队列里放候选、从不等它（I2）
    # ------------------------------------------------------------------------------------
    def configure_reviewer(self):
        """按设置和当前密钥备好复核后端：选了 Claude 且有密钥就造一个（同一把密钥沿用，它记着这把密钥被拒过），
        没有密钥就是 None（状态 no_key）。选「不用模型」时撤掉自己造的；外面塞进来的（测试里的替身）不碰。"""
        mine = isinstance(self.reviewer, cr.ClaudeReviewer)
        if self.cfg["backend"] != "claude":
            if mine:
                self.reviewer = None
            return
        if self.reviewer is not None and not mine:
            return
        self.reviewer = cr.make_reviewer(clock=self._clock, existing=self.reviewer if mine else None)

    async def _close_reviewer(self):
        close = getattr(self.reviewer, "close", None)
        if close is not None:
            try:
                await asyncio.wait_for(close(), timeout=2.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        self.policy.paused = False          # 这一场结束了，暂停状态不留到待机界面上

    def _probe_health(self):
        """(识别积压秒数, 识别是否在途)。探针没接上、或出错，就当识别没有积压。"""
        probe = self.health_probe
        if probe is None:
            return 0.0, False
        try:
            backlog, inflight = probe()
            return float(backlog or 0.0), bool(inflight)
        except Exception:
            return 0.0, False

    def _poll_health(self, t):
        """识别落后时暂停送审，回到 ok 满 30 秒再恢复（spec §5.8 云端）。规则照常。"""
        if self.reviewer is None or self.cfg["backend"] != "claude":
            return
        backlog, _ = self._probe_health()
        if self.policy.update_health(t, backlog):
            self._state_dirty = True
        self._sync_gate(t, backlog)

    def _gate_label(self, t):
        """现在复核被什么挡着：(审计里的 state, reason)，没挡着是 None。"""
        if self.policy.paused:
            return "paused", "lagging"
        blocked = getattr(self.reviewer, "blocked", None)
        code = blocked(t) if blocked is not None else None
        return {None: None, "cooldown": ("paused", "rate_limit"), "offline": ("paused", "offline"),
                "key_rejected": ("locked", "key_rejected"), "model_missing": ("locked", "model_missing"),
                "prompt_missing": ("locked", "prompt_missing"),
                "invalid": ("locked", "invalid")}.get(code, ("paused", code))

    def _sync_gate(self, t, backlog=None):
        """闸门状态转折才写一条审计 comment_guard_gate，并让页面上的状态胶囊更新。"""
        label = self._gate_label(t)
        if label == self._gate_seen:
            return
        before, self._gate_seen = self._gate_seen, label
        self._state_dirty = True
        if backlog is None:
            backlog = self._probe_health()[0]
        if label is None:
            self._audit("comment_guard_gate", "claude", "resumed", reason=before[1], backlog_sec=round(backlog, 1))
        else:
            self._audit("comment_guard_gate", "claude", label[0], reason=label[1], backlog_sec=round(backlog, 1))

    def _audit(self, method, *args, **kwargs):
        """往本场自己的审计里写一条（my_audit）。审计出错不影响守卫。"""
        fn = getattr(self.audit, method, None)
        if fn is None:
            return
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            print("[弹幕] 复核审计写入出错：{}".format(exc))

    def _queue_review(self, guard, r, cid, disp, text, now, ukey, sample, prev):
        """规则层给了 route 的候选：放进队列（或记「未复核」）。guard 是刚给这条弹幕做的标注（note 以上才有），
        在 comment 消息发出去之前就地改它的 rv，不用再发一条消息。"""
        prio = cr.priority(r)
        if prio is None:
            return
        entry = {"cid": cid, "disp": disp, "text": text, "prev": prev, "ts": now, "queued": self._clock(),
                 "token": self._token, "prio": prio, "route": r["route"], "lvl": r["lvl"], "amb": r["amb"],
                 "cats": list(r["cats"]), "sub": r["sub"], "tier": r["tier"], "why": list(r["why"])[:8],
                 "hint": cr.hint_of(r["cats"], r["sub"]), "flags": cr.flags_of(r), "sample": sample, "ukey": ukey}
        if self.reviewer is None:
            self._skip_review(entry, fresh=guard)
            return
        self._poll_health(entry["queued"])
        if self._gate_label(entry["queued"]) is not None or not self.policy.user_allowed(ukey, now):
            self._skip_review(entry, fresh=guard)
            return
        evicted = self._review_q.push(entry)
        if guard is not None and guard.get("lvl") == "amber" and all(e is not entry for e in evicted):
            guard["rv"] = "pending"                 # 复核中…（随 comment 消息一起到，不另发）
        for gone in evicted:
            self._skip_review(gone, fresh=guard if gone is entry else None)

    def tick_review(self):
        """统计循环每一跳调一次：识别健康闸、冷却到期这些随时间变的状态，没有候选进来时也要更新。"""
        if self.enabled and self.reviewer is not None and self.cfg["backend"] == "claude":
            t = self._clock()
            self._poll_health(t)
            self._sync_gate(t)

    def _skip_review(self, entry, fresh=None):
        """这条候选没能得到模型意见：计数；选了 Claude 的话，显示出来的那一条写「未经模型复核」。
        选「不用模型」的人不需要被提醒没复核（rv 不动）。fresh 是刚做的标注（还没发出去）：就地改，不另发。"""
        self.unreviewed += 1
        if self.cfg["backend"] != "claude":
            return
        self._state_dirty = True
        if fresh is not None:
            if fresh.get("lvl") in ("alert", "amber"):
                fresh["rv"] = "skipped"
            return
        cur = self._ann.get(entry["disp"])
        if cur is not None and cur.get("lvl") in ("alert", "amber") and cur.get("rv") in (None, "pending"):
            self._set_ann(entry["disp"], dict(cur, rv="skipped"))

    def _set_ann(self, tid, guard):
        self._ann[tid] = guard
        self._ann.move_to_end(tid)
        self._pending_cg[tid] = guard

    def _ensure_review_task(self):
        if self.reviewer is None or not self._review_q:
            return
        if self._review_task is None or self._review_task.done():
            self._review_event = asyncio.Event()
            self._review_task = asyncio.ensure_future(self._review_loop())
        self._review_event.set()

    async def _stop_review(self):
        """取消复核任务并等它真的收掉（最多 1 秒）：永不返回的复核不能挂住收尾，也不留下悬着的任务。
        还排在队里的、正在问模型还没回来的候选一律记成「未复核」（计数 + 弹幕上的「模型复核中…」改成
        「未经模型复核」）：没拿到模型意见的弹幕，合规计数不能少报。"""
        task = self._review_task
        self._review_task = None
        for entry in self._review_q.drain():
            self._skip_review(entry)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=self.STOP_REVIEW_WAIT_SEC)
        # 任务按时收掉的话，它自己已经记过（_skip_inflight）；没按时收掉（卡住、吞了取消）的，这里替它记
        batch, self._inflight = self._inflight, []
        for entry in batch:
            self._skip_review(entry)

    def _skip_inflight(self, batch):
        """正在问的这一批没有结果：记成「未复核」。只记一次（_stop_review 先替它记过的，这里不再重复）。"""
        if self._inflight is not batch:
            return
        self._inflight = []
        for entry in batch:
            self._skip_review(entry)

    async def _review_loop(self):
        """惰性启动，永不向外抛异常。攒 2 秒（或凑满一批）再一起送；结果回来时如果已经换场就丢掉。"""
        while True:
            try:
                while not self._review_q:
                    self._review_event.clear()
                    await self._review_event.wait()
                if self.REVIEW_WINDOW_SEC > 0 and len(self._review_q) < self.policy.batch_limit:
                    await asyncio.sleep(self.REVIEW_WINDOW_SEC)
                await self._review_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("[弹幕] 复核出错：{}".format(exc))
                await asyncio.sleep(1.0)

    async def _take_batch(self, t):
        """从队列里取一批要问模型的候选。排太久的、打码之后没什么可判的、同句缓存里已经有答案的，当场处理；
        送出去的内容一模一样的（cr.review_key：打码文本、hint、flags、前一条都相同）只问一次，其余的跟着用这一条的答案。
        返回 (要问的, {cid: 跟着用答案的候选})。
        每条之间让出事件循环：打码一条约 1 毫秒，队列满时一口气几十条，别占着循环（音频读取也在上面）。"""
        leaders, followers, seen = [], {}, {}
        try:
            while self._review_q and len(leaders) < self.policy.batch_limit:
                await asyncio.sleep(0)
                entry = self._review_q.pop_first()
                if entry is None:
                    break
                if entry["token"] != self._token:
                    continue
                if t - entry["queued"] > self.policy.MAX_AGE_SEC:
                    self._skip_review(entry)
                    continue
                masked = cr.mask(entry["text"])
                if not cr.reviewable(masked):
                    self._skip_review(entry)
                    continue
                entry["masked"] = masked
                entry["prev_masked"] = cr.mask(entry["prev"]) if entry.get("prev") else None
                # 键 = 模型看到的一切（见 cr.review_key），不是 fold 之后的文本：问句和断言不能共用一个判定
                entry["key"] = key = cr.review_key(masked, entry["hint"], entry["flags"], entry["prev_masked"])
                cached = self.policy.cache_get(key, t)
                if cached is not None:
                    self._apply_verdict(entry, cached)
                    continue
                if key in seen:
                    followers.setdefault(seen[key]["cid"], []).append(entry)
                    continue
                seen[key] = entry
                leaders.append(entry)
        except asyncio.CancelledError:
            # 收尾时取消：已经从队里取出来、还没送出去的，记成「未复核」（让出事件循环只发生在循环开头，
            # 手里没有拿着一半的候选）
            for entry in leaders + [e for group in followers.values() for e in group]:
                self._skip_review(entry)
            raise
        return leaders, followers

    async def _review_once(self):
        t = self._clock()
        self._poll_health(t)
        leaders, followers = await self._take_batch(t)
        if not leaders:
            return
        everyone = leaders + [e for group in followers.values() for e in group]
        if self._gate_label(t) is not None or not self.policy.call_allowed(t):
            for entry in everyone:
                self._skip_review(entry)
            for entry in self._review_q.drain():
                self._skip_review(entry)
            return
        self.policy.note_call(t)
        token = self._token
        waited_ms = int((t - min(e["queued"] for e in leaders)) * 1000)
        t0 = time.perf_counter()
        self._inflight = everyone
        try:
            result = await self.reviewer.review(leaders)
        except asyncio.CancelledError:
            if token == self._token:
                self._audit_review(None, len(leaders), int((time.perf_counter() - t0) * 1000), "cancelled", waited_ms)
                self._skip_inflight(everyone)       # 收尾 / 换场取消：这一批没有拿到模型意见
            raise
        except Exception as exc:
            # 后端自己的 bug（真后端永不抛）：这一批记「未复核」，别让「复核中…」一直挂在界面上
            print("[弹幕] 复核后端出错：{}".format(exc))
            if token == self._token:
                self._skip_inflight(everyone)
            return
        finally:
            # 回来了（或出错了）：不再是「在途」。_inflight 已经不是这一批：收尾的 _stop_review 等不及（后端没理会
            # 取消）已经替它们记过「未复核」，晚到的结果就不再算数，免得同一条既算未复核又算已复核
            still_mine = self._inflight is everyone
            if still_mine:
                self._inflight = []
        if token != self._token or not still_mine:
            return                              # 换场了、或这一批已被收尾记成未复核：晚到的结果不写
        t = self._clock()
        if not isinstance(result, cr.ReviewResult):
            # 只给一句模型意见的简单后端（{cid: rv 代码}）：照单写到旁边，不改档位
            result = result if isinstance(result, dict) else {}
            for entry in everyone:
                if entry["cid"] in result:
                    self._apply_verdict(entry, result[entry["cid"]])
                else:
                    self._skip_review(entry)
            return
        self.reviews += 1
        if cr.counts_as_invalid(result):
            self.review_invalid += 1
        self._audit_review(result, len(leaders), result.ms, result.outcome, waited_ms)
        self.policy.note_max_tokens_streak(getattr(self.reviewer, "max_tokens_streak", 0))
        self._sync_gate(t)
        self._state_dirty = True
        for entry in leaders:
            verdict = result.get(entry["cid"])
            group = [entry] + followers.get(entry["cid"], [])
            if verdict is None:
                for member in group:
                    self._skip_review(member)
                continue
            self.policy.cache_put(entry["key"], verdict, t)
            for member in group:
                self._apply_verdict(member, verdict)

    def _audit_review(self, result, n, ms, outcome, waited_ms):
        """一次模型调用写一条审计（只有计数和代码，没有弹幕内容）。每分钟至多 10 次由 ReviewPolicy 保证。"""
        backlog, inflight = self._probe_health()
        self._audit("comment_guard_review", "claude", getattr(self.reviewer, "model", None), n, ms, outcome,
                    refusal_category=getattr(result, "refusal_category", None), backlog_sec=round(backlog, 1),
                    asr_inflight=inflight, waited_ms=waited_ms, in_tokens=getattr(result, "in_tokens", None),
                    out_tokens=getattr(result, "out_tokens", None),
                    cache_read_tokens=getattr(result, "cache_read_tokens", None))

    # ---- 把模型的判定写到标注上（合并规则在 comment_review.decide，这里只落地）----
    def _apply_review(self, cid, label):
        """只写一句模型意见（rv），不改档位。简单后端和测试用。"""
        current = self._ann.get(cid)
        if current is None or label not in RV_LABELS:
            return
        self._set_ann(cid, dict(current, rv=label))

    def _apply_verdict(self, entry, verdict):
        if isinstance(verdict, str):
            self._apply_review(entry["disp"], verdict)
            return
        d = cr.decide(entry, verdict)
        if d.agree:
            self.review_agree += 1
        if d.disagree:
            self.review_disagree[d.disagree] = self.review_disagree.get(d.disagree, 0) + 1
        tid = entry["disp"]
        cur = self._ann.get(tid)
        evidence = ""
        if d.evidence and verdict.evidence:
            evidence = entry["masked"].evidence_in(verdict.evidence) or ""
        if entry["route"] == "confirm":
            if cur is None:
                return
            new = dict(cur, rv=d.rv)
            if evidence:
                new["evidence"] = evidence
            if d.promote == "amber_to_red" and cg.LVL_RANK[cur["lvl"]] < cg.LVL_RANK["alert"]:
                new.update(lvl="alert", amb=None, src="review")
                self.review_promoted["amber_to_red"] += 1
                self._recount(cur, new, "amber_to_red")
            self._set_ann(tid, new)
            self._sync_event(new)
            return
        if d.promote != "note_to_amber":
            return                              # 非攻击、拿不准：保持隐藏
        if cur is not None and cg.LVL_RANK[cur["lvl"]] >= cg.LVL_RANK["amber"]:
            return                              # 这一条已经被别的原因标成琥珀以上：不降也不重复计
        new = {"lvl": "amber", "amb": "review", "cats": d.cats, "sub": entry["sub"], "tier": entry["tier"],
               "src": "review", "rv": None, "ev": None, "why": list(entry["why"])[:8]}
        if evidence:
            new["evidence"] = evidence
        self.review_promoted["note_to_amber"] += 1
        self._recount(cur, new, "note_to_amber")
        self._ev_seq += 1
        eid = "g%d" % self._ev_seq
        self._put_event(eid, {"kind": "comment", "cat": d.cats[0], "lvl": "amber", "amb": "review", "n": 1,
                              "accounts": [], "window_sec": int(self.EVENT_MERGE_SEC), "first_ts": entry["ts"],
                              "ts": entry["ts"], "sample": entry["sample"], "rv": None}, entry["ts"], count_up=False)
        new["ev"] = eid
        self._set_ann(tid, new)

    def _recount(self, old, new, kind):
        """模型改了档位：session_end 里的 counts / amber_by_reason 跟着改，写的是操作员最后看到的样子。"""
        old_cat = ((old or {}).get("cats") or [None])[0] or "other"
        new_cat = (new.get("cats") or [None])[0] or "other"
        if kind == "amber_to_red":
            row = self.counts.setdefault(old_cat, {"alert": 0, "amber": 0, "note": 0})
            row["amber"] = max(0, row["amber"] - 1)
            row["alert"] += 1
            reason = (old or {}).get("amb") or "other"
            if self.amber_by_reason.get(reason):
                self.amber_by_reason[reason] -= 1
                if not self.amber_by_reason[reason]:
                    del self.amber_by_reason[reason]
            return
        if old is not None and old.get("lvl") == "note":
            row = self.counts.setdefault(old_cat, {"alert": 0, "amber": 0, "note": 0})
            row["note"] = max(0, row["note"] - 1)
        row = self.counts.setdefault(new_cat, {"alert": 0, "amber": 0, "note": 0})
        row["amber"] += 1
        self.amber_by_reason["review"] = self.amber_by_reason.get("review", 0) + 1

    def _sync_event(self, guard):
        """标注的档位和模型意见变了：它所属的事件跟着变，抽屉和顶部条才对得上。"""
        ev = self.events.get(guard.get("ev"))
        if ev is None:
            return
        ev["rv"] = guard.get("rv")
        if guard.get("lvl") == "alert" and ev["lvl"] != "alert":
            ev["lvl"], ev["amb"] = "alert", None
        self._ev_dirty[ev["id"]] = True

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

    def _get_flush_lock(self):
        loop = asyncio.get_running_loop()
        if self._flush_lock is None or self._flush_lock_loop is not loop:
            self._flush_lock = asyncio.Lock()
            self._flush_lock_loop = loop
        return self._flush_lock

    async def flush(self, broadcast, final=False, token=None):
        """把攒着的守卫消息发出去（节流见类常量），再把复核候选交给复核任务。永不抛。
        没开、没有待发的消息就立刻返回，零开销。

        收集和发送是一把锁里的一件事：评论读取、统计循环、控制消息、定时器都会调进来，发送遇到慢页面
        会挂起——不串行的话，先收集的消息可能在后收集的之后才发出去（名单的「加入」排在「移出」之后到页面，
        同一条弹幕的旧标注盖掉新标注）。token 是调用方入口时那一场的编号：等锁期间换了场就不替下一场发。"""
        if not (self.enabled or final):
            return
        if not final and self._review_q:
            self._ensure_review_task()          # 候选交给复核任务（只是放进队列叫醒它，不等它）
        if not self._wants_flush():
            return
        async with self._get_flush_lock():
            if token is not None and token != self._token:
                return
            if not (self.enabled or final) or not self._wants_flush():
                return                          # 等锁的时候别人已经替我发完了，或者这一场收尾了
            session = self._token
            try:
                self._broadcast = broadcast
                msgs = self._collect(self._clock(), final=final)
            except Exception as exc:
                print("[弹幕] 留意消息整理出错：{}".format(exc))
                return
            for msg in msgs:
                try:
                    await broadcast(msg)
                    if self._token == session:
                        self.guard_msgs += 1    # 发送期间换场了：这条不算进新一场的计数
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
        """Pipeline.__init__ 调：读设置、造守卫。默认开（PR-4 起）；关着时开场不载词表、不读写任何文件。"""
        guard = self.guard = GuardRuntime()
        guard.cfg = normalize_settings(load_settings().get(SETTINGS_KEY))
        # 识别落后时暂停送审（只读两个属性；Telemetry 和 _asr_inflight 在 Pipeline.__init__ 里先于这里建好）
        guard.health_probe = lambda: (getattr(self.telemetry, "audio_backlog_sec", 0.0),
                                      getattr(self, "_asr_inflight", None) is not None)
        guard.configure_reviewer()
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
            await self._send_guard_notices()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 留意启动出错：{}".format(exc))

    async def _send_guard_notices(self):
        """守卫攒下的提示代码 -> 文字 -> 终端和页面各一份，发完清掉。"""
        guard = self.guard
        codes, guard.notices = guard.notices, []
        for code in codes:
            text = guard_notice(code)
            print("[弹幕] " + text)
            await self.server.broadcast({"type": "notice", "text": text})

    async def _publish_comment_guard_config(self, reset=True):
        """把状态（reset=True 时连同清空的事件表和这个主播的名单）写进 server.config 并广播；
        和已有的一致就不发（守卫关着时开场零消息）。收尾时 reset=False：只更新状态，页面上这一场的
        事件和名单留着，到下一场开场才换。"""
        guard = self.guard
        config = self.server.config
        new = {"guard_state": guard.state()}
        if reset:
            new["guard_events"] = {}
            new["guard_marked"] = dict(guard.marks.snapshot()) if guard._marks_editable() else {}
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
                await self._send_guard_notices()       # 收尾时名单还是写不进去：停止之后页面还在，说一句
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
            guard.tick_review()
            await guard.flush(self.server.broadcast)
            if guard.enabled:
                # 名单里的人 last_seen / 条数的变化不广播（页面自己按弹幕现算），这里悄悄补进 config，
                # 刷新后的 hello 拿到的是新的
                self.server.config["guard_marked"] = dict(guard.marks.snapshot())
                guard.poll_marks_health()
                await self._send_guard_notices()
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
                await self._flush_guard_edit()
            elif mtype == "guard_clear_auto":
                if guard.clear_auto():
                    await self._flush_guard_edit()
            elif mtype == "guard_dismiss":
                if guard.dismiss(msg.get("ev_id")):
                    await guard.flush(self.server.broadcast)
            elif mtype == "guard_reenable":
                if guard.reenable():
                    await guard.flush(self.server.broadcast)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 处理 {} 出错: {}".format(mtype, exc))
        return None

    # ---- --demo：虚构弹幕流 ----
    async def _demo_comment_stream(self):
        """演示模式里的一路虚构弹幕：走和真实弹幕一样的守卫（observe → comment → flush），好在停播时看界面。
        关注名单写进一个临时目录，演示完丢掉：不往 comment_guard/marked/ 里留文件，也不碰真实主播的名单。
        守卫在设置里关着就只显示弹幕。从不抛。"""
        guard = getattr(self, "guard", None)
        if guard is None:
            return
        real_marks = guard.marks
        tmp_dir = None
        if guard.effective_enabled():        # 守卫关着时名单根本不会载入，也就不需要临时目录
            tmp_dir = tempfile.mkdtemp(prefix="tlt-demo-marks-")
            guard.marks = MarkStore(directory=Path(tmp_dir))
        else:
            guard.marks = MarkStore()
        try:
            await self._begin_comment_guard(DEMO_STREAMER)
            await self._bind_comment_guard(None)
            n = 0
            while True:
                for user, uid, handle, text, zh, gap in DEMO_COMMENTS:
                    n += 1
                    await self._demo_comment("demo-c%d" % n, user, uid, handle, text, zh)
                    await asyncio.sleep(gap)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 演示弹幕出错：{}".format(exc))
        finally:
            demo_marks = guard.marks
            try:
                await guard.end(None, broadcast=self.server.broadcast)
            except (Exception, asyncio.CancelledError) as exc:
                print("[弹幕] 演示弹幕收尾出错：{}".format(exc))
            # 先把真实的名单还回去，再按它重发页面上的状态：演示的名单和事件不留在设置行里，
            # 「移出」「清空自动加入的」也不会对着一份已经丢掉的名单空转
            demo_marks.close()
            guard.marks = real_marks
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            try:
                await self._publish_comment_guard_config(reset=True)
            except (Exception, asyncio.CancelledError) as exc:
                print("[弹幕] 演示弹幕收尾出错：{}".format(exc))

    async def _demo_comment(self, cid, user, uid, handle, text, zh):
        item = {"id": cid, "user": user, "text": text, "uid": uid, "uid_src": "id", "handle": handle}
        guard = self.guard
        if guard.enabled:
            item = guard.observe([item], now=time.time())[0]
        await self.server.broadcast(dict(
            {"type": "comment", "id": cid, "user": user, "text": text, "ts": time.time(),
             "translated": None, "state": "pending"}, **account_fields(item)))
        await guard.flush(self.server.broadcast)
        await asyncio.sleep(DEMO_TRANSLATE_DELAY)
        await self.server.broadcast({"type": "comment_update", "id": cid, "translated": zh, "state": "ok"})

    def _open_guard_dir(self):
        """设置行里的「打开规则文件夹」：comment_guard/local/ 是用户自己放词表的地方，平时程序不生成它，
        点了才建（不存在就打不开），再用系统方式打开。只在本机控制面可用（手机入站只认 retranslate）。
        打不开不算错误：只打一行警告、给界面提一次性提示，同 _open_brands_dir。"""
        try:
            cg.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
            self._brand_dir_opener(cg.LOCAL_DIR)
        except OSError as exc:
            print("[弹幕] 打开规则文件夹失败：{}".format(exc))
            return self.server.broadcast(
                {"type": "notice", "text": L("无法打开文件夹：{}",
                                             "Couldn’t open the folder: {}").format(cg.LOCAL_DIR)})
        return None

    async def _flush_guard_edit(self):
        """名单改了之后把消息发出去。直播中走平时的节流；待机时（设置行里改）马上发、马上写盘——
        下一场的 load 会丢掉没写完的脏标记。"""
        guard = self.guard
        if guard.enabled:
            await guard.flush(self.server.broadcast)
            return
        await guard.flush(self.server.broadcast, final=True)
        await guard.marks.flush_now()
        # 待机时没有统计循环替它查：写盘失败要在这里说，否则页面上已经「移出」了，下一场它又回来，
        # 而打包后的程序看不到终端
        guard.poll_marks_health()
        await self._send_guard_notices()

    async def _guard_set(self, msg):
        """设置里的开关和模型复核选择。只在待机时改：直播中这一场的开关已经定了（同违禁词警示），
        设置分组在直播中也是整块隐藏的。只在改了的时候写一次 settings.json。

        `key`：选 Claude 时设置行里的密钥框。写进和翻译引擎同一个 Claude 密钥（api_keys），**不切换翻译引擎**；
        回传页面的只有打码后的尾四位（_publish_engine）。复核用的就是这把密钥（comment_review.make_reviewer）。"""
        guard = self.guard
        if self._stream_active() or (guard._open and getattr(self, "audit", None) is not None):
            print("[弹幕] 直播进行中，弹幕留意的设置要停止后再改")
            return
        key = msg.get("key")
        key_saved = isinstance(key, str) and bool(key.strip())
        if key_saved:
            from .translator import saved_keys
            keys = saved_keys()
            keys[self.ENGINE_KEY_ENV["claude"]] = key.strip()
            self._save_setting("api_keys", keys)
            await self._publish_engine()
        new = dict(guard.cfg)
        if isinstance(msg.get("enabled"), bool):
            new["enabled"] = msg["enabled"]
        if msg.get("backend") in BACKENDS:
            new["backend"] = msg["backend"]
        if isinstance(msg.get("weak"), bool):
            new["weak"] = msg["weak"]
        if new == guard.cfg and not key_saved:
            return
        if new != guard.cfg:
            guard.cfg = new
            self._save_setting(SETTINGS_KEY, dict(new))
        guard.configure_reviewer()                  # 选了 Claude 且有密钥就备好后端；换了密钥就按新密钥重来
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
    if code == "marks_unreadable":
        return L("关注名单文件读不了，先按空名单继续，本场的改动不会存盘",
                 "The marked-viewers file couldn’t be read. The list starts empty, and this "
                 "session’s changes won’t be saved.")
    if code == "marks_write_failed":
        return L("关注名单暂时存不进磁盘，名单先留在内存里，下次改动和收尾时会再试",
                 "The marked-viewers list couldn’t be saved. It stays in memory, and saving is "
                 "retried on the next change and when the session ends.")
    if code == "marks_full":
        return L("关注名单已满（{} 人），先移出几位再加入",
                 "The marked-viewers list is full ({} viewers). Unmark someone first.").format(MAX_VIEWERS)
    if code == "rules_changed":
        return L("弹幕留意的规则文件已修改，点「停止」再「开始翻译」后生效",
                 "The Comment Review rules changed. Click Stop, then Start, to apply them.")
    return ""
