// 「连续多条字幕没有译出来」横幅和「超过 20 秒没有连上本地服务」那句（专家审查 U4；web/app.js
// trackTranslateHealth / translateFailText、ws.onclose）。
//
// 以前中文写「翻译服务可能暂时连不上」「本地程序似乎已经关闭」，猜了原因，英文却只说观察，两边意思不一样；
// 英文还叫人直接去 Settings > Translation Engine，而设置分组在直播中整块收起。现在中英都只写看到的事和能做的
// 事，翻译失败那句写明先点「停止」。用户 10-22 定 D3「只管翻译失败这一条」：自动检查（tests/i18n_rules.py 的
// RULE8_FAMILY）不扩大到整个桌面页，这两句由这里单独钉住——中英两臂都不含「可能 / 似乎 / might / may」这类词。
//
// 页面在 node:vm 里真跑一遍（tests/desktop-dom.mjs），中文页、英文页各走一遍：页面上显示的就是那一臂。
import { test } from "node:test";
import assert from "node:assert/strict";

import { runDesktop, leftoverChinese } from "./desktop-dom.mjs";

const ROOM = "https://www.tiktok.com/@luna_demo/live";
const T = 1700000000;

const SAID = {
  zh: {
    fail: "连续多条字幕没有译出来，字幕先显示原文（语音识别不受影响）。" +
          "一直这样的话，点「停止」后可以在「设置 > 翻译引擎」里换一个引擎。",
    offline: "超过 20 秒没有连上本地服务。如果程序已经关闭，请重新打开：macOS 双击「TikTok Live Translator.app」，" +
             "Windows 双击「Start.bat」。",
    stop: "点「停止」后", where: "「设置 > 翻译引擎」",
  },
  en: {
    fail: "The last several captions couldn’t be translated, so captions show the original text. " +
          "Speech recognition isn’t affected. If this continues, you can choose another engine in " +
          "Settings > Translation Engine after you click Stop.",
    offline: "No connection to the app’s local service for over 20 seconds. If the app has quit, reopen it. " +
             "On Mac, double-click TikTok Live Translator.app. On Windows, double-click Start.bat.",
    stop: "after you click Stop", where: "Settings > Translation Engine",
  },
};

// 把猜测当原因的说法（docs/i18n-style.md §2.5、§2.6）。中文这里比 ZH_CAUSAL 更严：光「可能」二字也不许——
// 这两句没有要说「将来可能发生」的事
const GUESS = {
  zh: /可能|似乎|好像|多半|八成|大概|估计|也许|或许|恐怕|看起来|应该是|因为|由于|导致|原因/,
  en: /\b(might|may|maybe|perhaps|probably|likely|seems?|apparently|because|due to|caused by|the reason)\b/i,
};

function hello(page) {
  page.push({ type: "hello", config: { status: { state: "live", detail: "" }, room_url: ROOM, version: "1.2.3" } });
}
const failed = (page, id, extra) =>
  page.push(Object.assign({ type: "caption", id, ts: T, original: "hola " + id, translate_state: "failed" }, extra || {}));
const banner = (page) => page.el("status-banner");
const shown = (el) => !el.classList.contains("hidden");

for (const lang of ["zh", "en"]) {
  const W = SAID[lang];

  test(lang + "：两句都只写看到的事和能做的事，不含猜原因的词（U4，D3 只管这一条）", () => {
    assert.doesNotMatch(W.fail, GUESS[lang]);
    assert.doesNotMatch(W.offline, GUESS[lang]);
  });

  test(lang + "：连续 4 条没译出来才挂横幅，写明先点「停止」再去「设置 > 翻译引擎」换引擎（U4）", () => {
    const page = runDesktop(lang);
    hello(page);
    for (let id = 1; id <= 3; id++) failed(page, id);
    assert.equal(shown(banner(page)), false, "3 条还不挂");
    failed(page, 4);
    assert.equal(shown(banner(page)), true);
    assert.equal(banner(page).textContent, W.fail);
    assert.ok(banner(page).textContent.includes(W.stop), "直播中设置收起：要先说停止");
    assert.ok(banner(page).textContent.includes(W.where), "位置写全，和别处的说法一致（V5）");
    assert.doesNotMatch(banner(page).textContent, GUESS[lang]);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：拒答、结果不像译文不算进连续失败；译出一条就收起", () => {
    const page = runDesktop(lang);
    hello(page);
    for (let id = 1; id <= 3; id++) failed(page, id);
    failed(page, 4, { refused: true });
    failed(page, 5, { not_translation: true });
    assert.equal(shown(banner(page)), false, "接口正常回了话：不算翻译失败");
    failed(page, 6);
    assert.equal(banner(page).textContent, W.fail);
    page.push({ type: "caption", id: 7, ts: T, original: "x", translated: "y", translate_state: "ok" });
    assert.equal(shown(banner(page)), false);
  });

  test(lang + "：断开超过 20 秒说「超过 20 秒没有连上本地服务」，程序关了的话怎么重新打开（U4）", () => {
    const page = runDesktop(lang);
    hello(page);
    page.closeSocket();
    page.advance(21000);
    page.closeSocket();
    assert.equal(banner(page).textContent, W.offline);
    assert.doesNotMatch(banner(page).textContent, GUESS[lang]);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });
}
