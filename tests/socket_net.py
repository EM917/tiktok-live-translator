"""测试总网（旁线 A）：看住测试进程发出的每一个连接。

开发机上同时开着用户直播正在用的程序：同传（127.0.0.1:8765、手机同看 *:8766，端口被占时往后顺延，
最多到 8774）、闪购助手（9333、9334：9334 的 /status 返回真实商品，控制接口会真的停掉直播里正在续开的
闪购）、Ollama（11434）。测试连上其中任何一个，就是在动直播。tests/conftest.py 已经一处一处把知道的地址
换成了没人听的 127.0.0.1:9，可那靠每处代码都记得换；这张网是兜底，看的是连接本身。

怎么看：Python 的审计钩子（sys.addaudithook，装上就卸不掉，整个测试进程都在）。
- socket.connect：connect、connect_ex 都报这一个（所以拦下时 connect_ex 也是抛异常，不是返回错误码）；
  asyncio、aiohttp、urllib、http.client 最后都走到它；
- socket.getaddrinfo、socket.gethostbyname（含 _ex）、socket.gethostbyaddr、socket.getnameinfo：查域名、
  反查地址，会发 DNS 请求；
- socket.sendto、socket.sendmsg：不先 connect、直接往外发的 UDP；
- socket.bind：认出本进程自己起的服务器。测试的假服务器绑 127.0.0.1:0、端口由系统挑，绑的那一刻还不知道
  端口；记下这个套接字，连的时候再问它绑到了哪个端口。
看不到的：子进程（Chrome、node、另起的 python）里的连接；Windows 上 asyncio 的连接（IOCP 事件循环用
_overlapped 的 ConnectEx，不报 socket.connect，aiohttp 在 Windows 上也就看不到；同步的 socket、urllib 看得到）。
开发机和直播用的电脑是 macOS，那里 asyncio 走 sock.connect，看得到。

放行的只有这几种（ALLOWED）：
- 127.0.0.1:9 哨兵：故意测「连不上」用它（9 是 discard，三个系统上默认都没开）；
- 本进程自己起、连的时候还开着的服务器（随机端口）；
- 用 allowing(port) 登记过的本机端口：子进程自己挑的端口，比如 G10 的无头 Chrome 的调试端口，从 Chrome
  的输出里读到以后登记（tests/test_i18n_dom.py 的 run_chrome_cdp）、测试在子进程里起的服务器
  （tests/test_viewer_hub.py）；还有测试刚关掉的自己的端口，要测的正是「关掉以后连不上」（tests/test_viewer_e2e.py）；
- Unix 套接字（不走网络）。
LIVE_PORTS 里的端口哪怕本进程自己绑过、登记过，也不放行。

A1 只记录、不拦，列出了全部连接（PR 说明里有那份清单）。A2（MODE = "strict"，现在）：别的连接一律当场拦下：
连接、发 UDP 抛 ConnectionRefusedError，查域名抛 socket.gaierror，被测代码照「连不上」「查不到」处理，
不会真的发出去；同时记下来，用例结束时判这个用例失败（tests/conftest.py 的 _no_connection_leaves_the_test），
不在任何用例里的（收集、导入阶段）让整次运行失败（pytest_sessionfinish）。终端摘要列出全部记下的。
专测这张网本身的用例用 conftest 的 socket_net_hits 夹具换一张自己的记录表。
"""
import contextlib
import errno
import ipaddress
import os
import socket
import sys
import weakref
from collections import Counter

# record：只记录（A1）；strict：拦下并判失败（A2）
MODE = "strict"
# 故意连「连不上」用的哨兵：127.0.0.1 的 9 号端口（discard，macOS / Linux / Windows 上默认都没开）
SENTINEL_PORT = 9
# 用户直播时开着的端口：同传 8765–8774、闪购助手 9333 和 9334、Ollama 11434。本进程哪怕自己也绑过同一个
# 端口，连它也不算连自己的服务器：macOS 上测试绑 0.0.0.0:8765 可能成功，可 127.0.0.1:8765 上更具体的那个
# 监听是用户的同传，连过去落到的是它
LIVE_PORTS = frozenset(range(8765, 8775)) | frozenset((9333, 9334, 11434))

# 判定：前四种放行，后面的拦下
ALLOWED = ("sentinel", "own", "registered", "unix")
LABELS = {
    "sentinel": "127.0.0.1:9 哨兵",
    "own": "本进程自己起的服务器（随机端口）",
    "registered": "登记过的子进程端口（G10 的 Chrome 调试端口）",
    "unix": "Unix 套接字（不走网络）",
    "live": "本机上用户直播时开着的端口（8765–8774、9333、9334、11434）",
    "local": "本机上不是本进程起的端口",
    "outside": "本机以外",
    "lookup": "查域名、反查地址（会发 DNS 请求）",
}
# 拦下时抛出的异常里都带着这一句：认得出是这张网拦的，不是对面真的拒绝了
MARK = "测试总网拦下了"

# hits：拦下（A1 里是「会被拦」）的那些，(PYTEST_CURRENT_TEST, 种类, 去处, 判定)；种类是 connect、udp、
# sendto、lookup。allowed：放行的只按判定数次数
_STATE = {"hits": [], "allowed": Counter()}
# 本进程 bind 过的套接字（弱引用：关掉、回收了就自动掉出去）
_BOUND = weakref.WeakValueDictionary()
# allowing() 登记的本机端口 → 登记了几层
_REGISTERED = Counter()
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


@contextlib.contextmanager
def allowing(port):
    """with 块里放行连本机的 port：子进程自己挑的端口（Chrome 的 --remote-debugging-port=0、子进程里的服务器），
    或者测试刚关掉、要验证「连不上」的自己的端口。LIVE_PORTS 里的端口登记了也不放行。出了 with 就撤掉。"""
    _REGISTERED[port] += 1
    try:
        yield
    finally:
        _REGISTERED[port] -= 1
        if _REGISTERED[port] <= 0:
            del _REGISTERED[port]


def judge(host, port):
    """连 (host, port) 算哪一种：sentinel、own、registered、live、local、outside（见 LABELS）。"""
    if not is_loopback(host):
        return "outside"
    if port in LIVE_PORTS:
        return "live"
    if port == SENTINEL_PORT:
        return "sentinel"
    if _REGISTERED.get(port):
        return "registered"
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


def _refusal(kind, where, verdict):
    text = "{}：{} {}（{}）。测试只能连自己起的假服务器（绑 127.0.0.1:0）或 127.0.0.1:9 哨兵，见 tests/socket_net.py".format(
        MARK, kind, where, LABELS[verdict])
    if kind == "lookup":
        return socket.gaierror(socket.EAI_NONAME, text)
    return ConnectionRefusedError(errno.ECONNREFUSED, text)


def _hook(event, args):
    if event not in _EVENTS:
        return
    try:
        found = _classify(event, args)
        if found is None:
            return
        kind, where, verdict = found
        if verdict in ALLOWED:
            _STATE["allowed"][verdict] += 1
            return
        _STATE["hits"].append((os.environ.get("PYTEST_CURRENT_TEST", ""), kind, where, verdict))
    except Exception:
        # 审计钩子里抛出的异常会落到被观察的那次调用上：网自己出错绝不能改变被测代码的行为
        return
    if MODE == "strict":
        raise _refusal(kind, where, verdict)


def install():
    """装上审计钩子（一个进程只装一次；装上就卸不掉）。"""
    if not _INSTALLED:
        sys.addaudithook(_hook)
        _INSTALLED.append(True)


def installed():
    return bool(_INSTALLED)


def node_id(current):
    """PYTEST_CURRENT_TEST 形如 "tests/test_x.py::test_y[a] (call)"：去掉阶段，剩下用例 id。"""
    return current.rsplit(" (", 1)[0] if current.endswith(")") else current


def hits_of(nodeid, hits=None):
    """记下的连接里属于这个用例（它的 setup、call、teardown 阶段）的那些。"""
    return [h for h in (_STATE["hits"] if hits is None else hits) if node_id(h[0]) == nodeid]


def hits_of_anyone():
    """记下的全部（含不在任何用例里的）。"""
    return list(_STATE["hits"])


def failure_text(hits):
    lines = sorted({"  {} {}  {}".format(kind, where, LABELS[verdict]) for _t, kind, where, verdict in hits})
    return ("测试总网：这个用例往外连了 {} 次，都已拦下（被测代码收到的是 ConnectionRefusedError，查域名是 "
            "socket.gaierror），没有发出去：\n{}\n测试只能连自己起的假服务器（绑 127.0.0.1:0，端口由系统挑，"
            "连的时候还开着）或 127.0.0.1:9 哨兵；要测「连不上」就连哨兵，别连一个关掉的端口。子进程自己挑的"
            "端口用 tests/socket_net.py 的 allowing(port) 登记。8765–8774、9333、9334、11434 是用户直播时开着的"
            "端口，登记了也不放行。".format(len(hits), "\n".join(lines)))


def summary(tr):
    """测试结束时把记下的连接列出来：放行的只报次数，拦下的逐个去处列出来自哪些用例。"""
    from collections import defaultdict

    hits, allowed = list(_STATE["hits"]), _STATE["allowed"]
    if not hits and (not allowed or tr.config.option.verbose <= 0):     # 平时只在拦下了东西时才占地方
        return
    strict = MODE == "strict"
    tr.write_sep("-", "测试总网（{}）：测试进程发出的连接".format(
        "strict，拦下并判失败" if strict else "只记录、不拦"))
    if allowed:
        tr.write_line("放行：" + "；".join(
            "{} {} 次".format(LABELS[v], n) for v, n in sorted(allowed.items())))
    if not hits:
        tr.write_line("拦下的：没有" if strict else "收紧以后会被拦的：没有")
        return
    blocked = defaultdict(list)
    for test, kind, where, verdict in hits:
        blocked[(verdict, kind, where)].append(node_id(test) or "（不在用例里）")
    tr.write_line("{}：{} 个去处，共 {} 次".format(
        "拦下的" if strict else "收紧以后会被拦的", len(blocked), len(hits)))
    for (verdict, kind, where), tests in sorted(blocked.items()):
        names = Counter(tests)
        tr.write_line("  {} {}  ×{}  {}，{} 个用例".format(kind, where, len(tests), LABELS[verdict], len(names)))
        for name, n in sorted(names.items()):
            tr.write_line("    {}{}".format(name, "" if n == 1 else "  ×{}".format(n)))
