// 「换主播」两段式确认纯函数的契约测试（node:test，零 npm 依赖）。
// 照 tests/brand.test.mjs / tests/follow.test.mjs 的加载方式。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { loadDesktop, hello, ROOM } from "./desktop-page.mjs";

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

// ---- 接线：换主播时胶囊和「正在连接 @…」跟着 config 里的新主播走（专家审查 F5）----
// 补核走真的换主播面板复现：停 A 期间胶囊写「连接中… · @A」，横幅已经是「正在切换到 @B」；config(room_url=B)
// 到了胶囊还是 A，要等新一场的第一条 status 才变成 B。胶囊的主播名取自输入框，以前收到 config 只填输入框、
// 不重画。桌面页在 node:vm 里真跑（tests/desktop-page.mjs），消息按 Pipeline 换主播的顺序推
const ROOM_B = "https://www.tiktok.com/@sol_demo/live";
const SWITCHING = "正在切换到 @sol_demo：先停止 @luna_demo 的监听，再连接新的直播间…";
const pill = (page) => page.el("status-text").textContent;
const banner = (page) => page.el("status-banner").textContent;

test("接线：换主播的 config 一到，胶囊和连接提示就是新主播；横幅仍是服务端那句", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page);
  assert.equal(pill(page), "直播中 · @luna_demo");
  page.push({ type: "status", state: "connecting", detail: SWITCHING });      // _start_with_ack
  assert.equal(pill(page), "连接中… · @luna_demo", "停 A 期间：输入框还是 A，config 还没到");
  // start_stream：quiet 停完 A 之后广播 config（这时还没有新一场的 status）
  page.push({ type: "config", room_url: ROOM_B, source_lang: "auto", brands: {}, active_brand: null });
  assert.equal(pill(page), "连接中… · @sol_demo");
  assert.equal(page.el("connect-hint-text").textContent, "正在连接 @sol_demo…");
  assert.equal(banner(page), SWITCHING, "横幅的字由服务端决定，不在这里改");
  // 新一场的第一条 status：照旧
  page.push({ type: "status", state: "connecting", detail: "正在解析直播流地址…" });
  assert.equal(pill(page), "连接中… · @sol_demo");
});

test("接线：待机时收到带 room_url 的 config 只回填输入框，胶囊不冒出主播名", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page, { status: { state: "idle", detail: "" } });
  const idle = pill(page);
  page.push({ type: "config", room_url: ROOM_B });
  assert.equal(pill(page), idle);
  assert.doesNotMatch(pill(page), /@/);
});

test("接线：断线期间换了主播，重连的 hello 带新 room_url：胶囊是新主播（以前输入框非空就不回填，一直写旧的）", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page);
  assert.equal(page.el("room-input").value, ROOM);
  hello(page, { room_url: ROOM_B });
  assert.equal(pill(page), "直播中 · @sol_demo");
  assert.equal(page.el("room-input").value, ROOM_B);
});

test("接线：待机时重连，输入框里已有的地址不被 hello 盖掉（开始面板开着，可能是中控刚敲的）", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page, { status: { state: "idle", detail: "" } });
  page.el("room-input").value = ROOM_B;                                      // 中控在开始面板里敲了新的
  hello(page, { status: { state: "idle", detail: "" }, room_url: ROOM });
  assert.equal(page.el("room-input").value, ROOM_B);
});
