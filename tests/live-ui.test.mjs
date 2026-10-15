// 直播中界面纯逻辑（web/live-ui.js）的契约测试（node:test，零 npm 依赖）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { shareButtonText, connectHint, stripStatusEmoji, barIconFor,
        popoverRight, alertTierText, captionStateChip, liveBarChip, captionSpokenAt,
        captionAgeText, statsLineParts, captionScale, captionSizeVars,
        captionSizeFromConfig } = require("../web/live-ui.js");

test("同看关着：只写功能名，没有人数", () => {
  assert.deepEqual(shareButtonText(false, 3), { text: "手机同看", count: "" });
});
test("开着没人看：写「已打开」", () => {
  assert.deepEqual(shareButtonText(true, 0), { text: "手机同看 · 已打开", count: "" });
  assert.deepEqual(shareButtonText(true, undefined), { text: "手机同看 · 已打开", count: "" });
  assert.deepEqual(shareButtonText(true, -1), { text: "手机同看 · 已打开", count: "" });
  assert.deepEqual(shareButtonText(true, "abc"), { text: "手机同看 · 已打开", count: "" });
});
test("有人在看：写人数，count 给窄窗口的徽标", () => {
  assert.deepEqual(shareButtonText(true, 2), { text: "手机同看 · 2 人", count: "2" });
  assert.deepEqual(shareButtonText(true, "3"), { text: "手机同看 · 3 人", count: "3" });
});

test("连接中：带主播名；没有主播名用通用文案", () => {
  assert.deepEqual(connectHint("connecting", "bella", false), { show: true, inline: false, text: "正在连接 @bella…" });
  assert.deepEqual(connectHint("connecting", "", false), { show: true, inline: false, text: "正在连接…" });
});
test("连接中且已有字幕：改成字幕末尾一行", () => {
  assert.equal(connectHint("connecting", "bella", true).inline, true);
});
test("其余状态隐藏；offline 返回 null（维持原样）", () => {
  for (const s of ["idle", "live", "ended", "error", undefined]) {
    assert.deepEqual(connectHint(s, "bella", false), { show: false, inline: false, text: "" });
  }
  assert.equal(connectHint("offline", "bella", false), null);
});

test("去掉提示条开头的状态 emoji（含 FE0F 与空格）", () => {
  assert.equal(stripStatusEmoji("🔴 检测已降级：识别落后 30 秒"), "检测已降级：识别落后 30 秒");
  assert.equal(stripStatusEmoji("⚠️ 识别开始落后"), "识别开始落后");
  assert.equal(stripStatusEmoji("⚠识别开始落后"), "识别开始落后");
  assert.equal(stripStatusEmoji("✅ 识别已追上"), "识别已追上");
  assert.equal(stripStatusEmoji("🔴 ⚠️ 两个"), "两个");
});
test("句中的不动；没有 emoji 原样；空值给空串", () => {
  assert.equal(stripStatusEmoji("自检：人声降噪未生效"), "自检：人声降噪未生效");
  assert.equal(stripStatusEmoji("GPU ⚠️ 出错"), "GPU ⚠️ 出错");
  assert.equal(stripStatusEmoji(null), "");
  assert.equal(stripStatusEmoji(undefined), "");
});

test("提示条图标：形状跟级别走", () => {
  assert.equal(barIconFor("error"), "octagon");
  assert.equal(barIconFor("degraded"), "octagon");
  assert.equal(barIconFor("info"), "info");
  assert.equal(barIconFor("warn"), "warn");
  assert.equal(barIconFor("lagging"), "warn");
  assert.equal(barIconFor(undefined), "warn");
});

test("浮层右边缘对齐按钮右边缘", () => {
  assert.equal(popoverRight(1230, 1320, 420, 12), 90);
});
test("按钮太靠左：往右推，左边至少留 margin", () => {
  assert.equal(popoverRight(300, 1000, 420, 12), 1000 - 420 - 12);
});
test("按钮贴右边：右边至少留 margin", () => {
  assert.equal(popoverRight(1318, 1320, 420, 12), 12);
});
test("窗口比浮层还窄（浮层宽已按 100vw-24 收过）：两边各 12", () => {
  assert.equal(popoverRight(400, 420, 396, 12), 12);
});

test("报警分级文字；认不出的按「命中」，原型链上的键也不认", () => {
  assert.equal(alertTierText("exact"), "命中");
  assert.equal(alertTierText("variant"), "变体");
  assert.equal(alertTierText("fuzzy"), "疑似");
  assert.equal(alertTierText("weird"), "命中");
  assert.equal(alertTierText(undefined), "命中");
  assert.equal(alertTierText("toString"), "命中");
  assert.equal(alertTierText("__proto__"), "命中");
});

// ---- 底部大字幕的翻译状态、滞后，页脚各段，字幕字号（2026-10-15）。页面接线在 tests/live-bar.test.mjs ----

test("字幕翻译状态角标：没译成的四种结局各有一句，卡片和大字幕共用", () => {
  assert.deepEqual(captionStateChip({ translate_state: "failed" }), { text: "翻译失败", fail: true });
  assert.deepEqual(captionStateChip({ translate_state: "failed", refused: true }), { text: "未翻译（接口拒答）", fail: true });
  assert.deepEqual(captionStateChip({ translate_state: "failed", not_translation: true }),
                   { text: "未翻译（结果不像译文）", fail: true });
  assert.deepEqual(captionStateChip({ translate_state: "dropped" }), { text: "翻译已跳过（积压）", fail: false });
});
test("字幕翻译状态角标：译好的、等着的、不用译的、没带状态的都没有角标", () => {
  for (const state of ["ok", "pending", "skipped", undefined, "weird"]) {
    assert.deepEqual(captionStateChip({ translate_state: state }), { text: "", fail: false }, String(state));
  }
  assert.deepEqual(captionStateChip(null), { text: "", fail: false });
  assert.deepEqual(captionStateChip(undefined), { text: "", fail: false });
});

test("大字幕角标：译文到了没有；没译成与卡片同款；还在等译文不画（每句都要走的几秒，不闪）", () => {
  const none = { text: "", fail: false };
  assert.deepEqual(liveBarChip({ translate_state: "ok", translated: "你好" }), none);
  assert.deepEqual(liveBarChip({ translate_state: "failed", translated: "你好" }), none, "有译文就没有角标");
  assert.deepEqual(liveBarChip({ translate_state: "pending" }), none);
  assert.deepEqual(liveBarChip({ translate_state: "failed", refused: true }), { text: "未翻译（接口拒答）", fail: true });
  assert.deepEqual(liveBarChip({ translate_state: "dropped" }), { text: "翻译已跳过（积压）", fail: false });
  assert.deepEqual(liveBarChip({ translate_state: "skipped" }), none);
  assert.deepEqual(liveBarChip(null), none);
});

test("这一句什么时候说的：ts − e2e_ms − segment_ms；缺任何一项就是 null", () => {
  assert.equal(captionSpokenAt({ ts: 1000, e2e_ms: 2300, segment_ms: 6000 }, 5), 1000 * 1000 - 8300);
  assert.equal(captionSpokenAt({ e2e_ms: 2300, segment_ms: 6000 }, 50000), 50000 - 8300, "没有 ts 按收到的这一刻");
  assert.equal(captionSpokenAt({ ts: 1000, e2e_ms: 0, segment_ms: 500 }, 5), 1000 * 1000 - 500);
  for (const bad of [{ ts: 1000, e2e_ms: 2300 }, { ts: 1000, segment_ms: 6000 },
                     { ts: 1000, e2e_ms: null, segment_ms: 6000 }, { ts: 1000, e2e_ms: "2300", segment_ms: 6000 },
                     { ts: 1000, e2e_ms: -1, segment_ms: 6000 }, { ts: 1000, e2e_ms: 2300, segment_ms: 0 },
                     { ts: 1000, e2e_ms: NaN, segment_ms: 6000 }, {}, null, undefined]) {
    assert.equal(captionSpokenAt(bad, 5), null, JSON.stringify(bad));
  }
  assert.equal(captionSpokenAt({ e2e_ms: 1, segment_ms: 1 }, undefined), null, "ts 和现在都不知道");
});

test("约 N 秒前：三秒内「刚刚」，一分钟内写秒，之后写分钟", () => {
  assert.equal(captionAgeText(0), "刚刚");
  assert.equal(captionAgeText(2999), "刚刚");
  assert.equal(captionAgeText(3000), "约 3 秒前");
  assert.equal(captionAgeText(8300), "约 8 秒前");
  assert.equal(captionAgeText(8500), "约 9 秒前");
  assert.equal(captionAgeText(59000), "约 59 秒前");
  assert.equal(captionAgeText(60000), "约 1 分钟前");
  assert.equal(captionAgeText(125000), "约 2 分钟前");
  assert.equal(captionAgeText(6 * 3600 * 1000), "约 360 分钟前");
});
test("约 N 秒前：负数、离谱的大数、不是数的都不写", () => {
  for (const bad of [-1, -5000, 6 * 3600 * 1000 + 1, NaN, Infinity, null, undefined, "8000"]) {
    assert.equal(captionAgeText(bad), "", String(bad));
  }
});

const WAIT = { p50: 4200, p95: 7100, n: 20 };
const BASE = { asr: { p50: 1100 }, translate: { p50: 2000 }, segment: { p50: 800 }, detect_worst: { p50: 1900, p95: 3400 } };
test("页脚：字幕等待打头，违禁词报警延迟紧跟其后，其余各段照旧", () => {
  assert.deepEqual(statsLineParts(Object.assign({ caption_wait: WAIT }, BASE)), [
    "字幕 4.2s / P95 7.1s 后出现", "违禁词最迟 1.9s / P95 3.4s 内报警",
    "其中 切段 0.8s + 识别 1.1s", "译文再等 2.0s"]);
});
test("页脚：还没有字幕等待的数就不写这一段；违禁词延迟一个字不少", () => {
  for (const wait of [undefined, { p50: null, p95: null, n: 0 }, {}]) {
    const parts = statsLineParts(Object.assign({ caption_wait: wait }, BASE));
    assert.equal(parts[0], "违禁词最迟 1.9s / P95 3.4s 内报警");
    assert.equal(parts.length, 3);
  }
  assert.deepEqual(statsLineParts({}).slice(0, 1), ["违禁词最迟 — / P95 — 内报警"], "没数据写「—」，不抛");
});
test("页脚：计数从第四段起按 积压、丢音频、识别超时、跳过翻译、队列 的顺序追加", () => {
  const parts = statsLineParts(Object.assign({ caption_wait: WAIT, audio_backlog_sec: 5, audio_segments_dropped: 2,
    asr_overruns: 1, translation_jobs_dropped: 3, asr_queue_depth: 1, translation_queue_depth: 2 }, BASE));
  assert.deepEqual(parts.slice(4), ["积压 5s", "丢音频 2", "识别超时 1", "跳过翻译 3", "积压 1/2"]);
  assert.equal(statsLineParts(Object.assign({ audio_backlog_sec: 2.9 }, BASE)).length, 3, "积压不到 3 秒不写");
});

test("字幕字号比例：34 是 1（今天的固定字号），按 字号/34 走，夹在 0.85–1.75", () => {
  assert.equal(captionScale(34), 1);
  assert.equal(captionScale(51), 1.5);
  assert.equal(captionScale(40), 1.176);
  assert.equal(captionScale(60), 1.75);
  assert.equal(captionScale(20), 0.85, "往小调到底不低于 85%");
  assert.equal(captionScale(29), 0.853);
  for (const bad of [0, -3, NaN, undefined, null, "abc"]) assert.equal(captionScale(bad), 1, String(bad));
});
test("字幕字号：一个值带出大字幕的字号和卡片/弹幕的比例两个 CSS 变量", () => {
  assert.deepEqual(captionSizeVars(34), { "--sub-size": "34px", "--cap-scale": "1" });
  assert.deepEqual(captionSizeVars("46"), { "--sub-size": "46px", "--cap-scale": "1.353" });
});
test("字号取值：20–60 之间的整数才认，来路不明的不夹边界、给 null", () => {
  assert.equal(captionSizeFromConfig(34), 34);
  assert.equal(captionSizeFromConfig("40"), 40);
  assert.equal(captionSizeFromConfig(41.5), 42);
  assert.equal(captionSizeFromConfig(20), 20);
  assert.equal(captionSizeFromConfig(60), 60);
  for (const bad of [19, 61, 0, -4, 1e9, "", "big", null, undefined, true, false, NaN, Infinity]) {
    assert.equal(captionSizeFromConfig(bad), null, String(bad));
  }
});
