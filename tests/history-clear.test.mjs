// 「清空」真的清、翻上去看时「回到最新」数着又来了几条（web/app.js、web/live-ui.js）。
//
// 页面在 node:vm 里真的跑一遍（脚手架见 tests/desktop-dom.mjs），中文页、英文页各走一遍同一组用例：
//   1. 字幕区往上翻着看时，新来的字幕在「回到最新」按钮上数出来：中文「回到最新 · 12 条新字幕」，
//      英文 “Jump to Latest · 12 new captions”（1 条时 “1 new caption”）；回放的不算；点按钮、
//      自己滚回底部、清空，都归零；
//   2. 弹幕栏自己滚，也有自己的「回到最新」，数的是弹幕，和字幕那颗互不相干；
//   3. 「清空字幕」「清空弹幕」要点两次（专家审查 V3）：第一次按钮变成「再点一次清空」（英文 Clear?），
//      6 秒内再点才清（和「清除记录」「一键更新」的确认一样长），到点复原；
//      清的时候除了删节点，还发 clear_history 给服务端（缓存才会清，刷新后不会又回来）；
//      hello 重建前的内部清场和别的页面清空后的回声（history_cleared）不能再发一遍，不然两个页面互相清；
//      连接断着时服务端收不到：只清了本页，页脚说一声；
//   4. 英文页上，这些新写进页面的字一个汉字都不剩；
//   5. 两颗清空按钮在静态页里各有中英 title，英文名分得开（专家审查 V3）。
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
    armed: "Clear?", clearCaptions: "Clear", clearComments: "Clear Comments",
  },
  zh: {
    jump: "回到最新",
    captions: (n) => "回到最新 · " + n + " 条新字幕",
    comments: (n) => "回到最新 · " + n + " 条新弹幕",
    offlineCaptions: "与本地服务断开，只清空了本页——重新连上后这些字幕会回来。",
    offlineComments: "与本地服务断开，只清空了本页——重新连上后这些弹幕会回来。",
    armed: "再点一次清空", clearCaptions: "清空字幕", clearComments: "清空弹幕",
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
// 两段式确认（V3）：第一次只是武装，紧接着第二次才真的清
const clearTwice = (btn) => { btn.click(); btn.click(); };
// 武装窗口 6 秒，和首页「清除记录」、顶栏「一键更新」的确认一样长（web/app.js 那两处都是 6000）
const CLEAR_CONFIRM_MS = 6000;
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
    clearTwice(page.el("clear-btn"));
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
    clearTwice(page.el("clear-comments"));
    assert.deepEqual(clearsSent(page), [{ type: "clear_history", kind: "comments" }]);
    assert.equal(page.el("comment-list").querySelectorAll(".cmt-item").length, 0);
    assert.equal(page.el("comment-jump").textContent, W.jump);
    assert.equal(isShown(page.el("comment-jump")), false);
    assert.equal(page.el("history").querySelectorAll(".cap").length, 3);
    assert.equal(page.el("alert-list").children.length, alertsBefore, "清空弹幕不碰警报");
    assert.equal(alertsBefore > 0, true, "这条用例要有警报才有意义");
  });

  for (const [id, kind, idleKey] of [["clear-btn", "captions", "clearCaptions"], ["clear-comments", "comments", "clearComments"]]) {
    test(lang + "：V3 " + id + " 点一次只武装、不清；6 秒内再点才清；按钮复原", () => {
      const page = liveWithCaptions(lang);
      comment(page, 1);
      const btn = page.el(id);
      btn.click();
      assert.equal(btn.textContent, W.armed);
      assert.deepEqual(clearsSent(page), [], "点一次不发 clear_history");
      assert.equal(page.el("history").querySelectorAll(".cap").length, 3, "点一次不清字幕");
      assert.equal(page.el("comment-list").querySelectorAll(".cmt-item").length, 1, "点一次不清弹幕");
      btn.click();
      assert.deepEqual(clearsSent(page), [{ type: "clear_history", kind }]);
      assert.equal(btn.textContent, W[idleKey], "清完按钮回到原来的字");
      // 清完再点一次：又是重新武装，不会直接再清一遍
      btn.click();
      assert.equal(btn.textContent, W.armed);
      assert.equal(clearsSent(page).length, 1);
    });

    test(lang + "：V3 " + id + " 武装之后 6 秒没再点：自动复原，之后再点一次也只是重新武装", () => {
      const page = liveWithCaptions(lang);
      comment(page, 1);
      const btn = page.el(id);
      btn.click();
      assert.equal(btn.textContent, W.armed);
      page.fire(3000);                                     // 计划原稿写的 3 秒：这时还在等第二下
      assert.equal(btn.textContent, W.armed, "3 秒时还没复原");
      page.fire(CLEAR_CONFIRM_MS);
      assert.equal(btn.textContent, W[idleKey]);
      btn.click();
      assert.equal(btn.textContent, W.armed, "超时之后这一下只是武装");
      assert.deepEqual(clearsSent(page), []);
      assert.equal(page.el("history").querySelectorAll(".cap").length, 3);
      assert.equal(page.el("comment-list").querySelectorAll(".cmt-item").length, 1);
    });
  }

  test(lang + "：V3 两颗按钮各自武装：点了清空字幕，再点清空弹幕不会把字幕清掉", () => {
    const page = liveWithCaptions(lang);
    comment(page, 1);
    page.el("clear-btn").click();
    page.el("clear-comments").click();
    assert.deepEqual(clearsSent(page), []);
    page.el("clear-comments").click();
    assert.deepEqual(clearsSent(page), [{ type: "clear_history", kind: "comments" }]);
    assert.equal(page.el("history").querySelectorAll(".cap").length, 3);
    assert.equal(page.el("clear-btn").textContent, W.armed, "字幕那颗还在等第二下");
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
    clearTwice(page.el("clear-btn"));
    assert.deepEqual(clearsSent(page), []);
    assert.equal(page.el("history").querySelectorAll(".cap").length, 0);
    assert.equal(page.el("app-version").textContent, " · " + W.offlineCaptions);
    clearTwice(page.el("clear-comments"));
    assert.equal(page.el("app-version").textContent, " · " + W.offlineComments);
    page.online();
    clearTwice(page.el("clear-btn"));                          // 连上之后再点：这回发得出去
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
  page.el("clear-btn").click();                             // 武装态的字也要是英文
  page.el("clear-comments").click();
  assert.equal(page.el("clear-btn").textContent, "Clear?");
  assert.equal(page.el("clear-comments").textContent, "Clear?");
  assert.deepEqual(leftoverChinese(page), []);
  page.el("clear-btn").click();
  page.el("clear-comments").click();
  assert.equal(page.el("clear-btn").textContent, "Clear");
  page.online();
  assert.deepEqual(leftoverChinese(page), []);
});

// 点「停止」时窗口先拿到 focus，resync 排下一帧「滚到底」；停止的状态赶在那一帧之前到达时，
// 页面已经回到首页、滚到顶——那一帧晚到也不能再把首页拽到「上一场字幕」底部（10-15 实测偶发）
test("停止赶在 focus 排下的那一帧之前到达：首页留在顶上", () => {
  const page = liveWithCaptions("zh");
  const box = page.el("history");
  box.clientHeight = 500;
  box.scrollHeight = 4000;
  // 对照：直播中 focus 排下的那一帧照常滚到底（不然下面的断言是空转）
  page.winFire("focus");
  page.nextFrame();
  assert.equal(box.scrollTop, 4000);
  page.winFire("focus");
  page.push({ type: "status", state: "idle", detail: "" });
  assert.equal(box.scrollTop, 0, "回到首页滚到顶");
  page.nextFrame();
  assert.equal(box.scrollTop, 0, "晚到的那一帧不能把首页拽到底");
});

test("英文页查漏本身不瞎：按钮上写了一句中文就会被报出来", () => {
  const page = liveWithCaptions("en");
  scrollAway(page.el("history"));
  caption(page, 4);
  assert.deepEqual(leftoverChinese(page), []);
  page.el("jump-latest").appendChild(page.document.createTextNode("回到最新"));
  assert.equal(leftoverChinese(page).length, 1);
});

// 专家审查 V3：英文界面上两颗清空按钮以前都只叫 Clear，清空弹幕那颗连 title 都没有。
// 顶栏那颗按钮上仍只写 Clear（Clear Captions 在 1000px 窗口把品牌标签挤到 100px 以下，G10 实测，
// docs/i18n-style.md §1.3、§4 R1），「字幕」两个字交给 title 说；弹幕栏那颗写全 Clear Comments
test("两颗清空按钮都有中英 title，英文名和 title 都分得开；弹幕那颗英文写全", async () => {
  const { readFileSync } = await import("node:fs");
  const html = readFileSync(new URL("../web/index.html", import.meta.url), "utf8").replace(/\r\n/g, "\n");
  const tag = (id) => {
    const m = html.match(new RegExp('<button id="' + id + '"((?:[^>"]|"[^"]*")*)>([^<]*)</button>'));
    assert.ok(m, "index.html 里找不到 #" + id);
    const attrs = Object.fromEntries([...m[1].matchAll(/([\w-]+)="([^"]*)"/g)].map((a) => [a[1], a[2]]));
    return { attrs, text: m[2] };
  };
  const cap = tag("clear-btn");
  assert.deepEqual([cap.text, cap.attrs["data-en"]], ["清空字幕", "Clear"]);
  assert.deepEqual([cap.attrs.title, cap.attrs["data-en-title"]], ["清空历史字幕", "Clear caption history"]);
  const cmt = tag("clear-comments");
  assert.deepEqual([cmt.text, cmt.attrs["data-en"]], ["清空弹幕", "Clear Comments"]);
  assert.deepEqual([cmt.attrs.title, cmt.attrs["data-en-title"]], ["清空历史弹幕", "Clear comment history"]);
  assert.notEqual(cap.attrs["data-en"], cmt.attrs["data-en"], "英文按钮名不能再一样");
  assert.notEqual(cap.attrs["data-en-title"], cmt.attrs["data-en-title"]);
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
