"""G10 的场景（spec §12.3）：在 i18n.use(lang) 下用真实函数组装页面会收到的消息。

界面上会出现的后端文字一律由生产代码生成：browser_only_message、share_note（经
ViewerHub.state）、自检行与自检提示条（_publish_selfcheck）、_announce_health 三档、
_check_clock_gap、_translate_alert 的 why、engine_label / restore_engine、update_check_note、
diskspace.inventory；闪购助手的状态（config.flash_sale）也走 flash_sale.StatusTracker。这里不手写任何一句
要上界面的中文或英文——迁移到哪一句，扫描就跟到哪一句。

数据字段（字幕原文、弹幕、词条、主播名）用西语或 ASCII。DATA_ZH 是故意放进去的中文数据
（一条译成简体中文的字幕、一个中文品牌名、一条报警上下文的中文译文），它们必须落在数据区里：
品牌名是名字，在 translate="no" 里；两条译文是正文，在 class i18n-data 里（用户决定 6：正文
留给浏览器翻译，不标 translate="no"）。英文页上它们不许被报出来，中文对照页上也不许——
用来证明两种豁免确实生效。
品牌是虚构的（主播和品牌词表属于本机数据，不进仓库），名字故意取长：顶栏品牌标签的自然宽度
要顶到 160px 的上限，量出来的宽度才反映「被挤了多少」（版面断言的下限是 100 / 120px）。

消息顺序照真实连接：桌面照 app/server.py 的 _ws（hello 整份 config → 报警回放 → 字幕回放 →
弹幕回放），再接不进缓存的实时消息；手机照 app/viewer.py 的 replay_snapshot，再接逐条
filter_payload 的实时消息。

生产方法（Pipeline._announce_health 这类）在一个替身 Pipeline 上跑，它要广播的消息先由
_Relay 记下，再由本文件调真的 CaptionServer.broadcast 进缓存。这一步的调用方在 tests/ 下，
G9 运行时网按夹具跳过，场景不会被算进「漏写 L()」的清单。
"""
import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import browser_login, diskspace, flash_sale, i18n, qr, selfcheck, viewer
from app.pipeline import LIVE_ENDED_NOTE, Pipeline, browser_only_message
from app.server import CaptionServer, replay_payloads
from app.telemetry import Telemetry
from app.translator import engine_label, mask_key, restore_engine
from app.updater import RELEASES_URL, update_check_note

FAKE_TOKEN = "fake-token-not-a-real-secret-0000000000"   # viewer.js 只认片段里的 #k=
# 主播名是虚构的，但和 docs/i18n-style.md §4、web/style.css 里量版面用的那个 15 个字母的真实主播名同宽：
# 同一组字形重新排列，SF 字体 11–20px、400–700 字重、中英两种页面下逐 1/64 像素量过，顶栏的余量数字不变
STREAMER = "beltranlulaalal"
RECENT = (STREAMER, "lamejorcrema", "tiendadeana", "glowbymaria")
BRAND_ID = "acme"
BRAND_ZH = "艾可美天然护肤旗舰店"              # 中文品牌名（数据，虚构）
CAPTION_ZH = "这款面霜一周就能淡化色斑"        # target_lang=zh-CN 的一条译文（数据）
CONTEXT_ZH = "这款面霜能治好色斑"              # 报警上下文的中文译文（数据）
DATA_ZH = (BRAND_ZH, CAPTION_ZH, CONTEXT_ZH)

NOW = datetime(2026, 9, 28, 20, 0, 0).timestamp()   # 固定时刻：日志文件的新旧、时钟间隙都按它算
# 弹幕留意的页面用浏览器的真实时钟判断「新事件」（hello 里回放的不算新）：实时消息的时刻放到很远的将来，
# 顶部条才会在场景里弹出来
FUTURE = NOW + 10 * 365 * 86400
DESKTOP = (1280, 860)
PHONE = (390, 844)
FLOAT = (460, 330)                             # 浮窗的默认大小（app/float_window.py DEFAULT_SIZE）
LIVE_WIDTHS = (1000, 1100, 1101, 1199, 1200)   # 版面断言的五档（spec §12.3 第 7 条）
NARROW_WIDTHS = (860, 720)                     # 这两档只断言 .topbar 不溢出
# 「字幕译成」下拉最宽的样子：当前目标是四种以外、名字最长的 Bahasa Indonesia，下拉补了一项并选中它（约 144px；
# 只有四种时约 116px）。2026-10-20 以前 13 种一直都在下拉里，直播中的 G10 量的都是 144px，
# docs/i18n-style.md §4.1/§4.2 的余量也按 144 算；只列四种以后 live-* 场景的目标是 en，量到的只有 116px。
# settings.json 里存着 id / vi / pt、或者 --target id 启动的中控，直播中看到的是 144px 的那个顶栏：
# 直播中量顶栏的场景都另有一份名字带 EXTRA 后缀的（with_extra_target），把这 28px 量回来
EXTRA_TARGET = "id"
EXTRA = "-extra"
# 首页顶栏的「字幕译成」标签（web/style.css body.home .ctl-text）：直播中 ≤1080px 省掉，首页一直露出来。
# 量英文首页（home-idle）和中文首页（home-zh，当前目标是 EXTRA_TARGET，下拉补了一项、是它最宽的样子）。
# 1000 是默认窗口，720 是直播中量过的最窄一档
HOME_WIDTHS = (1000, 860, 720)
HOME_ZH_TARGET = EXTRA_TARGET
# 下拉里补出来的那一项（web/app.js showTargetLang 标 data-extra）而且是选中的：量得到（不是 null）就说明
# 浏览器里真的补了这一项、显示的就是它，而不是静默落到第一项「中文（简体）」
TARGET_EXTRA_SELECTED = "#target-lang option[data-extra]:checked"
# 中文页「浮窗」两个字（web/style.css 的 html[lang^="zh"] 那一段，docs/i18n-style.md §4.2）的六档：
# 1000 / 1100 是露字范围的两头，1101 是「手机同看」露出文字之后仍是只有图标的第一档，1140 是第二段露字的起点，
# 1399 是它的另一头，1400 起图标加文字。1101–1139 这一段样式不变（同一组媒体查询），窗口越宽余量越多，
# 最紧的是 1101，所以 1117、1139 不另量。英文页「手机同看」到 1200 才露字，1101–1199 宽松（§4.4：品牌标签约 148–160px）
ZH_FLOAT_WIDTHS = (1000, 1100, 1101, 1140, 1399, 1400)
# 直播中量顶栏的基础场景（各自再按宽度出「名字@宽度」）：目标在四种之内的，和同一个场景目标换成 EXTRA_TARGET 的
LIVE_BASES = ("live-full", "live-float", "live-flash",
              "live-full" + EXTRA, "live-float" + EXTRA, "live-flash" + EXTRA)          # LIVE_WIDTHS + NARROW_WIDTHS
ZH_FULL_BASES = ("live-float-zh", "live-flash-zh", "live-float-zh" + EXTRA, "live-flash-zh" + EXTRA)   # 只量 1000
ZH_QUIET_BASES = ("live-float-zh-quiet", "live-flash-zh-quiet",
                  "live-float-zh-quiet" + EXTRA, "live-flash-zh-quiet" + EXTRA)           # ZH_FLOAT_WIDTHS
# 顶栏「闪购」按钮（装了闪购助手才有，首页和直播中都在；docs/i18n-style.md §4.2）：live-flash 是 live-float 再加上
# config.flash_sale，是窗口模式里最满的顶栏；1400px 起「Aa」不出现、字号控件回到顶栏，再量两档 FLASH_WIDE_WIDTHS
# （英文页 1400px 仍只有图标，1600px 起露出 Flash Sale）。首页的 home-flash（英文）/ home-flash-zh（中文）按 HOME_WIDTHS 量
FLASH_BASES = ("live-flash", "live-flash" + EXTRA)
FLASH_WIDE = 1400
FLASH_WIDE_WIDTHS = (FLASH_WIDE, 1600)
HOME_BASES = ("home-idle", "home-zh", "home-flash", "home-flash-zh")
# §4.1 中文列的基线品牌（标签约 143px）；BRAND_ZH 的标签顶到 160px 上限。名字是虚构的，和文档里那个真实的基线品牌同宽
# （同 STREAMER 的做法：同一组字形重新排列，逐 1/64 像素量过）
ZH_QUIET_BRAND = "Allura Belt Nalla"
DESKTOP_MEASURE = (".topbar", "#active-brand-tag", ".status-text", "#switch-confirm",
                   "#float-btn", "#float-btn .btn-label", "#float-btn .i", ".ctl-text", "#target-lang",
                   TARGET_EXTRA_SELECTED, "#flash-btn", "#flash-btn .btn-label", "#flash-btn .i", "#font-btn", "#font-ctl")
GUARD_MEASURE = (".guard-bar", ".guard-bar-text", ".guard-drawer", ".guard-panes", ".guard-pane:not(.hidden)", ".comment-list",
                 ".guard-state")
PHONE_MEASURE = (".status-pill", "#conn-text")
FLOAT_MEASURE = (".f-root", "#f-caption", "#f-comments")

# 弹幕留意（Comment Review）的四个场景，英文页进 G10 的扫描，中文页（-zh）只为核对中文界面和出截图。
# 设置行展开、直播中抽屉的「最近需要留意」页签、抽屉的「关注名单」页签、熔断后的胶囊与「本场重新启用」
GUARD_NAMES = ("home-guard", "live-guard", "live-guard-marked", "live-guard-tripped")
GUARD_ZH = tuple(n + "-zh" for n in GUARD_NAMES)
DESKTOP_EN = ("home-idle", "home-selfcheck-fail", "live-full", "live-float", "connecting", "offline",
              "error-4003110", "live-bad", "reconnect-no-audio", "live-flash", "home-flash") + GUARD_NAMES
PHONE_EN = ("phone-live", "phone-denied", "phone-full", "phone-local-en")
FLOAT_EN = ("float-live",)           # 浮窗页（web/float.html）：直播中，有红 / 琥珀 / 关注的弹幕，最新一条字幕没译成
CONTROL_ZH = "live-full-zh"


def room_url(streamer):
    return "https://www.tiktok.com/@{}/live".format(streamer)


# ---- 让生产方法跑起来 --------------------------------------------------------------------

class _Relay:
    """生产方法眼里的 server：config 就是真 CaptionServer 的那一份，broadcast 只记下来。"""

    def __init__(self, server):
        self.config = server.config
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(msg)


def _feed(server, msgs):
    """交给真的 CaptionServer.broadcast：字幕进 history、报警进 alerts、提示条进 config……"""
    async def go():
        for msg in msgs:
            await server.broadcast(msg)
    asyncio.run(go())


def _produce(server, steps, **attrs):
    """在替身 Pipeline 上跑 steps(p)（一个协程函数），它广播的消息进 server，并原样交回。
    替身同 tests/test_selfcheck_incident.py 的 _bare_pipeline：只有用得到的属性。"""
    relay = _Relay(server)
    p = Pipeline.__new__(Pipeline)
    p.server, p.audit, p.telemetry, p.translator = relay, None, Telemetry(), None
    for key, value in attrs.items():
        setattr(p, key, value)
    asyncio.run(steps(p))
    _feed(server, relay.sent)
    return relay.sent


# ---- 后端文字（全部来自生产代码） ---------------------------------------------------------

def _checks(fail, free_gb):
    """自检各行。名字查 NAMES；词表、领域词表、浏览器登录态、磁盘四行直接调真实的检查函数，
    其余几行的 detail 是版本号、模型名这类 ASCII 数据。"""
    names, ok = selfcheck.NAMES, selfcheck.OK
    detector = None if fail else SimpleNamespace(
        enabled=True, count=53, effective_count=53, load_warnings=[], source_path="banned_terms.txt")
    with mock.patch.object(selfcheck.shutil, "disk_usage",
                           return_value=SimpleNamespace(free=free_gb * 1024 ** 3)):
        disk = asyncio.run(selfcheck.check_disk())
    return [selfcheck._check(names["ffmpeg"], ok, "ffmpeg 7.1"),
            selfcheck._check(names["denoise"], ok, "rnnoise"),
            selfcheck._check(names["asr"], ok, "mlx-whisper large-v3-turbo"),
            selfcheck._check(names["translator"], ok, engine_label("hymt2")),
            asyncio.run(selfcheck.check_watchlist(detector)),
            asyncio.run(selfcheck.check_glossary(None)),
            selfcheck._browser_login_row({}),
            selfcheck._check(names["comments"], ok, "TikTokLive 7.0.2"),
            disk]


def _engine(note=None):
    """形状同 Pipeline._publish_engine。"""
    return {"engine": "auto", "usage": None, "active": "hymt2",
            "active_label": engine_label("hymt2"), "note": note,
            "keys": {env: mask_key("") for env in Pipeline.ENGINE_KEY_ENV.values()}}


def _inventory(tmp):
    """真的盘点：临时目录里造一个模型目录、一新一旧两份审计日志，外加一个 Ollama 模型。"""
    hub = tmp / "hub" / "models--mlx-community--whisper-large-v3-turbo"
    hub.mkdir(parents=True, exist_ok=True)
    (hub / "weights.bin").write_bytes(b"\0" * 2048)
    logs = tmp / "logs"
    logs.mkdir(exist_ok=True)
    for stem in ("session-20260801-100000", "session-20260927-100000"):
        (logs / (stem + ".jsonl")).write_text("{}\n", encoding="utf-8")
    # 识别音频片段（app/asr_evidence.py）：一场的证据目录，英文页上那一行也要扫到
    clips = logs / "asr-evidence" / "session-20260927-100000"
    clips.mkdir(parents=True, exist_ok=True)
    (clips / "seq-000050.wav").write_bytes(b"\0" * 4096)
    (clips / "index.jsonl").write_text("{}\n", encoding="utf-8")
    ollama = [{"name": "hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M", "size": 4509715660}]
    return diskspace.inventory(hf_dir=tmp / "hub", ollama_models=ollama, log_dir=logs,
                               active_asr=("mlx", "large-v3-turbo"),
                               now=datetime.fromtimestamp(NOW))


def _share_on(viewers):
    """同看打开、有人在看时 ViewerHub.state() 的载荷：note 是真的 share_note。
    hub 不真的监听端口，只把「在跑」和人数设进去。"""
    hub = viewer.ViewerHub(None, ports=[8766, 8767], token=FAKE_TOKEN)
    hub._running = True
    hub._viewers = [object()] * viewers
    ip = "192.168.1.23"
    url = hub.url(ip)
    return hub.state(ip_info={"ip": ip, "all": [ip, "10.0.0.8"], "ambiguous": True},
                     qr_rows=qr.rows(qr.encode(url.encode("utf-8"))),
                     ip_changed=("192.168.1.20", ip))


def _health(server, dropped):
    """_announce_health 三档：先落后、再追上、最后降级（界面上留下的是降级那一句）。"""
    tel = Telemetry()
    tel.audio_segments_dropped = dropped

    async def steps(p):
        await p._announce_health("lagging", 14.0)
        await p._announce_health("ok", 0.0)
        await p._announce_health("degraded", 75.0)
    return _produce(server, steps, telemetry=tel)


def _clock_gap(server):
    """_check_clock_gap：墙钟走了 30 分钟、单调时钟没走（合盖休眠）。"""
    audit = SimpleNamespace(clock_gap=lambda *a, **k: None)
    sess = {"audit": audit, "clock": (NOW - 1805, 100.0)}

    async def steps(p):
        await p._check_clock_gap(5, now=(NOW, 105.0))
    return _produce(server, steps, audit=audit, _session_state=sess)


def _alert_whys(server, context):
    """_translate_alert 两条「先不翻译」的真实 why：识别积压、强模型被另一条报警占着。
    交回这两条 alert_update：它们也要作为实时消息再发一次——桌面和手机画回放的报警时
    都不看 failed / why（只画「翻译中」），why 只有经 alert_update 才上界面。"""
    backlog = Telemetry()
    backlog.set_backlog(40.0)
    return (_produce(server, lambda p: p._translate_alert([2], context, "es"), telemetry=backlog)
            + _produce(server, lambda p: p._translate_alert([3], context, "es"),
                       _alert_strong_busy=True))


def _stats():
    """服务端照常每 10 秒推一次的统计（真 Telemetry 的 snapshot，各项计数都不为零）。页面 2026-10-15 起
    不再画延迟统计行，留着它是为了确认页面收到这条消息也不出漏翻的中文、不报错。"""
    tel = Telemetry()
    for asr_ms, e2e_ms, seg_ms in ((1100, 2300, 1800), (1300, 2700, 2100), (900, 2000, 1600)):
        tel.record_asr(asr_ms, e2e_ms, seg_ms)
        tel.record_translation(800)
        tel.record_caption_wait(seg_ms + e2e_ms + 800)
    tel.set_backlog(12.0)
    tel.audio_segments_dropped, tel.asr_overruns, tel.translation_jobs_dropped = 3, 1, 2
    tel.asr_queue_depth, tel.translation_queue_depth = 2, 5
    return dict(tel.snapshot(), type="stats")


# ---- 数据（西语 / ASCII，外加 DATA_ZH） ---------------------------------------------------

def _caption(cid, original, src="es", target="en", ts=None):
    """形状同 Pipeline 的 caption 广播：先是「翻译中」，译文由 caption_update 补上。"""
    ts = NOW - 300 + cid * 20 if ts is None else ts
    return {"type": "caption", "id": cid, "ts": ts, "original": original,
            "translated": None, "translate_state": "pending" if src != target else "skipped",
            "src_lang": src, "target_lang": target, "asr_ms": 1100, "e2e_ms": 2300}


def _caption_update(cid, translated, state="ok", target="en", **extra):
    return dict({"type": "caption_update", "id": cid, "translated": translated,
                 "translate_state": state, "target_lang": target, "translate_ms": 800,
                 "quality": 1 if translated else 0}, **extra)


def _captions():
    """上一场留下的一条字幕，再是场次分隔线和这一场各种状态的字幕。桌面只在前面已经有字幕
    卡片时才画分隔线（web/session-divider.js），所以上一场那条不能省。"""
    return [
        _caption(1, "Gracias a todas, nos vemos el lunes", ts=NOW - 3600),
        _caption_update(1, "Thanks everyone, see you on Monday"),
        {"type": "session_break", "ts": NOW - 320, "streamer": STREAMER},
        _caption(2, "Hola chicas, bienvenidas al live de hoy"),
        _caption_update(2, "Hi girls, welcome to today's live", strong=True, strong_state="ok"),
        _caption(3, "Esta crema aclara las manchas en una semana", target="zh-CN"),
        _caption_update(3, CAPTION_ZH, target="zh-CN"),
        _caption(4, "¿Ya vieron el nuevo sérum?"),
        _caption_update(4, "Have you seen the new serum?", strong_state="pending"),
        _caption(5, "Tenemos envío gratis hoy"),
        _caption_update(5, None, state="failed"),
        _caption(6, "Nos vemos mañana a la misma hora"),
        _caption_update(6, None, state="dropped"),
        _caption(7, "Thank you so much for watching", src="en"),
        _caption(8, "Les dejo el enlace abajo"),
        _caption_update(8, "I'll leave you the link below", strong_state="failed"),
        # 「语音识别不确定」角标（默认关；开着时服务端才带 asr_flag）：英文页上卡片里的那句也要扫到
        dict(_caption(10, "Y ya"), asr_flag=["few_words"]),
        _caption_update(10, "And that's it"),
        _caption(9, "Ahorita les enseño el precio"),   # 停在「翻译中」
    ]


def _comments():
    def comment(cid, user, text):
        return {"type": "comment", "id": cid, "user": user, "text": text, "ts": NOW - 200,
                "translated": None, "state": "pending"}
    return [
        comment("c1", "maria_g", "¿Cuánto cuesta la crema?"),
        {"type": "comment_update", "id": "c1", "translated": "How much is the cream?", "state": "ok"},
        comment("c2", "lupita88", "Ya compré dos"),
        {"type": "comment_update", "id": "c2", "translated": None, "state": "failed"},
        comment("c3", "ana.rz", "¿Envían a Monterrey?"),
        {"type": "comment_source", "backend": "connected", "detail": ""},
    ]


def _alerts():
    """三档报警。形状同 Pipeline：detector 的命中 + _alert_stamp + _count_alert。"""
    def alert(aid, tier, term, context):
        return {"type": "alert", "alert_id": aid, "term": term, "tier": tier, "matched": term,
                "ts": NOW - 90 + aid * 10, "context": context, "streamer": STREAMER,
                "session": "s1", "ui_clients": 1, "session_total": aid}
    return [
        alert(1, "exact", "cura", "esta crema cura las manchas en una semana"),
        {"type": "alert_update", "alert_id": 1, "context_zh": CONTEXT_ZH, "failed": False, "why": ""},
        alert(2, "variant", "curan", "estas vitaminas curan todo"),
        alert(3, "fuzzy", "kura", "la crema kura rápido"),
    ]


# ---- 组装 --------------------------------------------------------------------------------

def _server(lang, tmp, *, selfcheck_fail=False, free_gb=120):
    """待机时的真 CaptionServer：Pipeline.__init__ 与 main.py 放进 config 的那些键。"""
    server = CaptionServer(port=8765)
    server.config.update({
        "version": "9.9.9", "target_lang": "en", "source_lang": "es",
        "room_url": room_url(STREAMER), "alerts_enabled": False,
        "recent_rooms": [{"streamer": s, "url": room_url(s), "at": ""} for s in RECENT],
        "brands": {STREAMER: BRAND_ID},
        "brand_options": [{"id": BRAND_ID, "name": BRAND_ZH}, {"id": "xyz", "name": "Xyz Beauty"}],
        "active_brand": None, "watchlist": {"count": 53, "glossary": 0},
        "engine": _engine(), "disk": {"items": _inventory(tmp), "free": 128 * 1024 ** 3},
        "viewer": viewer.off_state(port=8766, ports=[8766, 8767]),
    })
    server.config.update(i18n.config_info())
    # 场景是一台没带 --ui-lang、系统语言就是界面语言的机器。i18n.use() 按一次性覆盖实现，
    # config_info 会说「被启动参数锁定」「系统语言检测不到」——那是开发和截图时的另一种状态
    server.config.update(ui_lang_setting="system", ui_lang_system=lang, ui_lang_locked=False)
    _produce(server, lambda p: p._publish_selfcheck(_checks(selfcheck_fail, free_gb)))
    return server


def _desktop_messages(server, lang, live=()):
    """照 CaptionServer._ws 的顺序，再接实时消息；按 lang 渲染，冻成普通 JSON 数据。"""
    out = [{"type": "hello", "config": server.config}]
    out += [dict(alert, replay=True) for alert in server.alerts]
    out += replay_payloads(server.history)
    out += [dict(comment, replay=True) for comment in server.comments]
    out += list(live)
    return [i18n.render(msg, lang) for msg in out]


def _phone_messages(server, live=()):
    """照 ViewerHub 的入册：replay_snapshot，再接逐条 filter_payload。要在 i18n.use 里调，
    *_en 派生字段只在 enabled() 时才有。"""
    out = viewer.replay_snapshot(server, viewers=2, share_since=NOW - 900)
    out += [p for p in (viewer.filter_payload(m) for m in live) if p is not None]
    return out


def _plain(data):
    """Bi 与元组冻成普通 JSON 数据：之后改 server 也不会影响已经组好的场景。"""
    return json.loads(json.dumps(data, ensure_ascii=False))


def _scenario(name, page, lang, messages, *, page_lang=None, replies=None, close=None,
              clicks=(), storage=None):
    """一个场景（spec §12.3 第 2 条的形状）。另有两个键：page_lang 是服务器往 <html> 里注入的
    语言（手机本机选了英文、服务端给中文页时两者不同），storage 是开页前预置的 localStorage。"""
    size = {"desktop": DESKTOP, "phone": PHONE, "float": FLOAT}[page]
    measure = {"desktop": DESKTOP_MEASURE, "phone": PHONE_MEASURE, "float": FLOAT_MEASURE}[page]
    return {"name": name, "page": page, "size": list(size),
            "lang": lang, "page_lang": page_lang or lang, "messages": _plain(messages),
            "replies": _plain(replies or {}), "close": close, "clicks": list(clicks),
            "measure": list(measure),
            "storage": dict(storage or {})}


def _disk_reply(server, lang):
    return {"disk_inventory": i18n.render(dict(server.config["disk"], type="disk"), lang)}


def _home_idle(tmp):
    with i18n.use("en"):
        server = _server("en", tmp)
        return _scenario("home-idle", "desktop", "en", _desktop_messages(server, "en"),
                         replies=_disk_reply(server, "en"),
                         clicks=["#sc-head", "#engine-head", "#watch-head", "#disk-head",
                                 "#lang-head", "#input-help-btn"])


def _home_zh(tmp):
    """中文首页，待机。只为量顶栏（「字幕译成」标签露着、下拉补了一项 Bahasa Indonesia），不扫漏翻。"""
    with i18n.use("zh"):
        server = _server("zh", tmp)
        server.config["target_lang"] = HOME_ZH_TARGET
        return _scenario("home-zh", "desktop", "zh", _desktop_messages(server, "zh"))


def _home_selfcheck_fail(tmp):
    """fail + warn（自检失败时整块自动展开，不再点）+ 自检提示条 + 引擎回退 + 有更新 +
    连不上更新服务器 + 磁盘展开、勾选日志项、点删除（确认框被替身记下）。"""
    with i18n.use("en"):
        server = _server("en", tmp, selfcheck_fail=True, free_gb=4)
        _, note = restore_engine(None, "deepl", key_lookup=lambda env: "")
        stale = update_check_note({"update_check_error": {
            "since": NOW - 20 * 86400, "at": NOW, "status_or_exc": "HTTP 503"}})
        server.config.update(engine=_engine(note), update_check={"note": stale},
                             update={"version": "v9.9.10", "notes": "", "url": RELEASES_URL,
                                     "can_auto": True})
        return _scenario("home-selfcheck-fail", "desktop", "en", _desktop_messages(server, "en"),
                         replies=_disk_reply(server, "en"),
                         clicks=["#engine-head", "#watch-head", "#disk-head",
                                 '#disk-list input[value="logs:old"]', "#disk-delete"])


def _live_server(lang, tmp):
    """直播中：主播 + 品牌 + 报警开 + 2 人在看 + 各种状态的字幕 + 场次分隔线 + 弹幕 +
    三档报警带 why + 时钟间隙提示条。返回 (server, 不进缓存的实时消息)。"""
    server = _server(lang, tmp)
    server.config.update(alerts_enabled=True, active_brand={"id": BRAND_ID, "name": BRAND_ZH},
                         alerts_session={"session": "s1", "streamer": STREAMER, "total": 3},
                         viewer=_share_on(2))
    _feed(server, [{"type": "status", "state": "live", "detail": ""}])
    _feed(server, _captions() + _comments() + _alerts())
    whys = _alert_whys(server, "estas vitaminas curan todo")
    _clock_gap(server)
    live = whys + _health(server, dropped=3) + [_stats()]
    return server, live


def _bad_session(mono_now, *, audio_until=None):
    """拉流断断续续的那一场的观察状态（app/stream_watch.py）：开播 5 分钟，只有头一段有声，断了 7 次。"""
    from app.stream_watch import StreamWatch

    watch = StreamWatch(60.0, mono=lambda: mono_now, wall=lambda: NOW)
    watch.first_spawn = (mono_now - 300.0, NOW - 300.0)
    if audio_until is not None:
        t = mono_now - 300.0
        while t < audio_until:
            watch.note_audio(1.0, t, NOW - (mono_now - t))
            t += 1.0
        watch.cov_start = mono_now - 300.0
    return {"watch": watch, "audit": None, "break_no": 7}


def _coverage_notice(server):
    """_check_coverage：近 5 分钟只收到三成，连续两次低于报警线 → session:audio_rate。"""
    sess = _bad_session(2000.0, audio_until=1790.0)

    async def steps(p):
        await p._check_coverage(sess, 1995.0)
        await p._check_coverage(sess, 2000.0)
    return _produce(server, steps)


def _live_bad(tmp):
    """断断续续的一场：直播中、有字幕，页面顶部是时钟跳变和覆盖率两条持续提示，底部大字幕的元信息行
    挂着覆盖率角标（stats 里的数字，字在 web/live-ui.js）。"""
    with i18n.use("en"):
        server = _server("en", tmp)
        _feed(server, [{"type": "status", "state": "live", "detail": ""}])
        _feed(server, _captions())
        _clock_gap(server)
        _coverage_notice(server)
        stats = dict(_stats(), coverage_pct=31, coverage_window_min=5, coverage_low=True, breaks=7)
        live = [_caption(30, "Les dejo el enlace abajo", ts=NOW - 5), _caption_update(30, None, state="failed"),
                stats]
        return _scenario("live-bad", "desktop", "en", _desktop_messages(server, "en", live))


def _reconnect_no_audio(tmp):
    """重连中：V2 查询次数到了上限、改用原来的流地址那一句状态（_reconnect_text），加上
    「已 N 分钟没收到直播音频」（_check_no_audio）。"""
    from app.reconnect_policy import Plan, V2Policy

    with i18n.use("en"):
        server = _server("en", tmp)
        sess = _bad_session(2000.0)
        sess["policy"] = V2Policy(False, clock=lambda: 2000.0)
        holder = {}

        async def steps(p):
            await p._check_no_audio(sess, 2000.0, NOW)
            holder["text"] = p._reconnect_text(sess, sess["policy"],
                                               Plan(8, "reuse_failed_capped", next_lookup_sec=140.0))
        _produce(server, steps)
        _feed(server, [{"type": "status", "state": "connecting", "detail": holder["text"]}])
        return _scenario("reconnect-no-audio", "desktop", "en", _desktop_messages(server, "en"))


def _live_full(tmp, lang):
    with i18n.use(lang):
        server, live = _live_server(lang, tmp)
        name = "live-full" if lang == "en" else CONTROL_ZH
        return _scenario(name, "desktop", lang, _desktop_messages(server, lang, live),
                         clicks=["#share-btn", "#share-rotate", "#switch-btn",
                                 "#switch-recent-list .recent-chip:not([disabled])"])


def _status_only(tmp, name, state, detail="", close=None):
    with i18n.use("en"):
        server = _server("en", tmp)
        _feed(server, [{"type": "status", "state": state, "detail": detail}])
        return _scenario(name, "desktop", "en", _desktop_messages(server, "en"), close=close)


# ---- 弹幕留意（Comment Review） -----------------------------------------------------------
# 后端只发代码（类别、琥珀理由、模型意见、状态），界面文字由 web/guard.js 按代码写，所以这里没有任何
# 要上界面的中文或英文，只有数据：昵称、账号（19 位，超过 2^53）、西语弹幕。形状同 app/pipeline_guard.py。

GUARD_UID = "7234567890123456789"
GUARD_STATE = {"enabled": True, "backend": "none", "status": "rules", "since": NOW, "trip": None,
               "rules": {"count": 510, "warnings": 2}, "n_marked": 3, "unreviewed": 0, "weak": False,
               "streamer": STREAMER}


def _guard_comments():
    """弹幕栏里的几种行：普通、红条（带模型给的证据）、琥珀（各种理由）、关注的人、多账号同句
    （后者是旧数据的形状：后端 2026-10-14 起不再发，页面还要认得它，所以场景留着）。"""
    def comment(cid, user, text, uid, handle, guard=None, offset=0):
        msg = {"type": "comment", "id": cid, "user": user, "text": text, "ts": NOW - 200 + offset,
               "translated": None, "state": "pending", "uid": uid, "uid_ok": True, "handle": handle}
        if guard:
            msg["guard"] = guard
        return msg

    def red(cat, ev, why, **extra):
        return dict({"lvl": "alert", "amb": None, "cats": [cat], "sub": cat, "tier": "red", "src": "rule",
                     "rv": None, "ev": ev, "why": why}, **extra)

    def amber(cat, amb, ev, **extra):
        return dict({"lvl": "amber", "amb": amb, "cats": [cat], "sub": "assert", "tier": "red", "src": "rule",
                     "rv": None, "ev": ev, "why": []}, **extra)

    return [
        comment("g1", "maria_g", "¿Cuánto cuesta el sérum?", "7100000000000000011", "maria_g"),
        {"type": "comment_update", "id": "g1", "translated": "How much is the serum?", "state": "ok"},
        comment("g2", "rival_demo", "no compren, es una estafa, denuncien este live", GUARD_UID, "rival_demo",
                red("accuse_call", "g-ev1", ["accuse/call:no compren"], evidence="no compren", rv="agree"), 20),
        {"type": "comment_update", "id": "g2", "translated": "Don't buy, it's a scam, report this live", "state": "ok"},
        comment("g3", "lupita88", "es falso", "7100000000000000013", "lupita88",
                amber("accuse_claim", "maybe_question", "g-ev2"), 40),
        {"type": "comment_update", "id": "g3", "translated": "It's fake", "state": "ok"},
        comment("g4", "pedro_demo", "mira https://ejemplo.example.com/pedro", "7100000000000000014", "pedro_demo",
                red("promo_link", "g-ev3", ["promo/link"]), 60),
        comment("g5", "sofia_ventas", "me llegó roto y nadie contesta", "7100000000000000015", "sofia_ventas",
                amber("accuse_claim", "grievance", "g-ev4", rv="neutral"), 80),
        comment("g6", "rival_demo", "ya les dije lo que pienso", GUARD_UID, "rival_demo", None, 100),
        {"type": "comment_update", "id": "g6", "translated": "I already told you what I think", "state": "ok"},
        comment("g7", "cuenta_e", "quiero uno ya mismo luna", "7100000000000000025", "cuenta_e",
                amber("flood_brigade", "brigade", "g-ev5"), 120),
    ]


def _guard_events():
    """聚合后的事件（config.guard_events 里的样子）：红条、琥珀的几种理由、多账号同句。"""
    def ev(eid, cat, lvl, ts, sample_cid, user, text, uid, **extra):
        return dict({"type": "guard_event", "ev": dict({
            "id": eid, "kind": "comment", "cat": cat, "lvl": lvl, "amb": None, "n": 1, "accounts": [],
            "window_sec": 60, "first_ts": ts, "ts": ts, "rv": None,
            "sample": {"cid": sample_cid, "user": user, "text": text, "uid": uid, "uid_src": "id", "handle": user}},
            **extra)})
    accounts = [{"user": "cuenta_" + c, "uid": "71000000000000000" + str(i)} for i, c in enumerate("abcde")]
    return [
        ev("g-ev1", "accuse_call", "alert", NOW - 180, "g2", "rival_demo", "no compren, es una estafa, denuncien este live",
           GUARD_UID, rv="agree"),
        ev("g-ev2", "accuse_claim", "amber", NOW - 160, "g3", "lupita88", "es falso", "7100000000000000013",
           amb="maybe_question"),
        ev("g-ev3", "promo_link", "alert", NOW - 140, "g4", "pedro_demo", "mira https://ejemplo.example.com/pedro",
           "7100000000000000014"),
        ev("g-ev4", "accuse_claim", "amber", NOW - 120, "g5", "sofia_ventas", "me llegó roto y nadie contesta",
           "7100000000000000015", amb="grievance", rv="neutral"),
        ev("g-ev5", "flood_brigade", "amber", NOW - 100, "g7", "cuenta_e", "quiero uno ya mismo luna",
           "7100000000000000025", kind="brigade", amb="brigade", n=5, window_sec=20, accounts=accounts),
    ]


def _guard_marked():
    """关注名单：一个自动加入的、一个手动加入的、一个只有 handle 的。"""
    def entry(keys, nick, handle, by, cat, seen):
        return {"type": "guard_mark", "key": keys[0], "entry": {
            "keys": keys, "nick": nick, "handle": handle, "aka": [], "by": by, "cat": cat,
            "first": NOW - 86400, "last_seen": seen, "hits": 3}}
    return [
        entry([GUARD_UID, "s:MS4wLjAB", "h:rival_demo"], "rival_demo", "rival_demo", "auto", "accuse_call", NOW - 90),
        entry(["7100000000000000013"], "lupita88", "lupita88", "manual", None, NOW - 3 * 86400),
        entry(["h:tienda_rival"], "Tienda Rival", "tienda_rival", "manual", None, NOW - 7200),
    ]


def _guard_scenario(tmp, lang, name, *, live, state=None, clicks=()):
    """live=False：待机的首页（设置行展开）；live=True：直播中，弹幕栏有几种标记、顶部条弹出。
    guard_* 的缓存经真的 CaptionServer.broadcast 进 config 和弹幕缓存，同真实连接的 hello 一致。"""
    with i18n.use(lang):
        server = _server(lang, tmp)
        server.config["guard_state"] = dict(GUARD_STATE, **(state or {}))
        if not live:
            _feed(server, _guard_marked())
            sc_name = name + ("" if lang == "en" else "-zh")
            return _scenario(sc_name, "desktop", lang, _desktop_messages(server, lang), clicks=clicks,
                             replies=_disk_reply(server, lang))
        server.config.update(alerts_enabled=True, alerts_session={"session": "s1", "streamer": STREAMER, "total": 0})
        _feed(server, [{"type": "status", "state": "live", "detail": ""}])
        _feed(server, _captions() + _guard_marked() + _guard_events() + _guard_comments())
        # 实时消息（不进缓存）：一条新事件和一条关注的人的发言，时刻在页面的真实时钟之后，顶部条才会弹出
        fresh = [
            {"type": "guard_event", "ev": {"id": "g-ev6", "kind": "flood", "cat": "flood_user", "lvl": "alert", "amb": None,
                                          "n": 7, "accounts": [], "window_sec": 10, "first_ts": FUTURE, "ts": FUTURE,
                                          "rv": None, "sample": {"cid": "g8", "user": "spam_demo", "text": "oferta oferta oferta",
                                                                 "uid": "7100000000000000031", "uid_src": "id",
                                                                 "handle": "spam_demo"}}},
            {"type": "comment", "id": "g8", "user": "rival_demo", "text": "otra vez yo", "ts": FUTURE, "translated": None,
             "state": "pending", "uid": GUARD_UID, "uid_ok": True, "handle": "rival_demo"},
            {"type": "comment_update", "id": "g8", "translated": "Me again", "state": "ok"},
        ]
        sc_name = name + ("" if lang == "en" else "-zh")
        sc = _scenario(sc_name, "desktop", lang, _desktop_messages(server, lang, fresh), clicks=clicks)
        sc["measure"] = list(DESKTOP_MEASURE + GUARD_MEASURE)
        return sc


def _guard_scenarios(tmp):
    out = []
    for lang in ("en", "zh"):
        out.append(_guard_scenario(tmp, lang, "home-guard", live=False, clicks=["#guard-head"],
                                   state={"backend": "claude", "status": "no_sdk"}))
        out.append(_guard_scenario(tmp, lang, "live-guard", live=True, clicks=["#guard-state"]))
        out.append(_guard_scenario(tmp, lang, "live-guard-marked", live=True, clicks=["#guard-state", "#guard-tab-marked"]))
        out.append(_guard_scenario(tmp, lang, "live-guard-tripped", live=True, clicks=["#guard-state"],
                                   state={"backend": "claude", "status": "tripped", "lag_sec": 14, "unreviewed": 6}))
    return out


def _phone(tmp):
    """手机四个场景。phone-live：每条后端文字都带 text_en / why_en，最后一条 status 是 ended。"""
    with i18n.use("en"):
        server, live = _live_server("en", tmp)
        ended = {"type": "status", "state": "ended", "detail": LIVE_ENDED_NOTE}
        messages = _phone_messages(server, live + [ended])
    denied = [{"type": "viewer_denied", "reason": "token"}]
    full = [{"type": "viewer_denied", "reason": "full"}]
    return [
        _scenario("phone-live", "phone", "en", messages),
        _scenario("phone-denied", "phone", "en", denied, close={"code": 4401}),
        _scenario("phone-full", "phone", "en", full, close={"code": 4429}),
        # 服务端按 Accept-Language 给的是中文页，这台手机自己选过英文：页面必须是英文，首帧不闪中文
        _scenario("phone-local-en", "phone", "en", messages, page_lang="zh",
                  storage={"tlt.viewer.lang": "en"}),
    ]


def _float(tmp):
    """浮窗页：消息照真实连接的样子——/ws?role=float 的 hello（只带浮窗用得上的键）、短回放（最后几条字幕和弹幕，
    没有报警）、再接不进缓存的实时消息：一条中文昵称、中文译文的弹幕（数据，必须落在数据区里），
    和最后一条没译成的字幕（角标才会画出来）。回放由真的 CaptionServer.replay_messages("float") 给出。"""
    with i18n.use("en"):
        server = CaptionServer(port=8765)
        server.config.update(caption_size=40, target_lang="en", guard_state=dict(GUARD_STATE))
        _feed(server, [{"type": "status", "state": "live", "detail": ""}])
        _feed(server, _captions() + _guard_marked() + _guard_comments() + _alerts())
        live = [
            {"type": "comment", "id": "gz", "user": "小明", "text": "Ya compré dos", "ts": NOW - 100,
             "translated": None, "state": "pending"},
            {"type": "comment_update", "id": "gz", "translated": CAPTION_ZH, "state": "ok"},
            _caption(20, "Nos vemos mañana a la misma hora", ts=NOW - 90),
            _caption_update(20, None, state="failed", refused=True),
        ]
        out = [{"type": "hello", "config": server._float_config(server.config)}]
        out += server.replay_messages("float")
        out += live
        return _scenario("float-live", "float", "en", [i18n.render(m, "en") for m in out])


def with_float_button(scenario, name):
    """同一个直播中场景，加上桌面应用窗口模式才有的 config.float_window：顶栏多出「浮窗」按钮。
    live-full 不带它——浏览器模式和 README 截图里没有这颗按钮；窗口模式的顶栏宽度另外用这个场景量。"""
    out = copy.deepcopy(scenario)
    out["name"] = name
    out["messages"][0]["config"]["float_window"] = {"available": True, "open": False}
    return out


# 闪购助手的状态（hello 的 config.flash_sale）：接口契约的虚构样例 running（tests/fixtures/flash_sale/running.json，
# 闪购助手 /status 原样的回话）经生产代码 flash_sale.StatusTracker 规整成推给页面的形状——同一个 tracker 先在单调时钟
# 0 秒、再在 STUCK_SEC 秒各喂一次，已停止的那个商品就成了卡住的（attention 是 stuck），和直播中真实的样子一样。
# 后端只发代码、数字和闪购助手的原话，没有要上界面的文字，文案由页面的 flash.js 按代码写。
# 样例里有一个卡住的商品和一条没看过的 ❌ 记录：按钮是变色的那个样子（橙点绝对定位、不占宽度）。记录正文里有中文
# （闪购助手写的），落在 class i18n-data 里；商品标题是英文。时间整体平移到页面打开时的「现在」之后，倒计时看起来才像真的
FLASH_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "flash_sale" / "running.json"
FLASH_ZH_ALERT = "📦 Calm Gel Mask x4 (Fictional): 1 个规格库存不足 5,按可用库存填"   # 样例里一条带中文的记录正文（数据）


def flash_state(now_ms=None):
    doc = json.loads(FLASH_FIXTURE.read_text(encoding="utf-8"))
    updated = flash_sale.parse_iso(doc["updated_at"])
    tracker = flash_sale.StatusTracker()
    tracker.update("ok", copy.deepcopy(doc), 0.0)
    # 60 秒后闪购助手早已刷新过 updated_at（每 2 秒一次）：照样盖一个新时刻，不然同一个值停了 60 秒就成了陈旧
    later = copy.deepcopy(doc)
    later["updated_at"] = datetime.fromtimestamp(updated + flash_sale.STUCK_SEC, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    state = tracker.update("ok", later, float(flash_sale.STUCK_SEC))
    shift = int(now_ms if now_ms is not None else time.time() * 1000) + 10 * 60 * 1000 - int(round(updated * 1000))
    for product in state["products"]:
        for key in ("ends_ms", "next_ms"):
            if product[key] is not None:
                product[key] += shift
    for alert in state["alerts"]:
        alert["at_ms"] += shift
    return state


def with_flash_button(scenario, name, state):
    """同一个场景，hello 里加上装了闪购助手才有的 config.flash_sale：顶栏多出「闪购」按钮。
    点击先点开闪购浮层（浮层里的字收到消息就画好了，扫描照样扫得到），再照原场景的顺序点；
    直播中的场景最后停在换主播面板开着（#switch-confirm 要量），闪购浮层被互斥关掉。"""
    out = copy.deepcopy(scenario)
    out["name"] = name
    out["messages"][0]["config"]["flash_sale"] = copy.deepcopy(state)
    out["clicks"] = ["#flash-btn"] + list(scenario["clicks"])
    return out


def zh_quiet(scenario, name):
    """中文的直播中场景，报警关着（顶栏没有「报警开」标签）、品牌是 ZH_QUIET_BRAND：「浮窗」两个字才露得出来。
    live-full-zh 是满编（报警开、BRAND_ZH），顶栏已经满了，按钮在那里是只有图标（has-alert-tag）；
    docs/i18n-style.md §4.2 的余量表按这里的基线算（报警关、品牌标签约 143px）。"""
    out = copy.deepcopy(scenario)
    out["name"] = name
    config = out["messages"][0]["config"]
    config["alerts_enabled"] = False
    config["active_brand"] = {"id": BRAND_ID, "name": ZH_QUIET_BRAND}
    return out


def with_extra_target(scenario):
    """同一个场景，hello 里的当前目标换成四种以外的 EXTRA_TARGET，名字加 EXTRA 后缀：页面照 web/app.js
    showTargetLang 给下拉补一项 Bahasa Indonesia 并选中它，下拉是它最宽的样子。别的一个字节不动——
    字幕回放里的 target_lang 仍是 en，页面不看它（直播中途从 en 换过去的中控就是这样）。"""
    out = copy.deepcopy(scenario)
    out["name"] = scenario["name"] + EXTRA
    out["messages"][0]["config"]["target_lang"] = EXTRA_TARGET
    return out


def resized(scenario, width, height=None):
    """同一个场景换一个窗口大小：live-full@1100 这样命名。"""
    out = copy.deepcopy(scenario)
    out["name"] = "{}@{}".format(scenario["name"], width)
    out["size"] = [width, height or scenario["size"][1]]
    return out


def build(tmp):
    """全部场景：{名字: 场景}。tmp 是一个空的临时目录（磁盘盘点要在里面造文件）。"""
    tmp = Path(tmp)
    scenarios = [_home_idle(tmp), _home_zh(tmp), _home_selfcheck_fail(tmp), _live_full(tmp, "en"),
                 _status_only(tmp, "connecting", "connecting"),
                 # 先收到 hello，随即断开：离线横幅。页面每次重连都会再走一遍
                 _status_only(tmp, "offline", "idle", close={"code": 1006}),
                 _status_only(tmp, "error-4003110", "error", browser_only_message(3, login={
                     "safari": browser_login.BLOCKED, "chrome": browser_login.NOT_LOGGED_IN})),
                 _live_full(tmp, "zh"), _live_bad(tmp), _reconnect_no_audio(tmp)]
    scenarios += _guard_scenarios(tmp) + _phone(tmp) + [_float(tmp)]
    out = {sc["name"]: sc for sc in scenarios}
    flash = flash_state()
    out["live-float"] = with_float_button(out["live-full"], "live-float")
    out["live-flash"] = with_flash_button(out["live-float"], "live-flash", flash)
    for base in ("live-full", "live-float", "live-flash"):
        out[base + EXTRA] = with_extra_target(out[base])
    for base in LIVE_BASES:
        for width in LIVE_WIDTHS + NARROW_WIDTHS:
            sc = resized(out[base], width)
            out[sc["name"]] = sc
    for base in FLASH_BASES:
        for width in FLASH_WIDE_WIDTHS:
            sc = resized(out[base], width)
            out[sc["name"]] = sc
    out["home-flash"] = with_flash_button(out["home-idle"], "home-flash", flash)
    out["home-flash-zh"] = with_flash_button(out["home-zh"], "home-flash-zh", flash)
    for base in HOME_BASES:
        for width in HOME_WIDTHS:
            sc = resized(out[base], width)
            out[sc["name"]] = sc
    # 中文页的浮窗按钮：满编（报警开）的 1000px 一档，和报警关着的六档（ZH_FLOAT_WIDTHS）；各有一份目标在四种以外的。
    # 再各加一份带闪购按钮的（live-flash-zh*）：「浮窗」两个字的露法和不带的一样，「Aa」省出来的地方只多不少
    out["live-float-zh"] = with_float_button(out[CONTROL_ZH], "live-float-zh")
    out["live-float-zh-quiet"] = zh_quiet(out["live-float-zh"], "live-float-zh-quiet")
    out["live-flash-zh"] = with_flash_button(out["live-float-zh"], "live-flash-zh", flash)
    out["live-flash-zh-quiet"] = with_flash_button(out["live-float-zh-quiet"], "live-flash-zh-quiet", flash)
    for base in ("live-float-zh", "live-float-zh-quiet", "live-flash-zh", "live-flash-zh-quiet"):
        out[base + EXTRA] = with_extra_target(out[base])
    for base in ZH_FULL_BASES:
        sc = resized(out[base], 1000)
        out[sc["name"]] = sc
    for base in ZH_QUIET_BASES:
        for width in ZH_FLOAT_WIDTHS:
            sc = resized(out[base], width)
            out[sc["name"]] = sc
    return out
