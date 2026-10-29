// 界面语言的两个「一眼认得出」的入口（用户 2026-10-29 定，lang-2 选 C）。
//
// 10-29 用户（中文中控）被困在英文界面里：界面语言只有设置分组最后一行一个入口，英文界面里它叫
// App Language，收起时摘要写 System · English，一个中文字都没有，是一步步指路才找到的。
//   1. 待机首页标题区右上角加一个「中文 / English」两段切换（#lang-quick）。两颗按钮都写语言自称，
//      带 lang 和 translate="no"（和 #ui-lang-select 的选项同一种标法）：看不懂当前界面的人也认得出。
//      按下的是此刻的界面语言；点另一颗发和设置行同一条 set_ui_lang；点按下的那颗什么都不发，
//      设置里的「跟随系统」不会被它悄悄改成固定语言（「跟随系统」只在设置那一行选）。
//      闸关着、又没有 --ui-lang 时（config 里没有 ui_lang_available）整组不出现；--ui-lang 固定时置灰。
//   2. 设置行名两种界面对称：中文页「界面语言 · Language」（原样），英文页「App Language · 界面语言」，
//      带上的另一种语言名标 lang 和 translate="no"。
// 浏览器里的位置和宽度（不压住图标和标题、不出窗口）由 G10 的 home-* 场景量（tests/test_i18n_dom.py，CI 的 macOS 跑器）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

import { hello as helloPage, loadDesktop, web } from "./desktop-page.mjs";

const require = createRequire(import.meta.url);
const { langQuickState, langQuickSend } = require("../web/settings-rows.js");

const stripHtmlComments = (html) => html.replace(/<!--[\s\S]*?-->/g, "");
const stripCssComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");

// ---- 纯函数（web/settings-rows.js） ---------------------------------------------------------

test("langQuickState：没有 ui_lang_available（闸关着）就不出现；按下的是此刻的界面语言；--ui-lang 固定时 locked", () => {
  assert.equal(langQuickState(null), null);
  assert.equal(langQuickState({}), null);
  assert.equal(langQuickState({ ui_lang_available: false, ui_lang: "en" }), null);
  assert.equal(langQuickState({ ui_lang_available: "true", ui_lang: "en" }), null, "只认 true");
  const base = { ui_lang_available: true, ui_lang_setting: "system", ui_lang_system: "en" };
  assert.deepEqual(langQuickState(Object.assign({ ui_lang: "en" }, base)), { pressed: "en", locked: false },
                   "跟随系统时按下的是它此刻解析成的那种");
  assert.deepEqual(langQuickState(Object.assign({}, base, { ui_lang: "zh", ui_lang_setting: "zh" })),
                   { pressed: "zh", locked: false });
  assert.deepEqual(langQuickState(Object.assign({}, base, { ui_lang: "fr" })), { pressed: "zh", locked: false },
                   "认不出的语言按中文（同服务端：宁可中文）");
  assert.deepEqual(langQuickState(Object.assign({}, base, { ui_lang: "en", ui_lang_locked: true })),
                   { pressed: "en", locked: true });
});

test("langQuickSend：点另一种语言才发；点按下的那颗、被固定、认不出的值都不发", () => {
  const zh = { pressed: "zh", locked: false };
  assert.equal(langQuickSend(zh, "en"), "en");
  assert.equal(langQuickSend(zh, "zh"), null, "已经是中文：不把「跟随系统」改成固定的中文");
  assert.equal(langQuickSend({ pressed: "en", locked: false }, "zh"), "zh");
  assert.equal(langQuickSend({ pressed: "zh", locked: true }, "en"), null);
  assert.equal(langQuickSend(zh, "system"), null, "跟随系统只在设置那一行选");
  assert.equal(langQuickSend(zh, "fr"), null);
  assert.equal(langQuickSend(null, "en"), null);
});

// ---- 静态页面 ---------------------------------------------------------------------------------

/** index.html 里 #lang-quick 的开标签和两颗按钮：{ attrs, buttons: [[开标签属性, 文字], ...] } */
function staticQuick() {
  const html = stripHtmlComments(web("index.html"));
  const m = /<div id="lang-quick"([^>]*)>([\s\S]*?)<\/div>/.exec(html);
  assert.ok(m, "index.html 里找不到 #lang-quick");
  const buttons = [...m[2].matchAll(/<button([^>]*)>([^<]*)<\/button>/g)].map((b) => [b[1], b[2]]);
  return { attrs: m[1], buttons, html };
}

test("index.html：两段切换在首页标题区（.sp-hero）里，默认藏着，是一组带名字的按钮", () => {
  const { attrs, buttons, html } = staticQuick();
  assert.match(attrs, /\sclass="lang-quick hidden"/, "闸关着时 config 里没有 ui_lang_available：必须一直藏着");
  assert.match(attrs, /\srole="group"/);
  assert.match(attrs, /\saria-label="界面语言"/);
  assert.match(attrs, /\sdata-en-aria-label="App Language"/, "组名和设置行名同一对");
  const hero = html.indexOf('<div class="sp-hero">');
  const title = html.indexOf('<div class="sp-title">');
  const quick = html.indexOf('id="lang-quick"');
  assert.ok(hero > 0 && hero < quick && quick < html.indexOf('<div class="sp-input-row"'), "在标题区里");
  assert.ok(quick > title, "排在标题后面：读屏和 Tab 先到标题和「输入说明」");
  assert.equal(buttons.length, 2);
  const want = [["lang-quick-zh", "zh-CN", "中文"], ["lang-quick-en", "en", "English"]];
  buttons.forEach(([a, text], i) => {
    const [id, lang, name] = want[i];
    assert.match(a, new RegExp('\\sid="' + id + '"'));
    assert.match(a, /\stype="button"/);
    assert.match(a, new RegExp('\\slang="' + lang + '"'), "按钮的 lang 是它自己的语言：读屏换对的声音念");
    assert.match(a, /\stranslate="no"/, "语言自称是数据，哪种界面都不翻");
    assert.match(a, /\saria-pressed="false"/);
    assert.doesNotMatch(a, /\saria-label=|\sdata-en/, "名字就是自称本身，不另写一句");
    assert.equal(text, name);
  });
});

test("index.html：设置行名中文页「界面语言 · Language」，英文页「App Language · 界面语言」，另一种语言名标 lang 和 translate=\"no\"", () => {
  const html = stripHtmlComments(web("index.html"));
  const m = /<span class="set-name">(<span data-en="App Language">界面语言<\/span>[\s\S]*?)<\/span>\n/.exec(html);
  assert.ok(m, "找不到 #lang-head 的行名");
  assert.ok(html.indexOf(m[0]) > html.indexOf('id="lang-head"') && html.indexOf(m[0]) < html.indexOf('id="lang-summary"'));
  const inner = m[1];
  assert.match(inner, / · <span id="lang-head-en" class="zh-page-only" lang="en" translate="no">Language<\/span>/);
  assert.match(inner, /<span id="lang-head-zh" class="en-page-only" lang="zh-CN" translate="no">界面语言<\/span>/);
  // 中文页看到的字：去掉英文页才有的那段，拼回来和原来逐字相同（中文一字不动）
  const zhText = inner.replace(/<span[^>]*class="en-page-only"[^>]*>[^<]*<\/span>/, "").replace(/<[^>]+>/g, "");
  assert.equal(zhText, "界面语言 · Language");
  const enText = inner.replace(/<span[^>]*class="zh-page-only"[^>]*>[^<]*<\/span>/, "")
    .replace(/<span data-en="App Language">界面语言<\/span>/, "App Language").replace(/<[^>]+>/g, "");
  assert.equal(enText, "App Language · 界面语言");
});

test("style.css：两段切换叠在标题区右上角，不挤动居中的图标和标题；行名的另一种语言名按页面语言二选一", () => {
  const css = stripCssComments(web("style.css"));
  assert.match(css, /\.sp-hero \{[^}]*position: relative;/);
  assert.match(css, /\.lang-quick \{[^}]*position: absolute;[^}]*top: 0;[^}]*right: 0;/);
  assert.match(css, /\.lang-quick-btn\[aria-pressed="true"\] \{/);
  assert.match(css, /\.lang-quick-btn:disabled \{/);
  assert.match(css, /html\[lang="en"\] \.zh-page-only,\s*html:not\(\[lang="en"\]\) \.en-page-only \{ display: none; \}/);
});

// ---- 页面里真跑（desktop-page.mjs） -------------------------------------------------------

/** 待机首页的 hello（desktop-page.mjs 的那条换成待机），再并进 cfg。 */
function hello(p, cfg) {
  helloPage(p, Object.assign({ status: { state: "idle", detail: "" } }, cfg));
}
const LANG = (lang, extra) => Object.assign({ ui_lang_available: true, ui_lang_setting: "system",
                                              ui_lang: lang, ui_lang_system: lang }, extra || {});
const shown = (p) => !p.el("lang-quick").classList.contains("hidden");
const pressed = (p) => ["zh", "en"].filter((l) => p.el("lang-quick-" + l).getAttribute("aria-pressed") === "true");
const uiLangSent = (p) => p.sent.filter((m) => m.type === "set_ui_lang");

for (const lang of ["zh", "en"]) {
  const other = lang === "en" ? "zh" : "en";

  test("[" + lang + "] 闸关着（config 里没有 ui_lang_available）：两段切换一直藏着", () => {
    const p = loadDesktop({ lang });
    hello(p, {});
    assert.ok(!shown(p));
    p.push({ type: "config", target_lang: "en" });
    assert.ok(!shown(p), "别的 config 广播不动它");
  });

  test("[" + lang + "] 待机首页：出现，按下的是本页语言；点另一种发 set_ui_lang，点按下的那颗什么都不发", () => {
    const p = loadDesktop({ lang });
    hello(p, LANG(lang));
    assert.ok(shown(p));
    assert.deepEqual(pressed(p), [lang]);
    assert.ok(!p.el("lang-quick-zh").disabled && !p.el("lang-quick-en").disabled);
    p.el("lang-quick-" + lang).click();
    assert.deepEqual(uiLangSent(p), [], "已经是这种语言：设置里的「跟随系统」不能被悄悄改掉");
    p.el("lang-quick-" + other).click();
    assert.deepEqual(uiLangSent(p), [{ type: "set_ui_lang", value: other }], "和设置行的下拉发同一条");
    assert.deepEqual(pressed(p), [lang], "按下态等服务端广播新语言再变（随后整页重载）");
  });

  test("[" + lang + "] 设置里换了语言（config 广播带语言键）：按下态跟着变", () => {
    const p = loadDesktop({ lang });
    hello(p, LANG(lang));
    p.push(Object.assign({ type: "config" }, LANG(other, { ui_lang_setting: other })));
    assert.deepEqual(pressed(p), [other]);
  });

  test("[" + lang + "] --ui-lang 固定：两颗都置灰，点了也不发；悬停说由启动参数固定", () => {
    const p = loadDesktop({ lang });
    hello(p, LANG(lang, { ui_lang_locked: true }));
    assert.ok(shown(p));
    assert.ok(p.el("lang-quick-zh").disabled && p.el("lang-quick-en").disabled);
    assert.equal(p.el("lang-quick").getAttribute("title"),
                 lang === "en" ? "Set by a launch option" : "由启动参数固定");
    p.el("lang-quick-" + other).fire("click");             // 绕过 disabled 直接触发，也不发
    assert.deepEqual(uiLangSent(p), []);
    hello(p, LANG(lang));
    assert.ok(!p.el("lang-quick-" + other).disabled);
    assert.equal(p.el("lang-quick").getAttribute("title"), null, "不再固定：悬停说明撤掉");
  });

  test("[" + lang + "] 和本地服务断开时点：发不出去就照设置行的做法说一声", () => {
    const p = loadDesktop({ lang });
    hello(p, LANG(lang));
    p.disconnect();
    p.el("lang-quick-" + other).click();
    assert.deepEqual(uiLangSent(p), []);
    assert.equal(p.el("status-dot").className, "dot offline");
  });
}
