// 手机同看页的界面语言闸（spec §7.1、§12.1 G6 的 gate 用例）。
//
// 手机拿不到 config（config/hello 在 viewer.py 的 DENY 里），自己判断不了发布闸；闸由服务端
// 写进页面：只有 <html data-i18n="on"> 时 viewer.js 才读这台手机上手动选过的语言、才显示
// 切换按钮。这里把 viewer.js 的浏览器分支真的跑起来（照 tests/viewer.test.mjs 的替身写法），
// 钉住三件事：
//   ① 没有 documentElement 的假 document（现有测试用的那种）：require 不抛，语言是 zh；
//   ② 有 lang="en" 但没有 data-i18n（闸关着）：仍是 zh，localStorage 里存的 en 连读都不读；
//   ③ 有 data-i18n="on"：localStorage 优先；localStorage 抛异常时退回 <html lang>。
// 还钉住 pick 的调用点：英文页用后端派生的 *_en，中文页取值和原来一样。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const MOD = require.resolve("../web/viewer.js");
const LANG_KEY = "tlt.viewer.lang";

// ---- 替身 ----------------------------------------------------------------------------------
// 比 viewer.test.mjs 的 makeFakeNode 多一样：属性真的存下来（getAttribute/setAttribute/
// hasAttribute），applyStatic 和 <html lang> 的改动才看得见
function makeNode(tag, attrs) {
  const listeners = {};
  const classes = new Set();
  const store = new Map(Object.entries(attrs || {}));
  let text = "";
  const node = {
    tagName: tag || "div",
    children: [],
    parentNode: null,
    dataset: {},
    className: "",
    disabled: false,
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    get textContent() { return text; },
    set textContent(v) { text = String(v); node.children = []; },
    set innerHTML(v) { if (v === "") node.children = []; },
    get firstChild() { return node.children[0] || null; },
    get lastChild() { return node.children[node.children.length - 1] || null; },
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      toggle: (c, force) => {
        if (force === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
        else if (force) classes.add(c); else classes.delete(c);
      },
      contains: (c) => classes.has(c),
    },
    getAttribute: (k) => (store.has(k) ? store.get(k) : null),
    setAttribute: (k, v) => { store.set(k, String(v)); },
    hasAttribute: (k) => store.has(k),
    removeAttribute: (k) => { store.delete(k); },
    appendChild(child) { child.parentNode = node; node.children.push(child); return child; },
    insertBefore(child, ref) {
      child.parentNode = node;
      const idx = ref ? node.children.indexOf(ref) : -1;
      if (idx === -1) node.children.push(child); else node.children.splice(idx, 0, child);
      return child;
    },
    removeChild(child) {
      const idx = node.children.indexOf(child);
      if (idx !== -1) node.children.splice(idx, 1);
      child.parentNode = null;
      return child;
    },
    addEventListener(type, cb) { (listeners[type] = listeners[type] || []).push(cb); },
    click() { (listeners.click || []).slice().forEach((cb) => cb()); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  return node;
}

// viewer.js 浏览器分支 getElementById 的全部 id，再加上这次的语言切换按钮
function pageElements() {
  const ids = ["conn-dot", "conn-text", "stream-text", "alert-badge", "alert-count", "stale-line",
    "alert-mode-line", "demo-banner", "incident-list", "health-line", "alert-section", "alert-list",
    "alert-clear-seen", "caption-section", "caption-list", "jump-latest", "comment-toggle",
    "comment-count", "comment-list", "ring-toggle", "lang-toggle-text"];
  const els = {};
  ids.forEach((id) => { els[id] = makeNode("div"); });
  // 和 web/viewer.html 一样：初始隐藏，自己带着中文 aria-label 与它的英文
  const toggle = makeNode("button", { "aria-label": "切换为英文界面", "data-en-aria-label": "Switch to Chinese" });
  toggle.classList.add("ring-toggle", "hidden");
  els["lang-toggle"] = toggle;
  return els;
}

// html: null 表示假 document 没有 documentElement（现有测试的替身就是这样）
function makeDocument(elements, html) {
  const doc = {
    hidden: false,
    getElementById: (id) => elements[id] || null,
    createElement: (tag) => makeNode(tag),
    createTextNode: (t) => ({ nodeType: 3, nodeValue: String(t) }),
    addEventListener() {},
    querySelectorAll() {
      return Object.values(elements).filter((el) =>
        ["data-en", "data-en-html", "data-en-title", "data-en-aria-label", "data-en-placeholder", "data-en-alt"]
          .some((a) => el.hasAttribute(a)));
    },
  };
  if (html) doc.documentElement = html;
  return doc;
}

function makeHtml(attrs) {
  const html = makeNode("html", attrs);
  Object.defineProperty(html, "lang", { get: () => html.getAttribute("lang") || "" });
  return html;
}

// storage: Map（能读能写）、"throw"（隐私模式：一碰就抛）。during(page) 在替身还挂在全局上时
// 跑：浏览器分支里的回调（收消息、点按钮）要用到 document / location / localStorage
function loadViewer({ html = null, storage = new Map() } = {}, during) {
  const elements = pageElements();
  const reads = [];
  const reloads = [];
  const sockets = [];
  function FakeWebSocket(url) { this.url = url; this.sent = []; sockets.push(this); }
  FakeWebSocket.prototype.send = function (d) { this.sent.push(d); };
  FakeWebSocket.prototype.close = function () {};
  const saved = {
    document: global.document, location: global.location, WebSocket: global.WebSocket,
    localStorage: global.localStorage, setTimeout: global.setTimeout, setInterval: global.setInterval,
  };
  global.document = makeDocument(elements, html);
  global.location = {
    hash: "#k=fake-token-not-a-real-secret-0000000000", protocol: "http:", host: "127.0.0.1:0",
    reload: () => { reloads.push(true); },
  };
  global.WebSocket = FakeWebSocket;
  global.localStorage = storage === "throw"
    ? { getItem: () => { throw new Error("SecurityError"); }, setItem: () => { throw new Error("SecurityError"); } }
    : {
        getItem: (k) => { reads.push(k); return storage.has(k) ? storage.get(k) : null; },
        setItem: (k, v) => { storage.set(k, String(v)); },
      };
  global.setTimeout = () => 1;
  global.setInterval = () => 0;
  const page = { elements, html, reads, reloads, storage, sockets };
  try {
    delete require.cache[MOD];
    page.V = require("../web/viewer.js");
    if (during) during(page);
  } finally {
    Object.assign(global, saved);
    delete require.cache[MOD];
  }
  return page;
}

function hello(ws) {
  ws.onopen();
  ws.onmessage({ data: JSON.stringify({ type: "viewer_hello", ok: true, ts: Date.now() / 1000, share_since: 0,
                                        viewers: 1, max_viewers: 12, read_only: true }) });
}

// ---- ① 没有 documentElement ---------------------------------------------------------------

test("① 没有 documentElement 的假 document：require 不抛，语言是 zh，不显示切换", () => {
  const page = loadViewer({ html: null, storage: new Map([[LANG_KEY, "en"]]) });
  assert.equal(page.V.UI_LANG, "zh");
  assert.equal(page.V.I18N_ON, false);
  assert.equal(page.reads.includes(LANG_KEY), false, "闸关着：本机选择连读都不读");
  assert.equal(page.elements["lang-toggle"].classList.contains("hidden"), true);
  assert.equal(page.V.L("中文", "English"), "中文");
});

test("① 不设 document 直接 require（纯函数测试的加载方式）：语言也是 zh", () => {
  delete require.cache[MOD];
  const V = require("../web/viewer.js");
  delete require.cache[MOD];
  assert.equal(V.UI_LANG, "zh");
  assert.equal(V.I18N_ON, false);
});

// ---- ② 闸关着：lang="en" 也不算 --------------------------------------------------------

test("② lang=en 但没有 data-i18n：仍是 zh，localStorage 的 en 不读，<html> 一点不改", () => {
  const html = makeHtml({ lang: "en" });
  const page = loadViewer({ html, storage: new Map([[LANG_KEY, "en"]]) });
  assert.equal(page.V.UI_LANG, "zh");
  assert.equal(page.V.I18N_ON, false);
  assert.equal(page.reads.includes(LANG_KEY), false, "闸关着：本机选择连读都不读");
  assert.equal(html.getAttribute("lang"), "en", "闸关着不改 <html lang>");
  assert.equal(html.classList.contains("i18n-done"), false, "闸关着不加 i18n-done");
  const toggle = page.elements["lang-toggle"];
  assert.equal(toggle.classList.contains("hidden"), true, "闸关着不显示切换");
  assert.equal(toggle.getAttribute("aria-label"), "切换为英文界面", "中文：静态文案一个不换");
  assert.equal(page.elements["lang-toggle-text"].textContent, "");
});

test("② 闸关着的中文页：data-i18n 不是 on（比如 off）也一样不读本机选择", () => {
  const html = makeHtml({ lang: "zh-CN", "data-i18n": "off" });
  const page = loadViewer({ html, storage: new Map([[LANG_KEY, "en"]]) });
  assert.equal(page.V.UI_LANG, "zh");
  assert.equal(page.reads.includes(LANG_KEY), false);
});

// ---- ③ 闸开着 -------------------------------------------------------------------------------

test("③ data-i18n=on：本机选过的 en 优先于服务端给的中文页，页面改成英文", () => {
  const html = makeHtml({ lang: "zh-CN", "data-i18n": "on" });
  const page = loadViewer({ html, storage: new Map([[LANG_KEY, "en"]]) }, (p) => {
    p.click = () => {                          // 点击要在替身还挂着时发生，这里先把动作存下来
      const saved = { location: global.location, localStorage: global.localStorage };
      global.location = { reload: () => { p.reloads.push(true); } };
      global.localStorage = { setItem: (k, v) => { p.storage.set(k, String(v)); } };
      try { p.elements["lang-toggle"].click(); } finally { Object.assign(global, saved); }
    };
  });
  assert.equal(page.V.UI_LANG, "en");
  assert.equal(page.V.I18N_ON, true);
  assert.equal(html.getAttribute("lang"), "en", "服务端给中文页、本机选了英文：<html lang> 要改");
  assert.equal(html.classList.contains("i18n-done"), true, "撤掉 viewer.css 的防闪");
  const toggle = page.elements["lang-toggle"];
  assert.equal(toggle.classList.contains("hidden"), false, "闸开着才显示切换");
  assert.equal(toggle.getAttribute("aria-label"), "Switch to Chinese", "applyStatic 换了按钮自己的属性");
  const label = page.elements["lang-toggle-text"];
  assert.equal(label.textContent, "中文", "按钮上写另一种语言的自称");
  assert.equal(label.getAttribute("lang"), "zh-CN");

  assert.equal(page.reloads.length, 0);
  page.click();
  assert.equal(page.storage.get(LANG_KEY), "zh", "点一下记下另一种语言");
  assert.equal(page.reloads.length, 1, "再重载一次");
});

test("③ data-i18n=on：本机选过的 zh 优先于服务端给的英文页", () => {
  const html = makeHtml({ lang: "en", "data-i18n": "on" });
  const page = loadViewer({ html, storage: new Map([[LANG_KEY, "zh"]]) });
  assert.equal(page.V.UI_LANG, "zh");
  assert.equal(html.getAttribute("lang"), "zh-CN");
  assert.equal(html.classList.contains("i18n-done"), true);
  assert.equal(page.elements["lang-toggle-text"].textContent, "EN");
  assert.equal(page.elements["lang-toggle-text"].getAttribute("lang"), "en");
  assert.equal(page.elements["lang-toggle"].getAttribute("aria-label"), "切换为英文界面");
});

test("③ data-i18n=on、没选过：跟服务端按 Accept-Language 给的 <html lang>", () => {
  const en = loadViewer({ html: makeHtml({ lang: "en", "data-i18n": "on" }) });
  assert.equal(en.V.UI_LANG, "en");
  const zh = loadViewer({ html: makeHtml({ lang: "zh-CN", "data-i18n": "on" }) });
  assert.equal(zh.V.UI_LANG, "zh");
  const junk = loadViewer({ html: makeHtml({ lang: "zh-CN", "data-i18n": "on" }),
                            storage: new Map([[LANG_KEY, "fr"]]) });
  assert.equal(junk.V.UI_LANG, "zh", "认不出的本机值当没选过");
});

test("③ data-i18n=on、localStorage 一碰就抛（隐私模式）：退回 <html lang>，不抛", () => {
  const page = loadViewer({ html: makeHtml({ lang: "en", "data-i18n": "on" }), storage: "throw" },
                          (p) => p.elements["lang-toggle"].click());
  assert.equal(page.V.UI_LANG, "en");
  assert.equal(page.elements["lang-toggle"].classList.contains("hidden"), false);
  assert.equal(page.reloads.length, 0, "记不下就不重载：重载了也还是原来的语言");
});

// ---- pick：后端派生的英文字段 ----------------------------------------------------------------

test("pick：中文页永远取基础字段，*_en 在也不用", () => {
  const V = loadViewer().V;
  assert.equal(V.pick({ text: "磁盘快满了", text_en: "Disk almost full" }, "text"), "磁盘快满了");
  assert.equal(V.pick({ why: "超时" }, "why"), "超时");
  assert.equal(V.pick(null, "why"), undefined);
});

test("pick：英文页有非空的 *_en 就用它，没有或是空串退回中文", () => {
  const V = loadViewer({ html: makeHtml({ lang: "en", "data-i18n": "on" }) }).V;
  assert.equal(V.UI_LANG, "en");
  assert.equal(V.pick({ text: "磁盘快满了", text_en: "Disk almost full" }, "text"), "Disk almost full");
  assert.equal(V.pick({ why: "超时" }, "why"), "超时", "没派生：漏翻只露中文");
  assert.equal(V.pick({ why: "超时", why_en: "" }, "why"), "超时", "空串不当译文");
  assert.equal(V.pick({ why: "", why_en: "" }, "why"), "");
  assert.equal(V.pick({ why: "超时", why_en: 5 }, "why"), "超时", "不是字符串不用");
});

function incidentText(page) {
  return page.elements["incident-list"].children.map((c) => c.textContent);
}

test("pick 调用点：持续提示和译文失败的原因，英文页用 *_en，中文页不变", () => {
  const msgs = [
    { type: "incident", id: "disk", level: "warn", text: "磁盘快满了", text_en: "Disk almost full" },
    { type: "caption", id: 7, ts: 1, original: "hola", failed: true, why: "超时", why_en: "timed out" },
  ];
  for (const [html, text, why] of [
    [makeHtml({ lang: "zh-CN" }), "磁盘快满了", "超时"],
    [makeHtml({ lang: "en", "data-i18n": "on" }), "Disk almost full", "timed out"],
  ]) {
    const page = loadViewer({ html }, (p) => {
      const ws = p.sockets[0];
      hello(ws);
      msgs.forEach((m) => ws.onmessage({ data: JSON.stringify(m) }));
    });
    assert.deepEqual(incidentText(page), [text]);
    const card = page.elements["caption-list"].children[0];
    const trans = card.children[2];
    assert.equal(trans.className, "cap-trans");
    assert.ok(trans.textContent.includes("（" + why + "）"), trans.textContent);
  }
});
