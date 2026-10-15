globalThis.UI_LANG = "en";   // 必须在 require 之前：web/i18n.js 加载时读一次（node --test 每个文件一个进程）
// 直播中界面几处文案的英文（web/live-ui.js，spec §12.1 G6）。中文断言在 tests/live-ui.test.mjs。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const U = require("../web/live-ui.js");

const CJK = /[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;
function english(text) {
  assert.equal(typeof text, "string");
  assert.ok(!CJK.test(text), "英文里还有中文：" + text);
  assert.ok(!/\{\w*\}/.test(text), "有没填的占位符：" + text);
  return text;
}

test("语言确实是英文（不然下面全是空转）", () => {
  assert.equal(require("../web/i18n.js").UI_LANG, "en");
});

test("手机同看按钮：关着写功能名，开着没人看写 On，有人看只写人数（不变形，不用分单复数）", () => {
  assert.deepEqual(U.shareButtonText(false, 3), { text: "Phone Viewing", count: "" });
  assert.deepEqual(U.shareButtonText(true, 0), { text: "Phone Viewing · On", count: "" });
  assert.deepEqual(U.shareButtonText(true, 1), { text: "Phone Viewing · 1", count: "1" });
  assert.deepEqual(U.shareButtonText(true, 2), { text: "Phone Viewing · 2", count: "2" });
  english(U.shareButtonText(true, "x").text);
});

test("连接中提示：带主播名 / 不带；主播名原样放", () => {
  assert.deepEqual(U.connectHint("connecting", "bellaallnatural", false),
    { show: true, inline: false, text: "Connecting to @bellaallnatural…" });
  assert.equal(U.connectHint("connecting", "", true).text, "Connecting…");
  assert.deepEqual(U.connectHint("live", "x", true), { show: false, inline: false, text: "" });
  assert.equal(U.connectHint("offline", "x", true), null);
});

test("报警分级：Exact / Variant / Similar，认不出的分级按 Exact（同中文按「命中」）", () => {
  assert.equal(U.alertTierText("exact"), "Exact");
  assert.equal(U.alertTierText("variant"), "Variant");
  assert.equal(U.alertTierText("fuzzy"), "Similar");
  for (const tier of [undefined, "", "toString", "__proto__", "new-tier"]) {
    assert.equal(U.alertTierText(tier), "Exact", String(tier));
  }
});

test("“Jump to Latest”：没有新的只写按钮名，有新的带条数；单复数分开；字幕 captions，弹幕 comments", () => {
  for (const n of [0, -3, NaN, undefined, null, "abc", 0.4]) {
    assert.equal(U.jumpLatestCaptionsText(n), "Jump to Latest", String(n));
    assert.equal(U.jumpLatestCommentsText(n), "Jump to Latest", String(n));
  }
  assert.equal(U.jumpLatestCaptionsText(1), "Jump to Latest · 1 new caption");
  assert.equal(U.jumpLatestCaptionsText(2), "Jump to Latest · 2 new captions");
  assert.equal(U.jumpLatestCaptionsText(12), "Jump to Latest · 12 new captions");
  assert.equal(U.jumpLatestCaptionsText("7"), "Jump to Latest · 7 new captions");
  assert.equal(U.jumpLatestCommentsText(1), "Jump to Latest · 1 new comment");
  assert.equal(U.jumpLatestCommentsText(120), "Jump to Latest · 120 new comments");
  for (const n of [0, 1, 2, 99]) {
    english(U.jumpLatestCaptionsText(n));
    english(U.jumpLatestCommentsText(n));
  }
});
