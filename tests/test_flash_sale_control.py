"""闪购助手的控制接口（app/flash_sale.py 的 read_control_token、control_url、post_control）：真的走 HTTP，
但对面是本机临时端口上的假闪购助手；密钥是 tmp_path 里的假文件。

**绝不连 127.0.0.1:9334，绝不读本机真的 ~/Library/Application Support/FlashSaleHelper/control-token**：
开发机上闪购助手可能正开着，一个真的 POST /stop 会停掉直播里正在续开的闪购。tests/conftest.py 的 autouse 夹具把
控制地址和密钥位置都换掉了，最后一节有一条钉着这张网。

假闪购助手照契约「控制(0.8.0 起)」逐条检查（fake_helper / contract_problems），任一条不满足回 403、什么都不做。
所以「200 回来了」本身就钉住了我们发的请求满足全部防冒充条件；另有一组对照，证明这些检查真的会拦。
假密钥 FAKE_TOKEN 是虚构的，它的任何一截都不许出现在终端输出里。
"""
import asyncio
import json
import os
import socket
import stat
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from app import flash_sale as fs

# tests/conftest.py 的 autouse 夹具在每条用例开始前才换；收集时（模块导入时）这里拿到的是真的那两个
REAL_CONTROL_BASE = fs.CONTROL_BASE
REAL_CONTROL_TOKEN_PATH = fs.control_token_path

FAKE_TOKEN = "7e57" * 16
OTHER_TOKEN = "c0de" * 16
LEAKS = ("7e577e57", "c0dec0de")      # 假密钥的一截：哪儿都不许出现


# ---- 照契约检查的假闪购助手 ----------------------------------------------------------------------

def contract_problems(request, body, port, token):
    """契约「控制(0.8.0 起)」的五条防冒充条件，逐条查，返回没满足的那几条的编号（空表 = 全满足）。"""
    headers = request.headers
    bad = []
    if headers.get("Host") not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
        bad.append(1)
    if "Origin" in headers or any(k.lower().startswith("sec-fetch-") for k in headers):
        bad.append(2)
    if headers.get("Authorization") != "Bearer " + token:
        bad.append(3)
    if headers.get("Content-Type", "").replace(" ", "").lower() not in (
            "application/json", "application/json;charset=utf-8"):
        bad.append(4)
    try:
        doc = json.loads(body) if len(body) <= 1024 else None
    except ValueError:
        doc = None
    if doc not in ({}, {"source": "tlt"}):
        bad.append(5)
    return bad


def fake_helper(seen, token=FAKE_TOKEN, reply=None, actions=fs.CONTROL_ACTIONS):
    """POST /start、/stop、/resume：先照契约查，不满足回 403（什么都不做）；满足了交给 reply，缺省回
    200 {"ok":true,"accepted":<动作>}。每个请求记进 seen（方法、路径、请求头、请求体、没满足的条件）。"""
    async def handle(request):
        port = request.transport.get_extra_info("sockname")[1]
        body = await request.read()
        problems = contract_problems(request, body, port, token)
        seen.append({"method": request.method, "path": request.path, "headers": dict(request.headers),
                     "body": body, "problems": problems})
        if problems:
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        if reply is not None:
            return await reply(request)
        return web.json_response({"ok": True, "accepted": request.path.lstrip("/")})

    app = web.Application()
    for action in actions:
        app.router.add_post("/" + action, handle)
    return app


async def serving(app, body):
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        assert server.port != 9334
        return await body("http://127.0.0.1:%d" % server.port)
    finally:
        await server.close()


def post_to(app, action="start", token=FAKE_TOKEN, **kwargs):
    async def body(base):
        return await fs.post_control(action, token, base=base, **kwargs)
    return asyncio.run(serving(app, body))


def replying(status, payload=None, raw=None):
    async def reply(request):
        if raw is not None:
            return web.Response(status=status, body=raw, content_type="application/json")
        return web.json_response(payload, status=status)
    return reply


def no_leak(text):
    return not any(piece in text for piece in LEAKS)


# ---- 我们发的请求满足全部条件 ----------------------------------------------------------------------

@pytest.mark.parametrize("action", ["start", "stop", "resume"])
def test_our_request_meets_every_condition_of_the_contract(action, capsys):
    seen = []
    assert post_to(fake_helper(seen), action) is None
    (req,) = seen
    assert req["problems"] == [] and req["method"] == "POST" and req["path"] == "/" + action
    assert req["body"] == b'{"source":"tlt"}' == fs.CONTROL_BODY
    assert req["headers"]["Authorization"] == "Bearer " + FAKE_TOKEN
    assert req["headers"]["Content-Type"] == "application/json"
    lowered = {k.lower() for k in req["headers"]}
    for absent in ("origin", "referer", "cookie", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
                   "sec-fetch-user"):
        assert absent not in lowered, absent
    assert capsys.readouterr().out == ""                 # 这个模块不打印


@pytest.mark.parametrize("change, rule", [
    ({"Host": "evil.example:9334"}, 1),
    ({"Host": "127.0.0.1:9334"}, 1),                        # 端口对不上假服务器的端口
    ({"Origin": "http://127.0.0.1:8765"}, 2),
    ({"Origin": "null"}, 2),
    ({"Sec-Fetch-Site": "same-site"}, 2),
    ({"Sec-Fetch-Mode": "cors"}, 2),
    ({"Authorization": None}, 3),
    ({"Authorization": "Bearer " + OTHER_TOKEN}, 3),
    ({"Authorization": "Basic " + FAKE_TOKEN}, 3),
    ({"Authorization": "Bearer " + FAKE_TOKEN.upper()}, 3),
    ({"Content-Type": "text/plain"}, 4),
    ({"Content-Type": None}, 4),
    ({"body": b'{"source":"tlt","action":"stop"}'}, 5),
    ({"body": b"[]"}, 5),
    ({"body": b'{"source":"' + b"x" * 2000 + b'"}'}, 5),
])
def test_the_fake_helper_really_refuses_what_the_contract_refuses(change, rule):
    """对照：照着正确的请求只改一处，假闪购助手就回 403——所以上面那几条 200 说明我们发的请求每一条都满足。"""
    seen = []

    async def body(base):
        headers = {"Authorization": "Bearer " + FAKE_TOKEN, "Content-Type": "application/json"}
        payload = fs.CONTROL_BODY
        for key, value in change.items():
            if key == "body":
                payload = value
            elif value is None:
                headers.pop(key)
            else:
                headers[key] = value
        async with ClientSession() as session:
            async with session.post(base + "/stop", data=payload, headers=headers,
                                    skip_auto_headers=["Content-Type"]) as resp:
                return resp.status

    assert asyncio.run(serving(fake_helper(seen), body)) == 403
    assert rule in seen[0]["problems"]


def test_a_charset_on_the_content_type_is_also_accepted_by_the_fake_helper():
    """契约允许 application/json 后面带 ; charset=utf-8：假闪购助手不能比契约更严，不然它挡住的东西说明不了问题。"""
    seen = []

    async def body(base):
        headers = {"Authorization": "Bearer " + FAKE_TOKEN, "Content-Type": "application/json; charset=utf-8"}
        async with ClientSession() as session:
            async with session.post(base + "/start", data=b"{}", headers=headers) as resp:
                return resp.status

    assert asyncio.run(serving(fake_helper(seen), body)) == 200 and seen[0]["problems"] == []


def test_a_token_the_helper_does_not_know_gets_its_403():
    seen = []
    assert post_to(fake_helper(seen, token=OTHER_TOKEN), "stop") == "http_403"
    assert seen[0]["problems"] == [3]


# ---- 回话 --------------------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["no_page", "login_required", "loading", "captcha", "awaiting_approval",
                                  "already_running", "paused", "not_running", "not_paused", "busy",
                                  "no_response", "error", "brand_new_code"])
def test_a_409_hands_back_the_helpers_code_as_is(code):
    """契约 409 的代码表，加一个认不出的：一律原样（清洗后）交给前端，前端认得的换成话，认不出的原样显示。"""
    seen = []
    got = post_to(fake_helper(seen, reply=replying(409, {"ok": False, "error": code})), "start")
    assert got == code and seen[0]["problems"] == []


@pytest.mark.parametrize("payload, raw, expected", [
    ({"ok": False, "error": "bad code<script>"}, None, "badcodescript"),
    ({"ok": False, "error": "x" * 100}, None, "x" * fs.MAX_CODE),
    ({"ok": False}, None, "http_409"),
    ({"ok": False, "error": 7}, None, "http_409"),
    ({"ok": False, "error": ""}, None, "http_409"),
    (None, b"not json", "http_409"),
    (None, b"", "http_409"),
])
def test_a_409_without_a_readable_code_is_http_409(payload, raw, expected):
    assert post_to(fake_helper([], reply=replying(409, payload, raw)), "stop") == expected


@pytest.mark.parametrize("payload, raw, action, expected", [
    ({"ok": True, "accepted": "stop"}, None, "stop", None),
    ({"ok": True}, None, "resume", None),                       # 没带 accepted：照样算接受
    ({"ok": True, "accepted": "start"}, None, "stop", "bad_reply"),   # 接受的不是我们发的那个
    ({"ok": "true"}, None, "stop", "bad_reply"),
    ({"ok": False, "error": "busy"}, None, "stop", "bad_reply"),     # 200 却说不行：不当接受
    ([True], None, "stop", "bad_reply"),
    (None, b"not json", "stop", "bad_reply"),
    (None, b"", "stop", "bad_reply"),
])
def test_only_a_200_that_says_ok_counts_as_accepted(payload, raw, action, expected):
    assert post_to(fake_helper([], reply=replying(200, payload, raw)), action) == expected


def test_a_reply_over_the_limit_is_bad_reply():
    big = json.dumps({"ok": True, "pad": "x" * 5000}).encode()
    assert post_to(fake_helper([], reply=replying(200, raw=big)), "stop", limit=1000) == "bad_reply"


def test_an_older_helper_without_the_control_paths_is_no_control():
    """0.8.0 以前只有 GET /status：POST /start 是 404。另一种：路径在、方法不认（405）。都当「这个版本没有控制接口」。"""
    async def status(request):
        return web.json_response({"schema": 1})

    old = web.Application()
    old.router.add_get("/status", status)
    assert post_to(old, "start") == "no_control"
    get_only = web.Application()
    get_only.router.add_get("/stop", status)
    assert post_to(get_only, "stop") == "no_control"


@pytest.mark.parametrize("status", [403, 500, 503, 400])
def test_other_statuses_are_http_codes(status):
    assert post_to(fake_helper([], reply=replying(status, {"ok": False})), "stop") == "http_%d" % status


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_not_followed(status):
    """不跟随重定向：/start 回 3xx 指向 /stop，我们不去 /stop（307、308 跟过去还会原样再 POST 一次，带着密钥）。"""
    seen = []

    async def bounce(request):
        return web.Response(status=status, headers={"Location": "/stop"})

    assert post_to(fake_helper(seen, reply=bounce), "start") == "http_%d" % status
    assert [r["path"] for r in seen] == ["/start"]


# ---- 连不上、不回话、回的不是 HTTP ------------------------------------------------------------------

def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_port_nobody_listens_on_is_unreachable():
    port = free_port()
    assert port != 9334
    got = asyncio.run(fs.post_control("stop", FAKE_TOKEN, base="http://127.0.0.1:%d" % port, timeout=1.0))
    assert got == "unreachable"


def test_a_slow_answer_is_no_answer():
    async def slow(request):
        await asyncio.sleep(2.0)
        return web.json_response({"ok": True})
    assert post_to(fake_helper([], reply=slow), "stop", timeout=0.2) == "no_answer"


def test_a_frozen_listener_is_no_answer():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        port = sock.getsockname()[1]
        assert port != 9334
        got = asyncio.run(fs.post_control("stop", FAKE_TOKEN, base="http://127.0.0.1:%d" % port, timeout=0.3))
    assert got == "no_answer"


async def raw_peer(respond, timeout=1.0):
    async def handle(reader, writer):
        try:
            await respond(reader, writer)
        finally:
            writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    assert port != 9334
    try:
        return await fs.post_control("stop", FAKE_TOKEN, base="http://127.0.0.1:%d" % port, timeout=timeout)
    finally:
        server.close()
        await server.wait_closed()


def test_hanging_up_without_answering_is_no_answer():
    async def hang_up(reader, writer):
        pass
    assert asyncio.run(raw_peer(hang_up)) == "no_answer"


def test_something_that_does_not_speak_http_is_bad_reply():
    async def not_http(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"SSH-2.0-Example\r\n\r\n")
        await writer.drain()
    assert asyncio.run(raw_peer(not_http)) == "bad_reply"


def test_a_reply_cut_off_halfway_is_bad_reply():
    async def half(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"ok\": tr")
        await writer.drain()
    assert asyncio.run(raw_peer(half)) == "bad_reply"


class _RaisesOnEnter:
    def __init__(self, exc):
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *exc_info):
        return False


def test_a_timeout_while_connecting_is_unreachable(monkeypatch):
    import aiohttp
    if not hasattr(aiohttp, "ConnectionTimeoutError"):
        pytest.skip("aiohttp 3.10 以前分不出连接超时和读超时")
    monkeypatch.setattr(aiohttp.ClientSession, "post",
                        lambda self, *a, **k: _RaisesOnEnter(aiohttp.ConnectionTimeoutError("connect")))
    assert asyncio.run(fs.post_control("stop", FAKE_TOKEN, base="http://127.0.0.1:9", timeout=1.0)) == "unreachable"


def test_a_timeout_after_connecting_is_no_answer(monkeypatch):
    import aiohttp
    if not hasattr(aiohttp, "SocketTimeoutError"):
        pytest.skip("aiohttp 3.10 以前分不出连接超时和读超时")
    monkeypatch.setattr(aiohttp.ClientSession, "post",
                        lambda self, *a, **k: _RaisesOnEnter(aiohttp.SocketTimeoutError("read")))
    assert asyncio.run(fs.post_control("stop", FAKE_TOKEN, base="http://127.0.0.1:9", timeout=1.0)) == "no_answer"


def test_a_client_response_error_carrying_the_token_never_leaves_post_control(monkeypatch, capsys):
    """aiohttp 的 ClientResponseError 的 repr 里带着请求头，也就带着 Authorization。post_control 把它换成代码，
    不往外抛、不打印。先确认这个隐患是真的（repr 里真有密钥），再确认它没出去。"""
    import aiohttp
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    info = aiohttp.RequestInfo(URL("http://127.0.0.1:9/stop"), "POST",
                               CIMultiDictProxy(CIMultiDict({"Authorization": "Bearer " + FAKE_TOKEN})))
    exc = aiohttp.ClientResponseError(info, (), status=400, message="x")
    assert FAKE_TOKEN in repr(exc)
    monkeypatch.setattr(aiohttp.ClientSession, "post", lambda self, *a, **k: _RaisesOnEnter(exc))
    assert asyncio.run(fs.post_control("stop", FAKE_TOKEN, base="http://127.0.0.1:9")) == "bad_reply"
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_cancelling_a_post_propagates():
    async def slow(request):
        await asyncio.sleep(5.0)
        return web.json_response({"ok": True})

    async def body(base):
        task = asyncio.ensure_future(fs.post_control("stop", FAKE_TOKEN, base=base, timeout=10.0))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return True
    assert asyncio.run(serving(fake_helper([], reply=slow), body)) is True


# ---- 不读环境变量里的代理 ---------------------------------------------------------------------------

PROXY_VARS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


async def counting_proxy(hits):
    """一个只记账的假代理：有人连上来就记一笔，回 502。"""
    async def handle(reader, writer):
        hits.append(1)
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            pass
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        try:
            await writer.drain()
        finally:
            writer.close()
    return await asyncio.start_server(handle, "127.0.0.1", 0)


def test_proxy_settings_in_the_environment_are_ignored(monkeypatch):
    """设了 HTTP_PROXY 的机器上，带密钥的请求也不能交给代理（trust_env=False）。对照：同样的环境下 trust_env=True
    的 session 确实会走这个假代理——不然这条测不出东西。"""
    proxy_hits, seen = [], []

    async def go():
        proxy = await counting_proxy(proxy_hits)
        url = "http://127.0.0.1:%d" % proxy.sockets[0].getsockname()[1]
        for name in PROXY_VARS:
            monkeypatch.setenv(name, url)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        try:
            async def body(base):
                ours = await fs.post_control("stop", FAKE_TOKEN, base=base, timeout=1.0)
                ours_hits, ours_seen = len(proxy_hits), [r["path"] for r in seen]
                async with ClientSession(trust_env=True) as session:
                    async with session.post(base + "/stop", data=b"{}",
                                            headers={"Content-Type": "application/json"}) as resp:
                        contrast = resp.status
                return ours, ours_hits, ours_seen, contrast
            return await serving(fake_helper(seen), body)
        finally:
            proxy.close()
            await proxy.wait_closed()

    ours, ours_hits, ours_seen, contrast = asyncio.run(go())
    assert ours is None and ours_hits == 0
    assert ours_seen == ["/stop"]                          # 我们的那一个直接到了假闪购助手
    if not proxy_hits:
        pytest.skip("这台机器上 trust_env=True 也没走环境变量里的代理，这条对照测不出东西")
    assert contrast == 502


# ---- 动作白名单、地址写死 ------------------------------------------------------------------------

def test_the_address_is_hard_wired_to_the_local_helper():
    assert REAL_CONTROL_BASE == "http://127.0.0.1:9334"
    assert fs.CONTROL_ACTIONS == ("start", "stop", "resume")


@pytest.mark.parametrize("action", ["START", "Stop", "../status", "status", "stop?x=1", "stop/", "/stop", "",
                                    " stop", "stop\n", None, 1, ["stop"], ("stop",), {"stop": 1}, b"stop"])
def test_only_the_three_actions_make_a_url_and_nothing_is_sent_otherwise(action):
    with pytest.raises(ValueError) as caught:
        fs.control_url(action)
    assert str(caught.value) == "not a control action"
    seen = []

    async def body(base):
        with pytest.raises(ValueError):
            await fs.post_control(action, FAKE_TOKEN, base=base)
        return True
    assert asyncio.run(serving(fake_helper(seen), body)) is True and seen == []


@pytest.mark.parametrize("token", [None, "", "7e57", FAKE_TOKEN + "0", FAKE_TOKEN[:-1] + "g", " " + FAKE_TOKEN,
                                   FAKE_TOKEN + "\n", b"7e57" * 16, 12])
def test_post_control_refuses_a_token_that_is_not_64_hex_without_sending(token):
    seen = []

    async def body(base):
        return await fs.post_control("stop", token, base=base)
    assert asyncio.run(serving(fake_helper(seen), body)) == "no_token" and seen == []


# ---- 读密钥 ------------------------------------------------------------------------------------

def token_file(tmp_path, content, name="control-token"):
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("ascii"))
    return path


@pytest.mark.parametrize("content", [FAKE_TOKEN, FAKE_TOKEN + "\n", FAKE_TOKEN + "\r\n", "  " + FAKE_TOKEN + " \n",
                                     FAKE_TOKEN.upper()])
def test_a_valid_token_file_is_read(tmp_path, content):
    assert fs.read_control_token(token_file(tmp_path, content)) == (content.strip(), None)


@pytest.mark.parametrize("content", [
    "", "\n", FAKE_TOKEN[:-1], FAKE_TOKEN + "0", FAKE_TOKEN[:-1] + "g", FAKE_TOKEN[:32] + " " + FAKE_TOKEN[32:],
    FAKE_TOKEN + "\n" + OTHER_TOKEN, "Bearer " + FAKE_TOKEN, b"\xef\xbb\xbf" + FAKE_TOKEN.encode(),
    FAKE_TOKEN.encode() + b"\x00", "０" * 64, "z" * 64, "7e57" * 256,
])
def test_anything_but_exactly_64_hex_is_format(tmp_path, content):
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    assert fs.read_control_token(token_file(tmp_path, raw)) == (None, "format")


class SpyOs:
    """只换 app/flash_sale.py 看到的 os（monkeypatch fs.os 这个名字，不动全局的 os 模块）：记下 open 的标志位和
    read 要了多少字节，其余照转。"""

    def __init__(self):
        self.open_flags, self.asked = [], []

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, *args):
        self.open_flags.append(flags)
        return os.open(path, flags, *args)

    def read(self, fd, n):
        self.asked.append(n)
        return os.read(fd, n)


def test_a_file_of_1025_bytes_or_more_is_too_long_and_only_1025_bytes_are_read(tmp_path, monkeypatch):
    """最多读 1025 字节：读满了就是太长，后面的不读（文件可能很大，也可能被换成一直往外吐的东西）。"""
    spy = SpyOs()
    monkeypatch.setattr(fs, "os", spy)
    assert fs.read_control_token(token_file(tmp_path, "7" * fs.TOKEN_READ_MAX)) == (None, "too_long")
    assert fs.read_control_token(token_file(tmp_path, "7" * (10 * 1024 * 1024), "big")) == (None, "too_long")
    assert fs.read_control_token(token_file(tmp_path, "7" * (fs.TOKEN_READ_MAX - 1), "just")) == (None, "format")
    assert fs.TOKEN_READ_MAX == 1025 and spy.asked and max(spy.asked) <= 1025


def test_a_missing_file_is_missing(tmp_path):
    assert fs.read_control_token(tmp_path / "nope") == (None, "missing")
    assert fs.read_control_token(tmp_path / "no-dir" / "control-token") == (None, "missing")
    plain = token_file(tmp_path, FAKE_TOKEN, "plain")
    assert fs.read_control_token(plain / "control-token") == (None, "missing")     # 上一级是文件：ENOTDIR


def test_a_symlink_is_not_followed(tmp_path, monkeypatch):
    """密钥文件本身是符号链接：不跟过去读（O_NOFOLLOW），哪怕它指向的是一个合法的密钥文件。"""
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("这个平台没有 O_NOFOLLOW（闪购只在 macOS 上有）")
    real = token_file(tmp_path, FAKE_TOKEN, "real")
    link = tmp_path / "control-token"
    link.symlink_to(real)
    spy = SpyOs()
    monkeypatch.setattr(fs, "os", spy)
    assert fs.read_control_token(link) == (None, "symlink")
    assert spy.open_flags and spy.open_flags[0] & os.O_NOFOLLOW
    assert fs.read_control_token(real) == (FAKE_TOKEN, None)


def test_a_directory_is_not_a_file(tmp_path):
    folder = tmp_path / "control-token"
    folder.mkdir()
    assert fs.read_control_token(folder) == (None, "not_file")


def test_a_fifo_is_not_a_file_and_does_not_block(tmp_path):
    """没人写的管道：不带 O_NONBLOCK 打开会一直卡着（卡的是事件循环）。打开后 fstat 看出不是普通文件。"""
    if not hasattr(os, "mkfifo"):
        pytest.skip("这个平台没有 mkfifo")
    fifo = tmp_path / "control-token"
    os.mkfifo(fifo)
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)
    assert fs.read_control_token(fifo) == (None, "not_file")


def test_an_unreadable_file_is_unreadable(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root 不受权限位限制")
    path = token_file(tmp_path, FAKE_TOKEN)
    path.chmod(0)
    try:
        assert fs.read_control_token(path) == (None, "unreadable")
    finally:
        path.chmod(0o600)


def test_no_home_means_no_token(monkeypatch):
    monkeypatch.setattr(fs, "control_token_path", lambda home=None: None)
    assert fs.read_control_token() == (None, "no_home")


def test_the_reason_codes_never_carry_the_file_content(tmp_path):
    for content in (FAKE_TOKEN + "x", FAKE_TOKEN * 20, FAKE_TOKEN + "\n" + FAKE_TOKEN):
        token, why = fs.read_control_token(token_file(tmp_path, content))
        assert token is None and why in ("format", "too_long") and no_leak(why)


def test_the_token_lives_in_the_helpers_application_support_folder(tmp_path):
    assert REAL_CONTROL_TOKEN_PATH(home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "FlashSaleHelper" / "control-token")


# ---- tests/conftest.py 的网：控制地址和密钥位置都换掉了 -------------------------------------------------

def test_tests_never_reach_the_real_control_port_or_the_real_token():
    """conftest 的 autouse 夹具：控制地址里没有 9334，密钥位置是一个不存在的路径（不是本机真的那个）。
    漏传地址、漏传路径的调用也碰不到真的：默认值都是在调用那一刻才取。"""
    assert ":9334" not in fs.CONTROL_BASE and ":9334" not in fs.STATUS_URL
    assert fs.control_url("stop") == fs.CONTROL_BASE + "/stop"
    path = fs.control_token_path()
    assert isinstance(path, Path) and not path.exists()
    assert path != REAL_CONTROL_TOKEN_PATH() and "FlashSaleHelper" not in str(path)
    assert fs.read_control_token() == (None, "missing")
    assert asyncio.run(fs.post_control("stop", FAKE_TOKEN, timeout=1.0)) == "unreachable"
