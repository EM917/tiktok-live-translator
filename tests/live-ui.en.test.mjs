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

test("页脚（英文）：Captions 打头，Alerts 紧跟其后，其余各段照旧", () => {
  const base = { asr: { p50: 1100 }, translate: { p50: 2000 }, segment: { p50: 800 },
                 detect_worst: { p50: 1900, p95: 3400 }, caption_wait: { p50: 4200, p95: 7100, n: 20 } };
  assert.deepEqual(U.statsLineParts(base), [
    "Captions 4.2s (P95 7.1s)", "Alerts ≤1.9s (P95 3.4s)", "Segment 0.8s + ASR 1.1s", "Translation +2.0s"]);
  assert.equal(U.statsLineParts(Object.assign({}, base, { caption_wait: { p50: null, p95: null, n: 0 } }))[0],
               "Alerts ≤1.9s (P95 3.4s)");
  const full = U.statsLineParts(Object.assign({ audio_backlog_sec: 5, audio_segments_dropped: 2, asr_overruns: 1,
    translation_jobs_dropped: 3, asr_queue_depth: 1, translation_queue_depth: 2 }, base));
  assert.deepEqual(full.slice(4), ["Backlog 5s", "Dropped 2", "ASR timeouts 1", "Skipped 3", "Queue 1/2"]);
  for (const part of full) english(part);
});

test("字幕字号（英文界面里同一套比例）", () => {
  assert.equal(U.captionScale(34), 1);
  assert.deepEqual(U.captionSizeVars(51), { "--sub-size": "51px", "--cap-scale": "1.5" });
  assert.equal(U.captionSizeFromConfig("40"), 40);
  assert.equal(U.captionSizeFromConfig(61), null);
});
