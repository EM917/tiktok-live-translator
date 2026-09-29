// i18n: done
/* 设置分组（自检 / 引擎 / 报警 / 界面语言）几行的纯逻辑：摘要文案该写什么、折叠行该不该
 * 自动展开。这些判断本身不碰 DOM，但曾经全部埋在 app.js 的立即执行函数里，
 * 149 个 node 测试没有一个覆盖到（engineer.md #4）：改摘要文案的措辞、或者
 * 改自动展开的判定条件，都不会有任何测试变红。
 *
 * 单独成文件、走 module.exports 供 node 测试直接 require，写法和加载方式照
 * web/follow.js、web/brand.js：浏览器里作为全局函数被 app.js 调用。 */

// 浏览器里 L/LN 是 i18n.js 定义的全局函数（这里的 var 不会清掉它）；node 测试里从 i18n.js 取
if (typeof L === "undefined") { var I18N_ = require("./i18n.js"); var L = I18N_.L, LN = I18N_.LN, APP_NAME = I18N_.APP_NAME; }

/* 自检摘要：三种结论只看 fail/warn/total 这几个数，不看具体是哪一项检查。
 * 同时返回 sig——「结论」的签名，给 nextAutoOpen 判断这一条自检广播是不是
 * 带来了新结论（哪怕后端换了检查项的措辞，只要三个数没变就不算新结论，
 * 不该重新弹开一个已经被中控手动收起的行）。 */
function selfcheckSummary(summary) {
  "use strict";
  var sum = summary || {};
  var sig = sum.fail + "/" + sum.warn + "/" + sum.total;
  if (sum.fail) return { text: LN(sum.fail, sum.fail + " 项功能未生效", "1 feature isn’t working",
                                  sum.fail + " features aren’t working"), icon: "fail", sig: sig };
  if (sum.warn) return { text: LN(sum.warn, "通过 · " + sum.warn + " 项提醒", "Passed · 1 warning",
                                  "Passed · " + sum.warn + " warnings"), icon: "warn", sig: sig };
  return { text: LN(sum.total, "全部通过 · " + sum.total + " 项", "1 check passed",
                    "All " + sum.total + " checks passed"), icon: "pass", sig: sig };
}

/* 引擎摘要：人话名 + 回退前缀 + 免费额度提示，全部拼进一行 active 文案。
 * hasNote 供调用方决定要不要给摘要标橙、给 engine-note 写回退原因；sig 是
 * 回退提示本身的签名（空串代表「没有回退」），同样只给 nextAutoOpen 用。
 * 35k 字符/小时是实测均值（2026-08-26 场），只做量级提示，不是精确预测。 */
function engineSummary(info) {
  "use strict";
  var data = info || {};
  var active = data.active_label || data.active
               || (data.engine === "none" ? L("不翻译", "No translation") : "");
  if (data.usage && data.usage.limit) {
    var pct = Math.round(data.usage.used * 100 / data.usage.limit);
    var hours = Math.max(0, Math.floor((data.usage.limit - data.usage.used) / 35000));
    // 英文用紧凑写法（docs/i18n-style.md §3 #62、§4.3 R11）：长写法会把摘要挤出这一行
    active += L(" · 免费额度已用 " + pct + "%（按近期速度约剩 " + hours + " 小时）",
                " · " + pct + "% of free quota used (~" + hours + " hr left)");
  }
  var hasNote = !!data.note;
  if (hasNote) active = L("已回退 · " + active, "Fallback · " + active);
  return { text: active, hasNote: hasNote, sig: hasNote ? String(data.note) : "" };
}

/* 报警摘要：开关文案、词表状态、以及词表状态和开关状态搭配出来的说明句。
 * count<=0 时 desc 只说「词表为空」，跟 alertsEnabled 无关——没有词就永远
 * 不会命中，这句话本身已经说清楚了，不需要再跟开关状态二选一。 */
function watchSummary(alertsEnabled, count) {
  "use strict";
  var configured = count > 0;
  return {
    mode: alertsEnabled ? L("开启", "On") : L("关闭", "Off"),
    state: configured ? LN(count, "词表 " + count + " 条", "1 term", count + " terms") : L("词表为空", "List empty"),
    desc: configured
      ? (alertsEnabled
          ? L("开播后会实时监听主播原话，命中立即报警（不依赖翻译，翻译再慢也不影响报警）。",
              "Listens to what the streamer says and alerts on a match right away. "
              + "Translation speed doesn’t affect alerts.")
          : L("开播后不报警；命中只记入审计。要报警请先打开此开关。",
              "No alerts during the stream. Matches are still written to the audit log. Turn this on to get alerts."))
      : L("当前词表为空，本工具不会发出任何违禁词报警。",
          "The banned-term list is empty, so no alerts will be raised."),
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

/* 界面语言行（index.html #lang-card，spec §3.4）。语言名永远用该语言自己的写法，
 * 不翻译：用户就算落进一个看不懂的界面，也能认出自己的语言、切回去。 */
function langName(code) {
  "use strict";
  return code === "en" ? "English" : "中文";   // i18n: data（语言自称，永不翻译）
}

/* 行右侧的摘要。setting 是设置里存的选择（system / zh / en，config.ui_lang_setting），
 * lang 是此刻生效的界面语言（config.ui_lang），system 是「跟随系统」解析出的语言
 * （config.ui_lang_system；检测失败时服务端已经按中文给）。跟随系统时写出它此刻
 * 解析成了什么；认不出的 setting（老数据）就写生效的语言，不留空。 */
function langSummary(setting, lang, system) {
  "use strict";
  if (setting === "zh" || setting === "en") return langName(setting);
  if (setting === "system") return L("跟随系统 · {name}", "System · {name}").replace("{name}", langName(system));
  return langName(lang);
}

/* 下拉里「跟随系统」那一项：括号里写它此刻会解析成的语言。 */
function langSystemLabel(system) {
  "use strict";
  return L("跟随系统（{name}）", "System ({name})").replace("{name}", langName(system));
}

/* 服务端的界面语言（config.ui_lang）和本页不同时要不要整页重载一次（spec §3.5）。
 * uiLang 是本页加载时的语言（i18n.js 的 UI_LANG），stored 是 sessionStorage 里
 * 上一次重载留下的「目标语言@毫秒时间」。10 秒内已经为同一个目标重载过就不再重载：
 * 服务端和页面万一一直对不上，宁可停在原来的语言，也不能无限刷新。
 * 返回 { reload, mark }：mark 是这次重载前要记下的值。 */
var LANG_RELOAD_WINDOW_MS = 10000;
function langReload(target, uiLang, stored, now) {
  "use strict";
  var keep = { reload: false, mark: null };
  if (target !== "zh" && target !== "en") return keep;      // 没带 ui_lang（闸关着）：不比对
  if (target === (uiLang === "en" ? "en" : "zh")) return keep;
  var m = /^(zh|en)@(\d+)$/.exec(stored == null ? "" : String(stored));
  var age = m ? now - Number(m[2]) : -1;
  if (m && m[1] === target && age >= 0 && age < LANG_RELOAD_WINDOW_MS) return keep;
  return { reload: true, mark: target + "@" + now };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { selfcheckSummary, engineSummary, watchSummary, nextAutoOpen,
                     langName, langSummary, langSystemLabel, langReload, LANG_RELOAD_WINDOW_MS };
}
