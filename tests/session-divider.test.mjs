// 桌面页「场次分隔线」纯逻辑的契约测试（web/session-divider.js，node:test，
// 零 npm 依赖）。照 tests/switch.test.mjs 的加载方式。
//
// 起因（见 code review）：renderSessionBreak 的「首场不画线」判断此前只活在
// web/app.js 里，而 app.js 是单个大 IIFE、顶层就摸 document，整份文件没法被
// node:test require，这条判断因此从未被自动化测试盯着——手动删掉这行判断，
// `node --test tests/*.test.mjs` 依旧全绿。抽出这份文件就是为了让它可测。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { loadDesktop, hello, caption, T } from "./desktop-page.mjs";

const require = createRequire(import.meta.url);
const { shouldRenderSessionDivider, sessionDividerText } =
  require("../web/session-divider.js");

// ---- shouldRenderSessionDivider：首场不画线 ----

test("页面里还没有字幕卡片（程序启动后的第一场）：不画分隔线", () => {
  assert.equal(shouldRenderSessionDivider(0), false);
  assert.equal(shouldRenderSessionDivider(undefined), false);
});

test("已经有字幕卡片：画分隔线", () => {
  assert.equal(shouldRenderSessionDivider(1), true);
  assert.equal(shouldRenderSessionDivider(7), true);
});

// ---- sessionDividerText：时间 + 主播名 ----

test("带主播名：时间 + 「以下为 @主播」", () => {
  // 本地时间刻意选个三个字段都是个位数的时刻，同时验证补零
  const d = new Date(2024, 0, 1, 3, 4, 5);
  const ts = d.getTime() / 1000;
  assert.equal(sessionDividerText({ ts: ts, streamer: "brisadelosandes" }),
    "── 03:04:05 以下为 @brisadelosandes ──");
});

test("主播名取不到（空串）：通用文案，不编造身份", () => {
  const d = new Date(2024, 5, 15, 23, 59, 0);
  const ts = d.getTime() / 1000;
  assert.equal(sessionDividerText({ ts: ts, streamer: "" }),
    "── 23:59:00 以下为新的一场 ──");
});

test("msg 缺失/streamer 缺失字段：不抛异常，落到通用文案", () => {
  assert.match(sessionDividerText({}), /^── \d\d:\d\d:\d\d 以下为新的一场 ──$/);
  assert.match(sessionDividerText(undefined), /^── \d\d:\d\d:\d\d 以下为新的一场 ──$/);
});

// ---- 弹幕栏里的分隔线：重连回放之后还在原来的位置（专家审查 F1）----
// 补核实测：实时顺序是「c1 c2 c3 线 c4 c5」，重连后变成「线 c1…c5」，上一位主播的弹幕被标成「以下为 @新主播」。
// 服务端的回放顺序是先字幕（分隔线夹在中间）、最后弹幕（app/server.py replay_messages）；页面在 node:vm 里
// 真跑（tests/desktop-page.mjs），按这个顺序推消息。弹幕的 ts 是本机收到的时间，和分隔线同一个钟

const BREAK_TS = T + 40;
const cmt = (n, ts, extra) => Object.assign({ type: "comment", id: "c" + n, user: "viewer_" + n,
  text: "hola " + n, ts: ts, translated: "你好 " + n, state: "ok" }, extra || {});
const cap = (id, extra) => caption(id, "frase " + id, Object.assign({ translated: "句子 " + id, translate_state: "ok" },
                                                                    extra || {}));
function commentOrder(page) {
  return page.el("comment-list").children.map((c) => (c.cls.includes("session-sep") ? "线" : c.dataset.cmtId)).join(" ");
}
function historySeps(page) {
  return page.el("history").children.filter((c) => c.cls.includes("session-sep")).length;
}

test("弹幕栏：重连回放后分隔线按 ts 插回原处（c1 c2 c3 线 c4 c5），不再跑到所有弹幕最前面", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page);
  // 实时：上一位主播的字幕和弹幕，换主播，新主播的字幕和弹幕
  page.push(cap(1));
  [1, 2, 3].forEach((n) => page.push(cmt(n, T + n)));
  page.push({ type: "session_break", ts: BREAK_TS, streamer: "sol_demo" });
  page.push(cap(2));
  [4, 5].forEach((n) => page.push(cmt(n, BREAK_TS + n)));
  assert.equal(commentOrder(page), "c1 c2 c3 线 c4 c5");

  // 重连：hello，字幕回放（分隔线夹在中间），最后才是弹幕回放
  hello(page);
  page.push(cap(1, { replay: true }));
  page.push({ type: "session_break", ts: BREAK_TS, streamer: "sol_demo", replay: true });
  page.push(cap(2, { replay: true, restore: true }));
  [1, 2, 3].forEach((n) => page.push(cmt(n, T + n, { replay: true })));
  [4, 5].forEach((n) => page.push(cmt(n, BREAK_TS + n, { replay: true })));
  assert.equal(commentOrder(page), "c1 c2 c3 线 c4 c5");
  assert.equal(historySeps(page), 1, "字幕栏的分隔线照旧画在两条字幕之间");

  // 回放之后的实时消息：不会再多补一条
  page.push(cmt(6, BREAK_TS + 6));
  assert.equal(commentOrder(page), "c1 c2 c3 线 c4 c5 c6");
});

test("弹幕栏：换场之后还没有新弹幕，分隔线在回放之后的第一条实时消息到达时补在末尾", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page);
  page.push(cap(1, { replay: true }));
  page.push({ type: "session_break", ts: BREAK_TS, streamer: "sol_demo", replay: true });
  [1, 2, 3].forEach((n) => page.push(cmt(n, T + n, { replay: true })));
  assert.equal(commentOrder(page), "c1 c2 c3", "回放期间还不知道后面有没有更晚的弹幕：先不插");
  page.push({ type: "stats", health: "ok", audio_backlog_sec: 0 });   // 回放之后的第一条实时消息
  assert.equal(commentOrder(page), "c1 c2 c3 线");
  page.push({ type: "stats", health: "ok", audio_backlog_sec: 0 });
  assert.equal(commentOrder(page), "c1 c2 c3 线", "只补一次");
});

test("弹幕栏：程序启动后的第一场（分隔线之前没有字幕卡片）回放时两栏都不画线", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page);
  page.push({ type: "session_break", ts: T, streamer: "luna_demo", replay: true });
  page.push(cap(1, { replay: true, restore: true }));
  [1, 2].forEach((n) => page.push(cmt(n, T + n, { replay: true })));
  page.push({ type: "stats", health: "ok", audio_backlog_sec: 0 });
  assert.equal(commentOrder(page), "c1 c2");
  assert.equal(historySeps(page), 0);
});

test("弹幕栏：hello 清场时还没插的分隔线一并作废，不会带到下一次回放里", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page);
  page.push(cap(1, { replay: true }));
  page.push({ type: "session_break", ts: BREAK_TS, streamer: "sol_demo", replay: true });
  // 弹幕回放还没到又断了：新的 hello 清场重来，这一次缓存里没有分隔线了（比如中控清空过字幕）
  hello(page);
  page.push(cap(2, { replay: true, restore: true }));
  page.push(cmt(1, T + 1, { replay: true }));
  page.push({ type: "stats", health: "ok", audio_backlog_sec: 0 });
  assert.equal(commentOrder(page), "c1");
});
