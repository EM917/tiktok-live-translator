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
const { isMeasurable, nextFollowing, needsResync } =
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
