/* 扩展设置页：保存本地服务端口 */
(function () {
  "use strict";
  var input = document.getElementById("port");
  var commentsInput = document.getElementById("comments");
  var saveBtn = document.getElementById("save");
  var savedTip = document.getElementById("saved");

  chrome.storage.local.get({ port: 8765, comments: true }, function (cfg) {
    input.value = cfg.port;
    commentsInput.checked = cfg.comments !== false;
  });

  saveBtn.addEventListener("click", function () {
    var port = parseInt(input.value, 10);
    if (!port || port < 1 || port > 65535) port = 8765;
    chrome.storage.local.set({ port: port, comments: commentsInput.checked }, function () {
      savedTip.style.visibility = "visible";
      setTimeout(function () { savedTip.style.visibility = "hidden"; }, 1500);
    });
  });
})();
