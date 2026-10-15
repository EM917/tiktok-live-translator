// i18n: done
/* 直播中界面几处「显示什么」的纯逻辑（web/live-ui.js）：顶栏手机同看按钮的文案、
 * 连接中提示、提示条的图标与文字、顶栏浮层的水平位置、报警分级标签、「回到最新」按钮上的未读条数，以及底部大字幕的
 * 翻译状态角标、「约 N 秒前」、页脚延迟统计行、字幕字号的比例。
 * 这些判断本身不碰 DOM，放进 app.js 的立即执行函数就没法被 node 测试 require
 * （同 web/settings-rows.js 的起因）。写法和加载方式照 web/switch.js：浏览器里
 * 是全局函数，Node 测试环境走 module.exports。 */

"use strict";

// 浏览器里 L/LN 是 i18n.js 定义的全局函数（这里的 var 不会清掉它）；node 测试里从 i18n.js 取
if (typeof L === "undefined") { var I18N_ = require("./i18n.js"); var L = I18N_.L, LN = I18N_.LN, APP_NAME = I18N_.APP_NAME; }

/* 顶栏「手机同看」按钮：关着只写功能名；开着没人看写「已打开」；有人在看直接写
 * 人数，比「已打开」信息多、字还短（pm.md #3）。count 只给窄窗口用——那时按钮
 * 只剩图标，文字视觉隐藏，人数作为图标旁的数字露出来；没人看时为空串 */
function shareButtonText(on, viewers) {
  if (!on) return { text: L("手机同看", "Phone Viewing"), count: "" };
  var n = Math.floor(Number(viewers));
  if (!(n > 0)) return { text: L("手机同看 · 已打开", "Phone Viewing · On"), count: "" };
  return { text: L("手机同看 · " + n + " 人", "Phone Viewing · " + n), count: String(n) };
}

/* 连接中提示：只在 connecting 时显示。有主播名就带上——点错了要等连上才看得出来，
 * 而失败的连接要等约 28 秒，这段时间里主播名是唯一能核对「点没点对」的线索
 * （pm.md #5）。已有字幕时 inline：改成字幕末尾的一行，不把旧字幕盖住。
 * offline（本地连接断线重连）返回 null：跟 viewTransition 一样，不代表直播
 * 本身变了，调用方维持原样不动 */
function connectHint(state, streamer, hasCaptions) {
  if (state === "offline") return null;
  if (state !== "connecting") return { show: false, inline: false, text: "" };
  return {
    show: true,
    inline: !!hasCaptions,
    text: streamer ? L("正在连接 @" + streamer + "…", "Connecting to @" + streamer + "…")
                   : L("正在连接…", "Connecting…"),
  };
}

/* 后端提示条（health / incident）的文字开头自带状态 emoji（app/pipeline.py 的
 * 「🔴 检测已降级…」「⚠️ 识别开始落后…」）：那几条同时打印到终端，终端里要留着；
 * 界面上换成按级别画的线性图标，文字开头那几个去掉，不然图标和 emoji 叠两遍。
 * 只去开头连续的这几种，句中的不动 */
var STATUS_EMOJI_PREFIX = /^(?:(?:🔴|🟠|🟡|⚠|✅|❌)️?\s*)+/;
function stripStatusEmoji(text) {
  return String(text == null ? "" : text).replace(STATUS_EMOJI_PREFIX, "");
}

/* 提示条级别 → 图标名（index.html .icon-sprite 里的 #i-<名字>）。形状跟级别一起变，
 * 不只靠底色：提醒＝三角，降级/出错＝八角，说明＝ⓘ（designer.md #1 的原则） */
function barIconFor(level) {
  if (level === "error" || level === "degraded") return "octagon";
  if (level === "info") return "info";
  return "warn";
}

/* 顶栏浮层的 right（CSS px，相对顶栏右边缘）：右边缘对齐触发按钮的右边缘；
 * 按钮太靠左、浮层会从左边出界时往右推，左右都至少留 margin */
function popoverRight(anchorRight, viewportWidth, panelWidth, margin) {
  var m = margin == null ? 12 : margin;
  var right = Math.round(viewportWidth - anchorRight);
  var maxRight = Math.floor(viewportWidth - panelWidth - m);
  return Math.max(m, Math.min(right, maxRight));
}

/* 报警分级胶囊上的字。以前是「🔴 命中」这类 emoji 前缀，颜色现在交给 CSS
 * （.alert-item.tier-*），这里只给字；认不出的分级按「命中」处理（同旧逻辑） */
var ALERT_TIER_TEXT = { exact: L("命中", "Exact"), variant: L("变体", "Variant"), fuzzy: L("疑似", "Similar") };
function alertTierText(tier) {
  return Object.prototype.hasOwnProperty.call(ALERT_TIER_TEXT, tier) ? ALERT_TIER_TEXT[tier] : L("命中", "Exact");
}

/* 「回到最新」按钮上的字。翻上去看的时候，新来的行不会把视野拉走，但得让人知道下面攒了多少没看的：
 * 「回到最新 · 12 条新字幕」。n 是翻上去之后新来的条数（回放的不算）；0、负数、不是数字时只写按钮
 * 本来的名字。字幕和弹幕各一个函数：英文的名词和单复数都不同（caption / comment），整句写成模板，
 * 不拼接（docs/i18n-style.md §0 第 5 条） */
function jumpLatestCaptionsText(n) {
  var k = Math.floor(Number(n));
  if (!(k > 0)) return L("回到最新", "Jump to Latest");
  return LN(k, "回到最新 · " + k + " 条新字幕",
            "Jump to Latest · 1 new caption", "Jump to Latest · " + k + " new captions");
}
function jumpLatestCommentsText(n) {
  var k = Math.floor(Number(n));
  if (!(k > 0)) return L("回到最新", "Jump to Latest");
  return LN(k, "回到最新 · " + k + " 条新弹幕",
            "Jump to Latest · 1 new comment", "Jump to Latest · " + k + " new comments");
}

/* ---- 字幕的翻译状态：历史卡片和底部大字幕共用一份 ----
 * 译不出来的字幕屏幕上显示的是原文。卡片早就有角标说明为什么（未翻译（接口拒答）……），
 * 底部大字幕以前没有：大字号的西语孤零零摆在那里，看不出是「还没译」「译失败了」还是「本来就不用译」。
 * 两处从同一个函数取字，改一处不会漏另一处。
 * 只写观察到的：接口拒答、结果不像译文、翻译失败、因积压被跳过。不写原因（CLAUDE.md 第八条）。
 * 返回 { text, fail }：text 为空串表示没有角标；fail 表示翻译没成（卡片上画成红色） */
function captionStateChip(msg) {
  var state = msg ? msg.translate_state : undefined;
  if (state === "failed") {
    return {
      text: msg.refused ? L("未翻译（接口拒答）", "Not translated (API refusal)")
        : msg.not_translation ? L("未翻译（结果不像译文）", "Not translated (the result didn’t look like a translation)")
        : L("翻译失败", "Translation failed"),
      fail: true,
    };
  }
  if (state === "dropped") return { text: L("翻译已跳过（积压）", "Skipped (backlog)"), fail: false };
  return { text: "", fail: false };
}

/* 底部大字幕的状态角标：译文到了没有角标；没译成（还有被积压跳过的）用卡片同款的字。
 * 译文还在路上（pending）时不画：那是每一句都要走的几秒，角标来了又走会在主阅读区闪个不停；
 * 译文迟迟不来的时候，下面的「约 N 秒前」照样在涨，而服务端最终一定会给出结局（译好、失败或被跳过）。 */
function liveBarChip(msg) {
  if (!msg || msg.translated) return { text: "", fail: false };
  return captionStateChip(msg);
}

/* ---- 底部大字幕的滞后：大字幕上这一句的声音结束多久了 ----
 * 服务端的 caption 消息带 ts（原文上屏的时刻）和 e2e_ms（这一段音频结束 → 上屏）。
 * ts − e2e_ms = 这一段音频结束的时刻，和违禁词报警延迟（now − audio_end_ts）是同一个起点。
 * 不从「最早一个词说出口」算：那个数被句子长短主导。一条新字幕上屏时就已经是 片段时长 + e2e ≈ 10 秒，
 * 下一句八九秒后才来，所以健康的直播里它一直在 10～18 秒之间转圈，永远写不出「刚刚」，
 * 中控没有基线，分不出这是正常还是积压了 10 秒。从最早一个词算的数留给页脚的「字幕等待」
 * （服务端 Telemetry.caption_wait），那个是一整场的 p50 / P95，有对照。
 * e2e_ms 缺了（演示模式、老版本服务端）就返回 null：不知道就不写。ts 缺失时按收到的这一刻算（nowMs）。 */
function captionAudioEndAt(msg, nowMs) {
  if (!msg || typeof msg.e2e_ms !== "number" || !(msg.e2e_ms >= 0)) return null;
  var shownAt = typeof msg.ts === "number" && msg.ts > 0 ? msg.ts * 1000 : nowMs;
  if (typeof shownAt !== "number" || !isFinite(shownAt)) return null;
  return shownAt - msg.e2e_ms;
}

/* 多久才值得写。2026-10-13～15 三场直播的 3216 次换句，「音频结束 → 被下一句换掉」p50 10.4 秒、
 * p90 11.8 秒、p99 13.0 秒：分段上限 9 秒 + 上屏 p90 2.8 秒，所以 12 秒会在健康的直播里闪（6.1% 的换句
 * 超过它），15 秒只剩 0.6%——那些是主播停了很久，或者真的积压了。低于它这一行不出现：
 * 正常的时候不让一个数字在主阅读区底下一秒一跳，它出现就是一个信号。 */
var CAPTION_LAG_SHOW_SEC = 15;

/* 「约 N 秒前」。ageMs 是从这一段音频结束到现在的毫秒数，页面每秒重算一次，所以数字会随时间涨——
 * 大字幕上的这一句停多久，它就老多久。不到 CAPTION_LAG_SHOW_SEC 秒、负数（本机时钟被拨回）、
 * 离谱的大数（超过 6 小时）都不写，返回空串。秒数四舍五入；一分钟以上改写分钟
 * （大字幕停着不动多半是主播不说话了）。只写观察，不猜原因（CLAUDE.md 第八条）。
 * 英文照 docs/i18n-style.md §2.3、§2.4：sec / min 缩写，数字和单位之间一个空格 */
function captionAgeText(ageMs) {
  if (typeof ageMs !== "number" || !isFinite(ageMs) || ageMs > 6 * 3600 * 1000) return "";
  if (ageMs < CAPTION_LAG_SHOW_SEC * 1000) return "";
  if (ageMs < 60000) {
    var sec = Math.round(ageMs / 1000);
    return L("约 " + sec + " 秒前", "About " + sec + " sec ago");
  }
  var min = Math.round(ageMs / 60000);
  return L("约 " + min + " 分钟前", "About " + min + " min ago");
}

/* ---- 页脚延迟统计行 ----
 * 第一个数是字幕等待：从主播说出这一句到中控读到的那一版字幕上屏（译出来的是译文），
 * 这是用这个工具看直播的人实际等的时间。违禁词报警的延迟（最迟的情况：词在片段最开头）紧跟其后，
 * 一个字都没删——它仍是产品的 KPI（CLAUDE.md 第六条），只是排在第二。
 * 字幕等待要等第一条译文回来才有数；那之前这一段不写（写「—」会像出了故障）。
 * 英文用紧凑写法（docs/i18n-style.md §3 #121）：所有计数都出现时逐字翻译会超过一行 */
function fmtMs(v) { return v == null ? "—" : (v / 1000).toFixed(1) + "s"; }

function statsLineParts(msg) {
  var asr = msg.asr || {};
  var tr = msg.translate || {};
  var seg = msg.segment || {};
  var det = msg.detect_worst || {};
  var wait = msg.caption_wait || {};
  var parts = [];
  if (wait.p50 != null) {
    parts.push(L("字幕 " + fmtMs(wait.p50) + " / P95 " + fmtMs(wait.p95) + " 后出现",
                 "Captions +" + fmtMs(wait.p50) + " (P95 " + fmtMs(wait.p95) + ")"));
  }
  parts.push(
    L("违禁词最迟 " + fmtMs(det.p50) + " / P95 " + fmtMs(det.p95) + " 内报警",
      "Alerts ≤" + fmtMs(det.p50) + " (P95 " + fmtMs(det.p95) + ")"),
    L("其中 切段 " + fmtMs(seg.p50) + " + 识别 " + fmtMs(asr.p50),
      "Segment " + fmtMs(seg.p50) + " + ASR " + fmtMs(asr.p50)),
    L("译文再等 " + fmtMs(tr.p50), "Translation +" + fmtMs(tr.p50)));
  if (msg.audio_backlog_sec >= 3) {
    parts.push(L("积压 " + msg.audio_backlog_sec.toFixed(0) + "s",
                 "Backlog " + msg.audio_backlog_sec.toFixed(0) + "s"));
  }
  if (msg.audio_segments_dropped) parts.push(L("丢音频 " + msg.audio_segments_dropped,
                                               "Dropped " + msg.audio_segments_dropped));
  // 识别跑飞是丢音频的前兆，出现就该看见
  if (msg.asr_overruns) parts.push(L("识别超时 " + msg.asr_overruns, "ASR timeouts " + msg.asr_overruns));
  if (msg.translation_jobs_dropped) parts.push(L("跳过翻译 " + msg.translation_jobs_dropped,
                                                 "Skipped " + msg.translation_jobs_dropped));
  if (msg.asr_queue_depth || msg.translation_queue_depth) {
    parts.push(L("积压 " + msg.asr_queue_depth + "/" + msg.translation_queue_depth,
                 "Queue " + msg.asr_queue_depth + "/" + msg.translation_queue_depth));
  }
  return parts;
}

/* ---- 字幕字号 ----
 * 顶栏滑块（20–60，宽窗口默认 34）定底部大字幕的字号；历史卡片的译文/原文和弹幕栏的译文/原文
 * 按同一个比例跟着变，不再各自固定 17/13/15px。比例 = 滑块值 / 34，夹在 0.85–1.75 之间：
 * 往小调时卡片不低于 85%（17px 的译文最小约 14.5px，13px 的原文最小约 11px），往大调到顶是 1.75 倍。
 * 没调过滑块的人什么也不设，样式表里 --cap-scale 的默认值是 1，卡片还是今天的 17/13/15px。 */
var CAPTION_SIZE_MIN = 20;       // 滑块 #font-size 的 min/max；服务端 app/settings.py 里是同一对数
var CAPTION_SIZE_MAX = 60;
var CAPTION_SIZE_BASE = 34;
var CAPTION_SCALE_MIN = 0.85;
var CAPTION_SCALE_MAX = 1.75;
function captionScale(size) {
  var n = Number(size);
  if (!(n > 0) || !isFinite(n)) return 1;
  var scale = Math.min(CAPTION_SCALE_MAX, Math.max(CAPTION_SCALE_MIN, n / CAPTION_SIZE_BASE));
  return Math.round(scale * 1000) / 1000;
}
/* 服务端（config.caption_size）或 localStorage 里的字号：是 20–60 之间的整数才认，否则 null。
 * 不夹到边界——来路不明的值不该被当成用户选的（同 app/settings.py normalize_caption_size） */
function captionSizeFromConfig(value) {
  if (typeof value === "boolean" || value == null || value === "") return null;
  var n = Number(value);
  if (!isFinite(n)) return null;
  n = Math.round(n);
  return n >= CAPTION_SIZE_MIN && n <= CAPTION_SIZE_MAX ? n : null;
}

/* 该写到 <html> 上的 CSS 变量：大字幕的字号和卡片/弹幕的比例 */
function captionSizeVars(size) {
  var n = Math.round(Number(size));
  return { "--sub-size": n + "px", "--cap-scale": String(captionScale(n)) };
}

/* Node 测试环境导出；浏览器里作为全局函数被 app.js 使用 */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { shareButtonText: shareButtonText, connectHint: connectHint,
                     stripStatusEmoji: stripStatusEmoji, barIconFor: barIconFor,
                     popoverRight: popoverRight, alertTierText: alertTierText,
                     jumpLatestCaptionsText: jumpLatestCaptionsText,
                     jumpLatestCommentsText: jumpLatestCommentsText,
                     captionStateChip: captionStateChip, liveBarChip: liveBarChip,
                     captionAudioEndAt: captionAudioEndAt, captionAgeText: captionAgeText,
                     CAPTION_LAG_SHOW_SEC: CAPTION_LAG_SHOW_SEC,
                     fmtMs: fmtMs, statsLineParts: statsLineParts,
                     captionScale: captionScale, captionSizeVars: captionSizeVars,
                     captionSizeFromConfig: captionSizeFromConfig };
}
