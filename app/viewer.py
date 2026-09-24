"""手机同看：局域网里的第二个监听面，只读，只发白名单里的字段。

为什么另起一套 aiohttp app，而不是给 CaptionServer 加一条路由：控制面绑在
127.0.0.1 上，它的 /ws 能下发 start/stop/改设置/一键更新。手机同看要绑
0.0.0.0，在同一个 app 上多开一条对外路由，就等于把控制面一起搬到了局域网上。
两套 app、两个端口、两份路由表——观众这一面上根本不存在通往 on_control 的路。

KPI 约束（CLAUDE.md 六）：这个产品只有两个 KPI，违禁词召回与检测延迟；识别
循环 await 的是报警广播。所以 fanout 是**同步**的：过滤 + put_nowait，没有
任何 I/O，耗时与观众数成正比且有上界（MAX_VIEWERS）。观众收不下就丢它自己
——它会重连并拿到完整回放，所以丢一台手机在业务上是无损的，而让手机拖慢识别
就是漏报。这条约束由 tests/test_viewer_server_hook.py 钉死。
"""
import asyncio
import hmac
import json
import re
import secrets
import time
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

from .server import replay_payloads

# ---- 常量（全部模块级：测试直接引用，不要散在函数里）----
TOKEN_BYTES = 32              # secrets.token_urlsafe(32) -> 43 个 URL 安全字符
TOKEN_LEN = 43
# 观众端口不写死，也**不自动漂移到任意端口**：控制端口漂移是因为中控没有别的
# 入口，而观众端口一漂移，已经发出去、贴在墙上的二维码就静默失效。见 pick_ports
PORT_OFFSET = 1
PORT_TRIES = 5
QUEUE_MAX = 512               # 每个观众一条有界队列；回放最多 273 条，留足余量
SEND_TIMEOUT_SEC = 5.0        # 写任务里单条发送的上限（不在关键路径上，可以比控制面宽）
AUTH_TIMEOUT_SEC = 5.0        # 握手后等第一条 auth 的上限
MAX_VIEWERS = 12              # 中控团队规模，也是 fanout 每条消息的成本上界
MAX_PENDING = 32              # 在途连接（含未鉴权）总数上限，独立于且大于 MAX_VIEWERS
# 入站帧：手机现在能发「重译」，但仍然不该发很多。滑动窗口而不是绝对上限——
# 允许零星的合法点击，持续的洪泛（坏页面/探测）依然会被断开。junk 帧也计数。
INBOUND_BURST = 30
INBOUND_BURST_WINDOW_SEC = 10.0
ACTION_GAP_SEC = 3.0          # 同一观众两次「重译」之间的最短间隔
ACTIONS_PER_MINUTE = 10       # 同一观众每分钟最多几次动作；超了静默忽略，不断开
MAX_MSG_SIZE = 4096           # 入站帧上限，超过由 aiohttp 自己断开
WS_HEARTBEAT = 30             # 与控制面一致
AUTH_FAIL_AUDIT_WINDOW_SEC = 10.0   # 同一 IP 的鉴权失败最多每 10 秒记一条
AUTH_FAIL_TRACK_MAX = 256     # 限流表的条数上限，别让扫端口的人把内存撑起来
IP_REFRESH_SEC = 60.0         # 同看开着时多久重取一次本机地址（地址会变）
# 踢人时给写任务多久把关闭帧发出去，到点一律 abort。「关闭同看」「换一个链接」
# 是中控的动作，不许等一台已经不读的手机——aiohttp 的 runner.cleanup() 默认要等
# 在跑的 handler 最多 60 秒，一个赖着不断的连接就能让界面上的按钮卡一分钟
KICK_GRACE_SEC = 1.0
SHUTDOWN_TIMEOUT_SEC = 2.0    # 传给 AppRunner：handler 收尾的上限
VIEWER_AUDIT_PENDING_MAX = 100     # 空闲期攒下的审计事件上限
ALERT_CONTEXT_LIMIT = 200
SCRUB_LIMIT = 120
REPLAY_MAX = 273              # 1+1+1+20+50+100+100，见 replay_snapshot

VIEWER_FILES = ("viewer.html", "viewer.js", "viewer.css", "icon.png")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 手机页不许有内联 script：CSP 里刻意没有 unsafe-inline。
# connect-src 用 'self' 而不是裸 ws:（A7）——放宽成 ws: 等于允许连任意主机。
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'")
SECURITY_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CSP,
}


class ViewerError(RuntimeError):
    pass


# ---- 逐类型白名单（默认拒绝）----
# 多一个字段都不给手机。表以外的 type 一律不发。
ALLOW = {
    # asr_ms / e2e_ms 是内部耗时，不给
    "caption": ("id", "ts", "original", "translated", "translate_state",
                "src_lang", "target_lang", "failed", "why", "replay", "restore",
                # demo 只认 True：手机上要能看出「这是演示数据，不是真实直播」，
                # 否则演示时空着的报警面板会被当成「这场很干净」
                "demo",
                # strong/strong_state 供手机端显示「重译中…/已重译/重译失败」，
                # 也用来隐藏已经是强模型译文那条的「重译」按钮
                "strong", "strong_state"),
    "caption_update": ("id", "translated", "translate_state", "failed", "why",
                       "strong", "strong_state"),
    # streamer / session / ui_clients 是 _alert_stamp() 塞进来的内部归属信息，不给手机
    "alert": ("alert_id", "term", "tier", "ts", "matched", "context", "context_zh",
              "failed", "why", "session_total", "replay"),
    "alert_update": ("alert_id", "context_zh", "failed", "why"),
    "comment": ("id", "user", "text", "ts", "translated", "state", "replay"),
    "comment_update": ("id", "translated", "state"),
    # detail 是给中控的自由文本，可能带路径/地址：整条丢
    "comment_source": ("backend",),
    "incident": ("id", "level", "text", "ts"),
    # detail 和 command 整条丢。command 是中控唯一的出路，也正是路径与地址的载体；
    # 手机端的文案按 state 查固定表生成，比转发自由文本稳，也更合规则八
    "status": ("state", "ts"),
    # backlog_sec / reason / text / dropped 都不给：手机只需要知道有没有在积压
    "health": ("level",),
    # 中控这一场有没有开违禁词警示。只给这一个布尔——手机不需要知道原因，
    # 只需要知道「命中了会不会有人看到」（CLAUDE.md 八：只报观察）
    "alert_mode": ("on",),
    # 场次分隔线：手机端用 ts 画一条分隔线。streamer 不给——主播名是内部
    # 归属信息，同一份考量见上面 alert 里丢掉的 streamer/session/ui_clients
    "session_break": ("ts",),
}

# 显式拒绝。viewer 必须单独钉死：它的载荷里带着含 token 的 URL，
# 一旦漏进 fanout，已经扫过码的人就能看到别人的钥匙。
DENY = frozenset({"viewer", "config", "hello"})

# 弹幕来源状态归一：_publish_comment_source 的取值是 connecting/connected/
# disconnected/error/unavailable/idle，手机端只需要四档
_BACKEND_MAP = {
    "idle": "idle",
    "connecting": "connecting",
    "connected": "live",
    "live": "live",
    "disconnected": "unavailable",
    "error": "unavailable",
    "unavailable": "unavailable",
}


def _norm_backend(value):
    return _BACKEND_MAP.get(str(value or "").strip().lower(), "unavailable")


# ---- 自由文本清洗 ----
# 顺序有讲究：带协议的 URL 先走，否则后面的裸域名规则会把它切碎。
_SCRUB_PATTERNS = (
    re.compile(r"\b(?:https?|wss?|ftp)://\S+", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[\\/][^\s]*"),                      # C:\Users\x、D:/logs
    re.compile(r"~[\\/][^\s]*"),                              # ~/logs
    re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}/\S*"),   # 裸域名 + 路径
    # POSIX 绝对路径：至少要有一整段加斜杠，才不会把中文里的「识别/翻译」当路径。
    # 字符类刻意只用 ASCII——\w 在 str 模式下把中文也一起匹配了
    re.compile(r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_.\-]*"),
)


def scrub_text(text, limit=SCRUB_LIMIT):
    """给手机看的自由文本。去掉网址、绝对路径、盘符路径、~/ 路径，各换成 "…"，再截断。

    手机端在局域网上，看到的人比中控多；路径和地址对「看字幕和报警」这件事毫无用处，
    却是实测会出现在自由文本里的东西（带版本号和命令的 status.detail 就是一例）。
    """
    if not isinstance(text, str):
        if text is None:
            return ""
        text = str(text)
    for pattern in _SCRUB_PATTERNS:
        text = pattern.sub("…", text)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = SCRUB_LIMIT
    return text[:limit] if limit > 0 else ""


def filter_payload(msg):
    """白名单过滤 + 快照。返回全新 dict（调用方之后不得改写它），或 None 表示不发。

    返回的 dict 只含标量：白名单里没有一个字段该是容器，所以容器一律丢掉——
    于是手机端的消息不可能和 server.config / server.history 里的那份共享对象，
    CaptionServer.broadcast 的 *_update 就地改写也影响不到已经发出去的载荷。
    """
    if not isinstance(msg, dict):
        return None
    mtype = msg.get("type")
    if not isinstance(mtype, str) or mtype in DENY:
        return None
    allowed = ALLOW.get(mtype)
    if allowed is None:
        return None
    out = {"type": mtype}
    for key in allowed:
        if key not in msg:
            continue
        value = msg[key]
        if isinstance(value, (dict, list, tuple, set, frozenset, bytes, bytearray)):
            continue
        if key == "demo":
            if value is not True:
                continue
        elif key == "context":
            value = str(value)[:ALERT_CONTEXT_LIMIT]
        elif key == "backend":
            value = _norm_backend(value)
        elif key == "text" and mtype == "incident":
            value = scrub_text(value)
        elif key == "why":
            # why 是自由文本。今天的取值都是固定的中文短句，但它长在会变的代码
            # 路径上——顺手过一遍清洗，将来谁把异常文本塞进来也带不出路径
            value = scrub_text(value, 160)
        elif key in ("replay", "restore", "failed", "strong", "on"):
            value = bool(value)
        elif key == "strong_state":
            # 只认这三档；来路不明的值归一成 None 而不是丢掉整个键——手机端按
            # 缺失/None 一视同仁地不显示状态，但 *_update 仍是 caption 的子集
            value = value if value in ("pending", "ok", "failed") else None
        out[key] = value
    if mtype == "comment_source" and "backend" not in out:
        out["backend"] = _norm_backend(None)
    return out


# ---- token ----
def new_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_ok(given, expected):
    """定长比较。长度检查也走比较本身，不靠 len 提前分支——提前返回会把
    「长度对不对」变成一个可计时的信号。"""
    if not isinstance(expected, str) or not expected:
        return False
    if not isinstance(given, str):
        return False
    want = expected.encode("utf-8", "replace")
    got = given.encode("utf-8", "replace")
    padded = (got + b"\0" * len(want))[:len(want)]
    same = hmac.compare_digest(padded, want)
    return bool(same) and len(got) == len(want)


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{%d}$" % TOKEN_LEN)


def sanitize_token(value):
    """settings.json 是可以手改的：不是 43 字符 URL 安全串就当没有（调用方重新生成）。"""
    if isinstance(value, str) and _TOKEN_RE.match(value):
        return value
    return None


def sanitize_port(value):
    """同上：不是 1024–65535 的整数就当没有。bool 是 int 的子类，要挡掉。"""
    if isinstance(value, bool):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1024 <= port <= 65535 else None


def pick_ports(control_port, saved=None):
    """按序尝试的观众端口候选。

    存过的端口排第一：发出去的链接尽量稳定，重启后还能用同一个二维码。
    然后是控制端口 +1…+5。全部失败时上层报观察并保持关闭——绝不漂到别的端口，
    那会让贴在墙上的二维码静默失效。
    """
    control = sanitize_port(control_port) or 8765
    ports = []
    keep = sanitize_port(saved)
    if keep is not None and keep != control:
        ports.append(keep)
    for offset in range(PORT_OFFSET, PORT_OFFSET + PORT_TRIES):
        port = control + offset
        if port == control or port > 65535 or port in ports:
            continue
        ports.append(port)
    return ports


# ---- 局域网 IPv4 ----
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def _octets(addr):
    if not isinstance(addr, str):
        return None
    m = _IPV4_RE.match(addr.strip())
    if m is None:
        return None
    parts = [int(x) for x in m.groups()]
    return parts if all(0 <= x <= 255 for x in parts) else None


def _usable_ipv4(addr):
    parts = _octets(addr)
    if parts is None:
        return False
    if parts[0] in (0, 127):                      # 0.0.0.0/8、回环
        return False
    if parts[0] == 169 and parts[1] == 254:       # 链路本地
        return False
    if parts[0] >= 224:                           # 组播及以上、255.255.255.255
        return False
    return True


def _rank(addr, routed):
    if routed is not None and addr == routed:
        return 0
    parts = _octets(addr) or [0, 0, 0, 0]
    if parts[0] == 192 and parts[1] == 168:
        return 1
    if parts[0] == 10:
        return 2
    if parts[0] == 172 and 16 <= parts[1] <= 31:
        return 3
    return 4


def _probe_route_ip():
    """按默认路由问内核「出去的话用哪个源地址」。

    UDP connect **不发任何包**，只让内核选源地址；目标用 RFC 5737 的 TEST-NET-1，
    不指向任何真实主机——排障时最忌讳的就是自己的探测制造出新的观察。
    """
    import socket

    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _hostname_ipv4s():
    import socket

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    out = []
    for item in infos:
        try:
            out.append(item[4][0])
        except (IndexError, TypeError):
            continue
    return out


def lan_ipv4(probe=None, hostname_addrs=None):
    """本机的局域网 IPv4。纯同步、两路都可注入（测试不碰 socket）。

    返回 {"ip", "all", "source", "ambiguous"}。取不到返回 ip=None——**不猜**是
    什么挡住了（没有对照组就没有结论），界面只陈述「没读到本机的局域网地址」。
    """
    probe = _probe_route_ip if probe is None else probe
    hostname_addrs = _hostname_ipv4s if hostname_addrs is None else hostname_addrs
    try:
        routed = probe()
    except Exception:
        routed = None
    if not _usable_ipv4(routed):
        routed = None
    else:
        routed = routed.strip()
    try:
        others = hostname_addrs() or []
    except Exception:
        others = []
    candidates = []
    if routed is not None:
        candidates.append(routed)
    for addr in others:
        if _usable_ipv4(addr):
            candidates.append(addr.strip())
    seen = set()
    uniq = []
    for addr in candidates:
        if addr not in seen:
            seen.add(addr)
            uniq.append(addr)
    ranked = sorted(uniq, key=lambda a: _rank(a, routed))
    ip = ranked[0] if ranked else None
    source = None
    if ip is not None:
        source = "route" if (routed is not None and ip == routed) else "hostname"
    return {"ip": ip, "all": ranked, "source": source, "ambiguous": len(ranked) > 1}


def viewer_url(ip, port, token):
    """token 只能进片段：URL 片段不会进服务端，也不会进 Referer。"""
    if not ip:
        raise ViewerError("没读到本机的局域网地址")
    return "http://{}:{}/#k={}".format(ip, int(port), token)


# ---- 中控文案（规则八，按 A8 修订）----
# 只写观察到的事实和能做的事。不许出现「年龄」「限流」「封禁」这类给不透明失败
# 贴的原因标签，也不许有「多半是」「应该是」这类因果断言句式。
# 系统会弹什么框、Wi-Fi 要连哪一个——这些是可观察事实 + 可做的事，要写。
NOTE_OFF = ("关闭。打开后，连着同一个 Wi-Fi 的手机可以扫码看字幕和报警，"
            "只能看，不能操作本程序。")
NOTE_OPEN = "已打开。手机连同一个 Wi-Fi，扫下面的二维码，或直接打开：{url}"
NOTE_COUNT = "当前 {n} 人在看，最多 {max} 人。"
NOTE_KEY = "这个链接里带着一把钥匙，当密码看待；发给谁，谁就能看到字幕和报警。"
NOTE_MULTI_IP = ("本机有多个网络地址：{ips}。二维码用的是 {ip}；"
                 "手机扫了打不开，就换列表里另一个地址手工输入。")
NOTE_NO_IP = ("已打开，但没读到本机的局域网地址（只读到回环地址），所以没出二维码。"
              "手机上手工输入 http://<本机地址>:{port}/#k=<没显示的钥匙> —— "
              "本机地址可以在系统的网络设置里看到；也可以先在本机浏览器打开 "
              "http://127.0.0.1:{port}/ 确认服务本身在跑。")
NOTE_PORT_BUSY = ("端口 {ports} 都没能打开监听，系统返回：{err}。已保持关闭。"
                  "可以关掉占用这些端口的程序后再打开一次。")
NOTE_QR_FAILED = "二维码没能生成，请让手机手工输入上面的地址。"
NOTE_IP_CHANGED = "本机地址已从 {old} 变为 {new}，之前发出去的链接需要重新扫码。"
NOTE_FIRST_OPEN = ("第一次打开时，系统可能弹出是否允许接受网络连接的确认框（macOS）"
                   "或防火墙提示（Windows），请选允许。")
NOTE_WIFI = ("手机要和这台电脑连同一个 Wi-Fi。连不上时，"
             "可以问网络管理员这个 Wi-Fi 是否允许设备互相访问。")
NOTE_WECHAT = "在微信里打开卡住或提示风险时，点右上角用浏览器打开，或改用手机相机扫码。"
NOTE_TROUBLE = ("手机打不开时，程序这边只知道没有连接进来。可以依次试："
                "确认手机连的是同一个 Wi-Fi；用列表里另一个地址手工输入；"
                "在本机浏览器打开同一个链接，确认服务本身在跑。")
NOTE_ROTATE_CONFIRM = "换链接之后，现在在看的 {n} 台手机会断开，要重新扫码。继续？"


def _ports_phrase(ports, port):
    items = []
    for value in (ports or []):
        num = sanitize_port(value)
        if num is not None:
            items.append(num)
    if not items:
        return str(port) if port else "?"
    if len(items) == 1:
        return str(items[0])
    return "{}–{}".format(min(items), max(items))


def share_note(on, port=None, url=None, ip=None, ips=(), viewers=0,
               error=None, qr_ok=True, ip_changed=None, ports=()):
    """卡片上那整段文案。每一行都是「观察 + 能做的事」，行间用换行分隔。"""
    if error is not None:
        return NOTE_PORT_BUSY.format(ports=_ports_phrase(ports, port), err=error)
    if not on:
        return NOTE_OFF
    lines = []
    if url:
        lines.append(NOTE_OPEN.format(url=url))
    else:
        lines.append(NOTE_NO_IP.format(port=port))
    lines.append(NOTE_COUNT.format(n=viewers, max=MAX_VIEWERS))
    lines.append(NOTE_KEY)
    if url and len(ips) > 1:
        lines.append(NOTE_MULTI_IP.format(ips="、".join(ips), ip=ip))
    if url and not qr_ok:
        lines.append(NOTE_QR_FAILED)
    if ip_changed:
        lines.append(NOTE_IP_CHANGED.format(old=ip_changed[0], new=ip_changed[1]))
    lines.append(NOTE_FIRST_OPEN)
    lines.append(NOTE_WIFI)
    lines.append(NOTE_WECHAT)
    lines.append(NOTE_TROUBLE)
    return "\n".join(lines)


def off_state(port=None, error=None, ports=()):
    """同看关着（或端口都没打开）时的 viewer 载荷。"""
    return {"on": False, "port": port, "url": None, "ip": None, "ips": [],
            "ambiguous": False, "viewers": 0, "max_viewers": MAX_VIEWERS,
            "qr_rows": None,
            "note": share_note(False, port=port, error=error, ports=ports)}


# ---- 回放 ----
def replay_snapshot(server, viewers=0, share_since=None):
    """新观众入册时压进它自己队列的那一串消息。纯同步，只读 server 的 deque 与 config。

    第 2–4 步（status / comment_source / incidents）是必须的：config 和 hello 都
    不在白名单里，没有这三步，后进来的手机在下一次事件之前是一片空白——而
    「面板空着」和「没连上」对中控的意义完全不同（同 app/server.py 里的那段注释）。
    """
    out = [{"type": "viewer_hello", "ok": True, "ts": time.time(),
            "share_since": share_since, "viewers": viewers,
            "max_viewers": MAX_VIEWERS, "read_only": True}]
    config = getattr(server, "config", None) or {}
    status = config.get("status")
    if isinstance(status, dict):
        payload = filter_payload(dict(status, type="status"))
        if payload is not None:
            out.append(payload)
    backend = config.get("comment_backend")
    if backend:
        payload = filter_payload({"type": "comment_source", "backend": backend})
        if payload is not None:
            out.append(payload)
    # alerts_enabled 不在 config 里是老审计/老会话（键从没写过），这时不发——
    # 手机端把「没收到过 alert_mode」当「未知」，不当「关闭」
    if "alerts_enabled" in config:
        payload = filter_payload({"type": "alert_mode",
                                  "on": bool(config.get("alerts_enabled"))})
        if payload is not None:
            out.append(payload)
    incidents = config.get("incidents")
    if isinstance(incidents, dict):
        items = [x for x in incidents.values() if isinstance(x, dict)]
        items.sort(key=lambda x: x.get("ts") or 0)
        for item in items:
            payload = filter_payload(dict(item, type="incident"))
            if payload is not None:
                out.append(payload)
    for alert in list(getattr(server, "alerts", None) or []):
        if isinstance(alert, dict):
            payload = filter_payload(dict(alert, replay=True))
            if payload is not None:
                out.append(payload)
    for item in replay_payloads(list(getattr(server, "history", None) or [])):
        payload = filter_payload(item)
        if payload is not None:
            out.append(payload)
    for comment in list(getattr(server, "comments", None) or []):
        if isinstance(comment, dict):
            payload = filter_payload(dict(comment, replay=True))
            if payload is not None:
                out.append(payload)
    # 按当前各集合的上限，回放序列本就不会超过 REPLAY_MAX；这里切一刀是保险，
    # 不是当前会触发的路径——某个集合的上限以后涨了，也不该让回放悄悄超过 QUEUE_MAX。
    return out[:REPLAY_MAX]


def _coerce_action_id(value):
    """手机发上来的 id：只接受 int，或者恰好是一串数字的字符串（JS 那边有的
    路径会把数字序列化成字符串）。bool 是 int 的子类，要挡掉；负数、小数、
    带空白或其它字符的字符串一律拒绝——这是字幕的序号，不该是别的东西。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    return None


# ---- 连接 ----
class _Close:
    """写队列里的哨兵：让写任务发一个关闭帧再退出。踢人要带上关闭码，
    手机端才分得出「中控关掉了同看」和「网络断了」。"""

    __slots__ = ("code", "reason")

    def __init__(self, code, reason):
        self.code = code
        self.reason = reason


class _Viewer:
    __slots__ = ("ws", "transport", "ip", "queue", "writer", "registered",
                 "inbound_times", "last_action", "action_times")

    def __init__(self, ws, transport=None, ip=""):
        self.ws = ws
        self.transport = transport
        self.ip = ip or ""
        self.queue = asyncio.Queue(QUEUE_MAX)
        self.writer = None
        self.registered = False
        self.inbound_times = deque()     # 入站帧时间戳，供滑动窗口洪泛检测
        self.last_action = None          # 上一次被接受的动作时间（ACTION_GAP_SEC）
        self.action_times = deque()      # 最近 60 秒内被接受的动作时间（ACTIONS_PER_MINUTE）


def _peer_ip(request):
    try:
        peer = request.transport.get_extra_info("peername")
    except Exception:
        return ""
    if isinstance(peer, (list, tuple)) and peer:
        return str(peer[0])
    return ""


async def _deny(ws, reason, code):
    """拒绝一个连接：先说清是哪一种拒绝，再带着关闭码断开。手机端按码查文案。"""
    try:
        await asyncio.wait_for(ws.send_json({"type": "viewer_denied", "reason": reason}), 2.0)
    except Exception:
        pass
    try:
        await asyncio.wait_for(ws.close(code=code), 2.0)
    except Exception:
        pass


class ViewerHub:
    def __init__(self, server, ports=None, token=None, bind_host="0.0.0.0",
                 audit_hook=None, on_action=None, log=print, web_dir=None,
                 share_since=None, auth_timeout=AUTH_TIMEOUT_SEC, now=None):
        self._server = server
        if ports is None:
            ports = []
        if isinstance(ports, int):
            ports = [ports]
        self._candidates = [int(p) for p in ports]
        self._token = token
        self._bind_host = bind_host
        self._audit_hook = audit_hook
        # 手机发起的动作（目前只有「重译」）交给 Pipeline 注入的回调；hub 自己
        # 从不 import pipeline，也从不直接碰 Pipeline 的状态（同 audit_hook）
        self._on_action = on_action
        self._log = log or (lambda *a, **k: None)
        self._web_dir = Path(web_dir) if web_dir is not None else WEB_DIR
        self.share_since = time.time() if share_since is None else share_since
        self.auth_timeout = auth_timeout
        self._now = now or time.monotonic
        self._viewers = []
        self._runner = None
        self._site = None
        self._bound_port = None
        self._running = False
        self._pending = 0
        self._auth_fail = {}
        # 踢人之后的兜底 abort 任务。保住引用：事件循环只弱引用任务，
        # 不保引用的会在半路被 GC 收走（同 Pipeline._spawn 那条教训）
        self._guards = set()
        # on_action 返回的协程（真正等强模型的部分）以同样的理由保住引用
        self._action_tasks = set()

    # ---- 只读属性 ----
    @property
    def running(self):
        return self._running

    @property
    def count(self):
        return len(self._viewers)

    @property
    def port(self):
        if self._bound_port is not None:
            return self._bound_port
        return self._candidates[0] if self._candidates else None

    @property
    def token(self):
        return self._token

    def url(self, ip):
        """给这个地址拼出观众链接；拿不到地址返回 None。"""
        try:
            return viewer_url(ip, self.port, self._token)
        except (ViewerError, TypeError, ValueError):
            return None

    # ---- 生命周期 ----
    def build_app(self):
        @web.middleware
        async def guard(request, handler):
            try:
                resp = await handler(request)
            except web.HTTPException as exc:
                # 连 404 也要带上这些头：手机上任何一个响应都在局域网里
                for key, value in SECURITY_HEADERS.items():
                    exc.headers[key] = value
                raise      # 原样抛回去（返回 HTTPException 对象已被 aiohttp 弃用）
            # WebSocket 已经 prepare 过，改头没有意义也不该动
            if not getattr(resp, "prepared", False):
                for key, value in SECURITY_HEADERS.items():
                    resp.headers[key] = value
            return resp

        app = web.Application(middlewares=[guard])
        # allow_head=False：aiohttp 默认给每条 GET 顺带登记一条 HEAD。观众面上
        # 没有任何东西需要 HEAD，路由表越短要守的门越少（HEAD /vws 尤其没意义）
        app.router.add_get("/", self.page, allow_head=False)
        app.router.add_get("/vws", self.vws, allow_head=False)
        app.router.add_get("/v/{name}", self.asset, allow_head=False)
        # 刻意不用 add_static：静态目录会把 web/ 下每个文件都端出去，包括
        # app.js 和 index.html。观众面只认 VIEWER_FILES 这四个文件。
        return app

    async def start(self):
        """逐个候选端口试。全部 OSError 就把最后一个原样抛给调用方（由它降级并报观察）。"""
        if self._running:
            return
        last = None
        for port in self._candidates:
            runner = self._make_runner()
            await runner.setup()
            site = web.TCPSite(runner, self._bind_host, port)
            try:
                await site.start()
            except OSError as exc:
                last = exc
                try:
                    await runner.cleanup()
                except Exception:
                    pass
                continue
            self._runner = runner
            self._site = site
            self._bound_port = self._socket_port(site, port)
            self._running = True
            return
        if last is None:
            last = OSError("没有可用的观众端口")
        raise last

    def _make_runner(self):
        """shutdown_timeout 的位置在 aiohttp 各版本间挪过（曾在 TCPSite 上）：
        认就传，不认就用它的默认值，反正 stop() 还会自己 abort 兜底。"""
        try:
            return web.AppRunner(self.build_app(), access_log=None,
                                 shutdown_timeout=SHUTDOWN_TIMEOUT_SEC)
        except TypeError:
            return web.AppRunner(self.build_app(), access_log=None)

    @staticmethod
    def _socket_port(site, fallback):
        try:
            sockets = site._server.sockets
            if sockets:
                return int(sockets[0].getsockname()[1])
        except Exception:
            pass
        return fallback

    async def stop(self, reason="off"):
        """关监听 + 踢掉所有观众。幂等，且有时间上界。"""
        site, runner = self._site, self._runner
        self._site = self._runner = None
        self._running = False
        victims = list(self._viewers)
        for viewer in victims:
            self._kick(viewer, 4403, reason)
        if victims:
            # 先给写任务一小会儿把关闭帧发出去（手机端据此说「中控已关闭手机同看」），
            # 到点一律 abort：不 abort 的话，下面的 runner.cleanup() 会等这些还在
            # 跑的 handler，一个赖着不断的连接就能让「关闭」这个按钮卡住
            writers = [v.writer for v in victims if v.writer is not None]
            if writers:
                try:
                    await asyncio.wait(writers, timeout=KICK_GRACE_SEC)
                except Exception:
                    pass
            for viewer in victims:
                self._abort(viewer)
        if site is not None:
            try:
                await site.stop()
            except Exception as exc:
                self._log("[警告] 停止手机同看监听出错: {}".format(exc))
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as exc:
                self._log("[警告] 清理手机同看监听出错: {}".format(exc))
        self._bound_port = None

    def rotate(self, token):
        """换 token 并踢掉所有在看的：旧链接从这一刻起就不该再进得来。

        同步返回：中控点「换一个链接」不该等任何一台手机。关闭帧由各自的写任务
        发出去，发不掉的由 _abort_later 兜底。
        """
        self._token = token
        victims = list(self._viewers)
        for viewer in victims:
            self._kick(viewer, 4403, "rotate")
        self._abort_later(victims)

    def _abort_later(self, victims, delay=None):
        """关闭帧发不出去的（手机已经离线但 TCP 还挂着）过一会儿一律断掉。
        fire-and-forget；没有在跑的事件循环时当场断。

        delay 默认取模块常量而不是写进签名默认值：默认值在函数定义时就绑死了，
        测试改 KICK_GRACE_SEC 会改不动——这类「看起来生效了其实没生效」的坑
        最难发现。
        """
        if not victims:
            return
        wait = KICK_GRACE_SEC if delay is None else delay

        async def later():
            await asyncio.sleep(wait)
            for viewer in victims:
                self._abort(viewer)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:          # 不在事件循环里：没人能替我们收尾
            for viewer in victims:
                self._abort(viewer)
            return
        task = loop.create_task(later())
        self._guards.add(task)
        task.add_done_callback(self._guards.discard)

    # ---- 分发（同步，绝不 await）----
    def fanout(self, msg):
        if not self._viewers:
            return
        payload = filter_payload(msg)
        if payload is None:
            return
        # payload 在所有观众之间共享同一个 dict：它是刚建的、之后谁都不改写，
        # 省掉 N 份拷贝。list()：循环里可能把人踢出去
        for viewer in list(self._viewers):
            try:
                viewer.queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._drop(viewer, "queue_full")

    # ---- 注册与丢弃 ----
    def new_viewer(self, ws, transport=None, ip=""):
        return _Viewer(ws, transport, ip)

    def register(self, viewer):
        """同步：把回放序列压进这条连接自己的队列，然后入册。

        全程没有一个 await，所以「回放快照 / 实时注册」不存在竞态——之后来的
        实时消息只能排在回放后面。
        """
        for payload in replay_snapshot(self._server, viewers=self.count + 1,
                                       share_since=self.share_since):
            try:
                viewer.queue.put_nowait(payload)
            except asyncio.QueueFull:
                break            # 回放放不下不该拦住入册：实时消息比历史重要
        viewer.registered = True
        self._viewers.append(viewer)
        self._event("viewer_connected", ip=viewer.ip, count=self.count)

    def _unregister(self, viewer):
        was = viewer.registered
        viewer.registered = False
        try:
            self._viewers.remove(viewer)
        except ValueError:
            pass
        return was

    def _abort(self, viewer):
        """用 abort 不用 close——沿用控制面那条教训：对一个不读的页面
        close() 永远等不完。被断开的手机会自己重连并拿到完整回放。"""
        transport = viewer.transport
        if transport is None:
            return
        try:
            transport.abort()
        except Exception:
            pass

    def _drop(self, viewer, reason):
        was = self._unregister(viewer)
        self._abort(viewer)
        if was:
            self._event("viewer_disconnected", ip=viewer.ip, count=self.count,
                        reason=reason)
        self._cancel_writer(viewer)

    def _kick(self, viewer, code, reason):
        """中控关掉同看/换了链接：带关闭码断开，手机端才说得出是哪一种。"""
        was = self._unregister(viewer)
        try:
            viewer.queue.put_nowait(_Close(code, reason))
        except asyncio.QueueFull:
            self._abort(viewer)
        if was:
            self._event("viewer_disconnected", ip=viewer.ip, count=self.count,
                        reason="kicked")

    def _cancel_writer(self, viewer):
        task = viewer.writer
        if task is None:
            return
        current = None
        try:
            current = asyncio.current_task()
        except Exception:
            current = None
        if task is not current:
            task.cancel()

    # ---- 审计 ----
    def _event(self, event, **fields):
        hook = self._audit_hook
        if hook is None:
            return
        try:
            hook(event, **fields)
        except Exception as exc:
            self._log("[警告] 记录手机同看事件失败: {}".format(exc))

    def _auth_failed(self, ip, why):
        """鉴权失败限流：同一 IP 每 AUTH_FAIL_AUDIT_WINDOW_SEC 最多记一条，
        被压掉的次数攒进下一条的 suppressed。token 绝不进审计。"""
        now = self._now()
        record = self._auth_fail.get(ip)
        if record is not None and (now - record["last"]) < AUTH_FAIL_AUDIT_WINDOW_SEC:
            record["suppressed"] += 1
            return
        suppressed = record["suppressed"] if record is not None else 0
        if record is None and len(self._auth_fail) >= AUTH_FAIL_TRACK_MAX:
            oldest = min(self._auth_fail, key=lambda k: self._auth_fail[k]["last"])
            self._auth_fail.pop(oldest, None)
        self._auth_fail[ip] = {"last": now, "suppressed": 0}
        self._event("viewer_auth_failed", ip=ip, why=why, suppressed=suppressed)

    # ---- 状态载荷（只发给 127.0.0.1 的控制面）----
    def state(self, ip_info=None, qr_rows=None, ip_changed=None):
        info = ip_info or {}
        ips = [x for x in (info.get("all") or []) if isinstance(x, str)]
        ip = info.get("ip")
        on = bool(self._running)
        port = self.port
        url = self.url(ip) if (on and ip) else None
        return {"on": on, "port": port, "url": url,
                "ip": ip if on else None, "ips": ips if on else [],
                "ambiguous": bool(info.get("ambiguous")) if on else False,
                "viewers": self.count, "max_viewers": MAX_VIEWERS,
                "qr_rows": qr_rows,
                "note": share_note(on, port=port, url=url, ip=ip, ips=ips,
                                   viewers=self.count, qr_ok=qr_rows is not None,
                                   ip_changed=ip_changed, ports=self._candidates),
                # 换链接确认框的文案：连着 {n} 占位符原样下发，前端点按钮那一刻
                # 才知道当下人数，由它自己替换——不在这里 .format()，也不在
                # web/app.js 里另存一份，唯一出处就是 NOTE_ROTATE_CONFIRM。
                "rotate_confirm": NOTE_ROTATE_CONFIRM}

    # ---- 路由 ----
    async def page(self, request):
        path = self._web_dir / "viewer.html"
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def asset(self, request):
        name = request.match_info.get("name") or ""
        if name not in VIEWER_FILES:
            raise web.HTTPNotFound()
        path = self._web_dir / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    def _make_ws(self):
        """单独一个方法，测试可以换成替身——观众握手这一段的分支（超时、错 token、
        满员、入站洪泛）不值得为了测它去真开一串 socket。
        max_msg_size：入站帧超过 4 KB 由 aiohttp 自己断开，手机端一条都不该发。"""
        return web.WebSocketResponse(heartbeat=WS_HEARTBEAT, max_msg_size=MAX_MSG_SIZE)

    async def vws(self, request):
        # 未鉴权连接也要有上限：在 prepare 之前就挡，不升级 WebSocket。没有这道闸，
        # 一串「连上但不发 auth」的连接就能把在途连接堆到没有上界
        if self._pending >= MAX_PENDING:
            raise web.HTTPServiceUnavailable(text="too many connections")
        self._pending += 1
        ip = _peer_ip(request)
        viewer = None
        ws = self._make_ws()
        try:
            await ws.prepare(request)
            try:
                first = await asyncio.wait_for(ws.receive(), self.auth_timeout)
            except asyncio.TimeoutError:
                self._auth_failed(ip, "timeout")
                await _deny(ws, "timeout", 4408)
                return ws
            if first.type in (WSMsgType.CLOSE, WSMsgType.CLOSING,
                              WSMsgType.CLOSED, WSMsgType.ERROR):
                return ws            # 自己走了：不是鉴权失败，不记
            why = self._check_auth(first)
            if why is not None:
                self._auth_failed(ip, why)
                await _deny(ws, "token", 4401)
                return ws
            if self.count >= MAX_VIEWERS:
                self._event("viewer_disconnected", ip=ip, count=self.count, reason="cap")
                await _deny(ws, "full", 4429)
                return ws
            viewer = _Viewer(ws, request.transport, ip)
            self.register(viewer)          # 同步块：鉴权通过到入册之间没有 await
            viewer.writer = asyncio.ensure_future(self._writer(viewer))
            async for msg in ws:
                # 鉴权之后的入站消息：只认恰好 {"type":"retranslate","id":…}，
                # 其它一律不解析、绝不转交 on_control——这道闸首先是给坏页面和
                # 探测的，「重译」只是从「丢弃一切」里单独开的一个小口子
                if self._flooding(viewer):
                    self._drop(viewer, "inbound_flood")
                    break
                self._handle_inbound(viewer, msg)
        finally:
            self._pending -= 1
            if viewer is not None:
                if self._unregister(viewer):
                    self._event("viewer_disconnected", ip=viewer.ip,
                                count=self.count, reason="closed")
                self._cancel_writer(viewer)
        return ws

    def _check_auth(self, msg):
        if msg.type != WSMsgType.TEXT:
            return "bad_type"
        try:
            data = json.loads(msg.data)
        except (ValueError, TypeError):
            return "not_json"
        if not isinstance(data, dict):
            return "not_json"
        if data.get("type") != "auth":
            return "missing"
        if not token_ok(data.get("k"), self._token):
            return "bad_token"
        return None

    # ---- 入站：手机发起的动作 ----
    def _flooding(self, viewer):
        """滑动窗口洪泛检测：INBOUND_BURST_WINDOW_SEC 秒内超过 INBOUND_BURST 帧
        （不论是不是能解析、是不是「重译」）就该断。junk 帧一样计数——不然
        坏页面只要把 id 换成乱七八糟的东西就绕过了这道闸。"""
        now = self._now()
        times = viewer.inbound_times
        times.append(now)
        while times and now - times[0] > INBOUND_BURST_WINDOW_SEC:
            times.popleft()
        return len(times) > INBOUND_BURST

    def _handle_inbound(self, viewer, msg):
        """只认恰好 {"type":"retranslate","id":<int 或数字字符串>}。别的一切
        （多一个键、类型不对、不是 JSON、不是文本帧）一律安静地什么都不做——
        既不回消息也不断开，坏页面和探测本就不该有回音。"""
        if msg.type != WSMsgType.TEXT:
            return
        try:
            data = json.loads(msg.data)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict) or set(data) != {"type", "id"}:
            return
        if data.get("type") != "retranslate":
            return
        seq = _coerce_action_id(data.get("id"))
        if seq is None:
            return
        if not self._allow_action(viewer):
            return          # 超过频率上限：静默忽略，不发送任何东西，不断开
        self._dispatch_action("retranslate", {"id": seq}, viewer.ip)

    def _allow_action(self, viewer):
        """每个观众自己的节流：两次动作之间至少 ACTION_GAP_SEC，每分钟至多
        ACTIONS_PER_MINUTE 次。超限不算错误、不记审计、不断连接——只是这次
        点击什么都不发生，手机端按钮上的本地冷却会先一步挡住大多数这种情况。"""
        now = self._now()
        if viewer.last_action is not None and (now - viewer.last_action) < ACTION_GAP_SEC:
            return False
        window = viewer.action_times
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= ACTIONS_PER_MINUTE:
            return False
        viewer.last_action = now
        window.append(now)
        return True

    def _dispatch_action(self, action, payload, ip):
        """调用 Pipeline 注入的回调。同步部分的异常在这里就地吞掉——一个动作
        处理坏了不该断连接，也不该炸掉正在读 socket 的循环。回调可能返回一个
        协程（真正等强模型的部分），那部分交给 _schedule_action 用 ensure_future
        调度，绝不在这儿 await：await 就是让一台手机的点击拖慢识别循环。"""
        callback = self._on_action
        if callback is None:
            return
        try:
            result = callback(action, payload, ip)
        except Exception as exc:
            self._log("[警告] 处理手机端动作出错: {}".format(exc))
            return
        if asyncio.iscoroutine(result):
            self._schedule_action(result)

    def _schedule_action(self, coro):
        async def runner():
            try:
                await coro
            except Exception as exc:
                self._log("[警告] 处理手机端动作出错: {}".format(exc))

        task = asyncio.ensure_future(runner())
        self._action_tasks.add(task)
        task.add_done_callback(self._action_tasks.discard)

    async def _writer(self, viewer):
        """每条连接一个写任务，只认这条连接自己的队列。识别循环从不 await 这里。"""
        while True:
            item = await viewer.queue.get()
            if isinstance(item, _Close):
                try:
                    await asyncio.wait_for(
                        viewer.ws.close(code=item.code), SEND_TIMEOUT_SEC)
                except Exception:
                    self._abort(viewer)
                return
            try:
                await asyncio.wait_for(viewer.ws.send_json(item), SEND_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                self._drop(viewer, "send_timeout")
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                # 连接已经没了：出册就好，别再报一次断开原因
                self._unregister(viewer)
                return
