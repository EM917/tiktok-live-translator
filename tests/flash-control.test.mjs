// 闪购浮层顶部那一排（开始、停止、继续，闪购助手 0.8.0 起）和闪购助手 0.8.2 的不续开原因、卡片序号：web/flash.js 的
// 纯函数和渲染函数（中文界面）。英文在 tests/flash-control.en.test.mjs；接线（app.js、index.html）在
// tests/flash-control-button.test.mjs。docs/flash-sale.md §9、decisions-step2、docs/i18n-style.md §3 K #167–#175。
//
// 钉住的几件事：
// - 和后端 app/flash_sale.py 一字不差的四条规则（等续开、排序、指令生效、开始后的过渡），吃同一张表
//   tests/fixtures/flash_sale/shared-rules.json（后端那一半在 tests/test_flash_sale.py）；
// - 那一排按状态显示什么、能不能点（连不上只留打开、版本认得出且低于 0.8.0 只留打开、页面没准备好开始不能点、
//   在运行出停止、确认框里点了取消的暂停出继续、在途和等生效的字、断线全禁用）；
// - 「开始」点两下：武装后 400 毫秒内的第二下不算、6 秒到点复原；
// - 结果的话：闪购助手 409 的代码照契约说是什么、认不出的原样，本程序这边的代码说走到了哪一步，30 秒没看到生效只写没看到；
// - 开始后的过渡（用户 10-27 定）：页面加载中、出错、不在商品列表、陈旧都不变色、不出红字，主句「正在开始…」；
//   30 秒后后端把 control 收掉，照原规则提醒；
// - 不续开原因的短说明、认不出的原样；标题前的卡片序号；同一档按序号排。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { SAMPLE, SHARED, NOW, makeFlashDoc, makeEls, product, st } from "./flash-fixtures.mjs";

const require = createRequire(import.meta.url);
const F = require("../web/flash.js");

// 同 tests/i18n_rules.py 的 ZH_LABELS / ZH_CAUSAL：flash.js 不在第八条家族里，但照同一个规矩写
const ZH_LABELS = /年龄|限流|封禁|封号|私密|观看限制|地区|风控|被挡|被墙|拦截|繁忙/;
const ZH_CAUSAL = /可能是|也可能|可能已|可能[^。；]*，或|多半|八成|应该是|大概是|估计是|似乎|好像|看起来是|因为|由于|导致|原因是|也许|或许|恐怕|不是[^。；]*问题/;
function observed(text) {
  assert.equal(typeof text, "string");
  assert.ok(!ZH_LABELS.test(text) && !ZH_CAUSAL.test(text), "猜了原因：" + text);
  return text;
}
const clone = (v) => JSON.parse(JSON.stringify(v));
const lines = (state, unread = 0) => F.flashLines(state, unread).map(F.flashLineText);
// 同一份样例，没有商品卡住、后端也没报提醒；版本换成带控制接口的
const calm = (extra = {}) => st(Object.assign({ attention: [], app_version: "0.9.0",
  products: SAMPLE.products.map((p) => Object.assign({}, p, { stuck: false })) }, extra));
const idle = (extra = {}) => calm(Object.assign({ running: false, paused_reason: "not_started" }, extra));
const view = (state, ui) => F.flashControlView(state, ui);
const shown = (v) => ["start", "stop", "resume"].filter((k) => v[k].shown);
const usable = (v) => ["start", "stop", "resume"].filter((k) => v[k].shown && !v[k].disabled);
const STARTING = "正在开始…（闪购助手在刷新页面）";

test("语言确实是中文（不然下面全是空转）", () => {
  assert.equal(require("../web/i18n.js").UI_LANG, "zh");
});

// ---- 和后端一字不差的四条规则（shared-rules.json） ----------------------------------------------------

test("等续开（同后端 stuck_candidate）：skip 不为 null 的不算，没有 skip 这个键当 null", () => {
  assert.ok(SHARED.waiting_renewal.length >= 10);
  for (const c of SHARED.waiting_renewal) {
    assert.equal(F.flashWaitingRenewal(clone(c.product), clone(c.state)), c.expect, c.why);
  }
});

test("排序（同后端 product_sort_key）：先按档，同一档按卡片序号，没有序号的在后，再相同保持原来的顺序", () => {
  assert.ok(SHARED.sort.length >= 2);
  for (const c of SHARED.sort) {
    assert.deepEqual(F.flashSortProducts(clone(c.products)).map((p) => p.id), c.ids, c.why);
    // 排过的再排一次不变（后端已排过，前端只是兜底）
    const once = F.flashSortProducts(clone(c.products));
    assert.deepEqual(F.flashSortProducts(once).map((p) => p.id), c.ids, c.why);
  }
  // 序号只认整数：字符串、小数、true 都当没有（同后端：int 而且不是 bool）
  const odd = [product("live", { id: "s", position: "1" }), product("live", { id: "f", position: 1.5 }),
               product("live", { id: "b", position: true }), product("live", { id: "n", position: 9 })];
  assert.deepEqual(F.flashSortProducts(odd).map((p) => p.id), ["n", "s", "f", "b"]);
  assert.equal(F.flashPosition({ position: 0 }), 0);
  assert.equal(F.flashPosition({ position: NaN }), null);
  assert.equal(F.flashPosition(null), null);
});

test("指令生效（同后端 control_done）", () => {
  assert.ok(SHARED.control_done.length >= 10);
  for (const c of SHARED.control_done) {
    assert.equal(F.flashControlDone(c.action, clone(c.state)), c.expect, c.why);
  }
  assert.equal(F.flashControlDone("start", null), false);
  assert.equal(F.flashControlDone("stop", "x"), false);
});

test("开始后的过渡（同后端 starting / attention）：按钮变不变色、主句里有没有要处理的，和后端算的 attention 一致", () => {
  assert.ok(SHARED.attention.length >= 10);
  for (const c of SHARED.attention) {
    const state = Object.assign(clone(c.state), { attention: c.attention, alerts: [] });
    assert.equal(F.flashStarting(state), c.starting, c.why);
    const attn = c.attention.length > 0;
    assert.equal(F.flashNeedsAttention(state, 0), attn, c.why);
    assert.equal(F.flashButtonState(state, 0).attn, attn, c.why);
    const got = F.flashLines(state, 0);
    assert.equal(got.some((l) => l.attn), attn, c.why + "：" + got.map(F.flashLineText).join(" | "));
    if (c.starting && state.conn === "ok") {
      const want = state.control.action === "resume" ? "正在继续…" : STARTING;
      assert.ok(got.some((l) => l.text === want && !l.attn), c.why + "：没有过渡那一句");
      assert.ok(!got.some((l) => l.text === "闪购助手没有更新状态。"), c.why + "：过渡里不写陈旧");
    }
  }
  for (const bad of [null, {}, { control: [] }, { control: { action: "start", phase: "sending" } },
                     { control: { action: "stop", phase: "waiting" } }, { control: { action: "pause", phase: "waiting" } }]) {
    assert.equal(F.flashStarting(bad), false, JSON.stringify(bad));
  }
});

// ---- 开始后的过渡：页面这一边看到的 30 秒 ---------------------------------------------------------

test("过渡 30 秒的边：29.9 秒（control 还在等）不变色、主句「正在开始…」；30 秒后端收掉 control、另发 not_seen，照原规则提醒", () => {
  // 开始被接受以后页面刷新：出错、15 秒没更新，还没开始运行。后端的 attention 在等生效时是 []，收掉 control 后是 [stale, page]
  const base = calm({ page: "error", stale: true, updated_ms: new Date(2026, 9, 27, 21, 36, 7).getTime(), running: false,
                      paused_reason: null, products: [] });
  const before = Object.assign(clone(base), { control: { action: "start", phase: "waiting" }, attention: [] });
  assert.equal(F.flashNeedsAttention(before, 0), false);
  // 胶囊和顶栏按钮的 title 也不写「未开始」（读成「没开起来」又去点一次），写和按钮上同一句「正在开始…」，颜色不变
  assert.deepEqual(F.flashChip(before, 0), { tone: "idle", text: "正在开始…" });
  assert.equal(F.flashButtonState(before, 0).title, "闪购助手 · 正在开始…");
  assert.deepEqual(F.flashChip(Object.assign(clone(before), { control: { action: "resume", phase: "waiting" } }), 0),
                   { tone: "idle", text: "正在继续…" });
  for (const control of [{ action: "start", phase: "sending" }, { action: "stop", phase: "waiting" }, null]) {
    const s = Object.assign(clone(before), { control, page: "ok", stale: false });
    assert.deepEqual(F.flashChip(s, 0), { tone: "idle", text: "未开始" }, "只有开始、继续在等生效时换字：" + JSON.stringify(control));
  }
  assert.deepEqual(lines(before), [STARTING], "只有过渡那一句：没有红字，也没有「还没开始运行」");
  assert.ok(F.flashLines(before, 0).every((l) => !l.attn));
  const doc = makeFlashDoc();
  const els = makeEls(doc);
  F.renderFlashPanel(doc, els, before, { now: NOW });
  assert.equal(els.headline.textContent, STARTING);
  assert.ok(!els.headline.classList.contains("attn"), "主句不是橙色的");
  // 30 秒：后端把 control 收掉、attention 照原规则重算
  const after = Object.assign(clone(base), { control: null, attention: ["stale", "page"] });
  assert.equal(F.flashNeedsAttention(after, 0), true);
  assert.deepEqual(lines(after), ["闪购助手没有更新状态。", "上次更新 21:36:07。", "页面没有回应，或者读取出错。"]);
  F.renderFlashPanel(doc, els, after, { now: NOW });
  assert.ok(els.headline.classList.contains("attn"));
  assert.equal(F.flashResultLine({ type: "flash_control_result", action: "start", ok: false, error: "not_seen" }).text,
               "30 秒内没看到闪购助手开始运行。");
  // 页面加载中、不在商品列表（列表回来之前），过渡里同样不写
  for (const page of ["loading", "no_list"]) {
    const s = Object.assign(clone(before), { page, stale: false, running: page === "no_list" });
    assert.deepEqual(lines(s), [STARTING], page);
  }
});

test("开始生效了、列表还没回来（running 已经是 true，后端还没收掉 control）：停止能点，主句照旧是过渡那一句、不变色", () => {
  // 后端 control_settled：刷新完闪购助手接着运行，running 先变 true，页面稳下来以前 control 照旧是 start waiting
  for (const page of ["no_list", "loading"]) {
    const s = calm({ page, stale: false, running: true, paused_reason: null, products: [],
                     control: { action: "start", phase: "waiting" } });
    const v = view(s, {});
    assert.deepEqual(shown(v), ["stop"], page);
    assert.deepEqual(usable(v), ["stop"], page + "：照「running 为 true」那一行，停是安全的方向");
    assert.equal(v.stop.text, "停止");
    assert.equal(v.note, null);
    assert.deepEqual(lines(s), [STARTING], page);
    assert.ok(F.flashLines(s, 0).every((l) => !l.attn), page);
    assert.equal(F.flashNeedsAttention(s, 0), false, page);
    assert.deepEqual(F.flashChip(s, 0), { tone: "idle", text: "正在开始…" }, page);
  }
});

test("过渡只认开始、继续被接受以后：在途（sending）、停止在等生效、没有指令，页面出错照常变色、写红字", () => {
  const base = calm({ page: "error", stale: false, running: false, paused_reason: null, products: [], attention: ["page"] });
  for (const control of [{ action: "start", phase: "sending" }, { action: "stop", phase: "waiting" }, null]) {
    const s = Object.assign(clone(base), { control });
    assert.equal(F.flashStarting(s), false);
    assert.deepEqual(F.flashLines(s, 0)[0], { text: "页面没有回应，或者读取出错。", attn: true }, JSON.stringify(control));
  }
});

test("过渡里要人处理的照常排在前面（登录、验证码、暂停、卡住、未读），过渡那一句跟在后面；继续写「正在继续…」", () => {
  const login = calm({ page: "login_required", running: false, paused_reason: null, products: [], attention: ["page"],
                       control: { action: "start", phase: "waiting" } });
  assert.deepEqual(lines(login), ["专用 Chrome 停在 TikTok 登录页，登录后才能继续。", STARTING]);
  const stuck = calm({ attention: ["stuck"], control: { action: "start", phase: "waiting" },
                       products: [product("stopped", { stuck: true })] });
  assert.deepEqual(lines(stuck, 1), ["1 个商品停了超过 1 分钟。", "最近记录里有 1 条新的问题。", STARTING]);
  const resume = calm({ page: "loading", stale: true, paused_reason: "user_interaction", attention: ["paused"],
                        control: { action: "resume", phase: "waiting" } });
  assert.deepEqual(lines(resume), ["已暂停：确认框里点了取消。", "正在继续…"]);
  assert.equal(F.flashStartingText("start"), STARTING);
  assert.equal(F.flashStartingText("resume"), "正在继续…");
  for (const l of [...lines(login), ...lines(resume)]) observed(l);
});

test("确认框里点了取消的暂停：浮层顶部有「继续」时主句不再叫人去闪购助手的面板上点；只剩「打开」时照旧指过去", () => {
  const PAUSED = "已暂停：确认框里点了取消。";
  const ELSEWHERE = "已暂停：确认框里点了取消。要在闪购助手的面板上点继续。";
  const paused = calm({ paused_reason: "user_interaction", attention: ["paused"] });
  assert.equal(view(paused, {}).resume.shown, true);
  assert.deepEqual(lines(paused), [PAUSED]);
  // 点了「继续」、等生效的那几秒：同一句，后面跟「正在继续…」，不会同时写着「要在闪购助手的面板上点继续」
  assert.deepEqual(lines(Object.assign(clone(paused), { control: { action: "resume", phase: "waiting" } })),
                   [PAUSED, "正在继续…"]);
  assert.equal(F.flashLines(paused, 0)[0].attn, true, "照旧是要处理的那一句（橙色）");
  // 版本认得出且低于 0.8.0：这一排只剩「打开」，主句照旧指到闪购助手的面板
  const old = calm({ paused_reason: "user_interaction", attention: ["paused"], app_version: "0.7.3" });
  assert.equal(view(old, {}).resume.shown, false);
  assert.deepEqual(lines(old), [ELSEWHERE]);
  assert.equal(F.flashPausedText("user_interaction"), ELSEWHERE);
  assert.equal(F.flashPausedText("user_interaction", true), PAUSED);
  assert.equal(F.flashPausedText("captcha", true), "出现验证码，已暂停。", "别的暂停原因不受影响");
  for (const l of [...lines(paused), ...lines(old)]) observed(l);
});

// ---- 那一排：按状态显示什么、能不能点 ----------------------------------------------------------------

test("连不上、读不出、正在查看、没有状态：开始、停止、继续一颗都不出，也不写说明", () => {
  for (const state of [null, undefined, { conn: "checking" }, { conn: "unreachable" }, { conn: "bad_response" },
                       { conn: "bad_response", why: "no_answer" }]) {
    const v = view(state, {});
    assert.deepEqual(shown(v), [], JSON.stringify(state));
    assert.equal(v.any, false);
    assert.equal(v.note, null);
  }
});

test("版本门槛：认得出且低于 0.8.0 只留「打开」，旁边写「0.8.0 起才能……」；认不出的照常出按钮", () => {
  assert.deepEqual(F.FLASH_CONTROL_SINCE, [0, 8, 0]);
  const gate = "闪购助手 0.8.0 起才能从这里开始、停止。";
  // 四段的照样比（闪购助手真的发四段版本号，它的说明里有 0.9.2.1）：0.7.0.1、0.7.9.9 都比 0.8.0 旧
  for (const v of ["0.7.2", "0.7.10", "0.7", "0.1.0-beta", "0.7.99+build", "0.7.0.1", "0.7.9.9", "0.7.999999.999999",
                   "0.7.9.9-rc1"]) {
    assert.equal(F.flashControlTooOld({ app_version: v }), true, v);
    const got = view(idle({ app_version: v }), {});
    assert.deepEqual(shown(got), [], v);
    assert.equal(got.any, false);
    assert.equal(observed(got.note.text), gate);
  }
  // 认得出且不旧的照常出；认不出的（带 v、带 x、五段、末尾一个点、段太长）也照常出，回来 404 / 405 再说
  for (const v of ["0.8.0", "0.8", "0.8.2", "0.9.0", "0.9.2", "0.9.2.1", "0.8.0.0", "0.8.0.1", "0.10.0", "1.0.0",
                   "0.8.0-rc1", "0.9.2.1-rc1", "v0.7.0", "0.7.x", "0.7.0.x", "0.7.0.1.5", "0.7.9.9.", "0.7.0.1234567",
                   "", null, 7, "abc"]) {
    assert.equal(F.flashControlTooOld({ app_version: v }), false, String(v));
    assert.deepEqual(shown(view(idle({ app_version: v }), {})), ["start"], String(v));
  }
  assert.deepEqual(F.flashVersionParts("0.8"), [0, 8, 0, 0]);
  assert.deepEqual(F.flashVersionParts("0.8.0"), [0, 8, 0, 0]);
  assert.deepEqual(F.flashVersionParts("12.3.45-x"), [12, 3, 45, 0]);
  assert.deepEqual(F.flashVersionParts("0.9.2.1"), [0, 9, 2, 1]);
  assert.deepEqual(F.flashVersionParts("0.9.2.1-rc1"), [0, 9, 2, 1]);
  assert.deepEqual(F.flashVersionParts("0.8.0.0"), [0, 8, 0, 0]);
  for (const v of ["0.8.x", "v0.9.2.1", "0.9.2.x", "0.9.2.1.5", "0.9.2.1.", "0.9.2.1234567", "0.9..1", "9"]) {
    assert.equal(F.flashVersionParts(v), null, v);
  }
  // 第四段也比，没写的段当 0
  const parts = F.flashVersionParts;
  assert.equal(F.flashVersionOlder(parts("0.9.2"), parts("0.9.2.1")), true, "0.9.2 比 0.9.2.1 旧");
  assert.equal(F.flashVersionOlder(parts("0.9.2.1"), parts("0.9.2")), false);
  assert.equal(F.flashVersionOlder(parts("0.9.2.1"), parts("0.9.2.1")), false);
  assert.equal(F.flashVersionOlder(parts("0.9.2.1"), parts("0.9.2.2")), true);
  assert.equal(F.flashVersionOlder(parts("0.9.2.9"), parts("0.9.3")), true);
  assert.equal(F.flashVersionOlder(parts("0.7.9.9"), F.FLASH_CONTROL_SINCE), true, "0.7.9.9 仍比 0.8.0 旧");
  assert.equal(F.flashVersionOlder(parts("0.8.0.0"), F.FLASH_CONTROL_SINCE), false, "0.8.0.0 就是 0.8.0");
  assert.equal(F.flashVersionOlder(parts("0.8"), F.FLASH_CONTROL_SINCE), false);
});

test("没在运行：出「开始」（页面 ok、不在商品列表时能点）；武装后写「再点一次开始」", () => {
  for (const page of ["ok", "no_list"]) {
    const v = view(idle({ page }), {});
    assert.deepEqual(shown(v), ["start"]);
    assert.deepEqual(usable(v), ["start"]);
    assert.equal(v.start.text, "开始");
    assert.equal(v.note, null);
    assert.equal(v.any, true);
  }
  assert.equal(view(idle(), { armed: true }).start.text, "再点一次开始");
  assert.equal(view(calm({ running: null, paused_reason: null }), {}).start.shown, true, "running 读不出也当没在运行");
});

test("页面没准备好（no_page、login_required、captcha、loading、error、认不出的）：开始不能点，旁边照 page 说一句；武装的字不出", () => {
  // decisions-step2（用户 10-27 定）：旁边一句说明为什么，照 /status 的 page 文案——和主句里那句是同一句
  const want = {
    no_page: { text: "专用 Chrome 里没有打开商品后台（LIVE Manager）。" },
    login_required: { text: "专用 Chrome 停在 TikTok 登录页，登录后才能继续。" },
    captcha: { text: "页面上出现了验证码，要人去处理。" },
    loading: { text: "商品后台正在打开…" },
    error: { text: "页面没有回应，或者读取出错。" },
    maintenance: { text: "页面状态：", code: "maintenance" },          // 认不出的代码原样（translate="no"）
    "": { text: "页面准备好以后才能从这里开始、继续。" },              // 连 page 都没有
  };
  for (const [page, note] of Object.entries(want)) {
    const state = idle({ page: page || null, paused_reason: null });
    const v = view(state, { armed: true });
    assert.deepEqual(shown(v), ["start"], page);
    assert.deepEqual(usable(v), [], page);
    assert.equal(v.start.text, "开始");
    assert.deepEqual(v.note, note, page);
    observed(v.note.text);
    if (page && page !== "maintenance") assert.ok(lines(state).includes(note.text), "和主句里那句同一句：" + page);
  }
  // 确认框里点了取消的暂停、页面没准备好：继续不能点，旁边同样照 page 说
  const v = view(calm({ paused_reason: "user_interaction", page: "loading" }), {});
  assert.deepEqual(shown(v), ["stop", "resume"]);
  assert.deepEqual(usable(v), ["stop"]);
  assert.deepEqual(v.note, { text: "商品后台正在打开…" });
});

test("在运行：出「停止」；确认框里点了取消的暂停另出「继续」；验证码暂停时停止照样能点", () => {
  let v = view(calm(), {});
  assert.deepEqual(shown(v), ["stop"]);
  assert.deepEqual(usable(v), ["stop"]);
  assert.equal(v.stop.text, "停止");
  v = view(calm({ paused_reason: "user_interaction" }), {});
  assert.deepEqual(shown(v), ["stop", "resume"]);
  assert.deepEqual(usable(v), ["stop", "resume"]);
  assert.equal(v.resume.text, "继续");
  v = view(calm({ page: "captcha", paused_reason: "captcha" }), {});
  assert.deepEqual(usable(v), ["stop"], "契约：验证码只拦开始和继续");
  assert.equal(v.note, null);
  v = view(calm({ paused_reason: "error" }), {});
  assert.deepEqual(shown(v), ["stop"], "别的暂停原因不出继续（继续只对应确认框里点了取消）");
});

test("确认框弹着（awaiting_approval）：停止不能点，旁边说一句", () => {
  const v = view(calm({ paused_reason: "awaiting_approval" }), {});
  assert.deepEqual(shown(v), ["stop"]);
  assert.deepEqual(usable(v), []);
  assert.equal(observed(v.note.text), "确认框处理完以后才能从这里停止。");
});

test("在途：本页刚点的（pending）或状态里 phase=sending，三颗都不能点，按下的那颗写「正在发送…」，不写说明", () => {
  let v = view(calm({ paused_reason: "user_interaction" }), { pending: "stop" });
  assert.deepEqual(usable(v), []);
  assert.equal(v.stop.text, "正在发送…");
  assert.equal(v.resume.text, "继续");
  v = view(idle({ control: { action: "start", phase: "sending" } }), { armed: true });
  assert.deepEqual(usable(v), []);
  assert.equal(v.start.text, "正在发送…");
  v = view(idle({ page: "loading", paused_reason: null, control: { action: "start", phase: "sending" } }), {});
  assert.equal(v.note, null, "在途时按钮旁边不写页面那句");
  v = view(calm({ paused_reason: "awaiting_approval" }), { pending: "stop" });
  assert.equal(v.note, null);
  assert.deepEqual(usable(view(calm(), { pending: "bogus" })), ["stop"], "认不出的 pending 不算");
});

test("等生效：按下的那颗写「正在开始…」「正在停止…」「正在继续…」、不能点；开始、继续也不能点，停止照样能点", () => {
  let v = view(idle({ page: "loading", paused_reason: null, control: { action: "start", phase: "waiting" } }), { armed: true });
  assert.deepEqual(shown(v), ["start"]);
  assert.deepEqual(usable(v), []);
  assert.equal(v.start.text, "正在开始…");
  assert.equal(v.note, null, "过渡里按钮旁边不写页面那句，免得和「正在开始…」打架");
  v = view(calm({ control: { action: "stop", phase: "waiting" } }), {});
  assert.deepEqual(usable(v), []);
  assert.equal(v.stop.text, "正在停止…");
  v = view(calm({ paused_reason: "user_interaction", control: { action: "resume", phase: "waiting" } }), {});
  assert.equal(v.resume.text, "正在继续…");
  assert.deepEqual(usable(v), ["stop"], "停是安全的方向：等继续生效时照样能停");
  // 状态已经显示生效了（后端下一跳就收掉 control）：照生效后的样子画
  v = view(calm({ control: { action: "start", phase: "waiting" } }), {});
  assert.deepEqual(usable(v), ["stop"]);
  v = view(idle({ control: { action: "stop", phase: "waiting" } }), {});
  assert.deepEqual(usable(v), ["start"]);
  assert.equal(v.start.text, "开始");
});

test("和本地服务断开：三颗都不能点（点了也发不出去）；形状不对的 control 当没有", () => {
  const v = view(calm({ paused_reason: "user_interaction" }), { offline: true });
  assert.deepEqual(shown(v), ["stop", "resume"]);
  assert.deepEqual(usable(v), []);
  assert.equal(v.stop.text, "停止");
  for (const control of [[], "start", { action: "pause", phase: "waiting" }, { action: "start", phase: "done" }, { action: "start" }]) {
    assert.equal(F.flashActiveControl(idle({ control })), null, JSON.stringify(control));
    assert.deepEqual(usable(view(idle({ control }), {})), ["start"], JSON.stringify(control));
  }
});

test("「开始」点两下：第一下武装；武装后 400 毫秒内的第二下不算；400 毫秒到 6 秒之间才发；满 6 秒（或时钟往回走）重新武装", () => {
  assert.equal(F.FLASH_START_CONFIRM_MS, 6000);
  assert.equal(F.FLASH_START_GUARD_MS, 400);
  const t = 1759000000000;
  assert.equal(F.flashStartClick(null, t), "arm");
  assert.equal(F.flashStartClick(undefined, t), "arm");
  assert.equal(F.flashStartClick(t, t), "ignore", "双击一次：第二下几乎同时到");
  assert.equal(F.flashStartClick(t, t + 399), "ignore");
  assert.equal(F.flashStartClick(t, t + 400), "send");
  assert.equal(F.flashStartClick(t, t + 5999), "send");
  assert.equal(F.flashStartClick(t, t + 6000), "arm");
  assert.equal(F.flashStartClick(t, t - 1), "arm");
  assert.equal(F.flashStartClick(t, NaN), "arm");
});

// ---- 结果的话 ---------------------------------------------------------------------------------

const result = (error, action = "start") => F.flashResultLine({ type: "flash_control_result", action, ok: false, error });

test("接受了不写（状态自己会说）；本程序这边的代码说走到了哪一步", () => {
  assert.equal(F.flashResultLine({ type: "flash_control_result", action: "start", ok: true, error: null }), null);
  assert.equal(F.flashResultLine(null), null);
  const want = {
    no_status: "没有发出：刚才没查到闪购助手的状态。",
    no_token: "没有发出：读不到闪购助手的控制密钥。确认它是 0.8.0 以上，重开它以后再试。",
    unreachable: "没有发出：连不上 127.0.0.1:9334。",
    no_answer: "发出了，但 127.0.0.1:9334 在 2 秒内没有回应。看看状态有没有变。",
    bad_reply: "发出了，但回话读不出来。看看状态有没有变。",
    no_control: "这个版本的闪购助手没有控制接口。",
    failed: "没能处理这个指令：本程序出错，详情在终端里。",
  };
  for (const [code, text] of Object.entries(want)) {
    assert.deepEqual(result(code), { text }, code);
    observed(text);
  }
  assert.deepEqual(Object.keys(want).concat(["not_seen"]).sort(), F.FLASH_OWN_ERRORS.slice().sort(), "每个本程序的代码都有话说");
});

test("等满 30 秒没看到生效（not_seen）：按动作各一句，只写没看到；动作认不出就原样显示代码", () => {
  assert.deepEqual(result("not_seen", "start"), { text: "30 秒内没看到闪购助手开始运行。" });
  assert.deepEqual(result("not_seen", "stop"), { text: "30 秒内没看到闪购助手停下。" });
  assert.deepEqual(result("not_seen", "resume"), { text: "30 秒内没看到闪购助手继续运行。" });
  assert.deepEqual(result("not_seen", "pause"), { text: "闪购助手没有接受：", code: "not_seen" });
  for (const a of ["start", "stop", "resume"]) observed(result("not_seen", a).text);
});

test("闪购助手 409 的代码：「闪购助手没有接受：」加契约里这个代码的意思；认不出的原样（不猜原因）", () => {
  const want = {
    no_page: "专用 Chrome 里没有打开商品后台（LIVE Manager）。",
    login_required: "专用 Chrome 停在 TikTok 登录页，登录后才能继续。",
    captcha: "页面上出现了验证码，要人去处理。",
    awaiting_approval: "专用 Chrome 里弹出了确认框，核对后点确定或取消。",
    loading: "页面正在加载，稍等再试。",
    already_running: "已经在运行了。",
    paused: "暂停着，要点继续，不是开始。",
    not_running: "还没开始运行。",
    not_paused: "没有暂停。",
    busy: "正忙，稍等再试。",
    no_response: "页面没有回应，这个指令不会补做。",
    error: "页面报告出错。",
  };
  for (const [code, text] of Object.entries(want)) {
    assert.deepEqual(result(code), { text: "闪购助手没有接受：" + text }, code);
    observed(result(code).text);
  }
  assert.deepEqual(result("cooldown_x"), { text: "闪购助手没有接受：", code: "cooldown_x" });
  assert.deepEqual(result("x".repeat(60)), { text: "闪购助手没有接受：", code: "x".repeat(40) }, "代码截到 40 个字符");
  assert.deepEqual(result(null), { text: "闪购助手没有接受：", code: "?" });
  assert.deepEqual(result(403), { text: "闪购助手没有接受：", code: "?" });
});

test("别的 HTTP 状态：写 127.0.0.1:9334 回了什么（9334 上回话的不一定是闪购助手）", () => {
  assert.deepEqual(result("http_403"), { text: "没有接受：127.0.0.1:9334 回了 HTTP 403。" });
  assert.deepEqual(result("http_302"), { text: "没有接受：127.0.0.1:9334 回了 HTTP 302。" });
  assert.deepEqual(result("http_409"), { text: "没有接受：127.0.0.1:9334 回了 HTTP 409。" });
  assert.deepEqual(result("http_4033"), { text: "闪购助手没有接受：", code: "http_4033" });
});

// ---- 不续开原因、卡片序号 ---------------------------------------------------------------------

test("不续开原因：七个代码各一句（0.8.2 的五个是闪购助手会话给的，next_to_pin 是 0.9.2 起的，top_n 是 0.9.2.2 起的），认不出的返回 null", () => {
  const want = { slot_1: "1 号位，不续开", top_n: "排在最前面，不续开", next_to_pin: "下一个要置顶，不续开", no_stock: "没有库存，不续开",
                 max_renewals: "续开次数到上限", auto_start_off: "没开自动开始", no_position: "读不到序号，先不处理" };
  for (const [code, text] of Object.entries(want)) assert.equal(observed(F.flashSkipText(code)), text, code);
  for (const code of ["", "brand_new_reason", null, 1]) assert.equal(F.flashSkipText(code), null);
});

test("商品一行：不续开的原因写在细节最前面（认不出的写「不续开：」加原样的代码），不说「等续开」；序号单独给", () => {
  const running = { running: true, paused_reason: null };
  const slot = F.flashProductParts(product("stopped", { skip: "slot_1", position: 1, stock_left: 3 }), running, NOW);
  assert.deepEqual(slot.detail, ["1 号位，不续开", "库存 3"]);
  assert.equal(slot.position, 1);
  const odd = F.flashProductParts(product("pending", { skip: "brand_new_reason" }), running, NOW);
  assert.deepEqual(odd.detail, [{ text: "不续开：", code: "brand_new_reason" }]);
  assert.equal(odd.position, null);
  const plain = F.flashProductParts(product("stopped"), running, NOW);
  assert.deepEqual(plain.detail, ["等闪购助手续开"], "没有 skip 的照旧");
  const off = F.flashProductParts(product("stopped", { enabled: false, skip: "no_stock" }), running, NOW);
  assert.deepEqual(off.detail, ["没有库存，不续开", "不自动续开"]);
  assert.equal(F.flashProductParts(product("live", { position: -1 }), running, NOW).position, null);
  assert.equal(F.flashProductParts(product("live", { position: 0 }), running, NOW).position, 0);
});

test("整张表：标题前写 #序号（不跟标题一起被省略号吃掉），认不出的不续开原因放进 translate=\"no\"，同一档按序号排", () => {
  const doc = makeFlashDoc();
  const els = makeEls(doc);
  const state = calm({ products: [
    product("live", { id: "a", title: "Alpha (Fictional)", position: 12 }),
    product("live", { id: "b", title: "Beta (Fictional)", position: null }),
    product("live", { id: "c", title: "Gamma (Fictional)", position: 3, skip: "max_renewals" }),
    product("stopped", { id: "d", title: "Delta (Fictional)", position: 1, skip: "slot_1" }),
    product("pending", { id: "e", title: "Epsilon (Fictional)", position: 7, skip: "brand_new_reason" }),
  ] });
  F.renderFlashPanel(doc, els, state, { now: NOW });
  const rows = els.products.children;
  assert.deepEqual(rows.map((li) => li.querySelector(".flash-title").textContent),
                   ["Epsilon (Fictional)", "Gamma (Fictional)", "Alpha (Fictional)", "Beta (Fictional)", "Delta (Fictional)"]);
  const pos = rows.map((li) => li.querySelector(".flash-pos"));
  assert.deepEqual(pos.map((el) => el && el.textContent), ["#7", "#3", "#12", null, "#1"]);
  const top = rows[0].querySelector(".flash-row-top");
  assert.equal(top.children[0], pos[0], "序号在标题前面");
  assert.ok(!top.children[1].children.includes(pos[0]), "序号不在 .flash-title（会被省略号截掉的那一截）里");
  assert.equal(rows[1].querySelector(".flash-row-detail").textContent, "续开次数到上限");
  assert.equal(rows[4].querySelector(".flash-row-detail").textContent, "1 号位，不续开");
  const detail = rows[0].querySelector(".flash-row-detail");
  assert.equal(detail.textContent, "不续开：brand_new_reason");
  const code = detail.querySelector(".flash-code");
  assert.equal(code.textContent, "brand_new_reason");
  assert.equal(code.getAttribute("translate"), "no");
  assert.equal(els.headline.textContent, "", "故意不续开的已停止不算卡住，没有主句");
});

// ---- 渲染那一排、那一句 ---------------------------------------------------------------------------

function ctlEls(doc) {
  const els = {};
  for (const k of ["start", "stop", "resume", "note", "desc"]) els[k] = doc.createElement(k === "note" || k === "desc" ? "p" : "button");
  for (const k of ["start", "stop", "resume", "note"]) els[k].classList.add("hidden");
  return els;
}

test("renderFlashControls：显隐、禁用、按钮上的字、旁边那句、最底下那句随那一排换", () => {
  const doc = makeFlashDoc();
  const els = ctlEls(doc);
  F.renderFlashControls(doc, els, view(calm({ paused_reason: "user_interaction" }), { pending: "resume" }));
  assert.ok(els.start.classList.contains("hidden") && els.start.disabled === true, "藏起来的也禁用（键盘碰不到）");
  assert.ok(!els.stop.classList.contains("hidden") && els.stop.disabled === true);
  assert.equal(els.resume.textContent, "正在发送…");
  assert.ok(els.note.classList.contains("hidden"));
  assert.equal(els.desc.textContent, "这里只能开始、停止和继续，其他操作请到闪购助手里。");
  F.renderFlashControls(doc, els, view(idle({ app_version: "0.7.3" }), {}));
  assert.ok(["start", "stop", "resume"].every((k) => els[k].classList.contains("hidden")));
  assert.ok(!els.note.classList.contains("hidden"));
  assert.equal(els.note.textContent, "闪购助手 0.8.0 起才能从这里开始、停止。");
  assert.equal(els.desc.textContent, "这里只显示状态。要操作，请到闪购助手里。", "一颗都没有：换回第一步那句，不和版本句打架");
  F.renderFlashControls(doc, els, view({ conn: "unreachable" }, {}));
  assert.ok(els.note.classList.contains("hidden"));
  assert.equal(els.desc.textContent, "这里只显示状态。要操作，请到闪购助手里。");
  F.renderFlashControls(doc, els, view(idle(), { armed: true }));
  assert.ok(!els.start.classList.contains("hidden") && els.start.disabled === false);
  assert.equal(els.start.textContent, "再点一次开始");
  assert.equal(els.desc.textContent, "这里只能开始、停止和继续，其他操作请到闪购助手里。");
});

test("renderFlashNote：认不出的代码放进 translate=\"no\" 的 span；纯文字也收", () => {
  const doc = makeFlashDoc();
  const el = doc.createElement("p");
  F.renderFlashNote(doc, el, result("cooldown_x"));
  assert.equal(el.textContent, "闪购助手没有接受：cooldown_x");
  assert.equal(el.querySelector(".flash-code").getAttribute("translate"), "no");
  F.renderFlashNote(doc, el, "与本地服务断开，正在重连——稍候再试。");
  assert.equal(el.textContent, "与本地服务断开，正在重连——稍候再试。");
  assert.equal(el.children.length, 0);
  const evil = '<img src=x onerror="alert(1)">';
  F.renderFlashNote(doc, el, result(evil));
  assert.equal(el.querySelector("img"), null, "代码是文字，不是 HTML");
});
