// 桌面二维码画布助手的契约测试（web/qrcanvas.js）。
//
// 只测 qrCanvasPlan：它是纯函数，不摸 DOM，真正的 renderQR 需要 <canvas>，
// 交给人工用一台真手机扫码验证（spec §9 第 6 条）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { qrCanvasPlan } = require("../web/qrcanvas.js");

function squareRows(n, fill) {
  const row = fill === "1" ? "1".repeat(n) : "0".repeat(n);
  return new Array(n).fill(row);
}

test("整数模块像素：devicePx 恰为 modulePx 的整数倍", () => {
  const rows = squareRows(21, "0");
  const plan = qrCanvasPlan(rows, 200, 2);
  assert.ok(plan);
  assert.equal(Number.isInteger(plan.modulePx), true);
  assert.equal(plan.devicePx, plan.modulePx * 21);
  assert.equal(plan.devicePx % plan.modulePx, 0);
});

test("总尺寸不小于调用方要的 cssSize（宁可略大，不许更小）", () => {
  const rows = squareRows(25, "0");
  for (const [cssSize, dpr] of [[180, 1], [180, 2], [180, 3], [77, 2.625], [1, 1]]) {
    const plan = qrCanvasPlan(rows, cssSize, dpr);
    assert.ok(plan.cssSize >= cssSize - 1e-9,
      `cssSize=${cssSize} dpr=${dpr} -> plan.cssSize=${plan.cssSize}`);
  }
});

test("quiet zone（矩阵行本身）原样保留，不被裁剪", () => {
  const n = 25;
  const rows = squareRows(n, "0");
  const plan = qrCanvasPlan(rows, 200, 2);
  assert.equal(plan.modules, n);
});

test("空 rows 返回 null 而不抛", () => {
  assert.equal(qrCanvasPlan([], 200, 2), null);
  assert.equal(qrCanvasPlan(null, 200, 2), null);
  assert.equal(qrCanvasPlan(undefined, 200, 2), null);
});

test("非法 rows（非方阵 / 非 0-1 字符）返回 null 而不抛", () => {
  assert.equal(qrCanvasPlan(["00", "0"], 200, 2), null);           // 不是方阵
  assert.equal(qrCanvasPlan(["0a", "10"], 200, 2), null);          // 含非法字符
  assert.equal(qrCanvasPlan(["00", 10], 200, 2), null);            // 元素不是字符串
  assert.equal(qrCanvasPlan("0011", 200, 2), null);                // 整体不是数组
});

test("非法 cssSize / dpr 不抛：cssSize<=0 返回 null，dpr<=0 回落为 1", () => {
  const rows = squareRows(21, "0");
  assert.equal(qrCanvasPlan(rows, 0, 2), null);
  assert.equal(qrCanvasPlan(rows, -10, 2), null);
  assert.equal(qrCanvasPlan(rows, NaN, 2), null);
  const plan = qrCanvasPlan(rows, 200, 0);
  assert.ok(plan);
  assert.equal(plan.cssSize >= 200, true);
});

test("同一输入两次调用结果一致（确定性，供渲染前反复布局用）", () => {
  const rows = squareRows(29, "1");
  const a = qrCanvasPlan(rows, 220, 2);
  const b = qrCanvasPlan(rows, 220, 2);
  assert.deepEqual(a, b);
});
