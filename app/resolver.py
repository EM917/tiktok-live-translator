"""把 TikTok 直播间页面地址解析成可供 ffmpeg 拉流的媒体地址（FLV/HLS）。"""
import asyncio
import re
import sys


class ResolveError(RuntimeError):
    pass


_DIRECT_RE = re.compile(r"\.(flv|m3u8)(\?|$)", re.IGNORECASE)

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


async def _check_media_url(url):
    """媒体地址安全校验：只允许 http(s)，且不得指向内网/环回地址。"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ResolveError("不支持的地址协议：{}（只接受 http/https 直播流）"
                           .format(parsed.scheme or "(空)"))
    if await _host_is_private(parsed.hostname):
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


async def resolve_stream_url(url, cookies=None):
    """返回直播流媒体地址。已经是 .flv/.m3u8 的直接放行，否则用 yt-dlp 解析。
    所有返回给 ffmpeg 的地址都先过 _check_media_url（协议 + 内网拦截）。"""
    if _DIRECT_RE.search(url):
        return await _check_media_url(url)

    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        raise ResolveError("缺少 yt-dlp，请先执行：pip install -r requirements.txt")

    # 优先纯音频 FLV（TikTok 的 flv-ao：省带宽、延迟低，且 HLS 地址对 ffmpeg 直连常返回 5XX），
    # 逐级回退到普通 FLV / best
    fmt = "flv-ao/bestaudio/flv-hd/flv-hd1/best"
    cmd = [sys.executable, "-m", "yt_dlp", "-g", "-f", fmt, "--no-warnings"]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd += ["--", url]      # `--` 之后一律当作地址，防止 "-xxx" 形式的地址被当成选项
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        # 限时 + 取消时务必杀掉子进程：否则用户点「停止」或换房间后，
        # 卡住的 yt-dlp 会永远挂在后台（asyncio 取消只解绑协程，不动进程）
        out, err = await asyncio.wait_for(proc.communicate(), timeout=45)
    except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
        if isinstance(exc, asyncio.TimeoutError):
            raise ResolveError("解析直播流超时（网络不通或该地区无法访问 TikTok）")
        raise
    if proc.returncode != 0:
        # yt-dlp 的 TikTok 提取器时不时失灵（接口说没播但页面在播）——先试页面兜底
        fallback = await _resolve_from_page(url)
        if fallback:
            print("[信息] yt-dlp 解析失败，已从直播页面直接找到流地址")
            return await _check_media_url(fallback)
        tail = err.decode(errors="replace").strip().splitlines()[-3:]
        raise ResolveError(
            "yt-dlp 未能解析直播流（主播可能没在播，或该地区需要登录 cookies）：\n" + "\n".join(tail)
        )
    lines = [line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()]
    if not lines:
        raise ResolveError("yt-dlp 没有返回流地址（直播可能尚未开始）")
    return await _check_media_url(lines[0])
