// 字幕区中间那张卡片和弹幕栏空态：该出现的时候出现、出现在中控看得到的地方（专家审查 U7、U8）。
//
// 页面在 node:vm 里真的跑一遍（脚手架见 tests/desktop-dom.mjs），中文页、英文页各走一遍：
//   U7 首次下载识别模型：status 带结构化的 progress（kind 为 model_download）时，中间卡片写下载进度；
//      顶部横幅照旧是服务端那整句。没有 progress 的连接中（detail 再长）卡片照旧写「正在连接 @…」，
//      不把横幅那句搬进来；
//   U8 直播中这一场还没出过字幕：中间卡片写「等待第一句字幕…」，第一句一到就收起（直播中清空过字幕、
//      再开下一场也一样）；
//      第二场起弹幕栏只数这一场的弹幕，上一场的弹幕和分隔线不算，空态那句照样出来；
//      页面重连时 hello 的 status 先到、回放字幕后到，「正在连接」挪回末尾、改成一行并跟到底部；
//   U9 房间地址输错：说明写在输入框下面、输入框标 aria-invalid，状态胶囊和顶部横幅都不动；再输入、
//      或者下一次认得出的开始，提示就清掉。
// 卡片上的字本身（中英两套、各种参数）在 tests/live-ui.test.mjs / live-ui.en.test.mjs。
import { test } from "node:test";
import assert from "node:assert/strict";

import { runDesktop, leftoverChinese } from "./desktop-dom.mjs";

const T = 1759000000;
const ROOM = "https://www.tiktok.com/@luna_demo/live";

const TEXT = {
  en: {
    connecting: "Connecting to @luna_demo…",
    download: (p) => "Downloading speech model… " + p + "%",
    waiting: "Waiting for the first caption…",
    cmtConnected: "Connected. Waiting for comments…",
    cmtDisconnected: "Disconnected. Reconnecting…",
    badInput: /^This isn’t a live link or username\. .*Display names won’t work\.$/,
    ready: "Ready",
  },
  zh: {
    connecting: "正在连接 @luna_demo…",
    download: (p) => "正在下载识别模型… " + p + "%",
    waiting: "等待第一句字幕…",
    cmtConnected: "已连接，等待观众发评论…",
    cmtDisconnected: "评论流断开，重连中…",
    badInput: /^认不出这个输入：请粘贴直播间链接，或输入主播的英文用户名.*中文昵称不行）。$/,
    ready: "待机",
  },
};
// 服务端发的下载进度那句（app/pipeline.py _model_download_progress 的样子，虚构的数）：只进横幅
const DL_DETAIL = {
  en: "Downloading speech model large-v3-turbo… 37% (600 of 1620 MB, first time only). Keep this window open.",
  zh: "正在下载识别模型 large-v3-turbo：37%（600 / 1620 MB，仅首次需要，请保持窗口打开）…",
};
const RESOLVING = { en: "Getting the stream URL…", zh: "正在解析直播流地址…" };

function hello(page, status, extra = {}) {
  page.push({ type: "hello", config: Object.assign({ status, room_url: ROOM, version: "1.2.3" }, extra) });
}
const status = (page, state, detail = "", more = {}) => page.push(Object.assign({ type: "status", state, detail }, more));
const caption = (page, id, more = {}) =>
  page.push(Object.assign({ type: "caption", id, ts: T + id, original: "hola " + id, translated: "hello " + id,
                            translate_state: "ok" }, more));
const comment = (page, id, more = {}) =>
  page.push(Object.assign({ type: "comment", id: "c" + id, user: "ana", text: "hola " + id, ts: T + id,
                            state: "same" }, more));
const isShown = (el) => !el.classList.contains("hidden");
const hint = (page) => ({
  shown: isShown(page.el("connect-hint")),
  inline: page.el("connect-hint").classList.contains("inline"),
  text: page.el("connect-hint-text").textContent,
});
const kids = (page) => page.el("history").children;
const lastChild = (page) => kids(page)[kids(page).length - 1];

for (const lang of ["en", "zh"]) {
  const W = TEXT[lang];

  test(lang + "：页面确实跑在这种语言里（不然下面全是空转）", () => {
    const page = runDesktop(lang);
    assert.equal(page.sandbox.UI_LANG, lang);
  });

  // ---- U7 ------------------------------------------------------------------------------------------

  test(lang + "：U7 首次下载模型：中间卡片写下载进度，横幅照旧是服务端那整句", () => {
    const page = runDesktop(lang);
    hello(page, { state: "connecting", detail: DL_DETAIL[lang], progress: { kind: "model_download", pct: 37 } });
    assert.deepEqual(hint(page), { shown: true, inline: false, text: W.download(37) });
    assert.equal(page.el("status-banner").textContent, DL_DETAIL[lang]);
    assert.equal(isShown(page.el("status-banner")), true);
    // 进度往前走：每条 status 都带着新的百分比
    status(page, "connecting", DL_DETAIL[lang].replace("37", "64"), { progress: { kind: "model_download", pct: 64 } });
    assert.equal(hint(page).text, W.download(64));
    // 下载完、接着解析流地址：那条 status 不带 progress，卡片回到「正在连接」
    status(page, "connecting", RESOLVING[lang]);
    assert.deepEqual(hint(page), { shown: true, inline: false, text: W.connecting });
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：U7 普通的连接中（只有 detail）：卡片写「正在连接 @…」，不重复横幅那句", () => {
    const page = runDesktop(lang);
    hello(page, { state: "connecting", detail: RESOLVING[lang] });
    assert.deepEqual(hint(page), { shown: true, inline: false, text: W.connecting });
    assert.equal(page.el("status-banner").textContent, RESOLVING[lang]);
    assert.notEqual(hint(page).text, page.el("status-banner").textContent);
    // 下载进度的那句 detail 没带 progress（老版本服务端）：同样不搬进卡片
    status(page, "connecting", DL_DETAIL[lang]);
    assert.equal(hint(page).text, W.connecting);
    // 认不出的 kind：照旧
    status(page, "connecting", DL_DETAIL[lang], { progress: { kind: "something_else", pct: 37 } });
    assert.equal(hint(page).text, W.connecting);
  });

  test(lang + "：U7 本地连接断线（offline）时卡片维持原样；重连的 hello 重放带 progress 的 status", () => {
    const page = runDesktop(lang);
    hello(page, { state: "connecting", detail: DL_DETAIL[lang], progress: { kind: "model_download", pct: 37 } });
    page.closeSocket();                                     // 页面自己转成 offline
    assert.equal(hint(page).text, W.download(37));
    assert.equal(hint(page).shown, true);
    hello(page, { state: "connecting", detail: DL_DETAIL[lang], progress: { kind: "model_download", pct: 52 } });
    assert.deepEqual(hint(page), { shown: true, inline: false, text: W.download(52) });
  });

  // ---- U8：直播中的第一句之前 ------------------------------------------------------------------------

  test(lang + "：U8 直播中、这一场还没出字幕：中间卡片写「等待第一句字幕…」，第一句一到就收起", () => {
    const page = runDesktop(lang);
    hello(page, { state: "connecting", detail: RESOLVING[lang] });
    status(page, "live");
    assert.deepEqual(hint(page), { shown: true, inline: false, text: W.waiting });
    assert.equal(isShown(page.el("status-banner")), false, "直播中没有横幅：卡片是这时唯一的说明");
    caption(page, 1);
    assert.equal(hint(page).shown, false);
    caption(page, 2);
    assert.equal(hint(page).shown, false);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：U8 页面打开时已经在直播、回放里有这一场的字幕：不写「等待第一句」", () => {
    const page = runDesktop(lang);
    hello(page, { state: "live", detail: "" });
    caption(page, 1, { replay: true });
    caption(page, 2, { replay: true, restore: true });
    assert.equal(hint(page).shown, false);
  });

  test(lang + "：U8 回放里只有上一场的字幕（这一场的分隔线在最后）：写「等待第一句」，改成末尾一行", () => {
    const page = runDesktop(lang);
    hello(page, { state: "live", detail: "" });
    caption(page, 1, { replay: true });
    caption(page, 2, { replay: true, restore: true });
    page.push({ type: "session_break", ts: T + 30, streamer: "luna_demo", replay: true });
    assert.deepEqual(hint(page), { shown: true, inline: true, text: W.waiting });
    assert.equal(lastChild(page), page.el("connect-hint"), "这一行在上一场字幕的下面");
  });

  test(lang + "：U8 停止后再开：上一场字幕还在，「等待第一句」是末尾一行；新一场第一句到了收起", () => {
    const page = runDesktop(lang);
    hello(page, { state: "live", detail: "" });
    caption(page, 1);
    caption(page, 2);
    status(page, "idle");
    assert.equal(hint(page).shown, false);
    status(page, "connecting", RESOLVING[lang]);
    page.push({ type: "session_break", ts: T + 60, streamer: "luna_demo" });
    assert.deepEqual(hint(page), { shown: true, inline: true, text: W.connecting });
    status(page, "live");
    assert.deepEqual(hint(page), { shown: true, inline: true, text: W.waiting });
    assert.equal(lastChild(page), page.el("connect-hint"));
    caption(page, 3);
    assert.equal(hint(page).shown, false);
  });

  test(lang + "：U8 直播中点了清空字幕：这一场早就出过字幕，不改写成「等待第一句」", () => {
    const page = runDesktop(lang);
    hello(page, { state: "live", detail: "" });
    caption(page, 1);
    page.el("clear-btn").click();
    assert.equal(page.el("history").querySelectorAll(".cap").length, 0, "这条用例要真的清掉才有意义");
    assert.equal(hint(page).shown, false);
  });

  // 清空字幕后页面上一条 .cap 也没有，下一场的 session_break 会在「没有字幕就不画线」那里提前返回：
  // 「这一场出没出过字幕」要在返回之前归零，不然新一场沿用上一场的「出过」，中间什么都不写
  for (const viaIdle of [false, true]) {
    test(lang + "：U8 直播中清空字幕后开下一场（" + (viaIdle ? "停止再开" : "直接换主播") +
         "）：中间卡片写「等待第一句字幕…」", () => {
      const page = runDesktop(lang);
      hello(page, { state: "live", detail: "" });
      caption(page, 1);
      page.el("clear-btn").click();
      assert.equal(page.el("history").querySelectorAll(".cap").length, 0, "这条用例要真的清掉才有意义");
      if (viaIdle) status(page, "idle");
      status(page, "connecting", RESOLVING[lang]);
      page.push({ type: "session_break", ts: T + 60, streamer: "luna_demo" });
      assert.equal(page.el("history").querySelectorAll(".session-sep").length, 0, "没有字幕：不画分隔线（提前返回的那条路）");
      assert.deepEqual(hint(page), { shown: true, inline: false, text: W.connecting });
      status(page, "live");
      assert.deepEqual(hint(page), { shown: true, inline: false, text: W.waiting });
      caption(page, 2);
      assert.equal(hint(page).shown, false);
      if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
    });
  }

  test(lang + "：U8 连接中又回到直播中（流断了重连上）：这一场出过字幕，不写「等待第一句」", () => {
    const page = runDesktop(lang);
    hello(page, { state: "live", detail: "" });
    caption(page, 1);
    status(page, "connecting", RESOLVING[lang]);
    assert.deepEqual(hint(page), { shown: true, inline: true, text: W.connecting });
    status(page, "live");
    assert.equal(hint(page).shown, false);
  });

  // ---- U8：页面重连时「正在连接」的位置 --------------------------------------------------------------

  test(lang + "：U8 页面重连时连接中、回放 12 条字幕：「正在连接」挪到回放字幕的下面，是末尾一行，跟到底部", () => {
    const page = runDesktop(lang);
    const box = page.el("history");
    box.clientHeight = 500;
    Object.defineProperty(box, "scrollHeight", { get: () => box.children.length * 100, configurable: true });
    hello(page, { state: "live", detail: "" });
    caption(page, 1);
    hello(page, { state: "connecting", detail: RESOLVING[lang] });            // 重连：status 先于回放
    for (let id = 1; id <= 12; id++) caption(page, id, { replay: true, restore: id === 12 });
    assert.equal(lastChild(page), page.el("connect-hint"), "提示在回放字幕的下面");
    assert.deepEqual(hint(page), { shown: true, inline: true, text: W.connecting });
    assert.equal(box.querySelectorAll(".cap").length, 12);
    assert.equal(box.scrollTop, box.scrollHeight, "跟到底部，提示在视野里");
    // 回放之后又来了实时字幕（连接中也可能到）：提示照样留在末尾
    caption(page, 13);
    assert.equal(lastChild(page), page.el("connect-hint"));
    status(page, "live");
    assert.equal(hint(page).shown, false);
  });

  test(lang + "：U8 页面重连：「这一场出没出过字幕」只按回放算，不沿用断线前的（和刷新后的页面一样）", () => {
    // 回放里没有这一场的分隔线、也没有字幕：服务端的字幕缓存清空过（clear_history 连分隔线一起清）。
    // 比如断线期间换了主播、新的一场还没出字幕时有人在另一个本机页面点了清空字幕
    const page = runDesktop(lang);
    hello(page, { state: "live", detail: "" });
    caption(page, 1);
    assert.equal(hint(page).shown, false, "断线前这一场出过字幕");
    hello(page, { state: "live", detail: "" });
    assert.deepEqual(hint(page), { shown: true, inline: false, text: W.waiting });
    const fresh = runDesktop(lang);
    hello(fresh, { state: "live", detail: "" });
    assert.deepEqual(hint(fresh), hint(page), "和刚打开的页面收到同一个 hello 一样");
    // 回放里有字幕时照旧收起
    hello(page, { state: "live", detail: "" });
    caption(page, 2, { replay: true, restore: true });
    assert.equal(hint(page).shown, false);
  });

  // ---- U8：第二场起弹幕栏的空态 ----------------------------------------------------------------------

  test(lang + "：U8 第一场：已连接、没人发弹幕，空态那句在（对照）", () => {
    const page = runDesktop(lang);
    hello(page, { state: "live", detail: "" });
    page.push({ type: "comment_source", backend: "connected" });
    assert.equal(isShown(page.el("comment-empty")), true);
    assert.equal(page.el("comment-empty").textContent, W.cmtConnected);
    comment(page, 1);
    assert.equal(isShown(page.el("comment-empty")), false);
  });

  for (const prevComments of [true, false]) {
    test(lang + "：U8 第二场开始、这一场还没有弹幕：空态那句出来（上一场" + (prevComments ? "有" : "没有") + "弹幕）", () => {
      const page = runDesktop(lang);
      hello(page, { state: "live", detail: "" });
      caption(page, 1);                                    // 有字幕才画分隔线（session-divider.js）
      page.push({ type: "comment_source", backend: "connected" });
      if (prevComments) comment(page, 1);
      status(page, "idle");
      status(page, "connecting", RESOLVING[lang]);
      page.push({ type: "session_break", ts: T + 60, streamer: "luna_demo" });
      // 分隔线一插，空态那句就出来：不靠后面那条 status 顺带刷新弹幕栏
      assert.equal(isShown(page.el("comment-empty")), true, "session_break 之后、下一条 status 之前");
      status(page, "live");
      page.push({ type: "comment_source", backend: "connected" });
      const list = page.el("comment-list");
      assert.equal(list.querySelectorAll(".session-sep").length, 1, "这条用例要有分隔线才有意义");
      assert.equal(list.querySelectorAll(".cmt-item").length, prevComments ? 1 : 0);
      assert.equal(isShown(page.el("comment-empty")), true);
      assert.equal(page.el("comment-empty").textContent, W.cmtConnected);
      // 评论流状态变了，空态跟着改说法
      page.push({ type: "comment_source", backend: "disconnected" });
      assert.equal(page.el("comment-empty").textContent, W.cmtDisconnected);
      // 这一场的第一条弹幕：空态收起
      page.push({ type: "comment_source", backend: "connected" });
      comment(page, 2);
      assert.equal(isShown(page.el("comment-empty")), false);
    });
  }

  test(lang + "：U8 重连回放：分隔线补在回放弹幕之后，这一场还没有弹幕时空态那句出来", () => {
    const page = runDesktop(lang);
    hello(page, { state: "live", detail: "" }, { comment_backend: "connected" });
    caption(page, 1, { replay: true });
    page.push({ type: "session_break", ts: T + 60, streamer: "luna_demo", replay: true });
    comment(page, 1, { replay: true });                    // ts 早于分隔线：上一场的
    assert.equal(isShown(page.el("comment-empty")), false, "回放还没结束，分隔线还没插：先当它是这一场的");
    page.push({ type: "stats" });                          // 回放之后的第一条实时消息：剩下的分隔线补在末尾
    assert.equal(page.el("comment-list").lastElementChild.classList.contains("session-sep"), true);
    assert.equal(isShown(page.el("comment-empty")), true);
    assert.equal(page.el("comment-empty").textContent, W.cmtConnected);
  });

  // ---- U9：输错地址 ---------------------------------------------------------------------------------

  test(lang + "：U9 输了中文昵称点开始：输入框下面说一句，胶囊不变红、没有横幅、不发 start", () => {
    const page = runDesktop(lang);
    hello(page, { state: "idle", detail: "" });
    const pillBefore = page.el("status-text").textContent;
    assert.equal(pillBefore, W.ready);
    page.el("room-input").value = "某个中文昵称";
    page.el("start-btn").click();
    const err = page.el("room-input-error");
    assert.equal(isShown(err), true);
    assert.match(err.textContent, W.badInput);
    assert.equal(page.el("room-input").getAttribute("aria-invalid"), "true");
    assert.equal(page.el("room-input").getAttribute("aria-describedby"), "room-input-error");
    assert.equal(page.el("status-text").textContent, pillBefore, "胶囊还是待机");
    assert.equal(page.el("status-dot").className, "dot idle");
    assert.equal(isShown(page.el("status-banner")), false, "没有红横幅");
    assert.deepEqual(page.sent.filter((m) => m.type === "start"), []);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：U9 再输入就清掉提示和 aria-invalid；下一次认得出的开始不残留", () => {
    const page = runDesktop(lang);
    hello(page, { state: "idle", detail: "" });
    const input = page.el("room-input");
    const err = page.el("room-input-error");
    input.value = "某个中文昵称";
    page.el("start-btn").click();
    assert.equal(isShown(err), true);
    input.value = "@luna_dem";
    input.fire("input");
    assert.equal(isShown(err), false);
    assert.equal(err.textContent, "");
    assert.equal(input.hasAttribute("aria-invalid"), false);
    assert.equal(input.hasAttribute("aria-describedby"), false);
    // 又输错一次，然后（不经过 input 事件，比如最近直播间的 chip 在背后填好地址）开始一个认得出的
    input.value = "otro nombre";
    page.el("start-btn").click();
    assert.equal(isShown(err), true);
    input.value = ROOM;
    page.el("start-btn").click();
    assert.equal(isShown(err), false, "认得出的开始：提示不残留");
    assert.equal(input.hasAttribute("aria-invalid"), false);
    assert.equal(page.sent.filter((m) => m.type === "start").length, 1);
    status(page, "connecting", RESOLVING[lang]);
    assert.equal(isShown(err), false);
  });

  test(lang + "：U9 输错之后别的页面开了一场：提示跟着收掉，回到首页不留旧提示", () => {
    const page = runDesktop(lang);
    hello(page, { state: "idle", detail: "" });
    page.el("room-input").value = "某个中文昵称";
    page.el("start-btn").click();
    assert.equal(isShown(page.el("room-input-error")), true);
    status(page, "connecting", RESOLVING[lang]);           // 另一个窗口点了开始
    status(page, "idle");
    assert.equal(isShown(page.el("room-input-error")), false);
    assert.equal(page.el("room-input").hasAttribute("aria-invalid"), false);
  });
}
