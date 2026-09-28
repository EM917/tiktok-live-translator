/* 设置分组（自检 / 引擎 / 报警）几行的纯逻辑：摘要文案该写什么、折叠行该不该
 * 自动展开。这些判断本身不碰 DOM，但曾经全部埋在 app.js 的立即执行函数里，
 * 149 个 node 测试没有一个覆盖到（engineer.md #4）：改摘要文案的措辞、或者
 * 改自动展开的判定条件，都不会有任何测试变红。
 *
 * 单独成文件、走 module.exports 供 node 测试直接 require，写法和加载方式照
 * web/follow.js、web/brand.js：浏览器里作为全局函数被 app.js 调用。 */

/* 自检摘要：三种结论只看 fail/warn/total 这几个数，不看具体是哪一项检查。
 * 同时返回 sig——「结论」的签名，给 nextAutoOpen 判断这一条自检广播是不是
 * 带来了新结论（哪怕后端换了检查项的措辞，只要三个数没变就不算新结论，
 * 不该重新弹开一个已经被中控手动收起的行）。 */
function selfcheckSummary(summary) {
  "use strict";
  var sum = summary || {};
  var sig = sum.fail + "/" + sum.warn + "/" + sum.total;
  if (sum.fail) return { text: sum.fail + " 项功能未生效", icon: "fail", sig: sig };
  if (sum.warn) return { text: "通过 · " + sum.warn + " 项提醒", icon: "warn", sig: sig };
  return { text: "全部通过 · " + sum.total + " 项", icon: "pass", sig: sig };
}

/* 引擎摘要：人话名 + 回退前缀 + 免费额度提示，全部拼进一行 active 文案。
 * hasNote 供调用方决定要不要给摘要标橙、给 engine-note 写回退原因；sig 是
 * 回退提示本身的签名（空串代表「没有回退」），同样只给 nextAutoOpen 用。
 * 35k 字符/小时是实测均值（2026-08-26 场），只做量级提示，不是精确预测。 */
function engineSummary(info) {
  "use strict";
  var data = info || {};
  var active = data.active_label || data.active
               || (data.engine === "none" ? "不翻译" : "");
  if (data.usage && data.usage.limit) {
    var pct = Math.round(data.usage.used * 100 / data.usage.limit);
    var hours = Math.max(0, Math.floor((data.usage.limit - data.usage.used) / 35000));
    active += " · 免费额度已用 " + pct + "%（按近期速度约剩 " + hours + " 小时）";
  }
  var hasNote = !!data.note;
  if (hasNote) active = "已回退 · " + active;
  return { text: active, hasNote: hasNote, sig: hasNote ? String(data.note) : "" };
}

/* 报警摘要：开关文案、词表状态、以及词表状态和开关状态搭配出来的说明句。
 * count<=0 时 desc 只说「词表为空」，跟 alertsEnabled 无关——没有词就永远
 * 不会命中，这句话本身已经说清楚了，不需要再跟开关状态二选一。 */
function watchSummary(alertsEnabled, count) {
  "use strict";
  var configured = count > 0;
  return {
    mode: alertsEnabled ? "开启" : "关闭",
    state: configured ? "词表 " + count + " 条" : "词表为空",
    desc: configured
      ? (alertsEnabled
          ? "开播后会实时监听主播原话，命中立即报警（不依赖翻译，翻译再慢也不影响报警）。"
          : "开播后不报警；命中只记入审计。要报警请先打开此开关。")
      : "当前词表为空，本工具不会发出任何违禁词报警。",
    configured: configured
  };
}

/* 折叠行的「结论变了才自动展开/收起」判定，自检、引擎回退提示两处共用。
 *
 * prevSig 是上一次记住的签名，sig 是这一条广播算出来的新签名（selfcheckSummary
 * / engineSummary 给的那个 sig），openWhenChanged 是「签名真的变了的话，这一行
 * 该不该展开」。签名没变（重连回放同一条结论、同一条回退提示）时 changed 为
 * false，调用方原样跳过，不去动用户可能已经手动收起的状态。
 *
 * 两处调用方对 open 的用法并不对称，这是刻意的，不能合并掉：
 *   - 自检传 sum.fail > 0：签名一变就无条件同步展开/收起——从「有失败」变成
 *     「全绿」也要跟着收起来，这是自检这一行自己的规则。
 *   - 引擎回退传固定的 true，但调用方只在 sig 非空（真的有新的回退提示）时
 *     才应用 open；提示被清除时签名也会变，但不该把用户正开着看的行强制
 *     收起——沿用 nextAutoOpen(prevSig, note) 这个签名给两种参数都留了口子，
 *     具体「变了之后要不要真的展开」由调用方按各自的规则决定。 */
function nextAutoOpen(prevSig, sig, openWhenChanged) {
  "use strict";
  var s = sig === undefined || sig === null ? "" : String(sig);
  var p = prevSig === undefined || prevSig === null ? "" : String(prevSig);
  return { sig: s, changed: s !== p, open: !!openWhenChanged };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { selfcheckSummary, engineSummary, watchSummary, nextAutoOpen };
}
