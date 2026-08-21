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

  function startStream() {
    var url = roomInput.value.trim();
    if (!url) { roomInput.focus(); return; }
    if (!/^https?:\/\//.test(url)) {
      roomInput.value = "https://" + url.replace(/^\/+/, "");
      url = roomInput.value;
    }
    localStorage.setItem("roomUrl", url);
    localStorage.setItem("sourceLang", sourceSel.value);
    send({ type: "start", url: url, source: sourceSel.value });
  }

  startBtn.addEventListener("click", startStream);
  roomInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") startStream();
  });
  stopBtn.addEventListener("click", function () { send({ type: "stop" }); });

  function applyFont(size) {
    document.documentElement.style.setProperty("--sub-size", size + "px");
  }

  // ---- WebSocket ----
  function connect() {
    ws = new WebSocket("ws://" + location.host + "/ws");

    ws.onopen = function () { retries = 0; };

    ws.onclose = function () {
      setStatus({ state: "offline", detail: "" });
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

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  function handle(msg) {
    switch (msg.type) {
      case "hello":
        // 重连时服务器会重发历史，先清掉本地已有的，避免重复
        clearBtn.click();
        if (msg.config) {
          if (msg.config.status) setStatus(msg.config.status);
          if (msg.config.target_lang) targetSel.value = msg.config.target_lang;
          if (msg.config.room_url && !roomInput.value) roomInput.value = msg.config.room_url;
        }
        break;
      case "status":
        setStatus(msg);
        break;
      case "config":
        if (msg.target_lang) targetSel.value = msg.target_lang;
        if (msg.room_url) roomInput.value = msg.room_url;
        break;
      case "caption":
        renderCaption(msg);
        break;
    }
  }

  // ---- 渲染 ----
  function setStatus(msg) {
    var state = msg.state || "idle";
    statusDot.className = "dot " + state;
    statusText.textContent = STATUS_TEXT[state] || state;

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

  function renderCaption(msg) {
    var main = msg.translated || msg.original || "";
    var hasBoth = Boolean(msg.translated && msg.original);

    var card = document.createElement("div");
    card.className = "cap";

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
    if (msg.translate_state === "failed") {
      var failChip = document.createElement("span");
      failChip.className = "lang-chip fail-chip";
      failChip.textContent = "翻译失败";
      meta.appendChild(failChip);
    }
    card.appendChild(meta);

    if (hasBoth) {
      var orig = document.createElement("div");
      orig.className = "orig";
      orig.textContent = msg.original;
      card.appendChild(orig);
    }
    var trans = document.createElement("div");
    trans.className = "trans";
    trans.textContent = main;
    card.appendChild(trans);

    var nearBottom = historyEl.scrollHeight - historyEl.scrollTop - historyEl.clientHeight < 120;
    historyEl.appendChild(card);

    var caps = historyEl.querySelectorAll(".cap");
    if (caps.length > maxHistory) caps[0].remove();

    if (nearBottom || msg.replay) historyEl.scrollTop = historyEl.scrollHeight;

    // 底部大字幕（回放历史时不更新，避免闪一串旧字幕）
    if (!msg.replay) {
      liveBar.classList.remove("hidden");
      liveTranslated.textContent = main;
      liveOriginal.textContent = hasBoth ? msg.original : "";
      liveOriginal.classList.toggle("hidden", !hasBoth);
    }
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  connect();
})();
