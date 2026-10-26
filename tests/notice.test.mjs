// 页脚版本号旁的提示（专家审查 U2 的前端那一半；web/app.js showVersionNote / queueNotice / endFooterNote）。
//
// 以前每条 notice 都是 showVersionNote(text, 6000)：6 秒就没，后一条直接顶掉前一条，11px 的 --label-3 淡灰，
// 页脚固定一行高，DeepL 改发 Google 那句（约 100 字）1000 宽时折出的第二行落在窗口外。用户 10-22 选「先做小改动」：
//   1. 服务端发来的提示排队：连发三条，按顺序一条一条显示，每条按字数停 10–12 秒，不互相顶掉；
//   2. 长句不截断：整句写进页脚，页脚这时可以换行长高（.copyright.noting），字用 --label-2；
//   3. 提示都显示完，版权那一行原样恢复（版本号回来，页脚回到一行高）——版权那行字本身不动；
//   4. 页面对一次点击的回应照旧立刻显示；被它顶掉的服务端提示之后完整再显示一遍，不丢；
//      「检查更新中…」是占位，结果一到直接换掉它；
//   5. 顶部已经挂着同一句持续提示（改发 Google：后端 notice 和持续提示各发一次）时页脚不重复这一句。
// 页面在 node:vm 里真的跑一遍（tests/desktop-dom.mjs），中文页、英文页各走一遍。fire(ms) 触发的是时长正好为 ms 的定时器。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { runDesktop, leftoverChinese } from "./desktop-dom.mjs";

const style = readFileSync(new URL("../web/style.css", import.meta.url), "utf8");
const html = readFileSync(new URL("../web/index.html", import.meta.url), "utf8");

const ROOM = "https://www.tiktok.com/@luna_demo/live";

const TEXT = {
  zh: {
    // app/pipeline.py _quota_fallback 改发 Google 那句（约 100 字）
    google: "DeepL 免费额度已用完，且本机没有可用的本地模型，已自动改用 Google 免费接口继续翻译——注意：字幕文本会" +
            "发送给 Google。在 DeepL 升级/续费后重选 DeepL 即可回来。",
    a: "已是最新版本", b: "违禁词表已修改，停止后再开始才生效", c: "浮窗没能打开",
    checking: "检查更新中…", offline: "未连接到本地服务", gone: "这条弹幕已经不在列表里",
  },
  en: {
    google: "The DeepL free quota is used up, and no local model is available on this computer, so the app " +
            "switched to Google (free) to keep translating. Captions are now sent to Google. After you upgrade or " +
            "renew DeepL, choose DeepL again to switch back.",
    a: "You’re on the latest version", b: "The banned-term list changed. Stop and Start to apply it.",
    c: "The floating window couldn’t open",
    checking: "Checking for updates…", offline: "Not connected to the local service",
    gone: "That comment is no longer in the list",
  },
};

// web/app.js noticeMs：10 秒起，每 50 个字多 1 秒，最多 12 秒；汉字和全角标点按两个字算
const msFor = (text) => Math.min(12000, 10000 + 1000 * Math.floor(
  (text.length + (text.match(/[\u3000-\u9fff\uff00-\uffef]/g) || []).length) / 50));

function hello(page, extra) {
  page.push({ type: "hello", config: Object.assign({ status: { state: "live", detail: "" }, room_url: ROOM,
    version: "1.2.3" }, extra || {}) });
}
const notice = (page, text) => page.push({ type: "notice", text });

for (const lang of ["zh", "en"]) {
  const W = TEXT[lang];

  test(lang + "：连发三条提示，按顺序各显示一段时间，不互相顶掉；都显示完版本号回来", () => {
    const page = runDesktop(lang);
    hello(page);
    const foot = () => page.el("app-version").textContent;
    assert.equal(foot(), " · v1.2.3");
    notice(page, W.a);
    notice(page, W.b);
    notice(page, W.c);
    assert.equal(foot(), " · " + W.a, "第一条马上出来");
    for (const [i, text] of [W.a, W.b, W.c].entries()) {
      assert.equal(foot(), " · " + text, "第 " + (i + 1) + " 条");
      assert.ok(msFor(text) >= 10000 && msFor(text) <= 12000);
      page.fire(6000);                                         // 以前的 6 秒到了：不再收起
      assert.equal(foot(), " · " + text, "6 秒后还在");
      page.fire(msFor(text));
    }
    assert.equal(foot(), " · v1.2.3", "版权那一行原样恢复");
    assert.equal(page.el("app-version").cls.includes("noting"), false);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：长句整句写进页脚、按字数多停一会儿；页脚这时可以换行长高，字用 --label-2", () => {
    const page = runDesktop(lang);
    hello(page);
    const foot = () => page.el("app-version").textContent;
    notice(page, W.google);
    assert.equal(foot(), " · " + W.google, "一个字不截");
    assert.equal(msFor(W.google), 12000, "长句停 12 秒（中文约 100 字、英文约 240 个字母都是）");
    assert.equal(page.el("app-version").cls.includes("noting"), true);
    page.fire(10000);
    assert.equal(foot(), " · " + W.google, "10 秒还没到点");
    page.fire(12000);
    assert.equal(foot(), " · v1.2.3");
    // 短句 10 秒
    notice(page, W.c);
    assert.equal(msFor(W.c), 10000);
    page.fire(10000);
    assert.equal(foot(), " · v1.2.3");
  });

  test(lang + "：点了什么的回应立刻显示；被顶掉的服务端提示之后完整再显示一遍；「检查更新中…」结果一到就换掉", () => {
    const page = runDesktop(lang);
    hello(page);
    const foot = () => page.el("app-version").textContent;
    notice(page, W.a);
    notice(page, W.b);
    page.el("app-version").click();                           // 点版本号：「检查更新中…」立刻出来，顶掉正显示的 A
    assert.equal(foot(), " · " + W.checking);
    notice(page, W.a);                                        // 已经排着的同一句不再排第二遍
    // 检查的结果是服务端的 notice：直接换掉占位，不排在它后面等 8 秒
    notice(page, W.c);
    assert.equal(foot(), " · " + W.c);
    page.fire(msFor(W.c));
    // 被顶掉的 A 回来、完整再显示一遍，然后是 B
    for (const text of [W.a, W.b]) {
      assert.equal(foot(), " · " + text);
      page.fire(msFor(text));
    }
    assert.equal(foot(), " · v1.2.3");
    // 页面自己的回应之间照旧后一句换前一句
    page.offline();
    page.el("app-version").click();
    assert.equal(foot(), " · " + W.offline);
    page.online();
    page.el("app-version").click();
    assert.equal(foot(), " · " + W.checking);
  });

  test(lang + "：顶部挂着同一句持续提示时页脚不重复；先到的那句在持续提示挂上时让出来（改发 Google）", () => {
    const page = runDesktop(lang);
    hello(page);
    const foot = () => page.el("app-version").textContent;
    // 后端的顺序：先 notice，再挂持续提示（同一句）
    notice(page, W.a);
    notice(page, W.google);
    page.fire(msFor(W.a));
    assert.equal(foot(), " · " + W.google);
    page.push({ type: "incident", id: "engine:google-fallback", level: "warn", text: W.google });
    assert.equal(page.el("incident-bar").textContent, W.google);
    assert.equal(foot(), " · v1.2.3", "顶部已经在说：页脚这句让出来");
    // 持续提示挂着时又来同一句：不进页脚
    notice(page, W.google);
    assert.equal(foot(), " · v1.2.3");
    // 排着的那条在轮到它之前顶部挂上了同一句：跳过
    notice(page, W.b);
    notice(page, W.c);
    page.push({ type: "incident", id: "session:x", level: "warn", text: W.c });
    page.fire(msFor(W.b));
    assert.equal(foot(), " · v1.2.3", "C 已经在顶部：不再轮到页脚");
  });

  test(lang + "：重连（hello）不抹掉正在显示的提示；回放的版本号等它到点再回来", () => {
    const page = runDesktop(lang);
    hello(page);
    const foot = () => page.el("app-version").textContent;
    notice(page, W.b);
    hello(page, { version: "1.2.4" });
    assert.equal(foot(), " · " + W.b);
    page.fire(msFor(W.b));
    assert.equal(foot(), " · v1.2.4");
  });
}

test("样式：页脚显示提示时可以长高、长句换行，字用 --label-2；平时仍是一行 22px，版权那行字不动", () => {
  const rule = (sel) => {
    const m = style.match(new RegExp("(^|\\n)" + sel.replace(/[.#]/g, "\\$&") + " \\{([^}]*)\\}"));
    assert.ok(m, "找不到 " + sel);
    return m[2];
  };
  assert.match(rule(".copyright"), /height: 22px;/);
  assert.match(rule(".copyright"), /color: var\(--label-3\);/);
  const noting = rule(".copyright.noting");
  assert.match(noting, /height: auto;/);
  assert.match(noting, /min-height: 22px;/);
  assert.match(noting, /line-height: 16px;/);
  assert.match(noting, /padding: 3px 16px;/, "一行时 3 + 16 + 3 = 22px，和平时一样高");
  assert.match(rule("#app-version.noting"), /color: var\(--label-2\);/);
  assert.match(html, /<div class="copyright">Copyright &copy; 2026 Elon Mei \(EM917\)\. All rights reserved\. &middot; <a [^>]*>MIT License<\/a><span id="app-version"/);
});
