globalThis.UI_LANG = "en";   // 必须在 require 之前：web/i18n.js 加载时读一次（node --test 每个文件一个进程）
// 场次分隔线的英文（web/session-divider.js，spec §12.1 G6）。中文断言在 tests/session-divider.test.mjs。
// 时间两种语言都是 24 小时制 HH:MM:SS（docs/i18n-style.md §2.4），前后的 ── 不变。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const D = require("../web/session-divider.js");

const CJK = /[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/;

function hhmmss(ts) {
  const d = new Date(ts * 1000);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

test("语言确实是英文（不然下面全是空转）", () => {
  assert.equal(require("../web/i18n.js").UI_LANG, "en");
});

test("有主播名：── HH:MM:SS Now monitoring @x ──", () => {
  const ts = 1759212089;
  assert.equal(D.sessionDividerText({ ts, streamer: "lamejorcrema" }),
    "── " + hhmmss(ts) + " Now monitoring @lamejorcrema ──");
});

test("没有主播名：写 New session，不编造主播身份", () => {
  const ts = 1759212089;
  for (const msg of [{ ts }, { ts, streamer: "" }]) {
    assert.equal(D.sessionDividerText(msg), "── " + hhmmss(ts) + " New session ──");
  }
  const text = D.sessionDividerText(undefined);
  assert.match(text, /^── \d\d:\d\d:\d\d New session ──$/);
  assert.ok(!CJK.test(text), text);
});
