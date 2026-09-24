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

/* 「状态迁移 → 布局动作」的判定。直播状态从 hello/status 消息到达，
   但「停止后页面该长什么样」是一个独立于具体消息形状的问题，抽成纯函数
   才能不靠真开页面去验证每一种迁移组合（同一批事故：大字幕、统计行、
   弹幕面板、回到最新按钮，停止后各自用不同的隐藏条件，改一个漏一个）。

   prevActive：上一次算出的 active（true/false），页面刚加载、还没收到任何
   状态时传 null——这不是「已经在待机」，而是「压根不知道」，两者要分开：
   否则首次到达就是 idle 时会被误判成「没有变化」而跳过进首页的动作。

   state：这一条 status 消息的 state 字段（idle/connecting/live/ended/error/offline）。

   返回 { active, enterHome, exitHome }：
     - active：这条消息之后，「直播中」这件事本身该算 true 还是 false。
     - enterHome：这一步要不要执行「进入首页」的动作（隐藏大字幕/统计行/
       弹幕面板、字幕区回到顶部）。只在从活跃变为非活跃的**那一刻**触发一次，
       同为非活跃的后续状态（如 error → idle）不重复触发。
     - exitHome：这一步要不要执行「离开首页」的动作（字幕跟随恢复到底部）。
       同理只在从非活跃变为活跃的那一刻触发一次。

   offline 是本地 WebSocket 断线重连中的状态，不代表直播本身发生了任何变化——
   断线前是直播中就还是直播中，断线前是待机就还是待机，布局原样不动，
   只有状态点和状态文字该刷新（那部分不归这个函数管）。重连成功后 hello
   里的真实状态会再算一次，到时候该做的动作照样会做，不会因为这次跳过而漏掉。 */
function viewTransition(prevActive, state) {
  if (state === "offline") {
    return { active: prevActive, enterHome: false, exitHome: false };
  }
  var active = state === "live" || state === "connecting";
  return {
    active: active,
    enterHome: !active && prevActive !== false,
    exitHome: active && prevActive !== true,
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { isMeasurable, atBottom, nextFollowing, needsResync,
                     viewTransition, FOLLOW_SLACK_PX };
}
