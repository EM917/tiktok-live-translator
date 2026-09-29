/* G10：虚拟时间约 2500ms 时收集英文模式下仍含 CJK 的文字与属性，写进 #i18n-scan。 */
(function () {
  var CJK = /[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;
  var ATTRS = ["title", "aria-label", "placeholder", "alt"];
  function skipText(node) {                       // 文字：祖先里有 translate=no / script / style 就豁免
    for (var el = node.parentElement; el; el = el.parentElement) {
      if (el.getAttribute("translate") === "no" || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(el.tagName)) return true;
    }
    return false;
  }
  function skipAttrs(el) {                        // 属性：只有**祖先**标了 translate=no 才豁免，元素自己标的不算
    for (var p = el.parentElement; p; p = p.parentElement) if (p.getAttribute("translate") === "no") return true;
    return false;
  }
  function mirrorsData(el, v) {                   // 属性只是把自己里面 translate=no 那段数据原样再写一遍（#active-brand-tag 的 title 就是品牌名）
    var marked = el.querySelectorAll('[translate="no"]');
    for (var i = 0; i < marked.length; i++) if (marked[i].textContent === v) return true;
    return false;
  }
  function where(el) {
    var parts = [];
    for (var e = el; e && e.nodeType === 1 && parts.length < 4; e = e.parentElement) {
      parts.unshift(e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (e.classList.length ? "." + e.classList[0] : ""));
    }
    return parts.join(" > ");
  }
  setTimeout(function () {
    var out = { cjk: [], bridge: [], confirms: [], measure: {} };
    var w = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT);
    for (var n = w.nextNode(); n; n = w.nextNode()) {  // 隐藏元素也扫
      if (CJK.test(n.nodeValue) && !skipText(n)) out.cjk.push({ at: where(n.parentElement), text: n.nodeValue.trim().slice(0, 80) });
    }
    document.querySelectorAll("*").forEach(function (el) {
      if (skipAttrs(el)) return;
      ATTRS.forEach(function (a) {
        var v = el.getAttribute(a);
        if (v && CJK.test(v) && !mirrorsData(el, v)) out.cjk.push({ at: where(el) + " @" + a, text: v.slice(0, 80) });
      });
    });
    if (CJK.test(document.title)) out.cjk.push({ at: "document.title", text: document.title });
    var rec = window.__I18N_REC || {};
    (rec.confirms || []).forEach(function (t) { if (CJK.test(t)) out.cjk.push({ at: "confirm", text: t.slice(0, 80) }); });
    out.bridge = rec.bridge || [];
    out.confirms = rec.confirms || [];               // 全部原生弹窗（不论语言）：用来核对场景里的点击真的点到了
    ((window.__SCENARIO || {}).measure || []).forEach(function (sel) {
      var el = document.querySelector(sel);
      if (!el) { out.measure[sel] = null; return; }
      var r = el.getBoundingClientRect();
      out.measure[sel] = { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, width: r.width, right: r.right };
    });
    out.viewport = { width: window.innerWidth, height: window.innerHeight };
    var s = document.createElement("script");
    s.type = "application/json"; s.id = "i18n-scan"; s.textContent = JSON.stringify(out);
    document.body.appendChild(s);
  }, 2500);
})();
