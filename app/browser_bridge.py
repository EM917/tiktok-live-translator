"""借用用户自己已登录的 Chrome 取直播流地址。

背景（2026-09-05 实录）：一个正在直播的房间，程序四条解析路径全部失败——
TikTok 的房间接口对它固定返回 4003110（见 resolver.py 的 AGE_GATE_CODE 注释，
这个代码不只用于年龄限制），yt-dlp 报「未开播」，直播页 HTML 里也没有流地址。
但用户自己那个已登录的 Chrome 能正常播放：播放器走的是带签名的 room/enter，
程序自己签不出来（商业版签名服务要付费，无头/临时资料的 Chrome 又被 TikTok
拒绝 403）。折中方案：在用户的 Chrome 里打开这个直播间页面，Chrome 插件
（extension/background.js 用 chrome.webRequest 旁观播放器实际发起的请求，
extension/content.js 转发）把地址回传给本地服务，这里再把它交给
resolve_stream_url 之后的老路径（_check_media_url + _media_url_works + ffmpeg）。

这个模块只负责「等一个地址」，不碰 ffmpeg/ASR/检测——违禁词检测跑在 ASR 的
raw_text 上，跟流地址怎么来的无关。
"""
import asyncio
import subprocess
import time
from urllib.parse import urlparse

from .resolver import ResolveError

# 插件送回的流地址必须落在这些 CDN 域名下——对应 extension/manifest.json 的
# host_permissions。这条通道的入站方是「view」来源（TikTok 页面本身的脚本），
# 内容不可信：不校验的话，一个被篡改/挟持的页面就能让 ffmpeg 去拉任意地址。
_ALLOWED_HOST_SUFFIXES = (
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokcdn-eu.com",
    "tiktokv.com",
    "byteoversea.com",
)


def _valid_stream_url(url):
    """插件回传的地址要不要收：字符串、不超长、https、主机名落在白名单 CDN 下。"""
    if not isinstance(url, str) or not url or len(url) > 2048:
        return False
    if not url.startswith("https://"):
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == suf or host.endswith("." + suf) for suf in _ALLOWED_HOST_SUFFIXES)


def open_in_browser(url):
    """在用户的 Chrome（优先）或系统默认浏览器里打开一个页面。

    永不抛：这只是「尽量」帮用户打开一下——失败了就换退回方式，全部失败
    也不能把解析流程搞挂（上层等插件送地址的逻辑不依赖这一步是否成功，
    用户自己手动切到 Chrome 打开那个直播间一样能用）。
    """
    import os
    import platform
    import webbrowser

    # 硬保险：测试环境下绝不真的打开浏览器。这不是假设——2026-09-05 一条重连
    # 测试走到了这里，在用户的 Chrome 里连开了三个 `@x/live` 标签页。
    # pytest 会给每个用例设置 PYTEST_CURRENT_TEST；TLT_NO_BROWSER 留给其它自动化。
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TLT_NO_BROWSER"):
        print("[信息] 测试/自动化环境，跳过打开浏览器: {}".format(url))
        return False
    system = platform.system()
    try:
        if system == "Darwin":
            r = subprocess.run(["open", "-a", "Google Chrome", url],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                return True
            r = subprocess.run(["open", url], capture_output=True, timeout=10)
            return r.returncode == 0
        if system == "Windows":
            r = subprocess.run(["cmd", "/c", "start", "chrome", url],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                return True
            return bool(webbrowser.open(url))
        return bool(webbrowser.open(url))
    except Exception as exc:
        print("[警告] 打开浏览器失败: {}".format(exc))
        try:
            return bool(webbrowser.open(url))
        except Exception:
            return False


class BrowserBridge:
    """记录插件回传的 page_state / stream_url，并让 request_stream 等到其中一个。

    所有等待时长都是类属性，测试里调小即可，不用真的等 60 秒。
    """

    ASK_FIRST_SEC = 3.0     # 先问一声已经打开的页面，看插件手头是不是正好有地址
    WAIT_SEC = 60.0         # 打开浏览器之后最多再等这么久
    STALE_SEC = 120.0       # 超过这么久的旧记录不算数，避免复用上一轮的过期地址

    def __init__(self, broadcast, open_url=None, status=None):
        self.broadcast = broadcast
        self.open_url = open_url or open_in_browser
        self.status = status
        # streamer -> {"page_state": {"logged_in":…, "ts":…},
        #              "stream_url": {"url":…, "ts":…, "logged_in":…, "reason":…}}
        self._state = {}
        # streamer -> asyncio.Event，惰性创建——Python 3.9 里 Event() 构造时就要
        # 绑事件循环，而 BrowserBridge 可能在循环外构造（如测试、Pipeline.__init__）
        self._events = {}

    def _event_for(self, streamer):
        ev = self._events.get(streamer)
        if ev is None:
            ev = asyncio.Event()
            self._events[streamer] = ev
        return ev

    def _wake(self, streamer):
        ev = self._events.get(streamer)
        if ev is not None:
            ev.set()

    def note(self, data):
        """server 转来的 stream_url / page_state 消息。永不抛——调用方是
        WebSocket 的消息循环，这里出错不该断开插件的连接，就地吞掉打印警告。"""
        try:
            self._note(data)
        except Exception as exc:
            print("[警告] BrowserBridge.note 处理消息失败: {}".format(exc))

    def _note(self, data):
        if not isinstance(data, dict):
            return
        streamer = data.get("streamer")
        if not isinstance(streamer, str) or not streamer:
            return
        msg_type = data.get("type")
        now = time.time()
        if msg_type == "page_state":
            logged_in = data.get("logged_in")
            if logged_in not in (True, False, None):
                return
            entry = self._state.setdefault(streamer, {})
            entry["page_state"] = {"logged_in": logged_in, "ts": now}
            self._wake(streamer)
        elif msg_type == "stream_url":
            url = data.get("url") or None
            if url is not None and not _valid_stream_url(url):
                print("[警告] 丢弃不合规的流地址（streamer={}）".format(streamer))
                return
            # url 为空/reason 存在也照样记录：表示「插件在场，但没抓到地址」，
            # 这本身是有用信息（区别于插件根本没连上）。
            entry = self._state.setdefault(streamer, {})
            entry["stream_url"] = {
                "url": url,
                "ts": now,
                "logged_in": data.get("logged_in"),
                "reason": data.get("reason"),
            }
            self._wake(streamer)

    def _clear_stale(self, streamer):
        """清掉该 streamer 上一轮留下的旧记录——不然一次失败的解析会让下一次
        请求误把 120 秒前那个早就失效的地址当成「插件刚刚送来的」。"""
        entry = self._state.get(streamer)
        if not entry:
            return
        now = time.time()
        su = entry.get("stream_url")
        if su and now - su.get("ts", 0) > self.STALE_SEC:
            entry.pop("stream_url", None)
        ps = entry.get("page_state")
        if ps and now - ps.get("ts", 0) > self.STALE_SEC:
            entry.pop("page_state", None)

    async def _safe_broadcast(self, msg):
        try:
            await self.broadcast(msg)
        except Exception as exc:
            print("[警告] BrowserBridge 广播失败: {}".format(exc))

    async def _safe_status(self, state, detail):
        if self.status is None:
            return
        try:
            await self.status(state, detail)
        except Exception as exc:
            print("[警告] BrowserBridge 状态更新失败: {}".format(exc))

    def _check_state(self, streamer):
        """看一眼当前已记录的状态：有合规地址就返回，明确未登录就抛
        ResolveError，都没有就返回 None。不等待、不清事件——调用方
        （_wait_phase）决定看完之后是返回还是接着等。

        必须先看状态、再决定要不要等事件：note() 完全可能在 request_stream
        开始等待之前就已经把地址记下来了（比如插件在收到 need_stream_url
        之前就靠 SIGI 快路径把地址发了过来），这时对应的 asyncio.Event 还没
        创建、或早被后面的 event.clear() 清掉，光等事件会把这个已经在手边的
        地址晾到超时。
        """
        entry = self._state.get(streamer) or {}
        page_state = entry.get("page_state")
        if page_state and page_state.get("logged_in") is False:
            raise ResolveError(
                "TikTok 只把这个直播间的流地址给登录用户。请在 Chrome 里"
                "登录 TikTok，然后重新点「开始翻译」。", kind="login")
        stream_url = entry.get("stream_url")
        if stream_url and stream_url.get("url"):
            return stream_url["url"]
        return None

    async def _wait_phase(self, streamer, event, timeout):
        """在 timeout 秒内等一条「有用」的消息：合规的流地址，或明确的未登录。

        中途被无关消息唤醒（比如 logged_in=True 的 page_state、或
        url 为空的 stream_url）不能提前返回 None——那会让上层过早去开浏览器
        或过早报超时，应当在剩余预算里继续等。
        """
        deadline = time.monotonic() + timeout
        while True:
            url = self._check_state(streamer)
            if url:
                return url
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            event.clear()

    async def request_stream(self, streamer, live_url):
        """返回可用的流地址；拿不到抛 ResolveError（kind login / unknown）。"""
        self._clear_stale(streamer)
        event = self._event_for(streamer)
        event.clear()
        await self._safe_broadcast({"type": "need_stream_url", "streamer": streamer})
        url = await self._wait_phase(streamer, event, self.ASK_FIRST_SEC)
        if url:
            return url

        self.open_url(live_url)
        await self._safe_status(
            "connecting",
            "TikTok 没有把这个直播间的流地址给程序，已在 Chrome 打开直播间，"
            "等待插件送来地址…（需要已安装本项目的 Chrome 插件并登录 TikTok）")
        url = await self._wait_phase(streamer, event, self.WAIT_SEC)
        if url:
            return url

        raise ResolveError(
            "没有收到 Chrome 插件送来的流地址。请确认：① Chrome 已安装本项目的"
            "插件（chrome://extensions → 开发者模式 → 加载已解压的扩展程序 → "
            "项目里的 extension 文件夹，改过要点「重新加载」）；② Chrome 里已"
            "登录 TikTok；③ 直播间页面在 Chrome 里能正常播放。然后重新点"
            "「开始翻译」。", kind="unknown")
