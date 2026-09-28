/* 直播中界面几处「显示什么」的纯逻辑（web/live-ui.js）：顶栏手机同看按钮的文案、
 * 连接中提示、提示条的图标与文字、顶栏浮层的水平位置、报警分级标签。
 * 这些判断本身不碰 DOM，放进 app.js 的立即执行函数就没法被 node 测试 require
 * （同 web/settings-rows.js 的起因）。写法和加载方式照 web/switch.js：浏览器里
 * 是全局函数，Node 测试环境走 module.exports。 */

"use strict";

/* 顶栏「手机同看」按钮：关着只写功能名；开着没人看写「已打开」；有人在看直接写
 * 人数，比「已打开」信息多、字还短（pm.md #3）。count 只给窄窗口用——那时按钮
 * 只剩图标，文字视觉隐藏，人数作为图标旁的数字露出来；没人看时为空串 */
function shareButtonText(on, viewers) {
  if (!on) return { text: "手机同看", count: "" };
  var n = Math.floor(Number(viewers));
  if (!(n > 0)) return { text: "手机同看 · 已打开", count: "" };
  return { text: "手机同看 · " + n + " 人", count: String(n) };
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
    text: streamer ? "正在连接 @" + streamer + "…" : "正在连接…",
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
var ALERT_TIER_TEXT = { exact: "命中", variant: "变体", fuzzy: "疑似" };
function alertTierText(tier) {
  return Object.prototype.hasOwnProperty.call(ALERT_TIER_TEXT, tier) ? ALERT_TIER_TEXT[tier] : "命中";
}

/* Node 测试环境导出；浏览器里作为全局函数被 app.js 使用 */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { shareButtonText: shareButtonText, connectHint: connectHint,
                     stripStatusEmoji: stripStatusEmoji, barIconFor: barIconFor,
                     popoverRight: popoverRight, alertTierText: alertTierText };
}
