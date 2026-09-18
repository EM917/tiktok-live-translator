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
