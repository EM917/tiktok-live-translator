// 浮窗页（web/float.js）：状态归并、取字、字号，以及在 node:vm 里真跑一遍的页面（中英文各一遍）。
// 英文的措辞和「不剩中文」的检查在 tests/float.en.test.mjs；这个文件的纯函数部分按默认的中文跑。
//
// 钉住的事：
//   1. 最新一条字幕：译文大字 / 原文小字，没译成时原文大字 + 角标，翻译中不画角标（同中控页底部大字幕）；
//   2. 弹幕：最多 5 条、新的在底部，译文行 / 原文行的规则同弹幕栏，红 / 琥珀 / 关注的标记复用 guard.js，
//      而且永远没有「加入关注名单」的按钮（浮窗只显示）；
//   3. 换主播清空，重连回放里比换主播更早的弹幕不要；
//   4. 不显示报警、不发任何消息、字号跟着 config.caption_size、非 macOS 的窗口加 solid。
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
  },
  en: {
    waiting: "Waiting for the first caption…", connecting: "Connecting…", translating: "Translating…",
    failed: "Translation failed", refused: "Not translated (API refusal)",
    notTranslation: "Not translated (the result didn’t look like a translation)",
    dropped: "Skipped (backlog)", callPill: "Possible call to report or not buy",
    noQuestionMark: "Short, with no question mark",
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

test("字幕：译文到了译文大字、原文小字；译文还在路上只有原文，不画角标", () => {
  const s = F.floatInitialState();
  assert.equal(F.floatCaptionView(s), null);
  F.floatReduce(s, caption(1, "hola a todas"));
  assert.deepEqual(F.floatCaptionView(s), { id: 1, big: "hola a todas", small: "", chip: "", fail: false });
  F.floatReduce(s, { type: "caption_update", id: 1, translated: "大家好", translate_state: "ok" });
  assert.deepEqual(F.floatCaptionView(s), { id: 1, big: "大家好", small: "hola a todas", chip: "", fail: false });
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
    F.floatReduce(s, caption(1, "no se oye bien"));
    F.floatReduce(s, Object.assign({ type: "caption_update", id: 1, translated: null }, update));
    assert.deepEqual(F.floatCaptionView(s), { id: 1, big: "no se oye bien", small: "", chip, fail }, JSON.stringify(update));
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

test("清空字幕 / 清空弹幕各清各的", () => {
  const s = F.floatInitialState();
  F.floatReduce(s, caption(1, "hola"));
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

  test(L("刚打开：没有字幕，写一句占位；连接中写连接中"), () => {
    const page = loadFloat({ lang });
    assert.equal(page.el("f-empty").textContent, X.waiting);
    assert.ok(hidden(page, "f-caption") && !hidden(page, "f-empty"));
    hello(page, { status: { state: "connecting", detail: "" } });
    assert.equal(page.el("f-empty").textContent, X.connecting);
    page.push(caption(1, "hola a todas"));
    assert.ok(!hidden(page, "f-caption") && hidden(page, "f-empty"));
  });

  test(L("字幕：译文大字 + 原文小字；译文到了才有小字"), () => {
    const page = loadFloat({ lang });
    hello(page);
    page.push(caption(1, "hola a todas"));
    assert.equal(page.el("f-big").textContent, "hola a todas");
    assert.ok(hidden(page, "f-small") && hidden(page, "f-chip"));
    page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok" });
    assert.equal(page.el("f-big").textContent, "大家好");
    assert.equal(page.el("f-small").textContent, "hola a todas");
    assert.ok(!hidden(page, "f-small") && hidden(page, "f-chip"));
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
    const before = page.el("f-big").textContent;
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
  assert.ok(!/<button|<input|<select|<textarea|<a\s/.test(html), "浮窗没有任何可点的东西");
});

test("float.js 里没有 send，也没有任何往服务端发东西的写法", () => {
  const src = readFileSync(new URL("../web/float.js", import.meta.url), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  assert.ok(!/\.send\(|function send\b/.test(src));
  assert.ok(!/localStorage|sessionStorage|fetch\(|XMLHttpRequest/.test(src));
});
