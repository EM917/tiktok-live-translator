globalThis.UI_LANG = "en";   // 必须在 require 之前：web/i18n.js 加载时读一次（node --test 每个文件一个进程）
// 闪购浮层顶部那一排（开始、停止、继续）、指令结果、开始后的过渡、不续开原因的英文（web/flash.js）。
// 中文断言和规则本身在 tests/flash-control.test.mjs。术语照 docs/i18n-style.md §1.3（Start / Stop / Resume、
// Click Again to Start、control key、not renewed、#12）和 §3 K #167–#175：按钮 Title Case，状态语 sentence case，
// 整句带句号，数字和单位照 §2.3（2 sec、30 sec）。每一句都不许剩中文、不许有没填的占位符、不许出现 EN_LABELS 的标签词。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { SAMPLE, SHARED, NOW, makeFlashDoc, makeEls, product, st } from "./flash-fixtures.mjs";

const require = createRequire(import.meta.url);
const F = require("../web/flash.js");

const CJK = /[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;
// 同 tests/i18n_rules.py 的 EN_LABELS（全仓英文都查）
const EN_LABELS = /\b(age|aged|age[- ]restrict\w*|rate[- ]?limit\w*|throttl\w*|ban(s|ned)?\b(?![- ]terms?)|block(ed|ing|s)?|restrict\w*)\b/i;
// 同 EN_CAUSAL：flash.js 不在第八条家族里，但结果的话一样只说看到的
const EN_CAUSAL = /\b(because|due to|caused by|the reason|probably|likely|must be|seems to|maybe|perhaps)\b/i;
function english(text) {
  assert.equal(typeof text, "string");
  assert.ok(!CJK.test(text), "英文里还有中文：" + text);
  assert.ok(!/\{\w*\}/.test(text), "有没填的占位符：" + text);
  assert.ok(!EN_LABELS.test(text), "标签词：" + text);
  assert.ok(!EN_CAUSAL.test(text), "猜了原因：" + text);
  return text;
}
const clone = (v) => JSON.parse(JSON.stringify(v));
const calm = (extra = {}) => st(Object.assign({ attention: [], app_version: "0.9.0",
  products: SAMPLE.products.map((p) => Object.assign({}, p, { stuck: false })) }, extra));
const idle = (extra = {}) => calm(Object.assign({ running: false, paused_reason: "not_started" }, extra));
const result = (error, action = "start") => F.flashResultLine({ type: "flash_control_result", action, ok: false, error });
const STARTING = "Starting… (Flash Sale Helper is reloading the page)";

test("语言确实是英文（不然下面全是空转）", () => {
  assert.equal(require("../web/i18n.js").UI_LANG, "en");
});

test("按钮：Start / Stop / Resume / Click Again to Start，在途、等生效 Sending… / Starting… / Stopping… / Resuming…", () => {
  assert.equal(F.flashControlView(idle(), {}).start.text, "Start");
  assert.equal(F.flashControlView(idle(), { armed: true }).start.text, "Click Again to Start");
  const paused = F.flashControlView(calm({ paused_reason: "user_interaction" }), {});
  assert.deepEqual([paused.stop.text, paused.resume.text], ["Stop", "Resume"]);
  assert.equal(F.flashControlView(calm(), { pending: "stop" }).stop.text, "Sending…");
  assert.equal(F.flashControlView(idle({ control: { action: "start", phase: "waiting" } }), {}).start.text, "Starting…");
  assert.equal(F.flashControlView(calm({ control: { action: "stop", phase: "waiting" } }), {}).stop.text, "Stopping…");
  assert.equal(F.flashControlView(calm({ paused_reason: "user_interaction", control: { action: "resume", phase: "waiting" } }), {})
    .resume.text, "Resuming…");
});

test("按钮旁边那句：版本门槛、页面没准备好、确认框弹着", () => {
  assert.equal(english(F.flashControlView(idle({ app_version: "0.7.3" }), {}).note.text),
               "Start and Stop need Flash Sale Helper 0.8.0 or later.");
  // 页面没准备好：照 /status 的 page 那句（decisions-step2），认不出的代码原样，连 page 都没有时才是通用的那句
  assert.equal(english(F.flashControlView(idle({ page: "login_required", paused_reason: null }), {}).note.text),
               "The dedicated Chrome is on the TikTok sign-in page. Sign in to continue.");
  assert.equal(english(F.flashControlView(idle({ page: "loading", paused_reason: null }), {}).note.text),
               "LIVE Manager is loading…");
  assert.deepEqual(F.flashControlView(idle({ page: "maintenance", paused_reason: null }), {}).note,
                   { text: "Page status: ", code: "maintenance" });
  assert.equal(english(F.flashControlView(idle({ page: null, paused_reason: null }), {}).note.text),
               "Start and Resume work here once the page is ready.");
  assert.equal(english(F.flashControlView(calm({ paused_reason: "awaiting_approval" }), {}).note.text),
               "Stop works here once the confirmation is handled.");
});

test("没找到闪购助手装在哪（app_found=false）：旁边那句说拖进 Applications、再开一次浮层", () => {
  const v = F.flashControlView({ conn: "unreachable", stale: false, products: [], alerts: [], attention: [], app_found: false }, {});
  assert.equal(v.open.shown, false);
  assert.equal(english(v.note.text),
               "Couldn’t find where Flash Sale Helper is installed, so it can’t be opened from here. "
               + "Drag it to Applications, then close and reopen this panel to get Open Flash Sale Helper.");
});

test("浮层最底下那句：有开始、停止、继续时一句，一颗都没有时换回第一步那句", () => {
  const doc = makeFlashDoc();
  const els = {};
  for (const k of ["start", "stop", "resume", "note", "desc"]) els[k] = doc.createElement("p");
  F.renderFlashControls(doc, els, F.flashControlView(calm(), {}));
  assert.equal(els.desc.textContent, "Only Start, Stop and Resume work here. Do everything else in Flash Sale Helper.");
  F.renderFlashControls(doc, els, F.flashControlView({ conn: "unreachable" }, {}));
  assert.equal(els.desc.textContent, "This panel only shows status. Make changes in Flash Sale Helper.");
});

test("本程序这边的代码：Not sent / Sent, but… / no control interface / this app hit an error", () => {
  const want = {
    no_status: "Not sent: Flash Sale Helper’s status didn’t come through just now.",
    no_token: "Not sent: Couldn’t read Flash Sale Helper’s control key. Make sure it’s 0.8.0 or later, reopen it, then try again.",
    unreachable: "Not sent: Couldn’t connect to 127.0.0.1:9334.",
    no_answer: "Sent, but 127.0.0.1:9334 didn’t answer within 2 sec. Check whether the status changes.",
    bad_reply: "Sent, but the reply couldn’t be read. Check whether the status changes.",
    no_control: "This version of Flash Sale Helper has no control interface.",
    failed: "Couldn’t handle the command: this app hit an error. Details are in the terminal.",
    local_only: "Not sent: A local script can only stop Flash Sale Helper. Start and Resume have to be clicked in this app.",
    not_confirmed: "Not sent: Start needs two clicks, the second 0.4 to 6 sec after the first. Click twice again.",
  };
  for (const [code, text] of Object.entries(want)) assert.equal(english(result(code).text), text, code);
  assert.equal(result("no_token").text.includes("token"), false, "术语表：control key，不写 token");
});

test("等满 30 秒没看到生效：只说状态里没显示，不说没执行", () => {
  assert.equal(english(result("not_seen", "start").text), "Flash Sale Helper didn’t show as running within 30 sec.");
  assert.equal(english(result("not_seen", "stop").text), "Flash Sale Helper didn’t show as stopped within 30 sec.");
  assert.equal(english(result("not_seen", "resume").text), "Flash Sale Helper didn’t show as resumed within 30 sec.");
});

test("闪购助手 409 的代码：Flash Sale Helper declined: 加契约里的意思；认不出的原样；别的 HTTP 状态", () => {
  const want = {
    no_page: "LIVE Manager isn’t open in the dedicated Chrome.",
    login_required: "The dedicated Chrome is on the TikTok sign-in page. Sign in to continue.",
    captcha: "A CAPTCHA appeared on the page. Someone needs to solve it.",
    awaiting_approval: "A confirmation is waiting in the dedicated Chrome. Check it, then confirm or cancel.",
    loading: "The page is loading. Try again in a moment.",
    already_running: "It’s already running.",
    paused: "It’s paused. Use Resume, not Start.",
    not_running: "It hasn’t started yet.",
    not_paused: "It isn’t paused.",
    busy: "It’s busy. Try again in a moment.",
    stop_pending: "A Stop is still queued. Start again once it reaches the page.",
    superseded: "Stop wasn’t carried out: Start was pressed on the page after it.",
    no_response: "The page didn’t respond. This command won’t be carried out later.",
    error: "The page reported an error.",
  };
  for (const [code, text] of Object.entries(want)) {
    assert.equal(english(result(code).text), "Flash Sale Helper declined: " + text, code);
  }
  assert.deepEqual(result("cooldown_x"), { text: "Flash Sale Helper declined: ", code: "cooldown_x" });
  assert.equal(english(result("http_403").text), "Not accepted: 127.0.0.1:9334 replied with HTTP 403.");
});

test("停止再试：Retrying… 和旁边那句；拒在确认框、验证码上说去哪儿点；再试过的句子前面加 Tried n times.", () => {
  const retrying = { action: "stop", phase: "retrying" };
  const v = F.flashControlView(calm({ control: retrying }), {});
  assert.equal(v.stop.text, "Retrying…");
  assert.equal(english(v.note.text), "Stop hasn’t been accepted yet. Trying again every 3 sec for up to 30 sec.");
  assert.equal(english(F.flashControlView({ conn: "unreachable", control: retrying }, {}).note.text), v.note.text);
  assert.equal(english(result("awaiting_approval", "stop").text),
               "Flash Sale Helper declined Stop: A confirmation is open in the dedicated Chrome. Confirm or cancel it there, then click Stop again.");
  assert.equal(english(result("captcha", "stop").text),
               "Flash Sale Helper declined Stop: There’s a CAPTCHA on the page in the dedicated Chrome. Solve it there, then click Stop again.");
  const tried = (error, tries) => F.flashResultLine({ type: "flash_control_result", action: "stop", ok: false, error, tries });
  assert.equal(english(tried("unreachable", 11).text), "Tried 11 times. Not sent: Couldn’t connect to 127.0.0.1:9334.");
  assert.equal(english(tried("busy", 2).text), "Tried 2 times. Flash Sale Helper declined: It’s busy. Try again in a moment.");
});

test("发布闸关着：最底下那句 Only Stop works here.、版本那句只说 Stop；主句照旧叫人去面板上点 Resume", () => {
  const doc = makeFlashDoc();
  const els = {};
  for (const k of ["start", "stop", "resume", "note", "desc"]) els[k] = doc.createElement("p");
  F.renderFlashControls(doc, els, F.flashControlView(calm(), { gated: true }));
  assert.equal(english(els.desc.textContent), "Only Stop works here. Do everything else in Flash Sale Helper.");
  assert.equal(english(F.flashControlView(idle({ app_version: "0.7.3" }), { gated: true }).note.text),
               "Stop needs Flash Sale Helper 0.8.0 or later.");
  const paused = calm({ paused_reason: "user_interaction", attention: ["paused"] });
  assert.equal(english(F.flashLines(paused, 0, { gated: true })[0].text),
               "Paused: a confirmation was canceled. Resume it from the Flash Sale Helper panel.");
});

test("开始后的过渡：Starting… (Flash Sale Helper is reloading the page)；继续是 Resuming…；和后端同一张表", () => {
  assert.equal(F.flashStartingText("start"), STARTING);
  assert.equal(F.flashStartingText("resume"), "Resuming…");
  for (const c of SHARED.attention) {
    const state = Object.assign(clone(c.state), { attention: c.attention, alerts: [] });
    assert.equal(F.flashNeedsAttention(state, 0), c.attention.length > 0, c.why);
    for (const l of F.flashLines(state, 0)) english(F.flashLineText(l));
  }
  const s = calm({ page: "error", stale: true, running: false, paused_reason: null, products: [], attention: [],
                   control: { action: "start", phase: "waiting" } });
  assert.deepEqual(F.flashLines(s, 0).map(F.flashLineText), [STARTING]);
  const button = F.flashButtonState(s, 0);
  assert.equal(button.attn, false, "按钮不变色");
  assert.equal(button.title, "Flash Sale Helper · Starting…", "不写 Not started：读起来像没开起来");
  assert.deepEqual(F.flashChip(s, 0), { tone: "idle", text: "Starting…" });
  assert.deepEqual(F.flashChip(Object.assign(clone(s), { control: { action: "resume", phase: "waiting" } }), 0),
                   { tone: "idle", text: "Resuming…" });
});

test("确认框里点了取消的暂停：浮层顶部有 Resume 时主句只说暂停了；只剩 Open 时照旧指到闪购助手的面板", () => {
  const paused = calm({ paused_reason: "user_interaction", attention: ["paused"] });
  assert.deepEqual(F.flashLines(paused, 0).map(F.flashLineText).map(english), ["Paused: a confirmation was canceled."]);
  const old = calm({ paused_reason: "user_interaction", attention: ["paused"], app_version: "0.7.3" });
  assert.deepEqual(F.flashLines(old, 0).map(F.flashLineText).map(english),
                   ["Paused: a confirmation was canceled. Resume it from the Flash Sale Helper panel."]);
});

test("不续开原因和卡片序号", () => {
  const want = { slot_1: "Slot 1, not renewed", top_n: "Top of the list, not renewed", next_to_pin: "Next to pin, not renewed",
                 no_stock: "Out of stock, not renewed",
                 max_renewals: "Renewal limit reached", auto_start_off: "Auto-start off",
                 no_position: "Position unread, skipped for now" };
  for (const [code, text] of Object.entries(want)) assert.equal(english(F.flashSkipText(code)), text, code);
  const parts = F.flashProductParts(product("pending", { skip: "brand_new_reason", position: 4 }), { running: true }, NOW);
  assert.deepEqual(parts.detail, [{ text: "Not renewed: ", code: "brand_new_reason" }]);
  assert.equal(parts.position, 4);
});

test("整张浮层和那一排画出来的字：除了数据区（标题、认不出的代码在 translate=no 里，记录正文 i18n-data）一个中文都没有", () => {
  const doc = makeFlashDoc();
  const walk = (el) => {
    if (el.getAttribute && (el.getAttribute("translate") === "no" || el.classList.contains("i18n-data"))) return;
    for (const k of ["title", "aria-label"]) {
      const v = el.getAttribute && el.getAttribute(k);
      if (v != null && !(k === "title" && el.classList.contains("flash-title"))) english(v);
    }
    for (const n of el.childNodes) {
      if (n.nodeType === 3) english(n.nodeValue);
      else walk(n);
    }
  };
  const skips = ["slot_1", "top_n", "next_to_pin", "no_stock", "max_renewals", "auto_start_off", "no_position",
                 "brand_new_reason", null];
  const states = [
    calm({ products: skips.map((skip, i) => product(["stopped", "pending", "live"][i % 3], { skip, position: i + 1 })) }),
    idle({ page: "error", stale: true, paused_reason: null, control: { action: "start", phase: "waiting" } }),
    calm({ paused_reason: "user_interaction", page: "loading", control: { action: "resume", phase: "waiting" } }),
    calm({ paused_reason: "awaiting_approval", attention: ["paused"] }), idle({ app_version: "0.7.3" }),
    idle({ page: "no_page", paused_reason: null }), { conn: "unreachable", products: [], alerts: [] },
  ];
  for (const state of states) {
    for (const ui of [{}, { armed: true }, { pending: "start" }, { pending: "stop" }, { offline: true }]) {
      const els = makeEls(doc);
      F.renderFlashPanel(doc, els, state, { now: NOW, unread: F.flashUnread(state.alerts, null) });
      Object.values(els).forEach(walk);
      const ctl = {};
      for (const k of ["start", "stop", "resume", "note", "desc"]) ctl[k] = doc.createElement("p");
      F.renderFlashControls(doc, ctl, F.flashControlView(state, ui));
      Object.values(ctl).forEach(walk);
    }
  }
  const note = doc.createElement("p");
  for (const code of ["no_status", "no_token", "unreachable", "no_answer", "bad_reply", "no_control", "failed", "not_seen",
                      "busy", "paused", "http_500", "cooldown_x"]) {
    F.renderFlashNote(doc, note, result(code));
    walk(note);
  }
});
