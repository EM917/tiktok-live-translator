"""server._classify_origin：WS 来源三级分级（control / view / 拒绝）。
浏览器里任何网页都能对 ws://127.0.0.1 发起连接，这里是唯一防线。"""
from types import SimpleNamespace

from app.server import CaptionServer


def classify(origin, port=8765):
    server = CaptionServer(port=port)
    request = SimpleNamespace(headers={} if origin is None else {"Origin": origin})
    return server._classify_origin(request)


def test_no_origin_is_control():
    # 非浏览器客户端（curl、本地脚本、应用窗口）没有 Origin 头
    assert classify(None) == "control"


def test_own_page_is_control():
    assert classify("http://127.0.0.1:8765") == "control"
    assert classify("http://localhost:8765") == "control"


def test_other_local_port_is_view_only():
    # 本机跑的其他网页（别人的开发服务器）不该能控制本程序
    assert classify("http://127.0.0.1:3000") == "view"
    assert classify("http://localhost") == "view"       # 默认 80 端口 ≠ 8765


def test_port_follows_server():
    assert classify("http://127.0.0.1:9000", port=9000) == "control"
    assert classify("http://127.0.0.1:8765", port=9000) == "view"


def test_tiktok_pages_are_view_only():
    # Chrome 插件以页面身份连接：只收字幕，不给控制权
    assert classify("https://www.tiktok.com") == "view"
    assert classify("https://live.tiktok.com") == "view"


def test_lookalike_and_http_tiktok_rejected():
    assert classify("https://evil-tiktok.com") is None
    assert classify("https://tiktok.com.evil.com") is None
    assert classify("http://www.tiktok.com") is None     # 必须是 https
    assert classify("https://nottiktok.com") is None


def test_arbitrary_sites_rejected():
    assert classify("https://example.com") is None
