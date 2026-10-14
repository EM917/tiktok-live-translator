// i18n: done
/* 弹幕留意（Comment Review）的前端（web/guard.js，spec §7）：文案、判断、渲染弹幕栏标记 / 顶部条 / 抽屉
 * 的 DOM。后端（app/pipeline_guard.py）只发代码——类别、琥珀理由、模型意见、状态——文案全在这里按代码映射，
 * 中英成对写（docs/i18n-style.md §1.6）。
 *
 * 分三层，都不碰 app.js 里的状态：
 *   1. 纯函数（文案、摘要、匹配、排序）：node 测试直接 require，tests/guard.test.mjs / guard.en.test.mjs；
 *   2. 渲染函数：第一个参数是 document，只用 createElement / appendChild / insertBefore / removeChild /
 *      setAttribute / classList / addEventListener，所以 node 测试里的假 DOM 也能跑；
 *   3. app.js 里的胶水只管接线：收消息、存状态、把回调传进来。
 * 加载方式照 web/live-ui.js、settings-rows.js：浏览器里是全局函数，Node 里走 module.exports。
 *
 * 几条不变量：
 * - **账号 id 一律当字符串比**：TikTok 的用户 id 约 19 位，超过 JS 的 2^53，Number() / parseInt 会让两个
 *   不同的人比出相等。这个文件里没有任何一处把 uid 转成数字（tests/guard.test.mjs 钉着）；
 * - **关注高亮不进缓存**：页面按 config.guard_marked 每个条目的 keys 对每条弹幕现算，加入和移出都即时生效；
 * - **只写观察，不写原因**（CLAUDE.md 第八条）：类别写「疑似……」/ “Possible …”，琥珀理由写「没有问号」
 *   「第 2 次」，不写「也可能是提问」；
 * - **昵称、账号、弹幕原文是数据**：昵称 / 账号标 translate="no"，弹幕正文带 class i18n-data（同 app.js
 *   的 markName / markBody），英文界面的检查不把它们当漏翻。 */

"use strict";

// 浏览器里 L/LN 是 i18n.js 定义的全局函数（这里的 var 不会清掉它）；node 测试里从 i18n.js 取
if (typeof L === "undefined") { var I18N_ = require("./i18n.js"); var L = I18N_.L, LN = I18N_.LN, APP_NAME = I18N_.APP_NAME; }

/* 类别按严重程度排（与 app/comment_guard.py 的 CAT_ORDER 相同）：顶部条只显示前 3 类时，人身威胁在最前 */
var GUARD_CAT_ORDER = ["threat", "insult", "sexual", "accuse_call", "accuse_report", "accuse_claim",
                       "promo_scam", "promo_pull", "promo_link", "promo_contact", "flood_repeat",
                       "flood_user", "flood_brigade"];
var GUARD_BAR_CATS = 3;          // 顶部条最多写几类，其余折成「另 n 类」
var GUARD_EVENTS_MAX = 50;       // 抽屉里最多列几条
var GUARD_ACCOUNTS_MAX = 20;     // 多账号同句事件展开后最多列几个账号
var GUARD_MAX_NICK = 64;         // 与后端 GuardRuntime.MAX_NICK 相同：只有昵称的人按前 64 个字比

// ---- 文案：按代码映射 --------------------------------------------------------------------------

/* 类别名。中文一律「疑似……」、英文一律 “Possible …”；flood_brigade 只写观察，不加「疑似」 */
function guardCatLabel(code) {
  switch (code) {
    case "insult": return L("疑似辱骂", "Possible insult");
    case "threat": return L("疑似人身威胁", "Possible threat of harm");
    case "sexual": return L("疑似涉性言语", "Possible sexual remark");
    case "accuse_claim": return L("疑似指称假货或骗局", "Possible claim of fakes or scams");
    case "accuse_call": return L("疑似号召举报或不买", "Possible call to report or not buy");
    case "accuse_report": return L("疑似扬言举报", "Possible threat to report");
    case "flood_user": return L("疑似刷屏", "Possible flooding");
    case "flood_repeat": return L("疑似反复发同一句", "Possible repeated comment");
    case "flood_brigade": return L("多账号同一句", "Same comment from several accounts");
    case "promo_link": return L("疑似引流（链接）", "Possible redirect (link)");
    case "promo_contact": return L("疑似引流（联系方式）", "Possible redirect (contact info)");
    case "promo_pull": return L("疑似拉人去别处", "Possible redirect to another stream or shop");
    case "promo_scam": return L("疑似冒充官方或诈骗信息", "Possible impersonation or scam offer");
    default: return L("需要留意", "To review");     // 认不出的类别（后端新加了、前端还没跟）
  }
}

/* 琥珀（拿不准）的理由：只写观察。ev 是事件，只有 brigade 用得上它的账号数和窗口秒数 */
function guardAmberText(amb, ev) {
  switch (amb) {
    case "maybe_question": return L("句子很短，没有问号", "Short, with no question mark");
    case "grievance": return L("说的是自己的遭遇", "Describes the viewer’s own experience");
    case "quote": return L("像在转述别人的话", "Reads like a quote");
    case "accumulated": return L("同一人 10 分钟内第 2 次", "Second time from this viewer in 10 min");
    case "question_link": return L("问句里带链接", "Link inside a question");
    case "pending": return L("命中的词条还没确认", "Matched an entry that isn’t confirmed yet");
    case "self": return L("句子是第一人称", "Written in the first person");
    case "review": return L("模型标出", "Flagged by the model");
    case "brigade": {
      var n = Math.max(0, Math.round(Number(ev && ev.n) || 0));
      var s = Math.max(0, Math.round(Number(ev && ev.window_sec) || 0));
      return LN(n, n + " 个账号，" + s + " 秒内同一句", "Same comment from 1 account in " + s + " seconds",
                "Same comment from " + n + " accounts in " + s + " seconds");
    }
    default: return "";
  }
}

/* 模型意见（英文写 “Model: …”，与功能状态的 “Comment Review: …” 分开）。没有结果（null）返回空串 */
function guardReviewText(rv) {
  switch (rv) {
    case "pending": return L("模型复核中…", "Model reviewing…");
    case "agree": return L("模型意见：同意规则", "Model: agrees with rules");
    case "question": return L("模型意见：提问", "Model: question");
    case "complaint": return L("模型意见：客诉", "Model: complaint");
    case "friendly": return L("模型意见：友好", "Model: friendly");
    case "neutral": return L("模型意见：未见攻击", "Model: no attack found");
    case "unsure": return L("模型意见：拿不准", "Model: unsure");
    case "skipped": return L("未经模型复核", "Not reviewed by the model");
    default: return "";
  }
}

/* 事件的计数说明。只写数过的数，不写「恶意」「水军」，不写原因 */
function guardCountText(ev) {
  var m = Math.max(0, Math.round(Number(ev && ev.n) || 0));
  var s = Math.max(0, Math.round(Number(ev && ev.window_sec) || 0));
  if (ev && ev.kind === "flood" && s > 0) {
    return LN(m, s + " 秒内 " + m + " 条", "1 comment in " + s + " seconds", m + " comments in " + s + " seconds");
  }
  return LN(m, "共 " + m + " 条", "1 comment", m + " comments");
}

/* 冒号后面除非是专有名词或完整句子，否则小写（docs/i18n-style.md §2.2）。类别名是标签式的 “Possible …”；
 * 第二个字母也是大写的（缩写、TikTok 这类专有名词）就原样留着 */
function guardLowerFirst(text) {
  var s = String(text == null ? "" : text);
  if (s.length > 1 && s.charAt(1) === s.charAt(1).toUpperCase() && s.charAt(1) !== s.charAt(1).toLowerCase()) return s;
  return s.charAt(0).toLowerCase() + s.slice(1);
}

function guardLagSec(state) {
  var t = state && state.trip;
  var sec = state && state.lag_sec != null ? state.lag_sec : (t && typeof t === "object" ? t.sec : null);
  return Math.max(0, Math.round(Number(sec) || 0));
}

/* 功能状态（#guard-state 胶囊和设置行的状态行共用）。后端只发 status 代码；
 * 带数字的几种从 state 里取：lag_sec / trip.sec（识别落后几秒）、http（密钥被拒的 HTTP 状态码）、
 * wait_min（Claude 要求等几分钟）。这几个键是模型复核才有的，规则层不发。
 * 现在的 Claude 后端发：rules / claude / paused / no_key / prompt_missing / key_rejected / offline / cooldown /
 * model_missing / invalid；no_review、no_sdk、starting、tripped 是更早的版本和以后可能有的熔断型后端用的，
 * 现在没有谁发，文案留着，万一收到也不会落到默认的一句 */
function guardStateText(state) {
  var st = state || {};
  var sec = guardLagSec(st);
  switch (st.status) {
    case "env_off": return L("弹幕留意：已被环境变量 TLT_COMMENT_GUARD 关闭",
                             "Comment Review: turned off by the TLT_COMMENT_GUARD environment variable");
    case "off": return L("弹幕留意：已关闭", "Comment Review: off");
    case "rules": return L("弹幕留意：只用规则", "Comment Review: rules only");
    case "starting": return L("弹幕留意：Claude 组件载入中，先只用规则",
                              "Comment Review: loading the Claude component. Rules only for now.");
    case "claude": return L("弹幕留意：规则 + Claude", "Comment Review: rules + Claude");
    case "paused": return L("模型复核暂停：识别落后 " + sec + " 秒",
                            "Model review paused: speech recognition is " + sec + " sec behind");
    case "tripped": return L("本场模型复核已停用：识别落后 " + sec + " 秒。只用规则。",
                             "Model review is off for this session: speech recognition fell " + sec
                             + " sec behind. Rules only.");
    case "no_key": return L("没有 Claude 密钥，只用规则", "No Claude API key. Using rules only.");
    case "prompt_missing": return L("读不到复核提示词文件，只用规则",
                                    "The review prompt file couldn’t be read. Using rules only.");
    case "no_review": return L("这个版本还没有模型复核，只用规则",
                               "Model review isn’t in this version yet. Using rules only.");
    case "no_sdk": return L("缺少 Claude 组件，只用规则", "The Claude component isn’t installed. Using rules only.");
    case "key_rejected": {
      var code = Math.round(Number(st.http) || 0);
      return L("Claude 密钥没被接受（HTTP " + code + "），只用规则",
               "The Claude API key wasn’t accepted (HTTP " + code + "). Using rules only.");
    }
    case "offline": return L("连不上 Claude，先只用规则", "Can’t reach Claude. Using rules only for now.");
    case "model_missing": return L("Claude 接口里找不到复核用的模型，只用规则",
                                   "The Claude API doesn’t have the review model. Using rules only.");
    case "invalid": return L("Claude 没接受复核请求，本场只用规则",
                             "Claude didn’t accept the review requests. Using rules only for this session.");
    case "cooldown": {
      var mins = Math.max(1, Math.round(Number(st.wait_min) || 0));
      return L("Claude 要求稍后再试，" + mins + " 分钟内只用规则",
               "Claude asked the app to wait. Using rules only for " + mins + " min.");
    }
    default: return st.enabled ? L("弹幕留意：只用规则", "Comment Review: rules only")
                               : L("弹幕留意：已关闭", "Comment Review: off");
  }
}

/* 本场有几条没经过模型复核。只在选了 Claude 时说：选「不用模型」的人不需要被提醒没复核 */
function guardUnreviewedText(state) {
  var n = Math.max(0, Math.round(Number(state && state.unreviewed) || 0));
  if (!state || state.backend !== "claude" || n <= 0) return "";
  return LN(n, "本场 " + n + " 条没经过模型复核", "1 comment wasn’t reviewed by the model this session.",
            n + " comments weren’t reviewed by the model this session.");
}

/* 弹幕栏状态胶囊：{ text, title, tone, unreviewed, reenable, reenableOk }。state 还没到（老后端）返回 null。
 * tone：on＝Claude 在复核；warn＝选了 Claude 但没在复核（暂停、停用、没密钥……）；idle＝只用规则或已关闭。
 * 「没复核」绝不显示成「没问题」：warn 的几种一直带着说明 */
function guardCapsule(state) {
  if (!state || typeof state !== "object" || !state.status) return null;
  var st = state.status;
  var tone = st === "claude" ? "on" : (st === "rules" || st === "off" || st === "env_off" ? "idle" : "warn");
  return { text: guardStateText(state), title: L("查看最近需要留意的弹幕", "See recent comments to review"),
           tone: tone, unreviewed: guardUnreviewedText(state),
           reenable: st === "tripped", reenableOk: state.reenable_ok !== false };
}

/* 设置行右侧的一句摘要：开关 · 复核方式 · 关注人数。复核方式写实际在起作用的：选了 Claude 但没在复核就写「只用规则」 */
function guardSummary(state, nMarked) {
  var st = state || {};
  var on = !!st.enabled;
  var parts = [on ? L("开启", "On") : L("关闭", "Off")];
  if (on) {
    parts.push(st.status === "claude" || st.status === "paused" ? L("规则 + Claude", "Rules + Claude")
                                                               : L("只用规则", "Rules only"));
  }
  var n = Math.max(0, Math.round(Number(nMarked != null ? nMarked : st.n_marked) || 0));
  if (n > 0) parts.push(LN(n, "关注 " + n + " 人", n + " marked", n + " marked"));
  return parts.join(" · ");
}

/* 设置行底部：规则条数和体检警告数。state.rules 还没有（没载过词表）返回空串 */
function guardRulesText(state) {
  var r = state && state.rules;
  if (!r || typeof r.count !== "number") return "";
  var out = LN(r.count, "规则 " + r.count + " 条已生效", "1 rule active", r.count + " rules active");
  var w = Math.max(0, Math.round(Number(r.warnings) || 0));
  if (w > 0) out += " · " + LN(w, w + " 条体检警告", "1 rule check warning", w + " rule check warnings");
  return out;
}

// ---- 时间 --------------------------------------------------------------------------------------

function guardPad(n) { return (n < 10 ? "0" : "") + n; }

/* 24 小时制 HH:MM:SS（docs/i18n-style.md §2.4），ts 是秒 */
function guardTimeText(ts) {
  var t = Number(ts);
  if (!(t > 0)) return "";
  var d = new Date(t * 1000);
  return guardPad(d.getHours()) + ":" + guardPad(d.getMinutes()) + ":" + guardPad(d.getSeconds());
}

/* 关注名单里「最近发言」：今天写时刻，更早写「N 天前」（名单是跨场次的，只写时刻会让 30 天前的人看着像刚说过话） */
function guardLastSeenText(ts, nowSec) {
  var t = Number(ts);
  if (!(t > 0)) return "";
  var now = new Date((Number(nowSec) > 0 ? Number(nowSec) : Date.now() / 1000) * 1000);
  var then = new Date(t * 1000);
  var a = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  var b = new Date(then.getFullYear(), then.getMonth(), then.getDate()).getTime();
  var days = Math.round((a - b) / 86400000);
  if (days <= 0) return guardTimeText(t);
  return LN(days, days + " 天前", "1 day ago", days + " days ago");
}

// ---- 关注名单的匹配：账号一律当字符串 -----------------------------------------------------------------

function guardStr(v) { return typeof v === "string" && v ? v : ""; }

function guardNickKey(nick) { return nick ? "n:" + String(nick).slice(0, GUARD_MAX_NICK) : ""; }

/* 一条弹幕能对上名单的所有键：uid、sec（后端已带 "s:" 前缀）、"h:" + 小写 handle；没有 uid 的人再加
 * "n:" + 昵称（只在本场有效）。与后端 GuardRuntime._observe_one 的 keys 同一个口径 */
function guardKeys(c) {
  var keys = [];
  var uid = guardStr(c && c.uid), sec = guardStr(c && c.sec), handle = guardStr(c && c.handle);
  if (uid) keys.push(uid);
  if (sec) keys.push(sec);
  if (handle) keys.push("h:" + handle.toLowerCase());
  if (!uid && c && c.user) keys.push(guardNickKey(c.user));
  return keys;
}

/* 名单里任一键 -> 主键。marked 是 config.guard_marked（主键 -> 条目，条目有 keys） */
function guardMarkIndex(marked) {
  var index = Object.create(null);
  var names = Object.keys(marked || {});
  for (var i = 0; i < names.length; i++) {
    var primary = names[i], entry = marked[primary];
    index[primary] = primary;
    var keys = entry && Array.isArray(entry.keys) ? entry.keys : [];
    for (var j = 0; j < keys.length; j++) if (typeof keys[j] === "string" && !(keys[j] in index)) index[keys[j]] = primary;
  }
  return index;
}

/* 这个人在不在名单里。index 可选（调用方连续问很多条时自己建一次） */
function guardIsMarked(c, marked, index) {
  var idx = index || guardMarkIndex(marked);
  var keys = guardKeys(c);
  for (var i = 0; i < keys.length; i++) if (keys[i] in idx) return true;
  return false;
}

/* 点「加入关注名单」时发给后端的键：有 uid 用 uid（字符串原样），没有就用昵称键（只在本场有效） */
function guardMarkKey(c) {
  return guardStr(c && c.uid) || guardNickKey(c && c.user) || "";
}

/* 加入 / 移出时随消息带的信息。uid_src 在 comment 消息里没有（只有事件样例带），没有就不带 */
function guardMarkMessage(c, on) {
  return { type: "guard_mark", key: guardMarkKey(c), on: !!on, nick: c && c.user ? String(c.user) : "",
           handle: guardStr(c && c.handle) || null, uid_src: guardStr(c && c.uid_src) || null };
}

// ---- 弹幕栏里一条弹幕的标记 --------------------------------------------------------------------------

/* 一条弹幕要怎么标。c 是 comment 消息（带 uid / sec / handle / guard），state 是 config.guard_state，
 * marked 是 config.guard_marked。返回 { cls, pill, note, evidence, marked, showMark }：
 * - cls：加在 .cmt-item 上的类（guard-alert / guard-amber / guard-weak / guard-marked）；
 * - pill：{ text, title, level }，没有判定就是 null；
 * - note：琥珀理由和模型意见，一行；evidence：模型给的原文片段。
 * events 是事件表（config.guard_events），多账号同句的琥珀理由要用到事件里的账号数和窗口秒数。
 * note 档（弱信号）只在设置里开了「也显示弱信号」时才有标记。 */
function guardCommentView(c, state, marked, index, events) {
  var g = c && c.guard && typeof c.guard === "object" ? c.guard : null;
  var isMarked = guardIsMarked(c, marked, index);
  // 加入 / 移出的按钮只在守卫开着时出现（后端关着的时候 guard_mark 会被拒绝）
  var out = { cls: [], pill: null, note: "", evidence: "", marked: isMarked,
              showMark: !!guardMarkKey(c) && !!(state && state.enabled) };
  if (isMarked) out.cls.push("guard-marked");
  if (!g) return out;
  var lvl = g.lvl;
  if (lvl === "note" && !(state && state.weak)) return out;
  if (lvl !== "alert" && lvl !== "amber" && lvl !== "note") return out;
  var level = lvl === "note" ? "weak" : lvl;
  out.cls.push("guard-" + level);
  var cats = Array.isArray(g.cats) ? g.cats : [];
  var label = guardCatLabel(cats[0]);
  var amber = lvl === "amber"
    ? guardAmberText(g.amb || (g.src === "review" ? "review" : ""), events && g.ev ? events[g.ev] : null) : "";
  var review = guardReviewText(g.rv);
  var title = [g.src === "review" ? L("来源：模型复核", "Source: model review") : L("来源：规则", "Source: rules")];
  var why = Array.isArray(g.why) ? g.why.filter(function (w) { return typeof w === "string" && w; }) : [];
  if (why.length) title.push(L("命中的词条：", "Matched entries: ") + why.join(", "));
  if (amber) title.push(amber);
  if (review) title.push(review);
  out.pill = { text: label, title: title.join("\n"), level: level };
  out.note = [amber, review].filter(Boolean).join(" · ");
  out.evidence = typeof g.evidence === "string" ? g.evidence : "";
  return out;
}

function guardEl(doc, tag, cls, text) {
  var el = doc.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}
function guardName(el) { el.setAttribute("translate", "no"); return el; }      // 昵称、账号：浏览器翻译不去改
function guardBody(el) { el.classList.add("i18n-data"); return el; }          // 弹幕正文：留给浏览器翻译，英文检查豁免
function guardButton(doc, cls, text, title) {
  var b = guardEl(doc, "button", cls, text);
  b.setAttribute("type", "button");
  if (title) { b.setAttribute("title", title); b.setAttribute("aria-label", title); }
  return b;
}

var GUARD_ITEM_CLASSES = ["guard-alert", "guard-amber", "guard-weak", "guard-marked"];

/* 给 .cmt-item 加（或重画）守卫的标记。可以对同一条重复调用：上一次插进去的节点（记在 item.__guardParts 上）
 * 先撤掉。ctx：{ state, marked, index, events, onMark(c, on) }。
 * 结构：昵称前的 ★、昵称后的类别胶囊、行尾的 ☆ / ★ 按钮；头下面按需要多一行琥珀理由 / 模型意见，再多一行证据。 */
function guardDecorateComment(doc, item, c, ctx) {
  if (!item) return null;
  var parts = item.__guardParts || [];
  for (var i = 0; i < parts.length; i++) if (parts[i].parentNode) parts[i].parentNode.removeChild(parts[i]);
  item.__guardParts = [];
  for (var k = 0; k < GUARD_ITEM_CLASSES.length; k++) item.classList.remove(GUARD_ITEM_CLASSES[k]);
  var view = guardCommentView(c, ctx && ctx.state, ctx && ctx.marked, ctx && ctx.index, ctx && ctx.events);
  for (var j = 0; j < view.cls.length; j++) item.classList.add(view.cls[j]);
  if (c && c.uid_ok && guardStr(c.uid)) item.dataset.uid = c.uid;     // 只在账号键稳定时加（uid 来自 id / sec_uid）
  var head = item.querySelector(".cmt-head");
  if (!head) return view;
  var added = [];
  if (view.marked) {
    var star = guardEl(doc, "span", "guard-part guard-star", "★");
    star.setAttribute("aria-hidden", "true");
    head.insertBefore(star, head.firstChild);
    added.push(star);
  }
  if (view.pill) {
    var pill = guardEl(doc, "span", "guard-part guard-pill " + view.pill.level, view.pill.text);
    pill.setAttribute("title", view.pill.title);
    head.appendChild(pill);
    added.push(pill);
  }
  if (view.showMark) {
    var label = view.marked ? L("移出关注名单", "Unmark") : L("加入关注名单", "Mark");
    var btn = guardButton(doc, "guard-part guard-mark-btn" + (view.marked ? " on" : ""), view.marked ? "★" : "☆", label);
    btn.setAttribute("aria-pressed", view.marked ? "true" : "false");
    btn.addEventListener("click", function () { if (ctx && ctx.onMark) ctx.onMark(c, !view.marked); });
    head.appendChild(btn);
    added.push(btn);
  }
  var anchor = item.querySelector(".cmt-zh");
  if (view.note) {
    var note = guardEl(doc, "div", "guard-part guard-note", view.note);
    item.insertBefore(note, anchor);
    added.push(note);
  }
  if (view.evidence) {
    var ev = guardBody(guardEl(doc, "div", "guard-part guard-ev", view.evidence));
    item.insertBefore(ev, anchor);
    added.push(ev);
  }
  item.__guardParts = added;
  return view;
}

// ---- 顶部提示条 --------------------------------------------------------------------------------

/* 事件表（对象或数组）-> 数组，新的在前，去掉形状不对的 */
function guardEventList(events) {
  var arr = [];
  if (Array.isArray(events)) arr = events.slice();
  else if (events && typeof events === "object") {
    var ks = Object.keys(events);
    for (var i = 0; i < ks.length; i++) arr.push(events[ks[i]]);
  }
  return arr.filter(function (ev) { return ev && typeof ev === "object" && ev.id && !ev.dismissed; })
            .sort(function (a, b) { return (Number(b.ts) || 0) - (Number(a.ts) || 0); });
}

/* 顶部条那一行。events 是事件表，seenTs 是「已看」的时刻（秒），opts.markedTs 是关注的人本场发言的时刻们。
 * 未读 = 晚于 seenTs 的事件。红条数、琥珀数、关注的人发言数分开计，为 0 的不写；类别按严重程度排，
 * 超过 3 类写「另 n 类」。show 为假（三项全 0）时整条不显示。返回 { red, amber, marked, cats, more,
 * latestTs, parts, text, show }：parts 是各段文字，text 是用 " · " 连起来的整行。 */
function guardBarSummary(events, seenTs, opts) {
  var seen = Number(seenTs) || 0;
  var red = 0, amber = 0, latest = 0, tally = Object.create(null);
  guardEventList(events).forEach(function (ev) {
    var ts = Number(ev.ts) || 0;
    if (ts <= seen || (ev.lvl !== "alert" && ev.lvl !== "amber")) return;
    var n = Math.max(1, Math.round(Number(ev.n) || 1));
    if (ev.lvl === "alert") red += n; else amber += n;
    if (ts > latest) latest = ts;
    var cat = ev.cat || "other";
    tally[cat] = (tally[cat] || 0) + n;
  });
  var spoke = 0, markedTs = opts && Array.isArray(opts.markedTs) ? opts.markedTs : [];
  for (var i = 0; i < markedTs.length; i++) if (Number(markedTs[i]) > seen) spoke++;
  var order = GUARD_CAT_ORDER.slice();
  Object.keys(tally).forEach(function (cat) { if (order.indexOf(cat) < 0) order.push(cat); });
  var cats = order.filter(function (cat) { return tally[cat]; }).map(function (cat) {
    return { code: cat, label: guardCatLabel(cat), n: tally[cat] };
  });
  var shown = cats.slice(0, GUARD_BAR_CATS), more = cats.length - shown.length;
  var parts = [];
  if (red) parts.push(LN(red, "需要留意的弹幕 " + red + " 条", "1 comment to review", red + " comments to review"));
  if (amber) parts.push(LN(amber, "拿不准 " + amber + " 条", amber + " unclear", amber + " unclear"));
  if (spoke) parts.push(LN(spoke, "关注的人发言 " + spoke, spoke + " from marked viewers", spoke + " from marked viewers"));
  var show = parts.length > 0;
  if (show && shown.length) {
    var names = shown.map(function (c) { return c.label + " " + c.n; });
    if (more > 0) names.push(L("另 " + more + " 类", more + " more"));
    parts.push(names.join(L("、", ", ")));
  }
  var latestText = latest ? guardTimeText(latest) : "";
  if (show && latestText) parts.push(L("最新 " + latestText, "Latest " + latestText));
  return { red: red, amber: amber, marked: spoke, cats: cats, more: more, latestTs: latest, parts: parts,
           text: parts.join(" · "), show: show };
}

// ---- 抽屉：最近需要留意 ----------------------------------------------------------------------------

/* 事件 -> 抽屉里一行的数据（不碰 DOM）。translation：弹幕栏里这条已有的译文（没有传空串） */
function guardEventRow(ev, marked, index, translation) {
  var sample = ev.sample && typeof ev.sample === "object" ? ev.sample : {};
  var brigade = ev.kind === "brigade";
  var who = { uid: sample.uid, handle: sample.handle, user: sample.user };
  var accounts = Array.isArray(ev.accounts) ? ev.accounts : [];
  var notes = [];
  var amber = ev.lvl === "amber" ? guardAmberText(ev.amb, ev) : "";
  if (amber) notes.push(amber);
  if (!brigade && !(ev.lvl === "amber" && ev.amb === "brigade") && (Number(ev.n) || 0) > 1) notes.push(guardCountText(ev));
  var review = guardReviewText(ev.rv);
  if (review) notes.push(review);
  return {
    id: String(ev.id), level: ev.lvl === "alert" ? "alert" : "amber", kind: ev.kind || "comment",
    catText: guardCatLabel(ev.cat), timeText: guardTimeText(ev.ts), user: sample.user ? String(sample.user) : "",
    text: sample.text ? String(sample.text) : "", translation: translation || "", note: notes.join(" · "),
    cid: sample.cid ? String(sample.cid) : "",
    accounts: accounts.slice(0, GUARD_ACCOUNTS_MAX).map(function (a) { return a && a.user ? String(a.user) : ""; }),
    moreAccounts: Math.max(0, accounts.length - GUARD_ACCOUNTS_MAX),
    who: who, markKey: brigade ? "" : guardMarkKey(who), marked: brigade ? false : guardIsMarked(who, marked, index)
  };
}

/* 画抽屉的「最近需要留意」页签。ctx：{ marked, index, translationOf(cid), onLocate(ev), onMark(who, on), onDismiss(ev) }。
 * 最多 GUARD_EVENTS_MAX 条，新的在上；多账号同句的事件折叠成一行，展开后列出前 20 个账号和样例句 */
function guardRenderEvents(doc, box, events, ctx) {
  while (box.firstChild) box.removeChild(box.firstChild);
  var list = guardEventList(events).slice(0, GUARD_EVENTS_MAX);
  if (!list.length) {
    box.appendChild(guardEl(doc, "div", "guard-empty", L("暂时没有需要留意的弹幕", "No comments to review yet.")));
    return 0;
  }
  var index = ctx && ctx.index ? ctx.index : guardMarkIndex(ctx && ctx.marked);
  list.forEach(function (ev) {
    var tr = ctx && ctx.translationOf ? ctx.translationOf(ev.sample && ev.sample.cid) : "";
    var row = guardEventRow(ev, ctx && ctx.marked, index, tr);
    var el = guardEl(doc, "div", "guard-ev-row " + row.level);
    el.setAttribute("data-ev", row.id);
    var top = guardEl(doc, "div", "gev-top");
    top.appendChild(guardEl(doc, "span", "gev-time", row.timeText));
    if (row.user) top.appendChild(guardName(guardEl(doc, "span", "gev-user", row.user)));
    top.appendChild(guardEl(doc, "span", "guard-pill " + row.level, row.catText));
    el.appendChild(top);
    if (row.text) el.appendChild(guardBody(guardEl(doc, "div", "gev-text", row.text)));
    if (row.translation && row.translation !== row.text) {
      el.appendChild(guardBody(guardEl(doc, "div", "gev-zh", row.translation)));
    }
    if (row.note) el.appendChild(guardEl(doc, "div", "gev-note", row.note));
    if (row.kind === "brigade" && row.accounts.length) {
      var det = guardEl(doc, "details", "gev-accounts");
      det.appendChild(guardEl(doc, "summary", "", L("展开账号", "Show accounts")));
      var ul = guardEl(doc, "ul", "gev-account-list");
      row.accounts.forEach(function (name) { if (name) ul.appendChild(guardName(guardEl(doc, "li", "", name))); });
      if (row.moreAccounts) {
        ul.appendChild(guardEl(doc, "li", "gev-more", L("另 " + row.moreAccounts + " 个", row.moreAccounts + " more")));
      }
      det.appendChild(ul);
      el.appendChild(det);
    }
    var actions = guardEl(doc, "div", "gev-actions");
    if (row.cid) {
      var locate = guardButton(doc, "btn small", L("定位", "Show in Comments"));
      locate.addEventListener("click", function () { if (ctx && ctx.onLocate) ctx.onLocate(ev); });
      actions.appendChild(locate);
    }
    if (row.markKey) {
      var mark = guardButton(doc, "btn small", row.marked ? L("移出关注名单", "Unmark") : L("加入关注名单", "Mark"));
      mark.addEventListener("click", function () { if (ctx && ctx.onMark) ctx.onMark(ev.sample || {}, !row.marked); });
      actions.appendChild(mark);
    }
    var dismiss = guardButton(doc, "btn small", L("忽略", "Dismiss"));
    dismiss.addEventListener("click", function () { if (ctx && ctx.onDismiss) ctx.onDismiss(ev); });
    actions.appendChild(dismiss);
    el.appendChild(actions);
    box.appendChild(el);
  });
  return list.length;
}

// ---- 关注名单（抽屉的第二个页签和设置行共用） ----------------------------------------------------------

/* 名单 -> 行数据，最近发言的在前 */
function guardMarkedRows(marked, nowSec) {
  var out = [];
  Object.keys(marked || {}).forEach(function (primary) {
    var e = marked[primary];
    if (!e || typeof e !== "object") return;
    var keys = Array.isArray(e.keys) ? e.keys : [];
    var onlyHandle = keys.length > 0 && keys.every(function (k) { return typeof k === "string" && k.indexOf("h:") === 0; });
    var catText = e.cat ? guardLowerFirst(guardCatLabel(e.cat)) : "";    // 冒号后小写；中文不受影响
    var how = e.by === "auto"
      ? (catText ? L("自动加入：" + catText, "Added automatically: " + catText) : L("自动加入", "Added automatically"))
      : L("手动加入", "Added by you");
    var keyNote = e.session ? L("只有昵称，只在本场有效", "Name only. Works for this session only.")
      : (onlyHandle ? L("按 @账号记，对方改账号后对不上", "Saved by @handle. It won’t match if they change it.") : "");
    var seen = guardLastSeenText(e.last_seen, nowSec);
    out.push({ key: primary, nick: e.nick ? String(e.nick) : "", handle: e.handle ? String(e.handle) : "",
               auto: e.by === "auto", how: how, keyNote: keyNote, lastSeen: Number(e.last_seen) || 0,
               lastText: seen ? L("最近发言 " + seen, "Last comment " + seen) : "" });
  });
  return out.sort(function (a, b) { return b.lastSeen - a.lastSeen; });
}

/* 画名单。ctx：{ now, onUnmark(key) }。一行：昵称、@handle、怎么加入的、最近发言、备注、「移出」 */
function guardRenderMarked(doc, box, marked, ctx) {
  while (box.firstChild) box.removeChild(box.firstChild);
  var rows = guardMarkedRows(marked, ctx && ctx.now);
  if (!rows.length) {
    box.appendChild(guardEl(doc, "div", "guard-empty", L("还没有关注的人", "No marked viewers yet.")));
    return 0;
  }
  rows.forEach(function (row) {
    var el = guardEl(doc, "div", "guard-mk-row");
    el.setAttribute("data-key", row.key);
    var main = guardEl(doc, "div", "gmk-main");
    main.appendChild(guardName(guardEl(doc, "span", "gmk-nick", row.nick || row.handle || row.key)));
    if (row.handle) main.appendChild(guardName(guardEl(doc, "span", "gmk-handle", "@" + row.handle)));
    el.appendChild(main);
    el.appendChild(guardEl(doc, "div", "gmk-meta", [row.how, row.lastText].filter(Boolean).join(" · ")));
    if (row.keyNote) el.appendChild(guardEl(doc, "div", "gmk-note", row.keyNote));
    var un = guardButton(doc, "btn small gmk-un", L("移出关注名单", "Unmark"));
    un.addEventListener("click", function () { if (ctx && ctx.onUnmark) ctx.onUnmark(row.key); });
    el.appendChild(un);
    box.appendChild(el);
  });
  return rows.length;
}

/* 设置行里的名单标题：「关注名单 · @主播」。主播名是数据，单独包一层 translate="no" */
function guardFillHeading(doc, el, streamer) {
  while (el.firstChild) el.removeChild(el.firstChild);
  el.appendChild(doc.createTextNode(L("关注名单", "Marked Viewers")));
  if (streamer) {
    el.appendChild(doc.createTextNode(" · "));
    el.appendChild(guardName(guardEl(doc, "span", "", "@" + streamer)));
  }
}

/* 弹幕栏收起时，浮钮上红点旁的提示：未读红条的条数 */
function guardFabTitle(red) {
  return LN(red, red + " 条需要留意的弹幕", "1 comment to review", red + " comments to review");
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    GUARD_CAT_ORDER, GUARD_BAR_CATS, GUARD_EVENTS_MAX, GUARD_ACCOUNTS_MAX,
    guardCatLabel, guardAmberText, guardReviewText, guardCountText, guardStateText, guardUnreviewedText,
    guardCapsule, guardSummary, guardRulesText, guardTimeText, guardLastSeenText,
    guardKeys, guardMarkIndex, guardIsMarked, guardMarkKey, guardMarkMessage, guardCommentView,
    guardDecorateComment, guardEventList, guardBarSummary, guardEventRow, guardRenderEvents,
    guardMarkedRows, guardRenderMarked, guardFillHeading, guardFabTitle,
  };
}
