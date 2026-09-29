globalThis.UI_LANG = "en";   // 必须在 require 之前：web/i18n.js 加载时读一次（node --test 每个文件一个进程）
// 「换主播」确认按钮的英文（web/switch.js，spec §12.1 G6）。中文断言在 tests/switch.test.mjs。
// 按钮文字用 Title Case，句中的 to / from 小写，@用户名原样（docs/i18n-style.md §2.1）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const S = require("../web/switch.js");

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

test("还没输入：禁用，写 Switch Streamer（与顶栏按钮同名）", () => {
  assert.deepEqual(S.switchButtonLabel(S.initialSwitchState(), "bellaallnatural"),
    { text: "Switch Streamer", disabled: true });
  assert.deepEqual(S.switchButtonLabel(null, ""), { text: "Switch Streamer", disabled: true });
});

test("目标就是当前主播：禁用，说清已经在听这个人（大小写不敏感，写当前的原样）", () => {
  const st = S.armSwitch("BellaAllNatural", "bellaallnatural", 1000);
  assert.deepEqual(S.switchButtonLabel(st, "BellaAllNatural"),
    { text: "Already Monitoring @BellaAllNatural", disabled: true });
});

test("武装：有当前主播时两个名字都写上（会停掉谁、改听谁），没有时只写目标", () => {
  const armed = S.armSwitch("lamejorcrema", "bellaallnatural", 1000);
  assert.deepEqual(S.switchButtonLabel(armed, "bellaallnatural"),
    { text: "Click Again to Switch from @bellaallnatural to @lamejorcrema", disabled: false });
  assert.deepEqual(S.switchButtonLabel(armed, ""),
    { text: "Click Again to Switch to @lamejorcrema", disabled: false });
});

test("超时复位后：目标保留，可点，写 Switch to @B", () => {
  const armed = S.armSwitch("lamejorcrema", "bellaallnatural", 1000);
  const expired = S.expireSwitchState(armed, 1000 + S.SWITCH_ARM_MS + 1);
  assert.deepEqual(S.switchButtonLabel(expired, "bellaallnatural"),
    { text: "Switch to @lamejorcrema", disabled: false });
  english(S.switchButtonLabel(expired, "bellaallnatural").text);
});
