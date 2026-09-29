globalThis.UI_LANG = "en";   // 必须在 require 之前：web/i18n.js 加载时读一次（node --test 每个文件一个进程）
// 报警面板纯逻辑的英文（web/alerts.js，spec §12.1 G6）。中文断言在 tests/alerts.test.mjs，一条不改。
//
// 窗口标题的英文要和 app/window_attention.py 的 attention_title 逐字相同；两边中文写法不同
// （这里拼接、那边模板），同中文→同英文的检查聚合不到一起，所以两边都对着
// tests/i18n_golden.json 的同一条金句断言（Python 那边在 tests/test_i18n_native.py）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const A = require("../web/alerts.js");
const GOLDEN = JSON.parse(readFileSync(new URL("./i18n_golden.json", import.meta.url), "utf8"));

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

test("窗口标题：与 tests/i18n_golden.json 的金句逐字相同，标签式写法不分单复数", () => {
  assert.equal(A.alertTitle(2), GOLDEN.window_title_2_en);
  assert.equal(english(A.alertTitle(1)), "(1) Possible Banned Terms · " + GOLDEN.app_name_en);
  assert.equal(english(A.alertTitle(0)), "(0) Possible Banned Terms · TikTok Live Translator");
});

test("isAlertTitle：认得出英文的提醒标题，认不出别的标题", () => {
  assert.equal(A.isAlertTitle(A.alertTitle(3)), true);
  assert.equal(A.isAlertTitle(GOLDEN.app_name_en), false);
  assert.equal(A.isAlertTitle("(3) Update Available · TikTok Live Translator"), false);
  assert.equal(A.isAlertTitle("(3) 疑似违禁词 · TikTok 直播同传"), false, "页面生命周期里语言不变：只认当前语言");
});

test("后台逐条累加、回到前台恢复：英文标题照样走通，默认恢复成英文程序名", () => {
  let s = A.noteAlert({ unseen: 0, restoreTo: null }, { type: "alert" }, false, GOLDEN.app_name_en);
  assert.equal(english(s.title), "(1) Possible Banned Terms · TikTok Live Translator");
  s = A.noteAlert(s, { type: "alert" }, false, s.title);
  assert.equal(s.title, GOLDEN.window_title_2_en);
  assert.equal(s.restoreTo, GOLDEN.app_name_en, "记的是第一条之前的标题，不是提醒本身");
  assert.equal(A.noteActive(s, s.title).title, GOLDEN.app_name_en);
  const lost = A.noteActive({ unseen: 2, restoreTo: null }, A.alertTitle(2));
  assert.equal(lost.title, GOLDEN.app_name_en, "没记住原标题时退回英文程序名");
});

test("sessionNote：条数说明按本场总数分单复数；不少于总数时为空", () => {
  assert.equal(english(A.sessionNote(57, 50)), "Showing the latest 50 of 57 alerts this session");
  assert.equal(english(A.sessionNote(2, 1)), "Showing the latest 1 of 2 alerts this session");
  assert.equal(english(A.sessionNote(1, 0)), "Showing the latest 0 of 1 alert this session",
    "手动清过面板时 shown 可以是 0：本场只有 1 条要用单数");
  assert.equal(english(A.sessionNote(3)), "Showing the latest 0 of 3 alerts this session");
  assert.equal(A.sessionNote(2, 2), "");
  assert.equal(A.sessionNote(0, 0), "");
});
