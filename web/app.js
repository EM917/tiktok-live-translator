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
  var engineSelect = document.getElementById("engine-select");
  var engineKey = document.getElementById("engine-key");
  var engineSave = document.getElementById("engine-save");
  var engineActive = document.getElementById("engine-active");
  var engineNote = document.getElementById("engine-note");
  var liveTranslated = document.getElementById("live-translated");
  var liveOriginal = document.getElementById("live-original");
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
  var commentCount = document.getElementById("comment-count");
  var toggleCommentsBtn = document.getElementById("toggle-comments");
  var clearCommentsBtn = document.getElementById("clear-comments");
  var commentFab = document.getElementById("comment-fab");
  var commentFabCount = document.getElementById("comment-fab-count");
  var commentEmpty = document.getElementById("comment-empty");
  var commentSource = document.getElementById("comment-source");
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
  var statsEl = document.getElementById("stats-line");
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
  bindRow(inputHelpBtn, inputHelp);
  bindRow(langHead, langBody);

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
  var maxHistory = 300;
  var currentVersion = "";
  var startWatchdog = null;
  var pendingStart = null;   // 已发出但服务器还没回执的「开始」指令（重连后补发）
  var versionNoticeTimer = null;
  var updateCheckNote = "";  // 很久没连上更新服务器时版本号后面那句话（文字由服务端给）
  var updateConfirmTimer = null;
  var recentClearConfirmTimer = null;
  var liveBarId = null;      // 底部大字幕当前显示的是哪一条（译文回来要就地替换）
  // 字幕先出原文、译文后补，所以要能按 id 找回已渲染的那张卡片
  var cardsById = {};
  // 布局意义上的「直播中」，供 viewTransition 判断迁移方向；null＝页面刚加载，
  // 还没收到过任何状态——不能当成「已经在待机」，否则首次到达就是 idle 时会被
  // 误判成「没有变化」，进首页该做的动作（大字幕/统计行/弹幕面板归位）就漏了
  var homeActive = null;

  // ---- 设置 ----
  // 只有用户显式调过字号才覆盖 CSS 默认值（否则会压掉移动端媒体查询的 26px）
  var savedFont = lsGet("subFontSize");
  if (savedFont) {
    fontSlider.value = parseInt(savedFont, 10);
    applyFont(parseInt(savedFont, 10));
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

  fontSlider.addEventListener("input", function () {
    var size = parseInt(fontSlider.value, 10);
    applyFont(size);
    lsSet("subFontSize", String(size));
  });

  // 目标语言以服务端为准（跟随 --target 启动参数），UI 切换即时生效但不做本地持久化
  targetSel.addEventListener("change", function () {
    send({ type: "set_target", value: targetSel.value });
  });

  // 「清空字幕」按钮本身，以及停止后停留在待机首页的「上一场字幕」小标题，
  // 都只在 #history 里确实还有 .cap 时才该出现（pm.md #8、designer.md #6）。
  // 两处判断条件不同（前者不看是不是首页，后者只在首页才显示），抽成一个
  // 函数只是不想让「有没有字幕」这个判断在多处各写一份、改一个漏一个
  function syncCaptionDependentUi() {
    var hasCaps = !!historyEl.querySelector(".cap");
    if (clearBtn) clearBtn.classList.toggle("hidden", !hasCaps);
    if (prevCapsTitle) prevCapsTitle.classList.toggle("hidden", streamActive || !hasCaps);
  }

  clearBtn.addEventListener("click", function () {
    // 分隔条（.session-sep，换主播时插的「── HH:MM:SS 以下为 @B ──」）跟着
    // 字幕一起清掉，不然重连回放前调这个函数清场时，旧分隔条会越攒越多——
    // 这个函数也被 hello 处理里的 clearBtn.click() 当内部重置用，不只是
    // 用户手点「清空」那一条路径
    var caps = historyEl.querySelectorAll(".cap, .session-sep");
    for (var i = 0; i < caps.length; i++) caps[i].remove();
    cardsById = {};              // 卡片没了，id 映射也要清，否则一直涨
    liveBarId = null;
    liveBar.classList.add("hidden");
    syncCaptionDependentUi();
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
    send({ type: "apply_update" });
  });

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
    updateBar.classList.remove("hidden");
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
      migrateBar.classList.remove("hidden");
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
        migrateBar.classList.add("hidden");
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

  function applyFont(size) {
    document.documentElement.style.setProperty("--sub-size", size + "px");
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
        commentCount.textContent = "0";
        commentFollowing = true;   // 回放重建整个列表：从最新处开始看
        commentPanel.classList.add("hidden");
        // 失败连击也归零——回放的陈旧字幕不该累积成新警告
        failStreak = 0;
        transBannerOn = false;
        clearBtn.click();
        if (msg.config) {
          if (msg.config.comment_backend) backendState = msg.config.comment_backend;
          if (msg.config.comment_detail != null) backendDetail = msg.config.comment_detail;
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
          // 持续提示以服务端为准：重连时整份重放，先清掉本地的
          incidents = Object.create(null);
          if (msg.config.incidents) {
            Object.keys(msg.config.incidents).forEach(function (k) {
              renderIncident(msg.config.incidents[k]);
            });
          }
          drawIncidents();
          if (msg.config.status) setStatus(msg.config.status);
          if (msg.config.target_lang) targetSel.value = msg.config.target_lang;
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
            updateBar.classList.add("hidden");
            resetUpdateBtn();
          }
          // 界面语言：闸关着时 config 里没有这几个键，行保持隐藏、不比对、不重载
          renderLang(msg.config);
          reloadForLang(msg.config);
        }
        break;
      case "update_available":
        showUpdate(msg);
        break;
      case "updating":
        updateBtn.disabled = true;
        updateBtn.textContent = L("更新中…", "Updating…");
        break;
      // 这次没更新成（原因另有提示/状态说明）：按钮恢复，可以再试
      case "update_aborted":
        resetUpdateBtn();
        break;
      case "status":
        // connecting/live/error 任一状态到达即视为服务器已接管「开始」指令
        if (pendingStart && (msg.state === "connecting" || msg.state === "live"
                             || msg.state === "error")) {
          pendingStart = null;
          if (startWatchdog) clearTimeout(startWatchdog);
        }
        setStatus(msg);
        break;
      // 一次性提示（如手动检查更新的结果）：只改版本号处的文案，
      // 不碰状态机——直播中收到它不该影响「停止」按钮等 UI
      case "notice":
        if (msg.text) showVersionNote(msg.text, 6000);
        break;
      case "config":
        if (msg.target_lang) targetSel.value = msg.target_lang;
        if (msg.source_lang && !sourceTouched) selectSourceLang(msg.source_lang);
        // 同一条 config 广播里 room_url 和 brands 一起到达时，room_url 要先填
        // 进输入框——换了主播的第二个页面靠它才能刷新出正确的默认品牌，
        // 顺序反了就会用上一个主播的房间算出错的默认值（踩过的坑，同 hello 分支）。
        // brand_options 同理要先于 brands：refresh_brands 的回执也走这条分支，
        // 选项要先建好，applyDefaultBrand() 才有值可选
        if (msg.room_url) fillRoomInput(msg.room_url);
        if (msg.brand_options) renderBrandOptions(msg.brand_options);
        if (msg.brands) { brandsMap = msg.brands; applyDefaultBrand(); }
        // start_stream 开播时、真正停止后都会广播这个字段（后者是显式 null）；
        // 用 "in" 而不是真值判断，是因为 set_target 等别的 config 广播不带这个
        // 键，不该被当成「清空品牌」处理
        if ("active_brand" in msg) setActiveBrand(msg.active_brand);
        if (msg.alerts_session) setAlertSession(msg.alerts_session);
        if ("update_check" in msg) renderUpdateCheck(msg.update_check);
        // 设置里换了界面语言（app/pipeline.py _set_ui_lang 广播语言那几个键）：刷新这一行，
        // 本页语言和新的不一样就重载。别的 config 广播（set_target 等）不带这些键，不动这一行
        if ("ui_lang_available" in msg) {
          renderLang(msg);
          reloadForLang(msg);
        }
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
        renderSessionBreak(msg);
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
      case "comment_source":
        if (msg.backend) {
          backendState = msg.backend;
          backendDetail = msg.detail || "";
        }
        refreshCommentPanel();
        break;
      case "stats":
        renderStats(msg);
        break;
      case "incident":
        renderIncident(msg);
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
  function setStatus(msg) {
    var state = msg.state || "idle";
    statusDot.className = "dot " + state;
    if (statusPill) statusPill.dataset.state = state;
    // 连接中/直播中额外带上正在听谁：「连接中… · @A」「直播中 · @A」。主播名和
    // 顶栏「换主播」面板认的是同一个来源（config.room_url，经 streamerFromInput
    // 提取），取不到（房间链接还没回填、或本来就是纯直连地址没有主播身份）就
    // 不带这半句。连接中也要带：点错了要等连上才看得出来，而失败的连接中位
    // 要等约 28 秒，这段时间里主播名是唯一能核对「点没点对」的线索（pm.md #5）
    var label = STATUS_TEXT[state] || state;
    var liveStreamer = (state === "live" || state === "connecting")
      ? streamerFromInput(roomInput.value) : "";
    statusText.textContent = liveStreamer ? label + " · " : label;
    if (liveStreamer) statusText.appendChild(nameSpan("@" + liveStreamer));   // 主播名

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
    updateAlertModeTag();   // 标签只在连接中/直播中露出，别的状态下退回隐藏
    updateActiveBrandTag(); // 本场品牌标签同一个显示条件，见该函数注释
    syncCaptionDependentUi(); // 「上一场字幕」标题只在首页才显示，随 streamActive 变化
    syncConnectHint(state);
    refreshCommentPanel();
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

  // 连接中：字幕区中央的转圈 + 「正在连接 @xxx…」（显示规则见 web/live-ui.js
  // connectHint）。已有字幕时挪到 #history 末尾当一行，并跟到底部——停止后再开、
  // 换主播时历史还在，中央那一版会盖在旧字幕上
  function syncConnectHint(state) {
    if (!connectHintEl) return;
    var hint = connectHint(state, streamerFromInput(roomInput.value),
                           !!historyEl.querySelector(".cap"));
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

    // 「重译」：用本机最强的模型重来一次。按条触发而不是开一个时段，
    // 是因为值得动用强模型的是具体某句话，而那只有看着的人知道是哪一句。
    var redo = document.createElement("button");
    redo.className = "redo";
    redo.type = "button";
    redo.title = L("用最强模型重新翻译这一条", "Retranslate with the most accurate model");
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
    // force 只在「重连时回放、且当前确实处于直播中」才为 true：那是唯一
    // 该无视用户滚动状态、强制跳到最新的场景。待机时收到的回放（刚停止/刚
    // 打开就是上一场留下的历史）不该跟着强制滚到底——否则 enterHomeLayout
    // 刚把 #history 归位到顶部，这里又把它拽回底部，开始面板重新被盖住
    // （这正是「长场次停止后看不到输入框」那个既有问题的成因）。
    stickToBottom(msg.replay && streamActive);

    // 底部大字幕：回放历史时不逐条更新（避免闪一串旧字幕），最后一条带
    // restore 标记，用它把大字幕恢复成断线前的样子——但只在当前确实处于
    // 直播中才恢复；待机时回放只用来恢复上面的历史卡片，大字幕留隐藏，
    // 不然停止后大字幕会跟着回放重新冒出来，压住开始面板下半部
    if ((!msg.replay || msg.restore) && streamActive) {
      liveBar.classList.remove("hidden");
      liveTranslated.textContent = msg.translated || msg.original || "";
      liveOriginal.textContent = msg.translated ? (msg.original || "") : "";
      liveOriginal.classList.toggle("hidden", !msg.translated);
      liveBarId = msg.id;
    }
  }

  // 译文后补：原地更新那张卡片，不新增一条
  function updateCaption(msg) {
    var card = cardsById[msg.id];
    if (card) applyTranslation(card, msg);
    if (liveBarId === msg.id && msg.translated) {
      // 小字永远是西语原文（从卡片取）。以前拿当前大字顶上去：第二次译文
      // （重译）到来时大字已经是中文，顶栏就变成两行中文，原文不见了
      var origEl = card ? card.querySelector(".orig") : null;
      liveOriginal.textContent = origEl ? origEl.textContent : liveOriginal.textContent;
      liveOriginal.classList.remove("hidden");
      liveTranslated.textContent = msg.translated;
    }
    trackTranslateHealth(msg);
  }

  // 换主播（或程序重启后的每一场）广播的场次分隔：约定见前后端约定文档——
  // 每场都发，前端只在页面里已经有字幕卡片时才画分隔线，所以程序启动后的
  // 第一场不会多出一条线（那时 #history 里还没有任何 .cap）。回放（重连）时
  // 同样调用这个函数，走的是和实时一样的路径，不需要额外的「回放」分支：
  // hello 处理会先用 clearBtn.click() 清场，之后回放按原始顺序重新触发
  // caption/session_break，历史上的场次边界就这样被原样重建一遍。
  function renderSessionBreak(msg) {
    var caps = historyEl.querySelectorAll(".cap");
    // 要不要画、画什么字是纯逻辑，抽到 web/session-divider.js 里单独测
    // （见该文件顶部注释）：app.js 这个大 IIFE 顶层就摸 DOM，整份没法被
    // node:test require
    if (!shouldRenderSessionDivider(caps.length)) return;
    for (var i = 0; i < caps.length; i++) caps[i].classList.add("prev-session");
    insertSessionDivider(historyEl, msg);
    // 底部大字幕清空，等新一场第一条字幕自己把它揭开（同 enterHomeLayout
    // 的道理：不主动显示，交给下一条真实字幕）
    liveBar.classList.add("hidden");
    liveBarId = null;
    insertSessionDivider(commentList, msg);
    syncConnectHint(statusPill ? statusPill.dataset.state : "");
    stickToBottom(false);
  }

  function insertSessionDivider(container, msg) {
    var sep = document.createElement("div");
    sep.className = "session-sep";
    sep.textContent = sessionDividerText(msg);
    container.appendChild(sep);
  }

  function applyTranslation(card, msg) {
    // 重译有自己的状态位，绝不能走普通翻译那条 pending 分支——
    // 那会把正在阅读的译文换成「翻译中…」，而强模型若返回空（约 2% 会），
    // 服务端不会再广播，页面就永远停在那里，好译文也没了。
    if (msg.strong_state) {
      var redoBtn = card.querySelector(".redo");
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
    stateChip.classList.toggle("fail-chip", state === "failed");
    stateChip.textContent = state === "failed" ? L("翻译失败", "Translation failed")
      : (state === "dropped" ? L("翻译已跳过（积压）", "Skipped (backlog)") : "");
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

  // 面板什么时候露面、空着的时候说什么。直播中面板必须在——否则中控分不清
  // 「今天没人发弹幕」和「评论流压根没连上」，而后者是要去处理的。
  // show 只看 streamActive，不再是「有内容也算」：待机页面（含刚停止那一刻）
  // 不该显示上一场的弹幕面板/小入口，但列表内容本身不清空，下一场开始时
  // streamActive 一变回 true，原样恢复显示。
  function refreshCommentPanel() {
    var has = commentList.children.length > 0;
    var show = streamActive;
    var collapsed = commentPanel.classList.contains("collapsed");
    commentPanel.classList.toggle("hidden", !show);
    commentEmpty.classList.toggle("hidden", has);
    // 收起时整列不显示，只在字幕区右上角留一个带条数的入口
    commentFab.classList.toggle("hidden", !(show && collapsed));
    commentFabCount.textContent = commentItemCount();
    // 「回到最新」是 fixed 定位在右下角的，弹幕列展开时把它往左挪，别盖在弹幕上
    var panelOpen = show && !collapsed;
    jumpBtn.style.right = (panelOpen ? commentPanel.offsetWidth + 20 : 20) + "px";

    // 标题旁的小字状态 + 空态文案：中控要能一眼分清「没人发弹幕」和
    // 「抓取本身出了问题」，两者处理方式完全不同（前者等，后者去修）。
    // 后端说明原样写进去，太长由 CSS 截成最多 6 行（style.css .cmt-source）：以前在这里按
    // 80 个字符截，英文只截得出半句话（spec §11 第 6 条）
    var title, cls = "cmt-source", emptyText;
    // 小字状态就放在 Comments 标题旁边，英文不再重复「评论流」这个主语
    if (backendState === "connected") {
      title = L("评论流已连接", "Connected");
      cls += " on";
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
    if (!has) commentEmpty.textContent = emptyText;
  }

  function renderComment(msg) {
    var item = document.createElement("div");
    item.className = "cmt-item";
    item.dataset.cmtId = msg.id;

    var head = document.createElement("div");
    head.className = "cmt-head";
    var ts = new Date((msg.ts || Date.now() / 1000) * 1000);
    head.appendChild(nameSpan(msg.user || ""));   // 观众名
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

    // 正序：新的追加在底部，和字幕列表同一个方向，从上往下读就是时间顺序
    commentList.appendChild(item);
    commentById[msg.id] = item;
    // 满 100 条从顶部删最旧的。用户正往上翻着看时，眼前那条不能跟着跑：
    // 删之前记下第一条可见弹幕的位置，删完量它实际移动了多少再补回来。
    // 不按「删掉多高」去减——Chromium 自带滚动锚定会先补一次，再减就补过头
    // （实测每删一条视野往上窜一条）；WebKit 不一定锚定。量实际位移两边都对。
    var anchor = null, anchorTop = 0;
    var excess = commentList.children.length - 100;
    if (excess > 0 && !commentFollowing && isMeasurable(commentList)) {
      var listTop = commentList.getBoundingClientRect().top;
      for (var i = excess; i < commentList.children.length; i++) {
        var rect = commentList.children[i].getBoundingClientRect();
        if (rect.bottom > listTop) { anchor = commentList.children[i]; anchorTop = rect.top; break; }
      }
    }
    while (commentList.children.length > 100) {
      var oldest = commentList.firstChild;
      delete commentById[oldest.dataset.cmtId];
      commentList.removeChild(oldest);
    }
    if (anchor) {
      var drift = anchor.getBoundingClientRect().top - anchorTop;
      if (Math.abs(drift) >= 1) commentList.scrollTop += drift;
    }
    commentCount.textContent = commentItemCount();
    refreshCommentPanel();
    if (commentFollowing) commentsToBottomNow();
  }

  // 弹幕条数：只数 .cmt-item，不数换主播时插进同一个列表里的 .session-sep
  // 分隔条——否则条数徽标会把分隔线也算进去，显得比实际弹幕数多
  function commentItemCount() {
    return commentList.querySelectorAll(".cmt-item").length;
  }

  function updateComment(msg) {
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
      return;
    }
    // failed/dropped：译不出来就显示原文本身——不显示「失败」字样，
    // 原文就是内容，中控照样看得懂观众在说什么
    zh.textContent = orig ? orig.textContent : "";
    zh.classList.add("failed");
    if (orig) orig.classList.add("hidden");
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
  }, { passive: true });

  // 瞬时滚到底：理由同字幕的 scrollToBottomNow（平滑动画追不上连续追加）
  function commentsToBottomNow() {
    var prev = commentList.style.scrollBehavior;
    commentList.style.scrollBehavior = "auto";
    commentList.scrollTop = commentList.scrollHeight;
    commentList.style.scrollBehavior = prev;
  }

  function resyncComments() {
    if (document.hidden || !commentFollowing) return;
    requestAnimationFrame(commentsToBottomNow);
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

  clearCommentsBtn.addEventListener("click", function () {
    commentList.innerHTML = "";
    commentById = Object.create(null);
    commentCount.textContent = "0";
    commentFollowing = true;
    refreshCommentPanel();    // 直播中清空后面板留着，只是回到空态
  });

  // ---- 延迟统计 ----
  function fmtMs(v) { return v == null ? "—" : (v / 1000).toFixed(1) + "s"; }

  function renderStats(msg) {
    var e2e = msg.e2e || {};
    var asr = msg.asr || {};
    var tr = msg.translate || {};
    var seg = msg.segment || {};
    var det = msg.detect_worst || {};
    // 第一指标是检测延迟（最坏情况）：违禁词说出口到报警最久要多少秒。
    // 字幕延迟只是副产品，合规上该被考核的是这个数。
    // 英文用紧凑写法（docs/i18n-style.md §3 #121）：所有计数都出现时逐字翻译会超过一行
    var parts = [
      L("违禁词最迟 " + fmtMs(det.p50) + " / P95 " + fmtMs(det.p95) + " 内报警",
        "Alerts ≤" + fmtMs(det.p50) + " (P95 " + fmtMs(det.p95) + ")"),
      L("其中 切段 " + fmtMs(seg.p50) + " + 识别 " + fmtMs(asr.p50),
        "Segment " + fmtMs(seg.p50) + " + ASR " + fmtMs(asr.p50)),
      L("译文再等 " + fmtMs(tr.p50), "Translation +" + fmtMs(tr.p50)),
    ];
    if (msg.audio_backlog_sec >= 3) {
      parts.push(L("积压 " + msg.audio_backlog_sec.toFixed(0) + "s",
                   "Backlog " + msg.audio_backlog_sec.toFixed(0) + "s"));
    }
    if (msg.audio_segments_dropped) parts.push(L("丢音频 " + msg.audio_segments_dropped,
                                                 "Dropped " + msg.audio_segments_dropped));
    // 识别跑飞是丢音频的前兆，出现就该看见
    if (msg.asr_overruns) parts.push(L("识别超时 " + msg.asr_overruns, "ASR timeouts " + msg.asr_overruns));
    if (msg.translation_jobs_dropped) parts.push(L("跳过翻译 " + msg.translation_jobs_dropped,
                                                   "Skipped " + msg.translation_jobs_dropped));
    if (msg.asr_queue_depth || msg.translation_queue_depth) {
      parts.push(L("积压 " + msg.asr_queue_depth + "/" + msg.translation_queue_depth,
                   "Queue " + msg.asr_queue_depth + "/" + msg.translation_queue_depth));
    }
    statsEl.textContent = parts.join(" · ");
    // 只在直播中/连接中揭开。后端每场收尾会补推一次终值（pipeline 的 _end_session），
    // 出错那条路上它晚于 status=error 到达——这时页面已经回到首页、enterHomeLayout
    // 已经把统计行收掉了，无条件揭开会让一行全是「—」的统计压在设置列表上
    // （2026-09-28 用户截图）。文字照常更新，下一场开播时的第一条统计再揭开。
    if (streamActive) statsEl.classList.remove("hidden");
  }

  // 识别落后时必须让中控看见——假装一切正常比晚几秒报警危险得多
  // 持续提示（电脑休眠过、审计日志写不进去、识别改用 CPU……）：按 id 覆盖，level=clear 去掉。
  // 和 health（识别积压，会被「已追上」覆盖）、notice（几秒后消失）不同：这类状况中控必须
  // 看到，刷新页面也还在（服务端放在 hello 的 config.incidents 里）。
  var incidents = Object.create(null);

  function renderIncident(msg) {
    if (!msg || !msg.id) return;
    if (msg.level === "clear") {
      delete incidents[msg.id];
    } else {
      incidents[msg.id] = { level: msg.level || "warn", text: msg.text || "",
                            since: msg.since || msg.ts || 0 };
    }
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
      incidentBar.appendChild(row);
    });
    incidentBar.classList.toggle("hidden", keys.length === 0);
  }

  function renderHealth(msg) {
    // 「识别落后/已降级」只对正在进行的这一场成立；停止或出错回到首页后，
    // 晚到的一条不该再把它揭开（同 renderStats 的道理）
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
      // 模型（hf/ollama）的标签是模型名，按名字标；日志两项（kind=logs）的标签是后端写的界面
      // 句子，要能换成英文——按 kind 分，不加新字段（spec §6、§17 第 6 条）
      if (it.kind !== "logs") markName(name);
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
    if (following) scrollToBottomNow();
    updateJumpButton();
  }

  // 进入首页状态（viewTransition 的 enterHome）：直播状态从活跃变为非活跃，
  // 或页面刚加载就是非活跃。把上一场直播留下的视觉残留收掉——大字幕、统计行、
  // 弹幕面板都是「直播中才有意义」的 UI，赖在待机页面上会压住开始面板、或
  // 让人以为还在直播（2026-09-24 用户报告的残留）。弹幕面板本身的隐藏交给
  // refreshCommentPanel（已在调用处按 streamActive 处理），这里不重复。
  function enterHomeLayout() {
    liveBar.classList.add("hidden");
    liveBarId = null;
    following = false;
    scrollToTopNow();
    updateJumpButton();
    statsEl.classList.add("hidden");
    // 识别落后提示只描述正在进行的那一场：后端停止时不会补发 level=ok，
    // 一场在落后状态下结束，这条会一直留在首页上
    healthBar.classList.add("hidden");
    // 换主播面板只在直播中有意义（按钮本身这时也被隐藏了）：停止之后若还开着，
    // 关掉它，不然会变成一块摆在待机页面上的残留浮层
    closeSwitchPanel();
  }

  // 离开首页状态（viewTransition 的 exitHome）：直播状态从非活跃变为活跃
  // （点了「开始翻译」、后端确认 connecting/live）。大字幕和统计行不在这里
  // 主动显示——它们等第一条真正的字幕/统计数据到达时由 renderCaption /
  // renderStats 自己揭开；这里只需要让接下来到达的新字幕照常跟到底部。
  function exitHomeLayout() {
    following = true;
  }

  // 按钮只在「确实有内容在下面」时出现。不可测量时不显示——那时什么都判断不了，
  // 摆一个按不动的按钮只会添乱。非活跃状态（待机/已结束/出错）下不显示：
  // 那时 #history 顶部是开始面板，「回到最新」没有意义。
  function updateJumpButton() {
    if (!jumpBtn) return;
    var show = streamActive && isMeasurable(historyEl) && !atBottom(historyEl);
    if (show) {
      // 底部大字幕是 fixed 且高度随字号变化，按钮得让开它。
      // 上限夹在视口 40% 处：布局异常时测出的 barTop 可能贴近顶部，
      // 不夹的话按钮会被顶到屏幕上方，看起来像个飞出来的东西。
      var vh = window.innerHeight || 800;
      var barTop = (liveBar && !liveBar.classList.contains("hidden"))
        ? liveBar.getBoundingClientRect().top : vh - 56;
      var offset = vh - barTop + 12;
      if (!(offset > 0)) offset = 68;                 // NaN / 负数兜底
      jumpBtn.style.bottom = Math.min(Math.max(offset, 56), vh * 0.4) + "px";
    }
    jumpBtn.classList.toggle("hidden", !show);
  }

  if (jumpBtn) {
    jumpBtn.addEventListener("click", function () {
      following = true;                 // 点了就是要看最新，恢复跟随
      scrollToBottomNow();
      updateJumpButton();
    });
  }

  // 重新可见/重新获得焦点时补一次：不可见期间的滚动请求全部落空了。
  // rAF 等布局稳定后再滚——刚显示出来时尺寸还没算完。
  function resync() {
    if (document.hidden || !following) return;
    requestAnimationFrame(scrollToBottomNow);
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
    claude: L("字幕文本会发送给 Anthropic，按用量计费。", "Captions are sent to Anthropic and billed by usage."),
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
