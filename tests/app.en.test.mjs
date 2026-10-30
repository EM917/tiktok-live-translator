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
// 每个页面都在自己的新上下文里跑一遍这些脚本，但脚本只编译一次。以前每个页面都把 app.js（3000 多行）
// 重新编译一遍：守卫的页面用例加进来以后，整组 `node --test tests/*.test.mjs` 并行跑时，这个文件的子进程
// 约三成概率在 V8 的垃圾回收里崩掉（Node 24.14.1，SIGSEGV，退出码 1，单跑从不崩）；只编译一次后连跑 40 次没有再崩。
const scriptCache = new Map();
function compiled(name) {
  if (!scriptCache.has(name)) scriptCache.set(name, new vm.Script(web(name), { filename: "web/" + name }));
  return scriptCache.get(name);
}

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
  const sent = [];                                   // 页面发给后端的消息（JSON 解开）
  function FakeWebSocket(url) { this.url = url; this.readyState = 1; sockets.push(this); }
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.prototype.send = function (data) { sent.push(JSON.parse(data)); };
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
  for (const name of scripts) compiled(name).runInContext(sandbox);
  assert.equal(vm.runInContext("UI_LANG", sandbox), "en", "i18n.js 应该从 <html lang> 读到英文");
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
    brand_options: [{ id: "b1", name: "Allura" }], brands: {}, active_brand: { id: "b1", name: "Allura" },
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
  assert.equal(page.el("active-brand-tag").textContent, "Brand · Allura");
  const states = { connecting: "Connecting… · @maria.ventas", ended: "Stream ended", error: "Error",
                   idle: "Ready", offline: "Reconnecting…" };
  for (const [state, text] of Object.entries(states)) {
    page.push({ type: "status", state, detail: "" });
    assert.equal(page.el("status-text").textContent, text, state);
  }
  // 认不出的输入：说明写在输入框下面（专家审查 U9），不再走状态横幅；连接断开时点开始
  page.el("room-input").value = "hola mundo";
  page.el("start-btn").click();
  assert.match(page.el("room-input-error").textContent, /^This isn’t a live link or username\. .*Display names won’t work\.$/);
  assert.doesNotMatch(page.el("status-banner").textContent, /live link or username/);
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
  assert.deepEqual([redo.textContent, redo.title], ["Retranslate", "Retranslate with the stronger model"]);
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

test("英文页：点「重译」被拒或没成功，说明显示在被点的那条字幕旁边几秒，不走页脚", () => {
  const page = runDesktop();
  hello(page);
  page.push({ type: "caption", id: 1, ts: T, original: "hola", translated: "hello", translate_state: "ok" });
  page.push({ type: "caption", id: 2, ts: T, original: "que tal", translated: "how are you", translate_state: "ok" });
  const [card1, card2] = page.el("history").querySelectorAll(".cap");
  const notes = (card) => card.querySelectorAll(".redo-note").map((n) => n.textContent);
  const footer = () => page.el("app-version").textContent;
  const before = footer();
  const sorry = "Couldn’t retranslate: Claude returned HTTP 500. The current translation is unchanged.";

  page.push({ type: "retranslate_note", id: 1, text: sorry });
  assert.deepEqual(notes(card1), [sorry]);
  assert.deepEqual(notes(card2), []);                       // 只在被点的那一条
  assert.equal(footer(), before);                           // 不再落在页脚那一行
  assert.equal(card1.querySelector(".redo-note").getAttribute("role"), "status");

  // 同一条再来一句：换掉，不叠加
  page.push({ type: "retranslate_note", id: 1, text: "This caption is already being retranslated." });
  assert.deepEqual(notes(card1), ["This caption is already being retranslated."]);
  // 到点收起（也只收这一条自己的计时器）
  page.fire(7000);
  assert.deepEqual(notes(card1), []);

  // 失败状态本身不收说明（服务端先发 failed 再发说明；顺序反过来也不该把说明吞掉）
  page.push({ type: "retranslate_note", id: 2, text: sorry });
  page.push({ type: "caption_update", id: 2, strong_state: "failed" });
  assert.deepEqual(notes(card2), [sorry]);
  // 又点了一次、或这次成了：上一次的说明不再适用
  page.push({ type: "caption_update", id: 2, strong_state: "pending" });
  assert.deepEqual(notes(card2), []);
  page.push({ type: "retranslate_note", id: 2, text: sorry });
  page.push({ type: "caption_update", id: 2, translated: "a better translation", strong: true, strong_state: "ok", quality: 2 });
  assert.deepEqual(notes(card2), []);

  // 那条字幕已经不在页面上了（清空了、被挤出去）：话退回页脚那一行，不能丢
  page.push({ type: "retranslate_note", id: 99, text: "This caption has no saved original to retranslate." });
  assert.match(footer(), /This caption has no saved original to retranslate\./);
  page.push({ type: "retranslate_note", id: 1, text: "" });  // 空话不画
  assert.deepEqual(notes(card1), []);
  assert.deepEqual(leftoverChinese(page), []);
});

// 弹幕重译（2026-10-14）：服务端的消息形状和文字由 tests/test_comment_redo.py 钉着，这里钉页面怎么画、
// 怎么发、怎么收
const CLAUDE_ENGINE = { type: "engine", engine: "claude", keys: { ANTHROPIC_API_KEY: "…abcd" },
                        active: "claude", active_label: "Claude", note: "" };
const LOCAL_ENGINE = { type: "engine", engine: "hymt2", keys: {}, active: "hymt2",
                       active_label: "Local Hy-MT2 1.8B", note: "" };
const REDO_OFF = "Comment retranslation needs the Claude engine";
const REDO_ON = "Retranslate this comment with the stronger model";

test("英文页：弹幕的「重译」——译好了才有；只有 Claude 引擎能点（别的引擎是灰的，悬停说明）；点了发 retranslate_comment", () => {
  const page = runDesktop();
  hello(page);                                              // 引擎是 DeepL，没有 active 字段
  page.push({ type: "comment", id: "c1", ts: T, user: "Ana", text: "cuanto cuesta", state: "pending" });
  const item = page.el("comment-list").querySelector(".cmt-item");
  assert.equal(item.querySelector(".cmt-redo"), null, "还在翻译中：没有可重译的");
  page.push({ type: "comment_update", id: "c1", state: "ok", translated: "how much is it" });
  const btn = item.querySelector(".cmt-redo");
  assert.ok(btn, "译好了的弹幕有「重译」");
  assert.deepEqual([btn.textContent, btn.title, btn.disabled], ["Retranslate", REDO_OFF, true]);
  btn.click();
  assert.deepEqual(page.sent.filter((m) => m.type === "retranslate_comment"), [], "灰的按钮点不出消息");

  page.push(CLAUDE_ENGINE);                                 // 字幕引擎换成 Claude：已经画出来的按钮跟着亮
  assert.deepEqual([btn.textContent, btn.title, btn.disabled], ["Retranslate", REDO_ON, false]);
  btn.click();
  assert.deepEqual(page.sent.filter((m) => m.type === "retranslate_comment"),
                   [{ type: "retranslate_comment", id: "c1" }]);

  page.push({ type: "engine", ...LOCAL_ENGINE });          // 回退到本地模型：又灰了
  assert.deepEqual([btn.title, btn.disabled], [REDO_OFF, true]);

  // 没译出来的（失败）也能重译（2026-10-15）：被标出来的弹幕常常正是 Haiku 不肯译的，用 Sonnet 再试一次。
  // 已是目标语言（same）、引擎不翻弹幕（skipped）的原文本身不需要重译，没有按钮
  page.push({ type: "comment_update", id: "c1", state: "failed" });
  assert.ok(item.querySelector(".cmt-redo"), "没译出来的也有「重译」");
  page.push({ type: "comment", id: "c2", ts: T, user: "Bea", text: "hello", state: "same" });
  page.push({ type: "comment", id: "c3", ts: T, user: "Cy", text: "hola", state: "skipped" });
  assert.equal(page.el("comment-list").querySelectorAll(".cmt-redo").length, 1, "只有 c1（失败）有");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：重新连上（hello + 回放）时，回放出来的已译弹幕按当时的引擎画按钮；手机页没有这个按钮", () => {
  const page = runDesktop();
  hello(page, { engine: { engine: "claude", keys: {}, active: "claude", active_label: "Claude", note: "" } });
  page.push({ type: "comment", id: "c1", ts: T, user: "Ana", text: "hola", translated: "hello",
              state: "ok", replay: true });
  const btn = page.el("comment-list").querySelector(".cmt-redo");
  assert.deepEqual([btn.title, btn.disabled], [REDO_ON, false]);
  assert.doesNotMatch(web("viewer.js") + web("viewer.html"), /retranslate_comment|cmt-redo/,
                      "弹幕重译只在电脑页");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：弹幕重译的进行中、成功、失败；说明显示在那条弹幕下面几秒", () => {
  const page = runDesktop();
  hello(page);
  page.push(CLAUDE_ENGINE);
  page.push({ type: "comment", id: "c1", ts: T, user: "Ana", text: "cuanto cuesta", translated: "how much",
              state: "ok" });
  page.push({ type: "comment", id: "c2", ts: T, user: "Bea", text: "hola", translated: "hi", state: "ok" });
  const [item1, item2] = page.el("comment-list").querySelectorAll(".cmt-item");
  const [btn1, btn2] = [item1, item2].map((it) => it.querySelector(".cmt-redo"));
  const notes = (item) => item.querySelectorAll(".redo-note").map((n) => n.textContent);
  const zh = (item) => item.querySelector(".cmt-zh");
  const footer = () => page.el("app-version").textContent;
  const before = footer();
  const sorry = "Couldn’t retranslate this comment: Claude Sonnet 5.5 returned HTTP 500. The current translation is unchanged.";

  page.push({ type: "comment_redo", id: "c1", redo_state: "pending" });
  assert.deepEqual([btn1.textContent, btn1.disabled, item1.cls.includes("redoing")], ["Retranslating…", true, true]);
  assert.equal(zh(item1).textContent, "how much", "进行中不把译文换成占位字");
  assert.deepEqual([btn2.textContent, btn2.disabled], ["Retranslate", false], "别的弹幕不受影响");

  // 成了：新译文换上，按钮回到原样
  page.push({ type: "comment_update", id: "c1", state: "ok", translated: "how much is it" });
  page.push({ type: "comment_redo", id: "c1", redo_state: "ok" });
  assert.equal(zh(item1).textContent, "how much is it");
  assert.deepEqual([btn1.textContent, btn1.disabled, item1.cls.includes("redoing")], ["Retranslate", false, false]);

  // 没成：按钮写「Couldn’t retranslate」4 秒，说明在弹幕下面，不落在页脚，原译文不动
  page.push({ type: "comment_redo", id: "c1", redo_state: "pending" });
  page.push({ type: "comment_redo", id: "c1", redo_state: "failed", text: sorry });
  assert.equal(btn1.textContent, "Couldn’t retranslate");
  assert.deepEqual(notes(item1), [sorry]);
  assert.deepEqual(notes(item2), []);
  assert.equal(footer(), before);
  assert.equal(item1.querySelector(".redo-note").getAttribute("role"), "status");
  assert.equal(zh(item1).textContent, "how much is it");
  page.fire(4000);
  assert.equal(btn1.textContent, "Retranslate");
  assert.deepEqual(notes(item1), [sorry], "按钮复原了，说明还在");
  page.fire(7000);
  assert.deepEqual(notes(item1), []);

  // 同一条再来一句：换掉，不叠加；只有说明、没有状态的（正在重译）不动按钮
  page.push({ type: "comment_redo", id: "c2", redo_state: "failed", text: "This comment has no saved original to retranslate." });
  page.push({ type: "comment_redo", id: "c2", text: "This comment is already being retranslated." });
  assert.deepEqual(notes(item2), ["This comment is already being retranslated."]);
  // 又点了一次：上一次的说明不再适用
  page.push({ type: "comment_redo", id: "c2", redo_state: "pending" });
  assert.deepEqual(notes(item2), []);

  // 那条弹幕已经不在页面上了（清空了、被挤出去）：话退回页脚那一行，不能丢
  page.push({ type: "comment_redo", id: "nunca", redo_state: "failed", text: "This comment has no saved original to retranslate." });
  assert.match(footer(), /This comment has no saved original to retranslate\./);
  page.push({ type: "comment_redo", id: "c1", text: "" });   // 空话不画
  assert.deepEqual(notes(item1), []);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：接口拒答的字幕有自己的角标，不算进连续翻译失败，也不顶出那句横幅", () => {
  const page = runDesktop();
  hello(page);
  const banner = () => page.el("status-banner");
  // 主播在一段话题上反复说同类宣称：连着几条都被拒答
  for (let id = 1; id <= 6; id++) {
    page.push({ type: "caption", id, ts: T, original: "x", translate_state: "pending" });
    page.push({ type: "caption_update", id, translated: null, translate_state: "failed", refused: true });
  }
  const chips = () => page.el("history").querySelectorAll(".state-chip").map((c) => c.textContent);
  assert.deepEqual(chips(), Array(6).fill("Not translated (API refusal)"));
  assert.ok(!/couldn’t be translated/.test(banner().textContent) || banner().classList.contains("hidden"),
            "拒答不该顶出「翻译失败」横幅");
  // 拒答夹在中间不清零：真失败照样数到第四条才出横幅
  for (let id = 7; id <= 9; id++) page.push({ type: "caption", id, ts: T, original: "x", translate_state: "failed" });
  page.push({ type: "caption", id: 10, ts: T, original: "x", translate_state: "pending" });
  page.push({ type: "caption_update", id: 10, translated: null, translate_state: "failed", refused: true });
  assert.ok(!/couldn’t be translated/.test(banner().textContent) || banner().classList.contains("hidden"));
  page.push({ type: "caption", id: 11, ts: T, original: "x", translate_state: "failed" });
  assert.match(banner().textContent, /^The last several captions couldn’t be translated, /);
  assert.ok(!banner().classList.contains("hidden"));
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：回来的不是译文（模型在回话）的字幕有自己的角标，也不算进连续翻译失败", () => {
  // 服务端在 translator.looks_like_reply 拦下回话时带 not_translation（tests/test_subtitle_replies.py 钉着）
  const page = runDesktop();
  hello(page);
  const banner = () => page.el("status-banner");
  for (let id = 1; id <= 5; id++) {
    page.push({ type: "caption", id, ts: T, original: "x", translate_state: "pending" });
    page.push({ type: "caption_update", id, translated: null, translate_state: "failed", not_translation: true });
  }
  const chips = () => page.el("history").querySelectorAll(".state-chip").map((c) => c.textContent);
  assert.deepEqual(chips(), Array(5).fill("Not translated (the result didn’t look like a translation)"));
  assert.ok(!/couldn’t be translated/.test(banner().textContent) || banner().classList.contains("hidden"),
            "回话不该顶出「翻译失败」横幅");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：被拒答的字幕点重译又被拒答，角标留着「未翻译（接口拒答）」，不顶出横幅", () => {
  // 点重译的人最可能点的就是快译被拒答的那几条。Sonnet 也拒答时服务端发的消息形状由
  // tests/test_claude_strong.py 钉着：带 refused，页面据此保留角标、不计入连续失败
  const page = runDesktop();
  hello(page);
  const banner = () => page.el("status-banner");
  const chips = () => page.el("history").querySelectorAll(".state-chip").map((c) => c.textContent);
  const bannerUp = () => /couldn’t be translated/.test(banner().textContent) && !banner().classList.contains("hidden");
  const refusedAgain = (id, extra) => {
    page.push({ type: "caption_update", id, strong_state: "pending" });
    page.push(Object.assign({ type: "caption_update", id, translated: null, translate_state: "failed",
                              target_lang: "zh-CN", translate_ms: 0, quality: 2, strong_state: "failed" }, extra));
    page.push({ type: "retranslate_note", id, text: "Couldn’t retranslate: Claude Sonnet 5.5 declined to translate this caption (the API returned a refusal). This caption still has no translation." });
  };
  for (let id = 1; id <= 4; id++) {
    page.push({ type: "caption", id, ts: T, original: "x", translate_state: "pending" });
    page.push({ type: "caption_update", id, translated: null, translate_state: "failed", refused: true });
  }
  assert.deepEqual(chips(), Array(4).fill("Not translated (API refusal)"));
  for (let id = 1; id <= 4; id++) refusedAgain(id, { refused: true });
  assert.deepEqual(chips(), Array(4).fill("Not translated (API refusal)"));
  assert.ok(!bannerUp(), "重译被拒答不该顶出「翻译失败」横幅");
  assert.equal(page.el("history").querySelectorAll(".cap")[0].querySelectorAll(".redo-note").length, 1);
  assert.deepEqual(leftoverChinese(page), []);

  // 对照：同一串消息少了 refused（评审之前的服务端），角标换成「Translation failed」，第四条把横幅顶出来
  const control = runDesktop();
  hello(control);
  for (let id = 1; id <= 4; id++) {
    control.push({ type: "caption", id, ts: T, original: "x", translate_state: "pending" });
    control.push({ type: "caption_update", id, translated: null, translate_state: "failed", refused: true });
  }
  for (let id = 1; id <= 4; id++) {
    control.push({ type: "caption_update", id, translated: null, translate_state: "failed",
                   target_lang: "zh-CN", translate_ms: 0, quality: 2, strong_state: "failed" });
  }
  assert.deepEqual(control.el("history").querySelectorAll(".state-chip").map((c) => c.textContent),
                   Array(4).fill("Translation failed"));
  assert.match(control.el("status-banner").textContent, /^The last several captions couldn’t be translated, /);
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

test("英文页：弹幕面板的状态与空态、统计消息不画", () => {
  const page = runDesktop();
  hello(page);
  const source = () => [page.el("comment-source").textContent, page.el("comment-empty").textContent];
  const sourceShown = () => !page.el("comment-source").cls.includes("hidden");
  assert.deepEqual(source(), ["Not connected", "Comments connect when the stream starts."]);
  assert.equal(sourceShown(), true);
  assert.equal(page.el("comment-title-text").title, "");
  // 连上是常态：小字整个收起（圆点在它里面），“Connected” 挪进标题的 title；空态那句照旧
  page.push({ type: "comment_source", backend: "connected" });
  assert.deepEqual(source(), ["", "Connected. Waiting for comments…"]);
  assert.equal(sourceShown(), false);
  assert.equal(page.el("comment-title-text").title, "Connected");
  page.push({ type: "comment_source", backend: "connecting" });
  assert.deepEqual(source(), ["Connecting…", "Connecting…"]);
  assert.equal(sourceShown(), true);
  assert.equal(page.el("comment-title-text").title, "");
  page.push({ type: "comment_source", backend: "disconnected" });
  assert.deepEqual(source(), ["Disconnected. Reconnecting…", "Disconnected. Reconnecting…"]);
  assert.equal(sourceShown(), true);
  page.push({ type: "comment", id: "c1", ts: T, user: "Ana", text: "hola", state: "pending" });
  assert.equal(page.el("comment-list").querySelector(".cmt-zh").textContent, "Translating…");
  // 和主播的关系：不显示 Mutual，带互关的只标一个 Follower；认不出的标签不画
  page.push({ type: "comment", id: "c2", ts: T, user: "Bea", text: "hola", state: "pending",
              rel: ["follower", "mutual", "gifter", "boss"] });
  const chips = [...page.el("comment-list").querySelectorAll(".cmt-rel")].map(c => c.textContent);
  assert.deepEqual(chips, ["Follower", "Gifter"]);
  page.push({ type: "stats", e2e: {}, asr: { p50: 1100 }, translate: { p50: 2000 }, segment: { p50: 800 },
              detect_worst: { p50: 1900, p95: 3400 }, audio_backlog_sec: 5, audio_segments_dropped: 2,
              asr_overruns: 1, translation_jobs_dropped: 3, asr_queue_depth: 1, translation_queue_depth: 2 });
  assert.ok(!page.el("stats-line"), "延迟统计行已删（2026-10-15），统计消息照收不画");
  assert.deepEqual(leftoverChinese(page), []);
});

test("弹幕状态的后端说明整句写进去，截断交给 CSS（spec §11 第 6 条）", () => {
  // 以前 app.js 按 80 个字符截再补「…」：英文只截得出半句话。现在原样写进 #comment-source，
  // 由 style.css 的 .cmt-source 截成最多 6 行
  const page = runDesktop();
  hello(page);
  const refused = "The comment service refused the connection (HTTP 400). " +
                  "Couldn’t check for a comments component update. Retrying in 2 min.";
  const updated = "Comments component updated from 7.0.0 to 7.0.2. Reconnecting… " +
                  "(the operator should see this whole line)";
  assert.ok(refused.length > 80 && updated.length > 80);
  page.push({ type: "comment_source", backend: "error", detail: refused });
  assert.equal(page.el("comment-source").textContent, refused);
  assert.equal(page.el("comment-empty").textContent, refused);
  page.push({ type: "comment_source", backend: "connecting", detail: updated });
  assert.equal(page.el("comment-source").textContent, updated);
  const rule = web("style.css").match(/\n\.cmt-source \{([^}]*)\}/);
  assert.ok(rule, "style.css 里找不到 .cmt-source 规则");
  assert.match(rule[1], /-webkit-line-clamp: 6;/);
  assert.match(rule[1], /overflow: hidden;/);
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
  // 弹幕和重译也走 Claude：数据去哪了要在选引擎的地方说清
  assert.equal(notes.claude, "Captions, viewer comments and retranslation requests are sent to Anthropic and billed by usage.");
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

// 不绑工作区的 Claude 密钥（2026-10-29）：工作区 ID 框只在选 Claude 时出现，框里是服务端给的在用的那个，
// 保存时跟着发；不像 wrkspc_… 的不发、密钥框不清，提示是英文。别的引擎不发这个字段（服务端据此不动它）
test("英文页：Claude 的工作区 ID 框", () => {
  const page = runDesktop();
  hello(page);
  const row = page.el("engine-workspace-row");
  const box = page.el("engine-workspace");
  const select = page.el("engine-select");
  const save = page.el("engine-save");
  assert.ok(row.classList.contains("hidden"), "选的是 DeepL：不该出现工作区 ID 框");
  page.push({ type: "engine", engine: "claude", keys: { ANTHROPIC_API_KEY: "…abcd" }, workspace_id: "wrkspc_01Abc",
              active: "claude", active_label: "Claude", note: "" });
  assert.ok(!row.classList.contains("hidden"));
  assert.equal(box.value, "wrkspc_01Abc");

  const key = page.el("engine-key");
  box.value = "Default";                                    // 填了工作区的名字，不是 ID
  key.value = "sk-ant-new-key";
  save.click();
  assert.deepEqual(page.sent.filter((m) => m.type === "set_engine"), [], "不像工作区 ID 的不发");
  assert.equal(key.value, "sk-ant-new-key", "刚粘贴的密钥不清掉");
  assert.equal(page.el("engine-note").textContent,
               "A workspace ID starts with wrkspc_ followed by letters and numbers. It’s listed on the Workspaces " +
               "page in the Claude Console. Nothing was saved. Check it and click Save again.");
  assert.equal(save.disabled, false, "没发出去：保存按钮不进「切换中…」");

  box.value = "  wrkspc_02Xyz ";
  save.click();
  assert.deepEqual(page.sent.filter((m) => m.type === "set_engine").pop(),
                   { type: "set_engine", engine: "claude", api_key: "sk-ant-new-key", workspace_id: "wrkspc_02Xyz" });
  page.fire(2500);
  box.value = "";                                            // 清空就是不带
  save.click();
  assert.equal(page.sent.filter((m) => m.type === "set_engine").pop().workspace_id, "");
  page.fire(2500);

  select.value = "deepl";
  select.fire("change");
  assert.ok(row.classList.contains("hidden"));
  save.click();
  assert.ok(!("workspace_id" in page.sent.filter((m) => m.type === "set_engine").pop()));
  assert.deepEqual(leftoverChinese(page), []);
});

// ---- 弹幕留意（Comment Review）的接线：web/app.js 里收消息、存状态、把回调传给 web/guard.js 的那部分 ----------
// 文案和渲染的细节在 tests/guard.test.mjs / guard.en.test.mjs；这里钉的是接线：
// 设置行默认收起且永不自动展开；hello 里的回放不算新事件；顶部条每秒最多重画一次；
// 加入 / 移出 / 忽略 / 重新启用 / 设置里的开关发出去的消息；直播结束后这些东西都收起；整页没有剩下的中文。

const GUARD_UID = "7234567890123456789";
const GUARD_STATE = { enabled: true, backend: "none", status: "rules", since: 1, trip: null,
  rules: { count: 510, warnings: 0 }, n_marked: 1, unreviewed: 0, weak: false, streamer: "luna_demo" };
const GUARD_ENTRY = { keys: [GUARD_UID, "h:ana_demo"], nick: "Ana", handle: "ana_demo", aka: [], by: "auto",
  cat: "threat", first: T - 3600, last_seen: T - 600, hits: 2 };
const guardEvent = (over) => Object.assign({ id: "g1", kind: "comment", cat: "threat", lvl: "alert", amb: null, n: 1,
  accounts: [], window_sec: 60, first_ts: T + 5, ts: T + 5, rv: null,
  sample: { cid: "c1", user: "Ana", text: "te voy a matar", uid: GUARD_UID, uid_src: "id", handle: "ana_demo" } }, over || {});
const guardComment = (over) => Object.assign({ type: "comment", id: "c1", user: "Ana", text: "te voy a matar", ts: T + 5,
  translated: null, state: "pending", uid: GUARD_UID, uid_ok: true, handle: "ana_demo" }, over || {});
const RED_GUARD = { lvl: "alert", amb: null, cats: ["threat"], sub: "threat", tier: "red", src: "rule", rv: null, ev: "g1",
  why: ["insult/threat:te voy a matar"] };

function helloGuard(page, extra) {
  hello(page, Object.assign({ guard_state: GUARD_STATE, guard_events: { g0: guardEvent({ id: "g0", ts: T - 30 }) },
    guard_marked: { [GUARD_UID]: GUARD_ENTRY } }, extra || {}));
}

test("英文页：弹幕留意——胶囊、设置行都是英文，设置行默认收起；hello 里回放的事件不算新事件", () => {
  const page = runDesktop();
  helloGuard(page);
  const cap = page.el("guard-state");
  assert.ok(!cap.cls.includes("hidden"));
  // 只用规则是常态：胶囊上只画名字，完整一句在按钮的 title 里；抽屉里的状态行（下面的 guard-drawer-state）仍是整句
  assert.equal(page.el("guard-state-text").textContent, "Comment Review");
  assert.equal(cap.title, "Comment Review: rules only\nSee recent comments to review");
  assert.ok(cap.cls.includes("idle"));
  assert.equal(page.el("guard-bar").cls.includes("hidden"), true, "回放里带来的事件不弹顶部条");
  const card = page.el("guard-card");
  assert.ok(!card.cls.includes("hidden"));
  assert.equal(page.el("guard-summary").textContent, "On · Rules only · 1 marked");
  assert.equal(page.el("guard-body").cls.includes("hidden"), true, "永不自动展开");
  assert.equal(page.el("guard-head").getAttribute("aria-expanded"), "false");
  assert.equal(page.el("guard-toggle").checked, true);
  assert.equal(page.el("guard-backend").value, "none");
  assert.equal(page.el("guard-status").textContent, "Comment Review: rules only");
  assert.equal(page.el("guard-rules").textContent, "510 rules active");
  assert.equal(page.el("guard-marked-heading").textContent, "Marked Viewers · @luna_demo");
  assert.equal(page.el("guard-marked-list").querySelectorAll(".guard-mk-row").length, 1);
  assert.equal(page.el("guard-clear-auto").cls.includes("hidden"), false, "名单里有自动加入的人");
  assert.equal(page.el("guard-key-row").cls.includes("hidden"), true);
  assert.equal(page.el("guard-privacy").cls.includes("hidden"), true);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：没有 guard_state（老后端）时整个功能不出现，也不报错", () => {
  const page = runDesktop();
  hello(page);
  assert.equal(page.el("guard-card").cls.includes("hidden"), true);
  assert.equal(page.el("guard-state").cls.includes("hidden"), true);
  assert.equal(page.el("guard-bar").cls.includes("hidden"), true);
  page.push(guardComment());                          // 老后端不会带 guard 字段
  const item = page.el("comment-list").querySelector(".cmt-item");
  assert.deepEqual(item.cls.filter((c) => c.startsWith("guard-")), []);
  assert.equal(item.querySelector(".guard-mark-btn"), null, "守卫没开：没有加入按钮");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：弹幕栏里的标记——红条类别、关注的人 ★、琥珀理由；加入 / 移出消息", () => {
  const page = runDesktop();
  helloGuard(page);
  page.push(guardComment({ guard: RED_GUARD }));
  page.push(guardComment({ id: "c2", user: "Lupita", uid: "11", handle: "lupita", text: "es falso",
    guard: { lvl: "amber", amb: "maybe_question", cats: ["accuse_claim"], why: [], ev: "g2" } }));
  const [first, second] = page.el("comment-list").querySelectorAll(".cmt-item");
  assert.ok(first.cls.includes("guard-alert") && first.cls.includes("guard-marked"));
  assert.equal(first.dataset.uid, GUARD_UID);
  assert.equal(first.querySelector(".guard-pill").textContent, "Possible threat of harm");
  assert.equal(first.querySelector(".guard-mark-btn").getAttribute("aria-label"), "Unmark");
  assert.ok(second.cls.includes("guard-amber") && !second.cls.includes("guard-marked"));
  assert.equal(second.querySelector(".guard-note").textContent, "Short, with no question mark");
  // 点 ☆：发加入消息（键是字符串，19 位原样）
  second.querySelector(".guard-mark-btn").click();
  assert.deepEqual(page.sent.pop(), { type: "guard_mark", key: "11", on: true, nick: "Lupita", handle: "lupita", uid_src: null });
  first.querySelector(".guard-mark-btn").click();
  assert.deepEqual(page.sent.pop(), { type: "guard_mark", key: GUARD_UID, on: false, nick: "Ana", handle: "ana_demo", uid_src: null });
  // 后端确认加入：已显示的弹幕即时亮起；确认移出：即时熄灭
  page.push({ type: "guard_mark", key: "11", entry: Object.assign({}, GUARD_ENTRY, { keys: ["11"], nick: "Lupita", by: "manual" }) });
  assert.ok(second.cls.includes("guard-marked"));
  page.push({ type: "guard_mark", key: GUARD_UID, entry: null });
  assert.ok(!first.cls.includes("guard-marked") && first.cls.includes("guard-alert"));
  assert.equal(first.querySelector(".guard-mark-btn").getAttribute("aria-label"), "Mark");
  // 复核结果经 comment_guard 后补
  page.push({ type: "comment_guard", id: "c2", guard: { lvl: "amber", amb: "review", cats: ["accuse_claim"], src: "review", rv: "unsure", why: [] } });
  assert.equal(second.querySelector(".guard-note").textContent, "Flagged by the model · Model: unsure");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：同一条弹幕既有关系标签（Gifter）又有守卫标记：两样都在，且复核结果补上来之后关系标签还在", () => {
  const page = runDesktop();
  helloGuard(page);
  page.push(guardComment({ rel: ["follower", "gifter"], guard: RED_GUARD }));
  const item = page.el("comment-list").querySelector(".cmt-item");
  const head = item.querySelector(".cmt-head");
  assert.deepEqual([...head.querySelectorAll(".cmt-rel")].map((c) => c.textContent), ["Follower", "Gifter"]);
  assert.equal(head.querySelector(".guard-pill").textContent, "Possible threat of harm");
  assert.ok(head.querySelector(".guard-star"), "名单里的人有 ★");
  assert.ok(item.cls.includes("guard-alert") && item.cls.includes("guard-marked"));
  // 复核结果经 comment_guard 补上来：守卫的节点重画，关系标签不被一并撤掉
  page.push({ type: "comment_guard", id: "c1", guard: Object.assign({}, RED_GUARD, { rv: "agree", src: "review" }) });
  assert.deepEqual([...head.querySelectorAll(".cmt-rel")].map((c) => c.textContent), ["Follower", "Gifter"]);
  assert.equal(head.querySelectorAll(".guard-pill").length, 1, "守卫的节点重画而不是叠加");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：顶部条——新事件弹出，每秒最多重画一次；关注的人发言单独计；全部看过收起", () => {
  const page = runDesktop();
  helloGuard(page);
  const bar = page.el("guard-bar");
  const text = () => page.el("guard-bar-text").textContent;
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", ts: T + 5 }) });
  assert.equal(bar.cls.includes("hidden"), true, "距上一次重画不到 1 秒：先压住");
  page.fire(1000);
  assert.equal(bar.cls.includes("hidden"), false);
  assert.ok(bar.cls.includes("has-red"));
  assert.equal(text(), "1 item to review · Possible threat of harm ×1 · Latest " + (new Date((T + 5) * 1000)).toTimeString().slice(0, 8));
  assert.equal(page.el("guard-bar-main").title, text());
  // 冲场：事件被同一个 id 持续更新，条数在涨，但事件还是 1 件（用户决定 10：按事件计，不数里面的弹幕）
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", kind: "flood", cat: "flood_user", n: 60, ts: T + 6 }) });
  assert.match(text(), /^1 item to review · Possible flooding ×1 · Latest /);
  // 琥珀不出红点，但算「拿不准」
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g2", cat: "accuse_claim", lvl: "amber", amb: "pending", ts: T + 7 }) });
  assert.match(text(), /^1 item to review · 1 unclear · /);
  // 关注的人发言：回放的不算，实时的算
  page.advance(1500);
  page.push(guardComment({ id: "c8", replay: true, ts: T + 8 }));
  assert.doesNotMatch(text(), /marked viewers/);
  page.push(guardComment({ id: "c9", ts: T + 9 }));
  page.fire(1000);                                   // 这一秒内已经画过一次：压住的那次到点补上
  assert.match(text(), /^1 item to review · 1 unclear · 1 from marked viewers · /);
  // 全部看过（假时钟走到事件之后，已看的时刻才盖得住它们）
  page.advance(30000);
  page.el("guard-bar-seen").click();
  assert.equal(bar.cls.includes("hidden"), true);
  assert.ok(Number(page.sandbox.localStorage.getItem("guardSeenTs")) > 0, "已看的时刻记在 localStorage");
  // 忽略的事件（后端广播 dismissed）不再计
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g3", ts: T + 100 }) });
  assert.equal(bar.cls.includes("hidden"), false);
  page.push({ type: "guard_event", ev: { id: "g3", dismissed: true } });
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g4", lvl: "amber", cat: "insult", ts: T + 101 }) });
  assert.match(text(), /^1 unclear/);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：浮钮上只显示未读红条的事件数（一个事件里有几条弹幕不算），琥珀不出红点", () => {
  const page = runDesktop();
  helloGuard(page);
  const dot = page.el("comment-fab-guard");
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "a1", lvl: "amber", cat: "insult", ts: T + 5 }) });
  assert.equal(dot.cls.includes("hidden"), true);
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "a2", n: 3, ts: T + 6 }) });
  assert.equal(dot.cls.includes("hidden"), false);
  assert.equal(dot.textContent, "1", "一个事件里有 3 条弹幕，还是 1 件");
  assert.equal(dot.title, "1 item to review");
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "a3", cat: "promo_link", ts: T + 7 }) });
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "a4", kind: "flood", cat: "flood_user", n: 40, ts: T + 8 }) });
  assert.equal(dot.textContent, "3");
  assert.equal(dot.title, "3 items to review");
});

test("英文页：抽屉——点条或胶囊展开，两个页签，忽略 / 加入 / 定位 / 重新启用都发对的消息", () => {
  const page = runDesktop();
  helloGuard(page);
  page.push(guardComment({ id: "c1", guard: RED_GUARD }));
  page.push({ type: "comment_update", id: "c1", translated: "I'm going to kill you", state: "ok" });
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", ts: T + 5, sample: { cid: "c1", user: "Pedro", text: "te voy a matar",
    uid: "55", uid_src: "id", handle: "pedro" } }) });
  const drawer = page.el("guard-drawer");
  assert.equal(drawer.cls.includes("hidden"), true);
  page.el("guard-state").click();
  assert.equal(drawer.cls.includes("hidden"), false);
  assert.equal(page.el("guard-state").getAttribute("aria-expanded"), "true");
  assert.equal(page.el("guard-drawer-state").textContent, "Comment Review: rules only");
  const row = page.el("guard-events").querySelector(".guard-ev-row");
  assert.equal(row.querySelector(".gev-user").textContent, "Pedro");
  assert.equal(row.querySelector(".gev-zh").textContent, "I'm going to kill you", "取弹幕栏里已有的译文");
  assert.deepEqual(row.querySelectorAll(".btn").map((b) => b.textContent),
                   ["Show in Comments", "Mark", "Dismiss", "Retranslate"], "「重译」放最后：后出现、字会变，别把别的按钮挤走");
  // 加入
  row.querySelectorAll(".btn")[1].click();
  assert.deepEqual(page.sent.pop(), { type: "guard_mark", key: "55", on: true, nick: "Pedro", handle: "pedro", uid_src: "id" });
  // 定位：面板收起着就先展开
  page.el("comment-panel").classList.add("collapsed");
  row.querySelectorAll(".btn")[0].click();
  assert.equal(page.el("comment-panel").cls.includes("collapsed"), false);
  assert.ok(page.el("comment-list").querySelector(".cmt-item").cls.includes("guard-flash"));
  page.fire(1600);
  assert.ok(!page.el("comment-list").querySelector(".cmt-item").cls.includes("guard-flash"));
  // 忽略：这一行没了，hello 里带来的那条（g0）还在；也忽略掉就是空态
  row.querySelectorAll(".btn")[2].click();
  assert.deepEqual(page.sent.pop(), { type: "guard_dismiss", ev_id: "g1" });
  const left = page.el("guard-events").querySelectorAll(".guard-ev-row");
  assert.deepEqual(left.map((r) => r.getAttribute("data-ev")), ["g0"]);
  left[0].querySelectorAll(".btn").find((b) => b.textContent === "Dismiss").click();
  assert.deepEqual(page.sent.pop(), { type: "guard_dismiss", ev_id: "g0" });
  assert.equal(page.el("guard-events").querySelector(".guard-ev-row"), null);
  assert.equal(page.el("guard-events").querySelector(".guard-empty").textContent, "No comments to review yet.");
  // 第二个页签：关注名单
  page.el("guard-tab-marked").click();
  assert.equal(page.el("guard-pane-marked").cls.includes("hidden"), false);
  assert.equal(page.el("guard-pane-recent").cls.includes("hidden"), true);
  assert.equal(page.el("guard-tab-marked-n").textContent, "1");
  page.el("guard-marked").querySelector(".gmk-un").click();
  assert.deepEqual(page.sent.pop(), { type: "guard_mark", key: GUARD_UID, on: false });
  // 收起
  page.el("guard-drawer-close").click();
  assert.equal(drawer.cls.includes("hidden"), true);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：顶部条右侧的控件写的是点了会怎样——抽屉收着是 View，开着是 Hide，aria-expanded 跟着一致", () => {
  const page = runDesktop();
  helloGuard(page);
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "v1", ts: T + 5 }) });
  page.fire(1000);
  const view = page.el("guard-bar-view");
  const main = page.el("guard-bar-main");
  const drawer = page.el("guard-drawer");
  const shows = (label, open) => {
    assert.equal(view.textContent, label);
    assert.equal(main.getAttribute("aria-expanded"), open ? "true" : "false");
    assert.equal(drawer.cls.includes("hidden"), !open);
  };
  shows("View", false);
  main.click();                                   // 点条本身：展开
  shows("Hide", true);
  main.click();                                   // 同一个控件再点：收起
  shows("View", false);
  page.el("guard-state").click();                 // 点弹幕栏里的胶囊展开：条上也跟着写 Hide
  shows("Hide", true);
  page.el("guard-drawer-close").click();          // 抽屉右上角的 ×：收起
  shows("View", false);
  page.el("guard-state").click();
  page.el("guard-tab-marked").click();            // 换页签不改写它
  shows("Hide", true);
  // 直播停了：抽屉跟着收起，条上写回 View（下一场的条不会带着「Hide」出现）
  page.push({ type: "status", state: "idle", detail: "" });
  shows("View", false);
  assert.equal(page.el("guard-bar").cls.includes("hidden"), true);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：模型复核熔断后，胶囊和抽屉都有原因和「本场重新启用」", () => {
  const page = runDesktop();
  helloGuard(page, { guard_state: Object.assign({}, GUARD_STATE, { backend: "claude", status: "tripped", lag_sec: 12, unreviewed: 3 }) });
  assert.equal(page.el("guard-state-text").textContent, "Model review is off for this session: speech recognition fell 12 sec behind. Rules only.");
  assert.ok(page.el("guard-state").cls.includes("warn"));
  // 不开抽屉也能重新启用：胶囊下面有同一个按钮
  const inline = page.el("guard-reenable-cap");
  assert.equal(inline.cls.includes("hidden"), false);
  inline.click();
  assert.deepEqual(page.sent.pop(), { type: "guard_reenable" });
  page.el("guard-state").click();
  const again = page.el("guard-reenable");
  assert.equal(again.cls.includes("hidden"), false);
  assert.equal(again.disabled, false);
  assert.match(page.el("guard-drawer-state").textContent, /3 comments weren’t reviewed by the model this session\.$/);
  again.click();
  assert.deepEqual(page.sent.pop(), { type: "guard_reenable" });
  page.push({ type: "config", guard_state: Object.assign({}, GUARD_STATE, { backend: "claude", status: "tripped", lag_sec: 12, reenable_ok: false }) });
  assert.equal(again.disabled, true);
  assert.equal(again.title, "Available after speech recognition has kept up for 60 sec");
  assert.equal(inline.disabled, true);
  assert.equal(inline.title, again.title);
  page.push({ type: "config", guard_state: GUARD_STATE });
  assert.equal(again.cls.includes("hidden"), true);
  assert.equal(inline.cls.includes("hidden"), true);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：设置行的控件发 guard_set；选 Claude 没有密钥时出现密钥框，存了密钥就收起", () => {
  const page = runDesktop();
  helloGuard(page);
  const toggle = page.el("guard-toggle");
  toggle.checked = false;
  toggle.fire("change");
  assert.deepEqual(page.sent.pop(), { type: "guard_set", enabled: false });
  const weak = page.el("guard-weak");
  weak.checked = true;
  weak.fire("change");
  assert.deepEqual(page.sent.pop(), { type: "guard_set", weak: true });
  const select = page.el("guard-backend");
  select.value = "claude";
  select.fire("change");
  assert.deepEqual(page.sent.pop(), { type: "guard_set", backend: "claude" });
  assert.equal(page.el("guard-key-row").cls.includes("hidden"), false, "翻译引擎那边没有 Claude 密钥");
  assert.equal(page.el("guard-privacy").cls.includes("hidden"), false);
  page.el("guard-key").value = "  sk-ant-test-not-a-real-key  ";
  page.el("guard-key-save").click();
  assert.deepEqual(page.sent.pop(), { type: "guard_set", backend: "claude", key: "sk-ant-test-not-a-real-key" });
  assert.equal(page.el("guard-key").value, "", "密钥框清空，不留在页面上");
  page.el("guard-key").value = "   ";
  page.el("guard-key-save").click();
  assert.equal(page.sent.length, 0, "空的不发");
  // 后端确认了选择（假的 guard_set 不会改 state，这里补一条）：密钥框还在，因为翻译引擎那边还是没有 Claude 密钥
  page.push({ type: "config", guard_state: Object.assign({}, GUARD_STATE, { backend: "claude", status: "no_review" }) });
  assert.equal(page.el("guard-backend").value, "claude");
  assert.equal(page.el("guard-key-row").cls.includes("hidden"), false, "没有 Claude 密钥：密钥框留着");
  // 后端存好密钥、广播打码后的尾四位：密钥框收起
  page.push({ type: "engine", engine: "deepl", keys: { DEEPL_API_KEY: "…abcd", ANTHROPIC_API_KEY: "…1234" }, active_label: "DeepL", note: "" });
  assert.equal(page.el("guard-key-row").cls.includes("hidden"), true);
  // 状态变化：选了 Claude 但这个版本还没有模型复核，摘要写实际在起作用的，也不说「会发给 Anthropic」
  page.push({ type: "config", guard_state: Object.assign({}, GUARD_STATE, { backend: "claude", status: "no_review" }) });
  assert.equal(page.el("guard-summary").textContent, "On · Rules only · 1 marked");
  assert.equal(page.el("guard-status").textContent, "Model review isn’t in this version yet. Using rules only.");
  assert.equal(page.el("guard-backend").value, "claude");
  assert.equal(page.el("guard-privacy").cls.includes("hidden"), true, "没有复核就没有东西发出去");
  // 组件缺失（复核接上之后才会有）：照常说
  page.push({ type: "config", guard_state: Object.assign({}, GUARD_STATE, { backend: "claude", status: "no_sdk" }) });
  assert.equal(page.el("guard-status").textContent, "The Claude component isn’t installed. Using rules only.");
  // 有密钥、读不到复核提示词：照实说（不是「没有密钥」），什么都发不出去，所以也不说「会发给 Anthropic」
  page.push({ type: "config", guard_state: Object.assign({}, GUARD_STATE, { backend: "claude", status: "prompt_missing" }) });
  assert.equal(page.el("guard-status").textContent, "The review prompt file couldn’t be read. Using rules only.");
  assert.equal(page.el("guard-privacy").cls.includes("hidden"), true);
  // 没有密钥：填了就会发，隐私那一行照说
  page.push({ type: "config", guard_state: Object.assign({}, GUARD_STATE, { backend: "claude", status: "no_key" }) });
  assert.equal(page.el("guard-status").textContent, "No Claude API key. Using rules only.");
  assert.equal(page.el("guard-privacy").cls.includes("hidden"), false);
  // 其余按钮
  page.el("guard-clear-auto").click();
  assert.deepEqual(page.sent.pop(), { type: "guard_clear_auto" });
  page.el("guard-dir-btn").click();
  assert.deepEqual(page.sent.pop(), { type: "open_guard_dir" });
  page.el("guard-marked-list").querySelector(".gmk-un").click();
  assert.deepEqual(page.sent.pop(), { type: "guard_mark", key: GUARD_UID, on: false });
  // 应急开关关着：开关灰掉
  page.push({ type: "config", guard_state: Object.assign({}, GUARD_STATE, { enabled: false, status: "env_off" }) });
  assert.equal(page.el("guard-toggle").disabled, true);
  assert.equal(page.el("guard-summary").textContent, "Off · 1 marked");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：停止之后顶部条、胶囊、抽屉都收起；开场的 config 把事件表和名单整份换掉", () => {
  const page = runDesktop();
  helloGuard(page);
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", ts: T + 5 }) });
  page.el("guard-state").click();
  assert.equal(page.el("guard-bar").cls.includes("hidden"), false);
  assert.equal(page.el("guard-drawer").cls.includes("hidden"), false);
  page.push({ type: "status", state: "idle", detail: "" });
  assert.equal(page.el("guard-bar").cls.includes("hidden"), true);
  assert.equal(page.el("guard-drawer").cls.includes("hidden"), true);
  assert.equal(page.el("guard-state").cls.includes("hidden"), true);
  // 再开一场：config 里事件表清空、名单是这个主播的
  page.push({ type: "status", state: "live", detail: "" });
  page.push({ type: "config", guard_events: {}, guard_marked: {} });
  assert.equal(page.el("guard-bar").cls.includes("hidden"), true);
  assert.equal(page.el("guard-marked-list").querySelectorAll(".guard-mk-row").length, 0);
  assert.equal(page.el("guard-marked-list").querySelector(".guard-empty").textContent, "No marked viewers yet.");
  assert.equal(page.el("guard-clear-auto").cls.includes("hidden"), true);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：上一场的「关注的人发言」和未读红条不带进下一场——开场的 config 归零，连接中就先收起", () => {
  const page = runDesktop();
  helloGuard(page);
  const bar = page.el("guard-bar");
  const text = () => page.el("guard-bar-text").textContent;
  page.advance(1500);
  page.push(guardComment({ id: "c1", ts: T + 5 }));
  page.push(guardComment({ id: "c2", ts: T + 6 }));
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", lvl: "amber", cat: "insult", ts: T + 7 }) });
  page.fire(1000);
  assert.match(text(), /2 from marked viewers/);
  // 停止、再开一场：先是「连接中」，后端的重置 config 要等模型和审计就绪才到
  page.push({ type: "status", state: "idle", detail: "" });
  assert.equal(bar.cls.includes("hidden"), true);
  page.advance(60000);
  page.push({ type: "status", state: "connecting", detail: "Connecting" });
  page.advance(1500);
  page.fire(1000);
  assert.equal(bar.cls.includes("hidden"), true, "连接中不露出上一场的未读");
  page.push({ type: "config", guard_state: GUARD_STATE, guard_events: {}, guard_marked: { [GUARD_UID]: GUARD_ENTRY } });
  page.push({ type: "status", state: "live", detail: "" });
  page.advance(1500);
  page.fire(1000);
  assert.equal(bar.cls.includes("hidden"), true, "新一场还没人说话");
  // 新一场里关注的人说话：只算这一场的
  page.push(guardComment({ id: "n1", ts: T + 100 }));
  page.fire(1000);
  assert.equal(text(), "1 from marked viewers");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：直播中换主播——开场的 config（事件表清空）把关注的人发言数归零，不挂在新主播的名单上", () => {
  const page = runDesktop();
  helloGuard(page);
  const bar = page.el("guard-bar");
  page.advance(1500);
  page.push(guardComment({ id: "c1", ts: T + 5 }));
  page.fire(1000);
  assert.equal(bar.cls.includes("hidden"), false);
  // 换主播不经过待机：live -> connecting，新开场的 config 带来新名单
  page.push({ type: "status", state: "connecting", detail: "Connecting" });
  page.push({ type: "config", guard_state: GUARD_STATE, guard_events: {}, guard_marked: {} });
  page.push({ type: "status", state: "live", detail: "" });
  page.advance(1500);
  page.fire(1000);
  assert.equal(bar.cls.includes("hidden"), true);
});

test("英文页：刷新或重连时 hello 里的事件照常显示在抽屉里，不被「新一场」清掉", () => {
  const page = runDesktop();
  // 页面一开始就知道是待机，随后 hello 说其实在直播（比如另一个窗口开的）
  page.push({ type: "status", state: "idle", detail: "" });
  helloGuard(page);
  page.el("guard-state").click();
  assert.equal(page.el("guard-events").querySelectorAll(".guard-ev-row").length, 1);
});

test("英文页：抽屉——定位后不被新弹幕拉回底部；行已不在列表里时说一声；胶囊再点一次收起", () => {
  const page = runDesktop();
  helloGuard(page);
  const list = page.el("comment-list");
  list.scrollHeight = 5000;
  page.push(guardComment({ id: "c1", guard: RED_GUARD }));
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", ts: T + 5 }) });
  page.push({ type: "guard_event", ev: guardEvent({ id: "g2", ts: T + 6, sample: { cid: "gone", user: "Ana", text: "x",
    uid: GUARD_UID, uid_src: "id", handle: "ana_demo" } }) });
  page.el("guard-state").click();
  const drawer = page.el("guard-drawer");
  assert.equal(drawer.cls.includes("hidden"), false);
  const locate = (id) => page.el("guard-events").querySelectorAll(".guard-ev-row")
    .find((r) => r.getAttribute("data-ev") === id).querySelectorAll(".btn")[0];
  // 定位到还在列表里的那一条：视野停在那儿，下一条弹幕来了也不拉回底部
  assert.equal(list.scrollTop, 5000, "一直跟着最新的，滚在底部");
  list.querySelector(".cmt-item").getBoundingClientRect = () => ({ top: -300 });
  locate("g1").click();
  assert.equal(list.scrollTop, 4700);
  page.push(guardComment({ id: "c2", ts: T + 10, text: "hola" }));
  assert.equal(list.scrollTop, 4700, "定位之后不再自动跟随");
  // 那条弹幕已经滚出列表：在页脚版本号旁说一声，视野不动
  locate("g2").click();
  assert.equal(page.el("app-version").textContent, " \u00b7 That comment is no longer in the list");
  assert.equal(list.scrollTop, 4700);
  // 同一个入口再点一次收起
  page.el("guard-state").click();
  assert.equal(drawer.cls.includes("hidden"), true);
  page.el("guard-state").click();
  assert.equal(drawer.cls.includes("hidden"), false);
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：字幕记录行——摘要、两个按钮发的消息、按钮下面的回音；默认收起且永不被消息弹开", () => {
  const page = runDesktop();
  hello(page, { transcripts: { count: 3 } });
  const head = page.el("transcript-head");
  const body = page.el("transcript-body");
  const status = page.el("transcript-status");
  assert.equal(page.el("transcript-summary").textContent, "3 sessions");
  // CLAUDE.md 第六条：和违禁词报警、弹幕留意一样默认收起，hello 和后面的消息都不会替中控展开它
  assert.equal(head.getAttribute("aria-expanded"), "false");
  assert.ok(body.cls.includes("hidden"));
  page.push({ type: "config", transcripts: { count: 1 } });
  assert.equal(page.el("transcript-summary").textContent, "1 session");
  page.push({ type: "config", transcripts: { count: 0 } });
  assert.equal(page.el("transcript-summary").textContent, "None yet");
  assert.equal(head.getAttribute("aria-expanded"), "false");
  head.click();
  assert.equal(head.getAttribute("aria-expanded"), "true");
  assert.ok(!body.cls.includes("hidden"));
  // 导出：发 export_transcript，先写「正在导出」，后端的 notice 带 where 就把同一句话写在按钮下面
  page.el("transcript-export").click();
  assert.deepEqual(page.sent.filter((m) => m.type === "export_transcript"), [{ type: "export_transcript" }]);
  assert.equal(status.textContent, "Exporting…");
  const done = "Exported 2 captions, 1 comment (Markdown, CSV and SRT files) to /app/transcripts";
  page.push({ type: "notice", text: done, where: "transcripts" });
  assert.equal(status.textContent, done);
  assert.equal(page.el("app-version").textContent, " · " + done, "页脚那一句照旧");
  // 别处的 notice 不往这一行写
  page.push({ type: "notice", text: "Update check finished" });
  assert.equal(status.textContent, done);
  // 打开文件夹
  page.el("transcript-dir-btn").click();
  assert.deepEqual(page.sent.filter((m) => m.type === "open_transcripts_dir"), [{ type: "open_transcripts_dir" }]);
  // 连不上本地服务时点导出：说清楚，不留在「正在导出」
  page.offline();
  page.el("transcript-export").click();
  assert.equal(status.textContent,
               "Lost connection to the local service. Reconnecting… Try again in a moment.");
  assert.deepEqual(leftoverChinese(page), []);
});

// ---- 抽屉里的译文和重译（2026-10-15：抽屉里被标出来的弹幕一直没有译文、也没有重译）--------------------------

test("英文页：抽屉——事件先到、译文后到时，译文一到就画上；弹幕被挤出弹幕栏后抽屉里照样有译文", () => {
  const page = runDesktop();
  helloGuard(page);
  page.push(guardComment({ id: "c1", guard: RED_GUARD }));                 // 还在翻译
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", ts: T + 5 }) });
  page.el("guard-state").click();
  const row = () => page.el("guard-events").querySelectorAll(".guard-ev-row").find((r) => r.getAttribute("data-ev") === "g1");
  assert.equal(row().querySelector(".gev-zh"), null, "还没有译文");
  assert.equal(row().querySelector(".gev-redo"), null, "还在翻译：没有可重译的");
  page.push({ type: "comment_update", id: "c1", translated: "I'm going to kill you", state: "ok" });
  assert.equal(row().querySelector(".gev-zh").textContent, "I'm going to kill you", "抽屉开着：译文到了就重画");
  // 弹幕栏只留 100 条：挤出去以后抽屉里的这一行仍然有译文
  for (let i = 0; i < 105; i++) page.push({ type: "comment", id: "x" + i, ts: T + 10, user: "u", text: "hola", state: "same" });
  assert.equal(page.el("comment-list").querySelectorAll(".cmt-item").filter((n) => n.dataset.cmtId === "c1").length, 0);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g2", lvl: "amber", cat: "insult", ts: T + 11,
    sample: { cid: "x3", user: "u", text: "hola" } }) });                  // 触发一次重画
  assert.equal(row().querySelector(".gev-zh").textContent, "I'm going to kill you");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：抽屉里的「重译」——没译出来的也能点；只有 Claude 引擎能点；进行中、说明都画在那一行", () => {
  const page = runDesktop();
  helloGuard(page);
  page.push(guardComment({ id: "c1", guard: RED_GUARD }));
  page.push({ type: "comment_update", id: "c1", state: "failed" });       // Haiku 没译出来
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", ts: T + 5 }) });
  page.el("guard-state").click();
  const row = () => page.el("guard-events").querySelectorAll(".guard-ev-row").find((r) => r.getAttribute("data-ev") === "g1");
  const redo = () => row().querySelector(".gev-redo");
  assert.deepEqual([redo().textContent, redo().disabled, redo().title], ["Retranslate", true, REDO_OFF],
                   "不是 Claude：灰的，悬停说明");
  page.push(CLAUDE_ENGINE);
  assert.deepEqual([redo().disabled, redo().title], [false, REDO_ON], "换成 Claude：抽屉里的按钮跟着亮");
  redo().click();
  assert.deepEqual(page.sent.filter((m) => m.type === "retranslate_comment"), [{ type: "retranslate_comment", id: "c1" }]);
  page.push({ type: "comment_redo", id: "c1", redo_state: "pending" });
  assert.deepEqual([redo().textContent, redo().disabled], ["Retranslating…", true]);
  // 成了：抽屉和弹幕栏都换上译文，弹幕栏里原文行回来
  page.push({ type: "comment_update", id: "c1", state: "ok", translated: "I'm going to kill you" });
  page.push({ type: "comment_redo", id: "c1", redo_state: "ok" });
  assert.equal(row().querySelector(".gev-zh").textContent, "I'm going to kill you");
  assert.deepEqual([redo().textContent, redo().disabled], ["Retranslate", false]);
  const item = page.el("comment-list").querySelector(".cmt-item");
  assert.equal(item.querySelector(".cmt-zh").textContent, "I'm going to kill you");
  assert.equal(item.querySelector(".cmt-orig").cls.includes("hidden"), false, "先失败、重译成了：原文行回来");
  // 没成：说明画在抽屉那一行，几秒后收起
  const sorry = "Couldn’t retranslate this comment: Claude Sonnet 5.5 returned HTTP 500. The current translation is unchanged.";
  page.push({ type: "comment_redo", id: "c1", redo_state: "failed", text: sorry });
  assert.equal(redo().textContent, "Couldn’t retranslate");
  assert.equal(row().querySelector(".gev-redo-note").textContent, sorry);
  assert.equal(row().querySelector(".gev-redo-note").getAttribute("role"), "status");
  page.fire(4000);                                   // 「重译失败」4 秒后回到「重译」
  assert.equal(redo().textContent, "Retranslate");
  assert.equal(row().querySelector(".gev-redo-note").textContent, sorry, "说明还在");
  page.fire(7000);
  assert.equal(row().querySelector(".gev-redo-note"), null);
  assert.deepEqual(leftoverChinese(page), []);
});


test("英文页：重译进行中断了线——重连（hello + 回放）后按钮不再卡在「重译中…」；抽屉开着时回放补上译文", () => {
  const page = runDesktop();
  helloGuard(page);
  page.push(CLAUDE_ENGINE);
  page.push(guardComment({ id: "c1", guard: RED_GUARD }));
  page.push({ type: "comment_update", id: "c1", state: "failed" });
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", ts: T + 5 }) });
  page.el("guard-state").click();
  page.push({ type: "comment_redo", id: "c1", redo_state: "pending" });
  const row = () => page.el("guard-events").querySelectorAll(".guard-ev-row").find((r) => r.getAttribute("data-ev") === "g1");
  assert.equal(row().querySelector(".gev-redo").textContent, "Retranslating…");
  // 断线期间服务端译完了，这次的 comment_redo 没收到；重连回放的是译好了的这一条
  helloGuard(page, { guard_events: { g1: guardEvent({ id: "g1", ts: T + 5 }) }, engine: CLAUDE_ENGINE });
  page.push(guardComment({ id: "c1", guard: RED_GUARD, state: "ok", translated: "I'm going to kill you", replay: true }));
  const btn = page.el("comment-list").querySelector(".cmt-redo");
  assert.deepEqual([btn.textContent, btn.disabled], ["Retranslate", false]);
  if (page.el("guard-drawer").cls.includes("hidden")) page.el("guard-state").click();
  assert.deepEqual([row().querySelector(".gev-redo").textContent, row().querySelector(".gev-redo").disabled],
                   ["Retranslate", false]);
  assert.equal(row().querySelector(".gev-zh").textContent, "I'm going to kill you");
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页：服务端只留最近 200 条弹幕的原文——更早的弹幕在抽屉里还看得到译文，但不再给「重译」", () => {
  const page = runDesktop();
  helloGuard(page);
  page.push(CLAUDE_ENGINE);
  page.push(guardComment({ id: "c1", guard: RED_GUARD }));
  page.push({ type: "comment_update", id: "c1", state: "ok", translated: "I'm going to kill you" });
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", ts: T + 5 }) });
  page.el("guard-state").click();
  const row = () => page.el("guard-events").querySelectorAll(".guard-ev-row").find((r) => r.getAttribute("data-ev") === "g1");
  assert.ok(row().querySelector(".gev-redo"));
  for (let i = 0; i < 200; i++) page.push({ type: "comment", id: "y" + i, ts: T + 10, user: "u", text: "hola", state: "same" });
  page.push({ type: "guard_event", ev: guardEvent({ id: "g2", lvl: "amber", cat: "insult", ts: T + 11,
    sample: { cid: "y3", user: "u", text: "hola" } }) });                  // 触发一次重画
  assert.equal(row().querySelector(".gev-redo"), null, "第 201 条以前的弹幕：服务端已经不记得原文");
  assert.equal(row().querySelector(".gev-zh").textContent, "I'm going to kill you");
});

test("英文页：抽屉里正显示着的那一行，重译的说明只出现在那一行，不再同时挤到页脚", () => {
  const page = runDesktop();
  helloGuard(page);
  page.push(CLAUDE_ENGINE);
  page.push(guardComment({ id: "c1", guard: RED_GUARD }));
  page.push({ type: "comment_update", id: "c1", state: "failed" });
  for (let i = 0; i < 105; i++) page.push({ type: "comment", id: "z" + i, ts: T + 10, user: "u", text: "hola", state: "same" });
  page.advance(1500);
  page.push({ type: "guard_event", ev: guardEvent({ id: "g1", ts: T + 5 }) });
  page.el("guard-state").click();
  const footer = () => page.el("app-version").textContent;
  const before = footer();
  const sorry = "Couldn’t retranslate this comment: Claude Sonnet 5.5 returned HTTP 500. The comment still shows the original text.";
  page.push({ type: "comment_redo", id: "c1", redo_state: "failed", text: sorry });
  const row = page.el("guard-events").querySelectorAll(".guard-ev-row").find((r) => r.getAttribute("data-ev") === "g1");
  assert.equal(row.querySelector(".gev-redo-note").textContent, sorry);
  assert.equal(footer(), before, "抽屉里已经有了，页脚不再重复");
  assert.deepEqual(leftoverChinese(page), []);
});
