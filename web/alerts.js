// i18n: done
/* 报警面板的纯逻辑：窗口在后台时的标题提醒、「上一场」标记、「本场共 N 条」。

   单独成文件是为了可测（tests/alerts.test.mjs）；app.js 只负责把结果写进 DOM。 */

// 浏览器里 L/LN 是 i18n.js 定义的全局函数（这里的 var 不会清掉它）；node 测试里从 i18n.js 取
if (typeof L === "undefined") { var I18N_ = require("./i18n.js"); var L = I18N_.L, LN = I18N_.LN, APP_NAME = I18N_.APP_NAME; }

var ALERT_PANEL_CAP = 50;      // 面板只留最近这么多条（与 app.js / server.alerts 一致）
var DEFAULT_TITLE = L("TikTok 直播同传", "TikTok Live Translator");

/* 英文是标签式写法，不分单复数；和 app/window_attention.py 的 attention_title 必须逐字相同，
   两边的中文写法不同（这里拼接、那边模板），同中文→同英文的检查聚合不到一起，
   改由 tests/i18n_golden.json 的 window_title_2_en 在 node 和 Python 两边各钉一次 */
function alertTitle(n) {
  return L("(" + n + ") 疑似违禁词 · " + DEFAULT_TITLE, "(" + n + ") Possible Banned Terms · " + DEFAULT_TITLE);
}

/* 标题是不是 alertTitle 自己设的提醒。不按标题里的中文认：拿开头的条数重新生成一遍，
   逐字相同才算，这样标题换成哪种语言都认得出。页面生命周期里语言不变（切换语言会重载页面，
   标题回到 <title>），所以此刻的 alertTitle 就是设标题时的那个。 */
function isAlertTitle(title) {
  var m = /^\((\d+)\) /.exec(String(title || ""));
  return !!m && String(title) === alertTitle(Number(m[1]));
}

/* 一条报警到达后标题该怎么变。state = { unseen, restoreTo }，返回新的 state，
   另带 title：要设的标题，null 表示不动。

   窗口在前台（active）时不动：人看得见。回放的报警永远不动：那是重连或刷新时补发的
   历史，不是新情况——和「有新版本」的提示同一条规矩。 */
function noteAlert(state, msg, active, currentTitle) {
  var s = state || { unseen: 0, restoreTo: null };
  if (!msg || msg.replay || active) {
    return { unseen: s.unseen, restoreTo: s.restoreTo, title: null };
  }
  var restoreTo = isAlertTitle(currentTitle) ? s.restoreTo : currentTitle;
  var unseen = s.unseen + 1;
  return { unseen: unseen, restoreTo: restoreTo, title: alertTitle(unseen) };
}

/* 窗口回到前台：恢复标题。只在标题仍是报警提醒时恢复——期间别的提示改过标题
   （如「有新版本」）就留着它。 */
function noteActive(state, currentTitle) {
  var s = state || { unseen: 0, restoreTo: null };
  if (!s.unseen) return { unseen: 0, restoreTo: null, title: null };
  var title = isAlertTitle(currentTitle) ? (s.restoreTo || DEFAULT_TITLE) : null;
  return { unseen: 0, restoreTo: null, title: title };
}

/* 桌面窗口的原生标题要单独同步：pywebview 不跟 document.title（见 app/window_attention.py）。
   bridge = { sent, seq }，返回新的 bridge，另带 send：要告诉窗口的条数，null 表示不用发。
   条数变了才发；force（页面刚加载、JS 桥刚就绪）时照发一次，把上一个页面留在窗口标题上的
   提醒清掉。seq 每发一次加一：Python 那边每次 JS 调用各开一个线程，靠它丢掉晚到的旧调用。 */
function windowAttentionUpdate(bridge, unseen, force) {
  var b = bridge || { sent: 0, seq: 0 };
  var n = unseen || 0;
  if (!force && n === b.sent) return { sent: b.sent, seq: b.seq, send: null };
  return { sent: n, seq: b.seq + 1, send: n };
}

/* 这条报警是不是别的场次的（换过主播，或上一场留下的）。没有场次标记的报警不判。 */
function isOtherSession(msg, currentSession) {
  return !!(msg && msg.session && currentSession && msg.session !== currentSession);
}

/* 面板上本场的条数比本场总数少时（最多留 50 条、手动清过、重连只补回最近的）
   要说出来，不然「50」看起来像是全部。shown 是面板上属于本场的条数。 */
function sessionNote(total, shown) {
  var n = shown || 0;
  // 手动清过面板时 n 可以是 0，本场只有 1 条也会走到这里：英文按 total 分单复数
  return total > n ? LN(total, "显示最近 " + n + " 条，本场共 " + total + " 条",
                        "Showing the latest " + n + " of 1 alert this session",
                        "Showing the latest " + n + " of " + total + " alerts this session") : "";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { alertTitle, isAlertTitle, noteAlert, noteActive, isOtherSession,
                     sessionNote, windowAttentionUpdate, ALERT_PANEL_CAP };
}
