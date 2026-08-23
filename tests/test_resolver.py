"""resolver：错误归类（kind + 中文话术）、媒体地址安检（SSRF 防线）、页面挖流。"""
import asyncio

import pytest

from app.resolver import (
    ResolveError,
    _check_media_url,
    _classify_ytdlp_error,
    _extract_stream_urls,
)


# ---- 错误归类：kind 决定断流重连是否收手，话术决定用户下一步动作 ----

@pytest.mark.parametrize("err,kind,expect", [
    ("ERROR: [tiktok:live] user: The channel is not currently live",
     "offline", "没有开播"),
    ("ERROR: Unable to find room ID", "not_found", "没有找到这个直播间"),
    ("ERROR: HTTP Error 404: Not Found", "not_found", "没有找到这个直播间"),
    ("ERROR: Unsupported URL: https://x", "not_found", "没有找到这个直播间"),
    ("ERROR: This video is private. Log in to access", "login", "需要登录"),
    ("ERROR: Use --cookies to provide authentication", "login", "需要登录"),
    ("urlopen error timed out", "network", "网络连接不畅"),
    ("[Errno 8] nodename nor servname provided, or not known",
     "network", "网络连接不畅"),
    ("getaddrinfo failed", "network", "网络连接不畅"),
    ("Temporary failure in name resolution", "network", "网络连接不畅"),
    ("something totally unexpected", "unknown", "无法连接这个直播间"),
])
def test_classify_ytdlp_error(err, kind, expect):
    got_kind, message = _classify_ytdlp_error(err)
    assert got_kind == kind
    assert expect in message


def test_classify_keeps_technical_detail_for_ambiguous_kinds():
    _, message = _classify_ytdlp_error("first line\nlast line of stderr")
    assert "last line of stderr" in message      # 兜底分支保留最后一行供排障


def test_resolve_error_kind_defaults_to_unknown():
    assert ResolveError("x").kind == "unknown"
    assert ResolveError("x", kind="offline").kind == "offline"


# ---- 媒体地址安检：派生地址（不可信）必须挡内网，用户输入（可信）放行 ----

def _check(url, trusted):
    return asyncio.run(_check_media_url(url, trusted=trusted))


def test_check_media_url_rejects_bad_schemes():
    with pytest.raises(ResolveError):
        _check("rtmp://example.com/live", trusted=False)
    with pytest.raises(ResolveError):
        _check("file:///etc/passwd", trusted=False)   # file 仅可信输入允许


def test_check_media_url_trusted_allows_file_and_local():
    # 用户自己输入的地址：本地文件、本机推流都是合理用法，不构成提权
    assert _check("file:///tmp/a.flv", trusted=True) == "file:///tmp/a.flv"
    assert _check("http://127.0.0.1:9000/live.flv", trusted=True)


@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.1.1", "[::1]", "0.0.0.0",
])
def test_check_media_url_untrusted_blocks_private_literal_ips(host):
    # 字面 IP 走 ipaddress 分支，不发 DNS——CI 无网络也能测
    with pytest.raises(ResolveError):
        _check("http://{}/steal".format(host), trusted=False)


def test_check_media_url_untrusted_allows_public_https():
    url = "https://93.184.216.34/live.flv"          # 公网字面 IP，无需 DNS
    assert _check(url, trusted=False) == url


# ---- 页面挖流（yt-dlp 失效时的兜底通道）：转义还原 + 三级优先级 ----

def test_extract_prefers_pure_audio_stream():
    html = (
        '{"flv":"https:\\/\\/pull.example.com\\/live\\/a.flv?x=1\\u0026y=2",'
        '"ao":"https:\\/\\/pull.example.com\\/live\\/a.flv?only_audio=1\\u0026z=3"}'
    )
    got = _extract_stream_urls(html)
    assert got == "https://pull.example.com/live/a.flv?only_audio=1&z=3"


def test_extract_falls_back_to_flv_then_any():
    # 注意：_PAGE_URL_RE 要求主机+路径至少 10 个字符（过滤碎片误匹配）
    html_flv = ('{"a":"https:\\/\\/pull.example.com\\/live\\/s.m3u8",'
                '"b":"https:\\/\\/pull.example.com\\/live\\/s.flv"}')
    assert _extract_stream_urls(html_flv) == "https://pull.example.com/live/s.flv"
    html_m3u8 = '{"a":"https:\\/\\/pull.example.com\\/live\\/s.m3u8"}'
    assert _extract_stream_urls(html_m3u8) == "https://pull.example.com/live/s.m3u8"


def test_extract_returns_none_when_no_stream():
    assert _extract_stream_urls("<html>no streams here</html>") is None
