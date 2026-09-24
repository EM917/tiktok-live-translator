// 「换主播」两段式确认纯函数的契约测试（node:test，零 npm 依赖）。
// 照 tests/brand.test.mjs / tests/follow.test.mjs 的加载方式。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { SWITCH_ARM_MS, SWITCH_DEBOUNCE_MS, initialSwitchState, armSwitch,
       isSwitchExpired, expireSwitchState, shouldConfirmSwitch,
       switchClickAction, switchButtonLabel } = require("../web/switch.js");

// ---- armSwitch：输入/点 chip 之后状态该变成什么 ----

test("认不出主播名（空输入）：回到初始态，不武装", () => {
  assert.deepEqual(armSwitch("", "alice", 1000), initialSwitchState());
  assert.deepEqual(armSwitch(null, "alice", 1000), initialSwitchState());
});

test("目标是别的主播：武装，记下 target 和 armedAt", () => {
  assert.deepEqual(armSwitch("bob", "alice", 1000),
    { target: "bob", armed: true, armedAt: 1000 });
});

test("目标就是当前主播（大小写不敏感）：记下 target 但不武装", () => {
  assert.deepEqual(armSwitch("Alice", "alice", 1000),
    { target: "alice", armed: false, armedAt: null });
});

test("目标本身按小写归一化存", () => {
  assert.deepEqual(armSwitch("Bob", "alice", 1000).target, "bob");
});

test("没有当前主播（边界情况）：仍然武装", () => {
  assert.deepEqual(armSwitch("bob", "", 1000),
    { target: "bob", armed: true, armedAt: 1000 });
  assert.deepEqual(armSwitch("bob", null, 1000),
    { target: "bob", armed: true, armedAt: 1000 });
});

// ---- isSwitchExpired / expireSwitchState：6 秒过期 ----

test("没武装：不算过期", () => {
  assert.equal(isSwitchExpired(initialSwitchState(), 999999), false);
});

test("恰好 6 秒整：还没过期（严格大于才算过期）", () => {
  const s = { target: "bob", armed: true, armedAt: 1000 };
  assert.equal(isSwitchExpired(s, 1000 + SWITCH_ARM_MS), false);
});

test("超过 6 秒一毫秒：过期", () => {
  const s = { target: "bob", armed: true, armedAt: 1000 };
  assert.equal(isSwitchExpired(s, 1000 + SWITCH_ARM_MS + 1), true);
});

test("expireSwitchState：过期只解除武装、保留目标，没过期原样返回（不改入参）", () => {
  const s = { target: "bob", armed: true, armedAt: 1000 };
  assert.deepEqual(expireSwitchState(s, 1000 + SWITCH_ARM_MS + 1),
    { target: "bob", armed: false, armedAt: null });
  assert.deepEqual(expireSwitchState(s, 1000 + 2000), s);
  assert.deepEqual(s, { target: "bob", armed: true, armedAt: 1000 });   // 不改入参
});

// ---- shouldConfirmSwitch：点确认按钮/回车这一下算不算数 ----

test("没武装：不算，什么都不发", () => {
  assert.equal(shouldConfirmSwitch(initialSwitchState(), 5000, "alice"), false);
});

test("武装后 400ms 内：防双击，不算", () => {
  const s = { target: "bob", armed: true, armedAt: 1000 };
  assert.equal(shouldConfirmSwitch(s, 1000 + SWITCH_DEBOUNCE_MS - 1, "alice"), false);
});

test("武装后 400ms 整：不再算防抖区间（边界，仍要求非过期）", () => {
  const s = { target: "bob", armed: true, armedAt: 1000 };
  assert.equal(shouldConfirmSwitch(s, 1000 + SWITCH_DEBOUNCE_MS, "alice"), true);
});

test("武装后 400ms ~ 6 秒之间：算，真的发送", () => {
  const s = { target: "bob", armed: true, armedAt: 1000 };
  assert.equal(shouldConfirmSwitch(s, 1000 + 3000, "alice"), true);
});

test("超过 6 秒：过期，不算", () => {
  const s = { target: "bob", armed: true, armedAt: 1000 };
  assert.equal(shouldConfirmSwitch(s, 1000 + SWITCH_ARM_MS + 1, "alice"), false);
});

test("目标就是当前主播：兜底拒绝（正常情况下 armSwitch 不会武装到这种状态）", () => {
  const s = { target: "alice", armed: true, armedAt: 1000 };
  assert.equal(shouldConfirmSwitch(s, 1000 + 1000, "alice"), false);
  assert.equal(shouldConfirmSwitch(s, 1000 + 1000, "Alice"), false);   // 大小写不敏感
});

// ---- switchButtonLabel：按钮文案 + 是否禁用 ----

test("初始态（还没输入过）：中性文案，禁用", () => {
  assert.deepEqual(switchButtonLabel(initialSwitchState(), "alice"),
    { text: "确认换主播", disabled: true });
});

test("武装了、目标是别的主播：带双方名字的确认文案，可点", () => {
  const s = { target: "bob", armed: true, armedAt: 1000 };
  assert.deepEqual(switchButtonLabel(s, "alice"),
    { text: "再点一次：改听 @bob（停止监听 @alice）", disabled: false });
});

test("武装了但当前主播未知：确认文案不带停止监听那半句", () => {
  const s = { target: "bob", armed: true, armedAt: 1000 };
  assert.deepEqual(switchButtonLabel(s, ""),
    { text: "再点一次：改听 @bob", disabled: false });
});

test("目标就是当前主播（不论武装与否）：禁用并说明已经在听谁", () => {
  const unarmed = { target: "alice", armed: false, armedAt: null };
  assert.deepEqual(switchButtonLabel(unarmed, "alice"),
    { text: "正在监听的就是 @alice", disabled: true });
  const armed = { target: "alice", armed: true, armedAt: 1000 };   // 防御：不应出现，但也要禁用
  assert.deepEqual(switchButtonLabel(armed, "alice"),
    { text: "正在监听的就是 @alice", disabled: true });
});

test("认不出目标、没有 currentStreamer：中性文案，禁用", () => {
  assert.deepEqual(switchButtonLabel(initialSwitchState(), ""),
    { text: "确认换主播", disabled: true });
});

// ---- 超时复位之后：按钮回到可点的第一步，点一下重新武装 ----

test("有目标但没武装（超时复位过）：「换到 @B」，可点", () => {
  assert.deepEqual(switchButtonLabel({ target: "bob", armed: false, armedAt: null }, "alice"),
    { text: "换到 @bob", disabled: false });
});

test("switchClickAction：武装中过了防抖就确认，防抖内不算", () => {
  const s = { target: "bob", armed: true, armedAt: 1000 };
  assert.equal(switchClickAction(s, 1000 + SWITCH_DEBOUNCE_MS, "alice"), "confirm");
  assert.equal(switchClickAction(s, 1000 + SWITCH_DEBOUNCE_MS - 1, "alice"), "none");
});

test("switchClickAction：超时复位过或已过期只重新武装，不发送", () => {
  assert.equal(switchClickAction({ target: "bob", armed: false, armedAt: null }, 5000, "alice"), "arm");
  const armed = { target: "bob", armed: true, armedAt: 1000 };
  assert.equal(switchClickAction(armed, 1000 + SWITCH_ARM_MS + 1, "alice"), "arm");
});

test("switchClickAction：没有目标或目标就是当前主播，什么都不做", () => {
  assert.equal(switchClickAction(initialSwitchState(), 5000, "alice"), "none");
  assert.equal(switchClickAction({ target: "alice", armed: false, armedAt: null }, 5000, "Alice"), "none");
});
