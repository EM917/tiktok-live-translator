"""测试总网本身（tests/socket_net.py）：该拦的拦下、该放的放行。

反向测试：故意去连 127.0.0.1:9334（闪购助手的状态端口），网要当场拦下、记下来。每条都先确认网装着、
是 strict，才去碰会被拦的地址：网要是坏了，不能让这几条自己变成真的去连。拦下发生在系统调用之前，
连接本身从没发出去。这几条用 socket_net_hits 换一张自己的记录表，记下的不算违例。
"""
import asyncio
import socket
import sys

import pytest

from tests import socket_net as N


@pytest.fixture
def strict_net(socket_net_hits):
    assert N.installed() and N.MODE == "strict", "测试总网没装上或不是 strict：下面这些会真的连出去，先停"
    return socket_net_hits


def test_connecting_to_the_flash_sale_helper_port_is_caught(strict_net):
    with socket.socket() as sock:
        sock.settimeout(1)
        with pytest.raises(ConnectionRefusedError, match=N.MARK):
            sock.connect(("127.0.0.1", 9334))
    assert [h[1:] for h in strict_net] == [("connect", "127.0.0.1:9334", "live")]


def test_connect_ex_is_caught_the_same_way(strict_net):
    """connect_ex 和 connect 报的是同一个审计事件：拦下时也是抛，不是返回错误码。"""
    with socket.socket() as sock:
        sock.settimeout(1)
        with pytest.raises(ConnectionRefusedError, match=N.MARK):
            sock.connect_ex(("localhost", 8765))
    assert [h[1:] for h in strict_net] == [("connect", "localhost:8765", "live")]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows 上 asyncio 走 IOCP 的 ConnectEx，不报 socket.connect，网看不到 aiohttp 的连接")
def test_the_program_sees_a_blocked_helper_as_unreachable(strict_net):
    """被拦的连接在程序看来就是「连不上」：闪购状态照契约报 unreachable，什么都没发出去。"""
    from app import flash_sale

    got = asyncio.run(flash_sale.fetch_status("http://127.0.0.1:9334/status", timeout=1.0))
    assert got == ("unreachable", None)
    assert ("connect", "127.0.0.1:9334", "live") in [h[1:] for h in strict_net]


def test_a_live_port_is_never_the_tests_own_server(monkeypatch):
    """本进程哪怕自己绑过 8765、登记过 9334，连它们也不放行：落到的可能是用户的同传、闪购助手。"""
    monkeypatch.setattr(N, "own_ports", lambda: {8765, 8790})
    with N.allowing(9334):
        assert N.judge("127.0.0.1", 9334) == "live"
    assert N.judge("127.0.0.1", 8765) == "live"
    assert N.judge("127.0.0.1", 8790) == "own"
    for port in (8765, 8774, 9333, 9334, 11434):
        assert N.judge("::1", port) == "live"


def test_leaving_the_machine_is_caught(strict_net):
    """TEST-NET-1（RFC 5737）不指向任何真实主机；TCP 连接和 UDP 都拦。"""
    with socket.socket() as sock:
        sock.settimeout(0.5)
        with pytest.raises(ConnectionRefusedError, match=N.MARK):
            sock.connect(("192.0.2.1", 80))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        with pytest.raises(ConnectionRefusedError, match=N.MARK):
            udp.sendto(b"x", ("192.0.2.1", 9))
    assert [h[1:] for h in strict_net] == [("connect", "192.0.2.1:80", "outside"),
                                           ("sendto", "192.0.2.1:9", "outside")]


def test_looking_up_a_name_is_caught(strict_net):
    """查域名本身就会发 DNS 请求。.invalid 保留给「一定查不到」（RFC 6761）。"""
    with pytest.raises(socket.gaierror, match=N.MARK):
        socket.getaddrinfo("tlt-test.invalid", 443)
    assert [h[1:] for h in strict_net] == [("lookup", "tlt-test.invalid", "lookup")]


def test_names_that_stay_on_the_machine_are_not_lookups():
    assert N.is_local_name("localhost") and N.is_local_name("127.0.0.1") and N.is_local_name("::1")
    assert N.is_local_name(None) and N.is_local_name(b"") and N.is_local_name("app.localhost")
    assert not N.is_local_name("example.com")
    assert N.is_loopback("127.0.0.2") and N.is_loopback("::ffff:127.0.0.1") and N.is_loopback("0.0.0.0")
    assert not N.is_loopback("192.168.1.2") and not N.is_loopback("example.com")


def test_the_sentinel_is_let_through_and_really_refused(strict_net):
    """127.0.0.1:9 放行：拒绝它的是系统（没人在听），不是这张网。"""
    with socket.socket() as sock:
        sock.settimeout(1)
        with pytest.raises(OSError) as caught:
            sock.connect(("127.0.0.1", N.SENTINEL_PORT))
    assert N.MARK not in str(caught.value)
    assert strict_net == []


def test_a_server_the_test_started_is_let_through(strict_net):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    assert strict_net == []


def test_a_port_the_test_closed_is_not_its_server_any_more(strict_net):
    """绑过又关掉的端口不算自己的：那时谁在听已经说不准了。要测「连不上」就连哨兵。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert port not in N.LIVE_PORTS
    with socket.socket() as sock:
        sock.settimeout(1)
        with pytest.raises(ConnectionRefusedError, match=N.MARK):
            sock.connect(("127.0.0.1", port))
    assert [h[1:] for h in strict_net] == [("connect", "127.0.0.1:{}".format(port), "local")]


def test_a_registered_port_is_let_through_only_inside_the_with(strict_net):
    """G10 的 Chrome 调试端口：从 Chrome 的输出里读到以后登记，出了 with 就撤掉。用一个本进程起的服务器当
    「子进程的端口」：登记的那一层先于「自己的服务器」判，所以判定是 registered。"""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]
        with N.allowing(port):
            assert N.judge("127.0.0.1", port) == "registered"
            with N.allowing(port):
                pass
            assert N.judge("127.0.0.1", port) == "registered"     # 里层撤掉，外层还在
        assert N.judge("127.0.0.1", port) == "own"
    assert N.judge("127.0.0.1", port) == "local"
    assert strict_net == []


def test_a_blocked_connection_is_charged_to_the_test_that_made_it(strict_net, request):
    """被拦的连接记在当时那个用例名下（用例自己吞掉了异常也一样）；conftest 的 _no_connection_leaves_the_test
    在用例结束时拿它判失败，失败说明里写着去处。"""
    try:
        socket.create_connection(("127.0.0.1", 9334), timeout=1).close()
    except OSError:
        pass
    mine = N.hits_of(request.node.nodeid, strict_net)
    assert [h[1:] for h in mine] == [("connect", "127.0.0.1:9334", "live")]
    assert N.hits_of("tests/test_other.py::test_x", strict_net) == []
    text = N.failure_text(mine)
    assert "往外连了 1 次" in text and "connect 127.0.0.1:9334" in text
