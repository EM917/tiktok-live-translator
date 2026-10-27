// 弹幕重译在途时被收尾取消（停止、换主播、下播自然结束）：服务端补发一条 comment_redo(redo_state=cancelled)，
// 不带说明（app/pipeline.py _retranslate_comment，b5-recog-input 第 1 轮审查）。以前它只记审计、不发终态，
// 最后一个状态停在 pending，按钮一直灰着写「重译中…」，点不了第二次，要等页面重连或刷新才恢复。
// 这里钉页面这一侧：web/app.js receiveCommentRedo 把 pending、failed 以外的取值都当成复位——收到 cancelled，
// 按钮回到「重译」、能再点、不留说明。服务端什么时候发、发什么由 tests/test_comment_redo.py 钉着。
// 中英两种页面都真跑 web/app.js（tests/desktop-page.mjs）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadDesktop, hello, T } from "./desktop-page.mjs";

const BUTTON = {
  zh: { idle: "重译", busy: "重译中…" },
  en: { idle: "Retranslate", busy: "Retranslating…" },
};
const CLAUDE_ENGINE = { type: "engine", engine: "claude", keys: { ANTHROPIC_API_KEY: "…abcd" },
                        active: "claude", active_label: "Claude", note: "" };

for (const lang of ["zh", "en"]) {
  const btn = BUTTON[lang];

  test(`${lang}：在途的弹幕重译被收尾取消——按钮回到「重译」、能再点，不留说明，别的弹幕不受影响`, () => {
    const page = loadDesktop({ lang });
    hello(page);
    page.push(CLAUDE_ENGINE);
    page.push({ type: "comment", id: "c1", ts: T, user: "Ana", text: "cuanto cuesta", translated: "多少钱", state: "ok" });
    page.push({ type: "comment", id: "c2", ts: T, user: "Bea", text: "hola", translated: "你好", state: "ok" });
    const [item1, item2] = page.el("comment-list").querySelectorAll(".cmt-item");
    const button = (item) => item.querySelector(".cmt-redo");
    const notes = (item) => item.querySelectorAll(".redo-note").map((n) => n.textContent);

    page.push({ type: "comment_redo", id: "c1", redo_state: "pending" });
    page.push({ type: "comment_redo", id: "c2", redo_state: "pending" });
    assert.deepEqual([button(item1).textContent, button(item1).disabled], [btn.busy, true]);
    assert.ok(item1.cls.includes("redoing"));

    page.push({ type: "comment_redo", id: "c1", redo_state: "cancelled" });
    assert.deepEqual([button(item1).textContent, button(item1).disabled], [btn.idle, false], "按钮回到原样");
    assert.ok(!item1.cls.includes("redoing") && !item1.cls.includes("redo-failed"));
    assert.deepEqual(notes(item1), [], "取消不带说明，也不留说明");
    assert.equal(item1.querySelector(".cmt-zh").textContent, "多少钱", "译文不动");
    assert.deepEqual([button(item2).textContent, button(item2).disabled], [btn.busy, true], "别的弹幕不受影响");

    const before = page.sent.filter((m) => m.type === "retranslate_comment").length;
    button(item1).click();
    assert.deepEqual(page.sent.filter((m) => m.type === "retranslate_comment").slice(before),
                     [{ type: "retranslate_comment", id: "c1" }], "能再点一次");
  });
}
