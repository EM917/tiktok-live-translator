"""取闪购助手的 /status（app/flash_sale.fetch_status）：真的走 HTTP，但对面是本机临时端口上的假服务器。

绝不连 127.0.0.1:9334：开发机上可能真开着闪购助手，那里返回的是真实商品。假服务器照契约检查 Host
（不是 127.0.0.1:<端口> 或 localhost:<端口> 就回 403），这样「200 回来了」本身就钉住了我们发的 Host 是对的。
"""
import asyncio
import json
import socket
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from app import flash_sale as fs

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "flash_sale"


def sample_bytes(name):
    return (FIXTURES / (name + ".json")).read_bytes()


def fake_status_app(handler):
    """契约里的 /status：Host 不对回 403；/status 以外 404；GET、HEAD 以外 405（aiohttp 路由自己会回）。"""
    async def guarded(request):
        port = request.transport.get_extra_info("sockname")[1]
        if request.headers.get("Host") not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
            return web.Response(status=403, text="bad host")
        return await handler(request)

    app = web.Application()
    app.router.add_get("/status", guarded)
    return app


async def serve(handler, body, **fetch_kwargs):
    server = TestServer(fake_status_app(handler), host="127.0.0.1")
    await server.start_server()
    try:
        url = "http://127.0.0.1:%d/status" % server.port
        assert server.port != 9334
        return await body(url, fetch_kwargs)
    finally:
        await server.close()


def fetch_from(handler, **kwargs):
    async def body(url, kw):
        return await fs.fetch_status(url, **kw)
    return asyncio.run(serve(handler, body, **kwargs))


def json_handler(payload_bytes, status=200):
    async def handler(request):
        return web.Response(body=payload_bytes, status=status, content_type="application/json")
    return handler


@pytest.mark.parametrize("name", ["idle", "running", "awaiting"])
def test_the_three_contract_samples_come_back_ok(name):
    kind, value = fetch_from(json_handler(sample_bytes(name)))
    assert kind == "ok"
    assert value == json.loads(sample_bytes(name))


def test_the_fake_server_really_checks_the_host_header():
    """对照：Host 不对时假服务器确实回 403，所以上面那几条 200 说明我们发的 Host 是对的。"""
    async def body(url, _kw):
        async with ClientSession() as session:
            async with session.get(url, headers={"Host": "evil.example:9334"}) as resp:
                return resp.status
    assert asyncio.run(serve(json_handler(b"{}"), body)) == 403


@pytest.mark.parametrize("status", [404, 403, 500, 503])
def test_http_errors_are_bad_responses(status):
    assert fetch_from(json_handler(b"{}", status=status)) == ("bad_response", "http_%d" % status)


def test_a_redirect_is_not_followed():
    async def handler(request):
        raise web.HTTPFound("/status?next=1")
    assert fetch_from(handler) == ("bad_response", "http_302")


@pytest.mark.parametrize("body", [b"not json", b"{\"a\": ", b"\xff\xfe\x00garbage", b"", b"[" * 100000])
def test_unreadable_bodies_are_bad_responses(body):
    assert fetch_from(json_handler(body)) == ("bad_response", "json")


def test_a_body_over_the_limit_is_too_large():
    big = json.dumps({"pad": "x" * 5000}).encode()
    assert fetch_from(json_handler(big), limit=1000) == ("bad_response", "too_large")


def test_the_default_limit_holds_three_hundred_long_products_but_not_much_more():
    product = {"product_id": "1000000000000000001", "title": "长" * 200, "shop": "example_shop",
               "state": "live", "round": 2, "round_ends_at": "2026-10-27T21:51:01.000Z",
               "next_round_at": None, "stock_left": 10, "stock_total": 10, "renewals": 0, "enabled": True}
    doc = json.loads(sample_bytes("running"))
    doc["products"] = [product] * fs.MAX_PRODUCTS
    body = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    assert len(body) < fs.MAX_BYTES
    kind, value = fetch_from(json_handler(body))
    assert kind == "ok" and len(value["products"]) == fs.MAX_PRODUCTS
    huge = b'{"pad": "' + b"x" * fs.MAX_BYTES + b'"}'
    assert fetch_from(json_handler(huge)) == ("bad_response", "too_large")


def test_a_non_object_json_is_returned_as_is_for_the_tracker_to_reject():
    kind, value = fetch_from(json_handler(b"[1, 2]"))
    assert (kind, value) == ("ok", [1, 2])
    tracker = fs.StatusTracker()
    assert tracker.update(kind, value, 0.0, 0.0)["conn"] == "bad_response" and tracker.why == "not_object"


def test_a_slow_answer_counts_as_unreachable():
    async def handler(request):
        await asyncio.sleep(2.0)
        return web.Response(body=b"{}", content_type="application/json")
    assert fetch_from(handler, timeout=0.2) == ("unreachable", None)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_port_nobody_listens_on_is_unreachable():
    port = free_port()
    assert port != 9334
    got = asyncio.run(fs.fetch_status("http://127.0.0.1:%d/status" % port, timeout=1.0))
    assert got == ("unreachable", None)


def test_a_server_that_hangs_up_without_answering_is_unreachable():
    async def scenario():
        async def hang_up(reader, writer):
            writer.close()
        server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await fs.fetch_status("http://127.0.0.1:%d/status" % port, timeout=1.0)
        finally:
            server.close()
            await server.wait_closed()
    assert asyncio.run(scenario()) == ("unreachable", None)


def test_cancelling_a_fetch_propagates():
    async def handler(request):
        await asyncio.sleep(5.0)
        return web.Response(body=b"{}")

    async def body(url, _kw):
        task = asyncio.ensure_future(fs.fetch_status(url, timeout=10.0))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return True
    assert asyncio.run(serve(handler, body)) is True


def test_the_default_address_in_tests_is_not_the_real_helper():
    """tests/conftest.py 把默认地址换成了没人听的端口：漏传 url 的调用也到不了 9334。"""
    assert fs.STATUS_URL != "http://127.0.0.1:9334/status"
    assert asyncio.run(fs.fetch_status(timeout=1.0)) == ("unreachable", None)
