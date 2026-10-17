// 浮窗页（web/float.html + 它按顺序载入的脚本，最后是 float.js）在 node:vm 里真跑一遍的假页面，
// 中英文两种界面都能起。tests/float.test.mjs 和 tests/float.en.test.mjs 用它。不是测试文件
// （不以 .test.mjs 结尾，`node --test tests/*.test.mjs` 不会把它当用例跑）。
//
// 同 tests/desktop-page.mjs 的思路：带 id 的元素照页面源码建好（标签名和属性与真页面一致，不建树），
// 脚本只编译一次。假 WebSocket 不许发任何东西——浮窗页只读，谁调 send 谁就是 bug。
import assert from "node:assert/strict";
import vm from "node:vm";
import { FakeEl, web } from "./desktop-page.mjs";

const START_TAG = /<([a-z]+)\b((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
const ATTR = /([\w-]+)=(?:"([^"]*)"|'([^']*)')/g;

const scriptCache = new Map();
function compiled(name) {
  if (!scriptCache.has(name)) scriptCache.set(name, new vm.Script(web(name), { filename: "web/" + name }));
  return scriptCache.get(name);
}

export const T = 1759000000;

/**
 * 起一个浮窗页。lang: "zh" 或 "en"；search: 地址里的查询串（"?solid=1"）。返回：
 *   el(id)         带 id 的元素
 *   push(msg)      模拟服务端推来一条 WebSocket 消息
 *   sockets / urls 建过的连接和它们的地址
 *   closeSocket()  模拟连接断开（页面会安排重连）
 *   runTimers()    把排队的 setTimeout 都跑掉（重连）
 *   tick()         每个 setInterval 跑一次；fireWindow(type) 触发 window 上的事件（resize）
 *   root / cssVars <html> 和它上面 setProperty 写过的 CSS 变量
 *   comments()     弹幕列表里现在的 .cmt-item
 */
export function loadFloat({ lang = "en", search = "" } = {}) {
  const html = web("float.html");
  const scripts = [...html.matchAll(/<script src="\/static\/([\w.-]+)\?v=\d+"><\/script>/g)].map((m) => m[1]);
  assert.equal(scripts[0], "i18n.js");
  assert.equal(scripts[scripts.length - 1], "float.js");
  const byId = new Map();
  for (const m of html.replace(/<!--[\s\S]*?-->/g, "").matchAll(START_TAG)) {
    if (!/\sid="/.test(m[2])) continue;
    const el = new FakeEl(m[1]);
    for (const a of m[2].matchAll(ATTR)) {
      const value = a[2] !== undefined ? a[2] : a[3];
      if (a[1] === "class") el.className = value; else el.setAttribute(a[1], value);
    }
    byId.set(el.id, el);
  }
  const cssVars = {};
  const root = new FakeEl("html");
  root.lang = lang;
  root.style = {
    setProperty: (k, v) => { cssVars[k] = v; },
    getPropertyValue: (k) => cssVars[k] || "",
  };
  const document = {
    documentElement: root, body: new FakeEl("body"), title: "", hidden: false,
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => new FakeEl(tag),
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
  };
  const sockets = [];
  function FakeWebSocket(url) { this.url = url; this.readyState = 1; sockets.push(this); }
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.prototype.send = function () { throw new Error("浮窗页是只读的，不该发任何消息"); };
  FakeWebSocket.prototype.close = function () {};
  const timers = [];
  const intervals = [];
  const winListeners = {};
  const sandbox = {
    document, console, WebSocket: FakeWebSocket,
    location: { host: "127.0.0.1:0", protocol: "http:", search },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {},
    setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
    clearInterval() {},
    addEventListener: (type, fn) => { (winListeners[type] = winListeners[type] || []).push(fn); },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const name of scripts) compiled(name).runInContext(sandbox);
  assert.equal(vm.runInContext("UI_LANG", sandbox), lang, "i18n.js 应该从 <html lang> 读到 " + lang);
  assert.equal(sockets.length, 1, "页面加载时应该连一次 WebSocket");
  const ws = () => sockets[sockets.length - 1];
  ws().onopen();
  return {
    el: (id) => byId.get(id),
    all: () => [...byId.values()],
    push: (msg) => ws().onmessage({ data: JSON.stringify(msg) }),
    sockets, urls: () => sockets.map((s) => s.url), root, cssVars, timers,
    comments: () => byId.get("f-comments").children,
    closeSocket: () => ws().onclose(),
    runTimers: () => { for (const t of timers.splice(0)) t.fn(); },
    intervals,
    tick: () => { for (const t of intervals) t.fn(); },          // 每个 setInterval 跑一次
    fireWindow: (type) => { for (const fn of winListeners[type] || []) fn({ type }); },
    reopen: () => { const s = ws(); if (s.onopen) s.onopen(); },
  };
}

/** 形状同 Pipeline 的 caption 广播：先是「翻译中」，译文由 caption_update 补上。 */
export function caption(id, original, extra = {}) {
  return Object.assign({ type: "caption", id, ts: T, original, translated: null, translate_state: "pending",
                         src_lang: "es", target_lang: "zh-CN", asr_ms: 1100, e2e_ms: 2300 }, extra);
}

export function comment(id, user, text, extra = {}) {
  return Object.assign({ type: "comment", id, user, text, ts: T + 1, translated: null, state: "pending" }, extra);
}

/** 一条 hello：状态 live，字号和守卫状态可以覆盖。 */
export function hello(page, config = {}) {
  page.push({ type: "hello", config: Object.assign({
    status: { state: "live", detail: "" }, caption_size: 34, target_lang: "zh-CN",
    guard_state: { enabled: true, weak: false }, guard_marked: {},
  }, config) });
}
