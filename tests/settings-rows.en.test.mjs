globalThis.UI_LANG = "en";   // 必须在 require 之前：web/i18n.js 加载时读一次（node --test 每个文件一个进程）
// 设置分组几行摘要的英文（web/settings-rows.js，spec §12.1 G6）。中文断言在 tests/settings-rows.test.mjs，
// 界面语言那一行（langSummary / langSystemLabel）的英文在 tests/i18n.test.mjs。
// 复数照 docs/i18n-style.md §2.3 的表：n=0/1/2 都跑一遍，1 用单数。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const R = require("../web/settings-rows.js");

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

test("自检摘要：失败 / 提醒 / 全部通过，1 用单数，图标和签名与中文相同", () => {
  assert.deepEqual(R.selfcheckSummary({ fail: 1, warn: 2, total: 11 }),
    { text: "1 feature isn’t working", icon: "fail", sig: "1/2/11" });
  assert.equal(R.selfcheckSummary({ fail: 2, warn: 0, total: 11 }).text, "2 features aren’t working");
  assert.deepEqual(R.selfcheckSummary({ fail: 0, warn: 1, total: 11 }),
    { text: "Passed · 1 warning", icon: "warn", sig: "0/1/11" });
  assert.equal(R.selfcheckSummary({ fail: 0, warn: 2, total: 11 }).text, "Passed · 2 warnings");
  assert.deepEqual(R.selfcheckSummary({ fail: 0, warn: 0, total: 11 }),
    { text: "All 11 checks passed", icon: "pass", sig: "0/0/11" });
  assert.equal(R.selfcheckSummary({ fail: 0, warn: 0, total: 1 }).text, "1 check passed");
  for (const sum of [{ fail: 0, warn: 0, total: 0 }, {}, undefined]) english(R.selfcheckSummary(sum).text);
});

test("引擎摘要：不翻译、免费额度（紧凑写法）、回退前缀；引擎名是后端给的数据，原样放", () => {
  assert.equal(R.engineSummary({ engine: "none" }).text, "No translation");
  const quota = R.engineSummary({ active_label: "DeepL", usage: { used: 115000, limit: 500000 } });
  assert.equal(english(quota.text), "DeepL · 23% of free quota used (~11 hr left)");
  assert.equal(R.engineSummary({ active_label: "DeepL", usage: { used: 500000, limit: 500000 } }).text,
    "DeepL · 100% of free quota used (~0 hr left)");
  const fell = R.engineSummary({ active_label: "Local Hy-MT2 1.8B", note: "DeepL has no API key yet." });
  assert.deepEqual(fell, { text: "Fallback · Local Hy-MT2 1.8B", hasNote: true, sig: "DeepL has no API key yet." });
  assert.equal(R.engineSummary({ engine: "none", note: "x" }).text, "Fallback · No translation");
  assert.equal(R.engineSummary({}).text, "", "什么都没有：和中文一样是空串");
});

test("报警摘要：开关、词表条数（1 用单数）、三种说明", () => {
  const on = R.watchSummary(true, 53);
  assert.equal(on.mode, "On");
  assert.equal(on.state, "53 terms");
  assert.equal(english(on.desc),
    "Listens to what the streamer says and alerts on a match right away. Translation speed doesn’t affect alerts.");
  assert.equal(on.configured, true);
  const off = R.watchSummary(false, 1);
  assert.equal(off.mode, "Off");
  assert.equal(off.state, "1 term");
  assert.equal(english(off.desc),
    "No alerts during the stream. Matches are still written to the audit log. Turn this on to get alerts.");
  assert.equal(R.watchSummary(false, 2).state, "2 terms");
  for (const enabled of [true, false]) {
    const empty = R.watchSummary(enabled, 0);
    assert.equal(empty.state, "List empty");
    assert.equal(english(empty.desc), "The banned-term list is empty, so no alerts will be raised.");
    assert.equal(empty.configured, false);
  }
});

test("界面语言行：英文界面里也没有中文（语言名「中文」是自称，只在选了中文时出现）", () => {
  english(R.langSummary("system", "en", "en"));
  english(R.langSystemLabel("en"));
  assert.equal(R.langName("zh"), "中文");
});
