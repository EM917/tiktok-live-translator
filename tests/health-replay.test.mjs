// 识别落后提示（#health-bar）跟着服务端走（专家审查 F3）。补核（10-22）用真服务端加真页面复现：「识别开始落后
// （积压 23 秒）」显示着时页面断线，断线期间服务端发了「已追上」，重连后旧提示还挂着，一直到这一场停止。
// 原因是服务端不存、不回放 health，pipeline 等级不变又不再广播。现在 app/server.py 把它存进 config["health"]，
// hello 随 config 带来；页面在 hello 里先收起、再按 config.health 画；新的一场（实时的 session_break）开始时收起。
// 页面在 node:vm 里真跑（tests/desktop-page.mjs）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadDesktop, hello, caption, T } from "./desktop-page.mjs";

const LAGGING = { level: "lagging", backlog_sec: 23.0, text: "⚠️ 识别开始落后（积压 23 秒），报警会相应延迟" };
// 识别连续出错时 pipeline 发的那条（Pipeline._asr_unrecoverable）：等级是 degraded，积压却是 0
const ASR_FAILING = { level: "degraded", backlog_sec: 0.0,
                      text: "🔴 识别连续出错。程序自动恢复不了——请关闭程序重新打开；若仍出错请反馈" };

function bar(page) {
  const el = page.el("health-bar");
  return { shown: !el.cls.includes("hidden"), text: el.textContent, degraded: el.cls.includes("degraded") };
}

test("hello 带着 lagging：重连 / 刷新后的页面照样显示落后提示，字以服务端为准", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page, { health: LAGGING });
  assert.deepEqual(bar(page), { shown: true, text: "识别开始落后（积压 23 秒），报警会相应延迟", degraded: false });
});

test("断线期间「已追上」了：重连的 hello 里没有 health，旧的落后提示收起（以前一直挂到这一场停止）", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page);
  page.push(Object.assign({ type: "health" }, LAGGING));
  assert.equal(bar(page).shown, true);
  // 页面断线；服务端在这期间发了 level=ok，config["health"] 已经删掉。页面重连：
  hello(page);
  assert.equal(bar(page).shown, false);
});

test("hello 带着 health 但这一场已经停了（idle）：不揭开（renderHealth 只描述正在进行的那一场）", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page, { status: { state: "idle", detail: "" }, health: LAGGING });
  assert.equal(bar(page).shown, false);
});

test("实时的 session_break（换主播）收起上一场的落后提示；回放里的分隔线不碰此刻的提示", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page);
  page.push(caption(1, "hola", { translated: "你好", translate_state: "ok" }));
  page.push(Object.assign({ type: "health" }, LAGGING));
  assert.equal(bar(page).shown, true);
  // 换主播走 quiet 停止，不发 idle；新一场的统计循环从 ok 数起，不会补发「已追上」
  page.push({ type: "session_break", ts: T + 60, streamer: "sol_demo" });
  assert.equal(bar(page).shown, false, "新的一场开始：上一场的落后提示作废");

  // 重连：hello 带着此刻（新一场）的落后，回放里有一条历史分隔线——它不该把此刻的提示收掉
  hello(page, { health: LAGGING });
  page.push(caption(1, "hola", { translated: "你好", translate_state: "ok", replay: true }));
  page.push({ type: "session_break", ts: T + 60, streamer: "sol_demo", replay: true });
  page.push(caption(2, "adios", { translated: "再见", translate_state: "ok", replay: true, restore: true }));
  assert.equal(bar(page).shown, true);
});

test("stats 带 health=ok 不收提示：识别连续出错时「检测已降级」要一直挂着（积压是 0，每跳 stats 都是 ok）", () => {
  const page = loadDesktop({ lang: "zh" });
  hello(page);
  page.push(Object.assign({ type: "health" }, ASR_FAILING));
  assert.ok(bar(page).shown && bar(page).degraded);
  for (let i = 0; i < 3; i++) page.push({ type: "stats", health: "ok", audio_backlog_sec: 0.0 });
  assert.equal(bar(page).shown, true, "pipeline 有意不让按积压算的 ok 盖掉它（Pipeline._announce_health）");
  // 真正的「已追上」（health level=ok）才收起
  page.push({ type: "health", level: "ok", backlog_sec: 0.0, text: "✅ 识别已追上，检测恢复正常" });
  assert.equal(bar(page).shown, false);
});

test("停止、下播、出错之后晚到的 health 不揭开（服务端也不再存它，重连的 hello 里没有）", () => {
  for (const state of ["idle", "ended", "error"]) {
    const page = loadDesktop({ lang: "zh" });
    hello(page);
    page.push(Object.assign({ type: "health" }, LAGGING));
    assert.equal(bar(page).shown, true);
    page.push({ type: "status", state, detail: "" });
    assert.equal(bar(page).shown, false, state);
    page.push(Object.assign({ type: "health" }, LAGGING));     // 比如下播之后、统计循环被取消之前的那一跳
    assert.equal(bar(page).shown, false, state + "：这一场已经结束");
  }
});
