/* 直播间地址输入归一化 —— 对输入宽容：接受完整链接、带文字的分享内容
 * （挖出其中的链接）、「@用户名」或裸用户名（自动补成直播间地址）。
 * 认不出的输入返回 null，由调用方报错——瞎拼一个地址发给后端只会
 * 换来一条方向完全错误的报错。
 * 独立成文件是为了可测：tests/normalize.test.mjs 用 node:test 钉住这些规则。 */
function normalizeRoomInput(raw) {
  "use strict";
  raw = raw.trim();
  // URL 只含 ASCII：遇到中文/全角标点即截断（分享文本是"快看…/live！超好看"这种）
  var m = raw.match(/https?:\/\/[\x21-\x7e]+/);
  if (m) {
    return m[0].replace(/[!?.,;:)\]'"<>]+$/, "");   // 再剥掉粘在结尾的标点/引号
  }
  var name = raw.replace(/^@/, "");
  var domainLike = /\.(com|net|org|tv|cn|co|io|me|app|xyz|live)$/i.test(name) ||
                   raw.indexOf("/") !== -1;
  if (/^[A-Za-z0-9._]+$/.test(name) && !domainLike) {
    return "https://www.tiktok.com/@" + name + "/live";
  }
  if (raw.indexOf(".") !== -1 || raw.indexOf("/") !== -1) {
    if (!/^https?:\/\//.test(raw)) {
      return "https://" + raw.replace(/^\/+/, "");
    }
    return raw;
  }
  return null;   // 中文昵称、带空格的文字等——不是地址也不是用户名
}

/* Node 测试环境导出；浏览器里作为全局函数被 app.js 使用 */
if (typeof module !== "undefined" && module.exports) {
  module.exports = normalizeRoomInput;
}
