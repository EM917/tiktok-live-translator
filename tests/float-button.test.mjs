// 顶栏「浮窗」按钮（web/index.html #float-btn、web/app.js updateFloatBtn / applyFloatConfig）。
//
// 页面是 node:vm 里真跑的 web/app.js（tests/desktop-page.mjs），中文、英文两种界面各跑一遍：
//   1. 什么时候露出：服务端 hello 里有 float_window.available（只有桌面应用窗口模式才有）且正在监听中/连接中，
//      和「换主播」按钮同一条规则；浏览器模式（没有这个键）、待机、结束、出错都藏着；
//   2. 点一下发 {type: "float_window", on: …}，等服务端回执期间禁用，回执（config.float_window）决定点亮；
//   3. 断线时点不出去就不装作点成了；回执迟迟不来 4 秒后自己解开；
//   4. 监听结束，服务端关掉浮窗并广播，按钮跟着藏起来、点亮清掉；
//   5. 中文页顶栏放得下「浮窗」两个字时露字（style.css 的 html[lang^="zh"] 那一段，docs/i18n-style.md §4.2）：
//      body 的 has-alert-tag 跟「报警开」标签同进同出，样式表据此在标签亮着时退回只有图标；
//      样式本身（宽度范围、只作用在中文页、露字时去掉图标）在文件末尾静态钉住。
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadDesktop, hello, web } from "./desktop-page.mjs";

const FLOAT = { available: true, open: false };
const hidden = (page) => page.el("float-btn").classList.contains("hidden");
const lit = (page) => page.el("float-btn").classList.contains("on");
const pressed = (page) => page.el("float-btn").getAttribute("aria-pressed");
const sentFloat = (page) => page.sent.filter((m) => m.type === "float_window");

for (const lang of ["zh", "en"]) {
  const L = (name) => name + "（" + lang + "）";

  test(L("窗口模式 + 监听中：按钮露出，没点亮"), () => {
    const page = loadDesktop({ lang });
    assert.ok(hidden(page), "页面刚加载、还没 hello：藏着");
    hello(page, { float_window: FLOAT });
    assert.ok(!hidden(page));
    assert.ok(!lit(page));
    assert.equal(pressed(page), "false");
    assert.equal(page.el("float-btn").disabled, false);
  });

  test(L("浏览器模式（hello 里没有 float_window）：整个藏着，状态怎么变都不露"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    assert.ok(hidden(page));
    page.push({ type: "status", state: "connecting", detail: "" });
    page.push({ type: "status", state: "live", detail: "" });
    assert.ok(hidden(page));
    page.push({ type: "config", target_lang: "en" });          // 别的 config 广播不带这个键：不动
    assert.ok(hidden(page));
  });

  test(L("和换主播同一条规则：连接中和直播中露出，待机 / 结束 / 出错藏起来"), () => {
    const page = loadDesktop({ lang });
    hello(page, { float_window: FLOAT, status: { state: "idle", detail: "" } });
    assert.ok(hidden(page) && page.el("switch-btn").classList.contains("hidden"));
    for (const state of ["connecting", "live"]) {
      page.push({ type: "status", state, detail: "" });
      assert.ok(!hidden(page), state);
      assert.equal(hidden(page), page.el("switch-btn").classList.contains("hidden"), "同一处切换：" + state);
    }
    for (const state of ["ended", "error", "idle"]) {
      page.push({ type: "status", state, detail: "" });
      assert.ok(hidden(page), state);
    }
  });

  test(L("点一下：发 float_window on，等回执期间禁用；回执来了点亮；再点发 off"), () => {
    const page = loadDesktop({ lang });
    hello(page, { float_window: FLOAT });
    page.el("float-btn").click();
    assert.deepEqual(sentFloat(page), [{ type: "float_window", on: true }]);
    assert.equal(page.el("float-btn").disabled, true);
    page.el("float-btn").click();                                // 禁用期间的连点不再发
    assert.equal(sentFloat(page).length, 1);
    page.push({ type: "config", float_window: { available: true, open: true } });
    assert.equal(page.el("float-btn").disabled, false);
    assert.ok(lit(page));
    assert.equal(pressed(page), "true");
    page.el("float-btn").click();
    assert.deepEqual(sentFloat(page), [{ type: "float_window", on: true }, { type: "float_window", on: false }]);
    page.push({ type: "config", float_window: { available: true, open: false } });
    assert.ok(!lit(page));
    assert.equal(pressed(page), "false");
  });

  test(L("用户直接关了浮窗窗口：服务端广播 open=false，按钮熄灭"), () => {
    const page = loadDesktop({ lang });
    hello(page, { float_window: { available: true, open: true } });
    assert.ok(lit(page));
    page.push({ type: "config", float_window: { available: true, open: false } });
    assert.ok(!lit(page) && !hidden(page));
  });

  test(L("回执 4 秒没来：自己解开禁用，能再点"), () => {
    const page = loadDesktop({ lang });
    hello(page, { float_window: FLOAT });
    page.el("float-btn").click();
    assert.equal(page.el("float-btn").disabled, true);
    page.fire(4000);
    assert.equal(page.el("float-btn").disabled, false);
    page.el("float-btn").click();
    assert.equal(sentFloat(page).length, 2);
  });

  test(L("断线时点不出去：不禁用、不装作成了"), () => {
    const page = loadDesktop({ lang });
    hello(page, { float_window: FLOAT });
    page.disconnect();
    page.el("float-btn").click();
    assert.deepEqual(sentFloat(page), []);
    assert.equal(page.el("float-btn").disabled, false);
    assert.ok(!lit(page));
    page.connect();
    page.el("float-btn").click();
    assert.equal(sentFloat(page).length, 1);
  });

  test(L("监听结束：服务端关浮窗并广播，按钮藏起来、点亮清掉；再开播不会自己亮"), () => {
    const page = loadDesktop({ lang });
    hello(page, { float_window: { available: true, open: true } });
    assert.ok(lit(page));
    page.push({ type: "status", state: "idle", detail: "" });
    assert.ok(hidden(page));
    page.push({ type: "config", float_window: { available: true, open: false } });
    page.push({ type: "status", state: "connecting", detail: "" });
    assert.ok(!hidden(page) && !lit(page));
  });

  test(L("重连后的 hello 整份换：服务端不再提供时按钮消失"), () => {
    const page = loadDesktop({ lang });
    hello(page, { float_window: FLOAT });
    assert.ok(!hidden(page));
    hello(page);                                                  // 重连到了一个没有浮窗的服务端
    assert.ok(hidden(page));
  });

  test(L("给 body 加 / 去掉 has-alert-tag：和「报警开」标签同一个显示条件（样式表据此让「浮窗」退回只有图标）"), () => {
    const page = loadDesktop({ lang });
    const tag = () => page.el("alert-mode-tag");
    const marked = () => page.body.classList.contains("has-alert-tag");
    assert.ok(!marked(), "页面刚加载：没有");
    hello(page, { float_window: FLOAT });                         // 报警关着（默认）：标签藏着，类也没有
    assert.ok(tag().classList.contains("hidden") && !marked());
    hello(page, { float_window: FLOAT, alerts_enabled: true });   // 直播中 + 报警开：标签亮，类跟着有
    assert.ok(!tag().classList.contains("hidden") && marked());
    page.push({ type: "status", state: "connecting", detail: "" });
    assert.ok(!tag().classList.contains("hidden") && marked(), "连接中也算");
    page.push({ type: "status", state: "idle", detail: "" });     // 待机：标签藏起来，类跟着去掉
    assert.ok(tag().classList.contains("hidden") && !marked());
    hello(page, { float_window: FLOAT, alerts_enabled: true, status: { state: "idle", detail: "" } });
    assert.ok(tag().classList.contains("hidden") && !marked(), "报警开着但没在监听：标签不亮，类也不该有");
    page.push({ type: "status", state: "live", detail: "" });
    assert.ok(marked());
    page.push({ type: "alert_mode", on: false });                 // 直播中把报警关掉：标签和类一起去掉
    assert.ok(tag().classList.contains("hidden") && !marked());
  });

  test(L("给 body 加 / 去掉 has-float-btn（样式表据此收窄字号滑块）"), () => {
    const page = loadDesktop({ lang });
    assert.ok(!page.body.classList.contains("has-float-btn"));
    hello(page, { float_window: FLOAT });
    assert.ok(page.body.classList.contains("has-float-btn"));
    page.push({ type: "status", state: "idle", detail: "" });
    assert.ok(!page.body.classList.contains("has-float-btn"));
  });
}

// ---- 页面文件本身 -------------------------------------------------------------------------

test("按钮在手机同看之后、换主播之前，带 aria-pressed 和中英文 title；文字是视觉隐藏的 .btn-label", () => {
  const html = web("index.html");
  const tag = /<button id="float-btn"[\s\S]*?<\/button>/.exec(html);
  assert.ok(tag, "index.html 里没有 #float-btn");
  assert.ok(html.indexOf('id="float-btn"') < html.indexOf('id="switch-btn"'));
  assert.ok(html.indexOf('id="share-btn"') < html.indexOf('id="float-btn"'));
  assert.match(tag[0], /class="btn float-btn hidden"/);
  assert.match(tag[0], /aria-pressed="false"/);
  assert.match(tag[0], /title="在始终置顶的小窗里看最新字幕和弹幕"/);
  assert.match(tag[0], /data-en-title="Show the latest caption and comments in a window that stays on top"/);
  assert.match(tag[0], /<span class="btn-label" data-en="Floating Window">浮窗<\/span>/);
  assert.match(tag[0], /<use href="#i-float"\/>/);
  assert.match(html, /<symbol id="i-float"/);
});

test("版本号：app.js、live-ui.js、style.css 改了，?v= 跟着涨；浮窗页引用的脚本版本与桌面页一致", () => {
  const index = web("index.html");
  const float = web("float.html");
  const ver = (html, file) => (new RegExp(file.replace(".", "\\.") + "\\?v=(\\d+)").exec(html) || [])[1];
  for (const file of ["live-ui.js", "guard.js", "i18n.js"]) assert.equal(ver(index, file), ver(float, file), file);
  assert.ok(Number(ver(index, "app.js")) >= 74 && Number(ver(index, "live-ui.js")) >= 3);
  assert.ok(Number(ver(index, "style.css")) >= 44);
});

// ---- 样式：中文页放得下的宽度露出「浮窗」两个字 ---------------------------------------------
// 浏览器里的真实宽度由 G10 的 live-float-zh* 场景量（tests/test_i18n_dom.py，只在 CI 的 macOS 跑器上跑）；
// 这里只钉静态的部分：宽度范围、只作用在中文页、露字时去掉图标且文字不再视觉隐藏、默认（和英文页）仍是只有图标。

const stripComments = (text) => text.replace(/\/\*[\s\S]*?\*\//g, "");

/** 最外层 @media 块：[{ query, body }]（body 是花括号里的全部文字，含嵌套规则）。 */
function mediaBlocks(text) {
  const out = [];
  const re = /@media\s*([^{]+)\{/g;
  let m;
  while ((m = re.exec(text))) {
    let depth = 1;
    let i = re.lastIndex;
    while (depth && i < text.length) {
      const c = text[i++];
      if (c === "{") depth += 1;
      else if (c === "}") depth -= 1;
    }
    out.push({ query: m[1].trim().replace(/\s+/g, " "), body: text.slice(re.lastIndex, i - 1) });
    re.lastIndex = i;
  }
  return out;
}

/** 一个块里每条规则的选择器列表和声明，按出现顺序：[{ selectors: [...], decl: "..." }]。 */
function rules(block) {
  return [...block.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
    selectors: m[1].split(",").map((x) => x.trim()), decl: m[2].replace(/\s+/g, " ").trim(),
  }));
}

/** "(min-width: a) and (max-width: b), (min-width: c) and (max-width: d)" 在宽度 w 下成不成立。 */
function queryMatches(query, w) {
  return query.split(",").some((part) => {
    const lo = /min-width:\s*(\d+)px/.exec(part);
    const hi = /max-width:\s*(\d+)px/.exec(part);
    return (!lo || w >= Number(lo[1])) && (!hi || w <= Number(hi[1]));
  });
}

const css = stripComments(web("style.css"));
const ZH_BTN = 'html[lang^="zh"] body:not(.has-alert-tag) #float-btn';
const zhBlock = () => {
  const found = mediaBlocks(css).filter((b) => b.body.includes(ZH_BTN));
  assert.equal(found.length, 1, "style.css 里该有且只有一个露字的 @media 块");
  return found[0];
};

test("中文页露字的宽度：1000–1100px 和 1140–1399px；1101–1139px、窄于 1000px、1400px 起不走这一段", () => {
  const { query } = zhBlock();
  assert.equal(query, "(min-width: 1000px) and (max-width: 1100px), (min-width: 1140px) and (max-width: 1399px)");
  for (const w of [1000, 1040, 1100, 1140, 1250, 1399]) assert.ok(queryMatches(query, w), w + "px 该露字");
  // 1101 起「手机同看」露出文字，按钮带字要到约 1117px 才回到 0 余量（docs/i18n-style.md §4.2 的表）
  for (const w of [999, 860, 1101, 1117, 1139, 1400, 1600]) assert.ok(!queryMatches(query, w), w + "px 不该露字");
});

test("露字的规则：每个选择器都只作用在中文页、报警标签没亮的 #float-btn；去掉图标，文字不再视觉隐藏", () => {
  const list = rules(zhBlock().body);
  assert.ok(list.length >= 3);
  for (const r of list) {
    for (const sel of r.selectors) {
      assert.ok(sel === ZH_BTN || sel.startsWith(ZH_BTN + " "), "选择器漏了前缀会连英文页一起改：" + sel);
    }
  }
  const of = (suffix) => list.find((r) => r.selectors.length === 1 && r.selectors[0] === ZH_BTN + suffix);
  assert.match(of("").decl, /padding:\s*0 10px/, "只有字的按钮左右各 10px（46px 宽，docs 的账按这个算）");
  assert.match(of(" .i").decl, /display:\s*none/, "露字时不带图标");
  const label = of(" .btn-label").decl;
  assert.match(label, /position:\s*static/);                        // 撤销 1px 的视觉隐藏
  assert.match(label, /width:\s*auto/);
  assert.match(label, /height:\s*auto/);
  assert.match(label, /margin:\s*0/);
  assert.match(label, /overflow:\s*visible/);
  assert.ok(!/#font-size|has-float-btn/.test(zhBlock().body), "字号滑块的收窄不在这一段");
});

test("默认（英文页、没量过的宽度、报警标签亮着）：窄于 1400px 只留图标，文字视觉隐藏，字号滑块收到 72px", () => {
  const block = mediaBlocks(css).find((b) => b.query === "(max-width: 1399px)" && b.body.includes("#float-btn"));
  assert.ok(block, "没有 (max-width: 1399px) 里的 #float-btn 规则");
  const list = rules(block.body);
  const hidden = list.find((r) => r.selectors.includes("#float-btn .btn-label"));
  assert.match(hidden.decl, /position:\s*absolute/);
  assert.match(hidden.decl, /width:\s*1px/);
  assert.match(hidden.decl, /clip:\s*rect\(0, 0, 0, 0\)/);
  assert.ok(!hidden.selectors.some((x) => x.includes("lang")), "默认这条不能带语言前缀：英文页靠它藏字");
  assert.match(list.find((r) => r.selectors.includes("#float-btn")).decl, /padding:\s*0 6px/);
  assert.match(list.find((r) => r.selectors.includes("body.has-float-btn #font-size")).decl, /width:\s*72px/);
  // 1101–1139px 这一小段再收到 64px（CI 实测 1101px 状态字差 0.03px）
  assert.ok(/@media \(min-width: 1101px\) and \(max-width: 1139px\)\s*\{\s*body\.has-float-btn #font-size \{ width: 64px; \}/.test(css));
  // 宽屏（≥1400px）的图标加文字没有被中文那一段改动：基础规则在所有 @media 之外
  assert.match(css, /#float-btn \{ gap: 4px; padding: 0 10px 0 7px; \}/);
});

test("浮窗按钮的可访问名称不依赖露不露字：title 和 .btn-label 里的文字都在，图标 aria-hidden", () => {
  const tag = /<button id="float-btn"[\s\S]*?<\/button>/.exec(web("index.html"))[0];
  assert.match(tag, /title="在始终置顶的小窗里看最新字幕和弹幕"/);
  assert.match(tag, /<svg class="i" aria-hidden="true">/);          // 样式用 .i 去掉图标，选择器靠这个类
  assert.match(tag, /<span class="btn-label" data-en="Floating Window">浮窗<\/span>/);
  assert.ok(!/aria-label=/.test(tag), "名称来自里面的文字，不另写 aria-label（露字与否都一样）");
});
