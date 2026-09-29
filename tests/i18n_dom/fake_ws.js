/* G10 装置：替换 WebSocket，按 window.__SCENARIO 推消息、点元素、记下原生弹窗和 JS 桥调用。只在测试页里注入。 */
(function () {
  var S = window.__SCENARIO || {};
  var rec = window.__I18N_REC = { confirms: [], bridge: [], sent: [] };
  window.confirm = function (t) { rec.confirms.push(String(t)); return false; };
  window.alert = function (t) { rec.confirms.push(String(t)); };
  window.prompt = function (t) { rec.confirms.push(String(t)); return null; };
  if (S.page === "desktop") {                     // pywebview 桥：任何方法都记下来并返回已完成的 Promise
    window.pywebview = { api: new Proxy({}, { get: function (_, name) {
      return function () { rec.bridge.push([String(name)].concat([].slice.call(arguments))); return Promise.resolve(null); };
    } }) };
  }
  function FakeWS(url) {
    var ws = this; this.url = url; this.readyState = 0;
    setTimeout(function () {
      ws.readyState = 1; if (ws.onopen) ws.onopen({});
      (S.messages || []).forEach(function (m) { if (ws.onmessage) ws.onmessage({ data: JSON.stringify(m) }); });
      if (S.close) {                               // 离线横幅、手机 denied/full：推完就关
        ws.readyState = 3; if (ws.onclose) ws.onclose({ code: S.close.code || 1006, reason: "", wasClean: false });
      }
    }, 20);
  }
  FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
  FakeWS.prototype.send = function (d) {
    rec.sent.push(String(d));
    var m = {}; try { m = JSON.parse(d); } catch (e) { return; }
    var reply = (S.replies || {})[m.type], ws = this;  // 例：disk_inventory → {type:"disk", ...}
    if (reply) setTimeout(function () { if (ws.onmessage) ws.onmessage({ data: JSON.stringify(reply) }); }, 10);
  };
  FakeWS.prototype.close = function () {};
  window.WebSocket = FakeWS;
  window.addEventListener("load", function () {
    if (S.page === "desktop") window.dispatchEvent(new Event("pywebviewready"));
    var i = 0, clicks = S.clicks || [];
    function next() {                              // 依次点：展开设置各行、输入说明、同看面板、换主播并武装、磁盘勾选、rotate
      if (i >= clicks.length) return;
      var el = document.querySelector(clicks[i++]); if (el) el.click();
      setTimeout(next, 60);
    }
    setTimeout(next, 100);                         // 等上面 20ms 的消息先到：load 常早于它，先点的行会被 hello 重画收起
  });
})();
