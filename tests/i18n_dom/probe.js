/* G10 防闪探针。页面里插两次：
   第一次在页面自己的脚本之前（场景、fake_ws.js 之前），记下这时 body 是否可见——英文页上防闪规则
   （style.css / viewer.css 的 :not(.i18n-done) body）必须让它是 hidden，否则首帧会闪一下中文；
   第二次在 scan.js 之前，0ms 后再记一次：这时应当已经可见、<html lang> 已定、i18n-done 已加上。
   结果写进 #i18n-probe（scan.js 只管 #i18n-scan）。 */
(function () {
  var P = window.__I18N_PROBE;
  if (!P) {
    window.__I18N_PROBE = { before: getComputedStyle(document.body).visibility };
    return;
  }
  setTimeout(function () {
    var root = document.documentElement;
    P.first = getComputedStyle(document.body).visibility;
    P.lang = root.getAttribute("lang");
    P.done = root.classList.contains("i18n-done");
    var s = document.createElement("script");
    s.type = "application/json"; s.id = "i18n-probe"; s.textContent = JSON.stringify(P);
    document.body.appendChild(s);
  }, 0);
})();
