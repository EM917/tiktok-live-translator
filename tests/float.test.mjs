// 浮窗页（web/float.js）：状态归并、取字、字号，以及在 node:vm 里真跑一遍的页面（中英文各一遍）。
// 英文的措辞和「不剩中文」的检查在 tests/float.en.test.mjs；这个文件的纯函数部分按默认的中文跑。
//
// 钉住的事：
//   0. 待译停留（专家审查 U1，用户 10-22 定的「等译文到了再一起换」，同中控页底部大字幕）：新的一句还在翻译时
//      窗口里停在上一句，有了结局再整句换上；等满 4 秒（FLOAT_HOLD_MS）还没结局就照以前的样子先放原文，
//      改等新句时不重新数 4 秒；
//   1. 最新一条字幕：译文大字 / 原文小字，没译成时原文大字 + 角标，翻译中不画角标（同中控页底部大字幕）；
//   2. 弹幕：最多 5 条、新的在底部，译文行 / 原文行的规则同弹幕栏，红 / 琥珀 / 关注的标记复用 guard.js，
//      而且永远没有「加入关注名单」的按钮（浮窗只显示）；
//   3. 换主播清空，重连回放里比换主播更早的弹幕不要；
//   4. 不显示报警、不发任何消息、字号跟着 config.caption_size、非 macOS 的窗口加 solid；
//   5. 字幕区的高度（专家审查 U1，同中控页底部大字幕）：最少「两行译文 + 两行原文」高，换句时下面的弹幕不跟着跳；
//      长句不截断，到窗口六成再从下面裁（样式表，假 DOM 没有版面；真排版的量法在 b1-livebar 的验证记录里）；
//   6. 结束卡片（专家审查 U3）：ended / error 时字幕和弹幕收起、露出卡片，标题只写状态、说明只指去主窗口；
//      卡片没露着时的 idle（中控点停止，后端照旧关窗）不画卡片；卡片露着时的 idle（结束后才开始的模型下载报进度
//      之类，后端也不关窗）不收卡片；connecting / live 回来才收起。「回到主窗口」只走 JS 桥，没有 JS 桥不画，
//      JS 桥在页面加载之后注入、发 pywebviewready 时重画。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { loadFloat, hello, caption, comment, T } from "./float-page.mjs";

const require = createRequire(import.meta.url);
const F = require("../web/float.js");

const TEXT = {
  zh: {
    waiting: "等待第一句字幕…", connecting: "连接中…", translating: "翻译中…",
    failed: "翻译失败", refused: "未翻译（接口拒答）", notTranslation: "未翻译（结果不像译文）",
    dropped: "翻译已跳过（积压）", callPill: "疑似号召举报或不买", noQuestionMark: "句子很短，没有问号",
    lagRe: /^约 3\d 秒前$/,
    endedTitle: "直播已结束", errorTitle: "监听已停止", endNote: "详情见主窗口。", goMain: "回到主窗口",
  },
  en: {
    waiting: "Waiting for the first caption…", connecting: "Connecting…", translating: "Translating…",
    failed: "Translation failed", refused: "Not translated (API refusal)",
    notTranslation: "Not translated (the result didn’t look like a translation)",
    dropped: "Skipped (backlog)", callPill: "Possible call to report or not buy",
    noQuestionMark: "Short, with no question mark", lagRe: /^About 3\d sec ago$/,
    endedTitle: "Stream ended", errorTitle: "Monitoring stopped", endNote: "See the main window for details.",
    goMain: "Show Main Window",
  },
};

// ---- 纯函数 -------------------------------------------------------------------------------

test("字号：顶栏滑块的值 × 0.62，夹在 16–40px；没设过就按默认的 34 算", () => {
  assert.equal(F.floatSizes(34).big, 21);
  assert.equal(F.floatSizes(null).big, 21);
  assert.equal(F.floatSizes(undefined).big, 21);
  assert.equal(F.floatSizes(20).big, 16);        // 12.4 → 夹到 16
  assert.equal(F.floatSizes(60).big, 37);
  assert.equal(F.floatSizes(50).big, 31);
  assert.equal(F.floatSizes("abc").big, 21);     // 来路不明的值不当成用户选的
  assert.equal(F.floatSizes(999).big, 21);
  for (const size of [20, 34, 60]) {
    const s = F.floatSizes(size);
    assert.ok(s.small < s.big && s.small >= 12, "原文比译文小一档：" + JSON.stringify(s));
    assert.ok(s.cmt >= 13 && s.cmt <= 20 && s.cmtSmall >= 11 && s.cmtSmall <= 16, JSON.stringify(s));
  }
  assert.ok(F.floatSizes(60).big > F.floatSizes(20).big, "滑块往大拉，浮窗字也大");
});

// 以前这条钉的是「译文还在路上时先放原文」：每句先闪一下西语再跳成中文（专家审查 U1）。用户 10-22 定了
// 「等译文到了再一起换」，这里改钉新的样子
test("字幕：译文还在路上时不换句（第一句之前就没有字幕）；译文到了译文大字、原文小字一起换上", () => {
  const s = F.floatInitialState();
  assert.equal(F.floatCaptionView(s), null);
  F.floatReduce(s, caption(1, "hola a todas"));
  assert.equal(F.floatCaptionView(s), null, "第一句：先不放西语");
  F.floatReduce(s, { type: "caption_update", id: 1, translated: "大家好", translate_state: "ok" });
  assert.deepEqual(F.floatCaptionView(s), { id: 1, big: "大家好", small: "hola a todas", chip: "", fail: false });
  F.floatReduce(s, caption(2, "y ahora la crema"));
  assert.deepEqual(F.floatCaptionView(s), { id: 1, big: "大家好", small: "hola a todas", chip: "", fail: false },
                   "新的一句还在翻译：停在上一句");
  F.floatReduce(s, { type: "caption_update", id: 2, translated: "现在是面霜", translate_state: "ok" });
  assert.deepEqual(F.floatCaptionView(s), { id: 2, big: "现在是面霜", small: "y ahora la crema", chip: "", fail: false });
});

test("待译停留：等满 4 秒还没结局（floatReleaseHeld）照以前的样子先放原文；译文后到就地换成译文", () => {
  assert.equal(F.FLOAT_HOLD_MS, 4000, "和中控页 web/app.js 的 LIVE_HOLD_MS 一样");
  const s = F.floatInitialState();
  F.floatReduce(s, caption(1, "esto tarda mucho"));
  assert.equal(F.floatReleaseHeld(s, 99), false, "别的 id：不动");
  assert.equal(F.floatCaptionView(s), null);
  assert.equal(F.floatReleaseHeld(s, 1), true);
  assert.deepEqual(F.floatCaptionView(s), { id: 1, big: "esto tarda mucho", small: "", chip: "", fail: false });
  assert.equal(F.floatReleaseHeld(s, 1), false, "已经放上去了：再到点也不动");
  F.floatReduce(s, { type: "caption_update", id: 1, translated: "这个要很久", translate_state: "ok" });
  assert.deepEqual(F.floatCaptionView(s), { id: 1, big: "这个要很久", small: "esto tarda mucho", chip: "", fail: false });
});

test("待译停留：等待中又来一句改等新的那句，前一句的译文后到不上窗口；进度消息（只带 strong_state）不是结局", () => {
  const s = F.floatInitialState();
  F.floatReduce(s, caption(1, "hola"));
  F.floatReduce(s, { type: "caption_update", id: 1, translated: "你好", translate_state: "ok" });
  F.floatReduce(s, caption(2, "primera parte"));
  F.floatReduce(s, caption(3, "segunda parte"));
  F.floatReduce(s, { type: "caption_update", id: 2, translated: "第一部分", translate_state: "ok" });
  assert.equal(F.floatCaptionView(s).big, "你好");
  assert.equal(F.floatReleaseHeld(s, 2), false, "第 2 句排过的计时器到点：已经不在等它");
  F.floatReduce(s, { type: "caption_update", id: 3, strong_state: "pending" });
  assert.equal(F.floatCaptionView(s).big, "你好");
  F.floatReduce(s, { type: "caption_update", id: 3, translated: "第二部分", translate_state: "ok" });
  assert.deepEqual(F.floatCaptionView(s), { id: 3, big: "第二部分", small: "segunda parte", chip: "", fail: false });
});

test("待译停留：不用翻译的（skipped）、回放来的直接上；换场、清空字幕、hello 把在等的那句忘掉", () => {
  const s = F.floatInitialState();
  F.floatReduce(s, caption(1, "buenas noches", { translate_state: "skipped", target_lang: "es" }));
  assert.equal(F.floatCaptionView(s).big, "buenas noches");
  F.floatReduce(s, caption(2, "en espera"));
  F.floatReduce(s, caption(3, "repetida", { replay: true }));         // 重连回放：断线前窗口里就是它
  assert.equal(F.floatCaptionView(s).big, "repetida");
  assert.equal(F.floatReleaseHeld(s, 2), false, "回放的那句已经取代了在等的那句");
  for (const reset of [{ type: "session_break", ts: T + 9 }, { type: "history_cleared", kind: "captions" },
                       { type: "hello", config: {} }]) {
    F.floatReduce(s, caption(4, "pendiente"));
    F.floatReduce(s, reset);
    assert.equal(F.floatReleaseHeld(s, 4), false, reset.type);
    assert.equal(F.floatCaptionView(s), null, reset.type);
  }
});

test("字幕：每一种没译成的结局都留着原文，角标和中控页同一句", () => {
  const endings = [
    [{ translate_state: "failed" }, "翻译失败", true],
    [{ translate_state: "failed", refused: true }, "未翻译（接口拒答）", true],
    [{ translate_state: "failed", not_translation: true }, "未翻译（结果不像译文）", true],
    [{ translate_state: "dropped" }, "翻译已跳过（积压）", false],
  ];
  for (const [update, chip, fail] of endings) {
    const s = F.floatInitialState();
    F.floatReduce(s, caption(1, "hola"));
    F.floatReduce(s, { type: "caption_update", id: 1, translated: "你好", translate_state: "ok" });
    F.floatReduce(s, caption(2, "no se oye bien"));
    assert.equal(F.floatCaptionView(s).big, "你好", "还在翻译：停在上一句");
    // 没译成也是结局：一到就整句换上，不等 4 秒
    F.floatReduce(s, Object.assign({ type: "caption_update", id: 2, translated: null }, update));
    assert.deepEqual(F.floatCaptionView(s), { id: 2, big: "no se oye bien", small: "", chip, fail }, JSON.stringify(update));
  }
});

test("字幕：更新只覆盖消息里带的键；别的 id 的更新不碰当前这条", () => {
  const s = F.floatInitialState();
  F.floatReduce(s, caption(1, "hola"));
  F.floatReduce(s, { type: "caption_update", id: 1, translated: "你好", translate_state: "ok" });
  // 重译的「进行中」通知不带 translated：不能把已有的译文抹掉
  F.floatReduce(s, { type: "caption_update", id: 1, strong_state: "pending" });
  assert.equal(F.floatCaptionView(s).big, "你好");
  F.floatReduce(s, { type: "caption_update", id: 99, translated: "别人的", translate_state: "ok" });
  assert.equal(F.floatCaptionView(s).big, "你好");
  // 重译成了：新的译文顶上去，旧的拒答标记跟着消息里的 refused 走
  F.floatReduce(s, { type: "caption_update", id: 1, translated: "您好", translate_state: "ok", refused: false });
  assert.deepEqual(F.floatCaptionView(s), { id: 1, big: "您好", small: "hola", chip: "", fail: false });
});

test("字幕：新的一条换掉旧的；回放里连着来几条，最后一条留下", () => {
  const s = F.floatInitialState();
  for (const i of [1, 2, 3]) F.floatReduce(s, caption(i, "uno " + i, { replay: true }));
  assert.equal(F.floatCaptionView(s).id, 3);
});

test("弹幕：最多 5 条，新的在后面；同一个 id 再来一次是原地换", () => {
  const s = F.floatInitialState();
  for (let i = 1; i <= 8; i++) F.floatReduce(s, comment("c" + i, "u" + i, "hola " + i));
  assert.deepEqual(s.comments.map((c) => c.id), ["c4", "c5", "c6", "c7", "c8"]);
  assert.equal(F.FLOAT_COMMENTS_MAX, 5);
  F.floatReduce(s, comment("c6", "u6", "hola otra vez"));
  assert.deepEqual(s.comments.map((c) => c.id), ["c4", "c5", "c6", "c7", "c8"]);
  assert.equal(s.comments[2].text, "hola otra vez");
});

test("弹幕：译文后补、守卫标注后补，都只改那一条；认不出的 id 不抛", () => {
  const s = F.floatInitialState();
  F.floatReduce(s, comment("c1", "ana", "¿cuánto cuesta?"));
  F.floatReduce(s, { type: "comment_update", id: "c1", translated: "多少钱？", state: "ok" });
  F.floatReduce(s, { type: "comment_update", id: "nope", translated: "x", state: "ok" });
  F.floatReduce(s, { type: "comment_guard", id: "c1", guard: { lvl: "alert", cats: ["accuse_call"] } });
  F.floatReduce(s, { type: "comment_guard", id: "nope", guard: { lvl: "alert" } });
  assert.equal(s.comments[0].translated, "多少钱？");
  assert.equal(s.comments[0].guard.lvl, "alert");
  F.floatReduce(s, { type: "comment_guard", id: "c1", guard: null });
  assert.equal(s.comments[0].guard, null);
});

test("弹幕取字：翻译中占位、译好了带原文、同语言 / 没翻的只有一行、译不出来显示原文本身", () => {
  const view = (extra) => F.floatCommentView(Object.assign({ id: "c", user: "ana", text: "hola", translated: null, state: "" }, extra));
  assert.deepEqual(view({ state: "pending" }), { id: "c", user: "ana", zh: "翻译中…", orig: "hola", showOrig: true, pending: true, failed: false });
  assert.deepEqual(view({ state: "ok", translated: "你好" }), { id: "c", user: "ana", zh: "你好", orig: "hola", showOrig: true, pending: false, failed: false });
  for (const state of ["same", "skipped"]) {
    assert.deepEqual(view({ state, translated: "hola" }), { id: "c", user: "ana", zh: "hola", orig: "hola", showOrig: false, pending: false, failed: false }, state);
  }
  for (const state of ["failed", "dropped"]) {
    assert.deepEqual(view({ state }), { id: "c", user: "ana", zh: "hola", orig: "hola", showOrig: false, pending: false, failed: true }, state);
  }
});

test("换主播：字幕和弹幕清空，之后重连回放里比这一刻更早的弹幕不要", () => {
  const s = F.floatInitialState();
  F.floatReduce(s, caption(1, "viejo"));
  F.floatReduce(s, comment("old", "ana", "del otro", { ts: T }));
  F.floatReduce(s, { type: "session_break", ts: T + 100, streamer: "otra" });
  assert.equal(F.floatCaptionView(s), null);
  assert.deepEqual(s.comments, []);
  F.floatReduce(s, comment("old2", "ana", "replay viejo", { ts: T + 50, replay: true }));
  F.floatReduce(s, comment("new", "ana", "nuevo", { ts: T + 101 }));
  F.floatReduce(s, comment("nots", "ana", "sin hora", { ts: undefined }));
  assert.deepEqual(s.comments.map((c) => c.id), ["new", "nots"]);
});

test("重连回放：服务端把最近一次换场放在最前面，页面只留换场之后的弹幕和字幕", () => {
  // 形状同 CaptionServer.replay_messages("float")：[session_break, 最后 3 条字幕, 最后 10 条弹幕里不早于换场的]
  const s = F.floatInitialState();
  F.floatReduce(s, { type: "hello", config: {} });
  F.floatReduce(s, { type: "session_break", ts: T + 100, streamer: "otra", replay: true });
  for (const [i, text] of ["uno", "dos", "tres"].entries()) F.floatReduce(s, caption(i + 1, text, { replay: true }));
  F.floatReduce(s, comment("n0", "ana", "nuevo", { ts: T + 150, replay: true }));
  assert.equal(F.floatCaptionView(s).big, "tres");
  assert.deepEqual(s.comments.map((c) => c.id), ["n0"]);
  assert.equal(s.breakTs, T + 100);
  // 万一服务端漏过滤，页面自己也挡得住比换场老的
  F.floatReduce(s, comment("o0", "ana", "viejo", { ts: T + 99, replay: true }));
  assert.deepEqual(s.comments.map((c) => c.id), ["n0"]);
});

test("清空字幕 / 清空弹幕各清各的", () => {
  const s = F.floatInitialState();
  F.floatReduce(s, caption(1, "hola"));
  F.floatReduce(s, { type: "caption_update", id: 1, translated: "你好", translate_state: "ok" });
  F.floatReduce(s, comment("c1", "ana", "hola"));
  F.floatReduce(s, { type: "history_cleared", kind: "comments" });
  assert.equal(s.comments.length, 0);
  assert.ok(F.floatCaptionView(s));
  F.floatReduce(s, { type: "history_cleared", kind: "captions" });
  assert.equal(F.floatCaptionView(s), null);
  F.floatReduce(s, { type: "history_cleared", kind: "other" });         // 认不出的 kind：不动
});

test("hello 整份换掉本地状态；config 只改它带的键", () => {
  const s = F.floatInitialState();
  F.floatReduce(s, caption(1, "hola"));
  F.floatReduce(s, comment("c1", "ana", "hola"));
  F.floatReduce(s, { type: "hello", config: { caption_size: 50, status: { state: "connecting" },
                                               guard_state: { enabled: true, weak: true },
                                               guard_marked: { u1: { keys: ["u1", "h:ana"], nick: "ana" } } } });
  assert.equal(F.floatCaptionView(s), null);
  assert.deepEqual(s.comments, []);
  assert.equal(s.captionSize, 50);
  assert.equal(s.status, "connecting");
  assert.equal(s.guardState.weak, true);
  assert.equal(s.markIdx["h:ana"], "u1");
  F.floatReduce(s, { type: "config", caption_size: 24 });
  assert.equal(s.captionSize, 24);
  assert.equal(s.status, "connecting");                                  // 没带的键不动
  F.floatReduce(s, { type: "config", caption_size: 999 });
  assert.equal(s.captionSize, null);                                     // 越界当没有，不夹
  F.floatReduce(s, { type: "status", state: "live" });
  assert.equal(s.status, "live");
});

test("关注名单的增删即时生效", () => {
  const s = F.floatInitialState();
  F.floatReduce(s, { type: "guard_mark", key: "u9", entry: { keys: ["u9", "h:pepe"], nick: "pepe" } });
  assert.equal(s.markIdx["h:pepe"], "u9");
  F.floatReduce(s, { type: "guard_mark", key: "u9", entry: null });
  assert.equal(Object.keys(s.marked).length, 0);
  F.floatReduce(s, { type: "guard_mark", key: "", entry: {} });          // 没有键：忽略
});

test("过时信号：这一句的声音结束超过 15 秒才写「约 N 秒前」，连接中写连接中（同中控页底部大字幕）", () => {
  const s = F.floatInitialState();
  assert.deepEqual(F.floatCaptionMeta(s, 0), { connecting: "", lag: "" });
  const shown = 1760000000;                                  // 上屏时刻（秒）；e2e 2.3 秒 → 声音结束于 shown*1000 - 2300
  F.floatReduce(s, caption(1, "hola", { ts: shown, e2e_ms: 2300, replay: true }), 0);   // 回放来的也按 ts 算，不看收到的时刻
  const end = shown * 1000 - 2300;
  assert.equal(s.caption.audioEndAt, end);
  assert.deepEqual(F.floatCaptionMeta(s, end + 14000), { connecting: "", lag: "" });    // 健康直播里的换句间隔：不写
  assert.equal(F.floatCaptionMeta(s, end + 20000).lag, "约 20 秒前");
  assert.equal(F.floatCaptionMeta(s, end + 150000).lag, "约 3 分钟前");
  F.floatReduce(s, { type: "caption_update", id: 1, translated: "你好", translate_state: "ok" });
  assert.equal(s.caption.audioEndAt, end, "补译文不改这一句的时刻");
  F.floatReduce(s, { type: "status", state: "connecting" });
  assert.deepEqual(F.floatCaptionMeta(s, end + 1000), { connecting: "连接中…", lag: "" });
  F.floatReduce(s, { type: "status", state: "live" });
  assert.equal(F.floatCaptionMeta(s, end + 1000).connecting, "");
  // 不知道 e2e（演示模式、老版本服务端）：不写
  F.floatReduce(s, caption(2, "sin e2e", { e2e_ms: undefined }), end);
  F.floatReleaseHeld(s, 2);                                   // 等满 4 秒没有译文：先放原文
  assert.equal(F.floatCaptionMeta(s, end + 600000).lag, "");
});

test("结束卡片：只有 ended / error 才有；标题只写状态，说明只指去主窗口", () => {
  const s = F.floatInitialState();
  assert.deepEqual(F.FLOAT_END_STATES, ["ended", "error"]);
  for (const st of ["", "idle", "connecting", "live", "offline", "something_new"]) {
    F.floatReduce(s, { type: "status", state: st });
    assert.equal(F.floatEndView(s), null, st);
  }
  F.floatReduce(s, { type: "status", state: "ended", detail: "上一场字幕在下方。" });
  assert.deepEqual(F.floatEndView(s), { state: "ended", title: "直播已结束", note: "详情见主窗口。" });
  F.floatReduce(s, { type: "status", state: "error", detail: "内部错误，已停止：boom" });
  assert.deepEqual(F.floatEndView(s), { state: "error", title: "监听已停止", note: "详情见主窗口。" });
  // detail 不转述（主窗口的横幅里才有）：卡片上的字不随它变
  F.floatReduce(s, { type: "status", state: "error", detail: "别的原文" });
  assert.equal(F.floatEndView(s).note, "详情见主窗口。");
  // 重连的 hello 带着结束状态：卡片照样有
  F.floatReduce(s, { type: "hello", config: { status: { state: "ended", detail: "" } } });
  assert.equal(F.floatEndView(s).title, "直播已结束");
  assert.equal(F.floatEndView(null), null);
});

test("结束卡片露着时来的 idle 不收卡片（floatNextStatus）：那不是点停止；没露着时 idle 照旧是 idle", () => {
  for (const end of ["ended", "error"]) {
    assert.equal(F.floatNextStatus(end, "idle"), end);
    assert.equal(F.floatNextStatus(end, "connecting"), "connecting");     // 下一场：收起
    assert.equal(F.floatNextStatus(end, "live"), "live");
    assert.equal(F.floatNextStatus(end, "something_new"), "something_new");
  }
  assert.equal(F.floatNextStatus("error", "ended"), "ended");
  for (const prev of ["", "idle", "connecting", "live"]) assert.equal(F.floatNextStatus(prev, "idle"), "idle", prev);
  assert.equal(F.floatNextStatus("ended", undefined), "");
  const s = F.floatInitialState();
  F.floatReduce(s, { type: "hello", config: { status: { state: "idle", detail: "" } } });
  assert.equal(F.floatEndView(s), null, "刚打开的页面：hello 里的 idle 不画卡片");
  F.floatReduce(s, { type: "status", state: "live" });
  F.floatReduce(s, { type: "status", state: "ended" });
  F.floatReduce(s, { type: "status", state: "idle", detail: "正在下载本地翻译模型：40%…" });
  assert.equal(F.floatEndView(s).title, "直播已结束");
  F.floatReduce(s, { type: "hello", config: { status: { state: "idle", detail: "" } } });    // 断线重连回来
  assert.equal(F.floatEndView(s).title, "直播已结束");
  F.floatReduce(s, { type: "hello", config: { status: { state: "connecting", detail: "" } } });
  assert.equal(F.floatEndView(s), null);
});

test("「回到主窗口」只调 JS 桥：有 pywebview.api.focus_main 才算有，调到了返回 true", () => {
  const calls = [];
  const api = { focus_main() { calls.push(this); return Promise.resolve(true); } };
  const win = { pywebview: { api } };
  assert.equal(F.floatMainBridge(win), api.focus_main);
  assert.equal(F.floatGoMain(win), true);
  assert.deepEqual(calls, [api], "this 是 api 本身（pywebview 的函数挂在它上面）");
  for (const bad of [null, undefined, {}, { pywebview: {} }, { pywebview: { api: {} } },
                     { pywebview: { api: { focus_main: "x" } } }]) {
    assert.equal(F.floatMainBridge(bad), null);
    assert.equal(F.floatGoMain(bad), false);
  }
  // 桥那边抛了、或者 Promise 被拒：页面不抛
  assert.equal(F.floatGoMain({ pywebview: { api: { focus_main() { throw new Error("gone"); } } } }), false);
  const rejected = Promise.reject(new Error("gone"));
  assert.equal(F.floatGoMain({ pywebview: { api: { focus_main: () => rejected } } }), true);
});

test("结束卡片的按钮：有结束状态且有 JS 桥才露出来", () => {
  const el = () => ({ textContent: "", cls: new Set(["hidden"]),
                      get classList() { const c = this.cls; return { toggle: (n, on) => (on ? c.add(n) : c.delete(n)), contains: (n) => c.has(n) }; } });
  const els = { end: el(), endTitle: el(), endNote: el(), endBack: el() };
  const s = F.floatInitialState();
  F.floatReduce(s, { type: "status", state: "ended" });
  F.floatPaintEnd(els, F.floatEndView(s), false);
  assert.ok(!els.end.classList.contains("hidden") && els.endBack.classList.contains("hidden"));
  assert.equal(els.endTitle.textContent, "直播已结束");
  F.floatPaintEnd(els, F.floatEndView(s), true);
  assert.ok(!els.endBack.classList.contains("hidden"));
  assert.equal(els.endBack.textContent, "回到主窗口");
  F.floatPaintEnd(els, null, true);
  assert.ok(els.end.classList.contains("hidden") && els.endBack.classList.contains("hidden"));
  assert.equal(els.endTitle.textContent, "");
  F.floatPaintEnd({}, F.floatEndView(s), true);                 // 没有卡片节点（老页面）：什么都不做
});

test("乱七八糟的输入不抛、不改状态", () => {
  const s = F.floatInitialState();
  for (const bad of [null, undefined, 3, "x", [], {}, { type: "alert", alert_id: 1 }, { type: "stats" }]) {
    F.floatReduce(s, bad);
  }
  assert.equal(F.floatCaptionView(s), null);
  assert.deepEqual(s.comments, []);
});

// ---- 页面（中英文各跑一遍） ----------------------------------------------------------------

for (const lang of ["zh", "en"]) {
  const X = TEXT[lang];
  const L = (name) => name + "（" + lang + "）";
  const hidden = (page, id) => page.el(id).classList.contains("hidden");

  test(L("页面连的是 /ws?role=float，而且一条消息都不发"), () => {
    const page = loadFloat({ lang });
    assert.deepEqual(page.urls(), ["ws://127.0.0.1:0/ws?role=float"]);
    hello(page);
    page.push(caption(1, "hola"));
    page.push(comment("c1", "ana", "hola"));
    // FakeWebSocket.send 一调就抛；走到这里没抛，说明没有发过
  });

  test(L("监听自己停了（ended / error）：字幕和弹幕收起，露出结束卡片；没有 JS 桥就不画按钮"), () => {
    for (const [state, title] of [["ended", X.endedTitle], ["error", X.errorTitle]]) {
      const page = loadFloat({ lang });
      hello(page);
      assert.ok(hidden(page, "f-end"), "直播中没有卡片");
      page.push(caption(1, "hola a todas"));
      page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok" });
      page.push(comment("c1", "ana", "hola"));
      page.push({ type: "status", state, detail: "whatever the main window says" });
      assert.ok(!hidden(page, "f-end"), state);
      assert.equal(page.el("f-end-title").textContent, title);
      assert.equal(page.el("f-end-note").textContent, X.endNote);
      assert.ok(hidden(page, "f-caption") && hidden(page, "f-empty") && hidden(page, "f-comments"), state);
      assert.ok(hidden(page, "f-end-back"), "假页面里没有 pywebview：按钮不画");
      assert.equal(page.el("f-end-back").textContent, X.goMain);
      page.fireWindow("pywebviewready");                              // JS 桥注入完：重画一次，还是没有桥就还不画
      assert.ok(hidden(page, "f-end-back"));
      page.el("f-end-back").click();                                  // 点了也只走 JS 桥：不发 WebSocket（发了会抛）
      assert.equal(page.el("f-end").getAttribute("role"), "status");  // 读屏会念出来
    }
  });

  test(L("JS 桥在页面加载之后才注入：pywebviewready 一来就重画，结束卡片上的按钮露出来，点了只调 focus_main"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push({ type: "status", state: "ended", detail: "" });
    assert.ok(!hidden(page, "f-end") && hidden(page, "f-end-back"), "还没有 JS 桥：卡片在、按钮不画");
    // 假 window 就是页面脚本的全局对象。从页面自己的函数（按钮的 click 处理函数）拿到页面那边的 Function，
    // 取到这个全局对象，在加载之后挂上 pywebview.api（真 pywebview 也是页面加载完才注入）
    const pageWindow = new (page.el("f-end-back").listeners.click[0].constructor)("return this")();
    const calls = [];
    pageWindow.pywebview = { api: { focus_main() { calls.push(1); return Promise.resolve(true); } } };
    assert.ok(hidden(page, "f-end-back"), "注入了、还没发 pywebviewready：页面还没重画");
    page.fireWindow("pywebviewready");
    assert.ok(!hidden(page, "f-end-back"), "pywebviewready 之后重画：按钮露出来");
    assert.equal(page.el("f-end-back").textContent, X.goMain);
    page.el("f-end-back").click();                                  // 只走 JS 桥：不发 WebSocket（发了会抛）
    assert.equal(calls.length, 1);
  });

  test(L("结束卡片露着时来的 idle（结束后才开始的模型下载报进度之类）：卡片留着，重连回来的 hello 带着 idle 也留着；下一场才收起"), () => {
    for (const [state, title] of [["ended", X.endedTitle], ["error", X.errorTitle]]) {
      const page = loadFloat({ lang });
      hello(page);
      page.push(caption(1, "hola"));
      page.push({ type: "caption_update", id: 1, translated: "你好", translate_state: "ok" });
      page.push(comment("c1", "ana", "hola"));
      page.push({ type: "status", state, detail: "" });
      for (const pct of [5, 10, 15]) page.push({ type: "status", state: "idle", detail: pct + "%" });
      assert.ok(!hidden(page, "f-end"), state);
      assert.equal(page.el("f-end-title").textContent, title);
      assert.ok(hidden(page, "f-caption") && hidden(page, "f-empty") && hidden(page, "f-comments"), state);
      page.closeSocket();                                           // 断线：卡片留着
      page.runTimers();
      page.reopen();
      hello(page, { status: { state: "idle", detail: "20%" } });
      assert.ok(!hidden(page, "f-end"), state + "：重连回来");
      assert.equal(page.el("f-end-title").textContent, title);
      page.push({ type: "status", state: "connecting", detail: "" }); // 中控点了开始：下一场
      assert.ok(hidden(page, "f-end"), state);
      page.push({ type: "status", state: "idle", detail: lang === "en" ? "Stopping…" : "正在停止…" });
      assert.ok(hidden(page, "f-end"), state + "：这一场里点停止，不画卡片");
    }
  });

  test(L("中控点停止：两条 idle 都不画结束卡片（后端照旧关窗）"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(caption(1, "hola"));
    page.push({ type: "caption_update", id: 1, translated: "你好", translate_state: "ok" });
    page.push({ type: "status", state: "idle", detail: lang === "en" ? "Stopping…" : "正在停止…" });
    assert.ok(hidden(page, "f-end"));
    page.push({ type: "status", state: "idle", detail: "" });
    assert.ok(hidden(page, "f-end"));
    assert.ok(!hidden(page, "f-caption"), "窗口马上就关了，字幕原样留到最后");
  });

  test(L("结束卡片之后又开始监听（connecting / live）：卡片收起，回到字幕和弹幕"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(comment("c1", "ana", "hola"));
    page.push({ type: "status", state: "ended", detail: "" });
    assert.ok(!hidden(page, "f-end"));
    page.push({ type: "status", state: "connecting", detail: "" });   // 中控在主窗口里点了开始
    assert.ok(hidden(page, "f-end") && !hidden(page, "f-comments") && !hidden(page, "f-empty"));
    page.push({ type: "session_break", ts: T + 99 });
    page.push({ type: "status", state: "live", detail: "" });
    assert.ok(hidden(page, "f-end"));
    assert.equal(page.el("f-empty").textContent, X.waiting);
  });

  test(L("结束卡片露着时和程序断了线：卡片留着，不改成连接中；重连的 hello 带着结束状态照样画卡片"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push({ type: "status", state: "error", detail: "" });
    page.closeSocket();
    assert.ok(!hidden(page, "f-end"));
    assert.equal(page.el("f-end-title").textContent, X.errorTitle);
    page.runTimers();                                                 // 重连
    page.reopen();
    hello(page, { status: { state: "ended", detail: "" } });
    assert.ok(!hidden(page, "f-end"));
    assert.equal(page.el("f-end-title").textContent, X.endedTitle);
    hello(page);                                                      // 程序重启后已经在下一场
    assert.ok(hidden(page, "f-end"));
  });

  test(L("刚打开：没有字幕，写一句占位；连接中写连接中"), () => {
    const page = loadFloat({ lang });
    assert.equal(page.el("f-empty").textContent, X.waiting);
    assert.ok(hidden(page, "f-caption") && !hidden(page, "f-empty"));
    hello(page, { status: { state: "connecting", detail: "" } });
    assert.equal(page.el("f-empty").textContent, X.connecting);
    page.push(caption(1, "hola a todas"));
    assert.ok(hidden(page, "f-caption") && !hidden(page, "f-empty"), "第一句还在翻译：占位留着");
    page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok" });
    assert.ok(!hidden(page, "f-caption") && hidden(page, "f-empty"));
  });

  // 以前这条钉的是「译文到了才有小字、之前大字是西语」（每句先闪西语）；改成待译停留之后的样子
  test(L("字幕：译文大字 + 原文小字一起换上；还在翻译的下一句不换"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(caption(1, "hola a todas"));
    assert.ok(hidden(page, "f-caption"), "第一句还在翻译：窗口里不放西语");
    page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok" });
    assert.equal(page.el("f-big").textContent, "大家好");
    assert.equal(page.el("f-small").textContent, "hola a todas");
    assert.ok(!hidden(page, "f-small") && hidden(page, "f-chip"));
    page.push(caption(2, "y ahora la crema"));
    assert.deepEqual([page.el("f-big").textContent, page.el("f-small").textContent], ["大家好", "hola a todas"]);
  });

  // 本批第 1 版每有一句新的在等就重排一个 4 秒计时器：句子间隔一直短于 4 秒、译文又都慢于间隔时（识别积压后
  // 一串句子集中到达），窗口会一直停在旧句。审查意见后改成一轮等待只排一个，改等新句不往后推（同中控页）
  test(L("待译停留：一轮等待只排一个 4 秒计时器，改等新句不重排；到点放还在等的那一句的原文；这一轮结束后旧计时器到点不动窗口"), () => {
    const page = loadFloat({ lang });
    hello(page);
    const holds = () => page.timers.filter((t) => t.ms === F.FLOAT_HOLD_MS);
    page.push(caption(1, "primera parte"));
    page.push(caption(2, "segunda parte"));
    page.push(comment("c1", "ana", "hola"));                        // 不相干的消息不打断这一轮
    assert.equal(holds().length, 1, "第 2 句顶掉第 1 句：截止时刻还是第 1 句开始等时排的那个");
    holds()[0].fn();                                               // 到点：放还在等的第 2 句
    assert.ok(!hidden(page, "f-caption"));
    assert.deepEqual([page.el("f-big").textContent, hidden(page, "f-small")], ["segunda parte", true]);
    page.push({ type: "caption_update", id: 2, translated: "第二部分", translate_state: "ok" });
    assert.deepEqual([page.el("f-big").textContent, page.el("f-small").textContent], ["第二部分", "segunda parte"]);
    // 新的一轮重新排；这一轮译文先到（结束了），它的计时器到点什么都不做
    page.push(caption(3, "tercera"));
    assert.equal(holds().length, 2);
    page.push({ type: "caption_update", id: 3, translated: "第三", translate_state: "ok" });
    page.push(caption(4, "cuarta"));
    assert.equal(holds().length, 3, "第 3 句那一轮已经结束：第 4 句另起一轮");
    holds()[1].fn();
    assert.equal(page.el("f-big").textContent, "第三", "上一轮的计时器到点：不动窗口");
    holds()[2].fn();
    assert.deepEqual([page.el("f-big").textContent, hidden(page, "f-small")], ["cuarta", true]);
    // 换场把这一轮清掉：它的计时器到点也不把字幕放出来
    page.push(caption(5, "quinta"));
    page.push({ type: "session_break", ts: T + 9 });
    holds()[3].fn();
    assert.ok(hidden(page, "f-caption"));
  });

  for (const [name, update, chip, fail] of [
    ["failed", { translate_state: "failed" }, X.failed, true],
    ["refused", { translate_state: "failed", refused: true }, X.refused, true],
    ["not_translation", { translate_state: "failed", not_translation: true }, X.notTranslation, true],
    ["dropped", { translate_state: "dropped" }, X.dropped, false],
  ]) {
    test(L("字幕没译成（" + name + "）：原文大字，角标是中控页同一句话"), () => {
      const page = loadFloat({ lang });
      hello(page);
      page.push(caption(1, "no se oye bien"));
      page.push(Object.assign({ type: "caption_update", id: 1, translated: null }, update));
      assert.equal(page.el("f-big").textContent, "no se oye bien");
      assert.ok(hidden(page, "f-small"));
      assert.equal(page.el("f-chip").textContent, chip);
      assert.ok(!hidden(page, "f-chip"));
      assert.equal(page.el("f-chip").classList.contains("fail"), fail);
    });
  }

  test(L("状态行：只有角标时才露出来；译好了就收起，不留一条空行"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(caption(1, "no se oye bien"));
    assert.ok(hidden(page, "f-meta"));                               // 翻译中：没有角标
    page.push({ type: "caption_update", id: 1, translated: null, translate_state: "failed", refused: true });
    assert.ok(!hidden(page, "f-meta") && !hidden(page, "f-chip"));
    page.push(caption(2, "hola"));
    page.push({ type: "caption_update", id: 2, translated: "你好", translate_state: "ok" });
    assert.ok(hidden(page, "f-meta"));
  });

  test(L("已经有字幕时：连接中、和程序断线都在状态行写连接中；旧了写约 N 秒前，每秒重算"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(caption(1, "hola a todas", { ts: Date.now() / 1000 - 30, e2e_ms: 2000 }));
    page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok" });
    assert.ok(!hidden(page, "f-meta") && !hidden(page, "f-lag") && hidden(page, "f-conn"));
    assert.match(page.el("f-lag").textContent, X.lagRe);
    page.el("f-lag").textContent = "";
    assert.equal(page.intervals.length, 1);
    assert.equal(page.intervals[0].ms, 1000);
    page.tick();                                                   // 每秒重画：数字会涨
    assert.match(page.el("f-lag").textContent, X.lagRe);
    page.push({ type: "status", state: "connecting", detail: "" });       // 断流重连 / 等主播开播
    assert.ok(!hidden(page, "f-conn"));
    assert.equal(page.el("f-conn").textContent, X.connecting);
    assert.equal(page.el("f-big").textContent, "大家好");                // 字幕留着，只是标明了
    page.push({ type: "status", state: "live", detail: "" });
    assert.ok(hidden(page, "f-conn"));
    page.closeSocket();                                            // 浮窗页自己和程序断了线
    assert.ok(!hidden(page, "f-conn") && !hidden(page, "f-meta"));
  });

  test(L("新鲜的字幕没有过时信号，状态行不露出来"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(caption(1, "hola", { ts: Date.now() / 1000, e2e_ms: 2000 }));
    page.push({ type: "caption_update", id: 1, translated: "你好", translate_state: "ok" });
    assert.ok(!hidden(page, "f-caption"));
    assert.ok(hidden(page, "f-meta") && hidden(page, "f-lag") && hidden(page, "f-conn"));
  });

  test(L("字幕装不下时加 clipped（底部渐隐）；窗口拉大装得下了就去掉"), () => {
    const page = loadFloat({ lang });
    hello(page);
    const cap = page.el("f-caption");
    cap.scrollHeight = 420; cap.clientHeight = 190;               // 字号调大后的一段长西语（没译成，原文大字）
    page.push(caption(1, "una frase muy larga ".repeat(12)));
    page.push({ type: "caption_update", id: 1, translated: null, translate_state: "failed" });
    assert.ok(cap.classList.contains("clipped"));
    cap.clientHeight = 430;                                        // 用户把窗口拉高了
    page.fireWindow("resize");
    assert.ok(!cap.classList.contains("clipped"));
    cap.scrollHeight = 100; cap.clientHeight = 100;                // 刚好装下（差 1px 以内）不算裁
    page.push(caption(2, "hola"));
    page.push({ type: "caption_update", id: 2, translated: "你好", translate_state: "ok" });
    assert.ok(!cap.classList.contains("clipped"));
  });

  test(L("弹幕：昵称 + 译文 + 原文，新的在底部，最多 5 条"), () => {
    const page = loadFloat({ lang });
    hello(page);
    for (let i = 1; i <= 7; i++) page.push(comment("c" + i, "user" + i, "hola " + i));
    page.push({ type: "comment_update", id: "c7", translated: "你好 7", state: "ok" });
    const items = page.comments();
    assert.equal(items.length, 5);
    assert.deepEqual(items.map((el) => el.dataset.cmtId), ["c3", "c4", "c5", "c6", "c7"]);
    const last = items[4];
    assert.equal(last.querySelector(".f-user").textContent, "user7");
    assert.equal(last.querySelector(".f-user").getAttribute("translate"), "no");        // 昵称是名字
    assert.equal(last.querySelector(".cmt-zh").textContent, "你好 7");
    assert.equal(last.querySelector(".cmt-orig").textContent, "hola 7");
    assert.ok(last.querySelector(".cmt-zh").classList.contains("i18n-data"));            // 正文，不是界面文字
    assert.ok(items[0].querySelector(".cmt-zh").classList.contains("pending"));
    assert.equal(items[0].querySelector(".cmt-zh").textContent, X.translating);
  });

  test(L("弹幕里的红 / 琥珀 / 关注标记复用 guard.js，而且永远没有加入名单的按钮"), () => {
    const page = loadFloat({ lang });
    hello(page, { guard_marked: { u3: { keys: ["u3"], nick: "pedro", by: "manual" } } });
    page.push(comment("g1", "rival", "no compren, es una estafa", { uid: "7234567890123456789", uid_ok: true,
      guard: { lvl: "alert", cats: ["accuse_call"], src: "rule", why: ["accuse/call:no compren"] } }));
    page.push(comment("g2", "lupita", "es falso", { uid: "7100000000000000013", uid_ok: true,
      guard: { lvl: "amber", amb: "maybe_question", cats: ["accuse_claim"], src: "rule" } }));
    page.push(comment("g3", "pedro", "hola de nuevo", { uid: "u3", uid_ok: true }));
    page.push(comment("g4", "sofia", "buenas"));
    const [red, amber, marked, plain] = page.comments();
    assert.ok(red.classList.contains("guard-alert"));
    assert.equal(red.querySelector(".guard-pill").textContent, X.callPill);
    assert.ok(amber.classList.contains("guard-amber"));
    assert.equal(amber.querySelector(".guard-note").textContent, X.noQuestionMark);
    assert.ok(marked.classList.contains("guard-marked"));
    assert.equal(marked.querySelector(".guard-star").textContent, "★");
    assert.ok(!["guard-alert", "guard-amber", "guard-marked", "guard-weak"].some((c) => plain.classList.contains(c)));
    assert.equal(page.comments().flatMap((el) => el.descendants()).filter((n) => n.cls.includes("guard-mark-btn")).length, 0);
    // 弱信号只在设置里开了才有标记
    page.push(comment("g5", "ines", "ok", { guard: { lvl: "note", cats: ["insult"] } }));
    assert.ok(!page.comments()[4].classList.contains("guard-weak"));
    hello(page, { guard_state: { enabled: true, weak: true } });
    page.push(comment("g5", "ines", "ok", { guard: { lvl: "note", cats: ["insult"] } }));
    assert.ok(page.comments()[0].classList.contains("guard-weak"));
  });

  test(L("译不出来的弹幕显示原文本身，不写「失败」字样"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(comment("c1", "ana", "se me rompió"));
    page.push({ type: "comment_update", id: "c1", translated: null, state: "failed" });
    const item = page.comments()[0];
    assert.equal(item.querySelector(".cmt-zh").textContent, "se me rompió");
    assert.ok(item.querySelector(".cmt-zh").classList.contains("failed"));
    assert.ok(item.querySelector(".cmt-orig").classList.contains("hidden"));
  });

  test(L("换主播：字幕和弹幕清空，回到占位"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(caption(1, "hola"));
    page.push(comment("c1", "ana", "hola"));
    page.push({ type: "session_break", ts: T + 500, streamer: "otra" });
    assert.ok(hidden(page, "f-caption") && !hidden(page, "f-empty"));
    assert.equal(page.el("f-empty").textContent, X.waiting);
    assert.equal(page.comments().length, 0);
  });

  test(L("字号跟着 config.caption_size，hello 和 config 广播都认"), () => {
    const page = loadFloat({ lang });
    assert.equal(page.cssVars["--f-big"], "21px");                  // 开页就有默认值，不等服务端
    hello(page, { caption_size: 50 });
    assert.equal(page.cssVars["--f-big"], "31px");
    page.push({ type: "config", caption_size: 24 });
    assert.equal(page.cssVars["--f-big"], "16px");
    page.push({ type: "config", caption_size: 60 });
    assert.equal(page.cssVars["--f-big"], "37px");
    for (const name of ["--f-small", "--f-cmt", "--f-cmt-small"]) assert.match(page.cssVars[name], /^\d+px$/);
  });

  test(L("报警等浮窗不该收到的消息，万一来了也不画、不抛"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(caption(1, "hola"));
    page.push({ type: "caption_update", id: 1, translated: "你好", translate_state: "ok" });
    const before = page.el("f-big").textContent;
    assert.equal(before, "你好");
    for (const msg of [{ type: "alert", alert_id: 1, term: "cura", tier: "exact", ts: T },
                       { type: "alert_update", alert_id: 1 }, { type: "incident", id: "i", level: "warn", text: "x" },
                       { type: "notice", text: "hi" }, { type: "stats" }]) page.push(msg);
    assert.equal(page.el("f-big").textContent, before);
    assert.equal(page.comments().length, 0);
  });

  test(L("断线后自己重连，重连的 hello 重新开始"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(caption(1, "hola"));
    page.closeSocket();
    page.runTimers();
    assert.equal(page.sockets.length, 2);
    assert.deepEqual(page.urls(), ["ws://127.0.0.1:0/ws?role=float", "ws://127.0.0.1:0/ws?role=float"]);
    page.reopen();
    hello(page);
    assert.ok(hidden(page, "f-caption"));
  });

  test(L("地址带 ?solid=1（非 macOS）给 <html> 加 solid，不带就不加"), () => {
    assert.ok(!loadFloat({ lang }).root.classList.contains("solid"));
    assert.ok(loadFloat({ lang, search: "?solid=1" }).root.classList.contains("solid"));
    assert.ok(!loadFloat({ lang, search: "?solid=0" }).root.classList.contains("solid"));
  });
}

// ---- 页面文件本身 -------------------------------------------------------------------------

test("float.html 只载入四个脚本，顺序对，每个都带 ?v=；样式表也带", () => {
  const html = readFileSync(new URL("../web/float.html", import.meta.url), "utf8");
  const scripts = [...html.matchAll(/<script src="\/static\/([\w.-]+)\?v=(\d+)"><\/script>/g)].map((m) => m[1]);
  assert.deepEqual(scripts, ["i18n.js", "live-ui.js", "guard.js", "float.js"]);
  assert.match(html, /<link rel="stylesheet" href="\/static\/float\.css\?v=\d+">/);
  assert.ok(!html.includes('name="tlt-app"'), "实例指纹只属于主界面");
  // 以前这里钉的是「浮窗没有任何可点的东西」。结束卡片（专家审查 U3）加了唯一一个按钮「回到主窗口」，
  // 它只走 pywebview 的 JS 桥、不发 WebSocket 消息（下面「float.js 里没有 send」那条照旧钉着）；别的照旧不许有
  const bare = html.replace(/<!--[\s\S]*?-->/g, "");
  const buttons = [...bare.matchAll(/<button\b[^>]*>/g)].map((m) => m[0]);
  assert.equal(buttons.length, 1, "只有结束卡片上的一个按钮");
  assert.match(buttons[0], /id="f-end-back"/);
  assert.match(buttons[0], /type="button"/);
  assert.match(buttons[0], /data-en="Show Main Window"/);
  assert.ok(/<section id="f-end"[\s\S]*?<button id="f-end-back"[\s\S]*?<\/section>/.test(bare), "按钮在结束卡片里");
  assert.ok(!/<input|<select|<textarea|<a\s/.test(bare), "除了那个按钮，浮窗没有别的可点的东西");
});

test("角标在字幕上面：字幕区从下面裁，状态行永远裁不到；裁了有渐隐", () => {
  // 假 DOM 没有版面，G10 的无头 Chrome 只在 CI 上跑：这里静态钉住结构和样式规则。
  // 以前角标排在译文、原文后面，字号调大或一段长西语没译成时，max-height 62% + overflow:hidden
  // 把「未翻译（接口拒答）」和句子结尾一起从底部裁掉，看上去像程序没在翻译
  const html = readFileSync(new URL("../web/float.html", import.meta.url), "utf8").replace(/<!--[\s\S]*?-->/g, "");
  const section = /<section id="f-caption"[\s\S]*?<\/section>/.exec(html)[0];
  const at = (id) => section.indexOf('id="' + id + '"');
  assert.ok(at("f-meta") > 0 && at("f-chip") > at("f-meta"), "角标在状态行里");
  assert.ok(at("f-chip") < at("f-big") && at("f-big") < at("f-small"), "顺序：状态行、译文、原文");
  const css = readFileSync(new URL("../web/float.css", import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const rule = (sel) => {
    const m = new RegExp("(?:^|\\n)" + sel.replace(/\./g, "\\.") + "\\s*\\{([^}]*)\\}").exec(css);
    return m ? m[1] : "";
  };
  assert.match(rule(".f-caption"), /overflow:\s*hidden/);
  assert.match(rule(".f-caption.clipped"), /-webkit-mask-image:\s*linear-gradient/);
  assert.ok(!/margin-top/.test(rule(".f-chip")), "角标在最上面，不要再往下推");
});

test("字幕区最少有「两行译文 + 两行原文」那么高（换句时弹幕不跟着上下跳），窗口太矮时让给 62%；长句不截断", () => {
  const css = readFileSync(new URL("../web/float.css", import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const decls = (sel) => {
    const m = new RegExp("(?:^|\\n)" + sel.replace(/\./g, "\\.") + "\\s*\\{([^}]*)\\}").exec(css);
    assert.ok(m, "float.css 里找不到 " + sel);
    const out = {};
    for (const part of m[1].split(";")) {
      const i = part.indexOf(":");
      if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
    }
    return out;
  };
  const cap = decls(".f-caption"), big = decls(".f-big"), small = decls(".f-small");
  assert.equal(cap.padding, "12px 14px 8px");
  assert.equal(cap["max-height"], "62%");
  assert.equal(big["line-height"], "1.35");
  assert.equal(small["line-height"], "1.4");
  assert.equal(small["margin-top"], "4px");
  const m = /^min\(62%, calc\(var\(--f-big\) \* ([\d.]+) \+ var\(--f-small\) \* ([\d.]+) \+ ([\d.]+)px\)\)$/.exec(cap["min-height"]);
  assert.ok(m, "min-height：" + cap["min-height"]);
  for (const size of [20, 32, 34, 48, 60]) {
    const s = F.floatSizes(size);
    const got = s.big * Number(m[1]) + s.small * Number(m[2]) + Number(m[3]);
    // 两行译文 + 两行原文 + 上下内边距 20 + 原文的 margin-top 4 + 1px 余量
    const want = s.big * 1.35 * 2 + s.small * 1.4 * 2 + 20 + 4 + 1;
    assert.ok(Math.abs(got - want) < 0.01, "字号 " + size + "：" + got + " ≠ " + want);
  }
  for (const sel of [".f-big", ".f-small"]) {
    const body = Object.keys(decls(sel)).join(" ");
    assert.doesNotMatch(body, /line-clamp|max-height|text-overflow/, sel + "：长句不截断（D13）");
  }
});

test("弹幕列表最上面被挤出去的那一行渐隐到分隔线底下，不会被线拦腰切开", () => {
  // 2026-10-16 真窗口截图：弹幕从底部往上排、放不下时最旧那条只露下半截，字被字幕下面那条分隔线横着切开，
  // 看着像页面坏了。假 DOM 没有版面：这里静态钉住样式规则（G10 的截图里能看到效果）
  const css = readFileSync(new URL("../web/float.css", import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const m = /(?:^|\n)\.f-comments\s*\{([^}]*)\}/.exec(css);
  assert.ok(m, "float.css 里找不到 .f-comments");
  const body = m[1];
  const decl = (prop) => {
    const d = new RegExp("(?:^|[;{\\s])" + prop.replace(/-/g, "\\-") + ":\\s*([^;]+);").exec(body);
    return d ? d[1].trim() : "";
  };
  // 新的在底部、放不下从上面裁；分隔线是这个列表自己的上边框
  assert.match(decl("justify-content"), /^flex-end$/);
  assert.match(decl("overflow"), /^hidden$/);
  assert.match(decl("border-top"), /solid/);
  // 渐隐段：列表顶上留出 --f-fade 的内边距，装得下时整行都在它下面、不受影响；遮罩从分隔线下面的透明
  // 渐变到 --f-fade 处的不透明，最上面 1px 不透明留住分隔线
  assert.ok(decl("--f-fade"), "要有 --f-fade");
  assert.match(decl("padding"), /^var\(--f-fade\)\s/, "内边距的上边就是渐隐段的高");
  const gradient = /^linear-gradient\(to bottom,\s*#000 1px,\s*transparent 1px,\s*#000 calc\(var\(--f-fade\) \+ 1px\)\)$/;
  assert.match(decl("-webkit-mask-image"), gradient);
  assert.match(decl("mask-image"), gradient);
});

test("小字的对比度：窗口透明、盖在白色或黑色的软件上，都在 4.5:1 以上", () => {
  // macOS 上窗口本身透明、没有模糊，页面的 rgba 底直接按 alpha 叠在下面的东西上。浮窗正是要盖在
  // 白色的 TikTok 页面、表格上用的，所以最坏的背景是纯白。颜色全从 float.css 里读，改了就重算
  const css = readFileSync(new URL("../web/float.css", import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const token = (name) => new RegExp(name + ":\\s*([^;]+);").exec(css)[1].trim();
  const rule = (sel) => {
    const m = new RegExp("(?:^|\\n)" + sel.replace(/\./g, "\\.") + "\\s*\\{([^}]*)\\}").exec(css);
    assert.ok(m, "float.css 里找不到 " + sel);
    return m[1];
  };
  const decl = (sel, prop) => new RegExp("(?:^|[;{\\s])" + prop + ":\\s*([^;]+)").exec(rule(sel))[1].trim();
  const color = (v) => {
    v = v.startsWith("var(") ? token(/var\((--[\w-]+)\)/.exec(v)[1]) : v;
    let m = /^#([0-9a-f]{6})$/i.exec(v);
    if (m) return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)).concat(1);
    m = /^rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)$/.exec(v);
    assert.ok(m, "认不出的颜色：" + v);
    return [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4])];
  };
  const over = (fg, bg) => [0, 1, 2].map((i) => fg[3] * fg[i] + (1 - fg[3]) * bg[i]).concat(1);
  const lum = (c) => {
    const [r, g, b] = c.slice(0, 3).map((v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => { const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
  const layers = (behind, ...bgs) => bgs.reduce((acc, bg) => over(color(bg), acc), behind);
  const bodyBg = decl("body", "background");
  const pairs = [
    ["正文的暗字（原文、昵称、约 N 秒前）", decl(".f-small", "color"), [bodyBg]],
    ["角标", decl(".f-chip", "color"), [bodyBg, decl(".f-chip", "background")]],
    ["角标·没译成", decl(".f-chip.fail", "color"), [bodyBg, decl(".f-chip.fail", "background")]],
    ["连接中…", decl(".f-conn", "color"), [bodyBg, decl(".f-chip", "background")]],
    ["红色类别胶囊", decl(".guard-pill.alert", "color"), [bodyBg, decl(".f-cmt.guard-alert", "background"), decl(".guard-pill.alert", "background")]],
    ["琥珀胶囊", decl(".guard-pill.amber", "color"), [bodyBg, decl(".f-cmt.guard-amber", "background"), decl(".guard-pill.amber", "background")]],
    ["红行里的说明", decl(".f-cmt.guard-alert .guard-note", "color"), [bodyBg, decl(".f-cmt.guard-alert", "background")]],
    ["琥珀行里的说明", decl(".guard-note", "color"), [bodyBg, decl(".f-cmt.guard-amber", "background")]],
    ["结束卡片的说明", decl(".f-end-note", "color"), [bodyBg]],
    ["「回到主窗口」按钮", decl(".f-end-back", "color"), [bodyBg, decl(".f-end-back", "background")]],
    ["「回到主窗口」按钮·按下", decl(".f-end-back", "color"), [bodyBg, decl(".f-end-back:active", "background")]],
  ];
  for (const [behindName, behind] of [["白底", [255, 255, 255, 1]], ["黑底", [0, 0, 0, 1]]]) {
    for (const [name, fg, bgs] of pairs) {
      const bg = layers(behind, ...bgs);
      const got = ratio(over(color(fg), bg), bg);
      assert.ok(got >= 4.5, behindName + "上的「" + name + "」只有 " + got.toFixed(2) + ":1");
    }
  }
});

test("float.js 里没有 send，也没有任何往服务端发东西的写法", () => {
  const src = readFileSync(new URL("../web/float.js", import.meta.url), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  assert.ok(!/\.send\(|function send\b/.test(src));
  assert.ok(!/localStorage|sessionStorage|fetch\(|XMLHttpRequest/.test(src));
});
