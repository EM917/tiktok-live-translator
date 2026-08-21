/* TikTok 直播同传 —— 页面叠加字幕（连接本地 tiktok-live-translator 服务）
 * 端口在扩展的「选项」页配置（默认 8765）。
 * 双击字幕条可折叠/展开；按住拖动可移动位置。 */
(function () {
  "use strict";

  var overlay = null;
  var transEl = null;
  var origEl = null;
  var collapsed = false;

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

  function connect(wsUrl) {
    var ws;
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      setTimeout(function () { connect(wsUrl); }, 5000);
      return;
    }
    ws.onmessage = function (evt) {
      var msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      if (msg.type === "caption" && !msg.replay) showCaption(msg);
    };
    ws.onclose = function () { setTimeout(function () { connect(wsUrl); }, 3000); };
    ws.onerror = function () { try { ws.close(); } catch (e) { /* noop */ } };
  }

  chrome.storage.local.get({ port: 8765 }, function (cfg) {
    connect("ws://127.0.0.1:" + cfg.port + "/ws");
  });
})();
