// 顶栏「闪购」浮层的前端（web/flash.js，中文界面）。纯函数 + 渲染函数（假 DOM，同 tests/guard-dom.mjs）。
// 英文在 tests/flash.en.test.mjs；接线（app.js、index.html）在 tests/flash-button.test.mjs。
//
// 钉住的几件事：
// - 页面、暂停、商品状态都按闪购助手接口契约的代码映射；认不出的代码原样显示（放进 translate="no"），不给通用文案；
// - 只写观察，不写原因（同第八条的中文规矩）；
// - 排序：要人看的在前（卡住的、等续开的已停止、待开始、看不准），同一档保持原来的先后；
// - 未读：error 级、❌ 开头的 warn 级，按 (at_ms, text) 和「已看过」比；打开浮层才记成已看；
// - 先列 40 行，多的写「还有 N 个」；倒计时、本机时刻的写法；
// - 商品标题、记录正文是数据：标题标 translate="no"，正文带 class i18n-data；一律 textContent，不用 innerHTML。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { SAMPLE, NOW, makeFlashDoc, makeEls, product, st } from "./flash-fixtures.mjs";

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

const visible = (el) => !el.classList.contains("hidden");

test("语言确实是中文（不然下面全是空转）", () => {
  assert.equal(require("../web/i18n.js").UI_LANG, "zh");
});

// ---- 代码 → 文案 -----------------------------------------------------------------------------------

test("页面状态：契约的七个代码都有话说（ok 不说话），认不出的返回 null", () => {
  assert.equal(F.flashPageText("ok"), "");
  const want = {
    no_page: "专用 Chrome 里没有打开商品后台（LIVE Manager）。",
    login_required: "专用 Chrome 停在 TikTok 登录页，登录后才能继续。",
    loading: "商品后台正在打开…",
    no_list: "商品后台没在 Buy now 列表上。",
    captcha: "页面上出现了验证码，要人去处理。",
    error: "页面没有回应，或者读取出错。",
  };
  for (const [code, text] of Object.entries(want)) assert.equal(observed(F.flashPageText(code)), text, code);
  for (const code of ["", "new_code", null, undefined, 3]) assert.equal(F.flashPageText(code), null);
});

test("暂停原因：五个代码，认不出的返回 null", () => {
  const want = {
    not_started: "闪购助手开着，但还没开始运行。",
    awaiting_approval: "专用 Chrome 里弹出了确认框，核对后点确定或取消。",
    user_interaction: "已暂停：确认框里点了取消。要在闪购助手的面板上点继续。",
    captcha: "出现验证码，已暂停。",
    error: "连续失败太多次，已暂停。",
  };
  for (const [code, text] of Object.entries(want)) assert.equal(observed(F.flashPausedText(code)), text, code);
  assert.equal(F.flashPausedText("cooldown"), null);
});

test("商品状态：七个代码，已停止、未开始与手机页同一句（同一句中文只许一种英文）", () => {
  const want = { live: "进行中", between_rounds: "两轮之间", stopped: "已停止", ended_by_user: "已手动结束",
                 pending: "待开始", unknown: "暂时看不准", no_sale: "没有闪购" };
  for (const [code, text] of Object.entries(want)) assert.equal(F.flashStateText(code), text, code);
  assert.equal(F.flashStateText("paused_by_tiktok"), null);
  assert.equal(F.flashStateTone("stopped"), "stop");
  assert.equal(F.flashStateTone("live"), "live");
  assert.equal(F.flashStateTone("whatever"), "other");
});

test("记录级别：error 八角、warn 三角、info ⓘ；认不出的按 info", () => {
  assert.deepEqual(F.flashLevel("error"), { level: "error", icon: "octagon", label: "要处理" });
  assert.deepEqual(F.flashLevel("warn"), { level: "warn", icon: "warn", label: "提醒" });
  assert.deepEqual(F.flashLevel("info"), { level: "info", icon: "info", label: "说明" });
  assert.deepEqual(F.flashLevel("debug"), F.flashLevel("info"));
});

// ---- 时间 -------------------------------------------------------------------------------------

test("倒计时：不到 1 小时 m:ss，到 1 小时 h:mm:ss，已经过了、不是数都写 0:00；不足 1 秒往上进", () => {
  assert.equal(F.flashCountdown(59000), "0:59");
  assert.equal(F.flashCountdown(59001), "1:00");
  assert.equal(F.flashCountdown(261000), "4:21");
  assert.equal(F.flashCountdown(3599000), "59:59");
  assert.equal(F.flashCountdown(3600000), "1:00:00");
  assert.equal(F.flashCountdown(3600000 + 61000), "1:01:01");
  assert.equal(F.flashCountdown(-5000), "0:00");
  assert.equal(F.flashCountdown(NaN), "0:00");
  assert.equal(F.flashCountdownText("ends", NOW + 261000, NOW), "还剩 4:21");
  assert.equal(F.flashCountdownText("next", NOW + 61000, NOW), "1:01 后开始下一轮");
});

test("本机时刻 HH:MM:SS（24 小时制、补零）；null、不是数写 --:--:--", () => {
  const ms = new Date(2026, 9, 27, 9, 5, 7).getTime();     // 本机时区的 09:05:07
  assert.equal(F.flashClock(ms), "09:05:07");
  assert.equal(F.flashClock(new Date(2026, 9, 27, 23, 59, 59).getTime()), "23:59:59");
  assert.equal(F.flashClock(null), "--:--:--");
  assert.equal(F.flashClock("x"), "--:--:--");
});

// ---- 未读 -------------------------------------------------------------------------------------

test("要看的记录：error 级，或 ❌ 开头的 warn 级（前面有空白也算）；info 带 ❌、warn 带 📦 都不算", () => {
  assert.ok(F.flashIsProblem({ level: "error", text: "x" }));
  assert.ok(F.flashIsProblem({ level: "warn", text: "❌ Toner Mist 250ml (Fictional): 续开后出现提示" }));
  assert.ok(F.flashIsProblem({ level: "warn", text: "  ❌ x" }));
  assert.ok(!F.flashIsProblem({ level: "warn", text: "📦 Calm Gel Mask x4 (Fictional): 1 个规格库存不足 5" }));
  assert.ok(!F.flashIsProblem({ level: "info", text: "❌ x" }));
  assert.ok(!F.flashIsProblem({ level: "warn", text: null }));
  assert.ok(!F.flashIsProblem(null));
});

test("未读按 (at_ms, text) 和「已看过」比：没看过时全算；看过以后只算更晚的、同一时刻没记过正文的", () => {
  const alerts = SAMPLE.alerts;
  assert.deepEqual(F.flashUnread(alerts, null).map((a) => a.text.slice(0, 1)), ["❌"]);
  const seen = F.flashSeenMark(alerts, null);
  assert.deepEqual(seen, { at: alerts[0].at_ms, texts: [alerts[0].text] });
  assert.deepEqual(F.flashUnread(alerts, seen), []);
  const later = alerts.concat([{ at_ms: alerts[5].at_ms + 1000, level: "error", text: "页面卡住" }]);
  assert.equal(F.flashUnread(later, seen).length, 1);
  const sameMs = alerts.concat([{ at_ms: alerts[0].at_ms, level: "warn", text: "❌ 另一句" }]);
  assert.equal(F.flashUnread(sameMs, seen).length, 1, "同一毫秒、正文不同：没看过");
  const older = [{ at_ms: alerts[0].at_ms - 1, level: "error", text: "更早的" }];
  assert.equal(F.flashUnread(older, seen).length, 0, "比看过的那条还早：不算新的");
  const both = F.flashSeenMark(sameMs, seen);
  assert.deepEqual(both, { at: alerts[0].at_ms, texts: [alerts[0].text, "❌ 另一句"] });
  assert.deepEqual(F.flashSeenMark([], seen), seen, "没有问题记录时原样保留");
  assert.deepEqual(F.flashSeenMark(older, seen), seen, "只往前走");
  assert.deepEqual(F.flashUnread(undefined, null), []);
});

test("存进 localStorage 的「已看过」：JSON 往返；读不出、形状不对都当没看过", () => {
  const seen = { at: 1793136662000, texts: ["❌ a", "❌ b"] };
  assert.deepEqual(F.flashParseSeen(JSON.stringify(seen)), seen);
  for (const raw of [null, "", "{", "[]", "null", '{"at":"1","texts":[]}', '{"at":1}', '{"at":1e999,"texts":[]}']) {
    assert.equal(F.flashParseSeen(raw), null, String(raw));
  }
  assert.deepEqual(F.flashParseSeen('{"at":5,"texts":["a",3,null]}'), { at: 5, texts: ["a"] });
});

// ---- 整体状态 -----------------------------------------------------------------------------------

test("胶囊与顶栏按钮：没有这个键就整颗不出现；连不上不变色；读不出、有提醒代码、陈旧、有未读才变色", () => {
  assert.deepEqual(F.flashButtonState(null, 0), { hidden: true, attn: false, title: "闪购助手" });
  assert.deepEqual(F.flashButtonState(undefined, 0).hidden, true);
  const cases = [
    [{ conn: "checking" }, 0, "checking", "正在查看…"],
    [{ conn: "unreachable" }, 0, "off", "没在运行"],
    [{ conn: "bad_response", attention: ["bad_response"] }, 0, "attn", "要处理"],
    [st({ attention: [] }), 0, "ok", "运行中"],
    [st({ attention: ["stuck"] }), 0, "attn", "要处理"],
    [st({ attention: [] }), 1, "attn", "要处理"],
    [st({ attention: [], stale: true }), 0, "attn", "要处理"],
    [st({ attention: [], running: false, paused_reason: "not_started" }), 0, "idle", "未开始"],
    [st({ attention: [], running: false, page: "no_page", paused_reason: null }), 0, "idle", "未开始"],
    [{ conn: "who_knows" }, 0, "checking", "正在查看…"],
  ];
  for (const [state, unread, tone, text] of cases) {
    assert.deepEqual(F.flashChip(state, unread), { tone, text }, JSON.stringify(state).slice(0, 80));
    const b = F.flashButtonState(state, unread);
    assert.equal(b.hidden, false);
    assert.equal(b.attn, tone === "attn");
    assert.equal(b.title, "闪购助手 · " + text);
  }
  assert.equal(F.flashNeedsAttention({ conn: "unreachable", attention: ["stale"] }, 3), false, "连不上一律不变色");
});

const lines = (state, unread = 0) => F.flashLines(state, unread).map(F.flashLineText);
// 同一份样例，但没有商品卡住（stuck 都是 false）、后端也没报提醒
const calm = (extra = {}) => st(Object.assign({ attention: [], products: SAMPLE.products.map((p) => Object.assign({}, p, { stuck: false })) },
                                              extra));

test("主句：正在查看、连不上（带每 15 秒再查）、读不出、连得上没回话（why=no_answer）", () => {
  assert.deepEqual(lines({ conn: "checking" }), ["正在查看闪购助手的状态…"]);
  assert.deepEqual(lines({ conn: "unreachable" }), ["闪购助手没在运行。", "127.0.0.1:9334 连不上，每 15 秒再查一次。"]);
  assert.deepEqual(lines({ conn: "bad_response" }), ["127.0.0.1:9334 返回的内容读不出来。"]);
  assert.deepEqual(lines({ conn: "bad_response", why: "json" }), ["127.0.0.1:9334 返回的内容读不出来。"]);
  // 闪购助手卡住时端口照样连得上、只是不回话：要人看（变色），也不能说它「没在运行」
  const frozen = { conn: "bad_response", why: "no_answer", attention: ["bad_response"] };
  assert.deepEqual(lines(frozen), ["127.0.0.1:9334 在 2 秒内没有回应。"]);
  assert.equal(F.flashLines(frozen, 0)[0].attn, true);
  assert.deepEqual(F.flashChip(frozen, 0), { tone: "attn", text: "要处理" });
  assert.equal(F.flashButtonState(frozen, 0).attn, true);
  assert.deepEqual(lines(calm({ alerts: [] })), [], "正常：不写主句，只看摘要");
  assert.deepEqual(lines(st({ attention: [], products: [], alerts: [] })), ["商品列表里还没有商品。"]);
});

test("主句的先后：陈旧 → 页面 → 暂停 → 卡住 → 认不出的提醒代码 → 未读记录 → 只是说明现状的", () => {
  const all = st({ stale: true, updated_ms: new Date(2026, 9, 27, 21, 36, 7).getTime(), page: "no_list", running: true,
                   paused_reason: null, attention: ["stuck", "page", "stale", "new_thing"] });
  assert.deepEqual(lines(all, 2), [
    "闪购助手没有更新状态。", "上次更新 21:36:07。", "商品后台没在 Buy now 列表上。", "1 个商品停了超过 1 分钟。",
    "要处理：new_thing", "最近记录里有 2 条新的问题。",
  ]);
  const paused = st({ paused_reason: "awaiting_approval", attention: ["paused", "stuck"] });
  assert.deepEqual(lines(paused, 0).slice(0, 2), ["专用 Chrome 里弹出了确认框，核对后点确定或取消。", "1 个商品停了超过 1 分钟。"]);
  const loading = st({ page: "loading", running: false, paused_reason: null, products: [], attention: [] });
  assert.deepEqual(lines(loading, 1), ["最近记录里有 1 条新的问题。", "商品后台正在打开…"], "说明现状的排在要处理的后面");
  assert.deepEqual(lines(st({ stale: true, updated_ms: null, attention: ["stale"] })).slice(0, 2),
                   ["闪购助手没有更新状态。", "还没有收到过状态。"]);
  for (const l of F.flashLines(all, 2)) observed(F.flashLineText(l));
});

test("主句里认不出的页面、暂停代码原样显示，不给一句通用文案糊过去", () => {
  const page = F.flashLines(calm({ page: "maintenance", running: false, paused_reason: null }), 0);
  assert.deepEqual(page, [{ text: "页面状态：", code: "maintenance", attn: false }]);
  const paused = F.flashLines(calm({ paused_reason: "cooldown", attention: ["paused"] }), 0);
  assert.deepEqual(paused[0], { text: "已暂停：", code: "cooldown", attn: true });
  assert.deepEqual(lines(calm({ paused_reason: "not_started", running: false })), ["闪购助手开着，但还没开始运行。"]);
  assert.equal(F.flashLines(calm({ page: "login_required", running: false, paused_reason: null }), 0)[0].attn, true);
  assert.equal(F.flashLines(calm({ page: "no_list", running: false, paused_reason: null }), 0)[0].attn, false,
               "没在运行时不在 Buy now 列表上：只是说明");
});

// ---- 商品 ---------------------------------------------------------------------------------------

test("排序（同后端 app/flash_sale.py 的 _rank）：卡住的 → 待开始 → 看不准 → 进行中 → 两轮之间 → 其余已停止 → 没有闪购 → 手动结束 → 认不出的；同档保持原顺序", () => {
  const list = [
    product("ended_by_user", { id: "a" }), product("live", { id: "b" }), product("no_sale", { id: "c" }),
    product("mystery", { id: "d" }), product("stopped", { id: "e" }), product("between_rounds", { id: "f" }),
    product("unknown", { id: "g" }), product("pending", { id: "h" }), product("stopped", { id: "i", stuck: true }),
    product("live", { id: "j" }), product("stopped", { id: "k", enabled: false }),
  ];
  assert.equal(F.flashSortProducts(list).map((p) => p.id).join(""), "ihgbjfekcad");
  assert.deepEqual(F.flashSortProducts([null, 3, product("live", { id: "z" })]).map((p) => p.id), ["z"]);
  assert.deepEqual(F.flashSortProducts(undefined), []);
  const sorted = F.flashSortProducts(SAMPLE.products);
  assert.deepEqual(F.flashSortProducts(sorted), sorted, "排过的再排一次不变（后端已排过序）");
  assert.equal(sorted[0].state, "stopped");
  assert.deepEqual(sorted.map((p) => p.state), ["stopped", "pending", "unknown", "live", "live", "live", "between_rounds",
                                                "no_sale", "ended_by_user"]);
});

test("摘要：共几个商品，各状态的数量，只列不为 0 的，先后同排序", () => {
  assert.equal(F.flashSummary(SAMPLE.products),
               "共 9 个商品：已停止 1 · 待开始 1 · 看不准 1 · 进行中 3 · 两轮之间 1 · 没有闪购 1 · 已手动结束 1");
  assert.equal(F.flashSummary([product("live"), product("mystery")]), "共 2 个商品：进行中 1 · 其他 1");
  assert.equal(F.flashSummary([]), "");
  assert.equal(F.flashSummary(null), "");
  // 后端的 products_total 比列表长（超出 300 个被截掉）：总数按它写，末尾「未列出 n」，各项加起来是总数
  assert.equal(F.flashSummary([product("live"), product("stopped")], 310), "共 310 个商品：已停止 1 · 进行中 1 · 未列出 308");
  assert.equal(F.flashSummary(SAMPLE.products, SAMPLE.products_total), F.flashSummary(SAMPLE.products), "没截过：同不带总数");
  assert.equal(F.flashSummary([product("live")], 0), "共 1 个商品：进行中 1", "总数比列表短不算数");
  assert.equal(F.flashSummary([product("live")], "9"), "共 9 个商品：进行中 1 · 未列出 8");
  assert.equal(F.flashSummary([product("live")], true), "共 1 个商品：进行中 1", "true 不当数字");
});

test("摘要只在段与段之间折行：每段一个 .flash-summary-part（第一段带着「共 n 个商品：」），段之间是「 · 」", () => {
  // 截图（2026-10-28）：440px 的浮层里摘要会从「两轮之间 1」「没有闪购 1」「已手动结束 1」中间折开。
  // 每段一个 inline-block（样式见下一条）：放不下时整段换到下一行，一段比一行还宽时才在段里折
  const PARTS = ["已停止 1", "待开始 1", "看不准 1", "进行中 3", "两轮之间 1", "没有闪购 1", "已手动结束 1"];
  assert.deepEqual(F.flashSummaryParts(SAMPLE.products), { lead: "共 9 个商品：", parts: PARTS });
  assert.deepEqual(F.flashSummaryParts([product("live")], 310), { lead: "共 310 个商品：", parts: ["进行中 1", "未列出 309"] });
  assert.equal(F.flashSummaryParts([]), null);
  assert.equal(F.flashSummaryParts(null, 0), null);
  const doc = makeFlashDoc();
  const els = makeEls(doc);
  F.renderFlashPanel(doc, els, SAMPLE, { now: NOW });
  const spans = els.summary.children;
  assert.deepEqual(spans.map((el) => el.className), PARTS.map(() => "flash-summary-part"));
  assert.deepEqual(spans.map((el) => el.textContent), ["共 9 个商品：" + PARTS[0], ...PARTS.slice(1)]);
  assert.equal(els.summary.textContent, F.flashSummary(SAMPLE.products), "拼起来还是同一句");
  assert.ok(visible(els.summary));
  // 重画不叠：再画一次还是这几段；没有商品时清空、藏起来
  F.renderFlashPanel(doc, els, SAMPLE, { now: NOW });
  assert.equal(els.summary.children.length, PARTS.length);
  F.renderFlashPanel(doc, els, st({ products: [], products_total: 0 }), { now: NOW });
  assert.equal(els.summary.textContent, "");
  assert.ok(!visible(els.summary));
});

test("样式：摘要的每一段是 inline-block（style.css .flash-summary-part）", () => {
  const css = readFileSync(new URL("../web/style.css", import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const m = /\n\.flash-summary-part \{([^}]*)\}/.exec(css);
  assert.ok(m, "style.css 里要有 .flash-summary-part");
  assert.match(m[1], /display: inline-block/);
});

test("商品一行的细节：卡住 / 等续开、倒计时、第几轮、库存、续开次数、不自动续开", () => {
  const live = F.flashProductParts(SAMPLE.products[0], SAMPLE, NOW);
  assert.equal(live.title, "Glow Serum Trio (Fictional)");
  assert.deepEqual(live.state, { text: "进行中", code: "", tone: "live" });
  assert.deepEqual(live.detail, [{ countdown: "ends", at: SAMPLE.products[0].ends_ms }, "第 2 轮", "库存 30/30"]);
  const next = F.flashProductParts(SAMPLE.products[1], SAMPLE, NOW);
  assert.deepEqual(next.detail[0], { countdown: "next", at: SAMPLE.products[1].next_ms });
  const stuck = F.flashProductParts(SAMPLE.products[2], SAMPLE, NOW);
  assert.deepEqual(stuck.detail, ["停了超过 1 分钟", "第 2 轮", "库存 30/30", "续开 3 次"]);
  const waiting = F.flashProductParts(product("stopped"), { running: true, paused_reason: null }, NOW);
  assert.deepEqual(waiting.detail, ["等闪购助手续开"]);
  // 同后端 stuck_candidate：enabled 只认 true。读不出（null）的后端永远不算卡住，这里也不说「等续开」
  assert.deepEqual(F.flashProductParts(product("stopped", { enabled: null }), { running: true, paused_reason: null }, NOW).detail, []);
  assert.equal(F.flashWaitingRenewal(product("stopped", { enabled: null }), { running: true, paused_reason: null }), false);
  assert.equal(F.flashWaitingRenewal(product("stopped"), { running: true, paused_reason: "awaiting_approval" }), false);
  assert.equal(F.flashWaitingRenewal(product("stopped"), { running: false, paused_reason: null }), false);
  const off = F.flashProductParts(product("stopped", { enabled: false, stock_left: 4, renewals: 1 }), { running: true }, NOW);
  assert.deepEqual(off.detail, ["库存 4", "续开 1 次", "不自动续开"]);
  const odd = F.flashProductParts(product("paused_by_tiktok", { title: "" }), {}, NOW);
  assert.deepEqual(odd.state, { text: "", code: "paused_by_tiktok", tone: "other" });
  assert.equal(odd.title, "1000000000000000099", "没有标题时显示商品 id");
  const bools = F.flashProductParts(product("live", { round: true, stock_left: false, renewals: "2" }), {}, NOW);
  assert.deepEqual(bools.detail, ["续开 2 次"], "true / false 不当数字");
});

test("先列 40 行，多的写「还有 N 个」，点开全列", () => {
  assert.equal(F.FLASH_ROWS, 40);
  assert.deepEqual(F.flashRowsToShow(9, false), { shown: 9, more: 0 });
  assert.deepEqual(F.flashRowsToShow(40, false), { shown: 40, more: 0 });
  assert.deepEqual(F.flashRowsToShow(41, false), { shown: 40, more: 1 });
  assert.deepEqual(F.flashRowsToShow(300, false), { shown: 40, more: 260 });
  assert.deepEqual(F.flashRowsToShow(300, true), { shown: 300, more: 0 });
  assert.equal(F.flashMoreText(1), "还有 1 个");
  assert.equal(F.flashMoreText(260), "还有 260 个");
});

test("最近记录：新的在上，平时最后 5 条，再加上更早的、没看过的问题记录（不然按钮变了色却找不到是哪条）", () => {
  const many = Array.from({ length: 20 }, (_, i) => ({ at_ms: i, level: i % 7 ? "info" : "error", text: "t" + i }));
  assert.deepEqual(F.flashAlertsToShow(many, []).map((a) => a.text), ["t19", "t18", "t17", "t16", "t15"]);
  const unread = F.flashUnread(many, null);
  assert.deepEqual(unread.map((a) => a.text), ["t0", "t7", "t14"]);
  assert.deepEqual(F.flashAlertsToShow(many, unread).map((a) => a.text), ["t19", "t18", "t17", "t16", "t15", "t14", "t7", "t0"]);
  assert.deepEqual(F.flashAlertsToShow(SAMPLE.alerts, F.flashUnread(SAMPLE.alerts, null)).length, 6, "样例：最后 5 条 + 最早那条 ❌");
  assert.deepEqual(F.flashAlertsToShow(null, null), []);
  assert.deepEqual(F.flashAlertsToShow([null, { at_ms: 1, level: "info", text: "x" }], []).map((a) => a.text), ["x"]);
});

// ---- 渲染 ---------------------------------------------------------------------------------------

const texts = (el) => el.children.map((c) => c.textContent);

test("整张浮层：胶囊、主句、摘要、商品行、记录、版本；标题标 translate=\"no\"，记录正文带 i18n-data", () => {
  const doc = makeFlashDoc();
  const els = makeEls(doc);
  const unread = F.flashUnread(SAMPLE.alerts, null);
  const countdowns = F.renderFlashPanel(doc, els, SAMPLE, { now: NOW, unread });
  assert.equal(els.state.textContent, "要处理");
  assert.equal(els.state.getAttribute("data-tone"), "attn");
  assert.equal(els.headline.textContent, "1 个商品停了超过 1 分钟。");
  assert.ok(els.headline.classList.contains("attn"));
  assert.deepEqual(texts(els.detail), ["最近记录里有 1 条新的问题。"]);
  assert.ok(visible(els.summary) && els.summary.textContent.startsWith("共 9 个商品："));
  const rows = els.products.children;
  assert.equal(rows.length, 9);
  assert.equal(rows[0].getAttribute("data-state"), "stop");
  const title = rows[0].querySelector(".flash-title");
  assert.equal(title.getAttribute("title"), "Calm Gel Mask x4 (Fictional)");
  assert.equal(title.children[0].getAttribute("translate"), "no");
  assert.equal(title.children[0].textContent, "Calm Gel Mask x4 (Fictional)");
  assert.equal(rows[0].querySelector(".flash-pill").textContent, "已停止");
  assert.equal(rows[0].querySelector(".flash-row-detail").textContent, "停了超过 1 分钟 · 第 2 轮 · 库存 30/30 · 续开 3 次");
  assert.ok(!visible(els.more));
  const alerts = els.alerts.children;
  assert.equal(alerts.length, 6, "最后 5 条，加上更早的那条没看过的 ❌");
  assert.equal(alerts[0].querySelector(".flash-alert-text").textContent, SAMPLE.alerts[5].text, "新的在上");
  for (const li of alerts) {
    const body = li.querySelector(".flash-alert-text");
    assert.ok(body.classList.contains("i18n-data"));
    assert.equal(body.getAttribute("translate"), null, "正文留给浏览器翻译，不标 translate=no");
  }
  assert.deepEqual(alerts.map((li) => li.classList.contains("new")), [false, false, false, false, false, true]);
  const x = alerts[5];
  assert.ok(x.classList.contains("new") && x.getAttribute("data-level") === "warn");
  assert.equal(x.querySelector(".flash-lv").getAttribute("aria-label"), "提醒");
  assert.ok(visible(els.alertsTitle));
  assert.equal(els.version.textContent, "版本 0.7.2");
  assert.equal(countdowns.length, 4, "三个进行中 + 一个两轮之间");
  assert.ok(countdowns.every((c) => c.el.classList.contains("flash-countdown")));
});

test("倒计时每秒只改字：flashTick 改的就是画出来的那几个 span", () => {
  const doc = makeFlashDoc();
  const els = makeEls(doc);
  const p = product("live", { ends_ms: NOW + 65000 });
  const countdowns = F.renderFlashPanel(doc, els, st({ products: [p], attention: [] }), { now: NOW });
  const span = els.products.querySelector(".flash-countdown");
  assert.equal(span.textContent, "还剩 1:05");
  F.flashTick(countdowns, NOW + 1000);
  assert.equal(span.textContent, "还剩 1:04");
  F.flashTick(countdowns, NOW + 70000);
  assert.equal(span.textContent, "还剩 0:00");
  F.flashTick(undefined, NOW);
});

test("45 个商品：先列 40 行，「还有 5 个」露出；点开后全列", () => {
  const doc = makeFlashDoc();
  const els = makeEls(doc);
  const many = st({ attention: [], products: Array.from({ length: 45 }, (_, i) => product("live", { id: "p" + i })) });
  F.renderFlashPanel(doc, els, many, { now: NOW });
  assert.equal(els.products.children.length, 40);
  assert.ok(visible(els.more));
  assert.equal(els.more.textContent, "还有 5 个");
  F.renderFlashPanel(doc, els, many, { now: NOW, expanded: true });
  assert.equal(els.products.children.length, 45);
  assert.ok(!visible(els.more));
});

test("认不出的代码原样放进 translate=\"no\" 的 span；标题和记录里的标签字符串原样成文字", () => {
  const doc = makeFlashDoc();
  const els = makeEls(doc);
  const evil = '<img src=x onerror="alert(1)">';
  const state = st({ attention: ["mystery"], page: "maintenance", running: false, paused_reason: null,
                     products: [product("paused_by_tiktok", { title: evil })],
                     alerts: [{ at_ms: NOW, level: "error", text: "<b>" + evil + "</b>" }] });
  F.renderFlashPanel(doc, els, state, { now: NOW, unread: [] });
  assert.equal(els.headline.textContent, "要处理：mystery");
  const code = els.headline.querySelector(".flash-code");
  assert.equal(code.getAttribute("translate"), "no");
  assert.equal(code.textContent, "mystery");
  assert.equal(texts(els.detail)[0], "页面状态：maintenance");
  const row = els.products.children[0];
  assert.equal(row.getAttribute("data-state"), "other");
  const pillCode = row.querySelector(".flash-pill").querySelector(".flash-code");
  assert.equal(pillCode.textContent, "paused_by_tiktok");
  assert.equal(pillCode.getAttribute("translate"), "no");
  const title = row.querySelector(".flash-title");
  assert.equal(title.textContent, evil);
  assert.equal(title.getAttribute("title"), evil);
  assert.equal(row.querySelector("img"), null, "标题没有被当成 HTML");
  const body = els.alerts.children[0].querySelector(".flash-alert-text");
  assert.equal(body.textContent, "<b>" + evil + "</b>");
  assert.equal(body.children.length, 0);
});

test("陈旧：商品表整体加 stale（样式换成次要文字色）；连不上：商品、记录、摘要都收起", () => {
  const doc = makeFlashDoc();
  const els = makeEls(doc);
  F.renderFlashPanel(doc, els, st({ stale: true, attention: ["stale"] }), { now: NOW });
  assert.ok(els.products.classList.contains("stale"));
  assert.equal(els.headline.textContent, "闪购助手没有更新状态。");
  F.renderFlashPanel(doc, els, { conn: "unreachable", products: [], alerts: [], attention: [] }, { now: NOW });
  assert.ok(!els.products.classList.contains("stale"));
  assert.ok(!visible(els.products) && !visible(els.summary) && !visible(els.alerts) && !visible(els.alertsTitle));
  assert.equal(els.state.textContent, "没在运行");
  assert.equal(els.headline.textContent, "闪购助手没在运行。");
  assert.deepEqual(texts(els.detail), ["127.0.0.1:9334 连不上，每 15 秒再查一次。"]);
  assert.equal(els.version.textContent, "");
  F.renderFlashPanel(doc, els, { conn: "checking" }, { now: NOW });
  assert.equal(els.headline.textContent, "正在查看闪购助手的状态…");
  assert.ok(!visible(els.detail));
});
