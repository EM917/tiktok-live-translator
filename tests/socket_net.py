"""测试总网（旁线 A）：看住测试进程发出的每一个连接。

开发机上同时开着用户直播正在用的程序：同传（127.0.0.1:8765、手机同看 *:8766，端口被占时往后顺延，
最多到 8774）、闪购助手（9333、9334：9334 的 /status 返回真实商品，控制接口会真的停掉直播里正在续开的
闪购）、Ollama（11434）。测试连上其中任何一个，就是在动直播。tests/conftest.py 已经一处一处把知道的地址
换成了没人听的 127.0.0.1:9，可那靠每处代码都记得换；这张网是兜底，看的是连接本身。

怎么看：Python 的审计钩子（sys.addaudithook，装上就卸不掉，整个测试进程都在）。
- socket.connect：connect、connect_ex 都报这一个；asyncio、aiohttp、urllib、http.client 最后都走到它；
- socket.getaddrinfo、socket.gethostbyname（含 _ex）、socket.gethostbyaddr、socket.getnameinfo：查域名、
  反查地址，会发 DNS 请求；
- socket.sendto、socket.sendmsg：不先 connect、直接往外发的 UDP；
- socket.bind：认出本进程自己起的服务器。测试的假服务器绑 127.0.0.1:0、端口由系统挑，绑的那一刻还不知道
  端口；记下这个套接字，连的时候再问它绑到了哪个端口。
看不到子进程（Chrome、node、另起的 python）里的连接。

A1（现在）：只记录、不拦。测试结束时在终端摘要里列出清单：哪些连接收紧以后会被拦、各来自哪些用例。
"""
import ipaddress
import os
import socket
import sys
import weakref

# 故意连「连不上」用的哨兵：127.0.0.1 的 9 号端口（discard，macOS / Linux / Windows 上默认都没开）
SENTINEL_PORT = 9
# 用户直播时开着的端口：同传 8765–8774、闪购助手 9333 和 9334、Ollama 11434。本进程哪怕自己也绑过同一个
# 端口，连它也不算连自己的服务器：macOS 上测试绑 0.0.0.0:8765 可能成功，可 127.0.0.1:8765 上更具体的那个
# 监听是用户的同传，连过去落到的是它
LIVE_PORTS = frozenset(range(8765, 8775)) | frozenset((9333, 9334, 11434))

# 判定：前三种收紧以后照样放行，后面的会被拦
ALLOWED = ("sentinel", "own", "unix")
LABELS = {
    "sentinel": "127.0.0.1:9 哨兵",
    "own": "本进程自己起的服务器（随机端口）",
    "unix": "Unix 套接字（不走网络）",
    "live": "本机上用户直播时开着的端口（8765–8774、9333、9334、11434）",
    "local": "本机上不是本进程起的端口",
    "outside": "本机以外",
    "lookup": "查域名、反查地址（会发 DNS 请求）",
}

# (PYTEST_CURRENT_TEST, 种类, 去处, 判定)。种类：connect、udp、sendto、lookup
_STATE = {"hits": []}
# 本进程 bind 过的套接字（弱引用：关掉、回收了就自动掉出去）
_BOUND = weakref.WeakValueDictionary()
_EVENTS = frozenset(("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
                     "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                     "socket.getnameinfo"))
_HOSTNAME = socket.gethostname()
_INSTALLED = []


def _text(host):
    if isinstance(host, (bytes, bytearray)):
        return bytes(host).decode("ascii", "replace")
    return "" if host is None else str(host)


def is_loopback(host):
    """连这个地址会不会留在本机：localhost、127/8、::1，以及 0.0.0.0 / ::（连它们落到的也是本机）。"""
    host = _text(host).split("%", 1)[0].strip().rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def is_local_name(host):
    """查这个名字要不要发 DNS 请求：空的、localhost、字面 IP 都不用（字面 IP 去哪，看后面的 connect）。"""
    host = _text(host).split("%", 1)[0].strip().rstrip(".").lower()
    if host in ("", "localhost") or host.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def own_ports():
    """本进程里还开着、绑过端口的套接字现在各占着哪个端口。"""
    ports = set()
    for ref in _BOUND.valuerefs():
        sock = ref()
        if sock is None:
            continue
        try:
            name = sock.getsockname()
        except (OSError, ValueError, TypeError):      # 已经关了
            continue
        if isinstance(name, tuple) and len(name) >= 2 and isinstance(name[1], int) and name[1]:
            ports.add(name[1])
    return ports


def judge(host, port):
    """连 (host, port) 算哪一种：sentinel、own、live、local、outside（见 LABELS）。"""
    if not is_loopback(host):
        return "outside"
    if port in LIVE_PORTS:
        return "live"
    if port == SENTINEL_PORT:
        return "sentinel"
    if port in own_ports():
        return "own"
    return "local"


def _where(host, port):
    host = _text(host)
    if host == _HOSTNAME:
        host = "<本机主机名>"          # 开发机的主机名不进日志、不进 PR
    if ":" in host:
        host = "[{}]".format(host)
    return host if port is None else "{}:{}".format(host, port)


def _inet(address):
    """(host, port)；不是 IPv4 / IPv6 地址就 None。"""
    if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[1], int):
        return address[0], address[1]
    return None


def _classify(event, args):
    """这次审计事件要记的 (种类, 去处, 判定)；不用记就 None。"""
    if event == "socket.bind":
        try:
            _BOUND[id(args[0])] = args[0]
        except TypeError:                    # 底层的 _socket.socket 不支持弱引用
            pass
        return None
    if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
        sock, address = args[0], args[1]
        if address is None:                  # sendmsg 发给已经 connect 过的去处：connect 时记过了
            return None
        inet = _inet(address)
        if inet is None:
            if isinstance(address, (str, bytes)):
                return "connect", "unix:" + _text(address), "unix"
            return event.split(".", 1)[1], repr(address)[:120], "outside"
        udp = getattr(sock, "type", None) == socket.SOCK_DGRAM
        kind = "sendto" if event != "socket.connect" else ("udp" if udp else "connect")
        return kind, _where(*inet), judge(*inet)
    if event == "socket.getnameinfo":
        inet = _inet(args[0])
        if inet is None or is_loopback(inet[0]):
            return None
        return "lookup", "反查 " + _where(inet[0], None), "lookup"
    host = args[0]
    if event == "socket.gethostbyaddr":      # 反查：本机地址查 hosts 就够，别的要问 DNS
        return None if is_loopback(host) else ("lookup", "反查 " + _where(host, None), "lookup")
    if is_local_name(host):                  # getaddrinfo、gethostbyname（含 _ex）
        return None
    return "lookup", _where(host, None), "lookup"


def _hook(event, args):
    if event not in _EVENTS:
        return
    try:
        found = _classify(event, args)
    except Exception:
        # 审计钩子里抛出的异常会落到被观察的那次调用上：记录这一步绝不能改变被测代码的行为
        return
    if found is not None:
        _STATE["hits"].append((os.environ.get("PYTEST_CURRENT_TEST", ""),) + found)


def install():
    """装上审计钩子（一个进程只装一次；装上就卸不掉）。"""
    if not _INSTALLED:
        sys.addaudithook(_hook)
        _INSTALLED.append(True)


def _test_id(current):
    """PYTEST_CURRENT_TEST 形如 "tests/test_x.py::test_y[a] (call)"：去掉阶段，剩下用例 id。"""
    return current.rsplit(" (", 1)[0] if current.endswith(")") else current


def summary(tr):
    """测试结束时把记下的连接列出来：放行的只报次数，会被拦的逐个去处列出来自哪些用例。"""
    from collections import Counter, defaultdict

    hits = list(_STATE["hits"])
    if not hits:
        return
    tr.write_sep("-", "测试总网（A1，只记录、不拦）：测试进程发出的连接")
    allowed = Counter(verdict for _t, _k, _w, verdict in hits if verdict in ALLOWED)
    if allowed:
        tr.write_line("收紧以后照样放行：" + "；".join(
            "{} {} 次".format(LABELS[v], n) for v, n in sorted(allowed.items())))
    blocked = defaultdict(list)
    for test, kind, where, verdict in hits:
        if verdict not in ALLOWED:
            blocked[(verdict, kind, where)].append(_test_id(test) or "（不在用例里）")
    if not blocked:
        tr.write_line("收紧以后会被拦的：没有")
        return
    tr.write_line("收紧以后会被拦的：{} 个去处，共 {} 次".format(
        len(blocked), sum(len(v) for v in blocked.values())))
    for (verdict, kind, where), tests in sorted(blocked.items()):
        names = Counter(tests)
        tr.write_line("  {} {}  ×{}  {}，{} 个用例".format(kind, where, len(tests), LABELS[verdict], len(names)))
        for name, n in sorted(names.items()):
            tr.write_line("    {}{}".format(name, "" if n == 1 else "  ×{}".format(n)))
