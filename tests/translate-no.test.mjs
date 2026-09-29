// 承载数据的节点怎么标（spec §4.1 R7、§6、§13 C6；用户决定 6 改了 §6，C6b）。
//
// 数据分两类，标法不同：
//   - 名字：主播名、观众名、品牌名、违禁词条、模型名，外加静态页里的链接、地址、命令、语言名，
//     标 translate="no"。浏览器自带的「翻译此页」不去改名字，英文界面的检查（G5/G10）也跳过它们；
//   - 正文：字幕原文和译文、弹幕正文和译文、报警原话和译文，**不标** translate——用浏览器翻译看
//     手机同看页的人读的就是这些，标了等于把这条退路堵上。只带 class "i18n-data"，英文界面的检查
//     按它豁免，不算漏翻。
// 标多了和标少了一样有害：把界面提示标上，漏翻就查不出来。钉五件事：
//   1. index.html / viewer.html 里静态标了 translate="no" 的正好是这几个名字类元素，静态带正文
//      class 的正好是底部大字幕两行；
//   2. 桌面 app.js 在 node:vm 里真的跑一遍（按 index.html 的顺序先载入前面那串脚本），推一串
//      WebSocket 消息：名字是 translate="no"；正文不带 translate、带 class；「翻译中…」这类界面
//      提示两样都不带；拆开的句子拼回来与原来逐字相同（闸关着，中文界面）；
//   3. 手机 viewer.js 同上；
//   4. 真的 G10 扫描脚本（tests/i18n_dom/scan.js）扫这两页渲染出来的 DOM：名字和正文里的中文
//      一条都不计入，界面提示照样计入——豁免靠的是 class，不是 translate="no"；
//   5. 两种标记只落在叶子上：扫描豁免的是整棵子树，标记挪到外层会连带豁免同一层的界面文字。
// 假 DOM 只实现这几条路径用得到的接口；不认识的选择器直接抛错，免得静默走偏。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// Windows 的 Git 默认 core.autocrlf=true，检出的是 CRLF：统一成 \n，下面的正则只写 \n
const read = (path) => readFileSync(new URL("../" + path, import.meta.url), "utf8").replace(/\r\n/g, "\n");
const web = (name) => read("web/" + name);

const BODY_CLASS = "i18n-data";   // 与 tests/i18n_rules.py 的 DATA_TEXT_CLASS 相同（那边另有一条钉五处一致）

// 开始标签：属性值里可能有 < >（data-en-html），引号里的整段跳过
const START_TAG = /<([a-z]+)\b((?:[^>"]|"[^"]*")*)>/g;

// ---- 1. 静态标记 ----------------------------------------------------------------------------
function staticTags(html, keep) {
  const bare = html.replace(/<!--[\s\S]*?-->/g, "").replace(/<script>[\s\S]*?<\/script>/g, "");
  return [...bare.matchAll(START_TAG)]
    .filter((m) => keep(m[2]))
    .map((m) => {
      const id = /\sid="([^"]+)"/.exec(m[2]);
      const value = /\svalue="([^"]*)"/.exec(m[2]);
      return m[1] + (id ? "#" + id[1] : value ? "[value=" + value[1] + "]" : "");
    });
}
const staticNames = (html) => staticTags(html, (attrs) => /\stranslate="no"/.test(attrs));
const staticBodies = (html) => staticTags(html, (attrs) => {
  const cls = /\sclass="([^"]*)"/.exec(attrs);
  return !!cls && cls[1].split(/\s+/).includes(BODY_CLASS);
});

test("index.html：静态标 translate=\"no\" 的正好是这几处名字、链接、地址", () => {
  assert.deepEqual(staticNames(web("index.html")), [
    "select#target-lang",        // 选项是各语言的自称；select 自己的 aria-label 照常要英文
    "div#share-url",             // 同看链接
    "ul#share-ip-list",          // 候选局域网地址，只有地址按钮
    "code#fix-command-text",     // 带真实路径的命令
    "option[value=zh]",          // 界面语言行的两个语言名（C5）
    "option[value=en]",
  ]);
});

test("index.html：底部大字幕是正文——不标 translate=\"no\"，带正文 class", () => {
  assert.deepEqual(staticBodies(web("index.html")), ["div#live-translated", "div#live-original"]);
});

test("viewer.html：静态标记只有切换按钮里的语言名（正文节点由 viewer.js 标）", () => {
  assert.deepEqual(staticNames(web("viewer.html")), ["span#lang-toggle-text"]);
  assert.deepEqual(staticBodies(web("viewer.html")), []);
});

// ---- 假 DOM ---------------------------------------------------------------------------------
class FakeText {
  constructor(v) { this.nodeType = 3; this.nodeValue = v == null ? "" : String(v); this.parentNode = null; }
  get textContent() { return this.nodeValue; }
  get parentElement() { return this.parentNode; }
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
  // 类数组（scan.js 的 where() 读 length 和 [0]），外加 add/remove/toggle/contains
  get classList() {
    const el = this;
    const set = (c, on) => { el.cls = el.cls.filter((x) => x !== c); if (on) el.cls.push(c); };
    return Object.assign([...el.cls], {
      add: (...cs) => cs.forEach((c) => set(c, true)),
      remove: (...cs) => cs.forEach((c) => set(c, false)),
      toggle: (c, on) => { const v = on === undefined ? !el.cls.includes(c) : !!on; set(c, v); return v; },
      contains: (c) => el.cls.includes(c),
    });
  }
  get id() { return this.attrs.get("id") || ""; }
  set id(v) { this.attrs.set("id", String(v)); }
  get title() { return this.attrs.get("title") || ""; }
  set title(v) { this.attrs.set("title", String(v)); }
  get parentElement() { return this.parentNode; }
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
    if (s === "*") return true;
    if ((m = /^\.([\w-]+)$/.exec(s))) return el.cls.includes(m[1]);
    if ((m = /^\[data-([\w-]+)="([^"]*)"\]$/.exec(s))) {
      return String(el.dataset[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())]) === m[2];
    }
    if ((m = /^\[([\w-]+)="([^"]*)"\]$/.exec(s))) return el.getAttribute(m[1]) === m[2];
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
    all: () => [...byId.values()],
    push: (msg) => ws.onmessage({ data: JSON.stringify(msg) }),
  };
}

const T = 1759000000;
function hhmmss(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => (n < 10 ? "0" : "") + n;
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}
// 一个节点的标法："name"（translate="no"）、"body"（只带正文 class）、null（界面文字，两样都不带）。
// 旧写法在界面提示上写 translate="yes"，新写法不写；两种标法也不许叠在同一个节点上
function kind(el) {
  const translate = el.getAttribute("translate");
  const body = el.classList.contains(BODY_CLASS);
  assert.ok(translate === null || translate === "no", "translate 只许不写或写 no：" + translate);
  assert.ok(!(translate === "no" && body), "名字和正文两种标法叠在了一个节点上");
  return translate === "no" ? "name" : body ? "body" : null;
}
// 子树里标成名字的节点的文字，按文档顺序。子树含根：整个报警头、弹幕头被标成名字时，
// 第一项就是整段文字，断言当场变红
const nameTexts = (el) => [el, ...el.descendants()].filter((n) => kind(n) === "name").map((n) => n.textContent);
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
// 重连或程序重启后的回放：CaptionServer 已把 alert_update 并进留存的那条报警（app/server.py），
// 译文随报警一起到，走 renderAlert 而不是 updateAlert
const REPLAYED_ALERT = { type: "alert", alert_id: "a4", ts: T + 8, term: "gratis", tier: "variant", session: "s1",
                         context: "todo gratis", context_zh: "全部免费", replay: true };

test("桌面：状态胶囊、本场品牌、品牌下拉——只把名字那一截标 translate=\"no\"，拼起来与原来相同", () => {
  const page = desktopLive();
  const status = page.el("status-text");
  assert.equal(status.textContent, "直播中 · @maria.ventas");
  assert.equal(kind(status), null, "「直播中」是界面文字，胶囊本身不标");
  assert.deepEqual(nameTexts(status), ["@maria.ventas"]);

  const brand = page.el("active-brand-tag");
  assert.equal(brand.textContent, "品牌 · 中文品牌");
  assert.equal(brand.title, "中文品牌");
  assert.equal(kind(brand), null);
  assert.deepEqual(nameTexts(brand), ["中文品牌"]);

  for (const id of ["brand-select", "switch-brand-select"]) {
    const opts = page.el(id).options.map((o) => [o.textContent, kind(o)]);
    assert.deepEqual(opts, [["不限（默认）", null], ["中文品牌", "name"]], id);
  }
});

test("桌面：字幕原文和译文是正文（留给浏览器翻译）；译文回来前的「翻译中…」是界面提示", () => {
  const page = desktopLive();
  const card = byClass(page.el("history"), "cap");
  assert.equal(kind(byClass(card, "orig")), "body");
  const trans = byClass(card, "trans");
  assert.deepEqual([trans.textContent, kind(trans)], ["翻译中…", null]);
  page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok" });
  assert.deepEqual([trans.textContent, kind(trans)], ["大家好", "body"]);
  // 底部大字幕两行静态带正文 class，里面放的永远是译文和原文
  assert.deepEqual([page.el("live-translated").textContent, kind(page.el("live-translated"))], ["大家好", "body"]);
  assert.deepEqual([page.el("live-original").textContent, kind(page.el("live-original"))], ["hola a todos", "body"]);
});

test("桌面：报警的词条、主播名是名字；原话、译文是正文；「翻译中…」和译文失败的说明是界面提示", () => {
  const page = desktopLive();
  const [second, first] = page.el("alert-list").children;       // 新的在上
  const head = byClass(first, "alert-head");
  assert.equal(head.children.length, 2, ".alert-head 是 flex：分级胶囊之外仍只有一个子项");
  assert.equal(head.children[1].textContent, "「milagro」 " + hhmmss(T + 1) + " @maria.ventas");
  assert.deepEqual(nameTexts(head), ["milagro", "@maria.ventas"]);
  assert.equal(byClass(second, "alert-head").children[1].textContent, "「cura」 " + hhmmss(T + 2));
  assert.equal(kind(byClass(first, "alert-ctx")), "body");

  const zh = byClass(first, "alert-zh");
  assert.deepEqual([zh.textContent, kind(zh)], ["翻译中…", null]);
  page.push({ type: "alert_update", alert_id: "a1", context_zh: "这是奇迹" });
  assert.deepEqual([zh.textContent, kind(zh)], ["这是奇迹", "body"]);
  page.push({ type: "alert_update", alert_id: "a2", why: "超时" });
  const failed = byClass(second, "alert-zh");
  assert.deepEqual([failed.textContent, kind(failed)], ["译文失败（超时）——请看上面的原话", null]);

  page.push(REPLAYED_ALERT);
  const replayed = byClass(page.el("alert-list").querySelector('[data-alert-id="a4"]'), "alert-zh");
  assert.deepEqual([replayed.textContent, kind(replayed)], ["全部免费", "body"], "回放时译文随报警一起到，仍是正文");
});

test("桌面：弹幕的观众名是名字；原文、译文是正文；「翻译中…」是界面提示", () => {
  const page = desktopLive();
  const [one, two] = page.el("comment-list").children;
  const head = byClass(one, "cmt-head");
  assert.equal(head.textContent, "李四 " + hhmmss(T + 3));
  assert.deepEqual(nameTexts(head), ["李四"]);
  assert.equal(kind(byClass(one, "cmt-orig")), "body");
  const zh = byClass(one, "cmt-zh");
  assert.deepEqual([zh.textContent, kind(zh)], ["翻译中…", null]);
  page.push({ type: "comment_update", id: "c1", state: "ok", translated: "多少钱" });
  assert.deepEqual([zh.textContent, kind(zh)], ["多少钱", "body"]);
  page.push({ type: "comment_update", id: "c2", state: "failed" });
  const other = byClass(two, "cmt-zh");
  assert.deepEqual([other.textContent, kind(other)], ["que bonito", "body"], "译不出来就显示原文本身，仍是正文");
});

test("桌面：最近直播间 chip 是主播名；换主播面板里「（当前）」不算名字", () => {
  const page = desktopLive();
  const chips = page.el("recent-list").children;
  assert.deepEqual(chips.map((c) => [c.textContent, kind(c)]), [["@maria.ventas", "name"], ["@otra.tienda", "name"]]);
  assert.equal(chips[0].title, "点击开始翻译 @maria.ventas");

  page.el("switch-btn").click();                                 // 打开面板才画它的列表
  const [current, other] = page.el("switch-recent-list").children;
  assert.equal(current.textContent, "@maria.ventas（当前）");
  assert.equal(kind(current), null);
  assert.equal(current.children.length, 1, ".recent-chip 是 inline-flex：里面仍只有一个子项");
  assert.deepEqual(nameTexts(current), ["@maria.ventas"]);
  assert.deepEqual([other.textContent, kind(other)], ["@otra.tienda", "name"]);
});

test("桌面：磁盘列表按 kind 分——模型名是名字，日志项的标签是界面句子（spec §17 第 6 条）", () => {
  const page = desktopLive();
  const names = page.el("disk-list").querySelectorAll(".disk-name").map((n) => [n.textContent, kind(n)]);
  assert.deepEqual(names, [["mlx-community/whisper", "name"], ["hy-mt2:7b", "name"],
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

test("手机：字幕原文和译文是正文；「翻译中…」和译文失败的说明是界面提示", () => {
  const page = phoneLive();
  const [first, second] = page.el("caption-list").children;
  assert.equal(kind(byClass(first, "cap-orig")), "body");
  const trans = byClass(first, "cap-trans");
  assert.deepEqual([trans.textContent, kind(trans)], ["翻译中…", null]);
  page.push({ type: "caption_update", id: 1, translated: "大家好" });
  assert.deepEqual([trans.textContent, kind(trans)], ["大家好", "body"]);
  const failed = byClass(second, "cap-trans");
  assert.deepEqual([failed.textContent, kind(failed)], ["译文失败（超时）——请看上面的原话", null]);
});

test("手机：报警头只把词条标成名字，拼起来与原来相同；原话、译文是正文", () => {
  const page = phoneLive();
  const [second, first] = page.el("alert-list").children;
  const label = byClass(first, "alert-head-label");
  assert.equal(label.children.length, 2, ".alert-head-label 是 inline-flex：图标 + 一段文字，子项不变");
  assert.equal(label.textContent, "精确 「milagro」 " + hhmmss(T + 2));
  assert.deepEqual(nameTexts(label), ["milagro"]);
  assert.equal(kind(byClass(first, "alert-ctx")), "body");

  const zh = byClass(first, "alert-zh");
  assert.deepEqual([zh.textContent, kind(zh)], ["中文正在补…", null]);
  page.push({ type: "alert_update", alert_id: "a1", context_zh: "这是奇迹" });
  assert.deepEqual([zh.textContent, kind(zh)], ["这是奇迹", "body"]);
  page.push({ type: "alert_update", alert_id: "a2", failed: true, why: "超时" });
  const failed = byClass(second, "alert-zh");
  assert.deepEqual([failed.textContent, kind(failed)], ["中文译不出来（超时）——请看上面的原话", null]);
});

test("手机：弹幕的观众名是名字；原文、译文是正文；「翻译中…」是界面提示", () => {
  const page = phoneLive();
  const item = page.el("comment-list").children[0];
  const head = byClass(item, "cmt-head");
  assert.equal(head.textContent, "李四 " + hhmmss(T + 4));
  assert.deepEqual(nameTexts(head), ["李四"]);
  assert.equal(kind(byClass(item, "cmt-orig")), "body");
  const zh = byClass(item, "cmt-zh");
  assert.deepEqual([zh.textContent, kind(zh)], ["翻译中…", null]);
  page.push({ type: "comment_update", id: "c1", state: "ok", translated: "多少钱" });
  assert.deepEqual([zh.textContent, kind(zh)], ["多少钱", "body"]);
});

// ---- 4. 真的 G10 扫描脚本扫渲染出来的 DOM -------------------------------------------------------
// scan.js 在无头 Chrome 里跑（只在 CI 或 TLT_DOM_SCAN=1）；这里把同一份文件放进 node:vm，交给它
// 一棵假树：页面里每个带 id 的元素（连同脚本画进去的子树）挂在一个 <body> 下。这一页是中文界面，
// 所以它会报出一堆中文——要看的是报了哪些：界面提示必须报，名字和正文一条都不许报
function runScan(page) {
  const html = new FakeEl("html");
  const body = new FakeEl("body");
  html.appendChild(body);
  for (const el of page.all()) if (!el.parentNode) body.appendChild(el);
  let fire = null;
  const document = {
    documentElement: html, body, title: "",
    createElement: (tag) => new FakeEl(tag),
    createTreeWalker(root) {                    // 只用到 SHOW_TEXT：文档顺序的文字节点
      const texts = [];
      (function walk(n) { n.childNodes.forEach((c) => (c.nodeType === 3 ? texts.push(c) : c.nodeType === 1 && walk(c))); })(root);
      let i = 0;
      return { nextNode: () => texts[i++] || null };
    },
    querySelectorAll: (sel) => (matches(html, sel) ? [html] : []).concat(html.querySelectorAll(sel)),
  };
  const sandbox = { document, NodeFilter: { SHOW_TEXT: 4 }, setTimeout: (fn) => { fire = fn; } };
  sandbox.window = sandbox;
  vm.runInNewContext(read("tests/i18n_dom/scan.js"), sandbox, { filename: "tests/i18n_dom/scan.js" });
  assert.equal(typeof fire, "function", "scan.js 应该排一个定时器再扫");
  fire();
  const out = body.children.find((c) => c.id === "i18n-scan");
  assert.ok(out, "scan.js 没写出 #i18n-scan");
  return JSON.parse(out.textContent).cjk;
}
function assertScan(found, data, ui) {
  const counted = found.filter((x) => data.some((d) => x.text.includes(d)));
  assert.deepEqual(counted, [], "名字或正文被当成漏翻的界面文字");
  for (const [text, where] of ui) {
    assert.ok(found.some((x) => x.text === text && where.test(x.at)),
              "界面提示「" + text + "」没被扫到（" + where + "）：\n" + JSON.stringify(found, null, 1));
  }
}

// 各种状态都推齐的两页：译文已回的、停在「翻译中…」的、翻译失败的、回放时译文随报警一起到的。
// 扫描和第 5 节的叶子检查看的是同一页
function desktopFull() {
  const page = desktopLive();
  page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok" });
  page.push({ type: "caption", id: 2, ts: T + 5, original: "你们好", translate_state: "pending" });   // 中文原文，停在「翻译中…」
  page.push({ type: "caption", id: 3, ts: T + 5, original: "no se oye", translate_state: "pending" });
  page.push({ type: "caption_update", id: 3, translate_state: "failed" });                         // 状态胶囊「翻译失败」
  page.push({ type: "alert_update", alert_id: "a1", context_zh: "这是奇迹" });
  page.push({ type: "alert_update", alert_id: "a2", why: "超时" });
  page.push({ type: "alert", alert_id: "a3", ts: T + 6, term: "治愈", tier: "exact", streamer: "maria.ventas",
              session: "s1", context: "这能治愈" });                                                // 停在「翻译中…」
  page.push(REPLAYED_ALERT);
  page.push({ type: "comment_update", id: "c1", state: "ok", translated: "多少钱" });
  page.push({ type: "comment", id: "c3", ts: T + 7, user: "王五", text: "好看", state: "same" });
  page.el("switch-btn").click();
  return page;
}
function phoneFull() {
  const page = phoneLive();
  page.push({ type: "caption_update", id: 1, translated: "大家好" });
  page.push({ type: "caption", id: 3, ts: T + 5, original: "你们好", translate_state: "pending" });
  page.push({ type: "alert_update", alert_id: "a2", failed: true, why: "超时" });
  page.push({ type: "alert", alert_id: "a3", ts: T + 6, term: "治愈", tier: "exact", context: "这能治愈",
              context_zh: "这能治愈吗" });
  page.push({ type: "comment_update", id: "c1", state: "ok", translated: "多少钱" });
  page.push({ type: "comment", id: "c2", ts: T + 7, user: "王五", text: "好看", state: "pending" });
  return page;
}

// 界面提示里也列上和名字、正文同在一张卡片里的那些：分级、「」和时间、状态胶囊、重译按钮、取消按钮的
// aria-label。标记从叶子挪到 .meta、.alert-head 这些外层时，它们会跟着被豁免，这里就扫不到了
test("G10 scan.js 扫桌面：名字（translate=\"no\"）和正文（class）都豁免，界面提示照查", () => {
  assertScan(runScan(desktopFull()),
    ["中文品牌", "李四", "王五", "治愈", "大家好", "你们好", "这是奇迹", "这能治愈", "全部免费", "多少钱", "好看"],
    [["翻译中…", /div\.trans$/], ["翻译中…", /div\.alert-zh$/], ["翻译中…", /div\.cmt-zh$/],
     ["译文失败（超时）——请看上面的原话", /div\.alert-zh$/],
     ["直播中 ·", /span#status-text/], ["品牌 ·", /#active-brand-tag/], ["不限（默认）", /option$/],
     ["（当前）", /button\.recent-chip > span$/], ["早于 30 天的会话审计日志（3 个文件）", /span\.disk-name$/],
     ["点击开始翻译 @maria.ventas", /button\.recent-chip @title$/],
     ["疑似", /span\.alert-tier$/], ["」 " + hhmmss(T + 2), /div\.alert-head > span$/],
     ["重译", /button\.redo$/], ["翻译失败", /div\.meta > span\.lang-chip$/]]);
});

test("G10 scan.js 扫手机：名字和正文都豁免，界面提示照查", () => {
  assertScan(runScan(phoneFull()),
    ["李四", "王五", "治愈", "大家好", "你们好", "这能治愈", "多少钱", "好看"],
    [["翻译中…", /div\.cap-trans$/], ["译文失败（超时）——请看上面的原话", /div\.cap-trans$/],
     ["中文正在补…", /div\.alert-zh$/], ["中文译不出来（超时）——请看上面的原话", /div\.alert-zh$/],
     ["翻译中…", /div\.cmt-zh$/],
     ["精确 「", /span\.alert-head-label > span > span$/],
     ["取消这条报警（只在本机隐藏）", /button\.alert-dismiss @aria-label$/], ["重译", /button\.cap-retranslate$/]]);
});

// ---- 5. 两种标记只落在叶子上 -------------------------------------------------------------------
// scan.js 豁免的是标记节点的整棵子树。class 或 translate="no" 一旦从叶子挪到外层（.meta、.alert-head、
// .cmt-head、.cap-meta、整张卡片），同层的界面文字跟着被豁免，所有检查照样绿：上面的 kind() 只看叶子，
// 中文界面提示在第 4 节列了，时间这类纯 ASCII 的邻居扫描根本看不见。所以把形状钉死：
//   - 带正文 class 的只有这几种正文叶子，而且没有子元素；
//   - translate="no" 的元素没有子元素。静态的 select#target-lang、ul#share-ip-list 除外：里面只有语言名
//     和地址按钮（第 1 节钉着这两处是静态标的）。
// 新增一种正文节点时，在下面的清单里加上它，是有意为之的一步
const NAME_CONTAINERS = ["target-lang", "share-ip-list"];
function marksOnLeaves(page) {
  const els = [...new Set(page.all().flatMap((el) => [el, ...el.descendants()]))];
  const label = (el) => el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + el.cls.map((c) => "." + c).join("");
  const bodies = els.filter((el) => el.cls.includes(BODY_CLASS));
  const names = els.filter((el) => el.getAttribute("translate") === "no");
  const notLeaf = [...bodies, ...names.filter((el) => !NAME_CONTAINERS.includes(el.id))]
    .filter((el) => el.children.length)
    .map((el) => label(el) + " 有 " + el.children.length + " 个子元素");
  const bodyKinds = [...new Set(bodies.map((el) => (el.id ? "#" + el.id : "." + el.cls[0])))].sort();
  return { notLeaf, bodyKinds, names: names.length };
}

test("桌面：正文 class 只在正文叶子上，translate=\"no\" 只在没有子元素的名字上", () => {
  const { notLeaf, bodyKinds, names } = marksOnLeaves(desktopFull());
  assert.deepEqual(notLeaf, []);
  assert.deepEqual(bodyKinds, ["#live-original", "#live-translated", ".alert-ctx", ".alert-zh",
                               ".cmt-orig", ".cmt-zh", ".orig", ".trans"]);
  assert.ok(names >= 10, "这一页上标成名字的节点太少（" + names + "），fixture 没画出来");
});

test("手机：正文 class 只在正文叶子上，translate=\"no\" 只在没有子元素的名字上", () => {
  const { notLeaf, bodyKinds, names } = marksOnLeaves(phoneFull());
  assert.deepEqual(notLeaf, []);
  assert.deepEqual(bodyKinds, [".alert-ctx", ".alert-zh", ".cap-orig", ".cap-trans", ".cmt-orig", ".cmt-zh"]);
  assert.ok(names >= 5, "这一页上标成名字的节点太少（" + names + "），fixture 没画出来");
});
