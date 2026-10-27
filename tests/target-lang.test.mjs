// 顶栏「字幕译成」下拉（#target-lang；web/index.html、web/live-ui.js targetLangExtra、web/app.js showTargetLang）。
//
// 用户 2026-10-20：原来列 13 种、标签叫「目标语言」，他把它当成界面语言切成了 English。决定只列四种
// （简体中文、繁体中文、英语、西语），标签改说「字幕译成」，界面语言照旧在「设置 > 界面语言」。
//   1. 页面上写死的正好是这四种，顺序同 live-ui.js 的 TARGET_LANGS；选项是语言自称，在 translate="no" 里；
//   2. 标签、title、aria-label 都说的是字幕，中英成对；窄窗口直播中省掉标签，首页一直露出来；
//   3. 设置里存着、或 --target 指定了四种以外的语言：下拉补一项显示它（不是静默显示成第一项），
//      也不因此发 set_target 改掉它；换回四种之一以后补的那项撤掉，下拉里只剩四种。中文页、英文页一样
//      （语言名是数据，不随界面语言变）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

import { FakeEl, hello as helloLive, loadDesktop, web } from "./desktop-page.mjs";

const require = createRequire(import.meta.url);
const { TARGET_LANGS, targetLangExtra } = require("../web/live-ui.js");

const FOUR = [["zh-CN", "中文（简体）"], ["zh-TW", "中文（繁體）"], ["en", "English"], ["es", "Español"]];

/** index.html 里 <select id="target-lang"> 的开标签和全部 option：[[value, 文字], ...] */
function staticSelect() {
  const html = web("index.html").replace(/<!--[\s\S]*?-->/g, "");
  const m = /<select id="target-lang"([^>]*)>([\s\S]*?)<\/select>/.exec(html);
  assert.ok(m, "index.html 里找不到 #target-lang");
  const options = [...m[2].matchAll(/<option value="([^"]*)">([^<]*)<\/option>/g)].map((o) => [o[1], o[2]]);
  return { attrs: m[1], options, label: html.slice(html.lastIndexOf("<label", m.index), m.index) };
}

test("index.html：下拉正好列四种，顺序和 live-ui.js 的 TARGET_LANGS 一致，名字是语言自称", () => {
  const { attrs, options } = staticSelect();
  assert.deepEqual(options, FOUR);
  assert.deepEqual(TARGET_LANGS, FOUR.map((o) => o[0]));
  assert.match(attrs, /\stranslate="no"/, "选项是数据，哪种界面语言都不翻");
});

test("index.html：标签、title、aria-label 说的是字幕译成，中英成对，并指出界面语言在哪", () => {
  const { attrs, label } = staticSelect();
  assert.match(attrs, /\saria-label="字幕译成"/);
  assert.match(attrs, /\sdata-en-aria-label="Captions in"/);
  assert.match(label, /<span class="ctl-text" data-en="Captions in">字幕译成<\/span>/);
  assert.match(label, /\stitle="字幕译成这种语言。界面语言在「设置 > 界面语言」里改"/);
  assert.match(label, /\sdata-en-title="Captions are translated into this language\. To change the app language, go to Settings > App Language\."/);
  assert.doesNotMatch(label + attrs, /目标语言|Translate to/, "旧的叫法会被读成界面语言");
});

test("style.css：直播中窄窗口照旧省掉标签，首页（body.home）一直露出来", () => {
  const css = web("style.css").replace(/\/\*[\s\S]*?\*\//g, "");
  const block = /@media \(max-width: 1080px\) \{([^@]*?)\n\}/.exec(css);
  assert.ok(block, "找不到 ≤1080px 那一段");
  assert.match(block[1], /^\s*\.ctl-text \{ display: none; \}$/m);
  assert.match(block[1], /^\s*body\.home \.ctl-text \{ display: inline; \}$/m);
});

test("targetLangExtra：四种以内（和空值）不补；别的语言补一项，名字是自称，没收录的显示代码本身", () => {
  for (const [code] of FOUR) assert.equal(targetLangExtra(code), null, code);
  for (const empty of ["", null, undefined]) assert.equal(targetLangExtra(empty), null);
  assert.deepEqual(targetLangExtra("ja"), { value: "ja", text: "日本語" });
  assert.deepEqual(targetLangExtra("id"), { value: "id", text: "Bahasa Indonesia" });
  assert.deepEqual(targetLangExtra("ko"), { value: "ko", text: "한국어" });
  // 以前那 13 种里另外 9 种都有名字（老设置里最可能存着的就是它们）
  for (const code of ["ja", "ko", "fr", "de", "ru", "pt", "vi", "th", "id"]) {
    assert.notEqual(targetLangExtra(code).text, code, code);
  }
  assert.deepEqual(targetLangExtra("ar"), { value: "ar", text: "ar" });
  assert.deepEqual(targetLangExtra("constructor"), { value: "constructor", text: "constructor" });
  assert.deepEqual(targetLangExtra("zh-cn"), { value: "zh-cn", text: "zh-cn" }, "大小写不同就是另一个代码：照实显示");
});

// ---- 真页面：app.js 收到 hello / config 广播以后下拉里有什么 ----------------------------------------

/** 起一个桌面页，并照 index.html 把四个 option 放进 #target-lang（假 DOM 只建带 id 的元素）。 */
function page(lang) {
  const p = loadDesktop({ lang });
  const sel = p.el("target-lang");
  for (const [value, text] of staticSelect().options) {
    const opt = new FakeEl("option");
    opt.value = value;
    opt.textContent = text;
    sel.appendChild(opt);
  }
  return p;
}

const shown = (p) => p.el("target-lang").options.map((o) => [o.value, o.textContent, o.getAttribute("data-extra")]);
// 待机首页的 hello（desktop-page.mjs 那份默认是直播中）：服务端在 config.target_lang 里带着当前目标
const hello = (p, target) => helloLive(p, { status: { state: "idle", detail: "" }, target_lang: target });
const setTargets = (p) => p.sent.filter((m) => m.type === "set_target");

for (const lang of ["zh", "en"]) {
  test("[" + lang + "] 设置里存着四种以外的语言：补一项选中它，不改设置；换回四种之一，补的那项撤掉", () => {
    const p = page(lang);
    const sel = p.el("target-lang");
    hello(p, "ja");
    assert.equal(sel.value, "ja");
    assert.deepEqual(shown(p), [...FOUR.map(([v, t]) => [v, t, null]), ["ja", "日本語", "1"]]);
    assert.deepEqual(setTargets(p), [], "只是显示，不能替用户改目标语言");

    hello(p, "ja");                                            // 重连：同一个 hello 再来一遍，不重复补
    assert.equal(shown(p).length, 5);

    p.push({ type: "config", target_lang: "fr" });             // 另一个窗口（或旧版本的页面）换成了法语
    assert.equal(sel.value, "fr");
    assert.deepEqual(shown(p).slice(4), [["fr", "Français", "1"]], "下拉里只留此刻正在用的那一种");

    p.push({ type: "config", target_lang: "en" });             // 换回四种之一
    assert.equal(sel.value, "en");
    assert.deepEqual(shown(p), FOUR.map(([v, t]) => [v, t, null]));

    p.push({ type: "config", target_lang: "pt-BR" });          // 没收录的代码：照实显示代码
    assert.deepEqual(shown(p).slice(4), [["pt-BR", "pt-BR", "1"]]);
    assert.equal(sel.value, "pt-BR");
  });

  test("[" + lang + "] 四种之一：只选中，不补；用户从下拉里选，照旧发 set_target", () => {
    const p = page(lang);
    const sel = p.el("target-lang");
    hello(p, "es");
    assert.equal(sel.value, "es");
    assert.deepEqual(shown(p), FOUR.map(([v, t]) => [v, t, null]));

    hello(p, "id");
    sel.value = "zh-TW";                                       // 用户从「Bahasa Indonesia」改选繁体中文
    sel.fire("change");
    assert.deepEqual(setTargets(p), [{ type: "set_target", value: "zh-TW" }]);
    p.push({ type: "config", target_lang: "zh-TW" });          // 服务端的回执
    assert.deepEqual(shown(p), FOUR.map(([v, t]) => [v, t, null]));
    assert.equal(sel.value, "zh-TW");
  });
}
