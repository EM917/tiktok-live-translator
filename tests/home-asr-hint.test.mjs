// 首页输入框下面那一行「语音模型还没下载（约 3.1 GB）[现在下载]」（#asr-model-hint，2026-10-29 用户选的 launch-2 A）。
//
// 自检「语音识别」一行的「现在下载」（tests/asr-fetch.test.mjs）收在「设置 > 启动自检」里，只有提醒时整行折叠成
// 「通过 · N 项提醒」，新装机的中控开播前看不到，第一次点「开始翻译」才开始下 3.1 GB。首页这一行跟自检那一行
// 看同一个东西（这一行带不带 action: "download_asr_model"），页面（web/app.js renderAsrHint）：
//   1. 带 action 才出现，写上多大（GB 一位小数，不到 1 GB 写 MB，不知道多大就不写括号）；不带就藏起来；
//   2. 按钮发同一条 {type: "download_asr_model"}，点了马上变成灰的「正在下载…」；自检里那颗跟着一起变，反过来也一样；
//      和本地服务断开时什么都没发出去，按钮照旧；
//   3. 后端说 busy（下载中重连、重放的 hello）时是灰的「正在下载…」；下完后端重发那一行（没有 action），这一行
//      自己消失；没下成（重发、不再 busy）按钮回来，可以再点；
//   4. 连接中、直播中整个开始面板本来就藏着；断开时置灰；待机、已结束、出错时可以点。
// 页面是 node:vm 里真跑的 web/app.js（tests/desktop-page.mjs），中文、英文两种界面各跑一遍。
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadDesktop, web } from "./desktop-page.mjs";

const CJK = /[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;
const TEXT = {
  zh: { gb: "语音模型还没下载（约 3.1 GB）", mb: "语音模型还没下载（约 484 MB）", plain: "语音模型还没下载",
        btn: "现在下载", busy: "正在下载…" },
  en: { gb: "The speech model isn’t downloaded yet (about 3.1 GB)",
        mb: "The speech model isn’t downloaded yet (about 484 MB)",
        plain: "The speech model isn’t downloaded yet", btn: "Download Now", busy: "Downloading…" },
};

function asrRow(extra) {
  return Object.assign({ name: "Speech Recognition", level: "warn",
                         detail: "mlx + large-v3 (model not downloaded yet, about 3.1 GB)",
                         fix: "Click Download Now here before you go live." }, extra);
}
const ACTION = { action: "download_asr_model", size_mb: 3100, busy: false };
const DONE = { name: "Speech Recognition", level: "ok", detail: "mlx + large-v3 (model downloaded)", fix: "" };

function selfcheck(checks) {
  const warn = checks.filter((c) => c.level === "warn").length;
  return { checks, summary: { total: checks.length, fail: 0, warn, ok: checks.length - warn } };
}

function idlePage(lang, checks, state = "idle") {
  const page = loadDesktop({ lang });
  const config = {
    status: { state, detail: "" }, room_url: null, alerts_enabled: false,
    brand_options: [], brands: {}, active_brand: null, recent_rooms: [],
  };
  if (checks) config.selfcheck = selfcheck(checks);
  page.push({ type: "hello", config });
  return page;
}
const hint = (page) => page.el("asr-model-hint");
const shown = (page) => !hint(page).classList.contains("hidden");
const text = (page) => page.el("asr-model-hint-text").textContent;
const btn = (page) => page.el("asr-model-hint-btn");
const scBtn = (page) => page.el("sc-list").querySelector(".sc-action");
const fetches = (page) => page.sent.filter((m) => m.type === "download_asr_model");
const status = (page, state) => page.push({ type: "status", state, detail: "" });
const pushChecks = (page, checks) => page.push(Object.assign({ type: "selfcheck" }, selfcheck(checks)));

test("页面源码：这一行在输入框下面、开始面板里，起始是藏着的；按钮的中英文成对", () => {
  const html = web("index.html").replace(/<!--[\s\S]*?-->/g, "");
  const panel = html.indexOf('id="start-panel"');
  const input = html.indexOf('id="room-input"');
  const row = html.indexOf('id="asr-model-hint"');
  const options = html.indexOf('class="sp-options"');
  assert.ok(panel < input && input < row && row < options, "要在输入框之后、常用选项之前");
  assert.match(html, /<div id="asr-model-hint" class="sp-model-hint hidden"/);
  assert.match(html, /<button id="asr-model-hint-btn"[^>]*data-en="Download Now"[^>]*>现在下载<\/button>/);
  assert.match(html, /id="asr-model-hint-btn"[^>]*aria-describedby="asr-model-hint-text"/);
});

for (const lang of ["zh", "en"]) {
  const X = TEXT[lang];
  const name = (s) => s + "（" + lang + "）";

  test(name("模型没下全：首页这一行出现，写上多大，按钮可以点"), () => {
    const page = idlePage(lang, [asrRow(ACTION), { name: "Storage", level: "ok", detail: "80 GB", fix: "" }]);
    assert.equal(shown(page), true);
    assert.equal(text(page), X.gb);
    assert.equal(btn(page).textContent, X.btn);
    assert.equal(btn(page).disabled, false);
    if (lang === "en") assert.doesNotMatch(text(page) + btn(page).textContent, CJK);

    pushChecks(page, [asrRow(Object.assign({}, ACTION, { size_mb: 484 }))]);
    assert.equal(text(page), X.mb);
    pushChecks(page, [asrRow(Object.assign({}, ACTION, { size_mb: null }))]);
    assert.equal(text(page), X.plain);
  });

  test(name("模型已下载、自检还没出结果、别的提醒：这一行都不出现"), () => {
    assert.equal(shown(idlePage(lang, [DONE])), false);
    assert.equal(shown(idlePage(lang, null)), false);
    // 苹果芯片却在用 CPU 这类提醒不带 action（要换的是后端，现在下的用不上）：首页也不提
    const cpu = asrRow({ detail: "This Mac supports GPU acceleration, but recognition is running on the CPU." });
    assert.equal(shown(idlePage(lang, [cpu])), false);
  });

  test(name("点了发 download_asr_model，两颗按钮一起变成灰的「正在下载…」"), () => {
    const page = idlePage(lang, [asrRow(ACTION)]);
    btn(page).click();
    assert.deepEqual(fetches(page), [{ type: "download_asr_model" }]);
    for (const b of [btn(page), scBtn(page)]) {
      assert.equal(b.textContent, X.busy);
      assert.equal(b.disabled, true);
    }
    btn(page).click();
    assert.equal(fetches(page).length, 1, "灰了就点不出第二条");
    assert.equal(text(page), X.gb, "下完之前这句话照旧（还没下载完）");
  });

  test(name("点的是自检里那颗：首页这颗也跟着变灰"), () => {
    const page = idlePage(lang, [asrRow(ACTION)]);
    scBtn(page).click();
    assert.equal(fetches(page).length, 1);
    assert.equal(btn(page).textContent, X.busy);
    assert.equal(btn(page).disabled, true);
  });

  test(name("和本地服务断开：点了什么都没发出去，按钮照旧；断开期间是灰的"), () => {
    const page = idlePage(lang, [asrRow(ACTION)]);
    page.disconnect();
    btn(page).click();
    assert.equal(fetches(page).length, 0);
    assert.equal(btn(page).textContent, X.btn);
    page.connect();
    status(page, "offline");
    assert.equal(shown(page), true, "断开不改布局，这一行还在");
    assert.equal(btn(page).disabled, true);
    status(page, "idle");
    assert.equal(btn(page).disabled, false);
  });

  test(name("后端说正在下载：灰的「正在下载…」；下完这一行消失；没下成按钮回来"), () => {
    const page = idlePage(lang, [asrRow(Object.assign({}, ACTION, { busy: true }))]);
    assert.equal(shown(page), true);
    assert.equal(btn(page).textContent, X.busy);
    assert.equal(btn(page).disabled, true);
    status(page, "idle");
    assert.equal(btn(page).disabled, true);

    pushChecks(page, [DONE]);
    assert.equal(shown(page), false);

    const again = idlePage(lang, [asrRow(ACTION)]);
    btn(again).click();
    pushChecks(again, [asrRow(ACTION)]);           // 下载失败、磁盘不够：后端重发这一行，不再 busy
    assert.equal(btn(again).textContent, X.btn);
    assert.equal(btn(again).disabled, false);
  });

  test(name("连接中、直播中开始面板整个藏着；待机、已结束、出错时这一行在、可以点"), () => {
    const page = idlePage(lang, [asrRow(ACTION)]);
    for (const state of ["connecting", "live"]) {
      status(page, state);
      assert.equal(page.el("start-panel").classList.contains("hidden"), true, state);
      assert.equal(btn(page).disabled, true, state);
    }
    for (const state of ["ended", "error", "idle"]) {
      status(page, state);
      assert.equal(page.el("start-panel").classList.contains("hidden"), false, state);
      assert.equal(shown(page), true, state);
      assert.equal(btn(page).disabled, false, state);
    }
  });
}
