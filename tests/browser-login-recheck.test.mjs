// 「我加好了，再查一次」（web/app.js recheckButton / sendRecheck；后端 app/pipeline.py _recheck_browser_login）。
//
// 2026-10-29 新 Mac 上，中控照着「完全磁盘访问权限」那几步做完，在点「开始翻译」之前没有任何地方告诉他成没成。
// 这里在假 DOM 里把桌面页跑一遍（脚手架见 tests/desktop-dom.mjs），中文页、英文页各走一遍：
//   * 「没有读到 TikTok 登录」那条带 recheck 时，第一行（「显示全部步骤」后面）有这颗按钮，折着也看得见；不带的没有；
//   * 自检「浏览器登录态」那一行带 recheck 时，那几步下面有这颗按钮；
//   * 点了发 {type: "recheck_browser_login"}，不带别的字段；
//   * 页面上有这颗按钮时，离开窗口再回来（blur→focus、visibilitychange）自动发一次，带 auto；两次之间至少隔 5 秒；
//     没离开过、按钮不在了，都不发；
//   * 刷新页面（hello 回放 config.incidents）照样画出按钮。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { runDesktop, leftoverChinese } from "./desktop-dom.mjs";

const style = readFileSync(new URL("../web/style.css", import.meta.url), "utf8");
const ID = "login-unreadable";

// 样例照 app/browser_login.py no_login_notice 的样子抄（虚构路径）：第一行是看到了什么 + 那几步的头一句
const TEXT = {
  zh: {
    notice: "解析流地址时，登录优先这一步只读 Safari 里的 TikTok 登录——Safari：系统拒绝读取。照下面三步给 Python 打开「完全磁盘访问权限」：\n" +
      "1. 打开「系统设置」→「隐私与安全性」→「完全磁盘访问权限」。\n" +
      "2. 点「+」，在选文件的窗口里按 ⌘⇧G，粘贴下面的路径后回车、点「打开」，再把它的开关打开：\n" +
      "「/opt/demo/bin/python3.13」\n列表里显示的名字是 python3.13，不是本程序的名字。\n3. 按 ⌘Q 完全退出本程序，再打开它。\n" +
      "有的直播间 TikTok 只把流地址给已登录的观众。读到登录后这条提示自动消失。",
    signIn: "解析流地址时，登录优先这一步只读 Safari 里的 TikTok 登录——Safari：能读取，没有 TikTok 登录 cookie（sessionid 等）。请在 Safari 里登录 TikTok。",
    again: "我加好了，再查一次", more: "显示全部步骤",
    row: { name: "浏览器登录态", level: "warn", detail: "Chrome：系统拒绝读取；Safari：系统拒绝读取",
           fix: "系统不允许本程序读取浏览器数据。照下面三步给 Python 打开「完全磁盘访问权限」：\n1. …", recheck: true },
    okRow: { name: "浏览器登录态", level: "ok", detail: "Safari：读到了 TikTok 登录", fix: "" },
  },
  en: {
    notice: "When looking up the stream URL, the app first reads the TikTok sign-in from Safari only. Safari: macOS didn’t allow access. Follow these three steps to turn on Full Disk Access for Python:\n" +
      "1. Open System Settings > Privacy & Security > Full Disk Access.\n" +
      "2. Click +. In the file window, press ⌘⇧G, paste the path below, press Return, click Open, and turn on its switch:\n" +
      "“/opt/demo/bin/python3.13”\nThe list shows python3.13, not the app’s name.\n3. Press ⌘Q to quit the app completely, then open it again.\n" +
      "For some live streams, TikTok provides the stream URL only to signed-in viewers. This notice disappears once a sign-in is found.",
    signIn: "When looking up the stream URL, the app first reads the TikTok sign-in from Safari only. Safari: readable, but no TikTok sign-in cookie (sessionid or similar). Sign in to TikTok in Safari.",
    again: "Check Again", more: "Show All Steps",
    row: { name: "Browser Login", level: "warn", detail: "Chrome: macOS didn’t allow access; Safari: macOS didn’t allow access",
           fix: "macOS didn’t allow the app to read browser data. Follow these three steps to turn on Full Disk Access for Python:\n1. …", recheck: true },
    okRow: { name: "Browser Login", level: "ok", detail: "Safari: found a TikTok sign-in", fix: "" },
  },
};

const shown = (el) => !el.classList.contains("hidden");
const rechecks = (page) => page.sent.filter((m) => m.type === "recheck_browser_login");

function hello(page, extra) {
  page.push({ type: "hello", config: Object.assign({ status: { state: "live", detail: "" }, version: "1.2.3" },
                                                   extra || {}) });
}
const incident = (page, more) => page.push(Object.assign({ type: "incident", id: ID, level: "warn", ts: 1 }, more));
const span = (page) => page.el("incident-bar").children[0].lastChild;
const button = (el, cls) => el.querySelector("." + cls);
const selfcheck = (page, checks) => page.push({ type: "selfcheck", checks, summary: { warn: 1, fail: 0 } });

for (const lang of ["zh", "en"]) {
  const W = TEXT[lang];

  test(lang + "：系统拒绝读取那条持续提示的第一行带「我加好了，再查一次」，折着也看得见；点了发 recheck_browser_login", () => {
    const page = runDesktop(lang);
    hello(page);
    incident(page, { text: W.notice, recheck: true });
    const s = span(page);
    const more = button(s, "banner-more");
    const again = button(s, "incident-recheck");
    assert.ok(more, "超过 3 行：折起来，有「显示全部步骤」");
    assert.ok(again, "有「我加好了，再查一次」");
    assert.equal(again.textContent, W.again);
    assert.equal(again.tagName, "BUTTON");
    const rest = button(s, "incident-rest");
    assert.equal(shown(rest), false, "其余几行折着");
    const kids = s.children;
    assert.ok(kids.indexOf(more) < kids.indexOf(again) && kids.indexOf(again) < kids.indexOf(rest),
              "按钮接在第一行的字和「显示全部步骤」后面，不在折起来的那几行里");
    assert.ok(!rest.contains(again));
    again.click();
    assert.deepEqual(rechecks(page), [{ type: "recheck_browser_login" }], "手点的不带 auto，也不带别的字段");
    // 展开、收起不影响这颗按钮
    more.click();
    assert.ok(button(span(page), "incident-recheck"));
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：不带 recheck 的（读得到、没登录）没有这颗按钮；撤掉以后也没有", () => {
    const page = runDesktop(lang);
    hello(page);
    incident(page, { text: W.signIn });
    assert.equal(button(span(page), "incident-recheck"), null);
    incident(page, { text: W.notice, recheck: true });
    assert.ok(button(span(page), "incident-recheck"));
    incident(page, { text: W.signIn });              // 重查后换成叫人去登录的那种：按钮跟着没了
    assert.equal(button(span(page), "incident-recheck"), null);
    incident(page, { level: "clear" });
    assert.equal(page.el("incident-bar").children.length, 0);
  });

  test(lang + "：刷新页面（hello 回放 config.incidents）照样画出这颗按钮", () => {
    const page = runDesktop(lang);
    hello(page, { incidents: { [ID]: { id: ID, level: "warn", text: W.notice, since: 1, ts: 1, recheck: true } } });
    assert.equal(button(span(page), "incident-recheck").textContent, W.again);
  });

  test(lang + "：自检「浏览器登录态」那一行带那几步时，下面有「我加好了，再查一次」", () => {
    const page = runDesktop(lang);
    hello(page, { status: { state: "idle", detail: "" } });
    selfcheck(page, [W.row]);
    const li = page.el("sc-list").children[0];
    const again = button(li, "sc-recheck");
    assert.ok(again, "带 recheck 的那一行有按钮");
    assert.equal(again.textContent, W.again);
    assert.ok(again.cls.includes("btn") && again.cls.includes("small"));
    again.click();
    assert.deepEqual(rechecks(page), [{ type: "recheck_browser_login" }]);
    selfcheck(page, [W.okRow]);
    assert.equal(button(page.el("sc-list").children[0], "sc-recheck"), null, "读到登录：按钮没了");
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：页面上有这颗按钮时，离开窗口再回来自动发一次（带 auto），两次之间至少隔 5 秒", () => {
    const page = runDesktop(lang);
    hello(page);
    // 没有按钮：离开、回来都不发
    page.winFire("blur");
    page.winFire("focus");
    assert.deepEqual(rechecks(page), []);
    incident(page, { text: W.notice, recheck: true });
    // 没离开过就拿到焦点（比如点了一下窗口）：不发
    page.winFire("focus");
    assert.deepEqual(rechecks(page), []);
    // 去系统设置（窗口失焦）再切回来：发一次
    page.winFire("blur");
    page.winFire("focus");
    assert.deepEqual(rechecks(page), [{ type: "recheck_browser_login", auto: true }]);
    // 马上又切走、切回来：不到 5 秒，不发
    page.advance(4999);
    page.winFire("blur");
    page.winFire("focus");
    assert.equal(rechecks(page).length, 1);
    // 过了 5 秒：再切一次就发（上一次没发出去的那次离开也算数）
    page.advance(1);
    page.winFire("focus");
    assert.equal(rechecks(page).length, 2);
    // 手点的也算一次：紧接着切回来不再自动发
    page.advance(10000);
    button(span(page), "incident-recheck").click();
    page.winFire("blur");
    page.winFire("focus");
    assert.deepEqual(rechecks(page).map((m) => !!m.auto), [true, true, false]);
  });

  test(lang + "：visibilitychange 同样算离开、回来；按钮不在了就不发；断线时发不出去，下次回来再发", () => {
    const page = runDesktop(lang);
    hello(page);
    incident(page, { text: W.notice, recheck: true });
    page.document.visibilityState = "hidden";
    page.docFire("visibilitychange");
    page.document.visibilityState = "visible";
    page.docFire("visibilitychange");
    assert.deepEqual(rechecks(page), [{ type: "recheck_browser_login", auto: true }]);
    // 离开期间提示撤了（比如别处重查读到了登录）：回来不发
    page.advance(6000);
    page.winFire("blur");
    incident(page, { level: "clear" });
    page.winFire("focus");
    assert.equal(rechecks(page).length, 1);
    // 只有自检那一行带按钮（待机时）：一样算
    hello(page, { status: { state: "idle", detail: "" } });
    selfcheck(page, [W.row]);
    page.winFire("blur");
    page.offline();
    page.winFire("focus");
    assert.equal(rechecks(page).length, 1, "连接断着：没发出去");
    page.online();
    page.winFire("focus");
    assert.equal(rechecks(page).length, 2, "连上以后再回来：补发");
  });
}

test("样式：持续提示里的「我加好了，再查一次」用这一条的字色加下划线（橙底、红底上的蓝字对比不够）", () => {
  assert.match(style, /\.incident \.incident-recheck \{[^}]*color: inherit;[^}]*text-decoration: underline;/);
  assert.match(style, /\.sc-item \.sc-fix \.sc-recheck \{[^}]*display: block;/);
});
