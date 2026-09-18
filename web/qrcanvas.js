/* 桌面「手机同看」卡片：把 ViewerHub.state() 给的 qr_rows（每行一个 "0"/"1" 字符串，
   已经含 quiet zone）画进 <canvas>。服务端只发数字矩阵、不发 SVG 字符串，画布这边
   也就不需要对服务端来的内容用 innerHTML——qr_rows 本身没有可注入的余地。

   devicePixelRatio 缩放必须按整数模块像素来算：每个模块本该是一块纯色方块，
   非整数缩放会让相邻模块之间出现半像素模糊边，手机摄像头对着模糊边缘常识别不出来。 */

/* 纯函数：给定二维码矩阵行、期望的 CSS 显示边长、设备像素比，算出
   「不出现小数缩放」的画布尺寸方案。不摸 DOM，供 node 测试直接调用。

   modulePx 向上取整：宁可让最终显示边长（cssSize）比调用方要的略大，
   也不允许比要求的小——否则二维码会被压扁到看不清。
   rows 非法（不是数组、空、非方阵、含 "0"/"1" 之外的字符）一律返回 null。 */
function qrCanvasPlan(rows, cssSize, dpr) {
  if (!Array.isArray(rows) || rows.length === 0) return null;
  var n = rows.length;
  for (var i = 0; i < n; i++) {
    var row = rows[i];
    if (typeof row !== "string" || row.length !== n) return null;
    if (!/^[01]+$/.test(row)) return null;
  }
  var size = Number(cssSize);
  if (!isFinite(size) || size <= 0) return null;
  var ratio = Number(dpr);
  if (!isFinite(ratio) || ratio <= 0) ratio = 1;
  var modulePx = Math.max(1, Math.ceil((size * ratio) / n));
  var devicePx = modulePx * n;
  return {
    modules: n,          // 含 quiet zone 的矩阵边长，原样保留，不在这里裁剪
    modulePx: modulePx,  // 每个模块占的设备像素，整数
    devicePx: devicePx,  // canvas.width / canvas.height（设备像素）
    cssSize: devicePx / ratio,   // canvas.style.width / height（CSS 像素），>= 调用方要求的 cssSize
  };
}

/* 把 qr_rows 画进一个 <canvas>：按 qrCanvasPlan 设好 width/height 与
   style.width/height，关掉抗锯齿，逐模块画实心方块。
   plan 为 null（rows 非法）时清空画布并返回 false——调用方据此显示
   「二维码没能生成」那句文案，不需要自己重复校验一遍矩阵。 */
function renderQR(canvas, rows, cssSize) {
  if (!canvas || typeof canvas.getContext !== "function") return false;
  var dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
  var plan = qrCanvasPlan(rows, cssSize, dpr);
  if (!plan) {
    canvas.width = 0;
    canvas.height = 0;
    return false;
  }
  canvas.width = plan.devicePx;
  canvas.height = plan.devicePx;
  canvas.style.width = plan.cssSize + "px";
  canvas.style.height = plan.cssSize + "px";
  var ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, plan.devicePx, plan.devicePx);
  ctx.fillStyle = "#000000";
  for (var y = 0; y < rows.length; y++) {
    var r = rows[y];
    for (var x = 0; x < r.length; x++) {
      if (r.charAt(x) === "1") {
        ctx.fillRect(x * plan.modulePx, y * plan.modulePx, plan.modulePx, plan.modulePx);
      }
    }
  }
  return true;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { qrCanvasPlan: qrCanvasPlan, renderQR: renderQR };
}
