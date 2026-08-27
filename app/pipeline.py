"""管线编排：拉流 → (降噪) → 切段 → 语音识别 → 翻译 → 广播给 UI。

支持两种启动方式：命令行传直播间地址，或在网页 UI 里输入地址点「开始」。
同一时间只跑一个直播间；切换房间时旧任务被取消，识别模型跨房间复用不重复加载。
"""
import asyncio
import os
import subprocess
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .asr import DEFAULT_TEMPERATURE
from .detector import BannedTermDetector, load_terms
from .glossary import load as load_glossary
from .nethttp import read_all
from .settings import load_settings, save_setting
from .telemetry import Telemetry
from .translator import ENGINE_KEY_ENV, create_translator

ROOT = Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / "banned_terms.txt"
TERMS_EXAMPLE = ROOT / "banned_terms.example.txt"


def load_detector(path=None):
    """读取违禁词表。首次运行时从模板复制一份用户可编辑的副本——
    模板入库、副本不入库，用户编辑不会挡住一键更新。"""
    target = Path(path) if path else TERMS_FILE
    if not target.exists() and target == TERMS_FILE and TERMS_EXAMPLE.exists():
        try:
            target.write_text(TERMS_EXAMPLE.read_text(encoding="utf-8"),
                              encoding="utf-8")
            print("[信息] 已生成违禁词表 {}（当前为空，按文件里的说明填写即可）"
                  .format(target.name))
        except OSError:
            pass
    return BannedTermDetector(load_terms(target))

# whisper 各模型的大致下载体积（MB），用来在 UI 上显示首次下载进度
MODEL_SIZES_MB = {"tiny": 75, "base": 145, "small": 484, "medium": 1530,
                  "large-v3": 3100, "large-v3-turbo": 1620}

DENOISE_MODEL = Path(__file__).resolve().parent.parent / "models" / "bd.rnnn"
DENOISE_MIN_BYTES = 100_000   # 完整模型约 300 KB；明显小于此值 = 下载被截断

# 音频积压预算（秒）。软阈值只是告警，硬上限才丢——丢一段就等于可能漏词，
# 所以硬上限给得很宽：60 秒 PCM 不到 2 MB，内存从来不是限制因素。
# 译文质量等级。低等级的结果**不得覆盖已生效的高等级结果**——
# 强模型重译走的是独立协程，不经过翻译队列，而队列可积压 4 条且单 worker
# 顺序处理，所以「快译一定先到」是赌执行顺序，不成立：队列一堵，2.3 秒的
# 强译就会先落地，随后排到的快译再把它盖回去。用等级判断从逻辑上消灭这个竞态。
QUALITY_FAST = 1
QUALITY_STRONG = 2

AUDIO_BACKLOG_WARN_SEC = 10.0
AUDIO_BACKLOG_DEGRADED_SEC = 30.0
AUDIO_BACKLOG_HARD_SEC = 60.0


def _arnndn_probe(model_path):
    """用 0.1 秒静音实测 arnndn 滤镜 + 模型能否初始化。

    一次探测同时覆盖两种真实故障：模型文件损坏（自动下载被截断——只验文件头
    magic 挡不住），以及 ffmpeg 不支持 arnndn（imageio-ffmpeg 的静态版没编译
    librnnoise）。两种情况下直接把坏参数交给拉流 ffmpeg，会让每一轮会话都
    「Error initializing filters」失败，再被自动重连放大成误导报错。"""
    from .ffmpeg_bin import find_ffmpeg
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return False
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.1",
             "-af", "arnndn=m={}".format(model_path),
             "-f", "null", "-"],
            capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False
RNNOISE_URL = ("https://raw.githubusercontent.com/GregorR/rnnoise-models/master/"
               "beguiling-drafter-2018-08-30/bd.rnnn")

# 演示模式的内置台词（英文原文 + 中文译文），用于在没有直播时验证 UI / 浏览器插件
DEMO_SCRIPT = [
    ("Hey everyone, welcome back to my live stream!", "嘿大家好，欢迎回到我的直播间！"),
    ("If you're new here, don't forget to tap the follow button.", "如果你是新来的，别忘了点一下关注按钮。"),
    ("Today I'm going to show you guys something really special.", "今天我要给大家展示一个特别的东西。"),
    ("Thank you so much for the rose, I really appreciate it!", "非常感谢你送的玫瑰，真的很感谢！"),
    ("Let me read the comments real quick... okay, interesting question.", "让我快速看一下评论……好，这个问题很有意思。"),
    ("This product is handmade, and it took me about three days to finish.", "这个产品是纯手工做的，我花了大概三天才完成。"),
    ("We just hit one thousand viewers, that's amazing!", "我们刚刚突破一千名观众，太棒了！"),
    ("I'll do a giveaway when we reach two thousand likes.", "点赞到两千的时候我会做一次抽奖。"),
    ("Alright, let's get started with today's main event.", "好的，让我们开始今天的重头戏吧。"),
    ("Don't forget you can ask me anything in the chat.", "别忘了你可以在聊天区随便问我问题。"),
]


class Pipeline:
    def __init__(self, args, server):
        self.args = args
        self.server = server
        self.target = args.target
        self.translator = create_translator(args.translator)
        self.server.config["target_lang"] = self.target
        # 上次的直播间地址由服务端记住（浏览器 localStorage 按端口隔离，
        # 端口自动漂移时会拿不到），启动时回填给 UI
        saved_room = load_settings().get("room_url")
        if saved_room:
            self.server.config["room_url"] = str(saved_room)[:500]
        # 主播语言同理走服务端持久化：localStorage 按端口隔离，端口一漂移
        # 「上次选的西语」就没了。而这一项丢失的代价是整场逐段自动检测语言：
        # 实测一场里 22.7% 的段被打上非西语标签，纯标点垃圾和按错误语言的
        # 翻译全从这里来。
        saved_source = load_settings().get("source_lang")
        if saved_source:
            self.server.config["source_lang"] = str(saved_source)[:12]
        self._counter = 0
        self._asr_pool = None            # 每条直播一个独立线程池，停止时整个丢弃
        self._stream_task = None
        # Python 3.9 的 asyncio.Lock() 构造时就要绑事件循环，而 Pipeline 可能
        # 在循环外构造（如测试）——惰性初始化，首次使用时必然已在循环内
        self._stream_lock = None
        self._transcriber = None
        self._transcriber_key = None     # 已就绪模型对应的配置 key
        self._loading_key = None         # 在途加载对应的配置 key（可能被取消）
        self._transcriber_future = None  # 正在加载中的模型，避免重复加载
        self._resolve_fail_streak = 0    # 连续解析失败计数（触发 yt-dlp 自动保鲜）
        self.telemetry = Telemetry()
        self.detector = load_detector(getattr(args, "banned_terms", None))
        self.glossary = load_glossary(getattr(args, "glossary", None))
        self.audit = None                # 每条直播一个审计日志文件
        self._stats_task = None
        self._selfcheck_task = None      # 持有引用，否则任务可能被 GC 掉
        self._provision_task = None
        # 最近若干条字幕的原文，供「重译」按 id 取回。只留少量：这个功能是
        # 给中控看到可疑一句时临时用的，不是历史检索。
        self._recent = OrderedDict()
        self._strong = None              # 按需创建，用完不常驻
        self._upgrade_tasks = []         # 报警触发的重译，持有引用防 GC
        self._quality = {}               # seq -> 当前已生效译文的质量等级
        self._strong_inflight = set()    # 正在跑强模型的 seq，防同一条重复触发
        self._alert_seq = 0              # 报警编号，供译文回来后对上号
        self._alert_tasks = []
        if self.detector.enabled:
            print("[信息] 违禁词检测已启用：{} 个词条".format(self.detector.count))
        else:
            print("[信息] 违禁词表为空——编辑 banned_terms.txt 后重新「开始翻译」即可启用")

    # ---- 来自 UI 的控制消息 ----
    def handle_control(self, msg):
        mtype = msg.get("type")
        if mtype == "set_target":
            value = str(msg.get("value", ""))[:12]
            if value:
                self.target = value
                self._save_setting("target_lang", value)
                return self.server.broadcast({"type": "config", "target_lang": value})
        elif mtype == "start":
            url = str(msg.get("url", "")).strip()
            source = str(msg.get("source", "") or "").strip()
            # UI 只允许网络地址（本地 CLI 不受此限制）
            if url.startswith("http://") or url.startswith("https://"):
                if source and source != "auto":
                    self.args.source = source
                elif source == "auto":
                    self.args.source = None
                if source:
                    self._save_setting("source_lang", source[:12])
                    self.server.config["source_lang"] = source[:12]
                    self.args.source_requested = source[:12]
                return self._start_with_ack(url)
            # 不合规的地址以前是被静默丢弃的——用户点了「开始」却毫无反应
            return self.server.status(
                "error", "地址无效：请填写 http:// 或 https:// 开头的直播间地址")
        elif mtype == "stop":
            return self.stop_stream()
        elif mtype == "set_engine":
            return self.set_engine(msg.get("engine"), msg.get("api_key"))
        elif mtype == "retranslate":
            return self.retranslate(msg.get("id"))
        elif mtype == "apply_update":
            if getattr(self, "updater", None) is not None:
                return self._apply_update()
        elif mtype == "check_update":
            if getattr(self, "updater", None) is not None:
                return self.updater.check_and_notify(delay=0, manual=True)
        return None

    async def _apply_update(self):
        await self.stop_stream(quiet=True)
        await self.updater.apply()

    async def _start_with_ack(self, url):
        """UI 点「开始」后立刻回执——停掉旧管线可能要好几秒（等 ffmpeg 退出），
        期间不给任何反馈的话，用户会以为点了没反应而反复点。"""
        await self.server.status("connecting", "已收到指令，正在连接…")
        await self.start_stream(url)

    def _save_setting(self, key, value):
        """把界面偏好写进 settings.json（重启后 main.py 读回）。"""
        save_setting(key, value)

    # ---- 直播任务管理 ----
    # 加锁的原因：多个页面/标签页可能同时连着服务，两条 start 消息并发进来时，
    # 「读旧任务→取消→建新任务」如果不是原子的，后一条会覆盖 _stream_task，
    # 把前一条的管线（连同它的 ffmpeg 进程）变成谁也停不掉的孤儿。
    def _lock(self):
        if self._stream_lock is None:
            self._stream_lock = asyncio.Lock()
        return self._stream_lock

    async def start_stream(self, url):
        async with self._lock():
            await self._stop_locked(quiet=True)
            self.server.config["room_url"] = url
            self._save_setting("room_url", url)
            # source_lang 捎在同一条 config 里：开着的第二个页面也要跟上，
            # 不能等它重连才看到第一个页面刚选的语言
            await self.server.broadcast({"type": "config", "room_url": url,
                                         "source_lang": getattr(self.args, "source",
                                                                None) or "auto"})
            self._stream_task = asyncio.create_task(self._run_stream(url))

    async def stop_stream(self, quiet=False):
        async with self._lock():
            await self._stop_locked(quiet=quiet)

    # 等旧管线收尾的上限。超过就不等了——见 _stop_locked 里的说明。
    STOP_GRACE_SEC = 3.0

    async def _stop_locked(self, quiet=False):
        """调用方必须已持有 _stream_lock。"""
        task = self._stream_task
        self._stream_task = None
        if task is not None and not task.done():
            # 先让界面立刻回到待机。停止是用户的明确指令，界面不该在这儿干等：
            # 点了没反应，人只会以为程序死了，然后反复点。
            if not quiet:
                await self.server.status("idle", "正在停止…")
            task.cancel()
            # 等待要有上限。`run_in_executor` 里的识别调用**取消不掉**——线程一旦
            # 开跑就只能等它自己结束，实测遇到复读跑飞时单次要十几秒。
            # 以前这里是无限期 await，于是积压严重时点停止会像卡死一样。
            # 超时就撒手：那条协程会自己走完 finally 收掉 ffmpeg，而它用的线程池
            # 下面就整个丢弃，不会占住下一场直播。
            try:
                await asyncio.wait_for(asyncio.shield(task), self.STOP_GRACE_SEC)
            except asyncio.TimeoutError:
                print("[信息] 识别调用一时停不下来，已放手让它自行收尾")
            except asyncio.CancelledError:
                pass
        if self._stats_task is not None and not self._stats_task.done():
            self._stats_task.cancel()
            self._stats_task = None
        if self.audit is not None:
            self.audit.close()
            self.audit = None
        pool, self._asr_pool = self._asr_pool, None
        if pool is not None:
            # 已排队的段直接丢弃；正在跑的那段让它自己跑完（无法安全打断），
            # 但它跑在旧线程池上，不会占住下一条直播的识别线程
            pool.shutdown(wait=False, cancel_futures=True)
        if not quiet:
            await self.server.status("idle", "已停止。输入直播间地址可重新开始。")

    # ---- 实际的直播管线 ----
    async def _run_stream(self, url):
        """外层兜底：任何未预料的异常都要反映到 UI，绝不无声卡死在「直播中」。"""
        try:
            await self._run_stream_inner(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[错误] 直播管线异常: {}".format(exc))
            try:
                await self.server.status("error", "内部错误，已停止：{}".format(exc))
            except Exception:
                pass

    async def _stats_loop(self, interval=10):
        """定期把延迟分位数、积压秒数和降级状态推给界面。

        中控必须能看出「检测正在落后多少秒」——识别卡住时假装一切正常，
        比晚几秒报警危险得多。"""
        last_level = "ok"
        try:
            while True:
                await asyncio.sleep(interval)
                snap = self.telemetry.snapshot()
                level = self._health_level(snap["audio_backlog_sec"])
                snap["health"] = level
                await self.server.broadcast({"type": "stats", **snap})
                if level != last_level:
                    await self._announce_health(level, snap["audio_backlog_sec"])
                    last_level = level
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    @staticmethod
    def _health_level(backlog_sec):
        if backlog_sec >= AUDIO_BACKLOG_DEGRADED_SEC:
            return "degraded"
        if backlog_sec >= AUDIO_BACKLOG_WARN_SEC:
            return "lagging"
        return "ok"

    async def _announce_health(self, level, backlog_sec):
        if level == "degraded":
            text = ("🔴 检测已降级：识别落后 {:.0f} 秒，仍在继续处理（不会漏掉这段音频）"
                    .format(backlog_sec))
        elif level == "lagging":
            text = "⚠️ 识别开始落后（积压 {:.0f} 秒），报警会相应延迟".format(backlog_sec)
        else:
            text = "✅ 识别已追上，检测恢复正常"
        print("[健康] " + text)
        await self.server.broadcast({"type": "health", "level": level,
                                     "backlog_sec": round(backlog_sec, 1),
                                     "text": text})

    async def _run_stream_inner(self, url):
        await self._begin_session(url)
        try:
            await self._run_session(url)
        finally:
            # 无论怎么结束（下播、预算耗尽、解析失败、模型加载失败、被取消），
            # 都要收掉统计循环和审计文件——否则界面上会继续刷新冻结的统计数字
            await self._end_session()

    async def _begin_session(self, url):
        from .audit import AuditLog
        from .provenance import app_version, streamer_of

        # 词表每场重新读：模板生成出来是空的，用户按提示填好后点「停止→开始」
        # 必须真的生效，否则头号卖点就是 100% 静默漏报
        self.detector = load_detector(getattr(self.args, "banned_terms", None))
        self.detector.reset_state()
        self._quality.clear()            # 等级按 seq 记，换场后 seq 会重号
        self._strong_inflight.clear()
        self.telemetry.reset()          # 统计按场计，不跨房间累计
        if self.audit is not None:
            self.audit.close()
        # requested 是用户的选择，active 是实际生效的对象——这两列并排记，
        # 是因为 2026-08-26 它们恰恰不一致：requested=deepl 被启动逻辑静默
        # 重置，实际整场跑的是 1.8B，事后只能靠延迟指纹反推。有了这两列，
        # 「以为跑 A 实际跑 B」变成 grep 一行就能发现的事。
        self.audit = AuditLog(room_url=url, extra={
            "app_version": app_version(),
            "streamer": streamer_of(url),
            # requested 由做决策的那一刻记录（main 启动 / UI 点开始），
            # 这里只转抄，不重算——重算读到的 settings 可能已经不是当时那份
            "source_requested": getattr(self.args, "source_requested", "?"),
            "source_active": getattr(self.args, "source", None) or "auto",
            "translator_requested": getattr(self.args, "translator", None) or "auto",
            # 引擎 none 时统一记字符串 "none"，别让下游在 null 和 "none"
            # 两种写法之间做字符串匹配
            "translator_active": (self.translator.name
                                  if self.translator is not None else "none"),
            # 主播 profile 尚未落地（onboarding 在实验分支），先占位——
            # 等它进来时这里换成真实指纹，老日志靠 None 区分
            "profile_hash": None,
        })
        if self._stats_task is None or self._stats_task.done():
            self._stats_task = asyncio.ensure_future(self._stats_loop())
        await self._publish_watchlist()
        await self._publish_engine()
        # 每场重跑自检：状态会漂。用户按红条的提示改完 banned_terms.txt 点
        # 「开始翻译」，面板若还挂着「词表为空」的红条，下次他就不信这个红条了；
        # 反过来更糟——开播时模型被删了、Ollama 停了、磁盘满了，面板还是启动
        # 时那份绿的。放后台跑，不挡开播。
        # 备货流程每场也重跑一次：用户可能在程序开着的时候才去装 Ollama，
        # 不该逼他重启程序才被发现。已经就绪时这里几毫秒就返回。
        self._provision_task = asyncio.ensure_future(self._provision_then_check())
        if self.detector.enabled:
            print("[信息] 违禁词检测已启用：{} 个词条".format(self.detector.count))
        else:
            # 词表为空是「这场不会有任何报警」，必须让运维在界面上看到
            print("[警告] 违禁词表为空，本场不会有任何报警")
            await self.server.broadcast({
                "type": "notice",
                "text": "违禁词表为空，本场不会报警——编辑 banned_terms.txt 后重新开始",
            })

    async def _provision_then_check(self):
        await self.ensure_local_translator()
        await self.run_selfcheck()

    async def ensure_local_translator(self):
        """开工前把本地翻译准备好，让用户不必为此开终端。

        没装 Ollama 的机器会退回 Google 免费接口——按 IP 限流，长时间监听经常
        整段翻译失败。以前的提示是「自己去装 Ollama，再敲一行 ollama pull」，
        对不会用终端的人等于永远用不上本地翻译。

        Ollama 装了没启动就帮他启动；启动了但没有模型就用 HTTP 接口拉下来
        （3GB 的 Whisper 模型我们本来就自动下，这个 1.1GB 是同一件事）。
        压根没装的只能引导——那一步需要管理员权限，代劳不了。
        """
        from . import localmodel
        from .translator import HYMT2_SMALL, _ollama_has_gemma, _ollama_has_hymt2

        if getattr(self.args, "translator", "auto") not in ("auto",):
            return                      # 用户显式指定了引擎，别自作主张
        if await localmodel.is_running():
            pass
        elif localmodel.is_installed():
            print("[信息] Ollama 已安装但没在运行，正在启动…")
            if not await localmodel.start():
                return
        else:
            return                      # 没装：交给自检那一行去引导

        if _ollama_has_hymt2() or _ollama_has_hymt2(large=True) or _ollama_has_gemma():
            return                      # 已经有本地模型了

        await self.server.status(
            "idle", "正在准备本地翻译模型（约 1.1 GB，只需这一次）…")
        last = [-10.0]

        def progress(pct, done_mb, total_mb):
            if pct - last[0] < 5:       # 别把界面刷爆
                return
            last[0] = pct
            asyncio.ensure_future(self.server.status(
                "idle", "正在下载本地翻译模型：{:.0f}%（{:.0f} / {:.0f} MB，"
                        "只需这一次）…".format(pct, done_mb, total_mb)))

        ok = await localmodel.pull(HYMT2_SMALL, on_progress=progress)
        if ok:
            print("[信息] 本地翻译模型已就绪")
            self.translator = create_translator("auto")
            await self.server.status("idle", "本地翻译已就绪，可以开始了。")
        else:
            print("[警告] 本地翻译模型下载失败，本次继续用网络翻译")
        await self.run_selfcheck()

    async def run_selfcheck(self):
        """启动自检：确认每项能力真的在工作，结果推到界面上。

        这个方法存在的原因是降噪那次事故——功能静默降级成关闭，只在一行
        没人看的日志里说了一句。自检把这类问题变成界面上的红条。"""
        from .selfcheck import run_all, summarize
        try:
            checks = await run_all(self.args, self.detector, self.glossary,
                                   self.translator)
        except Exception as exc:
            print("[警告] 自检执行失败: {}".format(exc))
            return
        summary = summarize(checks)
        for c in checks:
            icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}[c["level"]]
            print("[自检] {} {}：{}".format(icon, c["name"], c["detail"]))
        self.server.config["selfcheck"] = {"checks": checks, "summary": summary}
        await self.server.broadcast({"type": "selfcheck", "checks": checks,
                                     "summary": summary})

    async def _publish_watchlist(self):
        """把违禁词表状态推给界面并存进 config——首页那张卡片要靠它显示
        「已启用 N 条」还是「未配置」。词表默认为空，用户看不到这个提示
        就根本不知道有这个功能，也就不会去配。"""
        info = {"count": self.detector.count if self.detector else 0,
                "glossary": len(self.glossary.entries) if self.glossary else 0}
        self.server.config["watchlist"] = info
        await self.server.broadcast(dict(info, type="watchlist"))

    async def _end_session(self):
        if self._stats_task is not None and not self._stats_task.done():
            try:      # 收尾前推一次终值，别让界面停在半截数据上
                await self.server.broadcast({"type": "stats", **self.telemetry.snapshot()})
            except Exception:
                pass
            self._stats_task.cancel()
            self._stats_task = None
        if self.audit is not None:
            self.audit.close()
            self.audit = None

    async def _run_session(self, url):
        from .asr import create_transcriber
        from .resolver import ResolveError, is_direct_url, resolve_stream_url

        await self.server.status("connecting", "正在解析直播流地址…")
        try:
            media = await resolve_stream_url(
                url, cookies=self.args.cookies,
                cookies_browser=getattr(self.args, 'cookies_browser', 'auto'))
            self._resolve_fail_streak = 0
        except ResolveError as exc:
            self._note_resolve_failure(exc)
            await self.server.status("error", str(exc))
            print("[错误] {}".format(exc))
            return

        loop = asyncio.get_running_loop()
        # 未显式指定的参数用硬件推荐补齐。注意：backend/model/device/compute 是联动
        # 整体，用户锁定 backend/device 时其余字段围绕它重新推导（见 hwdetect.py）
        from .hwdetect import recommend
        rec = recommend(backend=self.args.backend, device=self.args.device)
        backend = rec["backend"]
        model = self.args.model or rec["model"]
        device = rec["device"]
        compute = (self.args.compute_type if self.args.compute_type != "auto"
                   else rec["compute_type"])
        print("[信息] 识别配置: backend={} model={} device={} ({})".format(
            backend, model, device, rec["note"]))

        temperature = (self.args.asr_temperature
                       if self.args.asr_temperature is not None
                       else DEFAULT_TEMPERATURE)
        key = (backend, model, device, compute, self.args.source,
               self.args.beam, self.args.context, temperature,
               self.glossary.asr_prompt())
        if self._transcriber is None or self._transcriber_key != key:
            size_mb = MODEL_SIZES_MB.get(model)
            if size_mb and size_mb >= 1000:
                size_note = "约 {:.1f} GB".format(size_mb / 1000)
            elif size_mb:
                size_note = "约 {} MB".format(size_mb)
            else:
                size_note = "可能较大"
            await self.server.status(
                "connecting",
                "正在加载语音识别模型 {}（仅首次使用需下载，{}，进度会显示在这里）…"
                .format(model, size_note))
            # 关键：把「正在加载」这件事本身记下来。模型要几分钟，用户等不及
            # 点停止再开始时，取消只会解绑协程、线程仍在后台加载；若不认这个
            # 在途任务，每次重来都会再起一个数 GB 的模型，几轮就把内存吃光。
            #
            # 在途 key（_loading_key）与已就绪 key（_transcriber_key）必须分开记：
            # 合用一个字段的话，加载中被取消会留下「key 是新的、模型还是旧的」的
            # 错配，之后外层判断永远成立不了，新参数至死不生效（静默用旧模型）。
            if (self._transcriber_future is None or self._transcriber_future.done()
                    or self._loading_key != key):
                self._loading_key = key
                self._transcriber_future = loop.run_in_executor(
                    None,
                    lambda: create_transcriber(
                        backend=backend,
                        model_size=model,
                        device=device,
                        compute_type=compute,
                        language=self.args.source,
                        beam_size=self.args.beam,
                        use_context=self.args.context,
                        temperature=temperature,
                        hotwords=self.glossary.asr_prompt(),
                    ),
                )
            watcher = asyncio.ensure_future(self._model_download_progress(model))
            try:
                # shield：本任务被取消时不要连带取消底层加载，
                # 下一次启动可以直接复用同一个在途结果
                transcriber = await asyncio.shield(self._transcriber_future)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._transcriber_future = None
                self._loading_key = None
                watcher.cancel()   # 先停进度播报，别让它把下面的 error 状态盖回去
                await self.server.status(
                    "error", "下载/加载识别模型失败——请检查网络后点「开始翻译」重试。\n"
                             "技术细节：{}".format(str(exc)[:200]))
                print("[错误] 加载模型失败: {}".format(exc))
                return
            finally:
                watcher.cancel()
            # 模型和它的 key 一起提交：中途取消时两者都不动，下次重来还会重新加载
            self._transcriber = transcriber
            self._transcriber_key = key
        transcriber = self._transcriber

        denoise = await self._ensure_denoise_model()
        live_note = ("已连接直播间，开始实时识别"
                     + ("（人声降噪已开启）" if denoise else ""))

        # ---- 断流自动重连 ----
        # TikTok 的流地址会过期、网络会抖动，ffmpeg 一断不等于主播下播了。
        # 中断后重新解析地址重连；只有解析结果明确说「没在播」（kind=offline）
        # 才真正宣布直播结束。连续多次重连都拉不到音频才放弃。
        # 直连 .flv/.m3u8 地址无法重新解析出「是否还在播」：播过一阵后结束
        # 就按正常收尾处理，重试预算也压到 1 次，别对着过期地址空耗。
        direct = is_direct_url(url)
        budget = 1 if direct else 5
        reconnects = 0
        while True:
            await self.server.status("connecting", "正在连接直播音频流…")
            got_audio, audio_secs = await self._stream_session(
                media, transcriber, denoise, live_note, loop)
            if audio_secs >= 30:
                if direct:
                    await self.server.status(
                        "ended", "直播流已结束。可以继续翻看上面的字幕，"
                                 "或输入新的地址。")
                    print("[信息] 直播流已结束。")
                    return
                reconnects = 0        # 刚才播得好好的：重置重连预算

            media = None
            while media is None:
                reconnects += 1
                if reconnects > budget:
                    await self.server.status(
                        "error", "直播流多次中断且自动重连失败——可能直播已结束，"
                                 "或网络不稳。请稍后点「开始翻译」重试。")
                    print("[信息] 自动重连预算用尽，放弃。")
                    return
                delay = min(30, 2 ** reconnects)
                await self.server.status(
                    "connecting",
                    "直播流中断，{} 秒后自动重连（第 {}/{} 次）…".format(
                        delay, reconnects, budget))
                await asyncio.sleep(delay)
                try:
                    media = await resolve_stream_url(
                url, cookies=self.args.cookies,
                cookies_browser=getattr(self.args, 'cookies_browser', 'auto'))
                    self._resolve_fail_streak = 0
                except ResolveError as exc:
                    if exc.kind == "offline":
                        await self.server.status(
                            "ended", "直播已结束。可以继续翻看上面的字幕，"
                                     "或输入新的直播间地址。")
                        print("[信息] 直播已结束。可在网页里输入新地址继续。")
                        return
                    self._note_resolve_failure(exc)
                    print("[错误] 重连解析失败: {}".format(exc))

    async def _stream_session(self, media, transcriber, denoise, live_note, loop):
        """跑一轮拉流→识别→翻译，直到流断开。返回 (是否收到过音频, 音频时长秒)。

        音频时长按真实收到的帧数累计，不用墙钟——网络劣化时 ffmpeg 可能连着
        30 秒只吐 2 秒音频，用墙钟会把这种「假连接」当成播得好好的，
        重连预算被错误重置后放弃分支永远走不到。"""
        from .audio import FRAME_SEC, SAMPLE_RATE, FFmpegAudioSource
        from .segmenter import SilenceSegmenter

        got_audio = False
        audio_secs = 0.0
        # 音频缓冲按**秒数**预算，不按段数——段数上限在 9 秒片段下是 27 秒缓冲、
        # 在 4 秒片段下只剩 12 秒，同一个数字对不同切段配置含义完全不同。
        # 而且业务上「晚报警」远好过「永不报警」：识别卡住时先把音频存住，
        # 60 秒的 16kHz 单声道 PCM 也才 1.9 MB，内存从来不是瓶颈。
        queue = asyncio.Queue()
        backlog = {"sec": 0.0}
        # 翻译独立成队列：翻译慢/卡住绝不能反压到识别链路上——那会让音频被丢弃，
        # 直接变成漏词。积压时丢的是**旧的翻译任务**，音频一段都不丢。
        trans_queue = asyncio.Queue(maxsize=4)
        asr_pool = ThreadPoolExecutor(max_workers=1)   # 本轮会话专用，停止时随之丢弃
        self._asr_pool = asr_pool
        source = FFmpegAudioSource(media, denoise_model=denoise)
        segmenter = SilenceSegmenter()

        def _put(segment):
            if segment is None:
                queue.put_nowait(None)
                return
            chunk, _ts = segment
            dur = len(chunk) / 2.0 / SAMPLE_RATE
            # 硬上限之前一律留着：识别慢只该推迟报警，不该让这段话永远消失
            while backlog["sec"] + dur > AUDIO_BACKLOG_HARD_SEC:
                try:
                    stale, _ = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                backlog["sec"] -= len(stale) / 2.0 / SAMPLE_RATE
                self.telemetry.drop_audio()
                if self.audit is not None:
                    self.audit.dropped_audio(queue_depth=queue.qsize())
                print("[严重] 积压超过 {} 秒，被迫丢弃最旧的一段音频——这段可能漏词"
                      .format(AUDIO_BACKLOG_HARD_SEC))
            queue.put_nowait(segment)
            backlog["sec"] += dur
            self.telemetry.asr_queue_depth = queue.qsize()
            self.telemetry.set_backlog(backlog["sec"])

        def _drop_job(job):
            """告诉界面这条不会有译文了——否则它永远停在「翻译中…」。"""
            self.telemetry.drop_translation()
            asyncio.ensure_future(self.server.broadcast({
                "type": "caption_update", "id": job["id"], "translated": None,
                "translate_state": "dropped",
            }))

        def _put_translation(job):
            """翻译积压时丢最旧的任务：中控不需要 30 秒前那句话的中文，
            西语原文早就显示了、违禁词也早就扫过了。"""
            while trans_queue.full():
                try:
                    stale = trans_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if stale is None:        # 退出哨兵不能被当成积压丢掉
                    trans_queue.put_nowait(None)
                    break
                _drop_job(stale)
            trans_queue.put_nowait(job)
            self.telemetry.translation_queue_depth = trans_queue.qsize()

        async def reader():
            # 「直播中」要等真的收到音频才宣布——ffmpeg 连流失败时不能先报喜再改口
            nonlocal got_audio, audio_secs
            try:
                async for frame in source.frames():
                    if not got_audio:
                        got_audio = True
                        await self.server.status("live", live_note)
                    audio_secs += FRAME_SEC
                    for segment in segmenter.feed(frame):
                        _put((segment, time.time()))
                for segment in segmenter.flush():   # 别丢掉最后一段话
                    _put((segment, time.time()))
            finally:
                _put(None)

        async def asr_worker():
            while True:
                item = await queue.get()
                if item is None:
                    break
                segment, audio_end_ts = item
                backlog["sec"] = max(0.0, backlog["sec"] - len(segment) / 2.0 / SAMPLE_RATE)
                self.telemetry.set_backlog(backlog["sec"])
                t0 = time.time()
                try:
                    result = await loop.run_in_executor(
                        asr_pool, transcriber.transcribe, segment
                    )
                except Exception as exc:
                    print("[警告] 识别一段音频失败: {}".format(exc))
                    continue
                asr_ms = (time.time() - t0) * 1000.0
                segment_ms = len(segment) / 2.0 / SAMPLE_RATE * 1000.0
                if asr_ms > segment_ms:
                    # 解码比音频本身还久：多半是复读跑飞，继续下去队列就会溢出
                    self.telemetry.note_overrun()
                    print("[警告] 识别耗时 {:.1f}s 超过片段时长 {:.1f}s（疑似复读跑飞）"
                          .format(asr_ms / 1000, segment_ms / 1000))
                    if self.audit is not None:
                        self.audit.asr_overrun(asr_ms=asr_ms, segment_ms=segment_ms)
                self.telemetry.asr_queue_depth = queue.qsize()
                self.telemetry.translation_queue_depth = trans_queue.qsize()
                job = await self._emit_original(result, audio_end_ts, asr_ms,
                                                segment_ms=segment_ms)
                if job is not None:
                    _put_translation(job)

        async def translation_worker():
            while True:
                job = await trans_queue.get()
                if job is None:
                    break
                await self._translate_and_update(job)

        async def run_workers():
            trans_task = asyncio.ensure_future(translation_worker())
            reader_task = asyncio.ensure_future(reader())
            asr_task = asyncio.ensure_future(asr_worker())
            try:
                # return_exceptions：一条协程出错时不能把另一条丢成孤儿
                #（孤儿 asr_worker 会继续给已结束的会话广播字幕）
                results = await asyncio.gather(reader_task, asr_task,
                                               return_exceptions=True)
                for r in results:
                    if isinstance(r, BaseException) and not isinstance(
                            r, asyncio.CancelledError):
                        raise r
                # 流自然结束：让翻译把在途那条跑完（最多等 5 秒）
                trans_queue.put_nowait(None)
                try:
                    await asyncio.wait_for(asyncio.shield(trans_task), timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    trans_task.cancel()
            finally:
                for task in (reader_task, asr_task, trans_task):
                    if not task.done():
                        task.cancel()
                # 被取消（用户点停止/换房间）时不 drain 队列：等积压的翻译跑完
                # 最多要 5 秒，而停止应当立刻生效——剩下的直接告知界面已跳过
                while True:
                    try:
                        pending = trans_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if pending is not None:
                        _drop_job(pending)

        try:
            await run_workers()
        finally:
            # 先切 ffmpeg 再收尾，任何退出路径（含异常）都不留子进程
            await source.stop()
            asr_pool.shutdown(wait=False)
        tail = source.stderr_tail()
        if tail:
            print("[信息] ffmpeg 输出: {}".format(tail))   # 英文技术输出只进终端，不上 UI
        return got_audio, audio_secs

    def _note_resolve_failure(self, exc):
        """连续两次「不是主播下播」的解析失败，多半是 yt-dlp 的 TikTok 提取器
        坏了——触发后台自动升级 yt-dlp（见 updater.freshen_ytdlp）。"""
        kind = getattr(exc, "kind", "unknown")
        if kind in ("offline", "login"):
            self._resolve_fail_streak = 0
            return
        if kind == "network":
            return   # 断网与提取器无关：不计数也不清零，更不能拉起注定失败的 pip
        self._resolve_fail_streak += 1
        if self._resolve_fail_streak >= 2 and getattr(self, "updater", None) is not None:
            asyncio.ensure_future(self.updater.freshen_ytdlp(reason="resolve-failures"))

    async def _model_download_progress(self, model):
        """模型加载期间轮询 HuggingFace 缓存目录的增量，把下载进度推到 UI。
        没有真的在下载（缓存已存在）时增量趋近于零，不会打扰用户。"""
        try:
            hf_home = os.environ.get("HF_HOME")
            cache = (Path(hf_home) / "hub" if hf_home
                     else Path.home() / ".cache" / "huggingface" / "hub")

            def _du():
                # lstat 不跟随符号链接：HF 缓存里 snapshots/ 是指向 blobs/ 的
                # 软链，跟着算会把每个文件记两遍，进度条显示 200%
                total = 0
                for root, _dirs, files in os.walk(cache):
                    for name in files:
                        try:
                            total += os.lstat(os.path.join(root, name)).st_size
                        except OSError:
                            pass
                return total

            loop = asyncio.get_running_loop()
            baseline = await loop.run_in_executor(None, _du)
            expected = MODEL_SIZES_MB.get(model)
            while True:
                await asyncio.sleep(3)
                done_mb = (await loop.run_in_executor(None, _du) - baseline) / 1e6
                if done_mb < 20:      # 没在下载（或刚开始），别闪一条 0% 出来
                    continue
                if expected:
                    done_mb = min(done_mb, expected)   # Windows 无软链权限时是真副本，仍可能翻倍
                    pct = min(99, int(done_mb * 100 / expected))
                    text = ("正在下载识别模型 {}：{}%（{:.0f} / {} MB，仅首次需要，"
                            "请保持窗口打开）…".format(model, pct, done_mb, expected))
                else:
                    text = ("正在下载识别模型 {}：已下载 {:.0f} MB（仅首次需要，"
                            "请保持窗口打开）…".format(model, done_mb))
                await self.server.status("connecting", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass      # 进度显示是锦上添花，任何失败都不能影响加载本身

    async def _ensure_denoise_model(self):
        if self.args.denoise == "off":
            return None
        loop = asyncio.get_running_loop()
        if DENOISE_MODEL.exists():
            if await loop.run_in_executor(None, _arnndn_probe, str(DENOISE_MODEL)):
                return str(DENOISE_MODEL)
            # 实测起不来：模型损坏（截断下载）或 ffmpeg 不支持 arnndn。
            # 明显截断的坏文件必须删掉——留着会毒害之后的每一次启动
            print("[警告] 降噪不可用（模型损坏或 ffmpeg 不支持 arnndn），本次不降噪")
            try:
                if DENOISE_MODEL.stat().st_size < DENOISE_MIN_BYTES:
                    DENOISE_MODEL.unlink()
            except OSError:
                pass
            return None
        # 自动下载（约 300 KB）；失败则本次不降噪，不阻塞直播启动
        try:
            import aiohttp

            DENOISE_MODEL.parent.mkdir(parents=True, exist_ok=True)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(RNNOISE_URL) as resp:
                    if resp.status == 200:
                        # read_all 会读到 EOF。这里曾经用单次 read(8MB)，拿到的是
                        # 残缺数据，长度校验必然失败，于是降噪一直是关着的，
                        # 而日志只说「下载失败或校验未过」，看不出是自己读少了。
                        data = await read_all(resp, 8 * 1024 * 1024) or b""
                        # 只验文件头不够：截断的下载同样带正确 magic，
                        # 长度下限 + 落盘后实测初始化才算数
                        if (data.startswith(b"rnnoise")
                                and len(data) >= DENOISE_MIN_BYTES):
                            # 先写临时文件再原子改名：中途断网不会留下半个模型文件
                            tmp = DENOISE_MODEL.with_suffix(".rnnn.part")
                            tmp.write_bytes(data)
                            tmp.replace(DENOISE_MODEL)
                            if await loop.run_in_executor(
                                    None, _arnndn_probe, str(DENOISE_MODEL)):
                                print("[信息] 已自动下载人声降噪模型")
                                return str(DENOISE_MODEL)
                            DENOISE_MODEL.unlink()   # 实测失败：删掉别留隐患
        except Exception:
            pass
        print("[警告] 降噪模型不可用（下载失败或校验未过），本次不降噪")
        return None

    async def _emit_original(self, result, audio_end_ts, asr_ms, segment_ms=None):
        """识别一出结果就立刻做两件事：扫违禁词、把西语原文推给界面。
        两者都不等翻译——翻译是可降级的，报警和原文不是。
        返回待翻译任务（无需翻译时返回 None）。"""
        # 违禁词扫的是 raw_text（含被质量过滤丢掉的部分）：宁可多报，不能漏报
        hits = []
        if self.detector is not None and self.detector.enabled:
            scan_text = result.raw_text or result.text
            if scan_text:
                hits = self.detector.scan(scan_text, ts=audio_end_ts)

        self._counter += 1
        seq = self._counter
        if self.audit is not None:
            self.audit.segment(seq, result, audio_end_ts, asr_ms, hits)

        alert_ids = []
        for hit in hits:
            if self.audit is not None:
                self.audit.alert(hit)
            print("[警报] 疑似违禁词「{}」（{}）：{}".format(
                hit["term"], hit["tier"], hit["context"][-80:]))
            self._alert_seq += 1
            hit["alert_id"] = self._alert_seq
            alert_ids.append(self._alert_seq)
            await self.server.broadcast({"type": "alert", **hit})

        # 报警的上下文是西语原话。中控读不了西语就无从判断该不该处理，
        # 而报警恰恰是最需要人工复核的地方——所以补一份中文，用最强模型：
        # 这句话可能要拿去跟平台交涉，值这 2 秒。
        #
        # **一次扫描只翻一遍。** 同一次扫描的多个命中共用同一段 context
        # （实测一句话同时命中 3 个词是常事），逐个翻是拿同一段文本跑三遍
        # temperature 0 的模型，输出必然一样。强模型的原则是稀疏、按需、
        # 短暂占内存，不能因为一次报警就成倍放大调用量——它每跑一次都在和
        # Whisper 抢内存，而识别就在报警链路上。
        if alert_ids:
            self._alert_tasks = [t for t in self._alert_tasks if not t.done()]
            self._alert_tasks.append(asyncio.ensure_future(
                self._translate_alert(alert_ids, hits[0].get("context") or "",
                                      result.language)))

        if not result.text:
            return None

        lang = result.language
        # 只有目标语言与检测语言完全一致才跳过翻译；zh-TW 这类带地区的目标
        # 仍要走翻译做简繁转换（whisper 只会返回裸 "zh"）
        same_lang = bool(lang) and self.target.lower() == str(lang).lower()
        needs_translation = self.translator is not None and not same_lang

        now = time.time()
        self.telemetry.record_asr(asr_ms, (now - audio_end_ts) * 1000.0, segment_ms)
        await self.server.broadcast({
            "type": "caption",
            "id": seq,
            "ts": now,
            "original": result.text,
            "translated": None,
            "translate_state": "pending" if needs_translation else "skipped",
            "src_lang": lang,
            "target_lang": self.target,
            "asr_ms": round(asr_ms),
            "e2e_ms": round((now - audio_end_ts) * 1000.0),
        })
        if not needs_translation:
            return None
        job = {"id": seq, "text": result.text, "lang": lang,
               "target": self.target, "audio_end_ts": audio_end_ts}
        self._recent[seq] = job
        while len(self._recent) > 120:
            self._recent.popitem(last=False)

        # 命中时**不再**额外强译这条字幕。
        #
        # 报警框里已经有整段上下文的强模型译文了（见上面的 _translate_alert），
        # 中控出事时看的就是那里；再对同一段话强译一次字幕，等于为一次报警
        # 付两遍 7B，而每一次都在和 Whisper 抢内存、抬高报警延迟。
        # 事后复核用的逐段精确译文由 tools/retranslate_audit.py 补齐——
        # 那时没有识别在争资源，是零代价的。想当场看某条的精确译文，
        # 「重译」按钮仍然随时可用。

        return job

    # 定义在 translator.py（启动恢复引擎时也要用），这里保留同名类属性
    ENGINE_KEY_ENV = ENGINE_KEY_ENV

    async def set_engine(self, engine, key=None):
        """从界面切换翻译引擎、并（可选）存下密钥。

        密钥存进 settings.json（已在 .gitignore 里），**从不回传页面**——
        回传的只有打码后的尾四位，够用户确认「我填的是哪一个」，
        又不至于让密钥出现在任何一条 WebSocket 消息里。
        """
        from .settings import load_settings, save_setting
        from .translator import TRANSLATOR_CHOICES

        if engine not in TRANSLATOR_CHOICES:
            return
        if key:
            env = self.ENGINE_KEY_ENV.get(engine)
            if env:
                keys = dict(load_settings().get("api_keys", {}))
                keys[env] = key.strip()
                save_setting("api_keys", keys)
        try:
            new = create_translator(engine)
        except RuntimeError as exc:
            await self.server.broadcast({"type": "notice", "text": str(exc)})
            return
        old, self.translator = self.translator, new
        if old is not None and old is not new:
            try:
                await old.close()
            except Exception:
                pass
        self.args.translator = engine
        # 用户刚亲手选完引擎，启动时「引擎被回退」的提示不再适用
        self.args.translator_note = None
        save_setting("translator", engine)
        await self._publish_engine()
        await self.run_selfcheck()

    async def _publish_engine(self):
        """把当前引擎和各密钥的填写状态告诉页面（密钥只给尾四位）。"""
        from .settings import load_settings
        from .translator import mask_key

        stored = load_settings().get("api_keys", {})
        # DeepL 的月度用量：中控要能看着额度用（实测约 3.5 万字符/小时，
        # 免费档 100 万/月 ≈ 29 小时）。拿不到就不显示，最多等 3 秒
        usage = None
        inner = getattr(self.translator, "inner", self.translator)
        if getattr(inner, "name", "") == "deepl" and hasattr(inner, "usage"):
            try:
                usage = await asyncio.wait_for(inner.usage(), timeout=3)
            except Exception:
                usage = None
        info = {"engine": getattr(self.args, "translator", "auto"),
                "usage": usage,
                "active": getattr(self.translator, "name", None),
                # 启动时引擎被回退的提示（如「deepl 缺密钥，本次先用 auto」）。
                # 终端里 print 过一遍，但窗口应用的用户看不到终端
                "note": getattr(self.args, "translator_note", None),
                "keys": {env: mask_key(os.environ.get(env) or stored.get(env, ""))
                         for env in self.ENGINE_KEY_ENV.values()}}
        self.server.config["engine"] = info
        await self.server.broadcast({"type": "engine", **info})

    async def _translate_alert(self, alert_ids, context, lang=None):
        """把报警上下文翻成中文，回来后补进这一批报警（它们共用同一段上下文）。

        `lang` 是识别出的源语言。别图省事传 "auto"——DeepL 的原生术语表
        必须带明确的 source_lang 才生效，而报警恰恰是最不能把商品名翻错的
        地方（实测不挂术语表词表遵从率只有 26.5%）。"""
        from .translator import create_strong_translator, looks_fabricated

        if not context.strip():
            return

        async def tell(zh=None, why=""):
            """**每一条路径都要走到这里。** 报警框先画的是「翻译中…」，
            没有后续消息它就永远停在那儿——实盘里出现过一条卡了两分钟，
            而报警恰恰是中控最需要立刻做判断的地方。译不出来也要说译不出来，
            西语原话就在上面一行，中控还能自己看。"""
            for alert_id in alert_ids:
                await self.server.broadcast({"type": "alert_update",
                                             "alert_id": alert_id,
                                             "context_zh": zh or "",
                                             "failed": not zh,
                                             "why": why})

        if self._strong is None:
            self._strong = create_strong_translator()
        tr = self._strong or self.translator
        if tr is None:
            await tell(why="没有可用的翻译引擎")
            return
        try:
            hint = (tuple(self.glossary.translation_pairs(context))
                    if self.glossary else ())
            out = await tr.translate(context, self.target, source=lang or "auto",
                                     glossary=hint or None)
            if out and looks_fabricated(context, out):
                print("[警告] 报警上下文的译文不像译文（疑似模型在回话），"
                      "改用常规引擎")
                out = await self.translator.translate(
                    context, self.target,
                    source=lang or "auto") if self.translator else None
            elif out and self.glossary:
                out = self.glossary.apply(context, out)
        except Exception as exc:
            print("[警告] 报警上下文翻译失败: {}".format(exc))
            await tell(why="翻译超时或出错")
            return
        await tell(out, why="" if out else "模型没有返回译文")

    async def _quota_fallback(self, old):
        """DeepL 额度用尽时一次性切到本地引擎，返回新引擎（切不了返回 None）。

        用户在下拉框里的选择**不动**（settings 仍是 deepl）：额度是按月的，
        下月第一场直播启动恢复出 deepl、第一条翻译成功，一切自动回到原样；
        还没恢复的话，第一条 456 会再次走到这里。两种结局都不需要用户操心。
        """
        if self.translator is not old:
            return self.translator          # 已经切过，或用户手动换了引擎
        # create_translator("auto") 会同步探测 Ollama（urllib，最坏 ~10 秒）。
        # 它的注释写明是「启动时的一次性探测」——直播中途绝不能在事件循环里
        # 跑：音频读取、识别调度、报警广播、停止按钮全会被冻住。丢进线程池。
        loop = asyncio.get_running_loop()
        try:
            new = await loop.run_in_executor(None, create_translator, "auto")
        except Exception as exc:
            print("[警告] 额度降级失败，没有可用的备用引擎: {}".format(exc))
            return None
        if new is None:
            return None
        if self.translator is not old:
            # 探测期间用户手动换了引擎：尊重用户的选择，丢弃我们建的
            try:
                await new.close()
            except Exception:
                pass
            return self.translator
        self.translator = new
        try:
            await old.close()
        except Exception:
            pass
        name = getattr(new, "name", "?")
        if name == "google":
            # 不是本地引擎，字幕会发给 Google——合规工具的提示绝不能在
            # 「数据去哪了」这件事上含糊
            note = ("DeepL 本月免费额度已用完，且本机没有可用的本地模型，"
                    "已自动改用 Google 免费接口继续翻译——注意：字幕文本会"
                    "发送给 Google。额度下月重置后会自动回到 DeepL。")
        else:
            note = ("DeepL 本月免费额度已用完，本场已自动改用本地引擎（{}）"
                    "继续翻译。额度每月重置，下月开播会自动回到 DeepL。"
                    ).format(name)
        print("[警告] " + note)
        self.args.translator_note = note
        await self.server.broadcast({"type": "notice", "text": note})
        await self._publish_engine()
        return new

    async def _publish_translation(self, seq, translated, ok, ms, level,
                                   target, extra=None):
        """发布译文，并保证低等级不覆盖已生效的高等级结果。

        失败不占等级：强模型对约 2% 的句子返回空，若失败也占住等级，那一条
        就会被永久挡成空白，快译再也上不去。所以只有**成功**的结果才记等级。
        """
        current = self._quality.get(seq, 0)
        if ok and level < current:
            return False                  # 已有更好的结果在屏幕上，不倒退
        if not ok and current > 0:
            return False                  # 已有可用译文，别用一次失败把它擦掉
        if ok:
            self._quality[seq] = level
            while len(self._quality) > 300:
                self._quality.pop(next(iter(self._quality)))
        msg = {"type": "caption_update", "id": seq, "translated": translated,
               "translate_state": "ok" if ok else "failed",
               "target_lang": target, "translate_ms": round(ms),
               "quality": level}
        if extra:
            msg.update(extra)
        await self.server.broadcast(msg)
        return True

    async def retranslate(self, seq, trigger="manual"):
        """用本机最强的翻译模型重译某一条字幕。

        为什么是「按条」而不是「按时段」：值得动用强模型的是**具体某句话**
        （价格、促销条件、功效宣称），而那是内容驱动的，只有看着的人知道是
        哪一句。实测强模型装卸只要约 2 秒，按需调用完全划算；常驻反而会把
        识别从 1.4 秒拖到 3.2 秒，直接推高违禁词报警延迟。
        """
        from .translator import create_strong_translator, looks_fabricated

        job = self._recent.get(seq)
        if job is None:
            return
        if self._strong is None:
            self._strong = create_strong_translator()
        if self._strong is None:
            await self.server.broadcast({
                "type": "notice",
                "text": "没有可用的本地模型，无法重译（见首页自检的「翻译引擎」一项）"})
            return
        if self._quality.get(seq, 0) >= QUALITY_STRONG:
            return                        # 这一条已经是强模型译的，不重复
        if seq in self._strong_inflight:
            return                        # 同一条已经在跑了
        # 重译用**独立的状态位**，绝不复用普通翻译的 translate_state=pending。
        #
        # 复用会造成这样一条死路：已有快译在屏幕上 → 广播 pending → 前端把
        # 译文换成「翻译中…」→ 强模型恰好返回空（约 2% 会）→ 服务端正确地
        # 拒绝用失败覆盖已有译文，于是不再广播 → 页面永远停在「翻译中…」，
        # 而那条本来好好的快译已经被擦掉了。
        # 「一次失败不得擦掉已在屏幕上的译文」这条规则，必须同时管住中间态。
        self._strong_inflight.add(seq)
        had = self._quality.get(seq, 0) > 0
        await self.server.broadcast({"type": "caption_update", "id": seq,
                                     "strong_state": "pending"})
        t0 = time.time()
        hint = (tuple(self.glossary.translation_pairs(job["text"]))
                if self.glossary else ())
        try:
            out = await self._strong.translate(
                job["text"], job["target"], source=job["lang"] or "auto",
                glossary=hint or None)
            if out and looks_fabricated(job["text"], out):
                # 强模型在「长句 + 带疑问」上会改成回话而不是翻译。
                # 宁可留着快译，也不能把凭空生成的内容写进屏幕和审计记录。
                print("[警告] 重译结果不像译文（疑似模型在回话），已丢弃")
                out = None
            elif out and self.glossary:
                out = self.glossary.apply(job["text"], out)
        except Exception as exc:
            print("[警告] 重译失败: {}".format(exc))
            out = None
        ms = (time.time() - t0) * 1000.0
        self._strong_inflight.discard(seq)
        if self.audit is not None:
            self.audit.translation_strong(seq, out, ms, bool(out),
                                          self._strong.model, trigger)
        if out:
            await self._publish_translation(seq, out, True, ms, QUALITY_STRONG,
                                            job["target"],
                                            {"strong": True, "strong_state": "ok"})
        elif had:
            # 已有译文时：只报告重译失败，屏幕上那一版原样留着
            await self.server.broadcast({"type": "caption_update", "id": seq,
                                         "strong_state": "failed"})
        else:
            # 连快译都还没有：这时才是真的「这条没有译文」
            await self._publish_translation(seq, None, False, ms, QUALITY_STRONG,
                                            job["target"], {"strong_state": "failed"})

    async def _translate_and_update(self, job):
        """翻译回来后原地更新那一条字幕（按 id）。失败只影响这一条。"""
        t0 = time.time()
        # 引擎抓一次快照：中途在界面里换引擎时，这一条从翻译到落日志必须
        # 始终指同一个对象，否则审计里的 engine 会记成换挡后的那个
        tr = self.translator
        try:
            # 词表两处生效：把本句命中的词条拼进提示词，译文回来再做兜底替换。
            # 商品名/自造词（Quema Lonja、moringa）通用模型必错，而且换多大的
            # 模型都不会自动变对——这类错误只能靠词表钉死。
            hint = (tuple(self.glossary.translation_pairs(job["text"]))
                    if self.glossary else ())
            translated = await tr.translate(
                job["text"], job["target"], source=job["lang"] or "auto",
                glossary=hint or None)
            if self.glossary:
                translated = self.glossary.apply(job["text"], translated)
        except Exception as exc:
            print("[警告] 翻译失败: {}".format(exc))
            translated = None
        # DeepL 月额度用尽（456）不是这一条的问题，是这个月的问题：不切换的话
        # 后面每条字幕都会「翻译失败」直到月底。切到本地引擎，并用新引擎把
        # 当前这条立刻补上——它不该成为切换的牺牲品。
        if translated is None and getattr(tr, "quota_exhausted", False):
            fallback = await self._quota_fallback(tr)
            if fallback is not None:
                tr = fallback
                # translate_ms 只记翻译本身：引擎切换的开销不该算进这一条的
                # 翻译耗时去污染延迟统计（e2e_translated_ms 仍如实含全部等待）
                t0 = time.time()
                try:
                    hint = (tuple(self.glossary.translation_pairs(job["text"]))
                            if self.glossary else ())
                    translated = await tr.translate(
                        job["text"], job["target"], source=job["lang"] or "auto",
                        glossary=hint or None)
                    if self.glossary:
                        translated = self.glossary.apply(job["text"], translated)
                except Exception as exc:
                    print("[警告] 降级引擎翻译失败: {}".format(exc))
                    translated = None
        translate_ms = (time.time() - t0) * 1000.0
        self.telemetry.record_translation(translate_ms)
        if self.audit is not None:
            self.audit.translation(job["id"], translated, translate_ms,
                                   bool(translated),
                                   engine=getattr(tr, "name", None))
        await self._publish_translation(
            job["id"], translated, bool(translated), translate_ms,
            QUALITY_FAST, job["target"],
            {"e2e_translated_ms": round((time.time() - job["audio_end_ts"]) * 1000.0)})

    # ---- 演示模式 ----
    async def start_demo(self):
        """演示同样挂到 _stream_task 上：这样「停止」能真的停下来，
        换成真实直播时旧循环也会被先取消（否则真假字幕会交错广播）。"""
        async with self._lock():
            await self._stop_locked(quiet=True)
            self._stream_task = asyncio.create_task(self.run_demo())

    async def run_demo(self):
        await self.server.status("connecting", "演示模式启动中…")
        await asyncio.sleep(1.0)
        await self.server.status("live", "演示模式：内置台词模拟直播字幕（未连接真实直播）")
        while True:
            for original, translated in DEMO_SCRIPT:
                self._counter += 1
                seq = self._counter
                # 与真实链路一致：先出原文，再补翻译（顺便演示 caption_update）
                await self.server.broadcast({
                    "type": "caption",
                    "id": seq,
                    "ts": time.time(),
                    "original": original,
                    "translated": None,
                    "translate_state": "pending",
                    "src_lang": "en",
                    "target_lang": self.target,
                    "demo": True,
                })
                await asyncio.sleep(0.4)
                await self.server.broadcast({
                    "type": "caption_update",
                    "id": seq,
                    "translated": translated,
                    "translate_state": "ok",
                    "target_lang": self.target,
                    "translate_ms": 400,
                })
                await asyncio.sleep(2.4)
