globalThis.UI_LANG = "en";   // 必须在 require 之前：web/i18n.js 加载时读一次（node --test 每个文件一个进程）
// 浮窗页的英文（web/float.js、web/float.html）。中文断言在 tests/float.test.mjs。
//
// 两件事：
//   1. 英文措辞：每一句都过三道筛——没有汉字、没有没填的占位符、没有第八条的两级禁用词（EN_LABELS / EN_CAUSAL，
//      与 tests/i18n_rules.py 同一份正则）；术语照 docs/i18n-style.md（captions、comments、Floating Window）；
//   2. 英文页上一个汉字都不剩：页面写到 DOM 里的字（文字节点和它写过的 title / aria-label 等）里，
//      数据区（带 class i18n-data 的字幕和弹幕正文、标 translate="no" 的昵称）可以有中文，别处不行。
//      先拿一个故意写了中文界面文字的节点证明这个扫描真的会报，再扫真页面。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { loadFloat, hello, caption, comment, T } from "./float-page.mjs";
import { FakeEl } from "./desktop-page.mjs";

const require = createRequire(import.meta.url);
const F = require("../web/float.js");

const CJK = /[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;
const EN_LABELS = /\b(age|aged|age[- ]restrict\w*|rate[- ]?limit\w*|throttl\w*|ban(s|ned)?\b(?![- ]terms?)|block(ed|ing|s)?|restrict\w*)\b/i;
const EN_CAUSAL = /\b(because|due to|caused by|the reason|probably|likely|must be|seems to|private|regions?|regional|maybe|perhaps|(?:not|isn[’']t) an? [\w -]*(?:problem|issue))\b/i;

function english(text) {
  assert.equal(typeof text, "string");
  assert.ok(!CJK.test(text), "英文里还有中文：" + text);
  assert.ok(!/\{\w*\}/.test(text), "有没填的占位符：" + text);
  assert.ok(!EN_LABELS.test(text), "英文里有标签词：" + text);
  assert.ok(!EN_CAUSAL.test(text), "英文里有因果词：" + text);
  return text;
}

/** 扫一棵子树：返回 [(位置, 文字)]。数据区（i18n-data 的子树、translate="no" 的子树）里的文字跳过；
 *  元素自己的 title / aria-label / placeholder / alt 照查。 */
function leftovers(el, path = "") {
  const here = path + "/" + el.tagName.toLowerCase() + (el.id ? "#" + el.id : "");
  const out = [];
  for (const name of ["title", "aria-label", "placeholder", "alt"]) {
    const v = el.getAttribute(name);
    if (v != null && CJK.test(v)) out.push([here + "@" + name, v]);
  }
  if (el.cls.includes("i18n-data") || el.getAttribute("translate") === "no") return out;
  for (const node of el.childNodes) {
    if (node.nodeType === 3) {
      if (CJK.test(node.nodeValue)) out.push([here, node.nodeValue]);
    } else if (node.nodeType === 1) {
      out.push(...leftovers(node, here));
    }
  }
  return out;
}

test("语言确实是英文（不然下面全是空转）", () => {
  assert.equal(require("../web/i18n.js").UI_LANG, "en");
});

test("占位、弹幕占位、角标：英文措辞照术语表，且过三道筛", () => {
  const s = F.floatInitialState();
  assert.equal(english(F.floatPlaceholder(s)), "Waiting for the first caption…");
  s.status = "connecting";
  assert.equal(english(F.floatPlaceholder(s)), "Connecting…");
  const view = (extra) => F.floatCommentView(Object.assign({ id: "c", user: "ana", text: "hola", translated: null, state: "" }, extra));
  assert.equal(english(view({ state: "pending" }).zh), "Translating…");
  const chips = [[{ translate_state: "failed" }, "Translation failed"],
                 [{ translate_state: "failed", refused: true }, "Not translated (API refusal)"],
                 [{ translate_state: "failed", not_translation: true }, "Not translated (the result didn’t look like a translation)"],
                 [{ translate_state: "dropped" }, "Skipped (backlog)"]];
  for (const [update, chip] of chips) {
    const st = F.floatInitialState();
    F.floatReduce(st, { type: "caption", id: 1, original: "hola", translated: null, translate_state: "pending" });
    F.floatReduce(st, Object.assign({ type: "caption_update", id: 1, translated: null }, update));
    assert.equal(english(F.floatCaptionView(st).chip), chip);
  }
});

test("扫描自己得先报得出来：一个写了中文界面文字的节点、一个中文的 title 都要被抓到", () => {
  const bad = new FakeEl("div");
  bad.textContent = "漏翻的界面文字";
  assert.equal(leftovers(bad).length, 1);
  const title = new FakeEl("span");
  title.setAttribute("title", "提示");
  assert.equal(leftovers(title).length, 1);
  const data = new FakeEl("div");
  data.className = "i18n-data";
  data.textContent = "这是字幕正文，数据区可以有中文";
  assert.deepEqual(leftovers(data), []);
  const name = new FakeEl("span");
  name.setAttribute("translate", "no");
  name.textContent = "小明";
  assert.deepEqual(leftovers(name), []);
});

test("英文页写出来的每一处：除了数据区，一个汉字都不剩", () => {
  const page = loadFloat({ lang: "en" });
  hello(page, { guard_marked: { u3: { keys: ["u3"], nick: "小明", by: "manual" } } });
  // 数据故意放中文：一条译成简体中文的字幕、中文昵称的观众、中文的弹幕译文——都得落在数据区里
  page.push(caption(1, "Esta crema aclara las manchas"));
  page.push({ type: "caption_update", id: 1, translated: "这款面霜能淡化色斑", translate_state: "ok" });
  page.push(comment("c1", "小明", "¿Cuánto cuesta?", { uid: "u3", uid_ok: true }));
  page.push({ type: "comment_update", id: "c1", translated: "多少钱？", state: "ok" });
  page.push(comment("c2", "rival", "no compren, es una estafa", { uid: "7234567890123456789", uid_ok: true,
    guard: { lvl: "alert", cats: ["accuse_call"], src: "rule", why: ["accuse/call:no compren"], rv: "agree" } }));
  page.push(comment("c3", "lupita", "es falso", { guard: { lvl: "amber", amb: "maybe_question", cats: ["accuse_claim"], src: "rule" } }));
  page.push(comment("c4", "pedro", "buenas"));                                  // 翻译中
  page.push(comment("c5", "ines", "se rompió"));
  page.push({ type: "comment_update", id: "c5", translated: null, state: "failed" });
  assert.deepEqual(leftovers(page.el("f-root")), []);

  // 没译成的字幕、占位、换主播之后
  page.push(caption(2, "no se oye"));
  page.push({ type: "caption_update", id: 2, translated: null, translate_state: "failed", refused: true });
  assert.deepEqual(leftovers(page.el("f-root")), []);
  assert.equal(page.el("f-chip").textContent, "Not translated (API refusal)");
  page.push({ type: "session_break", ts: T + 9999, streamer: "otra" });
  assert.deepEqual(leftovers(page.el("f-root")), []);
  assert.equal(page.el("f-empty").textContent, "Waiting for the first caption…");
});

test("弹幕上的标记是英文：类别胶囊、琥珀理由", () => {
  const page = loadFloat({ lang: "en" });
  hello(page);
  page.push(comment("c2", "rival", "no compren", { guard: { lvl: "alert", cats: ["accuse_call"], src: "rule", why: [], rv: "agree" } }));
  page.push(comment("c3", "lupita", "es falso", { guard: { lvl: "amber", amb: "maybe_question", cats: ["accuse_claim"], src: "rule" } }));
  const [red, amber] = page.comments();
  assert.equal(english(red.querySelector(".guard-pill").textContent), "Possible call to report or not buy");
  assert.equal(english(red.querySelector(".guard-note").textContent), "Model: agrees with rules");
  assert.equal(english(amber.querySelector(".guard-note").textContent), "Short, with no question mark");
});

test("float.html 的静态文字都挂着英文（G5 在 Python 里按同一套规则查整页，这里钉住 id 和 data-en 都在）", async () => {
  const { readFileSync } = await import("node:fs");
  const html = readFileSync(new URL("../web/float.html", import.meta.url), "utf8");
  assert.match(html, /<title data-en="Floating Window · TikTok Live Translator">浮窗 · TikTok 直播同传<\/title>/);
  assert.match(html, /id="f-empty"[^>]*data-en="Waiting for the first caption…"/);
  assert.match(html, /aria-label="弹幕" data-en-aria-label="Comments"/);
});
