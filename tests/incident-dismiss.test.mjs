// 顶部持续提示条上的 ×（2026-10-21）：只有时钟跳变那一条（session:clock_gap）有。
//
// 页面是 node:vm 里真跑的 web/app.js（tests/desktop-page.mjs），中文、英文两种界面各跑一遍：
//   1. 只有时钟跳变那一条画 ×，别的提示（审计写不进去、网络断了……）照旧没有；
//   2. 点 × 发 dismiss_incident（带看到的那一条的 ts），这一页先收起，别的提示不动；
//   3. 服务端又挂出来（又跳了一次，文字是「本场第 N 次」）：× 跟着回来，点了带的是新的 ts；
//   4. 连接断着时点 ×：发不出去就不收——不然这一页看着关掉了，重连后又冒出来；
//   5. 按钮的 title、aria-label 是界面文字，跟着界面语言走。
// 服务端那一半（只认这一条、ts 对不上不收、10 分钟自己收起）在 tests/test_clock_gap_dismiss.py。
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadDesktop, hello, T } from "./desktop-page.mjs";

const KEY = "session:clock_gap";
const CLOSE = { zh: "关闭", en: "Close" };
const GAP1 = "18:07–18:08 电脑休眠或挂起了约 29 秒，程序没有运行，这段时间的直播没有监听。直播期间请接电源、不要合盖";
const GAP2 = "本场第 2 次：18:20–18:21 电脑休眠或挂起了约 40 秒，程序没有运行，这段时间的直播没有监听。直播期间请接电源、不要合盖";
const AUDIT = "审计日志写不进去";

const rows = (page) => page.el("incident-bar").children;
// 一行 = 级别图标 + 文字 <span>（+ 能关的那条另有 ×）：只取文字那一截
const textOf = (row) => row.children.filter((c) => c.tagName === "SPAN").map((s) => s.textContent).join("");
const closeOf = (row) => row.querySelector(".incident-close");
const dismissals = (page) => page.sent.filter((m) => m.type === "dismiss_incident");

function withIncidents(page) {
  hello(page, { incidents: {
    [KEY]: { id: KEY, level: "warn", text: GAP1, since: T + 5, ts: T + 5 },
    "session:audit-write": { id: "session:audit-write", level: "error", text: AUDIT, since: T + 1, ts: T + 1 },
  } });
}

for (const lang of ["zh", "en"]) {
  const name = (s) => s + "（" + lang + "）";

  test(name("只有时钟跳变那一条有 ×，按钮的 title / aria-label 跟着界面语言"), () => {
    const page = loadDesktop({ lang });
    withIncidents(page);
    const [audit, gap] = rows(page);                       // 按出现的先后排：审计那条先出现
    assert.equal(textOf(audit), AUDIT);
    assert.equal(textOf(gap), GAP1);
    assert.equal(closeOf(audit), null, "别的提示照旧不能关");
    assert.equal(audit.classList.contains("dismissible"), false);
    const btn = closeOf(gap);
    assert.ok(btn, "时钟跳变那一条要有 ×");
    assert.equal(btn.tagName, "BUTTON");
    assert.equal(btn.type, "button");
    assert.equal(btn.getAttribute("title"), CLOSE[lang]);
    assert.equal(btn.getAttribute("aria-label"), CLOSE[lang]);
    assert.ok(gap.classList.contains("dismissible"));
    assert.equal(page.el("incident-bar").classList.contains("hidden"), false);
  });

  test(name("点 × 发 dismiss_incident（带看到的 ts），这一页先收起，别的提示不动"), () => {
    const page = loadDesktop({ lang });
    withIncidents(page);
    closeOf(rows(page)[1]).click();
    assert.deepEqual(dismissals(page), [{ type: "dismiss_incident", id: KEY, ts: T + 5 }]);
    assert.deepEqual(rows(page).map(textOf), [AUDIT]);
    // 服务端的 clear 回来：已经收起了，再收一次也不出错
    page.push({ type: "incident", id: KEY, level: "clear", text: "", ts: T + 6 });
    assert.deepEqual(rows(page).map(textOf), [AUDIT]);
  });

  test(name("又跳一次：服务端重新挂出（本场第 2 次），× 跟着回来，点了带新的 ts"), () => {
    const page = loadDesktop({ lang });
    withIncidents(page);
    closeOf(rows(page)[1]).click();
    page.push({ type: "incident", id: KEY, level: "warn", text: GAP2, ts: T + 800 });
    const gap = rows(page).find((r) => textOf(r) === GAP2);
    assert.ok(gap && closeOf(gap), "重新挂出的那条照样有 ×");
    closeOf(gap).click();
    assert.deepEqual(dismissals(page).map((m) => m.ts), [T + 5, T + 800]);
    assert.deepEqual(rows(page).map(textOf), [AUDIT]);
  });

  test(name("只剩这一条时点 ×：整条提示栏收起"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push({ type: "incident", id: KEY, level: "warn", text: GAP1, ts: T + 5 });
    assert.equal(page.el("incident-bar").classList.contains("hidden"), false);
    closeOf(rows(page)[0]).click();
    assert.equal(rows(page).length, 0);
    assert.equal(page.el("incident-bar").classList.contains("hidden"), true);
  });

  test(name("连接断着时点 ×：发不出去就不收，重连后的 hello 以服务端为准"), () => {
    const page = loadDesktop({ lang });
    withIncidents(page);
    page.disconnect();
    closeOf(rows(page)[1]).click();
    assert.deepEqual(dismissals(page), []);
    assert.deepEqual(rows(page).map(textOf), [AUDIT, GAP1], "没发出去：这一页也不收");
    page.connect();
    closeOf(rows(page)[1]).click();
    assert.equal(dismissals(page).length, 1);
    assert.deepEqual(rows(page).map(textOf), [AUDIT]);
  });
}
