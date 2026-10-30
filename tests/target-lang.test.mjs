// 顶栏「字幕译成」下拉（#target-lang；web/index.html、web/live-ui.js targetLangExtra、web/app.js showTargetLang）。
//
// 用户 2026-10-20：原来列 13 种、标签叫「目标语言」，他把它当成界面语言切成了 English。决定只列四种
// （简体中文、繁体中文、英语、西语），标签改说「字幕译成」，界面语言照旧在「设置 > 界面语言」。
//   1. 页面上写死的正好是这四种，顺序同 live-ui.js 的 TARGET_LANGS；选项是语言自称，在 translate="no" 里；
//   2. 标签、title、aria-label 都说的是字幕，中英成对；窄窗口直播中省掉标签，首页一直露出来；
//   3. 设置里存着、或 --target 指定了四种以外的语言：下拉补一项显示它（不是静默显示成第一项），
//      也不因此发 set_target 改掉它；换回四种之一以后补的那项撤掉，下拉里只剩四种。中文页、英文页一样
//      （语言名是数据，不随界面语言变）。
//   4. 2026-10-29 同一种误会又来了一次（连接中、默认 1000px 窗口，标签被省掉，只剩一个写着「中文（简体）」的下拉）：
//      在英文界面选了中文、或在中文界面选了 English 时，下拉下面就地弹一个小浮层，说这个下拉管的是字幕，
//      并给一颗切换界面的按钮（字是那种语言的自称）。每页只弹一次，几秒后自己收起。
//   5. 用户 2026-10-29 定（lang-4 选 A）：连接中、直播中也给这颗按钮，这是直播中切界面语言唯一的入口（设置分组
//      这时整块藏着，顶栏不另加按钮）。切换只让页面重载一次，识别、违禁词检测、审计、手机同看都不经过桌面页面，
//      重连后字幕、报警、弹幕照旧回放（tests/test_i18n_server.py 钉着服务端那一半）。只有 --ui-lang 固定时不给。
//      按钮的名字就是语言自称本身（同首页的两段切换），不另写 aria-label：aria-label 会跟着按钮的 lang 被念成
//      另一种语言的声音，英文页上还会多出一处中文；提示那句话由 aria-describedby 接上。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

import { FakeEl, hello as helloLive, loadDesktop, web } from "./desktop-page.mjs";

const require = createRequire(import.meta.url);
const { TARGET_LANGS, targetLangExtra, captionsLangHint } = require("../web/live-ui.js");

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

// ---- 4. 选了另一种界面语言：就地提示 + 一键切换界面（2026-10-29） ------------------------------------

test("captionsLangHint：英文界面选了中文（简繁都算）、中文界面选了英文，返回那种界面语言；别的都不提示", () => {
  assert.equal(captionsLangHint("en", "zh-CN"), "zh");
  assert.equal(captionsLangHint("en", "zh-TW"), "zh");
  assert.equal(captionsLangHint("zh", "en"), "en");
  for (const v of ["en", "es", "ja", "zh", "zh-cn", "", null, undefined]) {
    assert.equal(captionsLangHint("en", v), null, "en 界面选 " + v);
  }
  for (const v of ["zh-CN", "zh-TW", "es", "ja", "EN", "en-US", "", null, undefined]) {
    assert.equal(captionsLangHint("zh", v), null, "zh 界面选 " + v);
  }
});

test("index.html：提示浮层紧跟在「字幕译成」后面，默认藏着；按钮是数据（translate=\"no\"），字由 app.js 写、提示那句接在 aria-describedby 上", () => {
  const html = web("index.html").replace(/<!--[\s\S]*?-->/g, "");
  const hint = /<div id="target-hint"([^>]*)>([\s\S]*?)<\/div>/.exec(html);
  assert.ok(hint, "index.html 里找不到 #target-hint");
  assert.match(hint[1], /\sclass="target-hint popover hidden"/);
  assert.match(hint[1], /\srole="status"/);
  assert.match(hint[2], /<p id="target-hint-text" class="target-hint-text"><\/p>/, "正文由 app.js 按界面语言写（role=status 才会念）");
  const btn = /<button id="target-hint-switch"([^>]*)><\/button>/.exec(hint[2]);
  assert.ok(btn, "浮层里要有 #target-hint-switch");
  assert.match(btn[1], /\stype="button"/);
  assert.match(btn[1], /\stranslate="no"/);
  assert.match(btn[1], /\sclass="[^"]*\bhidden\b[^"]*"/);
  assert.match(btn[1], /\saria-describedby="target-hint-text"/, "读屏念完按钮名，接着念提示那一句");
  assert.doesNotMatch(btn[1], /\saria-label=/);
  assert.ok(html.indexOf('id="target-hint"') > html.indexOf('<select id="target-lang"'), "浮层在下拉后面：键盘按 Tab 就进去");
  assert.ok(html.indexOf('id="target-hint"') < html.indexOf('id="font-btn"'), "紧跟在「字幕译成」后面");
  const css = web("style.css").replace(/\/\*[\s\S]*?\*\//g, "");
  assert.match(css, /\.target-hint\.popover \{[^}]*width: min\(/, "比别的浮层窄：只有一句话和一颗按钮");
});

const HINT = {
  en: "Captions are translated into this language. To change the app language, go to Settings > App Language.",
  zh: "字幕译成这种语言。界面语言在「设置 > 界面语言」里改",
};
// 每种界面「选了另一种语言」时：选哪一项、按钮写什么、lang 属性、发什么、aria-label
const OTHER = {
  en: { pick: "zh-CN", also: "zh-TW", name: "中文", tag: "zh-CN", send: "zh" },
  zh: { pick: "en", also: null, name: "English", tag: "en", send: "en" },
};
const HINT_MS = 8000;

/** 带界面语言行的 hello（闸开着、跟随系统、解析成本页的语言）。state 缺省是待机首页。 */
function langHello(p, lang, state = "idle", extra = {}) {
  helloLive(p, Object.assign({
    status: { state, detail: "" }, target_lang: lang === "en" ? "en" : "zh-CN",
    ui_lang_available: true, ui_lang_setting: "system", ui_lang: lang, ui_lang_system: lang,
  }, extra));
}
const hintOpen = (p) => !p.el("target-hint").classList.contains("hidden");
const switchShown = (p) => !p.el("target-hint-switch").classList.contains("hidden");
const pick = (p, value) => { const sel = p.el("target-lang"); sel.value = value; sel.fire("change"); };
const uiLangSent = (p) => p.sent.filter((m) => m.type === "set_ui_lang");

for (const lang of ["zh", "en"]) {
  const o = OTHER[lang];

  test("[" + lang + "] 首页选了另一种界面语言：先照旧发 set_target，再弹提示；按钮写那种语言的自称，点了切换界面", () => {
    const p = page(lang);
    langHello(p, lang);
    assert.ok(!hintOpen(p), "没选之前不弹");
    pick(p, o.pick);
    assert.deepEqual(setTargets(p), [{ type: "set_target", value: o.pick }], "字幕目标照改，提示不拦它");
    assert.ok(hintOpen(p));
    assert.equal(p.el("target-hint-text").textContent, HINT[lang], "正文就是下拉 tooltip 那一句（同一对，不新增句子）");
    const btn = p.el("target-hint-switch");
    assert.ok(switchShown(p));
    assert.equal(btn.textContent, o.name, "按钮字是那种语言的自称：落进看不懂的界面也认得出");
    assert.equal(btn.getAttribute("lang"), o.tag);
    assert.equal(btn.getAttribute("aria-label"), null, "名字就是自称本身，读屏照按钮的 lang 念");
    assert.deepEqual(uiLangSent(p), [], "弹提示不等于切换");
    btn.click();
    assert.deepEqual(uiLangSent(p), [{ type: "set_ui_lang", value: o.send }]);
    assert.ok(!hintOpen(p), "点了就收起，等服务端广播新语言、页面重载");
  });

  test("[" + lang + "] 每页只弹一次；几秒后自己收起；Esc 收起、焦点回到下拉", () => {
    const p = page(lang);
    langHello(p, lang);
    let focused = 0;
    p.el("target-lang").focus = () => { focused += 1; };
    pick(p, o.pick);
    assert.ok(hintOpen(p));
    p.fire(HINT_MS);
    assert.ok(!hintOpen(p), "几秒后自己收起，不一直挡着字幕");
    pick(p, lang === "en" ? "en" : "zh-CN");
    pick(p, o.also || o.pick);
    assert.ok(!hintOpen(p), "同一页第二次不再弹");

    const q = page(lang);
    langHello(q, lang);
    q.el("target-lang").focus = () => { focused += 1; };
    pick(q, o.pick);
    q.dispatch("keydown", { key: "Escape" });
    assert.ok(!hintOpen(q));
    assert.equal(focused, 1, "Esc 之后焦点回到「字幕译成」下拉");
  });

  test("[" + lang + "] 指针移进提示或焦点进到里面：不再自己收起（点外面、Esc 照旧收）", () => {
    for (const ev of ["pointerenter", "focusin"]) {
      const p = page(lang);
      langHello(p, lang);
      pick(p, o.pick);
      p.el("target-hint").fire(ev);
      p.fire(HINT_MS);
      assert.ok(hintOpen(p), ev + " 之后不再按时收起");
      p.dispatch("pointerdown", { target: p.el("history") });
      assert.ok(!hintOpen(p), "点外面收起");
    }
  });

  test("[" + lang + "] 不弹：选的就是本页的语言、西语；服务端推来的目标语言；界面语言这一行不存在（闸关着）", () => {
    const p = page(lang);
    langHello(p, lang);
    pick(p, lang === "en" ? "en" : "zh-CN");
    pick(p, "es");
    if (lang === "zh") pick(p, "zh-TW");
    assert.ok(!hintOpen(p));
    p.push({ type: "config", target_lang: o.pick });           // 另一个窗口换的：不是这一页的人选的
    assert.ok(!hintOpen(p));
    pick(p, o.pick);
    assert.ok(hintOpen(p), "上面那些都不算这一页的「一次」");

    const gate = page(lang);
    hello(gate, "zh-CN");                                      // 没有 ui_lang_available：设置里没有界面语言这一行
    pick(gate, o.pick);
    assert.ok(!hintOpen(gate), "没有可切换的界面语言：那句话指向一个不存在的地方");
  });

  test("[" + lang + "] 连接中、直播中：照样给切换按钮，点了发 set_ui_lang（只让页面重载一次，不停监听）", () => {
    for (const state of ["connecting", "live"]) {
      const p = page(lang);
      langHello(p, lang, state);
      pick(p, o.pick);
      assert.ok(hintOpen(p), state);
      assert.equal(p.el("target-hint-text").textContent, HINT[lang]);
      assert.ok(switchShown(p), state + "：这是直播中切界面语言唯一的入口");
      assert.equal(p.el("target-hint-switch").textContent, o.name);
      p.el("target-hint-switch").click();
      assert.deepEqual(uiLangSent(p), [{ type: "set_ui_lang", value: o.send }], state);
      assert.ok(!hintOpen(p), state);
      assert.deepEqual(p.sent.filter((m) => m.type === "stop"), [], state + "：不发停止");
    }
  });

  test("[" + lang + "] 界面语言被启动参数固定时（待机、直播中都一样）：只说那句话，不给切换按钮", () => {
    for (const state of ["idle", "live"]) {
      const p = page(lang);
      langHello(p, lang, state, { ui_lang_locked: true });
      pick(p, o.pick);
      assert.ok(hintOpen(p), state);
      assert.equal(p.el("target-hint-text").textContent, HINT[lang]);
      assert.ok(!switchShown(p), state + "：不给按钮");
      p.el("target-hint-switch").click();                       // 万一点到（藏着的按钮），也不发
      assert.deepEqual(uiLangSent(p), [], state);
    }
  });

  test("[" + lang + "] 提示开着时开始了连接、进了直播：按钮照旧在，点了照发", () => {
    const p = page(lang);
    langHello(p, lang);
    pick(p, o.pick);
    assert.ok(switchShown(p));
    p.push({ type: "status", state: "connecting", detail: "" });
    p.push({ type: "status", state: "live", detail: "" });
    assert.ok(switchShown(p));
    p.el("target-hint-switch").click();
    assert.deepEqual(uiLangSent(p), [{ type: "set_ui_lang", value: o.send }]);
  });

  test("[" + lang + "] 和本地服务断开时点切换：发不出去就照设置行的做法说一声", () => {
    const p = page(lang);
    langHello(p, lang);
    pick(p, o.pick);
    p.disconnect();
    p.el("target-hint-switch").click();
    assert.deepEqual(uiLangSent(p), []);
    assert.equal(p.el("status-dot").className, "dot offline");
  });
}
