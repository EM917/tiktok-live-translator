"""直播闸 tools/livecheck.py：先查拉流、再问闪购助手、最后查同传程序，哪个先中就停；只读。

不跑真的 pgrep（Windows 跑器上没有），也不连真的 9334：running、helper_running 换成假的，问状态的那几条
对面是本机临时端口上的假服务器。最后一条让它照原样去问 9334，看测试总网（tests/socket_net.py）当场拦下。
"""
import json
import threading
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests import socket_net as N
from tools import livecheck

SOURCE = Path(livecheck.__file__).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_system_proxy(monkeypatch):
    """系统、环境里设的代理不能把问状态的请求改道：urlopen 缓存的那个 opener 也换掉。"""
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    monkeypatch.setattr(urllib.request, "_opener", None)


def fake_ps(*patterns):
    return lambda pattern: pattern in patterns


@pytest.mark.parametrize("procs,helper,line,code", [
    (("tiktokcdn",), True, "live stream: 同传程序在拉直播流", 0),
    ((), True, "live stream: 闪购助手正在运行", 0),
    ((r"main\.py", "tiktok-live-translator"), False, "app main: 同传程序开着", 0),
    ((r"main\.py",), False, "idle", 1),
    ((), False, "idle", 1),
])
def test_the_first_match_decides(monkeypatch, procs, helper, line, code):
    asked = []
    monkeypatch.setattr(livecheck, "running", fake_ps(*procs))
    monkeypatch.setattr(livecheck, "helper_running", lambda: asked.append(1) or helper)
    assert livecheck.verdict() == (line, code)
    assert asked == ([] if "tiktokcdn" in procs else [1])     # 在拉流就不再去问闪购助手


@pytest.mark.parametrize("procs,helper,skip", [
    (("tiktokcdn",), False, True), ((), True, True),
    ((r"main\.py", "tiktok-live-translator"), False, True), ((), False, False),
])
def test_main_prints_what_the_gate_greps_for(monkeypatch, capsys, procs, helper, skip):
    """CLAUDE.md 里那条命令：输出里有 live stream 或 app main 就打 GATE-SKIP、不跑。"""
    monkeypatch.setattr(livecheck, "running", fake_ps(*procs))
    monkeypatch.setattr(livecheck, "helper_running", lambda: helper)
    code = livecheck.main()
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert (("live stream" in out) or ("app main" in out)) is skip
    assert code == (0 if skip else 1)


@contextmanager
def status_server(body):
    """本机临时端口上的假 /status。"""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/status" else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_address[1] not in N.LIVE_PORTS
        yield "http://127.0.0.1:{}/status".format(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("body,expect", [
    (json.dumps({"running": True}).encode(), True),
    (json.dumps({"running": False}).encode(), False),
    (json.dumps({"running": "true"}).encode(), False),     # 只认 JSON 的 true
    (b"[]", False),
    (b"not json", False),
])
def test_helper_running_only_believes_a_json_true(monkeypatch, body, expect):
    with status_server(body) as url:
        monkeypatch.setattr(livecheck, "STATUS_URL", url)
        assert livecheck.helper_running() is expect


def test_nobody_listening_means_not_running(monkeypatch):
    monkeypatch.setattr(livecheck, "STATUS_URL", "http://127.0.0.1:{}/status".format(N.SENTINEL_PORT))
    assert livecheck.helper_running() is False


def test_it_only_reads():
    """只 GET 9334 的 /status：不发控制请求、不读密钥（CLAUDE.md 第九条写的唯一例外就是这一次 GET）。"""
    assert livecheck.STATUS_URL == "http://127.0.0.1:9334/status"
    for word in ("POST", "data=", "/stop", "/start", "/resume", "control-token", "Authorization"):
        assert word not in SOURCE, word


def test_asking_the_real_helper_from_a_test_is_caught(socket_net_hits):
    """反向测试：测试里照原样跑 helper_running()，去的是真的 127.0.0.1:9334，测试总网要当场拦下，它照「没在
    运行」处理。网要是没装上，先停：不能让这一条自己变成真的去问。"""
    assert N.installed() and N.MODE == "strict"
    assert livecheck.helper_running() is False
    assert [h[1:] for h in socket_net_hits] == [("connect", "127.0.0.1:9334", "live")]
