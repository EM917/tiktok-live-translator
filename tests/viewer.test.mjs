// 手机同看页面纯逻辑的契约测试（web/viewer.js）。
//
// viewer.js 同时是浏览器页面脚本和 node:test 的被测模块：文件里用
// `typeof document !== "undefined"` 把 DOM/WebSocket 那段挡住，Node 环境下
// 整段跳过，这里只测挡住之外的纯函数。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const V = require("../web/viewer.js");

// ---- statusText ----
test("statusText 覆盖 7 个状态", () => {
  assert.equal(V.statusText("connecting"), "连接中…");
  assert.equal(V.statusText("connected"), "已连接");
  assert.equal(V.statusText("reconnecting", 3), "已断开，3 秒后重连…");
  assert.equal(V.statusText("denied"), "链接已失效，请向中控要新的二维码");
  assert.equal(V.statusText("full"), "同看人数已满（12 人），稍后再试");
  assert.equal(V.statusText("off"), "中控已关闭手机同看");
  assert.equal(V.statusText("stale", 50), "50 秒没有新消息");
});
test("statusText 未知值有兜底文案", () => {
  assert.equal(V.statusText("weird-state"), "状态未知");
  assert.equal(V.statusText(undefined), "状态未知");
});

// ---- streamText ----
test("streamText 覆盖 status.state 全集", () => {
  assert.equal(V.streamText("idle"), "未开始");
  assert.equal(V.streamText("connecting"), "连接中");
  assert.equal(V.streamText("live"), "直播中");
  assert.equal(V.streamText("error"), "已停止");
});
test("streamText 未知值有兜底文案", () => {
  assert.equal(V.streamText("ended"), "状态未知");
  assert.equal(V.streamText(null), "状态未知");
});

// ---- alertModeText ----
test("alertModeText: 关闭时给出常驻提示文案，打开时空串（调用方据此隐藏）", () => {
  assert.equal(V.alertModeText(false), "违禁词警示已关闭（中控开播时未开启）");
  assert.equal(V.alertModeText(true), "");
});

// ---- backoffDelay ----
test("backoffDelay(n) n=0..10 单调不减、界内、抖动在注入的固定值附近", () => {
  const fixedRand = () => 0.5;   // (0.5*2-1)*0.2 = 0，无抖动，纯测单调性与边界
  let prev = -Infinity;
  for (let n = 0; n <= 10; n++) {
    const v = V.backoffDelay(n, fixedRand);
    assert.ok(v >= 400, `n=${n} v=${v} 应 >= 400`);
    assert.ok(v <= 15000, `n=${n} v=${v} 应 <= 15000`);
    assert.ok(v >= prev, `n=${n} v=${v} 应比上一个不小 (${prev})`);
    prev = v;
  }
});
test("backoffDelay 抖动确实生效且落在 ±20% 内（未被夹到边界的区间）", () => {
  const base = [500, 1000, 2000, 4000];
  [0, 1, 2, 3].forEach((n) => {
    const up = V.backoffDelay(n, () => 1);      // 满抖动 +20%
    const down = V.backoffDelay(n, () => 0);    // 满抖动 -20%
    assert.ok(up <= Math.round(base[n] * 1.2) + 1);
    assert.ok(down >= Math.round(base[n] * 0.8) - 1);
    assert.ok(up >= down);
  });
});
test("backoffDelay 边界：n=0 满负抖动正好落在 400 这个下限上", () => {
  assert.equal(V.backoffDelay(0, () => 0), 400);
});
test("backoffDelay 负数/未传 n 按 0 处理，不抛", () => {
  assert.equal(typeof V.backoffDelay(-5, () => 0.5), "number");
  assert.equal(typeof V.backoffDelay(undefined, () => 0.5), "number");
});

// ---- alertKey ----
test("alertKey 让回放的 alert 与随后实时同 id 的 alert 只渲染一份", () => {
  const replayed = { type: "alert", alert_id: "a1", replay: true, term: "x" };
  const live = { type: "alert", alert_id: "a1", term: "x" };
  assert.equal(V.alertKey(replayed), V.alertKey(live));
});
test("alertKey 不同 id 得到不同 key", () => {
  assert.notEqual(V.alertKey({ alert_id: "a1" }), V.alertKey({ alert_id: "a2" }));
});

// ---- applyUpdate ----
test("caption_update 只覆盖带来的键：不带 translated 时不清空已有译文", () => {
  const existing = { id: "c1", original: "hola", translated: "你好", translate_state: "done" };
  const merged = V.applyUpdate(existing, { id: "c1", translate_state: "done", failed: false });
  assert.equal(merged.translated, "你好");        // 没被清空
  assert.equal(merged.original, "hola");          // 没带的键原样保留
  assert.equal(merged.translate_state, "done");
});
test("caption_update 带 translated 时正常覆盖", () => {
  const existing = { id: "c1", translated: "旧译文" };
  const merged = V.applyUpdate(existing, { id: "c1", translated: "新译文" });
  assert.equal(merged.translated, "新译文");
});
test("alert_update 写入 context_zh / failed / why", () => {
  const existing = { alert_id: "a1", term: "x", context_zh: null };
  const merged = V.applyUpdate(existing, { alert_id: "a1", context_zh: "翻译好了", failed: false, why: null });
  assert.equal(merged.context_zh, "翻译好了");
  assert.equal(merged.failed, false);
  assert.equal(merged.term, "x");   // 未提及的键不受影响
});

// ---- tokenFromHash ----
test("tokenFromHash 只认 #k= 片段", () => {
  assert.equal(V.tokenFromHash("#k=abc"), "abc");
  assert.equal(V.tokenFromHash(""), null);
  assert.equal(V.tokenFromHash("#"), null);
  assert.equal(V.tokenFromHash("#k="), null);
  assert.equal(V.tokenFromHash("?k=abc"), null);
  assert.equal(V.tokenFromHash("#kk=abc"), null);
});

// ---- shouldRing ----
test("shouldRing 只在新 alert 且开关开启时为 true", () => {
  assert.equal(V.shouldRing({ type: "alert" }, true), true);
  assert.equal(V.shouldRing({ type: "alert" }, false), false);
  assert.equal(V.shouldRing({ type: "caption" }, true), false);
});
test("shouldRing 回放的 alert 永不响铃", () => {
  assert.equal(V.shouldRing({ type: "alert", replay: true }, true), false);
});
test("shouldRing 对空/非法输入不抛", () => {
  assert.equal(V.shouldRing(null, true), false);
  assert.equal(V.shouldRing(undefined, true), false);
});

// ---- retranslateLabel ----
test("retranslateLabel 没发起过：已是大模型译文就隐藏，否则给出可点的初始按钮", () => {
  assert.deepEqual(V.retranslateLabel(null, true), { hidden: true, text: "已重译", disabled: true });
  assert.deepEqual(V.retranslateLabel(undefined, false), { hidden: false, text: "重译", disabled: false });
});
test("retranslateLabel 三种发起过的状态都可见，各自给出对应文案", () => {
  assert.deepEqual(V.retranslateLabel("pending", false), { hidden: false, text: "重译中…", disabled: true });
  assert.deepEqual(V.retranslateLabel("ok", true), { hidden: false, text: "已重译", disabled: true });
  assert.deepEqual(V.retranslateLabel("failed", false), { hidden: false, text: "重译失败，可再试", disabled: false });
});
test("retranslateLabel failed 时即使 strong 意外为 true 也允许再点（未知组合，不锁死用户）", () => {
  assert.equal(V.retranslateLabel("failed", true).disabled, false);
});
test("retranslateLabel 未知取值兜底成可点，不抛", () => {
  assert.deepEqual(V.retranslateLabel("weird", false), { hidden: false, text: "重译", disabled: false });
});

// ---- canRetranslate ----
test("canRetranslate 断线时一律不能点，即便从没点过", () => {
  assert.equal(V.canRetranslate(null, 1000, false), false);
});
test("canRetranslate 已连接且从没点过：可以点", () => {
  assert.equal(V.canRetranslate(null, 1000, true), true);
});
test("canRetranslate 3 秒冷却：差一点不到不能点，刚好到点能点", () => {
  assert.equal(V.canRetranslate(1000, 3999, true), false);
  assert.equal(V.canRetranslate(1000, 4000, true), true);
});

// ---- shouldRenderSessionDivider / sessionDividerText ----
test("shouldRenderSessionDivider：首场（还没有字幕卡片）不画线，其余画", () => {
  assert.equal(V.shouldRenderSessionDivider(0), false);
  assert.equal(V.shouldRenderSessionDivider(undefined), false);
  assert.equal(V.shouldRenderSessionDivider(1), true);
});
test("sessionDividerText：固定文案，不带时间、不带主播名（哪怕传了 streamer）", () => {
  assert.equal(V.sessionDividerText(), "── 新的一场 ──");
  assert.equal(V.sessionDividerText({ ts: 123, streamer: "bella" }), "── 新的一场 ──");
});

// ---- 取消警报本地存储：loadDismissedAlerts / saveDismissedAlerts / createDismissedStore ----
function fakeStorage(initial) {
  const data = new Map(initial ? Object.entries(initial) : []);
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
    _raw: data,
  };
}

test("loadDismissedAlerts 坏 JSON / 非数组 / 没有 storage 都当空，不抛", () => {
  assert.deepEqual(V.loadDismissedAlerts(undefined), []);
  assert.deepEqual(V.loadDismissedAlerts(fakeStorage({ viewerDismissedAlerts: "not json" })), []);
  assert.deepEqual(V.loadDismissedAlerts(fakeStorage({ viewerDismissedAlerts: JSON.stringify({ a: 1 }) })), []);
  assert.deepEqual(V.loadDismissedAlerts(fakeStorage({ viewerDismissedAlerts: JSON.stringify(["a1", 2]) })), ["a1", "2"]);
});
test("saveDismissedAlerts 没有 storage 或抛错都不影响调用方，静默放弃", () => {
  assert.doesNotThrow(() => V.saveDismissedAlerts(null, ["a1"]));
  assert.doesNotThrow(() => V.saveDismissedAlerts({ setItem: () => { throw new Error("quota"); } }, ["a1"]));
});

test("createDismissedStore add/has 生效，并落盘到注入的 storage", () => {
  const storage = fakeStorage();
  const store = V.createDismissedStore(storage);
  assert.equal(store.has("a1"), false);
  store.add("a1");
  assert.equal(store.has("a1"), true);
  assert.deepEqual(JSON.parse(storage.getItem("viewerDismissedAlerts")), ["a1"]);
});
test("createDismissedStore 数字 id 和字符串 id 视为同一个键", () => {
  const store = V.createDismissedStore(fakeStorage());
  store.add(42);
  assert.equal(store.has(42), true);
  assert.equal(store.has("42"), true);
});
test("createDismissedStore addMany 一次性写入多个、只落盘一次", () => {
  const storage = fakeStorage();
  let writes = 0;
  const wrapped = { getItem: storage.getItem, setItem: (k, v) => { writes++; storage.setItem(k, v); } };
  const store = V.createDismissedStore(wrapped);
  store.addMany(["a1", "a2", "a3"]);
  assert.equal(writes, 1);
  assert.equal(store.has("a2"), true);
});
test("createDismissedStore 重复 add 同一个 id 不重复落盘", () => {
  const storage = fakeStorage();
  let writes = 0;
  const wrapped = { getItem: storage.getItem, setItem: (k, v) => { writes++; storage.setItem(k, v); } };
  const store = V.createDismissedStore(wrapped);
  store.add("a1");
  store.add("a1");
  assert.equal(writes, 1);
});
test("createDismissedStore 超过上限只留最新的（注入小上限，不用真凑 500 条）", () => {
  const storage = fakeStorage();
  const store = V.createDismissedStore(storage, 3);
  store.add("a1"); store.add("a2"); store.add("a3"); store.add("a4");
  assert.equal(store.has("a1"), false);   // 最老的被挤掉
  assert.equal(store.has("a4"), true);
  assert.equal(JSON.parse(storage.getItem("viewerDismissedAlerts")).length, 3);
});
test("createDismissedStore 从已有存储恢复，重连后仍认得之前取消过的 id", () => {
  const storage = fakeStorage({ viewerDismissedAlerts: JSON.stringify(["a1", "a2"]) });
  const store = V.createDismissedStore(storage);
  assert.equal(store.has("a1"), true);
  assert.equal(store.has("a2"), true);
  assert.equal(store.has("a3"), false);
});
test("createDismissedStore storage 抛错/没有 storage 也不影响内存态的功能", () => {
  const throwing = V.createDismissedStore({
    getItem: () => { throw new Error("boom"); },
    setItem: () => { throw new Error("boom"); },
  });
  assert.doesNotThrow(() => throwing.add("a1"));
  assert.equal(throwing.has("a1"), true);

  const noStorage = V.createDismissedStore(undefined);
  assert.doesNotThrow(() => noStorage.add("a1"));
  assert.equal(noStorage.has("a1"), true);
});

// ---- DOM 回归：断线时不能让 stream-text 停在最后一次「直播中」不动 ----
// 评审发现：setConnState/onSocketDown（上面那道 `typeof document` 门之内）只
// 更新了 conn-text/conn-dot，从没碰过 stream-text——它只由 "status" 消息写，
// 断线期间没人写它，于是 A9 整段重连升级期间头条一直停在最后一次收到的
// 「直播中」（spec §12：「绝不在断线时继续显示『直播中』」，即 app/server.py
// :221-225 那条教训的手机版）。上面的纯函数测试挡不住这个——它们从不碰 DOM。
// 这里手搭一个最小 document/WebSocket/location 替身，绕开 `typeof document`
// 那道门，把浏览器分支真正跑起来，直接复现并钉住修复。
test("断线时 stream-text 被盖成「状态未知」，重连成功后自动刷新回真实值", () => {
  function fakeEl() {
    const classes = new Set();
    return {
      classList: {
        add: function () { for (var i = 0; i < arguments.length; i++) classes.add(arguments[i]); },
        remove: function () { for (var i = 0; i < arguments.length; i++) classes.delete(arguments[i]); },
        toggle: function (c, force) {
          if (force === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
          else if (force) classes.add(c); else classes.delete(c);
        },
        contains: function (c) { return classes.has(c); },
      },
      textContent: "",
      innerHTML: "",
      addEventListener: function () {},
    };
  }

  var elements = {
    "conn-dot": fakeEl(),
    "conn-text": fakeEl(),
    "stream-text": fakeEl(),
  };

  function FakeWebSocket(url) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  FakeWebSocket.prototype.send = function () {};
  FakeWebSocket.prototype.close = function () {};
  FakeWebSocket.instances = [];

  var timeoutQueue = [];

  var originals = {
    document: global.document,
    location: global.location,
    WebSocket: global.WebSocket,
    localStorage: global.localStorage,
    setTimeout: global.setTimeout,
    setInterval: global.setInterval,
  };

  var modPath = require.resolve("../web/viewer.js");

  global.document = {
    getElementById: function (id) { return elements[id] || null; },
    createElement: function () { return fakeEl(); },
  };
  // 43 字符的假 token，字符集只用 tokenFromHash 认得的 [^&]+ 就够，不用真凑 hmac
  global.location = { hash: "#k=fake-token-not-a-real-secret-0000000000", protocol: "http:", host: "127.0.0.1:0" };
  global.WebSocket = FakeWebSocket;
  var store = new Map();
  global.localStorage = {
    getItem: function (k) { return store.has(k) ? store.get(k) : null; },
    setItem: function (k, v) { store.set(k, String(v)); },
  };
  global.setTimeout = function (fn) { timeoutQueue.push(fn); return timeoutQueue.length; };
  global.setInterval = function () { return 0; };

  try {
    delete require.cache[modPath];
    require("../web/viewer.js");   // 重新求值：这次 document 有定义，浏览器分支真正跑起来

    var streamTextEl = elements["stream-text"];
    assert.equal(FakeWebSocket.instances.length, 1, "加载时 connect() 应该已经建了第一条 WebSocket");
    var first = FakeWebSocket.instances[0];

    first.onopen();
    first.onmessage({ data: JSON.stringify({ type: "viewer_hello", ok: true, ts: Date.now() / 1000, share_since: 0, viewers: 1, max_viewers: 12, read_only: true }) });
    first.onmessage({ data: JSON.stringify({ type: "status", state: "live", ts: Date.now() / 1000 }) });
    assert.equal(streamTextEl.textContent, "直播中", "前置条件：已连接且收到 live 状态时应显示直播中");

    // WebSocket 掉线（Wi-Fi 抖动 / 服务端 abort），不是拒绝也不是满员——走正常重连退避
    first.onclose({ code: 1006 });

    assert.equal(streamTextEl.textContent, "状态未知",
      "断线期间绝不能继续显示上一次的「直播中」");
    assert.equal(streamTextEl.classList.contains("stale"), true, "断线期间应叠加视觉调暗标记");

    // 模拟重连定时器触发：应该重新连上并让真实状态自动刷回来
    assert.equal(timeoutQueue.length, 1, "掉线后应该已经排了一次重连");
    var fireReconnect = timeoutQueue.pop();
    fireReconnect();

    assert.equal(FakeWebSocket.instances.length, 2, "重连应该创建第二条 WebSocket");
    var second = FakeWebSocket.instances[1];
    second.onopen();
    second.onmessage({ data: JSON.stringify({ type: "viewer_hello", ok: true, ts: Date.now() / 1000, share_since: 0, viewers: 1, max_viewers: 12, read_only: true }) });
    second.onmessage({ data: JSON.stringify({ type: "status", state: "live", ts: Date.now() / 1000 }) });

    assert.equal(streamTextEl.textContent, "直播中", "重连成功后应该自动刷新回真实状态");
    assert.equal(streamTextEl.classList.contains("stale"), false, "重连成功后应该去掉调暗标记");
  } finally {
    global.document = originals.document;
    global.location = originals.location;
    global.WebSocket = originals.WebSocket;
    global.localStorage = originals.localStorage;
    global.setTimeout = originals.setTimeout;
    global.setInterval = originals.setInterval;
    delete require.cache[modPath];
  }
});

// ---- DOM 回归：手机端操作（重译按钮 / 取消警报）----
// 上面那道 `typeof document` 门之内的渲染/发送逻辑，纯函数测试摸不到——这里用
// 一套比上面 fakeEl 更完整的假节点（真的维护 children，appendChild/insertBefore/
// removeChild 都生效），把 renderCaption/renderAlert 真正建出来的按钮找到、点掉，
// 验证端到端行为，而不是只验证抽出来的纯函数。
function makeFakeNode(tag) {
  const listeners = {};
  const classes = new Set();
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
    set textContent(v) { text = v; node.children = []; },
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
    dispatch(type) { (listeners[type] || []).slice().forEach((cb) => cb()); },
    setAttribute() {},
    querySelector() { return null; },
    // 只支持 renderSessionBreak 用到的这种单类名选择器（".cap-item"），
    // 匹配 className 字符串（renderCaption 用 card.className = "cap-item"
    // 这种写法）和 classList（其余状态位用 classList.add 这种写法），
    // 递归子树——真实 DOM 的 querySelectorAll 也是找整棵子树
    querySelectorAll(sel) {
      var cls = String(sel || "").replace(/^\./, "");
      var out = [];
      (function walk(n) {
        n.children.forEach(function (c) {
          var hit = (c.className || "").split(/\s+/).indexOf(cls) !== -1
                    || c.classList.contains(cls);
          if (hit) out.push(c);
          walk(c);
        });
      })(node);
      return out;
    },
  };
  return node;
}
function makeFakeDocument(elements) {
  const listeners = {};
  return {
    hidden: false,
    getElementById: (id) => elements[id] || null,
    createElement: (tag) => makeFakeNode(tag),
    addEventListener(type, cb) { (listeners[type] = listeners[type] || []).push(cb); },
    dispatch(type) { (listeners[type] || []).slice().forEach((cb) => cb()); },
  };
}
// viewer.js 在浏览器分支里 getElementById 的全部 id——两个新测试都要建全，
// 少一个就会在对应功能上悄悄变成 no-op（代码里到处是 `if (!el) return;`）。
function baseElements() {
  return {
    "conn-dot": makeFakeNode(), "conn-text": makeFakeNode(), "stream-text": makeFakeNode(),
    "alert-badge": makeFakeNode(), "alert-count": makeFakeNode(), "stale-line": makeFakeNode(),
    "alert-mode-line": makeFakeNode(),
    "demo-banner": makeFakeNode(), "incident-list": makeFakeNode(), "health-line": makeFakeNode(),
    "alert-section": makeFakeNode(), "alert-list": makeFakeNode(), "alert-clear-seen": makeFakeNode(),
    "caption-section": makeFakeNode(), "caption-list": makeFakeNode(), "jump-latest": makeFakeNode(),
    "comment-toggle": makeFakeNode(), "comment-count": makeFakeNode(), "comment-list": makeFakeNode(),
    "ring-toggle": makeFakeNode(),
  };
}
function makeFakeWebSocket() {
  function FakeWebSocket(url) { this.url = url; this.sent = []; FakeWebSocket.instances.push(this); }
  FakeWebSocket.prototype.send = function (data) { this.sent.push(data); };
  FakeWebSocket.prototype.close = function () {};
  FakeWebSocket.instances = [];
  return FakeWebSocket;
}
function withFakeViewerPage(run) {
  const elements = baseElements();
  const FakeWebSocket = makeFakeWebSocket();
  const originals = {
    document: global.document, location: global.location, WebSocket: global.WebSocket,
    localStorage: global.localStorage, setTimeout: global.setTimeout, setInterval: global.setInterval,
  };
  const modPath = require.resolve("../web/viewer.js");
  const store = new Map();

  global.document = makeFakeDocument(elements);
  global.location = { hash: "#k=fake-token-not-a-real-secret-0000000000", protocol: "http:", host: "127.0.0.1:0" };
  global.WebSocket = FakeWebSocket;
  global.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
  };
  global.setTimeout = (fn) => { (global.setTimeout._queue = global.setTimeout._queue || []).push(fn); return 1; };
  global.setInterval = () => 0;

  try {
    delete require.cache[modPath];
    require("../web/viewer.js");
    const ws1 = FakeWebSocket.instances[0];
    ws1.onopen();
    ws1.onmessage({ data: JSON.stringify({ type: "viewer_hello", ok: true, ts: Date.now() / 1000, share_since: 0, viewers: 1, max_viewers: 12, read_only: true }) });
    ws1.sent = [];   // 握手时发的 auth 帧不是被测行为，清空后让调用方从干净基线数起
    run({ elements, ws: ws1, store, FakeWebSocket });
  } finally {
    global.document = originals.document;
    global.location = originals.location;
    global.WebSocket = originals.WebSocket;
    global.localStorage = originals.localStorage;
    global.setTimeout = originals.setTimeout;
    global.setInterval = originals.setInterval;
    delete require.cache[modPath];
  }
}

test("手机端「重译」按钮：点击经 /vws 发 retranslate、本地 3 秒冷却、断线后自动禁用", () => {
  withFakeViewerPage(({ elements, ws }) => {
    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 1, ts: Date.now() / 1000, original: "hola" }) });
    const captionList = elements["caption-list"];
    assert.equal(captionList.children.length, 1);
    const card = captionList.children[0];
    const btn = card.children[card.children.length - 1];

    assert.equal(btn.textContent, "重译", "没发起过重译、也不是大模型译文：可点");
    assert.equal(btn.classList.contains("hidden"), false);
    assert.equal(btn.disabled, false);

    btn.click();
    assert.equal(ws.sent.length, 1);
    assert.deepEqual(JSON.parse(ws.sent[0]), { type: "retranslate", id: 1 });

    btn.click();   // 3 秒冷却内再点一次：应该被拦住，不重复发
    assert.equal(ws.sent.length, 1, "3 秒冷却内不能重复发送");

    ws.onmessage({ data: JSON.stringify({ type: "caption_update", id: 1, strong_state: "pending" }) });
    assert.equal(btn.textContent, "重译中…");
    assert.equal(btn.disabled, true);

    ws.onmessage({ data: JSON.stringify({ type: "caption_update", id: 1, strong: true, strong_state: "ok", translated: "强模型译文" }) });
    assert.equal(btn.textContent, "已重译");
    assert.equal(btn.disabled, true);

    // 另一条从没点过、也不是大模型译文的字幕：应该是可点的初始状态
    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 2, ts: Date.now() / 1000, original: "adios" }) });
    const card2 = captionList.children[1];
    const btn2 = card2.children[card2.children.length - 1];
    assert.equal(btn2.disabled, false);

    // 断线：已经渲染出来的重译按钮要立刻禁用，不用等下一条消息
    ws.onclose({ code: 1006 });
    assert.equal(btn2.disabled, true, "断线后重译按钮应该立刻禁用");
  });
});

test("手机端「取消警报」：✕ 只在本机隐藏、回放不复活；批量清除同理；都不发服务端", () => {
  withFakeViewerPage(({ elements, ws, store }) => {
    const alertList = elements["alert-list"];

    ws.onmessage({ data: JSON.stringify({ type: "alert", alert_id: "a1", term: "x", tier: "exact", ts: Date.now() / 1000, context: "ctx1" }) });
    assert.equal(alertList.children.length, 1);

    const item1 = alertList.children[0];
    const head1 = item1.children[0];
    const dismissBtn1 = head1.children[1];
    dismissBtn1.click();
    assert.equal(alertList.children.length, 0, "点 ✕ 应该立刻从列表摘掉");
    assert.equal(ws.sent.length, 0, "取消警报绝不发服务端");

    // 回放同一个 alert_id：不应该复活
    ws.onmessage({ data: JSON.stringify({ type: "alert", alert_id: "a1", term: "x", tier: "exact", ts: Date.now() / 1000, context: "ctx1", replay: true }) });
    assert.equal(alertList.children.length, 0, "本机取消过的 alert_id，回放也不应该重新出现");

    // 两条新报警，用「清除已看过的报警」批量隐藏
    ws.onmessage({ data: JSON.stringify({ type: "alert", alert_id: "a2", term: "y", tier: "fuzzy", ts: Date.now() / 1000, context: "ctx2" }) });
    ws.onmessage({ data: JSON.stringify({ type: "alert", alert_id: "a3", term: "z", tier: "variant", ts: Date.now() / 1000, context: "ctx3" }) });
    assert.equal(alertList.children.length, 2);

    elements["alert-clear-seen"].click();
    assert.equal(alertList.children.length, 0, "清除已看过的报警应该把当前显示的全部隐藏");
    assert.equal(ws.sent.length, 0, "清除已看过的报警也绝不发服务端");

    ws.onmessage({ data: JSON.stringify({ type: "alert", alert_id: "a2", term: "y", tier: "fuzzy", ts: Date.now() / 1000, context: "ctx2", replay: true }) });
    ws.onmessage({ data: JSON.stringify({ type: "alert", alert_id: "a3", term: "z", tier: "variant", ts: Date.now() / 1000, context: "ctx3", replay: true }) });
    assert.equal(alertList.children.length, 0, "批量清除过的 id，回放也不应该重新出现");

    // 全新、从没取消过的报警：必须照常显示——不是「自动」隐藏新报警
    ws.onmessage({ data: JSON.stringify({ type: "alert", alert_id: "a4", term: "w", tier: "exact", ts: Date.now() / 1000, context: "ctx4" }) });
    assert.equal(alertList.children.length, 1, "新报警必须照常显示");

    const stored = JSON.parse(store.get("viewerDismissedAlerts"));
    assert.ok(["a1", "a2", "a3"].every((id) => stored.includes(id)));
    assert.ok(!stored.includes("a4"));
  });
});

// ---- DOM 回归：违禁词警示开关的常驻提示行 ----
test("alert_mode 广播：关闭时常驻灰字，打开时隐藏；新场次先隐藏，等 replay 刷回真实值", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const line = elements["alert-mode-line"];
    assert.equal(line.classList.contains("hidden"), true, "viewer_hello 之后、alert_mode 到达之前先保持隐藏");

    ws.onmessage({ data: JSON.stringify({ type: "alert_mode", on: false }) });
    assert.equal(line.classList.contains("hidden"), false, "关闭时应该显示常驻提示");
    assert.equal(line.textContent, "违禁词警示已关闭（中控开播时未开启）");

    ws.onmessage({ data: JSON.stringify({ type: "alert_mode", on: true }) });
    assert.equal(line.classList.contains("hidden"), true, "打开时应该隐藏这一行");

    // 断线重连：新场次的 viewer_hello 先把提示行盖回隐藏，不沿用上一场「关闭」的状态，
    // 直到回放的 alert_mode 把真实值刷回来
    ws.onmessage({ data: JSON.stringify({ type: "alert_mode", on: false }) });
    assert.equal(line.classList.contains("hidden"), false);
    ws.onmessage({ data: JSON.stringify({ type: "viewer_hello", ok: true, ts: Date.now() / 1000, share_since: 0, viewers: 1, max_viewers: 12, read_only: true }) });
    assert.equal(line.classList.contains("hidden"), true, "新场次的 viewer_hello 应该先隐藏，不沿用上一场");
  });
});

// ---- DOM 回归：自动滚动认的是外层容器 #caption-section ----
// 第一版滚的是 #caption-list（不溢出，设 scrollTop 无效），手机上不会自动滚动。
test("新字幕到达时滚动的是 #caption-section；用户上翻后不抢滚动，点「最新」再贴底", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const section = elements["caption-section"];
    const list = elements["caption-list"];
    const jump = elements["jump-latest"];
    section.clientHeight = 600;
    section.scrollHeight = 3000;
    list.scrollHeight = 3000;   // 列表和容器一样高：只有容器该被滚

    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 1, ts: 1, original: "hola", translated: "你好" }) });
    assert.equal(section.scrollTop, 3000, "跟随中：新字幕到达应把容器滚到底");
    assert.equal(list.scrollTop, 0, "内层列表不该被当成滚动容器");

    // 用户往上翻（离底部远于 FOLLOW_SLACK）：停止跟随，显示「最新」按钮。
    // 先有一次真实的触摸输入，随后的 scroll 才算用户自己滚的
    section.scrollTop = 1000;
    section.dispatch("touchstart");
    section.dispatch("scroll");
    section.scrollHeight = 3400;
    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 2, ts: 2, original: "adios", translated: "再见" }) });
    assert.equal(section.scrollTop, 1000, "上翻后新字幕不该抢滚动");
    assert.equal(jump.classList.contains("hidden"), false, "上翻后应显示「最新」按钮");

    // 译文后补也不抢
    ws.onmessage({ data: JSON.stringify({ type: "caption_update", id: 2, translated: "再见了" }) });
    assert.equal(section.scrollTop, 1000, "译文后补时上翻状态也不该抢滚动");

    jump.click();
    assert.equal(section.scrollTop, 3400, "点「最新」应贴底");
    assert.equal(jump.classList.contains("hidden"), true, "贴底后按钮隐藏");

    // 跟随中译文后补（卡片长高）也要再贴一次
    section.scrollHeight = 3500;
    ws.onmessage({ data: JSON.stringify({ type: "caption_update", id: 2, translated: "再见了，朋友" }) });
    assert.equal(section.scrollTop, 3500, "跟随中译文补进来应再贴底");
  });
});

// ---- DOM 回归：切到别的 App / 标签页再回来，仍然跟随 ----
// 第二版每个 scroll 事件都现算「在不在底部」：页面在后台时浏览器重置 scrollTop 并抛
// scroll 事件，跟随就被关掉，回来后不再自动滚动（用户报的）。
test("页面在后台期间的 scroll 事件不关闭跟随；回到前台补滚一次", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const section = elements["caption-section"];
    const jump = elements["jump-latest"];
    section.clientHeight = 600;
    section.scrollHeight = 3000;
    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 1, ts: 1, original: "hola", translated: "你好" }) });
    assert.equal(section.scrollTop, 3000);

    // 切到后台：浏览器把 scrollTop 重置并抛 scroll（没有任何用户输入）
    global.document.hidden = true;
    section.scrollTop = 0;
    section.dispatch("scroll");
    // 后台期间来了新字幕：内容变高，但滚动请求可能落空——模拟成落空
    section.scrollHeight = 3600;

    // 回到前台：补滚到底，且「最新」按钮不出现
    global.document.hidden = false;
    global.document.dispatch("visibilitychange");
    assert.equal(section.scrollTop, 3600, "回到前台应补滚到最新");
    assert.equal(jump.classList.contains("hidden"), true);

    // 之后的新字幕继续跟随
    section.scrollHeight = 3900;
    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 2, ts: 2, original: "adios", translated: "再见" }) });
    assert.equal(section.scrollTop, 3900, "回来之后仍在跟随");
  });
});

test("不可测量（尺寸读成 0）时的 scroll 事件沿用原意图", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const section = elements["caption-section"];
    section.clientHeight = 0;          // 页面不可渲染时浏览器给的就是 0
    section.scrollHeight = 0;
    section.dispatch("touchstart");
    section.dispatch("scroll");        // 即便有过触摸，也不能据 0 判断「用户翻上去了」
    section.clientHeight = 600;
    section.scrollHeight = 2000;
    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 1, ts: 1, original: "hola", translated: "你好" }) });
    assert.equal(section.scrollTop, 2000, "恢复可测量后仍在跟随");
  });
});

// ---- DOM 回归：session_break 场次分隔（renderSessionBreak）----
// 上面 shouldRenderSessionDivider/sessionDividerText 只测了纯逻辑；这里用
// withFakeViewerPage 把 renderSessionBreak 真正建出来的 DOM 节点找到，
// 验证首场不画线、真实换场时 class 和分隔文案都对、主播名不泄露到手机端。
test("session_break：程序启动后的第一场（还没有任何字幕卡片）不画分隔线", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const captionList = elements["caption-list"];
    ws.onmessage({ data: JSON.stringify({ type: "session_break", ts: 1000 }) });
    assert.equal(captionList.children.length, 0);
  });
});

test("session_break：真实换场时，旧卡片打上 prev-session，插入一条分隔线", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const captionList = elements["caption-list"];
    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 1, ts: 1, original: "hola" }) });
    assert.equal(captionList.children.length, 1);
    const oldCard = captionList.children[0];
    assert.equal(oldCard.classList.contains("prev-session"), false);

    ws.onmessage({ data: JSON.stringify({ type: "session_break", ts: 1000 }) });

    assert.equal(captionList.children.length, 2, "旧卡片 + 一条分隔线");
    assert.equal(oldCard.classList.contains("prev-session"), true);
    const sep = captionList.children[1];
    assert.equal(sep.className, "session-sep");
    assert.equal(sep.textContent, "── 新的一场 ──");
  });
});

test("session_break：即便消息里带了 streamer，手机端分隔文案也绝不带主播名", () => {
  withFakeViewerPage(({ elements, ws }) => {
    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 1, ts: 1, original: "hola" }) });
    // 正常情况下服务端的 ALLOW 白名单已经把 streamer 过滤掉（见
    // app/viewer.py），这里故意在消息里塞一个，确认前端自己也不会拿来拼文案——
    // 双重保险，任何一层疏漏都不至于把主播身份漏给手机端
    ws.onmessage({ data: JSON.stringify({ type: "session_break", ts: 1000, streamer: "leakedname" }) });
    const sep = elements["caption-list"].children[1];
    assert.equal(sep.textContent.indexOf("leakedname"), -1);
    assert.equal(sep.textContent, "── 新的一场 ──");
  });
});
