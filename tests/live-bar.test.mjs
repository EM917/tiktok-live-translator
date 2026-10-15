// 底部大字幕的翻译状态、「约 N 秒前」，页脚统计行的顺序，字幕字号滑块（2026-10-15）。
//
// 页面是 node:vm 里真跑的 web/app.js（tests/desktop-page.mjs），中文、英文两种界面各跑一遍：
//   1. 大字幕下面的角标：译文没到、没译成的每一种结局都留着原文，并且和历史卡片上是同一句话；
//   2. 「约 N 秒前」：按这一段音频结束的时刻（ts − e2e_ms）算，超过 15 秒才出现，每秒涨一次，
//      新的一句重置，缺 e2e_ms 就不写；健康的直播（上屏 2～3 秒、下一句八九秒后来）这一行始终不出现；
//   3. 页脚：字幕等待排第一，违禁词报警延迟紧跟其后、一个字没少（CLAUDE.md 第六条）；
//   4. 滑块：一个值带大字幕、历史卡片、弹幕栏三处；松手存进服务端，localStorage 只是退路。
// 纯函数（角标取字、年龄文案、统计行各段、字号比例）在 tests/live-ui.test.mjs / live-ui.en.test.mjs。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { loadDesktop, hello, caption, web, T } from "./desktop-page.mjs";

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
const cardTrans = (page) => page.el("history").querySelectorAll(".trans").map((c) => c.textContent)[0];

for (const lang of ["zh", "en"]) {
  const X = TEXT[lang];
  const L = (name) => name + "（" + lang + "）";

  // ---- 1. 翻译状态角标 ----------------------------------------------------------------------

  test(L("大字幕：译文还在路上时只有原文、不画角标（每句都要走的几秒，不闪）；译文到了译文上大字、原文退到小字"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "hola a todas"));
    assert.deepEqual([bar(page).big, bar(page).small, bar(page).chip], ["hola a todas", null, null]);
    assert.equal(cardTrans(page), X.pending, "历史卡片上有「翻译中…」");

    page.push({ type: "caption_update", id: 1, translated: "大家好", translate_state: "ok", quality: 1 });
    assert.deepEqual([bar(page).big, bar(page).small, bar(page).chip], ["大家好", "hola a todas", null]);
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
    page.push(caption(1, "primera"));
    page.push(caption(2, "segunda"));
    page.push({ type: "caption_update", id: 1, translated: null, translate_state: "failed" });   // 旧的一句
    assert.deepEqual([bar(page).big, bar(page).chip], ["segunda", null]);
    assert.deepEqual(cardChips(page), [X.failed, ""]);
    page.push({ type: "caption_update", id: 2, translated: null, translate_state: "failed" });
    assert.equal(bar(page).chip, X.failed);
    page.push(caption(3, "tercera"));                                                           // 新的一句：角标重新来过
    assert.equal(bar(page).chip, null);
  });

  test(L("角标是界面文字：不带 i18n-data（要查漏翻），中文界面不夹英文、英文界面没有汉字"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "x"));
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
    page.push(caption(1, "hola", { ts: T, e2e_ms: 2300 }));
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false], "刚上屏：没有数字，也没有空着的一行");
    for (const ms of [2000, 3000, 3000, 2000, 1000, 1000]) {            // 累计 12 秒，音频结束后 14.3 秒
      page.advance(ms);
      page.tick(1000);
      assert.equal(bar(page).lag, null, "已经 " + (page.now() / 1000 - T + 2.3) + " 秒");
    }
    page.push(caption(2, "adiós", { ts: T + 12, e2e_ms: 2500 }));        // 下一句来了
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false]);
  });

  test(L("滞后：从这一段音频结束算起，满 15 秒才出现，之后每秒涨一次，一分钟以上改写分钟"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "hola", { ts: T, e2e_ms: 2300 }));              // 音频在 2.3 秒前结束
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
    page.push(caption(1, "atrasado", LATE));
    assert.deepEqual([bar(page).lag, bar(page).meta], [X.sec(20), true]);
    page.push(caption(2, "al día", { e2e_ms: 2400 }));                  // 追上了：新的一句，数字收起来
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false]);
  });

  test(L("滞后：不依赖 segment_ms——老版本、新版本服务端算出来一样（只看 ts 和 e2e_ms）"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "sin segmento", LATE));
    assert.equal(bar(page).lag, X.sec(20));
    page.push(caption(2, "con segmento", Object.assign({ segment_ms: 7000 }, LATE)));
    assert.equal(bar(page).lag, X.sec(20), "带了也不多算一个片段");
  });

  test(L("滞后：译文回来不重置它（它算的是这一句的声音什么时候结束的）；新的一句才重置"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "hola", { ts: T, e2e_ms: 2000 }));
    page.advance(14000);                                                // 音频结束后 16 秒
    page.push({ type: "caption_update", id: 1, translated: "你好", translate_state: "ok", quality: 1 });
    page.tick(1000);
    assert.equal(bar(page).lag, X.sec(16));
    page.advance(6000);
    page.push(caption(2, "adiós", { ts: T + 20, e2e_ms: 2400 }));       // 下一句，正常
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false], "新的一句：重新从 2.4 秒数起，低于 15 秒不写");
  });

  test(L("滞后：本机时钟拨回（负数）和离谱的大数不写，同样的数据再老 20 秒就写"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "no", { ts: T + 100, e2e_ms: 400 }));         // ts 在「未来」
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false], "负数不写，整行也收起来");
    page.push(caption(2, "ok", { ts: T - 8 * 3600, e2e_ms: 400 }));    // 8 小时前
    assert.equal(bar(page).lag, null);
    page.push(caption(3, "sí", { ts: T - 20, e2e_ms: 400 }));          // 同样的字段，只是 20 秒前
    assert.equal(bar(page).lag, X.sec(20));
  });

  test(L("滞后：服务端没给 e2e（演示模式、老版本）就不写，不拿残缺的数冒充"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    const demo = caption(1, "demo line");
    delete demo.e2e_ms;
    page.push(demo);
    assert.deepEqual([bar(page).lag, bar(page).meta], [null, false]);
    page.push(caption(2, "sin e2e", { ts: T - 60, e2e_ms: undefined }));
    assert.equal(bar(page).lag, null, "再老也不写：没有 e2e 就不知道音频什么时候结束");
    page.push(caption(3, "nulo", { ts: T - 60, e2e_ms: null }));
    assert.equal(bar(page).lag, null, "null 不当 0");
    page.push(caption(4, "completo", { ts: T - 60, e2e_ms: 2300 }));
    assert.equal(bar(page).lag, X.min(1), "62 秒前");
  });

  test(L("滞后：和角标同在一行；只有一样也行；整行没内容时收起"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "no se oye", LATE));
    assert.deepEqual(bar(page), { big: "no se oye", small: null, chip: null, lag: X.sec(20), meta: true }, "只有滞后");
    page.push({ type: "caption_update", id: 1, translated: null, translate_state: "failed", refused: true });
    assert.deepEqual(bar(page), { big: "no se oye", small: null, chip: X.refused, lag: X.sec(20), meta: true }, "角标 + 滞后");
    page.push({ type: "caption_update", id: 1, translated: "听不清", translate_state: "ok", quality: 2 });
    assert.deepEqual([bar(page).chip, bar(page).lag, bar(page).meta], [null, X.sec(20), true]);
    page.push(caption(2, "sin datos"));
    assert.deepEqual([bar(page).chip, bar(page).lag, bar(page).meta], [null, null, false], "什么都没有：整行收起");
    page.push(caption(3, "falla", { translate_state: "failed" }));
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
