// 弹幕留意前端（web/guard.js，中文界面）。纯函数 + 渲染函数（假 DOM，见 tests/guard-dom.mjs）。
// 英文在 tests/guard.en.test.mjs。接线（app.js）在 tests/app.en.test.mjs 和 tests/translate-no.test.mjs。
//
// 钉住的几件事：
// - 类别、琥珀理由、模型意见、状态都按后端发的代码映射，并且只写观察，不写原因（第八条）；
// - 账号 id 是字符串，永远不转成数字：19 位的 id 超过 2^53，转了就会把两个人比成同一个人；
// - 关注高亮按名单里的键对每条弹幕现算（uid / sec / handle / 只有昵称的人），加入和移出都即时生效；
// - 顶部条永远一行，为 0 的不写，类别超过 3 个折成「另 n 类」，未读按「已看」的时刻算；
// - 昵称、账号标 translate="no"，弹幕正文带 class i18n-data（浏览器翻译 / 英文检查的两条规矩）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { makeDoc, makeItem, uiTexts } from "./guard-dom.mjs";
import { ALL_CATS, AMBER_CODES, RV_CODES, STATUS_CODES, STATE, UID_A, UID_B, comment, RED, AMBER, NOTE, entry,
         event } from "./guard-fixtures.mjs";

const require = createRequire(import.meta.url);
const G = require("../web/guard.js");

// 与 tests/i18n_rules.py 的 ZH_LABELS / ZH_CAUSAL 相同（第八条家族的中文臂，guard.js 整个文件在家族里）
const ZH_LABELS = /年龄|限流|封禁|封号|私密|观看限制|地区|风控|被挡|被墙|拦截|繁忙/;
const ZH_CAUSAL = /可能是|也可能|可能已|可能[^。；]*，或|多半|八成|应该是|大概是|估计是|似乎|好像|看起来是|因为|由于|导致|原因是|也许|或许|恐怕|不是[^。；]*问题/;

test("语言确实是中文（不然下面全是空转）", () => {
  assert.equal(require("../web/i18n.js").UI_LANG, "zh");
});

// ---- 文案 -----------------------------------------------------------------------------------------

test("类别名：中文一律「疑似……」，只有 flood_brigade 写观察；认不出的类别有兜底", () => {
  const labels = ALL_CATS.map((c) => G.guardCatLabel(c));
  assert.equal(new Set(labels).size, ALL_CATS.length, "每个类别一句话，不重复");
  for (const c of ALL_CATS) {
    const t = G.guardCatLabel(c);
    if (c === "flood_brigade") assert.equal(t, "多账号同一句"); else assert.match(t, /^疑似/, c);
  }
  assert.equal(G.guardCatLabel("insult"), "疑似辱骂");
  assert.equal(G.guardCatLabel("threat"), "疑似人身威胁");
  assert.equal(G.guardCatLabel("promo_link"), "疑似引流（链接）");
  assert.equal(G.guardCatLabel("something_new"), "需要留意");
  assert.equal(G.guardCatLabel(undefined), "需要留意");
});

test("琥珀理由：只写观察；多账号同句带账号数和秒数；认不出的返回空串", () => {
  assert.equal(G.guardAmberText("maybe_question"), "句子很短，没有问号");
  assert.equal(G.guardAmberText("grievance"), "说的是自己的遭遇");
  assert.equal(G.guardAmberText("quote"), "像在转述别人的话");
  assert.equal(G.guardAmberText("accumulated"), "同一人 10 分钟内第 2 次");
  assert.equal(G.guardAmberText("question_link"), "问句里带链接");
  assert.equal(G.guardAmberText("pending"), "命中的词条还没确认");
  assert.equal(G.guardAmberText("self"), "句子是第一人称");
  assert.equal(G.guardAmberText("review"), "模型标出");
  assert.equal(G.guardAmberText("brigade", { n: 5, window_sec: 20 }), "5 个账号，20 秒内同一句");
  assert.equal(G.guardAmberText("nope"), "");
  assert.equal(G.guardAmberText(null), "");
  for (const code of AMBER_CODES) {
    const t = G.guardAmberText(code, { n: 3, window_sec: 20 });
    assert.ok(t, code);
    assert.ok(!ZH_LABELS.test(t) && !ZH_CAUSAL.test(t), "琥珀理由写了原因或标签：" + t);
  }
});

test("模型意见：中文写「模型意见：…」，没有结果返回空串", () => {
  assert.equal(G.guardReviewText("pending"), "模型复核中…");
  assert.equal(G.guardReviewText("agree"), "模型意见：同意规则");
  assert.equal(G.guardReviewText("question"), "模型意见：提问");
  assert.equal(G.guardReviewText("complaint"), "模型意见：客诉");
  assert.equal(G.guardReviewText("friendly"), "模型意见：友好");
  assert.equal(G.guardReviewText("neutral"), "模型意见：未见攻击");
  assert.equal(G.guardReviewText("unsure"), "模型意见：拿不准");
  assert.equal(G.guardReviewText("skipped"), "未经模型复核");
  assert.equal(G.guardReviewText(null), "");
  assert.equal(G.guardReviewText("weird"), "");
});

test("事件计数：只写数过的数，不写恶意、水军，不写原因", () => {
  assert.equal(G.guardCountText({ kind: "flood", n: 6, window_sec: 10 }), "10 秒内 6 条");
  assert.equal(G.guardCountText({ kind: "comment", n: 3, window_sec: 60 }), "共 3 条");
  assert.equal(G.guardCountText({ kind: "comment", n: 1 }), "共 1 条");
});

test("状态文案：每个代码都有话，带数字的取 state 里的数；没复核绝不写成没问题", () => {
  const expect = {
    off: "弹幕留意：已关闭", rules: "弹幕留意：只用规则", claude: "弹幕留意：规则 + Claude",
    starting: "弹幕留意：Claude 组件载入中，先只用规则",
    no_key: "没有 Claude 密钥，只用规则", no_sdk: "缺少 Claude 组件，只用规则", no_review: "这个版本还没有模型复核，只用规则", offline: "连不上 Claude，先只用规则",
  };
  for (const [status, text] of Object.entries(expect)) assert.equal(G.guardStateText({ status }), text, status);
  assert.match(G.guardStateText({ status: "env_off" }), /TLT_COMMENT_GUARD/);
  assert.equal(G.guardStateText({ status: "paused", lag_sec: 4.4 }), "模型复核暂停：识别落后 4 秒");
  assert.equal(G.guardStateText({ status: "paused", trip: { sec: 7 } }), "模型复核暂停：识别落后 7 秒");
  assert.equal(G.guardStateText({ status: "tripped", lag_sec: 12 }), "本场模型复核已停用：识别落后 12 秒。只用规则。");
  assert.equal(G.guardStateText({ status: "key_rejected", http: 401 }), "Claude 密钥没被接受（HTTP 401），只用规则");
  assert.equal(G.guardStateText({ status: "cooldown", wait_min: 5 }), "Claude 要求稍后再试，5 分钟内只用规则");
  // 认不出的状态：按开关写，不留空
  assert.equal(G.guardStateText({ status: "zzz", enabled: true }), "弹幕留意：只用规则");
  assert.equal(G.guardStateText({ status: "zzz", enabled: false }), "弹幕留意：已关闭");
  for (const status of STATUS_CODES) {
    const t = G.guardStateText({ status, lag_sec: 3, http: 401, wait_min: 2 });
    assert.ok(t && !ZH_LABELS.test(t) && !ZH_CAUSAL.test(t), status + "：" + t);
  }
});

test("没经过模型复核的条数：只在选了 Claude 时说，1 条和多条各一句", () => {
  assert.equal(G.guardUnreviewedText({ backend: "none", unreviewed: 9 }), "");
  assert.equal(G.guardUnreviewedText({ backend: "claude", unreviewed: 0 }), "");
  assert.equal(G.guardUnreviewedText({ backend: "claude", unreviewed: 3 }), "本场 3 条没经过模型复核");
  assert.equal(G.guardUnreviewedText(null), "");
});

test("状态胶囊：气色（tone）、重新启用、没有状态返回 null", () => {
  assert.equal(G.guardCapsule(null), null);
  assert.equal(G.guardCapsule({}), null);
  const rules = G.guardCapsule(STATE);
  assert.deepEqual([rules.text, rules.tone, rules.reenable], ["弹幕留意：只用规则", "idle", false]);
  assert.equal(G.guardCapsule({ status: "claude", backend: "claude" }).tone, "on");
  for (const status of ["starting", "paused", "tripped", "no_key", "no_sdk", "no_review", "key_rejected", "offline", "cooldown"]) {
    assert.equal(G.guardCapsule({ status }).tone, "warn", status);
  }
  const tripped = G.guardCapsule({ status: "tripped", lag_sec: 9, backend: "claude", unreviewed: 4 });
  assert.equal(tripped.reenable, true);
  assert.equal(tripped.reenableOk, true);
  assert.equal(tripped.unreviewed, "本场 4 条没经过模型复核");
  assert.equal(G.guardCapsule({ status: "tripped", reenable_ok: false }).reenableOk, false);
  assert.equal(G.guardCapsule({ status: "rules" }).reenable, false);
});

test("设置行摘要：开关 · 实际在起作用的复核方式 · 关注人数", () => {
  assert.equal(G.guardSummary(STATE, 0), "开启 · 只用规则");
  assert.equal(G.guardSummary(Object.assign({}, STATE, { status: "claude", backend: "claude" }), 12), "开启 · 规则 + Claude · 关注 12 人");
  // 选了 Claude 但没在复核：写实际在起作用的
  assert.equal(G.guardSummary(Object.assign({}, STATE, { status: "no_sdk", backend: "claude" }), 0), "开启 · 只用规则");
  assert.equal(G.guardSummary(Object.assign({}, STATE, { status: "no_review", backend: "claude" }), 0), "开启 · 只用规则");
  assert.equal(G.guardSummary(Object.assign({}, STATE, { enabled: false, status: "off" }), 3), "关闭 · 关注 3 人");
  assert.equal(G.guardSummary(STATE), "开启 · 只用规则", "没传人数就看 state.n_marked");
  assert.equal(G.guardSummary(Object.assign({}, STATE, { n_marked: 2 })), "开启 · 只用规则 · 关注 2 人");
  assert.equal(G.guardSummary(null, 0), "关闭");
});

test("规则条数和体检警告", () => {
  assert.equal(G.guardRulesText(STATE), "规则 510 条已生效");
  assert.equal(G.guardRulesText(Object.assign({}, STATE, { rules: { count: 12, warnings: 3 } })), "规则 12 条已生效 · 3 条体检警告");
  assert.equal(G.guardRulesText({ rules: null }), "");
  assert.equal(G.guardRulesText(null), "");
});

test("时刻：24 小时制 HH:MM:SS；关注名单里更早的人写「N 天前」", () => {
  const noon = new Date(2026, 9, 13, 9, 5, 7).getTime() / 1000;
  assert.equal(G.guardTimeText(noon), "09:05:07");
  assert.equal(G.guardTimeText(0), "");
  assert.equal(G.guardTimeText("x"), "");
  const now = new Date(2026, 9, 13, 12, 0, 0).getTime() / 1000;
  assert.equal(G.guardLastSeenText(noon, now), "09:05:07");
  assert.equal(G.guardLastSeenText(noon - 86400, now), "1 天前");
  assert.equal(G.guardLastSeenText(noon - 3 * 86400, now), "3 天前");
  assert.equal(G.guardLastSeenText(0, now), "");
});

// ---- 账号当字符串、关注名单的匹配 ---------------------------------------------------------------------

test("账号是字符串：19 位的 id 原样到达，两个转成数字会相等的 id 仍然是两个人", () => {
  assert.equal(Number(UID_A), Number(UID_B), "夹具有效：转成数字这两个会撞（超过 2^53）");
  const marked = { [UID_A]: entry({ keys: [UID_A] }) };
  assert.equal(G.guardIsMarked({ uid: UID_A }, marked), true);
  assert.equal(G.guardIsMarked({ uid: UID_B }, marked), false);
  assert.deepEqual(G.guardKeys({ uid: UID_A }), [UID_A]);
  assert.equal(G.guardMarkKey({ uid: UID_A }), UID_A);
  assert.equal(G.guardMarkMessage({ uid: UID_A, user: "Ana" }, true).key, UID_A);
  assert.equal(typeof G.guardMarkMessage({ uid: UID_A }, true).key, "string");
  // 数字类型的 uid（后端不会发，但别被当成有效账号）
  assert.equal(G.guardMarkKey({ uid: 7234567890123456789 }), "");
});

test("guard.js 源码里没有把 uid 转成数字的写法", () => {
  const src = readFileSync(new URL("../web/guard.js", import.meta.url), "utf8");
  const bad = src.split("\n").filter((l) => /uid/i.test(l) && /(Number|parseInt|parseFloat|Math\.\w+)\s*\(|[^\w]\+\s*\w*uid/.test(l)
                                              && !/^\s*(\/\/|\*|\/\*)/.test(l));
  assert.deepEqual(bad, []);
});

test("匹配的键：uid、sec、小写的 handle；没有 uid 的人再加昵称键", () => {
  assert.deepEqual(G.guardKeys({ uid: UID_A, sec: "s:MS4w", handle: "Ana_Demo", user: "Ana" }),
    [UID_A, "s:MS4w", "h:ana_demo"]);
  assert.deepEqual(G.guardKeys({ user: "Solo Nick" }), ["n:Solo Nick"]);
  assert.deepEqual(G.guardKeys({ handle: "x" }), ["h:x"]);
  assert.deepEqual(G.guardKeys({ user: "x".repeat(100) }), ["n:" + "x".repeat(64)], "昵称与后端一样只取前 64 个字");
  assert.deepEqual(G.guardKeys(null), []);
  assert.deepEqual(G.guardKeys({ uid: "", sec: 5, handle: null }), []);
});

test("关注名单任一键命中就算同一个人：换了 uid 还有 sec，只有 handle 也认", () => {
  const marked = { [UID_A]: entry(), "n:Solo": { keys: ["n:Solo"], nick: "Solo", by: "manual", session: true } };
  assert.equal(G.guardIsMarked({ uid: "999", sec: "s:MS4wLjAB" }, marked), true, "sec 对上了");
  assert.equal(G.guardIsMarked({ uid: "999", handle: "ANA_demo" }, marked), true, "handle 对上了（不分大小写）");
  assert.equal(G.guardIsMarked({ user: "Solo" }, marked), true, "只有昵称的人按昵称认");
  assert.equal(G.guardIsMarked({ uid: "999", user: "Solo" }, marked), false, "有 uid 的人不按昵称认");
  assert.equal(G.guardIsMarked({ uid: "999", handle: "other" }, marked), false);
  assert.equal(G.guardIsMarked({ uid: UID_A }, {}), false);
  assert.equal(G.guardIsMarked({ uid: UID_A }, null), false);
  const index = G.guardMarkIndex(marked);
  assert.equal(index[UID_A], UID_A);
  assert.equal(index["s:MS4wLjAB"], UID_A);
  assert.equal(G.guardIsMarked({ uid: "s:MS4wLjAB" }, marked, index), true, "调用方可以传自己建好的索引");
});

test("加入 / 移出随消息带的信息；没有 uid 用昵称键；什么都没有就不发", () => {
  assert.deepEqual(G.guardMarkMessage({ uid: UID_A, user: "Ana", handle: "ana_demo", uid_src: "id" }, true),
    { type: "guard_mark", key: UID_A, on: true, nick: "Ana", handle: "ana_demo", uid_src: "id" });
  assert.deepEqual(G.guardMarkMessage({ user: "Solo" }, false),
    { type: "guard_mark", key: "n:Solo", on: false, nick: "Solo", handle: null, uid_src: null });
  assert.equal(G.guardMarkKey({}), "");
});

// ---- 弹幕栏里一条弹幕的标记 ------------------------------------------------------------------------------

test("红条：类别胶囊 + 来源和命中的词条写进 title；琥珀：理由写在下一行", () => {
  const red = G.guardCommentView(comment({ guard: RED }), STATE, {});
  assert.deepEqual(red.cls, ["guard-alert"]);
  assert.equal(red.pill.text, "疑似人身威胁");
  assert.equal(red.pill.level, "alert");
  assert.deepEqual(red.pill.title.split("\n"), ["来源：规则", "命中的词条：insult/threat:te voy a matar"]);
  assert.equal(red.note, "");
  const amber = G.guardCommentView(comment({ guard: AMBER }), STATE, {});
  assert.deepEqual(amber.cls, ["guard-amber"]);
  assert.equal(amber.pill.text, "疑似指称假货或骗局");
  assert.equal(amber.note, "句子很短，没有问号");
  assert.ok(amber.pill.title.includes("句子很短，没有问号"));
});

test("模型意见写在备注里，模型标出的琥珀来源写「模型复核」，证据片段单独一行", () => {
  const v = G.guardCommentView(comment({ guard: Object.assign({}, AMBER, { amb: "review", src: "review", rv: "unsure",
    evidence: "es falso" }) }), STATE, {});
  assert.equal(v.note, "模型标出 · 模型意见：拿不准");
  assert.equal(v.evidence, "es falso");
  assert.equal(v.pill.title.split("\n")[0], "来源：模型复核");
  const agree = G.guardCommentView(comment({ guard: Object.assign({}, RED, { rv: "agree" }) }), STATE, {});
  assert.equal(agree.note, "模型意见：同意规则");
  assert.equal(G.guardCommentView(comment({ guard: Object.assign({}, RED, { rv: "pending" }) }), STATE, {}).note, "模型复核中…");
});

test("多账号同句的琥珀理由取事件里的账号数和秒数", () => {
  const g = { lvl: "amber", amb: "brigade", cats: ["flood_brigade"], ev: "b1", why: [] };
  const events = { b1: { id: "b1", n: 5, window_sec: 20 } };
  assert.equal(G.guardCommentView(comment({ guard: g }), STATE, {}, undefined, events).note, "5 个账号，20 秒内同一句");
  assert.equal(G.guardCommentView(comment({ guard: g }), STATE, {}).note, "0 个账号，0 秒内同一句", "事件还没到：不抛");
});

test("弱信号（note）只在设置里开了才有标记，开了是浅灰描边", () => {
  assert.deepEqual(G.guardCommentView(comment({ guard: NOTE }), STATE, {}).cls, []);
  assert.equal(G.guardCommentView(comment({ guard: NOTE }), STATE, {}).pill, null);
  const on = G.guardCommentView(comment({ guard: NOTE }), Object.assign({}, STATE, { weak: true }), {});
  assert.deepEqual(on.cls, ["guard-weak"]);
  assert.equal(on.pill.level, "weak");
});

test("关注的人：加 guard-marked，可以和红条并存；加入按钮只在守卫开着时有", () => {
  const marked = { [UID_A]: entry() };
  const v = G.guardCommentView(comment({ guard: RED }), STATE, marked);
  assert.deepEqual(v.cls, ["guard-marked", "guard-alert"]);
  assert.equal(v.marked, true);
  assert.equal(v.showMark, true);
  assert.equal(G.guardCommentView(comment(), Object.assign({}, STATE, { enabled: false }), {}).showMark, false);
  assert.equal(G.guardCommentView(comment(), null, {}).showMark, false);
  assert.equal(G.guardCommentView({ id: "x" }, STATE, {}).showMark, false, "没有 uid 也没有昵称：没法加");
  const plain = G.guardCommentView(comment(), STATE, {});
  assert.deepEqual(plain.cls, []);
  assert.equal(plain.pill, null);
});

test("坏数据不抛：guard 不是对象、lvl 认不出、cats 不是数组", () => {
  for (const guard of [null, "x", 5, [], { lvl: "weird" }, { lvl: "alert" }, { lvl: "alert", cats: "threat", why: 5 }]) {
    const v = G.guardCommentView(comment({ guard }), STATE, {});
    assert.ok(Array.isArray(v.cls));
  }
  assert.equal(G.guardCommentView(comment({ guard: { lvl: "alert" } }), STATE, {}).pill.text, "需要留意");
});

// ---- 渲染：弹幕栏标记 ------------------------------------------------------------------------------------

test("画标记：★、胶囊、☆ 按钮、琥珀备注、证据都插在对的位置；重画不重复", () => {
  const doc = makeDoc();
  const item = makeItem(doc);
  const clicks = [];
  const ctx = { state: STATE, marked: {}, onMark: (c, on) => clicks.push([c.uid, on]) };
  const c = comment({ guard: Object.assign({}, AMBER, { evidence: "es falso", rv: "agree" }) });
  G.guardDecorateComment(doc, item, c, ctx);
  const head = item.querySelector(".cmt-head");
  assert.equal(item.dataset.uid, UID_A);
  assert.ok(item.cls.includes("guard-amber"));
  assert.equal(head.querySelector(".guard-pill").textContent, "疑似指称假货或骗局");
  assert.equal(head.querySelector(".guard-mark-btn").textContent, "☆");
  assert.equal(head.querySelector(".guard-mark-btn").getAttribute("aria-label"), "加入关注名单");
  assert.equal(head.querySelector(".guard-mark-btn").getAttribute("aria-pressed"), "false");
  // 备注和证据在头和译文行之间，顺序：头、备注、证据、译文、原文
  assert.deepEqual(item.children.map((x) => x.cls[0]), ["cmt-head", "guard-part", "guard-part", "cmt-zh", "cmt-orig"]);
  assert.equal(item.children[1].textContent, "句子很短，没有问号 · 模型意见：同意规则");
  assert.ok(item.children[2].cls.includes("guard-ev") && item.children[2].cls.includes("i18n-data"), "证据是弹幕正文，带正文 class");
  head.querySelector(".guard-mark-btn").click();
  assert.deepEqual(clicks, [[UID_A, true]]);
  // 重画（同一条，换成关注的人）：旧的撤干净，不叠加
  G.guardDecorateComment(doc, item, c, Object.assign({}, ctx, { marked: { [UID_A]: entry() } }));
  assert.equal(head.querySelectorAll(".guard-pill").length, 1);
  assert.equal(head.querySelectorAll(".guard-mark-btn").length, 1);
  assert.equal(head.querySelectorAll(".guard-star").length, 1);
  assert.equal(head.firstChild.textContent, "★", "★ 在昵称前面");
  assert.equal(head.querySelector(".guard-mark-btn").textContent, "★");
  assert.equal(head.querySelector(".guard-mark-btn").getAttribute("aria-label"), "移出关注名单");
  assert.equal(head.querySelector(".guard-mark-btn").getAttribute("aria-pressed"), "true");
  assert.ok(item.cls.includes("guard-marked") && item.cls.includes("guard-amber"));
  head.querySelector(".guard-mark-btn").click();
  assert.deepEqual(clicks[1], [UID_A, false]);
  // 再重画成什么都没有：类和节点都撤掉
  G.guardDecorateComment(doc, item, comment(), { state: STATE, marked: {} });
  assert.deepEqual(item.cls.filter((x) => x.startsWith("guard-")), []);
  assert.equal(item.children.length, 3);
  assert.equal(head.querySelectorAll(".guard-part").length, 1, "只剩一个 ☆ 按钮");
});

test("uid_ok 为假（只有 handle 的人）不加 data-uid；守卫关着没有按钮但已有的标记照画", () => {
  const doc = makeDoc();
  const item = makeItem(doc);
  G.guardDecorateComment(doc, item, comment({ uid: "h:ana_demo", uid_ok: false, guard: RED }), { state: STATE, marked: {} });
  assert.equal(item.dataset.uid, undefined);
  const item2 = makeItem(doc);
  G.guardDecorateComment(doc, item2, comment({ guard: RED }), { state: Object.assign({}, STATE, { enabled: false }), marked: {} });
  assert.equal(item2.querySelector(".guard-mark-btn"), null);
  assert.ok(item2.cls.includes("guard-alert"));
});

// ---- 顶部条 ----------------------------------------------------------------------------------------------

test("顶部条：红条事件数、琥珀事件数、关注的人发言数分开计，为 0 的不写，未读按已看的时刻算", () => {
  const events = {
    a: event({ id: "a", cat: "threat", lvl: "alert", n: 2, ts: 1000 }),
    b: event({ id: "b", cat: "insult", lvl: "amber", n: 1, ts: 1005 }),
    c: event({ id: "c", cat: "promo_link", lvl: "alert", n: 1, ts: 900 }),
  };
  const all = G.guardBarSummary(events, 0, { markedTs: [1001, 1002] });
  assert.equal(all.show, true);
  // 按事件计：a 里有 2 条弹幕也只算 1 件（用户决定 10）
  assert.deepEqual([all.red, all.amber, all.marked], [2, 1, 2]);
  assert.deepEqual(all.parts.slice(0, 3), ["需要留意 2 件", "拿不准 1 件", "关注的人发言 2"]);
  assert.equal(all.parts[3], "疑似人身威胁 1、疑似辱骂 1、疑似引流（链接） 1", "人身威胁排最前");
  assert.equal(all.parts[4], "最新 " + G.guardTimeText(1005));
  assert.equal(all.text, all.parts.join(" · "));
  // 已看到 1000：只剩 b 是未读
  const later = G.guardBarSummary(events, 1000, { markedTs: [1001, 999] });
  assert.deepEqual([later.red, later.amber, later.marked], [0, 1, 1]);
  assert.equal(later.parts[0], "拿不准 1 件", "为 0 的不写");
  // 全部看过：不显示
  const none = G.guardBarSummary(events, 1005, { markedTs: [1001] });
  assert.equal(none.show, false);
  assert.deepEqual(none.parts, []);
  assert.equal(none.text, "");
  assert.equal(G.guardBarSummary({}, 0).show, false);
  assert.equal(G.guardBarSummary(null, 0).show, false);
});

test("顶部条：超过 3 类只写前 3 类，其余折成「另 n 类」；没有的类别不凭空出现", () => {
  const cats = ["promo_contact", "flood_user", "insult", "threat", "accuse_call"];
  const events = {};
  cats.forEach((cat, i) => { events["e" + i] = event({ id: "e" + i, cat, lvl: "alert", ts: 2000 + i }); });
  const sum = G.guardBarSummary(events, 0);
  assert.deepEqual(sum.cats.map((c) => c.code), ["threat", "insult", "accuse_call", "promo_contact", "flood_user"]);
  assert.equal(sum.more, 2);
  assert.equal(sum.parts[1], "疑似人身威胁 1、疑似辱骂 1、疑似号召举报或不买 1、另 2 类");
  assert.ok(!sum.text.includes("疑似刷屏"));
  // 刚好 3 类：不写「另」
  const three = G.guardBarSummary({ a: events.e0, b: events.e1, c: events.e2 }, 0);
  assert.equal(three.more, 0);
  assert.ok(!three.text.includes("另"));
});

test("顶部条只数红条和琥珀的事件；忽略的事件和形状不对的事件不算；冲场的一个事件算 1 件，不数其中的弹幕条数", () => {
  const events = [
    event({ id: "x", kind: "flood", cat: "flood_user", lvl: "alert", n: 60, ts: 3000 }),
    event({ id: "y", lvl: "note", ts: 3001 }),
    event({ id: "z", dismissed: true, ts: 3002 }),
    null, "bad", { ts: 3003 },
  ];
  const sum = G.guardBarSummary(events, 0);
  assert.equal(sum.red, 1);
  assert.equal(sum.parts[0], "需要留意 1 件");
  assert.equal(sum.parts[1], "疑似刷屏 1", "类别也按事件计");
  assert.equal(G.guardEventList(events).length, 2, "列表只去掉忽略的和形状不对的；note 档由条自己筛掉");
});

test("顶部条按事件计：一次冲场、一次多账号同句各算 1 件；事件里的 n 不进条上的数字", () => {
  const flood = event({ id: "f", kind: "flood", cat: "flood_user", lvl: "alert", n: 60, window_sec: 20, ts: 4000 });
  const brigade = event({ id: "b", kind: "brigade", cat: "flood_brigade", lvl: "amber", amb: "brigade", n: 12,
                          accounts: [{ user: "a" }, { user: "b" }], ts: 4001 });
  const single = event({ id: "s", cat: "threat", lvl: "alert", n: 1, ts: 4002 });
  const one = G.guardBarSummary([flood], 0);
  assert.deepEqual([one.red, one.amber], [1, 0]);
  const sum = G.guardBarSummary([flood, brigade, single], 0);
  assert.deepEqual([sum.red, sum.amber], [2, 1], "60 条冲场 + 12 个账号同句 + 1 条威胁 = 2 件红 + 1 件琥珀");
  assert.deepEqual(sum.parts.slice(0, 2), ["需要留意 2 件", "拿不准 1 件"]);
  assert.equal(sum.parts[2], "疑似人身威胁 1、疑似刷屏 1、多账号同一句 1");
  // 同一个冲场事件被后端持续更新（同一个 id、n 在涨）：仍然是 1 件
  const grown = G.guardBarSummary({ f: Object.assign({}, flood, { n: 400, ts: 4010 }) }, 0);
  assert.equal(grown.red, 1);
  // 事件没有 n（老后端）也是 1 件
  const noN = event({ id: "z", ts: 4020 });
  delete noN.n;
  assert.equal(G.guardBarSummary([noN], 0).red, 1);
  // 抽屉那一行仍然写事件里的条数——那是在说这件事本身有多大
  assert.equal(G.guardCountText(flood), "20 秒内 60 条");
});

test("事件按最近更新排序，新的在前", () => {
  const list = G.guardEventList({ a: event({ id: "a", ts: 1 }), b: event({ id: "b", ts: 3 }), c: event({ id: "c", ts: 2 }) });
  assert.deepEqual(list.map((e) => e.id), ["b", "c", "a"]);
});

test("浮钮上红点的提示", () => {
  assert.equal(G.guardFabTitle(1), "1 件需要留意");
  assert.equal(G.guardFabTitle(7), "7 件需要留意");
});

// ---- 抽屉 ------------------------------------------------------------------------------------------------

test("抽屉：每行有时间、昵称、类别胶囊、原文、已有译文、琥珀理由和模型意见；最多 50 条", () => {
  const doc = makeDoc();
  const box = doc.createElement("div");
  const events = {
    g2: event({ id: "g2", cat: "accuse_claim", lvl: "amber", amb: "pending", rv: "unsure", ts: 1760000020,
                sample: { cid: "c2", user: "Lupita", text: "es falso", uid: "11", handle: "lupita" } }),
    g1: event({ ts: 1760000010 }),
  };
  const n = G.guardRenderEvents(doc, box, events, { marked: {}, translationOf: (cid) => cid === "c2" ? "是假的" : "" });
  assert.equal(n, 2);
  const rows = box.querySelectorAll(".guard-ev-row");
  assert.deepEqual(rows.map((r) => r.getAttribute("data-ev")), ["g2", "g1"], "新的在上");
  const second = rows[0];
  assert.ok(second.cls.includes("amber"));
  assert.equal(second.querySelector(".gev-user").textContent, "Lupita");
  assert.equal(second.querySelector(".gev-user").getAttribute("translate"), "no", "昵称是名字");
  assert.equal(second.querySelector(".guard-pill").textContent, "疑似指称假货或骗局");
  assert.equal(second.querySelector(".gev-text").textContent, "es falso");
  assert.ok(second.querySelector(".gev-text").cls.includes("i18n-data"), "弹幕原文是正文");
  assert.equal(second.querySelector(".gev-zh").textContent, "是假的");
  assert.equal(second.querySelector(".gev-note").textContent, "命中的词条还没确认 · 模型意见：拿不准");
  assert.deepEqual(second.querySelectorAll(".btn").map((b) => b.textContent), ["定位", "加入关注名单", "忽略"]);
  assert.equal(rows[1].querySelector(".gev-zh"), null, "没有译文就不画那一行");
  assert.equal(rows[1].querySelector(".gev-note"), null);
  assert.ok(rows[1].cls.includes("alert"));
  // 最多 50 条
  const many = {};
  for (let i = 0; i < 80; i++) many["e" + i] = event({ id: "e" + i, ts: 5000 + i });
  const box2 = doc.createElement("div");
  assert.equal(G.guardRenderEvents(doc, box2, many, { marked: {} }), 50);
  assert.equal(box2.querySelectorAll(".guard-ev-row").length, 50);
  assert.equal(box2.querySelectorAll(".guard-ev-row")[0].getAttribute("data-ev"), "e79");
});

test("抽屉：三个按钮各走各的回调；已关注的人按钮写「移出关注名单」", () => {
  const doc = makeDoc();
  const box = doc.createElement("div");
  const calls = [];
  const ctx = { marked: { [UID_A]: entry() },
    onLocate: (ev) => calls.push(["locate", ev.id]), onMark: (who, on) => calls.push(["mark", who.uid, on]),
    onDismiss: (ev) => calls.push(["dismiss", ev.id]) };
  G.guardRenderEvents(doc, box, { g1: event() }, ctx);
  const buttons = box.querySelectorAll(".btn");
  assert.deepEqual(buttons.map((b) => b.textContent), ["定位", "移出关注名单", "忽略"]);
  buttons.forEach((b) => b.click());
  assert.deepEqual(calls, [["locate", "g1"], ["mark", UID_A, false], ["dismiss", "g1"]]);
});

test("抽屉：多账号同句的事件折叠成一行，展开后列前 20 个账号，没有「加入」按钮", () => {
  const doc = makeDoc();
  const box = doc.createElement("div");
  const accounts = [];
  for (let i = 0; i < 25; i++) accounts.push({ user: "Cuenta " + i, uid: "70" + i });
  const ev = event({ id: "b1", kind: "brigade", cat: "flood_brigade", lvl: "amber", amb: "brigade", n: 25, window_sec: 20,
                     accounts, sample: { cid: "c9", user: "Cuenta 24", text: "quiero uno ya mismo luna", uid: "7024" } });
  G.guardRenderEvents(doc, box, { b1: ev }, { marked: {} });
  const row = box.querySelector(".guard-ev-row");
  assert.equal(row.querySelector(".gev-note").textContent, "25 个账号，20 秒内同一句");
  const names = row.querySelectorAll("li").filter((li) => !li.cls.includes("gev-more"));
  assert.equal(names.length, 20);
  assert.equal(names[0].getAttribute("translate"), "no");
  assert.equal(row.querySelector(".gev-more").textContent, "另 5 个");
  assert.equal(row.querySelector("summary").textContent, "展开账号");
  assert.deepEqual(row.querySelectorAll(".btn").map((b) => b.textContent), ["定位", "忽略"]);
  assert.equal(row.querySelector(".guard-pill").textContent, "多账号同一句");
});

test("抽屉：没有事件写一句空话；认不出的类别不抛", () => {
  const doc = makeDoc();
  const box = doc.createElement("div");
  assert.equal(G.guardRenderEvents(doc, box, {}, { marked: {} }), 0);
  assert.equal(box.querySelector(".guard-empty").textContent, "暂时没有需要留意的弹幕");
  G.guardRenderEvents(doc, box, { z: event({ id: "z", cat: "brand_new", sample: undefined }) }, { marked: {} });
  assert.equal(box.querySelectorAll(".guard-ev-row").length, 1, "重画先清空旧的");
  assert.equal(box.querySelector(".guard-pill").textContent, "需要留意");
  assert.equal(box.querySelector(".gev-text"), null);
});

// ---- 关注名单 --------------------------------------------------------------------------------------------

test("关注名单：最近发言的在前；自动加入写类别，手动加入写「手动加入」；键不稳的人有备注", () => {
  const now = new Date(2026, 9, 13, 12, 0, 0).getTime() / 1000;
  const today = new Date(2026, 9, 13, 9, 5, 7).getTime() / 1000;
  const marked = {
    [UID_A]: entry({ last_seen: today - 3 * 86400 }),
    h1: entry({ keys: ["h:handle_only"], nick: "Hana", handle: "handle_only", by: "manual", cat: null, last_seen: today }),
    "n:Solo": { keys: ["n:Solo"], nick: "Solo", handle: null, by: "manual", cat: null, first: 1, last_seen: today - 60, hits: 0, session: true },
  };
  const rows = G.guardMarkedRows(marked, now);
  assert.deepEqual(rows.map((r) => r.key), ["h1", "n:Solo", UID_A]);
  assert.equal(rows[0].how, "手动加入");
  assert.equal(rows[0].keyNote, "按 @账号记，对方改账号后对不上");
  assert.equal(rows[0].lastText, "最近发言 09:05:07");
  assert.equal(rows[1].keyNote, "只有昵称，只在本场有效");
  assert.equal(rows[2].how, "自动加入：疑似人身威胁");
  assert.equal(rows[2].keyNote, "");
  assert.equal(rows[2].lastText, "最近发言 3 天前");
  assert.equal(rows[2].auto, true);
});

test("画名单：昵称和 @handle 是名字，「移出」走回调，空名单写一句话", () => {
  const doc = makeDoc();
  const box = doc.createElement("div");
  const gone = [];
  const n = G.guardRenderMarked(doc, box, { [UID_A]: entry() }, { now: 1760000200, onUnmark: (k) => gone.push(k) });
  assert.equal(n, 1);
  const row = box.querySelector(".guard-mk-row");
  assert.equal(row.getAttribute("data-key"), UID_A);
  assert.equal(row.querySelector(".gmk-nick").textContent, "Ana");
  assert.equal(row.querySelector(".gmk-nick").getAttribute("translate"), "no");
  assert.equal(row.querySelector(".gmk-handle").textContent, "@ana_demo");
  assert.equal(row.querySelector(".gmk-handle").getAttribute("translate"), "no");
  assert.equal(row.querySelector(".gmk-meta").textContent.startsWith("自动加入：疑似人身威胁 · 最近发言"), true);
  row.querySelector(".gmk-un").click();
  assert.deepEqual(gone, [UID_A]);
  assert.equal(row.querySelector(".gmk-un").textContent, "移出关注名单");
  assert.equal(G.guardRenderMarked(doc, box, {}, {}), 0);
  assert.equal(box.querySelector(".guard-empty").textContent, "还没有关注的人");
});

test("设置行里的名单标题：关注名单 · @主播（主播名是名字）", () => {
  const doc = makeDoc();
  const el = doc.createElement("div");
  G.guardFillHeading(doc, el, "luna_demo");
  assert.equal(el.textContent, "关注名单 · @luna_demo");
  assert.equal(el.querySelector("span").getAttribute("translate"), "no");
  G.guardFillHeading(doc, el, "");
  assert.equal(el.textContent, "关注名单");
});

test("界面文字里没有标签词、因果词（中文臂，第八条）", () => {
  const doc = makeDoc();
  const texts = [];
  ALL_CATS.forEach((c) => texts.push(G.guardCatLabel(c)));
  AMBER_CODES.forEach((c) => texts.push(G.guardAmberText(c, { n: 4, window_sec: 20 })));
  RV_CODES.forEach((c) => texts.push(G.guardReviewText(c)));
  STATUS_CODES.forEach((c) => texts.push(G.guardStateText({ status: c, lag_sec: 5, http: 401, wait_min: 3 })));
  const box = doc.createElement("div");
  G.guardRenderEvents(doc, box, { g1: event({ lvl: "amber", amb: "quote", rv: "agree" }) }, { marked: {} });
  texts.push(...uiTexts(box));
  for (const t of texts) assert.ok(t && !ZH_LABELS.test(t) && !ZH_CAUSAL.test(t), "写了原因或标签：" + t);
});
