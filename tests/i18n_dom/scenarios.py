"""G10 的场景（spec §12.3）：在 i18n.use(lang) 下用真实函数组装页面会收到的消息。

界面上会出现的后端文字一律由生产代码生成：browser_only_message、share_note（经
ViewerHub.state）、自检行与自检提示条（_publish_selfcheck）、_announce_health 三档、
_check_clock_gap、_translate_alert 的 why、engine_label / restore_engine、update_check_note、
diskspace.inventory。这里不手写任何一句要上界面的中文或英文——迁移到哪一句，扫描就跟到哪一句。

数据字段（字幕原文、弹幕、词条、主播名）用西语或 ASCII。DATA_ZH 是故意放进去的中文数据
（一条译成简体中文的字幕、一个中文品牌名、一条报警上下文的中文译文），它们必须落在
translate="no" 里：英文页上它们不许被报出来，中文对照页上也不许——用来证明豁免确实生效。
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
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import browser_login, diskspace, i18n, qr, selfcheck, viewer
from app.pipeline import LIVE_ENDED_NOTE, Pipeline, browser_only_message
from app.server import CaptionServer, replay_payloads
from app.telemetry import Telemetry
from app.translator import engine_label, mask_key, restore_engine
from app.updater import RELEASES_URL, update_check_note

FAKE_TOKEN = "fake-token-not-a-real-secret-0000000000"   # viewer.js 只认片段里的 #k=
STREAMER = "bellaallnatural"
RECENT = ("bellaallnatural", "lamejorcrema", "tiendadeana", "glowbymaria")
BRAND_ID = "acme"
BRAND_ZH = "艾可美天然护肤旗舰店"              # 中文品牌名（数据，虚构）
CAPTION_ZH = "这款面霜一周就能淡化色斑"        # target_lang=zh-CN 的一条译文（数据）
CONTEXT_ZH = "这款面霜能治好色斑"              # 报警上下文的中文译文（数据）
DATA_ZH = (BRAND_ZH, CAPTION_ZH, CONTEXT_ZH)

NOW = datetime(2026, 9, 28, 20, 0, 0).timestamp()   # 固定时刻：日志文件的新旧、时钟间隙都按它算
DESKTOP = (1280, 860)
PHONE = (390, 844)
LIVE_WIDTHS = (1000, 1100, 1101, 1199, 1200)   # 版面断言的五档（spec §12.3 第 7 条）
NARROW_WIDTHS = (860, 720)                     # 这两档只断言 .topbar 不溢出
DESKTOP_MEASURE = (".topbar", "#active-brand-tag", ".status-text", "#switch-confirm")
PHONE_MEASURE = (".status-pill", "#conn-text")

DESKTOP_EN = ("home-idle", "home-selfcheck-fail", "live-full", "connecting", "offline",
              "error-4003110")
PHONE_EN = ("phone-live", "phone-denied", "phone-full", "phone-local-en")
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
    """统计行满格：真 Telemetry 的 snapshot，积压、丢段、超时、跳过翻译、队列都不为零。"""
    tel = Telemetry()
    for asr_ms, e2e_ms, seg_ms in ((1100, 2300, 1800), (1300, 2700, 2100), (900, 2000, 1600)):
        tel.record_asr(asr_ms, e2e_ms, seg_ms)
        tel.record_translation(800)
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
    desktop = page == "desktop"
    return {"name": name, "page": page, "size": list(DESKTOP if desktop else PHONE),
            "lang": lang, "page_lang": page_lang or lang, "messages": _plain(messages),
            "replies": _plain(replies or {}), "close": close, "clicks": list(clicks),
            "measure": list(DESKTOP_MEASURE if desktop else PHONE_MEASURE),
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


def resized(scenario, width, height=None):
    """同一个场景换一个窗口大小：live-full@1100 这样命名。"""
    out = copy.deepcopy(scenario)
    out["name"] = "{}@{}".format(scenario["name"], width)
    out["size"] = [width, height or scenario["size"][1]]
    return out


def build(tmp):
    """全部场景：{名字: 场景}。tmp 是一个空的临时目录（磁盘盘点要在里面造文件）。"""
    tmp = Path(tmp)
    scenarios = [_home_idle(tmp), _home_selfcheck_fail(tmp), _live_full(tmp, "en"),
                 _status_only(tmp, "connecting", "connecting"),
                 # 先收到 hello，随即断开：离线横幅。页面每次重连都会再走一遍
                 _status_only(tmp, "offline", "idle", close={"code": 1006}),
                 _status_only(tmp, "error-4003110", "error", browser_only_message(3, login={
                     "safari": browser_login.BLOCKED, "chrome": browser_login.NOT_LOGGED_IN})),
                 _live_full(tmp, "zh")] + _phone(tmp)
    out = {sc["name"]: sc for sc in scenarios}
    for width in LIVE_WIDTHS + NARROW_WIDTHS:
        sc = resized(out["live-full"], width)
        out[sc["name"]] = sc
    return out
