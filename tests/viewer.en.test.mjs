globalThis.UI_LANG = "en";   // 必须在 require 之前：web/viewer.js 加载时读一次（node --test 每个文件一个进程）
// 手机同看页的英文（web/viewer.js，spec §7、§12.1 G6）。中文断言在 tests/viewer.test.mjs，
// 语言闸（data-i18n、本机选择）在 tests/viewer.gate.test.mjs。
//
// 两段：纯函数直接 require；浏览器分支照 viewer.gate.test.mjs 的替身把页面真的跑起来，
// 喂一轮各类消息（数据全用 ASCII / 西语），再把整棵假 DOM 的文字和属性扫一遍：不许剩中文。
// 后端来的文字走派生的 *_en 字段（spec §7.2），这里只管页面自己写的那些。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const MOD = require.resolve("../web/viewer.js");
const V = require("../web/viewer.js");

const CJK = /[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/;
function english(text) {
  assert.equal(typeof text, "string");
  assert.ok(!CJK.test(text), "英文里还有中文：" + text);
  assert.ok(!/\{\w*\}/.test(text), "有没填的占位符：" + text);
  return text;
}

// ---- 纯函数 --------------------------------------------------------------------------------

test("语言确实是英文（不然下面全是空转）", () => {
  assert.equal(V.UI_LANG, "en");
});

test("连接状态胶囊：短写法，{n} 填上秒数", () => {
  assert.equal(V.statusText("connecting"), "Connecting…");
  assert.equal(V.statusText("connected"), "Connected");
  assert.equal(V.statusText("reconnecting", 4), "Reconnecting in 4 sec…");
  assert.equal(V.statusText("reconnecting"), "Reconnecting in 0 sec…");
  assert.equal(V.statusText("denied"), "Link expired · Ask for a new QR code");
  assert.equal(V.statusText("full"), "Full (12 max) · Try again later");
  assert.equal(V.statusText("off"), "Operator stopped sharing");
  assert.equal(V.statusText("stale", 50), "No updates for 50 sec");
  assert.equal(V.statusText("whatever"), "Status unknown");
});

test("直播状态：与桌面同一套词，认不出的写 Status unknown", () => {
  assert.equal(V.streamText("idle"), "Not started");
  assert.equal(V.streamText("connecting"), "Connecting");
  assert.equal(V.streamText("live"), "Live");
  assert.equal(V.streamText("error"), "Stopped");
  assert.equal(V.streamText(undefined), "Status unknown");
});

test("报警开关提示：关着写一句，开着是空串（调用方据此隐藏）", () => {
  assert.equal(V.alertModeText(false), "Banned-term alerts are off for this session.");
  assert.equal(V.alertModeText(true), "");
});

test("重译按钮：各状态的字，可点与否与中文相同", () => {
  assert.deepEqual(V.retranslateLabel(null, false), { hidden: false, text: "Retranslate", disabled: false });
  assert.deepEqual(V.retranslateLabel(null, true), { hidden: true, text: "Retranslated", disabled: true });
  assert.deepEqual(V.retranslateLabel("pending", false), { hidden: false, text: "Retranslating…", disabled: true });
  assert.deepEqual(V.retranslateLabel("ok", true), { hidden: false, text: "Retranslated", disabled: true });
  assert.deepEqual(V.retranslateLabel("failed", false),
    { hidden: false, text: "Couldn’t Retranslate · Try Again", disabled: false });
  assert.deepEqual(V.retranslateLabel("weird", true), { hidden: false, text: "Retranslate", disabled: true });
});

test("场次分隔线：不带主播名、不带时间", () => {
  assert.equal(V.sessionDividerText({ streamer: "bella", ts: 1 }), "── New session ──");
});

// ---- 浏览器分支 ----------------------------------------------------------------------------

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
    attrs: store,
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

const IDS = ["conn-dot", "conn-text", "stream-text", "alert-badge", "alert-count", "stale-line",
  "alert-mode-line", "demo-banner", "incident-list", "health-line", "alert-section", "alert-list",
  "alert-clear-seen", "caption-section", "caption-list", "jump-latest", "comment-toggle",
  "comment-count", "comment-list", "ring-toggle", "lang-toggle", "lang-toggle-text"];

// 在替身挂在全局上时跑整页：drive(page) 里收消息、关连接。now 是可调的假时钟（毫秒）
function runPage(drive) {
  const elements = {};
  IDS.forEach((id) => { elements[id] = makeNode("div"); });
  // 场次分隔线要先数一数已有的字幕卡片（renderSessionBreak）
  const captions = elements["caption-list"];
  captions.querySelectorAll = (sel) => (sel === ".cap-item"
    ? captions.children.filter((c) => c.className === "cap-item") : []);
  const sockets = [];
  function FakeWebSocket(url) { this.url = url; this.sent = []; sockets.push(this); }
  FakeWebSocket.prototype.send = function (d) { this.sent.push(d); };
  FakeWebSocket.prototype.close = function () {};
  const clock = { now: Date.UTC(2026, 8, 29, 12, 0, 0) };
  const saved = {
    document: global.document, location: global.location, WebSocket: global.WebSocket,
    localStorage: global.localStorage, setTimeout: global.setTimeout, setInterval: global.setInterval,
    now: Date.now,
  };
  global.document = {
    hidden: false,
    getElementById: (id) => elements[id] || null,
    createElement: (tag) => makeNode(tag),
    addEventListener() {},
    querySelectorAll() { return []; },
  };
  global.location = { hash: "#k=fake-token-not-a-real-secret-0000000000", protocol: "http:",
                      host: "127.0.0.1:0", reload() {} };
  global.WebSocket = FakeWebSocket;
  global.localStorage = { getItem: () => null, setItem() {} };
  global.setTimeout = () => 1;
  global.setInterval = () => 0;
  Date.now = () => clock.now;
  try {
    delete require.cache[MOD];
    require("../web/viewer.js");
    const ws = sockets[0];
    const send = (msg) => ws.onmessage({ data: JSON.stringify(msg) });
    ws.onopen();
    send({ type: "viewer_hello", ok: true, ts: clock.now / 1000, share_since: 0, viewers: 1,
           max_viewers: 12, read_only: true });
    drive({ ws, send, clock, elements });
  } finally {
    Object.assign(global, saved);
    Date.now = saved.now;
    delete require.cache[MOD];
  }
  return elements;
}

// 整棵假 DOM 上所有文字与属性，带位置
function everything(elements) {
  const out = [];
  const walk = (node, where) => {
    if (node.textContent) out.push([where, node.textContent]);
    for (const [k, v] of node.attrs) out.push([where + " @" + k, v]);
    if (node.dataset.sourceText) out.push([where + " data-source-text", node.dataset.sourceText]);
    node.children.forEach((c, i) => walk(c, where + " > " + (c.className || c.tagName) + "[" + i + "]"));
  };
  Object.entries(elements).forEach(([id, el]) => walk(el, "#" + id));
  return out;
}

function assertNoChinese(elements) {
  const left = everything(elements).filter(([, text]) => CJK.test(text));
  assert.deepEqual(left, [], "英文页上还有中文");
}

const TS = Date.UTC(2026, 8, 29, 12, 0, 0) / 1000;

test("一整场：健康提示、报警四档（含认不出的分级）、译文失败、翻译中、弹幕与弹幕状态，全是英文", () => {
  const els = runPage(({ send }) => {
    send({ type: "status", state: "live" });
    send({ type: "alert_mode", on: false });
    send({ type: "health", level: "lagging" });
    send({ type: "incident", id: "disk", level: "warn", text: "磁盘快满了", text_en: "Disk almost full" });
    ["exact", "variant", "fuzzy", "brand-new-tier"].forEach((tier, i) => {
      send({ type: "alert", alert_id: "a" + i, tier, term: "descuento", ts: TS, context: "hoy hay descuento" });
    });
    send({ type: "alert_update", alert_id: "a1", failed: true, why: "超时", why_en: "timed out" });
    send({ type: "alert_update", alert_id: "a2", failed: true });
    send({ type: "caption", id: 1, ts: TS, original: "hola", failed: true, why: "超时", why_en: "timed out" });
    send({ type: "caption", id: 2, ts: TS, original: "adios", failed: true });
    send({ type: "caption", id: 3, ts: TS, original: "gracias", translate_state: "pending" });
    send({ type: "caption", id: 4, ts: TS, original: "ya", translated: "ok", strong: false,
           strong_state: "failed" });
    send({ type: "session_break", ts: TS });
    send({ type: "comment", id: 9, ts: TS, user: "maria", text: "precio?", state: "pending" });
    send({ type: "comment_source", backend: "connecting" });
  });

  assert.equal(els["stream-text"].textContent, "Live");
  assert.equal(els["alert-mode-line"].textContent, "Banned-term alerts are off for this session.");
  assert.equal(els["health-line"].children[1].textContent, "Recognition is falling behind. Alerts will be delayed.");
  assert.equal(els["incident-list"].children[0].textContent, "Disk almost full");

  // 报警卡片：新的在上。分级 + 弯引号，词条单独一截（名字，translate="no"），再是时间
  const cards = els["alert-list"].children;
  const heads = cards.map((c) => c.children[0].children[0].children[1].children.map((s) => s.textContent));
  const time = heads[0][2].slice(2);
  assert.match(time, /^\d\d:\d\d:\d\d$/);
  assert.deepEqual(heads.map((h) => h[0]), ["Exact “", "Similar “", "Variant “", "Exact “"],
    "认不出的分级兜底与桌面一样写 Exact");
  heads.forEach((h) => { assert.equal(h[1], "descuento"); assert.equal(h[2], "” " + time); });
  assert.equal(cards[0].children[0].children[0].children[1].children[1].getAttribute("translate"), "no");
  assert.equal(cards[0].children[0].children[1].getAttribute("aria-label"), "Dismiss this alert (this phone only)");
  const ctxZh = (card) => card.children[2].textContent;
  assert.equal(ctxZh(cards[0]), "Translating…", "报警译文还没到");
  assert.equal(ctxZh(cards[2]), "Couldn’t translate (timed out). See the original above.");
  assert.equal(ctxZh(cards[1]), "Couldn’t translate. See the original above.",
    "没有原因时的兜底也要英文，且与桌面 app.js 一样整句不带括号");

  const caps = els["caption-list"].children;
  assert.equal(caps[0].children[2].textContent, "Couldn’t translate (timed out). See the original above.");
  assert.equal(caps[1].children[2].textContent, "Couldn’t translate. See the original above.");
  assert.equal(caps[2].children[2].textContent, "Translating…");
  assert.equal(caps[0].children[3].textContent, "Retranslate");
  assert.equal(caps[3].children[3].textContent, "Couldn’t Retranslate · Try Again");
  assert.equal(caps[4].textContent, "── New session ──");

  assert.equal(els["comment-list"].children[0].children[1].textContent, "Translating…");
  assert.equal(els["comment-toggle"].dataset.sourceText, "Connecting…");
  assertNoChinese(els);
});

test("识别明显落后、弹幕各状态（认不出的按暂时不可用）", () => {
  const seen = [];
  const els = runPage(({ send, elements }) => {
    send({ type: "health", level: "degraded" });
    for (const backend of ["idle", "connecting", "live", "unavailable", "who-knows"]) {
      send({ type: "comment_source", backend });
      seen.push(elements["comment-toggle"].dataset.sourceText);
    }
  });
  assert.equal(els["health-line"].children[1].textContent, "Detection degraded: recognition is far behind.");
  assert.deepEqual(seen, ["Not connected", "Connecting…", "Connected", "Temporarily unavailable",
                          "Temporarily unavailable"]);
  seen.forEach(english);
  assertNoChinese(els);
});

test("断线：胶囊按关闭码写短句，直播状态盖成 Status unknown", () => {
  for (const [code, text] of [[4401, "Link expired · Ask for a new QR code"],
                              [4429, "Full (12 max) · Try again later"],
                              [4403, "Operator stopped sharing"]]) {
    const els = runPage(({ ws, send }) => {
      send({ type: "status", state: "live" });
      ws.onclose({ code });
    });
    assert.equal(els["conn-text"].textContent, text, String(code));
    assert.equal(els["stream-text"].textContent, "Status unknown");
    assertNoChinese(els);
  }
});

test("重连拖过 60 秒：胶囊改成告诉人可以做什么", () => {
  const els = runPage(({ ws, clock }) => {
    clock.now += 75000;
    ws.onclose({ code: 1006 });
  });
  assert.equal(els["conn-text"].textContent,
    "Not connected for 75 sec. Ask the operator whether sharing is still on, or scan the QR code again.");
  assertNoChinese(els);
});
