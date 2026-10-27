// i18n: done
/* 顶栏「闪购」按钮和只读浮层的前端（web/flash.js）：文案、判断、渲染浮层的 DOM。
 * 后端（app/pipeline_flash.py）每 3 秒查一次本机闪购助手的 /status，规整成一份只带代码、数字和闪购助手原话的
 * 状态（hello 的 config.flash_sale，变化时再推一条 type:"flash_sale"）；文案全在这里按代码映射，中英成对写
 * （docs/i18n-style.md §1.3、§3 K）。页面不直接请求 9334：闪购助手的 CORS 只放 8765，控制端口可能是 8766–8774。
 *
 * 分两层，都不碰 app.js 里的状态（照 web/guard.js 的分法）：
 *   1. 纯函数（文案、排序、计数、未读判断、倒计时）：node 测试直接 require，tests/flash.test.mjs / flash.en.test.mjs；
 *   2. 渲染函数：第一个参数是 document，只用 createElement / createElementNS / appendChild / removeChild /
 *      setAttribute / classList / textContent，node 测试里的假 DOM 也能跑。数据一律 textContent，不用 innerHTML。
 * 加载方式照 web/guard.js：浏览器里是全局函数，Node 里走 module.exports。
 *
 * 几条不变量：
 * - **只读**：浮层只显示状态，唯一的按钮是「打开闪购助手」（app.js 发 open_flash_helper），不开始、停止、批准、续开；
 * - **只写观察，不猜原因**：页面、暂停、商品的状态都照闪购助手接口契约的代码说它是什么，不写「因为」「可能是」；
 * - **认不出的代码原样显示**：放进 translate="no" 的 span（它是数据），不给一句通用文案糊过去；
 * - **商品标题和记录正文是数据**：标题标 translate="no"，记录正文带 class i18n-data（同 app.js 的 markName /
 *   markBody），英文界面的检查不把它们当漏翻；记录正文不翻、原样显示（契约要求）。 */

"use strict";

// 浏览器里 L/LN 是 i18n.js 定义的全局函数（这里的 var 不会清掉它）；node 测试里从 i18n.js 取
if (typeof L === "undefined") { var I18N_ = require("./i18n.js"); var L = I18N_.L, LN = I18N_.LN, APP_NAME = I18N_.APP_NAME; }

var FLASH_ROWS = 40;           // 浮层先列这么多个商品，其余收进「还有 N 个」，点开才全列（一场最多约 300 个）
var FLASH_ALERTS = 5;          // 「最近记录」平时列最后几条（新的在上）；没看过的问题记录再多也全列出来
var FLASH_ALERTS_MAX = 20;     // 接口最多给 20 条
var FLASH_SEEN_TEXTS = 20;     // 记「已看过」时同一时刻最多记几句正文
var FLASH_X_MARK = "❌";   // 闪购助手的失败记录以 ❌ 开头（warn 级）；error 级本身就要人处理
// 顶栏按钮变色、浮层主句的先后（docs/flash-sale.md 的变色规则）；未读的问题记录排在这几条后面
var FLASH_ATTN_ORDER = ["stale", "page", "paused", "stuck", "bad_response"];

// ---- 文案：按代码映射。认不出返回 null，由调用方把原代码放进 translate="no" 的 span ----------------

/* 商品后台页面的状态（契约 page）。ok 不说话 */
function flashPageText(code) {
  switch (code) {
    case "ok": return "";
    case "no_page": return L("专用 Chrome 里没有打开商品后台（LIVE Manager）。", "LIVE Manager isn’t open in the dedicated Chrome.");
    case "login_required":
      return L("专用 Chrome 停在 TikTok 登录页，登录后才能继续。", "The dedicated Chrome is on the TikTok sign-in page. Sign in to continue.");
    case "loading": return L("商品后台正在打开…", "LIVE Manager is loading…");
    case "no_list": return L("商品后台没在 Buy now 列表上。", "LIVE Manager isn’t showing the Buy now list.");
    case "captcha": return L("页面上出现了验证码，要人去处理。", "A CAPTCHA appeared on the page. Someone needs to solve it.");
    case "error": return L("页面没有回应，或者读取出错。", "The page isn’t responding, or reading it failed.");
    default: return null;
  }
}

/* 闪购助手为什么停着（契约 paused_reason）。null 不说话 */
function flashPausedText(code) {
  switch (code) {
    case "not_started": return L("闪购助手开着，但还没开始运行。", "Flash Sale Helper is open but hasn’t started.");
    case "awaiting_approval":
      return L("专用 Chrome 里弹出了确认框，核对后点确定或取消。",
               "A confirmation is waiting in the dedicated Chrome. Check it, then confirm or cancel.");
    case "user_interaction":
      return L("已暂停：确认框里点了取消。要在闪购助手的面板上点继续。",
               "Paused: a confirmation was canceled. Resume it from the Flash Sale Helper panel.");
    case "captcha": return L("出现验证码，已暂停。", "Paused for a CAPTCHA.");
    case "error": return L("连续失败太多次，已暂停。", "Paused after too many failures in a row.");
    default: return null;
  }
}

/* 商品的闪购状态（契约 products[].state） */
function flashStateText(code) {
  switch (code) {
    case "live": return L("进行中", "Ongoing");
    case "between_rounds": return L("两轮之间", "Between rounds");
    case "stopped": return L("已停止", "Stopped");
    case "ended_by_user": return L("已手动结束", "Ended manually");
    case "pending": return L("待开始", "Ready to start");
    case "unknown": return L("暂时看不准", "Unclear for now");
    case "no_sale": return L("没有闪购", "No flash sale");
    default: return null;
  }
}

/* 状态药丸的形状和颜色（style.css .flash-pill[data-tone]）：已停止用三角，进行中用实心点，其余用空心圈 */
function flashStateTone(code) {
  switch (code) {
    case "stopped": return "stop";
    case "live": return "live";
    case "between_rounds": case "pending": return "wait";
    case "unknown": return "unclear";
    case "ended_by_user": case "no_sale": return "off";
    default: return "other";
  }
}

/* 记录的级别：图标形状（同 live-ui.js barIconFor 的语义：出错八角、提醒三角、说明 ⓘ）和读屏念的字。
 * 认不出的级别按 info 画 */
function flashLevel(level) {
  if (level === "error") return { level: "error", icon: "octagon", label: L("要处理", "Needs attention") };
  if (level === "warn") return { level: "warn", icon: "warn", label: L("提醒", "Warning") };
  return { level: "info", icon: "info", label: L("说明", "Info") };
}

/* 代码字段只认短字符串（后端已规整过，这里再兜一次：渲染的是闪购助手给的东西） */
function flashCode(v) {
  return typeof v === "string" ? v.slice(0, 40) : "";
}

// ---- 时间 -------------------------------------------------------------------------------------

function flashPad2(n) { return (n < 10 ? "0" : "") + n; }

/* 倒计时：不到 1 小时写 m:ss，以上写 h:mm:ss，已经过了写 0:00。两种语言写法一样（docs/i18n-style.md §2.4） */
function flashCountdown(ms) {
  var s = Number(ms);
  s = isFinite(s) && s > 0 ? Math.ceil(s / 1000) : 0;
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  return h > 0 ? h + ":" + flashPad2(m) + ":" + flashPad2(sec) : m + ":" + flashPad2(sec);
}

/* 本机时间 HH:MM:SS（24 小时制，§2.4）。不是有限的数写 --:--:-- */
function flashClock(ms) {
  var v = Number(ms);
  if (ms == null || !isFinite(v)) return "--:--:--";
  var d = new Date(v);
  return flashPad2(d.getHours()) + ":" + flashPad2(d.getMinutes()) + ":" + flashPad2(d.getSeconds());
}

/* 倒计时那一截的整句：live 是「还剩 m:ss」，between_rounds 是「m:ss 后开始下一轮」 */
function flashCountdownText(kind, at, now) {
  var t = flashCountdown(Number(at) - Number(now));
  return kind === "next" ? L(t + " 后开始下一轮", "Next round in " + t) : L("还剩 " + t, t + " left");
}

// ---- 记录：哪些算「要看的问题」、看过没有 ----------------------------------------------------------

/* 要人看的记录：error 级，或者 ❌ 开头的 warn 级（闪购助手的失败记录） */
function flashIsProblem(a) {
  if (!a || typeof a !== "object") return false;
  if (a.level === "error") return true;
  return a.level === "warn" && typeof a.text === "string" && a.text.replace(/^\s+/, "").charAt(0) === FLASH_X_MARK;
}

function flashAt(a) {
  var v = Number(a && a.at_ms);
  return a && a.at_ms != null && isFinite(v) ? v : 0;
}

/* 「已看过」记的是看过的问题记录里最新的那个 (at, text)：{at, texts}，texts 是时刻正好等于 at 的那几句正文
 * （同一毫秒可能不止一条）。比它新 = 时刻更晚，或时刻相同但正文没记过。null = 什么都没看过 */
function flashIsNewer(a, seen) {
  if (!seen) return true;
  var at = flashAt(a);
  if (at !== seen.at) return at > seen.at;
  return seen.texts.indexOf(String(a.text)) < 0;
}

/* 没看过的问题记录（保持接口给的先后：最早的在前） */
function flashUnread(alerts, seen) {
  if (!Array.isArray(alerts)) return [];
  return alerts.filter(function (a) { return flashIsProblem(a) && flashIsNewer(a, seen); });
}

/* 打开浮层时调：把眼下所有问题记录记成已看。只往前走，不会退回比 seen 更旧的位置 */
function flashSeenMark(alerts, seen) {
  var out = seen ? { at: seen.at, texts: seen.texts.slice() } : null;
  (Array.isArray(alerts) ? alerts : []).forEach(function (a) {
    if (!flashIsProblem(a)) return;
    var at = flashAt(a);
    var text = String(a.text);
    if (!out || at > out.at) out = { at: at, texts: [text] };
    else if (at === out.at && out.texts.indexOf(text) < 0 && out.texts.length < FLASH_SEEN_TEXTS) out.texts.push(text);
  });
  return out;
}

/* localStorage 里存的那一份（JSON）。读不出、形状不对一律当没看过 */
function flashParseSeen(raw) {
  if (typeof raw !== "string" || !raw) return null;
  var v;
  try { v = JSON.parse(raw); } catch (e) { return null; }
  if (!v || typeof v !== "object" || typeof v.at !== "number" || !isFinite(v.at) || !Array.isArray(v.texts)) return null;
  var texts = v.texts.filter(function (t) { return typeof t === "string"; }).slice(0, FLASH_SEEN_TEXTS);
  return { at: v.at, texts: texts };
}

// ---- 整体状态：胶囊、顶栏按钮、主句 -----------------------------------------------------------------

function flashAttention(st) {
  return st && Array.isArray(st.attention) ? st.attention.filter(function (c) { return typeof c === "string" && c; }) : [];
}

/* 要人处理吗：后端算的 attention（陈旧、页面、暂停、卡住、读不出），加上前端自己记的未读问题记录。
 * 连不上（闪购助手没开）不算 */
function flashNeedsAttention(st, unread) {
  if (!st || typeof st !== "object") return false;
  if (st.conn === "bad_response") return true;
  if (st.conn !== "ok") return false;
  return flashAttention(st).length > 0 || st.stale === true || unread > 0;
}

/* 浮层标题旁的状态胶囊：{tone, text}。tone 是 ok、attn、idle、off、checking 之一（style.css .flash-state） */
function flashChip(st, unread) {
  var conn = st && st.conn;
  if (conn === "unreachable") return { tone: "off", text: L("没在运行", "Not running") };
  if (flashNeedsAttention(st, unread)) return { tone: "attn", text: L("要处理", "Needs attention") };
  if (conn !== "ok") return { tone: "checking", text: L("正在查看…", "Checking…") };
  if (st.running !== true) return { tone: "idle", text: L("未开始", "Not started") };
  return { tone: "ok", text: L("运行中", "Running") };
}

/* 顶栏按钮：{hidden, attn, title}。st 为 null（hello 里没有 config.flash_sale：没装闪购助手）就整颗不出现。
 * title 同时写进 aria-label：颜色之外，读屏也念得到要不要处理 */
function flashButtonState(st, unread) {
  var name = L("闪购助手", "Flash Sale Helper");
  if (!st || typeof st !== "object") return { hidden: true, attn: false, title: name };
  var chip = flashChip(st, unread);
  return { hidden: false, attn: chip.tone === "attn", title: name + " · " + chip.text };
}

/* 卡住的商品数：按商品表里的 stuck 数（后端另发的 stuck_count 是同一个数，商品表最多 300 个、卡住的排在最前，不会被截掉） */
function flashStuckCount(st) {
  var list = st && Array.isArray(st.products) ? st.products : [];
  return list.filter(function (p) { return p && p.stuck === true; }).length;
}

/* 浮层顶上的几句话，最要紧的在前：[{text, code, attn}]。code 是要原样显示的代码（放进 translate="no"），
 * attn 表示这一句是要人处理的。先排要处理的（按 FLASH_ATTN_ORDER，再是认不出的提醒代码，最后是未读记录），
 * 再排只是说明现状的（页面正在打开、还没开始运行……） */
function flashLines(st, unread) {
  if (!st || typeof st !== "object") return [];
  var conn = st.conn;
  if (conn === "unreachable") {
    return [{ text: L("闪购助手没在运行。", "Flash Sale Helper isn’t running."), attn: false },
            { text: L("127.0.0.1:9334 连不上，每 15 秒再查一次。",
                      "127.0.0.1:9334 isn’t answering. Checking again every 15 sec."), attn: false }];
  }
  if (conn === "bad_response") {
    return [{ text: L("127.0.0.1:9334 返回的内容读不出来。", "Couldn’t read the response from 127.0.0.1:9334."), attn: true }];
  }
  if (conn !== "ok") return [{ text: L("正在查看闪购助手的状态…", "Checking Flash Sale Helper…"), attn: false }];

  var attention = flashAttention(st);
  var byCode = {};
  var info = [];
  if (st.stale === true || attention.indexOf("stale") >= 0) {
    var when = st.updated_ms != null && isFinite(Number(st.updated_ms))
      ? L("上次更新 " + flashClock(st.updated_ms) + "。", "Last updated at " + flashClock(st.updated_ms) + ".")
      : L("还没有收到过状态。", "No status has come in yet.");
    byCode.stale = [{ text: L("闪购助手没有更新状态。", "Flash Sale Helper hasn’t updated its status."), attn: true },
                    { text: when, attn: true }];
  }
  var page = flashCode(st.page);
  if (page && page !== "ok") {
    var pageText = flashPageText(page);
    var pageLine = pageText ? { text: pageText } : { text: L("页面状态：", "Page status: "), code: page };
    var pageAttn = page === "login_required" || page === "captcha" || page === "error" ||
                   (page === "no_list" && st.running === true) || attention.indexOf("page") >= 0;
    pageLine.attn = pageAttn;
    if (pageAttn) byCode.page = [pageLine]; else info.push(pageLine);
  }
  var paused = flashCode(st.paused_reason);
  if (paused) {
    var pausedText = flashPausedText(paused);
    var pausedLine = pausedText ? { text: pausedText } : { text: L("已暂停：", "Paused: "), code: paused };
    pausedLine.attn = paused !== "not_started";
    if (pausedLine.attn) byCode.paused = [pausedLine]; else info.push(pausedLine);
  }
  var stuck = flashStuckCount(st);
  if (stuck > 0 || attention.indexOf("stuck") >= 0) {
    byCode.stuck = [{ attn: true, text: stuck > 0
      ? LN(stuck, stuck + " 个商品停了超过 1 分钟。", "1 product has stayed stopped for over 1 min.",
           stuck + " products have stayed stopped for over 1 min.")
      : L("有商品停了超过 1 分钟。", "A product has stayed stopped for over 1 min.") }];
  }
  var lines = [];
  FLASH_ATTN_ORDER.forEach(function (code) { if (byCode[code]) lines = lines.concat(byCode[code]); });
  attention.forEach(function (code) {               // 后端以后加的提醒代码：原样显示，不丢
    if (FLASH_ATTN_ORDER.indexOf(code) < 0) lines.push({ text: L("要处理：", "Needs attention: "), code: flashCode(code), attn: true });
  });
  if (unread > 0) {
    lines.push({ attn: true, text: LN(unread, "最近记录里有 " + unread + " 条新的问题。", "1 new problem in Recent Activity.",
                                      unread + " new problems in Recent Activity.") });
  }
  lines = lines.concat(info);
  var products = Array.isArray(st.products) ? st.products : [];
  if (!lines.length && page === "ok" && !products.length) {
    lines.push({ text: L("商品列表里还没有商品。", "No products in the list yet."), attn: false });
  }
  return lines;
}

/* 一句话的纯文字（code 接在后面），给测试和 title 用 */
function flashLineText(line) {
  return line ? line.text + (line.code || "") : "";
}

// ---- 商品 --------------------------------------------------------------------------------------

/* 停着、等闪购助手续开的那种：允许自动续开，闪购助手在运行、没暂停（后端从这时起计 60 秒，到点标 stuck） */
function flashWaitingRenewal(p, st) {
  return !!p && p.state === "stopped" && p.enabled !== false && !!st && st.running === true && !st.paused_reason;
}

/* 排序的先后（数小的在前），和后端 app/flash_sale.py 的 _rank 同一张表：要人看的排前面——卡住的（停着、该续开、
 * 连续 60 秒没续开）第一，然后待开始、看不准；其次进行中、两轮之间；再其次还在续开计时里的、不自动续开的已停止
 * （续开实测 2–40 秒，排在前面会在列表顶上一闪一闪），没有闪购、手动结束；认不出的状态最后。
 * st 不参与排序（卡住与否后端已经算进 stuck），留着参数是给以后按整体状态排的余地 */
function flashRank(p) {
  if (p && p.stuck === true) return 0;
  switch (p && p.state) {
    case "pending": return 1;
    case "unknown": return 2;
    case "live": return 3;
    case "between_rounds": return 4;
    case "stopped": return 5;
    case "no_sale": return 6;
    case "ended_by_user": return 7;
    default: return 8;
  }
}

/* 稳定排序：同一档里保持闪购助手给的顺序。后端已按同一张表排过，这里再排一次不改变顺序，只是兜底 */
function flashSortProducts(list) {
  var items = (Array.isArray(list) ? list : []).filter(function (p) { return p && typeof p === "object"; });
  return items.map(function (p, i) { return { p: p, i: i, r: flashRank(p) }; })
    .sort(function (a, b) { return a.r - b.r || a.i - b.i; })
    .map(function (x) { return x.p; });
}

/* 摘要：「共 9 个商品：已停止 1 · 待开始 1 · …」，只列数量不为 0 的，先后同排序 */
function flashSummary(list) {
  var products = (Array.isArray(list) ? list : []).filter(function (p) { return p && typeof p === "object"; });
  if (!products.length) return "";
  var counts = {};
  products.forEach(function (p) {
    var key = flashStateText(p.state) == null ? "other" : p.state;
    counts[key] = (counts[key] || 0) + 1;
  });
  var parts = [];
  function add(key, text) { if (counts[key]) parts.push(text(counts[key])); }
  add("stopped", function (n) { return L("已停止 " + n, n + " stopped"); });
  add("pending", function (n) { return L("待开始 " + n, n + " ready to start"); });
  add("unknown", function (n) { return L("看不准 " + n, n + " unclear"); });
  add("live", function (n) { return L("进行中 " + n, n + " ongoing"); });
  add("between_rounds", function (n) { return L("两轮之间 " + n, n + " between rounds"); });
  add("no_sale", function (n) { return L("没有闪购 " + n, n + " with no flash sale"); });
  add("ended_by_user", function (n) { return L("已手动结束 " + n, n + " ended manually"); });
  add("other", function (n) { return L("其他 " + n, n + " other"); });
  var total = products.length;
  return LN(total, "共 " + total + " 个商品：", "1 product: ", total + " products: ") + parts.join(" · ");
}

function flashInt(v) {
  var n = Number(v);
  return v != null && typeof v !== "boolean" && isFinite(n) ? Math.round(n) : null;
}

/* 一个商品的一行：{title, state: {text, code, tone}, detail: [文字 | {countdown: kind, at}]}。
 * 倒计时那一截带时刻，渲染成单独的 span，每秒只改它的字 */
function flashProductParts(p, st, now) {
  var code = flashCode(p && p.state);
  var known = flashStateText(code);
  var state = known != null ? { text: known, code: "", tone: flashStateTone(code) }
                            : { text: "", code: code, tone: "other" };
  var detail = [];
  if (code === "stopped" && p.stuck === true) detail.push(L("停了超过 1 分钟", "Stopped for over 1 min"));
  else if (flashWaitingRenewal(p, st)) detail.push(L("等闪购助手续开", "Waiting for Flash Sale Helper to renew"));
  var ends = flashInt(p && p.ends_ms);
  var next = flashInt(p && p.next_ms);
  if (code === "live" && ends != null) detail.push({ countdown: "ends", at: ends });
  if (code === "between_rounds" && next != null) detail.push({ countdown: "next", at: next });
  var round = flashInt(p && p.round);
  if (round != null && round > 0) detail.push(L("第 " + round + " 轮", "Round " + round));
  var left = flashInt(p && p.stock_left);
  var total = flashInt(p && p.stock_total);
  if (left != null && total != null) detail.push(L("库存 " + left + "/" + total, left + " of " + total + " in stock"));
  else if (left != null) detail.push(L("库存 " + left, left + " in stock"));
  var renewals = flashInt(p && p.renewals);
  if (renewals != null && renewals >= 1) {
    detail.push(LN(renewals, "续开 " + renewals + " 次", "Renewed once", "Renewed " + renewals + " times"));
  }
  if (p && p.enabled === false) detail.push(L("不自动续开", "Auto-renew off"));
  var title = typeof (p && p.title) === "string" && p.title ? p.title : (p && p.id != null ? String(p.id) : "");
  return { title: title, state: state, detail: detail };
}

/* 列多少行：平时最多 FLASH_ROWS，点开「还有 N 个」后全列。{shown, more} */
function flashRowsToShow(total, expanded) {
  var n = Math.max(0, total | 0);
  if (expanded || n <= FLASH_ROWS) return { shown: n, more: 0 };
  return { shown: FLASH_ROWS, more: n - FLASH_ROWS };
}

function flashMoreText(more) {
  return LN(more, "还有 " + more + " 个", "1 more", more + " more");
}

/* 「最近记录」列哪几条：新的在上。平时最后 FLASH_ALERTS 条，再加上没看过的问题记录——它们可能比最后几条早，
 * 也要列出来，不然按钮变了色、浮层里却找不到是哪一条（接口最多给 20 条，全列也就 20 条）。unread 是 flashUnread 的结果 */
function flashAlertsToShow(alerts, unread) {
  var list = (Array.isArray(alerts) ? alerts : []).slice(-FLASH_ALERTS_MAX);
  var keep = Array.isArray(unread) ? unread : [];
  return list.filter(function (a, i) {
    return a && typeof a === "object" && (i >= list.length - FLASH_ALERTS || keep.indexOf(a) >= 0);
  }).reverse();
}

// ---- 渲染（document 由调用方传进来；只用 textContent，不用 innerHTML） ------------------------------

var FLASH_SVG_NS = "http://www.w3.org/2000/svg";

function flashEl(doc, tag, cls, text) {
  var el = doc.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

/* 原样显示的代码、商品标题：名字类数据，浏览器翻译不去改 */
function flashDataSpan(doc, text, cls) {
  var span = flashEl(doc, "span", cls || "", text);
  span.setAttribute("translate", "no");
  return span;
}

function flashIcon(doc, name) {
  var svg = doc.createElementNS(FLASH_SVG_NS, "svg");
  svg.setAttribute("class", "i");
  svg.setAttribute("aria-hidden", "true");
  var use = doc.createElementNS(FLASH_SVG_NS, "use");
  use.setAttribute("href", "#i-" + name);
  svg.appendChild(use);
  return svg;
}

function flashClear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

/* 一句话写进 el：文字 + 原样的代码（translate="no"） */
function flashFillLine(doc, el, line) {
  flashClear(el);
  if (!line) return;
  el.appendChild(doc.createTextNode(line.text));
  if (line.code) el.appendChild(flashDataSpan(doc, line.code, "flash-code"));
}

function flashProductRow(doc, p, st, now, countdowns) {
  var parts = flashProductParts(p, st, now);
  var li = flashEl(doc, "li", "flash-row");
  li.setAttribute("data-state", parts.state.tone);
  var top = flashEl(doc, "div", "flash-row-top");
  // 外层负责省略号和悬停看全名（title 就是里面那段数据原样再写一遍）；里面那段是名字，标 translate="no"
  var title = flashEl(doc, "span", "flash-title");
  title.setAttribute("title", parts.title);
  title.appendChild(flashDataSpan(doc, parts.title));
  top.appendChild(title);
  var pill = flashEl(doc, "span", "flash-pill", parts.state.text || null);
  pill.setAttribute("data-tone", parts.state.tone);
  if (parts.state.code) pill.appendChild(flashDataSpan(doc, parts.state.code, "flash-code"));
  top.appendChild(pill);
  li.appendChild(top);
  if (parts.detail.length) {
    var detail = flashEl(doc, "div", "flash-row-detail");
    parts.detail.forEach(function (item, i) {
      if (i) detail.appendChild(doc.createTextNode(" · "));
      if (typeof item === "string") { detail.appendChild(doc.createTextNode(item)); return; }
      var span = flashEl(doc, "span", "flash-countdown", flashCountdownText(item.countdown, item.at, now));
      countdowns.push({ el: span, kind: item.countdown, at: item.at });
      detail.appendChild(span);
    });
    li.appendChild(detail);
  }
  return li;
}

function flashAlertRow(doc, a, isNew) {
  var lv = flashLevel(a.level);
  var li = flashEl(doc, "li", "flash-alert" + (isNew ? " new" : ""));
  li.setAttribute("data-level", lv.level);
  li.appendChild(flashEl(doc, "span", "flash-time", flashClock(a.at_ms)));
  var icon = flashEl(doc, "span", "flash-lv");
  icon.setAttribute("role", "img");
  icon.setAttribute("aria-label", lv.label);
  icon.setAttribute("title", lv.label);
  icon.appendChild(flashIcon(doc, lv.icon));
  li.appendChild(icon);
  // 闪购助手写的正文：原样显示，留给浏览器翻译（class i18n-data，不标 translate="no"）
  var body = flashEl(doc, "span", "flash-alert-text", typeof a.text === "string" ? a.text : "");
  body.classList.add("i18n-data");
  li.appendChild(body);
  return li;
}

/* 把整张浮层按 st 画一遍。els：{state, headline, detail, summary, products, more, alertsTitle, alerts, version}
 * （index.html 里 #flash-panel 的各个节点）；view：{now, expanded, unread}，unread 是没看过的问题记录。
 * 收到状态就画，不等浮层打开（G10 的扫描也扫隐藏的元素）。返回这次画出来的倒计时 [{el, kind, at}]，
 * app.js 每秒交给 flashTick 只改它们的字 */
function renderFlashPanel(doc, els, st, view) {
  view = view || {};
  var now = view.now != null ? view.now : Date.now();
  var unread = Array.isArray(view.unread) ? view.unread : [];
  var countdowns = [];
  var chip = flashChip(st, unread.length);
  els.state.textContent = chip.text;
  els.state.setAttribute("data-tone", chip.tone);

  var lines = flashLines(st, unread.length);
  flashFillLine(doc, els.headline, lines[0]);
  els.headline.classList.toggle("hidden", !lines.length);
  els.headline.classList.toggle("attn", !!(lines[0] && lines[0].attn));
  flashClear(els.detail);
  lines.slice(1).forEach(function (line) {
    var p = flashEl(doc, "p", "flash-line" + (line.attn ? " attn" : ""));
    flashFillLine(doc, p, line);
    els.detail.appendChild(p);
  });
  els.detail.classList.toggle("hidden", lines.length < 2);

  var products = st && Array.isArray(st.products) ? flashSortProducts(st.products, st) : [];
  var summary = flashSummary(products);
  els.summary.textContent = summary;
  els.summary.classList.toggle("hidden", !summary);
  flashClear(els.products);
  // 陈旧时整张表换成次要文字色（不用 opacity：叠出来的对比度会掉到 AA 以下）
  els.products.classList.toggle("stale", !!(st && st.stale === true));
  var rows = flashRowsToShow(products.length, !!view.expanded);
  products.slice(0, rows.shown).forEach(function (p) {
    els.products.appendChild(flashProductRow(doc, p, st, now, countdowns));
  });
  els.products.classList.toggle("hidden", !products.length);
  els.more.textContent = rows.more ? flashMoreText(rows.more) : "";
  els.more.classList.toggle("hidden", !rows.more);

  var alerts = flashAlertsToShow(st && st.alerts, unread);
  flashClear(els.alerts);
  alerts.forEach(function (a) { els.alerts.appendChild(flashAlertRow(doc, a, unread.indexOf(a) >= 0)); });
  els.alerts.classList.toggle("hidden", !alerts.length);
  els.alertsTitle.classList.toggle("hidden", !alerts.length);

  var version = st && typeof st.app_version === "string" ? st.app_version.slice(0, 20) : "";
  els.version.textContent = version ? L("版本 " + version, "Version " + version) : "";
  return countdowns;
}

/* 每秒调一次：只改倒计时 span 的字，不重画整张表（滚动位置不跳） */
function flashTick(countdowns, now) {
  (countdowns || []).forEach(function (c) { c.el.textContent = flashCountdownText(c.kind, c.at, now); });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    FLASH_ROWS, FLASH_ALERTS, FLASH_ALERTS_MAX, FLASH_ATTN_ORDER,
    flashPageText, flashPausedText, flashStateText, flashStateTone, flashLevel,
    flashCountdown, flashClock, flashCountdownText,
    flashIsProblem, flashIsNewer, flashUnread, flashSeenMark, flashParseSeen,
    flashNeedsAttention, flashChip, flashButtonState, flashLines, flashLineText,
    flashWaitingRenewal, flashRank, flashSortProducts, flashSummary, flashProductParts,
    flashRowsToShow, flashMoreText, flashAlertsToShow,
    renderFlashPanel, flashTick,
  };
}
