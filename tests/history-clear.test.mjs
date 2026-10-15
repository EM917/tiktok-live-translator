// 「清空」真的清、翻上去看时「回到最新」数着又来了几条（web/app.js、web/live-ui.js）。
//
// 页面在 node:vm 里真的跑一遍（脚手架见 tests/desktop-dom.mjs），中文页、英文页各走一遍同一组用例：
//   1. 字幕区往上翻着看时，新来的字幕在「回到最新」按钮上数出来：中文「回到最新 · 12 条新字幕」，
//      英文 “Jump to Latest · 12 new captions”（1 条时 “1 new caption”）；回放的不算；点按钮、
//      自己滚回底部、清空，都归零；
//   2. 弹幕栏自己滚，也有自己的「回到最新」，数的是弹幕，和字幕那颗互不相干；
//   3. 「清空字幕」「清空弹幕」除了删节点，还发 clear_history 给服务端（缓存才会清，刷新后不会又回来）；
//      hello 重建前的内部清场和别的页面清空后的回声（history_cleared）不能再发一遍，不然两个页面互相清；
//      连接断着时服务端收不到：只清了本页，页脚说一声；
//   4. 英文页上，这些新写进页面的字一个汉字都不剩。
// 数字之外的句子形状（单复数、零条时只写按钮名）在 tests/live-ui.test.mjs / live-ui.en.test.mjs。
import { test } from "node:test";
import assert from "node:assert/strict";

import { runDesktop, leftoverChinese } from "./desktop-dom.mjs";

const T = 1759000000;
const ROOM = "https://www.tiktok.com/@luna_demo/live";

const TEXT = {
  en: {
    jump: "Jump to Latest",
    captions: (n) => "Jump to Latest · " + n + (n === 1 ? " new caption" : " new captions"),
    comments: (n) => "Jump to Latest · " + n + (n === 1 ? " new comment" : " new comments"),
    offlineCaptions: "Lost connection to the local service. Only this page was cleared, " +
      "and the captions come back once it reconnects.",
    offlineComments: "Lost connection to the local service. Only this page was cleared, " +
      "and the comments come back once it reconnects.",
  },
  zh: {
    jump: "回到最新",
    captions: (n) => "回到最新 · " + n + " 条新字幕",
    comments: (n) => "回到最新 · " + n + " 条新弹幕",
    offlineCaptions: "与本地服务断开，只清空了本页——重新连上后这些字幕会回来。",
    offlineComments: "与本地服务断开，只清空了本页——重新连上后这些弹幕会回来。",
  },
};

function hello(page) {
  page.push({ type: "hello", config: { status: { state: "live", detail: "" }, room_url: ROOM, version: "1.2.3" } });
}

function liveWithCaptions(lang, n = 3) {
  const page = runDesktop(lang);
  hello(page);
  for (let id = 1; id <= n; id++) {
    page.push({ type: "caption", id, ts: T + id, original: "hola " + id, translated: "hello " + id, translate_state: "ok" });
  }
  return page;
}

// 把字幕区或弹幕栏「翻上去」：几何量是假的（假 DOM 默认全 0，等于不可测量），给它一个能滚的高度，
// 再模拟一次真实的用户滚动（先 wheel 记下用户输入，再 scroll）
function scrollAway(box, { client = 500, height = 4000, top = 0 } = {}) {
  box.clientHeight = client;
  box.scrollHeight = height;
  box.scrollTop = top;
  box.fire("wheel");
  box.fire("scroll");
}
// 内容高度跟着子节点数走（真浏览器里清空之后 scrollHeight 会缩回去；假 DOM 里它是个死数）
function heightFollowsChildren(box, perChild = 100) {
  Object.defineProperty(box, "scrollHeight", { get: () => box.children.length * perChild, configurable: true });
}
function scrollToEnd(box) {
  box.scrollTop = box.scrollHeight;
  box.fire("wheel");
  box.fire("scroll");
}

const caption = (page, id) =>
  page.push({ type: "caption", id, ts: T + id, original: "hola " + id, translated: "hello " + id, translate_state: "ok" });
const comment = (page, id) =>
  page.push({ type: "comment", id: "c" + id, user: "ana", text: "hola " + id, ts: T + id, state: "same" });
const isShown = (el) => !el.classList.contains("hidden");
const clearsSent = (page) => page.sent.filter((m) => m.type === "clear_history");

for (const lang of ["en", "zh"]) {
  const W = TEXT[lang];

  test(lang + "：页面确实跑在这种语言里（不然下面全是空转）", () => {
    const page = liveWithCaptions(lang);
    assert.equal(page.sandbox.UI_LANG, lang);
    assert.equal(page.el("history").querySelectorAll(".cap").length, 3);
  });

  test(lang + "：贴底跟随着看时，按钮不出现、不数", () => {
    const page = liveWithCaptions(lang);
    for (let id = 4; id <= 10; id++) caption(page, id);
    assert.equal(isShown(page.el("jump-latest")), false);
    assert.equal(page.el("jump-latest").textContent, "");        // 没数过就没写过字，静态文字原样
  });

  test(lang + "：翻上去看时数新字幕，回放的不算，点按钮归零", () => {
    const page = liveWithCaptions(lang);
    const jump = page.el("jump-latest");
    scrollAway(page.el("history"));
    assert.equal(isShown(jump), true, "翻上去之后按钮该出现");
    caption(page, 4);
    assert.equal(jump.textContent, W.captions(1));
    for (let id = 5; id <= 15; id++) caption(page, id);
    assert.equal(jump.textContent, W.captions(12));
    // 点按钮：回到底部，归零
    jump.click();
    assert.equal(jump.textContent, W.jump);
    assert.equal(isShown(jump), false);
    caption(page, 16);
    assert.equal(jump.textContent, W.jump, "回到底部之后又跟着走了，不该再数");
    assert.equal(page.el("history").scrollTop, page.el("history").scrollHeight);
  });

  test(lang + "：自己滚回底部也归零；再翻上去从 1 开始数", () => {
    const page = liveWithCaptions(lang);
    const jump = page.el("jump-latest");
    scrollAway(page.el("history"));
    caption(page, 4);
    caption(page, 5);
    assert.equal(jump.textContent, W.captions(2));
    scrollToEnd(page.el("history"));
    assert.equal(jump.textContent, W.jump);
    assert.equal(isShown(jump), false);
    scrollAway(page.el("history"));
    caption(page, 6);
    assert.equal(jump.textContent, W.captions(1));
  });

  test(lang + "：字幕区和弹幕栏各数各的，互不相干", () => {
    const page = liveWithCaptions(lang);
    const captionJump = page.el("jump-latest");
    const commentJump = page.el("comment-jump");
    scrollAway(page.el("history"));
    scrollAway(page.el("comment-list"), { client: 400, height: 3000 });
    assert.equal(isShown(captionJump), true);
    assert.equal(isShown(commentJump), true);
    caption(page, 4);
    comment(page, 1);
    comment(page, 2);
    comment(page, 3);
    assert.equal(captionJump.textContent, W.captions(1));
    assert.equal(commentJump.textContent, W.comments(3));
    commentJump.click();
    assert.equal(commentJump.textContent, W.jump);
    assert.equal(isShown(commentJump), false);
    assert.equal(captionJump.textContent, W.captions(1), "点弹幕栏的按钮不该动字幕那颗");
    comment(page, 4);
    assert.equal(commentJump.textContent, W.jump);
    assert.equal(page.el("comment-list").scrollTop, page.el("comment-list").scrollHeight);
  });

  test(lang + "：弹幕栏贴底跟随着看时，没有按钮；收起面板时按钮不露面但照数", () => {
    const page = liveWithCaptions(lang);
    const commentJump = page.el("comment-jump");
    comment(page, 1);
    comment(page, 2);
    assert.equal(isShown(commentJump), false);
    scrollAway(page.el("comment-list"), { client: 400, height: 3000 });
    comment(page, 3);
    assert.equal(commentJump.textContent, W.comments(1));
    page.el("toggle-comments").click();                       // 收起：面板整列不显示
    comment(page, 4);
    assert.equal(isShown(commentJump), false);
    page.el("comment-fab").click();                           // 展开
    assert.equal(isShown(commentJump), true);
    assert.equal(commentJump.textContent, W.comments(2), "收起期间来的也算");
  });

  test(lang + "：点「清空字幕」发 clear_history，并清掉页面和计数", () => {
    const page = liveWithCaptions(lang);
    scrollAway(page.el("history"));
    caption(page, 4);
    assert.equal(page.el("jump-latest").textContent, W.captions(1));
    page.el("clear-btn").click();
    assert.deepEqual(clearsSent(page), [{ type: "clear_history", kind: "captions" }]);
    assert.equal(page.el("history").querySelectorAll(".cap").length, 0);
    assert.equal(page.el("jump-latest").textContent, W.jump);
  });

  test(lang + "：点「清空弹幕」发 clear_history，字幕不动，警报不碰", () => {
    const page = liveWithCaptions(lang);
    comment(page, 1);
    comment(page, 2);
    page.push({ type: "alert", alert_id: 1, term: "cura", tier: "exact", ts: T, matched: "cura" });
    const alertsBefore = page.el("alert-list").children.length;
    const list = page.el("comment-list");
    heightFollowsChildren(list);                                       // 一条 100 高，可视 400
    list.clientHeight = 400;
    for (let id = 3; id <= 6; id++) comment(page, id);                 // 6 条 = 600：放不下，跟随着贴底
    list.scrollTop = 0;
    list.fire("wheel");
    list.fire("scroll");                                               // 往上翻到顶
    comment(page, 7);
    comment(page, 8);
    assert.equal(isShown(page.el("comment-jump")), true);
    assert.equal(page.el("comment-jump").textContent, W.comments(2));
    page.el("clear-comments").click();
    assert.deepEqual(clearsSent(page), [{ type: "clear_history", kind: "comments" }]);
    assert.equal(page.el("comment-list").querySelectorAll(".cmt-item").length, 0);
    assert.equal(page.el("comment-jump").textContent, W.jump);
    assert.equal(isShown(page.el("comment-jump")), false);
    assert.equal(page.el("history").querySelectorAll(".cap").length, 3);
    assert.equal(page.el("alert-list").children.length, alertsBefore, "清空弹幕不碰警报");
    assert.equal(alertsBefore > 0, true, "这条用例要有警报才有意义");
  });

  test(lang + "：「清空警报」只清页面，不向服务端发清空（警报缓存要跨刷新留着）", () => {
    const page = liveWithCaptions(lang);
    page.push({ type: "alert", alert_id: 1, term: "cura", tier: "exact", ts: T, matched: "cura" });
    page.el("clear-alerts").click();
    assert.deepEqual(clearsSent(page), []);
  });

  test(lang + "：hello 重建前的清场、别的页面清空后的回声，都不会再发一遍 clear_history", () => {
    const page = liveWithCaptions(lang);
    comment(page, 1);
    hello(page);                                              // 重连：先清场，再放缓存里的回放
    assert.deepEqual(clearsSent(page), []);
    assert.equal(page.el("history").querySelectorAll(".cap").length, 0);
    caption(page, 7);
    comment(page, 2);
    page.push({ type: "history_cleared", kind: "captions" });  // 别的页面点了清空
    assert.deepEqual(clearsSent(page), []);
    assert.equal(page.el("history").querySelectorAll(".cap").length, 0);
    assert.equal(page.el("comment-list").querySelectorAll(".cmt-item").length, 1, "字幕的回声不动弹幕");
    page.push({ type: "history_cleared", kind: "comments" });
    assert.deepEqual(clearsSent(page), []);
    assert.equal(page.el("comment-list").querySelectorAll(".cmt-item").length, 0);
    page.push({ type: "history_cleared", kind: "nonsense" });  // 认不出的 kind：什么都不动
    page.push({ type: "history_cleared" });
  });

  test(lang + "：重连（hello）重建整个列表，计数跟着归零；重放的字幕不算新字幕", () => {
    const page = liveWithCaptions(lang);
    scrollAway(page.el("history"));
    caption(page, 4);
    caption(page, 5);
    assert.equal(page.el("jump-latest").textContent, W.captions(2));
    hello(page);
    for (let id = 1; id <= 5; id++) {
      page.push({ type: "caption", id, ts: T, original: "x", replay: true, translate_state: "ok" });
    }
    assert.equal(page.el("jump-latest").textContent, W.jump);
    assert.equal(isShown(page.el("jump-latest")), false, "回放强制跟到最新，按钮不该露面");
  });

  test(lang + "：连接断着时点清空：只清了本页，页脚说一声，不装作服务端也清了", () => {
    const page = liveWithCaptions(lang);
    comment(page, 1);
    page.offline();
    page.el("clear-btn").click();
    assert.deepEqual(clearsSent(page), []);
    assert.equal(page.el("history").querySelectorAll(".cap").length, 0);
    assert.equal(page.el("app-version").textContent, " · " + W.offlineCaptions);
    page.el("clear-comments").click();
    assert.equal(page.el("app-version").textContent, " · " + W.offlineComments);
    page.online();
    page.el("clear-btn").click();                              // 连上之后再点：这回发得出去
    assert.deepEqual(clearsSent(page), [{ type: "clear_history", kind: "captions" }]);
  });
}

test("英文页：新写进页面的字（两颗按钮、断线说明）一个汉字都不剩", () => {
  const page = liveWithCaptions("en");
  scrollAway(page.el("history"));
  scrollAway(page.el("comment-list"), { client: 400, height: 3000 });
  for (let id = 4; id <= 8; id++) caption(page, id);
  for (let id = 1; id <= 5; id++) comment(page, id);
  assert.equal(page.el("jump-latest").textContent, "Jump to Latest · 5 new captions");
  assert.equal(page.el("comment-jump").textContent, "Jump to Latest · 5 new comments");
  page.offline();
  page.el("clear-btn").click();
  page.el("clear-comments").click();
  page.online();
  assert.deepEqual(leftoverChinese(page), []);
});

test("英文页查漏本身不瞎：按钮上写了一句中文就会被报出来", () => {
  const page = liveWithCaptions("en");
  scrollAway(page.el("history"));
  caption(page, 4);
  assert.deepEqual(leftoverChinese(page), []);
  page.el("jump-latest").appendChild(page.document.createTextNode("回到最新"));
  assert.equal(leftoverChinese(page).length, 1);
});

test("两颗「回到最新」在静态页里是同一个样子（样式类、英文名、图标）", async () => {
  const { readFileSync } = await import("node:fs");
  const html = readFileSync(new URL("../web/index.html", import.meta.url), "utf8");
  for (const id of ["jump-latest", "comment-jump"]) {
    const tag = new RegExp('<button id="' + id + '" class="jump-latest hidden" type="button" data-en="Jump to Latest">\\s*' +
                           '<svg class="i" aria-hidden="true"><use href="#i-arrow-down"/></svg>回到最新\\s*</button>');
    assert.match(html, tag, id);
  }
});
