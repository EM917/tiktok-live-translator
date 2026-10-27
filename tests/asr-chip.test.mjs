// 「语音识别不确定」角标（C3，默认关）：web/live-ui.js captionAsrChip 的字，以及 app.js 把它画在
// 历史卡片的 .meta 行和底部大字幕的 #live-meta 行里。中文、英文两种界面各跑一遍。
//
// 服务端只在 settings.json 的 asr_uncertain_chip 打开、而且这一段命中识别风险规则时才给 caption 带
// asr_flag（tests/test_asr_chip.py 钉着）；这里钉的是：没有 asr_flag 的字幕和以前一模一样（没有角标、
// 那一行不多出东西），有 asr_flag 时卡片和大字幕是同一句话、图标是线性三角、说明只写看到的，
// caption_update 不动它，换句、场次分隔会把大字幕上的那个清掉。
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { loadDesktop, hello, caption } from "./desktop-page.mjs";

const TEXT = {
  zh: {
    chip: "语音识别不确定，建议查看原文",
    words: "识别出的字数和有声时长对不上。原文和译文都照识别结果显示，没有删改。",
    conf: "识别置信度偏低。原文和译文都照识别结果显示，没有删改。",
    nan: "这一段没有拿到识别置信度。原文和译文都照识别结果显示，没有删改。",
    all: "识别出的字数和有声时长对不上。识别置信度偏低。识别出的文字有重复。原文和译文都照识别结果显示，没有删改。",
    unknown: "这一段的识别被标记为不确定。原文和译文都照识别结果显示，没有删改。",
  },
  en: {
    chip: "Recognition unclear · check the original",
    words: "The word count didn’t match how long there was sound. The original and translation are shown as recognized, with nothing removed.",
    conf: "Recognition confidence was low. The original and translation are shown as recognized, with nothing removed.",
    nan: "No recognition confidence was reported for this segment. The original and translation are shown as recognized, with nothing removed.",
    all: "The word count didn’t match how long there was sound. Recognition confidence was low. The recognized text repeats. The original and translation are shown as recognized, with nothing removed.",
    unknown: "This segment’s recognition was marked unclear. The original and translation are shown as recognized, with nothing removed.",
  },
};
const CJK = /[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;

// live-ui.js 读 UI_LANG 的时机是 require 时：每种语言起一个子进程算纯函数的结果
function pure(lang, flags) {
  const script = "globalThis.UI_LANG = " + JSON.stringify(lang) + ";" +
    "const U = require(" + JSON.stringify(fileURLToPath(new URL("../web/live-ui.js", import.meta.url))) + ");" +
    "process.stdout.write(JSON.stringify(" + JSON.stringify(flags) + ".map((f) => U.captionAsrChip(f))));";
  return JSON.parse(execFileSync(process.execPath, ["-e", script], { encoding: "utf8" }));
}

const shown = (page, id) => !page.el(id).classList.contains("hidden");
const cardChips = (page) => page.el("history").querySelectorAll(".asr-chip");

for (const lang of ["zh", "en"]) {
  const X = TEXT[lang];
  const name = (s) => s + "（" + lang + "）";

  test(name("纯函数：没有 asr_flag 就没有角标；有就是同一句字，说明按看到的逐句写"), () => {
    const out = pure(lang, [null, {}, { asr_flag: [] }, { asr_flag: "few_words" },
                            { asr_flag: ["few_words"] }, { asr_flag: ["low_conf"] }, { asr_flag: ["nan_lp"] },
                            { asr_flag: ["loop", "low_conf", "few_words"] }, { asr_flag: ["v2_code"] }]);
    for (const empty of out.slice(0, 4)) assert.deepEqual(empty, { text: "", title: "" });
    assert.deepEqual(out[4], { text: X.chip, title: X.words });
    assert.deepEqual(out[5], { text: X.chip, title: X.conf });
    // nan_lp：解码器没给出置信度，不是置信度偏低——只写看到的（第八条）
    assert.deepEqual(out[6], { text: X.chip, title: X.nan });
    assert.deepEqual(out[7], { text: X.chip, title: X.all });
    assert.deepEqual(out[8], { text: X.chip, title: X.unknown }, "认不出的代码照样挂角标，字不变");
    for (const r of out.slice(4)) {
      // 第八条：只写观察；「有声时长」不写成「说话时长」（背景音乐也算有声）
      assert.doesNotMatch(r.title, /说话时长|可能|因为|probably|likely|because|maybe/);
      if (lang === "en") assert.ok(!CJK.test(r.text + r.title), r.title);
    }
  });

  test(name("卡片：带 asr_flag 的字幕在 .meta 行多一个琥珀角标（线性三角图标），别的字幕和以前一样"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "hola a todas"));
    page.push(caption(2, "y ya", { asr_flag: ["few_words"] }));
    const chips = cardChips(page);
    assert.equal(chips.length, 1);
    const chip = chips[0];
    assert.equal(chip.textContent, X.chip);
    assert.equal(chip.title, X.words);
    assert.match(chip.className, /\blang-chip\b/);
    const icon = chip.childNodes[0];
    assert.equal(icon.getAttribute("aria-hidden"), "true");
    assert.equal(icon.childNodes[0].getAttribute("href"), "#i-warn");
    const cards = page.el("history").querySelectorAll(".cap");
    assert.equal(cards[0].querySelectorAll(".asr-chip").length, 0);
    assert.equal(cards[1].querySelectorAll(".asr-chip").length, 1);
    // 译文到了也不动角标（caption_update 不带 asr_flag）
    page.push({ type: "caption_update", id: 2, translated: "然后", translate_state: "ok", quality: 1 });
    assert.equal(cardChips(page).length, 1);
  });

  test(name("大字幕：#live-meta 行露出角标；caption_update 不清它；下一句没有就收起；场次分隔一起清掉"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(1, "hola a todas"));
    assert.equal(shown(page, "live-asr"), false);
    assert.equal(shown(page, "live-meta"), false, "没有角标、没有滞后：这一行平时是收起的");

    // 待译停留（web/app.js LIVE_HOLD_MS）：还在翻译的一句先不上大字幕，等满 4 秒才照以前的样子放原文。
    // 这条用例要的是「这一句在大字幕上」，每句之后拨一下那个 4 秒的计时器
    page.push(caption(2, "y ya", { asr_flag: ["low_conf"] }));
    page.fire(4000);
    assert.equal(shown(page, "live-asr"), true);
    assert.equal(page.el("live-asr").textContent, X.chip);
    assert.equal(page.el("live-asr").title, X.conf);
    assert.equal(shown(page, "live-meta"), true);

    page.push({ type: "caption_update", id: 2, translated: "然后", translate_state: "ok", quality: 1 });
    assert.equal(shown(page, "live-asr"), true, "译文到了，角标还在");

    page.push(caption(3, "bueno chicas"));
    page.fire(4000);
    assert.equal(shown(page, "live-asr"), false);
    assert.equal(page.el("live-asr").textContent, "");
    assert.equal(shown(page, "live-meta"), false);

    page.push(caption(4, "otra", { asr_flag: ["loop"] }));
    page.fire(4000);
    assert.equal(shown(page, "live-asr"), true);
    page.push({ type: "session_break", ts: 1 });
    assert.equal(shown(page, "live-asr"), false);
    assert.equal(page.el("live-asr").textContent, "");
  });

  test(name("回放：重连后回放出来的那条还带着 asr_flag，卡片上的角标跟着回来"), () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(caption(5, "y ya", { asr_flag: ["few_words"], replay: true }));
    assert.equal(cardChips(page).length, 1);
  });

  test(name("报警面板不挂角标：报警消息本来就不带 asr_flag，带了也不画"), () => {
    const page = loadDesktop({ lang });
    hello(page, { alerts_enabled: true });
    page.push({ type: "alert", alert_id: 1, ts: 1, term: "cura", tier: "exact", session: "s1",
                context: "esto cura todo", asr_flag: ["low_conf"] });
    assert.equal(page.el("alert-list").querySelectorAll(".alert-item").length, 1);
    assert.equal(page.el("alert-list").querySelectorAll(".asr-chip").length, 0);
  });
}

// 浮窗 v1 不显示角标：float.js 只挑自己认得的字段，带不带 asr_flag 归并出来的状态和取的字都一样
test("浮窗：带 asr_flag 的字幕和不带的归并出同一个状态、同一份要画的东西", async () => {
  const { createRequire } = await import("node:module");
  const F = createRequire(import.meta.url)("../web/float.js");
  const base = { type: "caption", id: 7, ts: 1, original: "y ya", translated: null,
                 translate_state: "pending", e2e_ms: 2000 };
  const plain = F.floatReduce(F.floatInitialState(), base, 5000);
  const flagged = F.floatReduce(F.floatInitialState(), Object.assign({ asr_flag: ["few_words"] }, base), 5000);
  assert.deepEqual(flagged, plain);
  assert.deepEqual(F.floatCaptionView(flagged), F.floatCaptionView(plain));
});
