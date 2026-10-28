// 闪购浮层（web/flash.js）测试共用的数据和假 DOM：tests/flash.test.mjs、flash.en.test.mjs、flash-button.test.mjs。
// 不是测试文件（不以 .test.mjs 结尾，`node --test tests/*.test.mjs` 不会把它当用例跑）。
//
// SAMPLE 是推给页面的那份状态（hello 的 config.flash_sale、type:"flash_sale" 消息，形状见 docs/flash-sale.md），
// 由闪购助手接口契约的虚构样例 status-sample-running.json 规整而来：时间换成毫秒，product_id → id，
// 丢掉 shop、page_version；已停止的那个商品标成 stuck（后端计满 60 秒以后的样子），attention 是 ["stuck"]，
// 另带后端的 products_total、stuck_count（「还有 N 个」按商品表的长度算，卡住的个数按 stuck 数；products_total 只在
// 比商品表长时进摘要的「未列出 n」）。记录里那条 ❌ 开头的 warn 是「没看过的问题记录」。商品顺序照契约样例：前端会再排一次。
// 这份和后端 StatusTracker 对契约样例 running.json 的输出（计满 60 秒后）逐字段相同、只差商品顺序：
// tests/test_flash_sale.py 的 test_the_node_fixture_is_what_the_tracker_sends 钉着，两边改了形状哪一边都会红。
import { readFileSync } from "node:fs";
import { FakeEl, makeDoc } from "./guard-dom.mjs";

export const SAMPLE = JSON.parse(readFileSync(new URL("./fixtures/flash_sale/message-running.json", import.meta.url), "utf8"));

// 渲染函数只许用 textContent：假 DOM 上 innerHTML 一碰就抛
Object.defineProperty(FakeEl.prototype, "innerHTML", { set() { throw new Error("flash.js 不许用 innerHTML"); } });

export function makeFlashDoc() {
  return Object.assign(makeDoc(), { createElementNS: (_, tag) => new FakeEl(tag) });
}

/** renderFlashPanel 要的那几个节点（index.html #flash-panel 里的同名节点）。 */
export function makeEls(doc) {
  const els = {};
  for (const k of ["state", "headline", "detail", "summary", "products", "more", "alertsTitle", "alerts", "version"]) {
    els[k] = doc.createElement(k === "products" ? "ul" : k === "alerts" ? "ol" : "div");
  }
  return els;
}

/** 一个商品（字段照 SAMPLE），extra 盖在上面。 */
export const product = (state, extra = {}) => Object.assign({ id: "1000000000000000099", title: "Test Item (Fictional)", state,
  round: null, ends_ms: null, next_ms: null, stock_left: null, stock_total: null, renewals: 0, enabled: true, stuck: false }, extra);

/** SAMPLE 的一份深拷贝，extra 盖在上面。 */
export const st = (extra = {}) => Object.assign(JSON.parse(JSON.stringify(SAMPLE)), extra);

export const NOW = Date.UTC(2026, 9, 27, 21, 36, 10);            // 样例 updated_at 之后 3 秒
