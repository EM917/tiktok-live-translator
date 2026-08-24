"""把 TikTok 直播间页面地址解析成可供 ffmpeg 拉流的媒体地址（FLV/HLS）。"""
import asyncio
import re
import sys


class ResolveError(RuntimeError):
    """解析失败。kind 供上层决策（如断流自动重连时区分「主播真下播了」）：

      offline   —— 主播没在播 / 直播已结束（重连应就此收手）
      not_found —— 直播间不存在 / 地址错误
      login     —— 需要登录 / 私密限制
      network   —— 网络不通 / DNS 失败 / 超时
      internal  —— 本工具自身的问题（组件缺失等）
      unknown   —— 其余
    """

    def __init__(self, message, kind="unknown"):
        super().__init__(message)
        self.kind = kind


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
    except Exception:
        return True                       # 解析不了就别用
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


async def _resolve_from_page(url):
    """兜底方案：yt-dlp 的 TikTok 提取器失效时，直接抓直播页 HTML 挖流地址。
    优先纯音频流（only_audio=1），其次 FLV。找不到返回 None。"""
    import aiohttp

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.get(url, headers=_BROWSER_HEADERS) as resp:
                # 限制读取上限：直播页正常几百 KB，别让异常/恶意响应撑爆内存
                raw = await resp.content.read(8 * 1024 * 1024)
        html = raw.decode(resp.charset or "utf-8", errors="replace")
    except Exception:
        return None
    return _extract_stream_urls(html)


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
        # Safari 的 cookie 需要「完全磁盘访问权限」，没授权时会卡住——排在最后
        return tuple(b for b in found if b != "safari") + \
               tuple(b for b in found if b == "safari")
    if sys.platform == "win32":
        return ("chrome", "edge", "firefox", "brave")
    return tuple(b for b in ("chrome", "firefox", "chromium", "brave")
                 if shutil.which(b) or shutil.which(b + "-browser"))


def _browser_order(preference):
    """要试的浏览器顺序。上次成功的排最前——避免每次都从头逐个试，
    每次尝试都是一个几秒的 yt-dlp 子进程。"""
    if preference and preference not in ("auto", "none"):
        return (preference,)
    from .settings import load_settings
    remembered = load_settings().get("cookies_browser")
    installed = _installed_browsers() or BROWSER_CANDIDATES
    if remembered in installed:
        return (remembered,) + tuple(b for b in installed if b != remembered)
    return installed


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
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        # 限时 + 取消时务必杀掉子进程：否则用户点「停止」或换房间后，
        # 卡住的 yt-dlp 会永远挂在后台（asyncio 取消只解绑协程，不动进程）
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
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
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _first_url(stdout):
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    return lines[0] if lines else None


async def resolve_stream_url(url, cookies=None, cookies_browser="auto"):
    """返回直播流媒体地址。已经是 .flv/.m3u8 的直接放行，否则用 yt-dlp 解析。

    解析顺序：匿名 → （失败时）借用浏览器登录态。cookies 只在本机与
    TikTok 之间使用，不写入日志、不发往任何第三方。
    所有返回给 ffmpeg 的地址都先过 _check_media_url（协议 + 内网拦截）。"""
    if _DIRECT_RE.search(url):
        # 用户直接给的流地址：按可信处理（详见 _check_media_url 的说明）
        return await _check_media_url(url, trusted=True)

    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        raise ResolveError("组件 yt-dlp 缺失：请关闭程序后重新打开，会自动补装。"
                           "（进阶：pip install -r requirements.txt）",
                           kind="internal") from None

    code, out, err = await _run_ytdlp(url, cookies=cookies)

    # 匿名失败且用户没自带 cookies.txt：依次试各浏览器的现成登录态。
    # 记住成功的那个，下次直接用，不再逐个试。
    if code != 0 and not cookies and cookies_browser != "none":
        for browser in _browser_order(cookies_browser):
            try:
                # 单个浏览器给较短预算：读不到 cookie（未授权/未安装/被占用）
                # 应当快速失败换下一个，而不是把整体解析拖垮
                b_code, b_out, b_err = await _run_ytdlp(
                    url, browser=browser, timeout=BROWSER_ATTEMPT_TIMEOUT)
            except ResolveError:
                continue          # 这个浏览器超时了：换下一个，别中断整个兜底
            if b_code == 0 and _first_url(b_out):
                _remember_browser(browser)
                print("[信息] 匿名解析失败，已借用 {} 的 TikTok 登录状态".format(browser))
                code, out, err = b_code, b_out, b_err
                break
    if code != 0:
        # yt-dlp 的 TikTok 提取器时不时失灵（接口说没播但页面在播）——先试页面兜底
        fallback = await _resolve_from_page(url)
        if fallback:
            print("[信息] yt-dlp 解析失败，已从直播页面直接找到流地址")
            return await _check_media_url(fallback)
        err_text = err
        tail = err_text.strip().splitlines()[-3:]
        print("[错误] yt-dlp: " + " | ".join(tail))    # 英文原始输出只进终端
        kind, message = _classify_ytdlp_error(err_text)
        raise ResolveError(message, kind=kind)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        raise ResolveError("yt-dlp 没有返回流地址（直播可能尚未开始，或刚刚结束）",
                           kind="offline")
    return await _check_media_url(lines[0])
