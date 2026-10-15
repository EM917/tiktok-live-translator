// 2026-10-15 删掉的几处「没有信息量的字」（web/app.js、web/guard.js、web/settings-rows.js、web/index.html）。
//
// 页面在 node:vm 里真的跑一遍（脚手架见 tests/desktop-dom.mjs），中文页、英文页各走一遍同一组用例：
//   1. 状态横幅：直播中（detail 为空）和停止后（detail 为空）横幅收起；演示模式那句「未连接真实直播」
//      照旧显示；直播已结束那句换成短的；
//   2. 评论流已连接：#comment-source 整个收起（圆点是绝对定位在它里面的，只清文字会留下一颗孤点），
//      句子挪进标题的 title；别的状态（连接中、断开、出错）照旧写在标题旁边；
//   3. 弹幕条数：标题上和收起后的入口上都没有数字了（列表封顶 100，数字一直停在 100），入口上的红点照旧；
//   4. 「有新版本」条和「迁移旧词表」条：直播中不摆，回到首页再摆；更新进行中（updating）不收；
//      直播中点页脚版本号手动检查、找到新版本时，条仍不摆，页脚写一句作答（否则那次点击没有任何回应）；
//   5. 首页标题把「或 @用户名」并进去，下面那行字没了，ⓘ 按钮还在、还管着 #input-help；
//   6. 自检行：全绿只写「全部通过」，失败照旧自动展开。
// 纯函数的文案在 tests/guard*.test.mjs、tests/settings-rows*.test.mjs；后端那几句在 tests/test_pipeline_reconnect.py、
// tests/test_i18n_pipeline_en.py。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { runDesktop, leftoverChinese } from "./desktop-dom.mjs";

const html = readFileSync(new URL("../web/index.html", import.meta.url), "utf8");
const appJs = readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const style = readFileSync(new URL("../web/style.css", import.meta.url), "utf8");

const T = 1759000000;
const ROOM = "https://www.tiktok.com/@luna_demo/live";
const UPDATE = { version: "9.9.9", can_auto: true, url: "https://example.invalid/rel" };

const TEXT = {
  en: {
    ended: "The previous session’s captions are below.",
    demo: "Demo mode: built-in sample lines simulate live captions (not connected to a real stream)",
    connected: "Connected",
    updating: "Updating…",
    pass: "All passed",
    fail: "1 feature isn’t working",
    heading: "Enter a TikTok Live Link or @username",
  },
  zh: {
    ended: "上一场字幕在下方。",
    demo: "演示模式：内置台词模拟直播字幕（未连接真实直播）",
    connected: "评论流已连接",
    updating: "更新中…",
    pass: "全部通过",
    fail: "1 项功能未生效",
    heading: "输入 TikTok 直播间地址或 @用户名",
  },
};

const shown = (el) => !el.classList.contains("hidden");

function hello(page, status, extra) {
  page.push({ type: "hello", config: Object.assign({ status: { state: status, detail: "" }, room_url: ROOM,
    version: "1.2.3" }, extra || {}) });
}
const status = (page, state, detail = "") => page.push({ type: "status", state, detail });

for (const lang of ["en", "zh"]) {
  const W = TEXT[lang];

  // ---- 1. 状态横幅 ---------------------------------------------------------------------------------
  test(lang + "：直播中（detail 为空）横幅收起，演示模式那句未连接真实直播的话照旧", () => {
    const page = runDesktop(lang);
    hello(page, "idle");
    status(page, "connecting", "Connecting to the stream audio…");
    assert.ok(shown(page.el("status-banner")), "连接中的说明在横幅里");
    status(page, "live", "");                     // 收到首帧音频：后端发空 detail
    assert.ok(!shown(page.el("status-banner")), "直播中不再摆一条「已连接，开始识别」");
    assert.equal(page.el("status-banner").textContent, "Connecting to the stream audio…", "横幅只是收起，不被改写");
    status(page, "live", W.demo);                 // 演示模式：要提醒这不是真的
    assert.ok(shown(page.el("status-banner")));
    assert.equal(page.el("status-banner").textContent, W.demo);
    assert.ok(page.el("status-banner").classList.contains("info"));
    status(page, "live", "");
    assert.ok(!shown(page.el("status-banner")));
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：停止后（detail 为空）横幅收起；直播已结束那句是短的", () => {
    const page = runDesktop(lang);
    hello(page, "live");
    status(page, "idle", "Stopping…");            // 过渡提示照旧
    assert.ok(shown(page.el("status-banner")));
    status(page, "idle", "");                     // 真正停止：空 detail
    assert.ok(!shown(page.el("status-banner")), "停止后回到输入框，不再多说一句");
    status(page, "ended", W.ended);
    assert.ok(shown(page.el("status-banner")));
    assert.equal(page.el("status-banner").textContent, W.ended);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  // ---- 2. 评论流已连接 -------------------------------------------------------------------------------
  test(lang + "：评论流连上时 #comment-source 整个收起，句子进标题的 title；别的状态照旧", () => {
    const page = runDesktop(lang);
    hello(page, "live");
    const source = page.el("comment-source");
    const head = page.el("comment-title-text");
    page.push({ type: "comment_source", backend: "connected" });
    assert.ok(!shown(source), "整个元素收起，不是只清文字");
    assert.equal(source.textContent, "");
    assert.equal(head.title, W.connected);
    assert.ok(source.classList.contains("on"));
    for (const backend of ["connecting", "disconnected"]) {
      page.push({ type: "comment_source", backend });
      assert.ok(shown(source), backend);
      assert.notEqual(source.textContent, "", backend);
      assert.equal(head.title, "", backend);
    }
    page.push({ type: "comment_source", backend: "error", detail: "HTTP 400" });
    assert.ok(shown(source));
    assert.equal(source.textContent, "HTTP 400");
    assert.ok(source.classList.contains("warn"));
    assert.equal(head.title, "");
    page.push({ type: "comment_source", backend: "connected" });     // 又连上：再收起
    assert.ok(!shown(source));
    assert.equal(head.title, W.connected);
    // 空态那句留着：连上了但还没人发
    assert.notEqual(page.el("comment-empty").textContent, "");
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  // ---- 3. 条数 -------------------------------------------------------------------------------------
  test(lang + "：弹幕标题和收起后的入口上都没有条数，红点照旧", () => {
    const page = runDesktop(lang);
    hello(page, "live");
    assert.equal(page.el("comment-count"), undefined);
    assert.equal(page.el("comment-fab-count"), undefined);
    for (let i = 1; i <= 120; i++) {              // 超过封顶的 100 条：不报错，列表留 100 条
      page.push({ type: "comment", id: "c" + i, user: "ana", text: "hola " + i, ts: T + i, state: "same" });
    }
    assert.equal(page.el("comment-list").children.length, 100);
    page.el("toggle-comments").click();           // 收起：入口出现，里面没有数字
    assert.ok(shown(page.el("comment-fab")));
    assert.ok(!/\d/.test(page.el("comment-fab").textContent));
    assert.ok(page.el("comment-fab-guard"), "红点还在");
    assert.ok(!/\d/.test(page.el("comment-title-text").textContent));
    page.push({ type: "config", guard_state: { enabled: true, status: "rules", backend: "none", rules: { count: 1, warnings: 0 } } });
    page.el("clear-comments").click();            // 清空也不碰已删的数字元素
    assert.equal(page.el("comment-list").children.length, 0);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：index.html 里没有条数元素，也没有它们的样式", () => {
    assert.ok(!/id="comment-count"/.test(html));
    assert.ok(!/id="comment-fab-count"/.test(html));
    assert.ok(!/comment-count|comment-fab-count|commentItemCount/.test(appJs));
    assert.ok(!/#comment-count|#comment-fab-count/.test(style));
  });

  // ---- 4. 更新条、迁移条 ----------------------------------------------------------------------------
  test(lang + "：「有新版本」条直播中不摆，回到首页再摆；更新进行中不收", () => {
    const page = runDesktop(lang);
    const bar = page.el("update-bar");
    hello(page, "idle", { update: UPDATE });
    assert.ok(shown(bar), "首页摆着");
    status(page, "connecting");                  // 开播
    assert.ok(!shown(bar), "直播中收起");
    page.push({ type: "update_available", version: "9.9.10", can_auto: true, url: "https://example.invalid/r2" });
    assert.ok(!shown(bar), "直播中新到的提示先不摆");
    assert.match(page.el("update-text").textContent, /9\.9\.10/, "但内容已经更新，回到首页摆出来的是新的");
    status(page, "live");
    assert.ok(!shown(bar));
    status(page, "idle", "");                    // 停止
    assert.ok(shown(bar), "回到首页再摆出来");

    status(page, "connecting");
    assert.ok(!shown(bar));
    page.push({ type: "updating" });             // 更新开始：监听被暂停，按钮上的进度是唯一的反馈
    assert.ok(shown(bar), "更新进行中不收");
    assert.equal(page.el("update-btn").textContent, W.updating);
    status(page, "connecting", "Updating. Monitoring is paused and resumes in about 1 minute.");
    assert.ok(shown(bar));
    page.push({ type: "update_aborted" });       // 没更新成：监听没断，条跟着收起
    assert.ok(!shown(bar));
    status(page, "idle", "");
    assert.ok(shown(bar), "回到首页又摆出来");
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：直播中手动检查更新找到新版本：条不摆，页脚写一句作答；自动提示和静默提示不写", () => {
    const page = runDesktop(lang);
    const foot = () => page.el("app-version").textContent;
    hello(page, "live");
    const manualCheck = () => {
      page.el("app-version").click();
      assert.match(foot(), lang === "en" ? /Checking for updates/ : /检查更新中/);
    };
    // 手动点：服务端回 update_available（手动检查永远不是静默的）
    manualCheck();
    page.push({ type: "update_available", version: "9.9.9", can_auto: true, url: "https://example.invalid/r", quiet: false });
    assert.ok(!shown(page.el("update-bar")), "直播中条照旧不摆");
    assert.match(foot(), /9\.9\.9/, "但页脚有回应，不是「检查中…」8 秒后悄悄变回版本号");
    assert.ok(!/检查更新中|Checking for updates/.test(foot()));
    assert.equal(foot().includes("1.2.3"), false, "回应顶替了版本号那一行");
    page.fire(8000);                              // 一次性提示到点：版本号回来
    assert.match(foot(), /v1\.2\.3/);
    // 没人点：自动检查到的提示（非静默）直播中不在页脚打扰
    page.push({ type: "update_available", version: "9.9.10", can_auto: true, url: "https://example.invalid/r", quiet: false });
    assert.match(foot(), /v1\.2\.3/, "没点过就不写");
    // 点过但答案已经过期（超过窗口）：同样不写
    manualCheck();
    page.advance(13000);
    page.push({ type: "update_available", version: "9.9.11", can_auto: true, url: "https://example.invalid/r", quiet: false });
    assert.ok(!/9\.9\.11/.test(foot()), "超过窗口的不算这次手动检查的答案");
    // 静默提示不写
    manualCheck();
    page.push({ type: "update_available", version: "9.9.12", can_auto: true, url: "https://example.invalid/r", quiet: true });
    assert.ok(!/9\.9\.12/.test(foot()));
    // 一次点击只答一次
    page.push({ type: "update_available", version: "9.9.13", can_auto: true, url: "https://example.invalid/r", quiet: false });
    assert.match(foot(), /9\.9\.13/, "窗口内第一条非静默的作答");
    page.fire(8000);
    page.push({ type: "update_available", version: "9.9.14", can_auto: true, url: "https://example.invalid/r", quiet: false });
    assert.ok(!/9\.9\.14/.test(foot()), "答过一次就不再写");
    // 回到首页：条摆出来，是最新的那个版本
    status(page, "idle", "");
    assert.ok(shown(page.el("update-bar")));
    assert.match(page.el("update-text").textContent, /9\.9\.14/);
    // 首页上手动检查：条本来就在，不再多写一句
    page.fire(8000);
    manualCheck();
    page.push({ type: "update_available", version: "9.9.15", can_auto: true, url: "https://example.invalid/r", quiet: false });
    assert.ok(shown(page.el("update-bar")));
    assert.ok(!/9\.9\.15/.test(foot()), "条已经摆着，页脚不重复");
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：重连回放（hello）按当时的状态决定摆不摆；没有新版本就收起", () => {
    const page = runDesktop(lang);
    hello(page, "live", { update: UPDATE });
    assert.ok(!shown(page.el("update-bar")), "页面刷新时正在直播：不摆");
    status(page, "idle", "");
    assert.ok(shown(page.el("update-bar")));
    hello(page, "idle");                          // 重连：服务端说已经没有新版本了
    assert.ok(!shown(page.el("update-bar")));
    hello(page, "idle", { update: UPDATE });
    assert.ok(shown(page.el("update-bar")));
    assert.equal(page.document.title, "TikTok Live Translator", "回放的提示不改标题");
  });

  test(lang + "：「迁移旧词表」条同理；迁移完收起；页脚版本号照旧能点来检查更新", () => {
    const page = runDesktop(lang);
    const bar = page.el("migrate-bar");
    hello(page, "live");
    page.push({ type: "glossary_migration", stage: "available", count: 3 });
    assert.ok(!shown(bar), "直播中不摆");
    status(page, "idle", "");
    assert.ok(shown(bar), "回到首页摆出来");
    status(page, "connecting");
    assert.ok(!shown(bar));
    status(page, "idle", "");
    assert.ok(shown(bar));
    page.push({ type: "glossary_migration", stage: "done", result: { total: 3, failed: 0, backup: "glossary.bak" } });
    assert.ok(!shown(bar), "迁移完成就收起");
    status(page, "connecting");
    status(page, "idle", "");
    assert.ok(!shown(bar), "不会因为回到首页又冒出来");
    page.el("app-version").click();
    assert.deepEqual(page.sent.filter((m) => m.type === "check_update"), [{ type: "check_update" }]);
  });

  // ---- 5. 首页标题 ----------------------------------------------------------------------------------
  test(lang + "：首页标题带上「或 @用户名」，下面那行字没了，ⓘ 还在、还管着输入说明", () => {
    assert.ok(!/class="sp-sub"/.test(html));
    assert.ok(!/粘贴直播间链接，或只输入主播用户名/.test(html));
    const h2 = html.match(/<h2 data-en="([^"]*)">([^<]*)<\/h2>/);
    assert.ok(h2, "找不到首页标题");
    assert.equal(lang === "en" ? h2[1] : h2[2], W.heading);
    // ⓘ 在标题旁边（同一个 .sp-title 里），不在 <h2> 里；aria-controls 和说明段落都在
    const row = html.match(/<div class="sp-title">([\s\S]*?)<\/div>/);
    assert.ok(row && row[1].includes("<h2") && row[1].includes('id="input-help-btn"'));
    assert.ok(!/<h2[^>]*>[^<]*<button/.test(html));
    assert.match(html, /id="input-help-btn"[^>]*aria-controls="input-help"/);
    assert.match(html, /id="input-help" class="sp-help hidden"/);
    const page = runDesktop(lang);
    const btn = page.el("input-help-btn");
    const help = page.el("input-help");
    assert.equal(btn.getAttribute("aria-expanded"), "false");
    assert.ok(!shown(help));
    btn.click();
    assert.ok(shown(help));
    assert.equal(btn.getAttribute("aria-expanded"), "true");
    btn.click();
    assert.ok(!shown(help));
    assert.equal(btn.getAttribute("aria-expanded"), "false");
  });

  // ---- 6. 自检行 ------------------------------------------------------------------------------------
  test(lang + "：自检全绿只写结论，不报几项；有失败照旧自动展开", () => {
    const page = runDesktop(lang);
    hello(page, "idle");
    const checks = (level) => [{ name: "Speech Recognition", level, detail: "ok" }];
    page.push({ type: "selfcheck", summary: { fail: 0, warn: 0, total: 12 }, checks: checks("pass") });
    assert.equal(page.el("sc-summary").textContent, W.pass);
    assert.ok(!shown(page.el("sc-list")), "全绿收着");
    page.push({ type: "selfcheck", summary: { fail: 1, warn: 0, total: 12 }, checks: checks("fail") });
    assert.equal(page.el("sc-summary").textContent, W.fail);
    assert.ok(shown(page.el("sc-list")), "有失败自动展开");
    page.push({ type: "selfcheck", summary: { fail: 0, warn: 0, total: 12 }, checks: checks("pass") });
    assert.equal(page.el("sc-summary").textContent, W.pass);
    assert.ok(!shown(page.el("sc-list")), "回到全绿又收起");
  });
}
