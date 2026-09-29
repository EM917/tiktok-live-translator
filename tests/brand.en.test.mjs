globalThis.UI_LANG = "en";   // 必须在 require 之前：web/i18n.js 加载时读一次（node --test 每个文件一个进程）
// 品牌下拉的英文（web/brand.js，spec §12.1 G6）。中文断言在 tests/brand.test.mjs。
// 固定的第一项「不限（默认）」三处写法（这里和 index.html 两个下拉）同一句英文，G3 钉着；
// 品牌名是数据，原样放。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const B = require("../web/brand.js");

test("语言确实是英文（不然下面全是空转）", () => {
  assert.equal(require("../web/i18n.js").UI_LANG, "en");
});

test("选项列表：第一项是 Any (default)，品牌名原样", () => {
  assert.deepEqual(B.buildBrandOptionList(undefined), [{ value: "", label: "Any (default)" }]);
  assert.deepEqual(B.buildBrandOptionList([{ id: "bella", name: "Bella All Natural" }, { id: "mx" }]), [
    { value: "", label: "Any (default)" },
    { value: "bella", label: "Bella All Natural" },
    { value: "mx", label: "mx" },
  ]);
});

test("重建之后保留选中值的规则与语言无关", () => {
  const list = B.buildBrandOptionList([{ id: "bella", name: "Bella All Natural" }]);
  assert.equal(B.resolveSelectedBrand("bella", list), "bella");
  assert.equal(B.resolveSelectedBrand("gone", list), "");
});
