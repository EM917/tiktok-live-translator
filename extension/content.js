/* TikTok 直播同传 —— 页面叠加字幕（连接本地 tiktok-live-translator 服务）
 * 端口在扩展的「选项」页配置（默认 8765）。
 * 双击字幕条可折叠/展开；按住拖动可移动位置；右上角 × 可隐藏（本次浏览）。 */
(function () {
  "use strict";

  var overlay = null;
  var transEl = null;
  var origEl = null;
  var collapsed = false;
  var disabledForSession = false;   // 用户点了 × ：本次浏览不再显示字幕条
  var hint = null;
  var hintDismissed = false;
  var hadTroubleConnecting = false;
  var failedAttempts = 0;

  // TikTok 是单页应用，注入范围只能放宽到全站——但字幕条只该出现在直播页，
  // 不能在用户刷视频时弹别的直播间的字幕
  function onLivePage() {
    return /\/live(\/|$)/.test(location.pathname);
  }

  // SPA 路由变化不会重载页面：离开直播页时必须收走字幕条和提示条，
  // 否则最后一句字幕会永久冻结在信息流上方
  setInterval(function () {
    if (!onLivePage()) {
      if (overlay) overlay.classList.remove("tlt-visible");
      removeHint();
    }
  }, 1000);

  function ensureOverlay() {
    if (overlay) return;
    overlay = document.createElement("div");
    overlay.id = "tlt-overlay";
    transEl = document.createElement("div");
    transEl.className = "tlt-trans";
    origEl = document.createElement("div");
    origEl.className = "tlt-orig";
    overlay.appendChild(transEl);
    overlay.appendChild(origEl);

    var closeBtn = document.createElement("div");
    closeBtn.className = "tlt-close";
    closeBtn.textContent = "×";
    closeBtn.title = "隐藏字幕条（本次浏览不再显示；刷新页面可恢复）";
    closeBtn.addEventListener("pointerdown", function (e) { e.stopPropagation(); });
    closeBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      disabledForSession = true;
      overlay.classList.remove("tlt-visible");
    });
    overlay.appendChild(closeBtn);

    document.documentElement.appendChild(overlay);

    overlay.addEventListener("dblclick", function () {
      collapsed = !collapsed;
      overlay.classList.toggle("tlt-collapsed", collapsed);
    });

    // 拖动：用 pointer capture，鼠标移出窗口/中途松开都能正确结束
    var dragging = false, offX = 0, offY = 0;
    overlay.addEventListener("pointerdown", function (e) {
      dragging = true;
      var rect = overlay.getBoundingClientRect();
      offX = e.clientX - rect.left;
      offY = e.clientY - rect.top;
      overlay.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    overlay.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      overlay.style.left = (e.clientX - offX) + "px";
      overlay.style.top = (e.clientY - offY) + "px";
      overlay.style.bottom = "auto";
      overlay.style.transform = "none";
    });
    function endDrag(e) {
      dragging = false;
      try { overlay.releasePointerCapture(e.pointerId); } catch (err) { /* noop */ }
    }
    overlay.addEventListener("pointerup", endDrag);
    overlay.addEventListener("pointercancel", endDrag);
  }

  function showCaption(msg) {
    if (disabledForSession || !onLivePage()) return;
    ensureOverlay();
    var main = msg.translated || msg.original || "";
    if (!main) return;
    transEl.textContent = main;
    if (msg.translated && msg.original) {
      origEl.textContent = msg.original;
      origEl.style.display = "";
    } else {
      origEl.style.display = "none";
    }
    overlay.classList.add("tlt-visible");
  }

  // ---- 连接状态反馈：本地程序没开时，用户不该对着空页面猜 ----
  function showHint() {
    if (hint || hintDismissed || disabledForSession || !onLivePage()) return;
    hadTroubleConnecting = true;
    hint = document.createElement("div");
    hint.id = "tlt-hint";
    var text = document.createElement("span");
    text.textContent = "未检测到本地翻译程序——请先打开 TikTok 直播同传" +
      "（macOS 双击 TikTok Live Translator.app，Windows 双击 Start.bat）。" +
      "若程序已打开，请刷新本页，或到插件「选项」检查端口设置。";
    var x = document.createElement("span");
    x.className = "tlt-hint-close";
    x.textContent = "×";
    x.title = "知道了";
    x.addEventListener("click", function () {
      hintDismissed = true;
      removeHint();
    });
    hint.appendChild(text);
    hint.appendChild(x);
    document.documentElement.appendChild(hint);
  }

  function removeHint() {
    if (hint) {
      hint.remove();
      hint = null;
    }
  }

  function flashToast(text) {
    var t = document.createElement("div");
    t.id = "tlt-toast";
    t.textContent = text;
    document.documentElement.appendChild(t);
    setTimeout(function () { t.remove(); }, 2500);
  }

  // 本地程序在 8765 被占用时会自动改用 8766–8774（见 main.py），
  // 插件必须扫同一段端口，否则「端口漂移」时会误报「程序没开」
  chrome.storage.local.get({ port: 8765 }, function (cfg) {
    var candidates = [cfg.port];
    for (var p = 8765; p <= 8774; p++) {
      if (p !== cfg.port) candidates.push(p);
    }
    var idx = 0;

    function connect() {
      var port = candidates[idx % candidates.length];
      var ws;
      try {
        ws = new WebSocket("ws://127.0.0.1:" + port + "/ws");
      } catch (e) {
        idx++;
        failedAttempts++;
        if (failedAttempts >= candidates.length + 2) showHint();
        setTimeout(connect, 1200);
        return;
      }
      var opened = false;
      ws.onopen = function () {
        opened = true;
        failedAttempts = 0;
        removeHint();
        if (hadTroubleConnecting) {   // 只在刚从故障中恢复时提示，平时不打扰
          hadTroubleConnecting = false;
          if (onLivePage()) flashToast("✓ 已连接本地翻译程序");
        }
      };
      ws.onmessage = function (evt) {
        var msg;
        try { msg = JSON.parse(evt.data); } catch (e) { return; }
        if (msg.type === "caption" && !msg.replay) showCaption(msg);
      };
      ws.onclose = function () {
        if (!opened) idx++;      // 没连上过才换端口；连上过的端口掉线原地重试
        failedAttempts++;
        if (failedAttempts >= candidates.length + 2) showHint();
        setTimeout(connect, opened ? 3000 : 1200);
      };
      ws.onerror = function () { try { ws.close(); } catch (e) { /* noop */ } };
    }

    connect();
  });
})();
