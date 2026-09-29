// 桌面页的界面语言（web/i18n.js）与设置里的「界面语言」行（spec §2.3、§3.4、§3.5）。
//
// 语言由服务端写进 <html lang>，i18n.js 只读不猜；node 测试里用 globalThis.UI_LANG 指定。
// 这里每次都清掉 require 缓存重新求值，所以同一个文件里能分别测中文和英文。
// 另外钉住发布闸关着时的保证：语言行初始隐藏、中文页 applyStatic 不碰 DOM、
// 假 document（现有测试用的那两种，没有 documentElement）require 不抛。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const I18N = require.resolve("../web/i18n.js");
const ROWS = require.resolve("../web/settings-rows.js");
// Windows 的 Git 默认 core.autocrlf=true，检出的是 CRLF：统一成 \n，下面的正则只写 \n
const web = (name) =>
  readFileSync(new URL("../web/" + name, import.meta.url), "utf8").replace(/\r\n/g, "\n");

// 在给定的 globalThis.UI_LANG / document 下重新求值 i18n.js（和依赖它的 settings-rows.js）
function load({ lang, document } = {}) {
  const saved = { UI_LANG: globalThis.UI_LANG, document: globalThis.document };
  if (lang === undefined) delete globalThis.UI_LANG; else globalThis.UI_LANG = lang;
  if (document === undefined) delete globalThis.document; else globalThis.document = document;
  try {
    delete require.cache[I18N];
    delete require.cache[ROWS];
    return { i18n: require("../web/i18n.js"), rows: require("../web/settings-rows.js") };
  } finally {
    if (saved.UI_LANG === undefined) delete globalThis.UI_LANG; else globalThis.UI_LANG = saved.UI_LANG;
    if (saved.document === undefined) delete globalThis.document; else globalThis.document = saved.document;
    delete require.cache[I18N];
    delete require.cache[ROWS];
  }
}

// ---- 假 DOM：只做 applyStatic 用得到的那几样 ------------------------------------------------
function textNode(value) { return { nodeType: 3, nodeValue: value }; }
function element(attrs, children) {
  const store = new Map(Object.entries(attrs || {}));
  const el = {
    nodeType: 1,
    children: children || [],
    html: null,
    get firstChild() { return el.children[0] || null; },
    getAttribute: (k) => (store.has(k) ? store.get(k) : null),
    setAttribute: (k, v) => { store.set(k, String(v)); },
    hasAttribute: (k) => store.has(k),
    appendChild(c) { el.children.push(c); return c; },
    set innerHTML(v) { el.html = v; el.children = []; },
  };
  // firstChild / nextSibling 链：setOwnText 顺着它找自己的第一段文字
  el.children.forEach((c, i) => {
    Object.defineProperty(c, "nextSibling", { get: () => el.children[i + 1] || null, configurable: true });
  });
  return el;
}
function htmlEl(lang) {
  const classes = new Set();
  return { lang, classList: { add: (c) => classes.add(c), contains: (c) => classes.has(c) } };
}
function fakeDocument(root, elements) {
  let queried = 0;
  return {
    documentElement: root,
    createTextNode: textNode,
    querySelectorAll() { queried++; return elements; },
    get queried() { return queried; },
  };
}

// ---- L / LN / APP_NAME -------------------------------------------------------------------

test("不设语言：UI_LANG 是 zh，L/LN 交出中文，程序名是中文", () => {
  const { i18n } = load();
  assert.equal(i18n.UI_LANG, "zh");
  assert.equal(i18n.L("开始翻译", "Start"), "开始翻译");
  assert.equal(i18n.LN(1, "1 项", "1 item", "{n} items"), "1 项");
  assert.equal(i18n.APP_NAME, "TikTok 直播同传");
});

test("英文：L 交出英文，英文缺了（null/undefined）退回中文，不会显示 null", () => {
  const { i18n } = load({ lang: "en" });
  assert.equal(i18n.UI_LANG, "en");
  assert.equal(i18n.L("开始翻译", "Start"), "Start");
  assert.equal(i18n.L("开始翻译", null), "开始翻译");
  assert.equal(i18n.L("开始翻译", undefined), "开始翻译");
  assert.equal(i18n.L("", ""), "");
  assert.equal(i18n.APP_NAME, "TikTok Live Translator");
});

test("英文 LN：n 为 1（含字符串 \"1\"）用单数，其余用复数", () => {
  const { i18n } = load({ lang: "en" });
  assert.equal(i18n.LN(1, "{n} 项", "{n} item", "{n} items"), "{n} item");
  assert.equal(i18n.LN("1", "{n} 项", "{n} item", "{n} items"), "{n} item");
  assert.equal(i18n.LN(0, "{n} 项", "{n} item", "{n} items"), "{n} items");
  assert.equal(i18n.LN(2, "{n} 项", "{n} item", "{n} items"), "{n} items");
});

// ---- 假 document：现有测试的两种替身都没有 documentElement ------------------------------------

test("没有 documentElement 的假 document（viewer.test.mjs 两处的写法）：require 不抛，语言是 zh", () => {
  const fakes = [
    { getElementById: () => null, createElement: () => ({}) },
    { hidden: false, getElementById: () => null, createElement: () => ({}), addEventListener() {} },
  ];
  for (const document of fakes) {
    const { i18n, rows } = load({ document });
    assert.equal(i18n.UI_LANG, "zh");
    assert.equal(rows.langName("en"), "English");
  }
});

test("documentElement 没有 classList：不抛", () => {
  const document = fakeDocument({ lang: "en" }, []);
  const { i18n } = load({ document });
  assert.equal(i18n.UI_LANG, "en");
});

// ---- applyStatic -------------------------------------------------------------------------

test("中文页（<html lang=zh-CN>）：applyStatic 不碰 DOM，只加 i18n-done", () => {
  const span = element({ "data-en": "App Language" }, [textNode("界面语言 · Language")]);
  const root = htmlEl("zh-CN");
  const document = fakeDocument(root, [span]);
  const { i18n } = load({ document });
  assert.equal(i18n.UI_LANG, "zh");
  assert.equal(document.queried, 0, "中文页连 querySelectorAll 都不调");
  assert.equal(span.children[0].nodeValue, "界面语言 · Language");
  assert.equal(root.classList.contains("i18n-done"), true);
});

test("英文页：data-en 只换自己的第一段文字，旁边的图标不动；属性和 data-en-html 一并换", () => {
  const icon = { nodeType: 1, tag: "svg" };
  const blank = textNode("\n  ");
  const label = textNode("回到最新");
  const jump = element({ "data-en": "Jump to Latest" }, [blank, icon, label]);
  const empty = element({ "data-en": "Storage" }, []);
  const help = element({ "data-en-html": "Paste the <b>link</b>." }, [textNode("粘贴<b>链接</b>。")]);
  const btn = element({ title: "输入说明", "data-en-title": "Input help",
                        "aria-label": "关闭", "data-en-aria-label": "Close",
                        placeholder: "主播", "data-en-placeholder": "streamer",
                        alt: "图", "data-en-alt": "image" }, []);
  const root = htmlEl("en");
  const document = fakeDocument(root, [jump, empty, help, btn]);
  const { i18n } = load({ document });
  assert.equal(i18n.UI_LANG, "en");
  assert.equal(document.queried, 1);
  assert.equal(label.nodeValue, "Jump to Latest");
  assert.equal(blank.nodeValue, "\n  ", "空白文字节点不算自己的文字");
  assert.equal(jump.children[1], icon, "图标还在原处");
  assert.equal(empty.children[0].nodeValue, "Storage", "没有文字节点就补一个");
  assert.equal(help.html, "Paste the <b>link</b>.");
  assert.equal(btn.getAttribute("title"), "Input help");
  assert.equal(btn.getAttribute("aria-label"), "Close");
  assert.equal(btn.getAttribute("placeholder"), "streamer");
  assert.equal(btn.getAttribute("alt"), "image");
  assert.equal(root.classList.contains("i18n-done"), true);
});

test("applyStatic 对不像 DOM 的根（null、没有 querySelectorAll）直接返回", () => {
  const { i18n } = load({ lang: "en" });
  i18n.applyStatic(null);
  i18n.applyStatic({});
});

// ---- 界面语言行的纯函数（settings-rows.js） ---------------------------------------------------

test("中文界面的摘要：跟随系统 · 中文 / 中文 / English", () => {
  const { rows } = load();
  assert.equal(rows.langSummary("system", "zh", "zh"), "跟随系统 · 中文");
  assert.equal(rows.langSummary("system", "en", "en"), "跟随系统 · English");
  assert.equal(rows.langSummary("zh", "zh", "en"), "中文");
  assert.equal(rows.langSummary("en", "en", "zh"), "English");
  assert.equal(rows.langSummary(undefined, "en", "zh"), "English", "认不出的设置：写生效的语言，不留空");
  assert.equal(rows.langSummary("system", "zh", undefined), "跟随系统 · 中文", "检测不了按中文");
  assert.equal(rows.langSystemLabel("zh"), "跟随系统（中文）");
  assert.equal(rows.langSystemLabel("en"), "跟随系统（English）");
});

test("英文界面的摘要：System · English / 中文 / English（语言名永远用自称）", () => {
  const { rows } = load({ lang: "en" });
  assert.equal(rows.langSummary("system", "en", "en"), "System · English");
  assert.equal(rows.langSummary("system", "zh", "zh"), "System · 中文");
  assert.equal(rows.langSummary("zh", "zh", "en"), "中文");
  assert.equal(rows.langSummary("en", "en", "zh"), "English");
  assert.equal(rows.langSystemLabel("en"), "System (English)");
  assert.equal(rows.langSystemLabel("zh"), "System (中文)");
});

test("langReload：config 没带 ui_lang（闸关着）或和本页相同，都不重载", () => {
  const { rows } = load();
  const keep = { reload: false, mark: null };
  assert.deepEqual(rows.langReload(undefined, "zh", null, 5000), keep);
  assert.deepEqual(rows.langReload("fr", "zh", null, 5000), keep);
  assert.deepEqual(rows.langReload("zh", "zh", null, 5000), keep);
  assert.deepEqual(rows.langReload("en", "en", null, 5000), keep);
  assert.deepEqual(rows.langReload("zh", undefined, null, 5000), keep, "本页语言认不出按中文");
});

test("langReload：不同就重载一次，并记下「目标@时间」", () => {
  const { rows } = load();
  assert.deepEqual(rows.langReload("en", "zh", null, 5000), { reload: true, mark: "en@5000" });
  assert.deepEqual(rows.langReload("zh", "en", "", 7000), { reload: true, mark: "zh@7000" });
});

test("langReload：10 秒内对同一个目标已经重载过就不再重载（防死循环）", () => {
  const { rows } = load();
  const win = rows.LANG_RELOAD_WINDOW_MS;
  assert.equal(win, 10000);
  assert.equal(rows.langReload("en", "zh", "en@5000", 5000 + win - 1).reload, false);
  assert.equal(rows.langReload("en", "zh", "en@5000", 5000).reload, false);
  assert.equal(rows.langReload("en", "zh", "en@5000", 5000 + win).reload, true, "过了 10 秒可以再试");
  assert.equal(rows.langReload("en", "zh", "zh@5000", 6000).reload, true, "目标换了不算重复");
  assert.equal(rows.langReload("en", "zh", "en@9000", 6000).reload, true, "记下的时间在将来（时钟回拨）：当作过期");
  assert.equal(rows.langReload("en", "zh", "garbage", 6000).reload, true);
});

// ---- 页面接线：闸关着时什么都看不见 -----------------------------------------------------------

test("index.html：i18n.js 是第一个外部脚本，排在 normalize.js 和 app.js 之前", () => {
  const html = web("index.html");
  const srcs = [...html.matchAll(/<script src="\/static\/([^"?]+)/g)].map((m) => m[1]);
  assert.equal(srcs[0], "i18n.js", srcs.join(", "));
  assert.ok(srcs.indexOf("normalize.js") > 0 && srcs.indexOf("app.js") > srcs.indexOf("settings-rows.js"));
});

test("index.html：语言行初始隐藏，放在设置分组最后（磁盘空间之后），图标是内联实心 SVG", () => {
  const html = web("index.html");
  const card = html.match(/<div id="lang-card" class="([^"]*)">([\s\S]*?)\n {10}<\/div>\n/);
  assert.ok(card, "找不到 #lang-card");
  assert.ok(card[1].split(/\s+/).includes("hidden"), "闸关着时 config 里没有 ui_lang_available，行必须一直隐藏");
  assert.ok(html.indexOf('id="lang-card"') > html.indexOf('id="disk-card"'));
  assert.ok(html.indexOf('id="lang-card"') < html.indexOf("</section>", html.indexOf('id="disk-card"')));
  assert.ok(!card[2].includes("<use"), "sprite 里没有这个图形：<use> 会指向不存在的 symbol");
  assert.match(card[2], /<svg viewBox="0 0 16 16" fill="currentColor"/);
  for (const id of ["lang-head", "lang-summary", "lang-body", "ui-lang-select", "ui-lang-system"]) {
    assert.ok(card[2].includes('id="' + id + '"'), id);
  }
  assert.match(card[2], /<option value="zh" lang="zh-CN" translate="no">中文<\/option>/);
  assert.match(card[2], /<option value="en" lang="en" translate="no">English<\/option>/);
});

test("app.js：语言行只看 config.ui_lang_available，页面加载后经 JS 桥告诉窗口语言", () => {
  const app = web("app.js");
  assert.match(app, /cfg\.ui_lang_available === true/);
  assert.match(app, /api\.set_window_lang\(UI_LANG\)/);
  assert.match(app, /addEventListener\("pywebviewready", function \(\) \{ syncWindowLang\(\);/);
  assert.match(app, /send\(\{ type: "set_ui_lang", value: uiLangSelect\.value \}\)/);
});

test("防闪只挂在英文（桌面）/ data-i18n=on（手机）上：中文页、闸关着的手机页选择器不命中", () => {
  assert.ok(web("style.css").includes('html[lang="en"]:not(.i18n-done) body { visibility: hidden;'));
  assert.ok(web("viewer.css").includes('html[data-i18n="on"]:not(.i18n-done) body { visibility: hidden;'));
  const viewer = web("viewer.html");
  assert.match(viewer, /<button id="lang-toggle" class="ring-toggle hidden"/, "手机切换按钮初始隐藏");
  assert.ok(!/<html[^>]*data-i18n/.test(viewer), "data-i18n 只能由服务端在闸开着时注入");
});
