// web/app.js 的英文（spec §13 M2）。
//
// app.js 是一个顶层就摸 DOM 的大 IIFE，没法 require：照 tests/translate-no.test.mjs 的办法放进
// node:vm，按 index.html 的顺序先载入前面那串脚本，只是 <html lang="en">——web/i18n.js 读到它，
// L()/LN() 就交出英文。推一串 WebSocket 消息、点几个按钮、拨一拨定时器和时钟，然后查两件事：
//   1. app.js 自己写到页面上的字（文字节点、它写过的 title/placeholder 等属性、confirm 框、document.title）
//      一个汉字都不剩。漏写 L() 在英文界面上只会露中文，不报错，也不崩——只能这样查；
//   2. 单复数、拼进句子的变量取对了：钉几句关键的英文。
// 不查的：静态 HTML 里的字（index.html 的 data-en*，归 G5；这个假 DOM 本来就不建静态文字），
// 以及纯函数模块算出来的那几处（MODULE_TEXT，它们各有自己的 *.test.mjs）。后端推来的文字
// （status.detail、自检项名……）在这里一律用 ASCII：它们的英文由服务端渲染，归后端的测试。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// Windows 的 Git 默认 core.autocrlf=true，检出的是 CRLF：统一成 \n
const web = (name) =>
  readFileSync(new URL("../web/" + name, import.meta.url), "utf8").replace(/\r\n/g, "\n");

// 与 app/i18n.py 的 CJK、tests/i18n_dom/scan.js 同一个字符范围（含中文标点与全角符号）
const CJK = /[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/;

// 这些节点的字是纯函数模块算的（web/live-ui.js、switch.js、settings-rows.js、alerts.js、
// session-divider.js、brand.js），不是 app.js 自己写的字面量
const MODULE_TEXT = {
  ids: new Set(["share-btn-text", "share-btn-count", "connect-hint-text", "switch-confirm",
                "watch-mode", "watch-desc", "watch-state", "sc-summary", "engine-active",
                "alert-note", "lang-summary", "ui-lang-system"]),
  classes: new Set(["alert-tier", "session-sep"]),
  optionsOf: new Set(["brand-select", "switch-brand-select"]),   // buildBrandOptionList 的「不限（默认）」
};

// ---- 假 DOM（与 translate-no.test.mjs 同一套接口，外加记下脚本写过哪些属性） ----------------------
class FakeText {
  constructor(v) { this.nodeType = 3; this.nodeValue = v == null ? "" : String(v); this.parentNode = null; }
  get textContent() { return this.nodeValue; }
}
class FakeEl {
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
function runDesktop() {
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
  root.setAttribute("lang", "en");
  root.lang = "en";                                  // i18n.js 读的是 documentElement.lang
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
  function FakeWebSocket(url) { this.url = url; this.readyState = 1; sockets.push(this); }
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.prototype.send = function () {};
  FakeWebSocket.prototype.close = function () {};
  const storage = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) }; };
  const timers = [];
  const confirms = [];
  let confirmAnswer = false;
  let now = 1759000000000;
  const sandbox = {
    document, console, WebSocket: FakeWebSocket,
    location: { host: "127.0.0.1:0", protocol: "http:", hash: "", reload() {} },
    navigator: {}, localStorage: storage(), sessionStorage: storage(),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    setInterval: () => 0, clearInterval() {}, requestAnimationFrame: () => 0,
    getComputedStyle: () => ({ getPropertyValue: () => "17px" }),
    getSelection: () => ({ removeAllRanges() {}, addRange() {} }),
    addEventListener() {}, removeEventListener() {},
    confirm: (t) => { confirms.push(String(t)); return confirmAnswer; },
    __now: () => now,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext("Date.now = function () { return __now(); };", sandbox);
  for (const name of scripts) vm.runInContext(web(name), sandbox, { filename: "web/" + name });
  assert.equal(vm.runInContext("UI_LANG", sandbox), "en", "i18n.js 应该从 <html lang> 读到英文");
  assert.equal(sockets.length, 1, "页面加载时应该连一次 WebSocket");
  const ws = () => sockets[sockets.length - 1];
  ws().onopen();
  return {
    el: (id) => byId.get(id),
    all: () => [...byId.values()],
    document, confirms, sandbox,
    push: (msg) => ws().onmessage({ data: JSON.stringify(msg) }),
    offline: () => { ws().readyState = 3; },
    online: () => { ws().readyState = 1; },
    closeSocket: () => ws().onclose(),
    answerConfirm: (v) => { confirmAnswer = v; },
    advance: (ms) => { now += ms; },
    // 触发某个时长的定时器（按注册顺序，每个只触发一次）
    fire: (ms) => {
      for (const t of timers.slice()) if (t.fn && t.ms === ms) { const fn = t.fn; t.fn = null; fn(); }
    },
  };
}

// 页面上剩下的中文：app.js 写出来的文字节点、写过的属性、confirm 框、document.title
function leftoverChinese(page) {
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

const ROOM = "https://www.tiktok.com/@maria.ventas/live";
const OTHER = "https://www.tiktok.com/@otra.tienda/live";
const T = 1759000000;
const GB = 1024 * 1024 * 1024;

function hello(page, extra) {
  page.push({ type: "hello", config: Object.assign({
    status: { state: "live", detail: "" }, room_url: ROOM, alerts_enabled: true,
    alerts_session: { session: "s1", streamer: "maria.ventas", total: 0 },
    brand_options: [{ id: "b1", name: "Bella" }], brands: {}, active_brand: { id: "b1", name: "Bella" },
    recent_rooms: [{ streamer: "maria.ventas", url: ROOM }, { streamer: "otra.tienda", url: OTHER }],
    disk: { free: 50 * GB, items: [
      { id: "hf:x", kind: "hf", label: "mlx-community/whisper", size: 3 * GB, role: "app", note: "" },
      { id: "ollama:y", kind: "ollama", label: "hy-mt2:7b", size: 5 * GB, role: "other", note: "" },
      { id: "hf:z", kind: "hf", label: "whisper-small", size: GB, role: "in_use", note: "" }] },
    selfcheck: { summary: { fail: 1, warn: 1, total: 3 }, checks: [
      { name: "Speech Recognition", level: "pass", detail: "ok" },
      { name: "Noise Reduction", level: "warn", detail: "slow", fix: "Restart the app." },
      { name: "Storage", level: "fail", detail: "full" }] },
    engine: { engine: "deepl", keys: { DEEPL_API_KEY: "…abcd" }, active_label: "DeepL", note: "" },
    viewer: { on: false, note: "" },
    glossary_migration: { count: 3 },
    version: "1.2.3",
    update: { version: "1.3.0", can_auto: true, url: "https://example.invalid/notes" },
  }, extra || {}) });
}

test("查漏本身不瞎：app.js 写出的中文文字、写过的属性（哪怕与静态初值相同）、confirm 框、标题都报", () => {
  const page = runDesktop();
  hello(page);
  assert.deepEqual(leftoverChinese(page), []);
  const staticTitle = page.el("clear-btn").title;               // index.html 的中文初值（英文归 data-en-title）
  assert.match(staticTitle, CJK, "index.html 改了 #clear-btn 的 title，换一个有中文 title 的元素");
  const row = page.document.createElement("div");
  row.appendChild(page.document.createTextNode("正在统计…"));
  page.el("disk-list").appendChild(row);
  page.el("engine-key").placeholder = "粘贴 API 密钥";               // 与 index.html 的初值相同，照样要报
  page.confirms.push("确定删除以下内容？");
  page.document.title = "有新版本";
  assert.deepEqual(leftoverChinese(page).sort(), [
    "confirm: 确定删除以下内容？",
    "div#disk-list > div: 正在统计…",
    "document.title: 有新版本",
    "input#engine-key @placeholder: 粘贴 API 密钥",
  ]);
});

test("英文页：状态胶囊、顶栏标签、横幅都是英文", () => {
  const page = runDesktop();
  hello(page);
  assert.equal(page.el("status-text").textContent, "Live · @maria.ventas");
  assert.equal(page.el("alert-mode-tag").textContent, "Alerts on");
  assert.equal(page.el("active-brand-tag").textContent, "Brand · Bella");
  const states = { connecting: "Connecting… · @maria.ventas", ended: "Stream ended", error: "Error",
                   idle: "Ready", offline: "Reconnecting…" };
  for (const [state, text] of Object.entries(states)) {
    page.push({ type: "status", state, detail: "" });
    assert.equal(page.el("status-text").textContent, text, state);
  }
  // 认不出的输入、连接断开时点开始
  page.el("room-input").value = "hola mundo";
  page.el("start-btn").click();
  assert.match(page.el("status-banner").textContent, /^This isn’t a live link or username\. .*Display names won’t work\.$/);
  page.offline();
  page.el("start-btn").click();
  assert.equal(page.el("status-banner").textContent,
               "Lost connection to the local service. Reconnecting… Click Start again in a moment.");
  page.online();
  // 「开始」发出去一直没有回执：补发一次，再没回执就提示手点
  page.el("room-input").value = "@otra.tienda";
  page.el("start-btn").click();
  page.fire(8000);
  page.fire(8000);
  assert.equal(page.el("status-banner").textContent,
               "The command didn’t go through (connection interrupted). The page reconnected. Click Start again.");
  // 断开 20 秒以上
  page.closeSocket();
  page.advance(21000);
  page.closeSocket();
  assert.match(page.el("status-banner").textContent, /^No connection to the app’s local service for over 20 seconds\. /);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：字幕卡片、重译按钮、翻译失败横幅", () => {
  const page = runDesktop();
  hello(page);
  page.push({ type: "caption", id: 1, ts: T, original: "hola", translate_state: "pending" });
  const card = page.el("history").querySelector(".cap");
  assert.equal(card.querySelector(".trans").textContent, "Translating…");
  const redo = card.querySelector(".redo");
  assert.deepEqual([redo.textContent, redo.title], ["Retranslate", "Retranslate with the most accurate model"]);
  page.push({ type: "caption_update", id: 1, strong_state: "pending" });
  assert.equal(redo.textContent, "Retranslating…");
  page.push({ type: "caption_update", id: 1, strong_state: "failed" });
  assert.equal(redo.textContent, "Couldn’t retranslate");
  page.fire(4000);
  assert.equal(redo.textContent, "Retranslate");
  page.push({ type: "caption", id: 2, ts: T, original: "que tal", translate_state: "dropped" });
  const chips = page.el("history").querySelectorAll(".state-chip").map((c) => c.textContent);
  assert.deepEqual(chips, ["", "Skipped (backlog)"]);
  for (let id = 3; id <= 6; id++) page.push({ type: "caption", id, ts: T, original: "x", translate_state: "failed" });
  assert.equal(page.el("history").querySelectorAll(".state-chip")[2].textContent, "Translation failed");
  assert.match(page.el("status-banner").textContent,
               /^The last several captions couldn’t be translated, so captions show the original text\. /);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：报警头用弯双引号，译不出来的说明按有没有原因分两句", () => {
  const page = runDesktop();
  hello(page);
  page.push({ type: "alert", alert_id: "a1", ts: T, term: "milagro", tier: "exact", streamer: "maria.ventas",
              session: "s1", context: "es un milagro" });
  page.push({ type: "alert", alert_id: "a2", ts: T, term: "cura", tier: "fuzzy", session: "s1", context: "lo cura" });
  const [second, first] = page.el("alert-list").children;
  const head = first.querySelector(".alert-head").children[1].textContent;
  assert.match(head, /^“milagro” \d\d:\d\d:\d\d @maria\.ventas$/);
  assert.equal(first.querySelector(".alert-zh").textContent, "Translating…");
  page.push({ type: "alert_update", alert_id: "a1", why: "timeout" });
  assert.equal(first.querySelector(".alert-zh").textContent, "Couldn’t translate (timeout). See the original above.");
  page.push({ type: "alert_update", alert_id: "a2" });
  assert.equal(second.querySelector(".alert-zh").textContent, "Couldn’t translate. See the original above.");
  page.push({ type: "config", alerts_session: { session: "s2", streamer: "otra.tienda", total: 0 } });
  assert.equal(first.querySelector(".alert-prev").textContent, "Previous");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：弹幕面板的状态与空态、统计行", () => {
  const page = runDesktop();
  hello(page);
  const source = () => [page.el("comment-source").textContent, page.el("comment-empty").textContent];
  assert.deepEqual(source(), ["Not connected", "Comments connect when the stream starts."]);
  page.push({ type: "comment_source", backend: "connected" });
  assert.deepEqual(source(), ["Connected", "Connected. Waiting for comments…"]);
  page.push({ type: "comment_source", backend: "connecting" });
  assert.deepEqual(source(), ["Connecting…", "Connecting…"]);
  page.push({ type: "comment_source", backend: "disconnected" });
  assert.deepEqual(source(), ["Disconnected. Reconnecting…", "Disconnected. Reconnecting…"]);
  page.push({ type: "comment", id: "c1", ts: T, user: "Ana", text: "hola", state: "pending" });
  assert.equal(page.el("comment-list").querySelector(".cmt-zh").textContent, "Translating…");
  page.push({ type: "stats", e2e: {}, asr: { p50: 1100 }, translate: { p50: 2000 }, segment: { p50: 800 },
              detect_worst: { p50: 1900, p95: 3400 }, audio_backlog_sec: 5, audio_segments_dropped: 2,
              asr_overruns: 1, translation_jobs_dropped: 3, asr_queue_depth: 1, translation_queue_depth: 2 });
  assert.equal(page.el("stats-line").textContent,
               "Alerts ≤1.9s (P95 3.4s) · Segment 0.8s + ASR 1.1s · Translation +2.0s · Backlog 5s · " +
               "Dropped 2 · ASR timeouts 1 · Skipped 3 · Queue 1/2");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：磁盘列表、删除按钮的单复数、删除确认框", () => {
  const page = runDesktop();
  hello(page);
  assert.equal(page.el("disk-summary").textContent, "50.0 GB available · Models and logs 9.0 GB");
  const roles = page.el("disk-list").querySelectorAll(".disk-role").map((n) => n.textContent);
  assert.deepEqual(roles, ["This app", "Not this app", "In use"]);
  const del = page.el("disk-delete");
  assert.equal(del.textContent, "Delete Selected");
  const boxes = page.el("disk-list").querySelectorAll(".disk-item").map((row) => row.children[0]);
  boxes[0].checked = true;
  boxes[0].fire("change");
  assert.equal(del.textContent, "Delete 1 Item (3.0 GB)");
  boxes[1].checked = true;
  boxes[1].fire("change");
  assert.equal(del.textContent, "Delete 2 Items (8.0 GB)");
  del.click();
  assert.equal(page.confirms.pop(),
               "Delete these items? This can’t be undone.\n\n· mlx-community/whisper (3.0 GB)\n· hy-mt2:7b (5.0 GB)");
  page.answerConfirm(true);
  del.click();
  assert.equal(del.textContent, "Deleting…");
  page.el("disk-head").click();
  assert.equal(page.el("disk-list").textContent, "Calculating…");
  page.push({ type: "disk", free: null, items: [] });
  assert.deepEqual([page.el("disk-summary").textContent, page.el("disk-list").textContent],
                   ["Models and logs 1 KB", "No models or logs to manage."]);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：手机同看卡片各状态，换链接确认", () => {
  const page = runDesktop();
  hello(page);
  const desc = () => page.el("share-desc").textContent;
  assert.equal(page.el("share-state").textContent, "Off");
  assert.equal(desc(), "When this is on, phones on the same Wi-Fi can scan to view captions and alerts. " +
                       "They can only view, not control the app.");
  page.push({ type: "viewer", on: true, note: "" });                     // 刚打开：先提示系统权限框
  assert.equal(page.el("share-state").textContent, "On");
  assert.match(desc(), /^The first time you turn this on, macOS may ask .* Choose Allow\.$/);
  page.push({ type: "viewer", on: true, note: "" });                     // 没读到局域网地址
  assert.equal(desc(), "On, but the app couldn’t find this computer’s local network address.");
  const url = "http://192.168.1.5:8766/#k=fake";
  page.push({ type: "viewer", on: true, ip: "192.168.1.5", url, viewers: 2, max_viewers: 12, note: "" });
  assert.equal(desc(), "On. On a phone connected to the same Wi-Fi, scan the QR code or open: " + url);
  assert.equal(page.el("share-count").textContent, "2 watching (limit 12)");
  assert.equal(page.el("share-url").title,
               "This link contains an access key. Treat it like a password. Anyone who has it can see captions and alerts.");
  assert.equal(page.el("share-note").textContent, "Couldn’t create the QR code. Type the address above on the phone.");
  page.push({ type: "viewer", on: true, ip: "10.0.0.7", url: "http://10.0.0.7:8766/#k=fake", viewers: 1, note: "" });
  assert.equal(page.el("share-addr-changed").textContent,
               "This computer’s address changed from 192.168.1.5 to 10.0.0.7. Phones need to scan the new QR code.");
  assert.equal(page.el("share-count").textContent, "1 watching (limit 12)");
  page.el("share-rotate").click();
  assert.equal(page.confirms.pop(), "Changing the link disconnects the phones watching now (1). " +
                                    "They’ll need to scan the new QR code. Continue?");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：换主播浮层、最近直播间、清除记录", () => {
  const page = runDesktop();
  hello(page);
  const chips = page.el("recent-list").children;
  assert.deepEqual(chips.map((c) => c.title), ["Start with @maria.ventas", "Start with @otra.tienda"]);
  const clear = page.el("recent-clear");
  clear.click();
  assert.equal(clear.textContent, "Click Again to Clear");
  page.fire(6000);
  assert.equal(clear.textContent, "Clear History");

  page.el("switch-btn").click();
  assert.equal(page.el("switch-sub").textContent,
               "Now monitoring @maria.ventas. Nothing changes until you confirm.");
  assert.equal(page.el("switch-hint").textContent,
               "No captions from either streamer until the new streamer starts speaking.");
  const [current, other] = page.el("switch-recent-list").children;
  assert.equal(current.textContent, "@maria.ventas (current)");
  assert.equal(other.title, "Select @otra.tienda. You’ll still need to confirm.");
  const input = page.el("switch-input");
  input.value = "@otra.tienda";
  input.fire("input");
  assert.equal(page.el("switch-hint").textContent,
               "No captions from either streamer until @otra.tienda starts speaking.");
  page.advance(1000);                                                    // 过了防抖，这一下算确认
  page.offline();
  page.el("switch-confirm").click();
  assert.equal(page.el("switch-error").textContent,
               "Lost connection to the local service. Reconnecting… Try again in a moment.");
  page.online();
  page.el("room-input").value = "";                                      // 取不到正在听的主播名
  page.el("switch-btn").click();                                         // 收起
  page.el("switch-btn").click();                                         // 重新打开
  assert.equal(page.el("switch-panel").classList.contains("hidden"), false);
  assert.equal(page.el("switch-sub").textContent, "Monitoring continues until you confirm.");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：更新提示、版本号旁的一次性提示、迁移旧词表", () => {
  const page = runDesktop();
  hello(page);
  const note = () => page.el("app-version").textContent;
  assert.equal(page.el("update-text").textContent, "Version 1.3.0 is available");
  assert.equal(page.el("update-link").textContent, "Release Notes");
  assert.equal(page.document.title, "TikTok Live Translator", "回放的更新提示不改标题");
  page.push({ type: "update_available", version: "1.3.1", can_auto: false, url: "https://example.invalid/dl" });
  assert.equal(page.el("update-link").textContent, "Download New Version");
  assert.equal(page.document.title, "Update Available · TikTok Live Translator");
  const btn = page.el("update-btn");
  btn.click();                                                            // 直播中：先要再点一次
  assert.equal(btn.textContent, "Click Again to Update. Monitoring pauses and resumes afterward.");
  btn.click();
  assert.equal(btn.textContent, "Updating…");
  page.push({ type: "status", state: "idle", detail: "" });
  assert.equal(btn.textContent, "Update Now");

  page.el("app-version").click();
  assert.equal(note(), " · Checking for updates…");
  page.offline();
  page.el("app-version").click();
  assert.equal(note(), " · Not connected to the local service");
  page.online();

  const migrate = (msg) => page.push(Object.assign({ type: "glossary_migration" }, msg));
  assert.equal(page.el("migrate-text").textContent, "glossary.txt has 3 streamer-specific entries left over from " +
                                                    "the old template. They also affect other streamers’ translations.");
  migrate({ stage: "available", count: 1 });
  assert.match(page.el("migrate-text").textContent, /^glossary\.txt has 1 streamer-specific entry .* It also affects/);
  migrate({ stage: "plan", entries: [{ display: "tono", streamer: "maria" }, { display: "gel", streamer: "ana" }] });
  assert.match(page.confirms.pop(), /^These 2 entries will be moved \(glossary\.txt is backed up first\):\n\ntono {2}→ profiles\/maria\.txt\ngel {2}→ profiles\/ana\.txt\n\nOnly entries .* Continue\?$/);
  migrate({ stage: "plan", entries: [{ display: "tono", streamer: "maria" }] });
  assert.match(page.confirms.pop(), /^This entry will be moved /);
  migrate({ stage: "plan", entries: [] });
  assert.equal(note(), " · Nothing can be moved automatically. Move edited entries to profiles/ by hand.");
  migrate({ stage: "done", result: { total: 2, failed: 1, backup: "glossary.bak" } });
  assert.equal(note(), " · Moved 2 entries (backup: glossary.bak). 1 entry wasn’t moved because its profile " +
                       "couldn’t be written. It’s still in glossary.txt.");
  migrate({ stage: "done", result: { total: 1, failed: 2, backup: "glossary.bak" } });
  assert.match(note(), /^ · Moved 1 entry \(backup: glossary\.bak\)\. 2 entries weren’t moved /);
  migrate({ stage: "done", result: { total: 0, failed: 2, backup: "glossary.bak" } });
  assert.equal(note(), " · Couldn’t write to the profiles/ folder, so no entries were moved. " +
                       "All 2 are still in glossary.txt (backup: glossary.bak).");
  migrate({ stage: "done", result: { total: 0 } });
  assert.equal(note(), " · Nothing to move");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：翻译引擎说明与密钥框、自检的读屏级别、复制命令按钮", () => {
  const page = runDesktop();
  hello(page);
  const key = page.el("engine-key");
  assert.equal(key.placeholder, "Saved key …abcd (leave blank to keep)");
  const select = page.el("engine-select");
  const notes = {};
  for (const engine of ["auto", "hymt2", "hymt2-7b", "deepl", "claude", "openai", "google", "none"]) {
    select.value = engine;
    select.fire("change");
    notes[engine] = page.el("engine-note").textContent;
  }
  assert.equal(notes.auto, "Uses a local model: fully offline, unlimited, and captions never leave this computer.");
  assert.equal(notes.google, "Captions are sent to Google. Google caps how many requests one IP address can make.");
  select.value = "claude";
  select.fire("change");
  assert.equal(key.placeholder, "Paste API key");
  const save = page.el("engine-save");
  save.click();
  assert.equal(save.textContent, "Switching…");
  page.fire(2500);
  assert.equal(save.textContent, "Save");

  const levels = page.el("sc-list").querySelectorAll(".sr-only").map((n) => n.textContent);
  assert.deepEqual(levels, [" (warning)", " (failed)"]);

  page.push({ type: "status", state: "error", detail: "Something broke.", command: "pip install x" });
  const copy = page.el("fix-command-copy");
  copy.click();                                                          // 没有剪贴板接口：帮用户选中
  assert.equal(copy.textContent, "Selected. Press ⌘C");
  page.fire(3000);
  assert.equal(copy.textContent, "Copy");
  page.sandbox.navigator.clipboard = { writeText: () => ({ then: (ok) => ok() }) };
  copy.click();
  assert.equal(copy.textContent, "Copied");
  assert.deepEqual(leftoverChinese(page), []);
});
