/* TikTok 直播同传 —— 前端逻辑：WebSocket 收字幕、渲染历史 + 底部大字幕、启动/停止直播间 */
(function () {
  "use strict";

  var historyEl = document.getElementById("history");
  var startPanel = document.getElementById("start-panel");
  var roomInput = document.getElementById("room-input");
  var sourceSel = document.getElementById("source-lang");
  var startBtn = document.getElementById("start-btn");
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
  var updateBar = document.getElementById("update-bar");
  var updateText = document.getElementById("update-text");
  var updateBtn = document.getElementById("update-btn");
  var updateLink = document.getElementById("update-link");
  var versionEl = document.getElementById("app-version");
  var alertPanel = document.getElementById("alert-panel");
  var alertList = document.getElementById("alert-list");
  var alertCount = document.getElementById("alert-count");
  var clearAlertsBtn = document.getElementById("clear-alerts");
  var statsEl = document.getElementById("stats-line");
  var healthBar = document.getElementById("health-bar");
  var watchState = document.getElementById("watch-state");
  var watchDesc = document.getElementById("watch-desc");
  var fixCmd = document.getElementById("fix-command");
  var fixCmdText = document.getElementById("fix-command-text");
  var fixCmdCopy = document.getElementById("fix-command-copy");
  var scBox = document.getElementById("selfcheck");
  var scHead = document.getElementById("sc-head");
  var scSummary = document.getElementById("sc-summary");
  var scToggle = document.getElementById("sc-toggle");
  var scList = document.getElementById("sc-list");

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
  var liveBarId = null;      // 底部大字幕当前显示的是哪一条（译文回来要就地替换）
  // 字幕先出原文、译文后补，所以要能按 id 找回已渲染的那张卡片
  var cardsById = {};

  // ---- 设置 ----
  // 只有用户显式调过字号才覆盖 CSS 默认值（否则会压掉移动端媒体查询的 26px）
  var savedFont = localStorage.getItem("subFontSize");
  if (savedFont) {
    fontSlider.value = parseInt(savedFont, 10);
    applyFont(parseInt(savedFont, 10));
  } else {
    var cssSize = parseInt(getComputedStyle(document.documentElement)
      .getPropertyValue("--sub-size"), 10);
    if (cssSize) fontSlider.value = cssSize;
  }

  var savedSource = localStorage.getItem("sourceLang");
  if (savedSource) sourceSel.value = savedSource;
  var savedRoom = localStorage.getItem("roomUrl");
  if (savedRoom) roomInput.value = savedRoom;

  fontSlider.addEventListener("input", function () {
    var size = parseInt(fontSlider.value, 10);
    applyFont(size);
    localStorage.setItem("subFontSize", String(size));
  });

  // 目标语言以服务端为准（跟随 --target 启动参数），UI 切换即时生效但不做本地持久化
  targetSel.addEventListener("change", function () {
    send({ type: "set_target", value: targetSel.value });
  });

  clearBtn.addEventListener("click", function () {
    var caps = historyEl.querySelectorAll(".cap");
    for (var i = 0; i < caps.length; i++) caps[i].remove();
    cardsById = {};              // 卡片没了，id 映射也要清，否则一直涨
    liveBarId = null;
    liveBar.classList.add("hidden");
  });

  // 地址输入归一化在 normalize.js（独立成文件以便单元测试），
  // 此处使用其暴露的全局函数 normalizeRoomInput

  function startStream() {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setStatus({ state: "offline", detail: "与本地服务断开，正在重连——稍候再点「开始翻译」" });
      return;
    }
    var raw = roomInput.value.trim();
    if (!raw) { roomInput.focus(); return; }
    var url = normalizeRoomInput(raw);
    if (!url) {
      setStatus({ state: "error",
                  detail: "认不出这个输入：请粘贴直播间链接，或输入主播的英文用户名" +
                          "（到主播主页复制 @ 后面的部分，中文昵称不行）。" });
      return;
    }
    roomInput.value = url;
    localStorage.setItem("roomUrl", url);
    localStorage.setItem("sourceLang", sourceSel.value);
    // 「开始」指令必须确认送达：半死连接上 send 会无声进黑洞（readyState 还是
    // OPEN），随后自动重连成功、页面若无其事地回到待机——用户点了却毫无反应。
    // 服务器收到 start 后会立刻回执 connecting 状态；在那之前指令算「在途」，
    // 重连后的 hello 里补发一次，超时仍无回执才提示用户手点。
    pendingStart = { payload: { type: "start", url: url, source: sourceSel.value },
                     retried: false };
    send(pendingStart.payload);
    armStartWatchdog();
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
  stopBtn.addEventListener("click", function () {
    pendingStart = null;                             // 在途的「开始」随之作废
    if (startWatchdog) clearTimeout(startWatchdog);
    send({ type: "stop" });
  });

  updateBtn.addEventListener("click", function () {
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

  function showVersionNote(text, ms) {
    if (versionNoticeTimer) clearTimeout(versionNoticeTimer);
    versionEl.textContent = " · " + text;
    versionNoticeTimer = setTimeout(restoreVersion, ms);
  }

  function restoreVersion() {
    if (versionNoticeTimer) clearTimeout(versionNoticeTimer);
    versionNoticeTimer = null;
    if (currentVersion) versionEl.textContent = " · v" + currentVersion;
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
        // 失败连击也归零——回放的陈旧字幕不该累积成新警告
        failStreak = 0;
        transBannerOn = false;
        clearBtn.click();
        if (msg.config) {
          if (msg.config.watchlist) renderWatchlist(msg.config.watchlist);
          if (msg.config.selfcheck) renderSelfcheck(msg.config.selfcheck);
          if (msg.config.engine) renderEngine(msg.config.engine);
          if (msg.config.status) setStatus(msg.config.status);
          if (msg.config.target_lang) targetSel.value = msg.config.target_lang;
          if (msg.config.room_url && !roomInput.value) roomInput.value = msg.config.room_url;
          if (msg.config.version) {
            currentVersion = msg.config.version;
            versionEl.textContent = " · v" + msg.config.version;
          }
          if (msg.config.update) showUpdate(msg.config.update);
          else {
            updateBar.classList.add("hidden");
            updateBtn.disabled = false;
            updateBtn.textContent = "一键更新";
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
        if (msg.room_url) roomInput.value = msg.room_url;
        break;
      case "caption":
        renderCaption(msg);
        break;
      case "caption_update":
        updateCaption(msg);
        break;
      case "alert_update":
        updateAlert(msg);
        break;
      case "alert":
        renderAlert(msg);
        break;
      case "stats":
        renderStats(msg);
        break;
      case "health":
        renderHealth(msg);
        break;
      case "watchlist":
        renderWatchlist(msg);
        break;
      case "engine":
        renderEngine(msg);
        break;
      case "selfcheck":
        renderSelfcheck(msg);
        break;
    }
  }

  // ---- 渲染 ----
  function setStatus(msg) {
    var state = msg.state || "idle";
    statusDot.className = "dot " + state;
    statusText.textContent = STATUS_TEXT[state] || state;

    // 更新失败/被拒绝后恢复「一键更新」按钮，允许再试
    if (state === "error" || state === "idle") {
      updateBtn.disabled = false;
      updateBtn.textContent = "一键更新";
    }

    var active = state === "live" || state === "connecting";
    startPanel.classList.toggle("hidden", active);
    stopBtn.classList.toggle("hidden", !active);
    startBtn.disabled = state === "connecting";

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
    stickToBottom(msg.replay);

    // 底部大字幕：回放历史时不逐条更新（避免闪一串旧字幕），
    // 但最后一条带 restore 标记，用它把大字幕恢复成断线前的样子
    if (!msg.replay || msg.restore) {
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
      liveOriginal.textContent = liveTranslated.textContent;
      liveOriginal.classList.remove("hidden");
      liveTranslated.textContent = msg.translated;
    }
    trackTranslateHealth(msg);
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

  function renderAlert(msg) {
    alertPanel.classList.remove("hidden");
    var item = document.createElement("div");
    item.className = "alert-item tier-" + (msg.tier || "exact");

    var head = document.createElement("div");
    head.className = "alert-head";
    var ts = new Date((msg.ts || Date.now() / 1000) * 1000);
    head.textContent = (TIER_LABEL[msg.tier] || "命中") + "「" + msg.term + "」 " +
      pad(ts.getHours()) + ":" + pad(ts.getMinutes()) + ":" + pad(ts.getSeconds());
    item.appendChild(head);

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
    while (alertList.children.length > 50) {
      alertList.removeChild(alertList.lastChild);
    }
    alertCount.textContent = alertList.children.length;
  }

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
  function renderWatchlist(msg) {
    if (!watchState) return;
    if (msg.count > 0) {
      watchState.textContent = "已启用 · " + msg.count + " 条";
      watchState.className = "watch-state on";
      watchDesc.textContent = "开播后会实时监听主播原话，命中立即报警（不依赖翻译，"
        + "翻译再慢也不影响报警）。";
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
  var following = true;

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

  function stickToBottom(force) {
    if (force) following = true;
    if (following) scrollToBottomNow();
    updateJumpButton();
  }

  // 按钮只在「确实有内容在下面」时出现。不可测量时不显示——那时什么都判断不了，
  // 摆一个按不动的按钮只会添乱。
  function updateJumpButton() {
    if (!jumpBtn) return;
    var show = isMeasurable(historyEl) && !atBottom(historyEl);
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
  // 兜底：桌面窗口被遮挡时 visibilitychange 未必触发。跟随状态下若发现
  // 不在底部就补上，代价是每 2 秒读一次几何量。
  setInterval(function () {
    if (document.hidden) return;
    if (needsResync(historyEl, following)) scrollToBottomNow();
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
    deepl: "⚠️ 字幕文本会发送给 DeepL。免费额度每月 100 万字符，约合 30 小时监听。",
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
    engineActive.textContent = info.active ? "当前：" + info.active : "";
    syncEngineRow();
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
