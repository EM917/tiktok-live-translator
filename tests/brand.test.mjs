// 品牌词表默认值纯函数的契约测试（node:test，零 npm 依赖）。
// 照 tests/normalize.test.mjs / tests/follow.test.mjs 的加载方式。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { streamerFromInput, defaultBrandFor,
       buildBrandOptionList, resolveSelectedBrand } = require("../web/brand.js");

// ---- streamerFromInput：与 normalizeRoomInput 认的输入一致 ----

const streamerCases = [
  ["https://www.tiktok.com/@bellaallnatural/live", "bellaallnatural"],
  ["https://www.tiktok.com/@bellaallnatural/live?enter=share", "bellaallnatural"],
  ["@daisycabral_", "daisycabral_"],
  ["daisycabral_", "daisycabral_"],
  ["  @Spaced  ", "spaced"],           // 归一成小写，好和 settings 里的键对上
  ["ItzeSantana11", "itzesantana11"],
  ["某个中文昵称", ""],
  ["", ""],
  ["www.tiktok.com/@abc/live", "abc"], // 无协议地址一样能挖出 @ 后面的名字
  ["pull.example.com/live.flv", ""],   // 纯直连地址没有主播身份
];

for (const [input, expected] of streamerCases) {
  test(`streamerFromInput(${JSON.stringify(input)}) -> ${JSON.stringify(expected)}`, () => {
    assert.equal(streamerFromInput(input), expected);
  });
}

// ---- defaultBrandFor：空值/类型防御，和 app/settings.py 的空值语义一致 ----
// 品牌 id 用虚构的 "acme"：这个函数不关心 id 具体是什么，用真实品牌名反而
// 会让人误以为这里钉的是某个品牌的行为，而不是通用规则。

test("记过的主播返回记住的品牌", () => {
  assert.equal(defaultBrandFor("bellaallnatural", { bellaallnatural: "acme" }), "acme");
});

test("按小写键查找，与 streamerFromInput 的归一化对齐", () => {
  assert.equal(defaultBrandFor("Bella", { bella: "acme" }), "acme");
});

test("没记过的主播、没有主播名、brands 不是对象，都退回不限", () => {
  assert.equal(defaultBrandFor("nadie", { bella: "acme" }), "");
  assert.equal(defaultBrandFor("", { bella: "acme" }), "");
  assert.equal(defaultBrandFor("bella", null), "");
  assert.equal(defaultBrandFor("bella", undefined), "");
  assert.equal(defaultBrandFor("bella", "not an object"), "");
});

test("值不是字符串（settings 被手改过）时按不限处理", () => {
  assert.equal(defaultBrandFor("bella", { bella: 7 }), "");
});

// ---- buildBrandOptionList：由 config.brand_options 生成下拉框选项 ----

test("固定第一项是不限，其余按 brand_options 顺序追加", () => {
  assert.deepEqual(
    buildBrandOptionList([{ id: "acme", name: "ACME 严选" }, { id: "zeta", name: "zeta" }]),
    [{ value: "", label: "不限（默认）" },
     { value: "acme", label: "ACME 严选" },
     { value: "zeta", label: "zeta" }]);
});

test("条目没写 name 就用 id 当显示名", () => {
  assert.deepEqual(buildBrandOptionList([{ id: "acme" }]),
    [{ value: "", label: "不限（默认）" }, { value: "acme", label: "acme" }]);
});

test("半成品条目（缺 id、id 不是字符串）跳过，不搞坏整个列表", () => {
  assert.deepEqual(
    buildBrandOptionList([{ name: "没有 id" }, { id: 7, name: "id 不是字符串" },
                          { id: "", name: "空 id" }, null, "not an object",
                          { id: "acme", name: "ACME 严选" }]),
    [{ value: "", label: "不限（默认）" }, { value: "acme", label: "ACME 严选" }]);
});

test("brand_options 不是数组（缺失、类型不对）时只剩不限", () => {
  assert.deepEqual(buildBrandOptionList(undefined), [{ value: "", label: "不限（默认）" }]);
  assert.deepEqual(buildBrandOptionList(null), [{ value: "", label: "不限（默认）" }]);
  assert.deepEqual(buildBrandOptionList("not an array"), [{ value: "", label: "不限（默认）" }]);
});

// ---- resolveSelectedBrand：重建选项后该保留哪个选中值 ----

const acmeOptions = [{ value: "", label: "不限（默认）" }, { value: "acme", label: "ACME 严选" }];

test("当前值仍在新列表里：原样保留", () => {
  assert.equal(resolveSelectedBrand("acme", acmeOptions), "acme");
});

test("当前值不在新列表里了（文件被删/改名）：回落到不限", () => {
  assert.equal(resolveSelectedBrand("deleted-brand", acmeOptions), "");
});

test("当前值是空串（本来就选的不限）：保持不限", () => {
  assert.equal(resolveSelectedBrand("", acmeOptions), "");
});

test("optionList 不是数组时按不限处理", () => {
  assert.equal(resolveSelectedBrand("acme", null), "");
  assert.equal(resolveSelectedBrand("acme", undefined), "");
});

// ---- brandStateAfterRoomInput：只有主播真的换了才清掉「手动改过」 ----

const { brandStateAfterRoomInput } = require("../web/brand.js");

test("同一个主播的地址改写（补贴 .flv 直连地址、加参数）不清掉手动选择", () => {
  const touched = { streamer: "daisycabral_", touched: true };
  const s = streamerFromInput(
    "https://www.tiktok.com/@daisycabral_/live https://pull.example.com/x.flv");
  assert.deepEqual(brandStateAfterRoomInput(touched, s),
                   { streamer: "daisycabral_", touched: true });
  assert.deepEqual(brandStateAfterRoomInput(touched, "daisycabral_"),
                   { streamer: "daisycabral_", touched: true });
});

test("换了主播就清掉手动选择，改按记住的品牌刷新", () => {
  const touched = { streamer: "daisycabral_", touched: true };
  assert.deepEqual(brandStateAfterRoomInput(touched, "jessyjewelry1"),
                   { streamer: "jessyjewelry1", touched: false });
  assert.deepEqual(brandStateAfterRoomInput(touched, ""),
                   { streamer: "", touched: false });
});

test("初始/脏 state 按「没改过」处理，不改入参", () => {
  assert.deepEqual(brandStateAfterRoomInput(undefined, "abc"), { streamer: "abc", touched: false });
  assert.deepEqual(brandStateAfterRoomInput(null, ""), { streamer: "", touched: false });
  const prev = { streamer: "abc", touched: true };
  brandStateAfterRoomInput(prev, "xyz");
  assert.deepEqual(prev, { streamer: "abc", touched: true });
});
