// 桌面页（web/index.html + web/app.js）在 node:vm 里跑起来的假 DOM 脚手架，供 tests/history-clear.test.mjs 用。
// 不是测试文件（不以 .test.mjs 结尾，`node --test tests/*.test.mjs` 不会把它当用例跑）。
//
// 从 tests/app.en.test.mjs 的脚手架复制而来，改动只有两处：runDesktop(lang) 既能跑英文页也能跑中文页
// （app.en.test.mjs 固定英文，translate-no.test.mjs 固定中文；这里要在同一组用例里两种语言各走一遍），
// 以及全部导出。假 DOM 的接口、不认识的选择器就抛错的规矩都与那边相同。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// Windows 的 Git 默认 core.autocrlf=true，检出的是 CRLF：统一成 \n
const web = (name) =>
  readFileSync(new URL("../web/" + name, import.meta.url), "utf8").replace(/\r\n/g, "\n");

// 与 app/i18n.py 的 CJK、tests/i18n_dom/scan.js 同一个字符范围（含中文标点与全角符号）
export const CJK = /[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/;

// 这些节点的字是纯函数模块算的（web/live-ui.js、switch.js、settings-rows.js、alerts.js、
// session-divider.js、brand.js），不是 app.js 自己写的字面量。#jump-latest / #comment-jump 不在其中：
// 它们的字虽然也由 live-ui.js 算，但写进页面的是 app.js，英文页漏了一句中文要能被查出来
const MODULE_TEXT = {
  ids: new Set(["share-btn-text", "share-btn-count", "connect-hint-text", "switch-confirm",
                "watch-mode", "watch-desc", "watch-state", "sc-summary", "engine-active",
                "alert-note", "lang-summary", "ui-lang-system"]),
  classes: new Set(["alert-tier", "session-sep"]),
  optionsOf: new Set(["brand-select", "switch-brand-select"]),   // buildBrandOptionList 的「不限（默认）」
};

// ---- 假 DOM（与 translate-no.test.mjs 同一套接口，外加记下脚本写过哪些属性） ----------------------
export class FakeText {
  constructor(v) { this.nodeType = 3; this.nodeValue = v == null ? "" : String(v); this.parentNode = null; }
  get textContent() { return this.nodeValue; }
}
export class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.nodeType = 1; this.childNodes = []; this.parentNode = null;
    this.attrs = new Map(); this.written = new Set(); this.cls = []; this.dataset = {}; this.style = {};
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
  setAttribute(k, v) { this.attrs.set(k, String(v)); this.written.add(k); }   // 记下脚本写过哪些属性
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
// 属性：值可能用单引号（#brand-empty-hint 的 data-en-html 里面就有带双引号的 <button id="…">）
const ATTR = /([\w-]+)=(?:"([^"]*)"|'([^']*)')/g;

// 按 index.html 的顺序在一个新的 vm 上下文里跑桌面页的脚本。带 id 的元素照页面源码建好
// （标签名和属性与真页面一致；静态属性不算脚本写的），不建树：脚本只经 getElementById 拿它们
// 每个页面都在自己的新上下文里跑一遍这些脚本，但脚本只编译一次。以前每个页面都把 app.js（3000 多行）
// 重新编译一遍：守卫的页面用例加进来以后，整组 `node --test tests/*.test.mjs` 并行跑时，这个文件的子进程
// 约三成概率在 V8 的垃圾回收里崩掉（Node 24.14.1，SIGSEGV，退出码 1，单跑从不崩）；只编译一次后连跑 40 次没有再崩。
const scriptCache = new Map();
function compiled(name) {
  if (!scriptCache.has(name)) scriptCache.set(name, new vm.Script(web(name), { filename: "web/" + name }));
  return scriptCache.get(name);
}

export function runDesktop(lang = "en") {
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
    el.written.clear();
    byId.set(el.id, el);
  }
  // 绊线：单引号属性值里带着 <button id="…"> 的那个 span 以前会被里面的 id 顶掉
  assert.ok(byId.has("brand-empty-hint"), "假 DOM 漏建了 #brand-empty-hint");
  const root = new FakeEl("html");
  root.setAttribute("lang", lang === "en" ? "en" : "zh-CN");
  root.lang = lang === "en" ? "en" : "zh-CN";                                  // i18n.js 读的是 documentElement.lang
  const docListeners = {};                          // document 上的监听（visibilitychange 等），docFire() 触发
  const document = {
    documentElement: root, body: new FakeEl("body"), title: "TikTok Live Translator", hidden: false,
    visibilityState: "visible",
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => new FakeEl(tag),
    createElementNS: (_, tag) => new FakeEl(tag),
    createTextNode: (v) => new FakeText(v),
    createRange: () => ({ selectNodeContents() {} }),
    addEventListener: (type, cb) => { (docListeners[type] = docListeners[type] || []).push(cb); },
    querySelector: () => null, querySelectorAll: () => [], hasFocus: () => true,
  };
  const sockets = [];
  const sent = [];                                   // 页面发给后端的消息（JSON 解开）
  function FakeWebSocket(url) { this.url = url; this.readyState = 1; sockets.push(this); }
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.prototype.send = function (data) { sent.push(JSON.parse(data)); };
  FakeWebSocket.prototype.close = function () {};
  const storage = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) }; };
  const timers = [];
  const frames = [];                                 // requestAnimationFrame 排下的回调，nextFrame() 才跑
  const winListeners = {};                           // window 上的监听（focus / resize），winFire() 触发
  const confirms = [];
  let confirmAnswer = false;
  let now = 1759000000000;
  const sandbox = {
    document, console, WebSocket: FakeWebSocket,
    location: { host: "127.0.0.1:0", protocol: "http:", hash: "", reload() {} },
    navigator: {}, localStorage: storage(), sessionStorage: storage(),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: (fn) => { frames.push(fn); return 0; },
    getComputedStyle: () => ({ getPropertyValue: () => "17px" }),
    getSelection: () => ({ removeAllRanges() {}, addRange() {} }),
    addEventListener: (type, cb) => { (winListeners[type] = winListeners[type] || []).push(cb); },
    removeEventListener() {},
    confirm: (t) => { confirms.push(String(t)); return confirmAnswer; },
    __now: () => now,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext("Date.now = function () { return __now(); };", sandbox);
  for (const name of scripts) compiled(name).runInContext(sandbox);
  assert.equal(vm.runInContext("UI_LANG", sandbox), lang === "en" ? "en" : "zh",
               "i18n.js 应该从 <html lang> 读到 " + lang);
  assert.equal(sockets.length, 1, "页面加载时应该连一次 WebSocket");
  const ws = () => sockets[sockets.length - 1];
  ws().onopen();
  return {
    el: (id) => byId.get(id),
    all: () => [...byId.values()],
    document, confirms, sandbox, sent,
    push: (msg) => ws().onmessage({ data: JSON.stringify(msg) }),
    offline: () => { ws().readyState = 3; },
    online: () => { ws().readyState = 1; },
    closeSocket: () => ws().onclose(),
    answerConfirm: (v) => { confirmAnswer = v; },
    advance: (ms) => { now += ms; },
    winFire: (type) => (winListeners[type] || []).slice().forEach((cb) => cb({ type })),
    // document 上的事件：visibilitychange 之前先改 document.visibilityState
    docFire: (type) => (docListeners[type] || []).slice().forEach((cb) => cb({ type, target: document })),
    // 跑完当前排着的那一帧（回调里再排的留到下一次）
    nextFrame: () => frames.splice(0).forEach((fn) => fn()),
    // 触发某个时长的定时器（按注册顺序，每个只触发一次）
    fire: (ms) => {
      for (const t of timers.slice()) if (t.fn && t.ms === ms) { const fn = t.fn; t.fn = null; fn(); }
    },
  };
}
// 页面上剩下的中文：app.js 写出来的文字节点、写过的属性、confirm 框、document.title
export function leftoverChinese(page) {
  const out = new Set();
  const label = (el) => el.tagName.toLowerCase() + (el.id ? "#" + el.id : el.cls.map((c) => "." + c).join(""));
  const moduleText = (el) => MODULE_TEXT.ids.has(el.id) || el.cls.some((c) => MODULE_TEXT.classes.has(c))
    || (el.tagName === "OPTION" && el.parentNode && MODULE_TEXT.optionsOf.has(el.parentNode.id));
  function walk(el, where) {
    if (moduleText(el)) return;
    for (const k of el.written) {
      const v = el.attrs.get(k);
      if (v != null && CJK.test(v)) out.add(where + " @" + k + ": " + v);
    }
    for (const n of el.childNodes) {
      if (n.nodeType === 3 && CJK.test(n.nodeValue)) out.add(where + ": " + n.nodeValue);
      else if (n.nodeType === 1) walk(n, where + " > " + label(n));
    }
  }
  for (const el of page.all()) walk(el, label(el));
  for (const text of page.confirms) if (CJK.test(text)) out.add("confirm: " + text);
  if (CJK.test(page.document.title)) out.add("document.title: " + page.document.title);
  return [...out];
}

