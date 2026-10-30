// 自检「语音识别」一行的「现在下载」（2026-10-29）：待机时提前下载识别模型，不用等第一次开播。
//
// 后端只在模型没下全时给这一行带 action: "download_asr_model"（app/selfcheck.py check_asr），连同
// size_mb（按钮上写多大）和 busy（正在下载）。页面（web/app.js renderSelfcheck）：
//   1. 带 action 才画按钮，大小按 GB 写一位小数（不到 1 GB 写 MB，不知道多大就不写）；
//   2. 点了发 {type: "download_asr_model"}，按钮马上变成「正在下载…」并置灰；和本地服务断开时什么都没发出去，按钮照旧；
//   3. 连接中、直播中、断开时置灰，回到待机再亮；后端说 busy 时一直是灰的「正在下载…」；
//   4. 下完后端重发这一行（没有 action），按钮跟着没了。
// 页面是 node:vm 里真跑的 web/app.js（tests/desktop-page.mjs），中文、英文两种界面各跑一遍。
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadDesktop } from "./desktop-page.mjs";

const CJK = /[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;
const TEXT = {
  zh: { gb: "现在下载（约 3.1 GB）", mb: "现在下载（约 484 MB）", plain: "现在下载", busy: "正在下载…" },
  en: { gb: "Download Now (about 3.1 GB)", mb: "Download Now (about 484 MB)", plain: "Download Now",
        busy: "Downloading…" },
};

function asrRow(extra) {
  return Object.assign({ name: "Speech Recognition", level: "warn",
                         detail: "mlx + large-v3 (model not downloaded yet, about 3.1 GB)",
                         fix: "Click Download Now here before you go live." }, extra);
}
const ACTION = { action: "download_asr_model", size_mb: 3100, busy: false };

function idlePage(lang, checks) {
  const page = loadDesktop({ lang });
  page.push({ type: "hello", config: {
    status: { state: "idle", detail: "" }, room_url: null, alerts_enabled: false,
    brand_options: [], brands: {}, active_brand: null, recent_rooms: [],
    selfcheck: { checks, summary: { total: checks.length, fail: 0, warn: 1, ok: checks.length - 1 } },
  } });
  return page;
}
const buttons = (page) => page.el("sc-list").querySelectorAll(".sc-action");
const status = (page, state) => page.push({ type: "status", state, detail: "" });

for (const lang of ["zh", "en"]) {
  const X = TEXT[lang];
  const name = (s) => s + "（" + lang + "）";

  test(name("只有带 action 的那一行画「现在下载」，写上多大"), () => {
    const page = idlePage(lang, [asrRow(ACTION), { name: "Storage", level: "ok", detail: "80 GB", fix: "" }]);
    const [btn, ...rest] = buttons(page);
    assert.equal(rest.length, 0);
    assert.equal(btn.textContent, X.gb);
    assert.equal(btn.disabled, false);
    if (lang === "en") assert.doesNotMatch(btn.textContent, CJK);

    page.push({ type: "selfcheck", checks: [asrRow(Object.assign({}, ACTION, { size_mb: 484 }))],
                summary: { total: 1, fail: 0, warn: 1, ok: 0 } });
    assert.equal(buttons(page)[0].textContent, X.mb);
    page.push({ type: "selfcheck", checks: [asrRow(Object.assign({}, ACTION, { size_mb: null }))],
                summary: { total: 1, fail: 0, warn: 1, ok: 0 } });
    assert.equal(buttons(page)[0].textContent, X.plain);
  });

  test(name("点了发 download_asr_model，按钮马上变成「正在下载…」并置灰"), () => {
    const page = idlePage(lang, [asrRow(ACTION)]);
    buttons(page)[0].click();
    assert.deepEqual(page.sent.filter((m) => m.type === "download_asr_model"), [{ type: "download_asr_model" }]);
    const btn = buttons(page)[0];
    assert.equal(btn.textContent, X.busy);
    assert.equal(btn.disabled, true);
  });

  test(name("和本地服务断开时点了什么都没发出去，按钮照旧"), () => {
    const page = idlePage(lang, [asrRow(ACTION)]);
    page.disconnect();
    buttons(page)[0].click();
    assert.equal(page.sent.filter((m) => m.type === "download_asr_model").length, 0);
    assert.equal(buttons(page)[0].textContent, X.gb);
  });

  test(name("连接中、直播中、断开时置灰，回到待机再亮"), () => {
    const page = idlePage(lang, [asrRow(ACTION)]);
    for (const state of ["connecting", "live", "offline"]) {
      status(page, state);
      assert.equal(buttons(page)[0].disabled, true, state);
      buttons(page)[0].click();
    }
    assert.equal(page.sent.filter((m) => m.type === "download_asr_model").length, 0);
    for (const state of ["idle", "ended", "error"]) {
      status(page, state);
      assert.equal(buttons(page)[0].disabled, false, state);
    }
  });

  test(name("后端说正在下载：灰的「正在下载…」，回到待机也不亮；下完这一行没有 action，按钮没了"), () => {
    const page = idlePage(lang, [asrRow(Object.assign({}, ACTION, { busy: true }))]);
    const btn = buttons(page)[0];
    assert.equal(btn.textContent, X.busy);
    assert.equal(btn.disabled, true);
    status(page, "idle");
    assert.equal(buttons(page)[0].disabled, true);

    page.push({ type: "selfcheck", checks: [{ name: "Speech Recognition", level: "ok",
                                              detail: "mlx + large-v3 (model downloaded)", fix: "" }],
                summary: { total: 1, fail: 0, warn: 0, ok: 1 } });
    assert.equal(buttons(page).length, 0);
  });

  test(name("下载失败后那一行重发（不再 busy）：按钮回来，可以再点"), () => {
    const page = idlePage(lang, [asrRow(ACTION)]);
    buttons(page)[0].click();
    page.push({ type: "selfcheck", checks: [asrRow(ACTION)], summary: { total: 1, fail: 0, warn: 1, ok: 0 } });
    const btn = buttons(page)[0];
    assert.equal(btn.textContent, X.gb);
    assert.equal(btn.disabled, false);
  });
}
