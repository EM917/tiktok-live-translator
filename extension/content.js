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

  // ---- 观众弹幕翻译：从直播页评论区抓取评论，转发给本地服务，译文回贴到评论下方 ----
  var wsRef = null;              // 当前打开的 ws，供 flushComments 发送
  var cmtEnabled = true;         // 选项页可关；默认开
  var pendingCmts = [];          // 合批待发的评论
  var flushTimer = null;
  var seenNodes = new WeakSet(); // 处理过的评论 DOM 节点：同一个节点永远只报一次
  var seenKeys = new Map();      // "user\u0001text" -> 最近一次抓到的时间戳，60 秒内去重
  var nodeById = new Map();      // 评论 id -> {node, text}，收到译文后回贴用
  var cmtCounter = 0;
  var chatObserver = null;
  var chatContainer = null;

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

  // ---- 观众弹幕：抓取 + 合批发送 + 译文回贴 ----

  // 从一条评论 DOM 节点里取出发言人和文本。克隆节点后再摘掉头像图片和用户名，
  // 剩下的就是纯文本内容；直接读原节点会把头像 alt、用户名都混进正文。
  function extractComment(node) {
    var ownerEl = node.querySelector('[data-e2e="message-owner-name"]');
    var user = ownerEl ? ownerEl.textContent.trim() : "";
    var clone = node.cloneNode(true);
    var ownerClone = clone.querySelector('[data-e2e="message-owner-name"]');
    if (ownerClone) ownerClone.parentNode.removeChild(ownerClone);
    var imgs = clone.querySelectorAll("img");
    for (var i = 0; i < imgs.length; i++) {
      imgs[i].parentNode.removeChild(imgs[i]);
    }
    // 克隆节点没挂在文档里，innerText 在部分浏览器上拿不到值，退回 textContent
    var text = (clone.innerText || clone.textContent || "").replace(/\s+/g, " ").trim();
    if (!text) return null;
    return { user: user, text: text };
  }

  // 两层去重，缺一不可：
  //  1. 按节点身份（WeakSet）：兜底扫描每秒都会把评论区里现有的节点再过一遍，
  //     冷清的房间里同一条评论会在 DOM 里挂好几分钟——只按内容加时间窗去重的话，
  //     每过 60 秒它就会被当成新评论再报一次；
  //  2. 按「用户+文本」60 秒窗口：虚拟列表滚动/重渲染时同一条评论会换一个新节点，
  //     节点身份对不上，靠内容兜住。
  function noteComment(node) {
    if (seenNodes.has(node)) return;
    seenNodes.add(node);
    var c = extractComment(node);
    if (!c) return;
    var key = c.user + "\u0001" + c.text;
    var now = Date.now();
    var seenAt = seenKeys.get(key);
    if (seenAt !== undefined && (now - seenAt) < 60000) return;
    seenKeys.set(key, now);
    while (seenKeys.size > 300) {
      seenKeys.delete(seenKeys.keys().next().value);
    }

    var id = "c" + Date.now().toString(36) + "-" + (cmtCounter++);
    nodeById.set(id, { node: node, text: c.text });
    while (nodeById.size > 200) {
      nodeById.delete(nodeById.keys().next().value);
    }

    pendingCmts.push({ id: id, user: c.user, text: c.text });
    if (pendingCmts.length >= 20) {
      flushComments();
    } else if (!flushTimer) {
      flushTimer = setTimeout(flushComments, 400);
    }
  }

  // 尽力而为：连不上就丢这一批，不重试、不缓存到下一批（下一批很快又会来）
  function flushComments() {
    if (flushTimer) {
      clearTimeout(flushTimer);
      flushTimer = null;
    }
    if (pendingCmts.length === 0) return;
    if (wsRef && wsRef.readyState === WebSocket.OPEN) {
      try {
        wsRef.send(JSON.stringify({ type: "viewer_comments", items: pendingCmts }));
      } catch (e) { /* noop，尽力而为 */ }
    }
    pendingCmts = [];
  }

  function scanChat() {
    if (!chatContainer) return;
    var nodes = chatContainer.querySelectorAll('[data-e2e="chat-message"]');
    for (var i = 0; i < nodes.length; i++) noteComment(nodes[i]);
  }

  function handleChatMutations(records) {
    for (var i = 0; i < records.length; i++) {
      var added = records[i].addedNodes;
      for (var j = 0; j < added.length; j++) {
        var n = added[j];
        if (n.nodeType !== 1) continue;
        if (n.matches && n.matches('[data-e2e="chat-message"]')) {
          noteComment(n);
        } else if (n.querySelectorAll) {
          var inner = n.querySelectorAll('[data-e2e="chat-message"]');
          for (var k = 0; k < inner.length; k++) noteComment(inner[k]);
        }
      }
    }
  }

  // 每秒兜底扫描一遍：评论区是虚拟列表，同一时刻只挂 6-7 个 DOM 节点，
  // 弹幕爆发或节点复用时 MutationObserver 会漏掉一些，靠这个补齐。
  // 未登录 TikTok 时评论流约 20 秒后会被冻结（不再有新节点进来），
  // 这里也无能为力——须在该浏览器登录 TikTok 才能持续抓到评论。
  function attachChat() {
    if (!cmtEnabled || !onLivePage()) {
      if (chatObserver) {
        chatObserver.disconnect();
        chatObserver = null;
      }
      chatContainer = null;
      return;
    }
    var container = document.querySelector('[data-e2e="live-chat-container"]');
    if (!container) {
      if (chatObserver) {
        chatObserver.disconnect();
        chatObserver = null;
      }
      chatContainer = null;
      return;
    }
    if (container !== chatContainer) {
      // SPA 换房间：容器换了新节点，旧 observer 已经没用，重新挂
      if (chatObserver) chatObserver.disconnect();
      chatContainer = container;
      chatObserver = new MutationObserver(handleChatMutations);
      chatObserver.observe(chatContainer, { childList: true, subtree: true });
      scanChat();
    } else {
      scanChat();
    }
  }

  setInterval(attachChat, 1000);

  function showCommentZh(msg) {
    var entry = nodeById.get(msg.id);
    if (!entry) return;
    var node = entry.node;
    if (!node.isConnected) return;
    if (!msg.translated || msg.state !== "ok") return;
    // 虚拟列表可能把这个节点复用给了另一条评论：回贴前核对正文还是原来那条，
    // 否则译文会贴到别人的话下面——比不贴更糟
    var current = extractComment(node);
    if (!current || current.text !== entry.text) return;
    var el = node.querySelector(".tlt-cmt-zh");
    if (!el) {
      el = document.createElement("div");
      el.className = "tlt-cmt-zh";
      node.appendChild(el);
    }
    el.textContent = msg.translated;
  }

  // 本地程序在 8765 被占用时会自动改用 8766–8774（见 main.py），
  // 插件必须扫同一段端口，否则「端口漂移」时会误报「程序没开」
  chrome.storage.local.get({ port: 8765, comments: true }, function (cfg) {
    cmtEnabled = cfg.comments !== false;
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
        wsRef = ws;
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
        if (msg.type === "comment_update" && !msg.replay) showCommentZh(msg);
      };
      ws.onclose = function () {
        if (wsRef === ws) wsRef = null;
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
