/* 字幕跟随最新的判定逻辑。

   单独成文件是为了可测：这里的 bug 只在「页面不可见」时出现，用浏览器很难
   稳定复现，而它的后果是用户离开一会儿回来后字幕停在旧位置。

   核心：「跟随最新」是一个**用户意图**，不是每次都从几何量现算的结论。
   页面不可见时 scrollHeight 与 clientHeight 都是 0，此时
     - `scrollTop = scrollHeight` 是给 0 赋 0，空操作
     - `0 - 0 - 0 < 120` 又让「是否在底部」看起来成立
   两者叠加，离开期间到达的每一条字幕都没能滚动，且没有任何机制事后补上。 */

var FOLLOW_SLACK_PX = 120;   // 距底部多少像素以内仍算「在跟随」

/* 元素是否处于可测量状态。不可渲染时浏览器一律返回 0，
   这种 0 不能当成真实的几何量来用。 */
function isMeasurable(box) {
  return !!(box && box.clientHeight);
}

function atBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight < FOLLOW_SLACK_PX;
}

/* 一次 scroll 事件之后，跟随意图应该变成什么。

   userDriven 区分「用户自己滚的」和「别的原因导致的滚动」，这一点是必需的：
   元素从不可渲染恢复时，浏览器会把 scrollTop 重置为 0 并抛出 scroll 事件。
   若不加区分，那次重置会被读成「用户翻到了顶部」，跟随就此关闭，此后再也
   不会自动滚到最新——而这正是要修的症状本身。

   规则：
     - 不可测量：沿用原意图，不猜
     - 滚到了底部：一律恢复跟随（无论谁滚的，结果都是用户在看最新）
     - 没在底部：只有用户自己滚上去才停止跟随 */
function nextFollowing(box, previous, userDriven) {
  if (!isMeasurable(box)) return previous;
  if (atBottom(box)) return true;
  return userDriven ? false : previous;
}

/* 处于跟随状态、可测量、但实际不在底部——说明有滚动请求落空了，需要补滚。 */
function needsResync(box, following) {
  if (!following || !isMeasurable(box)) return false;
  return box.scrollHeight - box.scrollTop - box.clientHeight >= FOLLOW_SLACK_PX;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { isMeasurable, atBottom, nextFollowing, needsResync,
                     FOLLOW_SLACK_PX };
}
