// normalizeRoomInput 的契约测试（node:test，零 npm 依赖）。
// 规则密集且互相制约（URL 提取、全角截断、@用户名补全、域名判定、拒收）——
// 后续任何调整都必须先过这份用例。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const normalize = require("../web/normalize.js");

const cases = [
  // 完整链接原样通过（含查询串）
  ["https://www.tiktok.com/@abc/live", "https://www.tiktok.com/@abc/live"],
  ["https://www.tiktok.com/@abc/live?enter=share",
   "https://www.tiktok.com/@abc/live?enter=share"],
  // 分享文本：挖出链接、在全角标点/中文处截断
  ["快看这个直播 https://www.tiktok.com/@abc/live！超好看",
   "https://www.tiktok.com/@abc/live"],
  ["看 https://www.tiktok.com/@abc/live! now", "https://www.tiktok.com/@abc/live"],
  ['链接："https://www.tiktok.com/@abc/live"', "https://www.tiktok.com/@abc/live"],
  // @用户名 / 裸用户名（TikTok 用户名允许字母数字点下划线）
  ["@somebody", "https://www.tiktok.com/@somebody/live"],
  ["somebody", "https://www.tiktok.com/@somebody/live"],
  ["user.name_9", "https://www.tiktok.com/@user.name_9/live"],
  ["  @spaced  ", "https://www.tiktok.com/@spaced/live"],
  // 域名/无协议地址：补 https://，绝不能当成用户名
  ["www.tiktok.com/@abc/live", "https://www.tiktok.com/@abc/live"],
  ["vm.tiktok.com", "https://vm.tiktok.com"],
  ["abc.tv", "https://abc.tv"],
  ["pull.example.com/live.flv", "https://pull.example.com/live.flv"],
  // 认不出的输入：返回 null 让 UI 报错，不许瞎拼
  ["某个中文昵称", null],
  ["two words", null],
  ["@名字", null],
  ["@user name", null],
];

for (const [input, expected] of cases) {
  test(`normalize(${JSON.stringify(input)}) -> ${JSON.stringify(expected)}`, () => {
    assert.equal(normalize(input), expected);
  });
}
