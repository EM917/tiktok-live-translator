/* TikTok 直播同传 —— 前端逻辑：WebSocket 收字幕、渲染历史 + 底部大字幕、启动/停止直播间 */
(function () {
  // 浏览器把 127.0.0.1 的站点数据整个禁掉（隐私开关/企业策略）时，localStorage
  // 一碰就抛 SecurityError——不兜住的话整个初始化脚本在第一行就断掉，页面停在
  // 「等待连接…」且没有任何报错。这些值只是本地偏好，真正要紧的都在 settings.json
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 忽略 */ } }
  "use strict";

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
  var targetSel = document.getElementById("target-lang");
  var fontSlider = document.getElementById("font-size");
  var clearBtn = document.getElementById("clear-btn");
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
  var watchlistConfigured = false;   // 词表非空：来自最近一次 renderWatchlist，决定 watch-desc 是否显示模式文案
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
  var fixCmd = document.getElementById("fix-command");
  var fixCmdText = document.getElementById("fix-command-text");
  var fixCmdCopy = document.getElementById("fix-command-copy");
  var scBox = document.getElementById("selfcheck");
  var scHead = document.getElementById("sc-head");
  var scSummary = document.getElementById("sc-summary");
  var scToggle = document.getElementById("sc-toggle");
  var scList = document.getElementById("sc-list");
  var diskHead = document.getElementById("disk-head");
  var diskSummary = document.getElementById("disk-summary");
  var diskBody = document.getElementById("disk-body");
  var diskList = document.getElementById("disk-list");
  var diskDelete = document.getElementById("disk-delete");

  // 手机同看卡片（#share-card）：只在打开期间监听 0.0.0.0，控制面本身始终只在
  // 127.0.0.1；这张卡片只发/收 viewer_share / viewer_rotate，看不到任何观众数据
  //
  // 卡片以前挂在 #start-panel 下面，直播开始后整块面板被隐藏，中控恰恰是在
  // 直播中才想扫码给同事看，却找不到入口。现在卡片单独放进 #share-panel，
  // 由顶栏的 #share-btn 开关，跟直播状态无关，待机/直播都能点开。
  var shareBtn = document.getElementById("share-btn");
  var sharePanel = document.getElementById("share-panel");
  var shareToggle = document.getElementById("share-toggle");
  var shareState = document.getElementById("share-state");
  var shareDesc = document.getElementById("share-desc");
  var shareBody = document.getElementById("share-body");
  var shareQr = document.getElementById("share-qr");
  var shareUrl = document.getElementById("share-url");
  var shareCopy = document.getElementById("share-copy");
  var shareRotate = document.getElementById("share-rotate");
  var shareClose = document.getElementById("share-close");
  var shareCount = document.getElementById("share-count");
  var shareAddrChanged = document.getElementById("share-addr-changed");
  var shareNote = document.getElementById("share-note");
  var shareIpList = document.getElementById("share-ip-list");

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

  var STATUS_TEXT = {
    idle: "待机",
    connecting: "连接中…",
    live: "直播中",
    ended: "直播已结束",
    error: "出错了",
    offline: "与本地服务断开，重连中…",
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
  var savedRoom = lsGet("roomUrl");
  if (savedRoom) roomInput.value = savedRoom;
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
  if (brandsDirBtn) {
    brandsDirBtn.addEventListener("click", function () {
      send({ type: "open_brands_dir" });
    });
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

  var UNRECOGNIZED_INPUT_MSG = "认不出这个输入：请粘贴直播间链接，或输入主播的英文用户名" +
                               "（到主播主页复制 @ 后面的部分，中文昵称不行）。";

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
      setStatus({ state: "offline", detail: "与本地服务断开，正在重连——稍候再点「开始翻译」" });
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
      stickyOfflineDetail = "指令未送达（连接中断），已自动重连——请再点一次「开始翻译」。";
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
    updateBtn.textContent = "一键更新";
  }

  updateBtn.addEventListener("click", function () {
    // 监听中点的要再点一次确认：更新会暂停监听，而这个按钮直播中一直摆在中控眼前。
    // 不用 window.confirm——应用窗口（pywebview）里它可能根本弹不出来
    if (streamActive && updateBtn.dataset.confirm !== "1") {
      updateBtn.dataset.confirm = "1";
      // 暂停多久要看这次要不要装新组件，页面不知道：不许诺时长，服务端的状态行会说
      updateBtn.textContent = "再点一次确认更新：更新期间监听暂停，更新完自动恢复";
      updateConfirmTimer = setTimeout(resetUpdateBtn, 6000);
      return;
    }
    if (updateConfirmTimer) clearTimeout(updateConfirmTimer);
    updateConfirmTimer = null;
    delete updateBtn.dataset.confirm;
    updateBtn.disabled = true;
    updateBtn.textContent = "更新中…";
    send({ type: "apply_update" });
  });

  function showUpdate(info) {
    if (!info || !info.version) return;
    updateText.textContent = "🔄 发现新版本 " + info.version;
    if (info.can_auto) {
      updateBtn.classList.remove("hidden");
      updateLink.textContent = "更新说明";
      updateLink.className = "";
    } else {
      // ZIP 安装无法自动更新——别摆一个点了必失败的按钮，直接给下载入口
      updateBtn.classList.add("hidden");
      updateLink.textContent = "前往下载新版本";
      updateLink.className = "btn primary";
    }
    updateLink.href = info.url || "#";
    updateBar.classList.remove("hidden");
    // 静默模式：短时间内已经提示过了。按钮照常可用，但不再改标题——
    // 标题会闪在任务栏/标签页上，连续几个 patch 的日子那是纯粹的骚扰。
    if (!info.quiet) document.title = "有新版本 · TikTok 直播同传";
    updateBar.classList.toggle("quiet", !!info.quiet);
  }

  // 点底部版本号即可手动检查更新
  versionEl.addEventListener("click", function () {
    if (!send({ type: "check_update" })) {
      showVersionNote("未连接到本地服务", 3000);
      return;
    }
    // 「检查中…」的恢复定时器要能被随后到达的结果提示接管，
    // 否则结果刚显示就被这个定时器抹回版本号
    showVersionNote("检查更新中…", 8000);
  });

  // 「迁移旧词表」：扫描 → 展示 → 用户确认 → 服务端备份并迁移。
  // 只迁移整条与旧官方模板一致的行，用户自己写的内容绝不动。
  function handleMigration(msg) {
    if (msg.stage === "available") {
      migrateText.textContent = "glossary.txt 里有 " + msg.count +
        " 条旧模板遗留的主播专属词条，会污染其他主播的直播";
      migrateBar.classList.remove("hidden");
    } else if (msg.stage === "plan") {
      if (!msg.entries || !msg.entries.length) {
        showVersionNote("没有可以安全自动迁移的条目（改动过的条目需手动移到 profiles/）", 8000);
        migrateBtn.disabled = false;
        return;
      }
      var lines = msg.entries.map(function (e) {
        return e.display + "  → profiles/" + e.streamer + ".txt";
      });
      var ok = window.confirm(
        "将迁移以下 " + lines.length + " 条（先备份 glossary.txt）：\n\n" +
        lines.join("\n") + "\n\n只迁移与旧官方模板完全一致的条目，" +
        "你自己添加或改过的内容不会被改动。继续？");
      if (ok) {
        send({ type: "migrate_glossary", confirm: true });
      } else {
        migrateBtn.disabled = false;
      }
    } else if (msg.stage === "done") {
      migrateBtn.disabled = false;
      if (msg.result && msg.result.total) {
        migrateBar.classList.add("hidden");
        var note = "已迁移 " + msg.result.total + " 条（备份：" +
                   msg.result.backup + "）";
        if (msg.result.failed) {
          note += "；另有 " + msg.result.failed +
                  " 条因 profile 写入失败未迁移，仍保留在原词表里";
        }
        showVersionNote(note, 10000);
      } else if (msg.result && msg.result.failed) {
        showVersionNote("迁移失败：profiles/ 目录写不进去，" + msg.result.failed +
                        " 条全部保留在原词表（已留备份 " + msg.result.backup + "）", 10000);
      } else {
        showVersionNote("没有需要迁移的条目", 6000);
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
          ? "本地程序似乎已经关闭——请重新双击打开：macOS 双击「TikTok Live Translator.app」，Windows 双击「Start.bat」。"
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
          if (msg.config.room_url && !roomInput.value) roomInput.value = msg.config.room_url;
          // brand_options 要先于 brands 处理：下拉框的选项得先建好，
          // applyDefaultBrand() 才有值可选
          if (msg.config.brand_options) renderBrandOptions(msg.config.brand_options);
          if (msg.config.brands) { brandsMap = msg.config.brands; applyDefaultBrand(); }
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
        }
        break;
      case "update_available":
        showUpdate(msg);
        break;
      case "updating":
        updateBtn.disabled = true;
        updateBtn.textContent = "更新中…";
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
        if (msg.alerts_session) setAlertSession(msg.alerts_session);
        if ("update_check" in msg) renderUpdateCheck(msg.update_check);
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
    // 直播中额外带上正在听谁：「直播中 · @A」。主播名和顶栏「换主播」面板
    // 认的是同一个来源（config.room_url，经 streamerFromInput 提取），
    // 取不到（房间链接还没回填、或本来就是纯直连地址没有主播身份）就不带这半句
    var label = STATUS_TEXT[state] || state;
    if (state === "live") {
      var liveStreamer = streamerFromInput(roomInput.value);
      if (liveStreamer) label += " · @" + liveStreamer;
    }
    statusText.textContent = label;

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
    stopBtn.classList.toggle("hidden", !active);
    // 换主播按钮和停止按钮同一处切换：只在直播中/连接中有意义，待机/已结束/
    // 出错/离线时没有「正在监听的主播」可换
    if (switchBtn) switchBtn.classList.toggle("hidden", !active);
    startBtn.disabled = state === "connecting";
    streamActive = active;
    updateAlertModeTag();   // 标签只在连接中/直播中露出，别的状态下退回隐藏
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
        statusBanner.textContent =
          "⚠️ 连续多条字幕翻译失败——翻译服务可能暂时连不上，字幕先显示原文（语音识别不受影响）。";
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
    redo.title = "用最强模型重新翻译这一条";
    redo.textContent = "重译";
    redo.addEventListener("click", function () {
      send({ type: "retranslate", id: msg.id });
    });
    card.appendChild(redo);

    historyEl.appendChild(card);
    cardsById[msg.id] = card;

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
        redoBtn.textContent = msg.strong_state === "pending" ? "重译中…"
          : (msg.strong_state === "failed" ? "重译失败" : "重译");
        if (msg.strong_state === "failed") {
          setTimeout(function () { redoBtn.textContent = "重译"; }, 4000);
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
    if (msg.translated) {
      trans.textContent = msg.translated;
      trans.classList.remove("pending");
    } else if (state === "pending") {
      trans.textContent = "翻译中…";
      trans.classList.add("pending");
    } else {
      trans.textContent = "";
      trans.classList.remove("pending");
    }
    if (!stateChip) return;
    stateChip.classList.toggle("fail-chip", state === "failed");
    stateChip.textContent = state === "failed" ? "翻译失败"
      : (state === "dropped" ? "翻译已跳过（积压）" : "");
  }

  // ---- 违禁词警报 ----
  // 警报是这个工具的核心产出，绝不自动消失：中控没看到就等于漏报。
  var TIER_LABEL = { exact: "🔴 命中", variant: "🟠 变体", fuzzy: "🟡 疑似" };
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
    head.appendChild(document.createTextNode(
      (TIER_LABEL[msg.tier] || "命中") + "「" + msg.term + "」 " +
      pad(ts.getHours()) + ":" + pad(ts.getMinutes()) + ":" + pad(ts.getSeconds()) +
      (msg.streamer ? " @" + msg.streamer : "")));
    item.appendChild(head);
    item.dataset.session = msg.session || "";
    markAlertItem(item);

    var ctx = document.createElement("div");
    ctx.className = "alert-ctx";
    ctx.textContent = msg.context || "";
    item.appendChild(ctx);

    // 中文一行。报警是最需要人工复核的地方，只给西语原话等于让人没法判断。
    // 译文是后到的（要跑一次强模型），先占位，回来再填。
    var zh = document.createElement("div");
    zh.className = "alert-zh";
    zh.textContent = msg.context_zh || "翻译中…";
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
      tag.textContent = "上一场";
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
  // 页面刚加载（含刷新）时照发一次：上一个页面留在窗口标题上的提醒要清掉
  window.addEventListener("pywebviewready", function () { syncWindowAttention(true); });
  if (window.pywebview && window.pywebview.api) syncWindowAttention(true);

  function updateAlert(msg) {
    var item = alertList.querySelector('[data-alert-id="' + msg.alert_id + '"]');
    if (!item) return;
    var zh = item.querySelector(".alert-zh");
    if (!zh) return;
    zh.classList.remove("pending");
    if (msg.context_zh) {
      zh.textContent = msg.context_zh;
      zh.classList.remove("failed");
      return;
    }
    // 译不出来要说出来。以前这里把整行清空，中控看到一片空白，比停在
    // 「翻译中…」还糟——上面那行西语原话才是他真正要看的东西。
    zh.textContent = "译文失败（" + (msg.why || "未知原因") + "）——请看上面的原话";
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
    var title, cls = "cmt-source", emptyText;
    if (backendState === "connected") {
      title = "评论流已连接";
      cls += " on";
      emptyText = "已连接，等待观众发评论…";
    } else if (backendState === "connecting") {
      // 带说明的连接中（读浏览器登录态、组件刚更新完正在重连）要让中控看得见
      var cdetail = backendDetail || "正在连接评论流…";
      title = cdetail.length > 80 ? cdetail.slice(0, 80) + "…" : cdetail;
      emptyText = cdetail;
    } else if (backendState === "disconnected") {
      title = "评论流断开，重连中…";
      emptyText = title;
    } else if (backendState === "error" || backendState === "unavailable"
               || (backendState && backendState !== "idle" && backendDetail)) {
      // 后端还会发 offline / not_found / login_required / blocked 之类带原因的
      // 状态：没有专门分支的一律把 detail 原样给中控看，别显示成「未连接」
      // 让人以为弹幕功能没启动
      var detail = backendDetail || "";
      title = detail.length > 80 ? detail.slice(0, 80) + "…" : detail;
      cls += " warn";
      emptyText = detail;
    } else {
      title = "未连接";
      emptyText = "开播后自动连接评论流";
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
    head.textContent = (msg.user || "") + " " +
      pad(ts.getHours()) + ":" + pad(ts.getMinutes()) + ":" + pad(ts.getSeconds());
    item.appendChild(head);

    // 译文行：pending 时占位提示，same/skipped 时直接就是原文本身（不会再更新）
    var zh = document.createElement("div");
    zh.className = "cmt-zh";
    if (msg.state === "pending") {
      zh.textContent = "翻译中…";
      zh.classList.add("pending");
    } else {
      zh.textContent = msg.translated || msg.text || "";
    }
    item.appendChild(zh);

    // 原文行：跟译文重复时（same/skipped）没必要再显示一遍
    var orig = document.createElement("div");
    orig.className = "cmt-orig";
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
    var parts = [
      "违禁词最迟 " + fmtMs(det.p50) + " / P95 " + fmtMs(det.p95) + " 内报警",
      "其中 切段 " + fmtMs(seg.p50) + " + 识别 " + fmtMs(asr.p50),
      "译文再等 " + fmtMs(tr.p50),
    ];
    if (msg.audio_backlog_sec >= 3) {
      parts.push("积压 " + msg.audio_backlog_sec.toFixed(0) + "s");
    }
    if (msg.audio_segments_dropped) parts.push("丢音频 " + msg.audio_segments_dropped);
    // 识别跑飞是丢音频的前兆，出现就该看见
    if (msg.asr_overruns) parts.push("识别超时 " + msg.asr_overruns);
    if (msg.translation_jobs_dropped) parts.push("跳过翻译 " + msg.translation_jobs_dropped);
    if (msg.asr_queue_depth || msg.translation_queue_depth) {
      parts.push("积压 " + msg.asr_queue_depth + "/" + msg.translation_queue_depth);
    }
    statsEl.textContent = parts.join(" · ");
    statsEl.classList.remove("hidden");
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
      row.textContent = incidents[k].text;      // 当数据，不当 HTML
      incidentBar.appendChild(row);
    });
    incidentBar.classList.toggle("hidden", keys.length === 0);
  }

  function renderHealth(msg) {
    if (msg.level === "ok") {
      healthBar.classList.add("hidden");
      return;
    }
    healthBar.textContent = msg.text || "";
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
          chip.textContent = "@" + e.streamer;      // textContent：主播名当数据，不当 HTML
          chip.title = "点击开始翻译 @" + e.streamer;
          chip.addEventListener("click", function () {
            roomInput.value = e.url;                // 地址在背后填好，界面上只见主播名
            onRoomInputChanged();                   // 换了主播才按记住的品牌刷新
            startStream();
          });
          recentList.appendChild(chip);
        });
        recentRooms.classList.remove("hidden");
      }
    }
    renderSwitchRecentList();
  }

  if (recentClear) {
    recentClear.addEventListener("click", function () {
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
    diskSummary.textContent = (info.free != null ? "剩余 " + humanSize(info.free) + " · " : "")
      + "本机模型与日志 " + humanSize(total);
    diskList.innerHTML = "";
    if (!diskItems.length) {
      diskList.textContent = "没有找到可管理的模型或日志。";
      syncDiskButton();
      return;
    }
    var ROLE = { in_use: "正在用", app: "本程序", other: "非本程序" };
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
      ? "删除所选（" + ids.length + " 项，" + humanSize(bytes) + "）" : "删除所选";
  }

  if (diskHead) {
    diskHead.addEventListener("click", function () {
      var open = diskBody.classList.contains("hidden");
      diskBody.classList.toggle("hidden", !open);
      document.getElementById("disk-toggle").textContent = open ? "收起" : "管理";
      if (open) {
        diskList.textContent = "正在统计…";
        send({ type: "disk_inventory" });
      }
    });
  }
  if (diskDelete) {
    diskDelete.addEventListener("click", function () {
      var ids = diskSelected();
      if (!ids.length) return;
      var lines = diskItems.filter(function (it) { return ids.indexOf(it.id) !== -1; })
        .map(function (it) { return "· " + it.label + "（" + humanSize(it.size) + "）"; });
      if (!window.confirm("确定删除以下内容？删了就没有了。\n\n" + lines.join("\n"))) return;
      diskDelete.disabled = true;
      diskDelete.textContent = "删除中…";
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
  function updateShareBtn(on) {
    if (!shareBtn) return;
    shareBtn.textContent = on ? "📱 手机同看 · 已打开" : "📱 手机同看";
    shareBtn.classList.toggle("on", on);
  }

  function renderShare(state) {
    if (!shareToggle || !state) return;
    lastShareState = state;
    var on = !!state.on;
    var justOpened = on && !shareOn;
    updateShareBtn(on);

    shareState.textContent = on ? "打开" : "关闭";
    shareState.className = "share-state " + (on ? "on" : "off");
    shareToggle.classList.toggle("hidden", on);   // 打开后靠卡片里的「关闭」按钮，不重复放一个

    if (state.url !== shareLastUrl) {
      shareSelectedIp = null;   // 链接变了（换了链接/重新打开）：候选地址的手选状态失效
      shareLastUrl = state.url;
    }

    if (!on) {
      shareBody.classList.add("hidden");
      // note 有内容时是刚失败的一次尝试（状态 5：端口被占，带真实报错）；否则是普通关闭说明
      shareDesc.textContent = state.note ||
        "关闭。打开后，连着同一个 Wi-Fi 的手机可以扫码看字幕和报警，只能看，不能操作本程序。";
      shareLastIp = null;
      shareOn = false;
      return;
    }

    shareBody.classList.remove("hidden");

    if (!state.ip) {
      // 状态 4：没读到局域网地址，没有二维码
      shareDesc.textContent = state.note || "已打开，但没读到本机的局域网地址。";
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
      shareDesc.textContent = "已打开。手机连同一个 Wi-Fi，扫下面的二维码，或直接打开：" + url;
      shareUrl.textContent = url;      // 大号可选中：用户可以直接长按复制，不必点按钮
      shareUrl.title = "这个链接里带着一把钥匙，当密码看待；发给谁，谁就能看到字幕和报警。";
      shareCount.textContent = "当前 " + n + " 人在看，最多 " + max + " 人。";

      var ok = (typeof renderQR === "function") && renderQR(shareQr, state.qr_rows, 220);
      shareQr.classList.toggle("hidden", !ok);
      if (!ok) {
        // 状态 8：二维码没能生成——note 是后端已经拼好的那句话
        shareNote.textContent = state.note || "二维码没能生成，请让手机手工输入上面的地址。";
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
      shareAddrChanged.textContent = "本机地址已从 " + shareLastIp + " 变为 " + state.ip +
        "，之前发出去的链接需要重新扫码";
      shareAddrChanged.classList.remove("hidden");
    } else {
      shareAddrChanged.classList.add("hidden");
    }
    if (state.ip) shareLastIp = state.ip;

    // A8：从关到开的这一刻，一次性提示系统可能弹出的网络权限确认框；
    // 覆盖掉上面刚设的正常描述，下一次真实状态广播到达（如 A9 的地址重发）会把它换回来
    if (justOpened) {
      shareDesc.textContent = "第一次打开时，系统可能弹出是否允许接受网络连接的确认框（macOS）" +
        "或防火墙提示（Windows），请选允许。";
    }
    shareOn = on;
  }

  // 顶栏按钮只管面板的展开/收起，不碰同看开关本身——同看是否广播局域网端口
  // 完全由卡片里的「打开/关闭」决定，两件事故意分开，收起面板不应该顺手断掉正在看的手机
  if (shareBtn && sharePanel) {
    shareBtn.addEventListener("click", function () {
      sharePanel.classList.toggle("hidden");
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
      // 面板本身也一起收起：点「关闭」的人是要结束同看，没必要还占着字幕历史上方的位置
      if (sharePanel) sharePanel.classList.add("hidden");
    });
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
      var tmpl = (lastShareState && lastShareState.rotate_confirm) ||
        "换链接之后，现在在看的 {n} 台手机会断开，要重新扫码。继续？";
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
        ? "切换期间两个主播都没有字幕，直到 @" + switchArmState.target + " 出现第一句。"
        : "切换期间两个主播都没有字幕，直到新主播出现第一句。";
    }
  }

  // 输入框变化、或点了最近直播间 chip：只「武装」第一步，绝不直接发送
  // （见 web/switch.js）。品牌默认值的刷新规则和开始面板一致
  // （brandStateAfterRoomInput），只是认的是换主播面板自己这份 state 和输入框——
  // 这里换的是「要听谁」（@B），不是「正在听谁」（@A），两份状态不能混
  function armSwitchFromInput() {
    var streamer = streamerFromInput(switchInput.value);
    switchArmState = armSwitch(streamer, currentStreamerName(), Date.now());
    switchBrandState = brandStateAfterRoomInput(switchBrandState, streamer);
    applySwitchDefaultBrand();
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
  // 当前正在监听的那个主播标「监听中」且不可点。
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
        chip.textContent = "@" + e.streamer + "（监听中）";
      } else {
        chip.textContent = "@" + e.streamer;      // textContent：主播名当数据，不当 HTML
        chip.title = "填入并武装改听 @" + e.streamer + "（还要再点一次确认才会真的换）";
        chip.addEventListener("click", function () {
          switchInput.value = e.url;
          armSwitchFromInput();
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
      switchSub.textContent = cur
        ? "当前监听 @" + cur + "，确认前不会中断"
        : "确认前不会中断当前监听";
    }
    if (switchSourceEcho) {
      var opt = sourceSel.options[sourceSel.selectedIndex];
      switchSourceEcho.textContent = opt ? opt.textContent : sourceSel.value;
    }
    renderSwitchRecentList();
    renderSwitchButton();
    switchPanel.classList.remove("hidden");
    switchInput.focus();
  }

  function closeSwitchPanel() {
    if (!switchPanel) return;
    switchPanel.classList.add("hidden");
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
      showSwitchError("与本地服务断开，正在重连——稍候再试。");
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
    switchArmState = initialSwitchState();
    closeSwitchPanel();
  }

  if (switchBtn) switchBtn.addEventListener("click", openSwitchPanel);
  if (switchClose) switchClose.addEventListener("click", closeSwitchPanel);
  if (switchInput) {
    switchInput.addEventListener("input", armSwitchFromInput);
    switchInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") attemptSwitchConfirm();
    });
  }
  if (switchConfirmBtn) switchConfirmBtn.addEventListener("click", attemptSwitchConfirm);
  // Esc 关闭：只在面板确实打开时处理，不吞掉页面别处的 Esc（没有别处在用）
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && switchPanel && !switchPanel.classList.contains("hidden")) {
      closeSwitchPanel();
    }
  });

  // 违禁词警示开关：本地状态 + 顶栏标签 + 首页描述文案，三处一起同步。
  // 来源可能是用户手动扳开关、也可能是后端 hello/alert_mode 广播——不区分来源，
  // 一律走这一个函数，保证三处永远一致。
  function setAlertsEnabled(on) {
    alertsEnabled = !!on;
    if (alertsToggle) alertsToggle.checked = alertsEnabled;
    updateWatchDesc();
    updateAlertModeTag();
  }

  // 顶栏标签：只在连接中/直播中露出（跟其它「直播中才有意义」的 UI 一个逻辑），
  // 待机/已结束/出错/离线时没有场次可言，不该挂着一个「警示关/开」误导人。
  function updateAlertModeTag() {
    if (!alertModeTag) return;
    alertModeTag.classList.toggle("hidden", !streamActive);
    alertModeTag.textContent = alertsEnabled ? "警示开" : "警示关";
    alertModeTag.classList.toggle("on", alertsEnabled);
    alertModeTag.classList.toggle("off", !alertsEnabled);
  }

  // 词表为空时的「未配置」文案原样保留（renderWatchlist 里直接写），跟开关状态
  // 无关——没有词就永远不会命中，这句话本身已经说清楚了。只有词表非空时，
  // watch-desc 才需要按开关状态二选一。
  function updateWatchDesc() {
    if (!watchDesc || !watchlistConfigured) return;
    watchDesc.textContent = alertsEnabled
      ? "开播后会实时监听主播原话，命中立即报警（不依赖翻译，翻译再慢也不影响报警）。"
      : "开播后不报警；命中只记入审计。要报警请先打开此开关。";
  }

  function renderWatchlist(msg) {
    if (!watchState) return;
    watchlistConfigured = msg.count > 0;
    if (msg.count > 0) {
      watchState.textContent = "已启用 · " + msg.count + " 条";
      watchState.className = "watch-state on";
      updateWatchDesc();
    } else {
      watchState.textContent = "未配置";
      watchState.className = "watch-state off";
      watchDesc.textContent = "当前词表为空，本工具不会发出任何违禁词报警。";
    }
  }

  // 自检结果。有失败项时默认展开——「功能悄悄坏了」必须让人一眼看到，
  // 全绿时收起来不打扰
  var ICONS = { ok: "✅", warn: "⚠️", fail: "❌" };
  var scLastSig = null;   // 结论没变就别动展开状态

  function renderSelfcheck(msg) {
    if (!scBox || !msg.checks) return;
    var sum = msg.summary || {};
    scBox.classList.remove("hidden");
    scHead.classList.toggle("has-fail", sum.fail > 0);
    scHead.classList.toggle("has-warn", !sum.fail && sum.warn > 0);
    if (sum.fail) {
      scSummary.textContent = "❌ 自检发现 " + sum.fail + " 项功能未生效";
    } else if (sum.warn) {
      scSummary.textContent = "⚠️ 自检通过，" + sum.warn + " 项提醒";
    } else {
      scSummary.textContent = "✅ 自检全部通过（" + sum.total + " 项）";
    }
    scList.innerHTML = "";
    msg.checks.forEach(function (c) {
      var li = document.createElement("li");
      li.className = "sc-item " + c.level;
      var name = document.createElement("span");
      name.className = "sc-name";
      name.textContent = (ICONS[c.level] || "") + " " + c.name;
      var detail = document.createElement("span");
      detail.className = "sc-detail";
      detail.textContent = c.detail;
      if (c.fix) {
        var fix = document.createElement("span");
        fix.className = "sc-fix";
        fix.textContent = "→ " + c.fix;
        detail.appendChild(fix);
      }
      li.appendChild(name);
      li.appendChild(detail);
      scList.appendChild(li);
    });
    // 只有结论真的变了才自动展开/收起。重连会重放一次 hello，
    // 那时若无条件重置，正在看明细的人会被收起来
    var sig = sum.fail + "/" + sum.warn + "/" + sum.total;
    if (sig !== scLastSig) {
      scLastSig = sig;
      setSelfcheckOpen(sum.fail > 0);
    }
  }

  function setSelfcheckOpen(open) {
    scList.classList.toggle("hidden", !open);
    scToggle.textContent = open ? "收起" : "展开";
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
        fixCmdCopy.textContent = "已复制";
        setTimeout(function () { fixCmdCopy.textContent = "复制"; }, 2000);
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
    fixCmdCopy.textContent = "已选中，按 ⌘C";
    setTimeout(function () { fixCmdCopy.textContent = "复制"; }, 3000);
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
  var NOTES = {
    auto: "默认用本地模型：完全离线、不限量、字幕不出本机。",
    hymt2: "本地模型，离线免费。多数机器用这一档就够。",
    "hymt2-7b": "本地模型，术语更准，但会和语音识别抢内存，可能拖慢报警。",
    deepl: "⚠️ 字幕文本会发送给 DeepL。免费额度以此处显示的用量为准；额度周期与续用方式取决于你的 DeepL 账户方案。",
    claude: "⚠️ 字幕文本会发送给 Anthropic，按用量计费。",
    openai: "⚠️ 字幕文本会发送给该接口的提供方，按用量计费。",
    google: "⚠️ 字幕文本会发送给 Google，且会按 IP 限流。",
    none: "只显示识别原文，不翻译。"
  };
  var engineKeys = {};

  function renderEngine(info) {
    if (!engineSelect) return;
    engineKeys = info.keys || {};
    engineSelect.value = info.engine || "auto";
    var active = info.active ? "当前：" + info.active : "";
    if (info.usage && info.usage.limit) {
      var pct = Math.round(info.usage.used * 100 / info.usage.limit);
      // 35k 字符/小时是实测均值（2026-08-26 场），只做量级提示
      var hours = Math.max(0, Math.floor((info.usage.limit - info.usage.used) / 35000));
      active += " · 免费额度已用 " + pct + "%（按近期速度约剩 " + hours + " 小时）";
    }
    engineActive.textContent = active;
    syncEngineRow();
    // 启动时引擎被回退的提示（如「deepl 缺密钥，本次先用 auto」），
    // 压过常规注记——用户上次的选择被改掉了，必须看得见
    if (info.note) {
      engineNote.textContent = info.note;
      engineNote.classList.add("warn");
    }
  }

  function syncEngineRow() {
    var env = KEY_ENV[engineSelect.value];
    engineKey.classList.toggle("hidden", !env);
    if (env) {
      var have = engineKeys[env];
      engineKey.value = "";
      engineKey.placeholder = have ? "已填 " + have + "（留空则沿用）"
                                   : "粘贴 API 密钥";
    }
    engineNote.textContent = NOTES[engineSelect.value] || "";
    engineNote.classList.toggle("warn", engineSelect.value in KEY_ENV
                                        || engineSelect.value === "google");
  }

  if (engineSelect) {
    engineSelect.addEventListener("change", syncEngineRow);
    engineSave.addEventListener("click", function () {
      engineSave.disabled = true;
      engineSave.textContent = "切换中…";
      send({ type: "set_engine", engine: engineSelect.value,
             api_key: engineKey.value || null });
      engineKey.value = "";
      setTimeout(function () {
        engineSave.disabled = false;
        engineSave.textContent = "保存";
      }, 2500);
    });
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  connect();
})();
