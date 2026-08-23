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
    document.title = "有新版本 · TikTok 直播同传";
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
      case "alert":
        renderAlert(msg);
        break;
      case "stats":
        renderStats(msg);
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

  // 字幕先出原文、译文后补，所以要能按 id 找回已渲染的那张卡片
  var cardsById = {};

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

    var nearBottom = historyEl.scrollHeight - historyEl.scrollTop - historyEl.clientHeight < 120;
    historyEl.appendChild(card);
    cardsById[msg.id] = card;

    var caps = historyEl.querySelectorAll(".cap");
    while (caps.length > maxHistory) {
      delete cardsById[caps[0].dataset.id];
      caps[0].remove();
      caps = historyEl.querySelectorAll(".cap");
    }

    applyTranslation(card, msg);
    if (nearBottom || msg.replay) historyEl.scrollTop = historyEl.scrollHeight;

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

    alertList.insertBefore(item, alertList.firstChild);
    while (alertList.children.length > 50) {
      alertList.removeChild(alertList.lastChild);
    }
    alertCount.textContent = alertList.children.length;
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
    // 用户真实体感 = 等这句话切完（片段时长）+ 识别耗时
    var feltP50 = (seg.p50 != null && e2e.p50 != null) ? seg.p50 + e2e.p50 : null;
    var feltP95 = (seg.p95 != null && e2e.p95 != null) ? seg.p95 + e2e.p95 : null;
    var parts = [
      "首字等待 P50 " + fmtMs(feltP50) + " / P95 " + fmtMs(feltP95),
      "其中 切段 " + fmtMs(seg.p50) + " + 识别 " + fmtMs(asr.p50),
      "译文再等 " + fmtMs(tr.p50),
    ];
    if (msg.audio_segments_dropped) parts.push("丢音频 " + msg.audio_segments_dropped);
    if (msg.translation_jobs_dropped) parts.push("跳过翻译 " + msg.translation_jobs_dropped);
    if (msg.asr_queue_depth || msg.translation_queue_depth) {
      parts.push("积压 " + msg.asr_queue_depth + "/" + msg.translation_queue_depth);
    }
    statsEl.textContent = parts.join(" · ");
    statsEl.classList.remove("hidden");
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  connect();
})();
