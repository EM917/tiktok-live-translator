// 报警面板纯逻辑的契约测试（web/alerts.js）。
//
// 起因：中控把 TikTok LIVE Studio 全屏盖在本程序窗口上时，新报警只是被盖住的窗口里
// 多一行，人什么时候看到没有上限；换过主播后面板上的旧报警和新主播的混在一起；
// 面板只留 50 条，计数却像是全部。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const A = require("../web/alerts.js");

const BASE = "TikTok 直播同传";
const fresh = () => ({ unseen: 0, restoreTo: null });

test("回放的报警永远不改标题", () => {
  const s = A.noteAlert(fresh(), { type: "alert", replay: true }, false, BASE);
  assert.equal(s.title, null);
  assert.equal(s.unseen, 0);
});

test("窗口在前台时不改标题：人看得见", () => {
  const s = A.noteAlert(fresh(), { type: "alert" }, true, BASE);
  assert.equal(s.title, null);
  assert.equal(s.unseen, 0);
});

test("窗口在后台时逐条累加，回到前台恢复原标题", () => {
  let s = A.noteAlert(fresh(), { type: "alert" }, false, BASE);
  assert.equal(s.title, "(1) 疑似违禁词 · TikTok 直播同传");
  s = A.noteAlert(s, { type: "alert" }, false, s.title);
  assert.equal(s.title, "(2) 疑似违禁词 · TikTok 直播同传");
  assert.equal(s.restoreTo, BASE);            // 记的是第一条之前的标题，不是提醒本身
  const back = A.noteActive(s, s.title);
  assert.equal(back.title, BASE);
  assert.equal(back.unseen, 0);
});

test("回到前台时没有未看的报警就不动标题", () => {
  assert.equal(A.noteActive(fresh(), "有新版本 · TikTok 直播同传").title, null);
});

test("期间别的提示改过标题（有新版本）就留着它", () => {
  let s = A.noteAlert(fresh(), { type: "alert" }, false, BASE);
  const back = A.noteActive(s, "有新版本 · TikTok 直播同传");
  assert.equal(back.title, null);
  // 提示改过标题之后又来报警：回到前台时恢复成那条提示，不是更早的标题
  s = A.noteAlert(fresh(), { type: "alert" }, false, BASE);
  s = A.noteAlert(s, { type: "alert" }, false, "有新版本 · TikTok 直播同传");
  assert.equal(A.noteActive(s, s.title).title, "有新版本 · TikTok 直播同传");
});

test("标题里不出现词条原文", () => {
  const s = A.noteAlert(fresh(), { type: "alert", term: "cura el cancer" }, false, BASE);
  assert.ok(!s.title.includes("cura"));
});

test("别的场次的报警标成上一场；没有场次标记的不判", () => {
  assert.equal(A.isOtherSession({ session: "session-a" }, "session-b"), true);
  assert.equal(A.isOtherSession({ session: "session-b" }, "session-b"), false);
  assert.equal(A.isOtherSession({}, "session-b"), false);
  assert.equal(A.isOtherSession({ session: "session-a" }, null), false);
});

test("本场总数多于面板上的条数时说出来", () => {
  assert.equal(A.sessionNote(73, 50), "显示最近 50 条，本场共 73 条");
  assert.equal(A.sessionNote(50, 50), "");
  assert.equal(A.sessionNote(3, 3), "");
  assert.equal(A.sessionNote(12, 0), "显示最近 0 条，本场共 12 条");
  assert.equal(A.ALERT_PANEL_CAP, 50);
});

test("桌面窗口标题：条数变了才告诉窗口，页面刚加载时照发一次清掉旧提醒", () => {
  let r = A.windowAttentionUpdate({ sent: 0, seq: 0 }, 0, false);
  assert.equal(r.send, null);
  r = A.windowAttentionUpdate({ sent: 0, seq: 0 }, 0, true);
  assert.deepEqual(r, { sent: 0, seq: 1, send: 0 });
  r = A.windowAttentionUpdate({ sent: r.sent, seq: r.seq }, 2, false);
  assert.deepEqual(r, { sent: 2, seq: 2, send: 2 });
  const same = A.windowAttentionUpdate({ sent: 2, seq: 2 }, 2, false);
  assert.deepEqual(same, { sent: 2, seq: 2, send: null });
  const back = A.windowAttentionUpdate({ sent: 2, seq: 2 }, 0, false);
  assert.deepEqual(back, { sent: 0, seq: 3, send: 0 });
});
