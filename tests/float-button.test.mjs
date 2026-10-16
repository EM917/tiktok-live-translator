// 顶栏「浮窗」按钮（web/index.html #float-btn、web/app.js updateFloatBtn / applyFloatConfig）。
//
// 页面是 node:vm 里真跑的 web/app.js（tests/desktop-page.mjs），中文、英文两种界面各跑一遍：
//   1. 什么时候露出：服务端 hello 里有 float_window.available（只有桌面应用窗口模式才有）且正在监听中/连接中，
//      和「换主播」按钮同一条规则；浏览器模式（没有这个键）、待机、结束、出错都藏着；
//   2. 点一下发 {type: "float_window", on: …}，等服务端回执期间禁用，回执（config.float_window）决定点亮；
//   3. 断线时点不出去就不装作点成了；回执迟迟不来 4 秒后自己解开；
//   4. 监听结束，服务端关掉浮窗并广播，按钮跟着藏起来、点亮清掉。
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
  assert.ok(Number(ver(index, "app.js")) >= 73 && Number(ver(index, "live-ui.js")) >= 3);
  assert.ok(Number(ver(index, "style.css")) >= 43);
});
