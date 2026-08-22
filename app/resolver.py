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
    """返回直播流媒体地址。已经是 .flv/.m3u8 的直接放行，否则用 yt-dlp 解析。"""
    if _DIRECT_RE.search(url):
        return url

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
    out, err = await proc.communicate()
    if proc.returncode != 0:
        # yt-dlp 的 TikTok 提取器时不时失灵（接口说没播但页面在播）——先试页面兜底
        fallback = await _resolve_from_page(url)
        if fallback:
            print("[信息] yt-dlp 解析失败，已从直播页面直接找到流地址")
            return fallback
        tail = err.decode(errors="replace").strip().splitlines()[-3:]
        raise ResolveError(
            "yt-dlp 未能解析直播流（主播可能没在播，或该地区需要登录 cookies）：\n" + "\n".join(tail)
        )
    lines = [line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()]
    if not lines:
        raise ResolveError("yt-dlp 没有返回流地址（直播可能尚未开始）")
    return lines[0]
