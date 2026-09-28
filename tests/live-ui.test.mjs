// 直播中界面纯逻辑（web/live-ui.js）的契约测试（node:test，零 npm 依赖）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { shareButtonText, connectHint, stripStatusEmoji, barIconFor,
        popoverRight, alertTierText } = require("../web/live-ui.js");

test("同看关着：只写功能名，没有人数", () => {
  assert.deepEqual(shareButtonText(false, 3), { text: "手机同看", count: "" });
});
test("开着没人看：写「已打开」", () => {
  assert.deepEqual(shareButtonText(true, 0), { text: "手机同看 · 已打开", count: "" });
  assert.deepEqual(shareButtonText(true, undefined), { text: "手机同看 · 已打开", count: "" });
  assert.deepEqual(shareButtonText(true, -1), { text: "手机同看 · 已打开", count: "" });
  assert.deepEqual(shareButtonText(true, "abc"), { text: "手机同看 · 已打开", count: "" });
});
test("有人在看：写人数，count 给窄窗口的徽标", () => {
  assert.deepEqual(shareButtonText(true, 2), { text: "手机同看 · 2 人", count: "2" });
  assert.deepEqual(shareButtonText(true, "3"), { text: "手机同看 · 3 人", count: "3" });
});

test("连接中：带主播名；没有主播名用通用文案", () => {
  assert.deepEqual(connectHint("connecting", "bella", false), { show: true, inline: false, text: "正在连接 @bella…" });
  assert.deepEqual(connectHint("connecting", "", false), { show: true, inline: false, text: "正在连接…" });
});
test("连接中且已有字幕：改成字幕末尾一行", () => {
  assert.equal(connectHint("connecting", "bella", true).inline, true);
});
test("其余状态隐藏；offline 返回 null（维持原样）", () => {
  for (const s of ["idle", "live", "ended", "error", undefined]) {
    assert.deepEqual(connectHint(s, "bella", false), { show: false, inline: false, text: "" });
  }
  assert.equal(connectHint("offline", "bella", false), null);
});

test("去掉提示条开头的状态 emoji（含 FE0F 与空格）", () => {
  assert.equal(stripStatusEmoji("🔴 检测已降级：识别落后 30 秒"), "检测已降级：识别落后 30 秒");
  assert.equal(stripStatusEmoji("⚠️ 识别开始落后"), "识别开始落后");
  assert.equal(stripStatusEmoji("⚠识别开始落后"), "识别开始落后");
  assert.equal(stripStatusEmoji("✅ 识别已追上"), "识别已追上");
  assert.equal(stripStatusEmoji("🔴 ⚠️ 两个"), "两个");
});
test("句中的不动；没有 emoji 原样；空值给空串", () => {
  assert.equal(stripStatusEmoji("自检：人声降噪未生效"), "自检：人声降噪未生效");
  assert.equal(stripStatusEmoji("GPU ⚠️ 出错"), "GPU ⚠️ 出错");
  assert.equal(stripStatusEmoji(null), "");
  assert.equal(stripStatusEmoji(undefined), "");
});

test("提示条图标：形状跟级别走", () => {
  assert.equal(barIconFor("error"), "octagon");
  assert.equal(barIconFor("degraded"), "octagon");
  assert.equal(barIconFor("info"), "info");
  assert.equal(barIconFor("warn"), "warn");
  assert.equal(barIconFor("lagging"), "warn");
  assert.equal(barIconFor(undefined), "warn");
});

test("浮层右边缘对齐按钮右边缘", () => {
  assert.equal(popoverRight(1230, 1320, 420, 12), 90);
});
test("按钮太靠左：往右推，左边至少留 margin", () => {
  assert.equal(popoverRight(300, 1000, 420, 12), 1000 - 420 - 12);
});
test("按钮贴右边：右边至少留 margin", () => {
  assert.equal(popoverRight(1318, 1320, 420, 12), 12);
});
test("窗口比浮层还窄（浮层宽已按 100vw-24 收过）：两边各 12", () => {
  assert.equal(popoverRight(400, 420, 396, 12), 12);
});

test("报警分级文字；认不出的按「命中」，原型链上的键也不认", () => {
  assert.equal(alertTierText("exact"), "命中");
  assert.equal(alertTierText("variant"), "变体");
  assert.equal(alertTierText("fuzzy"), "疑似");
  assert.equal(alertTierText("weird"), "命中");
  assert.equal(alertTierText(undefined), "命中");
  assert.equal(alertTierText("toString"), "命中");
  assert.equal(alertTierText("__proto__"), "命中");
});
