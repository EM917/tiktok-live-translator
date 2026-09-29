// i18n: done
/* 品牌词表（「本场品牌」下拉）的纯函数：一组猜默认值（从房间输入框的文本或
 * 「最近直播间」chip 猜出主播名，再查服务端广播的 config.brands 取记住的
 * 品牌），一组管选项列表本身（用 config.brand_options 生成选项、决定重建
 * 之后该保留哪个选中值——选项由 brands/ 文件夹内容动态生成，不写死），
 * 一组管「下拉显示什么就发什么」这条规则——本页手动改过下拉（自上次发出
 * start 以来）就沿用当前值，没改过才按主播刷新记住的默认值
 * （brandForChipClick / brandStateAfterStart / brandStateSyncStreamer）。
 * 独立成文件以便单元测试：tests/brand.test.mjs 用 node:test 钉住这些规则。
 * 写法和加载方式照 web/normalize.js、web/follow.js：浏览器里是全局函数，
 * Node 测试环境走 module.exports。 */

// 浏览器里 L/LN 是 i18n.js 定义的全局函数（这里的 var 不会清掉它）；node 测试里从 i18n.js 取
if (typeof L === "undefined") { var I18N_ = require("./i18n.js"); var L = I18N_.L, LN = I18N_.LN, APP_NAME = I18N_.APP_NAME; }

/* 从房间输入框的文本里挖出主播用户名——与 normalizeRoomInput 认的输入一致：
 * 完整链接 .../@name/live、裸 @name、裸用户名。挖不出就返回空串（视为
 * 「不知道是谁」，查不到记住的品牌，下拉退回「不限」）。 */
function streamerFromInput(raw) {
  "use strict";
  raw = String(raw || "").trim();
  var m = raw.match(/@([A-Za-z0-9._]+)/);
  if (m) return m[1].toLowerCase();
  if (/^[A-Za-z0-9._]+$/.test(raw)) return raw.toLowerCase();
  return "";
}

/* 按主播名查「记住的品牌」。brands 不是一个普通对象（缺失、类型不对）、
 * 没有主播名、或者这个主播没记录过，都算「不限」（空串）——和
 * app/settings.py streamer_brands() 的空值语义一致。 */
function defaultBrandFor(streamer, brands) {
  "use strict";
  if (!streamer || !brands || typeof brands !== "object") return "";
  var v = brands[String(streamer).toLowerCase()];
  return typeof v === "string" ? v : "";
}

/* 房间输入变化之后，「这次房间变化后用户手动改过下拉」的标记要不要清掉：
 * 只有认出来的主播真的换了才清。同一个主播的地址被改写——在房间链接后面补贴
 * .flv 直连地址、带上 ?参数、逐字修改——不算换房间：否则中控刚把下拉手动改成
 * 「不限」，贴一个直连地址就被悄悄改回记住的品牌。state 是 {streamer, touched}，
 * 返回新的 state（不改入参）。这条规则只给「用户真的在敲键盘」（房间输入框的
 * input 事件）用；页面加载/hello/config 广播回填输入框走的是下面
 * brandStateSyncStreamer，不走这条——那些不是用户操作，不该被当成「换主播」
 * 去清用户手选的品牌。 */
function brandStateAfterRoomInput(state, streamer) {
  "use strict";
  var prev = state && typeof state === "object" ? state : {};
  var s = streamer ? String(streamer) : "";
  if (s === (prev.streamer || "")) return { streamer: s, touched: !!prev.touched };
  return { streamer: s, touched: false };
}

/* 页面加载、hello、config 广播回填输入框时用：只记下当前主播名，touched
 * 原样保留——这些都是程序自己填的，不是中控操作，不能套用
 * brandStateAfterRoomInput 那条「换了主播就清掉手动选择」的规则，否则「本页
 * 手动改过下拉」的记录会被一次跟中控无关的回填悄悄抹掉（品牌被换成记住的
 * 默认值甚至「不限」）。返回新的 state（不改入参）。 */
function brandStateSyncStreamer(state, streamer) {
  "use strict";
  var prev = state && typeof state === "object" ? state : {};
  return { streamer: streamer ? String(streamer) : "", touched: !!prev.touched };
}

/* 发出一条 start 指令（开始按钮、回车、「最近直播间」chip）之后：清掉
 * touched，主播名原样保留——「本页手动改过下拉」只在「自上一次发出 start
 * 以来」这段时间里算数，发出去之后这段就翻篇了，下一次不管是刷新默认值
 * 还是点 chip，都要重新按「有没有改过」判断。返回新的 state（不改入参）。 */
function brandStateAfterStart(state) {
  "use strict";
  var prev = state && typeof state === "object" ? state : {};
  return { streamer: prev.streamer ? String(prev.streamer) : "", touched: false };
}

/* 点「最近直播间」chip 时，「该发哪个品牌、状态该怎么变」——新规则「下拉
 * 显示什么就发什么」：本页手动改过品牌下拉（自上次发出 start 以来）就沿用
 * 下拉当前的值，不管点的是哪个主播的 chip，都不该被悄悄换掉；没手动改过
 * 才按这个 chip 对应主播记住的品牌刷新。currentValue 是点击前下拉框的值，
 * brands 是 config.brands（按主播记住的品牌映射）。返回
 * { brand: 该发送并回填进下拉的值, state: 新的 {streamer, touched} }——
 * state.touched 这里只会原样保留或维持 false，真正清零发生在发出 start
 * 之后（见 brandStateAfterStart），这个函数只管点击这一下。 */
function brandForChipClick(state, streamer, currentValue, brands) {
  "use strict";
  var prev = state && typeof state === "object" ? state : {};
  var s = streamer ? String(streamer) : "";
  if (prev.touched) {
    return { brand: currentValue ? String(currentValue) : "",
             state: { streamer: s, touched: true } };
  }
  return { brand: defaultBrandFor(s, brands), state: { streamer: s, touched: false } };
}

/* 下拉框的选项列表：固定第一项是「不限」，其余按 config.brand_options 的
 * 顺序（后端已经按显示名排好序）追加。只返回纯数据 [{value, label}, ...]，
 * 不碰 DOM——DOM 部分（建 <option> 节点）留给 app.js，这里单独用 node 测试
 * 钉住规则：半成品条目（缺 id、id 不是字符串）跳过，不让一条脏数据
 * 搞坏整个下拉框；brand_options 本身不是数组（缺失、类型不对）时只剩
 * 「不限」一项。 */
function buildBrandOptionList(brandOptions) {
  "use strict";
  var opts = [{ value: "", label: L("不限（默认）", "Any (default)") }];
  if (Array.isArray(brandOptions)) {
    for (var i = 0; i < brandOptions.length; i++) {
      var o = brandOptions[i];
      if (o && typeof o.id === "string" && o.id) {
        var label = typeof o.name === "string" && o.name ? o.name : o.id;
        opts.push({ value: o.id, label: label });
      }
    }
  }
  return opts;
}

/* 重建选项列表后应该保留哪个选中值：currentValue 若仍在新列表里就原样保留，
 * 不在了（对应的品牌文件被删掉/改名，或本来就没选过）就回落到「不限」
 * （空串）——app.js 的 selectBrand() 对真实 DOM <select> 做的是同一件事，
 * 这里是它的纯函数版本，供 node 测试直接钉住规则。 */
function resolveSelectedBrand(currentValue, optionList) {
  "use strict";
  var v = currentValue ? String(currentValue) : "";
  var list = Array.isArray(optionList) ? optionList : [];
  for (var i = 0; i < list.length; i++) {
    if (list[i] && list[i].value === v) return v;
  }
  return "";
}

/* Node 测试环境导出；浏览器里作为全局函数被 app.js 使用 */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { streamerFromInput, defaultBrandFor, brandStateAfterRoomInput,
                     brandStateSyncStreamer, brandStateAfterStart, brandForChipClick,
                     buildBrandOptionList, resolveSelectedBrand };
}
