// 直播中界面纯逻辑（web/live-ui.js）的契约测试（node:test，零 npm 依赖）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { shareButtonText, floatButtonState, connectHint, stripStatusEmoji, barIconFor,
        popoverRight, alertTierText, jumpLatestCaptionsText,
        jumpLatestCommentsText,
        captionStateChip, liveBarChip, captionAudioEndAt,
        captionAgeText, CAPTION_LAG_SHOW_SEC, captionScale, captionSizeVars,
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

test("浮窗按钮：服务端提供这个功能 + 正在监听才露出；开着才点亮；等回执时禁用", () => {
  assert.deepEqual(floatButtonState(true, true, false, false), { hidden: false, on: false, pressed: false, disabled: false });
  assert.deepEqual(floatButtonState(true, true, true, false), { hidden: false, on: true, pressed: true, disabled: false });
  assert.deepEqual(floatButtonState(true, true, true, true), { hidden: false, on: true, pressed: true, disabled: true });
  // 浏览器模式（服务端没有这个键）、待机 / 结束：整个藏起来，点亮和禁用也不留
  assert.deepEqual(floatButtonState(false, true, true, true), { hidden: true, on: false, pressed: false, disabled: false });
  assert.deepEqual(floatButtonState(true, false, true, true), { hidden: true, on: false, pressed: false, disabled: false });
  assert.deepEqual(floatButtonState(undefined, undefined, undefined, undefined), { hidden: true, on: false, pressed: false, disabled: false });
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

// 专家审查 U7：首次下载识别模型时中间卡片写下载进度。只认 status 的结构化 progress，不看 detail
test("连接中带下载进度：卡片写「正在下载识别模型… 37%」，主播名不再写", () => {
  const dl = { kind: "model_download", pct: 37 };
  assert.deepEqual(connectHint("connecting", "luna_demo", false, dl),
    { show: true, inline: false, text: "正在下载识别模型… 37%" });
  assert.equal(connectHint("connecting", "luna_demo", true, dl).inline, true, "已有字幕时照旧改成末尾一行");
  assert.equal(connectHint("connecting", "", false, { kind: "model_download", pct: 0 }).text, "正在下载识别模型… 0%");
  assert.equal(connectHint("connecting", "", false, { kind: "model_download", pct: 99.7 }).text, "正在下载识别模型… 99%");
  assert.equal(connectHint("connecting", "", false, { kind: "model_download", pct: 150 }).text, "正在下载识别模型… 100%");
  assert.equal(connectHint("connecting", "", false, { kind: "model_download", pct: -3 }).text, "正在下载识别模型… 0%");
});
test("下载进度缺 pct（大小未知）或不是数：只写「正在下载识别模型…」", () => {
  for (const pct of [undefined, null, "37", NaN, Infinity]) {
    assert.equal(connectHint("connecting", "luna_demo", false, { kind: "model_download", pct }).text,
                 "正在下载识别模型…", String(pct));
  }
});
test("没有 progress、只有 detail、kind 认不出：照旧「正在连接 @…」（不把横幅那句搬进来）", () => {
  // connectHint 根本不收 detail：横幅里的「正在解析直播流地址…」之类不会在中间再写一遍
  for (const progress of [undefined, null, {}, "model_download", { kind: "other", pct: 50 }, { pct: 50 }]) {
    assert.deepEqual(connectHint("connecting", "luna_demo", false, progress),
      { show: true, inline: false, text: "正在连接 @luna_demo…" }, JSON.stringify(progress));
  }
});
test("下载进度只在连接中显示：直播中、待机、出错时带着也不写", () => {
  const dl = { kind: "model_download", pct: 37 };
  for (const s of ["idle", "live", "ended", "error"]) {
    assert.deepEqual(connectHint(s, "luna_demo", false, dl), { show: false, inline: false, text: "" }, s);
  }
  assert.equal(connectHint("offline", "luna_demo", false, dl), null);
});

// 专家审查 U8：直播中这一场还没出过字幕时，中间卡片写「等待第一句字幕…」（和浮窗同一句）
test("直播中、这一场还没出过字幕：写「等待第一句字幕…」；有上一场字幕时改成末尾一行", () => {
  assert.deepEqual(connectHint("live", "luna_demo", false, null, true),
    { show: true, inline: false, text: "等待第一句字幕…" });
  assert.deepEqual(connectHint("live", "luna_demo", true, null, true),
    { show: true, inline: true, text: "等待第一句字幕…" });
  assert.deepEqual(connectHint("live", "luna_demo", true, null, false), { show: false, inline: false, text: "" },
                   "出过字幕就收起");
});
test("「等待第一句」只在直播中：连接中照旧写连接，其余状态不写", () => {
  assert.equal(connectHint("connecting", "luna_demo", false, null, true).text, "正在连接 @luna_demo…");
  for (const s of ["idle", "ended", "error"]) {
    assert.deepEqual(connectHint(s, "luna_demo", false, null, true), { show: false, inline: false, text: "" }, s);
  }
  assert.equal(connectHint("offline", "luna_demo", false, null, true), null);
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

test("「回到最新」：没有新的就只写按钮名，有新的带上条数（字幕写「条新字幕」，弹幕写「条新弹幕」）", () => {
  for (const n of [0, -3, NaN, undefined, null, "abc", 0.4]) {
    assert.equal(jumpLatestCaptionsText(n), "回到最新", String(n));
    assert.equal(jumpLatestCommentsText(n), "回到最新", String(n));
  }
  assert.equal(jumpLatestCaptionsText(1), "回到最新 · 1 条新字幕");
  assert.equal(jumpLatestCaptionsText(12), "回到最新 · 12 条新字幕");
  assert.equal(jumpLatestCaptionsText("7"), "回到最新 · 7 条新字幕");
  assert.equal(jumpLatestCaptionsText(3.9), "回到最新 · 3 条新字幕");
  assert.equal(jumpLatestCommentsText(1), "回到最新 · 1 条新弹幕");
  assert.equal(jumpLatestCommentsText(120), "回到最新 · 120 条新弹幕");
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

test("这一句的音频什么时候结束的：ts − e2e_ms；e2e_ms 缺了就是 null，segment_ms 有没有都不影响", () => {
  assert.equal(captionAudioEndAt({ ts: 1000, e2e_ms: 2300 }, 5), 1000 * 1000 - 2300);
  assert.equal(captionAudioEndAt({ ts: 1000, e2e_ms: 2300, segment_ms: 6000 }, 5), 1000 * 1000 - 2300, "不再减片段时长");
  assert.equal(captionAudioEndAt({ e2e_ms: 2300 }, 50000), 50000 - 2300, "没有 ts 按收到的这一刻");
  assert.equal(captionAudioEndAt({ ts: 1000, e2e_ms: 0 }, 5), 1000 * 1000);
  for (const bad of [{ ts: 1000 }, { ts: 1000, segment_ms: 6000 }, { ts: 1000, e2e_ms: null },
                     { ts: 1000, e2e_ms: "2300" }, { ts: 1000, e2e_ms: -1 }, { ts: 1000, e2e_ms: NaN },
                     {}, null, undefined]) {
    assert.equal(captionAudioEndAt(bad, 5), null, JSON.stringify(bad));
  }
  assert.equal(captionAudioEndAt({ e2e_ms: 1 }, undefined), null, "ts 和现在都不知道");
});

test("约 N 秒前：不到 15 秒不写（健康的直播里一句话停不到那么久），之后一分钟内写秒，再之后写分钟", () => {
  assert.equal(CAPTION_LAG_SHOW_SEC, 15);
  for (const ms of [0, 2300, 3000, 8300, 11800, 13000, 14999]) assert.equal(captionAgeText(ms), "", String(ms));
  assert.equal(captionAgeText(15000), "约 15 秒前");
  assert.equal(captionAgeText(15499), "约 15 秒前");
  assert.equal(captionAgeText(15500), "约 16 秒前");
  assert.equal(captionAgeText(59000), "约 59 秒前");
  assert.equal(captionAgeText(60000), "约 1 分钟前");
  assert.equal(captionAgeText(125000), "约 2 分钟前");
  assert.equal(captionAgeText(6 * 3600 * 1000), "约 360 分钟前");
});
test("约 N 秒前：负数、离谱的大数、不是数的都不写", () => {
  for (const bad of [-1, -5000, -20000, 6 * 3600 * 1000 + 1, NaN, Infinity, null, undefined, "20000"]) {
    assert.equal(captionAgeText(bad), "", String(bad));
  }
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


test("覆盖率角标：只在偏低时出现，写近几分钟收到了百分之多少", () => {
  const { coverageChip } = require("../web/live-ui.js");
  assert.deepEqual(coverageChip({ coverage_pct: 40, coverage_window_min: 5, coverage_low: false, breaks: 3 }),
                   { text: "", title: "" });
  assert.deepEqual(coverageChip({}), { text: "", title: "" });
  assert.deepEqual(coverageChip(null), { text: "", title: "" });
  assert.deepEqual(coverageChip({ coverage_pct: 38.6, coverage_window_min: 5, coverage_low: true, breaks: 7 }),
                   { text: "近 5 分钟收到 39%", title: "近 5 分钟里实际收到的直播音频占比；本场已中断 7 次" });
  // 开场不足 5 分钟：按已过的时间写
  assert.equal(coverageChip({ coverage_pct: 20, coverage_window_min: 3, coverage_low: true, breaks: 1 }).text,
               "近 3 分钟收到 20%");
});
