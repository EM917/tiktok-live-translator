// 手机同看页面纯逻辑的契约测试（web/viewer.js）。
//
// viewer.js 同时是浏览器页面脚本和 node:test 的被测模块：文件里用
// `typeof document !== "undefined"` 把 DOM/WebSocket 那段挡住，Node 环境下
// 整段跳过，这里只测挡住之外的纯函数。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync, readdirSync } from "node:fs";

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

// ---- reconnectTexts（V4、V7）----
// V7：秒数是离掉线那一刻的秒数，由调用方算好传进来；还没连上过传 null。
// V4：胶囊里只放短句，「请找中控确认…」那句放到胶囊下面能换行的 #stale-line
test("reconnectTexts：断开不到 60 秒是普通的「N 秒后重连」，#stale-line 不显示", () => {
  assert.deepEqual(V.reconnectTexts(null, 3), { pill: "已断开，3 秒后重连…", line: "" });
  assert.deepEqual(V.reconnectTexts(0, 1), { pill: "已断开，1 秒后重连…", line: "" });
  assert.deepEqual(V.reconnectTexts(60, 2), { pill: "已断开，2 秒后重连…", line: "" }, "正好 60 秒还不算拖久");
});
test("reconnectTexts：断开超过 60 秒，胶囊只写「未连接 N 秒」，该做什么写在 #stale-line", () => {
  assert.equal(V.STALE_RECONNECT_SEC, 60);
  assert.deepEqual(V.reconnectTexts(61, 15), { pill: "未连接 61 秒", line: "请找中控确认同看是否还开着，或重新扫码" });
  assert.equal(V.statusText("offline", 1800), "未连接 1800 秒");
});
test("胶囊长度：断开再久，也不比已有最长的胶囊文案长（R4 量过它在 320 宽放得下）", () => {
  const longest = Math.max(...["connecting", "connected", "denied", "full", "off"].map((s) => V.statusText(s).length),
                           V.statusText("reconnecting", 15).length);
  for (const sec of [61, 1800, 86400, 999999]) {
    const pill = V.reconnectTexts(sec, 15).pill;
    assert.ok(pill.length <= longest, pill + " 比已有最长的胶囊文案（" + longest + " 字）长");
  }
});

// ---- streamText ----
test("streamText 覆盖 status.state 全集", () => {
  assert.equal(V.streamText("idle"), "未开始");
  assert.equal(V.streamText("connecting"), "连接中");
  assert.equal(V.streamText("live"), "直播中");
  assert.equal(V.streamText("ended"), "直播已结束", "与桌面 app.js 的 ended 同一句");
  assert.equal(V.streamText("error"), "已停止");
});
test("streamText 未知值有兜底文案", () => {
  assert.equal(V.streamText("weird-state"), "状态未知");
  assert.equal(V.streamText(null), "状态未知");
});
// 以前 STREAM_TEXT 只核对了 pipeline 的四种，漏了 ended：主播下播时手机只显示「状态未知」。
// 这里直接读后端源码，把 server.status("…") 发出的 state 全抽出来逐个核对，后端加了新状态会红
test("后端 server.status() 发出的每一种 state，手机都有自己的字", () => {
  const root = new URL("../", import.meta.url);
  const files = [new URL("main.py", root)].concat(
    readdirSync(new URL("app/", root)).filter((f) => f.endsWith(".py")).map((f) => new URL("app/" + f, root)));
  const states = new Set();
  let calls = 0;
  let literal = 0;
  for (const f of files) {
    const src = readFileSync(f, "utf8");
    calls += (src.match(/server\.status\(/g) || []).length;
    for (const m of src.matchAll(/server\.status\(\s*"([a-z_]+)"/g)) { literal += 1; states.add(m[1]); }
  }
  assert.ok(calls > 0 && states.has("live"), "没抽到 server.status()：源码位置变了，这条测试要跟着改");
  assert.equal(literal, calls, "有一处 server.status() 的 state 不是字面量，这里认不出它能发哪些值");
  for (const s of states) assert.notEqual(V.streamText(s), "状态未知", "手机不认识后端的 state：" + s);
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

// ---- 取消记录属于「发出这些 id 的那一次共享」----
// 事故（2026-10-16 审阅）：alert_id 是进程内计数器，程序重启后从 1 重新数；手机 localStorage
// 里存的是光秃秃的 id，于是「取消过 7 号」把重启后全新的 7 号报警也静默藏了。
test("createDismissedStore.bind：同一次共享留着取消记录，换了一次共享就清空并落盘", () => {
  const storage = fakeStorage();
  const first = V.createDismissedStore(storage);
  first.bind(1000.5);
  first.add("7");
  assert.equal(first.has("7"), true);
  assert.equal(storage.getItem("viewerDismissedAlertsScope"), "1000.5");

  // 刷新页面/断线重连：新建一个 store，hello 带来同一个 share_since → 记录还在
  const sameRun = V.createDismissedStore(storage);
  assert.equal(sameRun.bind(1000.5), false, "同一次共享不该清东西");
  assert.equal(sameRun.has("7"), true);

  // 程序重启：share_since 变了 → 旧 id 全部作废，包括落盘的那份
  const afterRestart = V.createDismissedStore(storage);
  assert.equal(afterRestart.bind(2000.25), true, "换了一次共享，应报告清掉了东西");
  assert.equal(afterRestart.has("7"), false, "上一次共享里取消过的 7 号，不能藏这一次的 7 号");
  assert.deepEqual(JSON.parse(storage.getItem("viewerDismissedAlerts")), []);
  assert.equal(storage.getItem("viewerDismissedAlertsScope"), "2000.25");
  afterRestart.add("7");                       // 这一次共享里取消的 7 号照样记得住
  assert.equal(V.createDismissedStore(storage).has("7"), true);
});
test("createDismissedStore.bind：老版本留下的记录没有归属，第一次 hello 就清掉", () => {
  const storage = fakeStorage({ viewerDismissedAlerts: JSON.stringify(["1", "7"]) });
  const store = V.createDismissedStore(storage);
  assert.equal(V.loadDismissedScope(storage), null);
  assert.equal(store.bind(1000), true);
  assert.equal(store.has("1"), false);
  assert.equal(store.has("7"), false);
  assert.equal(V.loadDismissedScope(storage), "1000");
});
test("createDismissedStore.bind：share_since 为 0 是合法归属；缺省（null/undefined）当同一个空归属", () => {
  const storage = fakeStorage();
  const store = V.createDismissedStore(storage);
  store.bind(0);
  store.add("3");
  assert.equal(V.createDismissedStore(storage).bind(0), false, "0 不是「没有」，不能被当成缺省");
  assert.equal(V.createDismissedStore(storage).bind(1), true);

  const a = V.createDismissedStore(fakeStorage());
  assert.doesNotThrow(() => a.bind(undefined));
  a.add("3");
  assert.equal(a.bind(null), false, "undefined 与 null 同属缺省，不互相清");
  assert.equal(a.has("3"), true);
});
test("createDismissedStore.bind：storage 抛错/没有 storage 也不抛，内存态照常清", () => {
  const throwing = V.createDismissedStore({
    getItem: () => { throw new Error("boom"); },
    setItem: () => { throw new Error("boom"); },
  });
  throwing.bind(1);
  throwing.add("7");
  assert.equal(throwing.has("7"), true);
  assert.doesNotThrow(() => throwing.bind(2));
  assert.equal(throwing.has("7"), false);
  const none = V.createDismissedStore(undefined);
  assert.doesNotThrow(() => none.bind(1));
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
// clock：可调的假时钟（毫秒），Date.now 读它；tick()：把页面注册的 setInterval 回调各跑一次
// （1 秒的连接秒表、2 秒的贴底兜底），时间不会自己走，要先改 clock.now
function withFakeViewerPage(run) {
  const elements = baseElements();
  const FakeWebSocket = makeFakeWebSocket();
  const originals = {
    document: global.document, location: global.location, WebSocket: global.WebSocket,
    localStorage: global.localStorage, setTimeout: global.setTimeout, setInterval: global.setInterval,
    now: Date.now,
  };
  const modPath = require.resolve("../web/viewer.js");
  const store = new Map();
  const clock = { now: Date.UTC(2026, 9, 22, 12, 0, 0) };
  const intervals = [];
  const tick = () => intervals.slice().forEach((fn) => fn());

  global.document = makeFakeDocument(elements);
  global.location = { hash: "#k=fake-token-not-a-real-secret-0000000000", protocol: "http:", host: "127.0.0.1:0" };
  global.WebSocket = FakeWebSocket;
  global.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
  };
  global.setTimeout = (fn) => { (global.setTimeout._queue = global.setTimeout._queue || []).push(fn); return 1; };
  global.setInterval = (fn) => { intervals.push(fn); return 0; };
  Date.now = () => clock.now;

  try {
    delete require.cache[modPath];
    require("../web/viewer.js");
    const ws1 = FakeWebSocket.instances[0];
    ws1.onopen();
    ws1.onmessage({ data: JSON.stringify({ type: "viewer_hello", ok: true, ts: Date.now() / 1000, share_since: 0, viewers: 1, max_viewers: 12, read_only: true }) });
    ws1.sent = [];   // 握手时发的 auth 帧不是被测行为，清空后让调用方从干净基线数起
    // 让排着的重连定时器到点：connect() 建一条新连接并返回它（还没收到 viewer_hello）
    const fireReconnect = () => {
      const q = global.setTimeout._queue || [];
      while (q.length) q.shift()();
      const next = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
      if (next.onopen) next.onopen();
      return next;
    };
    run({ elements, ws: ws1, store, FakeWebSocket, clock, tick, fireReconnect });
  } finally {
    global.document = originals.document;
    global.location = originals.location;
    global.WebSocket = originals.WebSocket;
    global.localStorage = originals.localStorage;
    global.setTimeout = originals.setTimeout;
    global.setInterval = originals.setInterval;
    Date.now = originals.now;
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

// ---- DOM 回归：取消记录跟着「这一次共享」走 ----
function helloMsg(shareSince) {
  return JSON.stringify({ type: "viewer_hello", ok: true, ts: Date.now() / 1000, share_since: shareSince, viewers: 1, max_viewers: 12, read_only: true });
}
function alertMsg(id, extra) {
  return JSON.stringify(Object.assign({ type: "alert", alert_id: id, term: "x", tier: "exact", ts: Date.now() / 1000, context: "ctx" + id }, extra || {}));
}

test("手机端「取消警报」：程序重启后 alert_id 从头数，上一次共享里取消过的号不藏这一次的新报警", () => {
  withFakeViewerPage(({ elements, ws, store }) => {
    const alertList = elements["alert-list"];
    ws.onmessage({ data: alertMsg(7) });
    assert.equal(alertList.children.length, 1);
    alertList.children[0].children[0].children[1].click();            // 在这一次共享里取消 7 号
    assert.equal(alertList.children.length, 0);

    // 中控重启了程序：手机断线重连，viewer_hello 带来新的 share_since，计数器又数到了 7
    ws.onclose({ code: 1006 });
    ws.onmessage({ data: helloMsg(1760600000.5) });
    ws.onmessage({ data: alertMsg(7) });
    assert.equal(alertList.children.length, 1, "重启后的新 7 号报警必须显示（桌面和审计里都有它）");
    assert.equal(store.get("viewerDismissedAlertsScope"), "1760600000.5");
    assert.deepEqual(JSON.parse(store.get("viewerDismissedAlerts")), [], "旧的取消记录应已作废");

    // 这一次共享里再取消它，照样生效
    alertList.children[0].children[0].children[1].click();
    ws.onmessage({ data: alertMsg(7, { replay: true }) });
    assert.equal(alertList.children.length, 0);
  });
});

test("手机端「取消警报」：同一次共享里断线重连，取消过的报警仍不复活", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const alertList = elements["alert-list"];
    ws.onmessage({ data: alertMsg(7) });
    ws.onmessage({ data: alertMsg(8) });
    assert.equal(alertList.children.length, 2);
    alertList.children[0].children[0].children[1].click();            // 取消先到的 7 号
    assert.equal(alertList.children.length, 1);

    // 手机网络抖了一下：重连后 hello 的 share_since 没变，回放里 7、8 都会再发一遍
    ws.onclose({ code: 1006 });
    ws.onmessage({ data: helloMsg(0) });
    ws.onmessage({ data: alertMsg(7, { replay: true }) });
    ws.onmessage({ data: alertMsg(8, { replay: true }) });
    assert.equal(alertList.children.length, 1, "7 号取消过，回放不复活；8 号没取消，照常显示");
    ws.onmessage({ data: alertMsg(9) });
    assert.equal(alertList.children.length, 2, "新报警照常显示");
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

// ---- DOM 回归：断线秒数从掉线那一刻算起（V7），长句在 #stale-line（V4）----
// 补核（10-22）实测：连接 30 分钟后掉线，掉线那一刻胶囊就写「已经 1800 秒没连上。请找中控确认…」——
// 秒数从上一次 viewer_hello 算起，把连着的时间也算了进去；这句还塞在不换行的胶囊里，被截掉一半。
test("连着 30 分钟后掉线：那一刻是普通的「N 秒后重连」；断开 61 秒才改口，长句放在 #stale-line", () => {
  withFakeViewerPage(({ elements, ws, clock, tick, fireReconnect }) => {
    const pill = elements["conn-text"];
    const line = elements["stale-line"];
    ws.onmessage({ data: JSON.stringify({ type: "status", state: "live" }) });
    clock.now += 30 * 60 * 1000;
    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 1, ts: clock.now / 1000, original: "hola" }) });
    ws.onclose({ code: 1006 });
    assert.match(pill.textContent, /^已断开，\d+ 秒后重连…$/, "掉线那一刻不能把连着的 30 分钟算进去");
    assert.equal(line.classList.contains("hidden"), true);

    clock.now += 60 * 1000;
    tick();
    assert.match(pill.textContent, /^已断开，\d+ 秒后重连…$/, "断开正好 60 秒还不算拖久");
    assert.equal(line.classList.contains("hidden"), true);

    clock.now += 1000;
    tick();
    assert.equal(pill.textContent, "未连接 61 秒");
    assert.equal(line.classList.contains("hidden"), false, "该做什么写在胶囊下面那一行");
    assert.equal(line.textContent, "请找中控确认同看是否还开着，或重新扫码");

    // 重连上了：那一行立刻收起，不等下一秒的秒表；胶囊回到「已连接」
    const ws2 = fireReconnect();
    assert.equal(pill.textContent, "未连接 61 秒", "新连接还没收到 hello：仍算断开");
    ws2.onmessage({ data: helloMsg(0) });
    assert.equal(pill.textContent, "已连接");
    assert.equal(line.classList.contains("hidden"), true);
    assert.equal(line.textContent, "");

    // 再掉一次：从这一次掉线重新数，不接着上一次的 61 秒
    clock.now += 5 * 60 * 1000;
    ws2.onmessage({ data: JSON.stringify({ type: "caption", id: 2, ts: clock.now / 1000, original: "adios" }) });
    ws2.onclose({ code: 1006 });
    clock.now += 30 * 1000;
    tick();
    assert.match(pill.textContent, /^已断开，\d+ 秒后重连…$/);
    assert.equal(line.classList.contains("hidden"), true);
  });
});

test("掉线后在「重连中 / 已满 / 已关闭」之间转：秒数仍从第一次掉线那一刻数起", () => {
  withFakeViewerPage(({ elements, ws, clock, tick, fireReconnect }) => {
    clock.now += 10 * 60 * 1000;
    ws.onclose({ code: 4429 });                 // 已满：胶囊写自己的字，不显示秒数
    assert.equal(elements["conn-text"].textContent, "同看人数已满（12 人），稍后再试");
    assert.equal(elements["stale-line"].classList.contains("hidden"), true);
    clock.now += 50 * 1000;
    fireReconnect().onclose({ code: 1006 });   // 下一次重连没有关闭帧就断了：回到普通重连
    tick();
    assert.match(elements["conn-text"].textContent, /^已断开，\d+ 秒后重连…$/, "离掉线才 50 秒");
    clock.now += 20 * 1000;
    tick();
    assert.equal(elements["conn-text"].textContent, "未连接 70 秒");
    assert.equal(elements["stale-line"].textContent, "请找中控确认同看是否还开着，或重新扫码");
  });
});

test("已连接时 #stale-line 照旧是「N 秒没有新消息」，和断线那句不同时出现", () => {
  withFakeViewerPage(({ elements, clock, tick }) => {
    const line = elements["stale-line"];
    clock.now += 46 * 1000;
    tick();
    assert.equal(line.classList.contains("hidden"), false);
    assert.equal(line.textContent, "46 秒没有新消息");
  });
});

// ---- DOM 回归：翻译失败 / 因积压跳过的字幕要有字（F4）----
// 服务端发的是 translate_state（failed / dropped），不带 failed/why：以前译文行写成空串，只剩一行空白
function captionTrans(elements, i) {
  const card = elements["caption-list"].children[i];
  return card.children[2];
}
test("字幕的翻译结局：failed、dropped、ok、pending 各显示对应的字，不再留空行", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    send({ type: "caption", id: 1, ts: 1, original: "uno", translated: null, translate_state: "failed" });
    send({ type: "caption", id: 2, ts: 2, original: "dos", translated: null, translate_state: "dropped" });
    send({ type: "caption", id: 3, ts: 3, original: "tres", translated: "三", translate_state: "ok" });
    send({ type: "caption", id: 4, ts: 4, original: "cuatro", translate_state: "pending" });
    const t = [0, 1, 2, 3].map((i) => captionTrans(elements, i));
    assert.equal(t[0].textContent, "翻译失败");
    assert.equal(t[1].textContent, "翻译已跳过（积压）");
    assert.equal(t[2].textContent, "三");
    assert.equal(t[3].textContent, "翻译中…");
    assert.equal(t[0].classList.contains("failed"), true, "套用已有的 .failed 样式");
    assert.equal(t[1].classList.contains("failed"), true);
    assert.equal(t[2].classList.contains("failed") || t[2].classList.contains("pending"), false);
    assert.equal(t[3].classList.contains("pending"), true);
    // 失败、跳过的字是界面提示，不是数据（英文界面的检查不豁免它们）
    assert.equal(t[0].classList.contains("i18n-data"), false);
    assert.equal(t[1].classList.contains("i18n-data"), false);
    assert.equal(t[2].classList.contains("i18n-data"), true);
  });
});
test("字幕的翻译结局经 caption_update 到达（pipeline 的实际路径），重译成功后换成译文", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    send({ type: "caption", id: 1, ts: 1, original: "uno", translated: null, translate_state: "pending" });
    send({ type: "caption", id: 2, ts: 2, original: "dos", translated: null, translate_state: "pending" });
    send({ type: "caption_update", id: 1, translated: null, translate_state: "failed" });
    send({ type: "caption_update", id: 2, translated: null, translate_state: "dropped" });
    assert.equal(captionTrans(elements, 0).textContent, "翻译失败");
    assert.equal(captionTrans(elements, 1).textContent, "翻译已跳过（积压）");
    send({ type: "caption_update", id: 1, strong: true, strong_state: "ok", translated: "一", translate_state: "ok" });
    assert.equal(captionTrans(elements, 0).textContent, "一");
    assert.equal(captionTrans(elements, 0).classList.contains("failed"), false);
    // 老的 failed/why 写法照旧：带原因的那句更具体，优先
    send({ type: "caption", id: 3, ts: 3, original: "tres", failed: true, why: "超时", translate_state: "failed" });
    assert.equal(captionTrans(elements, 2).textContent, "译文失败（超时）——请看上面的原话");
  });
});

// ---- DOM 回归：弹幕面板贴底跟随（F6）----
// 补核（10-22）真 ViewerHub 加真手机页实测：展开后 scrollTop=0、最新一条不在视野里；重连后显示的是
// 回放 30 条里最旧的 5 条。下面给假列表装上和浏览器一样的几何：收起（display:none）时量出来全是 0，
// 展开时面板 280px 高、每条 60px；还在「翻译中…」的那条矮 20px，译文补上之后长到 60px（后补的译文
// 比「翻译中…」长时会折行，这一条就长高）——这时贴底靠的是 updateComment 里的那一下，假几何里条目
// 不长高的话，删掉它测试也看不出来；scrollTop 读的时候夹在 [0, scrollHeight − clientHeight] 里；
// 不做滚动锚定（viewer.css 给 .comment-list 关掉了）
const CMT_H = 60;
const CMT_PENDING_H = 40;
const PANEL_H = 280;
function comment(n, extra) {
  return JSON.stringify(Object.assign({ type: "comment", id: "c" + n, user: "viewer_" + n, ts: n,
                                        text: "comentario " + n, translated: "弹幕 " + n, state: "ok" }, extra || {}));
}
function cmtHeight(item) {   // 看这一条的译文行现在是不是「翻译中…」（renderComment/updateComment 用 .pending 标）
  const zh = item.children.find((c) => c.className === "cmt-zh");
  return zh && zh.classList.contains("pending") ? CMT_PENDING_H : CMT_H;
}
function commentGeometry(list) {
  const shown = () => !list.classList.contains("hidden");
  const append = list.appendChild.bind(list);
  list.appendChild = (child) => {
    Object.defineProperty(child, "offsetHeight", { configurable: true, get: () => cmtHeight(child) });
    return append(child);
  };
  Object.defineProperty(list, "clientHeight", { configurable: true, get: () => (shown() ? PANEL_H : 0) });
  Object.defineProperty(list, "scrollHeight", { configurable: true,
    get: () => (shown() ? list.children.reduce((sum, c) => sum + cmtHeight(c), 0) + 8 : 0) });
  let top = 0;
  const max = () => Math.max(0, list.scrollHeight - list.clientHeight);
  Object.defineProperty(list, "scrollTop", { configurable: true,
    get: () => Math.max(0, Math.min(top, max())), set: (v) => { top = Math.max(0, Math.min(v, max())); } });
  return {
    below: () => Math.max(0, list.scrollHeight - list.clientHeight) - list.scrollTop,   // 底下还藏着多少像素，0 = 在底部
    firstVisible: () => {   // 视野顶上那一条：按每条自己的高度往下累加
      let y = 0;
      for (const c of list.children) {
        y += cmtHeight(c);
        if (y > list.scrollTop) return c.dataset.id;
      }
      return null;
    },
  };
}
function userScrollsTo(list, top) {   // 用户自己滚：先有一次真实的触摸，随后的 scroll 才算用户滚的
  list.dispatch("touchstart");
  list.scrollTop = top;
  list.dispatch("scroll");
}

test("弹幕面板：收起时来的弹幕，展开那一刻就在底部（以前停在最旧的几条）", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const list = elements["comment-list"];
    const geo = commentGeometry(list);
    assert.equal(list.classList.contains("hidden"), true, "默认收起");
    for (let n = 1; n <= 12; n++) ws.onmessage({ data: comment(n) });
    assert.equal(list.scrollTop, 0, "收起时滚不动");
    elements["comment-toggle"].click();
    assert.equal(list.classList.contains("hidden"), false);
    assert.equal(geo.below(), 0, "展开就该看到最新一条");

    // 翻上去、收起、再展开：展开仍回到底部，并恢复跟随
    userScrollsTo(list, 0);
    elements["comment-toggle"].click();
    elements["comment-toggle"].click();
    assert.equal(geo.below(), 0);
    ws.onmessage({ data: comment(13) });
    assert.equal(geo.below(), 0, "展开之后照常跟随");
  });
});

test("弹幕面板：贴底时新弹幕（含裁到 30 条、译文后补）都跟到底", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const list = elements["comment-list"];
    const geo = commentGeometry(list);
    elements["comment-toggle"].click();
    for (let n = 1; n <= 40; n++) {
      ws.onmessage({ data: comment(n, { state: "pending", translated: null }) });
      assert.equal(geo.below(), 0, "第 " + n + " 条之后应在底部");
      const before = list.scrollHeight;
      ws.onmessage({ data: JSON.stringify({ type: "comment_update", id: "c" + n, translated: "弹幕 " + n, state: "ok" }) });
      assert.equal(list.scrollHeight - before, CMT_H - CMT_PENDING_H, "译文补上之后这一条长高");
      assert.equal(geo.below(), 0, "第 " + n + " 条译文补上之后仍在底部");
    }
    assert.equal(list.children.length, 30, "只留最近 30 条");
    assert.equal(list.children[29].dataset.id, "c40");
  });
});

test("弹幕面板：翻上去后新弹幕不拉动；顶上裁掉一条时看着的那几条不动；滚回底部恢复跟随", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const list = elements["comment-list"];
    const geo = commentGeometry(list);
    elements["comment-toggle"].click();
    for (let n = 1; n <= 30; n++) ws.onmessage({ data: comment(n) });
    userScrollsTo(list, 600);
    assert.equal(geo.firstVisible(), "c11");

    ws.onmessage({ data: comment(31) });             // 到了上限：顶上的 c1 被裁掉
    assert.equal(list.children.length, 30);
    assert.equal(list.children[0].dataset.id, "c2");
    assert.equal(list.scrollTop, 600 - CMT_H, "scrollTop 减回一条的高度");
    assert.equal(geo.firstVisible(), "c11", "视野里还是刚才那几条");
    ws.onmessage({ data: JSON.stringify({ type: "comment_update", id: "c31", translated: "弹幕 31 改" }) });
    assert.equal(geo.firstVisible(), "c11", "译文后补也不拉动");

    userScrollsTo(list, list.scrollHeight);          // 自己滚回底部：恢复跟随
    ws.onmessage({ data: comment(32) });
    assert.equal(geo.below(), 0, "滚回底部之后又跟着最新的走");
  });
});

test("弹幕面板：重连回放之后在底部；回放期间跟随意图被 scroll 事件改掉，回放结束时也拉回来", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const list = elements["comment-list"];
    const geo = commentGeometry(list);
    elements["comment-toggle"].click();
    for (let n = 1; n <= 30; n++) ws.onmessage({ data: comment(n) });
    userScrollsTo(list, 100);                         // 掉线前刚翻过（还在 USER_INPUT_WINDOW_MS 之内）

    ws.onclose({ code: 1006 });
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    send({ type: "viewer_hello", ok: true, ts: 1, share_since: 0, viewers: 1, max_viewers: 12, read_only: true });
    assert.equal(list.children.length, 0, "hello 清空重建");
    // replay_snapshot 的顺序：先是不带 replay 标记的几条（不算回放结束），然后是带标记的回放
    send({ type: "status", state: "live" });
    send({ type: "comment_source", backend: "live" });
    send({ type: "alert_mode", on: true });
    send({ type: "incident", id: "disk", level: "warn", text: "磁盘快满了" });
    // 接着是字幕历史，弹幕回放在它后面（app/viewer.py replay_snapshot）。字幕历史里夹着的场次分隔线
    // 本来带 replay，过白名单时被丢掉（ALLOW 的 session_break 只给 ts）：它也不算回放结束
    send({ type: "caption", id: 1, ts: 1, original: "uno", translated: "一", replay: true });
    send({ type: "session_break", ts: 2 });
    send({ type: "caption", id: 2, ts: 3, original: "dos", translated: "二", replay: true, restore: true });
    for (let n = 11; n <= 40; n++) ws.onmessage({ data: comment(n, { replay: true }) });
    assert.equal(geo.below(), 0, "回放逐条追加时就该贴底");

    // 回放重建列表期间浏览器补发了一个 scroll 事件（位置在中间），掉线前那次触摸还在窗口里：跟随意图被改掉
    list.scrollTop = 300;
    list.dispatch("scroll");
    ws.onmessage({ data: comment(41) });             // 回放之后的第一条实时消息
    assert.equal(geo.below(), 0, "回放结束时强制滚到底");
    ws.onmessage({ data: comment(42) });
    assert.equal(geo.below(), 0, "之后照常跟随");
    assert.equal(list.children[29].dataset.id, "c42");
  });
});

test("弹幕面板：回放结束前用户已经自己翻了，回放结束时不替他滚", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const list = elements["comment-list"];
    const geo = commentGeometry(list);
    elements["comment-toggle"].click();
    for (let n = 1; n <= 30; n++) ws.onmessage({ data: comment(n, { replay: true }) });
    userScrollsTo(list, 200);
    assert.equal(geo.firstVisible(), "c4");
    ws.onmessage({ data: comment(31) });
    assert.ok(geo.below() > 0, "用户在看旧的，不拉到底");
    assert.equal(geo.firstVisible(), "c4");
  });
});

test("弹幕面板：翻上去时视野上方一条的译文后补、长高了：视野跟着下移一点，但不被拉回底部、也不恢复跟随", () => {
  // #120 留下的缺口：.comment-list 关了滚动锚定（overflow-anchor:none），视野上方的条目长高时浏览器不补偿
  // scrollTop，视野里的内容整体往下挪了这么多；这是接受的结果（脚本只在跟随中贴底，updateComment 的那一下）。
  // 不能接受的是：被强制拉回底部，或者跟随被悄悄打开、下一条新弹幕把视野拉走
  assert.match(readFileSync(new URL("../web/viewer.css", import.meta.url), "utf8"),
               /\.comment-list \{[^}]*overflow-anchor: none/, "这条用例的几何前提：弹幕列表不做滚动锚定");
  withFakeViewerPage(({ elements, ws }) => {
    const list = elements["comment-list"];
    const geo = commentGeometry(list);
    elements["comment-toggle"].click();
    for (let n = 1; n <= 30; n++) {
      ws.onmessage({ data: comment(n, n === 5 ? { state: "pending", translated: null } : undefined) });
    }
    userScrollsTo(list, 4 * CMT_H + CMT_PENDING_H + 5 * CMT_H);   // c5 还在「翻译中…」，视野顶上正好是 c11
    const top = list.scrollTop;
    assert.equal(geo.firstVisible(), "c11");
    const below = geo.below();

    ws.onmessage({ data: JSON.stringify({ type: "comment_update", id: "c5", translated: "弹幕 5", state: "ok" }) });
    assert.equal(list.scrollTop, top, "没人动 scrollTop：不补偿，也不拉到底");
    assert.equal(geo.firstVisible(), "c10", "c5 长高了 " + (CMT_H - CMT_PENDING_H) + "px，视野里的内容往下挪，顶上露出 c10 的下半截");
    assert.equal(geo.below(), below + (CMT_H - CMT_PENDING_H), "离底部更远了，没有被拉回去");

    ws.onmessage({ data: comment(31) });                          // 跟随没被打开：新弹幕不拉动（顶上裁掉 c1）
    assert.ok(geo.below() > 0, "仍停在翻上去的位置");
    assert.equal(geo.firstVisible(), "c10");
  });
});

// ---- 重连之后的持续提示和识别落后提示（专家审查 F3）----
// 补核用真 ViewerHub 加真手机页复现：断线期间持续提示已清、识别已追上，手机重连后两条旧提示都还在——viewer_hello
// 不清它们，replay_snapshot 也不回放 health。现在 viewer_hello 先清掉，紧随其后的回放把此刻还在的重建
function incidentTexts(elements) {
  return elements["incident-list"].children.map((r) => r.textContent);
}
function healthShown(elements) {
  const line = elements["health-line"];
  return line.classList.contains("hidden") ? null : line.children[1].textContent;
}

test("重连：断线期间撤掉的持续提示和落后提示，重连后不再显示；回放里还在的照样重建", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    send({ type: "incident", id: "session:audio_rate", level: "warn", text: "近 5 分钟只收到 60% 的直播音频" });
    send({ type: "incident", id: "disk", level: "warn", text: "磁盘快满了" });
    send({ type: "health", level: "lagging" });
    assert.deepEqual(incidentTexts(elements), ["近 5 分钟只收到 60% 的直播音频", "磁盘快满了"]);
    assert.equal(healthShown(elements), "识别开始落后，报警会有延迟");

    ws.onclose({ code: 1006 });
    // 断线期间：音频那条 clear、识别追上（level=ok）。重连的回放（replay_snapshot 的顺序）只剩磁盘那条
    send({ type: "viewer_hello", ok: true, ts: 1, share_since: 0, viewers: 1, max_viewers: 12, read_only: true });
    assert.deepEqual(incidentTexts(elements), [], "viewer_hello 先清掉");
    assert.equal(healthShown(elements), null);
    send({ type: "status", state: "live" });
    send({ type: "incident", id: "disk", level: "warn", text: "磁盘快满了" });
    assert.deepEqual(incidentTexts(elements), ["磁盘快满了"]);
    assert.equal(healthShown(elements), null, "回放里没有 health：落后提示不再挂着");

    // 再断一次：这回识别正落后，回放带着 health
    ws.onclose({ code: 1006 });
    send({ type: "viewer_hello", ok: true, ts: 2, share_since: 0, viewers: 1, max_viewers: 12, read_only: true });
    send({ type: "status", state: "live" });
    send({ type: "health", level: "degraded" });
    assert.equal(healthShown(elements), "检测已降级，识别明显落后");
  });
});

test("重连：回放里的 health 不带 replay 标记，也不算回放结束（弹幕回放后照样强制贴底一次）", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const list = elements["comment-list"];
    const geo = commentGeometry(list);
    elements["comment-toggle"].click();
    for (let n = 1; n <= 30; n++) ws.onmessage({ data: comment(n) });
    userScrollsTo(list, 100);
    ws.onclose({ code: 1006 });
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    send({ type: "viewer_hello", ok: true, ts: 1, share_since: 0, viewers: 1, max_viewers: 12, read_only: true });
    send({ type: "status", state: "live" });
    send({ type: "incident", id: "disk", level: "warn", text: "磁盘快满了" });
    send({ type: "health", level: "lagging" });                     // replay_snapshot：incident 之后、报警之前
    for (let n = 11; n <= 40; n++) ws.onmessage({ data: comment(n, { replay: true }) });
    list.scrollTop = 300;                                             // 回放期间补发的 scroll 事件改掉了跟随意图
    list.dispatch("scroll");
    ws.onmessage({ data: comment(41) });                              // 回放之后的第一条实时消息
    assert.equal(geo.below(), 0, "health 要是算成回放结束，这一下贴底就在它那里提前用掉了");
    assert.equal(healthShown(elements), "识别开始落后，报警会有延迟");
  });
});

test("连着的手机：停止、下播、出错时收掉落后提示；此刻的换场收掉，回放里夹着的换场不碰此刻的提示", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    for (const state of ["idle", "ended", "error"]) {
      send({ type: "status", state: "live" });
      send({ type: "health", level: "lagging" });
      send({ type: "status", state: "live" });
      assert.equal(healthShown(elements), "识别开始落后，报警会有延迟", "直播中的 status 不收");
      send({ type: "status", state: state });
      assert.equal(healthShown(elements), null, state + "：这一场的落后作废（服务端同时删掉 config 里的）");
    }

    // 换主播：quiet 停止不发 idle，新一场也不补发「已追上」——只有 session_break 说「换场了」
    send({ type: "caption", id: 1, ts: 1, original: "uno", translated: "一" });      // 回放早就结束了
    send({ type: "status", state: "connecting" });
    send({ type: "health", level: "lagging" });
    assert.equal(healthShown(elements), "识别开始落后，报警会有延迟");
    send({ type: "session_break", ts: 2 });
    assert.equal(healthShown(elements), null, "新的一场开始：上一场的落后提示作废");

    // 重连回放：此刻正落后（回放带着 health），字幕历史里夹着一条历史分隔线（过白名单后不带 replay）
    ws.onclose({ code: 1006 });
    send({ type: "viewer_hello", ok: true, ts: 3, share_since: 0, viewers: 1, max_viewers: 12, read_only: true });
    send({ type: "status", state: "live" });
    send({ type: "health", level: "lagging" });
    send({ type: "caption", id: 1, ts: 1, original: "uno", translated: "一", replay: true });
    send({ type: "session_break", ts: 2 });
    send({ type: "caption", id: 2, ts: 3, original: "dos", translated: "二", replay: true, restore: true });
    assert.equal(healthShown(elements), "识别开始落后，报警会有延迟", "回放里的换场是历史");
    send({ type: "caption", id: 3, ts: 4, original: "tres", translated: "三" });      // 回放之后的第一条实时消息
    send({ type: "session_break", ts: 5 });
    assert.equal(healthShown(elements), null, "回放结束之后的换场照常收掉");
  });
});

// ---- b2 审查补改：停止之后晚到的落后提示；回放在哪儿结束 ----
const HELLO = { type: "viewer_hello", ok: true, ts: 1, share_since: 0, viewers: 1, max_viewers: 12, read_only: true };

test("停止、下播、出错之后晚到的落后提示不揭开（同桌面）；下一场开始后照常显示", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    for (const state of ["idle", "ended", "error"]) {
      send({ type: "status", state: "live" });
      send({ type: "health", level: "lagging" });
      assert.equal(healthShown(elements), "识别开始落后，报警会有延迟");
      send({ type: "status", state: state });
      // 比如下播之后、统计循环被取消之前的那一跳：实时广播照样到手机
      send({ type: "health", level: "degraded" });
      assert.equal(healthShown(elements), null, state + "：这一场已经结束，晚到的不揭开");
    }
    // 重连的回放里就算还带着一条（老服务端在停止后存下的），停止的状态下也不揭开
    send(Object.assign({}, HELLO, { replay_len: 2 }));
    send({ type: "status", state: "ended" });
    send({ type: "health", level: "lagging" });
    assert.equal(healthShown(elements), null, "待机页上不重建过时的落后提示");
    // 下一场
    send({ type: "status", state: "connecting" });
    send({ type: "health", level: "lagging" });
    assert.equal(healthShown(elements), "识别开始落后，报警会有延迟");
  });
});

test("回放按 viewer_hello 带的条数结束：安静时紧接着换主播，实时的 session_break 收掉上一场的落后提示", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    // replay_snapshot 的顺序：status、health、字幕历史（中间夹着一条历史分隔线）、弹幕
    send(Object.assign({}, HELLO, { replay_len: 6 }));
    send({ type: "status", state: "live" });
    send({ type: "health", level: "lagging" });
    send({ type: "caption", id: 1, ts: 1, original: "uno", translated: "一", replay: true });
    send({ type: "session_break", ts: 2 });                                         // 回放里的：历史
    send({ type: "caption", id: 2, ts: 3, original: "dos", translated: "二", replay: true, restore: true });
    assert.equal(healthShown(elements), "识别开始落后，报警会有延迟", "回放里的换场是历史");
    send({ type: "comment", id: "c1", user: "ana", ts: 3, text: "hola", replay: true });   // 回放的最后一条
    // 直播间很安静：回放之后一条实时字幕、弹幕、报警都没有，就换了主播
    send({ type: "status", state: "connecting" });
    send({ type: "session_break", ts: 9 });
    assert.equal(healthShown(elements), null,
                 "实时的换场：上一场的落后提示作废（以前被当成回放里的历史，挂过整场新直播）");
  });
});

test("回放按条数结束时，弹幕在回放的最后一条之后就强制贴底一次，不用等第一条实时消息", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const list = elements["comment-list"];
    const geo = commentGeometry(list);
    elements["comment-toggle"].click();
    for (let n = 1; n <= 30; n++) ws.onmessage({ data: comment(n) });
    userScrollsTo(list, 100);
    ws.onclose({ code: 1006 });
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    send(Object.assign({}, HELLO, { replay_len: 31 }));
    send({ type: "status", state: "live" });
    for (let n = 11; n <= 40; n++) {
      ws.onmessage({ data: comment(n, { replay: true }) });
      if (n === 39) {
        list.scrollTop = 300;                        // 回放期间补发的 scroll 事件改掉了跟随意图
        list.dispatch("scroll");
        assert.ok(geo.below() > 0, "回放还没完：还没贴底");
      }
    }
    assert.equal(geo.below(), 0, "回放的最后一条到了就贴底");
    ws.onmessage({ data: comment(41) });             // 之后的实时弹幕照常跟随
    assert.equal(geo.below(), 0);
  });
});

test("viewer_hello 说回放 0 条：hello 之后来的第一条就是实时的", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    send({ type: "status", state: "live" });
    send({ type: "health", level: "lagging" });
    send(Object.assign({}, HELLO, { replay_len: 0 }));
    send({ type: "status", state: "live" });
    send({ type: "health", level: "lagging" });
    send({ type: "session_break", ts: 9 });
    assert.equal(healthShown(elements), null, "回放已经结束：这条换场是实时的");
  });
});

// ---- 静默断开（专家审查 F8）----
// 补核实测（10-22，Chromium）：47 秒没消息，连接对象不变，页面没有发起重连——网络在中间静默断掉时浏览器拿不到
// onclose，手机只会一直写「N 秒没有新消息」。现在服务端安静满 20 秒发一条 viewer_ping（app/viewer.py），
// 已连接时 50 秒一帧都没收到就主动断开、走正常的重连
test("silentTooLong：只在已连接时、离上一帧超过 50 秒才算", () => {
  assert.equal(V.VIEWER_SILENT_SEC, 50);
  assert.equal(V.silentTooLong("connected", 0, 50000), false, "正好 50 秒还不算");
  assert.equal(V.silentTooLong("connected", 0, 50001), true);
  assert.equal(V.silentTooLong("reconnecting", 0, 999999), false, "已经在重连：不重复断");
  assert.equal(V.silentTooLong("connecting", 0, 999999), false, "还没收到 hello：交给鉴权超时");
  assert.equal(V.silentTooLong("connected", null, 999999), false);
});

test("已连接 50 秒一帧都没收到（连 viewer_ping 也没有）：主动断开、走正常重连；撒手的旧连接迟来的事件不再处理", () => {
  withFakeViewerPage(({ elements, ws, clock, tick, fireReconnect, FakeWebSocket }) => {
    let closed = 0;
    ws.close = () => { closed += 1; };
    clock.now += 50 * 1000;
    tick();
    assert.equal(closed, 0, "正好 50 秒还不断");
    assert.equal(elements["conn-text"].textContent, "已连接");
    clock.now += 1000;
    tick();
    assert.equal(closed, 1, "超过 50 秒：主动关掉这条连接");
    assert.match(elements["conn-text"].textContent, /^已断开，\d+ 秒后重连…$/, "同没有关闭帧的断开：走正常的重连退避");
    assert.equal(elements["stream-text"].textContent, "状态未知", "断开期间不再说「直播中」");
    // 不等旧连接的 onclose（网络断了，它可能很久才来）：重连定时器已经排上
    const ws2 = fireReconnect();
    assert.equal(FakeWebSocket.instances.length, 2);
    assert.deepEqual(JSON.parse(ws2.sent[0]), { type: "auth", k: "fake-token-not-a-real-secret-0000000000" });
    // 旧连接后来才到的关闭和消息：一概不管
    ws.onclose({ code: 1006 });
    ws.onmessage({ data: JSON.stringify({ type: "caption", id: 5, ts: 1, original: "tarde" }) });
    assert.equal(elements["caption-list"].children.length, 0, "撒手的旧连接送来的字幕不画");
    assert.match(elements["conn-text"].textContent, /^已断开，\d+ 秒后重连…$/, "旧连接的 onclose 不改新连接的状态");
    ws2.onmessage({ data: helloMsg(0) });
    assert.equal(elements["conn-text"].textContent, "已连接");
    tick();
    assert.equal(FakeWebSocket.instances.length, 2, "新连接刚连上：不会马上又被当成静默");
  });
});

test("viewer_ping 让安静的连接不被误断；但它不算新消息：「N 秒没有新消息」照旧按内容数", () => {
  withFakeViewerPage(({ elements, ws, clock, tick, FakeWebSocket }) => {
    let closed = 0;
    ws.close = () => { closed += 1; };
    for (let i = 0; i < 6; i++) {               // 两分钟，每 20 秒一条 ping
      clock.now += 20 * 1000;
      ws.onmessage({ data: JSON.stringify({ type: "viewer_ping" }) });
      tick();
    }
    assert.equal(closed, 0, "一直收得到 ping：连接好好的，不断");
    assert.equal(FakeWebSocket.instances.length, 1);
    assert.equal(elements["conn-text"].textContent, "已连接");
    assert.equal(elements["stale-line"].textContent, "120 秒没有新消息", "ping 不是新内容：安静的直播间照样这么写");
    clock.now += 51 * 1000;                     // ping 也不来了
    tick();
    assert.equal(closed, 1);
  });
});

test("viewer_ping 不算回放里的一条：回放照旧按 hello 给的条数结束", () => {
  withFakeViewerPage(({ elements, ws }) => {
    const send = (m) => ws.onmessage({ data: JSON.stringify(m) });
    send(Object.assign({}, HELLO, { replay_len: 3 }));
    send({ type: "status", state: "live" });          // 回放第 1 条
    send({ type: "health", level: "lagging" });       // 第 2 条
    send({ type: "viewer_ping" });                     // 不算
    send({ type: "session_break", ts: 9 });            // 第 3 条：回放里的历史换场，不收此刻的落后提示
    assert.equal(healthShown(elements), "识别开始落后，报警会有延迟");
    send({ type: "session_break", ts: 10 });           // 回放已经结束：这一条是此刻的换场
    assert.equal(healthShown(elements), null);
  });
});

// ---- viewer.css：不再用带 var() 的 color-mix（F8）----
// Safari 15.4–16.1 认 :has 却不认带 var() 的 color-mix：整条声明在计算期失效、不回退，状态胶囊的底色和
// 报警卡片的浅红底都变透明（桌面 d3386e7 已经换过）。现在按主题预先算好，这里核对算得对
const VIEWER_CSS = readFileSync(new URL("../web/viewer.css", import.meta.url), "utf8").replace(/\r\n/g, "\n");
const CSS_NO_COMMENTS = VIEWER_CSS.replace(/\/\*[\s\S]*?\*\//g, "");
function rootTokens(dark) {
  const parse = (body) => Object.fromEntries([...body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
  const light = parse(/:root\s*\{([^}]*)\}/.exec(CSS_NO_COMMENTS)[1]);
  if (!dark) return light;
  return Object.assign(light, parse(/@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/.exec(CSS_NO_COMMENTS)[1]));
}
const rgbOf = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));

test("viewer.css 里没有带 var() 的 color-mix 了", () => {
  assert.doesNotMatch(CSS_NO_COMMENTS, /color-mix\([^;]*var\(/);
});

test("预先算好的半透明色和报警卡片底色：深浅两套都等于原来 color-mix 算出的颜色", () => {
  for (const dark of [false, true]) {
    const t = rootTokens(dark);
    const rgba = (name, a) => "rgba(" + rgbOf(t[name]).join(", ") + ", " + a + ")";
    assert.equal(t["--a-green-16"], rgba("--green", "0.16"), dark ? "深色" : "浅色");
    assert.equal(t["--a-orange-16"], rgba("--orange", "0.16"));
    assert.equal(t["--a-red-14"], rgba("--red", "0.14"));
    assert.equal(t["--a-fail-15"], rgba("--fail", "0.15"));
    const bg = rgbOf(t["--bg"]);
    const mixed = rgbOf(t["--fail"]).map((v, i) => Math.round(v * 0.08 + bg[i] * 0.92));
    assert.equal(t["--tint-fail-8"], "#" + mixed.map((v) => v.toString(16).padStart(2, "0")).join(""));
  }
  assert.match(CSS_NO_COMMENTS, /\.status-pill:has\(\.dot\.connected\)\s*\{[^}]*background:\s*var\(--a-green-16\)/);
  assert.match(CSS_NO_COMMENTS, /\.alert-item\s*\{[^}]*background:\s*var\(--tint-fail-8\)/);
});

// ---- 报警卡片右上角的 ×（顺延事项：图形控件 3:1）----
// .alert-dismiss 以前用 --label-3：浅色卡片底上 1.91:1、深色 2.94:1，不到 WCAG 1.4.11 对图形控件要求的 3:1
// （b2-contrast 第 2 轮审查记下）。现在 --label-2：浅色 4.76:1、深色 6.16:1。半透明的字色先按 alpha 叠到卡片底
// （按主题预先混好的 --tint-fail-8）上再算，算法同 tests/test_contrast.py（那个文件不在本批清单里，这条钉在这里）
function cssRule(selector) {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = new RegExp("(?:^|\\n|\\})\\s*" + esc + "\\s*\\{([^}]*)\\}").exec(CSS_NO_COMMENTS);
  assert.ok(m, "viewer.css 里找不到 " + selector);
  return Object.fromEntries([...m[1].matchAll(/([\w-]+)\s*:\s*([^;]+);?/g)].map((d) => [d[1], d[2].trim()]));
}
function rgbOver(value, bg) {
  const m = /^rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)$/.exec(value);
  if (!m) return rgbOf(value);
  const a = m[4] === undefined ? 1 : Number(m[4]);
  return [1, 2, 3].map((k, i) => Number(m[k]) * a + bg[i] * (1 - a));
}
function contrast(fg, bg) {
  const lum = (rgb) => {
    const [r, g, b] = rgb.map((v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const [hi, lo] = [lum(fg), lum(bg)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

test("报警卡片的 × 是能点的图形控件：深浅两套在卡片底上都到 3:1（以前 --label-3 不到）", () => {
  assert.equal(Math.round(contrast([0, 0, 0], [255, 255, 255]) * 100) / 100, 21);   // 公式本身
  assert.equal(cssRule(".alert-dismiss").color, "var(--label-2)");
  assert.equal(cssRule(".alert-item").background, "var(--tint-fail-8)");
  for (const [dark, want, before] of [[false, 4.76, 1.91], [true, 6.16, 2.94]]) {
    const t = rootTokens(dark);
    const bg = rgbOf(t["--tint-fail-8"]);
    const got = contrast(rgbOver(t["--label-2"], bg), bg);
    assert.ok(got >= 3, (dark ? "深色" : "浅色") + " × 只有 " + got.toFixed(2) + ":1");
    assert.equal(Math.round(got * 100) / 100, want, dark ? "深色" : "浅色");
    assert.equal(Math.round(contrast(rgbOver(t["--label-3"], bg), bg) * 100) / 100, before,
      "审查记下的旧值：--label-3 在卡片底上不到 3:1");
  }
});

// ---- 口令记在这台手机上（专家审查 S4，用户决定 D5）----
// 地址、端口、口令都是虚构的。replaceState 的替身顺手把 location.hash 清掉，和真浏览器一样
function withPhone(opts, run) {
  const { hash = "", host = "192.168.1.23:8766", stored = {}, storage = "map", history = true } = opts || {};
  const elements = baseElements();
  const FakeWebSocket = makeFakeWebSocket();
  const had = Object.prototype.hasOwnProperty.call(global, "history");
  const originals = {
    document: global.document, location: global.location, WebSocket: global.WebSocket,
    localStorage: global.localStorage, setTimeout: global.setTimeout, setInterval: global.setInterval,
    history: global.history,
  };
  const modPath = require.resolve("../web/viewer.js");
  const store = new Map(Object.entries(stored));
  const replaced = [];
  const timeouts = [];
  global.document = makeFakeDocument(elements);
  global.location = { hash, protocol: "http:", host, pathname: "/", search: "" };
  if (history) {
    global.history = { state: null,
                       replaceState: (state, title, url) => { replaced.push(url); global.location.hash = ""; } };
  } else {
    delete global.history;
  }
  global.WebSocket = FakeWebSocket;
  const boom = () => { throw new Error("SecurityError"); };
  global.localStorage = storage === "throw" ? { getItem: boom, setItem: boom, removeItem: boom }
    : storage === "drop" ? { getItem: () => null, setItem() {}, removeItem() {} }     // 写了等于没写
    : { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => { store.set(k, String(v)); },
        removeItem: (k) => { store.delete(k); } };
  global.setTimeout = (fn) => { timeouts.push(fn); return timeouts.length; };
  global.setInterval = () => 0;
  try {
    delete require.cache[modPath];
    require("../web/viewer.js");
    run({ elements, store, replaced, sockets: FakeWebSocket.instances, timeouts });
  } finally {
    global.document = originals.document;
    global.location = originals.location;
    global.WebSocket = originals.WebSocket;
    global.localStorage = originals.localStorage;
    global.setTimeout = originals.setTimeout;
    global.setInterval = originals.setInterval;
    if (had) global.history = originals.history; else delete global.history;
    delete require.cache[modPath];
  }
}
const KEY = "tlt.viewer.k:8766";
function authOf(socket) {
  socket.onopen();
  return JSON.parse(socket.sent[0]);
}

test("tokenStorageKey / pickToken：键带端口；存储里的东西当不可信；没有存储也不抛", () => {
  assert.equal(V.tokenStorageKey("192.168.1.23:8766"), KEY);
  assert.equal(V.tokenStorageKey("example.invalid"), "tlt.viewer.k:");
  assert.deepEqual(V.pickToken("#k=abc", null, KEY), { token: "abc", fromHash: true, saved: false });
  assert.deepEqual(V.pickToken("", null, KEY), { token: null, fromHash: false, saved: false });
  const bad = { getItem: () => "a&b", setItem() {} };
  assert.equal(V.pickToken("", bad, KEY).token, null, "形状不对的不认");
  V.forgetToken(null, KEY);                     // 没有存储：什么都不做，不抛
});

test("扫码进来：口令存进这台手机，存成了就把 #k= 从地址栏去掉，拿它鉴权", () => {
  withPhone({ hash: "#k=tok-scanned" }, ({ store, replaced, sockets }) => {
    assert.equal(store.get(KEY), "tok-scanned");
    assert.deepEqual(replaced, ["/"], "地址栏只留页面本身");
    assert.deepEqual(authOf(sockets[0]), { type: "auth", k: "tok-scanned" });
  });
});

test("刷新（地址栏里已经没有口令）：从这台手机的存储里取，照常连上", () => {
  withPhone({ hash: "", stored: { [KEY]: "tok-scanned" } }, ({ elements, replaced, sockets }) => {
    assert.deepEqual(replaced, [], "没有可去掉的");
    assert.deepEqual(authOf(sockets[0]), { type: "auth", k: "tok-scanned" });
    sockets[0].onmessage({ data: helloMsg(0) });
    assert.equal(elements["conn-text"].textContent, "已连接");
  });
});

test("再扫一次新的二维码：地址栏里的口令盖掉存着的旧口令", () => {
  withPhone({ hash: "#k=tok-new", stored: { [KEY]: "tok-old" } }, ({ store, sockets }) => {
    assert.equal(store.get(KEY), "tok-new");
    assert.deepEqual(authOf(sockets[0]), { type: "auth", k: "tok-new" });
  });
});

test("被拒（4401）：清掉存着的口令，显示链接已失效，不再重连", () => {
  withPhone({ hash: "", stored: { [KEY]: "tok-rotated-away" } }, ({ elements, store, sockets, timeouts }) => {
    authOf(sockets[0]);
    sockets[0].onclose({ code: 4401 });
    assert.equal(store.has(KEY), false, "作废的口令不留在手机上");
    assert.equal(elements["conn-text"].textContent, "链接已失效，请向中控要新的二维码");
    assert.equal(timeouts.length, 0, "不排重连");
  });
});

test("存储用不了（抛错）：退回只用地址栏里的口令，地址栏不清——清了刷新就连不上", () => {
  withPhone({ hash: "#k=tok-scanned", storage: "throw" }, ({ replaced, sockets }) => {
    assert.deepEqual(replaced, []);
    assert.deepEqual(authOf(sockets[0]), { type: "auth", k: "tok-scanned" });
    sockets[0].onclose({ code: 4401 });         // 清存储也抛错：不影响走到「链接已失效」
  });
});

test("存储写了读不回来（被静默丢掉）：地址栏同样不清", () => {
  withPhone({ hash: "#k=tok-scanned", storage: "drop" }, ({ replaced, sockets }) => {
    assert.deepEqual(replaced, []);
    assert.deepEqual(authOf(sockets[0]), { type: "auth", k: "tok-scanned" });
  });
});

test("没有 history 接口：口令照旧留在地址栏，照常连上", () => {
  withPhone({ hash: "#k=tok-scanned", history: false }, ({ store, sockets }) => {
    assert.equal(store.get(KEY), "tok-scanned");
    assert.deepEqual(authOf(sockets[0]), { type: "auth", k: "tok-scanned" });
  });
});

test("地址栏和存储里都没有（换了地址或端口，或主屏幕图标读不到 Safari 存的那份）：不发起连接，直接说链接已失效", () => {
  withPhone({ hash: "", stored: { "tlt.viewer.k:8767": "tok-other-port" } }, ({ elements, sockets }) => {
    assert.equal(sockets.length, 0);
    assert.equal(elements["conn-text"].textContent, "链接已失效，请向中控要新的二维码");
  });
});

// ---- 持续提示里的换行（顺延事项：手机上换行被折成空格）----
// 「没有读到 TikTok 登录」那条带完全磁盘访问权限那几步时分了行（路径单独一行，app/browser_login.py）。
// 桌面照着断行（style.css .incident），手机以前没有 white-space: pre-line，整条挤成一段话
test("手机的持续提示照文字里的换行断行：样式是 pre-line，文字原样放进去（不拼成一行）", () => {
  assert.match(CSS_NO_COMMENTS, /\.incident-list \.incident\s*\{[^}]*white-space:\s*pre-line/);
  withFakeViewerPage(({ elements, ws }) => {
    const text = "程序没有读到浏览器里的 TikTok 登录。\n「…」\n然后完全退出程序再打开。";
    ws.onmessage({ data: JSON.stringify({ type: "incident", id: "login", level: "warn", text: text, ts: 1 }) });
    assert.equal(elements["incident-list"].children[0].textContent, text);
  });
});
