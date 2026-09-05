"""Chrome 桥：借用户已登录的 Chrome 拿直播流地址。

背景见 chrome-bridge-spec.md——TikTok 对有些房间（如年龄限制、或接口固定返回
4003110）只把播放地址给已登录的浏览器，程序自己签不出 room/enter 需要的参数。
这条链路让插件在用户自己的 Chrome 里把播放器实际拉的地址报回来。

覆盖范围（按 chrome-bridge-spec.md 的分工，这份文件只管 K.1/2/3/4/7）：
  1. server 白名单：view 来源发 stream_url/page_state → on_browser 被调、
     on_control 不被调；start 仍被拦；
  2. BrowserBridge.note：丢弃不合规地址，合规地址被记住；
  3. BrowserBridge.request_stream：先问已开的页面、无回应才开浏览器、
     logged_in=False 立刻报错、超时报错提到插件、旧地址过期不复用；
  4. Pipeline._resolve_media：本机解析不动时转桥，offline/直接流地址不转桥；
  7. 插件静态检查：background.js / content.js 语法、manifest.json 合法。

不 import 其它测试文件，避免耦合到别处的 fixture 变化（照抄
tests/test_comments.py 的 StubServer/make_pipeline 写法）。
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import pipeline as pipeline_mod
from app.pipeline import Pipeline
from app.resolver import ResolveError
from app.server import CaptionServer

REPO_ROOT = Path(__file__).resolve().parent.parent


def run(coro):
    return asyncio.run(coro)


async def wait_until(cond, limit=200):
    """有限轮询：最多 limit * 0.01s，绝不无限等（铁律：等待一律有限轮询）。"""
    for _ in range(limit):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


# ---------------------------------------------------------------------------
# 1. server 白名单：stream_url / page_state 走 on_browser，绝不到 on_control
# ---------------------------------------------------------------------------

def test_view_origin_stream_messages_go_to_on_browser_not_control():
    control_calls = []
    browser_calls = []

    async def scenario():
        server = CaptionServer(port=8765)
        server.on_control = lambda data: control_calls.append(data)
        server.on_browser = lambda data: browser_calls.append(data)

        app = web.Application()
        app.router.add_get("/ws", server._ws)
        test_server = TestServer(app)
        client = TestClient(test_server)
        await client.start_server()
        try:
            # view 来源：插件以 TikTok 页面身份连接
            ws = await client.ws_connect("/ws", headers={"Origin": "https://www.tiktok.com"})
            await ws.send_json({"type": "start", "url": "https://x"})  # 应被拦，不到 on_control
            await ws.send_json({"type": "stream_url", "streamer": "bella2",
                                "url": "https://pull-flv-x.tiktokcdn-us.com/a_hd.flv?sign=1",
                                "logged_in": True})
            await ws.send_json({"type": "page_state", "streamer": "bella2", "logged_in": True})
            await asyncio.sleep(0.05)
            await ws.close()

            # 无 Origin 头（本机/非浏览器客户端）：start 应该照常走 on_control
            ws2 = await client.ws_connect("/ws")
            await ws2.send_json({"type": "start", "url": "https://x"})
            await asyncio.sleep(0.05)
            await ws2.close()
        finally:
            await client.close()

    run(scenario())
    # view 来源的 start 一条都不该漏到 on_control；无 Origin 的那条才算数
    assert len(control_calls) == 1
    assert control_calls[0]["type"] == "start"
    assert [c["type"] for c in browser_calls] == ["stream_url", "page_state"]


def test_view_origin_browser_messages_are_rate_limited_per_connection():
    """和 viewer_comments 共用「按连接限频」这道闸——插件所在的页面同样能被
    恶意脚本高频灌 stream_url/page_state，不能让它们无限制地占用 CPU。"""
    browser_calls = []

    async def scenario():
        server = CaptionServer(port=8765)
        server.CMT_MSG_RATE_LIMIT = 3
        server.CMT_MSG_RATE_WINDOW_SEC = 1.0
        server.on_browser = lambda data: browser_calls.append(data)

        app = web.Application()
        app.router.add_get("/ws", server._ws)
        test_server = TestServer(app)
        client = TestClient(test_server)
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws", headers={"Origin": "https://www.tiktok.com"})
            for i in range(10):
                await ws.send_json({"type": "page_state", "streamer": "s{}".format(i),
                                    "logged_in": True})
            await asyncio.sleep(0.05)
            await ws.close()
        finally:
            await client.close()

    run(scenario())
    assert len(browser_calls) <= 3


# ---------------------------------------------------------------------------
# 2 & 3. BrowserBridge.note / request_stream
# ---------------------------------------------------------------------------

VALID_URL = "https://pull-flv-xxx.tiktokcdn-us.com/game/stream-abc_hd.flv?expire=1&sign=2"
LIVE_URL = "https://www.tiktok.com/@bella2/live"


def make_bridge(ask_first=0.05, wait=0.05):
    from app.browser_bridge import BrowserBridge

    broadcasts = []
    opens = []
    statuses = []

    async def broadcast(msg):
        broadcasts.append(msg)

    def open_url(url):
        opens.append(url)
        return True

    async def status(state, detail=""):
        statuses.append((state, detail))

    bridge = BrowserBridge(broadcast=broadcast, open_url=open_url, status=status)
    bridge.ASK_FIRST_SEC = ask_first
    bridge.WAIT_SEC = wait
    return bridge, broadcasts, opens, statuses


def _recorded_stream_url(bridge, streamer):
    """note() 把消息记进 bridge._state[streamer]（形状见 browser_bridge.py 里
    BrowserBridge.__init__ 的注释）。直接读这个内部状态，而不是借道
    request_stream 的等待逻辑去间接观察——note() 记没记住，和 request_stream
    什么时候会注意到，是两件事（后者在下面几条 request_stream 测试里覆盖）。"""
    entry = bridge._state.get(streamer) or {}
    su = entry.get("stream_url")
    return su.get("url") if su else None


def test_note_discards_illegal_urls():
    illegal = [
        "http://pull-flv-xxx.tiktokcdn-us.com/a.flv",       # 非 https
        "https://evil.example.com/a.flv?sign=1",             # 主机不是 TikTok CDN
        "https://pull-flv-xxx.tiktokcdn-us.com/" + "a" * 2100 + ".flv",  # 超长
    ]
    bridge, _, _, _ = make_bridge()
    for i, url in enumerate(illegal):
        streamer = "bad{}".format(i)
        bridge.note({"type": "stream_url", "streamer": streamer, "url": url,
                    "logged_in": True})
        assert _recorded_stream_url(bridge, streamer) is None


def test_note_keeps_compliant_url():
    bridge, _, _, _ = make_bridge()
    bridge.note({"type": "stream_url", "streamer": "bella2", "url": VALID_URL,
                "logged_in": True})
    assert _recorded_stream_url(bridge, "bella2") == VALID_URL


def test_note_keeps_compliant_url_and_request_stream_returns_it_without_opening_browser():
    """note() 记住的地址如果在 request_stream 开始等待之前就已存在（比如插件
    走 content.js 里的 SIGI 快路径，在 need_stream_url 广播之前就已经把地址
    发过来了），也应该被直接用上——不该白白打开一次浏览器。

    2026-09-05 记录：当前 app/browser_bridge.py 的 _wait_phase 只在 event 被
    唤醒时才去看 self._state，request_stream 一开始又会把 event.clear() 一次；
    如果合规地址是在 request_stream 开始等待之前记录的，没有后续消息把 event
    唤醒的话，这个已经在手边的地址就会被晾到超时，白白弹一次浏览器。这条测试
    目前会失败，已经在交付说明里报给整合者核实。"""
    async def scenario():
        bridge, _, opens, _ = make_bridge(ask_first=0.05, wait=0.05)
        bridge.note({"type": "stream_url", "streamer": "bella2", "url": VALID_URL,
                    "logged_in": True})
        media = await bridge.request_stream("bella2", LIVE_URL)
        assert media == VALID_URL
        assert opens == []

    run(scenario())


def test_request_stream_returns_immediately_when_answered_within_first_window():
    async def scenario():
        bridge, _, opens, _ = make_bridge(ask_first=1.0, wait=1.0)
        task = asyncio.ensure_future(bridge.request_stream("bella2", LIVE_URL))
        await asyncio.sleep(0.05)
        bridge.note({"type": "stream_url", "streamer": "bella2", "url": VALID_URL,
                    "logged_in": True})
        media = await asyncio.wait_for(task, timeout=2)
        assert media == VALID_URL
        assert opens == []  # 3 秒内就有回应，不该去开浏览器

    run(scenario())


def test_request_stream_opens_browser_after_no_early_answer_then_succeeds():
    async def scenario():
        bridge, _, opens, statuses = make_bridge(ask_first=0.05, wait=1.0)
        task = asyncio.ensure_future(bridge.request_stream("bella2", LIVE_URL))
        assert await wait_until(lambda: len(opens) == 1)
        assert opens == [LIVE_URL]
        assert any(st == "connecting" for st, _ in statuses)
        bridge.note({"type": "stream_url", "streamer": "bella2", "url": VALID_URL,
                    "logged_in": True})
        media = await asyncio.wait_for(task, timeout=2)
        assert media == VALID_URL
        assert opens == [LIVE_URL]  # 只开一次，不重复弹浏览器

    run(scenario())


def test_request_stream_fails_fast_on_logged_out_page_state():
    async def scenario():
        bridge, _, opens, _ = make_bridge(ask_first=0.05, wait=5.0)
        task = asyncio.ensure_future(bridge.request_stream("bella2", LIVE_URL))
        await asyncio.sleep(0.02)
        bridge.note({"type": "page_state", "streamer": "bella2", "logged_in": False})
        with pytest.raises(ResolveError) as excinfo:
            await asyncio.wait_for(task, timeout=1)  # 远小于 wait=5.0：不该等满
        assert excinfo.value.kind == "login"

    run(scenario())


def test_request_stream_times_out_with_message_mentioning_extension():
    async def scenario():
        bridge, _, _, _ = make_bridge(ask_first=0.02, wait=0.05)
        with pytest.raises(ResolveError) as excinfo:
            await bridge.request_stream("bella2", LIVE_URL)
        assert "插件" in str(excinfo.value)

    run(scenario())


def test_stale_stream_url_older_than_120s_is_not_reused(monkeypatch):
    """假设 note() 用 time.time() 打时间戳（仓库里限频等场景都是这么写的，见
    app/server.py 的 cmt_times）；这里冻结/推进这个时钟来模拟「120 秒前的旧地址」，
    而不是真的在测试里等 2 分钟。若 browser_bridge.py 换了别的计时方式，这条测试
    需要跟着调整取时钟的路径。"""
    from app import browser_bridge as bridge_mod

    fake_now = [1_000_000.0]
    monkeypatch.setattr(bridge_mod.time, "time", lambda: fake_now[0])

    async def scenario():
        bridge, _, opens, _ = make_bridge(ask_first=0.05, wait=0.05)

        bridge.note({"type": "stream_url", "streamer": "bella2", "url": VALID_URL,
                    "logged_in": True})
        fake_now[0] += 121  # 120 秒过期线之后

        with pytest.raises(ResolveError):
            await bridge.request_stream("bella2", LIVE_URL)
        # 旧地址不算数 → 照样要走「开浏览器」这一步
        assert len(opens) == 1

    run(scenario())


# ---------------------------------------------------------------------------
# 4. Pipeline._resolve_media：本机解析不动时转桥
# ---------------------------------------------------------------------------

class StubServer:
    """照抄 tests/test_comments.py 的写法：只记消息，不真的起网络。"""

    def __init__(self):
        self.config = {}
        self.messages = []

    async def status(self, state, detail=""):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)


def make_pipeline(monkeypatch, tmp_path):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)

    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None,
        cookies_browser="auto",
    )
    server = StubServer()
    p = Pipeline(args, server)
    return p, server


def test_resolve_media_falls_back_to_browser_bridge_on_browser_only(monkeypatch, tmp_path):
    from app import resolver as resolver_mod

    p, _server = make_pipeline(monkeypatch, tmp_path)

    async def fake_resolve(url, cookies=None, cookies_browser="auto"):
        raise ResolveError("TikTok 没有把流地址给程序（代码 4003110）", kind="browser_only")

    async def fake_check(url, trusted=False):
        return url

    async def fake_works(url, timeout=8):
        return True

    requested = []

    async def fake_request_stream(streamer, live_url):
        requested.append((streamer, live_url))
        return "https://pull-flv-xxx.tiktokcdn-us.com/from_chrome.flv?sign=1"

    monkeypatch.setattr(resolver_mod, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(resolver_mod, "_check_media_url", fake_check)
    monkeypatch.setattr(resolver_mod, "_media_url_works", fake_works)
    monkeypatch.setattr(p.browser_bridge, "request_stream", fake_request_stream)

    media = run(p._resolve_media("https://www.tiktok.com/@bella2/live"))

    assert media == "https://pull-flv-xxx.tiktokcdn-us.com/from_chrome.flv?sign=1"
    assert requested == [("bella2", "https://www.tiktok.com/@bella2/live")]


@pytest.mark.parametrize("kind", ["offline", "not_found", "network", "internal"])
def test_resolve_media_reraises_directly_for_non_browser_kinds(monkeypatch, tmp_path, kind):
    """offline/not_found/network/internal 这几种转去开 Chrome 毫无意义
    （offline 是「确认下播」，其余是本机/网络问题，插件也解决不了）——
    桥完全不该被调用，异常原样往外抛。"""
    from app import resolver as resolver_mod

    p, _server = make_pipeline(monkeypatch, tmp_path)

    async def fake_resolve(url, cookies=None, cookies_browser="auto"):
        raise ResolveError("解析失败", kind=kind)

    called = []

    async def fake_request_stream(streamer, live_url):
        called.append((streamer, live_url))
        return "should-not-be-used"

    monkeypatch.setattr(resolver_mod, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p.browser_bridge, "request_stream", fake_request_stream)

    with pytest.raises(ResolveError) as excinfo:
        run(p._resolve_media("https://www.tiktok.com/@bella2/live"))
    assert excinfo.value.kind == kind
    assert called == []  # 桥完全没被碰


def test_resolve_media_does_not_use_bridge_for_direct_stream_url(monkeypatch, tmp_path):
    """用户直接给的 .flv/.m3u8 地址：is_direct_url 为真，即便解析层抛
    browser_only 也不该转桥——那种地址本来就不该走「按用户名找直播间」这条路。"""
    from app import resolver as resolver_mod

    p, _server = make_pipeline(monkeypatch, tmp_path)

    async def fake_resolve(url, cookies=None, cookies_browser="auto"):
        raise ResolveError("解析失败", kind="browser_only")

    called = []

    async def fake_request_stream(streamer, live_url):
        called.append((streamer, live_url))
        return "should-not-be-used"

    monkeypatch.setattr(resolver_mod, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p.browser_bridge, "request_stream", fake_request_stream)

    with pytest.raises(ResolveError):
        run(p._resolve_media("https://cdn.example.com/direct.flv"))
    assert called == []


# ---------------------------------------------------------------------------
# 7. 插件静态检查
# ---------------------------------------------------------------------------

def _has_node():
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _has_node(), reason="node 不可用，跳过插件语法检查")
def test_background_and_content_js_are_syntactically_valid():
    for name in ("background.js", "content.js"):
        path = REPO_ROOT / "extension" / name
        result = subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, "{}: {}".format(name, result.stderr)


def test_manifest_json_is_valid_and_declares_webrequest_and_background():
    manifest_path = REPO_ROOT / "extension" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "webRequest" in data.get("permissions", [])
    assert data.get("background", {}).get("service_worker") == "background.js"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---- 硬保险：测试/自动化环境里绝不真的打开浏览器 ----

def test_open_in_browser_refuses_under_pytest(monkeypatch):
    """2026-09-05 一条重连测试真的在用户的 Chrome 里开了三个 @x/live 标签页。
    pytest 会设置 PYTEST_CURRENT_TEST，open_in_browser 看到它必须直接拒绝，
    连 subprocess 都不许碰。"""
    import subprocess

    from app import browser_bridge

    def boom(*args, **kwargs):
        raise AssertionError("open_in_browser 在测试里真的去起浏览器了")

    monkeypatch.setattr(subprocess, "run", boom)
    assert browser_bridge.open_in_browser("https://www.tiktok.com/@x/live") is False


def test_resolve_media_does_not_use_bridge_for_unknown_failures(monkeypatch, tmp_path):
    """kind=unknown 的普通失败（网络抖动、yt-dlp 500……）不该去借 Chrome：
    借了只会白等一轮超时，还会真的弹出浏览器。"""
    from app.resolver import ResolveError

    p, _server = make_pipeline(monkeypatch, tmp_path)
    called = []

    async def failing(url, cookies=None, cookies_browser="auto"):
        raise ResolveError("HTTP Error 500", kind="unknown")

    async def fake_request_stream(streamer, live_url):
        called.append(streamer)
        return "https://pull-flv-x.tiktokcdn-us.com/a.flv"

    import app.resolver as resolver_mod
    monkeypatch.setattr(resolver_mod, "resolve_stream_url", failing)
    monkeypatch.setattr(p.browser_bridge, "request_stream", fake_request_stream)
    with pytest.raises(ResolveError):
        run(p._resolve_media("https://www.tiktok.com/@x/live"))
    assert called == []
