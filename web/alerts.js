/* 报警面板的纯逻辑：窗口在后台时的标题提醒、「上一场」标记、「本场共 N 条」。

   单独成文件是为了可测（tests/alerts.test.mjs）；app.js 只负责把结果写进 DOM。 */

var ALERT_PANEL_CAP = 50;      // 面板只留最近这么多条（与 app.js / server.alerts 一致）
var DEFAULT_TITLE = "TikTok 直播同传";

function alertTitle(n) {
  return "(" + n + ") 疑似违禁词 · " + DEFAULT_TITLE;
}

function isAlertTitle(title) {
  return /^\(\d+\) 疑似违禁词 · /.test(String(title || ""));
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

/* 这条报警是不是别的场次的（换过主播，或上一场留下的）。没有场次标记的报警不判。 */
function isOtherSession(msg, currentSession) {
  return !!(msg && msg.session && currentSession && msg.session !== currentSession);
}

/* 面板上本场的条数比本场总数少时（最多留 50 条、手动清过、重连只补回最近的）
   要说出来，不然「50」看起来像是全部。shown 是面板上属于本场的条数。 */
function sessionNote(total, shown) {
  var n = shown || 0;
  return total > n ? "显示最近 " + n + " 条，本场共 " + total + " 条" : "";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { alertTitle, isAlertTitle, noteAlert, noteActive, isOtherSession,
                     sessionNote, ALERT_PANEL_CAP };
}
