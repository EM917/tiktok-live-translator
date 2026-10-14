globalThis.UI_LANG = "en";   // 必须在 require 之前：web/i18n.js 加载时读一次（node --test 每个文件一个进程）
// 弹幕留意前端的英文（web/guard.js，spec §12.1 G6）。中文断言在 tests/guard.test.mjs。
// 术语照 docs/i18n-style.md §1.6：Comment Review、comments to review、unclear、Marked Viewers、Mark / Unmark、
// “Model: …”、Possible …。复数照 §2.3 的表：n=0/1/2 都跑一遍，1 用单数。
// 每一句英文都过三道筛：没有汉字、没有没填的占位符、没有第八条的两级禁用词（EN_LABELS / EN_CAUSAL，
// 与 tests/i18n_rules.py 同一份正则；guard.js 整个文件在第八条家族里）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { CJK, makeDoc, makeItem, uiTexts } from "./guard-dom.mjs";
import { ALL_CATS, AMBER_CODES, RV_CODES, STATUS_CODES, STATE, UID_A, comment, RED, AMBER, entry, event }
  from "./guard-fixtures.mjs";

const require = createRequire(import.meta.url);
const G = require("../web/guard.js");

const EN_LABELS = /\b(age|aged|age[- ]restrict\w*|rate[- ]?limit\w*|throttl\w*|ban(s|ned)?\b(?![- ]terms?)|block(ed|ing|s)?|restrict\w*)\b/i;
const EN_CAUSAL = /\b(because|due to|caused by|the reason|probably|likely|must be|seems to|private|regions?|regional|maybe|perhaps|(?:not|isn[’']t) an? [\w -]*(?:problem|issue))\b/i;

function english(text) {
  assert.equal(typeof text, "string");
  assert.ok(!CJK.test(text), "英文里还有中文：" + text);
  assert.ok(!/\{\w*\}/.test(text), "有没填的占位符：" + text);
  assert.ok(!EN_LABELS.test(text), "英文里有标签词：" + text);
  assert.ok(!EN_CAUSAL.test(text), "英文里有因果词：" + text);
  return text;
}

test("语言确实是英文（不然下面全是空转）", () => {
  assert.equal(require("../web/i18n.js").UI_LANG, "en");
});

test("类别名：全部 “Possible …”，只有 flood_brigade 写观察", () => {
  for (const c of ALL_CATS) {
    const t = english(G.guardCatLabel(c));
    if (c === "flood_brigade") assert.equal(t, "Same comment from several accounts"); else assert.match(t, /^Possible /, c);
  }
  assert.equal(G.guardCatLabel("insult"), "Possible insult");
  assert.equal(G.guardCatLabel("threat"), "Possible threat of harm");
  assert.equal(G.guardCatLabel("sexual"), "Possible sexual remark");
  assert.equal(G.guardCatLabel("accuse_claim"), "Possible claim of fakes or scams");
  assert.equal(G.guardCatLabel("accuse_call"), "Possible call to report or not buy");
  assert.equal(G.guardCatLabel("accuse_report"), "Possible threat to report");
  assert.equal(G.guardCatLabel("flood_user"), "Possible flooding");
  assert.equal(G.guardCatLabel("flood_repeat"), "Possible repeated comment");
  assert.equal(G.guardCatLabel("promo_link"), "Possible redirect (link)");
  assert.equal(G.guardCatLabel("promo_contact"), "Possible redirect (contact info)");
  assert.equal(G.guardCatLabel("promo_pull"), "Possible redirect to another stream or shop");
  assert.equal(G.guardCatLabel("promo_scam"), "Possible impersonation or scam offer");
  assert.equal(G.guardCatLabel("brand_new"), "To review");
});

test("琥珀理由：只写观察；多账号同句 1 个账号用单数", () => {
  assert.equal(G.guardAmberText("maybe_question"), "Short, with no question mark");
  assert.equal(G.guardAmberText("grievance"), "Describes the viewer’s own experience");
  assert.equal(G.guardAmberText("quote"), "Reads like a quote");
  assert.equal(G.guardAmberText("accumulated"), "Second time from this viewer in 10 min");
  assert.equal(G.guardAmberText("question_link"), "Link inside a question");
  assert.equal(G.guardAmberText("pending"), "Matched an entry that isn’t confirmed yet");
  assert.equal(G.guardAmberText("self"), "Written in the first person");
  assert.equal(G.guardAmberText("review"), "Flagged by the model");
  assert.equal(G.guardAmberText("brigade", { n: 5, window_sec: 20 }), "Same comment from 5 accounts in 20 seconds");
  assert.equal(G.guardAmberText("brigade", { n: 1, window_sec: 20 }), "Same comment from 1 account in 20 seconds");
  for (const code of AMBER_CODES) english(G.guardAmberText(code, { n: 3, window_sec: 20 }));
});

test("模型意见：英文写 “Model: …”，与功能状态的 “Comment Review: …” 分开", () => {
  assert.equal(G.guardReviewText("pending"), "Model reviewing…");
  assert.equal(G.guardReviewText("agree"), "Model: agrees with rules");
  assert.equal(G.guardReviewText("question"), "Model: question");
  assert.equal(G.guardReviewText("complaint"), "Model: complaint");
  assert.equal(G.guardReviewText("friendly"), "Model: friendly");
  assert.equal(G.guardReviewText("neutral"), "Model: no attack found");
  assert.equal(G.guardReviewText("unsure"), "Model: unsure");
  assert.equal(G.guardReviewText("skipped"), "Not reviewed by the model");
  for (const rv of RV_CODES) assert.ok(!/^Comment Review/.test(english(G.guardReviewText(rv))), rv);
});

test("事件计数：1 条用单数", () => {
  assert.equal(G.guardCountText({ kind: "flood", n: 1, window_sec: 10 }), "1 comment in 10 seconds");
  assert.equal(G.guardCountText({ kind: "flood", n: 6, window_sec: 10 }), "6 comments in 10 seconds");
  assert.equal(G.guardCountText({ kind: "comment", n: 1 }), "1 comment");
  assert.equal(G.guardCountText({ kind: "comment", n: 3 }), "3 comments");
});

test("状态文案：每个代码都是英文，功能状态写 “Comment Review: …”", () => {
  const expect = {
    off: "Comment Review: off", rules: "Comment Review: rules only", claude: "Comment Review: rules + Claude",
    starting: "Comment Review: loading the Claude component. Rules only for now.",
    no_key: "No Claude API key. Using rules only.",
    no_sdk: "The Claude component isn’t installed. Using rules only.",
    no_review: "Model review isn’t in this version yet. Using rules only.",
    offline: "Can’t reach Claude. Using rules only for now.",
  };
  for (const [status, text] of Object.entries(expect)) assert.equal(G.guardStateText({ status }), text, status);
  assert.equal(G.guardStateText({ status: "env_off" }),
    "Comment Review: turned off by the TLT_COMMENT_GUARD environment variable");
  assert.equal(G.guardStateText({ status: "paused", lag_sec: 4.4 }), "Model review paused: speech recognition is 4 sec behind");
  assert.equal(G.guardStateText({ status: "tripped", lag_sec: 12 }),
    "Model review is off for this session: speech recognition fell 12 sec behind. Rules only.");
  assert.equal(G.guardStateText({ status: "key_rejected", http: 401 }),
    "The Claude API key wasn’t accepted (HTTP 401). Using rules only.");
  assert.equal(G.guardStateText({ status: "cooldown", wait_min: 5 }), "Claude asked the app to wait. Using rules only for 5 min.");
  assert.equal(G.guardStateText({ status: "zzz", enabled: true }), "Comment Review: rules only");
  assert.equal(G.guardStateText({ status: "zzz", enabled: false }), "Comment Review: off");
  for (const status of STATUS_CODES) english(G.guardStateText({ status, lag_sec: 3, http: 401, wait_min: 2 }));
});

test("没经过模型复核的条数：单复数各一句", () => {
  assert.equal(G.guardUnreviewedText({ backend: "claude", unreviewed: 1 }), "1 comment wasn’t reviewed by the model this session.");
  assert.equal(G.guardUnreviewedText({ backend: "claude", unreviewed: 3 }), "3 comments weren’t reviewed by the model this session.");
  assert.equal(G.guardUnreviewedText({ backend: "none", unreviewed: 3 }), "");
});

test("状态胶囊和设置行摘要", () => {
  const cap = G.guardCapsule(STATE);
  assert.deepEqual([cap.text, cap.title, cap.tone], ["Comment Review: rules only", "See recent comments to review", "idle"]);
  assert.equal(G.guardCapsule({ status: "tripped", lag_sec: 9 }).reenable, true);
  assert.equal(G.guardSummary(STATE, 0), "On · Rules only");
  assert.equal(G.guardSummary(Object.assign({}, STATE, { status: "claude", backend: "claude" }), 12), "On · Rules + Claude · 12 marked");
  assert.equal(G.guardSummary(Object.assign({}, STATE, { status: "claude", backend: "claude" }), 1), "On · Rules + Claude · 1 marked");
  assert.equal(G.guardSummary(Object.assign({}, STATE, { enabled: false, status: "off" }), 3), "Off · 3 marked");
  assert.equal(G.guardRulesText(STATE), "510 rules active");
  assert.equal(G.guardRulesText({ rules: { count: 1, warnings: 0 } }), "1 rule active");
  assert.equal(G.guardRulesText({ rules: { count: 12, warnings: 1 } }), "12 rules active · 1 rule check warning");
  assert.equal(G.guardRulesText({ rules: { count: 12, warnings: 3 } }), "12 rules active · 3 rule check warnings");
});

test("时刻：关注名单里更早的人写 “N days ago”", () => {
  const now = new Date(2026, 9, 13, 12, 0, 0).getTime() / 1000;
  const noon = new Date(2026, 9, 13, 9, 5, 7).getTime() / 1000;
  assert.equal(G.guardLastSeenText(noon, now), "09:05:07");
  assert.equal(G.guardLastSeenText(noon - 86400, now), "1 day ago");
  assert.equal(G.guardLastSeenText(noon - 3 * 86400, now), "3 days ago");
});

test("弹幕栏标记：红条、琥珀、模型意见、证据；title 里写来源和命中的词条", () => {
  const red = G.guardCommentView(comment({ guard: RED }), STATE, {});
  assert.equal(red.pill.text, "Possible threat of harm");
  assert.deepEqual(red.pill.title.split("\n"), ["Source: rules", "Matched entries: insult/threat:te voy a matar"]);
  const amber = G.guardCommentView(comment({ guard: Object.assign({}, AMBER, { amb: "review", src: "review", rv: "unsure" }) }), STATE, {});
  assert.equal(amber.note, "Flagged by the model · Model: unsure");
  assert.equal(amber.pill.title.split("\n")[0], "Source: model review");
  for (const v of [red, amber]) { english(v.pill.text); english(v.pill.title); english(v.note); }
});

test("渲染的标记：按钮 Mark / Unmark，整棵子树里没有汉字（昵称账号除外）", () => {
  const doc = makeDoc();
  const item = makeItem(doc, "Ana", "Hi");
  const ctx = { state: STATE, marked: {}, onMark() {} };
  G.guardDecorateComment(doc, item, comment({ guard: Object.assign({}, AMBER, { evidence: "es falso", rv: "agree" }) }), ctx);
  const btn = item.querySelector(".guard-mark-btn");
  assert.equal(btn.getAttribute("aria-label"), "Mark");
  assert.equal(btn.getAttribute("title"), "Mark");
  assert.equal(item.children[1].textContent, "Short, with no question mark · Model: agrees with rules");
  G.guardDecorateComment(doc, item, comment({ guard: AMBER }), Object.assign({}, ctx, { marked: { [UID_A]: entry() } }));
  assert.equal(item.querySelector(".guard-mark-btn").getAttribute("aria-label"), "Unmark");
  // 头里的昵称是 makeItem 放的数据，这里只查守卫自己加的节点
  const mine = item.querySelector(".cmt-head").querySelectorAll(".guard-part");
  assert.ok(mine.length >= 3);
  for (const el of mine) for (const t of uiTexts(el, false)) english(t.replace(/[★☆]/g, ""));
});

test("顶部条：1 件用单数；按事件计（一次冲场只算 1 件）；类别超过 3 个写 “n more”；Latest 时刻", () => {
  const one = G.guardBarSummary({ a: event({ id: "a", cat: "threat", ts: 1000 }) }, 0, { markedTs: [1001] });
  assert.deepEqual(one.parts, ["1 item to review", "1 from marked viewers", "Possible threat of harm 1",
                               "Latest " + G.guardTimeText(1000)]);
  assert.equal(one.text, "1 item to review · 1 from marked viewers · Possible threat of harm 1 · Latest " + G.guardTimeText(1000));
  const events = {
    a: event({ id: "a", cat: "threat", lvl: "alert", n: 2, ts: 1000 }),
    b: event({ id: "b", cat: "insult", lvl: "amber", n: 3, ts: 1001 }),
    c: event({ id: "c", cat: "promo_link", lvl: "alert", ts: 1002 }),
    d: event({ id: "d", cat: "flood_user", lvl: "alert", ts: 1003 }),
    e: event({ id: "e", cat: "promo_contact", lvl: "alert", ts: 1004 }),
  };
  const sum = G.guardBarSummary(events, 0);
  // a 里有 2 条弹幕、b 里有 3 条，各算 1 件（用户决定 10）：4 件红 + 1 件琥珀
  assert.deepEqual(sum.parts.slice(0, 2), ["4 items to review", "1 unclear"]);
  assert.equal(sum.parts[2], "Possible threat of harm 1, Possible insult 1, Possible redirect (link) 1, 2 more");
  for (const t of sum.parts) english(t);
  assert.equal(G.guardBarSummary(events, 5000).show, false);
  assert.equal(G.guardFabTitle(1), "1 item to review");
  assert.equal(G.guardFabTitle(4), "4 items to review");
  // 一次冲场、一次多账号同句：各 1 件，不是 60 件、12 件
  const flood = event({ id: "f", kind: "flood", cat: "flood_user", lvl: "alert", n: 60, window_sec: 20, ts: 2000 });
  const brigade = event({ id: "b", kind: "brigade", cat: "flood_brigade", lvl: "amber", amb: "brigade", n: 12, ts: 2001 });
  const two = G.guardBarSummary([flood, brigade], 0);
  assert.deepEqual(two.parts.slice(0, 2), ["1 item to review", "1 unclear"]);
  assert.equal(two.parts[2], "Possible flooding 1, Same comment from several accounts 1");
  assert.ok(!two.text.includes("comments to review"));
});

test("抽屉：按钮 Show in Comments / Mark / Unmark / Dismiss，空态一句话，多账号 “n more”", () => {
  const doc = makeDoc();
  const box = doc.createElement("div");
  G.guardRenderEvents(doc, box, {}, { marked: {} });
  assert.equal(box.querySelector(".guard-empty").textContent, "No comments to review yet.");
  G.guardRenderEvents(doc, box, { g1: event({ lvl: "amber", amb: "quote", n: 3 }) }, { marked: {} });
  assert.deepEqual(box.querySelectorAll(".btn").map((b) => b.textContent), ["Show in Comments", "Mark", "Dismiss"]);
  assert.equal(box.querySelector(".gev-note").textContent, "Reads like a quote · 3 comments");
  G.guardRenderEvents(doc, box, { g1: event() }, { marked: { [UID_A]: entry() } });
  assert.deepEqual(box.querySelectorAll(".btn").map((b) => b.textContent), ["Show in Comments", "Unmark", "Dismiss"]);
  const accounts = [];
  for (let i = 0; i < 22; i++) accounts.push({ user: "Cuenta " + i });
  G.guardRenderEvents(doc, box, { b1: event({ id: "b1", kind: "brigade", cat: "flood_brigade", lvl: "amber", amb: "brigade",
    n: 22, window_sec: 20, accounts, sample: { cid: "c9", user: "Cuenta 21", text: "quiero uno" } }) }, { marked: {} });
  assert.equal(box.querySelector(".gev-note").textContent, "Same comment from 22 accounts in 20 seconds");
  assert.equal(box.querySelector("summary").textContent, "Show accounts");
  assert.equal(box.querySelector(".gev-more").textContent, "2 more");
  for (const t of uiTexts(box)) english(t);
});

test("关注名单：Added automatically / Added by you / Last comment；Unmark；空态；标题", () => {
  const now = new Date(2026, 9, 13, 12, 0, 0).getTime() / 1000;
  const today = new Date(2026, 9, 13, 9, 5, 7).getTime() / 1000;
  const marked = {
    [UID_A]: entry({ last_seen: today - 86400 }),
    h1: entry({ keys: ["h:handle_only"], nick: "Hana", handle: "handle_only", by: "manual", cat: null, last_seen: today }),
    "n:Solo": { keys: ["n:Solo"], nick: "Solo", handle: null, by: "manual", cat: null, last_seen: today - 60, session: true },
  };
  const rows = G.guardMarkedRows(marked, now);
  assert.equal(rows[0].how, "Added by you");
  assert.equal(rows[0].keyNote, "Saved by @handle. It won’t match if they change it.");
  assert.equal(rows[0].lastText, "Last comment 09:05:07");
  assert.equal(rows[1].keyNote, "Name only. Works for this session only.");
  assert.equal(rows[2].how, "Added automatically: possible threat of harm");
  assert.equal(rows[2].lastText, "Last comment 1 day ago");
  const doc = makeDoc();
  const box = doc.createElement("div");
  G.guardRenderMarked(doc, box, marked, { now });
  assert.deepEqual(box.querySelectorAll(".gmk-un").map((b) => b.textContent), ["Unmark", "Unmark", "Unmark"]);
  for (const t of uiTexts(box)) english(t);
  G.guardRenderMarked(doc, box, {}, {});
  assert.equal(box.querySelector(".guard-empty").textContent, "No marked viewers yet.");
  const h = doc.createElement("div");
  G.guardFillHeading(doc, h, "luna_demo");
  assert.equal(h.textContent, "Marked Viewers · @luna_demo");
});
