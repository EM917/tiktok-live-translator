// 纯函数模块顶上那一行 node 守卫（spec §2.3）在浏览器里必须是空操作。
//
// 守卫是 `if (typeof L === "undefined") { var I18N_ = require("./i18n.js"); var L = …; }`：
// node 测试里各文件自己 require i18n.js；浏览器里 i18n.js 是页面的第一个外部脚本，L 早已是全局
// 函数，这一块不该执行，块里的 var 也不该把 L 清掉——清掉了整页就会退回中文或直接抛错。
// 这里照 index.html 的脚本顺序，把 app.js 之前的外部脚本放进同一个 vm 全局作用域里按「经典脚本」
// 逐个跑（和浏览器一样共享全局、各自是一段 Script），中文页、英文页各跑一次。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// Windows 的 Git 默认 core.autocrlf=true，检出的是 CRLF：统一成 \n
const web = (name) =>
  readFileSync(new URL("../web/" + name, import.meta.url), "utf8").replace(/\r\n/g, "\n");
const GOLDEN = JSON.parse(readFileSync(new URL("./i18n_golden.json", import.meta.url), "utf8"));
const GUARDED = ["alerts.js", "brand.js", "live-ui.js", "session-divider.js", "settings-rows.js", "switch.js"];

function pageScripts() {
  const srcs = [...web("index.html").matchAll(/<script src="\/static\/([^"?]+)/g)].map((m) => m[1]);
  return srcs.slice(0, srcs.indexOf("app.js"));
}

// lang 是服务端写进 <html lang> 的值（app/i18n.py inject_lang）
function runPage(lang) {
  const classes = new Set();
  const ctx = vm.createContext({
    document: {
      documentElement: { lang, classList: { add: (c) => classes.add(c) } },
      querySelectorAll: () => [],
      createTextNode: (t) => ({ nodeType: 3, nodeValue: String(t) }),
    },
    require: () => { throw new Error("浏览器里不该走到 require：守卫执行了"); },
  });
  let L0 = null;
  for (const name of pageScripts()) {
    vm.runInContext(web(name), ctx, { filename: "web/" + name });
    if (name === "i18n.js") L0 = ctx.L;
  }
  return { ctx, L0, classes };
}

test("页面里 i18n.js 排第一，带守卫的六个模块都在 app.js 之前加载", () => {
  const scripts = pageScripts();
  assert.equal(scripts[0], "i18n.js");
  for (const name of GUARDED) {
    assert.ok(scripts.includes(name), name + " 不在 app.js 之前");
    assert.equal(web(name).split("\n").filter((l) => l.startsWith('if (typeof L === "undefined")')).length, 1,
      name + " 应恰好有一行守卫");
  }
});

test("中文页：守卫不执行，L 还是 i18n.js 的那个，各模块照旧出中文", () => {
  const { ctx, L0, classes } = runPage("zh-CN");
  assert.equal(typeof L0, "function");
  assert.equal(ctx.L, L0, "块里的 var L 不能把全局的 L 换掉");
  assert.equal(ctx.I18N_, undefined, "守卫那一块没执行");
  assert.equal(ctx.UI_LANG, "zh");
  assert.equal(ctx.alertTitle(2), "(2) 疑似违禁词 · TikTok 直播同传");
  assert.equal(ctx.selfcheckSummary({ fail: 0, warn: 0, total: 11 }).text, "全部通过 · 11 项");
  assert.equal(ctx.shareButtonText(true, 2).text, "手机同看 · 2 人");
  assert.equal(ctx.switchButtonLabel(null, "").text, "确认换主播");
  assert.match(ctx.sessionDividerText({ ts: 1, streamer: "x" }), / 以下为 @x ──$/);
  assert.equal(ctx.buildBrandOptionList([])[0].label, "不限（默认）");
  assert.ok(classes.has("i18n-done"));
});

test("英文页：守卫同样不执行，各模块出英文（窗口标题与金句逐字相同）", () => {
  const { ctx, L0 } = runPage("en");
  assert.equal(ctx.L, L0);
  assert.equal(ctx.I18N_, undefined);
  assert.equal(ctx.UI_LANG, "en");
  assert.equal(ctx.alertTitle(2), GOLDEN.window_title_2_en);
  assert.equal(ctx.selfcheckSummary({ fail: 0, warn: 0, total: 11 }).text, "All 11 checks passed");
  assert.equal(ctx.shareButtonText(true, 2).text, "Phone Viewing · 2");
  assert.equal(ctx.switchButtonLabel(null, "").text, "Switch Streamer");
  assert.match(ctx.sessionDividerText({ ts: 1, streamer: "x" }), / Now monitoring @x ──$/);
  assert.equal(ctx.buildBrandOptionList([])[0].label, "Any (default)");
  assert.equal(ctx.alertTierText("fuzzy"), "Similar");
});
