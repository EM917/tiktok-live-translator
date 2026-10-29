// i18n: done
/* 顶栏「闪购」按钮和浮层的前端（web/flash.js）：文案、判断、渲染浮层的 DOM。
 * 后端（app/pipeline_flash.py）每 3 秒查一次本机闪购助手的 /status，规整成一份只带代码、数字和闪购助手原话的
 * 状态（hello 的 config.flash_sale，变化时再推一条 type:"flash_sale"）；文案全在这里按代码映射，中英成对写
 * （docs/i18n-style.md §1.3、§3 K）。页面不直接请求 9334：闪购助手的 CORS 只放 8765，控制端口可能是 8766–8774。
 * 浮层顶部那一排「开始」「停止」「继续」（闪购助手 0.8.0 起，docs/flash-sale.md §9）也一样：页面只发
 * {type:"flash_control", action}，后端读密钥、POST 过去，结果只回代码（type:"flash_control_result"），句子在这里。
 *
 * 分两层，都不碰 app.js 里的状态（照 web/guard.js 的分法）：
 *   1. 纯函数（文案、排序、计数、未读判断、倒计时、那一排按钮显示什么）：node 测试直接 require，
 *      tests/flash.test.mjs / flash.en.test.mjs / flash-control.test.mjs / flash-control.en.test.mjs；
 *   2. 渲染函数：第一个参数是 document，只用 createElement / createElementNS / appendChild / removeChild /
 *      setAttribute / classList / textContent，node 测试里的假 DOM 也能跑。数据一律 textContent，不用 innerHTML。
 * 加载方式照 web/guard.js：浏览器里是全局函数，Node 里走 module.exports。
 *
 * 几条不变量：
 * - **只替人按那三个按钮**：浮层里能做的只有「打开闪购助手」（app.js 发 open_flash_helper）和闪购助手面板上那个大按钮的
 *   开始、停止、继续（发 flash_control）；不新建闪购、不改价、不加商品、不批准、不续开。版本认得出且低于 0.8.0、
 *   连不上、读不出时那一排只剩「打开」；按钮显示什么、能不能点全由 flashControlView 按状态定；
 * - **两边一字不差的规则**：等续开（flashWaitingRenewal）、排序（flashSortProducts）、指令生效（flashControlDone）、
 *   开始后的过渡（flashStarting）和后端 app/flash_sale.py 各有一份，两边吃同一张表
 *   tests/fixtures/flash_sale/shared-rules.json；
 * - **只写观察，不猜原因**：页面、暂停、商品的状态都照闪购助手接口契约的代码说它是什么，不写「因为」「可能是」；
 *   指令的结果只说闪购助手回了什么、本程序走到了哪一步；
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

// 开始、停止、继续（docs/flash-sale.md §9）
var FLASH_ACTIONS = ["start", "stop", "resume"];        // 同后端 flash_sale.CONTROL_ACTIONS
var FLASH_CONTROL_SINCE = [0, 8, 0];                     // 闪购助手从这个版本起才有控制接口（契约「控制(0.8.0 起)」）
// 「开始」点两下，两下都发给后端（第一下 step=arm、第二下 step=fire，带同一个编号），后端照同样的两个数再判一次
// （app/flash_sale.py START_CONFIRM_SEC、START_GUARD_SEC，tests/test_flash_sale.py 钉着两边一样）
var FLASH_START_CONFIRM_MS = 6000;                       // 「开始」点了第一下以后这么久里再点才发，到点复原（同清空字幕）
var FLASH_START_GUARD_MS = 400;                          // 武装后这么快的第二下不算：双击一次不该就发出去（开始会刷新页面）
// 停止没被接受时后端再试（同后端 flash_sale.STOP_RETRY_SEC、STOP_RETRY_GAP_SEC，tests/test_flash_sale.py 钉着两边一样）
var FLASH_STOP_RETRY_SEC = 30;
var FLASH_STOP_RETRY_GAP_SEC = 3;
// flash_control_result 的 error 里本程序自己的代码，同后端 flash_sale.CONTROL_OWN_ERRORS（tests/test_flash_sale.py 钉着）；
// 其余的是 http_<状态码>，或者闪购助手 409 给的代码
var FLASH_OWN_ERRORS = ["no_status", "no_token", "unreachable", "no_answer", "bad_reply", "no_control", "failed", "not_seen",
                        "local_only", "not_confirmed"];
var FLASH_READY_PAGES = ["ok", "no_list"];               // 能开始、继续的页面状态（decisions-step2：其余的开始不可用）
// 开始、继续被接受、等生效的那段时间里不算要人处理的页面状态，同后端 flash_sale.START_QUIET_PAGES
var FLASH_QUIET_PAGES = ["loading", "error", "no_list"];

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

/* 闪购助手为什么停着（契约 paused_reason）。null 不说话。
 * resumeHere：浮层顶部这时有「继续」（flashControlView 的 resume.shown）。有的话 user_interaction 那句不再叫人去闪购助手的
 * 面板上点继续——按钮就在上面，点了以后等生效那几秒按钮上还写着「正在继续…」；版本太旧、只剩「打开」时照旧指过去 */
function flashPausedText(code, resumeHere) {
  switch (code) {
    case "not_started": return L("闪购助手开着，但还没开始运行。", "Flash Sale Helper is open but hasn’t started.");
    case "awaiting_approval":
      return L("专用 Chrome 里弹出了确认框，核对后点确定或取消。",
               "A confirmation is waiting in the dedicated Chrome. Check it, then confirm or cancel.");
    case "user_interaction":
      return resumeHere
        ? L("已暂停：确认框里点了取消。", "Paused: a confirmation was canceled.")
        : L("已暂停：确认框里点了取消。要在闪购助手的面板上点继续。",
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

/* 闪购助手为什么故意不续开这个商品（契约 products[].skip，0.8.2 起；前五个的文案是闪购助手会话 10-27 给的，
 * next_to_pin 是 0.9.2 起的：置顶商品正下方、下一个要置顶的那个，文案照它的说明写）。0.9.2.1 起有置顶时只标
 * next_to_pin、没置顶（或看不出）时才标 slot_1、no_position（docs/flash-sale.md §3），这里不分版本，每个代码的意思没变。
 * top_n 是 0.9.2.2 起的：Buy now 列表从上往下的前 N 个（N 在闪购助手面板上设，默认 2），不点 Start、也不重新设置；
 * 0.9.2.2 起不再发 slot_1、next_to_pin，这两句留给旧版（用户 10-29 定，文案照 slot_1 的句式）。
 * 认不出的返回 null，由调用方写「不续开：」加原样的代码 */
function flashSkipText(code) {
  switch (code) {
    case "slot_1": return L("1 号位，不续开", "Slot 1, not renewed");
    case "top_n": return L("排在最前面，不续开", "Top of the list, not renewed");
    case "next_to_pin": return L("下一个要置顶，不续开", "Next to pin, not renewed");
    case "no_stock": return L("没有库存，不续开", "Out of stock, not renewed");
    case "max_renewals": return L("续开次数到上限", "Renewal limit reached");
    case "auto_start_off": return L("没开自动开始", "Auto-start off");
    case "no_position": return L("读不到序号，先不处理", "Position unread, skipped for now");
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
 * 连不上（闪购助手没开）不算。st.stale 是看到的样子（开始后的过渡里也照实为 true），所以这里和后端 attention()
 * 一样，开始、继续被接受、等生效的那段时间（flashStarting）不拿它变色 */
function flashNeedsAttention(st, unread) {
  if (!st || typeof st !== "object") return false;
  if (st.conn === "bad_response") return true;
  if (st.conn !== "ok") return false;
  return flashAttention(st).length > 0 || (st.stale === true && !flashStarting(st)) || unread > 0;
}

/* 浮层标题旁的状态胶囊：{tone, text}。tone 是 ok、attn、idle、off、checking 之一（style.css .flash-state）。
 * 开始、继续被接受、等生效的那段时间（flashStarting）不写「未开始」：按钮上写着「正在开始…」，胶囊（和顶栏按钮的 title）
 * 要是写「未开始」，很容易读成「没开起来」，又去点一次——用户 10-27 定这段过渡就是为了免得这样。写同一句「正在开始…」
 * 「正在继续…」，颜色照 idle 不变；要处理的照常先说 */
function flashChip(st, unread) {
  var conn = st && st.conn;
  if (conn === "unreachable") return { tone: "off", text: L("没在运行", "Not running") };
  if (flashNeedsAttention(st, unread)) return { tone: "attn", text: L("要处理", "Needs attention") };
  if (conn !== "ok") return { tone: "checking", text: L("正在查看…", "Checking…") };
  if (flashStarting(st)) return { tone: "idle", text: flashWaitingText(st.control.action) };
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
 * 再排只是说明现状的（页面正在打开、还没开始运行……）。ui：{gated}，同 flashControlView（发布闸关着时没有「继续」，
 * 确认框里点了取消的暂停照旧叫人去闪购助手的面板上点）。
 * 开始、继续被接受、等生效的那段时间（flashStarting，最多 30 秒，用户 10-27 定）：POST /start 先刷新页面，
 * 这时页面会短暂报加载中、出错，列表回来之前不在 Buy now 上，updated_at 也会停下。这几种（同后端
 * START_QUIET_PAGES）和陈旧的那两句都不写、不变色，「还没开始运行」也不写，改写一句「正在开始…（闪购助手在刷新页面）」
 * （继续写「正在继续…」），排在要处理的后面。登录、验证码、暂停、卡住、读不出照常 */
function flashLines(st, unread, ui) {
  if (!st || typeof st !== "object") return [];
  var conn = st.conn;
  // 连不上 = 连接被拒，9334 上没人在听：契约里这就是闪购助手没开，不变色
  if (conn === "unreachable") {
    return [{ text: L("闪购助手没在运行。", "Flash Sale Helper isn’t running."), attn: false },
            { text: L("127.0.0.1:9334 连不上，每 15 秒再查一次。",
                      "Can’t connect to 127.0.0.1:9334. Checking again every 15 sec."), attn: false }];
  }
  // 9334 上有东西但没拿到能用的回话：要人看。no_answer（后端 why）是连得上、2 秒内没回完——闪购助手卡住时就是这样，
  // 只写看到的，不说它卡住了、也不说它没在运行
  if (conn === "bad_response") {
    if (st.why === "no_answer") {
      return [{ text: L("127.0.0.1:9334 在 2 秒内没有回应。", "127.0.0.1:9334 didn’t answer within 2 sec."), attn: true }];
    }
    return [{ text: L("127.0.0.1:9334 返回的内容读不出来。", "Couldn’t read the response from 127.0.0.1:9334."), attn: true }];
  }
  if (conn !== "ok") return [{ text: L("正在查看闪购助手的状态…", "Checking Flash Sale Helper…"), attn: false }];

  var attention = flashAttention(st);
  var quiet = flashStarting(st);
  var byCode = {};
  var info = [];
  if ((st.stale === true && !quiet) || attention.indexOf("stale") >= 0) {
    var when = st.updated_ms != null && isFinite(Number(st.updated_ms))
      ? L("上次更新 " + flashClock(st.updated_ms) + "。", "Last updated at " + flashClock(st.updated_ms) + ".")
      : L("还没有收到过状态。", "No status has come in yet.");
    byCode.stale = [{ text: L("闪购助手没有更新状态。", "Flash Sale Helper hasn’t updated its status."), attn: true },
                    { text: when, attn: true }];
  }
  var page = flashCode(st.page);
  if (page && page !== "ok" && !(quiet && FLASH_QUIET_PAGES.indexOf(page) >= 0)) {
    var pageText = flashPageText(page);
    var pageLine = pageText ? { text: pageText } : { text: L("页面状态：", "Page status: "), code: page };
    var pageAttn = page === "login_required" || page === "captcha" || page === "error" ||
                   (page === "no_list" && st.running === true) || attention.indexOf("page") >= 0;
    pageLine.attn = pageAttn;
    if (pageAttn) byCode.page = [pageLine]; else info.push(pageLine);
  }
  var paused = flashCode(st.paused_reason);
  if (paused && !(quiet && paused === "not_started")) {
    var pausedText = flashPausedText(paused, flashControlView(st, { gated: !!(ui && ui.gated) }).resume.shown);
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
  if (quiet) lines.push({ text: flashStartingText(st.control.action), attn: false });
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

/* 停着、等闪购助手续开的那种：允许自动续开、闪购助手没说故意不续开（skip 为 null，0.8.2 以前没有这个键也算），
 * 闪购助手在运行、没暂停（后端从这时起计 60 秒，到点标 stuck）。
 * 条件和后端 app/flash_sale.stuck_candidate 一字不差：enabled 只认 true。enabled 读不出（null）的商品后端永远不算卡住，
 * 这里要是写「等续开」，那句话就会一直挂着、60 秒后也不会变成「停了超过 1 分钟」。
 * tests/fixtures/flash_sale/shared-rules.json 的 waiting_renewal 两边各测一遍 */
function flashWaitingRenewal(p, st) {
  return !!p && p.state === "stopped" && p.enabled === true && p.skip == null &&
         !!st && st.running === true && !st.paused_reason;
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

/* 卡片序号（契约 position，0.8.2 起）：只认整数（同后端 product_sort_key：int 而且不是 bool），别的一律当没有 */
function flashPosition(p) {
  var v = p && p.position;
  return typeof v === "number" && isFinite(v) && Math.floor(v) === v ? v : null;
}

/* 先按 flashRank 分档；同一档里按卡片序号从小到大，没有序号的排在最后；再相同就保持闪购助手给的顺序（稳定排序）。
 * 和后端 app/flash_sale.product_sort_key 一字不差，tests/fixtures/flash_sale/shared-rules.json 的 sort 两边各测一遍。
 * 后端已排过，这里再排一次不改变顺序，只是兜底 */
function flashSortProducts(list) {
  var items = (Array.isArray(list) ? list : []).filter(function (p) { return p && typeof p === "object"; });
  return items.map(function (p, i) { return { p: p, i: i, r: flashRank(p), pos: flashPosition(p) }; })
    .sort(function (a, b) {
      if (a.r !== b.r) return a.r - b.r;
      if ((a.pos == null) !== (b.pos == null)) return a.pos == null ? 1 : -1;
      if (a.pos != null && a.pos !== b.pos) return a.pos - b.pos;
      return a.i - b.i;
    })
    .map(function (x) { return x.p; });
}

/* 摘要：「共 9 个商品：已停止 1 · 待开始 1 · …」，只列数量不为 0 的，先后同排序。
 * total 是后端的 products_total（闪购助手给了多少个，含后端超出 300 个截掉的）：比列表长时，总数按它写，
 * 末尾再加一项「未列出 n」，各项加起来正好是总数——截掉的不静默丢掉（闪购助手会话 10-27：一场最多 300 个）。
 * 拆成两半给渲染用：{lead: 「共 9 个商品：」, parts: [「已停止 1」, …]}，没有商品返回 null。浮层里每段一个
 * .flash-summary-part（inline-block，见 renderFlashPanel）：440px 放不下时整段换行，不把「两轮之间 1」
 * 「1 between rounds」从中间折开 */
function flashSummaryParts(list, total) {
  var products = (Array.isArray(list) ? list : []).filter(function (p) { return p && typeof p === "object"; });
  var all = flashInt(total);
  var unlisted = all != null && all > products.length ? all - products.length : 0;
  if (!products.length && !unlisted) return null;
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
  if (unlisted) parts.push(L("未列出 " + unlisted, unlisted + " not listed"));
  var count = products.length + unlisted;
  return { lead: LN(count, "共 " + count + " 个商品：", "1 product: ", count + " products: "), parts: parts };
}

/* 摘要的整句（flashSummaryParts 拼起来）：没有商品是空串 */
function flashSummary(list, total) {
  var s = flashSummaryParts(list, total);
  return s ? s.lead + s.parts.join(" · ") : "";
}

function flashInt(v) {
  var n = Number(v);
  return v != null && typeof v !== "boolean" && isFinite(n) ? Math.round(n) : null;
}

/* 一个商品的一行：{title, position, state: {text, code, tone}, detail: [文字 | {countdown: kind, at} | {text, code}]}。
 * 倒计时那一截带时刻，渲染成单独的 span，每秒只改它的字；{text, code} 是文字接一段原样的代码（认不出的不续开原因）。
 * position 是卡片序号（标题前写 #12），没有就是 null。闪购助手说了故意不续开（skip）的，原因写在细节的最前面 */
function flashProductParts(p, st, now) {
  var code = flashCode(p && p.state);
  var known = flashStateText(code);
  var state = known != null ? { text: known, code: "", tone: flashStateTone(code) }
                            : { text: "", code: code, tone: "other" };
  var detail = [];
  var skip = flashCode(p && p.skip);
  if (skip) {
    var skipText = flashSkipText(skip);
    detail.push(skipText != null ? skipText : { text: L("不续开：", "Not renewed: "), code: skip });
  }
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
  var position = flashPosition(p);
  return { title: title, position: position != null && position >= 0 ? position : null, state: state, detail: detail };
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

// ---- 开始、停止、继续（闪购助手 0.8.0 起，docs/flash-sale.md §9） ------------------------------------
// 页面只发 {type:"flash_control", action}；后端发出去以后把进度写进状态的 control（{action, phase}：sending 在途，
// waiting 闪购助手接受了、等它生效，最多 30 秒，不带时刻，等多久由后端判），结果另发 flash_control_result（只有代码）

/* app_version 开头的数字：[主, 次, 修订, 第四段]，没写的段当 0。只认两到四段数字（「数字.数字」到
 * 「数字.数字.数字.数字」）开头、后面不再接数字或点的：0.8、0.8.0、0.8.0-rc1、0.9.2.1 认得（闪购助手真的发四段的，
 * 它的说明里有 0.9.2.1）；v0.8.0、0.8.x、0.9.2.1.5 这种五段的认不出，认不出返回 null */
function flashVersionParts(v) {
  var m = typeof v === "string"
    ? /^(\d{1,6})\.(\d{1,6})(?:\.(\d{1,6})(?:\.(\d{1,6}))?)?(?=$|[^\d.])/.exec(v) : null;
  return m ? [Number(m[1]), Number(m[2]), m[3] != null ? Number(m[3]) : 0, m[4] != null ? Number(m[4]) : 0] : null;
}

/* 两个版本的数字（flashVersionParts 给的那种，或者 FLASH_CONTROL_SINCE）：a 比 b 旧吗。四段都比，没写的段当 0：
 * 0.9.2 比 0.9.2.1 旧，0.8.0.0 和 0.8.0 一样，0.7.9.9 仍比 0.8.0 旧 */
function flashVersionOlder(a, b) {
  for (var i = 0; i < 4; i++) {
    var x = a[i] || 0, y = b[i] || 0;
    if (x !== y) return x < y;
  }
  return false;
}

/* 版本认得出、而且比 0.8.0 旧：那一排只留「打开闪购助手」。认不出的照常出按钮（回来 404 / 405 是 no_control） */
function flashControlTooOld(st) {
  var v = flashVersionParts(st && st.app_version);
  return !!v && flashVersionOlder(v, FLASH_CONTROL_SINCE);
}

/* 这份状态说明这个动作已经生效了吗。和后端 app/flash_sale.control_done 一字不差（shared-rules.json 的 control_done）：
 * start、resume 是 running 为 true 且 paused_reason 不是 user_interaction；stop 是 running 为 false；
 * 只认 conn=ok 的状态（连不上、读不出时看不到）。页面拿它收掉浮层里那句已经过时的结果（不管浮层开没开） */
function flashControlDone(action, st) {
  if (!st || typeof st !== "object" || st.conn !== "ok") return false;
  if (action === "stop") return st.running === false;
  if (action === "start" || action === "resume") return st.running === true && st.paused_reason !== "user_interaction";
  return false;
}

/* 状态里的控制进度（后端 _flash_compose 叠上的 control）：{action, phase}，形状不对就是 null。phase 是 sending（在途）、
 * waiting（已接受、等生效），或 retrying（只有停止：没被接受，后端隔 3 秒再试，app/pipeline_flash.py _flash_stop_run） */
function flashActiveControl(st) {
  var c = st && typeof st === "object" ? st.control : null;
  if (!c || typeof c !== "object" || FLASH_ACTIONS.indexOf(c.action) < 0) return null;
  return c.phase === "sending" || c.phase === "waiting" || c.phase === "retrying" ? { action: c.action, phase: c.phase } : null;
}

/* 开始或继续已被闪购助手接受、后端还没收掉 control（用户 10-27 定的过渡：这段时间页面刷新不变色、不出红字）。
 * 生效了（running 已经是 true）、页面还没稳下来时后端也还不收（app/flash_sale.control_settled），这里不另判，
 * 跟着 control 走。和后端 app/flash_sale.starting 一字不差，shared-rules.json 的 attention 两边各测一遍 */
function flashStarting(st) {
  var c = st && typeof st === "object" ? st.control : null;
  return !!c && typeof c === "object" && c.phase === "waiting" && (c.action === "start" || c.action === "resume");
}

/* 过渡时的主句：开始会先刷新页面（契约），继续不说在刷新页面（契约没说，不猜） */
function flashStartingText(action) {
  return action === "resume" ? L("正在继续…", "Resuming…")
                             : L("正在开始…（闪购助手在刷新页面）", "Starting… (Flash Sale Helper is reloading the page)");
}

/* 闪购助手接受了、等它生效时按下的那颗按钮上的字 */
function flashWaitingText(action) {
  if (action === "stop") return L("正在停止…", "Stopping…");
  if (action === "resume") return L("正在继续…", "Resuming…");
  return L("正在开始…", "Starting…");
}

/* 停止在后端等着再试时，按钮旁边那句（#flash-ctl-note）。连不上、读不出时那一排一颗按钮都不出，这句照样写 */
function flashRetryingNote() {
  return { text: L("停止还没被接受，" + FLASH_STOP_RETRY_SEC + " 秒内每 " + FLASH_STOP_RETRY_GAP_SEC + " 秒再试一次。",
                   "Stop hasn’t been accepted yet. Trying again every " + FLASH_STOP_RETRY_GAP_SEC + " sec for up to " +
                   FLASH_STOP_RETRY_SEC + " sec.") };
}

/* 开始、继续因为页面没准备好不能点时，按钮旁边那句（#flash-ctl-note）：照 decisions-step2（用户 10-27 定的），
 * 说的就是 /status 的 page 那句话（flashPageText，和下面主句里那句同一句）；认不出的代码写「页面状态：」加原样的代码；
 * 连 page 都没有时写「页面准备好以后才能从这里开始、继续。」 */
function flashNotReadyNote(page) {
  var code = flashCode(page);
  if (!code) return { text: L("页面准备好以后才能从这里开始、继续。", "Start and Resume work here once the page is ready.") };
  var text = flashPageText(code);
  return text ? { text: text } : { text: L("页面状态：", "Page status: "), code: code };
}

/* 浮层顶部那一排（除了「打开闪购助手」，它一直在）：{start, stop, resume, note, any}。
 * start / stop / resume 各是 {shown, disabled, text}；note 是按钮旁边那句（#flash-ctl-note，按钮的 aria-describedby），
 * null 就不写；any 是三颗里有没有一颗露出来（没有时浮层最底下那句换回第一步的「这里只显示状态」）。
 * ui：{armed: 「开始」点了第一下, pending: 刚点出去、后端还没回话的动作, offline: 和本地服务断开,
 *      gated: 发布闸关着（hello 的 config.flash_start_enabled 不是 true，后端 flash_sale.START_ENABLED）}。
 * 规则（decisions-step2，docs/flash-sale.md §9.6）：
 * - 连不上、读不出、正在查看：三颗都不出；版本认得出且低于 0.8.0：也不出，note 写「0.8.0 起才能……」；
 * - 闪购助手没在运行（running 不是 true）出「开始」，在运行出「停止」，确认框里点了取消的暂停另出「继续」；
 * - page 不是 ok、no_list：开始、继续不能点（契约：闪购助手这时不接），note 写 page 那句（flashNotReadyNote）；
 *   确认框弹着（awaiting_approval）：停止不能点，note 写「确认框处理完以后……」；
 * - 在途（本页刚点的，或状态里 phase=sending）：三颗都不能点，按下的那颗写「正在发送…」；
 * - 等生效（phase=waiting，状态还没显示生效）：那颗写「正在开始…」之类、不能点；开始、继续也不能点（闪购助手这时回 busy），
 *   停止照样能点（停是安全的方向，闪购助手不拦）；
 * - 停止在后端等着再试（phase=retrying）：停止写「正在重试…」、不能点（后端已经在试）；开始、继续照常（点了，后端把
 *   还没发的重试作废）；note 写 flashRetryingNote，连不上、读不出（那一排一颗按钮都不出）时也写；
 * - 断线：三颗都不能点（点了也发不出去）；
 * - 发布闸关着（gated）：「开始」「继续」都不出（后端这时也不发），只剩停止和打开；版本那句只说停止。
 *   gated 也记在 view 里，最底下那句跟着换（renderFlashControls） */
function flashControlView(st, ui) {
  ui = ui || {};
  var view = { start: { shown: false, disabled: true, text: L("开始", "Start") },
               stop: { shown: false, disabled: true, text: L("停止", "Stop") },
               resume: { shown: false, disabled: true, text: L("继续", "Resume") },
               note: null, any: false, gated: !!ui.gated };
  if (!st || typeof st !== "object") return view;
  var ctl = flashActiveControl(st);
  var retrying = ctl && ctl.phase === "retrying" ? ctl.action : null;
  if (st.conn !== "ok") {
    if (retrying) view.note = flashRetryingNote();
    return view;
  }
  if (flashControlTooOld(st)) {
    view.note = { text: view.gated
      ? L("闪购助手 0.8.0 起才能从这里停止。", "Stop needs Flash Sale Helper 0.8.0 or later.")
      : L("闪购助手 0.8.0 起才能从这里开始、停止。", "Start and Stop need Flash Sale Helper 0.8.0 or later.") };
    return view;
  }
  var sending = FLASH_ACTIONS.indexOf(ui.pending) >= 0 ? ui.pending : (ctl && ctl.phase === "sending" ? ctl.action : null);
  var waiting = !sending && ctl && ctl.phase === "waiting" && !flashControlDone(ctl.action, st) ? ctl.action : null;
  var running = st.running === true;
  var paused = flashCode(st.paused_reason);
  var pageReady = FLASH_READY_PAGES.indexOf(flashCode(st.page)) >= 0;
  var awaiting = paused === "awaiting_approval";
  function button(b, action, shown, blocked) {
    b.shown = shown;
    b.disabled = !!(ui.offline || sending || waiting === action || retrying === action || blocked);
    if (sending === action) b.text = L("正在发送…", "Sending…");
    else if (waiting === action) b.text = flashWaitingText(action);
    else if (retrying === action) b.text = L("正在重试…", "Retrying…");
    return b;
  }
  button(view.start, "start", !running && !view.gated, !pageReady || waiting != null);
  if (view.start.shown && !view.start.disabled && ui.armed) view.start.text = L("再点一次开始", "Click Again to Start");
  button(view.stop, "stop", running, awaiting);
  button(view.resume, "resume", running && paused === "user_interaction" && !view.gated, !pageReady || waiting != null);
  if (retrying && !sending) {
    view.note = flashRetryingNote();
  } else if (!sending && !waiting) {
    if (view.stop.shown && awaiting) {
      view.note = { text: L("确认框处理完以后才能从这里停止。", "Stop works here once the confirmation is handled.") };
    } else if ((view.start.shown || view.resume.shown) && !pageReady) {
      view.note = flashNotReadyNote(st.page);
    }
  }
  view.any = view.start.shown || view.stop.shown || view.resume.shown;
  return view;
}

/* 点了「开始」：「arm」武装（按钮换成「再点一次开始」，FLASH_START_CONFIRM_MS 后复原）、「send」发出去、「ignore」不算。
 * armedAt 是点第一下的时刻（null = 没武装）。武装后 FLASH_START_GUARD_MS 以内的第二下不算（双击一次不该就发出开始），
 * 到点了（或者时钟往回走了）当作重新点第一下 */
function flashStartClick(armedAt, now) {
  var d = Number(now) - Number(armedAt);
  if (armedAt == null || !isFinite(d) || d < 0 || d >= FLASH_START_CONFIRM_MS) return "arm";
  return d < FLASH_START_GUARD_MS ? "ignore" : "send";
}

/* 「开始」第一下的编号：16 位十六进制。只用来让后端把第二下（fire）和这一下（arm）对上，不是口令，Math.random 就够；
 * rand 给测试换 */
function flashNewArmId(rand) {
  var r = typeof rand === "function" ? rand : Math.random;
  var out = "";
  for (var i = 0; i < 16; i++) out += "0123456789abcdef".charAt(Math.floor(r() * 16) & 15);
  return out;
}

/* 本程序这边的代码（FLASH_OWN_ERRORS）的那句话。「没有发出」「发出了」照后端走到了哪一步写；认不出返回 null */
function flashOwnErrorText(code, action) {
  switch (code) {
    case "no_status":
      return L("没有发出：刚才没查到闪购助手的状态。", "Not sent: Flash Sale Helper’s status didn’t come through just now.");
    case "no_token":
      return L("没有发出：读不到闪购助手的控制密钥。确认它是 0.8.0 以上，重开它以后再试。",
               "Not sent: Couldn’t read Flash Sale Helper’s control key. Make sure it’s 0.8.0 or later, reopen it, then try again.");
    case "unreachable": return L("没有发出：连不上 127.0.0.1:9334。", "Not sent: Couldn’t connect to 127.0.0.1:9334.");
    case "no_answer":
      return L("发出了，但 127.0.0.1:9334 在 2 秒内没有回应。看看状态有没有变。",
               "Sent, but 127.0.0.1:9334 didn’t answer within 2 sec. Check whether the status changes.");
    case "bad_reply":
      return L("发出了，但回话读不出来。看看状态有没有变。", "Sent, but the reply couldn’t be read. Check whether the status changes.");
    case "no_control": return L("这个版本的闪购助手没有控制接口。", "This version of Flash Sale Helper has no control interface.");
    case "failed":
      return L("没能处理这个指令：本程序出错，详情在终端里。",
               "Couldn’t handle the command: this app hit an error. Details are in the terminal.");
    case "not_seen":       // 已接受，等满 30 秒（后端 CONTROL_WAIT_SEC）状态里还没看到生效：只写没看到
      if (action === "start") return L("30 秒内没看到闪购助手开始运行。", "Flash Sale Helper didn’t show as running within 30 sec.");
      if (action === "stop") return L("30 秒内没看到闪购助手停下。", "Flash Sale Helper didn’t show as stopped within 30 sec.");
      if (action === "resume") return L("30 秒内没看到闪购助手继续运行。", "Flash Sale Helper didn’t show as resumed within 30 sec.");
      return null;
    case "local_only":     // 没带 Origin 的本机进程发的开始、继续（后端 app/server.py CONTROL_SOURCE）
      return L("没有发出：本机脚本只能让闪购助手停止，开始、继续要在本程序里点。",
               "Not sent: A local script can only stop Flash Sale Helper. Start and Resume have to be clicked in this app.");
    case "not_confirmed":  // 后端没看到对得上的两下（FLASH_START_GUARD_MS 到 FLASH_START_CONFIRM_MS 之间）
      return L("没有发出：「开始」要点两下，第二下在第一下之后 0.4 到 6 秒之间才算。请再点两下。",
               "Not sent: Start needs two clicks, the second 0.4 to 6 sec after the first. Click twice again.");
    default: return null;
  }
}

/* 闪购助手 409 给的代码（契约「控制」一节的表）是什么意思。只照契约说代码是什么，不猜为什么；认不出返回 null */
function flashRefusedText(code) {
  switch (code) {
    case "no_page": case "login_required": case "captcha": return flashPageText(code);
    case "awaiting_approval": return flashPausedText(code);
    case "loading": return L("页面正在加载，稍等再试。", "The page is loading. Try again in a moment.");
    case "already_running": return L("已经在运行了。", "It’s already running.");
    case "paused": return L("暂停着，要点继续，不是开始。", "It’s paused. Use Resume, not Start.");
    case "not_running": return L("还没开始运行。", "It hasn’t started yet.");
    case "not_paused": return L("没有暂停。", "It isn’t paused.");
    case "busy": return L("正忙，稍等再试。", "It’s busy. Try again in a moment.");
    case "no_response":
      return L("页面没有回应，这个指令不会补做。", "The page didn’t respond. This command won’t be carried out later.");
    case "error": return L("页面报告出错。", "The page reported an error.");
    default: return null;
  }
}

/* 停止被闪购助手拒了、而且要人去专用 Chrome 里处理的那两种（后端不再试，直播助手平台轮廓 §5.5）：说清去哪儿点。
 * 别的代码返回 null */
function flashStopBlockedText(code) {
  switch (code) {
    case "awaiting_approval":
      return L("闪购助手没有接受停止：专用 Chrome 里弹着确认框。到那里点确定或取消，再点停止。",
               "Flash Sale Helper declined Stop: A confirmation is open in the dedicated Chrome. Confirm or cancel it there, then click Stop again.");
    case "captcha":
      return L("闪购助手没有接受停止：专用 Chrome 的页面上有验证码。到那里处理完验证码，再点停止。",
               "Flash Sale Helper declined Stop: There’s a CAPTCHA on the page in the dedicated Chrome. Solve it there, then click Stop again.");
    default: return null;
  }
}

/* 一条 flash_control_result 在浮层里（#flash-note）写什么：{text, code} 或 null（接受了：不写，状态自己会说）。
 * error 是 FLASH_OWN_ERRORS 之一（本程序这边的），或 http_<状态码>，或闪购助手 409 给的代码；
 * 认不出的原样放进 code（translate="no"），不给一句通用文案糊过去。
 * 停止被拒在确认框、验证码上，写 flashStopBlockedText 那句（去专用 Chrome 里点哪儿）。
 * 后端再试过的停止带 tries（2 起，app/pipeline_flash.py _flash_stop_run）：句子前面加「试了 n 次，」 */
function flashResultLine(msg) {
  if (!msg || typeof msg !== "object" || msg.ok === true) return null;
  var line = flashResultBody(msg);
  var tries = msg.tries;
  if (typeof tries === "number" && isFinite(tries) && Math.floor(tries) === tries && tries >= 2) {
    line.text = LN(tries, "试了 " + tries + " 次，", "Tried once. ", "Tried " + tries + " times. ") + line.text;
  }
  return line;
}

function flashResultBody(msg) {
  var code = flashCode(msg.error);
  var own = flashOwnErrorText(code, msg.action);
  if (own != null) return { text: own };
  var http = /^http_(\d{3})$/.exec(code);
  if (http) {
    var status = http[1];
    return { text: L("没有接受：127.0.0.1:9334 回了 HTTP " + status + "。",
                     "Not accepted: 127.0.0.1:9334 replied with HTTP " + status + ".") };
  }
  var blocked = msg.action === "stop" ? flashStopBlockedText(code) : null;
  if (blocked != null) return { text: blocked };
  var prefix = L("闪购助手没有接受：", "Flash Sale Helper declined: ");
  var why = flashRefusedText(code);
  return why != null ? { text: prefix + why } : { text: prefix, code: code || "?" };
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
  // 卡片序号写在标题前（#12，两种语言一样），不跟着标题一起被省略号吃掉
  if (parts.position != null) top.appendChild(flashEl(doc, "span", "flash-pos", "#" + parts.position));
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
      if (item.code) {                            // 文字接原样的代码（认不出的不续开原因）
        detail.appendChild(doc.createTextNode(item.text || ""));
        detail.appendChild(flashDataSpan(doc, item.code, "flash-code"));
        return;
      }
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
 * （index.html 里 #flash-panel 的各个节点）；view：{now, expanded, unread, gated}，unread 是没看过的问题记录，gated 是发布闸
 * 关着（同 flashControlView）。
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

  var lines = flashLines(st, unread.length, { gated: !!view.gated });
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
  // 摘要每段一个 inline-block（第一段带着「共 n 个商品：」），段之间是「 · 」：只在段与段之间折行
  var summary = flashSummaryParts(products, st && st.products_total);
  flashClear(els.summary);
  if (summary) {
    summary.parts.forEach(function (part, i) {
      if (i) els.summary.appendChild(doc.createTextNode(" · "));
      els.summary.appendChild(flashEl(doc, "span", "flash-summary-part", i ? part : summary.lead + part));
    });
  }
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

/* 浮层顶部那一排按 flashControlView 的结果画（发布闸关着时最底下那句只说停止）：els 是 {start, stop, resume, note, desc}（index.html 的 #flash-start、
 * #flash-stop、#flash-resume、#flash-ctl-note、#flash-desc）。按钮只改显隐、禁用和字，节点本身不换（焦点、监听都在上面）。
 * 浮层最底下那句：三颗里有一颗露着就写「这里只能开始、停止和继续……」，一颗都没有时换回第一步那句（不和版本句打架） */
function renderFlashControls(doc, els, view) {
  ["start", "stop", "resume"].forEach(function (k) {
    var el = els[k];
    var b = view[k];
    if (!el) return;
    el.classList.toggle("hidden", !b.shown);
    el.disabled = !b.shown || !!b.disabled;
    if (el.textContent !== b.text) el.textContent = b.text;
  });
  if (els.note) {
    flashFillLine(doc, els.note, view.note);
    els.note.classList.toggle("hidden", !view.note);
  }
  if (els.desc) {
    if (!view.any) {
      els.desc.textContent = L("这里只显示状态。要操作，请到闪购助手里。", "This panel only shows status. Make changes in Flash Sale Helper.");
    } else if (view.gated) {
      els.desc.textContent = L("这里只能停止，其他操作请到闪购助手里。", "Only Stop works here. Do everything else in Flash Sale Helper.");
    } else {
      els.desc.textContent = L("这里只能开始、停止和继续，其他操作请到闪购助手里。",
                               "Only Start, Stop and Resume work here. Do everything else in Flash Sale Helper.");
    }
  }
}

/* 浮层里那一句（#flash-note）：控制的结果（flashResultLine）、断线、open 没打开。line 是 {text, code} 或纯文字 */
function renderFlashNote(doc, el, line) {
  flashFillLine(doc, el, typeof line === "string" ? { text: line } : line);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    FLASH_ROWS, FLASH_ALERTS, FLASH_ALERTS_MAX, FLASH_ATTN_ORDER,
    FLASH_ACTIONS, FLASH_CONTROL_SINCE, FLASH_START_CONFIRM_MS, FLASH_START_GUARD_MS, FLASH_OWN_ERRORS,
    FLASH_READY_PAGES, FLASH_QUIET_PAGES,
    flashPageText, flashPausedText, flashStateText, flashSkipText, flashStateTone, flashLevel,
    flashCountdown, flashClock, flashCountdownText,
    flashIsProblem, flashIsNewer, flashUnread, flashSeenMark, flashParseSeen,
    flashNeedsAttention, flashChip, flashButtonState, flashLines, flashLineText,
    flashWaitingRenewal, flashRank, flashPosition, flashSortProducts, flashSummaryParts, flashSummary, flashProductParts,
    flashRowsToShow, flashMoreText, flashAlertsToShow,
    flashVersionParts, flashVersionOlder, flashControlTooOld, flashControlDone, flashActiveControl, flashStarting, flashStartingText,
    flashWaitingText, flashNotReadyNote, flashRetryingNote, flashControlView, flashStartClick, flashNewArmId, flashOwnErrorText,
    flashRefusedText, flashStopBlockedText, flashResultLine, FLASH_STOP_RETRY_SEC, FLASH_STOP_RETRY_GAP_SEC,
    renderFlashPanel, flashTick, renderFlashControls, renderFlashNote,
  };
}
