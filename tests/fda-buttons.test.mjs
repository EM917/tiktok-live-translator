// 「完全磁盘访问权限」那几步旁边的按钮（web/app.js FDA_BUTTONS / drawFdaLines；后端 app/browser_login.py
// fda_button_lines、fda_open_argvs，app/pipeline.py _open_fda）。
//
// 2026-10-29 新 Mac 上，用户照着黄条的文字做不下来，是我们替他打开了「完全磁盘访问权限」那一页、在访达里选中了
// 解释器，他拖进去才成功。这里在假 DOM 里把桌面页跑一遍（脚手架见 tests/desktop-dom.mjs），中文页、英文页各走一遍：
//   * 「没有读到 TikTok 登录」那条带 fda（{settings|python|terminal: 那一行}）时，按钮接在 fda 给的那一行后面：
//     第 1 步配「打开完全磁盘访问设置」，第 2 步配「在访达里选中 Python」，从终端启动时「终端」那一句配「在访达里
//     选中终端」；只按 fda 给的字整行比对，不从文字里猜；折着时在折起来的那几行里，展开才看得见；
//   * 不带 fda（不是 macOS，或者不是系统拒绝读取的那种）就一颗都没有；fda 里没有 terminal 就没有终端那一颗；
//     表外的键不画；比不上的那一颗接在最后，不丢；
//   * 点了只发 {type}，不带任何路径或参数；断线时发不出去也不报错；
//   * 刷新页面（hello 回放 config.incidents）照样画出来；
//   * 自检「浏览器登录态」那一行带 fda 时一样，「我加好了，再查一次」还在那几步下面。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { runDesktop, leftoverChinese } from "./desktop-dom.mjs";

const style = readFileSync(new URL("../web/style.css", import.meta.url), "utf8");
const ID = "login-unreadable";

// 样例照 app/browser_login.py fda_steps(buttons=True) 的样子抄（虚构路径）
const TEXT = {
  zh: {
    head: "解析流地址时，登录优先这一步只读 Safari 里的 TikTok 登录——Safari：系统拒绝读取。照下面三步给 Python 打开「完全磁盘访问权限」：",
    settings: "1. 点「打开完全磁盘访问设置」，会打开「系统设置」→「隐私与安全性」→「完全磁盘访问权限」。",
    python: "2. 点「在访达里选中 Python」，把访达里选中的文件拖进那一页的列表，再把它的开关打开。要加的文件：",
    path: "「/opt/demo/bin/python3.13」",
    named: "列表里显示的名字是 python3.13，不是本程序的名字。",
    terminal: "本程序是从终端启动的：点「在访达里选中终端」，把「终端」也拖进那个列表，再打开它的开关。",
    quit: "3. 按 ⌘Q 完全退出本程序，再打开它。",
    tail: "有的直播间 TikTok 只把流地址给已登录的观众。读到登录后这条提示自动消失。",
    labels: { settings: "打开完全磁盘访问设置", python: "在访达里选中 Python", terminal: "在访达里选中终端" },
    more: "显示全部步骤", again: "我加好了，再查一次",
    observed: "系统不允许本程序读取浏览器数据。照下面三步给 Python 打开「完全磁盘访问权限」：",
    rowName: "浏览器登录态", rowDetail: "Safari：系统拒绝读取",
  },
  en: {
    head: "When looking up the stream URL, the app first reads the TikTok sign-in from Safari only. Safari: macOS didn’t allow access. Follow these three steps to turn on Full Disk Access for Python:",
    settings: "1. Click Open Full Disk Access Settings. It opens System Settings > Privacy & Security > Full Disk Access.",
    python: "2. Click Show Python in Finder, drag the selected file into the list on that page, and turn on its switch. File to add:",
    path: "“/opt/demo/bin/python3.13”",
    named: "The list shows python3.13, not the app’s name.",
    terminal: "The app was started from a terminal. Click Show Terminal in Finder, drag Terminal into the same list, and turn on its switch.",
    quit: "3. Press ⌘Q to quit the app completely, then open it again.",
    tail: "For some live streams, TikTok provides the stream URL only to signed-in viewers. This notice disappears once a sign-in is found.",
    labels: { settings: "Open Full Disk Access Settings", python: "Show Python in Finder", terminal: "Show Terminal in Finder" },
    more: "Show All Steps", again: "Check Again",
    observed: "macOS didn’t allow the app to read browser data. Follow these three steps to turn on Full Disk Access for Python:",
    rowName: "Browser Login", rowDetail: "Safari: macOS didn’t allow access",
  },
};

const TYPES = { settings: "open_fda_settings", python: "reveal_python", terminal: "reveal_terminal" };

function notice(W, terminal) {
  return [W.head, W.settings, W.python, W.path, W.named].concat(terminal ? [W.terminal] : [])
    .concat([W.quit, W.tail]).join("\n");
}
function anchors(W, terminal) {
  const out = { settings: W.settings, python: W.python };
  if (terminal) out.terminal = W.terminal;
  return out;
}

const fdaSent = (page) => page.sent.filter((m) => Object.values(TYPES).includes(m.type));
const fdaButtons = (el) => el.querySelectorAll(".incident-fda").concat(el.querySelectorAll(".sc-fda"));
const label = (btn) => btn.textContent;

function hello(page, extra) {
  page.push({ type: "hello", config: Object.assign({ status: { state: "live", detail: "" }, version: "1.2.3" },
                                                   extra || {}) });
}
const incident = (page, more) => page.push(Object.assign({ type: "incident", id: ID, level: "warn", ts: 1 }, more));
const span = (page) => page.el("incident-bar").children[0].lastChild;

// 按钮前面那一段字（同一个父节点里、紧挨着它前面的文字节点，去掉开头的换行）
function lineBefore(btn) {
  const kids = btn.parentNode.childNodes;
  let i = kids.indexOf(btn) - 1;
  while (i >= 0 && kids[i].nodeType !== 3) i -= 1;
  return i >= 0 ? kids[i].nodeValue.replace(/^\n/, "") : null;
}

for (const lang of ["zh", "en"]) {
  const W = TEXT[lang];

  for (const terminal of [false, true]) {
    test(lang + "：持续提示带 fda 时，按钮接在第 1 步、第 2 步" + (terminal ? "、「终端」那一句" : "") +
         "后面，折在「显示全部步骤」里；点了只发类型", () => {
      const page = runDesktop(lang);
      hello(page);
      incident(page, { text: notice(W, terminal), recheck: true, fda: anchors(W, terminal) });
      const s = span(page);
      const rest = s.querySelector(".incident-rest");
      assert.ok(rest, "超过 3 行：折起来");
      assert.equal(rest.classList.contains("hidden"), true, "其余几行折着");
      const buttons = fdaButtons(s);
      const keys = terminal ? ["settings", "python", "terminal"] : ["settings", "python"];
      assert.deepEqual(buttons.map(label), keys.map((k) => W.labels[k]));
      buttons.forEach((btn, i) => {
        assert.equal(btn.tagName, "BUTTON");
        assert.equal(btn.getAttribute("type") === null ? btn.type : btn.getAttribute("type"), "button");
        assert.ok(rest.contains(btn), "按钮在折起来的那几行里，跟着它那一步");
        assert.equal(lineBefore(btn), W[keys[i]], "第 " + (i + 1) + " 颗跟在它那一行后面");
      });
      // 字没变：去掉按钮，折起来的那几行和原来一字不差（.incident > span 是 pre-line）
      const textOnly = rest.childNodes.filter((n) => n.nodeType === 3).map((n) => n.nodeValue).join("");
      assert.equal(textOnly, "\n" + notice(W, terminal).split("\n").slice(1).join("\n"));
      // 「我加好了，再查一次」还在第一行
      assert.equal(s.querySelector(".incident-recheck").textContent, W.again);
      buttons.forEach((btn) => btn.click());
      assert.deepEqual(fdaSent(page), keys.map((k) => ({ type: TYPES[k] })), "只发类型，不带路径或别的字段");
      if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
    });
  }

  test(lang + "：不带 fda（不是 macOS，或不是系统拒绝读取的那种）就一颗都没有；表外的键不画", () => {
    const page = runDesktop(lang);
    hello(page);
    incident(page, { text: notice(W, true), recheck: true });
    assert.deepEqual(fdaButtons(span(page)), []);
    assert.equal(span(page).querySelector(".incident-rest").textContent,
                 "\n" + notice(W, true).split("\n").slice(1).join("\n"), "没有 fda 时和以前一样");
    incident(page, { text: notice(W, false), recheck: true, fda: { shell: W.settings, python: W.python } });
    assert.deepEqual(fdaButtons(span(page)).map(label), [W.labels.python], "表里没有的键不画");
    incident(page, { text: notice(W, false), recheck: true, fda: "not an object" });
    assert.deepEqual(fdaButtons(span(page)), []);
  });

  test(lang + "：fda 给的那一行在文字里找不到时，那一颗接在最后，不丢", () => {
    const page = runDesktop(lang);
    hello(page);
    incident(page, { text: notice(W, false), recheck: true,
                     fda: { settings: W.settings, python: "2. something else entirely" } });
    const buttons = fdaButtons(span(page));
    assert.deepEqual(buttons.map(label), [W.labels.settings, W.labels.python]);
    const rest = span(page).querySelector(".incident-rest");
    assert.equal(rest.lastChild, buttons[1], "比不上的接在最后");
  });

  test(lang + "：刷新页面（hello 回放 config.incidents）照样画出这几颗按钮；撤掉以后没了", () => {
    const page = runDesktop(lang);
    hello(page, { incidents: { [ID]: { id: ID, level: "warn", text: notice(W, false), since: 1, ts: 1,
                                       recheck: true, fda: anchors(W, false) } } });
    assert.deepEqual(fdaButtons(span(page)).map(label), [W.labels.settings, W.labels.python]);
    incident(page, { level: "clear" });
    assert.equal(page.el("incident-bar").children.length, 0);
  });

  test(lang + "：断线时点了发不出去，也不抛错", () => {
    const page = runDesktop(lang);
    hello(page);
    incident(page, { text: notice(W, false), recheck: true, fda: anchors(W, false) });
    page.offline();
    fdaButtons(span(page))[0].click();
    assert.deepEqual(fdaSent(page), []);
    page.online();
    fdaButtons(span(page))[0].click();
    assert.deepEqual(fdaSent(page), [{ type: "open_fda_settings" }]);
  });

  test(lang + "：自检「浏览器登录态」那一行带 fda 时，按钮接在对应那一行后面，「我加好了，再查一次」还在下面", () => {
    const page = runDesktop(lang);
    hello(page, { status: { state: "idle", detail: "" } });
    const fix = [W.observed, W.settings, W.python, W.path, W.named, W.terminal, W.quit].join("\n");
    page.push({ type: "selfcheck", summary: { warn: 1, fail: 0 },
                checks: [{ name: W.rowName, level: "warn", detail: W.rowDetail, fix, recheck: true,
                           fda: anchors(W, true) }] });
    const li = page.el("sc-list").children[0];
    const fixEl = li.querySelector(".sc-fix");
    const buttons = fdaButtons(fixEl);
    assert.deepEqual(buttons.map(label), ["settings", "python", "terminal"].map((k) => W.labels[k]));
    buttons.forEach((btn, i) => {
      assert.ok(btn.cls.includes("btn") && btn.cls.includes("small"));
      assert.equal(lineBefore(btn), W[["settings", "python", "terminal"][i]]);
    });
    const again = fixEl.querySelector(".sc-recheck");
    assert.ok(again, "「我加好了，再查一次」还在");
    assert.ok(fixEl.childNodes.indexOf(again) > fixEl.childNodes.indexOf(buttons[2]), "在那几步下面");
    buttons[1].click();
    assert.deepEqual(fdaSent(page), [{ type: "reveal_python" }]);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
    // 读到登录：这一行是「通过」、不带步骤，也就没有按钮
    page.push({ type: "selfcheck", summary: { warn: 0, fail: 0 },
                checks: [{ name: W.rowName, level: "ok", detail: W.rowDetail, fix: "" }] });
    assert.deepEqual(fdaButtons(page.el("sc-list").children[0]), []);
  });

  test(lang + "：自检那一行不带 fda 时照旧整段字，不画按钮", () => {
    const page = runDesktop(lang);
    hello(page, { status: { state: "idle", detail: "" } });
    const fix = [W.observed, W.settings, W.python].join("\n");
    page.push({ type: "selfcheck", summary: { warn: 1, fail: 0 },
                checks: [{ name: W.rowName, level: "warn", detail: W.rowDetail, fix, recheck: true }] });
    const fixEl = page.el("sc-list").children[0].querySelector(".sc-fix");
    assert.deepEqual(fdaButtons(fixEl), []);
    assert.equal(fixEl.childNodes[0].nodeValue, fix);
  });
}

test("样式：持续提示里的按钮用这一条的字色加下划线，同「我加好了，再查一次」；自检那一行里的按钮行内接在字后面", () => {
  assert.match(style, /\.incident \.incident-fda \{[^}]*color: inherit;[^}]*text-decoration: underline;/);
  assert.match(style, /\.sc-item \.sc-fix \.sc-fda \{[^}]*margin-left:/);
});
