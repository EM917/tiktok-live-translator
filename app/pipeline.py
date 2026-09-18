"""管线编排：拉流 → (降噪) → 切段 → 语音识别 → 翻译 → 广播给 UI。

支持两种启动方式：命令行传直播间地址，或在网页 UI 里输入地址点「开始」。
同一时间只跑一个直播间；切换房间时旧任务被取消，识别模型跨房间复用不重复加载。
"""
import asyncio
import os
import subprocess
import time
import traceback
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .asr import DEFAULT_TEMPERATURE
from .comment_source import CommentSource
from .comments import CommentTranslator
from .detector import BannedTermDetector, TermsFile, load_fuzzy_policy, read_terms
from .glossary import load as load_glossary
from .nethttp import read_all
from .redact import strip_query
from .settings import (load_settings, push_recent_room, recent_rooms,
                       save_setting)
from .telemetry import Telemetry
from .translator import ENGINE_KEY_ENV, create_translator

ROOT = Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / "banned_terms.txt"
TERMS_EXAMPLE = ROOT / "banned_terms.example.txt"
# 逐词模糊预算 policy：随代码入库、随更新分发（不走用户副本——词表模板的
# 教训：模板升级触达不了用户已复制的文件，而碰撞是全语言事实，不是用户偏好）
FUZZY_POLICY_FILE = ROOT / "banned_fuzzy_policy.txt"
# 空闲期攒下的手机同看审计事件上限，与 app/viewer.VIEWER_AUDIT_PENDING_MAX 一致
VIEWER_AUDIT_PENDING_MAX = 100
VIEWER_IP_TIMEOUT_SEC = 3.0      # 取本机地址放执行器里跑，最多等这么久
VIEWER_STOP_TIMEOUT_SEC = 2.0    # 退出收尾时关同看的预算：不得拖住退出


def load_detector(path=None):
    """读取违禁词表。首次运行时从模板复制一份用户可编辑的副本——
    模板入库、副本不入库，用户编辑不会挡住一键更新。

    **从不抛异常。** 以前非 UTF-8 的词表（Windows 上 ANSI/GBK 编辑器另存）让它抛
    UnicodeDecodeError：启动时整个程序起不来，开播时会话在建审计文件之前就停了，
    界面上只有一句英文。读的过程中发现的问题记在检测器上，由自检那一行和
    session_start 报出来。"""
    target = Path(path) if path else TERMS_FILE
    try:
        if not target.exists() and target == TERMS_FILE and TERMS_EXAMPLE.exists():
            try:
                target.write_text(TERMS_EXAMPLE.read_text(encoding="utf-8"),
                                  encoding="utf-8")
                print("[信息] 已生成违禁词表 {}（当前为空，按文件里的说明填写即可）"
                      .format(target.name))
            except (OSError, ValueError):
                pass
        info = read_terms(target)
        try:
            policy = load_fuzzy_policy(FUZZY_POLICY_FILE)
        except (OSError, ValueError) as exc:
            # policy 只会收紧模糊匹配：读不出来就按默认预算，多报不漏报
            print("[警告] 读不出 {}，本场模糊匹配按默认预算：{}".format(
                FUZZY_POLICY_FILE.name, exc))
            policy = {}
        detector = BannedTermDetector([line for _, line in info.entries],
                                      fuzzy_policy=policy,
                                      line_numbers=[n for n, _ in info.entries])
    except Exception as exc:
        print("[错误] 违禁词表没能加载：{}".format(exc))
        info = TermsFile()
        info.read_error = str(exc)[:200]
        detector = BannedTermDetector([])
    detector.source_path = str(target)
    detector.source_hash = info.hash
    detector.source_mtime = info.mtime
    detector.decode_error = info.decode_error
    detector.skipped_lines = list(info.skipped_lines)
    detector.read_error = info.read_error
    if info.decode_error:
        print("[警告] {} 不是 UTF-8 编码，读不出的 {} 行已跳过".format(
            target.name, len(info.skipped_lines)))
    for warning in detector.load_warnings:
        if warning["reason"] != "invalid_regex":      # 这一种构造时已经打印过
            print("[警告] 违禁词表：" + warning["text"])
    return detector

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


def _media_label(url):
    """流地址只留主机+路径进日志：query 里的 sign/expire 就是能拉流的凭证。"""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(str(url))
        return (parts.netloc + parts.path) or str(url)[:80]
    except Exception:
        return "?"


def browser_only_message(retries, login=None):
    """解析以 kind=browser_only 收场时给中控的话。

    前半句是一直以来的事实（TikTok 不给、原因它不说明）；中间按这次借浏览器登录时的
    观察（ResolveError.login，{浏览器: 代码}）接上对应的一步——系统拒绝读取就给
    「完全磁盘访问权限」的步骤，没登录就说去登录，借到了登录仍然不给就照实说；
    最后是粘 .flv 地址的办法。只写观察和能照做的事。"""
    from .browser_login import browser_only_advice

    return ("TikTok 不把这个直播间的流地址给程序（代码 4003110），已自动重试 {} 次。"
            "不是网络或限流问题——同一时刻其它直播间正常，原因 TikTok 不说明；"
            "有时过一会儿再点「开始翻译」就好，有时整场都不给。".format(retries)
            + browser_only_advice(login)
            + "想现在就看：把直播间链接和浏览器里的 .flv 地址一起粘进来"
              "（中间空格隔开），一次约两周有效。")


def _describe_asr(cfg):
    """给人看的识别配置：「ct2/large-v3-turbo/cpu/int8」（auto 的部分省略）。"""
    parts = [cfg.get("backend"), cfg.get("model"), cfg.get("device"), cfg.get("compute_type")]
    return "/".join(str(v) for v in parts if v and v != "auto")


class _ASRSlot:
    """一场会话正在用的识别器，外加「连续出错后换一个」需要的上下文。

    asr_worker 换模型时改的是这里：_run_session 的重连循环每轮把同一个 slot 交给
    _stream_session，只改局部变量的话，重连后的下一轮又会用回那个坏掉的模型。
    reloaded 保证一场会话最多换一次，不在两个模型之间来回加载。"""

    def __init__(self, transcriber, config=None, key=None, audit=None):
        self.transcriber = transcriber
        self.config = dict(config or {})
        self.key = key
        self.audit = audit
        self.reloaded = False
        self.gave_up = False


def _arnndn_probe(model_path):
    """用 0.1 秒静音实测 arnndn 滤镜 + 模型能否初始化。

    一次探测同时覆盖两种真实故障：模型文件损坏（自动下载被截断——只验文件头
    magic 挡不住），以及 ffmpeg 不支持 arnndn（imageio-ffmpeg 的静态版没编译
    librnnoise）。两种情况下直接把坏参数交给拉流 ffmpeg，会让每一轮会话都
    「Error initializing filters」失败，再被自动重连放大成误导报错。"""
    from .ffmpeg_bin import filter_path, find_ffmpeg
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return False
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.1",
             "-af", "arnndn=m={}".format(filter_path(model_path)),
             "-f", "null", "-"],
            capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False
RNNOISE_URL = ("https://raw.githubusercontent.com/GregorR/rnnoise-models/master/"
               "beguiling-drafter-2018-08-30/bd.rnnn")

# 演示模式的内置台词（英文原文 + 中文译文），用于在没有直播时验证 UI
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



def _tiktoklive_version():
    """读已安装的弹幕组件版本（只读包元数据）。放成模块函数，测试好替换。"""
    from .updater import tiktoklive_version
    return tiktoklive_version()

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
        # 「最近直播间」：启动就摆在首页，中控不必每次重新粘地址。只存主播名
        # 和地址，界面上只显示主播名（见 web/app.js renderRecentRooms）
        self.server.config["recent_rooms"] = recent_rooms()
        self._counter = 0
        self._asr_pool = None            # 每条直播一个独立线程池，停止时整个丢弃
        self._stream_task = None
        self._media_override = None      # 本场用户指定的音频源直连地址（可选）
        # Python 3.9 的 asyncio.Lock() 构造时就要绑事件循环，而 Pipeline 可能
        # 在循环外构造（如测试）——惰性初始化，首次使用时必然已在循环内
        self._stream_lock = None
        self._transcriber = None
        self._transcriber_key = None     # 已就绪模型对应的配置 key
        self._loading_key = None         # 在途加载对应的配置 key（可能被取消）
        self._transcriber_future = None  # 正在加载中的模型，避免重复加载
        self._asr_load_error = None      # 最近一次识别模型没能加载的详情，按配置加载成功后清掉
        self._asr_fallback = None        # 出错后改用的识别配置 {from,to,error}；None = 原配置
        self._asr_inflight = None        # 正在识别的那一段的开始时刻 [monotonic]，卡住检测用
        self._asr_failing = None         # 识别连续出错、提示还挂着的那一场的 audit
        self._fallback_load = None       # 在途的「改用 CPU」加载：被停止打断时下一场接着用
        self._resolve_fail_streak = 0    # 连续解析失败计数（触发 yt-dlp 自动保鲜）
        self._bg_tasks = set()           # fire-and-forget 任务的引用，见 _spawn
        self.telemetry = Telemetry()
        self.detector = load_detector(getattr(args, "banned_terms", None))
        self.glossary = load_glossary(getattr(args, "glossary", None))
        self.audit = None                # 每条直播一个审计日志文件
        self._stats_task = None
        self._selfcheck_task = None      # 持有引用，否则任务可能被 GC 掉
        self._provision_task = None
        self._comments_provision_task = None   # 弹幕组件（TikTokLive）后台安装任务，main.py 注入
        # 最近若干条字幕的原文，供「重译」按 id 取回。只留少量：这个功能是
        # 给中控看到可疑一句时临时用的，不是历史检索。
        self._recent = OrderedDict()
        self._strong = None              # 按需创建，用完不常驻
        self._strong_missing = False     # 本场探测过没有强模型；换场重探
        self._upgrade_tasks = []         # 报警触发的重译，持有引用防 GC
        self._quality = {}               # seq -> 当前已生效译文的质量等级
        self._strong_inflight = set()    # 正在跑强模型的 seq，防同一条重复触发
        self._alert_seq = 0              # 报警编号，供译文回来后对上号
        self._alert_tasks = []
        # 手机同看（app/viewer.py）。默认关闭，settings 里存过才在启动时恢复。
        # 这把锁是专用的，不复用开播那把：开/关同看不该和 start/stop 互相等
        self._viewer_lock_obj = None
        self._viewer_want = False        # 中控最后一条指令想要的终态（锁外记、锁内复核）
        self._viewer_ip = None           # 已发布的那个局域网地址，地址变了要重出二维码
        self._viewer_pinned_ip = None    # 中控在多地址里手工点过的那一个
        self._viewer_ip_task = None
        # 空闲期（还没开播、没有 audit）的同看事件先攒着，_begin_session 之后补写：
        # 合规证据不能因为「当时没在监听」就没了
        # maxlen 与 viewer.VIEWER_AUDIT_PENDING_MAX 同步（这里不 import viewer：
        # 它拉进 aiohttp，而 pipeline 的模块级导入刻意保持轻量）
        self._viewer_audit_pending = deque(maxlen=VIEWER_AUDIT_PENDING_MAX)
        if self.detector.enabled:
            print("[信息] 违禁词检测已启用：{} 个词条".format(self.detector.count))
        else:
            print("[信息] 违禁词表为空——编辑 banned_terms.txt 后重新「开始翻译」即可启用")
        # 观众弹幕翻译：独立于字幕主链路的 Pipeline 级协程，跨场次常驻。
        # 只用当前常驻的快速引擎（下面两个 getter 直接读 self.translator /
        # self.target，热切换时弹幕翻译立刻跟着变，不用重启）；字幕翻译
        # 排队或在途时弹幕让路，见 _subtitle_translation_busy。
        self._subtitle_busy = 0          # 在途的字幕翻译数（弹幕翻译要让路）
        self.comments = CommentTranslator(
            broadcast=self.server.broadcast,
            translator=lambda: self.translator, target=lambda: self.target,
            glossary=lambda: self.glossary, busy=self._subtitle_translation_busy,
            session=lambda: self.audit)     # 「弹幕只显示原文」的提示每场说一次
        # 弹幕抓取（TikTokLive，见 app/comment_source.py）：与上面的
        # CommentTranslator 是两回事——这里只负责把观众评论从 TikTok 的
        # WebSocket 弄到本地，弄到后喂给 self.comments.accept()。
        # 出错只影响弹幕，绝不碰音频/检测/审计链路。
        self.comment_source = CommentSource(
            on_items=lambda items: self.comments.accept(
                {"type": "viewer_comments", "items": items}),
            on_state=self._publish_comment_source,
            cookies_browser=getattr(args, "cookies_browser", "auto"))
        # on_provision 惰性读 self.updater：Pipeline 构造时 main.py 还没把
        # updater 注入进来（见 main.py 里 `pipeline.updater = updater` 那行），
        # 但这个回调只在真正需要装组件时才会被调用，那时 updater 早已就绪。
        self.comment_source.on_provision = lambda: (
            self.updater.ensure_tiktoklive("comments")
            if getattr(self, "updater", None) is not None else None)
        # 评论连接被服务端拒绝时找组件的补丁版本（和解析失败时升 yt-dlp 同一个思路）
        self.comment_source.on_stale = lambda reason, announce=None: (
            self.updater.freshen_tiktoklive(reason, announce=announce)
            if getattr(self, "updater", None) is not None else None)

    def _subtitle_translation_busy(self):
        """字幕翻译是否正忙（在途或排队）——弹幕翻译据此让路，最多等 3 秒。"""
        return self._subtitle_busy > 0 or self.telemetry.translation_queue_depth > 0

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
            # 可选的音频源直连地址：有些直播间 TikTok 不把流地址给程序（见
            # _resolve_media），这时用户可以把浏览器里的 .flv/.m3u8 地址一并
            # 贴进来。房间链接仍然是主输入——弹幕、词表、审计都认它。
            media = str(msg.get("media", "") or "").strip() or None
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
                self._note_operator_stream_action()
                return self._start_with_ack(url, media=media)
            # 不合规的地址以前是被静默丢弃的——用户点了「开始」却毫无反应
            return self.server.status(
                "error", "地址无效：请填写 http:// 或 https:// 开头的直播间地址")
        elif mtype == "stop":
            self._note_operator_stream_action()
            self._stop_reason = "user_stop"
            return self.stop_stream()
        elif mtype == "clear_recent_rooms":
            return self._clear_recent_rooms()
        elif mtype == "disk_inventory":
            return self._publish_disk()
        elif mtype == "disk_delete":
            ids = msg.get("ids")
            return self._disk_delete([str(x) for x in ids] if isinstance(ids, list) else [])
        elif mtype == "set_engine":
            return self.set_engine(msg.get("engine"), msg.get("api_key"))
        elif mtype == "retranslate":
            return self.retranslate(msg.get("id"))
        elif mtype == "migrate_glossary":
            return self._migrate_glossary(bool(msg.get("confirm")))
        elif mtype == "apply_update":
            if getattr(self, "updater", None) is not None:
                # 放后台跑，不在这个页面的消息循环里等：fetch、pip 要好几分钟，等着的话
                # 这期间点的「停止」要排到更新做完才处理——而更新成功就直接重启了
                self._spawn(self._apply_update())
                return None
        elif mtype == "check_update":
            if getattr(self, "updater", None) is not None:
                return self.updater.check_and_notify(delay=0, manual=True)
        elif mtype == "viewer_share":
            return self._set_viewer_share(bool(msg.get("on")))
        elif mtype == "viewer_rotate":
            return self._rotate_viewer_token()
        elif mtype == "viewer_pick_ip":
            # 本机有多个网络地址时中控点了另一个：用它重画二维码。
            # 程序这边分不出哪个地址手机能到——让人试，比让程序猜靠谱
            return self._pick_viewer_ip(msg.get("ip"))
        return None

    # 一键更新暂停监听后，新进程要在这么久之内起来，才自动接着监听（记号读到即删）
    RESUME_AFTER_UPDATE_MAX_SEC = 300

    async def _apply_update(self):
        """一键更新（步骤见 Updater._apply_inner）。以前是预检一过就停直播、再无时限地
        git pull：网络卡住时监听已经停了，界面还显示「直播中」；重启后回到待机，审计里
        只剩一条 session_end。现在：新版本取下来、确认能快进了才暂停监听；装依赖或合并
        失败就在当前版本上恢复；成功就留记号，重启后自动接着听。"""
        if getattr(self.updater, "_applying", False):
            return          # 已经有一次在跑：别动它正在用的暂停记录和横幅
        incidents = (getattr(self.server, "config", {}) or {}).get("incidents") or {}
        if "update" in incidents:
            await self._incident("update", "clear")    # 上次没更新成的说明，重试时收起
        self._update_pause = None
        try:
            await self.updater.apply(live=self._stream_active, pause=self._pause_for_update,
                                     resume=self._resume_after_update_failure,
                                     before_restart=self._save_update_resume)
        except Exception as exc:
            # 更新器里没料到的异常：暂停了的监听不能就这么一直停着
            print("[警告] 一键更新出错：{!r}".format(exc))
            pause = getattr(self, "_update_pause", None)
            if pause is not None:
                await self._resume_after_update_failure(
                    pause, "更新过程中程序出错（{}）".format(type(exc).__name__))
        finally:
            self._update_pause = None
            # 更新期间推迟的模型下载：走到这里说明没有重启（没更新成、或中控自己停了监听）
            if getattr(self, "_pull_deferred", None) and hasattr(self, "_bg_tasks") \
                    and not self._stream_active():
                self._spawn(self._pull_after_session(None))

    def _note_operator_stream_action(self):
        """中控在一键更新暂停监听期间自己点了开始或停止（handle_control 调用）。更新收尾时
        照中控最后的操作来：失败了不替他恢复监听，成功了也不留「重启后接着监听」的记号。"""
        pause = getattr(self, "_update_pause", None)
        if pause is not None:
            pause["operator_acted"] = True

    async def _pause_for_update(self, from_version, to_version, pip_minutes=None):
        """新版本已经取下来、确认能更新之后才调用：记审计、停监听、告诉中控在更新。
        pip_minutes 是要先装组件时 pip 的最长时限（分钟），不用装是 None。
        返回恢复监听要用的 {url, media}；没在监听（或演示模式）返回 None。"""
        if not self._stream_active() or getattr(self.args, "demo", False):
            return None
        token = {"url": (self.server.config or {}).get("room_url"),
                 "media": getattr(self, "_media_override", None)}
        self._update_pause = token
        if self.audit is not None:
            self.audit.update_stop(from_version, to_version)
        self._stop_reason = "update"
        await self.stop_stream(quiet=True)
        if pip_minutes:
            text = ("正在更新，监听已暂停：先安装新版本需要的组件（最长约 {} 分钟），"
                    "装好后自动重启并恢复监听".format(pip_minutes))
        else:
            text = "正在更新，监听已暂停（约 1 分钟后自动恢复）"
        await self.server.status("connecting", text)
        return token

    async def _resume_after_update_failure(self, token, text):
        """停了监听之后更新没成：工作区还是旧代码，在当前版本上把监听恢复起来，并用持续
        提示说清楚（状态行马上会被「连接中/直播中」盖掉，一次性提示几秒就没了）。

        更新期间中控自己点过开始/停止，或者此刻已经在监听：照中控的意思，不再替他开始。"""
        if load_settings().get("resume_after_update") is not None:
            save_setting("resume_after_update", None)   # 这个进程接着听，不再靠重启后恢复
        self._update_pause = None
        token = token or {}
        url = token.get("url")
        if token.get("operator_acted") or self._stream_active():
            await self._incident("update", "warn", "一键更新没完成：{}".format(text))
            return
        if not url:
            await self.server.status("idle", text)
            return
        await self._incident("update", "warn",
                             "一键更新没完成：{}。已在当前版本上自动恢复监听".format(text))
        self._resume_reason = "update_failed"
        await self.start_stream(url, media=token.get("media"))

    def _save_update_resume(self, token, to_version):
        """execv 之前留记号：新进程启动时据此接着监听（见 resume_after_update）。
        返回是否留了记号。更新期间中控点过开始/停止的不留：重启后照他最后的操作来。"""
        if not token or not token.get("url"):
            return False
        if token.get("operator_acted"):
            print("[信息] 更新期间中控点过开始/停止，重启后不自动接着监听")
            return False
        save_setting("resume_after_update", {"url": token["url"], "media": token.get("media"),
                                             "at": time.time(), "to_version": to_version})
        return True

    async def resume_after_update(self, now=None):
        """启动时调用（main.py，命令行没给直播间地址时）：上一个进程是一键更新暂停的监听，
        而且在 RESUME_AFTER_UPDATE_MAX_SEC 之内重启回来了，就接着监听那个房间。

        记号读到就删、过期不用：绝不能留着让以后哪次启动莫名其妙开始监听。返回是否已恢复。"""
        marker = load_settings().get("resume_after_update")
        if marker is None:
            return False
        save_setting("resume_after_update", None)
        if not isinstance(marker, dict):
            return False
        url = str(marker.get("url") or "")
        try:
            at = float(marker.get("at"))
        except (TypeError, ValueError):
            at = None
        now = time.time() if now is None else now
        if not url.startswith(("http://", "https://")) or at is None \
                or not (-60 <= now - at <= self.RESUME_AFTER_UPDATE_MAX_SEC):
            print("[信息] 一键更新留下的「重启后接着监听」记号已超过 {} 分钟或内容不对，没有自动开始"
                  .format(self.RESUME_AFTER_UPDATE_MAX_SEC // 60))
            return False
        from .provenance import app_version, streamer_of
        media = marker.get("media")
        streamer = streamer_of(url)
        self._resume_reason = "update"
        await self.server.status("connecting", "已更新到 v{}，正在自动恢复监听{}…".format(
            app_version(), " @" + streamer if streamer else ""))
        await self.start_stream(url, media=media if isinstance(media, str) and media else None)
        return True

    def note_component_updated(self, name, before, after, reason):
        """后台升级了解析组件（Updater.freshen_ytdlp）：有正在进行的会话就记进它的审计。"""
        audit = getattr(self, "audit", None)
        if audit is not None:
            audit.component_updated(name, before, after, reason)

    def _update_session_extras(self):
        """session_start 里与组件和更新有关的几项。resumed_after 只用一次：update 表示
        一键更新重启后自动接上的这一场，update_failed 表示更新没成、在旧版本上恢复的。"""
        from .updater import component_version, update_check_ok_iso
        reason = getattr(self, "_resume_reason", None)
        self._resume_reason = None
        return {
            "ytdlp_version": component_version("yt-dlp"),
            "curl_cffi_version": component_version("curl_cffi"),
            "update_check_ok_at": update_check_ok_iso(),
            "resumed_after": reason,
        }

    async def _start_with_ack(self, url, media=None):
        """UI 点「开始」后立刻回执——停掉旧管线可能要好几秒（等 ffmpeg 退出），
        期间不给任何反馈的话，用户会以为点了没反应而反复点。"""
        await self.server.status("connecting", "已收到指令，正在连接…")
        await self.start_stream(url, media=media)

    async def _strong_translator(self):
        """按需创建强模型翻译器；本场探测过「没有」就不再探——探测是同步 urllib，
        直播中每条报警都在事件循环上重探一遍，每次最坏几秒，直接加到检测延迟上。"""
        from .translator import create_strong_translator

        if self._strong is None and not self._strong_missing:
            loop = asyncio.get_running_loop()
            self._strong = await loop.run_in_executor(None, create_strong_translator)
            if self._strong is None:
                self._strong_missing = True
        return self._strong

    def _spawn(self, coro):
        """起一个不等结果的任务，但保住引用。事件循环只弱引用任务：不保引用的
        任务可能在半路被 GC 收走（Python 文档明写）。以前三处裸
        ensure_future——「这条不会有译文了」的 caption_update 若被收走，界面上那条
        字幕永远停在「翻译中…」，恰是那段代码想防的事；yt-dlp 保鲜同理会静默失踪。"""
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

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

    async def start_stream(self, url, media=None):
        async with self._lock():
            if not getattr(self, "_stop_reason", None):
                self._stop_reason = "new_session"    # 正在跑的那一场（如果有）因为开了新的一场而结束
            await self._stop_locked(quiet=True)
            # 本场的音频源直连地址（可选）。设在 _stop_locked 之后：停旧场会把它
            # 清掉，免得上一场的地址泄漏到这一场。
            self._media_override = media
            self.server.config["room_url"] = url
            self._save_setting("room_url", url)
            # source_lang 捎在同一条 config 里：开着的第二个页面也要跟上，
            # 不能等它重连才看到第一个页面刚选的语言
            await self.server.broadcast({"type": "config", "room_url": url,
                                         "source_lang": getattr(self.args, "source",
                                                                None) or "auto"})
            # Ollama 没跑就趁解析地址/加载模型这几秒把它拉起来，别等第一句翻译失败
            self._spawn(self._heal_local_engine())
            self._stream_task = asyncio.create_task(self._run_stream(url))
            # 任务第一步还没跑：_begin_session 能把拿到了哪些防睡眠手段记进 session_start
            self._hold_sleep_guard(self._stream_task)

    async def stop_stream(self, quiet=False):
        async with self._lock():
            await self._stop_locked(quiet=quiet)

    # 等旧管线收尾的上限。超过就不等了——见 _stop_locked 里的说明。
    STOP_GRACE_SEC = 3.0

    async def _stop_locked(self, quiet=False):
        """调用方必须已持有 _stream_lock。

        停止原因（写进 session_end 的 reason）由发起停止的一方在调用前放进
        self._stop_reason（user_stop / window_closed / update / new_session）；没放就按
        quiet 记成 stopped 或 user_stop。被取消的任务收尾时读它，这里用完清掉。"""
        if not getattr(self, "_stop_reason", None):
            self._stop_reason = "stopped" if quiet else "user_stop"
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
        # 弹幕来源也在这里停，不能只指望旧任务的 _end_session：识别线程卡住时
        # 上面的等待会超时撒手，这里随即把 audit 置空，等旧任务终于走到
        # _end_session 时它已经「不是当前会话」了，于是弹幕子进程和它的
        # WebSocket 一直挂到下一场——界面早就显示已停止。stop() 可重入。
        comment_source = getattr(self, "comment_source", None)
        if comment_source is not None:
            await comment_source.stop()
        closed = self.audit
        if closed is not None:
            self._close_audit(closed, self._stop_reason)
            self.audit = None
        self._stop_reason = None
        self._release_sleep_guard()
        if closed is not None:
            await self._settle_audit_incident(closed)
        # 旧任务可能超时没走到 _end_session（识别线程停不下来），这里也撤一次
        await self._clear_ongoing_incidents()
        await self._settle_engine_incident()
        self._media_override = None
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
            traceback.print_exc()      # 终端要能看到堆栈——这次就是没堆栈才查了半天
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
                await self._check_clock_gap(interval)
                snap = self.telemetry.snapshot()
                level = self._health_level(snap["audio_backlog_sec"])
                snap["health"] = level
                await self.server.broadcast({"type": "stats", **snap})
                if level != last_level:
                    await self._announce_health(level, snap["audio_backlog_sec"])
                    last_level = level
                await self._watch_detection(snap)
                await self._check_audit_health()
                self._asr_memory_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    ASR_MEMORY_EVERY_SEC = 300.0

    def _asr_memory_tick(self, now=None):
        """_stats_loop 每一跳调一次；到 5 分钟才真的写。只认当前这一场自己的观察状态
        （同 _check_clock_gap）。now 是单调时钟读数，只给测试用。不抛异常。"""
        try:
            sess = getattr(self, "_session_state", None)
            audit = getattr(self, "audit", None)
            if sess is None or audit is None or sess.get("audit") is not audit:
                return False
            return self._record_asr_memory(sess, getattr(self, "_transcriber", None),
                                           now=now, periodic=True)
        except Exception as exc:
            print("[警告] 记录 MLX 内存读数出错：{}".format(exc))
            return False

    def _record_asr_memory(self, sess, transcriber, now=None, periodic=False):
        """往**这一场自己的**审计（sess["audit"]，不读 self.audit）写一条 asr_memory。

        只有 mlx 后端写；读数来自 transcriber.memory_stats()（只读计数器）。在事件循环里
        调，不进识别线程：识别那条路上不多一步。periodic=True 是统计循环的调用：模型就绪
        那一条写过之后，每 ASR_MEMORY_EVERY_SEC 秒一条。读数出错也算写过这一轮，
        不每 10 秒重试一遍。任何异常都不抛——统计循环遇到异常会整个退出。"""
        try:
            audit = sess.get("audit") if sess else None
            if audit is None or getattr(transcriber, "backend", None) != "mlx":
                return False
            read = getattr(transcriber, "memory_stats", None)
            if not callable(read):
                return False
            now = time.monotonic() if now is None else now
            last = sess.get("asr_memory_at")
            if periodic and (last is None or now - last < self.ASR_MEMORY_EVERY_SEC):
                return False
            sess["asr_memory_at"] = now
            stats = read() or {}
            audit.asr_memory(stats.get("active_mb"), stats.get("cache_mb"),
                             stats.get("peak_mb"))
            return True
        except Exception as exc:
            print("[警告] 记录 MLX 内存读数出错：{}".format(exc))
            return False

    @staticmethod
    def _health_level(backlog_sec):
        if backlog_sec >= AUDIO_BACKLOG_DEGRADED_SEC:
            return "degraded"
        if backlog_sec >= AUDIO_BACKLOG_WARN_SEC:
            return "lagging"
        return "ok"

    async def _incident(self, key, level, text=""):
        """在界面顶部持续显示、直到明确清除的提示（电脑休眠过、审计日志写不进去、
        识别改用 CPU、网络断了……）。key 相同的覆盖同一条，level="clear" 去掉。

        和 health（识别积压，会被「已追上」覆盖）、notice（几秒后消失）不同：这类状况
        中控必须看到，刷新页面也还在。key 以 "session:" 开头的只属于这一场，下一场
        开始时自动清掉；其余的一直留到发出 clear。文字只写观察到的事实和能做的事。"""
        if level == "clear":
            print("[提示] {} 已清除".format(key))
        else:
            print("[提示] {}".format(text))
        await self.server.broadcast({"type": "incident", "id": key, "level": level,
                                     "text": text, "ts": time.time()})

    async def _clear_session_incidents(self):
        incidents = (getattr(self.server, "config", {}) or {}).get("incidents") or {}
        for key in [k for k in list(incidents) if str(k).startswith("session:")]:
            await self._incident(key, "clear")

    async def _announce_health(self, level, backlog_sec, text=None, reason="backlog"):
        """推一条检测健康状态。text 不给时按积压等级生成。

        同一句话已经在界面上时不重发（统计循环和识别卡住检查可能在同一轮说同一件事）；
        等级或原因变了才写进审计——审计要的是转折点，不是每轮刷新。

        识别连续出错的提示挂着时，按积压算的状态不覆盖它：出错的调用返回得快、积压归零，
        不拦的话统计循环会在检测停摆时报「已追上」，审计里也记一条 ok。"""
        audit = getattr(self, "audit", None)
        if (reason == "backlog" and audit is not None
                and getattr(self, "_asr_failing", None) is audit):
            return
        telemetry = getattr(self, "telemetry", None)
        # 只数积压挤掉的段：识别出错没检测的段不是「积压超过 60 秒的旧音频」，另有出错提示
        asr_failed = getattr(telemetry, "audio_segments_asr_failed", 0) or 0
        dropped = max(0, (getattr(telemetry, "audio_segments_dropped", 0) or 0) - asr_failed)
        if text is None:
            if level == "degraded":
                # 以前这里一直写「仍在继续处理（不会漏掉这段音频）」，而积压过 60 秒 _put
                # 已经在丢段、统计条上同时显示「丢音频 N」——界面自相矛盾，正是 08-31 那类事故
                if dropped:
                    text = ("🔴 检测已降级：识别落后 {:.0f} 秒；积压超过 {:.0f} 秒的旧音频"
                            "已丢弃 {} 段，这些音频没有做违禁词检测"
                            .format(backlog_sec, AUDIO_BACKLOG_HARD_SEC, dropped))
                else:
                    text = ("🔴 检测已降级：识别落后 {:.0f} 秒，仍在继续处理；积压超过 {:.0f} 秒"
                            "会开始丢弃最旧的音频".format(backlog_sec, AUDIO_BACKLOG_HARD_SEC))
            elif level == "lagging":
                text = "⚠️ 识别开始落后（积压 {:.0f} 秒），报警会相应延迟".format(backlog_sec)
            else:
                text = "✅ 识别已追上，检测恢复正常"
        if getattr(self, "_health_shown", None) == (audit, level, text):
            return
        self._health_shown = (audit, level, text)
        print("[健康] " + text)
        await self.server.broadcast({"type": "health", "level": level,
                                     "backlog_sec": round(backlog_sec, 1),
                                     "text": text})
        if audit is not None and getattr(self, "_health_audited", None) != (audit, level, reason):
            self._health_audited = (audit, level, reason)
            audit.health(level, backlog_sec, reason=reason, text=text, dropped=dropped,
                         asr_failed=asr_failed)

    ASR_STALL_SEC = 60.0

    async def _watch_detection(self, snap, now=None):
        """_stats_loop 每轮调一次的检测侧检查：识别调用卡住、直播中违禁词表被改。
        任何一项出错都只打一行日志——_stats_loop 遇到异常会整个退出，统计条就停了。"""
        try:
            await self._check_asr_stall(snap, now=now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[警告] 识别卡住检查出错：{}".format(exc))
        try:
            await self._check_terms_changed()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[警告] 违禁词表变更检查出错：{}".format(exc))

    async def _check_asr_stall(self, snap, now=None):
        """一段音频的识别调用 ASR_STALL_SEC 秒还没返回：写一条 asr_stalled，在界面上说清。

        **不自动重载模型。** 线程池里的调用取消不掉，它会一直抓着自己那份模型；这时再
        加载一份等于两个大模型同时驻留——正是 2026-08-31 丢 16 段音频的事故形态。
        卡住期间丢段数一变就更新那句话；卡住结束时按积压重报一次；其余时候不重复发。"""
        audit = getattr(self, "audit", None)
        state = getattr(self, "_asr_watch", None)
        if state is None or state["audit"] is not audit:
            state = self._asr_watch = {"audit": audit, "stalled": False, "dropped": 0}
        # 只数积压挤掉的段。识别出错没检测的段也记在 audio_segments_dropped 里，算进来的话
        # 每次出错都触发下面的「按积压重报」，把红色的出错提示刷成「已追上」
        dropped = max(0, int(snap.get("audio_segments_dropped") or 0)
                      - int(snap.get("audio_segments_asr_failed") or 0))
        backlog = float(snap.get("audio_backlog_sec") or 0.0)
        grew = dropped > state["dropped"]
        state["dropped"] = dropped
        mark = getattr(self, "_asr_inflight", None)
        now = time.monotonic() if now is None else now
        inflight = now - mark[0] if mark else 0.0
        if inflight >= self.ASR_STALL_SEC:
            first = not state["stalled"]
            state["stalled"] = True
            if first and audit is not None:
                audit.asr_stalled(inflight_sec=inflight, backlog_sec=backlog, dropped=dropped)
            if first or grew:
                await self._announce_health(
                    "degraded", backlog, reason="asr_stalled",
                    text="🔴 一段音频识别已 {:.0f} 秒没有返回；积压超过 {:.0f} 秒的旧音频会被丢弃"
                         "（可能漏报），本场已丢弃 {} 段。若持续几分钟，请关闭程序重新打开——"
                         "「停止/开始」不会重新加载识别模型".format(
                             inflight, AUDIO_BACKLOG_HARD_SEC, dropped))
            return
        if state["stalled"] or grew:
            state["stalled"] = False
            await self._announce_health(self._health_level(backlog), backlog)

    async def _check_terms_changed(self):
        """直播中 banned_terms.txt 被改了：新内容要「停止→开始」后才生效，说一次。
        每次内容变化只提示一次；改回本场加载的那份不提示。"""
        detector = getattr(self, "detector", None)
        path = getattr(detector, "source_path", None)
        if not path:
            return
        audit = getattr(self, "audit", None)
        state = getattr(self, "_terms_watch", None)
        if state is None or state["audit"] is not audit or state["path"] != path:
            state = self._terms_watch = {"audit": audit, "path": path,
                                         "mtime": detector.source_mtime,
                                         "hash": detector.source_hash}
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            mtime = None
        if mtime == state["mtime"]:
            return
        state["mtime"] = mtime
        from .provenance import file_hash
        digest = file_hash(path)
        if digest == state["hash"]:
            return
        state["hash"] = digest
        if digest == detector.source_hash:
            return
        if audit is not None:
            audit.terms_changed(digest)
        text = "违禁词表已修改，点「停止」再「开始翻译」后生效"
        print("[提示] " + text)
        await self.server.broadcast({"type": "notice", "text": text})

    async def _run_stream_inner(self, url):
        before = self.audit
        try:
            await self._clear_session_incidents()     # 上一场的持续提示不属于这一场
            await self._begin_session(url)
        except Exception as exc:
            # 开场这一步出错（词表读不了、推给界面失败……）时下面的 try 还没进：防睡眠断言
            # 在 start_stream 里已经拿了，审计也可能已经建好。不在这里收尾，caffeinate 会
            # 一直挂着、审计文件只剩一个头。只认这一步自己新建的那份审计（my_audit 的道理）
            mine = self.audit if self.audit is not before else None
            if mine is not None:
                self._record_internal_error(mine, exc)
                await self._end_session(mine, reason="internal_error")
            else:
                self._release_sleep_guard(owner=asyncio.current_task())
            raise
        # 记住**本会话自己的** audit：旧流任务可能取消不掉（识别一段要几十秒
        # 时，3 秒宽限必然超时、_stop_locked 放手让它自行收尾），等它终于走到
        # finally 时，self.audit 已经是**下一场**的了——关掉它等于让新会话的
        # 合规证据从头到尾静默丢失（实录：2026-08-31 一场 102 条字幕的直播，
        # audit 文件只有 544 字节的头）。收尾只许碰自己那一份。
        my_audit = self.audit
        # 这一场自己的观察状态（断流时刻、时钟跳变、结束原因……），同样只属于这一场
        sess = self._session_state = self._new_session_state(my_audit)
        end = {}
        try:
            await self._run_session(url)
            end = sess.get("end") or {}
        except asyncio.CancelledError:
            end = {"reason": getattr(self, "_stop_reason", None) or "cancelled"}
            raise
        except Exception as exc:
            # 打包运行时 stdout 指向 /dev/null，外层 _run_stream 打的堆栈没人看得到；
            # 等它接住异常时，下面的 finally 早把审计关了——所以在这里先写进本场审计
            end = {"reason": "internal_error"}
            self._record_internal_error(my_audit, exc)
            raise
        finally:
            # 无论怎么结束（下播、预算耗尽、解析失败、模型加载失败、被取消），
            # 都要收掉统计循环和审计文件——否则界面上会继续刷新冻结的统计数字
            await self._end_session(my_audit, **end)

    async def _begin_session(self, url):
        from .audit import AuditLog
        from .glossary import (fingerprint, misplaced_entries, profile_options,
                               profile_path, set_active)
        from .provenance import app_version, file_hash, streamer_of

        # 词表每场重新读：模板生成出来是空的，用户按提示填好后点「停止→开始」
        # 必须真的生效，否则头号卖点就是 100% 静默漏报
        self.detector = load_detector(getattr(self.args, "banned_terms", None))
        self.detector.reset_state()
        # 词表按主播加载：全局表 + profiles/<主播>.txt（后者优先）。商品知识
        # 是逐主播的——Bella 的品名进了全局表，Elisa 的直播里就会凭空冒出
        # 别家商品。set_active 让 DeepL 的原生术语表也拿到同一份合并结果。
        streamer = streamer_of(url)
        self.glossary = load_glossary(getattr(self.args, "glossary", None),
                                      streamer=streamer)
        set_active(self.glossary)
        # 记进「最近直播间」。放在这里而不是解析成功之后：用户「加入过」这个
        # 房间就该出现在列表里，哪怕这次没拿到流地址——下次点一下就能再试。
        # 只记有主播名的（直接 .flv 地址没有身份，进列表只是一串地址）。
        if streamer:
            await self._publish_recent_rooms(streamer, url)
        prof = profile_path(streamer)
        misplaced = misplaced_entries(self.glossary.entries)
        misplaced = [(o, v, zh) for o, v, zh in misplaced if o != streamer]
        # 称呼摘除是按主播验证的行为（触发率跨主播差 60 倍），profile 里写了
        # vocative_strip: on 才开。没验证过的新主播默认关——「规则不泛化，
        # 验证规则的流程泛化」，开关本身就是那个流程的产物
        self._vocative_strip = bool(profile_options(streamer).get(
            "vocative_strip", False))
        self._quality.clear()            # 等级按 seq 记，换场后 seq 会重号
        self._strong_inflight.clear()
        # 上一场的字幕不能再重译：seq 换场重号，旧 job 写进新场的审计会对不上号。
        # getattr 兜底：个别测试用 Pipeline.__new__ 造半成品实例
        recent = getattr(self, "_recent", None)
        if recent is not None:
            recent.clear()
        self._strong_missing = False     # 用户可能在两场之间拉好了模型
        self._drop_strong()              # 也可能删掉了：上一场的强模型对象不能接着用
        self.telemetry.reset()          # 统计按场计，不跨房间累计
        login_source = await self._login_source()
        if self.audit is not None:
            self.audit.close()
        # requested 是用户的选择，active 是实际生效的对象——这两列并排记，
        # 是因为 2026-08-26 它们恰恰不一致：requested=deepl 被启动逻辑静默
        # 重置，实际整场跑的是 1.8B，事后只能靠延迟指纹反推。有了这两列，
        # 「以为跑 A 实际跑 B」变成 grep 一行就能发现的事。
        self.audit = AuditLog(room_url=url, extra={
            "app_version": app_version(),
            # 弹幕组件版本：弹幕出问题时第一个要答的问题（2026-09-14 卡在 7.0.0）
            "tiktoklive_version": _tiktoklive_version(),
            "streamer": streamer,
            # requested 由做决策的那一刻记录（main 启动 / UI 点开始），
            # 这里只转抄，不重算——重算读到的 settings 可能已经不是当时那份
            "source_requested": getattr(self.args, "source_requested", "?"),
            "source_active": getattr(self.args, "source", None) or "auto",
            "translator_requested": getattr(self.args, "translator", None) or "auto",
            # 引擎 none 时统一记字符串 "none"，别让下游在 null 和 "none"
            # 两种写法之间做字符串匹配
            "translator_active": (self.translator.name
                                  if self.translator is not None else "none"),
            # 词表归属四件套：加载了谁的 profile、它的指纹、全局表指纹
            # （AuditLog 里的 glossary_hash）、合并后实际生效那份的指纹。
            # 引擎归属吃过「只能靠反推」的亏，词表归属不能再走一遍
            "profile": streamer if prof else None,
            "profile_hash": file_hash(prof) if prof else None,
            "merged_glossary_hash": fingerprint(self.glossary.entries),
            # 这一场解析流地址时先借哪个浏览器的 TikTok 登录（浏览器名，没有是 null）。
            # 关于账号只记这一个名字：不记用户名、不记 cookie 的名字和值
            "login_source": login_source,
            **self._sleep_guard_extra(),
            **self._banned_terms_provenance(),
            **self._evidence_session_extras(),
            # 解析组件版本、上次连上更新服务器的时间、这一场是不是更新后自动接上的
            **self._update_session_extras(),
        })
        # 空闲期（还没开播）攒下的手机同看事件在这里补写：session_start 已经落盘，
        # 顺序保持原样。晚了这一步，「开播前就打开了同看」这件事在证据里会不见
        self._flush_viewer_audit()
        await self._watch_audit(streamer)
        if misplaced:
            owner, variant, zh = misplaced[0]
            text = ("glossary.txt 里有 {} 条「{}」的专属词条（如 {} => {}），"
                    "在其他主播的直播里会凭空冒出别家商品——建议把它们移到 "
                    "profiles/{}.txt").format(len(misplaced), owner, variant,
                                              zh, owner)
            print("[警告] " + text)
            await self.server.broadcast({"type": "notice", "text": text})
            # 界面上给一个「迁移旧词表」入口。能一键迁移的只是整条与旧官方
            # 模板一致的行（migration_plan 的严判据）——可能少于提示条数，
            # 用户改过的条目仍然只提示、不代改。进 config：页面刷新/重连后
            # hello 会带上它，入口不消失
            self.server.config["glossary_migration"] = {"count": len(misplaced)}
            await self.server.broadcast({"type": "glossary_migration",
                                         "stage": "available",
                                         "count": len(misplaced)})
        else:
            self.server.config.pop("glossary_migration", None)
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
        # 弹幕后端抓取：只有 @主播名 / 直播间链接能反查出 unique_id，直接流
        # 地址（.m3u8/.flv 之类）没有主播身份，没法连 TikTok 的评论 WebSocket。
        self._comments_pending = None
        if not getattr(self.args, "comments", True):
            await self._publish_comment_source("unavailable", "已用 --no-comments 关闭")
        elif not streamer:
            await self._publish_comment_source(
                "unavailable", "直接流地址无法获取评论，请用 @主播名 或直播间链接")
        else:
            # getattr 兜底：个别测试用 Pipeline.__new__ 绕过 __init__ 造半成品
            # 实例，没有 comment_source 属性——这条锦上添花的功能缺了就悄悄
            # 跳过，不该拖累那些测试本来要验证的东西
            comment_source = getattr(self, "comment_source", None)
            if comment_source is None:
                pass
            elif self._comments_wait_for_resolve(url):
                # 登录优先（macOS）：弹幕子进程一启动就**匿名**抓同一个直播页（TikTokLive 的
                # fetch_room_id_from_html）。它要是先起，这一场第一个带登录的请求就落在一次匿名
                # 请求之后一两秒——2026-09-17 实测那样 3 次里 3 次拿不到地址。所以等第一次
                # 解析返回再起（_start_pending_comments）；解析失败这一场就此结束，弹幕不起。
                # 弹幕不进报警链路，晚几秒没有代价。记着是哪一场要起的：晚到的旧任务不能替
                # 新的一场起。
                self._comments_pending = (self.audit, streamer)
                await self._publish_comment_source("connecting", "流地址解析完成后连接评论…")
            else:
                comment_source.start(streamer)

    def _comments_wait_for_resolve(self, url):
        """这一场的弹幕要不要等第一次流地址解析返回之后再起：解析会走登录优先那一步
        （resolver.login_first_applies）的时候。用户自带流地址时也等——那个地址拉不动的话
        _resolve_media 会回到自动解析，照样有带登录的请求。其它平台照旧立刻起。"""
        from .resolver import login_first_applies

        try:
            return login_first_applies(url, getattr(self.args, "cookies_browser", "auto"))
        except Exception:
            return False

    async def _start_pending_comments(self, my_audit):
        """第一次解析返回之后起弹幕（见 _begin_session 里的说明）。只认这一场自己挂的那一笔。"""
        pending = getattr(self, "_comments_pending", None)
        if not pending or pending[0] is not my_audit or self.audit is not my_audit:
            return
        self._comments_pending = None
        comment_source = getattr(self, "comment_source", None)
        if comment_source is not None:
            comment_source.start(pending[1])

    def _banned_terms_provenance(self):
        """违禁词表的来源信息，并进 session_start。

        banned_terms.txt 不入库，code_commit 钉不住它；只存指纹又没人留历史版本——
        「那场直播时词表里有没有 X」这种合规复查最常问的问题就答不出来。所以把本场
        实际加载的条目原文整份记下（一百来条，几 KB），不做任何归一化。"""
        from .provenance import file_hash
        info = {"fuzzy_policy_hash": file_hash(FUZZY_POLICY_FILE)}
        detector = getattr(self, "detector", None)
        if detector is None:
            return dict(info, detector_enabled=False)
        decode_error = getattr(detector, "decode_error", None)
        info.update({
            "detector_enabled": bool(detector.enabled),
            "banned_terms_hash": getattr(detector, "source_hash", "?"),
            "banned_terms_count": detector.count,
            "banned_terms": list(getattr(detector, "loaded", None) or []),
            "banned_terms_warnings": [
                {"line": w.get("line"), "entry": w.get("entry"), "reason": w.get("reason")}
                for w in getattr(detector, "load_warnings", None) or []],
            "banned_terms_decode_error": (
                {"error": decode_error,
                 "skipped_lines": list(getattr(detector, "skipped_lines", None) or [])}
                if decode_error else None),
        })
        if getattr(detector, "read_error", None):
            info["banned_terms_read_error"] = detector.read_error
        return info

    async def _provision_then_check(self):
        await self.ensure_local_translator()
        await self.run_selfcheck()

    async def provision_comments(self):
        """启动时在后台备弹幕组件：缺了就装、过旧就升（updater.ensure_tiktoklive）。
        版本变了就重跑自检：启动自检和这里是并行的，「观众弹幕」那一行多半是按
        旧版本查的，不刷新就会一直标黄。"""
        updater = getattr(self, "updater", None)
        if updater is None:
            return False
        before = _tiktoklive_version()
        ok = await updater.ensure_tiktoklive()
        if _tiktoklive_version() != before:
            pending = getattr(self, "_selfcheck_task", None)
            if pending is not None and pending is not asyncio.current_task() \
                    and not pending.done():
                try:
                    await pending
                except Exception:
                    pass
            await self.run_selfcheck()
        return ok

    OLLAMA_HEAL_COOLDOWN_SEC = 60.0

    async def _heal_local_engine(self):
        """本地引擎在用、Ollama 却不通：后台把它拉起来，不阻塞调用方。

        开播那一刻和每次翻译失败都会来问一次。实录：Ollama 没跑时一场直播
        每句翻译 0.8 毫秒失败，界面只在第四条之后才提示「翻译服务可能连不上」，
        程序自己明明能启动它。节流一分钟一次；备模型任务在跑就不重复起。"""
        from . import localmodel

        if getattr(self.args, "translator", "auto") not in self.LOCAL_ENGINES:
            return
        if getattr(self.translator, "name", None) not in ("hymt2", "hymt2-7b", "gemma"):
            return
        now = time.monotonic()
        if now - getattr(self, "_heal_at", -1e9) < self.OLLAMA_HEAL_COOLDOWN_SEC:
            return
        task = getattr(self, "_provision_task", None)
        if task is not None and not task.done():
            return
        self._heal_at = now
        if await localmodel.is_running():
            return
        # is_installed 可能跑 Spotlight 查询（最坏 8 秒）：直播中在事件循环上跑，
        # 音频读取和报警广播会一起停住
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, localmodel.is_installed):
            return
        await self.server.broadcast({
            "type": "notice", "text": "翻译引擎用的 Ollama 没在运行，正在自动启动…"})
        self._provision_task = asyncio.ensure_future(self._provision_then_check())

    # 需要 Ollama 的引擎 → 它要的模型、以及「本机有没有这个模型」的探测
    LOCAL_ENGINES = ("auto", "hymt2", "hymt2-7b", "gemma")

    async def ensure_local_translator(self):
        """开工前把本地翻译准备好，让用户不必为此开终端。

        没装 Ollama 的机器会退回 Google 免费接口——按 IP 限流，长时间监听经常
        整段翻译失败。以前的提示是「自己去装 Ollama，再敲一行 ollama pull」，
        对不会用终端的人等于永远用不上本地翻译。

        Ollama 装了没启动就帮他启动；启动了但没有模型就用 HTTP 接口拉下来
        （3GB 的 Whisper 模型我们本来就自动下，这个 1.1GB 是同一件事）。
        压根没装的只能引导——那一步需要管理员权限，代劳不了。

        用户显式选了 hymt2 / hymt2-7b / gemma 时**同样**要做这些：以前这里
        「用户指定了引擎就不自作主张」直接返回，于是选了本地 Hy-MT2 的机器
        Ollama 永远不会被启动，自检一直红着「Ollama 没在运行」、每句翻译
        0.8 毫秒失败——用户明明就是要这个引擎，把它跑起来才是不自作主张。

        **直播中不下载。** 下载和拉流抢同一条网络（7B 有 4.6 GB），而会话日志里以前
        不留下载的痕迹，事后一段音频中断对不上号。直播中发现缺模型只记下来、说一句，
        停止后由 _end_session 再来一遍；已经在跑的下载（比如启动时起的那个）不去动它。
        """
        from . import localmodel
        from . import translator as T

        engine = getattr(self.args, "translator", "auto")
        if engine not in self.LOCAL_ENGINES:
            return                      # deepl/google/claude/openai/none：不碰 Ollama
        loop = asyncio.get_running_loop()
        started = False
        if await localmodel.is_running():
            pass
        # is_installed 可能跑 Spotlight 查询（最坏 8 秒）：放线程池
        elif await loop.run_in_executor(None, localmodel.is_installed):
            print("[信息] Ollama 已安装但没在运行，正在启动…")
            if not await localmodel.start():
                print("[警告] Ollama 没能启动，本地翻译暂不可用")
                return
            started = True
        else:
            return                      # 没装：交给自检那一行去引导

        # 探测是同步 urllib，放线程池（直播中也可能走到这里）
        if engine == "auto":
            active = getattr(self.translator, "name", None)
            if active in ("hymt2", "gemma"):
                # 正在用的那个本地模型本身得在。以前只要本机还有随便哪个本地模型就算
                # 「有」：1.8B 被 ollama rm 掉而 7B 还在时，既不重新下载也不重建引擎，
                # 每句 404 到本场结束
                has = self._local_wanted(active)[1]
            else:
                def has():
                    # 和 _local_wanted 同一个判据：按生成时真正用的名字精确比
                    return T._ollama_has_model(*(T.local_engine_model(e)
                                                 for e in ("hymt2", "hymt2-7b", "gemma")))
            need = None if await loop.run_in_executor(None, has) \
                else T.local_engine_model("hymt2")
        else:
            model, has = self._local_wanted(engine)
            need = None if await loop.run_in_executor(None, has) else model

        pulled = False
        if need is not None:
            if need in self._pulls_running():
                # 已经有一个下载在跑（比如启动时起的那个）：不再起第二个，也不推迟它；
                # 只在本场日志里留个记号，事后能把这段时间的网络占用对上号
                self._audit_pull("in_progress", need)
            elif self._stream_active() or self._update_in_progress():
                await self._defer_pull(need)
            else:
                pulled = await self._pull_model(need)

        if engine == "auto" and (started or pulled):
            # 启动时 Ollama 还没起来，auto 已经落到了 Google；现在本地模型能用了
            self.translator = await loop.run_in_executor(
                None, create_translator, "auto")
            await self._publish_engine()
        elif pulled and getattr(self, "_engine_pending", None) == engine:
            await self._apply_pending_engine(engine)
        if pulled:
            await self._provision_note("本地翻译已就绪，可以开始了。")
        if started or need is not None:
            # 启动时的自检和这里是并行跑的，那一行多半是在 Ollama 起来之前
            # 查的：等它跑完再查一遍，把红条刷掉
            pending = getattr(self, "_selfcheck_task", None)
            if pending is not None and pending is not asyncio.current_task() \
                    and not pending.done():
                try:
                    await pending
                except Exception:
                    pass
            await self.run_selfcheck()

    _NOT_NOTED = object()

    @staticmethod
    def _local_wanted(engine):
        """本地引擎 → (它生成时真正调用的模型, 本机有没有**这一个**模型的同步探测)。

        和自检（selfcheck.check_translator）同一个判据：按名字精确比（model_listed），
        环境变量改过的模型名也照认。以前这里按子串认——Ollama 里只有 translategemma:12b、
        或 1.8B 只有别的量化档时，自检红着说「会自动下载」，这里却当作有、一个字节都不下，
        set_engine 也照换不误，每句 404。探测在调用时才去 translator 模块里取，测试替换
        得到。不是本地引擎时模型是 None。"""
        from . import translator as T

        model = T.local_engine_model(engine)
        if model is None:
            return None, lambda: True
        return model, lambda: T._ollama_has_model(model)

    def _pulls_running(self):
        """正在下载的模型名（进程级集合：一次下载可能跨场次）。"""
        running = getattr(self, "_pulling", None)
        if running is None:
            running = self._pulling = set()
        return running

    def _audit_pull(self, state, model, error=None):
        """下载本身跨场次，记进事件发生这一刻正开着的那场审计；没有开着的就不记。"""
        audit = getattr(self, "audit", None)
        if audit is not None:
            audit.model_pull(state, model, error)

    async def _defer_pull(self, need):
        """直播中（或一键更新进行中）缺模型：不下载，记下来之后再下；同一场只说一次。"""
        from .translator import model_label

        audit = getattr(self, "audit", None)
        if getattr(self, "_pull_deferred", None) == need \
                and getattr(self, "_pull_deferred_session", self._NOT_NOTED) is audit:
            return
        self._pull_deferred = need
        self._pull_deferred_session = audit
        self._audit_pull("deferred", need)
        when = "更新结束后" if self._update_in_progress() else "停止后"
        text = "本地翻译模型 {} 还没下载，{}自动下载".format(model_label(need), when)
        print("[信息] " + text)
        await self._provision_note(text)

    async def _pull_model(self, need):
        """真的去下载（调用方已确认不在直播）。进度和结果都告诉界面；失败时带上
        Ollama 的原话，替换掉停在半截的百分比。返回是否成功。"""
        from . import localmodel
        from .translator import HYMT2_SMALL, engine_label

        size = "约 1.1 GB，" if need == HYMT2_SMALL else "首次需要下载，"
        await self._provision_note(
            "正在准备本地翻译模型（{}只需这一次）…".format(size))
        last = [-10.0]
        finished = [False]

        async def note(text):
            if not finished[0]:         # 下载结束后才轮到的进度，别盖掉结果
                await self._provision_note(text)

        def progress(pct, done_mb, total_mb):
            if pct - last[0] < 5:       # 别把界面刷爆
                return
            last[0] = pct
            self._spawn(note(
                "正在下载本地翻译模型：{:.0f}%（{:.0f} / {:.0f} MB，"
                "只需这一次）…".format(pct, done_mb, total_mb)))

        running = self._pulls_running()
        running.add(need)
        self._pull_deferred = None      # 推迟的那一次就是这一次
        self._audit_pull("start", need)
        try:
            result = await localmodel.pull(need, on_progress=progress)
        finally:
            finished[0] = True
            running.discard(need)
        ok, error = result if isinstance(result, tuple) else (bool(result), None)
        if ok:
            print("[信息] 本地翻译模型已就绪")
            self._audit_pull("done", need)
            return True
        error = error or "Ollama 没有给出说明"
        print("[警告] 本地翻译模型下载失败：{}".format(error))
        self._audit_pull("failed", need, error)
        current = getattr(self.translator, "name", None)
        keep = "继续用" + engine_label(current) if current else "继续不翻译"
        await self._provision_note(
            "本地翻译模型下载失败：{}；{}，下一场停止后会自动再试".format(error[:120], keep))
        return False

    async def _apply_pending_engine(self, engine):
        """界面上选了一个当时本机还没有模型的本地引擎：模型下好之后才换上它。"""
        loop = asyncio.get_running_loop()
        try:
            new = await loop.run_in_executor(None, create_translator, engine)
        except Exception as exc:
            print("[警告] 换用 {} 失败: {}".format(engine, exc))
            return
        if getattr(self.args, "translator", None) != engine \
                or getattr(self, "_engine_pending", None) != engine:
            try:                        # 下载期间用户又换了别的引擎：以后来的选择为准
                await new.close()
            except Exception:
                pass
            return
        old, self.translator = self.translator, new
        self._engine_pending = None
        self.args.translator_note = None
        if old is not None and old is not new:
            try:
                await old.close()
            except Exception:
                pass
        await self._publish_engine()

    async def _pull_after_session(self, session_task):
        """补上直播中推迟的模型下载。_end_session 跑在直播任务自己里面，那一刻
        _stream_active() 还是真：等这个任务真正结束再动手。这期间又开了新的一场，
        ensure_local_translator 会自己再推迟一次。"""
        try:
            if session_task is not None and session_task is not asyncio.current_task():
                await asyncio.wait({session_task})
            if self._update_in_progress():
                return          # 一键更新停掉的这一场：更新收尾时再补（见 _apply_update）
            if getattr(self, "_pull_deferred", None) and not self._stream_active():
                await self.ensure_local_translator()
        except Exception as exc:
            print("[警告] 停止后补下载本地翻译模型失败: {}".format(exc))

    def _stream_active(self):
        task = getattr(self, "_stream_task", None)
        return task is not None and not task.done()

    def _update_in_progress(self):
        """一键更新正在进行：取新版本、装依赖、合并，或为此暂停了监听。"""
        updater = getattr(self, "updater", None)
        return bool(getattr(updater, "_applying", False)) \
            or getattr(self, "_update_pause", None) is not None

    async def _provision_note(self, text):
        """后台备模型的进度：待机时走 status（首页大字），直播中只发 notice。
        这个下载要几分钟，而用户完全可以在它跑着的时候点开始——那时再广播
        status=idle 会把界面从「直播中」拽回待机、停止按钮消失，每 5% 刷一次。"""
        if self._stream_active() or self._update_in_progress():
            # 更新期间状态行是「正在更新，监听已暂停」那句，也不能被下载进度盖掉
            await self.server.broadcast({"type": "notice", "text": text})
        else:
            await self.server.status("idle", text)

    async def run_selfcheck(self):
        """启动自检：确认每项能力真的在工作，结果推到界面上。

        这个方法存在的原因是降噪那次事故——功能静默降级成关闭，只在一行
        没人看的日志里说了一句。自检把这类问题变成界面上的红条。"""
        from .selfcheck import run_all
        # 记下这次自检看到的弹幕组件版本：之后组件被自动升级（被拒时找到了补丁），
        # 评论流重新连上时对得上号就知道要不要重查，免得「观众弹幕」一行停在旧版本
        self._selfcheck_tiktoklive = _tiktoklive_version()
        asr_state = self._asr_check_state()
        try:
            checks = await run_all(self.args, self.detector, self.glossary,
                                   self.translator, asr_state=asr_state)
        except Exception as exc:
            print("[警告] 自检执行失败: {}".format(exc))
            return
        # 整轮自检要等 Ollama、解析器、磁盘这些慢探测，开播时和模型加载并行跑：这期间加载
        # 失败了（_refresh_asr_check 已经把那一行刷红），不能再用开跑时的旧状态盖回绿色
        checks = await self._with_live_asr_row(checks, asr_state)
        for c in checks:
            icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}[c["level"]]
            print("[自检] {} {}：{}".format(icon, c["name"], c["detail"]))
        await self._publish_selfcheck(checks)

    def _asr_check_state(self):
        """交给自检「语音识别」那一行的实际加载结果（加载失败、出错后改用了 CPU）。"""
        return {"load_error": getattr(self, "_asr_load_error", None),
                "fallback": getattr(self, "_asr_fallback", None)}

    async def _publish_selfcheck(self, checks):
        from .selfcheck import summarize
        summary = summarize(checks)
        self.server.config["selfcheck"] = {"checks": checks, "summary": summary}
        await self.server.broadcast({"type": "selfcheck", "checks": checks,
                                     "summary": summary})
        self._audit_selfcheck(checks, summary)

    def _audit_selfcheck(self, checks, summary):
        """自检结论写进本场审计：第一次整份写，之后只在某一行等级变了时写变了的那几行。
        打包运行时 stdout 指向 /dev/null，以前复盘「这场为什么漏报」时说不出当时哪项能力
        是坏的。键里带 audit 对象，换场后第一次照样整份写（与 _publish_comment_source 同法）。"""
        audit = getattr(self, "audit", None)
        if audit is None:
            return
        levels = {c.get("name"): c.get("level") for c in checks}
        last = getattr(self, "_selfcheck_audited", None)
        if last is None or last[0] is not audit:
            audit.selfcheck(checks, summary, full=True)
        else:
            changed = [c for c in checks if last[1].get(c.get("name")) != c.get("level")]
            if not changed:
                return
            audit.selfcheck(changed, summary, full=False)
        self._selfcheck_audited = (audit, levels)

    async def _refresh_asr_check(self):
        """只重查「语音识别」一行。加载失败、改用 CPU、恢复都发生在开播时那轮自检之后，
        不刷新的话那一行会一直停在旧颜色；整轮自检会去 ping Ollama、建 DeepL 术语表，
        不为这一行重跑。还没跑过自检就不管——之后那一轮会带上最新状态。"""
        if not self._published_checks():
            return
        row = await self._live_asr_row()
        # check_asr 会进线程：这期间整轮自检可能刚发布过，按发布那一刻的结果替换这一行
        current = self._published_checks()
        if row is None or not current:
            return
        await self._publish_selfcheck(
            [row if c.get("name") == row["name"] else c for c in current])

    def _published_checks(self):
        return ((getattr(self.server, "config", None) or {}).get("selfcheck") or {}).get("checks")

    async def _live_asr_row(self, state=None, row=None):
        """按管线此刻的识别加载状态算「语音识别」一行（row 是按 state 算好的现成结果）。
        check_asr 会进线程探测 import，算的这一会儿加载结果又变了就重算，最多三次。"""
        from .selfcheck import check_asr
        for _ in range(3):
            live = self._asr_check_state()
            if row is not None and live == state:
                return row
            state = live
            try:
                row = await check_asr(self.args, state)
            except Exception as exc:
                print("[警告] 语音识别自检刷新失败：{}".format(exc))
                return None
        return row

    async def _with_live_asr_row(self, checks, state):
        """整轮自检的结果里，「语音识别」一行换成按此刻加载状态算的（没变就原样返回）。"""
        current = next((c for c in checks if c.get("name") == "语音识别"), None)
        if current is None:
            return checks
        row = await self._live_asr_row(state, current)
        if row is None or row is current:
            return checks
        return [row if c.get("name") == row["name"] else c for c in checks]

    async def _clear_recent_rooms(self):
        """清空「最近直播间」——中控点了首页那个「清空」。"""
        save_setting("recent_rooms", [])
        self.server.config["recent_rooms"] = []
        await self.server.broadcast({"type": "recent_rooms", "entries": []})

    async def _publish_recent_rooms(self, streamer, url):
        """把刚打开的房间推进「最近直播间」并广播给所有页面。

        存进 server.config，晚打开或刷新的页面从 hello 里就能拿到——
        localStorage 按端口隔离，端口一漂移就丢，所以持久化走服务端。"""
        entries = push_recent_room(streamer, url)
        self.server.config["recent_rooms"] = entries
        await self.server.broadcast({"type": "recent_rooms", "entries": entries})

    # ---- 手机同看（见 app/viewer.py）----
    # 这一整块的三条不变式：
    #   1. 开/关/换链接全部在专用锁内串行，任何 await 之后都重新核对想要的终态
    #      ——并发的 on/off/on 不能留下一个谁也关不掉的监听端口；
    #   2. 文案只写观察到的事实和能做的事（CLAUDE.md 八，按 A8 修订）；
    #   3. 这里的任何失败都不许外抛到启动或退出路径上。
    def _viewer_lock(self):
        """专用锁，惰性建（3.9 的 asyncio.Lock 构造时就要绑事件循环）。
        刻意不复用 _stream_lock：开同看和开播互相等没有任何好处。"""
        if getattr(self, "_viewer_lock_obj", None) is None:
            self._viewer_lock_obj = asyncio.Lock()
        return self._viewer_lock_obj

    def _viewer_audit(self, event, **fields):
        """一条同看审计事件。没有 audit（还没开播）就先攒着，开播后由
        _flush_viewer_audit 按原顺序补写。无论有没有 audit 都打一行——
        沿用 on_ui_client_dropped 的做法，终端里也要看得见。"""
        shown = " ".join("{}={}".format(k, fields[k]) for k in sorted(fields)
                         if fields[k] is not None)
        print("[同看] {} {}".format(event, shown).rstrip())
        audit = getattr(self, "audit", None)
        if audit is None:
            pending = getattr(self, "_viewer_audit_pending", None)
            if pending is None:
                pending = deque(maxlen=VIEWER_AUDIT_PENDING_MAX)
                self._viewer_audit_pending = pending
            pending.append((event, dict(fields)))
            return
        self._write_viewer_audit(audit, event, fields)

    @staticmethod
    def _write_viewer_audit(audit, event, fields):
        method = getattr(audit, event, None)
        if method is None:
            return
        try:
            method(**fields)
        except Exception as exc:          # 审计写不进去不能影响同看本身
            print("[警告] 记录手机同看事件失败: {}".format(exc))

    def _flush_viewer_audit(self):
        """_begin_session 建好 self.audit 之后调一次：把空闲期攒下的事件按原顺序补写。"""
        audit = getattr(self, "audit", None)
        pending = getattr(self, "_viewer_audit_pending", None)
        if audit is None or not pending:
            return
        while pending:
            event, fields = pending.popleft()
            self._write_viewer_audit(audit, event, fields)

    def _make_viewer_hub(self, ports, token):
        """建一个观众面。单独一个方法是为了测试能注入替身。"""
        from .viewer import ViewerHub

        return ViewerHub(self.server, ports=ports, token=token,
                         audit_hook=self._viewer_audit)

    async def _viewer_ip_info(self):
        """取本机局域网地址。getaddrinfo 会阻塞（DNS 配错时能卡上几秒），放执行器里
        跑并且限时——界面卡住比没出二维码严重得多。取不到就按「没读到地址」处理。"""
        from .viewer import lan_ipv4

        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(loop.run_in_executor(None, lan_ipv4),
                                          VIEWER_IP_TIMEOUT_SEC)
        except Exception:
            return {"ip": None, "all": [], "source": None, "ambiguous": False}

    async def _publish_viewer(self, error=None, ports=(), ip_changed=None, ip_info=None):
        """把同看状态推给控制面并存进 config（刷新/晚开的页面从 hello 里取）。

        这条消息带着含 token 的 URL，所以 filter_payload 把 type="viewer" 显式拒了
        ——它只走 127.0.0.1 的控制面，永远到不了手机端。
        """
        from . import viewer as viewer_mod

        hub = getattr(self.server, "viewer", None)
        if hub is None or not hub.running:
            candidates = list(ports)
            if not candidates:
                candidates = viewer_mod.pick_ports(
                    getattr(self.server, "port", 8765),
                    load_settings().get("viewer_port"))
            state = viewer_mod.off_state(port=candidates[0] if candidates else None,
                                         error=error, ports=candidates)
        else:
            info = await self._viewer_ip_info() if ip_info is None else ip_info
            pinned = getattr(self, "_viewer_pinned_ip", None)
            if pinned and pinned in (info.get("all") or []):
                info = dict(info, ip=pinned)
            qr_rows = None
            url = hub.url(info.get("ip")) if info.get("ip") else None
            if url:
                # 整段包住：二维码坏掉绝不影响同看本身，只是要手工输入地址
                try:
                    from . import qr

                    qr_rows = qr.rows(qr.encode(url.encode("utf-8")))
                except Exception as exc:
                    print("[警告] 二维码没能生成（可以手工输入地址）: {}".format(exc))
                    qr_rows = None
            state = hub.state(ip_info=info, qr_rows=qr_rows, ip_changed=ip_changed)
            self._viewer_ip = state.get("ip")
        self.server.config["viewer"] = state
        await self.server.broadcast(dict(state, type="viewer"))
        return state

    async def _set_viewer_share(self, on):
        """中控点了「打开」/「关闭」。锁外先记下想要的终态，锁内复核——
        连着点三下 on/off/on 时，只有最后一条说的算，中间那几条不许留下监听端口。"""
        on = bool(on)
        self._viewer_want = on
        async with self._viewer_lock():
            if self._viewer_want != on:
                return          # 期间又来了新指令：由它落地，别来回开关端口
            if on:
                await self._start_viewer_locked("operator")
                if not self._viewer_want:
                    # start 里有 await，期间中控又点了关闭：别留下一个在监听的端口
                    await self._stop_viewer_locked("operator")
            else:
                await self._stop_viewer_locked("operator")

    async def _start_viewer_locked(self, reason):
        from . import viewer as viewer_mod

        hub = getattr(self.server, "viewer", None)
        if hub is not None and hub.running:
            await self._publish_viewer()
            return
        saved = load_settings()
        token = viewer_mod.sanitize_token(saved.get("viewer_token"))
        if token is None:
            # 手改坏了或首次打开：重新生成并存回。关闭同看**不**清 token——
            # 贴在墙上的二维码还要能用，只有「换一个链接」才换
            token = viewer_mod.new_token()
            self._save_setting("viewer_token", token)
        ports = viewer_mod.pick_ports(getattr(self.server, "port", 8765),
                                      saved.get("viewer_port"))
        hub = self._make_viewer_hub(ports=ports, token=token)
        try:
            await hub.start()
        except OSError as exc:
            # 观众端口刻意不漂到别的端口：已经发出去、贴在墙上的二维码会静默失效。
            # 只报观察（系统真的返回了什么）+ 能做的事，保持关闭
            self.server.viewer = None
            self._save_setting("viewer_share_enabled", False)
            self._viewer_audit("viewer_share", state="off",
                               port=ports[0] if ports else None, viewers=0,
                               reason="port_busy")
            await self._publish_viewer(error=strip_query(str(exc), 200), ports=ports)
            return
        self.server.viewer = hub
        self._viewer_pinned_ip = None
        self._save_setting("viewer_share_enabled", True)
        self._save_setting("viewer_port", hub.port)
        self._viewer_audit("viewer_share", state="on", port=hub.port, viewers=0,
                           reason=reason)
        await self._publish_viewer()
        self._start_viewer_ip_watch()

    async def _stop_viewer_locked(self, reason):
        hub = getattr(self.server, "viewer", None)
        self.server.viewer = None
        self._cancel_viewer_ip_watch()
        if reason != "shutdown":
            # 退出时不动这个开关：下次启动还要照中控最后的意思恢复
            self._save_setting("viewer_share_enabled", False)
        if hub is not None:
            port, viewers = hub.port, hub.count
            try:
                await hub.stop(reason)
            except Exception as exc:
                print("[警告] 关闭手机同看出错: {}".format(exc))
            self._viewer_audit("viewer_share", state="off", port=port,
                               viewers=viewers, reason=reason)
        if reason != "shutdown":
            await self._publish_viewer()

    async def _rotate_viewer_token(self):
        """「换一个链接」：换 token，在看的手机全部断开（要重新扫码）。"""
        from . import viewer as viewer_mod

        async with self._viewer_lock():
            token = viewer_mod.new_token()
            self._save_setting("viewer_token", token)
            hub = getattr(self.server, "viewer", None)
            viewers = hub.count if hub is not None else 0
            port = hub.port if hub is not None else None
            if hub is not None:
                hub.rotate(token)
            self._viewer_audit("viewer_share", state="rotate", port=port,
                               viewers=viewers, reason="operator")
            await self._publish_viewer()

    async def _pick_viewer_ip(self, ip):
        """中控在候选地址里点了一个。只接受确实在候选里的地址——界面传什么都不能
        直接拼进链接里。认不出就清掉，回到自动选的那个。"""
        async with self._viewer_lock():
            info = await self._viewer_ip_info()
            candidates = info.get("all") or []
            self._viewer_pinned_ip = ip if (
                isinstance(ip, str) and ip in candidates) else None
            await self._publish_viewer(ip_info=info)

    # 地址会变（换 Wi-Fi、插网线、VPN 上下线）。开着同看就每 60 秒重取一次：
    # 地址变了而二维码没换，中控只会看到手机「打不开」，而程序这边什么都不知道
    def _start_viewer_ip_watch(self):
        if getattr(self, "_viewer_ip_task", None) is not None:
            return
        self._viewer_ip_task = self._spawn(self._viewer_ip_watch())

    def _cancel_viewer_ip_watch(self):
        task = getattr(self, "_viewer_ip_task", None)
        self._viewer_ip_task = None
        if task is not None:
            task.cancel()

    async def _viewer_ip_watch(self):
        from . import viewer as viewer_mod

        try:
            while True:
                await asyncio.sleep(viewer_mod.IP_REFRESH_SEC)
                hub = getattr(self.server, "viewer", None)
                if hub is None or not hub.running:
                    return
                info = await self._viewer_ip_info()
                old, new = getattr(self, "_viewer_ip", None), info.get("ip")
                if new == old:
                    continue
                await self._publish_viewer(
                    ip_info=info, ip_changed=(old, new) if (old and new) else None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[警告] 重取本机地址出错: {}".format(exc))

    async def restore_viewer_share(self):
        """启动时恢复上次的开关（main.py 调用）。缺这一项就是关闭——要求 1。
        任何异常都不外抛：启动不能被同看带崩。"""
        try:
            if not bool(load_settings().get("viewer_share_enabled")):
                await self._publish_viewer()     # 卡片要显示「关闭」这个状态
                return
            self._viewer_want = True
            async with self._viewer_lock():
                if not self._viewer_want:
                    return
                await self._start_viewer_locked("boot_restore")
        except Exception as exc:
            print("[警告] 恢复手机同看失败: {}".format(exc))

    async def stop_viewer_share(self, reason="shutdown"):
        """退出收尾：尽力关掉观众监听面。带自己的预算，任何失败都不许拖住退出。"""
        self._viewer_want = False
        try:
            await asyncio.wait_for(self._stop_viewer_share_inner(reason),
                                   VIEWER_STOP_TIMEOUT_SEC)
        except Exception as exc:
            print("[警告] 收尾时关闭手机同看出错: {}".format(exc))

    async def _stop_viewer_share_inner(self, reason):
        async with self._viewer_lock():
            await self._stop_viewer_locked(reason)

    # ---- 磁盘空间：盘点与可选删除（见 app/diskspace.py）----
    def _disk_active(self):
        """正在用的语音模型 (backend, model) 与翻译模型名——盘点时标成「正在用」。"""
        key = self._transcriber_key or self._loading_key
        asr = (key[0], key[1]) if key else None
        engine = getattr(self.translator, "name", None) or ""
        from .translator import HYMT2_LARGE, HYMT2_SMALL
        ollama = {"hymt2": HYMT2_SMALL, "hymt2-7b": HYMT2_LARGE,
                  "gemma": "translategemma"}.get(engine)
        return asr, ollama

    async def _ollama_models(self):
        """/api/tags 的模型列表；Ollama 没在跑就是空列表。"""
        import aiohttp

        from .localmodel import base_url
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
                async with s.get(base_url() + "/api/tags") as r:
                    data = await r.json()
            return [m for m in (data.get("models") or []) if isinstance(m, dict)]
        except Exception:
            return []

    async def _ollama_delete(self, name):
        import aiohttp

        from .localmodel import base_url
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.delete(base_url() + "/api/delete", json={"name": name}) as r:
                    return r.status == 200
        except Exception:
            return False

    async def _publish_disk(self):
        """盘点结果推给页面并存进 config。目录大小要 walk 几十 GB 的缓存，
        放线程池里算，不卡事件循环。"""
        import shutil

        from . import diskspace
        asr, ollama = self._disk_active()
        models = await self._ollama_models()
        current_log = getattr(self.audit, "path", None) if self.audit else None
        loop = asyncio.get_running_loop()
        items = await loop.run_in_executor(
            None, lambda: diskspace.inventory(
                ollama_models=models, active_asr=asr, active_ollama=ollama,
                current_log=current_log))
        try:
            free = shutil.disk_usage(str(TERMS_FILE.parent)).free
        except OSError:
            free = None
        info = {"items": items, "free": free}
        self.server.config["disk"] = info
        await self.server.broadcast(dict(info, type="disk"))

    async def _disk_delete(self, ids):
        """删所选。直播进行中一律拒绝——模型可能正在用，日志正在写。"""
        from . import diskspace
        state = (self.server.config.get("status") or {}).get("state")
        if state in ("connecting", "live") or (
                self._stream_task is not None and not self._stream_task.done()):
            await self.server.broadcast({"type": "notice",
                                         "text": "直播进行中不删除文件，先点「停止」"})
            return
        asr, ollama = self._disk_active()
        models = await self._ollama_models()
        current_log = getattr(self.audit, "path", None) if self.audit else None
        freed, done, failed = await diskspace.delete(
            ids, ollama_models=models, ollama_delete=self._ollama_delete,
            current_log=current_log, active_asr=asr, active_ollama=ollama)
        text = "已删除 {} 项，释放 {}".format(len(done), diskspace.human(freed))
        if failed:
            text += "；未删除：" + "；".join(failed)[:300]
        print("[磁盘] " + text)
        await self.server.broadcast({"type": "notice", "text": text})
        await self._publish_disk()

    async def _publish_watchlist(self):
        """把违禁词表状态推给界面并存进 config——首页那张卡片要靠它显示
        「已启用 N 条」还是「未配置」。词表默认为空，用户看不到这个提示
        就根本不知道有这个功能，也就不会去配。"""
        info = {"count": self.detector.count if self.detector else 0,
                "glossary": len(self.glossary.entries) if self.glossary else 0}
        self.server.config["watchlist"] = info
        await self.server.broadcast(dict(info, type="watchlist"))

    async def _publish_comment_source(self, state, detail="", raw=""):
        """广播弹幕抓取（TikTokLive）的状态：connecting/connected/
        disconnected/error/unavailable/idle 之一。

        每次变化也写进本场审计。2026-09-14 弹幕连接每次被拒时，日志里一条弹幕状态
        都没有，查不出是哪一刻坏的、之前几场好不好。连续相同的状态只记一条；
        键里带上 audit 对象，换场后第一条状态照记。"""
        if hasattr(self, "_selfcheck_tiktoklive") and hasattr(self, "_bg_tasks"):
            current = _tiktoklive_version()
            if current != self._selfcheck_tiktoklive:
                # 组件版本在上次自检之后变了（开播时刚装上、被拒后升级了……）：刷新那一行。
                # 不只看「已连接」：装上了但主播没开播、升级后仍被拒，那一行也不能停在旧状态。
                # 先记下新版本，同一轮里连着几次状态变化不会重复起自检
                self._selfcheck_tiktoklive = current
                self._spawn(self.run_selfcheck())
        audit = getattr(self, "audit", None)
        key = (audit, state, detail, raw)
        if audit is not None and getattr(self, "_last_comment_state", None) != key:
            self._last_comment_state = key
            if raw:       # raw 只进审计，下面的广播里没有它
                audit.comment_source(state, detail, raw)
            else:
                audit.comment_source(state, detail)
        await self.server.broadcast({
            "type": "comment_source", "backend": state, "detail": detail})

    async def _end_session(self, my_audit=None, reason=None, **fields):
        """收尾。my_audit 是调用方会话自己的 audit——凭它判断「我还是不是
        当前会话」：晚到的旧任务只许关自己的 audit，不许碰 stats 循环等
        共享状态（那些已经属于下一场了）。

        reason/fields 写进 session_end（这一场为什么结束）。防睡眠断言只在「我还是
        当前会话、而且断言是我这个任务开的」时释放。"""
        still_current = my_audit is None or self.audit is my_audit
        if still_current:
            self._release_sleep_guard(owner=asyncio.current_task())
            await self._clear_ongoing_incidents()
        if still_current:
            # 弹幕后端抓取是这一场自己起的子进程，晚到的旧任务不该碰
            # 已经属于下一场的连接——只有「我还是当前会话」才停它。
            # getattr 兜底：个别测试用 Pipeline.__new__ 绕过 __init__ 造
            # 半成品实例，没有 comment_source 属性
            self._comments_pending = None
            comment_source = getattr(self, "comment_source", None)
            if comment_source is not None:
                await comment_source.stop()
        if still_current and self._stats_task is not None \
                and not self._stats_task.done():
            try:      # 收尾前推一次终值，别让界面停在半截数据上
                await self.server.broadcast({"type": "stats", **self.telemetry.snapshot()})
            except Exception:
                pass
            self._stats_task.cancel()
            self._stats_task = None
        target = my_audit if my_audit is not None else self.audit
        if target is not None:
            self._close_audit(target, reason, **fields)
            if self.audit is target:
                self.audit = None
            await self._settle_audit_incident(target)
        if still_current:
            await self._settle_engine_incident()
        if getattr(self, "_pull_deferred", None) and hasattr(self, "_bg_tasks"):
            self._spawn(self._pull_after_session(asyncio.current_task()))  # 直播中推迟的模型下载

    # TikTok 不给流地址（接口回 4003110、后面各层也没拿到）时的自动重试。
    # 曾以为是同一 IP 短时间内请求过多被限流——2026-09-05 实测推翻：同一分钟
    # 别的房间正常返回、用户自己的 Chrome 同一 IP 能播，是房间维度的拒绝，
    # 原因 TikTok 不说明。重试仍值得（09-06 一场第三次成功），但不能保证。
    TRANSLATION_DRAIN_SEC = 5.0       # 流结束后最多等在途翻译这么久
    BROWSER_ONLY_RETRIES = 3
    BROWSER_ONLY_RETRY_SEC = 20.0
    # ---- 断流之后：时钟对账、网络探测、等房间恢复在播（见 _run_session 的重连循环）----
    CLOCK_GAP_SEC = 15.0          # 一跳统计比预期晚这么多秒（墙钟），记一条 clock_gap
    OFFLINE_RECHECK_SEC = 30.0    # 时钟跳变之后的「已结束」判定：醒着再等这么久复查一次
    DIRECT_RECHECK_DELAYS = (2.0, 4.0, 8.0, 16.0)   # 时钟跳变之后直连地址拉不到数据：共 30 秒里再试
    STATS_TICK_SEC = 10.0         # _stats_loop 默认每跳间隔；统计循环还没跳过时补对时钟用它
    HOST_WAIT_POLL_SEC = 60.0     # 房间状态既不是在播也不是已结束：隔这么久问一次房间接口
    HOST_WAIT_MAX_SEC = 600.0     # 一次中断里最多这样等这么久
    NETWORK_RETRY_SEC = 20.0      # 本机连不上 TikTok 时隔这么久再探一次
    NETWORK_GIVE_UP_SEC = 1800.0  # 连不上超过这么久才放弃

    async def _resolve_media(self, url, reconnect=None):
        """解析直播流地址。TikTok 明确「不给程序」（kind=browser_only）时不立刻放弃：
        隔 BROWSER_ONLY_RETRY_SEC 秒再试，最多 BROWSER_ONLY_RETRIES 次，界面上说清
        在等什么；其它失败（下播、找不到、网络……）原样抛出。
        reconnect：会话中途第几次重连（首次开播为 None），原样记进审计。
        曾经试过在这一步借用户的 Chrome + 插件拿地址，用户嫌麻烦，撤掉了：
        程序只靠自己，拿不到就明白说「稍后再试」。"""
        from .resolver import (ResolveError, _check_media_url, _media_url_works,
                               resolve_stream_url)

        # 用户自带的音频源优先。TikTok 对某些直播间只把流地址交给真正的浏览器
        # （见 resolver 里 4003110 的说明），程序自己怎么伪装都拿不到；而这种
        # 签名地址实测有效期约两周，用户从浏览器里取一次就够用整场。
        override = getattr(self, "_media_override", None)
        if override:
            t0 = time.monotonic()
            checked = await _check_media_url(override, trusted=True)
            if await _media_url_works(checked):
                print("[信息] 使用用户指定的音频源")
                self._log_resolve(0, True, t0, [{"layer": "用户直连", "outcome": "url"}],
                                  media=checked, reconnect=reconnect)
                return checked
            self._log_resolve(0, False, t0, [{"layer": "用户直连", "outcome": "dead_url"}],
                              kind="dead_override", reconnect=reconnect)
            self._media_override = None      # 失效就别再用，回到正常解析
            await self.server.status(
                "connecting", "你给的流地址拉不动（可能已过期），改用自动解析…")

        last = None
        for attempt in range(1, self.BROWSER_ONLY_RETRIES + 1):
            layers, t0 = [], time.monotonic()
            try:
                media = await resolve_stream_url(
                    url, cookies=self.args.cookies,
                    cookies_browser=getattr(self.args, "cookies_browser", "auto"),
                    trace=layers)
            except ResolveError as exc:
                self._log_resolve(attempt, False, t0, layers, kind=exc.kind,
                                  reconnect=reconnect, message=str(exc),
                                  login=getattr(exc, "login", None))
                await self._sync_login_incident(layers)
                if exc.kind != "browser_only":
                    raise
                last = exc
            else:
                self._log_resolve(attempt, True, t0, layers, media=media,
                                  reconnect=reconnect)
                await self._sync_login_incident(layers)
                return media
            if attempt < self.BROWSER_ONLY_RETRIES:
                await self.server.status(
                    "connecting",
                    "TikTok 暂时没有把这个直播间的流地址给程序，{:.0f} 秒后自动重试"
                    "（第 {}/{} 次）…".format(self.BROWSER_ONLY_RETRY_SEC, attempt + 1,
                                            self.BROWSER_ONLY_RETRIES))
                await asyncio.sleep(self.BROWSER_ONLY_RETRY_SEC)
        raise ResolveError(browser_only_message(self.BROWSER_ONLY_RETRIES,
                                                getattr(last, "login", None)),
                           kind="browser_only",
                           login=getattr(last, "login", None)) from last

    LOGIN_SOURCE_BUDGET_SEC = 2.0     # 只读几个本地文件；到点没返回就记 null，不挡开播
    LOGIN_INCIDENT = "login-unreadable"   # 不带 "session:"：跨场保留，读到登录才撤

    async def _login_source(self):
        """session_start 的 login_source。只看登录 cookie 的名字（resolver.login_source），
        不解密、不弹钥匙串；出任何问题都记 null——这一栏不能挡住开播。"""
        from .resolver import login_source_with_budget

        try:
            return await login_source_with_budget(
                getattr(self.args, "cookies_browser", "auto"), self.LOGIN_SOURCE_BUDGET_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _sync_login_incident(self, layers):
        """按这次解析里「登录直播页」那一层的记录，挂上或撤掉「没有读到 TikTok 登录」的提示。

        读到了可用的登录（那一层记着浏览器名）就撤；一个都没读到就挂，文字写各浏览器的
        观察和能照做的步骤。监听照常开始、照常匿名解析——这条提示不挡任何事。没有那一层
        的记录（其它平台、--cookies-browser none、用户自带流地址）或什么都没去读（测试环境）
        时不动它。提示跨场保留，直到读到登录。"""
        from .browser_login import NOT_READ, no_login_notice
        from .resolver import LOGIN_LAYER

        rec = next((r for r in layers or () if r.get("layer") == LOGIN_LAYER), None)
        if rec is None:
            return
        try:
            incidents = (getattr(self.server, "config", {}) or {}).get("incidents") or {}
            if rec.get("browser"):
                if self.LOGIN_INCIDENT in incidents:
                    await self._incident(self.LOGIN_INCIDENT, "clear")
                return
            observed = {b: c for b, c in (rec.get("login") or {}).items() if c != NOT_READ}
            if not observed:
                return
            text = no_login_notice(observed)
            if (incidents.get(self.LOGIN_INCIDENT) or {}).get("text") != text:
                await self._incident(self.LOGIN_INCIDENT, "warn", text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 这一步跟在解析结果后面：提示推不出去不能把解析的结果（或它的错误）盖掉
            print("[警告] 「没有读到 TikTok 登录」的提示没能更新: {}".format(type(exc).__name__))

    def _log_resolve(self, attempt, ok, t0, layers, kind=None, media=None,
                     reconnect=None, message=None, login=None):
        """一次解析尝试：审计文件里一行（type=resolve）+ 终端一行。

        reconnect 是会话中途第几次重连（首次开播不带这一栏，和重连区分得开）；
        失败时 message 记错误原文前 200 字（URL 去掉 query）；login 是借各浏览器
        TikTok 登录时看到的代码 {浏览器: 代码}（见 app/browser_login.py），没有 cookie 的值。

        attempt=0 表示用户自带的直连地址；1..N 是自动解析的第几次。media 只记
        主机和路径——签名地址的 query 里带 sign/expire，两周内拿着就能拉流，
        不该躺在日志里。audit 为 None（会话还没建起来、或测试里的半成品
        实例）时只打终端。"""
        ms = int(round((time.monotonic() - t0) * 1000))
        rec = {"type": "resolve", "attempt": attempt, "of": self.BROWSER_ONLY_RETRIES,
               "ok": bool(ok), "ms": ms, "layers": list(layers)}
        if kind:
            rec["kind"] = kind
        if media:
            rec["media"] = _media_label(media)
        if reconnect is not None:
            rec["reconnect"] = reconnect
        if message and not ok:
            rec["message"] = strip_query(message, 200)
        if login and not ok:
            rec["login"] = dict(login)
        audit = getattr(self, "audit", None)
        if audit is not None:
            audit.resolve(rec)
        label = "用户直连" if attempt == 0 else "第 {}/{} 次".format(
            attempt, self.BROWSER_ONLY_RETRIES)
        walked = " ".join("{}→{}".format(r.get("layer"), r.get("outcome")) for r in layers)
        print("[解析] {} {} {:.1f}s{} {}".format(
            label, "成功" if ok else "失败", ms / 1000,
            " kind=" + kind if kind else "", walked).rstrip())

    # ---- 一场直播自己的观察状态、防睡眠断言、断流/休眠/断网/等房间恢复 ----

    def _new_session_state(self, audit):
        """一场直播的观察状态。重连循环、拉流 reader 和统计循环共用这一份；晚到的旧任务
        手里拿的是自己那一份，写不进下一场（同 my_audit 的道理）。"""
        return {
            "audit": audit,
            "clock": (time.time(), time.monotonic()),   # _check_clock_gap 上一次的读数
            "gap": None,              # 最近一次时钟跳变；之后收到音频就清掉
            "gap_count": 0,
            "gap_stop": False,        # 这一轮是时钟对账之后主动断开的
            "source": None,           # 当前这一轮的音频源
            "last_frame_wall": None,  # 最近一帧音频到达的墙钟时间
            "deaf_since": None,       # 上一轮收到最后一帧的时间；这一轮收到首帧时清掉
            "reconnect_no": 0,        # 本场第几次重连
            "meter": None,            # AudioFlowMeter，第一轮拉流时建
            "end": None,              # 结束原因 {"reason": ..., 附带观察}，写进 session_end
        }

    def _hold_sleep_guard(self, task):
        """开播时阻止空闲睡眠（见 app/power.py，合盖休眠阻止不了），记下是哪个任务开的。"""
        from . import power

        self._release_sleep_guard()
        try:
            guard = power.hold("直播合规监听中")
        except Exception as exc:
            print("[警告] 没能阻止系统空闲睡眠: {}".format(exc))
            return
        guard.owner = task
        self._sleep_guard = guard
        if guard.held:
            print("[信息] 监听期间阻止空闲睡眠：{}（合盖休眠阻止不了）".format(
                "、".join(guard.held)))

    def _release_sleep_guard(self, owner=None):
        """owner 给定时只释放这个任务开的那一份——晚到的旧任务碰不到新一场的断言。"""
        guard = getattr(self, "_sleep_guard", None)
        if guard is None or (owner is not None and guard.owner is not owner):
            return
        self._sleep_guard = None
        guard.release()

    def _sleep_guard_extra(self):
        """session_start 里记下这一场拿到了哪些防睡眠手段（空列表 = 没拿到或本平台没有）。"""
        guard = getattr(self, "_sleep_guard", None)
        return {"sleep_guard": list(guard.held) if guard is not None else []}

    @staticmethod
    def _record_internal_error(audit, exc):
        """把没预料到的异常写进这一场自己的审计。必须在 except 块里调（要取当前堆栈）。"""
        if audit is None:
            return
        try:
            audit.internal_error(exc, traceback.format_exc())
        except Exception:
            pass

    # 停止或一场结束时，各条 session: 提示怎么收（下一场开始时 _clear_session_incidents 全部撤掉）：
    #   撤掉：描述「正在进行」、监听停了就不成立的——「网络恢复后自动重连」、音频到达率、
    #         「程序继续监听」的安静提示（ONGOING_INCIDENTS）
    #   改写：承诺「会继续重试」的——审计写不进去（_settle_audit_incident）、远程引擎被拒
    #         （_settle_engine_incident）。停了就不再重试，改成已经发生的事和下一步
    #   留着：已经发生的事，或停了也成立的话——时钟跳变、识别连续出错（CPU 模型照样在后台
    #         加载，或请重开程序）、本地引擎连续出错、审计没能创建、审计文件被移走、磁盘快满
    ONGOING_INCIDENTS = ("session:network", "session:audio_rate", "session:quiet_audio")

    async def _clear_ongoing_incidents(self):
        config = getattr(getattr(self, "server", None), "config", None) or {}
        incidents = config.get("incidents") or {}
        for key in self.ONGOING_INCIDENTS:
            if key in incidents:
                try:
                    await self._incident(key, "clear")
                except Exception:
                    pass

    async def _settle_audit_incident(self, audit):
        """审计关掉之后收尾「写不进去」的提示。「程序会继续重试、能写入后补写」到这里就不
        成立了：文件关了，留在内存里等补写的记录跟着丢。改成已经发生的事留给中控看；关的
        那一刻恰好又写进去了，就换成「已恢复」那句。只认本场自己的 audit（规则七）。"""
        watch = getattr(self, "_audit_watch", None) or {}
        if audit is None or watch.get("audit") is not audit:
            return
        unwritten = audit.unwritten() if hasattr(audit, "unwritten") else None
        if not watch.get("failing") and unwritten is None:
            return
        watch["failing"] = False
        pending = watch.get("task")
        if pending is not None and not pending.done():
            try:            # 写失败回调发出的那条还在路上：等它先到，别让它盖掉收尾这句
                await pending
            except Exception:
                pass
        try:
            if unwritten is not None:
                await self._incident("session:audit-write", "error",
                                     self._audit_unwritten_text(unwritten))
            else:
                await self._incident("session:audit-write", "warn",
                                     self._audit_recovered_text(audit))
        except Exception:
            pass

    async def _settle_engine_incident(self):
        """远程引擎被拒那句里的「程序每 N 秒再试一次」只在监听时成立：停了改成已经发生的
        事和下一步。本地引擎连续出错那句本来就只是记录和建议，不动。"""
        info = getattr(self, "_engine_rejection", None)
        if not info or not getattr(self, "_engine_incident_up", False):
            return
        self._engine_rejection = None
        try:
            await self._incident(
                self.ENGINE_INCIDENT, "error",
                "{}，之后的字幕只显示了原文（违禁词报警不受影响）。下一场开始后会再试；"
                "可以在「翻译引擎」里{}".format(
                    info["what"], "换一个引擎" if info["status"] == 404
                    else "重新填写密钥，或换一个引擎"))
        except Exception:
            pass

    @staticmethod
    def _close_audit(audit, reason=None, **fields):
        """关审计，结束原因写进 session_end。没有原因时照老样子 close()。"""
        if reason:
            audit.close(reason=reason, **fields)
        else:
            audit.close()

    @staticmethod
    def _duration_text(sec):
        sec = max(0, int(round(sec)))
        if sec < 90:
            return "{} 秒".format(sec)
        minutes = int(round(sec / 60.0))
        if minutes < 90:
            return "{} 分钟".format(minutes)
        return "{} 小时 {} 分钟".format(minutes // 60, minutes % 60)

    async def _check_clock_gap(self, interval, now=None):
        """_stats_loop 每一跳调一次：对一次墙钟和单调时钟。now=(墙钟, 单调时钟) 只给测试用。

        统计循环每 interval 秒醒一次；墙钟比预期多走了 CLOCK_GAP_SEC 以上，就是这段时间
        程序没有运行。两种看法：
          - 墙钟走了、单调时钟没走（macOS/Linux 的单调时钟在系统休眠时停住）：这是电脑
            休眠或挂起的直接观察；
          - 两个钟一起走多了（Windows 的单调时钟休眠时照走，或者事件循环被卡住）：只说
            程序没有运行。
        记一条 clock_gap，挂一条持续提示（重连的状态文字盖不掉它），并告诉重连循环之后的
        「已结束」判定要复查。时钟被往前拨也会触发，所以记录只叫 clock_gap，文字不说合盖。
        任何异常都不能带崩统计循环。"""
        try:
            sess = getattr(self, "_session_state", None)
            audit = self.audit
            if sess is None or audit is None or sess.get("audit") is not audit:
                return
            wall, mono = now if now is not None else (time.time(), time.monotonic())
            sess["tick_sec"] = float(interval)
            prev_wall, prev_mono = sess["clock"]
            sess["clock"] = (wall, mono)
            wall_sec, mono_sec = wall - prev_wall, mono - prev_mono
            gap = wall_sec - float(interval)
            if gap < self.CLOCK_GAP_SEC:
                return
            diverged = wall_sec - mono_sec >= self.CLOCK_GAP_SEC
            sess["gap"] = {"from": prev_wall, "to": wall, "sec": gap, "diverged": diverged}
            sess["gap_count"] = sess.get("gap_count", 0) + 1
            audit.clock_gap(prev_wall, wall, gap_sec=gap, wall_sec=wall_sec,
                            mono_sec=mono_sec, clocks_diverged=diverged)
            span = "{}–{}".format(time.strftime("%H:%M", time.localtime(prev_wall)),
                                  time.strftime("%H:%M", time.localtime(wall)))
            if diverged:
                text = ("{} 电脑休眠或挂起了约 {}，程序没有运行，这段时间的直播没有监听。"
                        "直播期间请接电源、不要合盖").format(span, self._duration_text(gap))
            else:
                text = ("{} 程序约 {}没有运行，这段时间的直播没有监听。"
                        "直播期间请接电源、不要让电脑休眠").format(span, self._duration_text(gap))
            if sess["gap_count"] > 1:
                text = "本场第 {} 次：{}".format(sess["gap_count"], text)
            print("[时钟] 墙钟走了 {:.0f} 秒、单调时钟走了 {:.0f} 秒".format(wall_sec, mono_sec))
            await self._incident("session:clock_gap", "warn", text)
            # 睡着之前的连接多半已经失效：醒来 5 秒内没有新音频就直接断开这一轮、马上重连，
            # 不再干等 20 秒看门狗。只在两个钟对不上时这么做——事件循环卡住时数据还在管道里
            # 排着，断开反而丢
            source = sess.get("source")
            last = sess.get("last_frame_wall")
            if diverged and source is not None and (last is None or wall - last > 5.0):
                sess["gap_stop"] = True
                self._spawn(source.stop())
        except Exception as exc:
            print("[警告] 时钟对账出错: {}".format(exc))

    async def _recheck_clock(self, sess):
        """重连循环要下「结束」结论之前，补对一次时钟。

        统计循环醒着每 10 秒才对一次。睡着时它和重连退避、解析的计时器一起停住，醒来后谁
        剩的时间少谁先跑：解析先回来「已结束」时，统计循环还没发现刚才睡过，复查就被跳过，
        连 clock_gap 都来不及记（结束时统计循环被取消）。离上一次读数正常不超过一跳，
        补对不会误报；统计循环没在跑时读数是旧的，不对。"""
        stats = getattr(self, "_stats_task", None)
        if stats is None or stats.done() or getattr(self, "_session_state", None) is not sess:
            return
        await self._check_clock_gap(sess.get("tick_sec") or self.STATS_TICK_SEC)

    def _gap_lead(self, gap):
        """时钟跳变之后状态文字的开头。两个钟对不上才说休眠或挂起，否则只说程序没有运行。"""
        if gap.get("diverged"):
            return "电脑刚从休眠或挂起中恢复"
        return "程序刚才约 {}没有运行".format(self._duration_text(gap.get("sec") or 0))

    def _note_stream_resumed(self, sess, now):
        """这一轮收到第一帧：上一轮最后一帧之后多久没有音频，记一条 stream_resumed。"""
        sess["gap"] = None
        deaf_since = sess.get("deaf_since")
        if deaf_since is None:
            return
        sess["deaf_since"] = None
        audit = sess.get("audit")
        if audit is not None:
            audit.stream_resumed(deaf_sec=now - deaf_since,
                                 reconnect_no=sess.get("reconnect_no"))

    async def _on_audio_events(self, sess, events):
        """AudioFlowMeter 的事件：写审计、挂或撤持续提示。观察不能拖垮拉流，异常一律吞掉。"""
        audit = sess.get("audit")
        try:
            for kind, data in events:
                if kind == "heartbeat":
                    if audit is not None:
                        audit.audio_heartbeat(**data)
                elif kind == "summary":
                    if audit is not None:
                        audit.stream_audio(**data)
                elif kind == "low":
                    per_min = data["audio_sec"] * 60.0 / max(data["wall_sec"], 1.0)
                    await self._incident(
                        "session:audio_rate", "warn",
                        "过去 1 分钟只收到 {:.0f} 秒直播音频，缺的部分没有经过检测"
                        .format(per_min))
                elif kind == "recovered":
                    await self._incident("session:audio_rate", "clear")
                elif kind == "quiet":
                    await self._incident(
                        "session:quiet_audio", "warn",
                        "已收到 {:.0f} 秒直播音频，但音量一直低于识别门限，没有送去识别；"
                        "程序继续监听".format(data["audio_sec"]))
                elif kind == "speech":
                    await self._incident("session:quiet_audio", "clear")
        except Exception as exc:
            print("[警告] 音频计数事件处理出错: {}".format(exc))

    def _record_stream_break(self, sess, source, reason, error, got_audio, audio_secs,
                             meter):
        """一轮拉流结束时写 stream_audio + stream_break，写进 sess 里本场自己的审计。"""
        try:
            if reason == "eof" and getattr(source, "stalled", False):
                reason = "stall"
            if sess.get("gap_stop"):
                sess["gap_stop"] = False
                if reason in ("eof", "stall"):
                    reason = "clock_gap"
            if got_audio:
                sess["deaf_since"] = sess.get("last_frame_wall") or time.time()
            sess["source"] = None
            if meter is not None and meter.low:
                self._spawn(self._incident("session:audio_rate", "clear"))
            audit = sess.get("audit")
            if audit is None:
                return
            try:
                tail = source.stderr_tail()
            except Exception:
                tail = ""
            if meter is not None:
                audit.stream_audio(final=True, **meter.summary())
            audit.stream_break(
                reason, returncode=getattr(getattr(source, "proc", None), "returncode", None),
                audio_sec=audio_secs, got_audio=got_audio,
                reconnect_no=sess.get("reconnect_no", 0), stderr_tail=tail,
                error=str(error) if error is not None else None)
        except Exception as exc:
            print("[警告] 记录断流出错: {}".format(exc))

    async def _wait_for_network(self, sess):
        """重连解析之前先确认本机连得上 www.tiktok.com（DNS + TCP 443，3 秒，不发 HTTP 请求）。

        以前断网时解析照跑（每次走完五层、近一分钟），失败还算进重连预算，大约 7 分钟后
        监听就永久放弃，网络恢复了也不会自己回来。现在连不上就不解析、不计预算，每
        NETWORK_RETRY_SEC 秒再探一次；连上返回 True，超过 NETWORK_GIVE_UP_SEC 返回 False。
        探测通过而解析失败（比如要网页登录的公共 Wi-Fi）时，行为和以前一样。"""
        from .resolver import tiktok_reachable

        ok, why = await tiktok_reachable()
        if ok:
            return True
        audit = sess.get("audit")
        since = time.time()
        if audit is not None:
            audit.network_down(since, why=why)
        print("[网络] 本机连不上 www.tiktok.com（{}），暂停解析".format(why))
        tries = 0
        while tries * self.NETWORK_RETRY_SEC < self.NETWORK_GIVE_UP_SEC:
            deaf = time.time() - (sess.get("deaf_since") or since)
            text = "本机连不上 www.tiktok.com，已 {}没有监听，网络恢复后自动重连".format(
                self._duration_text(deaf))
            await self._incident("session:network", "error", text)
            await self.server.status("connecting", text)
            await asyncio.sleep(self.NETWORK_RETRY_SEC)
            tries += 1
            ok, why = await tiktok_reachable()
            if ok:
                if audit is not None:
                    audit.network_up(time.time() - since)
                await self._incident("session:network", "clear")
                print("[网络] 已恢复，继续重连")
                return True
        await self._incident("session:network", "clear")
        return False

    @staticmethod
    def _room_status_text(status):
        """只说接口返回了什么（CLAUDE.md 第八条）。2 本不该出现在「不在播」判定里，
        出现了也照实写出数字，按未知状态去等（等待有上限）。"""
        if status is None:
            return "TikTok 接口这次没有给出房间状态"
        if status == 2:
            return "TikTok 接口返回房间状态 2"
        return "TikTok 接口返回房间状态 {}（{}）".format(
            status, "已结束" if status == 4 else "不是在播状态")

    async def _probe_room_status(self, url):
        from .resolver import probe_room_status

        status, why = await probe_room_status(url)
        print("[解析] 房间状态复查：{}{}".format(status, "（{}）".format(why) if why else ""))
        return status, why

    async def _confirm_offline(self, url, exc, sess, waited):
        """重连时解析说「没在播」。返回 (verdict, waited)：verdict 为 live 时回去重新解析；
        其余情况这里已经写好界面文字和 sess["end"]。

        - 只有状态 4（已结束）才收手。以前任何非 2 都当结束，而 TikTok 的状态不止这两个
          （TikTokLive 也只把 4 当下播），断一下就永久停止监听。
        - 刚发现时钟跳变（电脑休眠或挂起过）时，4 也先不信：2026-09-14 那次「已结束」就是
          在合盖休眠的短暂维护唤醒里判的。醒着再等 OFFLINE_RECHECK_SEC 秒问一次房间接口。
        - 其它状态（含拿不到状态）：每 HOST_WAIT_POLL_SEC 秒问一次房间接口，一次中断里
          最多等 HOST_WAIT_MAX_SEC。
        文字只写接口返回了什么、程序在做什么，不猜主播为什么（CLAUDE.md 第八条）。"""
        from .resolver import ENDED_STATUS, LIVE_STATUS

        status = getattr(exc, "status", None)
        if status == ENDED_STATUS:
            status, waited, _why = await self._recheck_ended_after_gap(url, status, sess, waited)
            if status == LIVE_STATUS:
                return "live", waited
        if status == ENDED_STATUS:
            await self.server.status(
                "ended", "直播已结束。可以继续翻看上面的字幕，"
                         "或输入新的直播间地址。")
            print("[信息] 直播已结束。可在网页里输入新地址继续。")
            sess["end"] = {"reason": "offline", "status": status}
            return "ended", waited
        return await self._host_wait(url, status, sess, waited)

    async def _recheck_ended_after_gap(self, url, status, sess, waited, why=None):
        """状态 4，而且这次中断里发现过时钟跳变：先不信，醒着再等 OFFLINE_RECHECK_SEC 秒问一次
        房间接口。返回 (状态, waited, why)；没有跳变时原样返回，不多发请求。"""
        from .resolver import ENDED_STATUS, LIVE_STATUS

        if status != ENDED_STATUS:
            return status, waited, why
        await self._recheck_clock(sess)
        gap = sess.get("gap")
        if gap is None:
            return status, waited, why
        sess["gap"] = None
        await self.server.status(
            "connecting",
            "{}，{}；{:.0f} 秒后再查一次再下结论…".format(
                self._gap_lead(gap), self._room_status_text(status), self.OFFLINE_RECHECK_SEC))
        await asyncio.sleep(self.OFFLINE_RECHECK_SEC)
        waited += self.OFFLINE_RECHECK_SEC
        status, why = await self._probe_room_status(url)
        audit = sess.get("audit")
        if audit is not None:
            audit.host_wait(status, self.OFFLINE_RECHECK_SEC,
                            {LIVE_STATUS: "live", ENDED_STATUS: "ended"}.get(status, "waiting"),
                            trigger="clock_gap", why=why)
        return status, waited, why

    async def _direct_media_works(self, media, sess):
        """直连地址问 CDN 还出不出数据（只拉 2KB，不碰房间接口）。返回 (是否出数据, 探了几次)。

        这次中断里发现过时钟跳变时，一次失败不算数：醒来那一刻网络常常还没连上，而时钟对账
        可能已经提前断开了这一轮。按 DIRECT_RECHECK_DELAYS 再试，仍拉不到才下结论。"""
        from .resolver import _media_url_works

        await self._recheck_clock(sess)
        probes = 1
        if await _media_url_works(media):
            return True, probes
        gap = sess.get("gap")
        if gap is None:
            return False, probes
        sess["gap"] = None
        total = len(self.DIRECT_RECHECK_DELAYS)
        for i, delay in enumerate(self.DIRECT_RECHECK_DELAYS, 1):
            await self.server.status(
                "connecting", "{}，这个直连地址暂时拉不到数据；{:.0f} 秒后再试（第 {}/{} 次）…".format(
                    self._gap_lead(gap), delay, i, total))
            await asyncio.sleep(delay)
            probes += 1
            if await _media_url_works(media):
                return True, probes
        return False, probes

    async def _host_wait(self, url, status, sess, waited):
        from .resolver import ENDED_STATUS, LIVE_STATUS

        audit = sess.get("audit")
        why = None
        if audit is not None:
            audit.host_wait(status, waited, "started")
        while waited < self.HOST_WAIT_MAX_SEC:
            await self.server.status(
                "connecting",
                "{}，每 {:.0f} 秒复查一次，最多 {:.0f} 分钟（已等 {:.0f} 分钟）…".format(
                    self._room_status_text(status), self.HOST_WAIT_POLL_SEC,
                    self.HOST_WAIT_MAX_SEC / 60, waited / 60))
            await asyncio.sleep(self.HOST_WAIT_POLL_SEC)
            waited += self.HOST_WAIT_POLL_SEC
            status, why = await self._probe_room_status(url)
            if status == ENDED_STATUS:      # 等的这一分钟里睡过的话，4 也先复查
                status, waited, why = await self._recheck_ended_after_gap(
                    url, status, sess, waited, why)
            if status in (LIVE_STATUS, ENDED_STATUS):
                break
        outcome = {LIVE_STATUS: "live", ENDED_STATUS: "ended"}.get(status, "timeout")
        if audit is not None:
            audit.host_wait(status, waited, outcome, why=why)
        if outcome == "live":
            print("[信息] 房间接口回到在播状态，重新解析")
            return "live", waited
        if outcome == "ended":
            await self.server.status(
                "ended", "直播已结束。可以继续翻看上面的字幕，"
                         "或输入新的直播间地址。")
            sess["end"] = {"reason": "offline", "status": status,
                           "waited_sec": int(round(waited))}
        else:
            await self.server.status(
                "ended", "{:.0f} 分钟内 TikTok 接口一直没有返回在播状态（最后一次：{}），"
                         "监听已停止。可以点「开始翻译」重新开始。".format(
                             waited / 60, self._room_status_text(status)))
            sess["end"] = {"reason": "host_wait_timeout", "status": status,
                           "waited_sec": int(round(waited))}
        print("[信息] 等房间恢复在播：{}".format(outcome))
        return outcome, waited

    async def _run_session(self, url):
        from .asr import create_transcriber, forget_exception_locals
        from .resolver import ResolveError, is_direct_url

        sess = getattr(self, "_session_state", None)
        if sess is None or sess.get("audit") is not self.audit:
            sess = self._session_state = self._new_session_state(self.audit)
        my_audit = sess["audit"]     # 本场自己的审计，见 _run_stream_inner 与 _new_session_state
        await self.server.status("connecting", "正在解析直播流地址…")
        try:
            media = await self._resolve_media(url)
            self._resolve_fail_streak = 0
        except ResolveError as exc:
            self._note_resolve_failure(exc)
            await self.server.status("error", str(exc))
            print("[错误] {}".format(exc))
            sess["end"] = {"reason": "resolve_error", "kind": exc.kind}
            return
        await self._start_pending_comments(my_audit)    # macOS：弹幕等第一次解析返回才起

        # 从这里到识别模型就绪，中途 return 只可能是「加载模型失败」
        sess["end"] = {"reason": "model_load_failed"}
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
        config = {"backend": backend, "model": model, "device": device,
                  "compute_type": compute, "language": self.args.source,
                  "beam_size": self.args.beam, "use_context": self.args.context,
                  "temperature": temperature, "hotwords": self.glossary.asr_prompt(),
                  "note": rec["note"]}
        # 上一场「改用 CPU」的加载被停止打断、还在线程里跑：先等它，别和下面的加载同时驻留两份
        await self._settle_fallback_load(key)
        if self._transcriber is None or self._transcriber_key != key:
            # 正在用的是出错后改用的 CPU 模型：先放掉再加载新配置，两个模型不同时驻留
            dropped_fallback = await self._drop_fallback_transcriber()
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
            fut = self._transcriber_future
            if (fut is not None and fut.done() and not fut.cancelled()
                    and fut.exception() is None and self._loading_key == key):
                pass    # 上次被取消的那场加载已经跑完了：直接拿结果，别再载一遍
            elif fut is None or fut.done() or self._loading_key != key:
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
            as_configured = True
            try:
                # shield：本任务被取消时不要连带取消底层加载，
                # 下一次启动可以直接复用同一个在途结果
                transcriber = await asyncio.shield(self._transcriber_future)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 回溯里的帧还连着加载了一半的模型（mlx 预热失败时 model 是局部变量）：
                # 不清掉，下面改用 CPU 时两个模型同时驻留
                forget_exception_locals(exc)
                self._transcriber_future = None
                self._loading_key = None
                watcher.cancel()   # 先停进度播报，别让它把下面的 error 状态盖回去
                transcriber = await self._on_model_load_failed(exc, config, loop, key=key)
                if transcriber is None:
                    return
                as_configured = False
            finally:
                watcher.cancel()
            # 模型和它的 key 一起提交：中途取消时两者都不动，下次重来还会重新加载
            self._transcriber = transcriber
            self._transcriber_key = key
            if as_configured:
                await self._asr_loaded_ok(refresh=dropped_fallback)
        transcriber = self._transcriber
        sess["end"] = None
        if my_audit is not None:
            my_audit.asr_config(self._asr_config_record(config, transcriber))
        self._record_asr_memory(sess, transcriber)     # 模型就绪时一条，之后统计循环每 5 分钟一条
        slot = _ASRSlot(transcriber, config=config, key=key, audit=my_audit)

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
        reconnects = 0        # 连续重连次数：决定退避间隔（2、4、…、30 秒）
        silent = 0            # 连续「一帧音频都没有」的轮次：决定何时放弃
        host_waited = 0.0     # 这次中断里等房间恢复在播已经等了多久；收到音频才清零
        while True:
            await self.server.status("connecting", "正在连接直播音频流…")
            got_audio, audio_secs = await self._stream_session(
                media, slot, denoise, live_note, loop, sess=sess)
            if got_audio:
                host_waited = 0.0
            if audio_secs >= 30:
                if direct:
                    # 直连地址问不出「主播还在不在播」，但问得出「这个地址还出不出数据」。
                    # 以前播过 30 秒就一律按「直播流已结束」收尾：Wi-Fi 抖一下、半开连接被
                    # 看门狗掐断，剩下的直播就没人听了。只向 CDN 拉 2KB，不碰房间接口
                    works, probes = await self._direct_media_works(media, sess)
                    if not works:
                        await self.server.status(
                            "ended", "这个直连地址已经拉不到数据，监听已停止。"
                                     "可以输入直播间地址或新的流地址继续。")
                        print("[信息] 直连地址已拉不到数据，监听停止。")
                        sess["end"] = {"reason": "stream_ended", "probes": probes}
                        return
                reconnects = 0        # 刚才播得好好的：重置重连预算
            # 拿到过音频的轮次不算失败：网络劣化时每轮只播二十几秒就断，
            # 五轮之后主播还在播，监听却宣布「重连失败」放弃了。只有连续几轮
            # 一帧都没有（地址过期、真下播）才放弃；退避间隔照常增长，不空转
            silent = 0 if got_audio else silent + 1

            media = None
            while media is None:
                reconnects += 1
                if silent > budget:
                    await self.server.status(
                        "error", "直播流多次中断且自动重连失败——可能直播已结束，"
                                 "或网络不稳。请稍后点「开始翻译」重试。")
                    print("[信息] 自动重连预算用尽，放弃。")
                    sess["end"] = {"reason": "reconnect_exhausted",
                                   "silent": silent, "budget": budget}
                    return
                delay = min(30, 2 ** reconnects)
                await self.server.status(
                    "connecting",
                    "直播流中断，{} 秒后自动重连（第 {}/{} 次）…".format(
                        delay, reconnects, budget))
                await asyncio.sleep(delay)
                # 本机连不上 TikTok 时不去解析，也不算进重连预算（直连地址不走这一步）
                if not direct and not await self._wait_for_network(sess):
                    await self.server.status(
                        "error", "本机连不上 www.tiktok.com 已超过 {:.0f} 分钟，监听已停止。"
                                 "网络恢复后点「开始翻译」重新开始。".format(
                                     self.NETWORK_GIVE_UP_SEC / 60))
                    print("[信息] 网络长时间不通，放弃重连。")
                    sess["end"] = {"reason": "network_down",
                                   "down_sec": int(self.NETWORK_GIVE_UP_SEC)}
                    return
                sess["reconnect_no"] += 1
                try:
                    media = await self._resolve_media(url, reconnect=sess["reconnect_no"])
                    self._resolve_fail_streak = 0
                except ResolveError as exc:
                    if exc.kind == "offline":
                        verdict, host_waited = await self._confirm_offline(
                            url, exc, sess, host_waited)
                        if verdict != "live":
                            return        # 界面文字和结束原因 _confirm_offline 已经写好
                        reconnects = 0    # 房间回到在播：马上重新解析，不再长退避
                        continue
                    self._note_resolve_failure(exc)
                    silent += 1       # 解析不出地址也是一轮没有音频
                    print("[错误] 重连解析失败: {}".format(exc))

    async def _stream_session(self, media, transcriber, denoise, live_note, loop, sess=None):
        """跑一轮拉流→识别→翻译，直到流断开。返回 (是否收到过音频, 音频时长秒)。

        sess 是这一场的观察状态（见 _new_session_state）：这一轮怎么断的、断之前收到多少
        音频、下一轮隔了多久才又有声音，都写进 sess 里那一份审计，不读 self.audit——
        晚到的旧任务写不进下一场的文件。

        音频时长按真实收到的帧数累计，不用墙钟——网络劣化时 ffmpeg 可能连着
        30 秒只吐 2 秒音频，用墙钟会把这种「假连接」当成播得好好的，
        重连预算被错误重置后放弃分支永远走不到。"""
        from .asr import forget_exception_locals
        from .audio import FRAME_SEC, SAMPLE_RATE, AudioFlowMeter, FFmpegAudioSource
        from .audit import strip_url_queries
        from .segmenter import SilenceSegmenter

        if sess is None:
            sess = self._new_session_state(self.audit)

        # transcriber 可以是 _ASRSlot（_run_session 传的，出错换模型后重连也用新的），
        # 也可以是裸识别器（这时没有换模型需要的配置，出错只报告不换）
        slot = transcriber if isinstance(transcriber, _ASRSlot) else _ASRSlot(
            transcriber, audit=sess["audit"])
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
        sess["source"] = source
        meter = sess.get("meter")
        if meter is None:
            meter = sess["meter"] = AudioFlowMeter(
                speech_rms=getattr(segmenter, "silence_rms", 300.0))
        meter.start_round()

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
                if sess["audit"] is not None:
                    sess["audit"].dropped_audio(queue_depth=queue.qsize())
                print("[严重] 积压超过 {} 秒，被迫丢弃最旧的一段音频——这段可能漏词"
                      .format(AUDIO_BACKLOG_HARD_SEC))
            queue.put_nowait(segment)
            backlog["sec"] += dur
            self.telemetry.asr_queue_depth = queue.qsize()
            self.telemetry.set_backlog(backlog["sec"])

        def _drop_job(job):
            """告诉界面这条不会有译文了——否则它永远停在「翻译中…」。"""
            self.telemetry.drop_translation()
            self._spawn(self.server.broadcast({
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
                if stale is None:        # 已经在收尾：哨兵放回去，这条不翻了
                    trans_queue.put_nowait(None)
                    _drop_job(job)
                    return
                _drop_job(stale)
            trans_queue.put_nowait(job)
            self.telemetry.translation_queue_depth = trans_queue.qsize()

        def _put_sentinel():
            """收尾哨兵绝不能抛 QueueFull：翻译卡住时队列正好是满的（4 条），
            裸 put_nowait 会把「流断了→自动重连」变成「内部错误，已停止」，
            剩下的直播就没人听了。挤掉积压的旧任务给哨兵腾位——反正下面
            最多只等 5 秒，它们本来也跑不到。"""
            while trans_queue.full():
                try:
                    stale = trans_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if stale is not None:
                    _drop_job(stale)
            trans_queue.put_nowait(None)

        async def reader():
            # 「直播中」要等真的收到音频才宣布——ffmpeg 连流失败时不能先报喜再改口
            nonlocal got_audio, audio_secs
            try:
                async for frame in source.frames():
                    now = time.time()
                    if not got_audio:
                        got_audio = True
                        self._note_stream_resumed(sess, now)
                        await self.server.status("live", live_note)
                    audio_secs += FRAME_SEC
                    sess["last_frame_wall"] = now
                    segments = segmenter.feed(frame)
                    for segment in segments:
                        _put((segment, time.time()))
                    # 实际收到多少音频、有没有切出语音段：每帧只计数，有事件才 await
                    events = meter.feed(frame, segments=len(segments))
                    if events:
                        await self._on_audio_events(sess, events)
                rest = segmenter.flush()
                for segment in rest:   # 别丢掉最后一段话
                    _put((segment, time.time()))
                events = meter.cut(len(rest))
                if events:
                    await self._on_audio_events(sess, events)
            finally:
                _put(None)

        async def asr_worker():
            asr_failures = 0
            streak_handled = False   # 这一串连续出错已经处理过（换过模型或说过恢复不了）
            banner_up = False        # 出错提示在界面上：下一段识别成功时撤掉
            while True:
                item = await queue.get()
                if item is None:
                    break
                segment, audio_end_ts = item
                backlog["sec"] = max(0.0, backlog["sec"] - len(segment) / 2.0 / SAMPLE_RATE)
                self.telemetry.set_backlog(backlog["sec"])
                t0 = time.monotonic()
                # 在途标记：一段识别迟迟不返回时，统计循环据此报「卡住」（_check_asr_stall）
                inflight = [t0]
                self._asr_inflight = inflight
                failure = None
                try:
                    result = await loop.run_in_executor(
                        asr_pool, slot.transcriber.transcribe, segment
                    )
                except Exception as exc:
                    failure = exc
                    # 回溯里的帧连着模型（mlx_whisper.transcribe 的局部变量 model、faster-whisper
                    # 帧里的 self）：failure 还活着，放掉 ModelHolder 也放不掉模型，下面换 CPU
                    # 时就是两个模型同时驻留
                    forget_exception_locals(exc)
                finally:
                    if getattr(self, "_asr_inflight", None) is inflight:
                        self._asr_inflight = None
                if failure is not None:
                    # 这段音频没进检测器也没进审计——漏报的第五种成因，而且以前
                    # 只有一行 print（打包运行时 stdout 指向 /dev/null，等于没有）。
                    # 计数、写审计、连续失败就告诉界面并设法恢复，别让识别已死的会话
                    # 继续显示「直播中」。
                    asr_failures += 1
                    self.telemetry.drop_audio(asr_failed=True)
                    if sess["audit"] is not None:
                        sess["audit"].asr_failed(
                            segment_ms=len(segment) / 2.0 / SAMPLE_RATE * 1000.0,
                            error=strip_url_queries(failure, 200), queue_depth=queue.qsize())
                    print("[警告] 识别一段音频失败: {}".format(failure))
                    if asr_failures >= self.ASR_FAILURES_BEFORE_RECOVERY and not streak_handled:
                        streak_handled = banner_up = True
                        if await self._on_asr_failures(slot, strip_url_queries(failure, 200),
                                                       asr_failures, backlog["sec"],
                                                       asr_pool, loop):
                            # 换了模型：重新计数，新模型再连续出错才轮到「恢复不了」
                            asr_failures, streak_handled = 0, False
                    continue
                asr_failures, streak_handled = 0, False
                if banner_up:
                    banner_up = False
                    await self._asr_calls_recovered(slot, backlog["sec"])
                asr_ms = (time.monotonic() - t0) * 1000.0
                segment_ms = len(segment) / 2.0 / SAMPLE_RATE * 1000.0
                if asr_ms > segment_ms:
                    # 解码比音频本身还久：多半是复读跑飞，继续下去队列就会溢出
                    self.telemetry.note_overrun()
                    print("[警告] 识别耗时 {:.1f}s 超过片段时长 {:.1f}s（疑似复读跑飞）"
                          .format(asr_ms / 1000, segment_ms / 1000))
                    if sess["audit"] is not None:
                        sess["audit"].asr_overrun(asr_ms=asr_ms, segment_ms=segment_ms)
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
                _put_sentinel()
                try:
                    await asyncio.wait_for(asyncio.shield(trans_task),
                                           timeout=self.TRANSLATION_DRAIN_SEC)
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

        reason, error = "eof", None
        try:
            await run_workers()
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception as exc:
            reason, error = "error", exc
            raise
        finally:
            # 先切 ffmpeg 再收尾，任何退出路径（含异常）都不留子进程
            try:
                await source.stop()
            finally:
                asr_pool.shutdown(wait=False)
                # 这一轮怎么断的写进本场审计：打包运行时终端输出没人看得到
                self._record_stream_break(sess, source, reason, error, got_audio,
                                          audio_secs, meter)
        tail = source.stderr_tail()
        if tail:
            print("[信息] ffmpeg 输出: {}".format(tail))   # 英文技术输出只进终端，不上 UI
        return got_audio, audio_secs

    # ---- 识别出错：记录、说清、在安全的前提下换一个识别配置 ----
    ASR_FAILURES_BEFORE_RECOVERY = 3

    @staticmethod
    def _asr_active(transcriber, config):
        """识别器实际生效的配置：CUDA 构造时退回了 CPU、出错后改用了 CPU，都以对象上记的为准。"""
        return {"backend": getattr(transcriber, "backend", None) or config.get("backend"),
                "model": getattr(transcriber, "model_size", None) or config.get("model"),
                "device": getattr(transcriber, "device", None) or config.get("device"),
                "compute_type": (getattr(transcriber, "compute_type", None)
                                 or config.get("compute_type"))}

    def _asr_config_record(self, config, transcriber):
        """asr_config 审计记录：实际在听的配置；和请求的不一样、或出错后改用过时带上来龙去脉。"""
        active = self._asr_active(transcriber, config)
        record = dict(active, note=config.get("note"))
        # 实际设上的 MLX 缓冲缓存上限（MB）；没配置、没设上、不是 mlx 后端都是 null
        record["mlx_cache_limit_mb"] = getattr(transcriber, "mlx_cache_limit_mb", None)
        if (active["backend"] != config.get("backend") or active["model"] != config.get("model")
                or (config.get("device") not in (None, "auto")
                    and active["device"] != config.get("device"))):
            record["requested"] = {k: config.get(k)
                                   for k in ("backend", "model", "device", "compute_type")}
        if getattr(self, "_asr_fallback", None):
            record["fallback"] = dict(self._asr_fallback)
        return record

    @staticmethod
    def _transcriber_kwargs(cfg):
        return {"backend": cfg["backend"], "model_size": cfg["model"],
                "device": cfg["device"], "compute_type": cfg["compute_type"],
                "language": cfg.get("language"), "beam_size": cfg.get("beam_size", 5),
                "use_context": cfg.get("use_context", False),
                "temperature": cfg.get("temperature", DEFAULT_TEMPERATURE),
                "hotwords": cfg.get("hotwords")}

    def _asr_fallback_plan(self, config, active=None):
        """识别出错后可以改用的配置：返回 (配置, None)；没有安全的选择时返回 (None, 原因)。

        * CUDA 出错：同一个模型改在 CPU 上跑（int8，与 asr.Transcriber 构造失败时的退路一致）；
        * 苹果 GPU（mlx）出错：CPU 上跑 large-v3-turbo（hwdetect.CPU_FALLBACK）；
        * 其余配置（本来就在 CPU 上）没有更稳的退路：no_fallback_for_config；
        * 只用已经**完整下载**的模型，没下全是 fallback_not_cached——直播中途开始下载 1.6 GB，
          检测只会停得更久。两个原因分开记：复查的人看到「没下载」会去查下载，那未必是问题。"""
        from .hwdetect import CPU_FALLBACK
        from .selfcheck import _model_cached
        current = dict(config, **{k: v for k, v in (active or {}).items() if v})
        if current.get("backend") == "ct2" and current.get("device") == "cuda":
            plan = dict(current, device="cpu", compute_type="int8")
        elif current.get("backend") == "mlx":
            plan = dict(current, **CPU_FALLBACK)
        else:
            return None, "no_fallback_for_config"
        try:
            cached = _model_cached(plan["model"], "ct2")
        except Exception:
            cached = False
        return (plan, None) if cached else (None, "fallback_not_cached")

    async def _drop_fallback_transcriber(self):
        """要加载新配置之前，放掉出错后改用的那个 CPU 模型。返回是否真的放掉了。"""
        if getattr(self, "_asr_fallback", None) is None:
            return False
        from .asr import release_transcriber
        old, self._transcriber, self._transcriber_key = self._transcriber, None, None
        if old is not None:
            release_transcriber(old)
            self._forget_transcriber(old)
        self._asr_fallback = None
        await self._incident("asr-fallback", "clear")
        return True

    def _forget_transcriber(self, transcriber):
        """放掉了的识别器不能再被下一场拿回来用。上次按配置加载的结果除了 _transcriber，
        还留在 _transcriber_future 里（加载中被停止时靠它复用）——只清前者的话，下一场
        「开始」会直接拿回这个已释放的识别器，每段都报「识别模型已释放」。"""
        if getattr(self, "_transcriber", None) is transcriber:
            self._transcriber, self._transcriber_key = None, None
        fut = getattr(self, "_transcriber_future", None)
        if (fut is not None and fut.done() and not fut.cancelled()
                and fut.exception() is None and fut.result() is transcriber):
            self._transcriber_future, self._loading_key = None, None

    def _track_fallback_load(self, future, key, info, incident):
        """记下在途的「改用 CPU」加载。线程里的加载取消不掉：中控这时点了停止，下一场开始时
        由 _settle_fallback_load 等它结束接着用，而不是再起一份。"""
        self._fallback_load = {"future": future, "key": key, "info": info,
                               "incident": incident}
        return self._fallback_load

    def _end_fallback_load(self, pending):
        if getattr(self, "_fallback_load", None) is pending:
            self._fallback_load = None

    async def _settle_fallback_load(self, key):
        """上一场「改用 CPU」的加载被停止打断了，还在线程里跑。开始这一场之前先等它结束：
        配置没变就接着用它——和没被打断时一样，不再去加载那个出过错的原配置；配置变了就
        放掉。两种情况都不会和下面按配置的加载同时驻留两份模型。"""
        from .asr import release_transcriber
        pending = getattr(self, "_fallback_load", None)
        if pending is None:
            return
        if not pending["future"].done():
            await self.server.status(
                "connecting", "上一场开始改用的 CPU 识别模型（{}）还在加载，等它加载完…"
                .format(pending["info"]["to"]))
        try:
            new = await asyncio.shield(pending["future"])
        except asyncio.CancelledError:
            raise                          # 又点了停止：留给下一场接着等
        except Exception as exc:
            self._end_fallback_load(pending)
            print("[错误] 改用 CPU 识别没能加载: {}".format(exc))
            return                         # 什么也没驻留：下面按配置加载
        if getattr(self, "_fallback_load", None) is not pending:
            return                         # 发起加载的那一场自己收了尾（用上或放掉了）
        self._fallback_load = None
        if pending["key"] is None or pending["key"] != key or self._transcriber is not None:
            release_transcriber(new)
            return
        self._transcriber, self._transcriber_key = new, key
        self._asr_fallback = dict(pending["info"])
        print("[警告] 接着使用上一场改用的 CPU 识别：{}".format(pending["info"]["to"]))
        await self._incident("asr-fallback", "warn", pending["incident"])
        await self._refresh_asr_check()

    async def _asr_loaded_ok(self, refresh=False):
        """按配置加载成功：之前记下的加载失败作废，自检「语音识别」那一行刷回来。"""
        if getattr(self, "_asr_load_error", None) is not None:
            self._asr_load_error = None
            refresh = True
        if refresh:
            await self._refresh_asr_check()

    async def _on_model_load_failed(self, exc, config, loop, key=None):
        """识别模型没能加载。写审计；界面上只说观察到的事和能做的事——网络、磁盘、Metal
        初始化都出现过，程序分不清，就不猜。苹果 GPU 后端失败、而 CPU turbo 已经完整下载时
        改用它。返回能用的识别器；没有就返回 None（这一场随即结束）。
        key 是这套配置的 key：改用 CPU 的加载被停止打断时，下一场按它接着用。"""
        from .asr import create_transcriber, release_mlx_model
        from .audit import strip_url_queries
        error = strip_url_queries(exc, 200)
        backend, model, device = config.get("backend"), config.get("model"), config.get("device")
        print("[错误] 加载模型失败: {}".format(exc))
        audit = getattr(self, "audit", None)
        if audit is not None:
            audit.asr_load_failed(backend, model, device, error)
        self._asr_load_error = {"backend": backend, "model": model, "device": device,
                                "error": error}
        plan = self._asr_fallback_plan(config)[0] if backend == "mlx" else None
        if plan is not None:
            source, target = _describe_asr(config), _describe_asr(plan)
            await self.server.status(
                "connecting", "识别模型（{}/{}）没能加载，正在改用 CPU 识别（{}）…".format(
                    backend, model, target))
            release_mlx_model()     # 预热失败时模型可能已经进了 mlx 的类级缓存
            pending = self._track_fallback_load(
                loop.run_in_executor(
                    None, lambda: create_transcriber(**self._transcriber_kwargs(plan))),
                key, {"from": source, "to": target, "error": error},
                "识别模型（{}/{}）没能加载，已改用 CPU 识别（{}），较慢，可能积压。"
                "关闭程序重新打开会重新尝试原来的识别模型".format(backend, model, target))
            try:
                # shield：这时点了停止，线程里的加载照样跑完，下一场接着用（不另载一份）
                fallback = await asyncio.shield(pending["future"])
            except asyncio.CancelledError:
                raise
            except Exception as exc2:
                self._end_fallback_load(pending)
                print("[错误] 改用 CPU 识别也没能加载: {}".format(exc2))
                if audit is not None:
                    audit.asr_backend_fallback(source, None, error, tried=target,
                                               fallback_error=str(exc2))
            else:
                self._end_fallback_load(pending)
                if audit is not None:
                    audit.asr_backend_fallback(source, target, error)
                self._asr_fallback = dict(pending["info"])
                await self._incident("asr-fallback", "warn", pending["incident"])
                await self._refresh_asr_check()
                return fallback
        await self.server.status(
            "error", "识别模型（{}/{}）没能加载。可以：确认网络和磁盘空间后点「开始翻译」重试；"
                     "若反复出现，关闭程序重新打开。\n技术细节：{}".format(backend, model, error))
        await self._refresh_asr_check()
        return None

    async def _on_asr_failures(self, slot, error, failures, backlog_sec, pool, loop):
        """识别连续出错 ASR_FAILURES_BEFORE_RECOVERY 次。返回是否换上了新的识别器。

        以前这里只发一次「请点停止再开始翻译」——可停止/开始复用的是同一个坏模型（key
        没变），mlx 的类级缓存也还在，照做什么都不会变。现在：
          * 一场最多换一次：先放掉坏模型，再加载一个**已完整下载**的 CPU 配置；
          * 没有可换的就说实话：程序自己恢复不了，请关闭程序重新打开。
        在 asr_worker 协程里做：调用都返回了，识别线程是空的，换的时候没有调用在跑。"""
        from .asr import create_transcriber, release_transcriber
        failing = "识别连续 {} 次出错，这期间的音频没有做违禁词检测".format(failures)
        old = slot.transcriber
        active = self._asr_active(old, slot.config) if slot.config else {}
        source = _describe_asr(active) if active else "?"
        plan, reason = None, "already_switched" if slot.reloaded else "no_fallback_for_config"
        if slot.config and not slot.reloaded:
            plan, reason = self._asr_fallback_plan(slot.config, active)
        if plan is None:
            if not slot.gave_up and slot.audit is not None:
                slot.audit.asr_backend_fallback(source, None, error, reason=reason)
            slot.gave_up = True
            if slot.audit is getattr(self, "audit", None):
                await self._asr_unrecoverable(failing, backlog_sec)
            return False
        slot.reloaded = True
        target = _describe_asr(plan)
        if slot.audit is getattr(self, "audit", None):
            self._asr_failing = slot.audit
            await self._incident("session:asr-failing", "error",
                                 "{}，正在改用 CPU 识别（{}）…".format(failing, target))
            await self._announce_health("degraded", backlog_sec, reason="asr_failing",
                                        text="🔴 {}，正在改用 CPU 识别…".format(failing))
        # 先放掉坏模型再加载：两个模型同时驻留正是规则三那类事故
        release_transcriber(old)
        self._forget_transcriber(old)
        pending = self._track_fallback_load(
            loop.run_in_executor(
                pool, lambda: create_transcriber(**self._transcriber_kwargs(plan))),
            slot.key, {"from": source, "to": target, "error": error},
            "GPU 识别连续出错，已改用 CPU 识别（{}），较慢，可能积压。"
            "关闭程序重新打开会重新尝试 GPU 识别".format(target))
        try:
            # shield：这时点了停止，线程里的加载照样跑完，下一场接着用（不另载一份）
            new = await asyncio.shield(pending["future"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if getattr(self, "_fallback_load", None) is not pending:
                return False              # 下一场已经接手了这次加载
            self._end_fallback_load(pending)
            print("[错误] 改用 CPU 识别没能加载: {}".format(exc))
            if slot.audit is not None:
                slot.audit.asr_backend_fallback(source, None, error, tried=target,
                                                fallback_error=str(exc))
            slot.gave_up = True
            if slot.audit is getattr(self, "audit", None):
                await self._asr_unrecoverable(failing, backlog_sec)
            return False
        if getattr(self, "_fallback_load", None) is not pending:
            return False                  # 下一场已经接手了这次加载（_settle_fallback_load）
        self._end_fallback_load(pending)
        if slot.audit is not getattr(self, "audit", None):
            release_transcriber(new)      # 这一场已经结束：没人会用它，别留在内存里
            return False
        slot.transcriber = new
        if slot.key is not None:
            self._transcriber, self._transcriber_key = new, slot.key
            self._asr_fallback = dict(pending["info"])
        if slot.audit is not None:
            slot.audit.asr_backend_fallback(source, target, error)
        print("[警告] 识别连续出错，已从 {} 改用 {}".format(source, target))
        self._asr_failing = None
        await self._incident("session:asr-failing", "clear")
        await self._incident("asr-fallback", "warn", pending["incident"])
        await self._announce_health("lagging", backlog_sec, reason="asr_fallback",
                                    text="⚠️ GPU 识别连续出错，已改用 CPU 识别（较慢，可能积压）")
        await self._refresh_asr_check()
        return True

    async def _asr_unrecoverable(self, failing, backlog_sec):
        text = "{}。程序自动恢复不了——请关闭程序重新打开；若仍出错请反馈".format(failing)
        self._asr_failing = getattr(self, "audit", None)    # 按积压算的「已追上」别盖掉它
        await self._incident("session:asr-failing", "error", text)
        await self._announce_health("degraded", backlog_sec, reason="asr_failing",
                                    text="🔴 " + text)

    async def _asr_calls_recovered(self, slot, backlog_sec):
        """出错提示发出后又识别成功了一段：撤掉提示，健康条回到按积压算的状态。"""
        if slot.audit is not getattr(self, "audit", None):
            return
        self._asr_failing = None
        await self._incident("session:asr-failing", "clear")
        await self._announce_health(self._health_level(backlog_sec), backlog_sec)

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
            self._spawn(self.updater.freshen_ytdlp(reason="resolve-failures"))

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
            # 主播、场次、此刻连着几个界面：换过房间后认得出哪条是上一场的；事后答得出
            # 「这条报警响的时候有没有页面开着」。audit.alert 原样带上这几列
            hit.update(self._alert_stamp())
            if self.audit is not None:
                self.audit.alert(hit)
            print("[警报] 疑似违禁词「{}」（{}）：{}".format(
                hit["term"], hit["tier"], hit["context"][-80:]))
            self._alert_seq += 1
            hit["alert_id"] = self._alert_seq
            alert_ids.append(self._alert_seq)
            await self.server.broadcast({"type": "alert", **hit, **self._count_alert()})
        if alert_ids:
            self._notify_alert_burst()

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

    # ---- 合规证据：审计写入健康、报警的场次戳、界面断开留痕 ----
    DISK_LOW_BYTES = 1024 ** 3                 # 剩这么多就提醒：满了审计就写不进去
    DISK_LOW_CLEAR_BYTES = 1536 * 1024 ** 2    # 回到这以上才撤提醒，免得在门槛上反复闪

    def _evidence_session_extras(self):
        """session_start 的额外列。本次运行 settings.json 损坏被备份过就记下备份名：
        事后看到 translator_requested=auto 时答得出「设置文件坏过」，而不是去猜。"""
        from .settings import corrupt_backup_name
        return {"settings_backup": corrupt_backup_name()}

    async def _watch_audit(self, streamer):
        """紧跟在 AuditLog 构造之后：记下本场报警的主播和场次戳，挂上审计写失败的回调；
        审计文件根本没建起来就挂一条持续提示——整场都不会有证据。"""
        audit = self.audit
        path = getattr(audit, "path", None)
        self._session_serial = getattr(self, "_session_serial", 0) + 1
        stamp = (Path(str(path)).stem if path is not None else "nolog-{}-{}".format(
            time.strftime("%Y%m%d-%H%M%S"), self._session_serial))
        self._alert_scope = {"session": stamp, "streamer": streamer or "", "total": 0}
        self.server.config["alerts_session"] = dict(self._alert_scope)
        await self.server.broadcast({"type": "config",
                                     "alerts_session": dict(self._alert_scope)})
        self._audit_watch = {"audit": audit, "failing": False, "detached": False,
                             "disk_low": False, "errored": False}
        if audit is None:
            return
        if path is None:
            await self._incident(
                "session:audit-open", "error",
                "本场审计日志没能创建（{}）——报警会显示，但不会留下任何证据（见自检「审计日志」）"
                .format(getattr(audit, "open_error", None) or "没有拿到错误信息"))
            return
        loop = asyncio.get_running_loop()

        def on_write_error(_info, _audit=audit):
            # 可能在别的线程里被调：只切回事件循环，别的什么都不做
            try:
                loop.call_soon_threadsafe(self._audit_write_failed, _audit)
            except RuntimeError:
                pass                     # 事件循环已经关了（程序退出途中）

        audit.on_write_error = on_write_error
        if getattr(audit, "failing", False):   # 会话头就没写进去：发生在回调挂上之前
            self._audit_write_failed(audit)

    def _audit_write_failed(self, audit):
        """审计开始写不进去（AuditLog 的回调，已切回事件循环）。只认本场自己的 audit：
        晚到的旧会话回调不许动这一场的提示。一次中断只提示一次。"""
        watch = getattr(self, "_audit_watch", None) or {}
        if (audit is not getattr(self, "audit", None) or watch.get("audit") is not audit
                or watch.get("failing")):
            return
        watch["failing"] = True
        watch["task"] = asyncio.ensure_future(self._incident(
            "session:audit-write", "error", self._audit_failing_text(audit)))

    @staticmethod
    def _audit_failing_text(audit):
        return ("审计日志写不进去（{}）——报警照常显示，但这段时间没有留下证据。"
                "程序会继续重试，报警记录先留在内存里，能写入后补写"
                .format(getattr(audit, "last_error", "") or "没有拿到错误信息"))

    @staticmethod
    def _audit_recovered_text(audit):
        gap = getattr(audit, "last_gap", None) or {}
        return ("审计日志在 {} 到 {} 之间写不进去：{} 条记录没有留下，{} 条（会话头/报警）"
                "已补写。现在已恢复写入，日志里的 audit_gap 记录标出了这一段".format(
                    str(gap.get("from") or "")[11:19] or "?",
                    str(gap.get("to") or "")[11:19] or "?",
                    gap.get("lost_records", "?"), gap.get("retained_records", "?")))

    @staticmethod
    def _audit_unwritten_text(info):
        return ("审计日志从 {} 起写不进去，到这场监听结束也没有恢复（{}）：{} 条记录没有留下，"
                "{} 条会话头/报警/会话尾没能补写。这段时间的报警只显示在了界面上".format(
                    str(info.get("since") or "")[11:19] or "?",
                    info.get("error") or "没有拿到错误信息",
                    info.get("lost", "?"), info.get("retained", "?")))

    async def _check_audit_health(self):
        """_stats_loop 每 10 秒调一次：审计写入失败与恢复、审计文件被移走、磁盘快满。
        只在状态变化时提示。自己出错不能拖垮统计循环。"""
        watch = getattr(self, "_audit_watch", None)
        try:
            audit = getattr(self, "audit", None)
            if (audit is None or not watch or watch.get("audit") is not audit
                    or getattr(audit, "path", None) is None):
                return
            failing = bool(getattr(audit, "failing", False))
            if failing and not watch["failing"]:
                watch["failing"] = True
                await self._incident("session:audit-write", "error",
                                     self._audit_failing_text(audit))
            elif not failing and watch["failing"]:
                watch["failing"] = False
                await self._incident("session:audit-write", "warn",
                                     self._audit_recovered_text(audit))
            detached = bool(audit.detached()) if hasattr(audit, "detached") else False
            if detached != watch["detached"]:
                watch["detached"] = detached
                if detached:
                    await self._incident(
                        "session:audit-moved", "error",
                        "本场审计文件已不在原来的位置（{}）——之后的记录写进的是被移走的那个"
                        "文件，它若已被删除，这些记录会丢失。点「停止」再「开始翻译」会新建"
                        "审计文件".format(audit.path))
                else:
                    await self._incident("session:audit-moved", "clear")
            free = self._log_free_bytes(audit)
            if free is not None:
                if not watch["disk_low"] and free < self.DISK_LOW_BYTES:
                    watch["disk_low"] = True
                    await self._incident(
                        "session:disk-low", "warn",
                        "磁盘剩余 {:.1f} GB，满了以后审计日志会写不进去——请腾出磁盘空间"
                        .format(free / 1024 ** 3))
                elif watch["disk_low"] and free > self.DISK_LOW_CLEAR_BYTES:
                    watch["disk_low"] = False
                    await self._incident("session:disk-low", "clear")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if watch is not None and not watch.get("errored"):
                watch["errored"] = True
                print("[警告] 审计健康检查出错: {}".format(exc))

    @staticmethod
    def _log_free_bytes(audit):
        import shutil
        try:
            return shutil.disk_usage(str(Path(str(audit.path)).parent)).free
        except (OSError, ValueError):
            return None

    def on_ui_client_dropped(self, reason, buffered_bytes=None):
        """服务端断开了一个收不下消息的页面（CaptionServer.on_client_dropped）：打一行、
        记审计。页面会自己重连并补回报警。"""
        clients = getattr(self.server, "clients", None)
        left = len(clients) if clients is not None else None
        if reason == "buffer_full":
            what = "积压了 {} KB 消息没读".format(int((buffered_bytes or 0) / 1024))
        else:
            what = "{:.0f} 秒内没收下消息".format(
                getattr(self.server, "SEND_TIMEOUT_SEC", 2.0))
        print("[警告] 一个界面页面{}，已断开它（页面会自动重连并补回报警），还连着 {} 个页面"
              .format(what, "?" if left is None else left))
        audit = getattr(self, "audit", None)
        if audit is not None:
            audit.ui_client_dropped(reason, buffered_bytes, clients_left=left)

    def _alert_stamp(self):
        scope = getattr(self, "_alert_scope", None) or {}
        clients = getattr(getattr(self, "server", None), "clients", None)
        return {"streamer": scope.get("streamer") or "",
                "session": scope.get("session") or "",
                "ui_clients": len(clients) if clients is not None else 0}

    def _count_alert(self):
        """本场报警计数：面板只留最近 50 条，页面要说得出「本场共 N 条」。
        只进广播（和 hello 的 config），不进审计——审计里本来就是全量。"""
        scope = getattr(self, "_alert_scope", None)
        if scope is None:
            return {}
        scope["total"] += 1
        current = self.server.config.get("alerts_session")
        if isinstance(current, dict) and current.get("session") == scope["session"]:
            current["total"] = scope["total"]
        return {"session_total": scope["total"]}

    def _notify_alert_burst(self):
        """系统通知（默认关，见 app/alert_notify.py）：一阵报警只发一条，不带词条和声音。
        读设置、跑命令都放线程池，不占识别循环。"""
        notifier = getattr(self, "_alert_notifier", None)
        if notifier is None:
            from .alert_notify import AlertNotifier
            notifier = self._alert_notifier = AlertNotifier()
        if notifier.note_alert():
            try:
                asyncio.get_running_loop().run_in_executor(None, notifier.send)
            except RuntimeError:
                pass

    async def _announce_settings_backup(self):
        """settings.json 损坏被备份过（见 settings.load_settings）：挂一条持续提示，
        本次运行只挂一次；用户在页面上重新选过引擎（设置里又有了 translator）就撤下。"""
        from .settings import load_settings, take_corrupt_notice
        name = take_corrupt_notice()
        if name:
            self._settings_notice_shown = True
            await self._incident("settings-corrupt", "warn",
                                 "设置文件损坏，已备份为 {}；翻译引擎和密钥需要重新填写"
                                 .format(name))
        elif getattr(self, "_settings_notice_shown", False) \
                and "translator" in load_settings():
            self._settings_notice_shown = False
            await self._incident("settings-corrupt", "clear")

    # 定义在 translator.py（启动恢复引擎时也要用），这里保留同名类属性
    ENGINE_KEY_ENV = ENGINE_KEY_ENV

    async def set_engine(self, engine, key=None):
        """从界面切换翻译引擎、并（可选）存下密钥。

        密钥存进 settings.json（已在 .gitignore 里），**从不回传页面**——
        回传的只有打码后的尾四位，够用户确认「我填的是哪一个」，
        又不至于让密钥出现在任何一条 WebSocket 消息里。

        选的是本地引擎、而本机 Ollama（在跑）里没有它的模型时**先不换**：直播中本场
        继续用原来的引擎、停止后自动下载；不在直播就马上开始下载，下好再换上。以前
        这里直接建引擎（顺手把正在用的 1.8B 从显存卸掉），于是整场每句 404，自检
        还是绿的。
        """
        from .settings import save_setting
        from .translator import TRANSLATOR_CHOICES, saved_keys

        if engine not in TRANSLATOR_CHOICES:
            return
        if key:
            env = self.ENGINE_KEY_ENV.get(engine)
            if env:
                keys = saved_keys()
                keys[env] = key.strip()
                save_setting("api_keys", keys)
        missing = await self._local_model_missing(engine)
        if missing is not None:
            await self._keep_engine_until_model(engine, missing)
            return
        # create_translator 会同步探测 Ollama（urllib，最坏 ~10 秒）：直播中在事件
        # 循环上跑会冻住音频读取、识别调度和报警广播，与 _quota_fallback 同款进线程池
        loop = asyncio.get_running_loop()
        try:
            new = await loop.run_in_executor(None, create_translator, engine)
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
        self._engine_pending = None
        self._pull_deferred = None       # 之前为别的引擎推迟的下载不再需要；下一场开始时会重新判断
        save_setting("translator", engine)
        # 换了引擎（重填密钥也是新建一个对象）：旧引擎的报错横幅和冷却提示不再适用
        await self._clear_engine_marks()
        await self._publish_engine()
        await self.run_selfcheck()

    async def _local_model_missing(self, engine):
        """engine 是本地引擎、Ollama 在跑、但里面没有它要的模型时，返回那个模型名；
        否则 None（不是本地引擎；或 Ollama 连不上——那交给自检和自动启动）。
        探测是同步 urllib，放线程池。"""
        from . import translator as T

        model = T.local_engine_model(engine)
        if model is None:
            return None

        def probe():
            names = T._ollama_models_or_none()
            if names is None:
                return None
            return None if T.model_listed(model, names) else model    # 同 _local_wanted

        try:
            return await asyncio.get_running_loop().run_in_executor(None, probe)
        except Exception:
            return None

    async def _keep_engine_until_model(self, engine, model):
        """记下用户的选择，但先不换引擎：模型还没有（见 set_engine）。"""
        from .settings import save_setting
        from .translator import engine_label, model_label

        self.args.translator = engine
        self.args.translator_note = None
        self._engine_pending = engine
        save_setting("translator", engine)
        if self._stream_active():
            current = getattr(self.translator, "name", None)
            text = "本机 Ollama 里还没有 {}，本场{}；停止后自动下载".format(
                model_label(model), "继续用" + engine_label(current) if current else "继续不翻译")
            print("[信息] " + text)
            self.args.translator_note = text     # 引擎面板上一直看得到，直到下次选引擎
            self._pull_deferred = model
            self._pull_deferred_session = getattr(self, "audit", None)
            self._audit_pull("deferred", model)
            await self.server.broadcast({"type": "notice", "text": text})
        elif hasattr(self, "_bg_tasks"):
            self._spawn(self.ensure_local_translator())    # 马上下载，下好换上
        await self._publish_engine()
        await self.run_selfcheck()

    async def _clear_engine_marks(self):
        """忘掉已经报过的引擎问题；横幅挂着的话撤掉。"""
        self._cooldown_mark = None
        self._engine_error_mark = None
        if getattr(self, "_engine_incident_up", False):
            self._engine_incident_up = False
            self._engine_rejection = None
            await self._incident(self.ENGINE_INCIDENT, "clear")

    async def _publish_engine(self):
        """把当前引擎和各密钥的填写状态告诉页面（密钥只给尾四位）。"""
        from .settings import load_settings
        from .translator import mask_key

        await self._announce_settings_backup()
        stored = load_settings().get("api_keys", {})
        # DeepL 的月度用量：中控要能看着额度用（实测约 3.5 万字符/小时，
        # Developer 档一次性 100 万字符 ≈ 29 小时）。拿不到就不显示，最多等 3 秒
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
        地方（实测不挂术语表词表遵从率只有 26.5%）。

        强模型没译出来（返回空或出错）时用常驻引擎再译一次，并清掉 self._strong，
        下一条报警重新探测——7B 可能在两场之间被磁盘面板删了，以前每条报警都去叫
        一个不存在的模型，直到重启程序。识别正在积压、或另一条报警正占着强模型时，
        一开始就用常驻引擎：违禁词报警延迟排在译文质量前面（CLAUDE.md §6），7B 和
        Whisper 抢内存会把识别拖得更慢。每一次都记一条 alert_translation。"""
        from .translator import looks_fabricated

        if not context.strip():
            return
        # 本场自己的审计：这个任务可能换场之后才回来，不能写进下一场的文件
        audit = getattr(self, "audit", None)
        t0 = time.monotonic()

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

        fast = self.translator
        backlog = getattr(getattr(self, "telemetry", None), "audio_backlog_sec", 0.0) or 0.0
        for_backlog = backlog >= AUDIO_BACKLOG_WARN_SEC
        for_busy = not for_backlog and getattr(self, "_alert_strong_busy", False)
        # 占住强模型要在探测之前：探测会让出事件循环，同时到的另一条报警得看得见
        claimed = not (for_backlog or for_busy)
        if claimed:
            self._alert_strong_busy = True
        strong = used = out = error = None
        fallback = hard_fail = held = False
        try:
            if claimed:
                strong = await self._strong_translator()
                strong_model = self._model_of(strong) if strong is not None else None
                if strong_model and fast is not None and strong_model == self._model_of(fast):
                    # 本机最强的就是常驻的那个：直接用常驻实例。按需实例 keep_alive=0，
                    # 用它会在每条报警之后把常驻模型从显存里卸掉
                    strong = None
                if strong is None:
                    self._alert_strong_busy = claimed = False
                else:
                    self._hold_strong(strong)
                    held = True
            used = first = strong or fast
            if first is None:
                if for_backlog:
                    why = "识别正在积压，这条报警先不翻译"
                elif for_busy:
                    why = "另一条报警正在用强模型翻译，这条先不翻译"
                else:
                    why = "没有可用的翻译引擎"
                await tell(why=why)
                self._record_alert_translation(audit, alert_ids, None, None, t0, False, why,
                                               for_backlog, for_busy, None)
                return
            text, hint = self._for_translation(context)
            try:
                out = await first.translate(text, self.target, source=lang or "auto",
                                            glossary=hint)
                if out and looks_fabricated(context, out):
                    print("[警告] 报警上下文的译文不像译文（疑似模型在回话），"
                          "改用常规引擎")
                    fallback, used = True, fast
                    out = await fast.translate(
                        context, self.target,
                        source=lang or "auto") if fast else None
                elif out and self.glossary:
                    out = self.glossary.apply(text, out)
            except Exception as exc:
                print("[警告] 报警上下文翻译失败: {}".format(exc))
                out, hard_fail = None, True
            if claimed:
                self._alert_strong_busy = claimed = False
            if not out and first is strong and strong is not None and not fallback:
                err = getattr(strong, "last_error", None)
                if err and err[0] is not None:
                    error = "HTTP {}{}".format(err[0], "：" + err[1] if err[1] else "")
                if getattr(self, "_strong", None) is strong:
                    self._drop_strong()       # 下一条报警重新探测（探测在线程池里）
                if fast is not None and fast is not strong:
                    print("[警告] 强模型没有给出报警上下文译文，改用常驻引擎再译一次")
                    fallback, used, hard_fail = True, fast, False
                    try:
                        out = await fast.translate(text, self.target,
                                                   source=lang or "auto", glossary=hint)
                        if out and self.glossary:
                            out = self.glossary.apply(text, out)
                    except Exception as exc:
                        print("[警告] 常驻引擎翻译报警上下文也失败: {}".format(exc))
                        out, hard_fail = None, True
        finally:
            if claimed:
                self._alert_strong_busy = False
            if held:
                self._release_strong(strong)
        why = "" if out else ("翻译超时或出错" if hard_fail else "模型没有返回译文")
        await tell(out, why=why)
        self._record_alert_translation(audit, alert_ids, used, out, t0, fallback, why,
                                       for_backlog, for_busy, error)

    @staticmethod
    def _model_of(engine):
        """引擎实际用的模型名：本地引擎和 OpenAI 在实例的 model 上，Claude 在类的
        MODEL 上；都没有（DeepL、Google）就用引擎名。"""
        inner = getattr(engine, "inner", engine)
        return (getattr(inner, "model", None) or getattr(inner, "MODEL", None)
                or getattr(engine, "name", None))

    def _record_alert_translation(self, audit, alert_ids, used, out, t0, fallback, why,
                                  for_backlog, for_busy, error):
        if audit is None:
            return
        audit.alert_translation(
            alert_ids, self._model_of(used) if used is not None else None, bool(out),
            (time.monotonic() - t0) * 1000.0, fallback, why,
            downgraded_for_backlog=for_backlog, downgraded_for_busy=for_busy, error=error)

    def _strong_users(self):
        """正在用的强模型实例：id → [实例, 用的人数]。清掉 self._strong 时靠它判断能不能
        马上关掉旧实例的 HTTP 连接——关早了会掐断别人半路的请求，还会被当成「强模型
        失败」记进审计。"""
        users = getattr(self, "_strong_in_use", None)
        if users is None:
            users = self._strong_in_use = {}
        return users

    def _hold_strong(self, strong):
        self._strong_users().setdefault(id(strong), [strong, 0])[1] += 1

    def _release_strong(self, strong):
        users = self._strong_users()
        entry = users.get(id(strong))
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] > 0:
            return
        del users[id(strong)]
        if strong is not getattr(self, "_strong", None):
            self._close_later(strong)    # 已经换下的实例：最后一个用的人用完就关

    def _drop_strong(self):
        """忘掉缓存的强模型实例，下次用时重新探测；没人在用就马上关掉它的连接，有人在用
        就等最后一个人用完（_release_strong）。以前只是置 None：每场开始、每次强模型
        失败都丢下一个没关的 aiohttp 会话。"""
        old, self._strong = getattr(self, "_strong", None), None
        if old is not None and id(old) not in self._strong_users():
            self._close_later(old)

    def _close_later(self, engine):
        """不等结果地关掉一个换下来的引擎实例的连接。这一步跑在报警翻译的 finally 里：
        清理出了任何错都只能放弃，不能让报警框等不到回话（半成品实例没有 _bg_tasks 也一样）。"""
        if getattr(engine, "close", None) is None:
            return
        coro = self._close_engine(engine)
        try:
            self._spawn(coro)
        except Exception:
            coro.close()

    @staticmethod
    async def _close_engine(engine):
        try:
            await engine.close()
        except Exception:
            pass

    async def _migrate_glossary(self, confirm):
        """「迁移旧词表」：扫描 → 展示 → 用户确认 → 备份 → 迁移。

        永不自动执行、永不猜测：能迁的只有整条与旧官方模板完全一致的行
        （migration_plan 的判据），用户自己写的内容一个字节都不动。"""
        from .glossary import (load as load_glossary, migrate_legacy_entries,
                               migration_plan, set_active)
        from .provenance import streamer_of

        gpath = getattr(self.args, "glossary", None)
        if not confirm:
            plan = migration_plan(gpath)
            await self.server.broadcast({
                "type": "glossary_migration", "stage": "plan",
                "entries": [{"display": p["display"], "streamer": p["streamer"]}
                            for p in plan]})
            return
        result = migrate_legacy_entries(gpath)
        if result and result["total"]:
            # 热重载：正在直播也立刻用干净的词表（audit 的 merged_glossary_hash
            # 在下一场才会体现，本场以界面提示为准）
            streamer = streamer_of(self.server.config.get("room_url", ""))
            self.glossary = load_glossary(gpath, streamer=streamer or None)
            set_active(self.glossary)
            await self._publish_watchlist()
        if not migration_plan(gpath):      # 没有剩余可迁条目才撤掉入口
            self.server.config.pop("glossary_migration", None)
        await self.server.broadcast({
            "type": "glossary_migration", "stage": "done",
            "result": result and {"backup": result["backup"],
                                  "total": result["total"],
                                  "failed": result["failed"],
                                  "moved": result["moved"]}})

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
        # 措辞不承诺「下月重置」：新的 Developer 档 100 万字符是**一次性**
        # 额度（用完要升级付费档），只有已下架的老 Free 档才按月刷新。程序
        # 无法从 API 分辨账户档位，绝不替 DeepL 承诺任何刷新——曾经这里写着
        # 「额度每月重置」，对一次性额度的账户就是假话
        if name == "google":
            # 不是本地引擎，字幕会发给 Google——合规工具的提示绝不能在
            # 「数据去哪了」这件事上含糊
            note = ("DeepL 免费额度已用完，且本机没有可用的本地模型，"
                    "已自动改用 Google 免费接口继续翻译——注意：字幕文本会"
                    "发送给 Google。在 DeepL 升级/续费后重选 DeepL 即可回来。")
        else:
            note = ("DeepL 免费额度已用完，本场已自动改用本地引擎（{}）继续"
                    "翻译。在 DeepL 升级/续费后重选 DeepL 即可回来。"
                    ).format(name)
        print("[警告] " + note)
        self.args.translator_note = note
        await self.server.broadcast({"type": "notice", "text": note})
        await self._publish_engine()
        return new

    ENGINE_INCIDENT = "session:translation-engine"
    # 本地 Ollama 连续几次非 200 才说：单独一次 500 可能只是一时载入失败
    ENGINE_ERROR_STREAK = 3

    async def _note_engine_failure(self, tr):
        """一条字幕没译出来：看引擎自己记下的 HTTP 回应，说得清的告诉中控、记进审计。

        三种情况，都只在状态变化时说一次——同一个引擎在同一场里说过就不再说，直到它
        中间真的成功过一次（缓存命中不算，见 _note_engine_ok）：
          * 回 429 且引擎正在暂停请求：一条提示 + translation_cooldown；
          * 远程引擎回 401/403/404：translation_engine_error；本机有本地模型就本场改用
            它，没有就挂一条横幅、留在原引擎按冷却重试——**绝不**自动改发 Google；
          * 本地 Ollama 连续 ENGINE_ERROR_STREAK 次非 200：translation_engine_error +
            引用 Ollama 原话的横幅，并重跑备模型（模型不在了会安排停止后重新下载）。
        返回本场改用的本地引擎，没有就是 None。文字只写回应本身和能做的事，不猜原因。"""
        from .translator import BaseTranslator, engine_label

        if tr is not self.translator:
            return None                 # 这条翻译期间引擎已经换了：旧引擎的事不再报
        err = getattr(tr, "last_error", None)
        if not err or err[0] is None:
            return None
        status, said = err[0], err[1]
        name = getattr(tr, "name", None)
        label = engine_label(name)
        audit = getattr(self, "audit", None)
        mark = (tr, audit)
        remaining = getattr(tr, "cooldown_until", 0.0) - time.monotonic()
        if status == 429 and remaining > 0:
            if getattr(self, "_cooldown_mark", None) == mark:
                return None
            self._cooldown_mark = mark
            seconds = int(round(remaining))
            text = ("{} 返回 HTTP 429，程序暂停请求 {} 秒后自动重试；这期间字幕先显示原文，"
                    "违禁词报警不受影响").format(label, seconds)
            print("[警告] " + text)
            if audit is not None:
                audit.translation_cooldown(name, 429, seconds)
            await self.server.broadcast({"type": "notice", "text": text})
            return None
        if getattr(self, "_engine_error_mark", None) == mark:
            return None
        model = self._model_of(tr)
        if name in ("deepl", "claude", "openai") and status in BaseTranslator.REJECT_STATUSES:
            self._engine_error_mark = mark
            if audit is not None:
                audit.translation_engine_error(
                    name, status, model=None if name == "deepl" else model)
            what = "{} 返回 HTTP {}".format(label, status)
            if status == 404 and name != "deepl":
                what += "（模型 {}）".format(model)
            print("[警告] " + what)
            fallback = await self._rejection_fallback(tr, what, status)
            if fallback is None:
                self._engine_incident_up = True
                self._engine_rejection = {"what": what, "status": status}   # 停止时改写用
                await self._incident(
                    self.ENGINE_INCIDENT, "error",
                    "{}，字幕先显示原文（违禁词报警不受影响），程序每 {} 秒再试一次；"
                    "可以在「翻译引擎」里{}".format(
                        what, BaseTranslator.REJECT_COOLDOWN_SEC,
                        "换一个引擎" if status == 404 else "重新填写密钥，或换一个引擎"))
            return fallback
        if name in ("hymt2", "hymt2-7b", "gemma") \
                and getattr(tr, "fail_streak", 0) >= self.ENGINE_ERROR_STREAK:
            self._engine_error_mark = mark
            if audit is not None:
                audit.translation_engine_error(name, status, error=said, model=model)
            text = ("{} 连续 {} 次没有译文，Ollama 返回 HTTP {}{}。字幕先显示原文"
                    "（违禁词报警不受影响）；可以在「翻译引擎」里换一个引擎").format(
                        label, getattr(tr, "fail_streak", 0), status,
                        "：" + said if said else "")
            self._engine_incident_up = True
            self._engine_rejection = None
            await self._incident(self.ENGINE_INCIDENT, "warn", text)
            # 模型不在了的话 ensure_local_translator 会发现，安排停止后重新下载；
            # 自检那一行也会跟着变红（Ollama 在跑但没有这个模型）
            task = getattr(self, "_provision_task", None)
            if hasattr(self, "_bg_tasks") and (task is None or task.done()):
                self._provision_task = self._spawn(self._provision_then_check())
        return None

    async def _note_engine_ok(self, tr):
        """一条译文成功回来：之前报过的引擎问题解除（横幅撤掉），再出问题会重新说。
        缓存命中不算——引擎自己最近一次 HTTP 回应仍是失败（fail_streak>0）时不解除。"""
        if getattr(tr, "fail_streak", 0):
            return
        self._cooldown_mark = None
        self._engine_error_mark = None
        if getattr(self, "_engine_incident_up", False):
            self._engine_incident_up = False
            self._engine_rejection = None
            await self._incident(self.ENGINE_INCIDENT, "clear")

    async def _rejection_fallback(self, old, what, status):
        """远程引擎拒绝了密钥或模型：本机有本地模型就本场改用它，下拉框里的选择不动
        （和 _quota_fallback 同一个做法）。没有本地模型时返回 None，留在原引擎按冷却
        重试。**绝不**自动改发 Google：那等于不打招呼换了字幕的去处。"""
        from .translator import engine_label

        loop = asyncio.get_running_loop()
        try:
            new = await loop.run_in_executor(None, create_translator, "auto")
        except Exception as exc:
            print("[警告] 没有可换用的本地引擎: {}".format(exc))
            return None
        if new is None:
            return None
        if getattr(new, "name", None) not in ("hymt2", "gemma") or self.translator is not old:
            try:
                await new.close()
            except Exception:
                pass
            return None
        self.translator = new
        try:
            await old.close()
        except Exception:
            pass
        back = ("在「翻译引擎」里换好模型后重选 {} 即可回来。" if status == 404
                else "在「翻译引擎」里重新填写密钥后重选 {} 即可回来。").format(
                    engine_label(getattr(old, "name", None)))
        note = "{}，本场已改用{}继续翻译。{}".format(what, engine_label(new.name), back)
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
        from .translator import looks_fabricated

        job = self._recent.get(seq)
        if job is None:
            return
        # 拿本地引用：报警翻译失败时会把 self._strong 清掉，而这边可能还在等译文
        strong = await self._strong_translator()
        if strong is None:
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
        self._hold_strong(strong)        # 报警翻译清掉 self._strong 时，别关掉这边还在用的连接
        audit = self.audit               # 本场自己的审计：等译文期间可能已经换场
        had = self._quality.get(seq, 0) > 0
        await self.server.broadcast({"type": "caption_update", "id": seq,
                                     "strong_state": "pending"})
        t0 = time.monotonic()
        text, hint = self._for_translation(job["text"])
        try:
            out = await strong.translate(
                text, job["target"], source=job["lang"] or "auto",
                glossary=hint)
            if out and looks_fabricated(job["text"], out):
                # 强模型在「长句 + 带疑问」上会改成回话而不是翻译。
                # 宁可留着快译，也不能把凭空生成的内容写进屏幕和审计记录。
                print("[警告] 重译结果不像译文（疑似模型在回话），已丢弃")
                out = None
            elif out and self.glossary:
                out = self.glossary.apply(text, out)
        except Exception as exc:
            print("[警告] 重译失败: {}".format(exc))
            out = None
        ms = (time.monotonic() - t0) * 1000.0
        self._strong_inflight.discard(seq)
        self._release_strong(strong)
        if audit is not None:
            audit.translation_strong(seq, out, ms, bool(out),
                                     getattr(strong, "model", None), trigger)
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

    def _for_translation(self, text):
        """送给翻译器的那一份文本，以及配套的词表提示。

        与屏幕上的西语原文**不是同一份**：profile 开了 vocative_strip 的主播，
        称呼会在这里被摘掉（见 app/vocative.py，那是「爱邮递员」那一类错误的
        根治办法；开关按主播验证后才打开）。原文、审计日志、违禁词检测统统用
        未经改动的文本——报警跑在原文上，这条链路不能被翻译预处理碰。
        """
        cleaned = text
        if getattr(self, "_vocative_strip", False):
            from .vocative import strip

            cleaned, _hit = strip(text)
        hint = (tuple(self.glossary.translation_pairs(cleaned))
                if self.glossary else ())
        return cleaned, hint or None

    async def _translate_and_update(self, job):
        """翻译回来后原地更新那一条字幕（按 id）。失败只影响这一条。

        整个调用都算「字幕翻译在途」（self._subtitle_busy 计数）：观众弹幕的
        翻译 worker 靠这个数让路——字幕永远优先。主引擎与降级引擎两次
        translate() 之间的记录/审计开销一起算进去也无妨，它只是个「让不让路」
        的信号，不追求精确到毫秒。

        用 getattr 兜底：测试里常用 Pipeline.__new__(Pipeline) 绕过 __init__
        搭一个最小 Pipeline，不该因为少了这一个属性就崩掉。
        """
        self._subtitle_busy = getattr(self, "_subtitle_busy", 0) + 1
        try:
            await self._translate_and_update_inner(job)
        finally:
            self._subtitle_busy -= 1

    async def _translate_and_update_inner(self, job):
        """翻译回来后原地更新那一条字幕（按 id）。失败只影响这一条。"""
        t0 = time.monotonic()
        # 引擎抓一次快照：中途在界面里换引擎时，这一条从翻译到落日志必须
        # 始终指同一个对象，否则审计里的 engine 会记成换挡后的那个
        tr = self.translator
        # 词表两处生效：把本句命中的词条拼进提示词，译文回来再做兜底替换。
        # 商品名/自造词（Quema Lonja、moringa）通用模型必错，而且换多大的
        # 模型都不会自动变对——这类错误只能靠词表钉死。
        text, hint = self._for_translation(job["text"])
        try:
            translated = await tr.translate(
                text, job["target"], source=job["lang"] or "auto",
                glossary=hint)
            if self.glossary:
                translated = self.glossary.apply(text, translated)
        except Exception as exc:
            print("[警告] 翻译失败: {}".format(exc))
            translated = None
        # DeepL 月额度用尽（456）不是这一条的问题，是这个月的问题：不切换的话
        # 后面每条字幕都会「翻译失败」直到月底。切到本地引擎，并用新引擎把
        # 当前这条立刻补上——它不该成为切换的牺牲品。
        fallback = None
        if translated is None and getattr(tr, "quota_exhausted", False):
            fallback = await self._quota_fallback(tr)
        elif translated is None:
            # 说得清的失败（429 暂停、密钥被拒、Ollama 连续报错）告诉中控并记审计；
            # 远程引擎拒绝密钥、本机又有本地模型时，本场改用本地模型
            fallback = await self._note_engine_failure(tr)
        if fallback is not None:
            tr = fallback
            # translate_ms 只记翻译本身：引擎切换的开销不该算进这一条的
            # 翻译耗时去污染延迟统计（e2e_translated_ms 仍如实含全部等待）
            t0 = time.monotonic()
            try:
                translated = await tr.translate(
                    text, job["target"], source=job["lang"] or "auto",
                    glossary=hint)
                if self.glossary:
                    translated = self.glossary.apply(text, translated)
            except Exception as exc:
                print("[警告] 降级引擎翻译失败: {}".format(exc))
                translated = None
        if translated is None and hasattr(self, "_bg_tasks"):
            self._spawn(self._heal_local_engine())   # Ollama 掉了就拉起来（节流）
        elif translated is not None:
            await self._note_engine_ok(tr)        # 之前报过的引擎问题就此解除
        translate_ms = (time.monotonic() - t0) * 1000.0
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
