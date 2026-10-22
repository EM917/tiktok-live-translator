"""本机控制面（app/server.py）的响应头：不许被别的网页用 iframe 嵌进去（点击劫持）。

专家审查 S5：以前响应里既没有 X-Frame-Options 也没有 frame-ancestors，任何网页都能把控制页透明地
叠在诱饵按钮上，骗人点到「停止」。本程序自己从不用 iframe（浮窗是独立的原生窗口），两个头都发。
起的是真的 build_app()（真中间件、真路由），不占固定端口。"""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import server as server_mod
from app.server import FRAME_ANCESTORS, CaptionServer


def run(coro):
    return asyncio.run(coro)


async def _fetch(app, paths):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        out = {}
        for path in paths:
            resp = await client.get(path)
            await resp.read()
            out[path] = (resp.status, resp.headers)
        return out
    finally:
        await client.close()


def test_pages_and_static_files_refuse_to_be_framed():
    got = run(_fetch(CaptionServer(port=0).build_app(), ["/", "/float", "/static/app.js"]))
    for path, (status, headers) in got.items():
        assert status == 200, path
        assert headers["X-Frame-Options"] == "DENY", path
        assert headers["Content-Security-Policy"] == "frame-ancestors 'none'", path
        assert headers["Cache-Control"] == "no-cache", path        # 原有的头照旧


def test_the_english_page_gets_the_same_headers():
    """英文界面走另一条分支（读文件、换 <html lang>、web.Response），头一样要有。"""
    from app import i18n

    with i18n.use("en"):
        got = run(_fetch(CaptionServer(port=0).build_app(), ["/", "/float"]))
    for path, (status, headers) in got.items():
        assert status == 200, path
        assert headers["X-Frame-Options"] == "DENY", path
        assert headers["Content-Security-Policy"] == FRAME_ANCESTORS, path


def test_an_existing_policy_is_kept_and_only_gains_frame_ancestors():
    """哪个处理函数以后自己发了 CSP：中间件不覆盖它，只在后面补上 frame-ancestors；已经写了的不重复。"""
    app = CaptionServer(port=0).build_app()

    async def strict(request):
        return web.Response(text="x", headers={"Content-Security-Policy": "default-src 'self'"})

    async def framed(request):
        return web.Response(text="x", headers={"Content-Security-Policy": "frame-ancestors 'self'"})

    app.router.add_get("/strict", strict)
    app.router.add_get("/framed", framed)
    got = run(_fetch(app, ["/strict", "/framed"]))
    assert got["/strict"][1]["Content-Security-Policy"] == "default-src 'self'; frame-ancestors 'none'"
    assert got["/framed"][1]["Content-Security-Policy"] == "frame-ancestors 'self'"
    assert got["/framed"][1]["X-Frame-Options"] == "DENY"


def test_start_serves_the_same_app():
    """start() 用的就是 build_app()：真起一次（端口 0），路由和中间件都在。"""
    async def go():
        server = CaptionServer(port=0)
        await server.start()
        try:
            app = server._runner.app
            routes = {r.resource.canonical for r in app.router.routes()}
            return routes, len(app.middlewares)
        finally:
            await server._runner.cleanup()

    routes, middlewares = run(go())
    assert {"/", "/ws", "/float", "/static"} <= routes
    assert middlewares == 1


def test_nothing_in_the_app_frames_its_own_pages():
    """两个头的前提：本程序自己的页面里没有 iframe / frame（浮窗是原生窗口，不是框）。"""
    for path in sorted(server_mod.WEB_DIR.glob("*.html")) + sorted(server_mod.WEB_DIR.glob("*.js")):
        text = path.read_text(encoding="utf-8").lower()
        assert "<iframe" not in text and "<frame" not in text, path.name
