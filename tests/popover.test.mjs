// 顶栏浮层的共用开合（web/app.js 的 registerPopover 登记表）：手机同看、换主播两个浮层。
//
// 页面是 node:vm 里真跑的 web/app.js（tests/desktop-page.mjs），中文、英文两种界面各跑一遍：
//   1. 互斥：打开一个就关掉另一个，反过来也一样；aria-expanded 跟着走；
//   2. Esc 只关开着的那一个，焦点回到它自己的触发按钮；没有浮层开着时 Esc 什么都不做；
//   3. 点浮层外面关掉；点在浮层里、或点在触发按钮上（交给按钮自己切换）都不关。
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadDesktop, hello, FakeEl } from "./desktop-page.mjs";

const open = (page, id) => !page.el(id).classList.contains("hidden");
const expanded = (page, id) => page.el(id).getAttribute("aria-expanded");

for (const lang of ["zh", "en"]) {
  const name = (s) => s + "（" + lang + "）";

  test(name("打开一个浮层就关掉另一个，反过来也一样"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.el("share-btn").click();
    assert.ok(open(page, "share-panel") && !open(page, "switch-panel"));
    assert.equal(expanded(page, "share-btn"), "true");
    page.el("switch-btn").click();
    assert.ok(open(page, "switch-panel"), "换主播打开");
    assert.ok(!open(page, "share-panel"), "同看被关掉");
    assert.equal(expanded(page, "share-btn"), "false");
    assert.equal(expanded(page, "switch-btn"), "true");
    page.el("share-btn").click();
    assert.ok(open(page, "share-panel") && !open(page, "switch-panel"), "反过来：换主播被关掉");
    assert.equal(expanded(page, "switch-btn"), "false");
    page.el("share-btn").click();                                // 再点一次自己：收起
    assert.ok(!open(page, "share-panel") && !open(page, "switch-panel"));
    assert.equal(expanded(page, "share-btn"), "false");
  });

  test(name("Esc 关掉开着的那一个，焦点回到它的触发按钮；都关着时什么都不做"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    const focused = [];
    for (const id of ["share-btn", "switch-btn"]) page.el(id).focus = () => focused.push(id);
    page.dispatch("keydown", { key: "Escape" });
    assert.deepEqual(focused, []);
    page.el("share-btn").click();
    page.dispatch("keydown", { key: "Enter" });                  // 别的键不管
    assert.ok(open(page, "share-panel"));
    page.dispatch("keydown", { key: "Escape" });
    assert.ok(!open(page, "share-panel"));
    assert.deepEqual(focused, ["share-btn"]);
    page.el("switch-btn").click();
    page.dispatch("keydown", { key: "Escape" });
    assert.ok(!open(page, "switch-panel"));
    assert.deepEqual(focused, ["share-btn", "switch-btn"]);
  });

  test(name("点浮层外面关掉；点在浮层里或触发按钮上不关"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.el("switch-btn").click();
    page.dispatch("pointerdown", { target: page.el("switch-panel") });
    page.dispatch("pointerdown", { target: page.el("switch-btn") });
    assert.ok(open(page, "switch-panel"));
    page.dispatch("pointerdown", { target: page.el("history") });
    assert.ok(!open(page, "switch-panel"));
    assert.equal(expanded(page, "switch-btn"), "false");
    page.el("share-btn").click();
    // 假 DOM 按 id 建的元素各自独立、不成树：往浮层里挂一个子节点，点它算「浮层里」
    const inside = new FakeEl("button");
    page.el("share-panel").appendChild(inside);
    page.dispatch("pointerdown", { target: inside });
    assert.ok(open(page, "share-panel"));
    page.dispatch("pointerdown", { target: page.el("stop-btn") });
    assert.ok(!open(page, "share-panel"));
  });
}
