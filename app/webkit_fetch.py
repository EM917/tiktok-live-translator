"""用系统自带的 WebKit 引擎加载直播页，把播放器实际拿到的流地址交出来。

为什么要有这一层（2026-09-05 实录）：一个正在直播的房间，TikTok 的房间接口只回
`status_code 4003110`、不给流地址；yt-dlp 报「未开播」；程序抓的直播页 HTML 里也没有
`streamData`——TikTok 只把这个房间的流地址交给「真正的浏览器」：页面脚本要先走一次
带签名的 `room/enter`，成功后才把 `streamData` 写进 `SIGI_STATE`。程序自己签不了名
（Euler Stream 的签名接口是付费档），无头 / 临时资料的 Chrome 也被判成机器人（403）。
而 macOS 的 WebKit（pywebview 在 mac 上用的 WKWebView，也是 Safari 的引擎）不登录
就被放行：实测隐藏窗口 2 秒内 `streamData` 就位、视频开播。

所以：起一个**子进程**（pywebview 的事件循环必须占主线程），开一个隐藏窗口加载直播页，
每半秒读一次 `SIGI_STATE`，拿到流地址就按一行 JSON 打印后退出。父进程只认 stdout 的
那一行，超时就 kill——浏览器引擎的任何异常都关在这个子进程里，主链路不受影响。

用法：python -m app.webkit_fetch <直播间地址> [--timeout 秒]
输出（单行 JSON）：
    {"url": "https://pull-…/….flv?…", "status": 2}     拿到了
    {"offline": true, "status": 4}                     页面明确说已下播
    {"error": "…"}                                     没拿到 / 引擎出错
退出码：0 拿到；3 已下播；1 其它。
"""
import argparse
import json
import sys
import time

# 页面里读 SIGI_STATE 的脚本：返回 JSON 字符串，永不抛异常（抛了就是空对象）。
# 与 resolver._pick_stream 同一套挑法：纯音频档（ao）优先，其次任意档的 flv，
# 再退到 flv_pull_url / hls。
_READ_JS = r"""
(() => {
  try {
    const s = JSON.parse(document.getElementById('SIGI_STATE').textContent);
    const lr = ((s.LiveRoom || {}).liveRoomUserInfo || {}).liveRoom || {};
    const out = { status: lr.status === undefined ? null : lr.status, url: null };
    const sd = lr.streamData || {};
    let raw = (sd.pull_data || {}).stream_data;
    if (typeof raw === 'string') { try { raw = JSON.parse(raw); } catch (e) { raw = null; } }
    const opts = ((raw || {}).data) || {};
    const ao = ((opts.ao || {}).main || {}).flv;
    if (ao) { out.url = ao; return JSON.stringify(out); }
    for (const k of Object.keys(opts)) {
      const flv = ((opts[k] || {}).main || {}).flv;
      if (flv) { out.url = flv; return JSON.stringify(out); }
    }
    const flvs = sd.flv_pull_url || {};
    for (const k of Object.keys(flvs)) { if (flvs[k]) { out.url = flvs[k]; return JSON.stringify(out); } }
    out.url = sd.rtmp_pull_url || sd.hls_pull_url || null;
    return JSON.stringify(out);
  } catch (e) {
    return JSON.stringify({ status: null, url: null, err: String(e).slice(0, 120) });
  }
})()
"""

LIVE_STATUS = 2


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _hide_dock_icon():
    """mac 上别在 Dock 里闪一个 Python 火箭：把进程设成「附件」策略（无 Dock 图标、
    不抢焦点）。拿不到 AppKit 就算了，只是体面问题。"""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().setActivationPolicy_(1)   # NSApplicationActivationPolicyAccessory
    except Exception:
        pass


def _poll(window, timeout, result):
    """在 pywebview 的工作线程里轮询页面，拿到结果就写进 result 并关窗。"""
    deadline = time.time() + timeout
    started = time.time()
    last = None
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            raw = window.evaluate_js(_READ_JS)
            data = json.loads(raw) if raw else {}
        except Exception as exc:          # 页面还没就绪 / 引擎抖动：下一轮再试
            last = {"error": "evaluate_js: {}".format(exc)[:200]}
            continue
        if not isinstance(data, dict):
            continue
        last = data
        status = data.get("status")
        if data.get("url"):
            result.update({"url": data["url"], "status": status})
            break
        if status is not None and status != LIVE_STATUS:
            # 页面明确说已下播——比「没拿到」多一层确定性，父进程据此不再重试
            result.update({"offline": True, "status": status})
            break
        if status is None and time.time() - started > 8:
            # 8 秒了页面里连 LiveRoom 都没有：不是直播页（用户名打错、被封、
            # 地址不对），继续等只是白等
            result.update({"error": "page has no live room data"})
            break
    else:
        result.update({"error": "timeout ({}s), last={}".format(timeout, json.dumps(last)[:160])})
    try:
        window.destroy()
    except Exception:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app.webkit_fetch")
    p.add_argument("url")
    p.add_argument("--timeout", type=float, default=25.0)
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        import webview
    except ImportError:
        _emit({"error": "pywebview not installed"})
        return 1
    _hide_dock_icon()
    result = {}
    try:
        window = webview.create_window("tiktok-live-translator", args.url,
                                       width=1100, height=750, hidden=True)
        # private_mode=False 是刻意的：TikTok 的 room/enter 要靠首次访问种下的
        # ttwid/msToken 才放行——实测干净的隐私窗口对某些房间 20 秒都拿不到，
        # 而带着上次的 cookie 2 秒就位。cookie 只存在系统 WebKit 的本地数据里。
        webview.start(_poll, (window, args.timeout, result), private_mode=False)
    except Exception as exc:
        result = {"error": "webview: {}".format(exc)[:200]}
    _emit(result)
    if result.get("url"):
        return 0
    if result.get("offline"):
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main())
