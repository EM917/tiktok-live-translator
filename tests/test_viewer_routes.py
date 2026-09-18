"""观众面的路由与响应头。

这是攻击面本身：控制面绑 127.0.0.1，观众面绑 0.0.0.0。所以要钉住三件事——
观众 app 上**不存在**通往 on_control / _ws / _index 的路；静态文件只认那四个
名字（不是一个静态目录）；响应头带 CSP 且没有 unsafe-inline。

这里全部直接调 handler 和中间件，一个端口都不绑；唯一一条真 socket 在
tests/test_viewer_e2e.py。
"""
from types import SimpleNamespace

import pytest
from aiohttp import web

from app import viewer as viewer_mod
from app.viewer import ViewerHub
from tests.helpers import run

TOKEN = "t" * viewer_mod.TOKEN_LEN


@pytest.fixture
def web_dir(tmp_path):
    """手机页那几个文件由另一份改动提供。这里用同名的替身，路由面的性质
    不该依赖那几个文件的内容。"""
    for name in viewer_mod.VIEWER_FILES:
        (tmp_path / name).write_text("stand-in for " + name, encoding="utf-8")
    return tmp_path


@pytest.fixture
def hub(web_dir):
    return ViewerHub(SimpleNamespace(config={}, history=[], alerts=[], comments=[]),
                     ports=[0], token=TOKEN, bind_host="127.0.0.1",
                     web_dir=web_dir, log=lambda *a: None)


def request_for(name=None):
    return SimpleNamespace(match_info=({} if name is None else {"name": name}),
                           headers={}, transport=None)


def test_the_route_table_is_exactly_three_entries(hub):
    app = hub.build_app()
    routes = [(r.method, r.resource.canonical) for r in app.router.routes()]
    assert sorted(routes) == [("GET", "/"), ("GET", "/v/{name}"), ("GET", "/vws")]


def test_no_route_points_at_the_control_face(hub):
    """观众面上根本不存在通往控制面的路——这是「两套 app」这个设计的全部意义。"""
    server = hub._server
    app = hub.build_app()
    handlers = [r.handler for r in app.router.routes()]
    for attr in ("on_control", "_ws", "_index"):
        assert getattr(server, attr, None) not in handlers
    names = [getattr(h, "__name__", "") for h in handlers]
    assert names == sorted(["page", "vws", "asset"]) or set(names) == {"page", "vws",
                                                                      "asset"}


def test_there_is_no_static_resource(hub):
    """add_static 会把 web/ 下每个文件都端出去，包括 app.js 和 index.html。"""
    app = hub.build_app()
    kinds = [type(r).__name__ for r in app.router.resources()]
    assert "StaticResource" not in kinds
    assert all("Static" not in k for k in kinds)


@pytest.mark.parametrize("name", viewer_mod.VIEWER_FILES)
def test_the_four_viewer_files_are_served(hub, name):
    resp = run(hub.asset(request_for(name)))
    assert isinstance(resp, web.FileResponse)


@pytest.mark.parametrize("name", [
    "app.js", "index.html", "style.css", "normalize.js", "alerts.js", "follow.js",
    "../settings.json", "%2e%2e/settings.json", "..%2fsettings.json",
    "..", "../../etc/passwd", "viewer.html/../app.js", "", "VIEWER.HTML",
    "banned_terms.txt", "settings.json",
])
def test_everything_else_is_404(hub, name):
    with pytest.raises(web.HTTPNotFound):
        run(hub.asset(request_for(name)))


def test_a_whitelisted_name_that_is_not_on_disk_is_404(tmp_path):
    """名字对但文件不在（另一份改动还没落地）：404，不是 500。"""
    hub = ViewerHub(SimpleNamespace(config={}), ports=[0], token=TOKEN,
                    web_dir=tmp_path, log=lambda *a: None)
    with pytest.raises(web.HTTPNotFound):
        run(hub.asset(request_for("viewer.css")))
    with pytest.raises(web.HTTPNotFound):
        run(hub.page(request_for()))


def middleware_of(hub):
    app = hub.build_app()
    mws = list(app.middlewares)
    assert len(mws) == 1
    return mws[0]


def test_responses_carry_the_security_headers(hub):
    guard = middleware_of(hub)
    resp = run(guard(request_for(), hub.page))
    assert resp.headers["Cache-Control"] == "no-cache"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "script-src 'self'" in csp
    # A7：connect-src 用 'self'，不放宽成裸 ws:（那等于允许连任意主机）
    assert "connect-src 'self'" in csp
    assert "ws:" not in csp


def test_even_a_404_carries_the_headers(hub):
    """404 也在局域网里，也要带上这些头。中间件把头写在异常上再原样抛回去
    （直接返回 HTTPException 对象已被 aiohttp 弃用）。"""
    guard = middleware_of(hub)
    with pytest.raises(web.HTTPNotFound) as caught:
        run(guard(request_for("app.js"), hub.asset))
    assert "Content-Security-Policy" in caught.value.headers
    assert caught.value.headers["X-Content-Type-Options"] == "nosniff"


def test_the_phone_page_has_no_inline_script():
    """CSP 里没有 unsafe-inline，所以 viewer.html 里不能有内联 script——
    否则手机上整页静默不工作，而服务端这边什么都看不出来。"""
    path = viewer_mod.WEB_DIR / "viewer.html"
    if not path.is_file():
        pytest.skip("web/viewer.html 还没落地（由另一份改动提供）")
    html = path.read_text(encoding="utf-8")
    lowered = html.lower()
    depth = 0
    for chunk in lowered.split("<script")[1:]:
        head, _, rest = chunk.partition(">")
        if "src=" not in head:
            depth += 1
        body = rest.split("</script")[0]
        assert not body.strip(), "viewer.html 里有内联 script 块"
    assert depth == 0


def test_the_phone_page_only_references_viewer_assets():
    path = viewer_mod.WEB_DIR / "viewer.html"
    if not path.is_file():
        pytest.skip("web/viewer.html 还没落地（由另一份改动提供）")
    html = path.read_text(encoding="utf-8")
    assert "/static/" not in html


def test_viewer_files_are_plain_names():
    """白名单里的名字不能含分隔符：否则 /v/{name} 就成了一条路径穿越入口。"""
    for name in viewer_mod.VIEWER_FILES:
        assert "/" not in name and "\\" not in name and ".." not in name
