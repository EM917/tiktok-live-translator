// 字幕跟随判定的契约测试。
//
// 起因：离开软件页面一段时间后回来，字幕停在旧位置，必须手动往下拖。
// 浏览器里实测确认的机制——页面不可见时 scrollHeight 与 clientHeight 均为 0，
// 于是 `scrollTop = scrollHeight` 是给 0 赋 0（空操作），而 `0-0-0 < 120`
// 又让「是否在底部」的判定成立。两者叠加，不可见期间到达的每一条字幕都没能
// 滚动，且没有任何机制事后补上。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { isMeasurable, nextFollowing, needsResync, viewTransition } =
  require("../web/follow.js");

// 不可渲染的元素：浏览器一律返回 0
const hidden = { scrollHeight: 0, scrollTop: 0, clientHeight: 0 };
const atBottom = { scrollHeight: 10000, scrollTop: 9800, clientHeight: 200 };
const scrolledUp = { scrollHeight: 10000, scrollTop: 200, clientHeight: 200 };
const nearBottom = { scrollHeight: 10000, scrollTop: 9750, clientHeight: 200 };

test("不可见的元素不可测量", () => {
  assert.equal(isMeasurable(hidden), false);
  assert.equal(isMeasurable(atBottom), true);
  assert.equal(isMeasurable(null), false);
});

test("不可测量时沿用原意图，不猜", () => {
  // 这正是原来的 bug：0-0-0 < 120 会得出「在底部」，
  // 于是不可见期间的滚动请求被认为已完成
  assert.equal(nextFollowing(hidden, true, true), true);
  assert.equal(nextFollowing(hidden, false, true), false);
});

test("滚到底部一律恢复跟随", () => {
  assert.equal(nextFollowing(atBottom, false, true), true);
  assert.equal(nextFollowing(nearBottom, false, false), true);   // 50px 仍在容差内
});

test("只有用户自己滚上去才停止跟随", () => {
  assert.equal(nextFollowing(scrolledUp, true, true), false);
});

test("非用户导致的滚动不得关闭跟随", () => {
  // 元素从不可渲染恢复时，浏览器把 scrollTop 重置为 0 并抛出 scroll 事件。
  // 把它读成「用户翻到了顶部」的话，此后再也不会自动滚到最新——
  // 那正是本次要修的症状。
  assert.equal(nextFollowing(scrolledUp, true, false), true);
});

test("跟随状态下不在底部时需要补滚", () => {
  assert.equal(needsResync(scrolledUp, true), true);
});

test("已在底部时无需补滚", () => {
  assert.equal(needsResync(atBottom, true), false);
});

test("用户主动滚上去看历史时不打扰他", () => {
  assert.equal(needsResync(scrolledUp, false), false);
});

test("不可见时不尝试补滚——那时的滚动同样是空操作", () => {
  assert.equal(needsResync(hidden, true), false);
});

// 「停止」之后页面留残留：大字幕、统计行、弹幕面板各自用不同条件判断要不要
// 隐藏，改一个漏一个。viewTransition 把「状态变了该做什么」收成一个纯函数，
// 这样每种迁移组合都能在这里钉住，不用靠真开页面去点「停止」核对。
test("直播中→待机/结束/出错：进入首页", () => {
  assert.deepEqual(viewTransition(true, "idle"),
    { active: false, enterHome: true, exitHome: false });
  assert.deepEqual(viewTransition(true, "ended"),
    { active: false, enterHome: true, exitHome: false });
  assert.deepEqual(viewTransition(true, "error"),
    { active: false, enterHome: true, exitHome: false });
});

test("待机→连接中：离开首页", () => {
  assert.deepEqual(viewTransition(false, "connecting"),
    { active: true, enterHome: false, exitHome: true });
});

test("直播中→断线重连→恢复直播：布局全程不动", () => {
  var afterOffline = viewTransition(true, "offline");
  assert.deepEqual(afterOffline, { active: true, enterHome: false, exitHome: false });
  // 重连后 hello 带回真实状态仍是 live：不能因为「断线那一刻」已经算过
  // active=true 就重复触发离开首页的动作
  assert.deepEqual(viewTransition(afterOffline.active, "live"),
    { active: true, enterHome: false, exitHome: false });
});

test("待机中断线：布局同样不动（不会被误判成进首页）", () => {
  assert.deepEqual(viewTransition(false, "offline"),
    { active: false, enterHome: false, exitHome: false });
});

test("初始加载即待机：prevActive 传 null，仍要进首页", () => {
  // null 表示「还没收到过任何状态」，跟「已经在待机」（false）是两回事——
  // 不分开的话页面刚加载时会被判成「没有变化」，首页该做的动作就漏了
  assert.deepEqual(viewTransition(null, "idle"),
    { active: false, enterHome: true, exitHome: false });
});

test("出错→待机：都是非活跃，不重复触发进首页", () => {
  var afterError = viewTransition(true, "error");   // 先从直播中出错，触发一次
  assert.equal(afterError.enterHome, true);
  var afterIdle = viewTransition(afterError.active, "idle");
  assert.deepEqual(afterIdle, { active: false, enterHome: false, exitHome: false });
});

test("连接中→直播中：都是活跃，不重复触发离开首页", () => {
  var afterConnecting = viewTransition(false, "connecting");
  assert.equal(afterConnecting.exitHome, true);
  var afterLive = viewTransition(afterConnecting.active, "live");
  assert.deepEqual(afterLive, { active: true, enterHome: false, exitHome: false });
});
