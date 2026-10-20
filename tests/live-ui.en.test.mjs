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

// ---- 底部大字幕的翻译状态、滞后，页脚各段，字幕字号（2026-10-15）。中文断言在 tests/live-ui.test.mjs ----

test("字幕翻译状态角标（英文）：不贴原因标签，只写看到的", () => {
  assert.deepEqual(U.captionStateChip({ translate_state: "failed" }), { text: "Translation failed", fail: true });
  assert.deepEqual(U.captionStateChip({ translate_state: "failed", refused: true }),
                   { text: "Not translated (API refusal)", fail: true });
  assert.deepEqual(U.captionStateChip({ translate_state: "failed", not_translation: true }),
                   { text: "Not translated (the result didn’t look like a translation)", fail: true });
  assert.deepEqual(U.captionStateChip({ translate_state: "dropped" }), { text: "Skipped (backlog)", fail: false });
  for (const msg of [{ translate_state: "failed" }, { translate_state: "failed", refused: true },
                     { translate_state: "failed", not_translation: true }, { translate_state: "dropped" }]) {
    english(U.captionStateChip(msg).text);
  }
});

test("大字幕角标（英文）：没译成的写同样的英文，还在等译文不画，译文到了没有", () => {
  assert.deepEqual(U.liveBarChip({ translate_state: "pending" }), { text: "", fail: false });
  assert.equal(U.liveBarChip({ translate_state: "ok", translated: "你好" }).text, "");
  assert.equal(U.liveBarChip({ translate_state: "failed", refused: true }).text, "Not translated (API refusal)");
  assert.equal(U.liveBarChip({ translate_state: "dropped" }).text, "Skipped (backlog)");
});

test("约 N 秒前（英文）：About N sec ago / About N min ago，15 秒以内什么都不写，单位缩写、数字与单位间一个空格", () => {
  for (const ms of [0, 1200, 8300, 14999]) assert.equal(U.captionAgeText(ms), "", String(ms));
  assert.equal(U.captionAgeText(15000), "About 15 sec ago");
  assert.equal(U.captionAgeText(18300), "About 18 sec ago");
  assert.equal(U.captionAgeText(59000), "About 59 sec ago");
  assert.equal(U.captionAgeText(60000), "About 1 min ago");
  assert.equal(U.captionAgeText(125000), "About 2 min ago");
  for (const ms of [15000, 18300, 45000, 60000, 3600000]) english(U.captionAgeText(ms));
  assert.equal(U.captionAgeText(-1), "");
  assert.equal(U.captionAgeText(NaN), "");
});

test("字幕字号（英文界面里同一套比例）", () => {
  assert.equal(U.captionScale(34), 1);
  assert.deepEqual(U.captionSizeVars(51), { "--sub-size": "51px", "--cap-scale": "1.5" });
  assert.equal(U.captionSizeFromConfig("40"), 40);
  assert.equal(U.captionSizeFromConfig(61), null);
});


test("Coverage chip: only when low; the count in the title takes one / many", () => {
  assert.deepEqual(U.coverageChip({ coverage_pct: 40, coverage_low: false }), { text: "", title: "" });
  const one = U.coverageChip({ coverage_pct: 38.6, coverage_window_min: 5, coverage_low: true, breaks: 1 });
  assert.equal(english(one.text), "Last 5 min: 39% received");
  assert.equal(english(one.title), "Share of stream audio received in the last 5 min. 1 interruption this session.");
  const many = U.coverageChip({ coverage_pct: 12, coverage_window_min: 2, coverage_low: true, breaks: 9 });
  assert.equal(english(many.text), "Last 2 min: 12% received");
  assert.equal(english(many.title), "Share of stream audio received in the last 2 min. 9 interruptions this session.");
});
