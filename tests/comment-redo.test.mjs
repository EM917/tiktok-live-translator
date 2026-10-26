// 弹幕重译完了说一句（2026-10-26 用户要的，R1）：服务端在 redo_state=ok 的 comment_redo 里带上 same 和一句
// 说明——「已重译」或「重译结果和原来一样」（app/comment_redo.py note_done）。页面把它画在那条弹幕下面几秒，
// 「弹幕留意」抽屉里那一行也有；和没重译成的说明走同一套（COMMENT_REDO_NOTE_MS），只差两处：
//   1. 成了不是提醒：不用 .redo-note 的提醒底色（类名多一个 done，颜色先写在 app.js 里）；
//   2. 那条弹幕已经不在弹幕栏里时，成了的那句不退回页脚（没成的照旧退回，不能丢）。
// 中英两种页面都真跑 web/app.js（tests/desktop-page.mjs）。same 怎么算、服务端发哪一句由
// tests/test_comment_redo.py 钉着；这里先核对两边说的是同一句话，再用服务端会发的原样消息驱动页面。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loadDesktop, hello, T } from "./desktop-page.mjs";

const py = readFileSync(new URL("../app/comment_redo.py", import.meta.url), "utf8");

// 服务端按界面语言发的几句（app/comment_redo.py note_done、failure_text）
const SAID = {
  zh: { done: "已重译", same: "重译结果和原来一样",
        failed: "弹幕重译没有成功：Claude Sonnet 5.5 返回 HTTP 500，原译文保持不变" },
  en: { done: "Retranslated", same: "The retranslation is the same as before.",
        failed: "Couldn’t retranslate this comment: Claude Sonnet 5.5 returned HTTP 500. "
                + "The current translation is unchanged." },
};
const BUTTON = {
  zh: { idle: "重译", busy: "重译中…", failed: "重译失败" },
  en: { idle: "Retranslate", busy: "Retranslating…", failed: "Couldn’t retranslate" },
};
const CLAUDE_ENGINE = { type: "engine", engine: "claude", keys: { ANTHROPIC_API_KEY: "…abcd" },
                        active: "claude", active_label: "Claude", note: "" };

function setup(lang, config) {
  const page = loadDesktop({ lang });
  hello(page, config);
  page.push(CLAUDE_ENGINE);
  page.push({ type: "comment", id: "c1", ts: T, user: "Ana", text: "cuanto cuesta", translated: "多少钱", state: "ok" });
  page.push({ type: "comment", id: "c2", ts: T, user: "Bea", text: "hola", translated: "你好", state: "ok" });
  const [item1, item2] = page.el("comment-list").querySelectorAll(".cmt-item");
  return { page, item1, item2 };
}
const notes = (item) => item.querySelectorAll(".redo-note").map((n) => n.textContent);
const shown = (item) => item.querySelector(".cmt-zh").textContent;
const button = (item) => item.querySelector(".cmt-redo");
const footer = (page) => page.el("app-version").textContent;
const redoOk = (id, same, text) => ({ type: "comment_redo", id, redo_state: "ok", same, text });

test("页面上用的几句就是服务端发的那几句（app/comment_redo.py 里中英成对写着）", () => {
  assert.ok(py.includes('L("已重译", "Retranslated")'));
  assert.ok(py.includes('L("重译结果和原来一样", "The retranslation is the same as before.")'));
});

for (const lang of ["zh", "en"]) {
  const said = SAID[lang], btn = BUTTON[lang];

  test(`${lang}：重译成了、译文换了——那条弹幕下面说「已重译」几秒，不是提醒的样子，页脚不动`, () => {
    const { page, item1, item2 } = setup(lang);
    const before = footer(page);
    page.push({ type: "comment_redo", id: "c1", redo_state: "pending" });
    assert.deepEqual([button(item1).textContent, button(item1).disabled], [btn.busy, true]);
    page.push({ type: "comment_update", id: "c1", state: "ok", translated: "这个多少钱" });
    page.push(redoOk("c1", false, said.done));
    assert.equal(shown(item1), "这个多少钱");
    assert.deepEqual(notes(item1), [said.done]);
    const note = item1.querySelector(".redo-note");
    assert.ok(note.cls.includes("done"), "成了的那句有自己的类名");
    assert.equal(note.getAttribute("role"), "status", "读屏会念出来");
    assert.deepEqual([note.style.color, note.style.background], ["var(--label-2)", "none"], "不用提醒的底色");
    assert.deepEqual([button(item1).textContent, button(item1).disabled], [btn.idle, false], "按钮回到原样");
    assert.deepEqual(notes(item2), [], "别的弹幕不受影响");
    assert.equal(footer(page), before, "页脚不动");
    page.fire(4000);                                   // 「重译失败」才有的 4 秒复原：成了的说明不跟着收
    assert.deepEqual(notes(item1), [said.done]);
    page.fire(7000);
    assert.deepEqual(notes(item1), [], "几秒后收起");
  });

  test(`${lang}：重译结果和原来一样——译文不动，说明照样出来`, () => {
    const { page, item1 } = setup(lang);
    page.push({ type: "comment_redo", id: "c1", redo_state: "pending" });
    page.push({ type: "comment_update", id: "c1", state: "ok", translated: "多少钱" });
    page.push(redoOk("c1", true, said.same));
    assert.equal(shown(item1), "多少钱", "屏幕上的译文没变");
    assert.deepEqual(notes(item1), [said.same]);
    assert.ok(item1.querySelector(".redo-note").cls.includes("done"));
    assert.deepEqual([button(item1).textContent, button(item1).disabled], [btn.idle, false]);
    page.fire(7000);
    assert.deepEqual(notes(item1), []);
  });

  test(`${lang}：没重译成——照旧是提醒的样子，按钮写「重译失败」4 秒；再点、再成了时换成新的一句`, () => {
    const { page, item1 } = setup(lang);
    page.push({ type: "comment_redo", id: "c1", redo_state: "pending" });
    page.push({ type: "comment_redo", id: "c1", redo_state: "failed", text: said.failed });
    assert.deepEqual(notes(item1), [said.failed]);
    const note = item1.querySelector(".redo-note");
    assert.ok(!note.cls.includes("done"));
    assert.equal(note.style.background, undefined, "没成的说明用 style.css 里的提醒底色");
    assert.equal(button(item1).textContent, btn.failed);
    assert.equal(shown(item1), "多少钱", "原译文不动");
    page.fire(4000);
    assert.equal(button(item1).textContent, btn.idle);
    // 又点了一次：上一次的说明收起；这次成了，只剩成了的那一句
    page.push({ type: "comment_redo", id: "c1", redo_state: "pending" });
    assert.deepEqual(notes(item1), []);
    page.push({ type: "comment_update", id: "c1", state: "ok", translated: "这个多少钱" });
    page.push(redoOk("c1", false, said.done));
    assert.deepEqual(notes(item1), [said.done]);
    page.fire(7000);
    assert.deepEqual(notes(item1), []);
  });

  test(`${lang}：那条弹幕已经不在弹幕栏里——成了的那句不退回页脚，没成的照旧退回`, () => {
    const { page } = setup(lang);
    const before = footer(page);
    page.push({ type: "comment_redo", id: "gone-1", redo_state: "pending" });
    page.push({ type: "comment_update", id: "gone-1", state: "ok", translated: "这个多少钱" });
    page.push(redoOk("gone-1", false, said.done));
    assert.equal(footer(page), before, "页脚上一句不知道指哪条的「已重译」没有用");
    page.push(redoOk("gone-1", true, said.same));
    assert.equal(footer(page), before);
    page.push({ type: "comment_redo", id: "gone-2", redo_state: "failed", text: said.failed });
    assert.ok(footer(page).includes(said.failed), "没成的说明不能丢");
  });

  test(`${lang}：「弹幕留意」抽屉那一行也显示成了的那句，几秒后收起`, () => {
    const uid = "7234567890123456789";
    const ev = { id: "g1", kind: "comment", cat: "threat", lvl: "alert", amb: null, n: 1, accounts: [],
                 window_sec: 60, first_ts: T + 5, ts: T + 5, rv: null,
                 sample: { cid: "c9", user: "Ana", text: "te voy a matar", uid, uid_src: "id", handle: "ana_demo" } };
    const { page } = setup(lang, {
      guard_state: { enabled: true, backend: "none", status: "rules", since: 1, trip: null,
                     rules: { count: 510, warnings: 0 }, n_marked: 0, unreviewed: 0, weak: false,
                     streamer: "luna_demo" },
      guard_events: {}, guard_marked: {},
    });
    page.push({ type: "comment", id: "c9", user: "Ana", text: "te voy a matar", ts: T + 5, translated: null,
                state: "failed", uid, uid_ok: true, handle: "ana_demo",
                guard: { lvl: "alert", amb: null, cats: ["threat"], sub: "threat", tier: "red", src: "rule",
                         rv: null, ev: "g1", why: ["insult/threat:te voy a matar"] } });
    page.advance(1500);
    page.push({ type: "guard_event", ev });
    page.el("guard-state").click();
    const row = () => page.el("guard-events").querySelectorAll(".guard-ev-row")
      .find((r) => r.getAttribute("data-ev") === "g1");
    const said9 = () => row().querySelector(".gev-redo-note");
    page.push({ type: "comment_redo", id: "c9", redo_state: "pending" });
    page.push({ type: "comment_update", id: "c9", state: "ok", translated: "我要杀了你" });
    page.push(redoOk("c9", false, said.done));
    assert.equal(said9().textContent, said.done);
    assert.equal(said9().getAttribute("role"), "status");
    page.fire(7000);
    assert.equal(said9(), null);
  });
}
