// 弹幕作者旁的关系标签（web/app.js relTags）：2026-10-21 起不显示「互关 / Mutual」。
//
// 用户反映有的标了「互关」的观众其实不是互关；这个标记是 TikTok 随每条弹幕发的
// （app/comment_source.py _REL_FLAGS），本机核实不了。用户定：带 mutual 的只标一个「粉丝 / Follower」
// （互关里观众那一侧一定关注了主播），「送过礼」「订阅」不变；抓取端照旧收 mutual（字段统计、审计不动），
// 后端那一半由 tests/test_comment_source.py、tests/test_comments.py 钉着。
//
// 桌面页在 node:vm 里真跑（脚手架见 tests/desktop-dom.mjs），中文页、英文页各走一遍；
// 浮窗（tests/float-page.mjs）和手机页本来就不画关系标签，这里一并钉住。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { runDesktop, leftoverChinese } from "./desktop-dom.mjs";
import { loadFloat, hello as floatHello, comment as floatComment } from "./float-page.mjs";

const T = 1759000000;
const ROOM = "https://www.tiktok.com/@luna_demo/live";

const TEXT = {
  zh: {
    follower: ["粉丝", "关注了主播"],
    gifter: ["送过礼", "给主播送过礼物"],
    subscriber: ["订阅", "订阅了主播"],
    mutual: /互关|互相关注/,
  },
  en: {
    follower: ["Follower", "Follows the streamer"],
    gifter: ["Gifter", "Has sent the streamer a gift"],
    subscriber: ["Subscriber", "Subscribes to the streamer"],
    mutual: /mutual|follows back/i,
  },
};

function hello(page) {
  page.push({ type: "hello", config: { status: { state: "live", detail: "" }, room_url: ROOM, version: "1.2.3" } });
}

let seq = 0;
// 推一条带 rel 的弹幕，返回它头一行里的关系标签：[[字, 悬停说明, 关系名], …]
function chipsFor(page, rel) {
  const id = "c" + (++seq);
  page.push({ type: "comment", id, ts: T + seq, user: "ana", text: "hola", state: "same", rel });
  const item = page.el("comment-list").querySelectorAll(".cmt-item").find((el) => el.dataset.cmtId === id);
  assert.ok(item, "弹幕 " + id + " 没画出来");
  return item.querySelector(".cmt-head").querySelectorAll(".cmt-rel").map((chip) => {
    const kinds = chip.cls.filter((c) => c.startsWith("cmt-rel-"));
    assert.equal(kinds.length, 1, "每个标签只带一个 cmt-rel-* 类：" + chip.className);
    return [chip.textContent, chip.title, kinds[0].slice("cmt-rel-".length)];
  });
}

for (const lang of ["zh", "en"]) {
  const X = TEXT[lang];
  const L = (name) => name + "（" + lang + "）";
  const chip = (kind) => [X[kind][0], X[kind][1], kind];

  test(L("只带 mutual 的弹幕：画一个「粉丝」，不画「互关」"), () => {
    const page = runDesktop(lang);
    hello(page);
    assert.deepEqual(chipsFor(page, ["mutual"]), [chip("follower")]);
  });

  test(L("follower 和 mutual 都带（哪个在前都一样）：只画一个「粉丝」"), () => {
    const page = runDesktop(lang);
    hello(page);
    assert.deepEqual(chipsFor(page, ["follower", "mutual"]), [chip("follower")]);
    assert.deepEqual(chipsFor(page, ["mutual", "follower"]), [chip("follower")]);
    assert.deepEqual(chipsFor(page, ["follower"]), [chip("follower")], "只有 follower 的照旧");
  });

  test(L("「送过礼」「订阅」不变：单独带、和 mutual 一起带都照画"), () => {
    const page = runDesktop(lang);
    hello(page);
    assert.deepEqual(chipsFor(page, ["gifter"]), [chip("gifter")]);
    assert.deepEqual(chipsFor(page, ["subscriber"]), [chip("subscriber")]);
    assert.deepEqual(chipsFor(page, ["follower", "mutual", "gifter", "subscriber"]),
                     [chip("follower"), chip("gifter"), chip("subscriber")]);
    assert.deepEqual(chipsFor(page, ["mutual", "gifter"]), [chip("follower"), chip("gifter")]);
  });

  test(L("页面上哪儿都没有「互关」：字、悬停说明、cmt-rel-mutual 类都不出现"), () => {
    const page = runDesktop(lang);
    hello(page);
    for (const rel of [["mutual"], ["follower", "mutual"], ["mutual", "subscriber"], ["boss", "mutual"]]) {
      for (const [text, title, kind] of chipsFor(page, rel)) {
        assert.doesNotMatch(text, X.mutual);
        assert.doesNotMatch(title, X.mutual);
        assert.notEqual(kind, "mutual");
      }
    }
    assert.equal(page.el("comment-list").querySelectorAll(".cmt-rel-mutual").length, 0);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(L("浮窗本来就不画关系标签：带 mutual 也一个都没有"), () => {
    const page = loadFloat({ lang });
    floatHello(page);
    page.push(floatComment("c1", "ana", "hola", { rel: ["follower", "mutual", "gifter"] }));
    const items = page.comments();
    assert.equal(items.length, 1);
    assert.equal(items[0].querySelectorAll(".cmt-rel").length, 0);
  });
}

test("手机页收不到 rel（app/viewer.py 的白名单里没有它），viewer.js 也不画关系标签", () => {
  const viewer = readFileSync(new URL("../web/viewer.js", import.meta.url), "utf8");
  assert.doesNotMatch(viewer, /\.rel\b|cmt-rel|互关|Mutual/);
});
