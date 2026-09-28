// 设置分组（自检 / 引擎 / 报警）几行的纯逻辑契约测试。照 tests/follow.test.mjs /
// tests/brand.test.mjs 的加载方式（node:test，零 npm 依赖）。
//
// 覆盖的是 engineer.md #4 点名的那批行为：自检失败自动展开、手动收起后 hello
// 重放不再弹开；引擎回退提示同一条重放不重开、换新提示才重开、清除后摘要
// 不再标橙。这些行为本身要靠 app.js 里的 DOM 调用才能观察到，但驱动它们的
// 判断——摘要该写什么、折叠行该不该跟着动——都在这几个纯函数里，这里直接钉
// 这些函数的输入输出，不用假 DOM 也能防回归。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { selfcheckSummary, engineSummary, watchSummary, nextAutoOpen } =
  require("../web/settings-rows.js");

// ---- selfcheckSummary：三种结论 + 签名 ----

test("全部通过：摘要写总数，图标 pass", () => {
  assert.deepEqual(selfcheckSummary({ fail: 0, warn: 0, total: 11 }),
    { text: "全部通过 · 11 项", icon: "pass", sig: "0/0/11" });
});

test("只有提醒：摘要写提醒数，图标 warn", () => {
  assert.deepEqual(selfcheckSummary({ fail: 0, warn: 2, total: 11 }),
    { text: "通过 · 2 项提醒", icon: "warn", sig: "0/2/11" });
});

test("有失败：摘要写失败数（哪怕同时也有提醒），图标 fail", () => {
  assert.deepEqual(selfcheckSummary({ fail: 1, warn: 2, total: 11 }),
    { text: "1 项功能未生效", icon: "fail", sig: "1/2/11" });
});

test("缺 summary 字段：当 0 处理，不抛异常", () => {
  assert.equal(selfcheckSummary({}).icon, "pass");
  assert.equal(selfcheckSummary(undefined).icon, "pass");
});

test("签名只由 fail/warn/total 三个数决定，跟检查项的具体文字无关", () => {
  const a = selfcheckSummary({ fail: 1, warn: 0, total: 11 });
  const b = selfcheckSummary({ fail: 1, warn: 0, total: 11 });
  assert.equal(a.sig, b.sig);
});

// ---- engineSummary：人话名 / 回退前缀 / 免费额度 ----

test("有 active_label：直接用，不用内部代号", () => {
  assert.equal(engineSummary({ engine: "auto", active_label: "本地 Hy-MT2 1.8B", active: "hymt2" }).text,
    "本地 Hy-MT2 1.8B");
});

test("没有 active_label：兜底用 active 原样显示（老版本/回放数据没带人话名）", () => {
  assert.equal(engineSummary({ engine: "auto", active: "hymt2" }).text, "hymt2");
});

test("engine=none 且没有 active/active_label：写不翻译", () => {
  assert.equal(engineSummary({ engine: "none" }).text, "不翻译");
});

test("免费额度：拼百分比和按近期速度估算的剩余小时数", () => {
  const s = engineSummary({ engine: "deepl", active_label: "DeepL",
    usage: { used: 17500, limit: 35000 } });
  assert.equal(s.text, "DeepL · 免费额度已用 50%（按近期速度约剩 0 小时）");
});

test("usage.limit 为 0（未设置额度）：不拼额度提示", () => {
  const s = engineSummary({ engine: "deepl", active_label: "DeepL", usage: { used: 0, limit: 0 } });
  assert.equal(s.text, "DeepL");
});

test("有回退提示：文案加「已回退 ·」前缀，hasNote 为真，sig 就是提示原文", () => {
  const s = engineSummary({ engine: "auto", active_label: "本地 Hy-MT2 1.8B",
    note: "上次选的翻译引擎 DeepL 还没有密钥，本次先用自动" });
  assert.equal(s.text, "已回退 · 本地 Hy-MT2 1.8B");
  assert.equal(s.hasNote, true);
  assert.equal(s.sig, "上次选的翻译引擎 DeepL 还没有密钥，本次先用自动");
});

test("没有回退提示：hasNote 为假，sig 是空串", () => {
  const s = engineSummary({ engine: "auto", active_label: "本地 Hy-MT2 1.8B" });
  assert.equal(s.hasNote, false);
  assert.equal(s.sig, "");
});

// ---- watchSummary：开关 + 词表条数 ----

test("词表非空、开关开：desc 说明会实时报警", () => {
  assert.deepEqual(watchSummary(true, 53), {
    mode: "开启", state: "词表 53 条", configured: true,
    desc: "开播后会实时监听主播原话，命中立即报警（不依赖翻译，翻译再慢也不影响报警）。",
  });
});

test("词表非空、开关关：desc 说明只记审计", () => {
  const s = watchSummary(false, 53);
  assert.equal(s.mode, "关闭");
  assert.equal(s.desc, "开播后不报警；命中只记入审计。要报警请先打开此开关。");
});

test("词表为空：不管开关状态，desc 都是「不会报警」，跟开关状态无关", () => {
  const on = watchSummary(true, 0);
  const off = watchSummary(false, 0);
  assert.equal(on.state, "词表为空");
  assert.equal(off.state, "词表为空");
  assert.equal(on.desc, off.desc);
  assert.equal(on.desc, "当前词表为空，本工具不会发出任何违禁词报警。");
  assert.equal(on.configured, false);
});

// ---- nextAutoOpen：签名没变就不动，变了才按调用方的规则展开/收起 ----

test("签名没变：changed 为假，open 值不重要（调用方不应用它）", () => {
  const r = nextAutoOpen("1/0/11", "1/0/11", true);
  assert.equal(r.changed, false);
  assert.equal(r.sig, "1/0/11");
});

test("签名变了：changed 为真，open 就是 openWhenChanged", () => {
  const r = nextAutoOpen("0/0/11", "1/0/11", true);
  assert.equal(r.changed, true);
  assert.equal(r.open, true);
});

test("prevSig 是 null（页面刚加载，还没收到过结论）：第一次总是当成变了", () => {
  const r = nextAutoOpen(null, "0/0/11", false);
  assert.equal(r.changed, true);
  assert.equal(r.open, false);
});

test("自检式用法：签名一变就无条件同步展开/收起——包括从失败变回全绿", () => {
  // 第一条：出现失败，签名从初始 null 变成 "1/0/11"，按 fail>0 展开
  let prevSig = null;
  let r = nextAutoOpen(prevSig, selfcheckSummary({ fail: 1, warn: 0, total: 11 }).sig, true);
  prevSig = r.sig;
  assert.equal(r.changed, true);
  assert.equal(r.open, true);   // 应该展开

  // 中控手动收起（app.js 里发生，这里不用模拟，只验证下一步不会覆盖它）：
  // 重连回放同一条结论——签名没变，changed 为假，app.js 不会调用 setSelfcheckOpen
  r = nextAutoOpen(prevSig, selfcheckSummary({ fail: 1, warn: 0, total: 11 }).sig, true);
  assert.equal(r.changed, false, "同一条结论重放不应该被判定为变化，不能覆盖用户手动收起的状态");

  // 后来真的修好了：签名变了，无条件同步为收起
  r = nextAutoOpen(prevSig, selfcheckSummary({ fail: 0, warn: 0, total: 11 }).sig, false);
  assert.equal(r.changed, true);
  assert.equal(r.open, false);
});

test("引擎回退式用法：同一条提示重放不重开；换新提示才重开；清除后 changed 为真但调用方不应用 open", () => {
  const noteA = "上次选的翻译引擎 DeepL 还没有密钥，本次先用自动";
  const noteB = "另一条提示";

  // 首次出现回退提示：展开
  let prevSig = null;
  let sum = engineSummary({ engine: "auto", active_label: "本地 Hy-MT2 1.8B", note: noteA });
  let r = nextAutoOpen(prevSig, sum.sig, true);
  prevSig = r.sig;
  assert.equal(r.changed, true);
  assert.equal(sum.hasNote && r.open, true, "首次出现回退提示应该展开");

  // 中控手动收起后，重连 hello 回放同一条提示：签名没变，不应该重新弹开
  sum = engineSummary({ engine: "auto", active_label: "本地 Hy-MT2 1.8B", note: noteA });
  r = nextAutoOpen(prevSig, sum.sig, true);
  assert.equal(r.changed, false, "同一条回退提示重放不应该重新展开手动收起的行");

  // 换了一条新提示：签名变了，应该重开
  sum = engineSummary({ engine: "auto", active_label: "本地 Hy-MT2 1.8B", note: noteB });
  r = nextAutoOpen(prevSig, sum.sig, true);
  prevSig = r.sig;
  assert.equal(r.changed, true);
  assert.equal(sum.hasNote && r.open, true, "新提示应该重新展开");

  // 提示被清除：签名变成空串，changed 为真，但 hasNote 为假——
  // app.js 的调用方式是 `if (r.changed && engSum.hasNote)`，所以不会强制收起
  sum = engineSummary({ engine: "auto", active_label: "本地 Hy-MT2 1.8B" });
  r = nextAutoOpen(prevSig, sum.sig, true);
  assert.equal(r.changed, true);
  assert.equal(sum.hasNote, false, "提示清除后 hasNote 为假，调用方据此跳过 setRowOpen");
});
