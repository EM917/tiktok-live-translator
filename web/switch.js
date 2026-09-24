/* 「换主播」两段式确认的纯函数（web/switch.js）。
 * 直播中换主播是不可逆的破坏性操作（会立刻停听 @A），照开始面板那种「输入完
 * 直接开始」不安全——误触输入框、点错最近直播间 chip 都会中断正在进行的监听。
 * 两段式：第一次输入/点 chip 只「武装」，第二次点确认按钮（或回车）才真的发送。
 * 独立成文件以便单元测试：tests/switch.test.mjs 用 node:test 钉住这些规则。
 * 写法和加载方式照 web/brand.js、web/follow.js：浏览器里是全局函数，
 * Node 测试环境走 module.exports。DOM 部分（按钮文案怎么画、定时器怎么挂）
 * 留给 app.js，这里只管状态该是什么样。 */

"use strict";

// 武装后多久算过期（6 秒，见换主播面板说明文案）；武装后多久内的点击/回车
// 算「双击误触」，不当成第二次确认（400ms）。两个数字导出给 app.js 的
// setTimeout 复用，避免两处各写一份、改一个漏一个。
var SWITCH_ARM_MS = 6000;
var SWITCH_DEBOUNCE_MS = 400;

// 干净的初始态：没输入过，没有目标主播。
function initialSwitchState() {
  return { target: "", armed: false, armedAt: null };
}

/* 武装：输入框变化、或点了最近直播间 chip 时调用。始终按「刚刚发生的这一次
 * 输入」重新算一遍，不叠加旧状态——这正是「武装后改了输入就回到第一步」：
 * 旧的 armedAt 和旧的确认窗口作废，得重新点一次确认按钮。
 *
 * target 为空（认不出主播名）：回到初始态，不武装。
 * target 就是当前正在监听的主播（大小写不敏感）：记下 target 但不武装——
 * 调用方（switchButtonLabel）据此显示「正在监听的就是 @A」而不是确认文案，
 * 按钮本该被禁用，没有什么可确认的。
 * 其余情况：武装，armedAt 记为 now，交给 shouldConfirmSwitch 判断是否过期/防抖。 */
function armSwitch(target, currentStreamer, now) {
  var t = target ? String(target).toLowerCase() : "";
  var cur = currentStreamer ? String(currentStreamer).toLowerCase() : "";
  if (!t) return initialSwitchState();
  if (t === cur) return { target: t, armed: false, armedAt: null };
  return { target: t, armed: true, armedAt: now };
}

// 武装是否已经超过 6 秒没人确认。未武装本身谈不上过期。
function isSwitchExpired(state, now) {
  if (!state || !state.armed || state.armedAt == null) return false;
  return now - state.armedAt > SWITCH_ARM_MS;
}

// 超时复位：过期就回到干净的初始态，没过期原样返回（不改入参）。
// app.js 用 setTimeout 主动触发一次，这里单独抽出来是为了让「过期该变成什么」
// 这条规则本身也能被测，不依赖真的等 6 秒。
/* 超时只解除「武装」，目标保留：输入框里还是那个主播，按钮回到可点的第一步
 * （「换到 @B」），点一下重新武装。以前连目标一起清掉，按钮变成禁用的
 * 「确认换主播」，中控犹豫超过 6 秒就只能改输入或重点 chip 才能继续。 */
function expireSwitchState(state, now) {
  if (!isSwitchExpired(state, now)) return state;
  return { target: state.target, armed: false, armedAt: null };
}

/* 点确认按钮（或回车）这一下该做什么：
 *   "confirm"：武装中且过了防抖、没过期——真的发送
 *   "arm"：有目标、不是当前主播，但没武装（超时复位过）或已过期——重新武装，不发送
 *   "none"：防抖窗口内、没有目标、或目标就是当前主播 */
function switchClickAction(state, now, currentStreamer) {
  if (shouldConfirmSwitch(state, now, currentStreamer)) return "confirm";
  var target = state && state.target ? state.target : "";
  var cur = currentStreamer ? String(currentStreamer).toLowerCase() : "";
  if (!target || target === cur) return "none";
  if (!state.armed || isSwitchExpired(state, now)) return "arm";
  return "none";
}

/* 点确认按钮、或在输入框按 Enter，这一下算不算数（要不要真的发送）：
 *   - 没武装：不算——按钮本该是禁用的，这里仍兜底拒绝，不能空手发一条 start
 *   - 武装后 400ms 内：算双击误触，不算（防止武装那一下的点击事件被连着数成确认）
 *   - 武装超过 6 秒：过期，不算——调用方应该已经被 expireSwitchState 复位，
 *     这里仍兜底判一次，不依赖定时器一定先跑到
 *   - 目标就是当前主播：不算——按钮本该被禁用，这里兜底拒绝 */
function shouldConfirmSwitch(state, now, currentStreamer) {
  if (!state || !state.armed || state.armedAt == null) return false;
  if (now - state.armedAt < SWITCH_DEBOUNCE_MS) return false;
  if (isSwitchExpired(state, now)) return false;
  var cur = currentStreamer ? String(currentStreamer).toLowerCase() : "";
  if (cur && state.target === cur) return false;
  return true;
}

/* 确认按钮该显示什么文案、要不要禁用。三种情况：
 *   1. 目标就是当前主播：不管武装与否，禁用并说清楚「已经在监听这个人」
 *   2. 武装了、目标是别的主播：「再点一次：改听 @B（停止监听 @A）」
 *   3. 有目标但没武装（超时复位过）：「换到 @B」，可点——点一下重新武装
 *   4. 其余（还没输入、输入认不出主播）：中性文案，禁用 */
function switchButtonLabel(state, currentStreamer) {
  var cur = currentStreamer ? String(currentStreamer) : "";
  var target = state && state.target ? state.target : "";
  if (target && cur && target.toLowerCase() === cur.toLowerCase()) {
    return { text: "正在监听的就是 @" + cur, disabled: true };
  }
  if (state && state.armed && target) {
    var text = cur
      ? "再点一次：改听 @" + target + "（停止监听 @" + cur + "）"
      : "再点一次：改听 @" + target;
    return { text: text, disabled: false };
  }
  if (target) return { text: "换到 @" + target, disabled: false };
  return { text: "确认换主播", disabled: true };
}

/* Node 测试环境导出；浏览器里作为全局函数被 app.js 使用 */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { SWITCH_ARM_MS, SWITCH_DEBOUNCE_MS, initialSwitchState,
                     armSwitch, isSwitchExpired, expireSwitchState,
                     shouldConfirmSwitch, switchClickAction, switchButtonLabel };
}
