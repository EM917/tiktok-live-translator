/* 桌面页字幕流里「场次分隔线」的纯文案与判断逻辑：要不要画、画什么字。
 * DOM 部分（找 .cap 节点、插入 div、隐藏大字幕）留在 web/app.js 的
 * renderSessionBreak/insertSessionDivider 里，这里只管分隔线本身该长什么样。
 * 独立成文件以便单元测试：tests/session-divider.test.mjs 用 node:test 钉住
 * 这些规则——app.js 是单个大 IIFE，顶层就摸 DOM，整个文件没法被 Node
 * require。写法和加载方式照 web/switch.js：浏览器里是全局函数，Node 测试
 * 环境走 module.exports。 */

"use strict";

/* 只有页面里已经有字幕卡片时才画分隔线：程序启动后的第一场前面没有任何
 * 卡片，不该凭空多出一条线。session_break 每场都广播（含第一场，见
 * app/pipeline.py 的 _begin_session），「首场不画」是前端自己的判断，
 * 不是后端少发——这条判断丢了会在直播一开始就多出一条多余的分隔线。 */
function shouldRenderSessionDivider(hasExistingCaptions) {
  return !!hasExistingCaptions;
}

function pad2(n) { return (n < 10 ? "0" : "") + n; }

/* 分隔线文案：时间 + 新一场是谁。主播名取不到（streamer 为空串，见
 * _begin_session 的注释「取不到就是空串，不猜不补」）时用「以下为新的一场」
 * 这句通用文案，不编造主播身份。 */
function sessionDividerText(msg) {
  var d = new Date(((msg && msg.ts) || Date.now() / 1000) * 1000);
  var time = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  var who = (msg && msg.streamer) ? ("以下为 @" + msg.streamer) : "以下为新的一场";
  return "── " + time + " " + who + " ──";
}

/* Node 测试环境导出；浏览器里作为全局函数被 app.js 使用 */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { shouldRenderSessionDivider: shouldRenderSessionDivider,
                     sessionDividerText: sessionDividerText };
}
