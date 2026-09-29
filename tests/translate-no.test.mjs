// 承载数据的节点标 translate="no"（spec §4.1 R7、§6、§13 C6）。
//
// translate="no" 豁免的是子树里的文字：英文界面的检查（G5/G10）跳过它们，浏览器自带的网页翻译
// 也不去改主播名和译文。所以标多了和标少了一样有害——把界面提示标上，漏翻就查不出来。钉三件事：
//   1. index.html / viewer.html 里静态标了 translate="no" 的，正好是这几个元素；
//   2. 桌面 app.js 在 node:vm 里真的跑一遍（按 index.html 的顺序先载入前面那串脚本），推一串
//      WebSocket 消息：数据节点是 "no"，「翻译中…」这类界面提示是 "yes"，拆开的句子拼回来与原来
//      逐字相同（闸关着，中文界面）；
//   3. 手机 viewer.js 同上。
// 假 DOM 只实现这几条路径用得到的接口；不认识的选择器直接抛错，免得静默走偏。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const web = (name) => readFileSync(new URL("../web/" + name, import.meta.url), "utf8");

// 开始标签：属性值里可能有 < >（data-en-html），引号里的整段跳过
const START_TAG = /<([a-z]+)\b((?:[^>"]|"[^"]*")*)>/g;

// ---- 1. 静态标记 ----------------------------------------------------------------------------
function staticMarks(html) {
  const bare = html.replace(/<!--[\s\S]*?-->/g, "").replace(/<script>[\s\S]*?<\/script>/g, "");
  return [...bare.matchAll(START_TAG)]
    .filter((m) => /\stranslate="no"/.test(m[2]))
    .map((m) => {
      const id = /\sid="([^"]+)"/.exec(m[2]);
      const value = /\svalue="([^"]*)"/.exec(m[2]);
      return m[1] + (id ? "#" + id[1] : value ? "[value=" + value[1] + "]" : "");
    });
}

test("index.html：静态标 translate=\"no\" 的正好是这几处数据区", () => {
  assert.deepEqual(staticMarks(web("index.html")), [
    "select#target-lang",        // 选项是各语言的自称；select 自己的 aria-label 照常要英文
    "div#share-url",             // 同看链接
    "ul#share-ip-list",          // 候选局域网地址，只有地址按钮
    "code#fix-command-text",     // 带真实路径的命令
    "option[value=zh]",          // 界面语言行的两个语言名（C5）
    "option[value=en]",
    "div#live-translated",       // 底部大字幕：译文
    "div#live-original",         //             原文
  ]);
});

test("viewer.html：静态标记只有切换按钮里的语言名（其余数据节点由 viewer.js 标）", () => {
  assert.deepEqual(staticMarks(web("viewer.html")), ["span#lang-toggle-text"]);
});

// ---- 假 DOM ---------------------------------------------------------------------------------
class FakeText {
  constructor(v) { this.nodeType = 3; this.nodeValue = v == null ? "" : String(v); this.parentNode = null; }
  get textContent() { return this.nodeValue; }
}
class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.nodeType = 1; this.childNodes = []; this.parentNode = null;
    this.attrs = new Map(); this.cls = []; this.dataset = {}; this.style = {}; this.listeners = {};
    this.value = ""; this.disabled = false; this.checked = false; this.selectedIndex = 0;
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
  set id(v) { this.attrs.set("id", String(v)); }
  get title() { return this.attrs.get("title") || ""; }
  set title(v) { this.attrs.set("title", String(v)); }
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
  click() { if (!this.disabled) (this.listeners.click || []).slice().forEach((cb) => cb({ target: this, preventDefault() {}, stopPropagation() {} })); }
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

// 在一个新的 vm 上下文里按顺序跑页面脚本（浏览器里多个 <script> 共用同一个全局）。
// 带 id 的元素照页面源码建好，标签名和属性（class、hidden、translate…）与真页面一致，
// 不建树：脚本只经 getElementById 拿它们
function runPage(page, scripts) {
  const byId = new Map();
  const bare = web(page).replace(/<!--[\s\S]*?-->/g, "");
  for (const m of bare.matchAll(START_TAG)) {
    if (!/\sid="/.test(m[2])) continue;
    const el = new FakeEl(m[1]);
    for (const a of m[2].matchAll(/([\w-]+)="([^"]*)"/g)) {
      if (a[1] === "class") el.className = a[2]; else el.setAttribute(a[1], a[2]);
    }
    byId.set(el.id, el);
  }
  const html = new FakeEl("html");
  html.setAttribute("lang", "zh-CN");
  const document = {
    documentElement: html, body: new FakeEl("body"), title: "TikTok 直播同传", hidden: false,
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => new FakeEl(tag),
    createElementNS: (_, tag) => new FakeEl(tag),
    createTextNode: (v) => new FakeText(v),
    addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], hasFocus: () => true,
  };
  const sockets = [];
  function FakeWebSocket(url) { this.url = url; this.readyState = 1; sockets.push(this); }
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.prototype.send = function () {};
  FakeWebSocket.prototype.close = function () {};
  const storage = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) }; };
  const sandbox = {
    document, console, WebSocket: FakeWebSocket,
    location: { host: "127.0.0.1:0", protocol: "http:", hash: "#k=fake-token-not-a-real-secret-0000000000", reload() {} },
    navigator: {}, localStorage: storage(), sessionStorage: storage(),
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, requestAnimationFrame: () => 0,
    getComputedStyle: () => ({ getPropertyValue: () => "17px" }),
    addEventListener() {}, removeEventListener() {}, confirm: () => false,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const name of scripts) vm.runInContext(web(name), sandbox, { filename: "web/" + name });
  assert.equal(sockets.length, 1, "页面加载时应该连一次 WebSocket");
  const ws = sockets[0];
  ws.onopen();
  return {
    el: (id) => byId.get(id),
    push: (msg) => ws.onmessage({ data: JSON.stringify(msg) }),
  };
}

const T = 1759000000;
function hhmmss(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => (n < 10 ? "0" : "") + n;
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}
const mark = (el) => el.getAttribute("translate");
// 子树里标了 translate="no" 的节点的文字，按文档顺序
const dataTexts = (el) => el.descendants().filter((n) => mark(n) === "no").map((n) => n.textContent);
const byClass = (root, cls) => root.querySelector("." + cls);

// ---- 2. 桌面 app.js ---------------------------------------------------------------------------
function desktopScripts() {
  const html = web("index.html");
  const names = [...html.matchAll(/<script src="\/static\/([\w.-]+)\?v=\d+"><\/script>/g)].map((m) => m[1]);
  assert.equal(names[names.length - 1], "app.js");
  return names;
}
const ROOM = "https://www.tiktok.com/@maria.ventas/live";

function desktopLive() {
  const page = runPage("index.html", desktopScripts());
  page.push({ type: "hello", config: {
    status: { state: "live", detail: "" }, room_url: ROOM, alerts_enabled: true,
    alerts_session: { session: "s1", streamer: "maria.ventas", total: 0 },
    brand_options: [{ id: "b1", name: "中文品牌" }], brands: {}, active_brand: { id: "b1", name: "中文品牌" },
    recent_rooms: [{ streamer: "maria.ventas", url: ROOM },
                   { streamer: "otra.tienda", url: "https://www.tiktok.com/@otra.tienda/live" }],
    disk: { free: 5e10, items: [
      { id: "hf:x", kind: "hf", label: "mlx-community/whisper", size: 3e9, role: "app", note: "" },
      { id: "ollama:y", kind: "ollama", label: "hy-mt2:7b", size: 5e9, role: "other", note: "" },
      { id: "logs:old", kind: "logs", label: "早于 30 天的会话审计日志（3 个文件）", size: 1e6, role: "app", note: "" }] },
  } });
  page.push({ type: "caption", id: 1, ts: T, original: "hola a todos", translate_state: "pending" });
  page.push({ type: "alert", alert_id: "a1", ts: T + 1, term: "milagro", tier: "exact", streamer: "maria.ventas",
              session: "s1", context: "es un milagro" });
  page.push({ type: "alert", alert_id: "a2", ts: T + 2, term: "cura", tier: "fuzzy", session: "s1", context: "lo cura" });
  page.push({ type: "comment", id: "c1", ts: T + 3, user: "李四", text: "cuanto cuesta", state: "pending" });
  page.push({ type: "comment", id: "c2", ts: T + 4, user: "Ana", text: "que bonito", state: "pending" });
  return page;
}

test("桌面：状态胶囊、本场品牌、品牌下拉——只包数据那一截，拼起来与原来相同", () => {
  const page = desktopLive();
  const status = page.el("status-text");
  assert.equal(status.textContent, "直播中 · @maria.ventas");
  assert.equal(mark(status), null, "「直播中」是界面文字，胶囊本身不标");
  assert.deepEqual(dataTexts(status), ["@maria.ventas"]);

  const brand = page.el("active-brand-tag");
  assert.equal(brand.textContent, "品牌 · 中文品牌");
  assert.equal(brand.title, "中文品牌");
  assert.equal(mark(brand), null);
  assert.deepEqual(dataTexts(brand), ["中文品牌"]);

  for (const id of ["brand-select", "switch-brand-select"]) {
    const opts = page.el(id).options.map((o) => [o.textContent, mark(o)]);
    assert.deepEqual(opts, [["不限（默认）", null], ["中文品牌", "no"]], id);
  }
});

test("桌面：字幕原文是数据；译文回来前的「翻译中…」是界面提示，回来后改标数据", () => {
  const page = desktopLive();
  const card = byClass(page.el("history"), "cap");
  assert.equal(mark(byClass(card, "orig")), "no");
  const trans = byClass(card, "trans");
  assert.deepEqual([trans.textContent, mark(trans)], ["翻译中…", "yes"]);
  page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok" });
  assert.deepEqual([trans.textContent, mark(trans)], ["大家好", "no"]);
});

test("桌面：报警的词条、主播名、原话、译文是数据；「翻译中…」和译文失败的说明不是", () => {
  const page = desktopLive();
  const [second, first] = page.el("alert-list").children;       // 新的在上
  const head = byClass(first, "alert-head");
  assert.equal(head.children.length, 2, ".alert-head 是 flex：分级胶囊之外仍只有一个子项");
  assert.equal(head.children[1].textContent, "「milagro」 " + hhmmss(T + 1) + " @maria.ventas");
  assert.deepEqual(dataTexts(head), ["milagro", "@maria.ventas"]);
  assert.equal(byClass(second, "alert-head").children[1].textContent, "「cura」 " + hhmmss(T + 2));
  assert.equal(mark(byClass(first, "alert-ctx")), "no");

  const zh = byClass(first, "alert-zh");
  assert.deepEqual([zh.textContent, mark(zh)], ["翻译中…", "yes"]);
  page.push({ type: "alert_update", alert_id: "a1", context_zh: "这是奇迹" });
  assert.deepEqual([zh.textContent, mark(zh)], ["这是奇迹", "no"]);
  page.push({ type: "alert_update", alert_id: "a2", why: "超时" });
  const failed = byClass(second, "alert-zh");
  assert.deepEqual([failed.textContent, mark(failed)], ["译文失败（超时）——请看上面的原话", "yes"]);
});

test("桌面：弹幕的观众名、原文、译文是数据；「翻译中…」不是", () => {
  const page = desktopLive();
  const [one, two] = page.el("comment-list").children;
  const head = byClass(one, "cmt-head");
  assert.equal(head.textContent, "李四 " + hhmmss(T + 3));
  assert.deepEqual(dataTexts(head), ["李四"]);
  assert.equal(mark(byClass(one, "cmt-orig")), "no");
  const zh = byClass(one, "cmt-zh");
  assert.deepEqual([zh.textContent, mark(zh)], ["翻译中…", "yes"]);
  page.push({ type: "comment_update", id: "c1", state: "ok", translated: "多少钱" });
  assert.deepEqual([zh.textContent, mark(zh)], ["多少钱", "no"]);
  page.push({ type: "comment_update", id: "c2", state: "failed" });
  const other = byClass(two, "cmt-zh");
  assert.deepEqual([other.textContent, mark(other)], ["que bonito", "no"], "译不出来就显示原文本身，仍是数据");
});

test("桌面：最近直播间 chip 是主播名；换主播面板里「（当前）」不算数据", () => {
  const page = desktopLive();
  const chips = page.el("recent-list").children;
  assert.deepEqual(chips.map((c) => [c.textContent, mark(c)]), [["@maria.ventas", "no"], ["@otra.tienda", "no"]]);
  assert.equal(chips[0].title, "点击开始翻译 @maria.ventas");

  page.el("switch-btn").click();                                 // 打开面板才画它的列表
  const [current, other] = page.el("switch-recent-list").children;
  assert.equal(current.textContent, "@maria.ventas（当前）");
  assert.equal(mark(current), null);
  assert.equal(current.children.length, 1, ".recent-chip 是 inline-flex：里面仍只有一个子项");
  assert.deepEqual(dataTexts(current), ["@maria.ventas"]);
  assert.deepEqual([other.textContent, mark(other)], ["@otra.tienda", "no"]);
});

test("桌面：磁盘列表按 kind 分——模型名是数据，日志项的标签是界面句子（spec §17 第 6 条）", () => {
  const page = desktopLive();
  const names = page.el("disk-list").querySelectorAll(".disk-name").map((n) => [n.textContent, mark(n)]);
  assert.deepEqual(names, [["mlx-community/whisper", "no"], ["hy-mt2:7b", "no"],
                           ["早于 30 天的会话审计日志（3 个文件）", null]]);
});

// ---- 3. 手机 viewer.js ------------------------------------------------------------------------
function phoneLive() {
  const page = runPage("viewer.html", ["viewer.js"]);
  page.push({ type: "viewer_hello", ok: true, ts: T, share_since: 0, viewers: 1, max_viewers: 12, read_only: true });
  page.push({ type: "status", state: "live", ts: T });
  page.push({ type: "caption", id: 1, ts: T, original: "hola a todos", translate_state: "pending" });
  page.push({ type: "caption", id: 2, ts: T + 1, original: "gracias", failed: true, why: "超时" });
  page.push({ type: "alert", alert_id: "a1", ts: T + 2, term: "milagro", tier: "exact", context: "es un milagro" });
  page.push({ type: "alert", alert_id: "a2", ts: T + 3, term: "cura", tier: "fuzzy", context: "lo cura" });
  page.push({ type: "comment", id: "c1", ts: T + 4, user: "李四", text: "cuanto cuesta", state: "pending" });
  return page;
}

test("手机：字幕原文和译文是数据；「翻译中…」和译文失败的说明不是", () => {
  const page = phoneLive();
  const [first, second] = page.el("caption-list").children;
  assert.equal(mark(byClass(first, "cap-orig")), "no");
  const trans = byClass(first, "cap-trans");
  assert.deepEqual([trans.textContent, mark(trans)], ["翻译中…", "yes"]);
  page.push({ type: "caption_update", id: 1, translated: "大家好" });
  assert.deepEqual([trans.textContent, mark(trans)], ["大家好", "no"]);
  const failed = byClass(second, "cap-trans");
  assert.deepEqual([failed.textContent, mark(failed)], ["译文失败（超时）——请看上面的原话", "yes"]);
});

test("手机：报警头只把词条包成数据，拼起来与原来相同；原话、译文是数据", () => {
  const page = phoneLive();
  const [second, first] = page.el("alert-list").children;
  const label = byClass(first, "alert-head-label");
  assert.equal(label.children.length, 2, ".alert-head-label 是 inline-flex：图标 + 一段文字，子项不变");
  assert.equal(label.textContent, "精确 「milagro」 " + hhmmss(T + 2));
  assert.deepEqual(dataTexts(label), ["milagro"]);
  assert.equal(mark(byClass(first, "alert-ctx")), "no");

  const zh = byClass(first, "alert-zh");
  assert.deepEqual([zh.textContent, mark(zh)], ["中文正在补…", "yes"]);
  page.push({ type: "alert_update", alert_id: "a1", context_zh: "这是奇迹" });
  assert.deepEqual([zh.textContent, mark(zh)], ["这是奇迹", "no"]);
  page.push({ type: "alert_update", alert_id: "a2", failed: true, why: "超时" });
  const failed = byClass(second, "alert-zh");
  assert.deepEqual([failed.textContent, mark(failed)], ["中文译不出来（超时）——请看上面的原话", "yes"]);
});

test("手机：弹幕的观众名、原文、译文是数据；「翻译中…」不是", () => {
  const page = phoneLive();
  const item = page.el("comment-list").children[0];
  const head = byClass(item, "cmt-head");
  assert.equal(head.textContent, "李四 " + hhmmss(T + 4));
  assert.deepEqual(dataTexts(head), ["李四"]);
  assert.equal(mark(byClass(item, "cmt-orig")), "no");
  const zh = byClass(item, "cmt-zh");
  assert.deepEqual([zh.textContent, mark(zh)], ["翻译中…", "yes"]);
  page.push({ type: "comment_update", id: "c1", state: "ok", translated: "多少钱" });
  assert.deepEqual([zh.textContent, mark(zh)], ["多少钱", "no"]);
});
