// i18n: done
/* 界面文字的中英双语对（前端）。界面上的中文写成 L("中文", "English")。
   语言由服务端写进 <html lang>（app/i18n.py inject_lang），这里只读不猜——
   pywebview 的 WKWebView / WebView2 里 navigator.language 不是我们要的那个设置。
   切换语言时整页重载，所以一次页面生命周期里语言是常量。

   桌面页的外部脚本里它排第一个（index.html）：app.js 缓存 DOM 元素引用时 applyStatic
   早已跑完，data-en-html 换掉的节点不会变成孤儿。其余纯函数模块在浏览器里直接用这里的
   全局 L/LN；node 测试里它们自己 require 这份文件（见 web/settings-rows.js 顶部）。
   手机页不用这份（手机面只端 VIEWER_FILES 四个文件），web/viewer.js 里自带一份。 */
"use strict";
var UI_LANG = (function () {
  if (typeof globalThis !== "undefined" && globalThis.UI_LANG) return globalThis.UI_LANG;     // node 测试
  var d = typeof document !== "undefined" && document.documentElement;                       // 假 document 可能没有它
  return d && /^en/i.test(d.lang || "") ? "en" : "zh";
})();
function L(zh, en) { return UI_LANG === "en" && en != null ? en : zh; }
function LN(n, zh, one, many) { return UI_LANG === "en" ? (Number(n) === 1 ? one : many) : zh; }
var APP_NAME = L("TikTok 直播同传", "TikTok Live Translator");
var I18N_ATTRS = ["title", "aria-label", "placeholder", "alt"];
function setOwnText(el, text) {            // 只换自己的第一段非空文字节点，旁边的 <svg> 不动
  for (var n = el.firstChild; n; n = n.nextSibling) {
    if (n.nodeType === 3 && n.nodeValue.trim()) { n.nodeValue = text; return; }
  }
  el.appendChild(document.createTextNode(text));
}
function applyStatic(root) {
  if (UI_LANG !== "en" || !root || typeof root.querySelectorAll !== "function") return;   // 中文：不碰 DOM
  var els = root.querySelectorAll("[data-en],[data-en-html],[data-en-title],[data-en-aria-label],[data-en-placeholder],[data-en-alt]");
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    if (el.hasAttribute("data-en-html")) el.innerHTML = el.getAttribute("data-en-html");  // 只来自我们自己的 HTML
    else if (el.hasAttribute("data-en")) setOwnText(el, el.getAttribute("data-en"));
    for (var j = 0; j < I18N_ATTRS.length; j++) {
      var v = el.getAttribute("data-en-" + I18N_ATTRS[j]);
      if (v != null) el.setAttribute(I18N_ATTRS[j], v);
    }
  }
}
// i18n-done 撤掉 style.css 里英文页的防闪（首帧先不显示，免得闪一下中文）。
// 假 document（node 测试）可能没有 documentElement 或 classList：什么都不做，也不抛
if (typeof document !== "undefined" && document.documentElement) {
  applyStatic(document);
  if (document.documentElement.classList) document.documentElement.classList.add("i18n-done");
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = { L: L, LN: LN, UI_LANG: UI_LANG, APP_NAME: APP_NAME, applyStatic: applyStatic };
}
