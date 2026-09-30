#!/usr/bin/env python3
"""README 的截图与演示动图：中文界面、英文界面各一套，可重复运行。

    nice -n 19 python3 tools/readme_shots.py                 # 全部重出：截图 + 两个动图
    nice -n 19 python3 tools/readme_shots.py --lang en       # 只出英文那套
    nice -n 19 python3 tools/readme_shots.py --only live --only demo
    python3 tools/readme_shots.py --list                     # 只列场景和输出路径，不开 Chrome

输出（--out 默认是仓库根目录）：
    assets/screenshots/{en,zh}/<场景>.png   截图，2 倍像素
    assets/demo.{en,zh}.gif                  演示动图，1000px 宽、循环播放

要 Google Chrome（或 TLT_CHROME 指定的 Chromium 系浏览器）和 Pillow（合成动图、压缩 PNG；
PATH 上有 pngquant 时 PNG 交给它压）。项目的 .venv 里没装 Pillow，用装了它的 python3 跑。

装置照搬 G10（tests/test_i18n_dom.py、tests/i18n_dom/）：本机 127.0.0.1 上起一个假页面服务器，
端 web/ 里的真页面，WebSocket 换成假的、按场景推消息；Chrome 无头、别的主机名一律解析失败，
视口经 DevTools 协议定（桌面 1000×760，程序默认窗口；手机 390×844），浅色。
场景是专门给 README 的「干净」状态：没有休眠、识别降级、报警这类提示条，自检全绿。
后端文字（自检、引擎、同看说明……）照 G10 的做法由生产代码生成；数据是虚构的：
西语带货台词、示例主播名、虚构品牌，不用真实主播，也不读本机的词表和日志。

动图按时间轴推消息：一句字幕先到原文（「翻译中」），约 0.9 秒后译文补上，跟 pipeline 的
节奏一致（先广播 caption，译文由 caption_update 后补）。每推一次截一帧，帧时长就是到下一次
事件的间隔，所以动图的节奏与截图快慢无关。

每张图出图前都核对一遍：视口对、数据真的到了（状态不是「等待连接…」、字幕条数对）、没有
提示条、没有被省略号截断的文字、没有被滚动切掉一截的字幕卡片、英文图里没有一个汉字（切换
语言按钮上的「中文」这个语言自称除外）。对不上就报错退出，不写这张图。同一台机器、同一版
Chrome 重跑，出来的文件逐字节相同（2026-09-29 实测），所以图的 diff 就是界面的变化。

直播转写进行中拒绝运行（CLAUDE.md 第三条）：无头 Chrome 是一整个浏览器进程。
"""
import argparse
import asyncio
import base64
import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.stdio import harden_stdio                     # noqa: E402

# 重定向到文件时中文输出不该让工具中途崩掉（见 app/stdio.py）
harden_stdio()

from app import browser_login, i18n, qr, selfcheck, viewer   # noqa: E402
from app.telemetry import Telemetry                          # noqa: E402
from app.translator import engine_label                      # noqa: E402
from tests import test_i18n_dom as G10                       # noqa: E402
from tests.i18n_dom import scenarios as S                    # noqa: E402

LANGS = ("en", "zh")
TARGET = {"en": "en", "zh": "zh-CN"}          # 英文界面译成英文，中文界面译成简体中文；原文都是西语
DESKTOP = (1000, 760)                         # 程序默认窗口（web/index.html 顶栏注释）
PHONE = S.PHONE                               # 390×844
SHOT_SCALE = 2                                # 截图 2 倍像素，README 在高分屏上不糊
GIF_SCALE = 1                                 # 动图按 CSS 像素出：1000px 宽
GIF_MAX_BYTES = 3 * 1024 * 1024
SETTLE_SEC = 0.35                             # 推完消息等页面排好版再截
LOAD_TIMEOUT_SEC = 60
LIVE_PATTERNS = ("tiktokcdn", "main.py --browser")

# ---- 数据（虚构） ---------------------------------------------------------------------------
# 主播名一看就是示例；品牌名是编的，拉丁字母，两种界面都一样
STREAMER = "luna.demo"
RECENT = ("luna.demo", "tienda.demo", "glow.demo", "moda.demo")
BRAND = {"id": "lumaflor", "name": "Lumaflor"}
BRAND_OPTIONS = [BRAND, {"id": "nuvia", "name": "Nuvia Skin"}]
# 同看链接里的口令：形状同 viewer.new_token()（43 个 URL 安全字符），由固定种子算出，每次一样
SHARE_TOKEN = base64.urlsafe_b64encode(
    hashlib.sha256(b"readme-shots").digest()).decode("ascii").rstrip("=")
SHARE_IP = "192.168.1.23"

# (西语原文, 英文译文, 中文译文)。五句：1000×760 的窗口里字幕区正好一屏，最上面那张卡片不会
# 被顶栏切掉一半（多一句就要滚动，见 FACTS_JS 的 clipped）
CAPTIONS = (
    ("Hoy les traigo el sérum de vitamina C que tanto me pidieron",
     "Today I brought the vitamin C serum you kept asking me for",
     "今天给大家带来了你们一直在问的维C精华"),
    ("Se aplica en la mañana, antes del protector solar",
     "You apply it in the morning, before your sunscreen",
     "早上用，涂在防晒霜之前"),
    ("Tiene ácido hialurónico, así que hidrata muchísimo",
     "It has hyaluronic acid, so it's super hydrating",
     "里面有玻尿酸，所以特别保湿"),
    ("Hoy tenemos envío gratis en todos los pedidos",
     "Today we have free shipping on every order",
     "今天所有订单都包邮"),
    ("Si se llevan dos, el segundo va a mitad de precio",
     "If you take two, the second one is half price",
     "买两瓶的话，第二瓶半价"),
)
# 手机页 390×844 放得下四句（每句下面还有「重译」按钮），多了会滚动、最上面那句被切
PHONE_CAPTIONS = CAPTIONS[1:]
# 动图里实时到的几句，接在 CAPTIONS 前 DEMO_HISTORY 句之后（加起来也是五句，不滚动）
DEMO_CAPTIONS = (
    ("Miren qué ligerita es la textura, se absorbe al instante",
     "Look how light the texture is, it absorbs instantly",
     "看这个质地多轻薄，一抹就吸收了"),
    ("No deja la piel grasosa, ni siquiera con este calor",
     "It doesn't leave your skin greasy, not even in this heat",
     "不会让皮肤油腻，这么热的天也不会"),
    ("Les dejo el enlace aquí abajo en el carrito naranja",
     "I'll leave the link down here in the orange cart",
     "链接放在下面的橙色购物车里了"),
)
# (观众名, 西语原文, 英文译文, 中文译文)
COMMENTS = (
    ("sofi.glow", "¿Sirve para piel grasa?", "Does it work for oily skin?", "油皮能用吗？"),
    ("carla_mx", "Ya lo compré, me encantó", "I already bought it, I loved it", "我已经买了，超喜欢"),
    ("dany.beauty", "¿Hacen envíos a Colombia?", "Do you ship to Colombia?", "能寄到哥伦比亚吗？"),
    ("lu_martinez", "¿Cuánto cuesta el set completo?", "How much is the full set?", "全套多少钱？"),
    ("marisol22", "Saludos desde Guadalajara", "Greetings from Guadalajara", "来自瓜达拉哈拉的问候"),
)
DEMO_COMMENTS = (
    ("vale.rios", "¿Se puede usar con retinol?", "Can you use it with retinol?", "能和A醇一起用吗？"),
    ("anita_cdmx", "Necesito dos, ya lo agregué", "I need two, I already added them", "我要两瓶，已经加购了"),
)
# 动图时间轴（秒）：("cap", DEMO_CAPTIONS 下标) 在 t 秒出原文、t + TRANSLATE_SEC 秒补译文；
# ("cmt", DEMO_COMMENTS 下标) 在 t 秒出原文、t + COMMENT_TRANSLATE_SEC 秒补译文
TRANSLATE_SEC = 0.9            # 本地 Hy-MT2 1.8B 一句的量级（Telemetry 里的 translate p50）
COMMENT_TRANSLATE_SEC = 0.6
# 字幕约 3.7 秒一句（切段 2.5–9 秒，app/segmenter.py），弹幕夹在中间
DEMO_EVENTS = ((1.5, "cap", 0), (3.3, "cmt", 0), (5.2, "cap", 1), (7.2, "cmt", 1),
               (8.9, "cap", 2))
DEMO_END_SEC = 13.0            # 最后一帧停到这里再从头循环
DEMO_HISTORY = 2               # 动图开场时已经有的字幕条数（CAPTIONS 的前几句）


def pick(row, lang):
    """(原文, 英文, 中文) 里取 lang 的译文。"""
    return row[1] if lang == "en" else row[2]


# ---- 场景（照 tests/i18n_dom/scenarios.py 的组装方式） -----------------------------------------

def _room(streamer):
    return S.room_url(streamer)


def _clean_checks():
    """全绿的自检：名字查 selfcheck.NAMES，词表 / 浏览器登录态 / 磁盘三行调真的检查函数。"""
    names, ok = selfcheck.NAMES, selfcheck.OK
    detector = SimpleNamespace(enabled=True, count=53, effective_count=53, load_warnings=[],
                               source_path="banned_terms.txt")
    glossary = SimpleNamespace(enabled=True, entries=[None] * 38)
    with mock.patch.object(selfcheck.shutil, "disk_usage",
                           return_value=SimpleNamespace(free=120 * 1024 ** 3)):
        disk = asyncio.run(selfcheck.check_disk())
    return [selfcheck._check(names["ffmpeg"], ok, "ffmpeg 7.1"),
            selfcheck._check(names["denoise"], ok, "rnnoise"),
            selfcheck._check(names["asr"], ok, "mlx-whisper large-v3-turbo"),
            selfcheck._check(names["translator"], ok, engine_label("hymt2")),
            asyncio.run(selfcheck.check_watchlist(detector)),
            asyncio.run(selfcheck.check_glossary(glossary)),
            selfcheck._browser_login_row({"safari": browser_login.OK}),
            selfcheck._check(names["comments"], ok, "TikTokLive 7.0.2"),
            disk]


def _version():
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _server(lang, tmp, *, viewers=None, alerts=False):
    """待机的真 CaptionServer（S._server），换成 README 的数据：示例主播、虚构品牌、全绿自检。
    viewers 不是 None 时同看打开、这么多台手机在看。要在 i18n.use(lang) 里调。"""
    server = S._server(lang, tmp)
    server.config.update({
        "version": _version(), "target_lang": TARGET[lang], "room_url": _room(STREAMER),
        "recent_rooms": [{"streamer": s, "url": _room(s), "at": ""} for s in RECENT],
        "brands": {STREAMER: BRAND["id"]}, "brand_options": BRAND_OPTIONS,
        "source_lang": "es,en", "alerts_enabled": alerts,
        "viewer": _share(viewers) if viewers is not None else server.config["viewer"],
    })
    S._produce(server, lambda p: p._publish_selfcheck(_clean_checks()))
    return server


def _share(viewers):
    """同看打开、viewers 台手机在看：ViewerHub.state() 的真载荷，只有一个局域网地址。"""
    hub = viewer.ViewerHub(None, ports=[8766, 8767], token=SHARE_TOKEN)
    hub._running = True
    hub._viewers = [object()] * viewers
    url = hub.url(SHARE_IP)
    return hub.state(ip_info={"ip": SHARE_IP, "all": [SHARE_IP], "ambiguous": False},
                     qr_rows=qr.rows(qr.encode(url.encode("utf-8"))))


def _captions(lang, rows, ts0, first_id=1):
    """一句一对消息：caption（原文，翻译中）+ caption_update（译文），形状同 pipeline。"""
    out = []
    for i, row in enumerate(rows):
        cid = first_id + i
        out.append(S._caption(cid, row[0], target=TARGET[lang], ts=ts0 + i * 7))
        out.append(S._caption_update(cid, pick(row, lang), target=TARGET[lang]))
    return out


def _comment(cid, row, ts, lang):
    head = {"type": "comment", "id": cid, "user": row[0], "text": row[1], "ts": ts,
            "translated": None, "state": "pending"}
    done = {"type": "comment_update", "id": cid, "translated": pick(row[1:], lang), "state": "ok"}
    return head, done


def _comments(lang, rows, ts0):
    out = []
    for i, row in enumerate(rows):
        out.extend(_comment("c{}".format(i + 1), row, ts0 + i * 11, lang))
    return out + [{"type": "comment_source", "backend": "connected", "detail": ""}]


def _stats():
    """统计行：真 Telemetry 的 snapshot，没有积压、丢段、超时。"""
    tel = Telemetry()
    for asr_ms, e2e_ms, seg_ms in ((1050, 2150, 1700), (1150, 2350, 1900), (980, 2050, 1650)):
        tel.record_asr(asr_ms, e2e_ms, seg_ms)
        tel.record_translation(850)
    return dict(tel.snapshot(), type="stats")


def _live_server(lang, tmp, *, captions=CAPTIONS, comments=COMMENTS, viewers=None, alerts=False,
                 ts0=None):
    """直播中：主播 + 本场品牌 + 字幕 + 弹幕。ts0 是第一句字幕的时刻（默认两分半钟前）。"""
    server = _server(lang, tmp, viewers=viewers, alerts=alerts)
    server.config.update(active_brand=dict(BRAND))
    S._feed(server, [{"type": "status", "state": "live", "detail": ""}])
    cap0 = S.NOW - 150 if ts0 is None else ts0
    cmt0 = (cap0 + 30) if ts0 is None else (cap0 + 4)
    S._feed(server, _captions(lang, captions, ts0=cap0) + _comments(lang, comments, ts0=cmt0))
    return server


def _desktop(name, lang, server, *, clicks=(), live=()):
    sc = S._scenario(name, "desktop", lang, S._desktop_messages(server, lang, live),
                     clicks=clicks)
    sc["size"] = list(DESKTOP)
    return sc


def home(lang, tmp):
    """首页待机：开始面板 + 最近直播间 + 设置分组（全部收起、自检全绿）。"""
    with i18n.use(lang):
        return _desktop("home", lang, _server(lang, tmp))


def settings_language(lang, tmp):
    """首页往下，设置分组里「界面语言 · Language」那一行展开。"""
    with i18n.use(lang):
        return _desktop("settings-language", lang, _server(lang, tmp), clicks=["#lang-head"])


def live(lang, tmp):
    """直播中：字幕历史 + 右侧弹幕 + 底部大字幕 + 统计行，顶栏带本场品牌。"""
    with i18n.use(lang):
        return _desktop("live", lang, _live_server(lang, tmp), live=[_stats()])


def switch_streamer(lang, tmp):
    """直播中点「换主播」，再点一个最近直播间：已武装，确认按钮可点。"""
    with i18n.use(lang):
        return _desktop("switch-streamer", lang, _live_server(lang, tmp), live=[_stats()],
                        clicks=["#switch-btn", "#switch-recent-list .recent-chip:not([disabled])"])


def share_panel(lang, tmp):
    """直播中打开顶栏「手机同看」面板：二维码、链接、在看人数。"""
    with i18n.use(lang):
        return _desktop("share-panel", lang, _live_server(lang, tmp, viewers=1), live=[_stats()],
                        clicks=["#share-btn"])


def phone(lang, tmp):
    """手机同看页（390px）：照 ViewerHub 的入册顺序回放。报警开着、没有命中——关着的话
    手机顶上常驻一行「违禁词警示已关闭」，README 的图不要这一行。"""
    with i18n.use(lang):
        # 时间戳跟桌面那几张对得上：PHONE_CAPTIONS 是 CAPTIONS 去掉第一句
        server = _live_server(lang, tmp, captions=PHONE_CAPTIONS, viewers=1, alerts=True,
                              ts0=S.NOW - 150 + 7)
        return S._scenario("phone", "phone", lang, S._phone_messages(server))


def demo(lang, tmp):
    """动图的开场：直播中，已经有 DEMO_HISTORY 句字幕、两条弹幕；之后按 timeline() 推。"""
    with i18n.use(lang):
        # 开场那几句紧挨着时间轴上的第一句（S.NOW），时间戳连得上
        server = _live_server(lang, Path(tmp), captions=CAPTIONS[:DEMO_HISTORY],
                              comments=COMMENTS[:2], ts0=S.NOW - 7 * DEMO_HISTORY - 4)
        return _desktop("demo", lang, server, live=[_stats()])


def timeline(lang):
    """[(秒, [消息…])]：从 0 秒的开场帧起，按 DEMO_EVENTS 出原文、再补译文。"""
    target = TARGET[lang]
    events = {0.0: []}

    def at(t, msg):
        events.setdefault(round(t, 2), []).append(i18n.render(msg, lang))

    for t, kind, idx in DEMO_EVENTS:
        if kind == "cap":
            cid = DEMO_HISTORY + 1 + idx
            row = DEMO_CAPTIONS[idx]
            at(t, S._caption(cid, row[0], target=target, ts=S.NOW + t))
            at(t + TRANSLATE_SEC, S._caption_update(cid, pick(row, lang), target=target))
        else:
            row = DEMO_COMMENTS[idx]
            head, done = _comment("d{}".format(idx + 1), row, S.NOW + t, lang)
            at(t, head)
            at(t + COMMENT_TRANSLATE_SEC, done)
    return [(t, S._plain(events[t])) for t in sorted(events)]


SCENES = {"home": home, "settings-language": settings_language, "live": live,
          "switch-streamer": switch_streamer, "share-panel": share_panel, "phone": phone}
GIF = "demo"


def build(lang, tmp, names=None):
    """{场景名: 场景}（不含动图）。tmp 是空的临时目录（磁盘盘点要在里面造文件）。"""
    return {name: SCENES[name](lang, Path(tmp)) for name in (SCENES if names is None else names)}


def shot_path(out, lang, name):
    return Path(out) / "assets" / "screenshots" / lang / "{}.png".format(name)


def gif_path(out, lang):
    return Path(out) / "assets" / "demo.{}.gif".format(lang)


# ---- 出图前的核对 ---------------------------------------------------------------------------

CJK = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]")

# 页面上量出来的事实：状态、条数、哪些提示条露着、视口里看得见的文字、被省略号截断的元素
FACTS_JS = r"""
(function () {
  function shown(el) {
    for (var e = el; e && e.nodeType === 1; e = e.parentElement) {
      var cs = getComputedStyle(e);
      if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return false;
    }
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0
      && r.top < innerHeight && r.left < innerWidth;
  }
  function byId(id) { return document.getElementById(id); }
  function text(id) { var el = byId(id); return el ? el.textContent.trim() : null; }
  function where(el) {
    var parts = [];
    for (var e = el; e && e.nodeType === 1 && parts.length < 3; e = e.parentElement) {
      parts.unshift(e.tagName.toLowerCase() + (e.id ? "#" + e.id : "")
                    + (e.classList.length ? "." + e.classList[0] : ""));
    }
    return parts.join(" > ");
  }
  var visible = [];
  var walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (var n = walk.nextNode(); n; n = walk.nextNode()) {
    var el = n.parentElement, t = n.nodeValue.trim();
    if (!t || !el || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|OPTION)$/.test(el.tagName)) continue;
    if (!shown(el)) continue;
    visible.push({ at: where(el), text: t,
                   data: !!el.closest('[translate="no"], .i18n-data') });
  }
  document.querySelectorAll("select").forEach(function (s) {
    var o = s.selectedOptions && s.selectedOptions[0];
    if (o && shown(s)) visible.push({ at: where(s), text: o.text.trim(),
                                      data: !!s.closest('[translate="no"]') });
  });
  document.querySelectorAll("input[placeholder], input[type=text]").forEach(function (i) {
    if (!shown(i)) return;
    if (i.value) visible.push({ at: where(i), text: i.value, data: true });
    else if (i.placeholder) visible.push({ at: where(i) + " @placeholder", text: i.placeholder, data: false });
  });
  var cut = [];
  document.querySelectorAll("body *").forEach(function (el) {
    if (!shown(el)) return;
    var cs = getComputedStyle(el);
    var clamp = cs.webkitLineClamp && cs.webkitLineClamp !== "none";
    if (cs.textOverflow !== "ellipsis" && !clamp) return;
    var over = clamp ? el.scrollHeight > el.clientHeight + 1 : el.scrollWidth > el.clientWidth;
    if (!over && !clamp) {
      // 整数宽度看不出零点几像素的截断（tests/i18n_dom/scan.js 的注释），按小数量排好版的文字
      var r = el.getBoundingClientRect(), rg = document.createRange();
      rg.selectNodeContents(el);
      var inner = r.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)
                - parseFloat(cs.borderLeftWidth) - parseFloat(cs.borderRightWidth);
      over = rg.getBoundingClientRect().width > inner + 0.01;
    }
    if (over) cut.push(where(el) + "：" + el.textContent.trim().slice(0, 60));
  });
  var bars = ["incident-bar", "health-bar", "alert-panel", "status-banner", "update-bar",
              "migrate-bar", "fix-command", "alert-mode-tag",
              "alert-mode-line", "stale-line", "demo-banner", "health-line", "alert-section",
              "alert-badge"].filter(function (id) { var el = byId(id); return el && shown(el); });
  var incidents = byId("incident-list");
  if (incidents && incidents.children.length) bars.push("incident-list");
  var expanded = {};
  document.querySelectorAll("[aria-expanded]").forEach(function (el) {
    if (el.id) expanded[el.id] = el.getAttribute("aria-expanded") === "true";
  });
  // 滚动容器顶上被切掉一截的字幕卡片（桌面 #history，手机 #caption-section）
  var clipped = [];
  var box = byId("history") || byId("caption-section");
  var top = box ? box.getBoundingClientRect().top : 0;
  document.querySelectorAll("#history .cap, #caption-list > *").forEach(function (el) {
    var r = el.getBoundingClientRect();
    if (shown(el) && r.top < top - 0.5 && r.bottom > top) clipped.push(el.textContent.trim().slice(0, 40));
  });
  var qr = byId("share-qr");
  var confirm = byId("switch-confirm");
  return {
    page: byId("status-text") ? "desktop" : "phone",
    status: text("status-text"), conn: text("conn-text"), stream: text("stream-text"),
    caps: document.querySelectorAll("#history .cap, #caption-list > *").length,
    comments: document.querySelectorAll("#comment-list .cmt-item").length,
    liveBar: !!(byId("live-bar") && shown(byId("live-bar"))),
    startPanel: !!(byId("start-panel") && shown(byId("start-panel"))),
    commentPanel: !!(byId("comment-panel") && shown(byId("comment-panel"))),
    switchPanel: !!(byId("switch-panel") && shown(byId("switch-panel"))),
    switchArmed: !!(confirm && !confirm.disabled),
    sharePanel: !!(byId("share-panel") && shown(byId("share-panel"))),
    qr: qr ? qr.width : 0,
    langBody: !!(byId("lang-body") && shown(byId("lang-body"))),
    brandTag: !!(byId("active-brand-tag") && shown(byId("active-brand-tag"))),
    bars: bars, expanded: expanded, visible: visible, cut: cut, clipped: clipped,
    viewport: { width: innerWidth, height: innerHeight }
  };
})()
"""

# 英文图里唯一允许出现的汉字：切换语言的按钮上写的是另一种语言的自称（手机页 #lang-toggle，
# translate="no"），这是语言名，不是没翻的界面文字
AUTONYMS = {"中文"}
STARTING = {"等待连接…", "Starting…", "连接中…", "Connecting…"}
STATUS_LIVE = {"en": "Live · ", "zh": "直播中 · "}
STATUS_IDLE = {"en": "Ready", "zh": "待机"}


def problems(name, lang, facts, expect_caps=None):
    """这一帧能不能进 README：交回问题清单（空 = 可以）。"""
    out = []

    def need(ok, what):
        if not ok:
            out.append(what)

    size = PHONE if name == "phone" else DESKTOP
    need([facts["viewport"]["width"], facts["viewport"]["height"]] == list(size),
         "视口不是 {}×{}：{}".format(size[0], size[1], facts["viewport"]))
    need(not facts["bars"], "露着提示条：{}".format(facts["bars"]))
    need(not facts["cut"], "文字被截断：\n  " + "\n  ".join(facts["cut"]))
    need(not facts["clipped"], "字幕卡片顶上被切掉一截：{}".format(facts["clipped"]))
    if lang == "en":
        leaks = ["{}「{}」".format(v["at"], v["text"]) for v in facts["visible"]
                 if CJK.search(v["text"]) and not (v["data"] and v["text"] in AUTONYMS)]
        need(not leaks, "英文图里有汉字：\n  " + "\n  ".join(leaks))
    if facts["page"] == "phone":
        need(facts["conn"] not in STARTING and facts["conn"], "手机页没连上：{}".format(facts["conn"]))
        need(facts["caps"] == len(PHONE_CAPTIONS), "手机页字幕 {} 条，应为 {}".format(
            facts["caps"], len(PHONE_CAPTIONS)))
        return out
    need(facts["status"] not in STARTING, "状态还是「{}」：数据没到".format(facts["status"]))
    if name in ("home", "settings-language"):
        need(facts["status"] == STATUS_IDLE[lang], "首页状态应为待机：{}".format(facts["status"]))
        need(facts["startPanel"] and facts["caps"] == 0, "首页应只有开始面板")
        opened = [k for k in ("sc-head", "engine-head", "watch-head", "disk-head")
                  if facts["expanded"].get(k)]
        need(not opened, "设置分组里这些行展开了：{}".format(opened))
        need(facts["expanded"].get("lang-head") == (name == "settings-language"),
             "界面语言那一行的展开状态不对")
        if name == "settings-language":
            need(facts["langBody"], "界面语言那一行没展开")
        return out
    need((facts["status"] or "").startswith(STATUS_LIVE[lang]), "状态不是直播中：{}".format(
        facts["status"]))
    wanted = len(CAPTIONS) if expect_caps is None else expect_caps
    need(facts["caps"] == wanted, "字幕 {} 条，应为 {}".format(facts["caps"], wanted))
    need(facts["liveBar"], "底部大字幕没露出来")
    need(facts["commentPanel"] and facts["comments"] > 0, "右侧弹幕列没有内容")
    need(facts["brandTag"], "顶栏没有本场品牌")
    if name == "switch-streamer":
        need(facts["switchPanel"] and facts["switchArmed"], "换主播面板没打开或没武装")
    if name == "share-panel":
        need(facts["sharePanel"] and facts["qr"] > 0, "同看面板没打开或二维码没画")
    return out


# ---- 页面服务器与 Chrome（装置同 G10） ---------------------------------------------------------

# 同一个假 WebSocket（tests/i18n_dom/fake_ws.js），另留一个把手：按时间轴往页面推消息
PUSH_JS = r"""
/* tools/readme_shots.py：给假 WebSocket 留一个把手，动图按时间轴经它推消息 */
(function () {
  var Fake = window.WebSocket;
  function Handle(url) { var ws = new Fake(url); window.__readmeWS = ws; return ws; }
  ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function (k) { Handle[k] = Fake[k]; });
  window.WebSocket = Handle;
  window.__readmePush = function (msgs) {
    var ws = window.__readmeWS;
    if (!ws || !ws.onmessage) return false;
    msgs.forEach(function (m) { ws.onmessage({ data: JSON.stringify(m) }); });
    return true;
  };
})();
"""


class ReadmeSite(G10._Site):
    """G10 的假页面服务器，fake_ws.js 后面接上 PUSH_JS。"""

    def resolve(self, raw_path):
        hit = G10._Site.resolve(self, raw_path)
        if hit is not None and urlsplit(raw_path).path.rsplit("/", 1)[-1] == "fake_ws.js":
            return hit[0] + PUSH_JS.encode("utf-8"), hit[1]
        return hit


def live_transcription_running():
    """直播转写还在跑（CLAUDE.md 第三条的硬闸）：交回命中的进程模式，没有就 None。"""
    for pat in LIVE_PATTERNS:
        try:
            hit = subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0
        except OSError:          # 没有 pgrep（Windows）：查不了就当没有
            return None
        if hit:
            return pat
    return None


@contextlib.contextmanager
def serving():
    site = ReadmeSite(G10.WEB_DIR)
    thread = threading.Thread(target=site.serve_forever, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        site.shutdown()
        site.server_close()


@contextlib.contextmanager
def chrome(binary, lang, profile):
    """起一个无头 Chrome（开着调试端口，停在 about:blank），交回 DevTools 地址；出了 with
    连同辅助进程一起关掉。一次只开这一个。"""
    argv = G10._chrome_argv(binary, DESKTOP, lang, profile) + [
        "--hide-scrollbars", "--force-color-profile=srgb", "--remote-debugging-port=0",
        "about:blank"]
    deadline = time.monotonic() + LOAD_TIMEOUT_SEC
    with G10._chrome(argv) as (proc, out, err):
        ws_url = G10.devtools_url(err, profile)
        while ws_url is None:
            if proc.poll() is not None:
                raise SystemExit("Chrome 没报出 DevTools 地址就退出了（退出码 {}）\n{}".format(
                    proc.poll(), G10._tail(err)))
            if time.monotonic() > deadline:
                raise SystemExit("Chrome {} 秒没报出 DevTools 地址\n{}".format(
                    LOAD_TIMEOUT_SEC, G10._tail(err)))
            time.sleep(0.1)
            ws_url = G10.devtools_url(err, profile)
        yield ws_url


class Page:
    """一个标签页：定视口、浅色、减少动效，打开场景页面，等 G10 的 scan.js 跑完（页面脚本、
    场景里的点击都已经做完）。"""

    def __init__(self, cdp, session, target):
        self.cdp, self.session, self.target = cdp, session, target

    @classmethod
    async def open(cls, cdp, url, size, scale):
        target = (await cdp.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
        session = (await cdp.call("Target.attachToTarget",
                                  {"targetId": target, "flatten": True}))["sessionId"]
        page = cls(cdp, session, target)
        await page.call("Page.enable")
        await page.call("Emulation.setDeviceMetricsOverride", {
            "width": size[0], "height": size[1], "deviceScaleFactor": scale, "mobile": False})
        await page.call("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-color-scheme", "value": "light"},
            {"name": "prefers-reduced-motion", "value": "reduce"}]})
        await page.call("Page.navigate", {"url": url})
        deadline = time.monotonic() + LOAD_TIMEOUT_SEC
        while not await page.eval('!!document.getElementById("i18n-scan")', quiet=True):
            if time.monotonic() > deadline:
                raise SystemExit("{} 秒内页面没跑完：{}".format(LOAD_TIMEOUT_SEC, url))
            await asyncio.sleep(0.1)
        await asyncio.sleep(SETTLE_SEC)
        return page

    async def call(self, method, params=None):
        return await self.cdp.call(method, params, session=self.session)

    async def eval(self, expression, quiet=False):
        try:
            got = await self.call("Runtime.evaluate", {"expression": expression,
                                                       "returnByValue": True})
        except G10.CDPError:
            if quiet:             # 导航途中旧的执行上下文没了：下一轮再问
                return None
            raise
        if "exceptionDetails" in got:
            raise SystemExit("页面里执行出错：{}".format(got["exceptionDetails"]))
        return got.get("result", {}).get("value")

    async def push(self, msgs):
        if not await self.eval("window.__readmePush({})".format(json.dumps(msgs, ensure_ascii=False))):
            raise SystemExit("推不进消息：假 WebSocket 的把手没装上")
        await asyncio.sleep(SETTLE_SEC)

    async def screenshot(self, clip=None):
        params = {"format": "png"}
        if clip:
            params["clip"] = clip
        data = (await self.call("Page.captureScreenshot", params))["data"]
        return base64.b64decode(data)

    async def close(self):
        await self.cdp.call("Target.closeTarget", {"targetId": self.target})


# ---- 图片 ---------------------------------------------------------------------------------

def _pil():
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("要 Pillow：换一个装了它的 python3 跑（项目的 .venv 里没有）") from None
    return Image


def save_png(png, path):
    """写 PNG 并压体积：PATH 上有 pngquant 就用它，否则 Pillow 量化成 256 色（不抖动：界面是
    大片纯色，抖动只会添噪点）。交回写出的字节数。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tool = shutil.which("pngquant")
    if tool:
        path.write_bytes(png)
        subprocess.run([tool, "--quality=80-98", "--speed", "1", "--strip", "--force",
                        "--output", str(path), "--", str(path)], check=True)
        return path.stat().st_size
    Image = _pil()
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                 dither=Image.Dither.NONE).save(path, optimize=True)
    return path.stat().st_size


def save_gif(frames, path):
    """frames：[(PNG 字节, 毫秒)]。全部帧共用一张调色板（各帧各配一张会闪），不抖动；
    循环播放。Pillow 写多帧 GIF 时只存和上一帧不同的那块。交回写出的字节数。"""
    Image = _pil()
    imgs = [Image.open(io.BytesIO(png)).convert("RGB") for png, _ in frames]
    w, h = imgs[0].size
    sheet = Image.new("RGB", (w, h * len(imgs)))
    for i, img in enumerate(imgs):
        sheet.paste(img, (0, h * i))
    palette = sheet.quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                             dither=Image.Dither.NONE)
    quant = [img.quantize(palette=palette, dither=Image.Dither.NONE) for img in imgs]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    quant[0].save(path, save_all=True, append_images=quant[1:],
                  duration=[ms for _, ms in frames], loop=0, disposal=1, optimize=False)
    return path.stat().st_size


# ---- 跑 ---------------------------------------------------------------------------------

def _url(site, scenario):
    url = "http://127.0.0.1:{}/".format(site.server_address[1])
    return url + ("#k=" + SHARE_TOKEN if scenario["page"] == "phone" else "")


async def _capture(ws_url, site, lang, jobs, out):
    import aiohttp

    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(ws_url, max_msg_size=0) as ws:
            cdp = G10._CDP(ws)
            for name, scenario in jobs:
                site.scenario = scenario
                gif = name == GIF
                page = await Page.open(cdp, _url(site, scenario), scenario["size"],
                                       GIF_SCALE if gif else SHOT_SCALE)
                try:
                    if gif:
                        await _record(page, lang, out)
                    else:
                        await _shoot(page, name, lang, out)
                finally:
                    await page.close()


async def _check(page, name, lang, expect_caps=None):
    facts = await page.eval(FACTS_JS)
    found = problems(name, lang, facts, expect_caps)
    if found:
        raise SystemExit("{}/{} 不能进 README：\n{}".format(lang, name, "\n".join(found)))
    return facts


# 设置分组那一张只截分组本身：先把展开的界面语言那一行滚到底部版权行（fixed）上面，
# 再量分组的位置。.history 是 scroll-behavior: smooth，要显式 instant，否则量到的是动画半途
SETTINGS_CLIP_JS = r"""
(function () {
  var card = document.getElementById("lang-card"), box = document.getElementById("history");
  var foot = document.querySelector(".copyright").getBoundingClientRect().top;
  box.scrollBy({ top: card.getBoundingClientRect().bottom - (foot - 24), behavior: "instant" });
  var r = document.querySelector("#start-panel .settings").getBoundingClientRect();
  var pad = 24, x = Math.max(0, r.left - pad), y = Math.max(0, r.top - pad);
  return { x: x, y: y, width: Math.min(innerWidth, r.right + pad) - x,
           height: Math.min(foot, r.bottom + pad) - y, scale: 1 };
})()
"""


async def _shoot(page, name, lang, out):
    clip = None
    if name == "settings-language":
        clip = await page.eval(SETTINGS_CLIP_JS)
        await asyncio.sleep(SETTLE_SEC)
    await _check(page, name, lang)
    path = shot_path(out, lang, name)
    size = save_png(await page.screenshot(clip), path)
    print("  {}  {:.0f} KB".format(path.relative_to(out), size / 1024))


async def _record(page, lang, out):
    """按时间轴推消息、每次事件截一帧；帧时长是到下一次事件的间隔。"""
    steps = timeline(lang)
    shots = []
    seen = DEMO_HISTORY
    for t, msgs in steps:
        if msgs:
            await page.push(msgs)
        seen += sum(1 for m in msgs if m["type"] == "caption")
        await _check(page, GIF, lang, expect_caps=seen)
        shots.append((t, await page.screenshot()))
    ends = [t for t, _ in shots[1:]] + [DEMO_END_SEC]
    frames = [(png, int(round((end - t) * 1000))) for (t, png), end in zip(shots, ends)]
    path = gif_path(out, lang)
    size = save_gif(frames, path)
    if size > GIF_MAX_BYTES:
        raise SystemExit("{} 有 {:.1f} MB，超过 3 MB".format(path, size / 1024 / 1024))
    print("  {}  {} 帧 {:.1f} 秒  {:.0f} KB".format(
        path.relative_to(out), len(frames), DEMO_END_SEC, size / 1024))


def run(langs, names, gif, out):
    pat = live_transcription_running()
    if pat:
        raise SystemExit("直播转写仍在进行（{}），不开无头 Chrome".format(pat))
    binary = G10.find_chrome()
    if binary is None:
        raise SystemExit("找不到 Chrome：装 Google Chrome，或用 TLT_CHROME 指定")
    _pil()
    with tempfile.TemporaryDirectory(prefix="readme-shots-") as tmp, serving() as site:
        for lang in langs:
            data = Path(tmp) / "data-{}".format(lang)
            data.mkdir()
            jobs = list(build(lang, data, names).items())
            if gif:
                jobs.append((GIF, demo(lang, data)))
            print("{}：{} 个场景".format(lang, len(jobs)))
            with chrome(binary, lang, Path(tmp) / "profile-{}".format(lang)) as ws_url:
                asyncio.run(_capture(ws_url, site, lang, jobs, Path(out)))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="出 README 的截图（assets/screenshots/{en,zh}/）与演示动图（assets/demo.{en,zh}.gif）。"
                    "开一个无头 Chrome，只访问本机 127.0.0.1 上的假页面；直播转写进行中拒绝运行。")
    ap.add_argument("--lang", choices=LANGS, action="append",
                    help="只出这种界面语言（可重复；默认两种都出）")
    ap.add_argument("--only", choices=list(SCENES) + [GIF], action="append", metavar="场景",
                    help="只出这些场景（可重复；demo 是动图）。可选：{}".format(
                        "、".join(list(SCENES) + [GIF])))
    ap.add_argument("--out", default=str(ROOT), help="输出根目录（默认仓库根目录）")
    ap.add_argument("--list", action="store_true", help="只列场景和输出路径，不开 Chrome")
    args = ap.parse_args(argv)
    langs = tuple(args.lang or LANGS)
    only = args.only or list(SCENES) + [GIF]
    names = [n for n in SCENES if n in only]
    gif = GIF in only
    out = Path(args.out).resolve()
    if args.list:
        for lang in langs:
            for name in names:
                print("{}  {:<18} {}".format(lang, name, shot_path(out, lang, name)))
            if gif:
                print("{}  {:<18} {}".format(lang, GIF, gif_path(out, lang)))
        return 0
    run(langs, names, gif, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
