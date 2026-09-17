"""把 TikTok 直播间页面地址解析成可供 ffmpeg 拉流的媒体地址（FLV/HLS）。"""
import asyncio
import contextvars
import os
import json
import re
import sys
import time
import traceback
from pathlib import Path

from .nethttp import read_all
from .redact import strip_query


class ResolveError(RuntimeError):
    """解析失败。kind 供上层决策（如断流自动重连时区分「主播真下播了」）：

      offline   —— 主播没在播 / 直播已结束（重连应就此收手）
      not_found —— 直播间不存在 / 地址错误
      login     —— 需要登录 / 私密限制
      network   —— 网络不通 / DNS 失败 / 超时
      internal  —— 本工具自身的问题（组件缺失等）
      browser_only —— 程序拿不到、需借用户浏览器
      unknown   —— 其余

    status：kind=offline 时房间接口/页面原样给出的房间状态值（4=已结束，其它值含义
    TikTok 没说明）。重连循环只在 4 时收手，其余值先等一等（见 pipeline._confirm_offline）。

    login：这次解析里借各浏览器 TikTok 登录时看到的结果，{浏览器: 代码}（代码见
    app/browser_login.py）。上层拿它给中控写「能照做的一步」，不用去解析错误文本。
    只有代码，没有 cookie 的值。
    """

    def __init__(self, message, kind="unknown", status=None, login=None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.login = dict(login or {})


_DIRECT_RE = re.compile(r"\.(flv|m3u8)(\?|$)", re.IGNORECASE)


def is_direct_url(url):
    """是否用户直接给的流地址（.flv/.m3u8）。这类地址无法重新解析出「主播是否
    还在播」，断流重连策略要据此收敛（见 pipeline 的重连循环）。"""
    return bool(_DIRECT_RE.search(url))

# 直播页内嵌 JSON 里的流地址（含 \/ 与 & 转义形态）
_PAGE_URL_RE = re.compile(r"https:\\?/\\?/[^\"'\s]{10,400}?\.(?:flv|m3u8)[^\"'\s]{0,300}")

_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


async def _host_is_private(host):
    """判断主机是否落在环回/内网/链路本地网段。用于挡住把 ffmpeg 指向内网的地址
    ——尤其是「页面兜底解析」会从 HTML 里提取地址，那段 HTML 未必可信。"""
    import ipaddress
    import socket

    if not host:
        return True
    try:                                  # 字面 IP
        ip = ipaddress.ip_address(host.strip("[]"))
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        pass
    try:                                  # 域名：解析后逐个校验
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        # 「解析不了」和「命中内网」是两回事：以前一律 return True，CDN 域名在
        # 用户网络下解析失败时中控看到的是「安全限制」，而且整条解析链在第一层
        # 就被终止、审计里 layers=[]。如实说是 DNS 问题，kind=network 可重试。
        raise ResolveError("流媒体域名解析失败：{}（检查网络 / DNS）".format(host),
                           kind="network") from exc
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


async def _check_media_url(url, trusted=False):
    """媒体地址安全校验，分两档信任级别：

    trusted=True —— 用户自己输入的地址（命令行参数，或 UI 里手动粘贴的）。
        用户本来就能在自己电脑上运行任何东西，放行本机/内网地址不构成提权，
        而且本地文件、自建的局域网推流服务器都是合理用法。
    trusted=False —— 派生地址：yt-dlp 的输出、以及从直播页 HTML 里正则提取的
        地址。后者内容不可信，必须挡住指向环回/内网的地址，否则一个恶意页面就能
        让本程序去探测用户的内网服务（SSRF）。
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    allowed = ("http", "https", "file") if trusted else ("http", "https")
    if parsed.scheme not in allowed:
        raise ResolveError("不支持的地址协议：{}（只接受 http/https 直播流）"
                           .format(parsed.scheme or "(空)"))
    if not trusted and await _host_is_private(parsed.hostname):
        raise ResolveError("拒绝访问内网/本机地址的流媒体地址（安全限制）")
    return url


# 正在读的浏览器登录 {浏览器: concurrent.futures.Future}，进程级。读 Chrome 的 cookie 要向
# 钥匙串要密钥，对话框没人点时那个线程一直不返回、也取消不掉。登录优先之后每次解析
# （含每次断流重连）都会读一次：不记着在途的那一个，每次解析就多挂一个线程、多弹一次
# 对话框。同一个浏览器同一时刻只读一次；读完的 Future 立刻丢掉——cookie 的值不在这里留着。
_LOGIN_READS = {}
_LOGIN_POOL = []


def _login_pool():
    if not _LOGIN_POOL:
        from concurrent.futures import ThreadPoolExecutor
        _LOGIN_POOL.append(ThreadPoolExecutor(max_workers=len(BROWSER_CANDIDATES),
                                              thread_name_prefix="tlt-login"))
    return _LOGIN_POOL[0]


def _login_read_pending(browser):
    """这个浏览器上一次的读取还没返回。为什么没返回这里不知道，只知道它没返回。"""
    fut = _LOGIN_READS.get(browser)
    return fut is not None and not fut.done()


def _start_login_read(browser, reader=None):
    fut = _LOGIN_READS.get(browser)
    if fut is None or fut.done():
        fut = _login_pool().submit(reader or _read_login, browser)
        _LOGIN_READS[browser] = fut

        def forget(done, _browser=browser):
            if _LOGIN_READS.get(_browser) is done:
                _LOGIN_READS.pop(_browser, None)

        fut.add_done_callback(forget)
    return fut


async def _await_login_read(browser, reader=None):
    """等这个浏览器的登录读取返回（在途的那一次，或新起一次）。不设时限，调用方自己套预算；
    被取消时线程收不回来，但协程不再陪它等。"""
    return await asyncio.wrap_future(_start_login_read(browser, reader))


async def _login_with_budget(browser):
    """在线程池里读浏览器 cookie，并给它一个预算。yt-dlp 解密 Chrome cookie 要跑
    macOS 的 security 命令，会弹钥匙串授权对话框——用户没看到就一直阻塞，
    整条解析停在「正在解析直播流地址…」，点停止也取消不掉线程。超时按
    「读不到」处理；线程本身收不回来，但协程不再陪它等。

    返回 LoginRead(Cookie 头或 None, 代码)。读到了什么（见 browser_login 的代码）记进
    当前这一层的 why 和这次解析的登录观察里——只记代码，cookie 的值不出这个返回值。"""
    from .browser_login import KEYCHAIN_WAIT, LoginRead

    try:
        got = await asyncio.wait_for(_await_login_read(browser),
                                     timeout=BROWSER_ATTEMPT_TIMEOUT)
    except asyncio.TimeoutError:
        print("[信息] 读取 {} 的 TikTok 登录状态 {} 秒没有返回，先按未登录继续；屏幕上"
              "如果有「钥匙串」对话框，点「始终允许」".format(browser, BROWSER_ATTEMPT_TIMEOUT))
        got = LoginRead(None, KEYCHAIN_WAIT)
    _observe_login(browser, got.code)
    return got


async def _cookie_header_with_budget(browser):
    """同 _login_with_budget，只要 Cookie 头（读不到为 None）。"""
    return (await _login_with_budget(browser)).header


def _read_login(browser):
    """借用浏览器里现成的 TikTok cookie：返回 LoginRead(Cookie 头或 None, 代码)。

    cookies 只在本机与 TikTok 之间使用：不写日志、不落盘、不发往任何第三方。
    读不到时 header 为 None，调用方按匿名处理；为什么读不到看 code。

    以前这里叫 _cookie_header，任何异常都吞成 None，调用方一律记「no_cookie」——
    2026-09-17 实测 macOS 27 上两个浏览器都是系统拒绝读取，日志却说「没有 cookie」。"""
    from .browser_login import read_login
    return read_login(browser)


def _read_login_prechecked(browser):
    """登录优先那一步用的读取。Safari 直接读（明文文件，实测 0.0 秒）。其它浏览器——只有中控
    点了名才会在这一步被读到（见 _login_first_browsers）——先用不解密的 probe 看一眼：
    系统拒绝读取、没有数据、没有 tiktok.com 的 cookie、没有登录 cookie 的名字
    ——这四种就不做会解密的读取了（Chrome 上那一次约 5 秒，还要向钥匙串要密钥），直接给出
    probe 的代码。这一步每次解析、每次断流重连都会走，没有登录可借时不该为它多等 5 秒。
    probe 看不到还没落进主库的 cookie（见 browser_login._sqlite_names）：刚在 Chrome 里登录的
    那一小段时间这里会说没登录，后面各层照旧做完整读取。"""
    from .browser_login import (BLOCKED, NO_DATA, NO_TIKTOK, NOT_LOGGED_IN, LoginRead,
                                probe)

    if browser != "safari" and sys.platform == "darwin":
        code = probe(browser)
        if code in (BLOCKED, NO_DATA, NO_TIKTOK, NOT_LOGGED_IN):
            return LoginRead(None, code)
    return _read_login(browser)


# 一次解析里借各浏览器登录的观察 {浏览器: 代码}，最后挂在 ResolveError.login 上。
_LOGIN_OBS = contextvars.ContextVar("tlt_resolver_login_obs", default=None)
# 这次解析借的是哪个浏览器的登录：{"source": 浏览器}。第一个读到可用登录的浏览器就此定下，
# 后面的层只借它、不再读别的浏览器（macOS 上 Safari 排最前，见 _installed_browsers）。
# 放 dict 而不是直接放值：asyncio.wait_for 会把协程包进新任务，任务里对 ContextVar 的
# set 传不回来，改同一个 dict 才传得回来。
_LOGIN_PLAN = contextvars.ContextVar("tlt_resolver_login_plan", default=None)


def _observe_login(browser, code):
    """记一笔「借这个浏览器的登录时看到了什么」：进当前层的 why，也进这次解析的汇总。
    同一个浏览器一次解析里会被读好几回（接口层、直播页层）；读到过登录（ok）就不再被
    后面的结果盖掉——「借到了登录、TikTok 仍然不给」是要原样告诉中控的事实。"""
    _note("{}: {}".format(browser, code))
    seen = _LOGIN_OBS.get()
    if seen is not None and seen.get(browser) != "ok":
        seen[browser] = code
    plan = _LOGIN_PLAN.get()
    if code == "ok" and plan is not None and _login_first_enabled():
        plan.setdefault("source", browser)


# ---- 登录优先（2026-09-17 定的）------------------------------------------
# 实测（macOS 27，房间 @daisycabral_ 在播，中控在 Safari 和 Chrome 里都登录着 TikTok）：
#   * 不登录：直播页是一道登录门，页面里没有流地址；官方接口带不带登录 cookie 都回 4003110；
#     yt-dlp 带 cookie 仍报未开播。
#   * 只做一件事——带 Safari 的登录抓直播页：5 次里 5 次拿到 .flv 地址，0.4–0.8 秒。
#   * 同一个请求放在一次**匿名**抓页之后 0.5 秒、3 秒：3 次里 3 次拿不到；隔 8 秒、20 秒：拿到。
#     每次调用都是全新的 aiohttp 会话，程序在两次调用之间不留任何状态——是 TikTok 那一侧的事，
#     这里只记观察，不写原因。
#   * 以前的「直播页兜底」层固定按 匿名 → Chrome → Safari 背靠背地抓，借登录的那一次永远落在
#     匿名那一次之后 0.5 秒；它前面的层（官方接口、WebKit 隐藏页 25 秒、yt-dlp）也全是匿名的。
#     一次失败约 37 秒，三次之后这一场就结束了——整场没有监听。
#   * 读 Safari 的 cookie 0.0 秒（明文文件，解释器要有「完全磁盘访问权限」）；读 Chrome 的约
#     5 秒，还要向钥匙串要密钥。
# 产品负责人的决定（已被告知代价：此后每次解析都带着中控的 TikTok 身份）：只要读得到登录，
# 就一律先用登录；不动 Chrome，直接用 Safari。
LOGIN_LAYER = "登录直播页"
# 借登录抓直播页之前，离本进程上一次**匿名**请求至少隔这么久。取 8 秒：上面实测 0.5 秒和
# 3 秒都拿不到，8 秒和 20 秒都拿到，8 是量到过的最小可行间隔。
LOGIN_AFTER_ANON_GAP_SEC = 8.0
# 登录优先这一步（读 cookie + 抓直播页）的总预算。实测 Safari 这条路总共不到 1 秒；Chrome
# 读 cookie 约 5 秒 + 抓页 0.8 秒 ≈ 5.8 秒，8 秒留了约 2 秒余量。读取到点没有返回、
# 或抓页太慢就放弃这一步往下走：不登录也能解析的直播间最多被它拖慢这么久。刻意等的间隔
# （waited_ms）和拿到地址之后的拉流探活不算在里面——探活和其它层一样，拿到地址才会发生。
LOGIN_STEP_BUDGET_SEC = 8.0
# 等间隔最多等这么久（三个间隔）。同一个进程里弹幕子进程的重连也会发匿名请求
# （见 note_anonymous_request），等的时候时刻可能被刷新；不设上限就可能一直等下去。
LOGIN_GAP_MAX_WAIT_SEC = 3 * LOGIN_AFTER_ANON_GAP_SEC

# 进程级：本进程上一次向 TikTok 发**匿名**解析请求的 monotonic 时刻。
_ANON = {"last": None}


def _login_first_enabled():
    """登录优先、Safari 排最前、间隔规则只在 macOS 上启用：上面的实测全部来自 macOS，
    其它平台没有量过（读不读得到浏览器登录都没有验证），保持原来的顺序。"""
    return sys.platform == "darwin"


def login_first_applies(url, cookies_browser="auto"):
    """这个链接的解析会不会走登录优先那一步。只看平台、--cookies-browser 和链接本身，
    不读浏览器。pipeline 用它决定弹幕子进程要不要等第一次解析完再起（子进程一启动就匿名
    抓同一个直播页）。"""
    return (_login_first_enabled() and cookies_browser != "none"
            and _is_tiktok_host(_upgrade_tiktok_scheme(url)))


def _monotonic():
    return time.monotonic()


async def _gap_sleep(seconds):
    await asyncio.sleep(seconds)


def _tiktok_hostname(host):
    host = (host or "").lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def _is_tiktok_host(url):
    """https 的 tiktok.com 或它的子域。浏览器里借来的 cookie 只发给这样的地址——输入框里
    粘进来的「直播间链接」未必真是 TikTok 的，重定向也可能指到别处。"""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url or "")
        host = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme == "https" and _tiktok_hostname(host)


def _upgrade_tiktok_scheme(url):
    """http:// 开头的 TikTok 直播间链接改成 https://，别的地址原样返回。

    界面接受 http:// 开头的链接（pipeline 的输入校验）。借来的 cookie 不走明文 http，这一条
    不变；但只因为链接少了一个 s 就整场不用登录，只对已登录观众给流地址的直播间就解析不出来，
    界面上也没有一句话说明。所以换协议，不丢登录。只换主机确实是 tiktok.com（或子域）、
    没有用户名密码、端口是默认的那种；其余一概不动（照旧拿不到 cookie）。"""
    from urllib.parse import urlparse, urlunparse

    try:
        parsed = urlparse(url or "")
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return url
    if (parsed.scheme != "http" or not _tiktok_hostname(host)
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 80)):
        return url
    return urlunparse(parsed._replace(scheme="https", netloc=host.lower()))


def _cookie_refusal(url):
    """_is_tiktok_host 说不行时，trace 里记哪一种：主机是 tiktok.com 但不是 https（not_https），
    还是主机根本不是 tiktok.com（not_tiktok_host）。"""
    from urllib.parse import urlparse

    try:
        host = urlparse(url or "").hostname
    except ValueError:
        host = None
    return "not_https" if _tiktok_hostname(host) else "not_tiktok_host"


def _carries_login(cookie):
    """这个 Cookie 头里有没有 TikTok 的登录 cookie（只看名字）。没有 Cookie 头、或只有 ttwid
    之类不代表登录的 cookie，都算匿名请求。"""
    from .browser_login import LOGIN_COOKIE_NAMES

    names = {part.split("=", 1)[0].strip() for part in (cookie or "").split(";")}
    return any(name in names for name in LOGIN_COOKIE_NAMES)


def _stamp_anon():
    """记一笔「刚向 TikTok 发过匿名请求」：不带 cookie 的官方接口、匿名抓直播页、
    WebKit 隐藏页、不带 cookie 的 yt-dlp。请求发出前、返回后各记一次。"""
    _ANON["last"] = _monotonic()


def note_anonymous_request():
    """给 resolver 之外、同一个进程里也会向 TikTok 发匿名请求的代码用：弹幕子进程
    （TikTokLive）不带 sessionid 启动时，第一件事就是匿名抓 https://www.tiktok.com/@主播/live
    ——正是实测里排在登录请求前面的那种请求。它不记进来，间隔规则就看不见它。"""
    _stamp_anon()


async def _wait_out_anon_gap():
    """借登录抓直播页之前调用：离上一次匿名请求不足 LOGIN_AFTER_ANON_GAP_SEC 就把差额等完。
    返回等了多少秒（没等是 0.0），并记进当前层的 waited_ms。这段等待是刻意的，
    不算进任何一层的预算。

    等完要再看一眼：等的这几秒里，别的任务（弹幕子进程重连、房间状态复查）可能又发了一次
    匿名请求。总共最多等 LOGIN_GAP_MAX_WAIT_SEC，到点仍没等到空档就照常去抓、在 why 里记
    gap_not_clear——晚一点监听可以，永远不去抓不行。"""
    if not _login_first_enabled():
        return 0.0
    waited = 0.0
    while True:
        last = _ANON["last"]
        if last is None:
            break
        remain = LOGIN_AFTER_ANON_GAP_SEC - (_monotonic() - last)
        if remain <= 0:
            break
        if waited >= LOGIN_GAP_MAX_WAIT_SEC:
            _note("gap_not_clear")
            break
        remain = min(remain, LOGIN_GAP_MAX_WAIT_SEC - waited)
        note = _LAYER_NOTE.get()
        if note is not None:
            note["waited_ms"] = note.get("waited_ms", 0) + int(round(remain * 1000))
        await _gap_sleep(remain)
        waited += remain
    return waited


async def _resolve_from_page(url, browser=None):
    """兜底方案：yt-dlp 的 TikTok 提取器失效时，直接抓直播页 HTML 挖流地址。
    优先纯音频流（only_audio=1），其次 FLV。永远返回 (流地址或 None, 是否确认已下播)
    二元组——曾经有两个分支裸返回 None，调用方按二元组拆包，于是前面几条
    路都失败的房间会直接以「内部错误」收场（2026-09-05 实录）。

    browser 非空时借用该浏览器的 TikTok 登录态再抓一次——有些房间的页面
    对未登录访问就是不带流地址。带着登录抓之前先过间隔规则（_wait_out_anon_gap）。"""
    if not browser:
        return await _fetch_live_page(url)
    cookie = await _cookie_header_with_budget(browser)    # 读不到的原因它已经记下
    if not cookie:
        return None, False
    if _carries_login(cookie):
        await _wait_out_anon_gap()
    # 有 tiktok.com 的 cookie 但没有登录 cookie 时和以前一样带上去——它算匿名请求，不用等
    return await _fetch_live_page(url, cookie=cookie)


async def _fetch_live_page(url, cookie=None):
    """抓一次直播页，返回 (流地址或 None, 是否确认已下播)。cookie 只进发给 TikTok 的
    Cookie 头。不带登录的请求记进匿名时刻（见 _stamp_anon）。"""
    import aiohttp

    from .nethttp import read_all

    from urllib.parse import urljoin

    headers = dict(_BROWSER_HEADERS)
    if cookie and not _is_tiktok_host(url):
        _note("cookie_not_sent: " + _cookie_refusal(url))   # 借来的 cookie 只发给 https 的 tiktok.com
        cookie = None
    if cookie:
        headers["Cookie"] = cookie
    logged_in = _carries_login(cookie)
    if not logged_in:
        _stamp_anon()
    # 带着 cookie 时自己跟重定向：aiohttp 自动跟的话，请求头里的 Cookie 会原样带到
    # 重定向指向的任何主机上。每一跳都先确认还在 tiktok.com 里，出去了就不跟。
    extra = {"allow_redirects": False} if cookie else {}
    raw, charset = None, None
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            current = url
            for _hop in range(6):
                async with session.get(current, headers=headers, **extra) as resp:
                    if cookie and resp.status in (301, 302, 303, 307, 308):
                        current = urljoin(current, resp.headers.get("Location") or "")
                        if not _is_tiktok_host(current):
                            _note("redirect_off_tiktok")
                            return None, False
                        continue
                    if resp.status != 200:
                        _note("http={}".format(resp.status))
                    # 必须读到 EOF。这里曾经写成单次 read(8MB)，实测 213KB 的直播页
                    # 只拿到 85KB——流地址在被截掉的那 60% 里，于是每个直播间都
                    # 「解析失败」，而日志上看不出任何异常。
                    raw = await read_all(resp, 8 * 1024 * 1024)
                    charset = resp.charset
                break
            else:
                _note("too_many_redirects")
                return None, False
        if raw is None:
            _note("empty_body")
            return None, False
        html = raw.decode(charset or "utf-8", errors="replace")
    except Exception as exc:
        _note(type(exc).__name__)
        return None, False
    finally:
        if not logged_in:
            _stamp_anon()
    return _parse_live_page(html)


def _parse_live_page(html):
    """从直播页 HTML 取 (流地址, 确认已下播)。

    先解析 SIGI_STATE 里的结构化数据，**用 liveRoom.status 把关**：实测一个
    已经下播的房间，页面里照样残留着上一场的完整流地址（含 only_audio），
    裸正则会拿到它，然后要等探活超时 8 秒才发现是 404。有了状态位就能当场
    判定，还能把「确认下播」和「没解析出来」分开。

    SIGI_STATE 缺失或解析不了时才退回裸正则——那时我们对状态一无所知，
    绝不能替它断言「主播没在播」。"""
    m = _SIGI_RE.search(html)
    if m:
        try:
            sigi = json.loads(m.group(1))
            room = ((sigi.get("LiveRoom") or {}).get("liveRoomUserInfo")
                    or {}).get("liveRoom") or {}
            status = room.get("status")
            if status is not None and status != LIVE_STATUS:
                _note(status=status, field="liveRoom.status")
                return None, True                     # 页面明确说已结束
            picked = _pick_stream({"stream_url": _sigi_stream_url(room)})
            if picked:
                return picked, False
        except (ValueError, AttributeError, TypeError):
            pass
    return _extract_stream_urls(html), False


def _sigi_stream_url(room):
    """把 SIGI_STATE 里的 streamData 摆成和 webcast 接口一样的形状，
    这样 _pick_stream 一份逻辑两处用（含纯音频优先）。"""
    sd = room.get("streamData") or {}
    return {"live_core_sdk_data": {"pull_data": (sd.get("pull_data") or {})}}


def _extract_stream_urls(html):
    """纯函数：从直播页 HTML 里提取最优媒体地址（含 \\/ 与 \\u0026 转义还原）。
    优先纯音频流（only_audio=1），其次 FLV，最后任意候选；没有则 None。"""
    candidates = []
    for m in _PAGE_URL_RE.finditer(html):
        u = (m.group(0).replace("\\u0026", "&").replace("\\/", "/").rstrip("\\"))
        if u not in candidates:
            candidates.append(u)
    for pick in (lambda u: "only_audio=1" in u,
                 lambda u: ".flv" in u,
                 lambda u: True):
        for u in candidates:
            if pick(u):
                return u
    return None


# ---- TikTok 官方直播接口 ----------------------------------------------
# 这两个接口匿名可用，且**独立于 yt-dlp 的提取器**。实测过一个确实在播的
# 房间：yt-dlp 匿名、yt-dlp 带 cookies、带 curl_cffi 伪装，三种都报「未开播」，
# 而这条链路直接拿到了 status=2 和多档流地址。yt-dlp 的 TikTok 提取器一旦
# 被挡（HTTP 400），它就会把失败一律翻译成「主播未开播」，那句话是错的。
_ROOM_API = ("https://www.tiktok.com/api-live/user/room/"
             "?aid=1988&sourceType=54&uniqueId={user}")
_WEBCAST_API = ("https://webcast.tiktok.com/webcast/room/info/"
                "?aid=1988&room_id={room}")
_USER_RE = re.compile(r"tiktok\.com/@([\w.\-]+)", re.IGNORECASE)
_SIGI_RE = re.compile(r'id="SIGI_STATE"[^>]*>(.*?)</script>', re.S)

# TikTok 的房间状态：2=在播，4=已结束。只有拿到明确的非 2 才敢说「主播没在播」，
# 拿不到就只能说「没解析出来」——两者对重连策略的含义完全不同。
LIVE_STATUS = 2
ENDED_STATUS = 4
# webcast 房间接口拒绝给出流地址时的**通用**代码：status_code 4003110，data 里
# 只剩一个空的 prompts 字段。TikTok 不说原因，返回体里也没有任何可读的原因。
#
# 这个常量曾叫 AGE_GATE_CODE。2026-09-05/06 一天之内它被先后解释成「年龄限制」
# 「IP 限流」「被我们的探测打坏」，三个都错——每次都是拿单个观察往外推，没有
# 对照组。定案的证据：同一分钟对照房间正常返回 242 个字段而目标房间只回这个码；
# 目标房间在程序日志里 12 场全 0 段，第一场早于我们的第一次请求。
# **规则：代码、注释、界面文案里都只写「接口不给流地址」这个观察，不写原因。**
# 排查方法在 tools/diagnose_room.py 和 CLAUDE.md 第八条。
STREAM_WITHHELD_CODE = 4003110


def _username(url):
    m = _USER_RE.search(url or "")
    return m.group(1) if m else None


async def _get_json(session, url, limit=4 * 1024 * 1024, headers=None):
    """拿不到返回 None；为什么拿不到（HTTP 状态码 / 异常类名 / 空响应）记进当前层的观察。
    不带登录 cookie 的请求记进匿名时刻（见 _stamp_anon）。

    请求头里带着 Cookie 时（4003110 之后借浏览器登录重问的那一次）：只发给 https 的
    tiktok.com，而且**不跟重定向**——aiohttp 自动跟的话，显式写的 Cookie 头会原样带到
    重定向指向的任何主机上（3.11 实测跨域也带）。这两个接口平时不重定向；真回了 3xx
    就和别的非 200 一样当这次没拿到，记 http=3xx。"""
    sent_cookie = (headers or {}).get("Cookie")
    if sent_cookie and not _is_tiktok_host(url):
        _note("cookie_not_sent: " + _cookie_refusal(url))
        headers = {k: v for k, v in headers.items() if k != "Cookie"}
        sent_cookie = None
    anonymous = not _carries_login(sent_cookie)
    if anonymous:
        _stamp_anon()
    extra = {"allow_redirects": False} if sent_cookie else {}
    try:
        async with session.get(url, headers=headers or _BROWSER_HEADERS, **extra) as resp:
            if resp.status != 200:
                _note("http={}".format(resp.status))
                return None
            raw = await read_all(resp, limit)
        if not raw:
            _note("empty_body")
            return None
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:
        _note(type(exc).__name__)
        return None
    finally:
        if anonymous:
            _stamp_anon()


async def _room_status(session, user):
    """用户名 → (room_id, status)。拿不到返回 (None, None)。"""
    j = await _get_json(session, _ROOM_API.format(user=user), limit=1024 * 1024)
    if not isinstance(j, dict) or j.get("statusCode") not in (0, None):
        if isinstance(j, dict):
            _note("statusCode={}".format(j.get("statusCode")))
        return None, None
    u = ((j.get("data") or {}).get("user") or {})
    room = u.get("roomId")
    status = u.get("status")
    if not room:
        _note("no_roomId")
    if status is not None:
        _note(status=status, field="user.status")
    return (str(room) if room else None), status


def _pick_stream(data):
    """从 webcast 房间信息里挑一个地址，优先纯音频。

    我们只要声音。纯音频档（only_audio=1）省掉整条视频码流——对一个要连着
    盯几小时的工具，这不是微优化。"""
    su = (data or {}).get("stream_url") or {}
    sdk = ((su.get("live_core_sdk_data") or {}).get("pull_data") or {})
    raw = sdk.get("stream_data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    opts = ((raw or {}).get("data") or {})
    for quality in ("ao",):                      # 纯音频优先
        flv = ((opts.get(quality) or {}).get("main") or {}).get("flv")
        if flv:
            return flv
    for val in opts.values():                    # 其次任意档的 flv
        flv = ((val or {}).get("main") or {}).get("flv")
        if flv:
            return flv
    flvs = su.get("flv_pull_url") or {}
    if isinstance(flvs, dict):
        for v in flvs.values():
            if v:
                return v
    return su.get("rtmp_pull_url") or su.get("hls_pull_url") or None


def _stream_withheld(info):
    """房间接口是不是回了那个「不给流地址」的通用拒绝码（见 STREAM_WITHHELD_CODE）。"""
    if not isinstance(info, dict):
        return False
    if info.get("status_code") == STREAM_WITHHELD_CODE:
        return True
    data = info.get("data")
    return isinstance(data, dict) and "prompts" in data


# ---- 第 2 层：系统 WebKit 引擎加载直播页（见 app/webkit_fetch.py 的说明） ----

WEBKIT_TIMEOUT_SEC = 30.0


def _webkit_available():
    """只在 macOS 且装了 pywebview 时有这一层；Windows 上 pywebview 走的是
    Chromium 内核（WebView2），是否被 TikTok 放行没有验证过，先不开。"""
    import importlib.util

    if sys.platform != "darwin":
        return False
    try:
        return importlib.util.find_spec("webview") is not None
    except Exception:
        return False


async def _run_webkit_fetch(url, timeout):
    """起子进程 `python -m app.webkit_fetch`，返回它 stdout 的最后一行 JSON（dict），
    拿不到返回 None。测试环境下（pytest）绝不真的起浏览器引擎——测试要验证
    解析逻辑就 monkeypatch 这个函数。"""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TLT_NO_WEBKIT"):
        return None
    root = str(Path(__file__).resolve().parent.parent)
    _stamp_anon()                       # 隐藏页不登录：它是一次匿名请求
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.webkit_fetch", url,
        "--timeout", str(max(5.0, timeout - 5.0)),
        cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        _stamp_anon()
    except asyncio.TimeoutError:
        _stamp_anon()
        _note("timeout")
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return None
    except asyncio.CancelledError:
        _stamp_anon()
        proc.kill()
        raise
    for line in reversed(out.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            return obj if isinstance(obj, dict) else None
    _note("no_output")
    return None


async def _resolve_via_webkit(url, timeout=WEBKIT_TIMEOUT_SEC):
    """返回 (stream_url, live_known_offline)，与 _resolve_via_api 同一形状。

    这一层的存在理由：TikTok 对某些房间只把流地址交给「真正的浏览器」（房间
    接口回 4003110，直播页 HTML 不带 streamData），而系统的 WebKit 引擎不登录
    就被放行，实测 2 秒拿到。子进程出任何问题都只当「这层没拿到」。"""
    if not _webkit_available():
        return None, False
    result = await _run_webkit_fetch(url, timeout)
    if not result:
        return None, False
    if result.get("url"):
        return str(result["url"]), False
    if result.get("offline"):
        _note(status=result.get("status"), field="liveRoom.status")
        return None, True
    if result.get("error"):
        _note(strip_query(result["error"], 120))
    return None, False


async def _resolve_via_api(url, cookies_browser="auto"):
    """官方接口链路：用户名 → room_id → 流地址。

    返回 (stream_url, live_known_offline)。第二个值只有在接口明确告诉我们
    房间已结束时才是 True——用来把「确认没播」和「我们没解析出来」分开。

    status_code 4003110：接口不肯把流地址给这次请求，data 里只有一个 prompts
    字段，原因 TikTok 不说明（见 STREAM_WITHHELD_CODE 上面的说明——别给它编
    原因）。处理：先借浏览器里的 TikTok 登录态重试一次（有些房间登录态确实
    管用），仍被拒就如实说「TikTok 这次不给程序」（kind="browser_only"），
    交给上层隔一会儿自动重试（见 pipeline._resolve_media）。实测过的事实只有：
    同一房间有时第三次重试就给（2026-09-06 晚），有时三次全不给（次日）。"""
    import aiohttp

    user = _username(url)
    if not user:
        _note("no_username")
        return None, False
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)) as session:
            room, status = await _room_status(session, user)
            if room is None:
                return None, False
            if status is not None and status != LIVE_STATUS:
                return None, True          # 接口明确说没在播
            info = await _get_json(session, _WEBCAST_API.format(room=room))
            if _stream_withheld(info):
                for browser in _iter_borrow(cookies_browser):
                    cookie = await _cookie_header_with_budget(browser)
                    if not cookie:
                        continue
                    headers = dict(_BROWSER_HEADERS)
                    headers["Cookie"] = cookie
                    again = await _get_json(session, _WEBCAST_API.format(room=room),
                                            headers=headers)
                    if _stream_withheld(again):
                        continue
                    picked = _pick_stream((again or {}).get("data") or {})
                    if picked:
                        _remember_browser(browser)
                        print("[信息] 接口曾拒绝匿名请求（代码 4003110）："
                              "已借用 {} 的 TikTok 登录状态取到流地址".format(browser))
                        return picked, False
                # 走到这里：借了浏览器登录态也没能绕过，统一在函数末尾抛错
            else:
                data = (info or {}).get("data") or {}
                if data.get("status") is not None and data["status"] != LIVE_STATUS:
                    _note(status=data["status"], field="data.status")
                    return None, True
                picked = _pick_stream(data)
                if not picked:
                    _note("no_stream_url")
                return picked, False
    except Exception as exc:
        _note(type(exc).__name__)
        return None, False
    # 只有一种情况会走到这里：接口一直不肯给流地址（4003110），登录态也没帮上忙。
    # 只报观察，不编原因——交给上层隔一会儿自动重试（见 pipeline._resolve_media）。
    raise ResolveError(
        "TikTok 没有把这个直播间的流地址给程序（代码 4003110）",
        kind="browser_only")


async def _media_url_works(url, timeout=8):
    """真的去拉几个字节，确认这个地址能用。

    签名地址解析得出来不等于拉得动：签名过期、地区限制、CDN 节点故障都会让
    ffmpeg 在开播那一刻才失败——那时用户已经以为连上了。宁可在这里多花一秒。
    """
    if not url:
        return False
    import aiohttp

    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers=_BROWSER_HEADERS) as resp:
                if resp.status != 200:
                    _note("probe http={}".format(resp.status))
                    return False
                # 只取一小口：能出数据就说明流是活的
                chunk = await resp.content.read(2048)
                if not chunk:
                    _note("probe empty")
                return bool(chunk)
    except Exception as exc:
        _note("probe " + type(exc).__name__)
        return False


# ---- 重连循环用的两个小探测（都不走五层解析）----
PROBE_HOST = "www.tiktok.com"


async def tiktok_reachable(host=PROBE_HOST, port=443, timeout=3.0):
    """本机连不连得上 TikTok：DNS 解析 + TCP 建连，**不发任何 HTTP 请求**，不碰房间。

    返回 (ok, why)，why 只写观察到的异常类名或 timeout。连得上不代表解析一定成功
    （比如要网页登录的公共 Wi-Fi），那种情况照旧走解析。测试里（pytest）不真的连，
    直接当连得上——要测探测本身就调 _tcp_probe。"""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True, "not_probed_in_tests"
    return await _tcp_probe(host, port, timeout)


async def _tcp_probe(host, port, timeout):
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port),
                                                 timeout=timeout)
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as exc:
        return False, type(exc).__name__
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    except Exception:
        pass
    return True, ""


async def probe_room_status(url):
    """只问一次房间接口（一个请求），返回 (房间状态值或 None, why)。

    重连时接口说「不在播」但不是「已结束」，程序隔一会儿用它复查，不再走五层解析。
    测试里（pytest）不发请求，返回 (None, "not_probed_in_tests")。"""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None, "not_probed_in_tests"
    import aiohttp

    user = _username(url)
    if not user:
        return None, "no_username"
    note = {}
    token = _LAYER_NOTE.set(note)
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)) as session:
            _room, status = await _room_status(session, user)
    except Exception as exc:
        return None, type(exc).__name__
    finally:
        _LAYER_NOTE.reset(token)
    return status, _layer_note(note).get("why", "")


def _classify_ytdlp_error(err_text):
    """把 yt-dlp 的英文报错归类成 (kind, 用户能行动的中文话术)；技术细节只留最后一行。"""
    lowered = err_text.lower()
    lines = [ln.strip() for ln in err_text.strip().splitlines() if ln.strip()]
    detail = "\n技术细节：{}".format(lines[-1][:200]) if lines else ""
    if "not currently live" in lowered or "room is offline" in lowered:
        # 注意：TikTok 对**未登录**的请求也经常返回「not currently live」——
        # 浏览器里明明在播、这里却说没开播，多半是这个原因。实测同一时刻
        # 6 个在播房间里 5 个被判为未开播，只有 1 个能匿名解析。
        # 所以不能把这条当成板上钉钉的「主播下播了」。
        return ("offline",
                "没能获取到这个直播间的音频流。\n"
                "· 如果主播确实没在播：等开播后再试即可；\n"
                "· 如果你在浏览器里看得到这个直播：TikTok 对未登录访问经常"
                "返回「未开播」，需要导出登录 cookies 后用 --cookies 指定"
                "（见 README 常见问题）。")
    if ("unable to find room" in lowered or "http error 404" in lowered
            or "unsupported url" in lowered or "does not exist" in lowered):
        return ("not_found",
                "没有找到这个直播间——请确认地址形如 "
                "https://www.tiktok.com/@用户名/live，"
                "或在直播间里点「分享 → 复制链接」粘贴过来。")
    if ("log in" in lowered or "login" in lowered or "cookies" in lowered
            or "authentication" in lowered or "private" in lowered):
        return ("login",
                "这个直播间需要登录后才能观看（可能是私密或有观看限制），"
                "换一个直播间试试吧。（进阶：若你在浏览器里能看这个直播，"
                "可用 --cookies 导入登录信息，见 README 常见问题）" + detail)
    if ("timed out" in lowered or "connection" in lowered
            or "network" in lowered or "temporary failure" in lowered
            or "getaddrinfo" in lowered or "nodename" in lowered
            or "name resolution" in lowered or "unreachable" in lowered
            or "reset by peer" in lowered or "urlopen error" in lowered):
        return ("network",
                "网络连接不畅，暂时访问不到 TikTok——请检查网络后重试。" + detail)
    return ("unknown",
            "无法连接这个直播间：主播可能没在播，也可能是网络问题或地址有误。"
            "请检查后重试。" + detail)


# 从浏览器直接借用登录态的候选顺序。TikTok 现在对**未登录**请求把大多数
# 直播间报成「未开播」（实测同一时刻 6 个在播房间只有 1 个能匿名解析），
# 而看直播的人浏览器里本来就登录着——读现成的 cookie 比让用户手工导出
# cookies.txt 友好得多，也不需要额外注册账号。
BROWSER_CANDIDATES = ("chrome", "safari", "firefox", "edge", "brave", "chromium")
BROWSER_ATTEMPT_TIMEOUT = 20      # 单个浏览器的尝试预算（秒）


def _installed_browsers():
    """只试本机真的装了的浏览器——为不存在的浏览器各花几秒是纯浪费。"""
    import shutil
    from pathlib import Path as _P

    apps = {
        "chrome": "/Applications/Google Chrome.app",
        "safari": "/Applications/Safari.app",
        "firefox": "/Applications/Firefox.app",
        "edge": "/Applications/Microsoft Edge.app",
        "brave": "/Applications/Brave Browser.app",
        "chromium": "/Applications/Chromium.app",
    }
    if sys.platform == "darwin":
        found = [b for b, path in apps.items() if _P(path).exists()]
        # Safari 排最前（2026-09-17 起；以前排最后）。实测读它的 cookie 0.0 秒——明文文件，
        # 不碰钥匙串；没有「完全磁盘访问权限」时是立刻 PermissionError，不会卡住。读 Chrome
        # 约 5 秒、还要向钥匙串要密钥。Safari 里读到登录就不再读别的浏览器（见 _iter_borrow）。
        return tuple(b for b in found if b == "safari") + \
               tuple(b for b in found if b != "safari")
    if sys.platform == "win32":
        return ("chrome", "edge", "firefox", "brave")
    return tuple(b for b in ("chrome", "firefox", "chromium", "brave")
                 if shutil.which(b) or shutil.which(b + "-browser"))


def _named_browser(preference):
    """中控**点名**的那一个浏览器：命令行的 --cookies-browser，其次设置里的
    cookies_browser_only；都没有（auto / none）返回 None。"""
    if preference and preference not in ("auto", "none"):
        return preference
    from .settings import load_settings
    only = load_settings().get("cookies_browser_only")
    if isinstance(only, str) and only.strip().lower() in BROWSER_CANDIDATES:
        return only.strip().lower()
    return None


def _browser_order(preference):
    """要试的浏览器顺序。

    优先级：命令行显式给的 --cookies-browser > 设置里的 cookies_browser_only（只读这一个
    浏览器，比如 "safari"：别的浏览器的数据一概不碰）> 自动。自动时上次成功的排前面——
    避免每次都从头逐个试；macOS 上 Safari 永远在它前面（读它不花时间，读到登录就不再
    读别的，见 _installed_browsers）。"""
    named = _named_browser(preference)
    if named:
        return (named,)
    from .settings import load_settings
    remembered = load_settings().get("cookies_browser")
    installed = _installed_browsers() or BROWSER_CANDIDATES
    if remembered in installed:
        order = (remembered,) + tuple(b for b in installed if b != remembered)
    else:
        order = tuple(installed)
    if _login_first_enabled() and "safari" in order:
        order = ("safari",) + tuple(b for b in order if b != "safari")
    return order


def _iter_borrow(cookies_browser):
    """各层借登录时要试的浏览器。这次解析里已经有浏览器读到了可用的登录（_LOGIN_PLAN）
    就只借它：macOS 上 Safari 排最前，Safari 里有登录时 Chrome 的数据一次都不读
    （不弹钥匙串、不花那 5 秒）。每次取下一个之前重新看一眼，循环中途读到也算。"""
    if cookies_browser == "none":
        return
    for browser in tuple(_browser_order(cookies_browser)):
        source = (_LOGIN_PLAN.get() or {}).get("source")
        if source and browser != source:
            continue
        yield browser


def _login_first_browsers(cookies_browser):
    """登录优先那一步读哪些浏览器：中控点名了就读点名的那一个，没点名就**只读 Safari**。

    这一步每次解析、每次断流重连都走，而且排在匿名的官方接口前面——不登录也能解析的
    直播间，那一层约 1 秒就拿到地址。实测读 Safari 的 cookie 0.0 秒；读 Chrome 的约 5 秒，
    还要向钥匙串要密钥（对话框点的是「允许」而不是「始终允许」的话，每次重连都会再弹）。
    把 Chrome 放进这一步，等于让每次重连的监听空档多出 5–8 秒——只有 Chrome 里读得到登录
    的机器（没给「完全磁盘访问权限」的现有安装都是）以前解析这类直播间从不碰 Chrome。
    产品负责人定的也是「不动 Chrome，直接用 Safari」。Chrome 仍然留给后面原有的几层
    （接口被拒后的重问、yt-dlp 借 cookie、直播页兜底）：它们只在匿名各层都没拿到时才走，
    和以前一样。"""
    if cookies_browser == "none":
        return ()
    named = _named_browser(cookies_browser)
    if named:
        return (named,)
    return tuple(b for b in _browser_order(cookies_browser) if b == "safari")


def login_source(cookies_browser="auto"):
    """这一场登录优先那一步会借哪个浏览器的 TikTok 登录：浏览器名，没有就 None。给
    session_start 用。看的浏览器和那一步读的一样（_login_first_browsers）：没点名时只看 Safari。

    只看 cookie 库读不读得到、里面有没有登录 cookie 的**名字**（browser_login.probe）：
    不解密、不碰钥匙串，也不读任何 cookie 的值。"""
    if not _login_first_enabled() or cookies_browser == "none":
        return None
    from .browser_login import OK, probe

    for browser in _login_first_browsers(cookies_browser):
        if probe(browser) == OK:
            return browser
    return None


async def login_source_with_budget(cookies_browser="auto", budget=2.0):
    """login_source 放进读登录用的那个线程池里跑，到点没返回就当 None。不用事件循环的默认
    线程池：这一步每场开头都走，不为它给每个事件循环多起一组线程。"""
    future = _login_pool().submit(login_source, cookies_browser)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=budget)
    except asyncio.TimeoutError:
        return None


def _remember_browser(browser):
    from .settings import load_settings, save_setting
    if load_settings().get("cookies_browser") != browser:
        save_setting("cookies_browser", browser)


async def _run_ytdlp(url, cookies=None, browser=None, timeout=45):
    """跑一次 yt-dlp 取流地址，返回 (returncode, stdout, stderr)。"""
    fmt = "flv-ao/bestaudio/flv-hd/flv-hd1/best"
    cmd = [sys.executable, "-m", "yt_dlp", "-g", "-f", fmt, "--no-warnings"]
    if cookies:
        cmd += ["--cookies", cookies]
    elif browser:
        cmd += ["--cookies-from-browser", browser]
    cmd += ["--", url]      # `--` 之后一律当作地址，防止 "-xxx" 形式的地址被当成选项
    anonymous = not cookies and not browser
    if anonymous:
        _stamp_anon()
    # PYTHONIOENCODING=utf-8：我们按 UTF-8 解这两个管道（下面的 decode），而 Windows 上
    # 子进程默认按 ANSI 代码页输出——报错里的主播昵称、路径会解成一串 U+FFFD，
    # 而 U+FFFD 本身又是 GBK 编不出来的字符，等于把 yt-dlp 的原话变成第二个编码坑。
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    try:
        # 限时 + 取消时务必杀掉子进程：否则用户点「停止」或换房间后，
        # 卡住的 yt-dlp 会永远挂在后台（asyncio 取消只解绑协程，不动进程）
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
        if anonymous:
            _stamp_anon()
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
        if isinstance(exc, asyncio.TimeoutError):
            raise ResolveError("解析直播流超时（网络不通或该地区无法访问 TikTok）",
                               kind="network") from None
        raise
    if anonymous:
        _stamp_anon()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _first_url(stdout):
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    return lines[0] if lines else None


# 每一层解析的原始观察：HTTP 状态码、接口 statusCode、异常类名、房间状态值。以前每层
# 失败都只记一个 outcome=none，官方接口 23 毫秒就失败的那几次，分不清是 DNS 不通、
# HTTP 4xx 还是返回结构变了。用 ContextVar 收集，各层函数的签名和返回值都不用变。
_LAYER_NOTE = contextvars.ContextVar("tlt_resolver_layer_note", default=None)


def _note(why=None, **fields):
    """给当前正在跑的那一层记一笔观察。只记代码、状态值和异常类名，不写原因推测
    （CLAUDE.md 第八条）；没有层在记时（单独调用内部函数）什么也不做。"""
    note = _LAYER_NOTE.get()
    if note is None:
        return
    if why:
        whys = note.setdefault("why", [])
        if str(why) not in whys:
            whys.append(str(why))
    note.update(fields)


def _fresh_note():
    note = {}
    _LAYER_NOTE.set(note)
    return note


def _layer_note(note):
    """一层的观察摊成 _mark 的额外字段：why（≤120 字符，URL 去掉 query）、status、field、
    waited_ms。"""
    out = {}
    if note.get("why"):
        out["why"] = strip_query("; ".join(note["why"]), 120)
    if note.get("status") is not None:
        out["status"] = note["status"]
        if note.get("field"):
            out["field"] = note["field"]
    if note.get("waited_ms"):
        out["waited_ms"] = note["waited_ms"]      # 见 _wait_out_anon_gap：刻意等的间隔
    return out


def _stderr_why(text):
    """yt-dlp 失败时 stderr 的最后一行（URL 去掉 query，120 字符），摊成 _mark 的额外字段。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return {"why": strip_query(lines[-1], 120)} if lines else {}


def _borrow_why(note, stderr_text):
    """yt-dlp借cookie 这一层的 why：各浏览器的登录代码在前，yt-dlp stderr 的最后一行在后。"""
    parts = list(note.get("why") or [])
    parts += [v for v in _stderr_why(stderr_text).values() if v not in parts]
    return {"why": strip_query("; ".join(parts), 120)} if parts else {}


def _mark(trace, layer, outcome, t0, **extra):
    """解析日志的一条：哪一层、什么结果、花了多久。trace 为 None 时什么也不做。

    outcome 取值：url（拿到且能拉）/ dead_url（拿到但拉不动）/ offline（确认
    下播）/ browser_only（4003110）/ none（这层没拿到）/ skipped（这层在本机
    不可用）/ crash（层内部异常，已跳过）/ 或 ResolveError 的 kind。
    2026-09-06 一场直播前两次解析失败、第三次才成，事后只能靠「只有一个
    会话文件」+「秒数对得上」倒推——因为解析过程一个字都没记。

    extra 里常见的 why / status / field 是这一层的原始观察（见 _note）。"""
    if trace is None:
        return
    rec = {"layer": layer, "outcome": outcome,
           "ms": int(round((time.monotonic() - t0) * 1000))}
    rec.update(extra)
    trace.append(rec)


async def _vet(url, layer, rejected):
    """派生地址的安全校验（协议、内网、DNS）。失败不该终止整条解析：记下来、
    返回 None 让这一层按「没拿到」处理，后面的层继续。"""
    try:
        return await _check_media_url(url)
    except ResolveError as exc:
        rejected.append((layer, exc))
        return None


def _note_layer_crash(crashed, layer_name, exc):
    """某一层解析内部出了非 ResolveError 的异常：打印完整 traceback（终端能
    看到堆栈，而不是像 2026-09-05 那次一样只剩一句「内部错误」），记入
    crashed 列表后放过——绝不能让一层的 bug 把后面几层一起带崩。"""
    traceback.print_exc()
    print("[警告] 解析路径 {} 内部出错，已跳过: {}".format(layer_name, exc))
    crashed.append(layer_name)


def _page_attempts(cookies_browser):
    """直播页兜底层依次怎么抓：浏览器名 = 借它的登录，None = 匿名。惰性地给——借登录的
    浏览器要看这次解析里已经读到了谁的登录（_iter_borrow）。"""
    if _login_first_enabled():
        for browser in _iter_borrow(cookies_browser):
            yield browser
        yield None
        return
    yield None
    for browser in _iter_borrow(cookies_browser):
        yield browser


async def _find_login(browsers, codes):
    """按顺序读，读到第一个可用的 TikTok 登录就停。返回 (浏览器, Cookie 头)，没有是
    (None, None)。codes 是 {浏览器: 代码}，读一个填一个——预算到点被取消时，调用方
    还看得到读到哪儿了（值是 None 的那个就是正在读的）。

    上一次读取还没返回的浏览器直接跳过、记 keychain_wait：陪它再等一个预算没有意义，
    那次读取返回之后，下一次解析会重新读。"""
    from .browser_login import KEYCHAIN_WAIT, OK

    for browser in browsers:
        if _login_read_pending(browser):
            codes[browser] = KEYCHAIN_WAIT
            _observe_login(browser, KEYCHAIN_WAIT)
            continue
        codes[browser] = None
        got = await _await_login_read(browser, _read_login_prechecked)
        codes[browser] = got.code
        _observe_login(browser, got.code)
        if got.code == OK and got.header:
            return browser, got.header
    return None, None


async def _login_page_step(url, cookies_browser):
    """登录优先这一步：读到可用的 TikTok 登录，就带着它抓直播页。

    返回 (流地址或 None, 是否确认已下播, info)。info：browser（借到登录的浏览器或 None）、
    login（{浏览器: 代码}）、budget（True = 预算内没做完，放弃了这一步）。Cookie 头只在
    这个函数里经手：从读取函数到发给 TikTok 的请求头，不进 info、不进 why。"""
    from .browser_login import KEYCHAIN_WAIT

    codes = {}
    info = {"browser": None, "login": codes, "budget": False}
    began = time.monotonic()
    try:
        browser, header = await asyncio.wait_for(
            _find_login(_login_first_browsers(cookies_browser), codes),
            timeout=LOGIN_STEP_BUDGET_SEC)
    except asyncio.TimeoutError:
        for name, code in codes.items():
            if code is None:                      # 预算到点时正在读的那个
                codes[name] = KEYCHAIN_WAIT
                _observe_login(name, KEYCHAIN_WAIT)
        _note("budget")
        info["budget"] = True
        return None, False, info
    if not header:
        return None, False, info
    info["browser"] = browser
    spent = time.monotonic() - began
    await _wait_out_anon_gap()                    # 刻意的等待，不算进预算
    try:
        page_url, offline = await asyncio.wait_for(
            _fetch_live_page(url, cookie=header),
            timeout=max(0.1, LOGIN_STEP_BUDGET_SEC - spent))
    except asyncio.TimeoutError:
        _note("budget")
        info["budget"] = True
        return None, False, info
    return page_url, offline, info


async def resolve_stream_url(url, cookies=None, cookies_browser="auto", trace=None):
    """返回直播流媒体地址，详见 _resolve_stream_url。

    这一层包装只负责各层观察（_note）的上下文：返回或抛出之前把它复位，上一次解析的
    笔记不会漏到同一个任务里后面的调用上。"""
    token = _LAYER_NOTE.set(None)
    login_token = _LOGIN_OBS.set({})
    plan_token = _LOGIN_PLAN.set({})
    try:
        return await _resolve_stream_url(url, cookies=cookies,
                                         cookies_browser=cookies_browser, trace=trace)
    finally:
        _LOGIN_PLAN.reset(plan_token)
        _LOGIN_OBS.reset(login_token)
        _LAYER_NOTE.reset(token)


async def _resolve_stream_url(url, cookies=None, cookies_browser="auto", trace=None):
    """返回直播流媒体地址。已经是 .flv/.m3u8 的直接放行，否则用 yt-dlp 解析。

    trace：传一个 list 进来，每走过一层就追加一条 {layer, outcome, ms}
    （见 _mark）。调用方拿它写审计日志；不传就不记。

    解析顺序：（macOS 上读得到浏览器里的 TikTok 登录时；没点名浏览器就只读 Safari，见
    _login_first_browsers）带登录抓直播页 → 官方接口 →
    系统 WebKit 引擎加载直播页（macOS）→ yt-dlp 匿名 →（失败时）yt-dlp 借用浏览器
    登录态 → 直播页兜底（有登录时先带登录抓、匿名的那一次放最后）。断流重连走的是同一个
    函数，顺序一样。cookies 只在本机与 TikTok 之间使用，不写入日志、不发往任何
    第三方。所有返回给 ffmpeg 的地址都先过 _check_media_url（协议 + 内网拦截）。

    分层保险：每一层各自 try/except Exception（ResolveError 和 CancelledError
    除外，前者是层内部主动给出的明确判断、后者是取消信号，两者都要原样往上
    抛）。一层内部炸了（比如接口返回的结构变了、正则挂了）不能让整条解析链路
    跟着崩成一句「内部错误」——继续试下一层，最后如果所有层都没拿到结果，
    再把哪几层内部出过错附在错误消息里，方便定位（详细堆栈在终端）。"""
    if _DIRECT_RE.search(url):
        # 用户直接给的流地址：按可信处理（详见 _check_media_url 的说明）
        _mark(trace, "直连地址", "url", time.monotonic())
        return await _check_media_url(url, trusted=True)

    # http:// 开头的 TikTok 链接：换成 https 再解析（见 _upgrade_tiktok_scheme）。下面每一层
    # 用的都是换过的地址；审计里的 room_url 仍是中控输入的原文。
    url = _upgrade_tiktok_scheme(url)

    crashed = []
    rejected = []      # (层, ResolveError)：派生地址没过安全校验，不算这层拿到

    # 第 0 层：登录优先（见 LOGIN_LAYER 上面的实测）。读得到浏览器里的 TikTok 登录时，
    # 这次解析向 TikTok 发的**第一个**请求就是带登录的直播页——它前面不能有任何匿名请求。
    # 链接不是 https 的 tiktok.com 就没有这一步：借来的 cookie 不发给别的主机。
    # 拿到地址、过了校验、拉得动就直接返回，后面的匿名各层一概不走；页面明确说本场已结束
    # 就照旧报下播；其余情况（没读到登录、页面里没有地址、预算到点）往下走原来的链路。
    if login_first_applies(url, cookies_browser):
        t0, login_outcome = time.monotonic(), "none"
        login_note = _fresh_note()
        info = {"browser": None, "login": {}}
        try:
            login_url, login_offline, info = await _login_page_step(url, cookies_browser)
            if info["budget"]:
                login_outcome = "budget"
            elif info["browser"] is None:
                login_outcome = "no_login"
            if login_offline:
                _mark(trace, LOGIN_LAYER, "offline", t0, browser=info["browser"],
                      login=dict(info["login"]), **_layer_note(login_note))
                raise ResolveError("主播当前没有在直播（直播页确认本场已结束）",
                                   kind="offline", status=login_note.get("status"))
            if login_url:
                checked = await _vet(login_url, LOGIN_LAYER, rejected)
                if checked is None:
                    login_outcome = "rejected"
                elif await _media_url_works(checked):
                    _remember_browser(info["browser"])
                    print("[信息] 已借用 {} 的 TikTok 登录从直播页取到流地址".format(
                        info["browser"]))
                    _mark(trace, LOGIN_LAYER, "url", t0, browser=info["browser"],
                          login=dict(info["login"]), **_layer_note(login_note))
                    return checked
                else:
                    print("[信息] 带登录从直播页拿到的地址拉不动，继续试其它方式")
                    login_outcome = "dead_url"
        except (ResolveError, asyncio.CancelledError):
            raise
        except Exception as exc:
            _note_layer_crash(crashed, LOGIN_LAYER, exc)
            _note(type(exc).__name__)
            login_outcome = "crash"
        _mark(trace, LOGIN_LAYER, login_outcome, t0, browser=info["browser"],
              login=dict(info["login"]), **_layer_note(login_note))

    # 第 1 层：TikTok 官方接口。放在最前有两个理由——它给的是**纯音频档**
    # （only_audio=1，省掉整条视频码流），而且它独立于 yt-dlp 的提取器：
    # 实测一个确实在播的房间，yt-dlp 的三种方式全报「未开播」，这条链路照样通。
    api_url, known_offline = None, False
    browser_only = None      # 接口说「这房间的流地址不给程序」——先记着，后面的层可能拿得到
    t0, api_outcome = time.monotonic(), "none"
    api_note = _fresh_note()
    try:
        api_url, known_offline = await _resolve_via_api(url, cookies_browser=cookies_browser)
    except ResolveError as exc:
        if exc.kind != "browser_only":
            _mark(trace, "官方接口", exc.kind, t0, **_layer_note(api_note))
            raise
        browser_only = exc
        api_outcome = "browser_only"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _note_layer_crash(crashed, "官方接口", exc)
        _note(type(exc).__name__)
        api_outcome = "crash"
    if api_url:
        checked = await _vet(api_url, "官方接口", rejected)
        if checked is None:
            api_outcome = "rejected"
        elif await _media_url_works(checked):
            print("[信息] 已通过 TikTok 直播接口取到纯音频流")
            _mark(trace, "官方接口", "url", t0, **_layer_note(api_note))
            return checked
        else:
            print("[信息] 直播接口给的地址拉不动，继续试其它方式")
            api_outcome = "dead_url"
    if known_offline:
        # 接口明确说房间不在播。状态值原样带上去：重连时只有 4（已结束）才收手
        _mark(trace, "官方接口", "offline", t0, **_layer_note(api_note))
        raise ResolveError("主播当前没有在直播（TikTok 接口确认直播已结束）",
                           kind="offline", status=api_note.get("status"))
    _mark(trace, "官方接口", api_outcome, t0, **_layer_note(api_note))

    # 第 2 层：系统 WebKit 引擎加载直播页。TikTok 只把某些房间的流地址交给真正的
    # 浏览器（接口回 4003110），而 mac 的 WebKit 不登录就放行，实测 2 秒拿到。
    t0 = time.monotonic()
    wk_outcome = "none" if _webkit_available() else "skipped"
    wk_note = _fresh_note()
    try:
        wk_url, wk_offline = await _resolve_via_webkit(url)
        if wk_url:
            checked = await _vet(wk_url, "WebKit", rejected)
            if checked is None:
                wk_outcome = "rejected"
            elif await _media_url_works(checked):
                print("[信息] 已通过系统 WebKit 引擎从直播页取到流地址")
                _mark(trace, "WebKit", "url", t0, **_layer_note(wk_note))
                return checked
            else:
                print("[信息] WebKit 拿到的地址拉不动，继续试其它方式")
                wk_outcome = "dead_url"
        elif wk_offline:
            _mark(trace, "WebKit", "offline", t0, **_layer_note(wk_note))
            raise ResolveError("主播当前没有在直播（直播页确认本场已结束）",
                               kind="offline", status=wk_note.get("status"))
    except (ResolveError, asyncio.CancelledError):
        raise
    except Exception as exc:
        _note_layer_crash(crashed, "WebKit", exc)
        _note(type(exc).__name__)
        wk_outcome = "crash"
    _mark(trace, "WebKit", wk_outcome, t0, **_layer_note(wk_note))

    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        raise ResolveError("组件 yt-dlp 缺失：请关闭程序后重新打开，会自动补装。"
                           "（进阶：pip install -r requirements.txt）",
                           kind="internal") from None

    # 第 2 层：yt-dlp 匿名解析。
    code, out, err = 1, "", ""
    t0 = time.monotonic()
    try:
        code, out, err = await _run_ytdlp(url, cookies=cookies)
    except ResolveError as exc:
        _mark(trace, "yt-dlp匿名", exc.kind, t0)
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _note_layer_crash(crashed, "yt-dlp匿名", exc)
        _mark(trace, "yt-dlp匿名", "crash", t0, why=type(exc).__name__)
    else:
        got = code == 0 and _first_url(out)
        _mark(trace, "yt-dlp匿名", "url" if got else "none",
              t0, code=code, **({} if got else _stderr_why(err)))
        if code == 0 and not _first_url(out):
            # 退出码 0 却没有地址：不能据此断言下播（只有接口确认才敢说），
            # 当失败处理，让后面借 cookie / 直播页兜底的层继续
            code = 1

    # 第 3 层：匿名失败且用户没自带 cookies.txt——依次试各浏览器的现成登录态。
    # 记住成功的那个，下次直接用，不再逐个试。
    if code != 0 and not cookies and cookies_browser != "none":
        from .browser_login import classify_stderr

        t0, used_browser, cookie_outcome = time.monotonic(), None, "none"
        last_err = ""
        borrow_note = _fresh_note()
        try:
            for browser in _iter_borrow(cookies_browser):
                try:
                    # 单个浏览器给较短预算：读不到 cookie（未授权/未安装/被占用）
                    # 应当快速失败换下一个，而不是把整体解析拖垮
                    b_code, b_out, b_err = await _run_ytdlp(
                        url, browser=browser, timeout=BROWSER_ATTEMPT_TIMEOUT)
                except ResolveError:
                    last_err = "{}: timeout".format(browser)
                    continue          # 这个浏览器超时了：换下一个，别中断整个兜底
                last_err = b_err
                # 子进程读 cookie 读不到时，stderr 里是系统或 yt-dlp 的原话：归成代码记下。
                # stderr 说的是别的事（比如提取器报未开播）就不记——这一层不知道登录状态
                login_code = classify_stderr(browser, b_err) if b_code != 0 else None
                if login_code:
                    _observe_login(browser, login_code)
                if b_code == 0 and _first_url(b_out):
                    _remember_browser(browser)
                    print("[信息] 匿名解析失败，已借用 {} 的 TikTok 登录状态".format(browser))
                    code, out, err = b_code, b_out, b_err
                    used_browser, cookie_outcome = browser, "url"
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _note_layer_crash(crashed, "yt-dlp借cookie", exc)
            cookie_outcome, last_err = "crash", type(exc).__name__
        _mark(trace, "yt-dlp借cookie", cookie_outcome, t0, browser=used_browser,
              **_borrow_why(borrow_note, "" if cookie_outcome == "url" else last_err))
    if code != 0:
        # 第 4 层：yt-dlp 的 TikTok 提取器时不时失灵（接口说没播但页面在播）——
        # 试页面兜底。有些房间的页面对未登录访问就是不带流地址，所以也借浏览器登录抓。
        # 顺序（macOS）：**先**带登录抓（_resolve_from_page 里先过间隔规则），匿名的那一次
        # 放最后——以前是 匿名 → 登录 背靠背，实测借登录的那一次落在匿名请求之后 0.5 秒，
        # 3 次里 3 次拿不到。其它平台没有量过，保持原来的 匿名 → 登录。
        t0, page_outcome = time.monotonic(), "none"
        page_note = _fresh_note()
        try:
            for browser in _page_attempts(cookies_browser):
                fallback, page_offline = await _resolve_from_page(url, browser=browser)
                if page_offline:
                    _mark(trace, "直播页兜底", "offline", t0, browser=browser,
                          **_layer_note(page_note))
                    raise ResolveError("主播当前没有在直播（直播页确认本场已结束）",
                                       kind="offline", status=page_note.get("status"))
                if not fallback:
                    continue
                checked = await _vet(fallback, "直播页兜底", rejected)
                if checked is None:
                    page_outcome = "rejected"
                    continue
                if not await _media_url_works(checked):
                    page_outcome = "dead_url"
                    continue
                print("[信息] yt-dlp 解析失败，已从直播页面直接找到流地址{}".format(
                    "（借用 {} 的登录状态）".format(browser) if browser else ""))
                _mark(trace, "直播页兜底", "url", t0, browser=browser,
                      **_layer_note(page_note))
                return checked
        except (ResolveError, asyncio.CancelledError):
            raise
        except Exception as exc:
            _note_layer_crash(crashed, "直播页兜底", exc)
            _note(type(exc).__name__)
            page_outcome = "crash"
        _mark(trace, "直播页兜底", page_outcome, t0, **_layer_note(page_note))
        err_text = err
        tail = err_text.strip().splitlines()[-3:] if err_text.strip() else []
        if tail:
            print("[错误] yt-dlp: " + " | ".join(tail))    # 英文原始输出只进终端
        kind, message = _classify_ytdlp_error(err_text)
        # yt-dlp 把「提取器被挡」也说成「未开播」。我们已经把每条路都走过了，
        # 而没有任何一条**确认**过房间结束，所以不能替它下这个断言。
        if kind == "offline":
            # 只说做了什么、看到什么、能做什么。不说「多半是被挡了」——我们不知道。
            kind, message = "unknown", (
                "试过全部方式都没能拿到这个直播间的音频流，也没有任何一种"
                "确认直播已结束。可以过一会儿再点「开始翻译」；如果你在浏览器里"
                "看得到这个直播，也可以把直播间链接和浏览器里的 .flv 地址一起"
                "粘进来（中间空格隔开）。")
        if browser_only is not None:
            # 接口早就说了「不给程序」，后面各层也都没拿到：把这个明确的原因
            # 传上去，上层据此隔一会儿自动重试，而不是当成普通失败
            kind, message = "browser_only", str(browser_only)
        if rejected:
            message += "（另有拿到的地址没过安全校验：{}）".format(
                "；".join("{}：{}".format(layer, exc) for layer, exc in rejected))
            if kind == "unknown" and any(exc.kind == "network" for _, exc in rejected):
                kind = "network"          # DNS 解析失败：可重试，别当成谜之失败
        if crashed:
            message += "（另有解析路径内部出错已跳过：{}，详见终端）".format(
                "、".join(crashed))
        raise ResolveError(message, kind=kind, login=_LOGIN_OBS.get())
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        # 退出码 0 但没有任何输出：我们不知道发生了什么，不能替它说「下播了」
        message = "yt-dlp 没有返回流地址"
        if crashed:
            message += "（另有解析路径内部出错已跳过：{}，详见终端）".format(
                "、".join(crashed))
        raise ResolveError(message, kind="unknown")
    return await _check_media_url(lines[0])
