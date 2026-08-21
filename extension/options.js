/* 扩展设置页：保存本地服务端口 */
(function () {
  "use strict";
  var input = document.getElementById("port");
  var saveBtn = document.getElementById("save");
  var savedTip = document.getElementById("saved");

  chrome.storage.local.get({ port: 8765 }, function (cfg) {
    input.value = cfg.port;
  });

  saveBtn.addEventListener("click", function () {
    var port = parseInt(input.value, 10);
    if (!port || port < 1 || port > 65535) port = 8765;
    chrome.storage.local.set({ port: port }, function () {
      savedTip.style.visibility = "visible";
      setTimeout(function () { savedTip.style.visibility = "hidden"; }, 1500);
    });
  });
})();
