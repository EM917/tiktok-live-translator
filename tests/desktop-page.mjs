// 桌面页（web/index.html + 它按顺序载入的脚本，最后是 app.js）在 node:vm 里真跑一遍的假页面，
// 中英文两种界面都能起。tests/live-bar.test.mjs 用它。不是测试文件（不以 .test.mjs 结尾，
// `node --test tests/*.test.mjs` 不会把它当用例跑）。
//
// 和 tests/app.en.test.mjs 里的那份同一个思路（那份只起英文页、带查漏中文的断言，这份中英都起，
// 外加：<html> 的 style 能记下 setProperty 写过的 CSS 变量、setInterval 记下来由测试手动拨、
// localStorage 能预置）。app.js 是个顶层就摸 DOM 的大 IIFE，没法 require：按 index.html 的
// 顺序载入前面那串脚本，带 id 的元素照页面源码建好（标签名和属性与真页面一致，不建树）。
// 假 DOM 只实现用得到的接口；不认识的选择器直接抛错，免得静默走偏。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// Windows 的 Git 默认 core.autocrlf=true，检出的是 CRLF：统一成 \n
export const web = (name) =>
  readFileSync(new URL("../web/" + name, import.meta.url), "utf8").replace(/\r\n/g, "\n");

class FakeText {
  constructor(v) { this.nodeType = 3; this.nodeValue = v == null ? "" : String(v); this.parentNode = null; }
  get textContent() { return this.nodeValue; }
}
export class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.nodeType = 1; this.childNodes = []; this.parentNode = null;
    this.attrs = new Map(); this.cls = []; this.dataset = {}; this.style = {};
    this.listeners = {}; this.value = ""; this.disabled = false; this.checked = false; this.selectedIndex = 0;
    this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0; this.offsetWidth = 0;
  }
  get className() { return this.cls.join(" "); }
  set className(v) { this.cls = String(v).split(/\s+/).filter(Boolean); }
  get classList() {
    const el = this;
    const set = (c, on) => { el.cls = el.cls.filter((x) => x !== c); if (on) el.cls.push(c); };
    return {
      add: (...cs) => cs.forEach((c) => set(c, true)),
      remove: (...cs) => cs.forEach((c) => set(c, false)),
      toggle: (c, on) => { const v = on === undefined ? !el.cls.includes(c) : !!on; set(c, v); return v; },
      contains: (c) => el.cls.includes(c),
    };
  }
  get id() { return this.attrs.get("id") || ""; }
  get title() { return this.attrs.get("title") || ""; }
  set title(v) { this.setAttribute("title", v); }
  get placeholder() { return this.attrs.get("placeholder") || ""; }
  set placeholder(v) { this.setAttribute("placeholder", v); }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  hasAttribute(k) { return this.attrs.has(k); }
  removeAttribute(k) { this.attrs.delete(k); }
  appendChild(c) { return this.insertBefore(c, null); }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode.removeChild(c);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i === -1) this.childNodes.push(c); else this.childNodes.splice(i, 0, c);
    c.parentNode = this;
    return c;
  }
  removeChild(c) { this.childNodes = this.childNodes.filter((n) => n !== c); c.parentNode = null; return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] || null; }
  get options() { return this.children.filter((c) => c.tagName === "OPTION"); }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) { this.childNodes = []; if (v != null && String(v)) this.appendChild(new FakeText(v)); }
  set innerHTML(v) { this.childNodes = []; if (v) this.childNodes.push({ nodeType: 99, textContent: "", parentNode: this }); }
  addEventListener(type, cb) { (this.listeners[type] = this.listeners[type] || []).push(cb); }
  removeEventListener() {}
  fire(type) { (this.listeners[type] || []).slice().forEach((cb) => cb({ target: this, key: "", preventDefault() {} })); }
  click() { if (!this.disabled) this.fire("click"); }
  focus() {}
  getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
  descendants() { return this.children.flatMap((c) => [c, ...c.descendants()]); }
  querySelectorAll(sel) { return this.descendants().filter((el) => matches(el, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
function matches(el, sel) {
  return String(sel).split(",").some((raw) => {
    const s = raw.trim();
    let m;
    if ((m = /^\.([\w-]+)$/.exec(s))) return el.cls.includes(m[1]);
    if ((m = /^\[data-([\w-]+)="([^"]*)"\]$/.exec(s))) {
      return String(el.dataset[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())]) === m[2];
    }
    if (s === "input[type=checkbox]:checked") return el.tagName === "INPUT" && el.checked;
    throw new Error("假 DOM 不认识这个选择器：" + s);
  });
}

const START_TAG = /<([a-z]+)\b((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
const ATTR = /([\w-]+)=(?:"([^"]*)"|'([^']*)')/g;

// 脚本只编译一次（app.js 三千多行；每个页面都重新编译会让并行跑的 node 子进程不稳，见 app.en.test.mjs）
const scriptCache = new Map();
function compiled(name) {
  if (!scriptCache.has(name)) scriptCache.set(name, new vm.Script(web(name), { filename: "web/" + name }));
  return scriptCache.get(name);
}

export const T = 1759000000;                    // 一个固定的 Unix 秒，字幕的 ts 都围着它
export const ROOM = "https://www.tiktok.com/@luna_demo/live";

/**
 * 起一个桌面页。lang: "zh" 或 "en"；stored: 预置的 localStorage（{键: 值}）；
 * startMs: 假时钟起点（毫秒）。返回的对象：
 *   el(id) / all()      带 id 的元素
 *   push(msg)           模拟服务端推来一条 WebSocket 消息
 *   sent                页面发给服务端的消息（JSON 解开）
 *   advance(ms)         拨快假时钟；tick(ms) 触发所有周期为 ms 的 setInterval；fire(ms) 同理触发 setTimeout
 *   cssVars             <html> 上 setProperty 写过的 CSS 变量（{名: 值}）
 *   store               localStorage 的当前内容（Map）
 *   disconnect()/connect()  切断 / 恢复发送
 */
export function loadDesktop({ lang = "en", stored = {}, startMs = T * 1000 } = {}) {
  const html = web("index.html");
  const scripts = [...html.matchAll(/<script src="\/static\/([\w.-]+)\?v=\d+"><\/script>/g)].map((m) => m[1]);
  assert.equal(scripts[0], "i18n.js");
  assert.equal(scripts[scripts.length - 1], "app.js");
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
  root.lang = lang;                              // i18n.js 读的是 documentElement.lang
  root.style = {
    setProperty: (k, v) => { cssVars[k] = v; },
    getPropertyValue: (k) => cssVars[k] || "",
  };
  const document = {
    documentElement: root, body: new FakeEl("body"), title: "TikTok Live Translator", hidden: false,
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => new FakeEl(tag),
    createElementNS: (_, tag) => new FakeEl(tag),
    createTextNode: (v) => new FakeText(v),
    createRange: () => ({ selectNodeContents() {} }),
    addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], hasFocus: () => true,
  };
  const sockets = [];
  const sent = [];
  function FakeWebSocket(url) { this.url = url; this.readyState = 1; sockets.push(this); }
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.prototype.send = function (data) { sent.push(JSON.parse(data)); };
  FakeWebSocket.prototype.close = function () {};
  const store = new Map(Object.entries(stored));
  const storage = () => ({ getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) });
  const timers = [];
  const intervals = [];
  let now = startMs;
  const sandbox = {
    document, console, WebSocket: FakeWebSocket,
    location: { host: "127.0.0.1:0", protocol: "http:", hash: "", reload() {} },
    navigator: {}, localStorage: storage(), sessionStorage: storage(),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
    clearInterval() {}, requestAnimationFrame: () => 0,
    getComputedStyle: () => ({ getPropertyValue: () => "34px" }),
    getSelection: () => ({ removeAllRanges() {}, addRange() {} }),
    addEventListener() {}, removeEventListener() {}, confirm: () => false,
    __now: () => now,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext("Date.now = function () { return __now(); };", sandbox);
  for (const name of scripts) compiled(name).runInContext(sandbox);
  assert.equal(vm.runInContext("UI_LANG", sandbox), lang, "i18n.js 应该从 <html lang> 读到 " + lang);
  assert.equal(sockets.length, 1, "页面加载时应该连一次 WebSocket");
  const ws = () => sockets[sockets.length - 1];
  ws().onopen();
  return {
    el: (id) => byId.get(id),
    all: () => [...byId.values()],
    push: (msg) => ws().onmessage({ data: JSON.stringify(msg) }),
    sent, cssVars, store, root,
    advance: (ms) => { now += ms; },
    now: () => now,
    tick: (ms) => { intervals.filter((t) => t.ms === ms).forEach((t) => t.fn()); },
    fire: (ms) => { for (const t of timers.slice()) if (t.fn && t.ms === ms) { const fn = t.fn; t.fn = null; fn(); } },
    // 断开：页面看到的 readyState 不是 OPEN，send() 返回 false，什么也发不出去
    disconnect: () => { ws().readyState = 3; },
    connect: () => { ws().readyState = 1; },
  };
}

/** 一条直播中的 hello：状态 live，别的什么都没有（这个文件只关心大字幕、页脚和字号）。 */
export function hello(page, config = {}) {
  page.push({ type: "hello", config: Object.assign({
    status: { state: "live", detail: "" }, room_url: ROOM,
    alerts_enabled: false, alerts_session: { session: "s1", streamer: "luna_demo", total: 0 },
    brand_options: [], brands: {}, active_brand: null, recent_rooms: [],
  }, config) });
}

/** 形状同 Pipeline 的 caption 广播：先是「翻译中」，译文由 caption_update 补上。 */
export function caption(id, original, extra = {}) {
  return Object.assign({ type: "caption", id, ts: T, original, translated: null, translate_state: "pending",
                         src_lang: "es", target_lang: "zh-CN", asr_ms: 1100, e2e_ms: 2300, segment_ms: 6000 }, extra);
}
