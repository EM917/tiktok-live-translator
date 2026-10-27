// 顶栏「闪购」按钮、浮层和「Aa」字号按钮的接线（web/index.html #flash-btn / #flash-panel / #font-btn，web/app.js）。
//
// 页面是 node:vm 里真跑的 web/app.js（tests/desktop-page.mjs），中文、英文两种界面各跑一遍：
//   1. 什么时候露出：hello 的 config 里有 flash_sale（后端只在 macOS 装了闪购助手时才放）就露出，首页和直播中都在；
//      没有这个键整颗不出现，重连到一个没有它的服务端时藏起来、浮层关掉；
//   2. 变色：后端的 attention 非空、或有没看过的问题记录（error、❌ 开头的 warn）就加 .attn，title / aria-label 跟着写；
//      打开浮层才算看过，记进 localStorage（读写都抛也照常跑，只在这一页记着）；
//   3. 浮层和同看、换主播、「Aa」互斥；Esc、点外面关；点「打开闪购助手」发 open_flash_helper，断线时不装作发出去了；
//      notice where=flash 写进浮层；倒计时每秒走；「还有 N 个」点开全列、关上收回；
//   4. 「Aa」：has-flash-btn 时字号控件收成按钮（样式在 style.css，这里查开合和 aria-expanded）；回首页、没了闪购按钮时关掉。
// Esc 和点外面关靠 page.dispatch 触发 document 上的监听；真浏览器里的版面由 G10 的 live-flash* / home-flash* 场景量。
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadDesktop, hello, web } from "./desktop-page.mjs";
import { SAMPLE, product, st } from "./flash-fixtures.mjs";

const calm = (extra = {}) => st(Object.assign({ attention: [], alerts: [],
  products: SAMPLE.products.map((p) => Object.assign({}, p, { stuck: false })) }, extra));
const hidden = (page, id) => page.el(id).classList.contains("hidden");
const btn = (page) => page.el("flash-btn");
const attn = (page) => btn(page).classList.contains("attn");
const open = (page) => !hidden(page, "flash-panel");
const sentOpen = (page) => page.sent.filter((m) => m.type === "open_flash_helper");

for (const lang of ["zh", "en"]) {
  const name = (s) => s + "（" + lang + "）";
  const T = (zh, en) => (lang === "en" ? en : zh);

  test(name("hello 里没有 flash_sale：整颗不出现，状态怎么变都不露；body 没有 has-flash-btn"), () => {
    const page = loadDesktop({ lang });
    assert.ok(hidden(page, "flash-btn"), "页面刚加载、还没 hello：藏着");
    hello(page);
    assert.ok(hidden(page, "flash-btn"));
    page.push({ type: "status", state: "idle", detail: "" });
    page.push({ type: "config", target_lang: "en" });
    assert.ok(hidden(page, "flash-btn"));
    assert.ok(!page.body.classList.contains("has-flash-btn"));
  });

  test(name("有 flash_sale：首页和直播中都露出；body 加 has-flash-btn；正常时不变色"), () => {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: calm(), status: { state: "idle", detail: "" } });
    assert.ok(!hidden(page, "flash-btn"), "首页也在");
    assert.ok(page.body.classList.contains("has-flash-btn"));
    assert.ok(!attn(page));
    assert.equal(btn(page).title, T("闪购助手 · 运行中", "Flash Sale Helper · Running"));
    assert.equal(btn(page).getAttribute("aria-label"), btn(page).title);
    page.push({ type: "status", state: "live", detail: "" });
    assert.ok(!hidden(page, "flash-btn"), "直播中也在");
    page.push({ type: "flash_sale", conn: "unreachable", stale: false, products: [], alerts: [], attention: [] });
    assert.ok(!hidden(page, "flash-btn") && !attn(page), "连不上：照样露出、不变色");
    assert.equal(btn(page).title, T("闪购助手 · 没在运行", "Flash Sale Helper · Not running"));
  });

  test(name("变色：后端 attention 非空，或有没看过的问题记录；打开浮层才算看过，记进 localStorage"), () => {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: calm() });
    assert.ok(!attn(page));
    page.push(Object.assign({ type: "flash_sale" }, calm({ attention: ["paused"], paused_reason: "awaiting_approval" })));
    assert.ok(attn(page));
    assert.equal(btn(page).title, T("闪购助手 · 要处理", "Flash Sale Helper · Needs attention"));
    page.push(Object.assign({ type: "flash_sale" }, calm({ alerts: SAMPLE.alerts })));   // 只有一条没看过的 ❌
    assert.ok(attn(page), "未读的问题记录也变色");
    assert.equal(page.store.get("flashSeen"), undefined);
    btn(page).click();
    assert.ok(open(page));
    assert.ok(!attn(page), "打开就算看过");
    assert.deepEqual(JSON.parse(page.store.get("flashSeen")), { at: SAMPLE.alerts[0].at_ms, texts: [SAMPLE.alerts[0].text] });
    const later = SAMPLE.alerts.concat([{ at_ms: SAMPLE.alerts[5].at_ms + 1000, level: "error", text: "x" }]);
    page.push(Object.assign({ type: "flash_sale" }, calm({ alerts: later })));
    assert.ok(attn(page), "开着时新来的照样算没看过");
    btn(page).click();                                            // 关上
    assert.ok(attn(page), "关上不算看过");
    btn(page).click();                                            // 再打开
    assert.ok(!attn(page));
  });

  test(name("看过的记录跨页面记着；localStorage 读写都抛时照常跑，只在这一页记着"), () => {
    const seen = JSON.stringify({ at: SAMPLE.alerts[0].at_ms, texts: [SAMPLE.alerts[0].text] });
    const again = loadDesktop({ lang, stored: { flashSeen: seen } });
    hello(again, { flash_sale: calm({ alerts: SAMPLE.alerts }) });
    assert.ok(!attn(again), "上一页看过的不再算新");
    const page = loadDesktop({ lang, brokenStorage: true });
    hello(page, { flash_sale: calm({ alerts: SAMPLE.alerts }) });
    assert.ok(attn(page));
    btn(page).click();
    assert.ok(open(page) && !attn(page), "存不了也照样记成看过");
  });

  test(name("重连后的 hello 没有这个键：按钮藏起来、浮层关掉、has-flash-btn 去掉"), () => {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: calm() });
    btn(page).click();
    assert.ok(open(page));
    hello(page);
    assert.ok(hidden(page, "flash-btn") && !open(page));
    assert.equal(btn(page).getAttribute("aria-expanded"), "false");
    assert.ok(!page.body.classList.contains("has-flash-btn"));
  });

  test(name("浮层：点按钮开合、aria-expanded 跟着；收到状态就画（不等打开）；回首页不关"), () => {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: st() });
    assert.ok(!open(page));
    assert.equal(page.el("flash-headline").textContent,
                 T("1 个商品停了超过 1 分钟。", "1 product has stayed stopped for over 1 min."), "没打开也已经画好");
    assert.equal(page.el("flash-products").children.length, 9);
    btn(page).click();
    assert.ok(open(page));
    assert.equal(btn(page).getAttribute("aria-expanded"), "true");
    page.push({ type: "status", state: "idle", detail: "" });
    assert.ok(open(page), "闪购按钮首页也在：回首页不关它的浮层");
    page.el("flash-close").click();
    assert.ok(!open(page));
    assert.equal(btn(page).getAttribute("aria-expanded"), "false");
  });

  test(name("和同看、换主播互斥；Esc 关掉它并把焦点还给按钮；点外面关"), () => {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: calm() });
    btn(page).click();
    page.el("share-btn").click();
    assert.ok(!open(page) && !hidden(page, "share-panel"), "打开同看关掉闪购");
    btn(page).click();
    assert.ok(open(page) && hidden(page, "share-panel"), "反过来也一样");
    page.el("switch-btn").click();
    assert.ok(!open(page) && !hidden(page, "switch-panel"));
    btn(page).click();
    assert.ok(open(page) && hidden(page, "switch-panel"));
    let focused = 0;
    btn(page).focus = () => { focused += 1; };
    page.dispatch("keydown", { key: "Escape" });
    assert.ok(!open(page));
    assert.equal(focused, 1);
    btn(page).click();
    page.dispatch("pointerdown", { target: page.el("flash-panel") });
    assert.ok(open(page));
    page.dispatch("pointerdown", { target: page.el("history") });
    assert.ok(!open(page));
  });

  test(name("「打开闪购助手」：发 open_flash_helper；断线时不装作发出去了，浮层里说一句；和本地服务断开期间禁用"), () => {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: { conn: "unreachable", stale: false, products: [], alerts: [], attention: [] } });
    btn(page).click();
    page.el("flash-open").click();
    assert.deepEqual(sentOpen(page), [{ type: "open_flash_helper" }]);
    assert.ok(hidden(page, "flash-note"));
    page.disconnect();
    page.el("flash-open").click();
    assert.equal(sentOpen(page).length, 1);
    assert.ok(!hidden(page, "flash-note"));
    assert.equal(page.el("flash-note").textContent, T("与本地服务断开，正在重连——稍候再试。",
      "Lost connection to the local service. Reconnecting… Try again in a moment."));
    page.connect();
    page.push({ type: "status", state: "offline", detail: "" });
    assert.equal(page.el("flash-open").disabled, true);
    page.push({ type: "status", state: "idle", detail: "" });
    assert.equal(page.el("flash-open").disabled, false);
    page.el("flash-open").click();
    assert.equal(sentOpen(page).length, 2);
    assert.ok(hidden(page, "flash-note"), "发出去了就把上一句收掉");
  });

  test(name("notice where=flash 写进浮层；关上浮层时收掉"), () => {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: calm() });
    btn(page).click();
    page.push({ type: "notice", where: "flash", text: "Couldn’t open Flash Sale Helper (open returned 1)." });
    assert.equal(page.el("flash-note").textContent, "Couldn’t open Flash Sale Helper (open returned 1).");
    assert.ok(!hidden(page, "flash-note"));
    page.push({ type: "notice", text: "elsewhere" });             // 不带 where 的不写进来
    assert.equal(page.el("flash-note").textContent, "Couldn’t open Flash Sale Helper (open returned 1).");
    btn(page).click();
    assert.ok(hidden(page, "flash-note"));
  });

  test(name("倒计时：浮层开着时每秒走；关着时不动"), () => {
    const page = loadDesktop({ lang });
    const ends = page.now() + 65000;
    hello(page, { flash_sale: calm({ products: [product("live", { ends_ms: ends })] }) });
    const span = () => page.el("flash-products").children[0].querySelector(".flash-countdown");
    assert.equal(span().textContent, T("还剩 1:05", "1:05 left"));
    page.advance(1000);
    page.tick(1000);
    assert.equal(span().textContent, T("还剩 1:05", "1:05 left"), "关着：不改");
    btn(page).click();                                            // 打开时按当下重画
    assert.equal(span().textContent, T("还剩 1:04", "1:04 left"));
    page.advance(2000);
    page.tick(1000);
    assert.equal(span().textContent, T("还剩 1:02", "1:02 left"));
  });

  test(name("45 个商品：先列 40 行，点「还有 5 个」全列；关上浮层收回"), () => {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: calm({ products: Array.from({ length: 45 }, (_, i) => product("live", { id: "p" + i })) }) });
    btn(page).click();
    const rows = () => page.el("flash-products").children.length;
    assert.equal(rows(), 40);
    assert.equal(page.el("flash-more").textContent, T("还有 5 个", "5 more"));
    page.el("flash-more").click();
    assert.equal(rows(), 45);
    assert.ok(hidden(page, "flash-more"));
    btn(page).click();
    assert.equal(rows(), 40);
  });

  test(name("「Aa」：点开合 #font-ctl（.open）、aria-expanded 跟着；和闪购浮层互斥；回首页、没了闪购按钮时关掉"), () => {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: calm() });
    const fontBtn = page.el("font-btn");
    const fontOpen = () => page.el("font-ctl").classList.contains("open");
    fontBtn.click();
    assert.ok(fontOpen());
    assert.equal(fontBtn.getAttribute("aria-expanded"), "true");
    btn(page).click();
    assert.ok(open(page) && !fontOpen(), "打开闪购浮层关掉 Aa");
    assert.equal(fontBtn.getAttribute("aria-expanded"), "false");
    fontBtn.click();
    assert.ok(fontOpen() && !open(page), "反过来也一样");
    page.dispatch("keydown", { key: "Escape" });
    assert.ok(!fontOpen());
    fontBtn.click();
    page.push({ type: "status", state: "idle", detail: "" });
    assert.ok(!fontOpen(), "回首页：字号控件不显示，小浮层也关掉");
    page.push({ type: "status", state: "live", detail: "" });
    fontBtn.click();
    hello(page);
    assert.ok(!fontOpen(), "没了闪购按钮：字号控件回到顶栏里");
    assert.equal(fontBtn.getAttribute("aria-expanded"), "false");
  });
}

// ---- 页面文件本身 -------------------------------------------------------------------------

test("按钮在浮窗之后、换主播之前，浮层紧跟着它；文字是视觉隐藏的 .btn-label，图标是 #i-flash（不是 emoji）", () => {
  const html = web("index.html");
  const tag = /<button id="flash-btn"[\s\S]*?<\/button>/.exec(html)[0];
  assert.ok(html.indexOf('id="float-btn"') < html.indexOf('id="flash-btn"'));
  assert.ok(html.indexOf('id="flash-btn"') < html.indexOf('id="flash-panel"'));
  assert.ok(html.indexOf('id="flash-panel"') < html.indexOf('id="switch-btn"'));
  assert.match(tag, /class="btn flash-btn hidden"/);
  assert.match(tag, /title="闪购助手" data-en-title="Flash Sale Helper"/);
  assert.match(tag, /aria-expanded="false" aria-controls="flash-panel"/);
  assert.match(tag, /<span class="btn-label" data-en="Flash Sale">闪购<\/span>/);
  assert.match(tag, /<svg class="i" aria-hidden="true"><use href="#i-flash"\/><\/svg>/);
  assert.match(html, /<symbol id="i-flash" viewBox="0 0 16 16">/);
  assert.match(html, /<script src="\/static\/flash\.js\?v=1"><\/script>\n {2}<script src="\/static\/app\.js/);
});

test("「Aa」紧挨着字号控件、名字同滑块（字幕字号 / Caption size），两种语言都写 Aa", () => {
  const html = web("index.html");
  const tag = /<button id="font-btn"[\s\S]*?<\/button>/.exec(html)[0];
  assert.match(tag, /title="字幕字号" aria-label="字幕字号"/);
  assert.match(tag, /data-en-title="Caption size" data-en-aria-label="Caption size"/);
  assert.match(tag, /aria-expanded="false" aria-controls="font-ctl"><span aria-hidden="true">Aa<\/span>/);
  const after = html.slice(html.indexOf(tag) + tag.length).replace(/^\s+/, "");
  assert.ok(after.startsWith('<label id="font-ctl" class="ctl ctl-font"'), "按钮后面紧跟着字号控件：" + after.slice(0, 60));
});

// ---- 样式：静态钉住几条（真实宽度由 G10 量） --------------------------------------------------------

const css = web("style.css").replace(/\/\*[\s\S]*?\*\//g, "");
function blocksWith(text, needle) {
  const out = [];
  const re = /@media\s*([^{]+)\{/g;
  let m;
  while ((m = re.exec(text))) {
    let depth = 1;
    let i = re.lastIndex;
    while (depth && i < text.length) depth += { "{": 1, "}": -1 }[text[i++]] || 0;
    const body = text.slice(re.lastIndex, i - 1);
    if (body.includes(needle)) out.push({ query: m[1].trim(), body });
    re.lastIndex = i;
  }
  return out;
}

test("样式：要处理时橙字橙底加一个不占宽度的橙点；窄于 1400px 只留图标（文字视觉隐藏，不是 display:none）", () => {
  assert.match(css, /#flash-btn\.attn \{ color: var\(--orange-ink\); background-color: var\(--a-orange-16\); \}/);
  const dot = /#flash-btn\.attn::after \{([^}]*)\}/.exec(css)[1];
  assert.match(dot, /position: absolute/);
  assert.match(dot, /width: 7px; height: 7px/);
  assert.match(css, /#flash-btn \{ position: relative;/);
  const narrow = blocksWith(css, "#flash-btn .btn-label").find((b) => b.query === "(max-width: 1399px)");
  assert.ok(narrow, "没有 (max-width: 1399px) 里的 #flash-btn .btn-label");
  assert.match(narrow.body, /#flash-btn \.btn-label \{\s*position: absolute; width: 1px; height: 1px;[^}]*clip: rect\(0, 0, 0, 0\);/);
  assert.ok(!/#float-btn/.test(narrow.body), "不和浮窗那一段混在一起（tests/float-button.test.mjs 按块找它）");
});

test("样式：「Aa」只在有闪购按钮、窄于 1400px、不在首页时出现；字号控件原地变成浮层，.open 才显示；≤720px 都收起", () => {
  assert.match(css, /#font-btn \{ display: none;/);
  const compact = blocksWith(css, "#font-btn").find((b) => b.query === "(max-width: 1399px)");
  assert.ok(compact);
  assert.match(compact.body, /body\.has-flash-btn:not\(\.home\) #font-btn \{ display: inline-flex; \}/);
  assert.match(compact.body, /body\.has-flash-btn #font-ctl \{[^}]*position: absolute;[^}]*display: none;/);
  assert.match(compact.body, /body\.has-flash-btn:not\(\.home\) #font-ctl\.open \{ display: flex; \}/);
  const tiny = blocksWith(css, "#font-btn").find((b) => b.query === "(max-width: 720px)");
  assert.ok(tiny);
  assert.match(tiny.body, /body\.has-flash-btn:not\(\.home\) #font-btn,\s*body\.has-flash-btn:not\(\.home\) #font-ctl\.open \{ display: none; \}/);
});

test("样式：中文页 1000–1399px 露「闪购」两个字（报警标签亮着时不露）；每个选择器都只作用在中文页", () => {
  const ZH = 'html[lang^="zh"] body:not(.has-alert-tag) #flash-btn';
  const found = blocksWith(css, ZH);
  assert.equal(found.length, 1);
  assert.equal(found[0].query, "(min-width: 1000px) and (max-width: 1399px)");
  const sels = [...found[0].body.matchAll(/([^{}]+)\{/g)].flatMap((m) => m[1].split(",").map((x) => x.trim()));
  assert.ok(sels.length >= 3 && sels.every((x) => x === ZH || x.startsWith(ZH + " ")), sels.join(" | "));
  assert.match(found[0].body, new RegExp(ZH.replace(/[[\]^().:]/g, "\\$&") + " \\.i \\{ display: none; \\}"));
  assert.match(found[0].body, /#flash-btn \.btn-label \{\s*position: static; width: auto; height: auto; margin: 0; overflow: visible;/);
});

test("样式：英文页 1400–1599px 仍只有图标（字视觉隐藏），1600px 起才露 Flash Sale", () => {
  const found = blocksWith(css, 'html[lang="en"] #flash-btn');
  assert.equal(found.length, 1);
  assert.equal(found[0].query, "(min-width: 1400px) and (max-width: 1599px)");
  assert.match(found[0].body, /html\[lang="en"\] #flash-btn \.btn-label \{\s*position: absolute; width: 1px;[^}]*clip: rect\(0, 0, 0, 0\);/);
});
