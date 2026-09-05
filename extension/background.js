/* TikTok 直播同传 —— 后台 service worker（MV3，无依赖，ES5 风格）
 *
 * 职责：在用户已登录的 Chrome 里，被动看一眼播放器实际拉取的直播流地址
 * （.flv / .m3u8），记下来，content.js 需要时来问、或主动推给它。
 *
 * 为什么用 chrome.webRequest 而不是往页面里注入脚本去 hook fetch/XHR：
 *  - 不改动页面本身的任何行为，不会跟播放器抢跑或打乱它的时序；
 *  - TikTok 是 SPA，播放器初始化的时机不固定，注入脚本很容易「装晚了」
 *    错过第一次请求；webRequest 是浏览器网络层面的旁路监听，跟页面脚本
 *    加载顺序无关，从 service worker 启动那一刻就在生效；
 *  - 不需要 "world": "MAIN" 之类更高权限的注入方式。
 */
(function () {
  "use strict";

  // tabId -> {url: string, ts: number}　最近一次在该标签页看到的流地址
  var lastByTab = {};

  var STREAM_URL_RE = /\.flv(\?|$)|\.m3u8(\?|$)/i;

  var CDN_URL_PATTERNS = [
    "https://*.tiktok.com/*",
    "https://*.tiktokcdn.com/*",
    "https://*.tiktokcdn-us.com/*",
    "https://*.tiktokcdn-eu.com/*",
    "https://*.tiktokv.com/*",
    "https://*.byteoversea.com/*"
  ];

  function handleBeforeRequest(details) {
    if (details.tabId < 0) return;               // 不是标签页发起的请求（比如预取），跟丢也没用
    if (!STREAM_URL_RE.test(details.url)) return;

    lastByTab[details.tabId] = { url: details.url, ts: Date.now() };

    // content.js 不一定在监听（比如还没跑到注册 onMessage 那一步），
    // 这种「没有接收方」的报错是预期内的，吞掉即可——反正 lastByTab 已经记下了，
    // 之后 content.js 主动来问（tlt_get_stream_url）也能拿到。
    try {
      chrome.tabs.sendMessage(details.tabId, { type: "tlt_stream_url", url: details.url }, function () {
        void chrome.runtime.lastError; // 读一下以清掉「未处理的错误」控制台告警，不需要额外处理
      });
    } catch (e) { /* noop */ }
  }

  chrome.webRequest.onBeforeRequest.addListener(
    handleBeforeRequest,
    { urls: CDN_URL_PATTERNS, types: ["xmlhttprequest", "media", "other", "websocket"] }
  );

  chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    if (!msg || msg.type !== "tlt_get_stream_url") return false;
    var tabId = sender && sender.tab && sender.tab.id;
    var rec = (tabId !== undefined && tabId !== null) ? lastByTab[tabId] : null;
    sendResponse({ url: rec ? rec.url : null });
    return false; // 同步回复，不需要保持消息通道开着
  });

  chrome.tabs.onRemoved.addListener(function (tabId) {
    delete lastByTab[tabId];
  });
})();
