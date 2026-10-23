// i18n: done
/* 浮窗页（web/float.html，app/float_window.py 开的那个小窗）：最新一条字幕 + 最近几条弹幕。
 *
 * 只显示，不发任何消息：连的是 /ws?role=float，服务端对这种连接只转发 FLOAT_TYPES 里的类型
 * （app/server.py），入站一律丢弃。这里没有 send()。不显示报警（报警留在中控页，CLAUDE.md 第六条）。
 * 唯一的按钮是结束卡片上的「回到主窗口」（专家审查 U3）：监听自己停了（下播 ended、出错 error）时浮窗
 * 不再直接消失，换成一张卡片；按钮走 pywebview 的 JS 桥（window.pywebview.api.focus_main，
 * app/float_window.py 的 FloatApi），只在本机把主窗口提到前面，不经过 WebSocket、不发给服务端。
 * 没有 JS 桥（老版本 pywebview、在浏览器里直接打开 /float）就不画这个按钮。
 *
 * 分三层，跟 web/guard.js、web/live-ui.js 一样：
 *   1. 纯函数（状态归并、取字、字号）：node 测试直接 require，tests/float.test.mjs / float.en.test.mjs；
 *   2. 渲染函数：第一个参数是 document，只用 createElement / appendChild / removeChild / classList /
 *      setAttribute / textContent，node 测试里的假 DOM 也能跑；
 *   3. 页面里的胶水（IIFE，文件末尾）：连 WebSocket、把消息交给归并函数、再渲染。node 里没有
 *      document，直接跳过。
 * 字幕的角标（翻译中不画，失败 / 拒答 / 不像译文 / 积压跳过才画）用 live-ui.js 的 liveBarChip，
 * 新的一句等译文到了再整句换上（待译停留，同中控页底部大字幕，见 floatReduce），
 * 「约 N 秒前」用它的 captionAudioEndAt / captionAgeText，
 * 弹幕的红 / 琥珀标记用 guard.js 的 guardDecorateComment——和中控页是同一份判断，不抄第二遍。
 *
 * 几条不变量：
 * - **账号 id 一律当字符串比**，这里不碰 uid 的数值（同 guard.js）；
 * - **只写观察，不写原因**（CLAUDE.md 第八条）；
 * - 昵称标 translate="no"，字幕和弹幕正文带 class i18n-data（同 app.js 的 markName / markBody）。 */

"use strict";

// 浏览器里这些都是前面几个脚本定义的全局；node 测试里从各自的模块取
if (typeof L === "undefined") { var I18N_ = require("./i18n.js"); var L = I18N_.L, LN = I18N_.LN, APP_NAME = I18N_.APP_NAME; }
if (typeof liveBarChip === "undefined") {
  var LIVE_ = require("./live-ui.js");
  var liveBarChip = LIVE_.liveBarChip, captionSizeFromConfig = LIVE_.captionSizeFromConfig;
  var captionAudioEndAt = LIVE_.captionAudioEndAt, captionAgeText = LIVE_.captionAgeText;
}
if (typeof guardDecorateComment === "undefined") {
  var GUARD_ = require("./guard.js");
  var guardDecorateComment = GUARD_.guardDecorateComment, guardMarkIndex = GUARD_.guardMarkIndex;
}

var FLOAT_COMMENTS_MAX = 5;          // 窗口里最多留几条弹幕（新的在底部）
var FLOAT_HOLD_MS = 4000;            // 新的一句最多等译文多久（同中控页 web/app.js 的 LIVE_HOLD_MS）
// 监听自己停下来的两种状态（同 app/pipeline_float.py 的 END_STATES）：浮窗留着，换成结束卡片。
// idle 只来自停止，后端照旧关窗，这里不为它画卡片（中控点停止时不会先闪一下卡片再关）
var FLOAT_END_STATES = ["ended", "error"];

// ---- 状态归并 -----------------------------------------------------------------------------

function floatInitialState() {
  return { caption: null, held: null, comments: [], breakTs: 0, status: "", captionSize: null,
           guardState: null, marked: Object.create(null), markIdx: Object.create(null) };
}

function floatSetMarked(state, marked) {
  var next = Object.create(null);
  var src = marked && typeof marked === "object" ? marked : {};
  Object.keys(src).forEach(function (k) { if (src[k] && typeof src[k] === "object") next[k] = src[k]; });
  state.marked = next;
  state.markIdx = guardMarkIndex(next);
}

function floatApplyConfig(state, cfg) {
  if (!cfg || typeof cfg !== "object") return;
  if ("caption_size" in cfg) state.captionSize = captionSizeFromConfig(cfg.caption_size);
  if (cfg.status && typeof cfg.status === "object") state.status = cfg.status.state || "";
  if ("guard_state" in cfg) state.guardState = cfg.guard_state && typeof cfg.guard_state === "object" ? cfg.guard_state : null;
  if ("guard_marked" in cfg) floatSetMarked(state, cfg.guard_marked);
}

function floatFindComment(state, id) {
  for (var i = state.comments.length - 1; i >= 0; i--) if (state.comments[i].id === id) return state.comments[i];
  return null;
}

/* 一条消息 → 新状态（原地改 state 并返回它）。认不出的类型原样放过。
 * 换主播（session_break）清掉字幕和弹幕，并记下那一刻：缓存里的弹幕不会随换场清空，重连回放时
 * 比这一刻老的弹幕是上一个主播的，不要。
 * nowMs：收到这条消息的时刻（毫秒，缺省是此刻）。字幕记下「这一句的声音结束于何时」（live-ui.js 的
 * captionAudioEndAt，同中控页底部大字幕）：消息带 ts 时不看 nowMs，所以回放来的那几条也算得对。
 *
 * 待译停留（专家审查 U1，用户 10-22 定的「等译文到了再一起换」，同中控页底部大字幕）：实时来的一句还在翻译
 * （translate_state 是 pending、没有译文）时先放进 state.held，窗口里停在上一句；它的 caption_update
 * 带来译文、或者确定不会有译文了（failed / dropped / skipped），再整句换上。以前每句先放西语、
 * 约 0.9 秒后跳成中文。等待中又来一句就改等新的那句；从这一轮开始等算起满 FLOAT_HOLD_MS 还没结局（改等新句
 * 不重新数），页面里的计时器调 floatReleaseHeld 照以前的样子先放原文。回放来的照旧直接显示：那是断线前已经在
 * 屏幕上的样子。 */
function floatReduce(state, msg, nowMs) {
  if (!msg || typeof msg !== "object") return state;
  switch (msg.type) {
    case "hello": {
      var fresh = floatInitialState();
      Object.keys(fresh).forEach(function (k) { state[k] = fresh[k]; });
      floatApplyConfig(state, msg.config);
      break;
    }
    case "config":
      floatApplyConfig(state, msg);
      break;
    case "status":
      state.status = msg.state || "";
      break;
    case "caption": {
      var next = { id: msg.id, original: msg.original || "", translated: msg.translated || null,
                   translate_state: msg.translate_state, refused: !!msg.refused,
                   not_translation: !!msg.not_translation,
                   audioEndAt: captionAudioEndAt(msg, typeof nowMs === "number" ? nowMs : Date.now()) };
      if (!msg.replay && next.translate_state === "pending" && !next.translated) {
        state.held = next;
      } else {
        state.caption = next;
        state.held = null;
      }
      break;
    }
    case "caption_update": {
      // 只覆盖消息里带的键：重译的「进行中」通知不带 translated，整段照抄会把已有的译文抹掉
      var waiting = state.held && state.held.id === msg.id;
      var cap = waiting ? state.held : (state.caption && state.caption.id === msg.id ? state.caption : null);
      if (cap) {
        if ("translated" in msg) cap.translated = msg.translated || null;
        if ("translate_state" in msg) cap.translate_state = msg.translate_state;
        if ("refused" in msg) cap.refused = !!msg.refused;
        if ("not_translation" in msg) cap.not_translation = !!msg.not_translation;
      }
      // 在等的那一句有了结局：整句换上。只带 strong_state 的重译进度消息不是结局，接着等
      if (waiting && (msg.translated || (msg.translate_state && msg.translate_state !== "pending"))) {
        state.caption = cap;
        state.held = null;
      }
      break;
    }
    case "session_break":
      state.caption = null;
      state.held = null;
      state.comments = [];
      state.breakTs = typeof msg.ts === "number" ? msg.ts : 0;
      break;
    case "history_cleared":
      if (msg.kind === "captions") { state.caption = null; state.held = null; }
      else if (msg.kind === "comments") state.comments = [];
      break;
    case "comment": {
      if (typeof msg.ts === "number" && msg.ts < state.breakTs) break;
      var item = { id: msg.id, user: msg.user || "", text: msg.text || "", ts: msg.ts,
                   translated: msg.translated || null, state: msg.state || "",
                   uid: msg.uid, uid_ok: msg.uid_ok, sec: msg.sec, handle: msg.handle,
                   guard: msg.guard && typeof msg.guard === "object" ? msg.guard : null };
      var old = floatFindComment(state, msg.id);
      if (old) state.comments[state.comments.indexOf(old)] = item;
      else state.comments.push(item);
      while (state.comments.length > FLOAT_COMMENTS_MAX) state.comments.shift();
      break;
    }
    case "comment_update": {
      var target = floatFindComment(state, msg.id);
      if (target) {
        target.translated = msg.translated || null;
        target.state = msg.state || "";
      }
      break;
    }
    case "comment_guard": {
      var tagged = floatFindComment(state, msg.id);
      if (tagged) tagged.guard = msg.guard && typeof msg.guard === "object" ? msg.guard : null;
      break;
    }
    case "guard_mark": {
      var key = String(msg.key || "");
      if (key) {
        var marked = Object.assign({}, state.marked);
        if (msg.entry && typeof msg.entry === "object") marked[key] = msg.entry;
        else delete marked[key];
        floatSetMarked(state, marked);
      }
      break;
    }
  }
  return state;
}

/* 等满 FLOAT_HOLD_MS 译文还没来：把在等的那一句照以前的样子放上去（原文大字，译文到了再由 caption_update
 * 就地换成译文）。id 已经不是在等的那一句（换上了、又来了新的一句改等新的、换了场）就什么都不做，
 * 所以页面的计时器取消不了也没关系。返回有没有换。 */
function floatReleaseHeld(state, id) {
  if (!state || !state.held || state.held.id !== id) return false;
  state.caption = state.held;
  state.held = null;
  return true;
}

// ---- 取字 ---------------------------------------------------------------------------------

/* 最新一条字幕怎么显示。和中控页底部大字幕同一套：译文到了译文大字、原文小字；没译成就是原文大字
 * （没有小字），下面的角标说明为什么（翻译中不画，见 liveBarChip）。没有字幕返回 null。 */
function floatCaptionView(state) {
  var c = state && state.caption;
  if (!c) return null;
  var chip = liveBarChip(c);
  return { id: c.id, big: c.translated || c.original || "", small: c.translated ? (c.original || "") : "",
           chip: chip.text, fail: chip.fail };
}

/* 有字幕时，字幕上面的状态行除了翻译角标还写什么。浮窗常常是中控唯一看着的地方，而它没有中控页的
 * 顶栏状态胶囊：断流重连、等主播开播、浮窗页自己和程序断了线，大字幕都还停在最后一句，看着像刚说的。
 * 只写观察到的（CLAUDE.md 第八条）：
 *   connecting  状态是「连接中」时写「连接中…」（和没字幕时的占位同一句）；
 *   lag         这一句的声音结束多久了，live-ui.js 的 captionAgeText：超过 15 秒才有字，规则同中控页
 *               底部大字幕下面那个「约 N 秒前」。不知道（缺 e2e_ms）就不写。 */
function floatCaptionMeta(state, nowMs) {
  var c = state && state.caption;
  if (!c) return { connecting: "", lag: "" };
  return { connecting: state.status === "connecting" ? L("连接中…", "Connecting…") : "",
           lag: typeof c.audioEndAt === "number" ? captionAgeText(nowMs - c.audioEndAt) : "" };
}

/* 还没有字幕时窗口里写的话：只写观察到的状态 */
function floatPlaceholder(state) {
  return state && state.status === "connecting" ? L("连接中…", "Connecting…")
                                                : L("等待第一句字幕…", "Waiting for the first caption…");
}

/* 结束卡片（专家审查 U3）：状态是 ended / error 时返回 { state, title, note }，否则 null。
 * 只写观察到的和能做的（CLAUDE.md 第八条）：标题是中控页状态胶囊对这个状态的说法（ended 是「直播已结束」，
 * 同 web/app.js 的 STATUS_TEXT；error 写「监听已停止」——胶囊的「出错了」单独放在卡片上读不出监听停了），
 * 具体发生了什么只在主窗口的横幅里，卡片不转述、不猜。 */
function floatEndView(state) {
  var st = state && state.status;
  if (FLOAT_END_STATES.indexOf(st) < 0) return null;
  return { state: st,
           title: st === "ended" ? L("直播已结束", "Stream ended") : L("监听已停止", "Monitoring stopped"),
           note: L("详情见主窗口。", "See the main window for details.") };
}

/* 「回到主窗口」要用的 JS 桥函数（pywebview 在页面加载后注入 window.pywebview.api，注入完发
 * pywebviewready 事件）；没有就是 null，按钮不画。win 是页面的 window（测试里传替身） */
function floatMainBridge(win) {
  var api = win && win.pywebview && win.pywebview.api;
  return api && typeof api.focus_main === "function" ? api.focus_main : null;
}

/* 点「回到主窗口」：调 JS 桥（它返回 Promise，失败只在终端留一行，页面不管）。返回有没有调到 */
function floatGoMain(win) {
  var fn = floatMainBridge(win);
  if (!fn) return false;
  try {
    var done = fn.call(win.pywebview.api);
    if (done && typeof done.catch === "function") done.catch(function () { /* 后端自己打警告 */ });
  } catch (e) { return false; }
  return true;
}

/* 一条弹幕怎么显示：昵称、译文行、原文行。照中控页弹幕栏的规则（app.js renderComment / updateComment）：
 * 翻译中占位；同语言 / 没翻译的（same、skipped）译文行就是原文本身、不再重复一遍；
 * 译不出来的（failed、dropped）显示原文本身，不写「失败」字样——原文就是内容，中控照样看得懂。 */
function floatCommentView(c) {
  var text = c.text || "";
  var out = { id: c.id, user: c.user || "", zh: text, orig: text, showOrig: false, pending: false, failed: false };
  if (c.state === "pending") {
    out.zh = L("翻译中…", "Translating…");
    out.pending = true;
    out.showOrig = !!text;
  } else if (c.translated && c.state !== "same" && c.state !== "skipped") {
    out.zh = c.translated;
    out.showOrig = !!text;
  } else if (c.state === "failed" || c.state === "dropped") {
    out.failed = true;
  }
  return out;
}

/* 字号。config.caption_size（20–60，中控页顶栏滑块，默认 34）定中控页底部大字幕的字号；这里的窗口小得多，
 * 按 0.62 缩（34 → 21px），夹在 16–40 之间；原文和弹幕按译文的比例跟着变。没设过就按默认的 34 算 */
function floatSizes(captionSize) {
  var size = captionSizeFromConfig(captionSize);
  var big = Math.max(16, Math.min(40, Math.round((size == null ? 34 : size) * 0.62)));
  return { big: big, small: Math.max(12, Math.round(big * 0.62)),
           cmt: Math.max(13, Math.min(20, Math.round(big * 0.66))),
           cmtSmall: Math.max(11, Math.min(16, Math.round(big * 0.55))) };
}

// ---- 渲染 ---------------------------------------------------------------------------------

function floatEl(doc, tag, cls, text) {
  var el = doc.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

/* 一条弹幕的节点。结构同中控页的 .cmt-item（.cmt-head / .cmt-zh / .cmt-orig），guardDecorateComment 靠它
 * 找位置。ctx.guard 是传给它的 { state, marked, index, events, onMark }：这里 state.enabled 恒为 false，
 * 所以不会出现「加入关注名单」的按钮——浮窗只显示。 */
function floatBuildComment(doc, c, guardCtx) {
  var v = floatCommentView(c);
  var item = floatEl(doc, "div", "cmt-item f-cmt");
  item.dataset.cmtId = String(c.id);
  var head = floatEl(doc, "div", "cmt-head f-head");
  var name = floatEl(doc, "span", "f-user", v.user);
  name.setAttribute("translate", "no");                      // 昵称是名字，不是正文
  head.appendChild(name);
  item.appendChild(head);
  item.appendChild(floatEl(doc, "div", "cmt-zh f-zh i18n-data" + (v.pending ? " pending" : "") + (v.failed ? " failed" : ""), v.zh));
  item.appendChild(floatEl(doc, "div", "cmt-orig f-orig i18n-data" + (v.showOrig ? "" : " hidden"), v.orig));
  guardDecorateComment(doc, item, { uid: c.uid, uid_ok: c.uid_ok, sec: c.sec, handle: c.handle, user: c.user,
                                    guard: c.guard }, guardCtx);
  return item;
}

function floatGuardCtx(state) {
  var gs = state.guardState || {};
  return { state: { enabled: false, weak: !!gs.weak }, marked: state.marked, index: state.markIdx,
           events: null, onMark: null };
}

/* 字幕区装不下（字号调大、句子很长）时给它加 clipped：底部渐隐，看得出下面还有字被裁掉了。
 * 状态行在字幕上面，裁不到它（web/float.css）。量的是真排版；node 测试里的假元素 scrollHeight、
 * clientHeight 都是 0，不会加。 */
function floatMarkClipped(els) {
  var cap = els.caption;
  var over = !cap.classList.contains("hidden") && typeof cap.scrollHeight === "number" &&
             typeof cap.clientHeight === "number" && cap.scrollHeight > cap.clientHeight + 1;
  cap.classList.toggle("clipped", over);
}

/* 字幕上面的状态行：「连接中…」、翻译角标、「约 N 秒前」，哪个都没有就整行收起。页面每秒调一次（数字会涨） */
function floatPaintMeta(els, state, nowMs) {
  var cv = floatCaptionView(state);
  var meta = floatCaptionMeta(state, nowMs);
  var chip = cv ? cv.chip : "";
  els.conn.textContent = meta.connecting;
  els.conn.classList.toggle("hidden", !meta.connecting);
  els.chip.textContent = chip;
  els.chip.classList.toggle("hidden", !chip);
  els.chip.classList.toggle("fail", !!(cv && cv.fail));
  els.lag.textContent = meta.lag;
  els.lag.classList.toggle("hidden", !meta.lag);
  els.meta.classList.toggle("hidden", !(meta.connecting || chip || meta.lag));
}

/* 结束卡片：end 是 floatEndView 的结果（null 就收起）；canGoMain：有没有 JS 桥，没有就不画按钮。
 * 卡片露出来时字幕、占位、弹幕都收起——这一场已经停了，窗口里只说这一件事 */
function floatPaintEnd(els, end, canGoMain) {
  if (!els.end) return;
  els.end.classList.toggle("hidden", !end);
  els.endTitle.textContent = end ? end.title : "";
  els.endNote.textContent = end ? end.note : "";
  els.endBack.textContent = L("回到主窗口", "Show Main Window");
  els.endBack.classList.toggle("hidden", !(end && canGoMain));
}

/* 把状态画出来。els：{ empty, caption, meta, conn, chip, lag, big, small, comments }，可选
 * { end, endTitle, endNote, endBack }（结束卡片）。opts.canGoMain：有没有「回到主窗口」的 JS 桥。
 * 整个重画：最多 5 条，不值得做差量。nowMs 缺省是此刻 */
function floatRender(doc, els, state, nowMs, opts) {
  var end = floatEndView(state);
  floatPaintEnd(els, end, !!(opts && opts.canGoMain));
  var cv = end ? null : floatCaptionView(state);
  els.comments.classList.toggle("hidden", !!end);
  els.caption.classList.toggle("hidden", !cv);
  els.empty.classList.toggle("hidden", !!cv || !!end);
  if (cv) {
    els.big.textContent = cv.big;
    els.small.textContent = cv.small;
    els.small.classList.toggle("hidden", !cv.small);
  } else {
    els.empty.textContent = floatPlaceholder(state);
  }
  floatPaintMeta(els, state, typeof nowMs === "number" ? nowMs : Date.now());
  while (els.comments.firstChild) els.comments.removeChild(els.comments.firstChild);
  var ctx = floatGuardCtx(state);
  for (var i = 0; i < state.comments.length; i++) els.comments.appendChild(floatBuildComment(doc, state.comments[i], ctx));
  floatMarkClipped(els);
}

// ---- 页面里的胶水 -------------------------------------------------------------------------

(function () {
  if (typeof document === "undefined" || typeof WebSocket === "undefined" || !document.getElementById("f-root")) return;
  var root = document.documentElement;
  // 非 macOS 的窗口不透明（app/float_window.py 往地址里写 ?solid=1）
  if (/[?&]solid=1(?:&|$)/.test((typeof location !== "undefined" && location.search) || "")) root.classList.add("solid");
  var els = { empty: document.getElementById("f-empty"), caption: document.getElementById("f-caption"),
              meta: document.getElementById("f-meta"), conn: document.getElementById("f-conn"),
              chip: document.getElementById("f-chip"), lag: document.getElementById("f-lag"),
              big: document.getElementById("f-big"), small: document.getElementById("f-small"),
              comments: document.getElementById("f-comments"),
              end: document.getElementById("f-end"), endTitle: document.getElementById("f-end-title"),
              endNote: document.getElementById("f-end-note"), endBack: document.getElementById("f-end-back") };
  var state = floatInitialState();
  var retries = 0;
  var win = typeof window !== "undefined" ? window : null;

  function render() {
    floatRender(document, els, state, undefined, { canGoMain: !!floatMainBridge(win) });
  }

  function applySizes() {
    var s = floatSizes(state.captionSize);
    root.style.setProperty("--f-big", s.big + "px");
    root.style.setProperty("--f-small", s.small + "px");
    root.style.setProperty("--f-cmt", s.cmt + "px");
    root.style.setProperty("--f-cmt-small", s.cmtSmall + "px");
  }

  // 待译停留的计时器：一轮等待（从没有在等的一句，到有一句在等）只排一个，到点时还在等的是哪一句就先放哪一句的
  // 原文（floatReleaseHeld）。等待中又来一句改等新的那句，但截止时刻不往后推（同中控页 holdLiveBar）：句子一直
  // 比译文来得快时（识别积压后一串句子集中到达），每来一句重数 4 秒的话，窗口会一直停在旧句。
  // 这一轮结束了（换上了、换场、清空、hello）holdRound 就清掉，旧的计时器到点什么都不做
  var holdRound = null;
  function armHold(wasHeld) {
    if (!state.held) { holdRound = null; return; }
    if (wasHeld && holdRound) return;
    var round = holdRound = {};
    setTimeout(function () {
      if (holdRound !== round || !state.held) return;
      holdRound = null;
      if (floatReleaseHeld(state, state.held.id)) render();
    }, FLOAT_HOLD_MS);
  }

  function handle(msg) {
    var wasHeld = !!state.held;
    floatReduce(state, msg);
    if (msg && (msg.type === "hello" || msg.type === "config")) applySizes();
    armHold(wasHeld);
    render();
  }

  function connect() {
    var ws = new WebSocket("ws://" + location.host + "/ws?role=float");
    ws.onopen = function () { retries = 0; };
    ws.onclose = function () {
      // 和程序断了线：大字幕上那一句不会再更新了，状态行写「连接中…」，重连后的 hello 会整份换掉。
      // 结束卡片露着时不换：监听已经停了，断线不改变这一点（程序重启后 hello 会带来新的状态）
      if (!floatEndView(state)) handle({ type: "status", state: "connecting" });
      setTimeout(connect, Math.min(5000, 700 * Math.pow(2, retries++)));
    };
    ws.onerror = function () { try { ws.close(); } catch (e) { /* noop */ } };
    ws.onmessage = function (evt) {
      var msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      handle(msg);
    };
  }

  applySizes();
  render();
  connect();
  // 「回到主窗口」：只调 JS 桥，不发 WebSocket 消息
  if (els.endBack && typeof els.endBack.addEventListener === "function") {
    els.endBack.addEventListener("click", function () { floatGoMain(win); });
  }
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    // 拉大缩小窗口会改变装不装得下：重新量一次
    window.addEventListener("resize", function () { floatMarkClipped(els); });
    // pywebview 在页面加载之后才注入 JS 桥，注入完发这个事件：按钮这时才画得出来
    window.addEventListener("pywebviewready", render);
  }
  // 「约 N 秒前」每秒涨一次（同中控页底部大字幕）；状态行出现、消失会改变装不装得下，顺手再量
  if (typeof setInterval === "function") {
    setInterval(function () {
      if (!state.caption || document.hidden) return;
      floatPaintMeta(els, state, Date.now());
      floatMarkClipped(els);
    }, 1000);
  }
})();

/* Node 测试环境导出；浏览器里作为全局函数被上面的胶水使用 */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { FLOAT_COMMENTS_MAX: FLOAT_COMMENTS_MAX, FLOAT_HOLD_MS: FLOAT_HOLD_MS,
                     FLOAT_END_STATES: FLOAT_END_STATES, floatEndView: floatEndView,
                     floatMainBridge: floatMainBridge, floatGoMain: floatGoMain, floatPaintEnd: floatPaintEnd,
                     floatInitialState: floatInitialState,
                     floatReduce: floatReduce, floatReleaseHeld: floatReleaseHeld, floatCaptionView: floatCaptionView,
                     floatPlaceholder: floatPlaceholder, floatCommentView: floatCommentView,
                     floatSizes: floatSizes, floatBuildComment: floatBuildComment,
                     floatGuardCtx: floatGuardCtx, floatRender: floatRender, floatMarkClipped: floatMarkClipped,
                     floatCaptionMeta: floatCaptionMeta, floatPaintMeta: floatPaintMeta };
}
