// 桌面页「场次分隔线」纯逻辑的契约测试（web/session-divider.js，node:test，
// 零 npm 依赖）。照 tests/switch.test.mjs 的加载方式。
//
// 起因（见 code review）：renderSessionBreak 的「首场不画线」判断此前只活在
// web/app.js 里，而 app.js 是单个大 IIFE、顶层就摸 document，整份文件没法被
// node:test require，这条判断因此从未被自动化测试盯着——手动删掉这行判断，
// `node --test tests/*.test.mjs` 依旧全绿。抽出这份文件就是为了让它可测。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { shouldRenderSessionDivider, sessionDividerText } =
  require("../web/session-divider.js");

// ---- shouldRenderSessionDivider：首场不画线 ----

test("页面里还没有字幕卡片（程序启动后的第一场）：不画分隔线", () => {
  assert.equal(shouldRenderSessionDivider(0), false);
  assert.equal(shouldRenderSessionDivider(undefined), false);
});

test("已经有字幕卡片：画分隔线", () => {
  assert.equal(shouldRenderSessionDivider(1), true);
  assert.equal(shouldRenderSessionDivider(7), true);
});

// ---- sessionDividerText：时间 + 主播名 ----

test("带主播名：时间 + 「以下为 @主播」", () => {
  // 本地时间刻意选个三个字段都是个位数的时刻，同时验证补零
  const d = new Date(2024, 0, 1, 3, 4, 5);
  const ts = d.getTime() / 1000;
  assert.equal(sessionDividerText({ ts: ts, streamer: "bellaallnatural" }),
    "── 03:04:05 以下为 @bellaallnatural ──");
});

test("主播名取不到（空串）：通用文案，不编造身份", () => {
  const d = new Date(2024, 5, 15, 23, 59, 0);
  const ts = d.getTime() / 1000;
  assert.equal(sessionDividerText({ ts: ts, streamer: "" }),
    "── 23:59:00 以下为新的一场 ──");
});

test("msg 缺失/streamer 缺失字段：不抛异常，落到通用文案", () => {
  assert.match(sessionDividerText({}), /^── \d\d:\d\d:\d\d 以下为新的一场 ──$/);
  assert.match(sessionDividerText(undefined), /^── \d\d:\d\d:\d\d 以下为新的一场 ──$/);
});
