globalThis.UI_LANG = "en";   // 必须在 require 之前：web/i18n.js 加载时读一次（node --test 每个文件一个进程）
// 顶栏「闪购」浮层的英文（web/flash.js）。中文断言在 tests/flash.test.mjs。
// 术语照 docs/i18n-style.md §1.3（Flash Sale、Flash Sale Helper、LIVE Manager、dedicated Chrome、CAPTCHA、
// sign in、renew、round）；状态写 sentence case，按钮和小标题 Title Case；数字和单位照 §2.3（15 sec、1 min）；
// 带数的句子用 LN 分单复数。每一句都不许剩中文、不许有没填的占位符，也不许出现 EN_LABELS 的标签词。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { SAMPLE, NOW, makeFlashDoc, makeEls, product, st, AUTO_RENEW_OFF, autoRenewProduct } from "./flash-fixtures.mjs";

const require = createRequire(import.meta.url);
const F = require("../web/flash.js");

const CJK = /[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;
// 同 tests/i18n_rules.py 的 EN_LABELS（全仓英文都查）
const EN_LABELS = /\b(age|aged|age[- ]restrict\w*|rate[- ]?limit\w*|throttl\w*|ban(s|ned)?\b(?![- ]terms?)|block(ed|ing|s)?|restrict\w*)\b/i;
function english(text) {
  assert.equal(typeof text, "string");
  assert.ok(!CJK.test(text), "英文里还有中文：" + text);
  assert.ok(!/\{\w*\}/.test(text), "有没填的占位符：" + text);
  assert.ok(!EN_LABELS.test(text), "标签词：" + text);
  return text;
}
const calm = (extra = {}) => st(Object.assign({ attention: [], products: SAMPLE.products.map((p) => Object.assign({}, p, { stuck: false })) },
                                              extra));
const lines = (state, unread = 0) => F.flashLines(state, unread).map(F.flashLineText);

test("语言确实是英文（不然下面全是空转）", () => {
  assert.equal(require("../web/i18n.js").UI_LANG, "en");
});

test("页面状态、暂停原因、商品状态的英文", () => {
  assert.equal(F.flashPageText("no_page"), "LIVE Manager isn’t open in the dedicated Chrome.");
  assert.equal(F.flashPageText("login_required"), "The dedicated Chrome is on the TikTok sign-in page. Sign in to continue.");
  assert.equal(F.flashPageText("loading"), "LIVE Manager is loading…");
  assert.equal(F.flashPageText("no_list"), "LIVE Manager isn’t showing the Buy now list.");
  assert.equal(F.flashPageText("captcha"), "A CAPTCHA appeared on the page. Someone needs to solve it.");
  assert.equal(F.flashPageText("error"), "The page isn’t responding, or reading it failed.");
  assert.equal(F.flashPausedText("not_started"), "Flash Sale Helper is open but hasn’t started.");
  assert.equal(F.flashPausedText("awaiting_approval"),
               "A confirmation is waiting in the dedicated Chrome. Check it, then confirm or cancel.");
  assert.equal(F.flashPausedText("user_interaction"),
               "Paused: a confirmation was canceled. Resume it from the Flash Sale Helper panel.");
  assert.equal(F.flashPausedText("captcha"), "Paused for a CAPTCHA.");
  assert.equal(F.flashPausedText("error"), "Paused after too many failures in a row.");
  const states = { live: "Ongoing", between_rounds: "Between rounds", stopped: "Stopped", ended_by_user: "Ended manually",
                   pending: "Ready to start", unknown: "Unclear for now", no_sale: "No flash sale" };
  for (const [code, text] of Object.entries(states)) assert.equal(F.flashStateText(code), text);
  for (const code of ["no_page", "login_required", "loading", "no_list", "captcha", "error"]) english(F.flashPageText(code));
  for (const code of ["not_started", "awaiting_approval", "user_interaction", "captcha", "error"]) english(F.flashPausedText(code));
  assert.deepEqual(["error", "warn", "info"].map((l) => F.flashLevel(l).label), ["Needs attention", "Warning", "Info"]);
});

test("胶囊和按钮：Running / Needs attention / Not started / Not running / Checking…；按钮名 Flash Sale Helper", () => {
  assert.equal(F.flashButtonState(null, 0).title, "Flash Sale Helper");
  assert.equal(F.flashButtonState(calm(), 0).title, "Flash Sale Helper · Running");
  assert.equal(F.flashButtonState(st(), 0).title, "Flash Sale Helper · Needs attention");
  assert.equal(F.flashButtonState(calm({ running: false, paused_reason: "not_started" }), 0).title, "Flash Sale Helper · Not started");
  assert.equal(F.flashButtonState({ conn: "unreachable" }, 0).title, "Flash Sale Helper · Not running");
  assert.equal(F.flashButtonState({ conn: "checking" }, 0).title, "Flash Sale Helper · Checking…");
});

test("主句：连不上、读不出、陈旧（带本机时刻）、卡住和未读分单复数、认不出的代码原样", () => {
  assert.deepEqual(lines({ conn: "checking" }), ["Checking Flash Sale Helper…"]);
  assert.deepEqual(lines({ conn: "unreachable" }),
                   ["Flash Sale Helper isn’t running.", "Can’t connect to 127.0.0.1:9334. Checking again every 15 sec."]);
  assert.deepEqual(lines({ conn: "bad_response" }), ["Couldn’t read the response from 127.0.0.1:9334."]);
  assert.deepEqual(lines({ conn: "bad_response", why: "no_answer" }), ["127.0.0.1:9334 didn’t answer within 2 sec."]);
  const at = new Date(2026, 9, 27, 21, 36, 7).getTime();
  assert.deepEqual(lines(calm({ stale: true, updated_ms: at, attention: ["stale"] })),
                   ["Flash Sale Helper hasn’t updated its status.", "Last updated at 21:36:07."]);
  assert.deepEqual(lines(calm({ stale: true, updated_ms: null, attention: ["stale"] })),
                   ["Flash Sale Helper hasn’t updated its status.", "No status has come in yet."]);
  assert.deepEqual(lines(st()), ["1 product has stayed stopped for over 1 min."]);
  const two = st({ products: SAMPLE.products.map((p) => Object.assign({}, p, { stuck: p.state === "stopped" || p.state === "pending" })) });
  assert.deepEqual(lines(two), ["2 products have stayed stopped for over 1 min."]);
  assert.deepEqual(lines(calm({ attention: ["stuck"] })), ["A product has stayed stopped for over 1 min."]);
  assert.deepEqual(lines(calm(), 1), ["1 new problem in Recent Activity."]);
  assert.deepEqual(lines(calm(), 3), ["3 new problems in Recent Activity."]);
  assert.deepEqual(lines(calm({ attention: ["mystery"] })), ["Needs attention: mystery"]);
  assert.deepEqual(lines(calm({ page: "maintenance", running: false, paused_reason: null })), ["Page status: maintenance"]);
  assert.deepEqual(lines(calm({ paused_reason: "cooldown", attention: ["paused"] })), ["Paused: cooldown"]);
  assert.deepEqual(lines(calm({ products: [], alerts: [] })), ["No products in the list yet."]);
});

test("Auto-renew off：checked 为准，没有或读不出才看 enabled；中文那边同一张表", () => {
  assert.ok(AUTO_RENEW_OFF.length >= 9);
  for (const c of AUTO_RENEW_OFF) {
    const p = autoRenewProduct(c.fields);
    assert.equal(F.flashAutoRenewOff(p), c.off, c.why);
    assert.deepEqual(F.flashProductParts(p, {}, NOW).detail, c.off ? ["Auto-renew off"] : [], c.why);
  }
});

test("checked 不碰卡住和等续开：英文同样只认 enabled", () => {
  const running = { running: true, paused_reason: null };
  const quiet = product("stopped", { checked: true, enabled: false });
  assert.equal(F.flashWaitingRenewal(quiet, running), false);
  assert.deepEqual(F.flashProductParts(quiet, running, NOW).detail, []);
  const odd = product("stopped", { checked: false, enabled: true });
  assert.deepEqual(F.flashProductParts(odd, running, NOW).detail, ["Waiting for Flash Sale Helper to renew", "Auto-renew off"]);
});

test("商品行和摘要：倒计时、轮次、库存、续开次数分单复数", () => {
  assert.equal(F.flashCountdownText("ends", NOW + 261000, NOW), "4:21 left");
  assert.equal(F.flashCountdownText("next", NOW + 61000, NOW), "Next round in 1:01");
  assert.deepEqual(F.flashProductParts(SAMPLE.products[2], SAMPLE, NOW).detail,
                   ["Stopped for over 1 min", "Round 2", "30 of 30 in stock", "Renewed 3 times"]);
  assert.deepEqual(F.flashProductParts(product("stopped", { renewals: 1, stock_left: 4 }), { running: true }, NOW).detail,
                   ["Waiting for Flash Sale Helper to renew", "4 in stock", "Renewed once"]);
  assert.deepEqual(F.flashProductParts(product("stopped", { enabled: false }), {}, NOW).detail, ["Auto-renew off"]);
  assert.equal(F.flashSummary(SAMPLE.products),
               "9 products: 1 stopped · 1 ready to start · 1 unclear · 3 ongoing · 1 between rounds · " +
               "1 with no flash sale · 1 ended manually");
  assert.equal(F.flashSummary([product("live")]), "1 product: 1 ongoing");
  // 每段一个 .flash-summary-part：440px 的浮层放不下时不把 "1 between rounds" 从中间折开（截图 2026-10-28）
  assert.deepEqual(F.flashSummaryParts(SAMPLE.products), { lead: "9 products: ", parts: ["1 stopped", "1 ready to start",
    "1 unclear", "3 ongoing", "1 between rounds", "1 with no flash sale", "1 ended manually"] });
  assert.deepEqual(F.flashSummaryParts([product("live")]), { lead: "1 product: ", parts: ["1 ongoing"] });
  const sdoc = makeFlashDoc();
  const sels = makeEls(sdoc);
  F.renderFlashPanel(sdoc, sels, SAMPLE, { now: NOW });
  assert.deepEqual(sels.summary.children.map((el) => el.textContent),
                   ["9 products: 1 stopped", "1 ready to start", "1 unclear", "3 ongoing", "1 between rounds",
                    "1 with no flash sale", "1 ended manually"]);
  assert.equal(sels.summary.textContent, F.flashSummary(SAMPLE.products));
  assert.equal(F.flashSummary([product("live"), product("odd")]), "2 products: 1 ongoing · 1 other");
  assert.equal(F.flashSummary([product("live"), product("stopped")], 310), "310 products: 1 stopped · 1 ongoing · 308 not listed");
  assert.equal(F.flashMoreText(1), "1 more");
  assert.equal(F.flashMoreText(260), "260 more");
});

test("整张浮层画出来的字：除了数据区（标题 translate=no、记录正文 i18n-data）一个中文都没有", () => {
  const doc = makeFlashDoc();
  const states = [
    st(), calm({ stale: true, updated_ms: NOW - 20000, attention: ["stale", "zzz"], page: "error", running: false, paused_reason: null }),
    calm({ paused_reason: "user_interaction", attention: ["paused"] }), calm({ paused_reason: "not_started", running: false }),
    { conn: "unreachable", products: [], alerts: [] }, { conn: "bad_response", attention: ["bad_response"] }, { conn: "checking" },
    { conn: "bad_response", why: "no_answer", attention: ["bad_response"] },
    calm({ products: Array.from({ length: 41 }, (_, i) => product(["live", "stopped", "pending", "odd"][i % 4], { renewals: i % 3 })) }),
  ];
  for (const state of states) {
    const els = makeEls(doc);
    F.renderFlashPanel(doc, els, state, { now: NOW, unread: F.flashUnread(state.alerts, null) });
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
    Object.values(els).forEach(walk);
  }
});
