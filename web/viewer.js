/* 手机同看页面逻辑。只读：不引用 app.js/alerts.js/follow.js（VIEWER_FILES 只有
   四个文件是安全性质，见 spec §12），贴底跟随那 12 行在下面故意重写了一份。

   本文件分两段：
     1. 纯函数——不摸 document/window，node:test 直接 require 这份文件测。
     2. 浏览器专用——真机上才跑，用 `typeof document` 挡住，Node 环境下整段跳过，
        这样纯函数照样能被测，不会因为 document 不存在而在 require 时就报错。 */
"use strict";

// ============ 一、纯函数 ============

// 连接状态文案。n 用于 reconnecting 的倒计时秒数 / stale 的静默秒数。
var CONN_TEXT = {
  connecting: "连接中…",
  connected: "已连接",
  reconnecting: "已断开，{n} 秒后重连…",
  denied: "链接已失效，请向中控要新的二维码",
  full: "同看人数已满（12 人），稍后再试",
  off: "中控已关闭手机同看",
  stale: "{n} 秒没有新消息",
};

function statusText(state, n) {
  var t = CONN_TEXT[state];
  if (!t) return "状态未知";
  return t.replace("{n}", String(n == null ? 0 : n));
}

// 直播状态文案：与 app/pipeline.py 里 server.status() 实际用到的取值一致
// （idle/connecting/live/error，见 spec §12「实现者请核对」一条，已用
// `grep -n 'server.status(' app/pipeline.py` 核对，只有这四种）。
var STREAM_TEXT = {
  idle: "未开始",
  connecting: "连接中",
  live: "直播中",
  error: "已停止",
};

function streamText(state) {
  return STREAM_TEXT[state] || "状态未知";
}

// 重连退避：0.5/1/2/4/8/15 秒封顶，±20% 抖动。抖动之后再夹到 [400, 15000]——
// 这个下限正好是 500ms 的 -20%，上限正好是 15000ms 的封顶本身，保证任何
// rand() 取值下界都不会失守，序列也保持单调不减（同一个 rand 对整段序列
// 是同一个缩放因子，缩放不改变单调性）。
var BACKOFF_STEPS = [500, 1000, 2000, 4000, 8000, 15000];
var BACKOFF_MIN = 400;
var BACKOFF_MAX = 15000;

function backoffDelay(n, rand) {
  var r = typeof rand === "function" ? rand : Math.random;
  var i = n < 0 || n == null ? 0 : n;
  var base = BACKOFF_STEPS[Math.min(i, BACKOFF_STEPS.length - 1)];
  var jitter = (r() * 2 - 1) * 0.2;   // ±20%
  var v = Math.round(base * (1 + jitter));
  return Math.max(BACKOFF_MIN, Math.min(BACKOFF_MAX, v));
}

// 让「回放的 alert」与「随后实时同 id 的 alert」只渲染一份：只按 alert_id 取键，
// 不掺 replay 标记——回放先到、实时后到，同一个 key 命中同一张卡片，后到的
// 走 alert_update 的更新路径，不会重复插入。
function alertKey(msg) {
  return "alert:" + (msg && msg.alert_id);
}

// caption_update / alert_update 通用合并：只覆盖消息里真正带来的键，
// 不带的键留着已有值——不带 translated 的 caption_update 不能把已有译文清空，
// 这是 app/server.py:163-170 就地改写语义的手机版（spec §12 用例 61）。
function applyUpdate(existing, msg) {
  var out = {};
  var base = existing || {};
  Object.keys(base).forEach(function (k) { out[k] = base[k]; });
  Object.keys(msg || {}).forEach(function (k) {
    if (k === "type" || k === "id" || k === "alert_id") return;
    out[k] = msg[k];
  });
  return out;
}

// 只在「新报警」且提示音开关开启时响；回放的报警（重连/刷新补发的历史）
// 永不响铃——那不是新情况，响铃只对着眼下正在发生的事。
function shouldRing(msg, ringEnabled) {
  return !!(ringEnabled && msg && msg.type === "alert" && !msg.replay);
}

// URL 片段里取 token：只认 "#k=<非空>"，其余（空串、"#"、"#k="、"?k=..."、
// "#kk=..."）一律 null——片段不会进服务端日志也不会进 Referer，query 会，
// 所以 token 只能放片段，这里也只认片段。
function tokenFromHash(hash) {
  var h = String(hash || "");
  var m = h.match(/^#k=([^&]+)$/);
  return m ? m[1] : null;
}

// 重译按钮文案。state 是这条字幕当前的 strong_state（服务端 caption/caption_update
// 带来的；从没发起过重译是 null/undefined），strong 是这条字幕是否已经是大模型译文。
// 规则：从没发起过重译、且已经是大模型译文（比如桌面端按过，或回放带来的）——
// 按钮没意义，隐藏；只要发起过一次（pending/ok/failed 任一），就一直给反馈，
// 哪怕之后 strong 变成 true 也要先亮一下「已重译」，不能让人以为白点了。
function retranslateLabel(state, strong) {
  if (state == null) {
    return strong
      ? { hidden: true, text: "已重译", disabled: true }
      : { hidden: false, text: "重译", disabled: false };
  }
  if (state === "pending") return { hidden: false, text: "重译中…", disabled: true };
  if (state === "ok") return { hidden: false, text: "已重译", disabled: true };
  if (state === "failed") return { hidden: false, text: "重译失败，可再试", disabled: false };
  return { hidden: false, text: "重译", disabled: !!strong };   // 未知取值，兜底成可点
}

// 重译点击的本地节流：3 秒内只认第一次点击，断线时一律不让点。这只是本地体验
// （服务端 ACTION_GAP_SEC 才是真正的闸，值保持一致纯是为了不让人白点白等）。
var RETRANSLATE_COOLDOWN_MS = 3000;
function canRetranslate(lastTapTs, now, connected) {
  if (!connected) return false;
  if (lastTapTs == null) return true;
  return (now - lastTapTs) >= RETRANSLATE_COOLDOWN_MS;
}

// ---- 取消警报（本地隐藏，只在这台手机上生效）----
// 不发服务端：桌面报警面板和审计日志都不受影响。storage 可注入（真 localStorage
// 或测试里的假存储）；取不到/抛错都不影响功能，只是刷新后记不住已取消的报警——
// 这本身就是「本地、best-effort」的设计，不是缺陷。
var DISMISSED_ALERTS_KEY = "viewerDismissedAlerts";
var DISMISSED_ALERTS_MAX = 500;

function loadDismissedAlerts(storage) {
  try {
    var raw = storage && storage.getItem(DISMISSED_ALERTS_KEY);
    if (!raw) return [];
    var arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.map(String) : [];
  } catch (e) { return []; }
}

function saveDismissedAlerts(storage, ids, max) {
  try {
    if (!storage) return;
    var cap = max > 0 ? max : DISMISSED_ALERTS_MAX;
    var capped = ids.length > cap ? ids.slice(ids.length - cap) : ids;
    storage.setItem(DISMISSED_ALERTS_KEY, JSON.stringify(capped));
  } catch (e) { /* 没有存储也要能正常用，只是刷新后记不住 */ }
}

// 工厂：内存态 + best-effort 持久化，浏览器分支和测试共用同一套 has/add/addMany。
// 只留最新 max 个（默认 500，超过就挤掉最老的）——最近取消的更值得记住。
function createDismissedStore(storage, max) {
  var cap = max > 0 ? max : DISMISSED_ALERTS_MAX;
  var ids = loadDismissedAlerts(storage);
  var set = Object.create(null);
  ids.forEach(function (id) { set[id] = true; });

  function addOne(id) {
    var key = String(id);
    if (set[key]) return false;
    set[key] = true;
    ids.push(key);
    while (ids.length > cap) delete set[ids.shift()];
    return true;
  }

  return {
    has: function (id) { return !!set[String(id)]; },
    add: function (id) {
      if (addOne(id)) saveDismissedAlerts(storage, ids, cap);
    },
    addMany: function (list) {
      var changed = false;
      (list || []).forEach(function (id) { if (addOne(id)) changed = true; });
      if (changed) saveDismissedAlerts(storage, ids, cap);
    },
  };
}

// ============ 二、浏览器专用：DOM + WebSocket ============
// Node 环境没有 document，整段跳过；node:test 只用得到上面的纯函数。
if (typeof document !== "undefined") {
  (function () {
    var MAX_ALERTS = 50;      // 与回放上限一致（spec §5）
    var MAX_COMMENTS = 30;    // 手机端默认折叠，只留最近这么多条（负责人定稿：三个待定问题）
    var MAX_CAPTIONS = 200;   // 回放上限 100 条 + 直播中持续追加，留够余量不无限长
    var STALE_MS = 45000;     // 已连接但 45 秒没有新消息：叠加显示，不改变连接状态本身
    var STALE_RECONNECT_SEC = 60;   // A9：重连超过这么久，文案升级为找中控确认

    var connDot = document.getElementById("conn-dot");
    var connText = document.getElementById("conn-text");
    var streamTextEl = document.getElementById("stream-text");
    var alertBadge = document.getElementById("alert-badge");
    var alertCountEl = document.getElementById("alert-count");
    var staleLine = document.getElementById("stale-line");
    var demoBanner = document.getElementById("demo-banner");
    var incidentList = document.getElementById("incident-list");
    var healthLine = document.getElementById("health-line");
    var alertSection = document.getElementById("alert-section");
    var alertList = document.getElementById("alert-list");
    var captionList = document.getElementById("caption-list");
    var jumpBtn = document.getElementById("jump-latest");
    var commentToggle = document.getElementById("comment-toggle");
    var commentCountEl = document.getElementById("comment-count");
    var commentList = document.getElementById("comment-list");
    var ringToggle = document.getElementById("ring-toggle");
    var alertClearSeenBtn = document.getElementById("alert-clear-seen");

    function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
    function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 忽略 */ } }

    // 取消警报：本地 best-effort 存储，注入的读写就是上面两个已经带 try/catch 的
    // 小函数——没有 localStorage 也不影响这场的取消，只是刷新后记不住。
    var dismissedAlerts = createDismissedStore({ getItem: lsGet, setItem: lsSet });

    function pad(n) { return n < 10 ? "0" + n : String(n); }
    function hhmmss(ts) {
      var d = new Date((ts || Date.now() / 1000) * 1000);
      return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    }

    // ---- 只看模式的提示音/振动：首次点击才建 AudioContext（自动播放策略），
    //      偏好存 localStorage，只在新报警上响，字幕不响 ----
    var ringEnabled = lsGet("viewerRingEnabled") === "1";
    var audioCtx = null;
    function syncRingBtn() {
      if (!ringToggle) return;
      ringToggle.classList.toggle("on", ringEnabled);
      ringToggle.setAttribute("aria-pressed", ringEnabled ? "true" : "false");
    }
    function ensureAudio() {
      if (audioCtx) return;
      try {
        var Ctor = window.AudioContext || window.webkitAudioContext;
        if (Ctor) audioCtx = new Ctor();
      } catch (e) { /* 拿不到就算了，静音不影响看字幕 */ }
      if (navigator.vibrate) { try { navigator.vibrate(1); } catch (e) { /* 忽略 */ } }
    }
    function playRing() {
      if (!audioCtx) return;
      try {
        var osc = audioCtx.createOscillator();
        var gain = audioCtx.createGain();
        osc.frequency.value = 880;
        gain.gain.value = 0.18;
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start();
        osc.stop(audioCtx.currentTime + 0.22);
      } catch (e) { /* 忽略 */ }
      if (navigator.vibrate) { try { navigator.vibrate([80, 60, 80]); } catch (e) { /* 忽略 */ } }
    }
    if (ringToggle) {
      syncRingBtn();
      ringToggle.addEventListener("click", function () {
        ringEnabled = !ringEnabled;
        ensureAudio();
        lsSet("viewerRingEnabled", ringEnabled ? "1" : "0");
        syncRingBtn();
      });
    }

    // ---- 字幕区贴底跟随：follow.js 的 12 行重写版（故意不复用，见 spec §12） ----
    var FOLLOW_SLACK = 120;
    var following = true;
    function atBottom() {
      return captionList.scrollHeight - captionList.scrollTop - captionList.clientHeight < FOLLOW_SLACK;
    }
    if (captionList) {
      captionList.addEventListener("scroll", function () {
        if (!captionList.clientHeight) return;   // 不可测量时沿用原意图，不猜
        following = atBottom();
        if (jumpBtn) jumpBtn.classList.toggle("hidden", following);
      });
    }
    function stickCaptions() {
      if (!captionList) return;
      if (following) captionList.scrollTop = captionList.scrollHeight;
      else if (jumpBtn) jumpBtn.classList.remove("hidden");
    }
    if (jumpBtn) {
      jumpBtn.addEventListener("click", function () {
        following = true;
        jumpBtn.classList.add("hidden");
        stickCaptions();
      });
    }

    // ---- 弹幕区：默认折叠，展开/收起记忆在 localStorage ----
    var commentsOpen = lsGet("viewerCommentsOpen") === "1";
    function syncCommentPanel() {
      if (!commentList) return;
      commentList.classList.toggle("hidden", !commentsOpen);
    }
    syncCommentPanel();
    if (commentToggle) {
      commentToggle.addEventListener("click", function () {
        commentsOpen = !commentsOpen;
        lsSet("viewerCommentsOpen", commentsOpen ? "1" : "0");
        syncCommentPanel();
      });
    }

    // ---- 连接状态机 ----
    var TOKEN = tokenFromHash(location.hash);
    var ws = null;
    var retryCount = 0;
    var reconnectTimer = null;
    var connState = "connecting";
    var lastConnectedAt = null;   // 上一次收到 viewer_hello 的时间（ms）
    var lastMessageAt = null;     // 上一次收到任意消息的时间（ms），驱动 45 秒静默提示
    var staleReconnectShown = 0;  // 上一次渲染时用的重连累计秒数，避免每次 tick 都重排 DOM

    function renderConnBar() {
      if (!connText || !connDot) return;
      connDot.className = "dot " + connState;
      var text;
      if (connState === "reconnecting" && lastConnectedAt) {
        var elapsedSec = Math.round((Date.now() - lastConnectedAt) / 1000);
        if (elapsedSec > STALE_RECONNECT_SEC) {
          // A9：重连拖得够久，普通的「N 秒后重连」已经不够用——告诉人可以做什么
          text = "已经 " + elapsedSec + " 秒没连上。请找中控确认同看是否还开着，或重新扫码";
          staleReconnectShown = elapsedSec;
        } else {
          text = statusText(connState, pendingRetrySec);
        }
      } else {
        text = statusText(connState, pendingRetrySec);
      }
      connText.textContent = text;
    }

    var pendingRetrySec = 0;   // 下一次重连前的倒计时秒数，供 statusText 的 {n} 用

    function tickStaleLine() {
      if (!staleLine) return;
      if (connState === "connected" && lastMessageAt
          && Date.now() - lastMessageAt > STALE_MS) {
        var sec = Math.round((Date.now() - lastMessageAt) / 1000);
        staleLine.textContent = statusText("stale", sec);
        staleLine.classList.remove("hidden");
      } else {
        staleLine.classList.add("hidden");
      }
      // 重连状态下的倒计时/累计秒数也要跟着秒表刷新，不能只在状态切换那一刻画一次
      if (connState === "reconnecting") renderConnBar();
    }
    setInterval(tickStaleLine, 1000);

    function wsUrl() {
      var proto = location.protocol === "https:" ? "wss:" : "ws:";
      return proto + "//" + location.host + "/vws";
    }

    function setConnState(state) {
      connState = state;
      renderConnBar();
      renderStreamStale();
      refreshAllRetranslateButtons();   // 断线/重连都要跟着刷「重译」按钮的可点状态
    }

    // 断线（reconnecting/denied/full/off）时不能让 stream-text 停在最后一次
    // 收到的「直播中」不动——那正是 app/server.py:221-225 那条教训的手机版
    // （spec §12：「绝不在断线时继续显示『直播中』」）。之前只有 setConnState
    // 更新 conn-text/conn-dot，stream-text 只由 "status" 消息写，断线期间没人
    // 碰它，于是整段 A9 重连升级期间头条都在说「直播中」。这里断线就立刻把它
    // 盖成「状态未知」并调暗；重连成功后紧跟 viewer_hello 的那条 status 回放
    // （spec §5 第 2 步）会马上把真实值刷回来。
    function renderStreamStale() {
      if (!streamTextEl) return;
      if (connState === "connected") {
        streamTextEl.classList.remove("stale");
      } else {
        streamTextEl.textContent = "状态未知";
        streamTextEl.classList.add("stale");
      }
    }

    function scheduleReconnect(slow) {
      if (reconnectTimer) return;
      var n = slow ? BACKOFF_STEPS.length - 1 : retryCount++;
      var delay = backoffDelay(n);
      pendingRetrySec = Math.round(delay / 1000);
      renderConnBar();
      reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        connect();
      }, delay);
    }

    function connect() {
      if (!TOKEN) { setConnState("denied"); return; }   // 拿不到 token：连文案都不发起连接
      setConnState(connState === "reconnecting" || connState === "full" || connState === "off"
        ? connState : "connecting");
      var socket;
      try {
        socket = new WebSocket(wsUrl());
      } catch (e) {
        scheduleReconnect(false);
        return;
      }
      ws = socket;
      socket.onopen = function () {
        try { socket.send(JSON.stringify({ type: "auth", k: TOKEN })); }
        catch (e) { try { socket.close(); } catch (e2) { /* 忽略 */ } }
      };
      socket.onmessage = function (evt) {
        lastMessageAt = Date.now();
        var msg;
        try { msg = JSON.parse(evt.data); } catch (e) { return; }
        handle(msg);
      };
      socket.onclose = function (evt) {
        if (ws !== socket) return;   // 旧连接的收尾事件，已经换了新连接
        ws = null;
        onSocketDown(evt && evt.code);
      };
      socket.onerror = function () {
        try { socket.close(); } catch (e) { /* 忽略 */ }
      };
    }

    function onSocketDown(code) {
      if (code === 4401) { setConnState("denied"); return; }         // 不再重连
      if (code === 4429) { setConnState("full"); scheduleReconnect(true); return; }
      if (code === 4403) { setConnState("off"); scheduleReconnect(true); return; }
      // 4408（鉴权超时）或没有关闭帧（queue_full / send_timeout / inbound_flood 的 abort）：
      // 走正常重连退避
      setConnState("reconnecting");
      scheduleReconnect(false);
    }

    // ---- 消息处理 ----
    var alertsById = Object.create(null);
    var captionsById = Object.create(null);
    var commentsById = Object.create(null);
    var demoMode = false;
    var lastRetranslateTapAt = Object.create(null);   // 每条字幕自己的本地节流时间戳

    function resetForNewSession() {
      alertsById = Object.create(null);
      captionsById = Object.create(null);
      commentsById = Object.create(null);
      lastRetranslateTapAt = Object.create(null);   // 新的一场：seq 从头计，旧的冷却时间戳不该跟过来
      if (alertList) alertList.innerHTML = "";
      if (captionList) captionList.innerHTML = "";
      if (commentList) commentList.innerHTML = "";
      if (alertCountEl) alertCountEl.textContent = "0";
      if (alertBadge) alertBadge.classList.add("hidden");
      if (commentCountEl) commentCountEl.textContent = "0";
      demoMode = false;
      updateDemoBanner();
    }

    function updateDemoBanner() {
      if (demoBanner) demoBanner.classList.toggle("hidden", !demoMode);
    }

    function handle(msg) {
      if (!msg || typeof msg !== "object") return;
      switch (msg.type) {
        case "viewer_hello":
          retryCount = 0;
          lastConnectedAt = Date.now();
          resetForNewSession();
          setConnState("connected");
          break;
        case "viewer_denied":
          // 关闭码本身已经决定了最终文案（onSocketDown），这里不用再动状态；
          // 只是不让它落进 default 分支被当成未知消息忽略。
          break;
        case "status":
          if (streamTextEl) streamTextEl.textContent = streamText(msg.state);
          if (msg.state === "live" || msg.state === "connecting") {
            demoMode = false;
            updateDemoBanner();
          }
          break;
        case "incident":
          renderIncident(msg);
          break;
        case "health":
          renderHealth(msg);
          break;
        case "alert":
          renderAlert(msg);
          break;
        case "alert_update":
          updateAlert(msg);
          break;
        case "caption":
          renderCaption(msg);
          break;
        case "caption_update":
          updateCaption(msg);
          break;
        case "comment":
          renderComment(msg);
          break;
        case "comment_update":
          updateComment(msg);
          break;
        case "comment_source":
          renderCommentSource(msg);
          break;
        default:
          break;   // 白名单以外的类型服务端本就不会发；未知类型安静忽略
      }
    }

    // ---- 持续提示（磁盘空间不足之类）----
    var incidents = Object.create(null);
    function renderIncident(msg) {
      if (!msg || !msg.id) return;
      if (msg.level === "clear") delete incidents[msg.id];
      else incidents[msg.id] = { level: msg.level || "warn", text: msg.text || "" };
      drawIncidents();
    }
    function drawIncidents() {
      if (!incidentList) return;
      incidentList.innerHTML = "";
      Object.keys(incidents).forEach(function (k) {
        var row = document.createElement("div");
        var level = incidents[k].level;
        row.className = "incident " + (level === "error" ? "error" : (level === "info" ? "info" : "warn"));
        row.textContent = incidents[k].text;   // 当数据，不当 HTML
        incidentList.appendChild(row);
      });
    }

    // ---- 识别健康度：白名单只留 level，文案按 level 查表自己生成 ----
    var HEALTH_TEXT = {
      lagging: "⚠️ 识别开始落后，报警会有延迟",
      degraded: "🔴 检测已降级，识别明显落后",
    };
    function renderHealth(msg) {
      if (!healthLine) return;
      var text = msg && HEALTH_TEXT[msg.level];
      if (!text) { healthLine.classList.add("hidden"); return; }
      healthLine.textContent = text;
      healthLine.className = "health-line " + msg.level;
      healthLine.classList.remove("hidden");
    }

    // ---- 违禁词报警：最显眼的红卡片，新的插最前，最多 50 条 ----
    var TIER_LABEL = { exact: "🔴 精确", variant: "🟠 变体", fuzzy: "🟡 疑似" };
    function renderAlert(msg) {
      if (!alertSection || !alertList) return;
      // 之前在本机取消过：回放（重连/刷新补发的历史）也不复活。不是「自动取消」，
      // 是记住了「取消过」——真正的新报警（新 alert_id）永远照常显示。
      if (dismissedAlerts.has(msg.alert_id)) return;
      if (msg.demo) { demoMode = true; updateDemoBanner(); }
      alertSection.classList.remove("hidden");
      var item = document.createElement("div");
      item.className = "alert-item";
      item.dataset.key = alertKey(msg);

      var head = document.createElement("div");
      head.className = "alert-head";
      var headLabel = document.createElement("span");
      headLabel.className = "alert-head-label";
      headLabel.textContent = (TIER_LABEL[msg.tier] || "命中") + " 「" + (msg.term || "") + "」 " + hhmmss(msg.ts);
      head.appendChild(headLabel);
      var dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.className = "alert-dismiss";
      dismissBtn.setAttribute("aria-label", "取消这条报警（只在本机隐藏）");
      dismissBtn.textContent = "✕";
      dismissBtn.addEventListener("click", function () { dismissAlert(msg.alert_id); });
      head.appendChild(dismissBtn);
      item.appendChild(head);

      var ctx = document.createElement("div");
      ctx.className = "alert-ctx";
      ctx.textContent = msg.context || "";
      item.appendChild(ctx);

      var zh = document.createElement("div");
      zh.className = "alert-zh" + (msg.context_zh ? "" : " pending");
      zh.textContent = msg.context_zh || "中文正在补…";
      item.appendChild(zh);

      alertsById[item.dataset.key] = { msg: msg, el: item, zhEl: zh };
      alertList.insertBefore(item, alertList.firstChild);
      while (alertList.children.length > MAX_ALERTS) {
        var last = alertList.lastChild;
        delete alertsById[last.dataset.key];
        alertList.removeChild(last);
      }
      if (alertCountEl) alertCountEl.textContent = String(alertList.children.length);
      if (alertBadge) alertBadge.classList.remove("hidden");

      if (shouldRing(msg, ringEnabled)) playRing();
    }
    function updateAlert(msg) {
      var entry = alertsById[alertKey(msg)];
      if (!entry) return;
      entry.msg = applyUpdate(entry.msg, msg);
      if (entry.msg.context_zh) {
        entry.zhEl.textContent = entry.msg.context_zh;
        entry.zhEl.classList.remove("pending", "failed");
      } else if (entry.msg.failed) {
        entry.zhEl.textContent = "中文译不出来（" + (entry.msg.why || "未知原因") + "）——请看上面的原话";
        entry.zhEl.classList.remove("pending");
        entry.zhEl.classList.add("failed");
      }
    }
    // 摘掉一张已经在列表里的报警卡片：本地取消（✕）和「清除已看过」共用。
    // 只改 DOM 和这台手机自己的 alertsById，不发服务端、不碰审计。
    function removeAlertCard(key) {
      var entry = alertsById[key];
      if (!entry) return;
      delete alertsById[key];
      if (entry.el.parentNode) entry.el.parentNode.removeChild(entry.el);
      if (alertCountEl) alertCountEl.textContent = String(alertList.children.length);
      if (alertBadge && !alertList.children.length) alertBadge.classList.add("hidden");
    }
    // ✕：取消单条。只在这台手机上隐藏，桌面报警面板和审计日志都不受影响。
    function dismissAlert(alertId) {
      dismissedAlerts.add(alertId);
      removeAlertCard(alertKey({ alert_id: alertId }));
    }
    // 「清除已看过的报警」：把这台手机眼下还显示着的报警一次性全部取消。
    function clearSeenAlerts() {
      var ids = Object.keys(alertsById).map(function (key) { return alertsById[key].msg.alert_id; });
      dismissedAlerts.addMany(ids);
      ids.forEach(function (id) { removeAlertCard(alertKey({ alert_id: id })); });
    }
    if (alertClearSeenBtn) alertClearSeenBtn.addEventListener("click", clearSeenAlerts);

    // ---- 重译：状态完全由服务端 caption/caption_update 的 strong/strong_state 驱动，
    //      本地只管节流和发送，不本地伪造「重译中」——避免服务端真实状态和手机
    //      显示的不一致 ----
    function applyRetranslateButton(id) {
      var entry = captionsById[id];
      if (!entry || !entry.btnEl) return;
      var label = retranslateLabel(entry.msg.strong_state, entry.msg.strong);
      entry.btnEl.classList.toggle("hidden", label.hidden);
      entry.btnEl.textContent = label.text;
      entry.btnEl.disabled = label.disabled || connState !== "connected";
    }
    function refreshAllRetranslateButtons() {
      Object.keys(captionsById).forEach(applyRetranslateButton);
    }
    function onRetranslateTap(id) {
      var connected = connState === "connected";
      if (!canRetranslate(lastRetranslateTapAt[id], Date.now(), connected)) return;
      if (!ws) return;
      lastRetranslateTapAt[id] = Date.now();
      try { ws.send(JSON.stringify({ type: "retranslate", id: id })); }
      catch (e) { /* 发不出去就算了，冷却过了能再点 */ }
    }

    // ---- 字幕：时间顺序，自动跟到底 ----
    function renderCaption(msg) {
      if (!captionList) return;
      if (msg.demo) { demoMode = true; updateDemoBanner(); }
      var card = document.createElement("div");
      card.className = "cap-item";
      card.dataset.id = msg.id;

      var meta = document.createElement("div");
      meta.className = "cap-meta";
      meta.textContent = hhmmss(msg.ts) + (msg.src_lang ? " · " + msg.src_lang : "");
      card.appendChild(meta);

      var orig = document.createElement("div");
      orig.className = "cap-orig";
      orig.textContent = msg.original || "";
      card.appendChild(orig);

      var trans = document.createElement("div");
      trans.className = "cap-trans";
      card.appendChild(trans);

      var retranslateBtn = document.createElement("button");
      retranslateBtn.type = "button";
      retranslateBtn.className = "cap-retranslate";
      retranslateBtn.addEventListener("click", function () { onRetranslateTap(msg.id); });
      card.appendChild(retranslateBtn);

      captionList.appendChild(card);
      captionsById[msg.id] = { msg: msg, el: card, transEl: trans, btnEl: retranslateBtn };
      while (captionList.children.length > MAX_CAPTIONS) {
        var oldest = captionList.firstChild;
        delete captionsById[oldest.dataset.id];
        captionList.removeChild(oldest);
      }
      applyCaptionState(msg, trans);
      applyRetranslateButton(msg.id);
      stickCaptions();
    }
    function applyCaptionState(msg, transEl) {
      transEl.classList.remove("pending", "failed");
      if (msg.translated) {
        transEl.textContent = msg.translated;
      } else if (msg.failed) {
        transEl.textContent = "译文失败（" + (msg.why || "未知原因") + "）——请看上面的原话";
        transEl.classList.add("failed");
      } else if (msg.translate_state === "pending") {
        transEl.textContent = "翻译中…";
        transEl.classList.add("pending");
      } else {
        transEl.textContent = "";
      }
    }
    function updateCaption(msg) {
      var entry = captionsById[msg.id];
      if (!entry) return;
      entry.msg = applyUpdate(entry.msg, msg);
      applyCaptionState(entry.msg, entry.transEl);
      applyRetranslateButton(msg.id);
    }

    // ---- 弹幕：默认折叠，只留最近 30 条 ----
    function renderComment(msg) {
      if (!commentList) return;
      var item = document.createElement("div");
      item.className = "cmt-item";
      item.dataset.id = msg.id;

      var head = document.createElement("div");
      head.className = "cmt-head";
      head.textContent = (msg.user || "") + " " + hhmmss(msg.ts);
      item.appendChild(head);

      var zh = document.createElement("div");
      zh.className = "cmt-zh";
      if (msg.state === "pending") {
        zh.textContent = "翻译中…";
        zh.classList.add("pending");
      } else {
        zh.textContent = msg.translated || msg.text || "";
      }
      item.appendChild(zh);

      var orig = document.createElement("div");
      orig.className = "cmt-orig";
      orig.textContent = msg.text || "";
      if (msg.state === "same" || msg.state === "skipped") orig.classList.add("hidden");
      item.appendChild(orig);

      commentList.appendChild(item);
      commentsById[msg.id] = { msg: msg, el: item, zhEl: zh };
      while (commentList.children.length > MAX_COMMENTS) {
        var oldest = commentList.firstChild;
        delete commentsById[oldest.dataset.id];
        commentList.removeChild(oldest);
      }
      if (commentCountEl) commentCountEl.textContent = String(commentList.children.length);
    }
    function updateComment(msg) {
      var entry = commentsById[msg.id];
      if (!entry) return;
      entry.msg = applyUpdate(entry.msg, msg);
      if (entry.msg.state === "pending") {
        entry.zhEl.textContent = "翻译中…";
        entry.zhEl.classList.add("pending");
      } else {
        entry.zhEl.textContent = entry.msg.translated || entry.msg.text || "";
        entry.zhEl.classList.remove("pending");
      }
    }

    var COMMENT_SOURCE_TEXT = {
      idle: "弹幕未连接",
      connecting: "弹幕连接中…",
      live: "弹幕已连接",
      unavailable: "弹幕暂时不可用",
    };
    function renderCommentSource(msg) {
      if (!commentToggle) return;
      var backend = msg && msg.backend;
      var text = COMMENT_SOURCE_TEXT[backend] || COMMENT_SOURCE_TEXT.unavailable;
      commentToggle.dataset.sourceText = text;
      // 标题旁的小字状态；条数照旧显示在 commentCountEl 里，两者不互相覆盖
      var label = commentToggle.querySelector(".cmt-source-label");
      if (label) label.textContent = text;
    }

    connect();
  })();
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    statusText: statusText,
    streamText: streamText,
    backoffDelay: backoffDelay,
    alertKey: alertKey,
    applyUpdate: applyUpdate,
    shouldRing: shouldRing,
    tokenFromHash: tokenFromHash,
    retranslateLabel: retranslateLabel,
    canRetranslate: canRetranslate,
    loadDismissedAlerts: loadDismissedAlerts,
    saveDismissedAlerts: saveDismissedAlerts,
    createDismissedStore: createDismissedStore,
  };
}
