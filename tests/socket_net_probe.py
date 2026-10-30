"""测试总网的探针：tests/test_socket_net.py 的端到端用例在子进程里点名跑它，看 conftest 是不是真的判失败。
不是普通测试：文件名不以 test_ 开头，平时不收集，只有命令行点名时才跑。

被拦的那次连的是本进程绑过又关掉的端口：网要是没装上、真连了出去，落到的也只是本机一个没人听的端口，
碰不到用户直播开着的那些（LIVE_PORTS）。
"""
import os
import socket

from tests.socket_net import LIVE_PORTS, SENTINEL_PORT


def _closed_port():
    while True:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in LIVE_PORTS:
            return port


def _connect_and_swallow(port):
    """连一次，把异常吞掉：被测代码常这么写（「连不上」当正常分支），网照样要判失败。"""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        pass


if os.environ.get("TLT_SOCKET_NET_PROBE") == "import":
    _connect_and_swallow(_closed_port())        # 导入、收集阶段：不在任何用例里


def test_connects_and_swallows_the_error():
    _connect_and_swallow(_closed_port())


def test_only_the_sentinel():
    _connect_and_swallow(SENTINEL_PORT)
