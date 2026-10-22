// 弹幕留意的版面规则（web/style.css）：直接读样式表，钉住「截图里看出来的」几个问题别回来。
//
// 这些问题只在真的排版时才看得到（假 DOM 没有版面），而 G10（tests/test_i18n_dom.py，开无头 Chrome）只在 CI 的
// macOS 跑器上跑。所以这里先静态钉住规则本身——改掉其中任何一条，下面对应的那个测试就红：
// - 抽屉高度固定、不超过窗口约三分之一，两个页签一样高，列表在固定的区域里自己滚，有可见的滚动提示；
// - 头一行折行时，折到下一行的胶囊不带左边距缩进；
// - 浅色模式下关系标签的字 4.5:1 以上，深色模式的取值和改之前一样；
// - 条右侧的「查看 / 收起」箭头在抽屉开着时转向上。
// 不起浏览器、不 import main.py。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (name) => readFileSync(new URL("../web/" + name, import.meta.url), "utf8").replace(/\r\n/g, "\n");
const CSS = read("style.css");
const HTML = read("index.html");

/* 样式规则 -> [{ sel, body, at }]。at 是外面包着的 @ 规则的前导（@media / @supports），没有就是 ""。
 * 只认到这份样式表用到的写法：规则里没有嵌套，@ 规则里面是普通规则。 */
function parseRules(css) {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const out = [];
  const stack = [];
  let prelude = "";
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "{") {
      const head = prelude.trim();
      prelude = "";
      if (head.startsWith("@") && !head.startsWith("@keyframes") && !head.startsWith("@font-face")) {
        stack.push({ at: head });
        continue;
      }
      const end = text.indexOf("}", i);
      out.push({ sel: head.replace(/\s+/g, " "), body: text.slice(i + 1, end), at: stack.map((s) => s.at).join(" ") });
      i = end;
    } else if (ch === "}") {
      stack.pop();
      prelude = "";
    } else {
      prelude += ch;
    }
  }
  return out;
}
const RULES = parseRules(CSS);

/* 选择器列表里有这一项的规则，把它们的声明合起来（后写的覆盖先写的）。atFilter 为真时只看带这个 @ 前导的 */
function decls(selector, atFilter) {
  const merged = {};
  for (const r of RULES) {
    if (atFilter !== undefined ? !r.at.includes(atFilter) : r.at !== "") continue;
    if (!r.sel.split(",").map((s) => s.trim()).includes(selector)) continue;
    for (const d of r.body.split(";")) {
      const k = d.indexOf(":");
      if (k > 0) merged[d.slice(0, k).trim()] = d.slice(k + 1).trim();
    }
  }
  return merged;
}

test("样式表解析：规则、@media 里的规则、选择器列表都读得对（不然下面全是空转）", () => {
  const rules = parseRules("/* a { x: y } */ .a, .b { color: red; margin: 0 } @media (x) { .c { top: 1px } } @supports not selector(::p) { .d { left: 0 } }");
  assert.deepEqual(rules.map((r) => [r.sel, r.at]), [[".a, .b", ""], [".c", "@media (x)"], [".d", "@supports not selector(::p)"]]);
  assert.equal(decls(".guard-drawer").display, "flex");
  assert.ok(RULES.length > 200);
});

// ---- 抽屉 ----------------------------------------------------------------------------------------

test("抽屉：高度是固定的（不是 max-height），1000×760 的窗口里约占三分之一，列表区自己滚动", () => {
  const d = decls(".guard-drawer");
  assert.equal(d.display, "flex");
  assert.equal(d["flex-direction"], "column");
  assert.ok(!("max-height" in d), "max-height 会让两个页签各自按内容算高度，切页签时抽屉就跳");
  const m = /^clamp\((\d+)px,\s*(\d+)vh,\s*(\d+)px\)$/.exec(d.height || "");
  assert.ok(m, "height 应该是 clamp(下限px, N vh, 上限px)：" + d.height);
  const at = (vh) => Math.min(Math.max(Number(m[1]), vh * Number(m[2]) / 100), Number(m[3]));
  const h760 = at(760);
  assert.ok(h760 <= 760 / 3 + 4 && h760 >= 760 / 3 - 12, "760px 高的窗口里抽屉 " + h760 + "px，应当约三分之一（253px）");
  assert.ok(at(1400) <= 360, "很高的窗口里也不无限长");
  // 状态行和页签不跟着滚走；列表区吃掉剩下的高度
  assert.equal(decls(".guard-drawer-top").flex, "none");
  assert.equal(decls(".guard-tabs").flex, "none");
  const panes = decls(".guard-panes");
  assert.match(panes.flex, /^1 1 0/);
  assert.ok(parseInt(panes["min-height"], 10) >= 48, "列表区缩到最小也得露出一点");
  // 滚动的是每个页签自己的 .guard-pane，不是整个抽屉
  const pane = decls(".guard-pane");
  assert.equal(pane["overflow-y"], "auto");
  assert.equal(pane["min-height"], "0");
  assert.equal(pane["overscroll-behavior"], "contain", "滚到头不带着下面的弹幕列一起滚");
});

test("抽屉：两个页签放在同一个 .guard-panes 里，各自是一个 .guard-pane（切页签只换里面的内容，不换高度）", () => {
  const wrap = /<div class="guard-panes">([\s\S]*?)<\/section>/.exec(HTML);
  assert.ok(wrap, "index.html 里找不到 .guard-panes");
  const panes = [...wrap[1].matchAll(/<div id="(guard-pane-(?:recent|marked))" class="guard-pane\b/g)].map((m) => m[1]);
  assert.deepEqual(panes, ["guard-pane-recent", "guard-pane-marked"]);
  assert.equal((HTML.match(/class="guard-panes"/g) || []).length, 1);
});

test("抽屉的滚动提示：常驻的滚动条 + 底部渐隐，最后一张卡片停在渐隐上方", () => {
  const bar = decls(".guard-pane::-webkit-scrollbar");
  assert.ok(parseInt(bar.width, 10) >= 6, "::-webkit-scrollbar 有宽度，macOS 才不会用「滚动时才出现」的细条");
  assert.ok(decls(".guard-pane::-webkit-scrollbar-thumb").background, "滑块要有颜色");
  const fade = decls(".guard-panes::after");
  assert.match(fade.background, /^linear-gradient\(to bottom,\s*var\(--card-clear\),\s*var\(--card\)\)$/);
  assert.equal(fade["pointer-events"], "none", "渐隐不能挡住下面卡片上的按钮");
  assert.equal(fade.position, "absolute");
  const fadeH = parseInt(fade.height, 10);
  assert.ok(fadeH >= 16);
  // 列表末尾留的空白 >= 渐隐高度：滚到底时最后一张卡片（连按钮）整张在渐隐上方
  const tail = decls("#guard-events");
  assert.ok(parseInt(tail["padding-bottom"], 10) >= fadeH, "列表末尾的空白 " + tail["padding-bottom"] + " 小于渐隐 " + fadeH + "px");
  assert.ok(parseInt(decls("#guard-marked")["padding-bottom"], 10) >= fadeH);
  assert.ok(parseInt(decls(".guard-pane")["scroll-padding-bottom"], 10) >= fadeH, "scrollIntoView 到的卡片也要停在渐隐上方");
  // 渐隐起点与卡片底色同色、全透明（不写 transparent）：深浅色各一份
  for (const at of ["", "prefers-color-scheme: dark"]) {
    const root = RULES.filter((r) => r.sel === ":root" && (at ? r.at.includes(at) : r.at === "")).map((r) => r.body).join(";");
    const card = /--card:\s*#([0-9a-f]{6})/i.exec(root);
    const clear = /--card-clear:\s*rgba\((\d+),\s*(\d+),\s*(\d+),\s*0\)/.exec(root);
    assert.ok(clear, "缺 --card-clear（" + (at || "浅色") + "）");
    if (card) {
      const [r, g, b] = [0, 2, 4].map((i) => parseInt(card[1].slice(i, i + 2), 16));
      assert.deepEqual(clear.slice(1).map(Number), [r, g, b], "--card-clear 要与 --card 同色");
    }
  }
});

// ---- 弹幕栏里一行的头 ----------------------------------------------------------------------------

test("弹幕头一行：折行时胶囊不带左边距缩进（间隔用 gap，胶囊自己没有 margin-left）", () => {
  const head = decls(".cmt-head");
  assert.equal(head.display, "flex");
  assert.equal(head["flex-wrap"], "wrap");
  assert.match(head.gap || "", /^\d+px \d+px$/, "行间距 列间距");
  for (const sel of [".guard-pill", ".cmt-rel", ".guard-star"]) {
    const ml = decls(sel)["margin-left"];
    assert.ok(ml === undefined || ml === "0" || ml === "0px", sel + " 带着 margin-left " + ml + "：折到下一行就会缩进");
  }
  assert.equal(decls(".guard-mark-btn").position, "absolute", "☆ 按钮不进 flex 的排版");
  // ★ 的右边距是负的，抵掉一部分 gap，★ 和昵称还是挨着的
  assert.ok(parseInt(decls(".guard-star")["margin-right"], 10) < 0);
});

// ---- 关系标签的对比度 ----------------------------------------------------------------------------

function lum(hex) {
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
const contrast = (a, b) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
function rootToken(name, dark) {
  const root = RULES.filter((r) => r.sel === ":root" && (dark ? r.at.includes("prefers-color-scheme: dark") : r.at === ""))
    .map((r) => r.body).join(";");
  const m = new RegExp("--" + name + ":\\s*([^;]+)").exec(root);
  return m ? m[1].trim() : null;
}

test("关系标签（粉丝 / 送过礼 / 订阅）：浅色模式字对底 4.5:1 以上，底色不透明", () => {
  const pairs = [
    ["粉丝", rootToken("rel-blue-ink"), rootToken("tint-blue-10")],
    ["送过礼", rootToken("rel-purple-ink"), rootToken("rel-purple-bg")],
    ["订阅", rootToken("rel-gray-ink"), rootToken("rel-gray-bg")],
  ];
  for (const [name, ink, bg] of pairs) {
    assert.match(ink || "", /^#[0-9a-f]{6}$/i, name + " 的字色要写成不透明的 6 位十六进制：" + ink);
    assert.match(bg || "", /^#[0-9a-f]{6}$/i, name + " 的底色要不透明（落在红 / 琥珀的行底上对比度才不变）：" + bg);
    const ratio = contrast(ink.slice(1), bg.slice(1));
    assert.ok(ratio >= 4.5, name + " 对比度 " + ratio.toFixed(2) + " < 4.5");
  }
  // 三种标签的样式确实取这几个令牌
  assert.equal(decls(".cmt-rel-follower").color, "var(--rel-blue-ink)");
  // 「互关」不再显示（2026-10-21，web/app.js relTags 把它标成「粉丝」），页面不会再画 .cmt-rel-mutual
  assert.deepEqual(decls(".cmt-rel-mutual"), {});
  assert.equal(decls(".cmt-rel-gifter").color, "var(--rel-purple-ink)");
  assert.equal(decls(".cmt-rel-gifter").background, "var(--rel-purple-bg)");
  assert.equal(decls(".cmt-rel").color, "var(--rel-gray-ink)");
  assert.equal(decls(".cmt-rel").background, "var(--rel-gray-bg)");
});

test("关系标签：深色模式的取值和改之前逐项相同（蓝 --blue、紫 #bf5af2 加 12% 紫底、灰 --label-2 配 --fill）", () => {
  assert.equal(rootToken("rel-blue-ink", true), "var(--blue)");
  assert.equal(rootToken("rel-purple-ink", true), "#bf5af2");
  assert.equal(rootToken("rel-purple-bg", true), "rgba(191, 90, 242, 0.12)");
  assert.equal(rootToken("rel-gray-ink", true), "var(--label-2)");
  assert.equal(rootToken("rel-gray-bg", true), "var(--fill)");
  assert.equal(decls(".cmt-rel-follower").background, "var(--tint-blue-10)", "蓝标签的底色没动");
});

// ---- 条右侧的「查看 / 收起」 ----------------------------------------------------------------------

test("顶部条右侧的箭头：抽屉收着朝右，开着（aria-expanded=true）转成朝上", () => {
  assert.equal(decls(".guard-bar-view::after").content, '"\\203A"');
  assert.equal(decls('.guard-bar-main[aria-expanded="true"] .guard-bar-view::after').transform, "rotate(-90deg)");
  assert.match(HTML, /<span id="guard-bar-view" class="guard-bar-view" data-en="View">查看<\/span>/);
});
