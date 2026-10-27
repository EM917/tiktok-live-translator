// 底部大字幕的翻译状态、「约 N 秒前」，页脚不画统计消息，字幕字号滑块（2026-10-15）。
//
// 页面是 node:vm 里真跑的 web/app.js（tests/desktop-page.mjs），中文、英文两种界面各跑一遍：
//   0. 待译停留（专家审查 U1，用户 10-22 定的「等译文到了再一起换」）：新的一句还在翻译时大字幕停在上一句，
//      译文到了、或者确定没有译文了再整句换上；等满 4 秒还没结局就照以前的样子先放原文；
//   1. 大字幕下面的角标：译文没到、没译成的每一种结局都留着原文，并且和历史卡片上是同一句话；
//   2. 「约 N 秒前」：按这一段音频结束的时刻（ts − e2e_ms）算，超过 15 秒才出现，每秒涨一次，
//      新的一句重置，缺 e2e_ms 就不写；健康的直播（上屏 2～3 秒、下一句八九秒后来）这一行始终不出现；
//   3. 页脚：延迟统计行 2026-10-15 删了，stats 消息照收不画、也不报错；
//   4. 滑块：一个值带大字幕、历史卡片、弹幕栏三处；松手存进服务端，localStorage 只是退路；
//   5. 条高（专家审查 U1，2026-10-22）：条子最少「两行大字 + 两行小字」高、贴底排、长句不截断（样式表）；
//      实测高度写进 --live-bar-h，字幕区和弹幕栏的底部留白按它算，值没变不重复写、条子收起时清掉（页面）；
//      条子最高到顶栏下面，超出时在条内滚动、停在最底下（#125 审查，样式表）。
//      假 DOM 没有版面：getBoundingClientRect 由测试给；真排版的逐句量法在 b1-livebar 的验证记录里。
// 纯函数（角标取字、年龄文案、字号比例）在 tests/live-ui.test.mjs / live-ui.en.test.mjs。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { loadDesktop, hello, caption, web, T, FakeEl } from "./desktop-page.mjs";
import { runDesktop } from "./desktop-dom.mjs";

// live-ui.js 在浏览器里是全局函数，Node 里 require（它自己会去 require i18n.js）
const pureModule = () => createRequire(import.meta.url)("../web/live-ui.js");

const TEXT = {
  zh: {
    pending: "翻译中…", failed: "翻译失败", refused: "未翻译（接口拒答）",
    notTranslation: "未翻译（结果不像译文）", dropped: "翻译已跳过（积压）",
    sec: (n) => "约 " + n + " 秒前", min: (n) => "约 " + n + " 分钟前",
    lagTitle: "从这一句的声音结束算起，超过 15 秒才显示",
  },
  en: {
    pending: "Translating…", failed: "Translation failed", refused: "Not translated (API refusal)",
    notTranslation: "Not translated (the result didn’t look like a translation)", dropped: "Skipped (backlog)",
    sec: (n) => "About " + n + " sec ago", min: (n) => "About " + n + " min ago",
    lagTitle: "Counted from when the audio for this line ended. Shown after 15 sec",
  },
};
const CJK = /[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;

const hiddenOf = (page, id) => page.el(id).classList.contains("hidden");
// 大字幕此刻的样子：大字、小字（隐藏时是 null）、角标（隐藏时是 null）、滞后（隐藏时是 null）
function bar(page) {
  const shown = (id) => (hiddenOf(page, id) ? null : page.el(id).textContent);
  return {
    big: page.el("live-translated").textContent, small: shown("live-original"),
    chip: shown("live-state"), lag: shown("live-lag"), meta: !hiddenOf(page, "live-meta"),
  };
}
const cardChips = (page) => page.el("history").querySelectorAll(".state-chip").map((c) => c.textContent);
// 待译停留：还在翻译的一句最多等这么久才上大字幕（web/app.js 的 LIVE_HOLD_MS）。下面「角标」「滞后」那些用例要的是
// 「这一句已经在大字幕上了」，用 onBar 放上去——走的是等满 4 秒、照以前的样子先放原文的那条路
const HOLD_MS = 4000;
const onBar = (page, msg) => { page.push(msg); page.fire(HOLD_MS); };
const cardTrans = (page) => page.el("history").querySelectorAll(".trans").map((c) => c.textContent)[0];

for (const lang of ["zh", "en"]) {
  const X = TEXT[lang];
  const L = (name) => name + "（" + lang + "）";

  // ---- 0. 待译停留 ----------------------------------------------------------------------------
  // 用户 10-22 定的「等译文到了再一起换」：新的一句还在翻译时大字幕停在上一句，有了结局再整句换上；
  // 等满 HOLD_MS 还没结局就照以前的样子先放原文。卡片（字幕记录）不受影响，照常先出原文、「翻译中…」

  // 大字幕上先摆好一句译好了的（上一句）
  const settled = (page, id, es, zh) => {
    page.push(caption(id, es));
    page.push({ type: "caption_update", id, translated: zh, translate_state: "ok", quality: 1 });
  };

  test(L("待译停留：等满 4 秒还没结局，照以前的样子先放原文（不画角标）；译文后到再就地换成译文"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    settled(page, 1, "hola", "你好");
    page.push(caption(2, "esto tarda mucho"));
    page.advance(3999);
    assert.equal(bar(page).big, "你好", "还没到 4 秒：停在上一句");
    page.fire(HOLD_MS);
    assert.deepEqual([bar(page).big, bar(page).small, bar(page).chip], ["esto tarda mucho", null, null]);
    page.push({ type: "caption_update", id: 2, translated: "这个要很久", translate_state: "ok", quality: 1 });
    assert.deepEqual([bar(page).big, bar(page).small], ["这个要很久", "esto tarda mucho"]);
  });

  test(L("待译停留：没译成的结局一到就整句换上——原文大字 + 角标，和卡片上同一句；不等 4 秒"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    settled(page, 1, "hola", "你好");
    page.push(caption(2, "no se oye bien"));
    page.push({ type: "caption_update", id: 2, translated: null, translate_state: "failed", refused: true });
    assert.deepEqual([bar(page).big, bar(page).small, bar(page).chip], ["no se oye bien", null, X.refused]);
    assert.ok(page.el("live-state").classList.contains("fail"));
    assert.deepEqual(cardChips(page), ["", X.refused]);
    page.fire(HOLD_MS);                                                  // 已经换上了：4 秒到点什么都不做
    assert.deepEqual([bar(page).big, bar(page).chip], ["no se oye bien", X.refused]);
    page.push(caption(3, "otra"));
    page.push({ type: "caption_update", id: 3, translated: null, translate_state: "dropped" });
    assert.deepEqual([bar(page).big, bar(page).chip], ["otra", X.dropped], "积压跳过也是结局");
  });

  test(L("待译停留：等待中又来一句，改等新的那句；前一句的译文后到不上大字幕（卡片上照常有）"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    settled(page, 1, "hola", "你好");
    page.push(caption(2, "primera parte"));
    page.push(caption(3, "segunda parte"));
    page.push({ type: "caption_update", id: 2, translated: "第一部分", translate_state: "ok", quality: 1 });
    assert.equal(bar(page).big, "你好", "第 2 句已经被第 3 句顶掉：它的译文不上大字幕");
    assert.equal(page.el("history").querySelectorAll(".trans").map((c) => c.textContent)[1], "第一部分");
    page.push({ type: "caption_update", id: 3, translated: "第二部分", translate_state: "ok", quality: 1 });
    assert.deepEqual([bar(page).big, bar(page).small], ["第二部分", "segunda parte"]);
    page.fire(HOLD_MS);                                                  // 第 2、3 句排过的计时器：都不再动大字幕
    assert.equal(bar(page).big, "第二部分");
  });

  // 审查意见（b1-livebar 第 1 轮）：以前每来一句就重数 4 秒，句子间隔一直短于 4 秒、译文又都慢于间隔时
  // （识别积压后一串句子集中到达），大字幕会一直停在旧句（假页面里推 10 句、间隔 3 秒，30 秒后还是第一句）
  test(L("待译停留：改等新句时截止时刻沿用这一轮最早那次，不重新数 4 秒——句子一直比译文快时大字幕至少每 4 秒换一次"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    settled(page, 1, "hola", "你好");
    page.push(caption(2, "uno"));
    page.advance(3000);
    page.push(caption(3, "dos"));                                        // 3 秒后又来一句，译文都还没到
    page.fire(HOLD_MS);
    assert.equal(bar(page).big, "你好", "没有为第 3 句重新排一个 4 秒的计时器");
    page.advance(1000);
    page.fire(1000);                                                     // 第 2 句到达后满 4 秒
    assert.deepEqual([bar(page).big, bar(page).small], ["dos", null], "到点放的是还在等的那一句（最新的）");
    page.advance(2000);
    page.push(caption(4, "tres"));                                       // 新的一轮：从第 4 句重新数
    page.push({ type: "caption_update", id: 3, translated: "二", translate_state: "ok", quality: 1 });
    assert.deepEqual([bar(page).big, bar(page).small], ["二", "dos"], "大字幕上的那一句译文后到，照常就地换");
    page.advance(3000);
    page.push(caption(5, "cuatro"));
    page.fire(HOLD_MS);
    assert.equal(bar(page).big, "二");
    page.advance(1000);
    page.fire(1000);
    assert.deepEqual([bar(page).big, bar(page).small], ["cuatro", null]);
    page.push({ type: "caption_update", id: 4, translated: "三", translate_state: "ok", quality: 1 });
    assert.equal(bar(page).big, "cuatro", "被顶掉的第 4 句：译文后到也不上大字幕");
  });

  test(L("待译停留：只带 strong_state 的重译进度消息不是结局，接着等；跟上一句有关的消息照常改上一句"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    settled(page, 1, "hola", "你好");
    page.push(caption(2, "espera"));
    page.push({ type: "caption_update", id: 2, strong_state: "pending" });
    assert.equal(bar(page).big, "你好");
    page.push({ type: "caption_update", id: 1, translated: "您好", translate_state: "ok", quality: 2, strong_state: "ok" });
    assert.deepEqual([bar(page).big, bar(page).small], ["您好", "hola"], "上一句的重译照常就地换");
    page.push({ type: "caption_update", id: 2, translated: "等等", translate_state: "ok", quality: 1 });
    assert.deepEqual([bar(page).big, bar(page).small], ["等等", "espera"]);
  });

  test(L("待译停留：不用翻译的（skipped）、一到就带着结局的、回放里带 restore 的，都直接上，不等"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "buenas noches", { translate_state: "skipped", target_lang: "es" }));
    assert.equal(bar(page).big, "buenas noches");
    page.push(caption(2, "ya traducida", { translated: "已经译好", translate_state: "ok" }));
    assert.deepEqual([bar(page).big, bar(page).small], ["已经译好", "ya traducida"]);
    page.push(caption(3, "en espera"));
    page.push(caption(4, "ultima", { replay: true, restore: true }));    // 重连回放：断线前屏幕上就是它
    assert.deepEqual([bar(page).big, bar(page).small], ["ultima", null]);
    page.fire(HOLD_MS);                                                  // 第 3 句的等待已经被 restore 取代
    assert.equal(bar(page).big, "ultima");
  });

  test(L("待译停留：清空字幕、换场、回首页、重连的 hello 都把在等的那句忘掉——4 秒到点也不把大字幕放出来"), () => {
    const page = loadDesktop({ lang });
    const reset = [
      () => { page.el("clear-btn").click(); page.el("clear-btn").click(); },   // 两段式确认（V3）：第二次才清
      () => page.push({ type: "session_break", ts: T + 1 }),
      () => page.push({ type: "status", state: "idle", detail: "" }),
      () => hello(page),
    ];
    for (const action of reset) {
      hello(page);
      page.push(caption(1, "pendiente"));
      action();
      page.fire(HOLD_MS);
      assert.ok(hiddenOf(page, "live-bar"), String(action));
      assert.equal(bar(page).big, "", String(action));
    }
  });

  test(L("待译停留：「约 N 秒前」按这一句到达页面的时刻算，不按换上大字幕的时刻（消息不带 ts 时）"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    const noTs = caption(1, "atrasada", { e2e_ms: 20000 });
    delete noTs.ts;
    page.push(noTs);
    page.advance(3000);                                                  // 等译文等了 3 秒
    page.push({ type: "caption_update", id: 1, translated: "晚了", translate_state: "ok", quality: 1 });
    assert.equal(bar(page).lag, X.sec(23), "到达时已经落后 20 秒，又等了 3 秒");
  });

  // ---- 1. 翻译状态角标 ----------------------------------------------------------------------

  // 以前这条钉的是「译文还在路上时大字幕先放原文」：每句先闪一下西语、约 0.9 秒后跳成中文（专家审查 U1）。
  // 用户 10-22 定了「等译文到了再一起换」，这里改钉新的样子
  test(L("大字幕：译文还在路上时不换句——停在上一句（第一句之前就空着），卡片上照常是「翻译中…」；译文到了整句一起换上"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "hola a todas"));
    assert.ok(hiddenOf(page, "live-bar"), "第一句：大字幕先不放西语");
    assert.equal(cardTrans(page), X.pending, "历史卡片上有「翻译中…」");

    page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok", quality: 1 });
    assert.deepEqual([bar(page).big, bar(page).small, bar(page).chip], ["大家好", "hola a todas", null]);

    page.push(caption(2, "y ahora la crema"));
    assert.deepEqual([bar(page).big, bar(page).small, bar(page).chip], ["大家好", "hola a todas", null],
                     "新的一句还在翻译：大字幕停在上一句，中文和西语都不动");
    page.push({ type: "caption_update", id: 2, translated: "现在是面霜", translate_state: "ok", quality: 1 });
    assert.deepEqual([bar(page).big, bar(page).small, bar(page).chip], ["现在是面霜", "y ahora la crema", null]);
  });

  const ENDINGS = [
    ["failed", { translate_state: "failed" }, X.failed],
    ["refused", { translate_state: "failed", refused: true }, X.refused],
    ["not_translation", { translate_state: "failed", not_translation: true }, X.notTranslation],
    ["dropped", { translate_state: "dropped" }, X.dropped],
  ];
  for (const [name, update, chip] of ENDINGS) {
    test(L("大字幕：" + name + " 的结局留着原文，下面的角标和历史卡片上是同一句话"), () => {
      const page = loadDesktop({ lang });
      hello(page);
      page.push(caption(1, "no se oye bien"));
      page.push(Object.assign({ type: "caption_update", id: 1, translated: null }, update));
      const now = bar(page);
      assert.deepEqual([now.big, now.small, now.chip], ["no se oye bien", null, chip]);
      assert.deepEqual(cardChips(page), [chip], "和历史卡片上是同一句");
      assert.equal(page.el("live-state").classList.contains("fail"), name !== "dropped",
                   "没译成画成红色，被积压跳过的是中性");
    });
  }

  test(L("大字幕：一开始就带结局的字幕（回放里的最后一条）直接画角标"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "no se oye", { translate_state: "failed", refused: true, replay: true, restore: true }));
    assert.deepEqual([bar(page).big, bar(page).chip], ["no se oye", X.refused]);
  });

  test(L("大字幕：不用翻译的字幕（skipped）没有角标；译文到了之后再来一次重译，角标仍是空的"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "buenas noches", { translate_state: "skipped", target_lang: "es" }));
    assert.deepEqual([bar(page).big, bar(page).chip], ["buenas noches", null]);
    page.push(caption(2, "hola"));
    page.push({ type: "caption_update", id: 2, translated: "你好", translate_state: "ok", quality: 1 });
    page.push({ type: "caption_update", id: 2, strong_state: "pending" });
    page.push({ type: "caption_update", id: 2, translated: "您好", translate_state: "ok", quality: 2, strong_state: "ok" });
    assert.deepEqual([bar(page).big, bar(page).small, bar(page).chip], ["您好", "hola", null]);
  });

  test(L("大字幕：重译的进度消息（只带 strong_state）不动角标；重译又被拒答，角标保留"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "x"));
    page.push({ type: "caption_update", id: 1, translated: null, translate_state: "failed", refused: true });
    assert.equal(bar(page).chip, X.refused);
    page.push({ type: "caption_update", id: 1, strong_state: "pending" });
    assert.equal(bar(page).chip, X.refused, "进度消息不带 translate_state，没有话可说");
    page.push({ type: "caption_update", id: 1, translated: null, translate_state: "failed", refused: true,
                quality: 2, strong_state: "failed" });
    assert.equal(bar(page).chip, X.refused);
    assert.deepEqual(cardChips(page), [X.refused]);
  });

  test(L("大字幕：角标跟的是大字幕上的那一句，另一句的消息不改它"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    onBar(page, caption(1, "primera"));
    onBar(page, caption(2, "segunda"));
    page.push({ type: "caption_update", id: 1, translated: null, translate_state: "failed" });   // 旧的一句
    assert.deepEqual([bar(page).big, bar(page).chip], ["segunda", null]);
    assert.deepEqual(cardChips(page), [X.failed, ""]);
    page.push({ type: "caption_update", id: 2, translated: null, translate_state: "failed" });
    assert.equal(bar(page).chip, X.failed);
    onBar(page, caption(3, "tercera"));                                                           // 新的一句：角标重新来过
    assert.equal(bar(page).chip, null);
  });

  test(L("角标是界面文字：不带 i18n-data（要查漏翻），中文界面不夹英文、英文界面没有汉字"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    onBar(page, caption(1, "x"));
    for (const id of ["live-state", "live-lag", "live-meta"]) {
      assert.ok(!page.el(id).classList.contains("i18n-data"), id);
      assert.equal(page.el(id).getAttribute("translate"), null, id);
    }
    for (const [, update] of ENDINGS) {
      page.push(Object.assign({ type: "caption_update", id: 1, translated: null }, update));
      const text = page.el("live-state").textContent + " " + page.el("live-lag").textContent;
      if (lang === "en") assert.ok(!CJK.test(text), text);
    }
    assert.equal(page.el("live-lag").title, X.lagTitle);
  });

  // ---- 2. 「约 N 秒前」 ---------------------------------------------------------------------
  // 默认的 caption() 是健康的一句：音频结束后 2.3 秒上屏（e2e_ms 2300）。LATE 是上屏时就已经落后很多的一句。
  const LATE = { e2e_ms: 20000 };

  test(L("滞后：健康的直播里始终不出现——上屏 2.3 秒，下一句 8～10 秒后才来，整个周期都低于 15 秒"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    onBar(page, caption(1, "hola", { ts: T, e2e_ms: 2300 }));
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false], "刚上屏：没有数字，也没有空着的一行");
    for (const ms of [2000, 3000, 3000, 2000, 1000, 1000]) {            // 累计 12 秒，音频结束后 14.3 秒
      page.advance(ms);
      page.tick(1000);
      assert.equal(bar(page).lag, null, "已经 " + (page.now() / 1000 - T + 2.3) + " 秒");
    }
    onBar(page, caption(2, "adiós", { ts: T + 12, e2e_ms: 2500 }));        // 下一句来了
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false]);
  });

  test(L("滞后：从这一段音频结束算起，满 15 秒才出现，之后每秒涨一次，一分钟以上改写分钟"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    onBar(page, caption(1, "hola", { ts: T, e2e_ms: 2300 }));              // 音频在 2.3 秒前结束
    page.advance(12699);                                                // 14.999 秒
    page.tick(1000);
    assert.equal(bar(page).lag, null, "差 1 毫秒不写");
    page.advance(1);                                                    // 15.0 秒
    page.tick(1000);
    assert.deepEqual([bar(page).lag, bar(page).meta], [X.sec(15), true]);
    page.advance(4000);
    page.tick(1000);
    assert.equal(bar(page).lag, X.sec(19));
    page.advance(46000);                                                // 65 秒
    page.tick(1000);
    assert.equal(bar(page).lag, X.min(1));
    page.advance(60000);                                                // 125 秒
    page.tick(1000);
    assert.equal(bar(page).lag, X.min(2));
  });

  test(L("滞后：积压时一上屏就写——这一句上屏的时候已经比平常老得多"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    onBar(page, caption(1, "atrasado", LATE));
    assert.deepEqual([bar(page).lag, bar(page).meta], [X.sec(20), true]);
    onBar(page, caption(2, "al día", { e2e_ms: 2400 }));                  // 追上了：新的一句，数字收起来
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false]);
  });

  test(L("滞后：不依赖 segment_ms——老版本、新版本服务端算出来一样（只看 ts 和 e2e_ms）"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    onBar(page, caption(1, "sin segmento", LATE));
    assert.equal(bar(page).lag, X.sec(20));
    onBar(page, caption(2, "con segmento", Object.assign({ segment_ms: 7000 }, LATE)));
    assert.equal(bar(page).lag, X.sec(20), "带了也不多算一个片段");
  });

  test(L("滞后：译文回来不重置它（它算的是这一句的声音什么时候结束的）；新的一句才重置"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    onBar(page, caption(1, "hola", { ts: T, e2e_ms: 2000 }));
    page.advance(14000);                                                // 音频结束后 16 秒
    page.push({ type: "caption_update", id: 1, translated: "你好", translate_state: "ok", quality: 1 });
    page.tick(1000);
    assert.equal(bar(page).lag, X.sec(16));
    page.advance(6000);
    onBar(page, caption(2, "adiós", { ts: T + 20, e2e_ms: 2400 }));       // 下一句，正常
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false], "新的一句：重新从 2.4 秒数起，低于 15 秒不写");
  });

  test(L("滞后：本机时钟拨回（负数）和离谱的大数不写，同样的数据再老 20 秒就写"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    onBar(page, caption(1, "no", { ts: T + 100, e2e_ms: 400 }));         // ts 在「未来」
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false], "负数不写，整行也收起来");
    onBar(page, caption(2, "ok", { ts: T - 8 * 3600, e2e_ms: 400 }));    // 8 小时前
    assert.equal(bar(page).lag, null);
    onBar(page, caption(3, "sí", { ts: T - 20, e2e_ms: 400 }));          // 同样的字段，只是 20 秒前
    assert.equal(bar(page).lag, X.sec(20));
  });

  test(L("滞后：服务端没给 e2e（演示模式、老版本）就不写，不拿残缺的数冒充"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    const demo = caption(1, "demo line");
    delete demo.e2e_ms;
    page.push(demo);
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false]);
    onBar(page, caption(2, "sin e2e", { ts: T - 60, e2e_ms: undefined }));
    assert.equal(bar(page).lag, null, "再老也不写：没有 e2e 就不知道音频什么时候结束");
    onBar(page, caption(3, "nulo", { ts: T - 60, e2e_ms: null }));
    assert.equal(bar(page).lag, null, "null 不当 0");
    onBar(page, caption(4, "completo", { ts: T - 60, e2e_ms: 2300 }));
    assert.equal(bar(page).lag, X.min(1), "62 秒前");
  });

  test(L("滞后：和角标同在一行；只有一样也行；整行没内容时收起"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    onBar(page, caption(1, "no se oye", LATE));
    assert.deepEqual(bar(page), { big: "no se oye", small: null, chip: null, lag: X.sec(20), meta: true }, "只有滞后");
    page.push({ type: "caption_update", id: 1, translated: null, translate_state: "failed", refused: true });
    assert.deepEqual(bar(page), { big: "no se oye", small: null, chip: X.refused, lag: X.sec(20), meta: true }, "角标 + 滞后");
    page.push({ type: "caption_update", id: 1, translated: "听不清", translate_state: "ok", quality: 2 });
    assert.deepEqual([bar(page).chip, bar(page).lag, bar(page).meta], [null, X.sec(20), true]);
    onBar(page, caption(2, "sin datos"));
    assert.deepEqual([bar(page).chip, bar(page).lag, bar(page).meta], [null, null, false], "什么都没有：整行收起");
    onBar(page, caption(3, "falla", { translate_state: "failed" }));
    assert.deepEqual([bar(page).chip, bar(page).lag, bar(page).meta], [X.failed, null, true], "只有角标（这一句不老）");
  });

  test(L("滞后：断线重连后的 restore 那一句也算，按它自己的 ts；回放里别的句子不动大字幕"), () => {
    const page = loadDesktop({ lang, startMs: (T + 30) * 1000 });
    hello(page);
    page.push(caption(1, "vieja", { replay: true }));
    assert.equal(bar(page).big, "", "普通回放不画大字幕");
    page.push(caption(2, "ultima", { replay: true, restore: true, ts: T }));
    assert.deepEqual([bar(page).big, bar(page).lag], ["ultima", X.sec(32)]);   // 30 + 2.3
    const fresh = loadDesktop({ lang, startMs: (T + 5) * 1000 });
    hello(fresh);
    fresh.push(caption(2, "reciente", { replay: true, restore: true, ts: T }));
    assert.equal(bar(fresh).lag, null, "刚断开几秒就重连：7.3 秒，不写");
  });

  test(L("滞后：清空字幕、换场、回到首页，角标和滞后一起收掉，不留给下一句"), () => {
    const page = loadDesktop({ lang });
    const fresh = () => {
      hello(page);
      page.push(caption(1, "hola", Object.assign({ translate_state: "failed" }, LATE)));
      assert.ok(bar(page).meta && bar(page).lag && bar(page).chip);
    };
    fresh();
    page.el("clear-btn").click();
    page.el("clear-btn").click();                                      // 两段式确认（V3）：第二次才清
    assert.deepEqual([bar(page).chip, bar(page).lag, bar(page).meta], [null, null, false]);
    assert.ok(hiddenOf(page, "live-bar"));
    page.tick(1000);                                                    // 大字幕没了，计时器不能把它写回来
    assert.equal(bar(page).lag, null);

    fresh();
    page.push({ type: "session_break", ts: T + 1 });
    assert.deepEqual([bar(page).chip, bar(page).lag, bar(page).meta], [null, null, false]);

    fresh();
    page.push({ type: "status", state: "idle", detail: "" });
    assert.deepEqual([bar(page).chip, bar(page).lag, bar(page).meta], [null, null, false]);
    assert.ok(hiddenOf(page, "live-bar"));
  });

  test(L("手机页不带这一行：滞后和角标只在电脑页"), () => {
    assert.doesNotMatch(web("viewer.js") + web("viewer.html"), /live-lag|live-state|segment_ms/);
  });

  // ---- 3. 页脚 -----------------------------------------------------------------------------

  test(L("页脚：没有延迟统计行（2026-10-15 删了），统计消息照收不画，也不报错"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push({ type: "stats", asr: { p50: 1100 }, translate: { p50: 2000 }, segment: { p50: 800 },
                detect_worst: { p50: 1900, p95: 3400 }, caption_wait: { p50: 4200, p95: 7100, n: 20 },
                audio_backlog_sec: 5, audio_segments_dropped: 2, asr_overruns: 1 });
    assert.ok(!page.el("stats-line"));
  });

  // ---- 4. 字幕字号滑块 ----------------------------------------------------------------------

  test(L("滑块：没调过、服务端也没记过，页面一个 CSS 变量都不设，也不往服务端发"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    assert.deepEqual(page.cssVars, {});
    assert.deepEqual(page.sent.filter((m) => m.type === "set_caption_size"), []);
    assert.equal(page.el("font-size").value, 34, "滑块停在样式表里的默认值");
  });

  test(L("滑块：拖动即时改大字幕、历史卡片、弹幕栏三处的变量；松手才存服务端和 localStorage 退路"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    const slider = page.el("font-size");
    slider.value = "51";
    slider.fire("input");
    assert.deepEqual(page.cssVars, { "--sub-size": "51px", "--cap-scale": "1.5" });
    assert.equal(page.store.get("subFontSize"), "51");
    assert.deepEqual(page.sent.filter((m) => m.type === "set_caption_size"), [], "拖动中不写 settings.json");
    slider.fire("change");
    assert.deepEqual(page.sent.filter((m) => m.type === "set_caption_size"), [{ type: "set_caption_size", value: 51 }]);
  });

  test(L("滑块：服务端记着的字号是正本，盖过 localStorage（端口漂移后本机是空的或旧的）"), () => {
    const page = loadDesktop({ lang, stored: { subFontSize: "30" } });
    assert.deepEqual(page.cssVars, { "--sub-size": "30px", "--cap-scale": "0.882" }, "hello 之前先用本机记的，不闪默认");
    hello(page, { caption_size: 50 });
    assert.equal(page.el("font-size").value, 50);
    assert.deepEqual(page.cssVars, { "--sub-size": "50px", "--cap-scale": "1.471" });
    assert.equal(page.store.get("subFontSize"), "50");
    assert.deepEqual(page.sent.filter((m) => m.type === "set_caption_size"), [], "服务端有记录就不回发");

    const moved = loadDesktop({ lang });                                 // 换了端口：localStorage 是空的
    hello(moved, { caption_size: 44 });
    assert.deepEqual(moved.cssVars, { "--sub-size": "44px", "--cap-scale": "1.294" });
  });

  test(L("滑块：服务端没记过而本机记着（旧版本只存 localStorage），交给服务端存起来"), () => {
    const page = loadDesktop({ lang, stored: { subFontSize: "38" } });
    hello(page);
    assert.deepEqual(page.sent.filter((m) => m.type === "set_caption_size"), [{ type: "set_caption_size", value: 38 }]);
    assert.equal(page.el("font-size").value, 38);
  });

  test(L("滑块：来路不明的值（越界、非数字）不当字号"), () => {
    for (const bad of [9000, 3, "big", true, null]) {
      const page = loadDesktop({ lang });
      hello(page, { caption_size: bad });
      assert.deepEqual(page.cssVars, {}, String(bad));
      assert.deepEqual(page.sent.filter((m) => m.type === "set_caption_size"), [], String(bad));
    }
    const dirty = loadDesktop({ lang, stored: { subFontSize: "huge" } });
    assert.deepEqual(dirty.cssVars, {});
  });

  test(L("滑块：另一个窗口调了字号，这一页没动过滑块就跟上；动过之后以这一页为准"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push({ type: "config", caption_size: 56 });
    assert.deepEqual([page.el("font-size").value, page.cssVars["--sub-size"]], [56, "56px"]);
    page.push({ type: "config", target_lang: "en" });                    // 别的 config 广播不带字号：不动
    assert.equal(page.cssVars["--sub-size"], "56px");
    page.el("font-size").value = "40";
    page.el("font-size").fire("input");
    page.push({ type: "config", caption_size: 24 });
    assert.deepEqual([page.el("font-size").value, page.cssVars["--sub-size"]], ["40", "40px"]);
  });

  test(L("滑块：松手时连接断着，重连后的 hello 补发这一页的字号，不被服务端的旧值盖回去"), () => {
    const page = loadDesktop({ lang });
    hello(page, { caption_size: 30 });
    page.disconnect();
    page.el("font-size").value = "46";
    page.el("font-size").fire("input");
    page.el("font-size").fire("change");
    assert.deepEqual(page.sent.filter((m) => m.type === "set_caption_size"), [], "断着发不出去");
    page.connect();
    hello(page, { caption_size: 30 });
    assert.deepEqual(page.sent.filter((m) => m.type === "set_caption_size"), [{ type: "set_caption_size", value: 46 }]);
    assert.deepEqual([page.el("font-size").value, page.cssVars["--sub-size"]], ["46", "46px"]);
    hello(page, { caption_size: 46 });                                    // 补发成功之后不再重复发
    assert.equal(page.sent.filter((m) => m.type === "set_caption_size").length, 1);
  });

  // ---- 5. 条高：实测高度 → --live-bar-h ---------------------------------------------------------
  // node 里没有 ResizeObserver：页面在改了条子的地方直接量（liveBarChanged）。条子的高度由测试给

  // 记下页面往 <html> 上写 --live-bar-h 的每一次，并让条子报出 height（可以随时改）
  function barHeight(page, initial) {
    const box = { height: initial, writes: [] };
    const set = page.root.style.setProperty;
    page.root.style.setProperty = (k, v) => { if (k === "--live-bar-h") box.writes.push(v); set(k, v); };
    page.el("live-bar").getBoundingClientRect = () =>
      ({ top: 710 - box.height, bottom: 710, left: 0, right: 1000, width: 1000, height: box.height });
    return box;
  }

  test(L("条高：大字幕的实测高度写进 --live-bar-h（向上取整到 px）；值没变不重复写，每秒的「约 N 秒前」重画也不写"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    const bar = barHeight(page, 165.8);
    assert.deepEqual(bar.writes, [], "还没有字幕、条子收着：不量");
    page.push(caption(1, "hola a todas"));
    assert.deepEqual(bar.writes, [], "第一句还在翻译：大字幕还没出来，不量");
    page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok", quality: 1 });
    assert.deepEqual(bar.writes, ["166px"]);
    assert.equal(page.cssVars["--live-bar-h"], "166px");
    page.push(caption(2, "segunda"));
    page.push({ type: "caption_update", id: 2, translated: "第二句", translate_state: "ok", quality: 1 });
    for (let i = 0; i < 3; i++) page.tick(1000);
    assert.deepEqual(bar.writes, ["166px"], "一到两行的句子条高不变：一次都不多写");
    bar.height = 210.6;                                                 // 一句三行的长句：条子临时变高（不截断）
    onBar(page, caption(3, "una frase larguísima"));
    assert.deepEqual(bar.writes, ["166px", "211px"]);
    bar.height = 165.8;                                                 // 下一句回到两行以内
    onBar(page, caption(4, "corta"));
    assert.deepEqual(bar.writes, ["166px", "211px", "166px"]);
  });

  // 以前（本批第 1 版）条子收起时留着上一次的值。审查意见：上一场最后一句是超长句（字号 60 时条子可达 625px）的话，
  // 回到首页后两栏底下多出一大段能滚的空白，要等下一场第一句才纠正。改成收起时清掉，回到没量过时的留白
  test(L("条高：条子收起（清空字幕、换场、回首页）时把 --live-bar-h 清掉，留白回到没量过时的样子；量出来的 0 不写；下一句出来重新写"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    const bar = barHeight(page, 624.6);                                 // 字号 60 下的超长句
    onBar(page, caption(1, "hola"));
    assert.deepEqual(bar.writes, ["625px"]);
    bar.height = 0;                                                     // display:none 的元素量出来就是 0
    page.el("clear-btn").click();
    page.el("clear-btn").click();                                      // 两段式确认（V3）：第二次才清
    assert.deepEqual(bar.writes, ["625px", ""], "清空字幕：变量清掉（空值＝去掉这条声明）");
    page.push(caption(2, "otra"));
    page.push({ type: "session_break", ts: T + 1 });
    page.push({ type: "status", state: "idle", detail: "" });
    assert.ok(hiddenOf(page, "live-bar"));
    assert.deepEqual(bar.writes, ["625px", ""], "已经清过了：换场、回首页不再重复写，量出来的 0 也不写");
    assert.equal(page.cssVars["--live-bar-h"], "");
    hello(page);
    bar.height = 624.6;                                                 // 下一场第一句和上一场最后一句一样高：照样重新写
    onBar(page, caption(3, "otra vez"));
    assert.deepEqual(bar.writes, ["625px", "", "625px"]);
    for (const reset of [() => page.push({ type: "session_break", ts: T + 2 }),
                         () => page.push({ type: "status", state: "idle", detail: "" })]) {
      hello(page);
      onBar(page, caption(4, "y otra"));
      assert.equal(page.cssVars["--live-bar-h"], "625px");
      reset();
      assert.equal(page.cssVars["--live-bar-h"], "", String(reset));
    }
  });

  test(L("条高：拖字号滑块时重新量（条子跟着字号变高变矮），两栏贴底看的人跟着再贴一次；翻上去看的不动"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    const bar = barHeight(page, 165.8);
    onBar(page, caption(1, "hola"));
    const hist = page.el("history"), cmts = page.el("comment-list");
    for (const box of [hist, cmts]) Object.assign(box, { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 });
    bar.height = 219.9;                                                 // 拖到 48
    page.el("font-size").value = "48";
    page.el("font-size").fire("input");
    assert.deepEqual(bar.writes, ["166px", "220px"]);
    // 留白长了：贴底的人马上再贴到底，不然条子新长高的那一截正好盖住最新一条
    for (const box of [hist, cmts]) box.scrollHeight = 2054;
    bar.height = 230;
    page.el("font-size").value = "50";
    page.el("font-size").fire("input");
    assert.deepEqual([hist.scrollTop, cmts.scrollTop], [2054, 2054]);
    // 中控翻上去看字幕记录（自己滚的）：之后留白再变，也不把他拽回底部
    hist.fire("wheel");
    hist.scrollTop = 300;
    hist.fire("scroll");
    bar.height = 165.8;
    page.el("font-size").value = "34";
    page.el("font-size").fire("input");
    assert.equal(hist.scrollTop, 300);
    assert.deepEqual(bar.writes, ["166px", "220px", "230px", "166px"]);
  });

  // ---- 6. 页脚长高：实测高度 → --foot-h（#143 审查） ------------------------------------------------
  // 760 宽、英文约 260 字的提示折成 3 行（54px），以前条子 bottom 写死 22px，页脚多出来的 32px 盖住条子最下面一行
  // （西语原文、角标）约 12 秒。页面量页脚的高度写进 --foot-h，条子和两栏的留白跟着让（样式表那几条）
  test(L("页脚：显示长提示长高时把实测高度写进 --foot-h，提示到点收回一行时清掉；一行（22px）不写；贴底看的人跟着再贴一次"), () => {
    const page = loadDesktop({ lang });
    hello(page, { version: "1.2.3" });
    const foot = { height: 22, writes: [] };
    const set = page.root.style.setProperty;
    page.root.style.setProperty = (k, v) => { if (k === "--foot-h") foot.writes.push(v); set(k, v); };
    // 假页面不建树：给版本号挂上页脚这个父元素（真页面里 #app-version 就在 .copyright 里），由测试报出高度
    const footer = new FakeEl("div");
    footer.className = "copyright";
    footer.appendChild(page.el("app-version"));
    footer.getBoundingClientRect = () =>
      ({ top: 760 - foot.height, bottom: 760, left: 0, right: 760, width: 760, height: foot.height });
    onBar(page, caption(1, "hola"));
    const hist = page.el("history");
    Object.assign(hist, { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 });
    // 短提示：一行，不写
    page.push({ type: "notice", text: "short" });
    assert.deepEqual(foot.writes, []);
    assert.ok(page.el("app-version").classList.contains("noting"));
    page.fire(10000);
    assert.deepEqual(foot.writes, [], "收回一行：本来就没写，也不写");
    // 长提示折成三行
    foot.height = 53.6;
    hist.scrollHeight = 2032;
    page.push({ type: "notice", text: "x".repeat(260) });
    assert.deepEqual(foot.writes, ["54px"], "向上取整到 px");
    assert.equal(page.cssVars["--foot-h"], "54px");
    assert.equal(hist.scrollTop, 2032, "留白长了：贴底看的人跟着再贴到底");
    // 到点：页脚回到一行，变量清掉（空值＝去掉这条声明），样式回到退路 22px
    foot.height = 22;
    page.fire(12000);
    assert.ok(!page.el("app-version").classList.contains("noting"));
    assert.deepEqual(foot.writes, ["54px", ""]);
    assert.equal(page.cssVars["--foot-h"], "");
  });
}

// ---- 两段式确认（confirmTwice，专家审查 V3）：点了确认之后，武装时排下的复原定时器要撤掉 -------------------
// #137 核对时记下：reset() 里那句 clearTimeout 删掉，现有用例照样全绿。漏撤的后果：点两下清空之后马上又点一下
// （重新武装），上一次武装时排下的 6 秒复原到点照样触发，新的这一次没等满自己的 6 秒就被复原，第二下落空。
// 假页面的定时器不分先后（fire 按时长一次全触发），这里换成按到期时刻排队的时钟，才分得出是哪一个先到
function clockedPage(lang) {
  const page = runDesktop(lang);
  let now = 0;
  const queue = [];
  page.sandbox.setTimeout = (fn, ms) => { queue.push({ fn, at: now + (ms || 0) }); return queue.length; };
  page.sandbox.clearTimeout = (id) => { if (queue[id - 1]) queue[id - 1].fn = null; };
  const runTo = (t) => {
    for (;;) {
      const due = queue.filter((q) => q.fn && q.at <= t).sort((x, y) => x.at - y.at)[0];
      if (!due) break;
      now = due.at;
      const fn = due.fn;
      due.fn = null;
      fn();
    }
    now = t;
  };
  return { page, runTo };
}
for (const [lang, armed, idle] of [["zh", "再点确认", "清空字幕"], ["en", "Clear?", "Clear"]]) {
  test("两段式确认（" + lang + "）：确认清空之后马上再武装，新的一次照样等满自己的 6 秒，不被上一次的复原定时器提前复原", () => {
    const { page, runTo } = clockedPage(lang);
    page.push({ type: "hello", config: { status: { state: "live", detail: "" },
                                         room_url: "https://www.tiktok.com/@luna_demo/live" } });
    const btn = page.el("clear-btn");
    btn.click();                                     // t=0 武装，复原排在 6000
    assert.equal(btn.textContent, armed);
    runTo(1000);
    btn.click();                                     // t=1000 确认：清空，按钮复原，6000 那个复原要撤掉
    assert.equal(btn.textContent, idle);
    runTo(2000);
    btn.click();                                     // t=2000 重新武装，复原排在 8000
    assert.equal(btn.textContent, armed);
    runTo(6500);
    assert.equal(btn.textContent, armed, "上一次武装的复原（6000）不该触发：这一次要到 8000 才复原");
    runTo(8000);
    assert.equal(btn.textContent, idle, "到点复原");
  });
}

// ---- 滑块带动卡片和弹幕：样式表（语言无关） ----------------------------------------------------

const css = web("style.css");
const ruleBody = (selector) => {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = css.match(new RegExp("\\n" + esc + " \\{([^}]*)\\}"));
  assert.ok(m, "style.css 里找不到规则 " + selector);
  return m[1];
};
const rootVar = (name) => {
  const m = css.match(new RegExp("\\n  " + name.replace(/-/g, "\\-") + ": ([^;]+);"));
  assert.ok(m, ":root 里找不到 " + name);
  return m[1].trim();
};
// 把 font-size 的值算成 px：calc(<数>px * var(--cap-scale)) 或 calc(var(--fs-x) * var(--cap-scale))；scale 由调用方给
function fontPx(selector, scale) {
  const m = ruleBody(selector).match(/font-size:\s*([^;]+);/);
  assert.ok(m, selector + " 没有 font-size");
  const value = m[1].trim();
  const times = value.match(/^calc\((.+?) \* var\(--cap-scale\)\)$/);
  assert.ok(times, selector + " 的字号不跟 --cap-scale：" + value);
  const base = times[1].match(/^var\((--[\w-]+)\)$/) ? rootVar(times[1].slice(4, -1)) : times[1];
  const px = base.match(/^([\d.]+)px$/);
  assert.ok(px, selector + " 的基准不是 px：" + base);
  return Number(px[1]) * scale;
}
// 读在滑块之前的固定字号（这次改动之前就是这些数）：没调过字号的人看到的必须和以前一样
const TODAY = {
  ".cap .orig": 13, ".cap .trans": 17, ".cap .trans.pending": 14,
  ".cmt-zh": 15, ".cmt-zh.pending": 13, ".cmt-orig": 12,
};

test("样式表：--cap-scale 默认是 1，卡片和弹幕的字号逐个等于今天的固定值（没调过滑块的人看不出变化）", () => {
  assert.equal(rootVar("--cap-scale"), "1");
  for (const [selector, px] of Object.entries(TODAY)) assert.equal(fontPx(selector, 1), px, selector);
});

test("样式表：滑块的值按比例带动历史卡片和弹幕栏（译文、原文、占位提示都跟着）", () => {
  const { captionSizeVars } = pureModule();
  for (const size of [29, 34, 40, 51, 60]) {
    const scale = Number(captionSizeVars(size)["--cap-scale"]);
    assert.equal(scale, Math.round(Math.min(1.75, Math.max(0.85, size / 34)) * 1000) / 1000);
    for (const [selector, px] of Object.entries(TODAY)) {
      assert.ok(Math.abs(fontPx(selector, scale) - px * scale) < 1e-9, selector + " @" + size);
    }
  }
  // 往大调：34 → 51 是 1.5 倍，卡片译文 17px → 25.5px
  assert.equal(fontPx(".cap .trans", Number(captionSizeVars(51)["--cap-scale"])), 25.5);
  // 往小调到底也不小于 85%：17px 的译文最小 14.45px，12px 的弹幕原文最小 10.2px
  assert.ok(Math.abs(fontPx(".cap .trans", Number(captionSizeVars(20)["--cap-scale"])) - 14.45) < 1e-9);
});

test("样式表：大字幕下面的一行说明（角标、滞后）字号固定，不跟滑块走", () => {
  for (const selector of [".live-state", ".live-lag"]) {
    assert.doesNotMatch(ruleBody(selector), /cap-scale|sub-size/, selector);
  }
});

test("样式表：「翻译中…」占位提示不再用 --label-3（约 2:1），提到 --label-2 的档", () => {
  // --text-dim 是 --label-2 的别名
  assert.equal(rootVar("--text-dim"), "var(--label-2)");
  for (const selector of [".cap .trans.pending", ".cmt-zh.pending", ".alert-zh.pending"]) {
    const color = ruleBody(selector).match(/color:\s*var\((--[\w-]+)\)/);
    assert.ok(color, selector);
    assert.ok(["--text-dim", "--label-2"].includes(color[1]), selector + " 用了 " + color[1]);
  }
});

// ---- 5. 条高稳定（专家审查 U1）：样式表 ----------------------------------------------------------

const decls = (selector) => {
  const out = {};
  for (const part of ruleBody(selector).replace(/\/\*[\s\S]*?\*\//g, "").split(";")) {
    const i = part.indexOf(":");
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
};
// 把 calc 里的 var(--x) 换成数，算出 px（只认 + 和 *，样式表里就这么写的）
function calcPx(expr, vars) {
  const m = /^calc\((.+)\)$/.exec(expr.trim());
  assert.ok(m, "不是 calc：" + expr);
  return m[1].split(" + ").reduce((sum, term) => sum + term.split(" * ").reduce((prod, f) => {
    f = f.trim();
    const v = /^var\((--[\w-]+)(?:,\s*([\d.]+)px)?\)$/.exec(f);
    if (v) return prod * (v[1] in vars ? vars[v[1]] : Number(v[2]));
    const px = /^([\d.]+)(px)?$/.exec(f);
    assert.ok(px, "认不出的一项：" + f);
    return prod * Number(px[1]);
  }, 1), 0);
}

test("样式表：大字幕条最少有「两行大字 + 两行小字」那么高，内容贴着底边排（一到两行的句子条高不变）", () => {
  const bar = decls(".live-bar");
  const big = decls(".live-translated");
  const small = decls(".live-original");
  // 贴着底边排：以前写 column + flex-end；封顶以后（见下面「条子封顶」）改成 column-reverse + flex-start——
  // 没超出时排出来一样（从底边往上排），超出时滚动的原点在底边，停在最底下，开头那几行还滚得到
  // （column + flex-end 超出的部分落在上沿外面，滚不到）
  assert.deepEqual([bar.display, bar["flex-direction"], bar["justify-content"]], ["flex", "column-reverse", "flex-start"]);
  assert.equal(bar.padding, "14px 24px 14px");
  assert.equal(big["line-height"], "1.35");
  assert.equal(small["line-height"], "1.4", "小字的行高写死，最小高度才算得准");
  assert.equal(small["font-size"], "calc(var(--sub-size) * 0.42)");
  assert.equal(small["margin-top"], "4px");
  for (const size of [26, 32, 34, 48, 60]) {
    // 上下内边距 28 + 上边框按 1px（Chrome 一倍屏把 0.5px 画成 1px）+ 小字的 margin-top 4 + 余量 1
    const want = size * 1.35 * 2 + size * 0.42 * 1.4 * 2 + 28 + 1 + 4 + 1;
    const got = calcPx(bar["min-height"], { "--sub-size": size });
    assert.ok(Math.abs(got - want) < 0.01, "字号 " + size + "：" + got + " ≠ " + want);
  }
});

test("样式表：超长句不截断（D13）——大字幕、大字、小字都不限行数、不藏溢出", () => {
  for (const selector of [".live-translated", ".live-original"]) {
    const body = ruleBody(selector).replace(/\/\*[\s\S]*?\*\//g, "");
    // overflow-wrap 管的是在哪儿折行，不藏东西；别的 overflow 一律不许
    assert.doesNotMatch(body, /line-clamp|max-height|overflow(-[xy])?\s*:|text-overflow/, selector);
    // 条子是封了顶的滚动容器（overflow-x 的计算值也跟着成了 auto）：一长串断不开的字符（网址之类）要在条内
    // 折行，否则会在条子里横向滚动、后半截看不见（b2-contrast 审查；WKWebView 和无头 Chrome 实测横向溢出
    // 1441–5086px → 0，普通句子的条顶、条高逐像素不变）
    assert.equal(decls(selector)["overflow-wrap"], "anywhere", selector);
  }
  // 条子本身封了顶（#125 审查，见下一条），但超出的部分是滚出来看，不是藏起来：只许 overflow-y: auto
  const bar = decls(".live-bar");
  assert.doesNotMatch(ruleBody(".live-bar").replace(/\/\*[\s\S]*?\*\//g, ""), /line-clamp|text-overflow|overflow:\s*(hidden|clip)/);
  assert.equal(bar["overflow-y"], "auto");
  assert.ok(!("overflow" in bar) && !("overflow-x" in bar), "只开竖向滚动");
});

test("样式表：条子封顶（#125 审查）——最高到顶栏下面 8px，超出时条内滚动并停在最底下；字号 26–60 都成立", () => {
  const bar = decls(".live-bar");
  // 上限 = 窗口高 − 底部署名 − 顶栏 − 8px 空隙。底部署名平时 22px，显示提示时长高，按 --foot-h 算（#143 审查）
  const m = /^calc\(100vh - var\(--foot-h, ([\d.]+)px\) - var\(--topbar-room\) - ([\d.]+)px\)$/.exec(bar["max-height"] || "");
  assert.ok(m, "max-height：" + bar["max-height"]);
  const copyright = Number(m[1]), gap = Number(m[2]);
  assert.equal(bar.bottom, "var(--foot-h, " + copyright + "px)", "条子贴着底部署名，页脚长高时跟着往上挪");
  assert.equal(decls(".copyright").height, copyright + "px", "没写 --foot-h 时的退路就是一行页脚的高度");
  assert.ok(gap >= 4, "和顶栏之间留点空");
  // 顶栏：600px 以上一行（min-height 52px，满编也是 52，b2-contrast 无头 Chrome 量的），600px 及以下折成两行 75px
  assert.equal(bar["--topbar-room"], decls(".topbar")["min-height"]);
  const narrow = /@media \(max-width: 600px\) \{\s*\.live-bar \{ --topbar-room: ([\d.]+)px; \}\s*\}/.exec(css);
  assert.ok(narrow, "≤600px 时给折成两行的顶栏留地方");
  assert.ok(/@media \(max-width: 600px\) \{\s*\.topbar \{ flex-wrap: wrap; \}/.test(css), "顶栏正是在 600px 及以下折行");
  const rooms = { wide: parseFloat(bar["--topbar-room"]), narrow: Number(narrow[1]) };
  assert.ok(rooms.narrow >= 75, "两行的顶栏量出来 75px");
  // 窗口最小 420×480（main.py），去掉标题栏视口约 452px；再矮一点也算上。各种字号下：
  // ① 封顶后条顶在顶栏下面；② 最小高度（两行大字 + 两行小字）不超过上限，封顶总是生效
  for (const vh of [440, 452, 600, 732, 900]) {
    for (const [name, room] of Object.entries(rooms)) {
      const maxH = vh - copyright - room - gap;
      const barTop = vh - copyright - maxH;
      assert.ok(barTop >= room + 4, vh + "px " + name + "：条顶 " + barTop + " 压到顶栏（" + room + "）");
      for (const size of [26, 34, 48, 60]) {
        const minH = calcPx(bar["min-height"], { "--sub-size": size });
        assert.ok(minH <= maxH, vh + "px " + name + " 字号 " + size + "：最小高度 " + minH + " 超过上限 " + maxH);
      }
    }
  }
  // 停在最底下：column-reverse 的滚动原点在底边。三块用 order 排回原来的上下顺序（大字、小字、角标），
  // 和 index.html 里的文档顺序一致（读屏照文档顺序念，看到的也是这个顺序）
  assert.equal(bar["overflow-y"], "auto");
  assert.equal(bar["flex-direction"], "column-reverse");
  // 关掉滚动锚定（b2-contrast 审查）：不关时 macOS 的系统 WebKit（真 app 用的 WKWebView）在条子停在离底 1px
  // （scrollTop −1，条高带小数时常见）时按离顶边的距离保持位置，同一句变长、译文到达、下一句长句换上来后
  // scrollTop 跳到 −322 ～ −779，句子结尾和角标被推到条子下沿外面；关掉后按离底边的距离保持，一直贴底。
  // 无头 Chrome 关不关都贴底，所以这条只能靠样式表钉住
  assert.equal(bar["overflow-anchor"], "none", "条子里不做滚动锚定，换句、变长后仍停在最底下");
  const order = (sel) => Number(decls(sel).order);
  assert.ok(order(".live-translated") > order(".live-original") && order(".live-original") > order(".live-meta"),
    "column-reverse 从底边往上排：order 小的在下面");
  const html = web("index.html").replace(/<!--[\s\S]*?-->/g, "");
  const footer = /<footer id="live-bar"[\s\S]*?<\/footer>/.exec(html)[0];
  const at = (id) => footer.indexOf('id="' + id + '"');
  assert.ok(at("live-translated") < at("live-original") && at("live-original") < at("live-meta"), "文档顺序：大字、小字、角标");
  assert.equal((footer.match(/<div id=/g) || []).length, 3, "条子里只有这三块（多了要给它也排 order）");
});

test("样式表：字幕区和弹幕栏的底部留白按大字幕的实测高度算，不再写死 204px；没量到之前和以前一样", () => {
  assert.doesNotMatch(css.replace(/\/\*[\s\S]*?\*\//g, ""), /\b204px\b/, "注释之外不再有写死的 204px");
  const bottom = (padding) => { const m = /(calc\(.*\))$/.exec(padding); assert.ok(m, padding); return m[1]; };
  const hist = bottom(decls(".history").padding);
  const list = bottom(decls(".comment-list").padding);
  for (const [name, value, before] of [[".history", hist, 204], [".comment-list", list, 204]]) {
    assert.match(value, /^calc\(var\(--live-bar-h, [\d.]+px\) \+ var\(--foot-h, 22px\) \+ 8px\)$/, name);
    assert.equal(calcPx(value, {}), before, name + "：没量到之前（变量还没写）是以前的 " + before + "px");
    assert.equal(calcPx(value, { "--live-bar-h": 220 }), 250, name + "：条子 + 底部署名 22 + 空隙 8");
    assert.equal(calcPx(value, { "--live-bar-h": 220, "--foot-h": 54 }), 282,
                 name + "：页脚长成三行（54px）时条子往上挪了 32px，留白跟着多 32px（#143 审查）");
  }
  // 窄屏（≤640px）那一条同样跟着量出来的高度走，没量到之前是以前的 174px
  const narrow = /@media \(max-width: 640px\) \{[\s\S]*?\n  \.history \{ padding: 4px 12px (calc\([^;]+\)); \}/.exec(css);
  assert.ok(narrow, "窄屏的 .history 留白");
  assert.equal(calcPx(narrow[1], {}), 174);
  assert.equal(calcPx(narrow[1], { "--live-bar-h": 120 }), 150);
  assert.equal(calcPx(narrow[1], { "--live-bar-h": 120, "--foot-h": 38 }), 166);
});

// #143 审查：页脚显示一句长提示时长高，以前大字幕条 bottom 写死 22px，页脚多出来的几行盖住条子最下面一行。
// 条子的 bottom、高度上限、两栏的留白现在都按 --foot-h 算（上面几条钉样式表），这一条钉页面怎么写这个变量
test("app.js：用 ResizeObserver 盯着页脚写 --foot-h；换字时也直接量一次（没有 ResizeObserver 的环境）", () => {
  const src = web("app.js");
  assert.match(src, /new ResizeObserver\(function \(\) \{ syncFootHeight\(\); \}\)\.observe\(copyrightEl\)/);
  assert.match(src, /setProperty\("--foot-h", h \? h \+ "px" : ""\)/);
  const html = web("index.html").replace(/<!--[\s\S]*?-->/g, "");
  assert.match(html, /<div class="copyright">[^<]*(<a [^>]*>[^<]*<\/a>)?<span id="app-version"/,
               "量的是 #app-version 的父元素，它就是页脚");
});

test("app.js：用 ResizeObserver 盯着 #live-bar 写 --live-bar-h（字号、内容、窗口宽度一变就量）", () => {
  const src = web("app.js");
  assert.match(src, /new ResizeObserver\(function \(\) \{ syncLiveBarHeight\(\); \}\)/);
  assert.match(src, /liveBarObserver\.observe\(liveBar\)/);
  assert.match(src, /setProperty\("--live-bar-h", h \+ "px"\)/);
  assert.match(src, /if \(!\(h > 0\) \|\| h === liveBarH\) return;/, "值没变、量出来是 0 都不写");
});
