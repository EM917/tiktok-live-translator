// i18n: done
/* TikTok 直播同传 —— 前端逻辑：WebSocket 收字幕、渲染历史 + 底部大字幕、启动/停止直播间。
   界面文字写成 L("中文", "English") / LN(n, …)（web/i18n.js，docs/i18n-style.md）；数据不进 L */
(function () {
  // 浏览器把 127.0.0.1 的站点数据整个禁掉（隐私开关/企业策略）时，localStorage
  // 一碰就抛 SecurityError——不兜住的话整个初始化脚本在第一行就断掉，页面停在
  // 「等待连接…」且没有任何报错。这些值只是本地偏好，真正要紧的都在 settings.json
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 忽略 */ } }
  "use strict";

  // index.html 顶部 .icon-sprite 里的 <symbol>：动态生成的图标也走 <use href>，
  // 路径只在 HTML 里存一份。createElementNS：普通 createElement 造出来的 <svg>
  // 不在 SVG 命名空间里，浏览器不会画
  var SVG_NS = "http://www.w3.org/2000/svg";
  function iconEl(name) {
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "i");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }
  // 图标 + 一段文字：文字仍按数据写（textContent），不拼 HTML
  function setIconText(el, name, text) {
    if (!el) return;
    el.textContent = "";
    el.appendChild(iconEl(name));
    var span = document.createElement("span");
    span.textContent = text;
    el.appendChild(span);
  }

  // 数据不进 L()（spec §4.1 R7），承载数据的节点分两类标（用户决定 6，改了 spec §6）：
  // - 名字：主播名、观众名、品牌名、违禁词条、模型名，标 translate="no"。浏览器自带的「翻译此页」
  //   不去改名字，英文界面的检查（G5/G10）也跳过它们。
  // - 正文：字幕原文和译文、弹幕正文和译文、报警原话和译文，不标 translate——用浏览器翻译看页面的
  //   人读的就是这些，不能把这条退路堵上。只加 class "i18n-data"，英文界面的检查按它豁免，不算漏翻。
  //   同一个节点有时放正文、有时放「翻译中…」这类界面提示的，class 跟着当下的内容加上或去掉，
  //   界面提示照样要查（写法同 viewer.js）
  function markName(el) {
    el.setAttribute("translate", "no");
  }
  function markBody(el, isData) {
    el.classList.toggle("i18n-data", isData);
  }
  // 一段名字：句子里夹着主播名、词条时，把名字那一截单独包起来
  function nameSpan(text) {
    var span = document.createElement("span");
    markName(span);
    span.textContent = text;
    return span;
  }

  var historyEl = document.getElementById("history");
  var startPanel = document.getElementById("start-panel");
  var roomInput = document.getElementById("room-input");
  var sourceSel = document.getElementById("source-lang");
  var brandSel = document.getElementById("brand-select");
  var brandsDirBtn = document.getElementById("brands-dir-btn");
  var brandEmptyHint = document.getElementById("brand-empty-hint");
  var startBtn = document.getElementById("start-btn");
  var recentRooms = document.getElementById("recent-rooms");
  var recentList = document.getElementById("recent-list");
  var recentClear = document.getElementById("recent-clear");
  var stopBtn = document.getElementById("stop-btn");
  var statusDot = document.getElementById("status-dot");
  var statusText = document.getElementById("status-text");
  // 状态胶囊本体：setStatus 把状态写进它的 data-state，CSS 用属性选择器
  // 派生底色/文字色，不依赖 :has()（Safari 15.4 以下不支持，见 style.css）
  var statusPill = document.querySelector(".status-pill");
  var statusBanner = document.getElementById("status-banner");
  var liveBar = document.getElementById("live-bar");
  var jumpBtn = document.getElementById("jump-latest");
  var commentJump = document.getElementById("comment-jump");
  var engineSelect = document.getElementById("engine-select");
  var engineKey = document.getElementById("engine-key");
  var engineSave = document.getElementById("engine-save");
  var engineActive = document.getElementById("engine-active");
  var engineNote = document.getElementById("engine-note");
  var liveTranslated = document.getElementById("live-translated");
  var liveOriginal = document.getElementById("live-original");
  var liveMeta = document.getElementById("live-meta");     // 大字幕下面一小行：翻译状态角标 + 「约 N 秒前」
  var liveState = document.getElementById("live-state");
  var liveAsr = document.getElementById("live-asr");       // 「语音识别不确定」角标（默认关）
  var liveLag = document.getElementById("live-lag");
  var connectHintEl = document.getElementById("connect-hint");
  var connectHintText = document.getElementById("connect-hint-text");
  var targetSel = document.getElementById("target-lang");
  var fontSlider = document.getElementById("font-size");
  var clearBtn = document.getElementById("clear-btn");
  var prevCapsTitle = document.getElementById("prev-caps-title");
  var migrateBar = document.getElementById("migrate-bar");
  var migrateText = document.getElementById("migrate-text");
  var migrateBtn = document.getElementById("migrate-btn");
  var updateBar = document.getElementById("update-bar");
  var updateText = document.getElementById("update-text");
  var updateBtn = document.getElementById("update-btn");
  var updateLink = document.getElementById("update-link");
  var versionEl = document.getElementById("app-version");
  var alertPanel = document.getElementById("alert-panel");
  var alertList = document.getElementById("alert-list");
  var alertCount = document.getElementById("alert-count");
  var clearAlertsBtn = document.getElementById("clear-alerts");
  var commentPanel = document.getElementById("comment-panel");
  var commentList = document.getElementById("comment-list");
  var toggleCommentsBtn = document.getElementById("toggle-comments");
  var clearCommentsBtn = document.getElementById("clear-comments");
  var commentFab = document.getElementById("comment-fab");
  var commentEmpty = document.getElementById("comment-empty");
  var commentSource = document.getElementById("comment-source");
  var commentTitleEl = document.getElementById("comment-title-text");
  // 弹幕留意（Comment Review，web/guard.js）：顶部条 + 抽屉 + 弹幕栏状态胶囊 + 设置行。
  // 规则 / 文案 / 渲染在 guard.js，这里只接线：收消息、存状态、把回调传进去
  var guardBar = document.getElementById("guard-bar");
  var guardBarMain = document.getElementById("guard-bar-main");
  var guardBarText = document.getElementById("guard-bar-text");
  var guardBarSeen = document.getElementById("guard-bar-seen");
  var guardBarView = document.getElementById("guard-bar-view");
  var guardDrawer = document.getElementById("guard-drawer");
  var guardDrawerState = document.getElementById("guard-drawer-state");
  var guardReenableBtn = document.getElementById("guard-reenable");
  var guardReenableCap = document.getElementById("guard-reenable-cap");
  var guardDrawerClose = document.getElementById("guard-drawer-close");
  var guardTabRecent = document.getElementById("guard-tab-recent");
  var guardTabMarked = document.getElementById("guard-tab-marked");
  var guardTabMarkedN = document.getElementById("guard-tab-marked-n");
  var guardPaneRecent = document.getElementById("guard-pane-recent");
  var guardPaneMarked = document.getElementById("guard-pane-marked");
  var guardEventsEl = document.getElementById("guard-events");
  var guardMarkedEl = document.getElementById("guard-marked");
  var guardStateBtn = document.getElementById("guard-state");
  var guardStateTextEl = document.getElementById("guard-state-text");
  var commentFabGuard = document.getElementById("comment-fab-guard");
  var guardCard = document.getElementById("guard-card");
  var guardHead = document.getElementById("guard-head");
  var guardBodyEl = document.getElementById("guard-body");
  var guardSummaryEl = document.getElementById("guard-summary");
  var guardToggle = document.getElementById("guard-toggle");
  var guardBackendSel = document.getElementById("guard-backend");
  var guardKeyRow = document.getElementById("guard-key-row");
  var guardKeyInput = document.getElementById("guard-key");
  var guardKeySave = document.getElementById("guard-key-save");
  var guardStatusEl = document.getElementById("guard-status");
  var guardPrivacy = document.getElementById("guard-privacy");
  var guardWeak = document.getElementById("guard-weak");
  var guardMarkedHeading = document.getElementById("guard-marked-heading");
  var guardMarkedList = document.getElementById("guard-marked-list");
  var guardClearAuto = document.getElementById("guard-clear-auto");
  var guardDirBtn = document.getElementById("guard-dir-btn");
  var guardRulesEl = document.getElementById("guard-rules");
  var backendState = "idle";   // 后端 TikTokLive 抓取协程的状态：idle/connecting/connected/disconnected/error/unavailable
  var backendDetail = "";      // backendState 为 error/unavailable 时的说明文字
  var streamActive = false;    // 直播中/连接中：这时面板即使空着也要显示
  // 违禁词警示：默认关闭（负责人明确要求，见 CLAUDE.md），实际值以后端 hello/
  // alert_mode 广播为准；本地只在用户没点过开关、也还没收到后端值之前用这个默认。
  var alertsEnabled = false;
  var watchlistCount = 0;   // 最近一次 renderWatchlist 报的词表条数，watchSummary（settings-rows.js）据此算 desc
  // Object.create(null)：commentById 的键直接取自服务端转发的弹幕 id（最终来自
  // TikTok 页面上任意脚本可控的 viewer_comments.items[].id），普通字面量 {} 遇到
  // "__proto__" 这个键时会触发 Object.prototype 的存取器而不是新增普通键，
  // 用无原型对象从根上避免这种协议层面就能触发的原型链篡改。
  var commentById = Object.create(null);
  // 弹幕重译（app/comment_redo.py）：当前生效的字幕引擎是不是 Claude——只有 Claude 能重译弹幕，
  // 其它引擎下按钮是灰的。值来自 engine 消息的 active（renderEngine）；还没收到时按不是处理
  var commentRedoEngine = "";
  var commentRedoTimers = Object.create(null);   // 弹幕 id -> 说明的收起计时器
  // 弹幕的译文和重译状态按 id 另记一份（不靠弹幕栏的 DOM）：弹幕栏只留最近 100 条，「需要留意」抽屉里的
  // 事件可能更早；抽屉也要在译文、重译结果到了的时候跟着重画（2026-10-15：抽屉里的弹幕一直没有译文）
  var commentTr = Object.create(null), commentTrOrder = [], commentTrSeq = 0;
  var COMMENT_TR_KEEP = 500;
  // 服务端只留最近 200 条弹幕的原文（app/comment_redo.py 的 ORIGINAL_KEEP）：更早的弹幕不再给「重译」
  var COMMENT_REDO_KEEP = 200;
  var commentRedoState = Object.create(null);    // 弹幕 id -> "pending" | "failed"
  var commentRedoNotes = Object.create(null);    // 弹幕 id -> 正在显示的重译说明
  var commentRedoStateTimers = Object.create(null), commentRedoNoteTimers = Object.create(null);
  // 能点「重译」的状态：译好了的，和没译出来的（失败、积压跳过）。
  // 已是目标语言（same）、引擎不翻弹幕（skipped）的没有可重译的
  var COMMENT_REDO_STATES = { ok: true, failed: true, dropped: true };
  var healthBar = document.getElementById("health-bar");
  var incidentBar = document.getElementById("incident-bar");
  var watchState = document.getElementById("watch-state");
  var watchDesc = document.getElementById("watch-desc");
  var alertsToggle = document.getElementById("alerts-toggle");
  var alertModeTag = document.getElementById("alert-mode-tag");
  var activeBrandTag = document.getElementById("active-brand-tag");
  var fixCmd = document.getElementById("fix-command");
  var fixCmdText = document.getElementById("fix-command-text");
  var fixCmdCopy = document.getElementById("fix-command-copy");
  var scBox = document.getElementById("selfcheck");
  var scHead = document.getElementById("sc-head");
  var scIcon = document.getElementById("sc-icon");
  var scSummary = document.getElementById("sc-summary");
  var scList = document.getElementById("sc-list");
  var diskHead = document.getElementById("disk-head");
  var diskSummary = document.getElementById("disk-summary");
  var diskBody = document.getElementById("disk-body");
  var diskList = document.getElementById("disk-list");
  var diskDelete = document.getElementById("disk-delete");
  // 设置分组里新增的可展开行（自检、磁盘沿用原来的 #sc-head / #disk-head）
  var engineHead = document.getElementById("engine-head");
  var engineBody = document.getElementById("engine-body");
  var watchHead = document.getElementById("watch-head");
  var watchBody = document.getElementById("watch-body");
  var watchMode = document.getElementById("watch-mode");
  var inputHelpBtn = document.getElementById("input-help-btn");
  var inputHelp = document.getElementById("input-help");
  // 界面语言行（spec §3.4）：config 里没有 ui_lang_available（发布闸关着、又没有
  // --ui-lang 一次性覆盖）时整行一直隐藏，这几个元素什么都不改
  var langCard = document.getElementById("lang-card");
  var langHead = document.getElementById("lang-head");
  var langBody = document.getElementById("lang-body");
  var langSummaryEl = document.getElementById("lang-summary");
  var uiLangSelect = document.getElementById("ui-lang-select");
  var uiLangSystemOpt = document.getElementById("ui-lang-system");
  // 字幕记录行：和违禁词报警、弹幕留意一样默认收起、永不自动展开（CLAUDE.md 第六条）
  var transcriptHead = document.getElementById("transcript-head");
  var transcriptBody = document.getElementById("transcript-body");
  var transcriptSummaryEl = document.getElementById("transcript-summary");
  var transcriptStatusEl = document.getElementById("transcript-status");
  var transcriptExportBtn = document.getElementById("transcript-export");
  var transcriptDirBtn = document.getElementById("transcript-dir-btn");

  // 设置分组的行：展开状态记在行的 aria-expanded 上（CSS 据此转箭头），内容区
  // 照旧靠 .hidden 收放。引擎、报警、输入说明这三行没有额外的开合时机，直接
  // bindRow 挂点击；自检、磁盘各自在展开时还要多做一件事（自检失败自动展开、
  // 磁盘展开时才向服务端要盘点），改成自己接管点击、调用 setRowOpen——收放
  // 本身仍是同一个函数，只是不是每一行都经过 bindRow（engineer.md #8）
  function setRowOpen(row, body, open) {
    if (!row || !body) return;
    body.classList.toggle("hidden", !open);
    row.setAttribute("aria-expanded", open ? "true" : "false");
  }
  function bindRow(row, body) {
    if (!row || !body) return;
    row.addEventListener("click", function () {
      setRowOpen(row, body, body.classList.contains("hidden"));
    });
  }
  bindRow(engineHead, engineBody);
  bindRow(watchHead, watchBody);
  bindRow(guardHead, guardBodyEl);        // 弹幕留意：默认收起，永不自动展开（会自己展开的只有自检和翻译引擎）
  bindRow(inputHelpBtn, inputHelp);
  bindRow(langHead, langBody);
  bindRow(transcriptHead, transcriptBody);

  // 手机同看卡片（#share-card）：只在打开期间监听 0.0.0.0，控制面本身始终只在
  // 127.0.0.1；这张卡片只发/收 viewer_share / viewer_rotate，看不到任何观众数据
  //
  // 卡片以前挂在 #start-panel 下面，直播开始后整块面板被隐藏，中控恰恰是在
  // 直播中才想扫码给同事看，却找不到入口。现在卡片单独放进 #share-panel，
  // 由顶栏的 #share-btn 开关，跟直播状态无关，待机/直播都能点开。
  var shareBtn = document.getElementById("share-btn");
  var shareBtnText = document.getElementById("share-btn-text");
  var shareBtnCount = document.getElementById("share-btn-count");
  var sharePanel = document.getElementById("share-panel");
  var shareHelpBtn = document.getElementById("share-help-btn");
  var shareHelp = document.getElementById("share-help");
  var shareToggle = document.getElementById("share-toggle");
  var shareState = document.getElementById("share-state");
  var shareDesc = document.getElementById("share-desc");
  var shareBody = document.getElementById("share-body");
  var shareQr = document.getElementById("share-qr");
  var shareUrl = document.getElementById("share-url");
  var shareCopy = document.getElementById("share-copy");
  var shareRotate = document.getElementById("share-rotate");
  var shareClose = document.getElementById("share-close");
  var shareCollapse = document.getElementById("share-collapse");
  var shareCount = document.getElementById("share-count");
  var shareAddrChanged = document.getElementById("share-addr-changed");
  var shareNote = document.getElementById("share-note");
  var shareIpList = document.getElementById("share-ip-list");
  // 浮层标题旁的 ⓘ：三段「手机连不上时」的排查说明，跟首页输入框旁那颗同一种收放
  bindRow(shareHelpBtn, shareHelp);

  // 浮窗（app/float_window.py）：顶栏 #float-btn 开关一个始终在最上面的小窗。只有桌面应用窗口模式才有
  // （config.float_window.available），而且只在监听中/连接中露出，和 #switch-btn 同一处切换（setStatus）；
  // 监听结束服务端会把浮窗关掉，按钮的点亮状态以服务端回执（config.float_window.open）为准
  var floatBtn = document.getElementById("float-btn");
  var floatAvailable = false;      // config.float_window.available
  var floatOpen = false;           // config.float_window.open
  var floatPending = false;        // 点过了、还没等到服务端回执：按钮暂时禁用
  var floatPendingTimer = null;

  function updateFloatBtn() {
    if (!floatBtn) return;
    var st = floatButtonState(floatAvailable, streamActive, floatOpen, floatPending);
    floatBtn.classList.toggle("hidden", st.hidden);
    floatBtn.classList.toggle("on", st.on);
    floatBtn.setAttribute("aria-pressed", st.pressed ? "true" : "false");
    floatBtn.disabled = st.disabled;
    // 顶栏多了一个按钮：样式表据此把字号滑块收窄一点（style.css 的 has-float-btn）
    document.body.classList.toggle("has-float-btn", !st.hidden);
  }

  // hello 里整份换（没有这个键 = 这个服务端不提供浮窗），config 广播里只在带这个键时换
  function applyFloatConfig(cfg) {
    var ok = cfg && typeof cfg === "object";
    floatAvailable = ok && !!cfg.available;
    floatOpen = ok && !!cfg.open;
    floatPending = false;
    if (floatPendingTimer) { clearTimeout(floatPendingTimer); floatPendingTimer = null; }
    updateFloatBtn();
  }

  if (floatBtn) {
    floatBtn.addEventListener("click", function () {
      if (floatPending) return;
      // 没发出去（断线）就不装作点成了：状态等重连后的 hello 来说
      if (!send({ type: "float_window", on: !floatOpen })) return;
      floatPending = true;
      floatPendingTimer = setTimeout(function () {
        floatPending = false;
        floatPendingTimer = null;
        updateFloatBtn();
      }, 4000);
      updateFloatBtn();
    });
  }

  // 换主播：直播中不停止监听、直接改听另一个主播。面板照 #share-panel 挂在
  // #start-panel/#history 之外，跟直播状态无关地独立开关——但按钮本身（顶栏
  // #switch-btn）只在直播中/连接中才露出，和 #stop-btn 同一处切换（见 setStatus）。
  var switchBtn = document.getElementById("switch-btn");
  var switchPanel = document.getElementById("switch-panel");
  var switchClose = document.getElementById("switch-close");
  var switchSub = document.getElementById("switch-sub");
  var switchInput = document.getElementById("switch-input");
  var switchRecent = document.getElementById("switch-recent");
  var switchRecentList = document.getElementById("switch-recent-list");
  var switchBrandSel = document.getElementById("switch-brand-select");
  var switchSourceEcho = document.getElementById("switch-source-echo");
  var switchError = document.getElementById("switch-error");
  var switchConfirmBtn = document.getElementById("switch-confirm");
  var switchHint = document.getElementById("switch-hint");

  // 页面生命周期里语言不变（换语言整页重载），加载时求值一次就够
  var STATUS_TEXT = {
    idle: L("待机", "Ready"),
    connecting: L("连接中…", "Connecting…"),
    live: L("直播中", "Live"),
    ended: L("直播已结束", "Stream ended"),
    error: L("出错了", "Error"),
    offline: L("与本地服务断开，重连中…", "Reconnecting…"),   // 完整说明在横幅里
  };

  var ws = null;
  var retries = 0;
  // 页面最多留几张字幕卡片、几条弹幕。服务端 hello 回放的深度取同一个数（app/server.py 的 HISTORY_MAX /
  // COMMENTS_MAX），刷新或重连后看到的就是刷新前能往上翻的那些。tests/test_server_clear.py 读这两行
  // 比对，改一边不改另一边测试会红；弹幕栏的裁剪也都用 maxComments，别再写裸数字
  var maxHistory = 300;
  var maxComments = 100;
  var currentVersion = "";
  var startWatchdog = null;
  var pendingStart = null;   // 已发出但服务器还没回执的「开始」指令（重连后补发）
  var versionNoticeTimer = null;
  var updateCheckNote = "";  // 很久没连上更新服务器时版本号后面那句话（文字由服务端给）
  var updateConfirmTimer = null;
  var recentClearConfirmTimer = null;
  var liveBarId = null;      // 底部大字幕当前显示的是哪一条（译文回来要就地替换）
  // 底部大字幕的待译停留（专家审查 U1，用户 10-22 定的「等译文到了再一起换」）：新的一句先不上大字幕，
  // 大字幕停在上一句；译文到了、或者确定不会有译文了，再整句一起换上。以前每句先放西语、约 0.9 秒后跳成中文，
  // 中控盯着的那一行每小时闪约 475 次。等满 LIVE_HOLD_MS 还没结果就照以前的样子先放原文（Claude 下 p99 约 3.9 秒）
  var LIVE_HOLD_MS = 4000;
  var liveHold = null;       // { msg, at, until, timer }：在等译文的那一句；null 是没有在等
  // 字幕先出原文、译文后补，所以要能按 id 找回已渲染的那张卡片
  var cardsById = {};
  // 布局意义上的「直播中」，供 viewTransition 判断迁移方向；null＝页面刚加载，
  // 还没收到过任何状态——不能当成「已经在待机」，否则首次到达就是 idle 时会被
  // 误判成「没有变化」，进首页该做的动作（大字幕/弹幕面板归位）就漏了
  var homeActive = null;
  var statusState = "";      // 最近一条 status 的 state（setStatus 记下）：换主播时 config 先到，胶囊据此重画（F5）
  // 最近一条 status 带的结构化进度（专家审查 U7）：首次下载识别模型时后端带 {kind:"model_download", pct}，
  // 字幕区中间的卡片据此写下载进度（web/live-ui.js connectHint）。不带就是 null，卡片照旧写「正在连接」
  var statusProgress = null;
  // 这一场有没有出过字幕（专家审查 U8）：直播中还没出过时，字幕区中间写「等待第一句字幕…」。
  // 新的一场（session_break，实时和回放都算）、重连的 hello（回放会按顺序重新走一遍）时归零，来一条字幕置真。
  // 不按「#history 里有没有本场的 .cap」现算：直播中点了「清空字幕」之后页面上一条也没有，
  // 那时写「等待第一句」不对——这一场早就出过字幕了，只是被清掉了
  var sessionCaptionSeen = false;
  // 回放里的场次分隔线在弹幕栏里的位置（专家审查 F1）。服务端先回放字幕（分隔线夹在字幕中间），最后才回放
  // 弹幕；分隔线要是在字幕阶段就插进弹幕栏，就跑到所有回放弹幕的最前面，上一位主播的弹幕被标成「以下为
  // @新主播」。所以回放的分隔线先记在这里，回放弹幕时插到第一条 ts 不早于它的弹幕前面（弹幕的 ts 是本机
  // 收到的时间，和分隔线同一个钟）；回放之后的第一条实时消息到了，剩下的（换场之后还没有弹幕）补在末尾
  var pendingCommentBreaks = [];

  // ---- 设置 ----
  // 字幕字号。服务端（settings.json）是正本：localStorage 按「协议+主机+端口」隔离，端口一漂移
  // （8765 被占用时程序自动改用下一个）调好的字号就没了。所以这里只做两件事：
  //   1. 页面一加载先用 localStorage 里的值画出来（hello 到之前不闪成默认字号）；
  //   2. 之后以服务端为准：hello/config 带来的 caption_size 覆盖它，服务端没记过而本机记着
  //      就交给服务端存起来（旧版本只存在这里的字号这样不丢）。
  // 只有用户显式调过字号才覆盖 CSS 默认值（否则会压掉移动端媒体查询的 26px）
  var savedFont = captionSizeFromConfig(lsGet("subFontSize"));
  if (savedFont != null) {
    fontSlider.value = savedFont;
    applyFont(savedFont);
  } else {
    var cssSize = parseInt(getComputedStyle(document.documentElement)
      .getPropertyValue("--sub-size"), 10);
    if (cssSize) fontSlider.value = cssSize;
  }

  // 主播语言的 <select> 只认表里已有的 value（单个语言码，或默认那一种列表组合
  // "es,en"）：回填的值缺失、或是表里没有的旧值/脏数据，都退回默认组合，不能让
  // select 卡在没有任何选项被选中的空白态
  function selectSourceLang(value) {
    var v = value ? String(value) : "";
    for (var i = 0; i < sourceSel.options.length; i++) {
      if (sourceSel.options[i].value === v) { sourceSel.value = v; return; }
    }
    sourceSel.value = "es,en";
  }

  // localStorage 里的 "auto" 不回填：老版本默认就是 auto，它大概率是历史
  // 默认值而非用户的选择——现在默认是西语+英语。真选过自动检测的用户，其选择
  // 存在服务端（settings.json），随 config 消息回填，不经这里
  var savedSource = lsGet("sourceLang");
  if (savedSource && savedSource !== "auto") selectSourceLang(savedSource);
  // savedRoom 先读出来，回填挪到下面（brandState 声明之后）——回填要顺带
  // 同步 brandState.streamer（brandStateSyncStreamer，只记主播不清 touched），
  // 这是页面加载不是中控操作，不能套用 brandStateAfterRoomInput 那条规则
  var savedRoom = lsGet("roomUrl");
  // 服务端记住的主播语言随 config 到达后回填（localStorage 按端口隔离，
  // 端口漂移就丢了）；但本页里用户已亲手改过的选择不能被盖掉
  var sourceTouched = false;
  sourceSel.addEventListener("change", function () { sourceTouched = true; });

  // 品牌词表下拉：只认表里已有的 value，回填的值缺失/脏数据退回「不限」——
  // 不能让 select 卡在空白态。selectEl 是形参而不是硬编码 brandSel，是因为
  // 换主播面板（#switch-brand-select）要用同一套回填规则，不能只有开始面板那份
  function selectBrand(selectEl, value) {
    if (!selectEl) return;
    var v = value ? String(value) : "";
    for (var i = 0; i < selectEl.options.length; i++) {
      if (selectEl.options[i].value === v) { selectEl.value = v; return; }
    }
    selectEl.value = "";
  }

  // 下拉框的选项由 config.brand_options 动态生成（brands/ 文件夹里的文件
  // 决定有哪些可选），不写死在页面里；选项列表本身、以及重建后该保留哪个
  // 选中值，是 web/brand.js 里的纯函数（buildBrandOptionList /
  // resolveSelectedBrand），这里只管 DOM——textContent 赋值，不拼 HTML，
  // 显示名是用户自己写的文件内容，不能当成 HTML 解析。返回 opts 供调用方
  // （目前只有开始面板要用它判断「有没有可选品牌」）做进一步判断
  function renderBrandOptionsInto(selectEl, brandOptions) {
    if (!selectEl) return null;
    var opts = buildBrandOptionList(brandOptions);
    var kept = resolveSelectedBrand(selectEl.value, opts);
    while (selectEl.firstChild) selectEl.removeChild(selectEl.firstChild);
    for (var i = 0; i < opts.length; i++) {
      var opt = document.createElement("option");
      opt.value = opts[i].value;
      opt.textContent = opts[i].label;
      if (opts[i].value) markName(opt);   // 品牌名是名字；value 为空的「不限」是界面文字
      selectEl.appendChild(opt);
    }
    selectEl.value = kept;
    return opts;
  }

  // 开始面板和换主播面板的品牌下拉是同一份数据（config.brand_options），
  // 两个 <select> 一起重建，保持永远同步——不然换主播面板打开时可能还是
  // 上一次没刷新过的旧选项列表
  function renderBrandOptions(brandOptions) {
    var opts = renderBrandOptionsInto(brandSel, brandOptions);
    renderBrandOptionsInto(switchBrandSel, brandOptions);
    if (!opts) return;
    // opts 里固定带着「不限」一项：长度 1 就是除它之外一个可选品牌都没有
    if (brandEmptyHint) brandEmptyHint.classList.toggle("hidden", opts.length > 1);
  }

  // 按主播记住的品牌映射（hello/config 里的 config.brands），及「换到这个主播
  // 之后用户有没有手动改过下拉」——规则见 web/brand.js：换了主播（输入框或
  // 最近直播间 chip）时，没手动改过就按记住的值刷新；改过之后，只要主播没换就
  // 不再替用户做主（同一个主播的地址改写不算换，见 brandStateAfterRoomInput）
  var brandsMap = {};
  var brandState = { streamer: "", touched: false };
  function applyDefaultBrand() {
    if (!brandSel || brandState.touched) return;
    selectBrand(brandSel, defaultBrandFor(streamerFromInput(roomInput.value), brandsMap));
  }
  function onRoomInputChanged() {
    brandState = brandStateAfterRoomInput(brandState, streamerFromInput(roomInput.value));
    applyDefaultBrand();
  }
  // 获得焦点/按下鼠标时先让后端重新扫一遍 brands 文件夹：用户刚放进去的
  // 词表不用重启程序就能在下拉里出现。两个事件都挂是为了尽量在选项真正
  // 展开之前把新列表发过来，键盘/触屏只触发 focus，鼠标点击两个都会触发。
  // 开始面板和换主播面板的品牌下拉共用同一个刷新请求
  var requestBrandRefresh = function () { send({ type: "refresh_brands" }); };
  if (brandSel) {
    brandSel.addEventListener("change", function () { brandState.touched = true; });
    brandSel.addEventListener("focus", requestBrandRefresh);
    brandSel.addEventListener("mousedown", requestBrandRefresh);
  }

  // 换主播面板自己的一份「按主播记住的品牌」跟踪状态，规则和开始面板的
  // brandState 完全一样（见 web/brand.js），只是主播名来自 #switch-input
  // 而不是 #room-input——两个面板认的是两个不同的主播（@A 正在听的，@B 要换成的）
  var switchBrandState = { streamer: "", touched: false };
  function applySwitchDefaultBrand() {
    if (!switchBrandSel || switchBrandState.touched) return;
    selectBrand(switchBrandSel, defaultBrandFor(switchBrandState.streamer, brandsMap));
  }
  if (switchBrandSel) {
    switchBrandSel.addEventListener("change", function () { switchBrandState.touched = true; });
    switchBrandSel.addEventListener("focus", requestBrandRefresh);
    switchBrandSel.addEventListener("mousedown", requestBrandRefresh);
  }
  function openBrandsDir() { send({ type: "open_brands_dir" }); }
  if (brandsDirBtn) brandsDirBtn.addEventListener("click", openBrandsDir);
  // 空态里那句话末尾的「打开 brands 文件夹」文字按钮：同一个消息，跟旁边的
  // 文件夹图标按钮是两个入口、一个动作（pm.md #6）
  var brandHintOpenBtn = document.getElementById("brand-hint-open");
  if (brandHintOpenBtn) brandHintOpenBtn.addEventListener("click", openBrandsDir);
  // savedRoom 的回填放在这里（brandState 已声明）：同步记一下主播名，
  // 不清 touched（此刻必然是 false，页面刚加载还没人碰过下拉，写这行只是
  // 让「回填不清 touched」这条规则从一开始就一致，不是这里真的需要保留什么）
  if (savedRoom) {
    roomInput.value = savedRoom;
    brandState = brandStateSyncStreamer(brandState, streamerFromInput(savedRoom));
  }
  roomInput.addEventListener("input", onRoomInputChanged);

  // 本页里用户拖过滑块：之后服务端广播来的字号不再覆盖他手上的（其它窗口的改动也一样，以这一页为准）
  var captionSizeTouched = false;
  // 松手时没能发给服务端（连接断着）的字号；重连后的 hello 里补发，免得被服务端的旧值盖回去
  var captionSizeUnsent = null;

  fontSlider.addEventListener("input", function () {
    var size = parseInt(fontSlider.value, 10);
    captionSizeTouched = true;
    applyFont(size);
    lsSet("subFontSize", String(size));
  });
  // 拖动中只改本页的显示；松手（change）才存进服务端，免得一次拖动写几十次 settings.json
  fontSlider.addEventListener("change", function () {
    var size = captionSizeFromConfig(fontSlider.value);
    if (size == null) return;
    captionSizeTouched = true;
    applyFont(size);
    lsSet("subFontSize", String(size));
    captionSizeUnsent = send({ type: "set_caption_size", value: size }) ? null : size;
  });

  // 服务端说的字号（hello 的 config.caption_size，或另一个窗口调整后的 config 广播）
  function adoptCaptionSize(value) {
    var size = captionSizeFromConfig(value);
    if (size == null || captionSizeTouched) return;
    fontSlider.value = size;
    applyFont(size);
    lsSet("subFontSize", String(size));
  }

  // hello 里调：重连时先补发没发出去的；服务端有记录就以它为准；服务端没记过而本机记着，交给它存
  function syncCaptionSize(config) {
    if (captionSizeUnsent != null) {
      captionSizeUnsent = send({ type: "set_caption_size", value: captionSizeUnsent }) ? null : captionSizeUnsent;
      return;
    }
    if (captionSizeFromConfig(config.caption_size) != null) {
      adoptCaptionSize(config.caption_size);
      return;
    }
    var local = captionSizeFromConfig(lsGet("subFontSize"));
    if (local != null) send({ type: "set_caption_size", value: local });
  }

  // 目标语言以服务端为准（跟随 --target 启动参数），UI 切换即时生效但不做本地持久化
  targetSel.addEventListener("change", function () {
    send({ type: "set_target", value: targetSel.value });
  });

  // 「字幕译成」下拉显示服务端说的当前目标（hello 和 config 广播）。下拉只列四种（index.html）；
  // 当前目标不在里面时（settings.json 里存着以前选的日语、--target ja 启动）照 targetLangExtra
  // （live-ui.js）补一项、标 data-extra，选中它——直接写 value 的话，没有这一项的下拉会显示成空白或
  // 第一项，看着像在出简体中文。换回四种之一以后，补的那项撤掉：下拉里只有四种加「此刻正在用的那一种」
  function showTargetLang(code) {
    var value = code == null ? "" : String(code);
    if (!value) return;
    var opts = [];
    for (var i = 0; i < targetSel.options.length; i++) opts.push(targetSel.options[i]);
    var present = false;
    opts.forEach(function (opt) {
      if (opt.value === value) present = true;
      else if (opt.getAttribute("data-extra") !== null) targetSel.removeChild(opt);
    });
    var extra = targetLangExtra(value);
    if (extra && !present) {
      var added = document.createElement("option");
      added.value = extra.value;
      added.textContent = extra.text;
      added.setAttribute("data-extra", "1");
      targetSel.appendChild(added);
    }
    targetSel.value = value;
  }

  // 「清空字幕」按钮本身，以及停止后停留在待机首页的「上一场字幕」小标题，
  // 都只在 #history 里确实还有 .cap 时才该出现（pm.md #8、designer.md #6）。
  // 两处判断条件不同（前者不看是不是首页，后者只在首页才显示），抽成一个
  // 函数只是不想让「有没有字幕」这个判断在多处各写一份、改一个漏一个
  function syncCaptionDependentUi() {
    var hasCaps = !!historyEl.querySelector(".cap");
    if (clearBtn) clearBtn.classList.toggle("hidden", !hasCaps);
    if (prevCapsTitle) prevCapsTitle.classList.toggle("hidden", streamActive || !hasCaps);
  }

  // 只清这一页的字幕：点「清空」、hello 重建前的清场、别的页面点了清空后的同步，都走这里
  function clearCaptionsView() {
    // 分隔条（.session-sep，换主播时插的「── HH:MM:SS 以下为 @B ──」）跟着
    // 字幕一起清掉，不然重连回放前清场时，旧分隔条会越攒越多
    var caps = historyEl.querySelectorAll(".cap, .session-sep");
    for (var i = 0; i < caps.length; i++) caps[i].remove();
    cardsById = {};              // 卡片没了，id 映射也要清，否则一直涨
    hideLiveBar();
    captionsBelow = 0;           // 没有字幕了，「N 条新字幕」也就没有意义
    syncCaptionDependentUi();
    updateJumpButton();
  }

  // 让服务端也忘掉那一类：以前只删页面上的节点，缓存原样留着，刷新或重连后 hello 又把这些行
  // 放了回来——「清空」只在没刷新之前成立。连接断着时服务端收不到，只清了本页：说一声，
  // 别装作已经清掉了（重新连上后 hello 会把缓存里的放回来）
  function forgetOnServer(kind) {
    if (send({ type: "clear_history", kind: kind })) return;
    showVersionNote(kind === "comments"
      ? L("与本地服务断开，只清空了本页——重新连上后这些弹幕会回来。",
          "Lost connection to the local service. Only this page was cleared, and the comments come back once it reconnects.")
      : L("与本地服务断开，只清空了本页——重新连上后这些字幕会回来。",
          "Lost connection to the local service. Only this page was cleared, and the captions come back once it reconnects."),
      8000);
  }

  clearBtn.addEventListener("click", function () {
    clearCaptionsView();
    // 直播中点的：和弹幕栏一样，清空之后从最新处接着跟（首页状态不碰，见 following 的说明）
    if (streamActive) following = true;
    forgetOnServer("captions");
  });

  // 地址输入归一化在 normalize.js（独立成文件以便单元测试），
  // 此处使用其暴露的全局函数 normalizeRoomInput

  // 服务端回填直播间地址时保住输入框里的直连地址（房间链接 + .flv/.m3u8 那种
  // 双地址用法）：开始那一刻 config 广播只带房间链接，整个覆盖掉的话，接口
  // 不给流地址的房间在「停止→开始」时就没有直连地址可用了
  var MEDIA_RE = /https?:\/\/[^\s]+?\.(?:flv|m3u8)(?:\?[^\s]*)?/i;
  function fillRoomInput(roomUrl) {
    var m = roomInput.value.match(MEDIA_RE);
    roomInput.value = m ? roomUrl + " " + m[0] : roomUrl;
    // 服务端广播的回填（如换主播成功后台推来的新 room_url），不是中控在敲
    // 键盘——只记主播名，不清 touched（brandStateSyncStreamer，同页面加载/
    // hello 那两处，见 web/brand.js）
    brandState = brandStateSyncStreamer(brandState, streamerFromInput(roomInput.value));
  }

  // 从原始输入解析房间地址（+ 可选的直连媒体地址）。开始面板和换主播面板要
  // 各自校验一遍同样格式的输入，抽出来是不想让同一套 MEDIA_RE/normalizeRoomInput
  // 拼接逻辑在两处各写一份、改一个漏一个。error 只有两种："empty"（原始输入是
  // 空串，调用方通常直接聚焦输入框，不算错误）和 "unrecognized"（认不出，要提示）
  function parseRoomInput(raw) {
    raw = (raw || "").trim();
    if (!raw) return { error: "empty" };
    // 允许一次粘两个地址：直播间链接 + 浏览器里拿到的 .flv/.m3u8 直连地址。
    // 房间链接决定弹幕、词表和审计归属，直连地址只作音频源——只贴直连地址
    // 也能用，但那样没有主播身份，弹幕就出不来。
    var mediaMatch = raw.match(MEDIA_RE);
    var media = mediaMatch ? mediaMatch[0] : null;
    var roomPart = media ? raw.replace(media, " ").trim() : raw;
    var url = normalizeRoomInput(roomPart || raw);
    if (media && !url) { url = media; media = null; }   // 只给了直连地址
    if (!url) return { error: "unrecognized" };
    return { url: url, media: media };
  }

  var UNRECOGNIZED_INPUT_MSG = L("认不出这个输入：请粘贴直播间链接，或输入主播的英文用户名" +
                                 "（到主播主页复制 @ 后面的部分，中文昵称不行）。",
                                 "This isn’t a live link or username. Paste the live link, or enter " +
                                 "the streamer’s username, which is the part after @ on their profile. " +
                                 "Display names won’t work.");

  // 组装一条「开始」指令的 payload。开始面板和换主播面板字段完全一致，
  // 只是 source/alerts 恒取开始面板当前的值、brand 各取各自下拉的值（见调用处）
  function buildStartPayload(url, media, source, alerts, brand) {
    return { type: "start", url: url, source: source, media: media || undefined,
             alerts: !!alerts, brand: brand || "" };
  }

  // 发送「开始」指令并接管 pendingStart/看门狗——开始面板和换主播面板共用，
  // 抽出来是不想让「指令必须确认送达」这条规则（见 armStartWatchdog 的注释）
  // 在两处各实现一遍
  function sendStartCommand(payload) {
    // 「开始」指令必须确认送达：半死连接上 send 会无声进黑洞（readyState 还是
    // OPEN），随后自动重连成功、页面若无其事地回到待机——用户点了却毫无反应。
    // 服务器收到 start 后会立刻回执 connecting 状态；在那之前指令算「在途」，
    // 重连后的 hello 里补发一次，超时仍无回执才提示用户手点。
    pendingStart = { payload: payload, retried: false };
    send(pendingStart.payload);
    armStartWatchdog();
  }

  function startStream() {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setStatus({ state: "offline", detail: L("与本地服务断开，正在重连——稍候再点「开始翻译」",
        "Lost connection to the local service. Reconnecting… Click Start again in a moment.") });
      return;
    }
    var parsed = parseRoomInput(roomInput.value);
    if (parsed.error === "empty") { roomInput.focus(); return; }
    if (parsed.error) {
      setStatus({ state: "error", detail: UNRECOGNIZED_INPUT_MSG });
      return;
    }
    roomInput.value = parsed.media ? parsed.url + " " + parsed.media : parsed.url;
    lsSet("roomUrl", parsed.url);
    lsSet("sourceLang", sourceSel.value);
    sendStartCommand(buildStartPayload(parsed.url, parsed.media, sourceSel.value,
      alertsToggle && alertsToggle.checked, brandSel ? brandSel.value : ""));
    // 发出 start 之后清掉 touched：三个入口（开始按钮、回车、chip）都走这个
    // 函数，一处清零就够了。下一次不管是刷新默认值还是点 chip，都要重新按
    // 「有没有改过」判断（brandStateAfterStart，见 web/brand.js）
    brandState = brandStateAfterStart(brandState);
  }

  function armStartWatchdog() {
    if (startWatchdog) clearTimeout(startWatchdog);
    startWatchdog = setTimeout(function () {
      if (!pendingStart) return;                     // 服务器已接管
      if (!pendingStart.retried && ws && ws.readyState === WebSocket.OPEN) {
        pendingStart.retried = true;                 // 连接还在（或已重连好）：补发一次
        send(pendingStart.payload);
        armStartWatchdog();
        return;
      }
      pendingStart = null;
      try { ws.close(); } catch (e) { /* noop */ }   // 强制换一条新连接
      stickyOfflineDetail = L("指令未送达（连接中断），已自动重连——请再点一次「开始翻译」。",
        "The command didn’t go through (connection interrupted). The page reconnected. Click Start again.");
      setStatus({ state: "offline", detail: stickyOfflineDetail });
    }, 8000);
  }

  startBtn.addEventListener("click", startStream);
  roomInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") startStream();
  });
  // 用户手动扳这个开关：立刻更新描述文案和顶栏标签的预览，不用等下一次开播
  if (alertsToggle) {
    alertsToggle.addEventListener("change", function () {
      setAlertsEnabled(alertsToggle.checked);
    });
  }
  stopBtn.addEventListener("click", function () {
    pendingStart = null;                             // 在途的「开始」随之作废
    if (startWatchdog) clearTimeout(startWatchdog);
    send({ type: "stop" });
  });

  function resetUpdateBtn() {
    if (updateConfirmTimer) clearTimeout(updateConfirmTimer);
    updateConfirmTimer = null;
    delete updateBtn.dataset.confirm;
    updateBtn.disabled = false;
    updateBtn.textContent = L("一键更新", "Update Now");
    updateRunning = false;          // 按钮回到可点：这次更新不在进行了（条要不要摆由 syncNoticeBars 定）
  }

  updateBtn.addEventListener("click", function () {
    // 监听中点的要再点一次确认：更新会暂停监听，而这个按钮直播中一直摆在中控眼前。
    // 不用 window.confirm——应用窗口（pywebview）里它可能根本弹不出来
    if (streamActive && updateBtn.dataset.confirm !== "1") {
      updateBtn.dataset.confirm = "1";
      // 暂停多久要看这次要不要装新组件，页面不知道：不许诺时长，服务端的状态行会说
      updateBtn.textContent = L("再点一次确认更新：更新期间监听暂停，更新完自动恢复",
                                "Click Again to Update. Monitoring pauses and resumes afterward.");
      updateConfirmTimer = setTimeout(resetUpdateBtn, 6000);
      return;
    }
    if (updateConfirmTimer) clearTimeout(updateConfirmTimer);
    updateConfirmTimer = null;
    delete updateBtn.dataset.confirm;
    updateBtn.disabled = true;
    updateBtn.textContent = L("更新中…", "Updating…");
    updateRunning = true;
    send({ type: "apply_update" });
  });

  // 「有新版本」条和「迁移旧词表」条：都是可以晚点做的事，不是问题。直播中不摆（中控在看字幕，
  // 不该被它们占一行），回到首页（enterHomeLayout）再露出来；页脚版本号照旧能点来手动检查更新。
  // 唯一的例外是更新已经在进行（服务端回过 updating）：按钮上的「更新中…」是这次更新唯一的进度，
  // 监听又恰恰是更新暂停的，这时收起会让中控以为什么都没发生
  var updateOffered = false;      // 服务端说有新版本（showUpdate 收到过）
  var updateRunning = false;      // 点了「一键更新」、服务端回了 updating，还没结束
  // 点页脚版本号手动检查的那一刻。直播中条不摆，而这是中控自己问的，必须有回应：
  // 这个窗口内到的、条被收起的「有新版本」改在页脚写一句（「已是最新」「检查失败」本来就在页脚）
  var manualCheckAt = 0;
  var MANUAL_CHECK_WINDOW_MS = 12000;   // 服务端请求 GitHub 最多等 8 秒
  var migrateOffered = false;     // 词表里有旧模板遗留的条目

  function syncNoticeBars() {
    updateBar.classList.toggle("hidden", !updateOffered || (streamActive && !updateRunning));
    if (migrateBar) migrateBar.classList.toggle("hidden", !migrateOffered || streamActive);
  }

  function showUpdate(info) {
    if (!info || !info.version) return;
    setIconText(updateText, "arrow-clockwise", L("发现新版本 " + info.version,
                                                 "Version " + info.version + " is available"));
    if (info.can_auto) {
      updateBtn.classList.remove("hidden");
      updateLink.textContent = L("更新说明", "Release Notes");
      updateLink.className = "";
    } else {
      // ZIP 安装无法自动更新——别摆一个点了必失败的按钮，直接给下载入口
      updateBtn.classList.add("hidden");
      updateLink.textContent = L("前往下载新版本", "Download New Version");
      updateLink.className = "btn primary";
    }
    updateLink.href = info.url || "#";
    updateOffered = true;
    syncNoticeBars();       // 直播中先不摆，回到首页时再露出来（见 syncNoticeBars）
    if (!info.quiet && streamActive && !updateRunning && manualCheckAt
        && Date.now() - manualCheckAt < MANUAL_CHECK_WINDOW_MS) {
      manualCheckAt = 0;
      showVersionNote(L("有新版本 " + info.version + "，停止监听后在首页查看",
                        "Version " + info.version + " is available. Check the home screen after you stop monitoring."),
                      8000);
    }
    // 静默模式：短时间内已经提示过了。按钮照常可用，但不再改标题——
    // 标题会闪在任务栏/标签页上，连续几个 patch 的日子那是纯粹的骚扰。
    if (!info.quiet) document.title = L("有新版本 · TikTok 直播同传", "Update Available · TikTok Live Translator");
    updateBar.classList.toggle("quiet", !!info.quiet);
  }

  // 点底部版本号即可手动检查更新
  versionEl.addEventListener("click", function () {
    if (!send({ type: "check_update" })) {
      showVersionNote(L("未连接到本地服务", "Not connected to the local service"), 3000);
      return;
    }
    manualCheckAt = Date.now();
    // 「检查中…」的恢复定时器要能被随后到达的结果提示接管，
    // 否则结果刚显示就被这个定时器抹回版本号
    showVersionNote(L("检查更新中…", "Checking for updates…"), 8000);
  });

  // 「迁移旧词表」：扫描 → 展示 → 用户确认 → 服务端备份并迁移。
  // 只迁移整条与旧官方模板一致的行，用户自己写的内容绝不动。
  function handleMigration(msg) {
    if (msg.stage === "available") {
      migrateText.textContent = LN(msg.count, "glossary.txt 里有 " + msg.count +
        " 条旧模板遗留的主播专属词条，会污染其他主播的直播",
        "glossary.txt has 1 streamer-specific entry left over from the old template. " +
        "It also affects other streamers’ translations.",
        "glossary.txt has " + msg.count + " streamer-specific entries left over from the old template. " +
        "They also affect other streamers’ translations.");
      migrateOffered = true;
      syncNoticeBars();
    } else if (msg.stage === "plan") {
      if (!msg.entries || !msg.entries.length) {
        showVersionNote(L("没有可以安全自动迁移的条目（改动过的条目需手动移到 profiles/）",
                          "Nothing can be moved automatically. Move edited entries to profiles/ by hand."), 8000);
        migrateBtn.disabled = false;
        return;
      }
      var lines = msg.entries.map(function (e) {
        return e.display + "  → profiles/" + e.streamer + ".txt";
      });
      var ok = window.confirm(LN(lines.length,
        "将迁移以下 " + lines.length + " 条（先备份 glossary.txt）：\n\n" +
        lines.join("\n") + "\n\n只迁移与旧官方模板完全一致的条目，" +
        "你自己添加或改过的内容不会被改动。继续？",
        "This entry will be moved (glossary.txt is backed up first):\n\n" +
        lines.join("\n") + "\n\nOnly entries that exactly match the old official template are moved. " +
        "Anything you added or edited stays as it is. Continue?",
        "These " + lines.length + " entries will be moved (glossary.txt is backed up first):\n\n" +
        lines.join("\n") + "\n\nOnly entries that exactly match the old official template are moved. " +
        "Anything you added or edited stays as it is. Continue?"));
      if (ok) {
        send({ type: "migrate_glossary", confirm: true });
      } else {
        migrateBtn.disabled = false;
      }
    } else if (msg.stage === "done") {
      migrateBtn.disabled = false;
      if (msg.result && msg.result.total) {
        migrateOffered = false;
        syncNoticeBars();
        var note = LN(msg.result.total, "已迁移 " + msg.result.total + " 条（备份：" +
                   msg.result.backup + "）",
                   "Moved 1 entry (backup: " + msg.result.backup + ")",
                   "Moved " + msg.result.total + " entries (backup: " + msg.result.backup + ")");
        if (msg.result.failed) {
          // 接在上一句后面：英文另起一句
          note += LN(msg.result.failed, "；另有 " + msg.result.failed +
                  " 条因 profile 写入失败未迁移，仍保留在原词表里",
                  ". 1 entry wasn’t moved because its profile couldn’t be written. It’s still in glossary.txt.",
                  ". " + msg.result.failed + " entries weren’t moved because their profiles couldn’t be " +
                  "written. They’re still in glossary.txt.");
        }
        showVersionNote(note, 10000);
      } else if (msg.result && msg.result.failed) {
        showVersionNote(LN(msg.result.failed, "迁移失败：profiles/ 目录写不进去，" + msg.result.failed +
                        " 条全部保留在原词表（已留备份 " + msg.result.backup + "）",
                        "Couldn’t write to the profiles/ folder, so the entry wasn’t moved. " +
                        "It’s still in glossary.txt (backup: " + msg.result.backup + ").",
                        "Couldn’t write to the profiles/ folder, so no entries were moved. All " +
                        msg.result.failed + " are still in glossary.txt (backup: " + msg.result.backup + ")."),
                        10000);
      } else {
        showVersionNote(L("没有需要迁移的条目", "Nothing to move"), 6000);
      }
    }
  }

  if (migrateBtn) {
    migrateBtn.addEventListener("click", function () {
      migrateBtn.disabled = true;
      send({ type: "migrate_glossary" });
    });
  }

  function showVersionNote(text, ms) {
    if (versionNoticeTimer) clearTimeout(versionNoticeTimer);
    versionEl.textContent = " · " + text;
    versionNoticeTimer = setTimeout(restoreVersion, ms);
  }

  function restoreVersion() {
    if (versionNoticeTimer) clearTimeout(versionNoticeTimer);
    versionNoticeTimer = null;
    if (currentVersion) {
      versionEl.textContent = " · v" + currentVersion + (updateCheckNote ? " · " + updateCheckNote : "");
    }
  }

  // 连续很多天没连上更新服务器：只在页脚版本号旁边安静地提一句，不进自检、不进横幅
  function renderUpdateCheck(info) {
    updateCheckNote = info && info.note ? String(info.note) : "";
    if (!versionNoticeTimer) restoreVersion();   // 正在显示的一次性提示到点后会带上它
  }

  // 滑块一个值带三处：底部大字幕的字号（--sub-size），历史卡片和弹幕栏的比例（--cap-scale）
  function applyFont(size) {
    var vars = captionSizeVars(size);
    Object.keys(vars).forEach(function (name) {
      document.documentElement.style.setProperty(name, vars[name]);
    });
    liveBarChanged();       // 字号变了，大字幕条跟着变高变矮：两栏底部的留白要重新量（见 syncLiveBarHeight）
  }

  // ---- WebSocket ----
  var offlineSince = 0;
  var stickyOfflineDetail = "";   // 看门狗留下的行动指引，重连成功前不许被空 detail 抹掉

  function connect() {
    ws = new WebSocket("ws://" + location.host + "/ws");

    ws.onopen = function () { retries = 0; offlineSince = 0; stickyOfflineDetail = ""; };

    ws.onclose = function () {
      // 断开 20 秒还连不回来，多半是本地程序被关掉了——告诉用户怎么办，
      // 而不是永远「重连中…」
      if (!offlineSince) offlineSince = Date.now();
      var gone = Date.now() - offlineSince > 20000;
      setStatus({
        state: "offline",
        detail: gone
          ? L("本地程序似乎已经关闭——请重新双击打开：macOS 双击「TikTok Live Translator.app」，Windows 双击「Start.bat」。",
              "No connection to the app’s local service for over 20 seconds. If the app has quit, reopen it. " +
              "On Mac, double-click TikTok Live Translator.app. On Windows, double-click Start.bat.")
          : stickyOfflineDetail,
      });
      var delay = Math.min(5000, 700 * Math.pow(2, retries++));
      setTimeout(connect, delay);
    };

    ws.onerror = function () {
      try { ws.close(); } catch (e) { /* noop */ }
    };

    ws.onmessage = function (evt) {
      var msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      handle(msg);
    };
  }

  // 返回是否真的发出去了——连接断开时调用方需要告诉用户，而不是静默失败
  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  function handle(msg) {
    // 回放结束（这是回放之后的第一条实时消息）：还没插进弹幕栏的分隔线补在末尾（F1）
    if (pendingCommentBreaks.length && msg.type !== "hello" && !msg.replay) flushCommentBreaks();
    switch (msg.type) {
      case "hello":
        // 重连成功：上一条连接上可能丢了「开始」指令，在这条新连接上补发
        if (pendingStart && !pendingStart.retried) {
          pendingStart.retried = true;
          send(pendingStart.payload);
          armStartWatchdog();
        }
        // 重连时服务器会重发历史（含警报），先清掉本地已有的，避免重复
        alertList.innerHTML = "";
        alertCount.textContent = "0";
        alertPanel.classList.add("hidden");
        // 当前场次先于回放的报警到：回放进来的每一条都要据此判断是不是上一场的
        setAlertSession(msg.config && msg.config.alerts_session);
        // 弹幕同理：服务器会重放最近的评论，先清掉本地已有的
        commentList.innerHTML = "";
        commentById = Object.create(null);
        commentMeta = Object.create(null);
        pendingCommentBreaks = [];
        resetCommentRedo();         // 断线期间那次重译的结果收不到了：「重译中…」不能一直挂着
        commentFollowing = true;   // 回放重建整个列表：从最新处开始看
        commentsBelow = 0;
        commentPanel.classList.add("hidden");
        // 失败连击也归零——回放的陈旧字幕不该累积成新警告
        failStreak = 0;
        transBannerOn = false;
        clearCaptionsView();
        // 这一场出没出过字幕，交给接下来的回放重算（场次分隔线归零、字幕置真，见 sessionCaptionSeen 的说明）。
        // 回放里带着这一场的分隔线时，这一行和分隔线那次归零是重复的保险；只有回放里没有分隔线时它说了算：
        // 服务端的字幕缓存被清空过（app/server.py clear_history 连分隔线一起清）。这时重连后的页面和刷新后的
        // 一样，只按回放判断，不沿用断线前的（tests/ui-states.test.mjs 钉着）
        sessionCaptionSeen = false;
        if (msg.config) {
          syncCaptionSize(msg.config);
          if (msg.config.comment_backend) backendState = msg.config.comment_backend;
          if (msg.config.comment_detail != null) backendDetail = msg.config.comment_detail;
          // 弹幕留意：状态、事件、关注名单以服务端为准，整份换掉；回放里带来的事件不算新事件
          applyGuardConfig(msg.config, true);
          if (msg.config.watchlist) renderWatchlist(msg.config.watchlist);
          // 违禁词警示开关：hello 每次都带真实值（默认关闭），不猜测、不沿用上一场
          setAlertsEnabled(!!msg.config.alerts_enabled);
          if (msg.config.disk) renderDisk(msg.config.disk);
          if (msg.config.recent_rooms) renderRecentRooms(msg.config.recent_rooms);
          // room_url 必须先回填，applyDefaultBrand() 才能读到这一场真正的主播名——
          // 顺序反了的话，私密窗口/换设备等 roomInput 本来是空的场景会先按空
          // 主播算出「不限」，room_url 填进来后却没有再刷新一遍（踩过的坑）
          if (msg.config.room_url && !roomInput.value) {
            roomInput.value = msg.config.room_url;
            // 同 fillRoomInput：程序自己回填的，不清 touched，只记主播名
            brandState = brandStateSyncStreamer(brandState, streamerFromInput(msg.config.room_url));
          } else if (msg.config.room_url && msg.config.status && isListening(msg.config.status.state)
                     && streamerFromInput(roomInput.value) !== streamerFromInput(msg.config.room_url)) {
            // 断线期间换了主播（在别的窗口换的，或这一页没收到那条 config）：输入框里还是上一位，下面的
            // setStatus 会把胶囊画成「直播中 · @上一位」（F5 的重连那一半）。连接中 / 直播中开始面板是
            // 收着的，输入框里不会有中控正在敲的字，按服务端的回填
            fillRoomInput(msg.config.room_url);
          }
          // brand_options 要先于 brands 处理：下拉框的选项得先建好，
          // applyDefaultBrand() 才有值可选
          if (msg.config.brand_options) renderBrandOptions(msg.config.brand_options);
          if (msg.config.brands) { brandsMap = msg.config.brands; applyDefaultBrand(); }
          // 本场品牌标签：hello 每次都带真实值（默认没有），同 alerts_enabled，
          // 不沿用上一次连接看到的值
          setActiveBrand(msg.config.active_brand || null);
          if (msg.config.selfcheck) renderSelfcheck(msg.config.selfcheck);
          if (msg.config.engine) renderEngine(msg.config.engine);
          if (msg.config.viewer) renderShare(msg.config.viewer);
          applyFloatConfig(msg.config.float_window);
          // 持续提示以服务端为准：重连时整份重放，先清掉本地的
          incidents = Object.create(null);
          if (msg.config.incidents) {
            Object.keys(msg.config.incidents).forEach(function (k) {
              renderIncident(msg.config.incidents[k]);
            });
          }
          drawIncidents();
          if (msg.config.status) setStatus(msg.config.status);
          // 识别落后提示同样以服务端为准（专家审查 F3）：先收起，再按 config.health 画。以前 hello 不碰它，
          // 断线期间那条「已追上」没收到的页面，一直挂着旧的「识别开始落后」到这一场停止。放在 setStatus
          // 之后：renderHealth 只在这一场还在进行时才揭开它
          healthBar.classList.add("hidden");
          if (msg.config.health) renderHealth(msg.config.health);
          if (msg.config.target_lang) showTargetLang(msg.config.target_lang);
          if (msg.config.source_lang && !sourceTouched) selectSourceLang(msg.config.source_lang);
          if (msg.config.glossary_migration) {
            handleMigration({ stage: "available",
                              count: msg.config.glossary_migration.count });
          }
          updateCheckNote = msg.config.update_check && msg.config.update_check.note
            ? String(msg.config.update_check.note) : "";
          if (msg.config.version) {
            currentVersion = msg.config.version;
            restoreVersion();
          }
          // 重连/刷新时回放的提示从来不是「新」提示：不许再改标题打扰直播中的中控
          if (msg.config.update) showUpdate(Object.assign({}, msg.config.update, { quiet: true }));
          else {
            updateOffered = false;
            resetUpdateBtn();
            syncNoticeBars();
          }
          // 界面语言：闸关着时 config 里没有这几个键，行保持隐藏、不比对、不重载
          renderLang(msg.config);
          reloadForLang(msg.config);
          renderTranscripts(msg.config.transcripts);
        }
        break;
      case "update_available":
        showUpdate(msg);
        break;
      case "updating":
        updateBtn.disabled = true;
        updateBtn.textContent = L("更新中…", "Updating…");
        updateRunning = true;
        syncNoticeBars();       // 直播中收起着的条，更新一开始就要露出来
        break;
      // 这次没更新成（原因另有提示/状态说明）：按钮恢复，可以再试
      case "update_aborted":
        resetUpdateBtn();
        syncNoticeBars();       // 直播中没更新成：监听没断，条跟着收起
        break;
      case "status":
        // connecting/live/error 任一状态到达即视为服务器已接管「开始」指令
        if (pendingStart && (msg.state === "connecting" || msg.state === "live"
                             || msg.state === "error")) {
          pendingStart = null;
          if (startWatchdog) clearTimeout(startWatchdog);
        }
        // homeActive === false：明确知道刚才是待机（null 是页面刚加载还不知道，offline 不改它）
        if (homeActive === false && (msg.state === "connecting" || msg.state === "live")) guardNewSession();
        setStatus(msg);
        break;
      // 一次性提示（如手动检查更新的结果）：只改版本号处的文案，
      // 不碰状态机——直播中收到它不该影响「停止」按钮等 UI
      case "notice":
        if (msg.text) showVersionNote(msg.text, 6000);
        // 字幕记录行的按钮（导出、打开文件夹）：同一句话也写在按钮下面，页脚常常不在视野里
        if (msg.text && msg.where === "transcripts") showTranscriptStatus(msg.text);
        break;
      // 点「重译」没有开始或没成功：话显示在被点的那条字幕旁边（不是页脚那一行）
      case "retranslate_note":
        showRedoNote(msg.id, msg.text);
        break;
      case "config":
        // 另一个窗口调了字幕字号（或本页松手后服务端的回执）：本页没动过滑块就跟上
        if (msg.caption_size != null) adoptCaptionSize(msg.caption_size);
        if (msg.target_lang) showTargetLang(msg.target_lang);
        if (msg.source_lang && !sourceTouched) selectSourceLang(msg.source_lang);
        // 同一条 config 广播里 room_url 和 brands 一起到达时，room_url 要先填
        // 进输入框——换了主播的第二个页面靠它才能刷新出正确的默认品牌，
        // 顺序反了就会用上一个主播的房间算出错的默认值（踩过的坑，同 hello 分支）。
        // brand_options 同理要先于 brands：refresh_brands 的回执也走这条分支，
        // 选项要先建好，applyDefaultBrand() 才有值可选
        if (msg.room_url) {
          fillRoomInput(msg.room_url);
          // 换主播（专家审查 F5）：这条 config 是停完上一位之后才到的，新一场的第一条 status 还要再等开场
          // 那几步。胶囊和「正在连接 @…」的主播名都取自输入框，以前要等那条 status 才改过来，这几秒里
          // 胶囊写着「连接中… · @上一位」，横幅却已经是「正在切换到 @新的」。现在输入框一换就按最近
          // 一条 status 的状态重画这两处；横幅的字是服务端写的，不动
          if (isListening(statusState)) {
            paintStatusText(statusState);
            syncConnectHint(statusState);
          }
        }
        if (msg.brand_options) renderBrandOptions(msg.brand_options);
        if (msg.brands) { brandsMap = msg.brands; applyDefaultBrand(); }
        // start_stream 开播时、真正停止后都会广播这个字段（后者是显式 null）；
        // 用 "in" 而不是真值判断，是因为 set_target 等别的 config 广播不带这个
        // 键，不该被当成「清空品牌」处理
        if ("active_brand" in msg) setActiveBrand(msg.active_brand);
        if (msg.alerts_session) setAlertSession(msg.alerts_session);
        // 弹幕留意：开场时事件表清空、名单换成这个主播的，状态变化时只带 guard_state。不带这些键的广播不动它
        if ("guard_state" in msg || "guard_events" in msg || "guard_marked" in msg) applyGuardConfig(msg, false);
        if ("update_check" in msg) renderUpdateCheck(msg.update_check);
        // 设置里换了界面语言（app/pipeline.py _set_ui_lang 广播语言那几个键）：刷新这一行，
        // 本页语言和新的不一样就重载。别的 config 广播（set_target 等）不带这些键，不动这一行
        if ("ui_lang_available" in msg) {
          renderLang(msg);
          reloadForLang(msg);
        }
        // 新的一场开始时后端推一次已经存了几场（字幕记录行的摘要）
        if ("transcripts" in msg) renderTranscripts(msg.transcripts);
        // 浮窗开了 / 关了（含监听结束时服务端替我们关掉）：按钮跟着变
        if ("float_window" in msg) applyFloatConfig(msg.float_window);
        break;
      case "glossary_migration":
        handleMigration(msg);
        break;
      case "caption":
        renderCaption(msg);
        break;
      case "caption_update":
        updateCaption(msg);
        break;
      case "session_break":
        // 新的一场开始：上一场的识别落后提示作废（专家审查 F3）。换主播走 quiet 停止、不发 idle，新一场的
        // 统计循环又从 ok 数起、等级不变就不广播，不在这里收起，它会一直挂到下一次等级变化。
        // 回放里的分隔线是历史，不碰：此刻的状态由 hello 的 config.health 定
        if (!msg.replay) healthBar.classList.add("hidden");
        renderSessionBreak(msg);
        break;
      // 服务端清掉了一类缓存（这一页或别的本机页面点了「清空」）：这一页也跟着清。
      // 这一页自己点的那次会收到回声，再清一遍是空操作
      case "history_cleared":
        if (msg.kind === "captions") clearCaptionsView();
        else if (msg.kind === "comments") clearCommentsView();
        break;
      case "alert_update":
        updateAlert(msg);
        break;
      case "alert":
        renderAlert(msg);
        break;
      case "comment":
        renderComment(msg);
        break;
      case "comment_update":
        updateComment(msg);
        break;
      // 点弹幕的「重译」：进行中 / 成了 / 没成的状态，和显示在那条弹幕下面的说明
      case "comment_redo":
        receiveCommentRedo(msg);
        break;
      // 弹幕留意：复核结果 / 刷屏升级后补的标注、聚合后的事件、关注名单的增删（都只发本机页面）
      case "comment_guard":
        updateCommentGuard(msg);
        break;
      case "guard_event":
        receiveGuardEvent(msg);
        break;
      case "guard_mark":
        receiveGuardMark(msg);
        break;
      case "comment_source":
        if (msg.backend) {
          backendState = msg.backend;
          backendDetail = msg.detail || "";
        }
        refreshCommentPanel();
        break;
      // stats（每 10 秒一次的延迟统计）页面不再显示：2026-10-15 用户嫌底部那行数字是噪音，删掉了。
      // 积压期间的落后和丢段由 health 提示条说（积压 ≥10 秒才出现，追上就收起）；识别连续出错
      // ASR_FAILURES_BEFORE_RECOVERY 次走 incident。单次识别出错、识别超时次数、积压追上之后的
      // 丢段总数，界面上不再有痕迹，只在审计（asr_failed / asr_overrun / audio_dropped 三种记录）和终端里
      case "incident":
        renderIncident(msg);
        break;
      // stats 本身不画（见上面）；只取跨轮覆盖率的数字，偏低时在底部大字幕元信息行挂一个角标
      case "stats":
        paintLiveCoverage(msg);
        break;
      case "health":
        renderHealth(msg);
        break;
      case "watchlist":
        renderWatchlist(msg);
        break;
      // 违禁词警示开关的状态广播：会话开始时发一次，中途改变时再发一次
      case "alert_mode":
        setAlertsEnabled(!!msg.on);
        break;
      case "recent_rooms":
        renderRecentRooms(msg.entries);
        break;
      case "disk":
        renderDisk(msg);
        break;
      case "engine":
        renderEngine(msg);
        break;
      case "selfcheck":
        renderSelfcheck(msg);
        break;
      case "viewer":
        renderShare(msg);
        break;
    }
  }

  // ---- 渲染 ----
  // 连接中 / 直播中：有一位「正在听的主播」
  function isListening(state) {
    return state === "connecting" || state === "live";
  }

  // 连接中/直播中额外带上正在听谁：「连接中… · @A」「直播中 · @A」。主播名和
  // 顶栏「换主播」面板认的是同一个来源（config.room_url，经 streamerFromInput
  // 提取），取不到（房间链接还没回填、或本来就是纯直连地址没有主播身份）就
  // 不带这半句。连接中也要带：点错了要等连上才看得出来，而失败的连接中位
  // 要等约 28 秒，这段时间里主播名是唯一能核对「点没点对」的线索（pm.md #5）。
  // 单独成函数：换主播时 config 先于新一场的 status 到，只重画这几个字（F5）
  function paintStatusText(state) {
    var label = STATUS_TEXT[state] || state;
    var liveStreamer = isListening(state) ? streamerFromInput(roomInput.value) : "";
    statusText.textContent = liveStreamer ? label + " · " : label;
    if (liveStreamer) statusText.appendChild(nameSpan("@" + liveStreamer));   // 主播名
  }

  function setStatus(msg) {
    var state = msg.state || "idle";
    statusState = state;
    // 只记最近一条的：下载完、开始识别后的那条 status 不带它，卡片就回到「正在连接」/ 收起
    statusProgress = msg.progress && typeof msg.progress === "object" ? msg.progress : null;
    statusDot.className = "dot " + state;
    if (statusPill) statusPill.dataset.state = state;
    paintStatusText(state);

    // 更新失败/被拒绝后恢复「一键更新」按钮，允许再试
    if (state === "error" || state === "idle") resetUpdateBtn();

    // offline（本地连接断线重连中）不代表直播本身发生了变化：布局（开始面板/
    // 停止按钮/弹幕面板/大字幕的显示状态）原样不动，viewTransition 对这个状态
    // 直接把 active 原样传回来，下面几行也就什么都不会改——只有状态点和状态
    // 文字（上面已经写完）该刷新。重连后 hello 带回真实状态会再算一次。
    var transition = viewTransition(homeActive, state);
    homeActive = transition.active;
    var active = transition.active;
    startPanel.classList.toggle("hidden", active);
    // 首页（开始面板可见）用分组灰底，直播中回到白底读字幕（style.css body.home）
    document.body.classList.toggle("home", !active);
    stopBtn.classList.toggle("hidden", !active);
    // 换主播按钮和停止按钮同一处切换：只在直播中/连接中有意义，待机/已结束/
    // 出错/离线时没有「正在监听的主播」可换
    if (switchBtn) switchBtn.classList.toggle("hidden", !active);
    startBtn.disabled = state === "connecting";
    streamActive = active;
    updateFloatBtn();       // 浮窗按钮跟换主播同一条显示规则
    updateAlertModeTag();   // 标签只在连接中/直播中露出，别的状态下退回隐藏
    updateActiveBrandTag(); // 本场品牌标签同一个显示条件，见该函数注释
    syncCaptionDependentUi(); // 「上一场字幕」标题只在首页才显示，随 streamActive 变化
    syncConnectHint(state);
    refreshCommentPanel();
    syncGuardLive();    // 顶部条 / 抽屉 / 状态胶囊只在直播中出现
    // 「停止」之后桌面页面留残留（大字幕压住开始面板、回到最新按钮悬空、
    // 上一场弹幕还挂着）：这两步把「直播中才有意义」的 UI 收掉/摆好，
    // 只在活跃↔非活跃真正切换的那一刻各触发一次
    if (transition.enterHome) enterHomeLayout();
    if (transition.exitHome) exitHomeLayout();

    // 附带的命令：程序自己已经帮不上忙时，至少让用户有一条能照做的路
    if (fixCmd) {
      if (msg.command) {
        fixCmdText.textContent = msg.command;
        fixCmd.classList.remove("hidden");
      } else {
        fixCmd.classList.add("hidden");
      }
    }

    var detail = msg.detail || "";
    if (detail) {
      statusBanner.textContent = detail;
      statusBanner.classList.remove("hidden");
      statusBanner.classList.toggle("info", state !== "error");
    } else {
      statusBanner.classList.add("hidden");
    }
  }

  // 连接中：字幕区中央的转圈 + 「正在连接 @xxx…」；首次下载识别模型时写下载进度；直播中这一场还没出过
  // 字幕时写「等待第一句字幕…」（显示规则见 web/live-ui.js connectHint）。已有字幕时挪到 #history 末尾
  // 当一行，并跟到底部——停止后再开、换主播时历史还在，中央那一版会盖在旧字幕上
  function syncConnectHint(state) {
    if (!connectHintEl) return;
    var hint = connectHint(state, streamerFromInput(roomInput.value),
                           !!historyEl.querySelector(".cap"), statusProgress, !sessionCaptionSeen);
    if (!hint) return;
    connectHintEl.classList.toggle("hidden", !hint.show);
    connectHintEl.classList.toggle("inline", hint.inline);
    if (!hint.show) return;
    connectHintText.textContent = hint.text;
    if (connectHintEl !== historyEl.lastElementChild) historyEl.appendChild(connectHintEl);
    if (hint.inline) stickToBottom(false);
  }

  // 翻译连续失败时给一条全局解释——每条字幕角落的小标签太容易被忽略，
  // 用户面对满屏外文会以为整个程序坏了
  var failStreak = 0;
  var transBannerOn = false;

  function trackTranslateHealth(msg) {
    if (msg.translate_state === "failed") {
      // 拒答（refused）是接口正常回了话、只是不译这一句：不算进连续失败，否则主播反复说
      // 同类宣称时，几条拒答就会顶出下面那句「翻译失败」横幅。也不清零——它不说明翻译通了
      // 回来的不是译文（not_translation，模型在回话）同理：接口是通的
      if (msg.refused || msg.not_translation) return;
      failStreak++;
      // >= 且每条都重刷：状态横幅是共用的，中途被别的 status 覆盖后，
      // 只要翻译还在持续失败，警告就要顶回来
      if (failStreak >= 4) {
        transBannerOn = true;
        setIconText(statusBanner, "warn",
          L("连续多条字幕翻译失败——翻译服务可能暂时连不上，字幕先显示原文（语音识别不受影响）。",
            "The last several captions couldn’t be translated, so captions show the original text. " +
            "Speech recognition isn’t affected. If this continues, choose another engine in " +
            "Settings > Translation Engine."));
        statusBanner.classList.remove("hidden");
        statusBanner.classList.remove("info");
      }
    } else if (msg.translate_state === "ok") {
      failStreak = 0;
      if (transBannerOn) {
        transBannerOn = false;
        statusBanner.classList.add("hidden");
      }
    }
  }

  function renderCaption(msg) {
    if (!msg.replay) trackTranslateHealth(msg);

    var card = document.createElement("div");
    card.className = "cap";
    card.dataset.id = msg.id;

    var meta = document.createElement("div");
    meta.className = "meta";
    var ts = new Date((msg.ts || Date.now() / 1000) * 1000);
    var timeSpan = document.createElement("span");
    timeSpan.textContent = pad(ts.getHours()) + ":" + pad(ts.getMinutes()) + ":" + pad(ts.getSeconds());
    meta.appendChild(timeSpan);
    if (msg.src_lang) {
      var chip = document.createElement("span");
      chip.className = "lang-chip";
      chip.textContent = msg.src_lang;
      meta.appendChild(chip);
    }
    var stateChip = document.createElement("span");
    stateChip.className = "lang-chip state-chip";
    meta.appendChild(stateChip);
    // 「语音识别不确定」角标：服务端只在开关打开、这一段命中识别风险规则时才带 asr_flag（web/live-ui.js
    // captionAsrChip）。只在 caption 消息里有，caption_update 不带，所以只在建卡片时画一次；
    // 重连回放的 caption 里还留着这个键，角标跟着回来
    var asrChip = captionAsrChip(msg);
    if (asrChip.text) {
      var asrEl = document.createElement("span");
      asrEl.className = "lang-chip asr-chip";
      setIconText(asrEl, "warn", asrChip.text);
      asrEl.title = asrChip.title;
      meta.appendChild(asrEl);
    }
    card.appendChild(meta);

    // 原文永远先显示（不等翻译）；译文回来前译文行留空
    var orig = document.createElement("div");
    orig.className = "orig";
    markBody(orig, true);
    orig.textContent = msg.original || "";
    card.appendChild(orig);

    var trans = document.createElement("div");
    trans.className = "trans";
    card.appendChild(trans);

    // 「重译」：用最强的模型重来一次（字幕引擎是 Claude 时是 Claude Sonnet 5.5，否则是本机最强的
    // 本地模型）。按条触发而不是开一个时段，
    // 是因为值得动用强模型的是具体某句话，而那只有看着的人知道是哪一句。
    var redo = document.createElement("button");
    redo.className = "redo";
    redo.type = "button";
    // 「更强」不写成「最准」：Claude 引擎下的 Sonnet 5.5 档位没有测过准确率，测出来之前不说它更准
    // （docs/engine-benchmarks.zh-CN.md：只能说读起来更顺，不能说更准）
    redo.title = L("用更强的模型重新翻译这一条", "Retranslate with the stronger model");
    redo.textContent = L("重译", "Retranslate");
    redo.addEventListener("click", function () {
      send({ type: "retranslate", id: msg.id });
    });
    card.appendChild(redo);

    historyEl.appendChild(card);
    cardsById[msg.id] = card;
    syncCaptionDependentUi();

    var caps = historyEl.querySelectorAll(".cap");
    while (caps.length > maxHistory) {
      delete cardsById[caps[0].dataset.id];
      caps[0].remove();
      caps = historyEl.querySelectorAll(".cap");
    }

    applyTranslation(card, msg);
    // 翻上去看的时候，新字幕不会把视野拉走，但按钮上要数出下面攒了多少没看的（回放的不算）。
    // 在 stickToBottom 之前数：贴底跟随时它会把计数归零
    if (!msg.replay && streamActive && !following) captionsBelow++;
    // 这一场出过字幕了：「等待第一句字幕…」收起（专家审查 U8）。中间那张卡片还露着的时候（连接中）
    // 重新摆一次：卡片是追加在 #history 里的，新字幕又追加在它后面——页面重连时 hello 的 status 先到、
    // 回放的字幕后到，「正在连接」被顶到回放字幕的上面、滚出视野。重摆会把它挪回末尾、改成一行并跟到底部。
    // 卡片收着时不用管：它下次露出来时 syncConnectHint 自己会挪到末尾
    sessionCaptionSeen = true;
    if (connectHintEl && !connectHintEl.classList.contains("hidden")) syncConnectHint(statusState);
    // force 只在「重连时回放、且当前确实处于直播中」才为 true：那是唯一
    // 该无视用户滚动状态、强制跳到最新的场景。待机时收到的回放（刚停止/刚
    // 打开就是上一场留下的历史）不该跟着强制滚到底——否则 enterHomeLayout
    // 刚把 #history 归位到顶部，这里又把它拽回底部，开始面板重新被盖住
    // （这正是「长场次停止后看不到输入框」那个既有问题的成因）。
    stickToBottom(msg.replay && streamActive);

    // 底部大字幕：回放历史时不逐条更新（避免闪一串旧字幕），最后一条带
    // restore 标记，用它把大字幕恢复成断线前的样子——但只在当前确实处于
    // 直播中才恢复；待机时回放只用来恢复上面的历史卡片，大字幕留隐藏，
    // 不然停止后大字幕会跟着回放重新冒出来，压住开始面板下半部。
    // 实时到达、还在翻译的一句先不上（待译停留，见 holdLiveBar）；回放里带 restore 的那一句照旧直接画——
    // 那是断线前已经在屏幕上的样子，再等一次反而让大字幕在重连后空着
    if ((!msg.replay || msg.restore) && streamActive) {
      if (!msg.replay && liveHoldWanted(msg)) holdLiveBar(msg);
      else showOnLiveBar(msg, Date.now());
    }
  }

  // ---- 底部大字幕：换上一句 / 待译停留 ----
  // 一句话上大字幕只走 showOnLiveBar：大字放译文（没有译文时放原文），小字放原文（没有译文时收起）。
  // receivedAt 是这一句到达页面的时刻：等过译文的那一句按到达时算「约 N 秒前」，不按换上去的时刻
  function showOnLiveBar(msg, receivedAt) {
    cancelLiveHold();
    liveBar.classList.remove("hidden");
    liveTranslated.textContent = msg.translated || msg.original || "";
    liveOriginal.textContent = msg.translated ? (msg.original || "") : "";
    liveOriginal.classList.toggle("hidden", !msg.translated);
    liveBarId = msg.id;
    paintLiveState(msg);
    paintLiveAsr(msg);
    liveAudioEndAt = captionAudioEndAt(msg, receivedAt);
    paintLiveLag();
  }

  // 只有「明确在翻译中、还没有译文」的一句才等。skipped（同语言、不用翻译）、一到就带着结局的、
  // 老版本服务端不带 translate_state 的，都直接上
  function liveHoldWanted(msg) {
    return msg.translate_state === "pending" && !msg.translated;
  }

  // 新的一句在翻译：大字幕停在上一句（第一句之前就还空着）。等待中又来一句就改等新的那句——
  // 前一句从头到尾不上大字幕（约 0.7% 的句子，译文比下一句原文还晚到），它在上面的字幕记录里照常有。
  // 等满 LIVE_HOLD_MS 还没结局：照以前的样子先放原文，译文到了再由 updateCaption 就地换成译文。
  // 改等新句时截止时刻沿用这一轮最早那次（until），不重新数 4 秒：句子一直比译文来得快时（识别积压后
  // 一串句子集中到达，间隔不到 4 秒、译文又都慢于间隔），每来一句就重数的话大字幕会一直停在旧句。
  // 这样大字幕停在旧句最多 LIVE_HOLD_MS，到点时放的是还在等的那一句（最新的）
  function holdLiveBar(msg) {
    var now = Date.now();
    var until = liveHold ? liveHold.until : now + LIVE_HOLD_MS;
    cancelLiveHold();
    var hold = { msg: msg, at: now, until: until, timer: null };
    hold.timer = setTimeout(function () {
      if (liveHold !== hold) return;
      liveHold = null;
      if (streamActive) showOnLiveBar(hold.msg, hold.at);
    }, Math.min(LIVE_HOLD_MS, Math.max(0, until - now)));
    liveHold = hold;
  }

  function cancelLiveHold() {
    if (!liveHold) return;
    clearTimeout(liveHold.timer);
    liveHold = null;
  }

  // ---- 底部大字幕下面的一小行：翻译状态角标 + 「约 N 秒前」 ----
  // 大字幕没译成（译失败、被拒答、结果不像译文、因积压被跳过）时显示的是原文。
  // 以前这时什么说明都没有，大字号的西语看着像程序没在翻译；现在和历史卡片同款的角标写在下面
  // （字从 web/live-ui.js 的同一个函数来），原文照旧留在大字幕上。还在等译文的不画角标（见 liveBarChip）。
  // 「约 N 秒前」：这一句的声音结束到现在，数字每秒涨一次，大字幕上的这一句停多久它就老多久。
  // 只在超过 CAPTION_LAG_SHOW_SEC 秒（web/live-ui.js）才出现：健康的直播里一句话换下一句的间隔不到那么久，
  // 它一出现就说明这一句比平常停得久（主播停了，或者积压了）；这里只写数字，不猜是哪一种。
  var liveAudioEndAt = null;   // 大字幕这一句音频结束的时刻（毫秒，本机时钟）；不知道是 null
  liveLag.title = L("从这一句的声音结束算起，超过 " + CAPTION_LAG_SHOW_SEC + " 秒才显示",
                    "Counted from when the audio for this line ended. Shown after " + CAPTION_LAG_SHOW_SEC + " sec");

  function syncLiveMeta() {
    var any = !liveState.classList.contains("hidden") || !liveLag.classList.contains("hidden")
      || (liveAsr && !liveAsr.classList.contains("hidden"))
      || (liveCov && !liveCov.classList.contains("hidden"));
    liveMeta.classList.toggle("hidden", !any);
    liveBarChanged();       // 大字幕的每一处改动最后都走到这里：条高可能变了
  }

  // 覆盖率角标跟着 stats 走（每 10 秒），不跟着某一句字幕：近几分钟收到的直播音频偏低时才出现
  var liveCov = document.getElementById("live-cov");
  function paintLiveCoverage(stats) {
    if (!liveCov) return;
    var chip = coverageChip(stats);
    if (chip.text) {
      setIconText(liveCov, "warn", chip.text);
      liveCov.title = chip.title;
    } else {
      liveCov.textContent = "";
      liveCov.removeAttribute("title");
    }
    liveCov.classList.toggle("hidden", !chip.text);
    syncLiveMeta();
  }

  // 「语音识别不确定」角标跟着大字幕上的这一句走：换句时重画，caption_update 不动它（那类消息不带 asr_flag）
  function paintLiveAsr(msg) {
    if (!liveAsr) return;
    var chip = captionAsrChip(msg);
    if (chip.text) {
      setIconText(liveAsr, "warn", chip.text);
      liveAsr.title = chip.title;
    } else {
      liveAsr.textContent = "";
      liveAsr.removeAttribute("title");
    }
    liveAsr.classList.toggle("hidden", !chip.text);
    syncLiveMeta();
  }

  function paintLiveState(msg) {
    var chip = liveBarChip(msg);
    liveState.textContent = chip.text;
    liveState.classList.toggle("hidden", !chip.text);
    liveState.classList.toggle("fail", chip.fail);
    syncLiveMeta();
  }

  function paintLiveLag() {
    var text = liveBarId == null || liveAudioEndAt == null ? "" : captionAgeText(Date.now() - liveAudioEndAt);
    liveLag.textContent = text;
    liveLag.classList.toggle("hidden", !text);
    syncLiveMeta();
  }
  setInterval(function () {
    if (liveBarId != null && !document.hidden) paintLiveLag();
  }, 1000);

  // 收起大字幕：场次分隔、清空字幕、回到首页都走这里，角标和滞后数一起清掉，不留给下一句。
  // 在等译文的那一句也一起忘掉：不然计时器到点会把收起的大字幕重新放出来
  function hideLiveBar() {
    cancelLiveHold();
    liveBar.classList.add("hidden");
    liveBarId = null;
    liveAudioEndAt = null;
    liveState.textContent = "";
    liveState.classList.add("hidden");
    if (liveAsr) {
      liveAsr.textContent = "";
      liveAsr.removeAttribute("title");
      liveAsr.classList.add("hidden");
    }
    liveLag.textContent = "";
    liveLag.classList.add("hidden");
    if (liveCov) {
      liveCov.textContent = "";
      liveCov.removeAttribute("title");
      liveCov.classList.add("hidden");
    }
    liveMeta.classList.add("hidden");
    forgetLiveBarHeight();
  }

  // ---- 大字幕条的实测高度 → CSS 变量 --live-bar-h ----
  // 大字幕条是 fixed 的，盖在字幕区和弹幕栏的下沿上，两栏底部要留出它那么高的空。以前写死 204px：
  // 字号调到 48、60 或者句子长时条子比这高，弹幕栏最新一条被整条盖住（专家审查 U1）。现在量条子的真实
  // 高度写进变量，.history / .comment-list 的底部留白按它算（web/style.css）。
  // 防布局循环：值没变就不写（ResizeObserver 回调里写样式，值不变也写会白白再触发一轮）；变量只影响两栏的
  // 内边距，不影响条子自己的高度，所以写一次就停。条子收起时量出来是 0，不写（见 forgetLiveBarHeight）。
  var liveBarH = 0;
  function syncLiveBarHeight() {
    if (liveBar.classList.contains("hidden")) return;
    var h = Math.ceil(liveBar.getBoundingClientRect().height);
    if (!(h > 0) || h === liveBarH) return;
    liveBarH = h;
    document.documentElement.style.setProperty("--live-bar-h", h + "px");
    // 留白变了：正贴底看的人要再贴一次，不然条子新长高的那一截正好盖住最新一条
    if (following) scrollToBottomNow();
    if (commentFollowing) commentsToBottomNow();
  }
  // 条子收起（清空字幕、换场、回首页）：变量清掉，两栏的留白回到没量过时的样子（合起来 204px，同改之前）。
  // 不留上一次的值：上一场最后一句若是超长句（字号 60 时条子可达 625px），回到首页后两栏底下会多出一大段
  // 能滚的空白，要等下一场第一句出来才纠正。liveBarH 归零，条子下次出来时一定重新写
  function forgetLiveBarHeight() {
    if (!liveBarH) return;
    liveBarH = 0;
    document.documentElement.style.setProperty("--live-bar-h", "");   // 空值＝去掉这条声明（CSSOM 规定）
  }
  var liveBarObserver = null;
  if (typeof ResizeObserver === "function") {
    liveBarObserver = new ResizeObserver(function () { syncLiveBarHeight(); });
    liveBarObserver.observe(liveBar);
  }
  // 条子的内容、字号、窗口宽度变了。有 ResizeObserver 时它自己会报，这里什么都不做；没有它的环境
  // （老版本 WebKit、node 测试里的假页面）在这些地方直接量一次
  function liveBarChanged() {
    if (!liveBarObserver) syncLiveBarHeight();
  }
  window.addEventListener("resize", liveBarChanged);

  // 点「重译」没有开始或没成功时，服务端发 retranslate_note：显示在被点的那条字幕旁边几秒。
  // 以前这些话走 notice，落在页脚版本号那一行（6 秒、小字），中控点完像是什么都没发生。
  // 文字由服务端给（中英两句已按界面语言换好，只写观察到的事实和能做的事，不猜原因）。
  // 卡片已经不在页面上（清空了字幕、被挤出 300 条）时才退回页脚那一行：话不能丢。
  var REDO_NOTE_MS = 7000;
  var redoNoteTimers = {};

  function clearRedoNote(card) {
    var id = card.dataset.id;
    if (redoNoteTimers[id]) {
      clearTimeout(redoNoteTimers[id]);
      delete redoNoteTimers[id];
    }
    var old = card.querySelector(".redo-note");
    if (old) old.remove();
  }

  function showRedoNote(id, text) {
    if (!text) return;
    var card = cardsById[id];
    if (!card) {
      showVersionNote(text, 6000);
      return;
    }
    clearRedoNote(card);
    var note = document.createElement("div");
    note.className = "redo-note";
    note.setAttribute("role", "status");
    note.textContent = text;
    card.appendChild(note);
    redoNoteTimers[id] = setTimeout(function () { clearRedoNote(card); }, REDO_NOTE_MS);
    // 译文行下面多了一行：正在贴底看的人得跟着再贴一次
    stickToBottom(false);
  }

  // 译文后补：原地更新那张卡片，不新增一条
  function updateCaption(msg) {
    var card = cardsById[msg.id];
    if (card) applyTranslation(card, msg);
    if (liveHold && liveHold.msg.id === msg.id) {
      // 在等的那一句有了结局：译文到了，或者确定不会有译文了（failed / dropped / skipped）——
      // 整句一起换上；没译成的照旧原文大字 + 角标。只带 strong_state 的重译进度消息不是结局，接着等
      if (msg.translated || (msg.translate_state && msg.translate_state !== "pending")) {
        showOnLiveBar(Object.assign({}, liveHold.msg, msg), liveHold.at);
      }
      trackTranslateHealth(msg);
      return;
    }
    if (liveBarId === msg.id && msg.translated) {
      // 小字永远是西语原文（从卡片取）。以前拿当前大字顶上去：第二次译文
      // （重译）到来时大字已经是中文，顶栏就变成两行中文，原文不见了
      var origEl = card ? card.querySelector(".orig") : null;
      liveOriginal.textContent = origEl ? origEl.textContent : liveOriginal.textContent;
      liveOriginal.classList.remove("hidden");
      liveTranslated.textContent = msg.translated;
    }
    // 角标跟着这一句的翻译状态走。只带 strong_state 的重译进度消息不动它——和 applyTranslation
    // 对卡片的处理一致（那类消息不带 translate_state，没有话可说）
    if (liveBarId === msg.id && (msg.translated || msg.translate_state)) paintLiveState(msg);
    trackTranslateHealth(msg);
  }

  // 换主播（或程序重启后的每一场）广播的场次分隔：约定见前后端约定文档——
  // 每场都发，前端只在页面里已经有字幕卡片时才画分隔线，所以程序启动后的
  // 第一场不会多出一条线（那时 #history 里还没有任何 .cap）。回放（重连）时
  // 同样调用这个函数，走的是和实时一样的路径，不需要额外的「回放」分支：
  // hello 处理会先用 clearCaptionsView() 清场（只清本页，不告诉服务端），之后回放按原始顺序重新触发
  // caption/session_break，历史上的场次边界就这样被原样重建一遍。
  function renderSessionBreak(msg) {
    // 新的一场还没出过字幕（专家审查 U8）。放在下面「没有字幕就不画线」的提前返回之前：
    // 直播中点了「清空字幕」再开下一场（停止再开、换主播），页面上一条 .cap 也没有、这里提前返回，
    // 而上一场留下的「出过」还是真；不在这里归零，新一场就不写「等待第一句字幕…」（tests/ui-states.test.mjs 钉着）
    sessionCaptionSeen = false;
    var caps = historyEl.querySelectorAll(".cap");
    // 要不要画、画什么字是纯逻辑，抽到 web/session-divider.js 里单独测
    // （见该文件顶部注释）：app.js 这个大 IIFE 顶层就摸 DOM，整份没法被
    // node:test require
    if (!shouldRenderSessionDivider(caps.length)) return;
    for (var i = 0; i < caps.length; i++) caps[i].classList.add("prev-session");
    insertSessionDivider(historyEl, msg);
    // 底部大字幕清空，等新一场第一条字幕自己把它揭开（同 enterHomeLayout
    // 的道理：不主动显示，交给下一条真实字幕）
    hideLiveBar();
    // 弹幕栏：实时的分隔线就插在末尾（这一刻之前的弹幕都是上一场的）。回放的先记下，等弹幕回放按 ts
    // 插到对的位置（见 pendingCommentBreaks 的说明，专家审查 F1）
    if (msg.replay) pendingCommentBreaks.push(msg);
    else {
      insertSessionDivider(commentList, msg);
      refreshCommentPanel();    // 分隔线之后这一场还没有弹幕：空态那句露出来（U8）
    }
    // 状态取 setStatus 记下的 statusState（和胶囊的 data-state 是同一个值，config 那一支也用它），
    // 不依赖胶囊元素在不在
    syncConnectHint(statusState);
    stickToBottom(false);
  }

  function insertSessionDivider(container, msg) {
    var sep = document.createElement("div");
    sep.className = "session-sep";
    sep.textContent = sessionDividerText(msg);
    container.appendChild(sep);
  }

  // 把记下的回放分隔线插进弹幕栏末尾。beforeTs 给了就只插 ts 不晚于它的（下一条弹幕要追加在它们后面）；
  // 没给就全插（回放结束）。分隔线按发生顺序记的，遇到第一条还不到时候的就停：ts 不是数的那条也停在
  // 那里，等回放结束补在末尾——不知道它在哪就不猜
  function flushCommentBreaks(beforeTs) {
    var all = beforeTs === undefined;
    var added = false;
    while (pendingCommentBreaks.length) {
      var ts = pendingCommentBreaks[0].ts;
      if (!all && !(typeof ts === "number" && ts <= beforeTs)) break;
      insertSessionDivider(commentList, pendingCommentBreaks.shift());
      added = true;
    }
    // 回放结束补在末尾的那条：跟随中就让它露出来（回放的弹幕已经贴过底，这一条是后加的）
    if (all && added && commentFollowing) commentsToBottomNow();
    // 补在末尾的分隔线之后没有弹幕：这一场还是空的，空态那句要露出来（U8）
    if (added) refreshCommentPanel();
  }

  function applyTranslation(card, msg) {
    // 重译有自己的状态位，绝不能走普通翻译那条 pending 分支——
    // 那会把正在阅读的译文换成「翻译中…」，而强模型若返回空（约 2% 会），
    // 服务端不会再广播，页面就永远停在那里，好译文也没了。
    if (msg.strong_state) {
      var redoBtn = card.querySelector(".redo");
      // 又点了一次、或者这次成了：上一次的说明不再适用
      if (msg.strong_state === "pending" || msg.strong_state === "ok") clearRedoNote(card);
      card.classList.toggle("redoing", msg.strong_state === "pending");
      if (redoBtn) {
        redoBtn.disabled = msg.strong_state === "pending";
        redoBtn.textContent = msg.strong_state === "pending" ? L("重译中…", "Retranslating…")
          : (msg.strong_state === "failed" ? L("重译失败", "Couldn’t retranslate") : L("重译", "Retranslate"));
        if (msg.strong_state === "failed") {
          setTimeout(function () { redoBtn.textContent = L("重译", "Retranslate"); }, 4000);
        }
      }
      // 只带状态位、没有译文和等级的消息到此为止，不动屏幕上的内容
      if (!msg.translated && !msg.quality) return;
    }

    // 二次把关：服务端已经不会广播低等级结果，这里再挡一次，
    // 顺便让「强模型重译」的标记跟着**实际在屏幕上的那一版**走。
    // 只加不减的话，快译覆盖强译后标记还留着，界面就会撒谎——
    // 而这个标记存在的全部意义就是让人分辨自己看的是哪一版。
    if (msg.quality) {
      var have = Number(card.dataset.quality || 0);
      if (msg.translated && msg.quality < have) return;
      if (msg.translated) {
        card.dataset.quality = msg.quality;
        card.classList.toggle("strong", msg.quality >= 2);
      }
    }
    var trans = card.querySelector(".trans");
    var stateChip = card.querySelector(".state-chip");
    var state = msg.translate_state;
    markBody(trans, !!msg.translated);   // 译文是正文；「翻译中…」是界面提示
    if (msg.translated) {
      trans.textContent = msg.translated;
      trans.classList.remove("pending");
    } else if (state === "pending") {
      trans.textContent = L("翻译中…", "Translating…");
      trans.classList.add("pending");
    } else {
      trans.textContent = "";
      trans.classList.remove("pending");
    }
    if (!stateChip) return;
    // 角标的字在 web/live-ui.js：底部大字幕画的是同一个函数的结果
    var chip = captionStateChip(msg);
    stateChip.classList.toggle("fail-chip", chip.fail);
    stateChip.textContent = chip.text;
  }

  // ---- 违禁词警报 ----
  // 警报是这个工具的核心产出，绝不自动消失：中控没看到就等于漏报。
  var alertNote = document.getElementById("alert-note");
  var alertSession = null;   // 当前场次 { session, streamer, total }，服务端在 config 里给
  var sessionTotal = 0;      // 本场报警总数（面板只留最近 50 条）
  var attention = { unseen: 0, restoreTo: null };   // 窗口在后台时标题上的提醒
  // 桌面窗口（pywebview）的原生标题不跟 document.title：条数经 JS 桥另外告诉窗口
  // （app/window_attention.py）。浏览器里没有 window.pywebview，这几行什么都不做
  var windowBridge = { sent: 0, seq: 0 };
  var windowPage = Math.random().toString(36).slice(2);

  function renderAlert(msg) {
    alertPanel.classList.remove("hidden");
    var item = document.createElement("div");
    item.className = "alert-item tier-" + (msg.tier || "exact");

    var head = document.createElement("div");
    head.className = "alert-head";
    var ts = new Date((msg.ts || Date.now() / 1000) * 1000);
    // 带上主播：换过房间后，面板上的旧报警不能被当成眼前这个主播说的
    // 分级：文字胶囊（颜色由 CSS 按 .alert-item.tier-* 给），不再是 🔴🟠🟡 前缀
    var tierTag = document.createElement("span");
    tierTag.className = "alert-tier";
    tierTag.textContent = alertTierText(msg.tier);
    head.appendChild(tierTag);
    // 词条和主播名是名字，各包一层 translate="no"。外面再套一个 span：.alert-head 是 flex，
    // 原来这一整段文字是一个匿名 flex 项，拆成几个直接子节点会多出几道 gap
    var headText = document.createElement("span");
    // 英文引用用户内容用弯双引号（docs/i18n-style.md §2.2）
    headText.appendChild(document.createTextNode(L("「", "“")));
    headText.appendChild(nameSpan(String(msg.term)));   // 与原来的字符串拼接逐字相同
    headText.appendChild(document.createTextNode(L("」 ", "” ") +
      pad(ts.getHours()) + ":" + pad(ts.getMinutes()) + ":" + pad(ts.getSeconds())));
    if (msg.streamer) {
      headText.appendChild(document.createTextNode(" "));
      headText.appendChild(nameSpan("@" + msg.streamer));
    }
    head.appendChild(headText);
    item.appendChild(head);
    item.dataset.session = msg.session || "";
    markAlertItem(item);

    var ctx = document.createElement("div");
    ctx.className = "alert-ctx";
    markBody(ctx, true);
    ctx.textContent = msg.context || "";
    item.appendChild(ctx);

    // 中文一行。报警是最需要人工复核的地方，只给西语原话等于让人没法判断。
    // 译文是后到的（要跑一次强模型），先占位，回来再填。
    var zh = document.createElement("div");
    zh.className = "alert-zh";
    markBody(zh, !!msg.context_zh);
    zh.textContent = msg.context_zh || L("翻译中…", "Translating…");
    if (!msg.context_zh) zh.classList.add("pending");
    item.appendChild(zh);
    if (msg.alert_id) item.dataset.alertId = msg.alert_id;

    alertList.insertBefore(item, alertList.firstChild);
    while (alertList.children.length > ALERT_PANEL_CAP) {
      alertList.removeChild(alertList.lastChild);
    }
    alertCount.textContent = alertList.children.length;
    if (!msg.replay && alertSession && msg.session === alertSession.session) {
      sessionTotal = Math.max(sessionTotal, msg.session_total || 0);
    }
    drawAlertNote();
    // 窗口被别的软件盖住时，标题是任务栏/Dock/标签页上唯一看得到的地方。
    // 回放的报警不改标题（noteAlert 里挡掉）：那是补发的历史，不是新情况
    attention = noteAlert(attention, msg, pageActive(), document.title);
    if (attention.title) document.title = attention.title;
    syncWindowAttention(false);
  }

  function markAlertItem(item) {
    var other = isOtherSession({ session: item.dataset.session },
                               alertSession && alertSession.session);
    item.classList.toggle("prev-session", other);
    var head = item.querySelector(".alert-head");
    var tag = item.querySelector(".alert-prev");
    if (other && !tag && head) {
      tag = document.createElement("span");
      tag.className = "alert-prev";
      tag.textContent = L("上一场", "Previous");
      head.insertBefore(tag, head.firstChild);
    } else if (!other && tag) {
      tag.parentNode.removeChild(tag);
    }
  }

  // 换场（或重连拿到当前场次）：旧报警标成「上一场」，不删——上一场可能是断线结束的，
  // 那几条报警中控可能还没处理
  function setAlertSession(info) {
    alertSession = info || null;
    sessionTotal = alertSession ? (alertSession.total || 0) : 0;
    for (var i = 0; i < alertList.children.length; i++) markAlertItem(alertList.children[i]);
    drawAlertNote();
  }

  function drawAlertNote() {
    if (!alertNote) return;
    var current = alertSession && alertSession.session;
    var shown = 0;
    for (var i = 0; i < alertList.children.length; i++) {
      var s = alertList.children[i].dataset.session;
      if (!current || !s || s === current) shown++;
    }
    var text = sessionNote(sessionTotal, shown);
    alertNote.textContent = text;
    alertNote.classList.toggle("hidden", !text);
  }

  function pageActive() {
    return !document.hidden && (typeof document.hasFocus !== "function" || document.hasFocus());
  }

  function clearAttention() {
    if (!attention.unseen || !pageActive()) return;
    attention = noteActive(attention, document.title);
    if (attention.title) document.title = attention.title;
    syncWindowAttention(false);
  }

  function syncWindowAttention(force) {
    var api = window.pywebview && window.pywebview.api;
    if (!api || typeof api.set_attention !== "function") return;
    var next = windowAttentionUpdate(windowBridge, attention.unseen, force);
    windowBridge = { sent: next.sent, seq: next.seq };
    if (next.send === null) return;
    try {
      var pending = api.set_attention(next.send, windowPage, next.seq);
      if (pending && typeof pending.catch === "function") pending.catch(function () {});
    } catch (e) { /* 桥出错不影响报警面板本身 */ }
  }

  document.addEventListener("visibilitychange", clearAttention);
  window.addEventListener("focus", clearAttention);
  document.addEventListener("pointerdown", clearAttention);
  document.addEventListener("keydown", clearAttention);
  // 兜底：窗口本来就在前台、没有再触发 focus 时，也要把标题换回来
  setInterval(clearAttention, 2000);
  // 页面告诉窗口自己是什么语言（app/window_lang.py，spec §8.1）：原生标题、后台报警时的
  // 标题、关窗确认框跟着换。双击第二次时窗口开在另一个进程里，收不到语言切换，只有页面
  // 知道自己此刻是什么语言。闸关着时这里总是 "zh"，窗口那边什么都不动
  function syncWindowLang() {
    var api = window.pywebview && window.pywebview.api;
    if (!api || typeof api.set_window_lang !== "function") return;
    try {
      var pending = api.set_window_lang(UI_LANG);
      if (pending && typeof pending.catch === "function") pending.catch(function () {});
    } catch (e) { /* 桥出错不影响页面本身 */ }
  }

  // 页面刚加载（含刷新）时照发一次：上一个页面留在窗口标题上的提醒要清掉。
  // 先告诉语言再同步条数：标题按新语言的基底加上条数
  window.addEventListener("pywebviewready", function () { syncWindowLang(); syncWindowAttention(true); });
  if (window.pywebview && window.pywebview.api) { syncWindowLang(); syncWindowAttention(true); }

  function updateAlert(msg) {
    var item = alertList.querySelector('[data-alert-id="' + msg.alert_id + '"]');
    if (!item) return;
    var zh = item.querySelector(".alert-zh");
    if (!zh) return;
    zh.classList.remove("pending");
    markBody(zh, !!msg.context_zh);
    if (msg.context_zh) {
      zh.textContent = msg.context_zh;
      zh.classList.remove("failed");
      return;
    }
    // 译不出来要说出来。以前这里把整行清空，中控看到一片空白，比停在
    // 「翻译中…」还糟——上面那行西语原话才是他真正要看的东西。
    // why 是服务端渲染好的界面文字；英文没有原因时整句不带括号
    zh.textContent = L("译文失败（" + (msg.why || "未知原因") + "）——请看上面的原话",
                       msg.why ? "Couldn’t translate (" + msg.why + "). See the original above."
                               : "Couldn’t translate. See the original above.");
    zh.classList.add("failed");
  }

  clearAlertsBtn.addEventListener("click", function () {
    alertList.innerHTML = "";
    alertCount.textContent = "0";
    alertPanel.classList.add("hidden");
    drawAlertNote();
  });

  // ---- 观众弹幕 ----
  // 弹幕只翻译、只显示，不进报警链路；面板折叠状态与警报面板无关，单独记忆。
  if (lsGet("commentPanelCollapsed") === "1") {
    commentPanel.classList.add("collapsed");
  }

  // 弹幕栏里这一场有没有弹幕（专家审查 U8）：从末尾往前看，到最近一条场次分隔线为止。
  // 以前用列表的 children.length 判断空不空：第二场起列表里留着上一场的弹幕、或者只剩那条分隔线，
  // 空态那句（「已连接，等待观众发评论…」「评论流断开，重连中…」）就再也不出现，要刷新页面才看得到。
  // 分隔线和上一场的弹幕都不算这一场的
  function sessionHasComments() {
    var kids = commentList.children;
    for (var i = kids.length - 1; i >= 0; i--) {
      if (kids[i].classList.contains("session-sep")) return false;
      if (kids[i].classList.contains("cmt-item")) return true;
    }
    return false;
  }

  // 面板什么时候露面、空着的时候说什么。直播中面板必须在——否则中控分不清
  // 「今天没人发弹幕」和「评论流压根没连上」，而后者是要去处理的。
  // show 只看 streamActive，不再是「有内容也算」：待机页面（含刚停止那一刻）
  // 不该显示上一场的弹幕面板/小入口，但列表内容本身不清空，下一场开始时
  // streamActive 一变回 true，原样恢复显示。
  function refreshCommentPanel() {
    var has = sessionHasComments();
    var show = streamActive;
    var collapsed = commentPanel.classList.contains("collapsed");
    commentPanel.classList.toggle("hidden", !show);
    commentEmpty.classList.toggle("hidden", has);
    // 收起时整列不显示，只在字幕区右上角留一个入口（不带条数：列表封顶 100 条，数字一直停在 100）
    commentFab.classList.toggle("hidden", !(show && collapsed));
    scheduleGuardBar();       // 浮钮上的未读红条数跟顶部条同一份数，同一个节流
    // 「回到最新」是 fixed 定位在右下角的，弹幕列展开时把它往左挪，别盖在弹幕上
    var panelOpen = show && !collapsed;
    jumpBtn.style.right = (panelOpen ? commentPanel.offsetWidth + 20 : 20) + "px";
    updateCommentJump();      // 面板收起/展开、直播起止都会改变这颗按钮该不该露面

    // 标题旁的小字状态 + 空态文案：中控要能一眼分清「没人发弹幕」和
    // 「抓取本身出了问题」，两者处理方式完全不同（前者等，后者去修）。
    // 后端说明原样写进去，太长由 CSS 截成最多 6 行（style.css .cmt-source）：以前在这里按
    // 80 个字符截，英文只截得出半句话（spec §11 第 6 条）
    var title, cls = "cmt-source", emptyText, headTip = "";
    // 小字状态就放在 Comments 标题旁边，英文不再重复「评论流」这个主语
    if (backendState === "connected") {
      // 连上是常态，不占一行：整个小字元素收起（圆点是绝对定位在它里面的，只清文字会留下一颗孤零零的点），
      // 这句话挪进标题的 title。其余状态（连接中、断开、出错、带原因的）照旧写在标题旁边
      title = "";
      cls += " on hidden";
      headTip = L("评论流已连接", "Connected");
      emptyText = L("已连接，等待观众发评论…", "Connected. Waiting for comments…");
    } else if (backendState === "connecting") {
      // 带说明的连接中（读浏览器登录态、组件刚更新完正在重连）要让中控看得见
      var cdetail = backendDetail || L("正在连接评论流…", "Connecting…");
      title = cdetail;
      emptyText = cdetail;
    } else if (backendState === "disconnected") {
      title = L("评论流断开，重连中…", "Disconnected. Reconnecting…");
      emptyText = title;
    } else if (backendState === "error" || backendState === "unavailable"
               || (backendState && backendState !== "idle" && backendDetail)) {
      // 后端还会发 offline / not_found / login_required / blocked 之类带原因的
      // 状态：没有专门分支的一律把 detail 原样给中控看，别显示成「未连接」
      // 让人以为弹幕功能没启动
      var detail = backendDetail || "";
      title = detail;
      cls += " warn";
      emptyText = detail;
    } else {
      title = L("未连接", "Not connected");
      emptyText = L("开播后自动连接评论流", "Comments connect when the stream starts.");
    }

    commentSource.textContent = title;
    commentSource.className = cls;
    if (commentTitleEl) {
      if (headTip) commentTitleEl.title = headTip;
      else commentTitleEl.removeAttribute("title");
    }
    if (!has) commentEmpty.textContent = emptyText;
  }

  // 弹幕作者和主播的关系：TikTok 随每条弹幕发的标记（app/comment_source.py _REL_FLAGS）。
  // 「互关」不显示（2026-10-21 用户定）：中控看到有的标了互关的观众其实不是互关，这个标记本机核实不了。
  // 带 mutual 的只标「粉丝」（互关里观众那一侧一定关注了主播），和 follower 并成一个。
  // 抓取端照旧收 mutual（字段统计、审计都不动），只是页面不画。只在电脑上显示，手机页收不到这个字段
  var REL_LABELS = {
    follower: [L("粉丝", "Follower"), L("关注了主播", "Follows the streamer")],
    gifter: [L("送过礼", "Gifter"), L("给主播送过礼物", "Has sent the streamer a gift")],
    subscriber: [L("订阅", "Subscriber"), L("订阅了主播", "Subscribes to the streamer")],
  };
  function relTags(rel) {
    if (!Array.isArray(rel)) return [];
    var tags = [];
    rel.forEach(function (t) {
      if (t === "mutual") t = "follower";
      if (REL_LABELS.hasOwnProperty(t) && tags.indexOf(t) < 0) tags.push(t);
    });
    return tags.map(function (t) {
      var chip = document.createElement("span");
      chip.className = "cmt-rel cmt-rel-" + t;
      chip.textContent = REL_LABELS[t][0];
      chip.title = REL_LABELS[t][1];
      return chip;
    });
  }

  function renderComment(msg) {
    var item = document.createElement("div");
    item.className = "cmt-item";
    item.dataset.cmtId = msg.id;

    var head = document.createElement("div");
    head.className = "cmt-head";
    var ts = new Date((msg.ts || Date.now() / 1000) * 1000);
    head.appendChild(nameSpan(msg.user || ""));   // 观众名
    relTags(msg.rel).forEach(function (tag) { head.appendChild(tag); });
    head.appendChild(document.createTextNode(" " +
      pad(ts.getHours()) + ":" + pad(ts.getMinutes()) + ":" + pad(ts.getSeconds())));
    item.appendChild(head);

    // 译文行：pending 时占位提示，same/skipped 时直接就是原文本身（不会再更新）
    var zh = document.createElement("div");
    zh.className = "cmt-zh";
    markBody(zh, msg.state !== "pending");
    if (msg.state === "pending") {
      zh.textContent = L("翻译中…", "Translating…");
      zh.classList.add("pending");
    } else {
      zh.textContent = msg.translated || msg.text || "";
    }
    item.appendChild(zh);

    // 原文行：跟译文重复时（same/skipped）没必要再显示一遍
    var orig = document.createElement("div");
    orig.className = "cmt-orig";
    markBody(orig, true);
    orig.textContent = msg.text || "";
    if (msg.state === "same" || msg.state === "skipped") orig.classList.add("hidden");
    item.appendChild(orig);

    // 正序：新的追加在底部，和字幕列表同一个方向，从上往下读就是时间顺序。回放时先把发生在这条之前的
    // 换场分隔线插进去（F1）
    if (pendingCommentBreaks.length && typeof msg.ts === "number") flushCommentBreaks(msg.ts);
    commentList.appendChild(item);
    commentById[msg.id] = item;
    noteCommentTr(msg.id, msg.state, msg.translated);
    if (msg.replay) guardCommentChanged(msg.id);   // 重连回放：抽屉里的这一行补上译文
    syncCommentRedo(item);
    noteCommentGuard(msg, item);
    // 满 maxComments 条（100）从顶部删最旧的。用户正往上翻着看时，眼前那条不能跟着跑：
    // 删之前记下第一条可见弹幕的位置，删完量它实际移动了多少再补回来。
    // 不按「删掉多高」去减——Chromium 自带滚动锚定会先补一次，再减就补过头
    // （实测每删一条视野往上窜一条）；WebKit 不一定锚定。量实际位移两边都对。
    var anchor = null, anchorTop = 0;
    var excess = commentList.children.length - maxComments;
    if (excess > 0 && !commentFollowing && isMeasurable(commentList)) {
      var listTop = commentList.getBoundingClientRect().top;
      for (var i = excess; i < commentList.children.length; i++) {
        var rect = commentList.children[i].getBoundingClientRect();
        if (rect.bottom > listTop) { anchor = commentList.children[i]; anchorTop = rect.top; break; }
      }
    }
    while (commentList.children.length > maxComments) {
      var oldest = commentList.firstChild;
      delete commentById[oldest.dataset.cmtId];
      delete commentMeta[oldest.dataset.cmtId];
      commentList.removeChild(oldest);
    }
    if (anchor) {
      var drift = anchor.getBoundingClientRect().top - anchorTop;
      if (Math.abs(drift) >= 1) commentList.scrollTop += drift;
    }
    // 往上翻着看时，新弹幕不会把视野拉走：数着，「回到最新」按钮上显示（回放的不算）
    if (!msg.replay && streamActive && !commentFollowing) commentsBelow++;
    refreshCommentPanel();
    if (commentFollowing) commentsToBottomNow();
    updateCommentJump();
  }

  function updateComment(msg) {
    noteCommentTr(msg.id, msg.state, msg.translated);
    guardCommentChanged(msg.id);     // 抽屉里有这条：译文到了就重画（弹幕栏里已经没有它也一样）
    var item = commentById[msg.id];
    if (!item) return;
    var zh = item.querySelector(".cmt-zh");
    var orig = item.querySelector(".cmt-orig");
    if (!zh) return;
    zh.classList.remove("pending");
    markBody(zh, true);   // 下面两路放的都是正文：译文，或者原文本身
    if (msg.state === "ok") {
      zh.textContent = msg.translated || "";
      zh.classList.remove("failed");
      if (orig) orig.classList.remove("hidden");     // 先失败、重译成了：原文行回来
      syncCommentRedo(item);
      return;
    }
    // failed/dropped：译不出来就显示原文本身——不显示「失败」字样，
    // 原文就是内容，中控照样看得懂观众在说什么
    zh.textContent = orig ? orig.textContent : "";
    zh.classList.add("failed");
    if (orig) orig.classList.add("hidden");
    syncCommentRedo(item);
  }

  // ---- 弹幕重译 ----
  // 译好了的、和没译出来的弹幕，悬停时右上角出一个「重译」（「弹幕留意」抽屉里那一行也有）：服务端用 Claude Sonnet 5.5 再译一次
  // （app/comments.py retranslate）。只有字幕引擎是 Claude 时才能点——别的引擎下按钮是灰的，
  // 悬停说明为什么：弹幕重译从不去叫本地的 7B。手机页没有这个按钮。
  // 译好了的、和没译出来的（失败、积压跳过）都能重译；状态（进行中 / 失败）按 id 记在 commentRedoState，
  // 按钮的样子由 commentRedoView 现算，弹幕栏和「弹幕留意」抽屉共用：引擎变了、状态变了都走同一处。
  var COMMENT_REDO_NOTE_MS = 7000;

  function noteCommentTr(id, state, translated) {
    if (!id) return;
    var seq;
    if (!(id in commentTr)) {
      commentTrOrder.push(id);
      if (commentTrOrder.length > COMMENT_TR_KEEP) delete commentTr[commentTrOrder.shift()];
      seq = ++commentTrSeq;
    } else {
      seq = commentTr[id].seq;
    }
    commentTr[id] = { state: state || "", text: translated || "", seq: seq };
  }

  // 「重译」按钮的样子：弹幕栏和抽屉共用。没有可重译的返回 null
  function commentRedoView(id) {
    var t = id ? commentTr[id] : null;
    if (!t || !COMMENT_REDO_STATES[t.state]) return null;
    if (commentTrSeq - t.seq >= COMMENT_REDO_KEEP) return null;     // 服务端已经不记得它的原文
    var usable = commentRedoEngine === "claude";
    var busy = commentRedoState[id] === "pending";
    return {
      disabled: busy || !usable,
      text: busy ? L("重译中…", "Retranslating…")
        : (commentRedoState[id] === "failed" ? L("重译失败", "Couldn’t retranslate") : L("重译", "Retranslate")),
      title: usable ? L("用更强的模型重新翻译这条弹幕", "Retranslate this comment with the stronger model")
                    : L("弹幕重译需要 Claude 引擎", "Comment retranslation needs the Claude engine"),
      note: commentRedoNotes[id] || ""
    };
  }

  function sendCommentRedo(id) {
    if (id) send({ type: "retranslate_comment", id: id });
  }

  function resetCommentRedo() {
    Object.keys(commentRedoStateTimers).forEach(function (id) { clearTimeout(commentRedoStateTimers[id]); });
    Object.keys(commentRedoNoteTimers).forEach(function (id) { clearTimeout(commentRedoNoteTimers[id]); });
    commentRedoState = Object.create(null);
    commentRedoNotes = Object.create(null);
    commentRedoStateTimers = Object.create(null);
    commentRedoNoteTimers = Object.create(null);
  }

  function syncCommentRedo(item) {
    var btn = item.querySelector(".cmt-redo");
    var view = commentRedoView(item.dataset.cmtId);
    if (!view) {          // 已是目标语言、还在翻、引擎不翻弹幕：没有可重译的
      if (btn) btn.remove();
      return;
    }
    if (!btn) {
      btn = document.createElement("button");
      btn.className = "cmt-redo";
      btn.type = "button";
      btn.addEventListener("click", function () { sendCommentRedo(item.dataset.cmtId); });
      item.appendChild(btn);
    }
    btn.disabled = view.disabled;
    btn.textContent = view.text;
    btn.title = view.title;
  }

  function syncAllCommentRedo() {
    var items = commentList.querySelectorAll(".cmt-item");
    for (var i = 0; i < items.length; i++) syncCommentRedo(items[i]);
  }

  function clearCommentRedoNote(item, id) {
    if (commentRedoTimers[id]) {
      clearTimeout(commentRedoTimers[id]);
      delete commentRedoTimers[id];
    }
    var old = item.querySelector(".redo-note");
    if (old) old.remove();
  }

  function receiveCommentRedo(msg) {
    var item = commentById[msg.id];
    if (msg.redo_state) {
      var pending = msg.redo_state === "pending";
      var failed = msg.redo_state === "failed";
      if (pending || failed) commentRedoState[msg.id] = msg.redo_state;
      else delete commentRedoState[msg.id];
      // 又点了一次、或这次成了：上一次的说明不再适用
      if (pending || msg.redo_state === "ok") delete commentRedoNotes[msg.id];
      clearTimeout(commentRedoStateTimers[msg.id]);
      delete commentRedoStateTimers[msg.id];
      if (failed) {
        commentRedoStateTimers[msg.id] = setTimeout(function () {
          delete commentRedoStateTimers[msg.id];
          if (commentRedoState[msg.id] === "failed") delete commentRedoState[msg.id];
          var now = commentById[msg.id];           // 到点时再取：期间可能重连重画过
          if (now) { now.classList.remove("redo-failed"); syncCommentRedo(now); }
          guardCommentChanged(msg.id);
        }, 4000);
      }
      if (item) {
        item.classList.toggle("redoing", pending);
        item.classList.toggle("redo-failed", failed);
        if (pending || msg.redo_state === "ok") clearCommentRedoNote(item, msg.id);
        syncCommentRedo(item);
      }
    }
    if (msg.text) {
      // 抽屉里那一行也显示这句说明，几秒后收起
      commentRedoNotes[msg.id] = msg.text;
      clearTimeout(commentRedoNoteTimers[msg.id]);
      commentRedoNoteTimers[msg.id] = setTimeout(function () {
        delete commentRedoNoteTimers[msg.id];
        delete commentRedoNotes[msg.id];
        guardCommentChanged(msg.id);
      }, COMMENT_REDO_NOTE_MS);
    }
    guardCommentChanged(msg.id);
    if (!msg.text) return;
    // 那条弹幕已经不在页面上了（清空了、被挤出 100 条）：话退回页脚那一行，不能丢——
    // 抽屉里正显示着这一行的话就不用了，说明已经在那里
    if (!item) {
      if (!guardDrawerShows(msg.id)) showVersionNote(msg.text, 6000);
      return;
    }
    clearCommentRedoNote(item, msg.id);
    var note = document.createElement("div");
    note.className = "redo-note";
    note.setAttribute("role", "status");
    note.textContent = msg.text;
    item.appendChild(note);
    commentRedoTimers[msg.id] = setTimeout(function () {
      delete commentRedoTimers[msg.id];
      note.remove();
    }, COMMENT_REDO_NOTE_MS);
    if (commentFollowing) commentsToBottomNow();       // 多了一行：贴底看的人跟着再贴一次
  }

  // ---- 弹幕跟随 ----
  // 和字幕同一套规则（见 follow.js）：停在底部就跟着最新走；用户自己往上翻
  // 就不打扰，翻回底部再恢复跟随。面板收起或页面不可见时几何量全是 0，
  // 这时沿用原来的意图，等重新可见再补滚。
  var commentFollowing = true;
  var lastCommentInput = 0;
  ["wheel", "touchstart", "touchmove", "keydown", "mousedown"].forEach(
    function (name) {
      commentList.addEventListener(name, function () {
        lastCommentInput = Date.now();
      }, { passive: true });
    });

  commentList.addEventListener("scroll", function () {
    commentFollowing = nextFollowing(commentList, commentFollowing,
                                     Date.now() - lastCommentInput < 700);
    if (commentFollowing) commentsBelow = 0;
    updateCommentJump();
  }, { passive: true });

  // 弹幕栏自己滚（和字幕区各管各的），所以自己也要有一个「回到最新」：往上翻着看时，按钮在
  // 弹幕栏右下角，写着翻上去之后又来了几条。bottom 与字幕那颗取同一个值（躲开底部大字幕）
  var commentsBelow = 0;
  var commentJumpLabel = jumpLatestCommentsText(0);   // 与 index.html 里的静态文字相同，没变就不重写
  function updateCommentJump() {
    if (!commentJump) return;
    var show = streamActive && !commentPanel.classList.contains("collapsed")
      && isMeasurable(commentList) && !atBottom(commentList);
    if (show) commentJump.style.bottom = jumpBottomPx() + "px";
    commentJump.classList.toggle("hidden", !show);
    var label = jumpLatestCommentsText(commentsBelow);
    if (label !== commentJumpLabel) {
      commentJumpLabel = label;
      setIconText(commentJump, "arrow-down", label);
    }
  }

  if (commentJump) {
    commentJump.addEventListener("click", function () {
      commentFollowing = true;          // 点了就是要看最新，恢复跟随
      commentsToBottomNow();
      updateCommentJump();
    });
  }

  // 瞬时滚到底：理由同字幕的 scrollToBottomNow（平滑动画追不上连续追加）
  function commentsToBottomNow() {
    commentsBelow = 0;         // 只在跟随着（或刚点了「回到最新」）时才会滚到底：没有没看的了
    var prev = commentList.style.scrollBehavior;
    commentList.style.scrollBehavior = "auto";
    commentList.scrollTop = commentList.scrollHeight;
    commentList.style.scrollBehavior = prev;
  }

  function resyncComments() {
    if (document.hidden || !commentFollowing) return;
    requestAnimationFrame(function () { if (commentFollowing) commentsToBottomNow(); });  // 理由同 resync
  }

  toggleCommentsBtn.addEventListener("click", function () {
    commentPanel.classList.add("collapsed");
    lsSet("commentPanelCollapsed", "1");
    refreshCommentPanel();
  });

  commentFab.addEventListener("click", function () {
    commentPanel.classList.remove("collapsed");
    lsSet("commentPanelCollapsed", "0");
    refreshCommentPanel();
    resyncComments();         // 收起期间来的弹幕没法滚动，展开时补到最新
  });

  window.addEventListener("resize", refreshCommentPanel);

  // 只清这一页的弹幕：点「清空」、别的页面点了清空后的同步，都走这里
  function clearCommentsView() {
    commentList.innerHTML = "";
    pendingCommentBreaks = [];   // 弹幕清空了，还没插进去的分隔线也不再有位置
    commentById = Object.create(null);
    commentMeta = Object.create(null);
    commentFollowing = true;
    commentsBelow = 0;
    refreshCommentPanel();    // 直播中清空后面板留着，只是回到空态
    updateCommentJump();
  }

  clearCommentsBtn.addEventListener("click", function () {
    clearCommentsView();
    forgetOnServer("comments");   // 服务端缓存也清，刷新后不会又回来
  });

  // ---- 弹幕留意（Comment Review，web/guard.js；spec §7） ----
  // 规则判定随 comment 消息的 guard 字段到；复核结果经 comment_guard 补；事件聚合成 guard_event；关注名单经
  // guard_mark 增删。状态都存在这里，文案、匹配、渲染在 guard.js。守卫的东西只在直播中出现，不进首页或开始
  // 面板（CLAUDE.md 第六条）；设置分组在直播中整块隐藏，所以直播中要用的（看名单、移出、重新启用）都在抽屉里。
  // 手机同看页上没有这些（用户决定 8）：后端不发，手机页也不加载 guard.js。
  var GUARD_SEEN_KEY = "guardSeenTs";
  var REENABLE_WAIT_NOTE = L("识别稳定追上 60 秒后可用", "Available after speech recognition has kept up for 60 sec");
  var GUARD_BAR_GAP_MS = 1000;                 // 顶部条每秒最多重画 1 次：冲场时只是数字在变
  var guardState = null;                       // config.guard_state；老后端没有，整个功能就不出现
  var guardEvents = Object.create(null);       // 事件 id -> 事件（config.guard_events + guard_event）
  var guardMarked = Object.create(null);       // 主键 -> 条目（config.guard_marked + guard_mark）
  var guardMarkIdx = Object.create(null);      // 名单里任一键 -> 主键，按弹幕现算高亮用
  var guardTab = "recent";
  var guardDrawerOpen = false;
  var guardSeenTs = Number(lsGet(GUARD_SEEN_KEY)) || 0;   // 「已看」：晚于它的事件才算未读。存 localStorage，读写都兜住了
  var commentMeta = Object.create(null);       // 弹幕 id -> 账号字段和判定：关注名单或设置变了，按它重画标记
  var markedSpoke = [];                        // 关注的人本场发言的时刻（秒）：顶部条的「关注的人发言 n」
  var guardBarTimer = null, guardBarAt = 0;
  var guardPressing = false, guardDrawerDirty = false;     // 鼠标按着时不重画抽屉，免得点击落空

  function guardCtx() {
    return {
      state: guardState, marked: guardMarked, index: guardMarkIdx, events: guardEvents, now: Date.now() / 1000,
      onMark: function (who, on) { send(guardMarkMessage(who, on)); },
      onUnmark: function (key) { send({ type: "guard_mark", key: key, on: false }); },
      onLocate: guardLocate, onDismiss: guardDismiss, translationOf: guardTranslationOf,
      redoOf: commentRedoView, onRedo: sendCommentRedo
    };
  }

  // 一条弹幕的译文或重译状态变了：抽屉开着、而且「最近需要留意」里有这一条，就重画
  function guardCommentChanged(cid) {
    if (guardDrawerShows(cid)) drawGuardDrawer();
  }

  function guardDrawerShows(cid) {
    if (!cid || !guardDrawerOpen || guardTab !== "recent") return false;
    for (var id in guardEvents) {
      var ev = guardEvents[id];
      if (ev && !ev.dismissed && ev.sample && ev.sample.cid === cid) return true;
    }
    return false;
  }

  // 收到一条弹幕：记下账号字段和判定，画标记；关注的人发言了就记一笔（回放里的不算）
  function noteCommentGuard(msg, item) {
    commentMeta[msg.id] = { uid: msg.uid, uid_ok: msg.uid_ok, sec: msg.sec, handle: msg.handle,
                            user: msg.user, guard: msg.guard };
    var view = guardDecorateComment(document, item, commentMeta[msg.id], guardCtx());
    if (view && view.marked && !msg.replay) {
      markedSpoke.push(msg.ts || Date.now() / 1000);
      if (markedSpoke.length > 200) markedSpoke.splice(0, markedSpoke.length - 200);
      scheduleGuardBar();
    }
  }

  function decorateComment(id, ctx) {
    var item = commentById[id], meta = commentMeta[id];
    if (item && meta) guardDecorateComment(document, item, meta, ctx || guardCtx());
  }

  function decorateAllComments() {
    var ctx = guardCtx();
    Object.keys(commentMeta).forEach(function (id) { decorateComment(id, ctx); });
  }

  function updateCommentGuard(msg) {
    var meta = commentMeta[msg.id];
    if (!meta) return;
    meta.guard = msg.guard && typeof msg.guard === "object" ? msg.guard : null;
    decorateComment(msg.id);
  }

  // hello / config：整份换掉本地的。回放不算已看：哪些看过由 guardSeenTs 定（存在 localStorage 里，刷新也在），
  // 只有中控点了「已看」才往前推（例外见下面 hello 那几行）
  function applyGuardConfig(cfg, hello) {
    if (!cfg) return;
    var stateChanged = false;
    if ("guard_state" in cfg) {
      guardState = cfg.guard_state && typeof cfg.guard_state === "object" ? cfg.guard_state : null;
      stateChanged = true;
    }
    if ("guard_events" in cfg) {
      guardEvents = Object.create(null);
      var evs = cfg.guard_events && typeof cfg.guard_events === "object" ? cfg.guard_events : {};
      Object.keys(evs).forEach(function (k) { if (evs[k] && evs[k].id) guardEvents[evs[k].id] = evs[k]; });
      // 事件表是按场整份换的（开场清空 / hello 重放）：「关注的人本场发言」跟着归零，
      // 不然上一场的数会带到下一场（换主播时则是挂在新主播的名单上）
      markedSpoke = [];
    }
    if ("guard_marked" in cfg) {
      guardMarked = Object.create(null);
      var mk = cfg.guard_marked && typeof cfg.guard_marked === "object" ? cfg.guard_marked : {};
      Object.keys(mk).forEach(function (k) { if (mk[k] && typeof mk[k] === "object") guardMarked[k] = mk[k]; });
      guardMarkIdx = guardMarkIndex(guardMarked);
    }
    // 以前每次 hello 都在这里 markGuardSeen()（专家审查 F2）：页面任何一次重连——瞬断、手动刷新、切换界面
    // 语言的重载——都把未读全标成已看，顶部条和浮钮红点一起消失；断线期间新到的红色事件只随回放到达，
    // 从头到尾一次都没亮过。现在只剩一种情况会标：这台浏览器从没记过「已看」（guardSeenTs 是 0：第一次打开
    // 页面）。刚打开页面不为这一场早先的事件弹顶部条，这条约定 tests/app.en.test.mjs 钉着；标过一次之后
    // 时刻就存下了，之后的重连、刷新都不再标。localStorage 用不了的话每次刷新都是「第一次」，断线期间的
    // 事件仍可能被这样标掉——只剩这一种
    if (hello) {
      markedSpoke = [];      // 回放的弹幕不重新计「关注的人本场发言」
      if (!guardSeenTs) markGuardSeen();
    }
    if (stateChanged || "guard_marked" in cfg) decorateAllComments();
    drawGuardAll();
  }

  // 一场新的开始（待机 -> 连接中/直播中）：上一场的事件和「关注的人发言」不再显示。后端的重置
  // config（事件表清空）要等模型和审计就绪才到，这之前「连接中…」不该露出上一场的未读红条。
  // 只在 status 消息里调：hello 是整份重放，那里的事件正是要显示的
  function guardNewSession() {
    guardEvents = Object.create(null);
    markedSpoke = [];
  }

  function receiveGuardEvent(msg) {
    var ev = msg.ev;
    if (!ev || typeof ev !== "object" || !ev.id) return;
    if (ev.dismissed) delete guardEvents[ev.id]; else guardEvents[ev.id] = ev;
    drawGuardDrawer();
    scheduleGuardBar();
  }

  function receiveGuardMark(msg) {
    if (!msg.key) return;
    if (msg.entry && typeof msg.entry === "object") guardMarked[msg.key] = msg.entry;
    else delete guardMarked[msg.key];
    guardMarkIdx = guardMarkIndex(guardMarked);
    decorateAllComments();
    drawGuardDrawer();
    syncGuardSettings();
  }

  function markGuardSeen() {
    guardSeenTs = Math.max(guardSeenTs, Date.now() / 1000);
    lsSet(GUARD_SEEN_KEY, String(guardSeenTs));
  }

  // ---- 画：顶部条（节流）、浮钮红点、胶囊、抽屉、设置行 ----
  function scheduleGuardBar() {
    var wait = GUARD_BAR_GAP_MS - (Date.now() - guardBarAt);
    if (wait <= 0) { drawGuardBar(); return; }
    if (guardBarTimer) return;
    guardBarTimer = setTimeout(function () { guardBarTimer = null; drawGuardBar(); }, wait);
  }

  function drawGuardBar() {
    guardBarAt = Date.now();
    if (!guardBar) return;
    var sum = guardBarSummary(guardEvents, guardSeenTs, { markedTs: markedSpoke });
    var show = streamActive && !!guardState && !!guardState.enabled && sum.show;
    guardBar.classList.toggle("hidden", !show);
    if (show) {
      guardBarText.textContent = sum.text;
      guardBarMain.title = sum.text;
      guardBar.classList.toggle("has-red", sum.red > 0);
    }
    // 弹幕列收起时，浮钮上显示未读红条数（琥珀不出红点）
    var dot = streamActive && !!guardState && !!guardState.enabled && sum.red > 0;
    if (commentFabGuard) {
      commentFabGuard.classList.toggle("hidden", !dot);
      if (dot) { commentFabGuard.textContent = String(sum.red); commentFabGuard.title = guardFabTitle(sum.red); }
    }
  }

  function drawGuardCapsule() {
    var cap = guardCapsule(guardState);
    var show = streamActive && !!cap;
    if (guardStateBtn) guardStateBtn.classList.toggle("hidden", !show);
    if (guardReenableCap && !show) guardReenableCap.classList.add("hidden");
    if (!show || !guardStateBtn) return;
    guardStateTextEl.textContent = cap.label;     // 常态只画「弹幕留意」，完整一句在 title 里
    guardStateBtn.className = "guard-state " + cap.tone;
    guardStateBtn.title = cap.unreviewed ? cap.title + "\n" + cap.unreviewed : cap.title;
    // 读屏读到的名字以看得见的字开头，再接完整一句（键盘用户看不到 title）
    if (cap.text && cap.text !== cap.label) guardStateBtn.setAttribute("aria-label", cap.text);
    else guardStateBtn.removeAttribute("aria-label");
    guardStateBtn.setAttribute("aria-expanded", guardDrawerOpen ? "true" : "false");
    // 熔断后胶囊下面也有「本场重新启用」：不必先打开抽屉
    if (guardReenableCap) {
      guardReenableCap.classList.toggle("hidden", !cap.reenable);
      guardReenableCap.disabled = !!(cap.reenable && !cap.reenableOk);
      guardReenableCap.title = guardReenableCap.disabled ? REENABLE_WAIT_NOTE : "";
    }
  }

  function guardFocusedButton() {
    var el = document.activeElement;
    if (!el || !guardEventsEl || !guardEventsEl.contains || !guardEventsEl.contains(el)) return null;
    var row = el.closest ? el.closest(".guard-ev-row") : null;
    var btns = row ? Array.prototype.slice.call(row.querySelectorAll("button")) : [];
    var at = btns.indexOf(el);
    return row && at >= 0 ? { ev: row.getAttribute("data-ev"), at: at } : null;
  }

  function guardRestoreFocus(focusAt) {
    var rows = guardEventsEl.querySelectorAll(".guard-ev-row");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-ev") !== focusAt.ev) continue;
      var btn = rows[i].querySelectorAll("button")[focusAt.at];
      if (btn && btn.focus) btn.focus();
      return;
    }
  }

  function guardTranslationOf(cid) {
    var t = cid ? commentTr[cid] : null;
    return t && t.state === "ok" ? t.text : "";
  }

  function drawGuardDrawer() {
    if (!guardDrawer) return;
    var open = guardDrawerOpen && streamActive && !!guardState;
    guardDrawer.classList.toggle("hidden", !open);
    if (guardBarMain) guardBarMain.setAttribute("aria-expanded", open ? "true" : "false");
    if (guardStateBtn) guardStateBtn.setAttribute("aria-expanded", open ? "true" : "false");
    // 条右侧的控件写的是点了会怎样：抽屉开着就是「收起」，不再写「查看」
    if (guardBarView) guardBarView.textContent = guardBarViewText(open);
    if (!open) return;
    if (guardPressing) { guardDrawerDirty = true; return; }
    guardDrawerDirty = false;
    var cap = guardCapsule(guardState);
    guardDrawerState.textContent = cap ? cap.text : "";
    if (cap && cap.unreviewed) {
      var extra = document.createElement("span");
      extra.className = "gds-extra";
      extra.textContent = cap.unreviewed;
      guardDrawerState.appendChild(extra);
    }
    guardReenableBtn.classList.toggle("hidden", !(cap && cap.reenable));
    guardReenableBtn.disabled = !!(cap && cap.reenable && !cap.reenableOk);
    guardReenableBtn.title = guardReenableBtn.disabled ? REENABLE_WAIT_NOTE : "";
    var recent = guardTab === "recent";
    guardTabRecent.classList.toggle("on", recent);
    guardTabMarked.classList.toggle("on", !recent);
    guardTabRecent.setAttribute("aria-selected", recent ? "true" : "false");
    guardTabMarked.setAttribute("aria-selected", recent ? "false" : "true");
    guardPaneRecent.classList.toggle("hidden", !recent);
    guardPaneMarked.classList.toggle("hidden", recent);
    var nMarked = Object.keys(guardMarked).length;
    guardTabMarkedN.textContent = nMarked ? String(nMarked) : "";
    var ctx = guardCtx();
    // 重画会重建每一行：键盘焦点在抽屉里的某个按钮上时，记下它在哪一行、是哪个按钮，画完放回去
    var focusAt = guardFocusedButton();
    if (recent) guardRenderEvents(document, guardEventsEl, guardEvents, ctx);
    else guardRenderMarked(document, guardMarkedEl, guardMarked, ctx);
    if (focusAt) guardRestoreFocus(focusAt);
  }

  function syncGuardSettings() {
    if (!guardCard) return;
    var st = guardState;
    guardCard.classList.toggle("hidden", !st);         // 老后端没有这个功能：整行不出现
    if (!st) return;
    var nMarked = Object.keys(guardMarked).length;
    guardSummaryEl.textContent = guardSummary(st, nMarked);
    guardToggle.checked = !!st.enabled;
    guardToggle.disabled = st.status === "env_off";     // 应急开关关着：设置里改不动
    guardBackendSel.value = st.backend === "claude" ? "claude" : "none";
    guardWeak.checked = !!st.weak;
    var statusText = guardStateText(st), unrev = guardUnreviewedText(st);
    guardStatusEl.textContent = unrev ? statusText + " " + unrev : statusText;
    syncGuardKeyRow();
    guardFillHeading(document, guardMarkedHeading, st.streamer || "");
    guardRenderMarked(document, guardMarkedList, guardMarked, guardCtx());
    guardClearAuto.classList.toggle("hidden", !Object.keys(guardMarked).some(function (k) { return guardMarked[k].by === "auto"; }));
    guardRulesEl.textContent = guardRulesText(st);
  }

  // 选了 Claude、而翻译引擎那边还没有 Claude 密钥：显示密钥框。写进同一个密钥，不切换翻译引擎
  function syncGuardKeyRow() {
    var claude = guardBackendSel.value === "claude";
    guardKeyRow.classList.toggle("hidden", !(claude && !(engineKeys && engineKeys.ANTHROPIC_API_KEY)));
    // 「需要复核的弹幕会发给 Anthropic」：复核根本发不出去的状态不说——prompt_missing（读不到提示词）；
    // no_review 是老版本后端发的（现在的后端没有密钥时发 no_key，那时这句话照说：填了密钥就会发）
    var nothingSent = guardState && (guardState.status === "no_review" || guardState.status === "prompt_missing");
    guardPrivacy.classList.toggle("hidden", !claude || !!nothingSent);
  }

  function syncGuardLive() {
    if (!streamActive) guardDrawerOpen = false;
    drawGuardBar();
    drawGuardCapsule();
    drawGuardDrawer();
  }

  function drawGuardAll() {
    drawGuardBar();
    drawGuardCapsule();
    drawGuardDrawer();
    syncGuardSettings();
  }

  // ---- 抽屉里的动作 ----
  function guardLocate(ev) {
    var cid = ev && ev.sample ? ev.sample.cid : null;
    var item = cid ? commentById[cid] : null;
    if (!item) {      // 弹幕列表只留最近 100 条：这一条已经滚出去了
      showVersionNote(L("这条弹幕已经不在列表里", "That comment is no longer in the list"), 4000);
      return;
    }
    if (commentPanel.classList.contains("collapsed")) {     // 面板收起着就先展开
      commentPanel.classList.remove("collapsed");
      lsSet("commentPanelCollapsed", "0");
      refreshCommentPanel();
    }
    commentFollowing = false;       // 不然自动跟随下一条弹幕来时又把视野拉回底部
    var delta = item.getBoundingClientRect().top - commentList.getBoundingClientRect().top
                - commentList.clientHeight / 2;
    commentList.scrollTop += delta;
    item.classList.add("guard-flash");
    setTimeout(function () { item.classList.remove("guard-flash"); }, 1600);
  }

  function guardDismiss(ev) {
    send({ type: "guard_dismiss", ev_id: ev.id });
    delete guardEvents[ev.id];
    drawGuardDrawer();
    scheduleGuardBar();
  }

  function openGuardDrawer(tab) {
    if (tab) guardTab = tab;
    guardDrawerOpen = !(guardDrawerOpen && !tab);      // 同一个入口再点一次收起；指明页签（点胶囊 / 条）就只展开
    drawGuardDrawer();
    drawGuardCapsule();
  }

  if (guardBarMain) guardBarMain.addEventListener("click", function () { openGuardDrawer(); });
  if (guardStateBtn) guardStateBtn.addEventListener("click", function () { openGuardDrawer(); });
  if (guardDrawerClose) guardDrawerClose.addEventListener("click", function () {
    guardDrawerOpen = false;
    drawGuardDrawer();
    drawGuardCapsule();
  });
  if (guardBarSeen) guardBarSeen.addEventListener("click", function () {
    markedSpoke = [];
    markGuardSeen();
    drawGuardBar();
  });
  [[guardTabRecent, "recent"], [guardTabMarked, "marked"]].forEach(function (pair) {
    if (pair[0]) pair[0].addEventListener("click", function () { guardTab = pair[1]; drawGuardDrawer(); });
  });
  [guardReenableBtn, guardReenableCap].forEach(function (btn) {
    if (btn) btn.addEventListener("click", function () { send({ type: "guard_reenable" }); });
  });
  if (guardDrawer) {
    guardDrawer.addEventListener("mousedown", function () { guardPressing = true; });
    window.addEventListener("mouseup", function () {
      guardPressing = false;
      if (guardDrawerDirty) drawGuardDrawer();
    });
  }

  // ---- 设置行里的控件（直播中设置分组是隐藏的，这些只在待机时用） ----
  if (guardToggle) guardToggle.addEventListener("change", function () {
    send({ type: "guard_set", enabled: guardToggle.checked });
  });
  if (guardBackendSel) guardBackendSel.addEventListener("change", function () {
    syncGuardKeyRow();
    send({ type: "guard_set", backend: guardBackendSel.value });
  });
  if (guardWeak) guardWeak.addEventListener("change", function () {
    send({ type: "guard_set", weak: guardWeak.checked });
  });
  if (guardKeySave) guardKeySave.addEventListener("click", function () {
    var key = (guardKeyInput.value || "").trim();
    if (!key) return;
    send({ type: "guard_set", backend: "claude", key: key });
    guardKeyInput.value = "";
  });
  if (guardClearAuto) guardClearAuto.addEventListener("click", function () { send({ type: "guard_clear_auto" }); });
  if (guardDirBtn) guardDirBtn.addEventListener("click", function () { send({ type: "open_guard_dir" }); });

  // 识别落后时必须让中控看见——假装一切正常比晚几秒报警危险得多
  // 持续提示（电脑休眠过、审计日志写不进去、识别改用 CPU……）：按 id 覆盖，level=clear 去掉。
  // 和 health（识别积压，会被「已追上」覆盖）、notice（几秒后消失）不同：这类状况中控必须
  // 看到，刷新页面也还在（服务端放在 hello 的 config.incidents 里）。
  var incidents = Object.create(null);
  // 带 × 的持续提示：只有时钟跳变那一条（2026-10-21 用户决定：一场合盖几十次，那条黄条一直挂着关不掉）。
  // 跟服务端 Pipeline.DISMISSABLE_INCIDENTS 是同一份（tests/test_clock_gap_dismiss.py 比对）；别的提示照旧
  // 只由程序自己撤。点 × 是让服务端撤：所有页面、手机同看一起收起，刷新也不再冒出来；又跳一次服务端会
  // 重新挂出（「本场第 N 次」接着数），10 分钟没再跳服务端也会自己收起
  var DISMISSABLE_INCIDENTS = ["session:clock_gap"];

  function renderIncident(msg) {
    if (!msg || !msg.id) return;
    if (msg.level === "clear") {
      delete incidents[msg.id];
    } else {
      incidents[msg.id] = { level: msg.level || "warn", text: msg.text || "",
                            since: msg.since || msg.ts || 0, ts: msg.ts };
    }
    drawIncidents();
  }

  // 带上看到的那一条的 ts：这期间又跳了一次的话服务端不收，新的那次中控还没看到。
  // 发出去了才先在本页收起（等服务端的 clear 回来也是同样的结果）；连接断着时发不出去，
  // 就不收——不然这一页看着关掉了，服务端那条还在，重连后又冒出来
  function dismissIncident(id) {
    var item = incidents[id];
    if (!item) return;
    if (!send({ type: "dismiss_incident", id: id, ts: item.ts })) return;
    delete incidents[id];
    drawIncidents();
  }

  function drawIncidents() {
    if (!incidentBar) return;
    incidentBar.innerHTML = "";
    var keys = Object.keys(incidents).sort(function (a, b) {
      return (incidents[a].since || 0) - (incidents[b].since || 0);
    });
    keys.forEach(function (k) {
      var row = document.createElement("div");
      var level = incidents[k].level;
      row.className = "incident " + (level === "error" ? "error" : (level === "info" ? "info" : "warn"));
      // 当数据，不当 HTML；开头自带的 🔴⚠️ 换成按级别画的图标
      setIconText(row, barIconFor(level), stripStatusEmoji(incidents[k].text));
      if (DISMISSABLE_INCIDENTS.indexOf(k) !== -1) {
        row.classList.add("dismissible");
        var closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.className = "incident-close";
        closeBtn.title = L("关闭", "Close");
        closeBtn.setAttribute("aria-label", L("关闭", "Close"));
        closeBtn.appendChild(iconEl("xmark"));
        closeBtn.addEventListener("click", function () { dismissIncident(k); });
        row.appendChild(closeBtn);
      }
      incidentBar.appendChild(row);
    });
    incidentBar.classList.toggle("hidden", keys.length === 0);
  }

  function renderHealth(msg) {
    // 「识别落后/已降级」只对正在进行的这一场成立；停止或出错回到首页后，
    // 晚到的一条不该再把它揭开
    if (msg.level === "ok" || !streamActive) {
      healthBar.classList.add("hidden");
      return;
    }
    setIconText(healthBar, barIconFor(msg.level), stripStatusEmoji(msg.text || ""));
    healthBar.classList.remove("hidden");
    healthBar.classList.toggle("degraded", msg.level === "degraded");
  }

  // 首页的违禁词监控状态。词表默认为空，用户不看到这个就不知道要去配
  // 「最近直播间」：按用户要求只显示主播名字，不铺一长串地址。点一下就把
  // 地址填进输入框并开始——中控启动后不必每次重新粘。空列表整块隐藏。
  // 存一份供换主播面板的最近直播间列表复用（renderSwitchRecentList）——
  // 两个面板显示同一份数据，但点击行为完全不同（这里直接开始，那边只武装）
  var recentRoomsEntries = [];

  function renderRecentRooms(entries) {
    recentRoomsEntries = Array.isArray(entries) ? entries : [];
    if (recentRooms && recentList) {
      recentList.innerHTML = "";
      if (!recentRoomsEntries.length) {
        recentRooms.classList.add("hidden");
      } else {
        recentRoomsEntries.forEach(function (e) {
          if (!e || !e.streamer || !e.url) return;
          var chip = document.createElement("button");
          chip.className = "recent-chip";
          chip.type = "button";
          markName(chip);                           // 整个 chip 只有主播名；title 仍要双语（R7：自己的属性不豁免）
          chip.textContent = "@" + e.streamer;      // textContent：主播名当数据，不当 HTML
          chip.title = L("点击开始翻译 @" + e.streamer, "Start with @" + e.streamer);
          chip.addEventListener("click", function () {
            roomInput.value = e.url;                // 地址在背后填好，界面上只见主播名
            // 「下拉显示什么就发什么」：本页手动改过品牌下拉就沿用当前值，
            // 不管点的是哪个主播的 chip；没改过才按这个主播记住的品牌刷新
            // （brandForChipClick，见 web/brand.js）。这里不再走
            // onRoomInputChanged/brandStateAfterRoomInput——那条规则是给
            // 「敲键盘改地址」用的，chip 点击是另一套规则。主播名过
            // streamerFromInput 而不是直接用 e.streamer：后者是 URL 里的原始
            // 大小写（provenance.streamer_of 不转小写），brandState.streamer
            // 要和「手打输入」那条路径存的值大小写一致，否则点完 chip 再手打
            // 同一个主播会被误判成「换了主播」
            var decision = brandForChipClick(brandState, streamerFromInput(e.url),
              brandSel ? brandSel.value : "", brandsMap);
            brandState = decision.state;
            if (brandSel) selectBrand(brandSel, decision.brand);
            startStream();
          });
          recentList.appendChild(chip);
        });
        recentRooms.classList.remove("hidden");
      }
    }
    renderSwitchRecentList();
  }

  // 两段式确认，做法同顶栏「一键更新」（resetUpdateBtn/updateBtn 那一对）：
  // 第一次点只改文案、6 秒后自动复位，第二次点在窗口内才真的发送。这份列表
  // 是「一键开始」的唯一数据来源，误触清空的代价不小，原来一点就清、连按钮
  // 本身还是最显眼的系统蓝，是最容易被误触的地方之一（pm.md #5）
  function resetRecentClear() {
    if (recentClearConfirmTimer) clearTimeout(recentClearConfirmTimer);
    recentClearConfirmTimer = null;
    if (recentClear) {
      delete recentClear.dataset.confirm;
      recentClear.textContent = L("清除记录", "Clear History");
    }
  }
  if (recentClear) {
    recentClear.addEventListener("click", function () {
      if (recentClear.dataset.confirm !== "1") {
        recentClear.dataset.confirm = "1";
        recentClear.textContent = L("再点一次清除", "Click Again to Clear");
        recentClearConfirmTimer = setTimeout(resetRecentClear, 6000);
        return;
      }
      resetRecentClear();
      send({ type: "clear_recent_rooms" });     // 服务端清空并广播空列表回来
    });
  }

  // 磁盘空间卡：展开时才向服务端要盘点（要 walk 几十 GB 的缓存目录），
  // 勾选后「删除所选」先弹确认，列出每一项和体积——删的是几 GB 的模型和
  // 合规证据，没有回头路
  var diskItems = [];

  function humanSize(n) {
    if (n == null) return "";
    if (n >= 1024 * 1024 * 1024) return (n / 1073741824).toFixed(1) + " GB";
    if (n >= 1024 * 1024) return (n / 1048576).toFixed(0) + " MB";
    return Math.max(1, Math.round(n / 1024)) + " KB";
  }

  function renderDisk(info) {
    if (!diskList || !info) return;
    diskItems = Array.isArray(info.items) ? info.items : [];
    var total = 0;
    diskItems.forEach(function (it) { total += it.size || 0; });
    diskSummary.textContent = (info.free != null ? L("剩余 " + humanSize(info.free) + " · ",
                                                     humanSize(info.free) + " available · ") : "")
      + L("本机模型与日志 " + humanSize(total), "Models and logs " + humanSize(total));
    diskSummary.title = diskSummary.textContent;   // 摘要被截断时补全文，见 designer.md #6
    diskList.innerHTML = "";
    if (!diskItems.length) {
      diskList.textContent = L("没有找到可管理的模型或日志。", "No models or logs to manage.");
      syncDiskButton();
      return;
    }
    // 三个词与 index.html #disk-hint 那行说明用词逐字一致
    var ROLE = { in_use: L("正在用", "In use"), app: L("本程序", "This app"), other: L("非本程序", "Not this app") };
    diskItems.forEach(function (it) {
      var row = document.createElement("label");
      row.className = "disk-item role-" + it.role;
      var box = document.createElement("input");
      box.type = "checkbox";
      box.value = it.id;
      box.disabled = it.role === "in_use";
      box.addEventListener("change", syncDiskButton);
      var name = document.createElement("span");
      name.className = "disk-name";
      // 模型（hf/ollama）的标签是模型名，按名字标；日志两项（kind=logs）和识别音频片段（kind=evidence）
      // 的标签是后端写的界面句子，要能换成英文——按 kind 分，不加新字段（spec §6、§17 第 6 条）
      if (it.kind !== "logs" && it.kind !== "evidence") markName(name);
      name.textContent = it.label;                 // textContent：模型名当数据，不当 HTML
      var tag = document.createElement("span");
      tag.className = "disk-role";
      tag.textContent = ROLE[it.role] || it.role;
      var size = document.createElement("span");
      size.className = "disk-size";
      size.textContent = humanSize(it.size);
      var note = document.createElement("span");
      note.className = "disk-note";
      note.textContent = it.note || "";
      row.appendChild(box); row.appendChild(name); row.appendChild(tag);
      row.appendChild(size); row.appendChild(note);
      diskList.appendChild(row);
    });
    syncDiskButton();
  }

  function diskSelected() {
    var out = [];
    diskList.querySelectorAll("input[type=checkbox]:checked").forEach(function (b) {
      out.push(b.value);
    });
    return out;
  }

  function syncDiskButton() {
    if (!diskDelete) return;
    var ids = diskSelected();
    var bytes = 0;
    diskItems.forEach(function (it) { if (ids.indexOf(it.id) !== -1) bytes += it.size || 0; });
    diskDelete.disabled = ids.length === 0;
    diskDelete.textContent = ids.length
      ? LN(ids.length, "删除所选（" + ids.length + " 项，" + humanSize(bytes) + "）",
           "Delete 1 Item (" + humanSize(bytes) + ")",
           "Delete " + ids.length + " Items (" + humanSize(bytes) + ")") : L("删除所选", "Delete Selected");
  }

  if (diskHead) {
    diskHead.addEventListener("click", function () {
      var open = diskBody.classList.contains("hidden");
      setRowOpen(diskHead, diskBody, open);
      if (open) {
        diskList.textContent = L("正在统计…", "Calculating…");
        send({ type: "disk_inventory" });
      }
    });
  }
  if (diskDelete) {
    diskDelete.addEventListener("click", function () {
      var ids = diskSelected();
      if (!ids.length) return;
      var lines = diskItems.filter(function (it) { return ids.indexOf(it.id) !== -1; })
        .map(function (it) {
          return L("· " + it.label + "（" + humanSize(it.size) + "）", "· " + it.label + " (" + humanSize(it.size) + ")");
        });
      if (!window.confirm(L("确定删除以下内容？删了就没有了。\n\n", "Delete these items? This can’t be undone.\n\n") +
                          lines.join("\n"))) return;
      diskDelete.disabled = true;
      diskDelete.textContent = L("删除中…", "Deleting…");
      send({ type: "disk_delete", ids: ids });
    });
  }

  // ---- 手机同看卡片（#share-card）----
  // 状态 1/2/3/6/7 的文案是这里用结构化字段（url/viewers/ips/ambiguous）拼出来的；
  // 状态 4/5/8（没读到局域网地址 / 端口被占用，带原始报错 / 二维码生成失败）用的是
  // server.viewer.note——那几句话依赖后端才知道的事实（真实的 OSError 文本、qr.encode
  // 是否抛了异常），前端原样显示，不二次改写、不重新猜一遍原因（规则八）。
  var shareOn = false;        // 上一次渲染出来的开关状态，用来判定「刚打开」（A8 一次性提示）
  var shareLastIp = null;     // 上一次渲染用的局域网地址，用来判定「地址变了」（A9）
  var shareLastUrl = null;    // url 变了（如换了链接）就重置用户手选的候选地址
  var shareSelectedIp = null; // 多地址时（A10）用户点选的那个，默认跟 state.ip 一致
  var lastShareState = null;

  // 把 state.url 里的 host 换成另一个候选地址，端口和 #k=token 原样保留——
  // 手机同看不下发裸 token，只下发拼好的完整 URL，多地址靠字符串替换而不是重新拼接
  function buildViewerUrl(url, ip) {
    if (!url || !ip) return url || "";
    return url.replace(/^(https?:\/\/)[^/:#?]+(:\d+)?/, function (m, proto, port) {
      return proto + ip + (port || "");
    });
  }

  function fallbackCopyText(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* 复制失败不影响同看本身 */ }
    document.body.removeChild(ta);
  }
  function copyShareUrl(text) {
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(function () { fallbackCopyText(text); });
    } else {
      fallbackCopyText(text);
    }
  }

  // 本机有多个网络地址（A10）：列出全部候选，点了就用那个地址重画二维码/URL
  function drawShareIpList(ips, activeIp) {
    if (!shareIpList) return;
    shareIpList.innerHTML = "";
    ips.forEach(function (ip) {
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "share-ip-btn" + (ip === activeIp ? " active" : "");
      btn.textContent = ip;            // 地址当数据，不当 HTML
      btn.addEventListener("click", function () {
        shareSelectedIp = ip;
        if (lastShareState) renderShare(lastShareState);   // 先改文字，给个即时反馈
        // 二维码是服务端按 ip 生成的矩阵，光改前端的 URL 文本对不上——真正把
        // 二维码换成这个地址的，要让后端用它重新 qr.encode 一遍（app/pipeline.py
        // 的 _pick_viewer_ip），下一条 viewer 广播回来才是文字与二维码一致的那份
        send({ type: "viewer_pick_ip", ip: ip });
      });
      li.appendChild(btn);
      shareIpList.appendChild(li);
    });
  }

  // 顶栏按钮只反映「同看是否打开」这一件事，跟面板本身是否展开无关——
  // 中控可能收起面板但没关同看，这时按钮要接着显示「已打开」
  // 有人在看时写人数（shareButtonText，web/live-ui.js）；文字写进 #share-btn-text，
  // 不再整段覆盖按钮（按钮里还有图标和窄窗口用的人数）
  function updateShareBtn(on, viewers) {
    if (!shareBtn) return;
    var label = shareButtonText(on, viewers);
    if (shareBtnText) shareBtnText.textContent = label.text;
    if (shareBtnCount) {
      shareBtnCount.textContent = label.count;
      shareBtnCount.classList.toggle("hidden", !label.count);
    }
    shareBtn.classList.toggle("on", on);
  }

  function renderShare(state) {
    if (!shareToggle || !state) return;
    lastShareState = state;
    var on = !!state.on;
    var justOpened = on && !shareOn;
    updateShareBtn(on, state.viewers);

    // 顶栏按钮同一个状态写的是「已打开」（updateShareBtn），这里跟着改成
    // 「已打开/未打开」，别再各写各的（pm.md #3）
    shareState.textContent = on ? L("已打开", "On") : L("未打开", "Off");
    shareState.className = "share-state " + (on ? "on" : "off");
    shareToggle.classList.toggle("hidden", on);   // 打开后靠卡片里的「关闭」按钮，不重复放一个

    if (state.url !== shareLastUrl) {
      shareSelectedIp = null;   // 链接变了（换了链接/重新打开）：候选地址的手选状态失效
      shareLastUrl = state.url;
    }

    if (!on) {
      shareBody.classList.add("hidden");
      // note 有内容时是刚失败的一次尝试（状态 5：端口被占，带真实报错）；否则是普通关闭说明
      // 状态字已经写着「未打开」，这句不再以「关闭。」开头
      shareDesc.textContent = state.note ||
        L("打开后，连着同一个 Wi-Fi 的手机可以扫码看字幕和报警，只能看，不能操作本程序。",
          "When this is on, phones on the same Wi-Fi can scan to view captions and alerts. " +
          "They can only view, not control the app.");
      shareLastIp = null;
      shareOn = false;
      return;
    }

    shareBody.classList.remove("hidden");

    if (!state.ip) {
      // 状态 4：没读到局域网地址，没有二维码
      shareDesc.textContent = state.note ||
        L("已打开，但没读到本机的局域网地址。", "On, but the app couldn’t find this computer’s local network address.");
      shareUrl.textContent = "";
      shareQr.classList.add("hidden");
      shareIpList.classList.add("hidden");
      shareCount.textContent = "";
      shareNote.classList.add("hidden");
    } else {
      var activeIp = shareSelectedIp || state.ip;
      var url = buildViewerUrl(state.url, activeIp) || state.url || "";
      var n = state.viewers || 0;
      var max = state.max_viewers || 12;
      shareDesc.textContent = L("已打开。手机连同一个 Wi-Fi，扫下面的二维码，或直接打开：" + url,
        "On. On a phone connected to the same Wi-Fi, scan the QR code or open: " + url);
      shareUrl.textContent = url;      // 大号可选中：用户可以直接长按复制，不必点按钮
      shareUrl.title = L("这个链接里带着一把钥匙，当密码看待；发给谁，谁就能看到字幕和报警。",
        "This link contains an access key. Treat it like a password. Anyone who has it can see captions and alerts.");
      // 英文 “N watching” 单复数同形（docs/i18n-style.md §2.3），不用 LN
      shareCount.textContent = L("当前 " + n + " 人在看，最多 " + max + " 人。", n + " watching (limit " + max + ")");

      var ok = (typeof renderQR === "function") && renderQR(shareQr, state.qr_rows, 220);
      shareQr.classList.toggle("hidden", !ok);
      if (!ok) {
        // 状态 8：二维码没能生成——note 是后端已经拼好的那句话
        shareNote.textContent = state.note ||
          L("二维码没能生成，请让手机手工输入上面的地址。", "Couldn’t create the QR code. Type the address above on the phone.");
        shareNote.classList.remove("hidden");
      } else {
        shareNote.classList.add("hidden");
      }

      if (state.ambiguous && Array.isArray(state.ips) && state.ips.length > 1) {
        shareIpList.classList.remove("hidden");
        drawShareIpList(state.ips, activeIp);
      } else {
        shareIpList.classList.add("hidden");
      }
    }

    // A9：地址变了。只在「已经打开着」的连续期间比较，刚打开的这一次不算「变了」
    if (shareLastIp && state.ip && state.ip !== shareLastIp) {
      shareAddrChanged.textContent = L("本机地址已从 " + shareLastIp + " 变为 " + state.ip +
        "，之前发出去的链接需要重新扫码",
        "This computer’s address changed from " + shareLastIp + " to " + state.ip +
        ". Phones need to scan the new QR code.");
      shareAddrChanged.classList.remove("hidden");
    } else {
      shareAddrChanged.classList.add("hidden");
    }
    if (state.ip) shareLastIp = state.ip;

    // A8：从关到开的这一刻，一次性提示系统可能弹出的网络权限确认框；
    // 覆盖掉上面刚设的正常描述，下一次真实状态广播到达（如 A9 的地址重发）会把它换回来
    if (justOpened) {
      shareDesc.textContent = L("第一次打开时，系统可能弹出是否允许接受网络连接的确认框（macOS）" +
        "或防火墙提示（Windows），请选允许。",
        "The first time you turn this on, macOS may ask to allow incoming network connections, " +
        "or Windows Firewall may ask for access. Choose Allow.");
    }
    shareOn = on;
  }

  // ---- 顶栏浮层（手机同看 / 换主播）的共用开合 ----
  // 两个浮层互斥；位置按触发按钮右边缘算（popoverRight）；aria-expanded 跟着走。
  // 关闭的途径：按钮再点一次、×、Esc、点浮层外。不在窗口失焦（blur/focusout）时
  // 关——中控要切到浏览器复制新主播的地址再回来粘，浮层得还在
  function isPopoverOpen(panel) { return !!panel && !panel.classList.contains("hidden"); }
  function placePopover(panel, trigger) {
    if (!isPopoverOpen(panel) || !trigger) return;
    var r = trigger.getBoundingClientRect();
    panel.style.right = popoverRight(r.right, window.innerWidth, panel.offsetWidth, 12) + "px";
  }
  function showPopover(panel, trigger) {
    if (panel === switchPanel) closeSharePanel(); else closeSwitchPanel();
    panel.classList.remove("hidden");
    if (trigger) trigger.setAttribute("aria-expanded", "true");
    placePopover(panel, trigger);
  }
  function hidePopover(panel, trigger) {
    if (!panel) return;
    panel.classList.add("hidden");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
  }
  window.addEventListener("resize", function () {
    placePopover(sharePanel, shareBtn);
    placePopover(switchPanel, switchBtn);
  });
  // 捕获阶段：浮层里的按钮自己的 click 照常触发；点在触发按钮上不算「外面」，
  // 交给按钮自己的切换（否则这里先关、按钮的 click 又打开）
  document.addEventListener("pointerdown", function (e) {
    var t = e.target;
    if (isPopoverOpen(switchPanel) && !switchPanel.contains(t) && !(switchBtn && switchBtn.contains(t))) {
      closeSwitchPanel();
    }
    if (isPopoverOpen(sharePanel) && !sharePanel.contains(t) && !(shareBtn && shareBtn.contains(t))) {
      closeSharePanel();
    }
  }, true);

  // 不主动挪焦点：浮层在 DOM 里紧跟着 #share-btn，键盘用户按 Tab 就进去了；
  // 鼠标用户点开后焦点跳到某个按钮上反而会亮出一圈焦点环
  function openSharePanel() {
    if (!sharePanel) return;
    showPopover(sharePanel, shareBtn);
  }
  // 顶栏按钮只管面板的展开/收起，不碰同看开关本身——同看是否广播局域网端口
  // 完全由卡片里的「打开/关闭」决定，两件事故意分开，收起面板不应该顺手断掉正在看的手机
  if (shareBtn && sharePanel) {
    shareBtn.addEventListener("click", function () {
      if (isPopoverOpen(sharePanel)) closeSharePanel(); else openSharePanel();
    });
  }
  if (shareToggle) {
    shareToggle.addEventListener("click", function () {
      send({ type: "viewer_share", on: true });
    });
  }
  if (shareClose) {
    shareClose.addEventListener("click", function () {
      send({ type: "viewer_share", on: false });
      // 浮层也一起收起：点「停止同看」的人是要结束同看
      closeSharePanel();
    });
  }
  // 卡片右上角的 ×：只收起面板，不发 viewer_share——跟上面「停止同看」故意
  // 是两个控件，别把两件事并回一个按钮（pm.md #3；closeSharePanel 给 Esc 复用）
  function closeSharePanel() {
    hidePopover(sharePanel, shareBtn);
  }
  if (shareCollapse) {
    shareCollapse.addEventListener("click", closeSharePanel);
  }
  if (shareCopy) {
    shareCopy.addEventListener("click", function () {
      copyShareUrl(shareUrl.textContent);
    });
  }
  if (shareRotate) {
    shareRotate.addEventListener("click", function () {
      var n = (lastShareState && lastShareState.viewers) || 0;
      // 状态 6：换链接确认——发出去的旧链接立刻失效，在看的手机全部断开重扫。
      // 文案来自服务端 viewer 载荷的 rotate_confirm（唯一出处是 app/viewer.py
      // 的 NOTE_ROTATE_CONFIRM），这里只补上当下人数，不再自己存一份重复文案。
      // 兜底与后端 NOTE_ROTATE_CONFIRM 同一句；英文用不变形的句式：{n} 到这里才填，后端选不了单复数
      var tmpl = (lastShareState && lastShareState.rotate_confirm) ||
        L("换链接之后，现在在看的 {n} 台手机会断开，要重新扫码。继续？",
          "Changing the link disconnects the phones watching now ({n}). They’ll need to scan the new QR code. Continue?");
      if (!window.confirm(tmpl.replace("{n}", String(n)))) return;
      send({ type: "viewer_rotate" });
    });
  }

  // ---- 换主播 ----
  // 直播中不停止监听、直接改听另一个主播。两段式确认的状态机在 web/switch.js
  // （可测，tests/switch.test.mjs 钉住规则），这里只管 DOM：面板开合、最近
  // 直播间 chip、品牌/主播语言的回显、拼包发送。
  var switchArmState = initialSwitchState();
  var switchResetTimer = null;

  // 当前正在监听的主播名，和顶栏「直播中 · @A」取的是同一个来源
  // （config.room_url，经 streamerFromInput 提取）
  function currentStreamerName() {
    return streamerFromInput(roomInput.value);
  }

  function clearSwitchResetTimer() {
    if (switchResetTimer) clearTimeout(switchResetTimer);
    switchResetTimer = null;
  }

  function showSwitchError(text) {
    if (!switchError) return;
    switchError.textContent = text;
    switchError.classList.remove("hidden");
  }
  function clearSwitchError() {
    if (!switchError) return;
    switchError.textContent = "";
    switchError.classList.add("hidden");
  }

  function renderSwitchButton() {
    if (!switchConfirmBtn) return;
    var label = switchButtonLabel(switchArmState, currentStreamerName());
    switchConfirmBtn.textContent = label.text;
    // 发出去的「开始」还没等到服务器接管（pendingStart 非空）时也保持禁用：
    // 换主播面板只在直播中/连接中才能打开，这时唯一可能在途的 pendingStart
    // 只会是换主播自己刚发的那条（开始面板此刻已经隐藏，发不出新的）
    switchConfirmBtn.disabled = label.disabled || !!pendingStart;
    if (switchHint) {
      switchHint.textContent = switchArmState.target
        ? L("切换期间两个主播都没有字幕，直到 @" + switchArmState.target + " 出现第一句。",
            "No captions from either streamer until @" + switchArmState.target + " starts speaking.")
        : L("切换期间两个主播都没有字幕，直到新主播出现第一句。",
            "No captions from either streamer until the new streamer starts speaking.");
    }
  }

  // 输入框变化：只「武装」第一步，绝不直接发送（见 web/switch.js）。品牌
  // 默认值的刷新规则和开始面板「敲键盘改地址」那一路一致
  // （brandStateAfterRoomInput），只是认的是换主播面板自己这份 state 和输入框——
  // 这里换的是「要听谁」（@B），不是「正在听谁」（@A），两份状态不能混。
  // chip 点击走 armSwitchFromChip，规则不同（见其注释）
  function armSwitchFromInput() {
    var streamer = streamerFromInput(switchInput.value);
    switchArmState = armSwitch(streamer, currentStreamerName(), Date.now());
    switchBrandState = brandStateAfterRoomInput(switchBrandState, streamer);
    applySwitchDefaultBrand();
    clearSwitchError();
    renderSwitchButton();
    startSwitchResetTimer();
  }

  // 最近直播间 chip：同样只「武装」，不直接发送，但品牌那部分不能复用
  // brandStateAfterRoomInput——那条规则一遇到「主播变了」就清 touched，点
  // chip 换主播必然「变了」，手动选的品牌会被立刻冲掉。这里和开始面板的
  // chip 一样走「下拉显示什么就发什么」（brandForChipClick，见
  // web/brand.js）：本页手动改过换主播面板的下拉就沿用当前值，没改过才按
  // 这个 chip 对应主播记住的品牌刷新
  function armSwitchFromChip(streamer) {
    switchArmState = armSwitch(streamer, currentStreamerName(), Date.now());
    var decision = brandForChipClick(switchBrandState, streamer,
      switchBrandSel ? switchBrandSel.value : "", brandsMap);
    switchBrandState = decision.state;
    if (switchBrandSel) selectBrand(switchBrandSel, decision.brand);
    clearSwitchError();
    renderSwitchButton();
    startSwitchResetTimer();
  }

  // 6 秒未确认自动复位：不能只靠 shouldConfirmSwitch 兜底判断——不然过期后
  // 按钮文案会一直停在「再点一次…」，看着像还能点，实际点了也没反应。
  // 复位只解除武装、保留目标（expireSwitchState），按钮回到可点的「换到 @B」
  function startSwitchResetTimer() {
    clearSwitchResetTimer();
    if (!switchArmState.armed) return;
    switchResetTimer = setTimeout(function () {
      switchArmState = expireSwitchState(switchArmState, Date.now() + SWITCH_ARM_MS);
      renderSwitchButton();
    }, SWITCH_ARM_MS);
  }

  // 最近直播间 chip：数据同开始面板（renderRecentRooms 存的 recentRoomsEntries），
  // 但点击行为不同——这里只填入并武装，绝不直接开始，直播中误触一下不该立刻断流。
  // 当前正在监听的那个主播标「当前」且不可点。
  function renderSwitchRecentList() {
    if (!switchRecent || !switchRecentList) return;
    switchRecentList.innerHTML = "";
    if (!recentRoomsEntries.length) { switchRecent.classList.add("hidden"); return; }
    var cur = currentStreamerName();
    recentRoomsEntries.forEach(function (e) {
      if (!e || !e.streamer || !e.url) return;
      var chip = document.createElement("button");
      chip.className = "recent-chip";
      chip.type = "button";
      var isCurrent = !!cur && e.streamer.toLowerCase() === cur.toLowerCase();
      if (isCurrent) {
        chip.disabled = true;
        // 主播名是名字，「（当前）」是界面文字：只包前一截。外面再套一个 span，理由同报警头——
        // .recent-chip 是 inline-flex，原来整段文字是一个匿名 flex 项
        var curText = document.createElement("span");
        curText.appendChild(nameSpan("@" + e.streamer));
        curText.appendChild(document.createTextNode(L("（当前）", " (current)")));
        chip.appendChild(curText);
      } else {
        markName(chip);
        chip.textContent = "@" + e.streamer;      // textContent：主播名当数据，不当 HTML
        chip.title = L("填入并武装改听 @" + e.streamer + "（还要再点一次确认才会真的换）",
                       "Select @" + e.streamer + ". You’ll still need to confirm.");
        chip.addEventListener("click", function () {
          switchInput.value = e.url;
          armSwitchFromChip(streamerFromInput(e.url));   // 大小写规则同开始面板的 chip
          switchInput.focus();
        });
      }
      switchRecentList.appendChild(chip);
    });
    switchRecent.classList.remove("hidden");
  }

  function openSwitchPanel() {
    if (!switchPanel) return;
    switchInput.value = "";
    switchArmState = initialSwitchState();
    switchBrandState = { streamer: "", touched: false };
    clearSwitchResetTimer();
    clearSwitchError();
    if (switchBrandSel) switchBrandSel.value = "";
    var cur = currentStreamerName();
    if (switchSub) {
      // 「当前 @A」跟最近直播间 chip 的「（当前）」用同一个词，不再说「监听中」——
      // 顶栏是「直播中 · @A」，这里以前的「当前监听」/chip 的「监听中」是第三种
      // 说法，混用容易让人以为指的不是同一件事（pm.md #7）
      switchSub.textContent = cur
        ? L("当前 @" + cur + "，确认前不会中断", "Now monitoring @" + cur + ". Nothing changes until you confirm.")
        : L("确认前不会中断当前监听", "Monitoring continues until you confirm.");
    }
    if (switchSourceEcho) {
      var opt = sourceSel.options[sourceSel.selectedIndex];
      switchSourceEcho.textContent = opt ? opt.textContent : sourceSel.value;
    }
    renderSwitchRecentList();
    renderSwitchButton();
    showPopover(switchPanel, switchBtn);
    switchInput.focus();
  }

  // 关闭＝作废这一轮：定时器清掉，下次打开 openSwitchPanel 从头重置输入与武装状态，
  // 点浮层外关掉也不会留下一个「再点一次就换」的半截状态
  function closeSwitchPanel() {
    if (!switchPanel) return;
    hidePopover(switchPanel, switchBtn);
    clearSwitchResetTimer();
  }

  // 确认按钮点击、或输入框按 Enter：只有 shouldConfirmSwitch 判定「这一下算数」
  // 才真的往下走。本地校验失败（认不出输入、连接断开）的报错显示在面板里，
  // 绝不调用 setStatus——那会把直播中的界面切成非直播状态（既有教训，
  // 见 startStream 对同一类错误的处理方式，这里不能共用因为不能动 setStatus）
  function attemptSwitchConfirm() {
    var now = Date.now();
    var cur = currentStreamerName();
    var action = switchClickAction(switchArmState, now, cur);
    if (action === "arm") {
      // 超时复位过（或已过期）：这一下只重新武装，下一下才发送
      switchArmState = armSwitch(switchArmState.target, cur, now);
      renderSwitchButton();
      startSwitchResetTimer();
      return;
    }
    if (action !== "confirm") return;
    var parsed = parseRoomInput(switchInput.value);
    if (parsed.error) {
      showSwitchError(UNRECOGNIZED_INPUT_MSG);
      return;
    }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      showSwitchError(L("与本地服务断开，正在重连——稍候再试。",
                        "Lost connection to the local service. Reconnecting… Try again in a moment."));
      return;
    }
    clearSwitchResetTimer();
    // 换主播成功后 localStorage 也要跟着换，否则刷新页面时页面加载那段
    // 「savedRoom 非空就回填 roomInput.value」会用旧主播的地址把输入框填上，
    // hello 处理里 `!roomInput.value` 那个兜底判断见它非空就不再信服务器的
    // config.room_url——界面就会显示回换之前的主播（同 startStream 里
    // lsSet("roomUrl", ...) 的道理，这里之前漏了）
    lsSet("roomUrl", parsed.url);
    sendStartCommand(buildStartPayload(parsed.url, parsed.media, sourceSel.value,
      alertsToggle && alertsToggle.checked, switchBrandSel ? switchBrandSel.value : ""));
    // 同开始面板的 startStream：发出 start 之后清掉 touched（面板下次打开
    // 会整个重置，这里做只是让规则在两个面板上一致，不依赖「反正会重置」）
    switchBrandState = brandStateAfterStart(switchBrandState);
    switchArmState = initialSwitchState();
    closeSwitchPanel();
  }

  if (switchBtn) {
    switchBtn.addEventListener("click", function () {
      if (isPopoverOpen(switchPanel)) closeSwitchPanel(); else openSwitchPanel();
    });
  }
  if (switchClose) switchClose.addEventListener("click", closeSwitchPanel);
  if (switchInput) {
    switchInput.addEventListener("input", armSwitchFromInput);
    switchInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") attemptSwitchConfirm();
    });
  }
  if (switchConfirmBtn) switchConfirmBtn.addEventListener("click", attemptSwitchConfirm);
  // Esc 关闭：只在面板确实打开时处理，不吞掉页面别处的 Esc（没有别处在用）。
  // 同看面板走 closeSharePanel——只收起，不碰同看开关，跟卡片里的 × 同一个函数（pm.md #3）
  // 关掉后焦点回到触发按钮，键盘用户不会掉到页面开头
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (isPopoverOpen(switchPanel)) {
      closeSwitchPanel();
      if (switchBtn) switchBtn.focus();
    } else if (isPopoverOpen(sharePanel)) {
      closeSharePanel();
      if (shareBtn) shareBtn.focus();
    }
  });

  // 违禁词警示开关：本地状态 + 顶栏标签 + 首页描述文案，三处一起同步。
  // 来源可能是用户手动扳开关、也可能是后端 hello/alert_mode 广播——不区分来源，
  // 一律走这一个函数，保证三处永远一致。
  function setAlertsEnabled(on) {
    alertsEnabled = !!on;
    if (alertsToggle) alertsToggle.checked = alertsEnabled;
    // 统一叫「报警」：这一行摘要「开启/关闭 · 词表 N 条」，跟设置行名称
    // 「违禁词报警」、顶栏标签「报警开」用同一个词，不再是「警示」（pm.md #7）
    var sum = watchSummary(alertsEnabled, watchlistCount);
    if (watchMode) watchMode.textContent = sum.mode;
    if (watchDesc) watchDesc.textContent = sum.desc;
    updateAlertModeTag();
  }

  // 顶栏标签：只在「连接中/直播中且报警开着」才露出。关闭是默认值，常驻显示
  // 一个「警示关」既占顶栏空间（默认窗口下顶栏本就放不下一行，见 designer.md #3），
  // 对新人也是看不懂的术语；不像开关本身，这里没有「什么都不显示」会被误解的
  // 风险——顶栏别的地方也不会暗示这个功能存在（pm.md #4）
  function updateAlertModeTag() {
    if (!alertModeTag) return;
    var show = streamActive && alertsEnabled;
    alertModeTag.classList.toggle("hidden", !show);
    if (show) alertModeTag.textContent = L("报警开", "Alerts on");
    // 标签在 .brand 里，「浮窗」按钮在 .controls 里，样式选不到兄弟的兄弟：标签亮着时由 body 的类告诉样式表，
    // 顶栏已经没有余量给「浮窗」两个字（style.css 的 has-alert-tag）
    document.body.classList.toggle("has-alert-tag", !!show);
  }

  // 本场品牌标签：config.active_brand（{id, name} 或 null）来自 hello/config
  // 广播，见 app/pipeline.py _active_brand_info。只存显示名——标签只负责
  // 显示，id 用不上。hello 每次都带真实值（同 alerts_enabled），不猜测、
  // 不沿用上一次连接看到的值，重连/换设备也不会显示错主播的品牌
  var activeBrandName = "";
  function setActiveBrand(info) {
    activeBrandName = info && typeof info === "object" && typeof info.name === "string"
      ? info.name : "";
    updateActiveBrandTag();
  }

  // 只在连接中/直播中且这一场选了品牌时露出（跟 updateAlertModeTag 同一个
  // 逻辑），过长的名字交给 CSS text-overflow 省略号，这里把完整名字放进
  // title 供悬停查看
  function updateActiveBrandTag() {
    if (!activeBrandTag) return;
    var show = streamActive && !!activeBrandName;
    activeBrandTag.classList.toggle("hidden", !show);
    if (show) {
      activeBrandTag.textContent = L("品牌 · ", "Brand · ");
      activeBrandTag.appendChild(nameSpan(activeBrandName));   // 品牌名（标签是 inline-block，省略号照样生效）
      activeBrandTag.title = activeBrandName;
    }
  }

  // 词表状态、开关状态搭配出来的说明句由 watchSummary（settings-rows.js）统一算，
  // count<=0 时的「未配置」文案跟开关状态无关——没有词就永远不会命中，这句话
  // 本身已经说清楚了，watchSummary 内部已经处理了这条分支，这里不用再分两路写。
  function renderWatchlist(msg) {
    if (!watchState) return;
    watchlistCount = msg.count || 0;
    // 不再切 .watch-state.on/.off——旧版靠这两个类换色，这次改版的 style.css
    // 里 .watch-state 从未定义任何样式（词表状态只用文字说明，见 pm.md #7 附带
    // 发现），继续写这两个类只是死代码（engineer.md #7）
    var sum = watchSummary(alertsEnabled, watchlistCount);
    watchState.textContent = sum.state;
    if (watchDesc) watchDesc.textContent = sum.desc;
  }

  // 自检总览图标：颜色和图形一起变，不是只换背景色——「自检中…」还没出结果时
  // 就已经显示绿底对勾，是这次要修的自相矛盾（designer.md #1）。四态都画在
  // 同一个盾牌轮廓上，内部的勾/叹号/叉用状态自己的颜色（跟 .set-icon.<level>
  // 的背景色一致），在纯白盾牌上「抠」出一个同色标记，效果上等价于 SF Symbols
  // 的 xxx.shield.fill 二色画法，不需要真的做镂空
  var SC_SHIELD_D = "M8 1.8 13 3.6v4c0 3.2-2.1 5.6-5 6.6-2.9-1-5-3.4-5-6.6v-4Z";
  var SC_ICON_SVG = {
    checking: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="' + SC_SHIELD_D + '" fill="currentColor"/></svg>',
    pass: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="' + SC_SHIELD_D + '" fill="currentColor"/>' +
      '<path d="M5.8 8 7.4 9.6 10.3 6.5" fill="none" stroke="var(--green)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    warn: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="' + SC_SHIELD_D + '" fill="currentColor"/>' +
      '<path d="M8 5.2v3.4" stroke="var(--orange)" stroke-width="1.6" stroke-linecap="round"/>' +
      '<circle cx="8" cy="10.6" r=".65" fill="var(--orange)"/></svg>',
    fail: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="' + SC_SHIELD_D + '" fill="currentColor"/>' +
      '<path d="M6.1 6.1 9.9 9.9M9.9 6.1 6.1 9.9" stroke="var(--red)" stroke-width="1.6" stroke-linecap="round"/></svg>'
  };
  // level 是背景色（.set-icon.<level>），跟上面 SVG 用哪一种内部标记一一对应
  var SC_ICON_LEVEL = { checking: "gray", pass: "green", warn: "orange", fail: "red" };
  function setSelfcheckIcon(state) {
    if (!scIcon) return;
    scIcon.className = "set-icon " + (SC_ICON_LEVEL[state] || "gray");
    scIcon.innerHTML = SC_ICON_SVG[state] || SC_ICON_SVG.checking;
  }

  // 自检结果。有失败项时默认展开——「功能悄悄坏了」必须让人一眼看到，
  // 全绿时收起来不打扰
  var scLastSig = null;   // 结论没变就别动展开状态
  var SC_LEVEL_SR_TEXT = { warn: L("（提醒）", " (warning)"), fail: L("（未通过）", " (failed)") };

  function renderSelfcheck(msg) {
    if (!scBox || !msg.checks) return;
    var sum = msg.summary || {};
    scBox.classList.remove("hidden");
    scHead.classList.toggle("has-fail", sum.fail > 0);
    scHead.classList.toggle("has-warn", !sum.fail && sum.warn > 0);
    // 摘要文案 + 图标状态由 selfcheckSummary（settings-rows.js）统一算，
    // 折叠行要不要跟着自动展开见下面的 nextAutoOpen
    var scSum = selfcheckSummary(sum);
    scSummary.textContent = scSum.text;
    setSelfcheckIcon(scSum.icon);
    scSummary.title = scSummary.textContent;
    scList.innerHTML = "";
    msg.checks.forEach(function (c) {
      var li = document.createElement("li");
      li.className = "sc-item " + c.level;
      var name = document.createElement("span");
      name.className = "sc-name";
      name.textContent = c.name;          // 状态由 .sc-item.<level>::before 的圆点/圆环表示
      var detail = document.createElement("span");
      detail.className = "sc-detail";
      detail.textContent = c.detail;
      // 提醒/失败：圆点的形状已经跟通过项不一样（实心 vs 空心），但色弱看不出
      // 颜色差异时还是分不清「提醒」和「失败」——补一句读屏可读、视觉隐藏的
      // 级别说明，不额外占版面（engineer.md #3）
      if (SC_LEVEL_SR_TEXT[c.level]) {
        var srLevel = document.createElement("span");
        srLevel.className = "sr-only";
        srLevel.textContent = SC_LEVEL_SR_TEXT[c.level];
        name.appendChild(srLevel);
      }
      if (c.fix) {
        var fix = document.createElement("span");
        fix.className = "sc-fix";
        fix.textContent = c.fix;   // 不再拼「→」：这不是链接，没法点（designer.md #1）
        detail.appendChild(fix);
      }
      li.appendChild(name);
      li.appendChild(detail);
      scList.appendChild(li);
    });
    // 只有结论真的变了才自动展开/收起。重连会重放一次 hello，
    // 那时若无条件重置，正在看明细的人会被收起来。自检这一行的规则是「签名
    // 一变就无条件同步」——从「有失败」变回「全绿」也要跟着收起，所以直接用
    // r.open，不像引擎回退提示那样还要额外判断（见 nextAutoOpen 的注释）
    var r = nextAutoOpen(scLastSig, scSum.sig, sum.fail > 0);
    scLastSig = r.sig;
    if (r.changed) setSelfcheckOpen(r.open);
  }

  function setSelfcheckOpen(open) {
    setRowOpen(scHead, scList, open);
  }

  if (scHead) {
    scHead.addEventListener("click", function () {
      setSelfcheckOpen(scList.classList.contains("hidden"));
    });
  }

  if (fixCmdCopy) {
    fixCmdCopy.addEventListener("click", function () {
      var text = fixCmdText.textContent;
      var done = function () {
        fixCmdCopy.textContent = L("已复制", "Copied");
        setTimeout(function () { fixCmdCopy.textContent = L("复制", "Copy"); }, 2000);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {
          selectCommand();   // 剪贴板被拒（非 https 等）：至少帮用户选中
        });
      } else {
        selectCommand();
      }
    });
  }

  function selectCommand() {
    var range = document.createRange();
    range.selectNodeContents(fixCmdText);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    fixCmdCopy.textContent = L("已选中，按 ⌘C", "Selected. Press ⌘C");
    setTimeout(function () { fixCmdCopy.textContent = L("复制", "Copy"); }, 3000);
  }

  // ---- 字幕跟随 ----
  // 「跟随最新」是一个**用户意图**，不是每次都从几何量现算的结论。
  // 现算会在页面不可见时出错：那时 scrollHeight 和 clientHeight 都是 0，
  // `scrollTop = scrollHeight` 变成给 0 赋 0（空操作），而 0-0-0 < 120 又让
  // 判定看起来是「在底部」。于是离开页面期间到达的每一条字幕都没能滚动，
  // 回来时停在旧位置，必须手动往下拖。
  // 初始值 false：页面刚加载默认是首页状态（见 viewTransition），要等真正
  // 进入直播中（exitHomeLayout）才恢复跟随；这之前不该有任何东西把 #history
  // 拽向底部。
  var following = false;
  // 翻上去看的时候（following 为假）又来了几条新字幕，显示在「回到最新」按钮上。
  // 回到底部（点按钮、自己滚下去、清空）就归零；回放的字幕不算
  var captionsBelow = 0;
  var jumpLabel = jumpLatestCaptionsText(0);   // 与 index.html 里的静态文字相同，没变就不重写

  // 记下最近一次真实的用户输入。浏览器在元素重新可渲染时会把 scrollTop
  // 重置为 0 并抛出 scroll 事件，不区分来源的话那次重置会被当成
  // 「用户翻到了顶部」，跟随就此永久关闭。
  var lastUserInput = 0;
  ["wheel", "touchstart", "touchmove", "keydown", "mousedown"].forEach(
    function (name) {
      historyEl.addEventListener(name, function () {
        lastUserInput = Date.now();
      }, { passive: true });
    });

  historyEl.addEventListener("scroll", function () {
    following = nextFollowing(historyEl, following,
                              Date.now() - lastUserInput < 700);
    if (following) captionsBelow = 0;
    updateJumpButton();
  }, { passive: true });

  // 程序化滚到底必须是**瞬时**的。
  //
  // .history 上有 `scroll-behavior: smooth`，于是 `scrollTop = …` 是一次动画；
  // 而字幕在不断追加，每来一条就重启一次动画，动画永远追不上——实测赋值
  // 999999 之后 scrollTop 仍停在 9。页面不可渲染时更糟：动画连帧都不跑。
  // 这才是「离开页面一会儿回来后停在旧位置」的真正原因。
  //
  // `scrollTo({behavior:"auto"})` 实测也压不住已在进行的动画，所以直接把
  // scroll-behavior 临时关掉再赋值——这一步实测有效（落在精确的最大值上）。
  // 用户自己拖动仍然是平滑的，CSS 原样保留。
  function scrollToBottomNow() {
    var prev = historyEl.style.scrollBehavior;
    historyEl.style.scrollBehavior = "auto";
    historyEl.scrollTop = historyEl.scrollHeight;
    historyEl.style.scrollBehavior = prev;
  }

  // 同上，瞬时滚到顶：#start-panel 是 #history 的第一个子元素，进首页状态时
  // 要让它回到视口里。理由同 scrollToBottomNow——.history 的 scroll-behavior:
  // smooth 会让赋值变成一次追不上后续变化的动画，干脆临时关掉。
  function scrollToTopNow() {
    var prev = historyEl.style.scrollBehavior;
    historyEl.style.scrollBehavior = "auto";
    historyEl.scrollTop = 0;
    historyEl.style.scrollBehavior = prev;
  }

  function stickToBottom(force) {
    if (force) following = true;
    if (following) {
      scrollToBottomNow();
      captionsBelow = 0;
    }
    updateJumpButton();
  }

  // 进入首页状态（viewTransition 的 enterHome）：直播状态从活跃变为非活跃，
  // 或页面刚加载就是非活跃。把上一场直播留下的视觉残留收掉——大字幕、
  // 弹幕面板都是「直播中才有意义」的 UI，赖在待机页面上会压住开始面板、或
  // 让人以为还在直播（2026-09-24 用户报告的残留）。弹幕面板本身的隐藏交给
  // refreshCommentPanel（已在调用处按 streamActive 处理），这里不重复。
  function enterHomeLayout() {
    hideLiveBar();
    following = false;
    captionsBelow = 0;
    scrollToTopNow();
    updateJumpButton();
    // 识别落后提示只描述正在进行的那一场：后端停止时不会补发 level=ok，
    // 一场在落后状态下结束，这条会一直留在首页上
    healthBar.classList.add("hidden");
    // 换主播面板只在直播中有意义（按钮本身这时也被隐藏了）：停止之后若还开着，
    // 关掉它，不然会变成一块摆在待机页面上的残留浮层
    closeSwitchPanel();
    // 直播中收起着的「有新版本」「迁移旧词表」两条，回到首页再露出来
    syncNoticeBars();
  }

  // 离开首页状态（viewTransition 的 exitHome）：直播状态从非活跃变为活跃
  // （点了「开始翻译」、后端确认 connecting/live）。大字幕不在这里主动显示——
  // 它等第一条真正的字幕到达时由 renderCaption 自己揭开；这里只需要让接下来到达的新字幕照常跟到底部。
  function exitHomeLayout() {
    following = true;
    syncNoticeBars();       // 开播：「有新版本」「迁移旧词表」两条先收起（更新进行中的除外）
  }

  // 按钮只在「确实有内容在下面」时出现。不可测量时不显示——那时什么都判断不了，
  // 摆一个按不动的按钮只会添乱。非活跃状态（待机/已结束/出错）下不显示：
  // 那时 #history 顶部是开始面板，「回到最新」没有意义。
  function updateJumpButton() {
    if (!jumpBtn) return;
    var show = streamActive && isMeasurable(historyEl) && !atBottom(historyEl);
    if (show) jumpBtn.style.bottom = jumpBottomPx() + "px";
    jumpBtn.classList.toggle("hidden", !show);
    var label = jumpLatestCaptionsText(captionsBelow);
    if (label !== jumpLabel) {
      jumpLabel = label;
      setIconText(jumpBtn, "arrow-down", label);
    }
  }

  // 两颗「回到最新」（字幕区的、弹幕栏的）共用的 bottom 值：底部大字幕是 fixed 且高度随字号
  // 变化，按钮得让开它。上限夹在视口 40% 处：布局异常时测出的 barTop 可能贴近顶部，
  // 不夹的话按钮会被顶到屏幕上方，看起来像个飞出来的东西。
  function jumpBottomPx() {
    var vh = window.innerHeight || 800;
    var barTop = (liveBar && !liveBar.classList.contains("hidden"))
      ? liveBar.getBoundingClientRect().top : vh - 56;
    var offset = vh - barTop + 12;
    if (!(offset > 0)) offset = 68;                 // NaN / 负数兜底
    return Math.min(Math.max(offset, 56), vh * 0.4);
  }

  if (jumpBtn) {
    jumpBtn.addEventListener("click", function () {
      following = true;                 // 点了就是要看最新，恢复跟随
      captionsBelow = 0;
      scrollToBottomNow();
      updateJumpButton();
    });
  }

  // 重新可见/重新获得焦点时补一次：不可见期间的滚动请求全部落空了。
  // rAF 等布局稳定后再滚——刚显示出来时尺寸还没算完。
  // 到那一帧再看一眼还跟不跟：点「停止」时窗口先收到 focus，排下这一帧；停止的状态若赶在
  // 这一帧之前到达，页面已经回到首页、滚到顶（enterHomeLayout），这一帧不能再把它拽到底
  // ——10-15 实测停止后偶尔停在「上一场字幕」底部，就是这一帧晚到了。
  function resync() {
    if (document.hidden || !following) return;
    requestAnimationFrame(function () { if (following) scrollToBottomNow(); });
  }

  document.addEventListener("visibilitychange", resync);
  window.addEventListener("focus", resync);
  window.addEventListener("resize", resync);
  document.addEventListener("visibilitychange", resyncComments);
  window.addEventListener("focus", resyncComments);
  window.addEventListener("resize", resyncComments);
  // 兜底：桌面窗口被遮挡时 visibilitychange 未必触发。跟随状态下若发现
  // 不在底部就补上，代价是每 2 秒读一次几何量。
  setInterval(function () {
    if (document.hidden) return;
    if (needsResync(historyEl, following)) scrollToBottomNow();
    if (needsResync(commentList, commentFollowing)) commentsToBottomNow();
    updateJumpButton();
    updateCommentJump();
  }, 2000);

  // ---- 翻译引擎 ----
  // 需要密钥的引擎才显示密钥框；已经填过的显示尾四位作为占位，
  // 用户不重填就沿用旧的（页面永远拿不到完整密钥）。
  var KEY_ENV = { deepl: "DEEPL_API_KEY", claude: "ANTHROPIC_API_KEY",
                  openai: "OPENAI_API_KEY" };
  // auto 那一句与 index.html #engine-note 的初值是同一句（首帧不闪另一种语言）
  var NOTES = {
    auto: L("默认用本地模型：完全离线、不限量、字幕不出本机。",
            "Uses a local model: fully offline, unlimited, and captions never leave this computer."),
    hymt2: L("本地模型，离线免费。多数机器用这一档就够。",
             "Local model, offline and free. Good enough for most computers."),
    "hymt2-7b": L("本地模型，术语更准，但会和语音识别抢内存，可能拖慢报警。",
                  "Local model with more accurate terms. It shares memory with speech recognition " +
                  "and can slow alerts."),
    deepl: L("字幕文本会发送给 DeepL。免费额度以此处显示的用量为准；额度周期与续用方式取决于你的 DeepL 账户方案。",
             "Captions are sent to DeepL. Free quota usage is shown here. Your DeepL plan sets the quota " +
             "period and renewal."),
    // 观众弹幕和重译也走 Claude（2026-10-13）：数据去哪了不能含糊
    claude: L("字幕文本、观众弹幕和重译的请求会发送给 Anthropic，按用量计费。",
              "Captions, viewer comments and retranslation requests are sent to Anthropic and billed by usage."),
    openai: L("字幕文本会发送给该接口的提供方，按用量计费。",
              "Captions are sent to this API’s provider and billed by usage."),
    // 不写 rate limit 一类的词（CLAUDE.md 第八条的禁用词对全仓英文都查）
    google: L("字幕文本会发送给 Google，且会按 IP 限流。",
              "Captions are sent to Google. Google caps how many requests one IP address can make."),
    none: L("只显示识别原文，不翻译。", "Shows the original text without translating.")
  };
  var engineKeys = {};
  var engineNoteSig = null;   // 上一次的回退提示；变了才自动展开，重连回放不反复弹开

  // 引擎被回退时摘要前面加的小三角（跟 .sc-icon 同一套线性画法，颜色固定橙——
  // 这是「提醒」级别，不是失败），配合摘要文字改写成「已回退 · …」一起说明，
  // 不再只靠摘要标橙一种视觉（designer.md #1）
  var ENGINE_FALLBACK_ICON =
    '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" ' +
    'stroke-width="1.3" stroke-linejoin="round" stroke-linecap="round">' +
    '<path d="M8 2.3 14.2 13.2H1.8Z"/><path d="M8 6.6v3"/>' +
    '<circle cx="8" cy="11.1" r=".55" fill="currentColor" stroke="none"/></svg>';

  function renderEngine(info) {
    if (!engineSelect) return;
    engineKeys = info.keys || {};
    engineSelect.value = info.engine || "auto";
    commentRedoEngine = info.active || "";
    syncAllCommentRedo();     // 字幕引擎换了：已经画出来的「重译」按钮跟着变灰或可点
    if (guardDrawerOpen) drawGuardDrawer();
    // 说人话的引擎名由服务端给（app/translator.py engine_label），页面不再自己
    // 维护一份对照表——以前这里的 ENGINE_LABEL 和服务端那份已经不一致（同一个
    // openai 一边叫「OpenAI 兼容接口」一边叫「OpenAI」，见 pm.md #7）。摘要文案
    // 本身由 engineSummary（settings-rows.js）算，这里只管把它塞进 DOM
    var engSum = engineSummary(info);
    if (engSum.hasNote) {
      engineActive.innerHTML = "";
      var triangle = document.createElement("span");
      triangle.className = "set-fallback-icon";
      triangle.setAttribute("aria-hidden", "true");
      triangle.innerHTML = ENGINE_FALLBACK_ICON;
      engineActive.appendChild(triangle);
      engineActive.appendChild(document.createTextNode(engSum.text));
    } else {
      engineActive.textContent = engSum.text;
    }
    engineActive.title = engSum.text;
    syncEngineRow();
    // 启动时引擎被回退的提示（如「上次选的翻译引擎 DeepL 还没有密钥，本次先用
    // 自动」，来自 app/translator.py restore_engine），压过常规注记——
    // 用户上次的选择被改掉了，必须看得见
    if (engSum.hasNote) {
      setIconText(engineNote, "warn", info.note);
      engineNote.classList.add("warn");
    }
    // 卡片收进设置列表后，「上次的选择被改掉了」不能藏在折叠里：
    // 摘要标橙，提示内容变了就自动展开这一行
    if (engineHead) engineHead.classList.toggle("has-warn", engSum.hasNote);
    // 同一条提示重放（sig 没变）不重开；换了新提示才展开；提示被清除时签名
    // 也会变，但只有 engSum.hasNote 为真才应用 open——不能把用户正开着看的行
    // 强制收起（nextAutoOpen 的注释里解释了这一步为什么不能合并进纯函数）
    var r = nextAutoOpen(engineNoteSig, engSum.sig, true);
    engineNoteSig = r.sig;
    if (r.changed && engSum.hasNote) setRowOpen(engineHead, engineBody, true);
    syncGuardSettings();      // 弹幕留意选了 Claude 时要不要显示密钥框，看的是同一个 Claude 密钥
  }

  function syncEngineRow() {
    var env = KEY_ENV[engineSelect.value];
    engineKey.classList.toggle("hidden", !env);
    if (env) {
      var have = engineKeys[env];
      engineKey.value = "";
      // have 是打过码的尾四位（app/translator.py mask_key：「…abcd」）
      engineKey.placeholder = have ? L("已填 " + have + "（留空则沿用）", "Saved key " + have + " (leave blank to keep)")
                                   : L("粘贴 API 密钥", "Paste API key");
    }
    // 字幕会发到外部服务的几档：前面加三角图标（以前是 ⚠️ 前缀），颜色仍由 .warn 给
    var sendsOut = engineSelect.value in KEY_ENV || engineSelect.value === "google";
    if (sendsOut) setIconText(engineNote, "warn", NOTES[engineSelect.value] || "");
    else engineNote.textContent = NOTES[engineSelect.value] || "";
    engineNote.classList.toggle("warn", sendsOut);
  }

  if (engineSelect) {
    engineSelect.addEventListener("change", syncEngineRow);
    engineSave.addEventListener("click", function () {
      engineSave.disabled = true;
      engineSave.textContent = L("切换中…", "Switching…");
      send({ type: "set_engine", engine: engineSelect.value,
             api_key: engineKey.value || null });
      engineKey.value = "";
      setTimeout(function () {
        engineSave.disabled = false;
        engineSave.textContent = L("保存", "Save");
      }, 2500);
    });
  }

  // ---- 字幕记录（设置分组里的一行，app/pipeline_transcript.py） ----
  function renderTranscripts(info) {
    if (transcriptSummaryEl) transcriptSummaryEl.textContent = transcriptSummary(info);
  }
  function showTranscriptStatus(text) {
    if (transcriptStatusEl) transcriptStatusEl.textContent = text;
  }
  if (transcriptExportBtn) {
    transcriptExportBtn.addEventListener("click", function () {
      showTranscriptStatus(L("正在导出…", "Exporting…"));
      if (!send({ type: "export_transcript" })) {
        showTranscriptStatus(L("与本地服务断开，正在重连——稍候再试。",
                               "Lost connection to the local service. Reconnecting… Try again in a moment."));
      }
    });
  }
  if (transcriptDirBtn) {
    transcriptDirBtn.addEventListener("click", function () { send({ type: "open_transcripts_dir" }); });
  }

  // ---- 界面语言（spec §3.4、§3.5） ----
  // 语言名、摘要、要不要重载都由 settings-rows.js 的纯函数算，这里只管 DOM 和存储
  var LANG_RELOAD_KEY = "tlt.langReload";
  var langSetting = null;   // 服务端此刻存的选择（system / zh / en）：发送失败时下拉退回它

  function renderLang(cfg) {
    if (!langCard || !cfg) return;
    var available = cfg.ui_lang_available === true;
    langCard.classList.toggle("hidden", !available);
    if (!available) return;
    langSetting = cfg.ui_lang_setting;
    if (uiLangSystemOpt) uiLangSystemOpt.textContent = langSystemLabel(cfg.ui_lang_system);
    if (uiLangSelect) {
      if (langSetting === "system" || langSetting === "zh" || langSetting === "en") {
        uiLangSelect.value = langSetting;
      }
      // 带了 --ui-lang 一次性覆盖：这次启动的语言由启动参数定，改设置也不会生效
      var locked = cfg.ui_lang_locked === true;
      uiLangSelect.disabled = locked;
      if (locked) uiLangSelect.title = L("由启动参数固定", "Set by a launch option");
      else uiLangSelect.removeAttribute("title");
    }
    if (langSummaryEl) langSummaryEl.textContent = langSummary(langSetting, cfg.ui_lang, cfg.ui_lang_system);
  }

  // 服务端的界面语言和本页不同（刚在设置里换过，或重连到一个换过语言的程序）：整页重载一次。
  // 重载后服务端按新语言注入 <html lang>，hello 回放也按新语言渲染，页面上没有残留。
  // sessionStorage 记不下就不重载：宁可停在原来的语言，也不能在对不上时无限刷新
  function reloadForLang(cfg) {
    if (!cfg || typeof cfg.ui_lang !== "string") return false;
    var stored = null;
    try { stored = sessionStorage.getItem(LANG_RELOAD_KEY); } catch (e) { stored = null; }
    var decision = langReload(cfg.ui_lang, UI_LANG, stored, Date.now());
    if (!decision.reload) return false;
    try { sessionStorage.setItem(LANG_RELOAD_KEY, decision.mark); } catch (e) { return false; }
    location.reload();
    return true;
  }

  if (uiLangSelect) {
    uiLangSelect.addEventListener("change", function () {
      if (send({ type: "set_ui_lang", value: uiLangSelect.value })) return;
      if (langSetting) uiLangSelect.value = langSetting;
      setStatus({ state: "offline",
                  detail: L("与本地服务断开，正在重连——稍候再试。",
                            "Lost connection to the local service. Reconnecting… Try again in a moment.") });
    });
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  connect();
})();
