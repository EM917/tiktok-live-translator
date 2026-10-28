"""闪购助手（本机另一个程序）的只读状态：找程序、发 GET /status、把回话规整成推给控制页的那份 dict。

这里只有纯函数和一个记计时的小类，不依赖 Pipeline，不广播、不打印；接线在 app/pipeline_flash.py。
不是界面文件：界面文字在前端按代码映射（web/flash.js），这里只产出代码、数字和闪购助手给的数据。

接口契约（闪购助手那边定的）：
- `GET http://127.0.0.1:9334/status`，只监听本机；Host 必须是 127.0.0.1:9334 或 localhost:9334，
  aiohttp 默认发的就是前者。CORS 只放 8765，所以页面不直连，由后端来取（控制端口可能漂到 8766–8774）。
- 连不上（连接被拒）就当作闪购助手没在运行：它和专用 Chrome 同生共死，Chrome 一关它就退出。
  连得上却不回话不算「没在运行」，见 fetch_status。
- `updated_at` 每次成功判断都刷新（后台每 2 秒一次），UTC、以 Z 结尾；page=error 时不刷新；
  还没判断过时为 null。陈旧就看它多久没变，见 StatusTracker。
- 代码表（page、paused_reason、state、level）以契约为准；认不出的代码原样（清洗后）交给前端显示。

9334 上回话的不一定是闪购助手（本机任何进程都能占这个端口），所以读体限长、宽容解析：
不认识的键丢掉，类型不对的当 None，字符串截长、去掉控制字符。
"""
import asyncio
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "闪购助手.app"
APP_PATH = Path("/Applications") / APP_NAME
STATUS_URL = "http://127.0.0.1:9334/status"

POLL_OK_SEC = 3          # 连上了：每 3 秒查一次
POLL_DOWN_SEC = 15       # 连不上或读不出：每 15 秒查一次
BOOST_SEC = 30           # 点了「打开闪购助手」、open 返回 0 之后这么久里一律 3 秒，好尽快看到它起来
STALE_SEC = 15           # 连得上但 updated_at 超过这么久没更新：陈旧
STUCK_SEC = 60           # 商品停着、该续开却一直没续开，连续这么久才算卡住（续开实测最长 30–40 秒）
# 卡住计时表里不在列表上的商品留多久、表最多记几个（见 StatusTracker）。闪购助手 0.8.0 起每翻完一轮刷新一次
# Buy now，列表先变短再补回，排在后面的商品会暂时不在 /status 里；10 分钟都没再出现的才丢
STUCK_FORGET_SEC = 600
STUCK_TRACK_MAX = 600
HTTP_TIMEOUT_SEC = 2
OPEN_TIMEOUT_SEC = 10    # open -a 正常立刻返回；等这么久还没返回就当没打开

# 一场最多 300 个商品（闪购助手会话 10-27 告知，实际见过 103 个）。300 个带长标题的商品
# 约 300 KB，读体上限留足余量，挡的只是撑爆内存的回话
MAX_BYTES = 1024 * 1024
MAX_PRODUCTS = 300
MAX_ALERTS = 20          # 契约：最多 20 条，最早的在前
MAX_TITLE = 200
MAX_TEXT = 500
MAX_CODE = 40
MAX_VERSION = 20

PAGE_ATTENTION = ("login_required", "captcha", "error")

# 推给页面前商品的排序（闪购助手会话 10-27 定的优先级）：要人看的在前。同一档保持闪购助手给的顺序。
# stopped 只有卡住的（stuck）排第一档；还在续开计时里的、不自动续开的排在进行中那几档后面
_RANK_STUCK = 0
_RANK = {"pending": 1, "unknown": 2, "live": 3, "between_rounds": 4, "stopped": 5,
         "no_sale": 6, "ended_by_user": 7}
_RANK_OTHER = 8          # 认不出的代码、缺 state 的

_CODE_DROP = re.compile(r"[^A-Za-z0-9_.-]")
_VERSION_DROP = re.compile(r"[^A-Za-z0-9_.+-]")
# C0/C1 控制字符、行/段分隔符、孤立代理（json 认 "\ud800"，写终端或再编码时会炸）
_TEXT_DROP = re.compile("[\x00-\x1f\x7f-\x9f  \ud800-\udfff]")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?"
                  r"(Z|z|[+-]\d{2}(?::?\d{2})?)?")
# 合理的时间戳范围（秒）：2001 年到 5138 年。越界的当认不出，免得页面拿到一个画不出的日期
_TS_MIN, _TS_MAX = 1e9, 1e11


# ---- 找程序、打开程序 --------------------------------------------------------------------

def app_candidates(home=None):
    """要看的两个位置：/Applications 和 ~/Applications。只看这两处，不跑 mdfind。"""
    out = [APP_PATH]
    try:
        base = Path.home() if home is None else Path(home)
        out.append(base / "Applications" / APP_NAME)
    except (RuntimeError, KeyError, OSError):
        pass                      # 取不到家目录：只看 /Applications
    return tuple(out)


def find_app(platform=None, candidates=None):
    """装了就返回 .app 的路径，没装（或不是 macOS）返回 None。只在启动时判一次。"""
    if (sys.platform if platform is None else platform) != "darwin":
        return None
    for path in (app_candidates() if candidates is None else candidates):
        try:
            path = Path(path)
            if path.is_dir():
                return path
        except (OSError, ValueError, TypeError):
            continue
    return None


def installed(platform=None, candidates=None):
    return find_app(platform, candidates) is not None


def open_argv(path=APP_PATH):
    """按完整路径打开：检测到的和打开的是同一个东西。不按 bundle id 打开（它不进公开仓库）。
    不带任何来自页面的参数：控制页发来的指令只有一个类型，argv 写死在这里。"""
    return ["open", "-a", str(path)]


# ---- 取 /status ----------------------------------------------------------------------------

class _NeverRaised(Exception):
    """aiohttp 3.10 以前没有 ConnectionTimeoutError：拿它占位，那条 except 永远不命中。"""


async def fetch_status(url=None, timeout=HTTP_TIMEOUT_SEC, limit=MAX_BYTES):
    """GET 一次，返回 (kind, value)：
    - ("ok", 解析出的 JSON)；
    - ("unreachable", None)：连不上（连接被拒：9334 上没人在听，契约里这就是闪购助手没在运行）；
    - ("bad_response", 原因代码)：9334 上有东西，但没拿到能用的回话。原因是
      no_answer（timeout 秒内没回完，或者没回话就断开：进程卡住、被挂起时内核照样完成握手，我们这边只看到读超时）、
      cut_off（回到一半断开）、http（回的不是 HTTP）、http_<状态码>、too_large、json。
    只有连不上才算「没在运行」：卡住的闪购助手也是这个端口连得上、却不回话，那正是要人去看的时候，
    不能和「没开」混成一种、不变色（CLAUDE.md 第八条：只报看到的）。
    连不上包括「连接阶段超时」：Windows 上连本机没人听的端口不是马上被拒，而是重试约 2 秒，超时先到。
    所以连接和读分开计时：连接阶段超时算 unreachable，连上以后读超时才算 no_answer。aiohttp 3.10 以前
    没有 ConnectionTimeoutError，分不出两段，一律按 no_answer。
    取消照样往外抛。每次一个短命 session（同 app/localmodel.is_running），不用管关闭。
    url 缺省时在调用这一刻取 STATUS_URL（不写成默认参数：tests/conftest.py 要能把它换掉）。"""
    import aiohttp
    from .nethttp import read_all

    url = STATUS_URL if url is None else url

    # 连接、每次读各 timeout 秒；总时长再兜一层，挡住一点一点往回滴的对端
    timeouts = aiohttp.ClientTimeout(total=timeout * 2, connect=timeout, sock_read=timeout)
    connect_timeout = getattr(aiohttp, "ConnectionTimeoutError", None) or _NeverRaised
    try:
        async with aiohttp.ClientSession(timeout=timeouts) as session:
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status != 200:
                    return "bad_response", "http_%d" % resp.status
                body = await read_all(resp, limit)
    except asyncio.CancelledError:
        raise
    # 先后有讲究：ClientConnectorError 是 ClientOSError 的子类；3.11 起 asyncio.TimeoutError 就是内置的
    # TimeoutError，它又是 OSError 的子类，得排在最后那条 OSError 前面
    except aiohttp.ClientConnectorError:
        return "unreachable", None
    except connect_timeout:    # 是 asyncio.TimeoutError 的子类，得排在下面那条前面
        return "unreachable", None
    except (asyncio.TimeoutError, aiohttp.ServerDisconnectedError, aiohttp.ClientOSError):
        return "bad_response", "no_answer"
    except aiohttp.ClientPayloadError:
        return "bad_response", "cut_off"
    except aiohttp.ClientResponseError:
        return "bad_response", "http"
    except (aiohttp.ClientError, OSError, ValueError):
        return "unreachable", None
    if body is None:
        return "bad_response", "too_large"
    try:
        return "ok", json.loads(body)
    except (ValueError, RecursionError):
        return "bad_response", "json"


# ---- 字段规整 ------------------------------------------------------------------------------

def _code(value, cap=MAX_CODE):
    """代码字段：只收 str，只留字母数字和 _ . -，截长；空串当 None。"""
    if not isinstance(value, str):
        return None
    return _CODE_DROP.sub("", value)[:cap] or None


def _version(value):
    if not isinstance(value, str):
        return None
    return _VERSION_DROP.sub("", value)[:MAX_VERSION] or None


def _int(value):
    """整数字段：挡掉 bool（同 app/float_window._number）；整数值的 float 转 int；其余、超出 ±1e9 的当 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        value = int(value)
    return value if abs(value) <= 1e9 else None


def _bool(value):
    return value if isinstance(value, bool) else None


def _text(value, cap):
    """数据文本（商品标题、记录正文）：只收 str，去掉控制字符，截长。原样交给前端用 textContent 显示。"""
    if not isinstance(value, str):
        return None
    return _TEXT_DROP.sub("", value)[:cap]


def _product_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    return _TEXT_DROP.sub("", value)[:MAX_CODE] or None


def parse_iso(value):
    """ISO 8601 时间 → epoch 秒（float）；认不出返回 None。

    自己拆，不用 datetime.fromisoformat：CI 最低 Python 3.10，它不认结尾的 Z，小数秒也只认 3 位或 6 位。
    认：可选 1–9 位小数（截到微秒），Z、±HH:MM、±HHMM、±HH；不带时区按 UTC（契约写明是 UTC）。
    也认数字：大于 1e12 当毫秒，大于 1e9 当秒。超出合理范围的（2001 年以前、5138 年以后）当认不出。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        stamp = value / 1000.0 if value > 1e12 else float(value)
    elif isinstance(value, str) and len(value) <= 64:
        m = _ISO.fullmatch(value.strip())
        if not m:
            return None
        year, month, day, hour, minute, second, frac, tz = m.groups()
        micro = int((frac or "0").ljust(6, "0")[:6])
        try:
            moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                              micro, tzinfo=timezone.utc)
        except ValueError:
            return None
        offset = 0
        if tz and tz not in ("Z", "z"):
            digits = tz[1:].replace(":", "")
            hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
            if hours > 23 or minutes > 59:
                return None
            offset = (hours * 3600 + minutes * 60) * (-1 if tz[0] == "-" else 1)
        stamp = moment.timestamp() - offset
    else:
        return None
    return stamp if _TS_MIN < stamp < _TS_MAX else None


def _ms(value):
    stamp = parse_iso(value)
    return None if stamp is None else int(round(stamp * 1000))


def _product(raw):
    return {
        "id": _product_id(raw.get("product_id")),
        "title": _text(raw.get("title"), MAX_TITLE),
        "state": _code(raw.get("state")),
        "round": _int(raw.get("round")),
        "ends_ms": _ms(raw.get("round_ends_at")),
        "next_ms": _ms(raw.get("next_round_at")),
        "stock_left": _int(raw.get("stock_left")),
        "stock_total": _int(raw.get("stock_total")),
        "renewals": _int(raw.get("renewals")),
        "enabled": _bool(raw.get("enabled")),
    }


def _alert(raw):
    return {"at_ms": _ms(raw.get("at")), "level": _code(raw.get("level")),
            "text": _text(raw.get("text"), MAX_TEXT)}


def normalize(raw):
    """/status 的 JSON → 规整后的 dict；raw 不是对象返回 None（调用方记成 bad_response / not_object）。

    带两个以 _ 开头的内部键，不发给页面：_schema（终端提示用）、_updated_s（判陈旧用）。
    商品保持闪购助手给的顺序、最多 MAX_PRODUCTS 个（排序由 StatusTracker 做，它要先算 stuck）；
    products_total 是收到的商品总数（含截掉的）。记录只取最后 MAX_ALERTS 条，最早的在前。
    shop、page_version 和不认识的键一律丢掉。"""
    if not isinstance(raw, dict):
        return None
    schema = raw.get("schema")
    schema = _int(schema) if _int(schema) is not None else _code(schema, 20)
    items = raw.get("products")
    items = [p for p in items if isinstance(p, dict)] if isinstance(items, list) else []
    alerts = raw.get("alerts")
    alerts = [a for a in alerts if isinstance(a, dict)] if isinstance(alerts, list) else []
    return {
        "_schema": schema,
        "_updated_s": parse_iso(raw.get("updated_at")),
        "app_version": _version(raw.get("app_version")),
        "page": _code(raw.get("page")),
        "running": _bool(raw.get("running")),
        "paused_reason": _code(raw.get("paused_reason")),
        "products": [_product(p) for p in items[:MAX_PRODUCTS]],
        "products_total": len(items),
        "alerts": [_alert(a) for a in alerts[-MAX_ALERTS:]],
    }


# ---- 提醒 ----------------------------------------------------------------------------------

def stuck_candidate(product, state):
    """这个商品此刻是否满足「卡住」的条件（还没算时长）：state=stopped、enabled=true，
    且闪购助手在运行（running=true）、没暂停（paused_reason=null）。"""
    return (product.get("state") == "stopped" and product.get("enabled") is True
            and state.get("running") is True and state.get("paused_reason") is None)


def attention(state):
    """顶栏按钮要不要变色：返回原因代码表，空表表示不提醒。顺序就是浮层顶部主句的优先级
    （闪购助手会话 10-27 定稿）：
    - stale：连得上，但超过 STALE_SEC 秒没更新；
    - page：page 是 login_required / captcha / error，或 page=no_list 且 running=true；
    - paused：paused_reason 不是 null 也不是 not_started（认不出的代码也算）；
    - stuck：有商品卡住（stopped 且该续开，连续 STUCK_SEC 秒，见 StatusTracker）；
    - bad_response：9334 上有东西，但没拿到能用的回话（读不出、或者连上了不回话，原因在 why）。
    不变色：连不上（连接被拒）、正在查看、not_started、loading、no_page、enabled=false 的 stopped、
    ended_by_user、no_sale、unknown。「有没看过的报警」由前端判断，不在这里。"""
    conn = state.get("conn")
    if conn == "bad_response":
        return ["bad_response"]
    if conn != "ok":
        return []
    out = []
    if state.get("stale"):
        out.append("stale")
    page = state.get("page")
    if page in PAGE_ATTENTION or (page == "no_list" and state.get("running") is True):
        out.append("page")
    paused = state.get("paused_reason")
    if paused is not None and paused != "not_started":
        out.append("paused")
    if any(p.get("stuck") for p in state.get("products") or ()):
        out.append("stuck")
    return out


def empty_state(conn, why=None):
    """conn 不是 ok 时推给页面的形状：商品和记录清空，不留旧数据（免得中控照着几分钟前的状态做事）。
    why 只在 bad_response 时有：fetch_status 给的原因代码（页面按它挑一句话，no_answer 单独一句）。"""
    state = {
        "conn": conn, "why": why if conn == "bad_response" else None, "stale": False, "updated_ms": None,
        "app_version": None, "page": None, "running": None, "paused_reason": None, "products": [],
        "products_total": 0, "alerts": [], "stuck_count": 0,
    }
    state["attention"] = attention(state)
    return state


def _rank(product):
    if product.get("stuck"):
        return _RANK_STUCK
    return _RANK.get(product.get("state"), _RANK_OTHER)


class StatusTracker:
    """把每次取 /status 的结果变成推给页面的 dict。跨轮询记着这几样，计时一律用单调时钟：

    - updated_at 这个值（认不出、null 都当 None）从哪一刻起没变过：同一个值连续超过 STALE_SEC 秒算陈旧。
      不拿本机墙钟去减 updated_at：合盖睡眠时单调时钟不走（macOS 上是 mach_absolute_time）、墙钟照走，
      醒来第一跳闪购助手多半还没刷新，墙钟一减就是睡了多久，会白白变一次色；墙钟往前往后调也不受影响。
      代价是刚连上时要先看满 15 秒才判得出陈旧（哪怕它早就停了），比起误报这点晚可以接受；
    - 每个商品（按 product_id）从哪一刻起满足「卡住」的条件（_stuck_clock）。商品暂时不在列表里时**不清零**，
      接着计：闪购助手 0.8.0 起每翻完一轮刷新一次 Buy now，列表先变短再补回，按旧做法排在后面的商品
      每轮都被清零一次，真卡住的永远攒不满 60 秒。清零只在这几种时候：看到它不再满足条件（state 不是 stopped、
      enabled 不是 true、skip 不为 null）；全局条件不满足（running 不是 true、paused_reason 不是 null），
      这时不在列表里的也一起清；连续两次没查到（见下一条）。STUCK_FORGET_SEC 秒都没再出现的丢掉
      （隔了这么久才回来的从头计），表最多记 STUCK_TRACK_MAX 个，多了先丢最久没出现的。
      没有 product_id 的商品记不了时长，永远不算卡住；
    - 连续几次没查到（连不上、读不出）。连上之后**第一次**没查到先不发布：照旧交回上一份状态，两种计时都留着，
      调用方照「连上」的节奏 3 秒后再查。连续第二次才发布那份空状态、计时清零。一次抖动（偶尔一次超时、
      事件循环卡了一下）不至于清空浮层、把按钮染橙、让轮询退到 15 秒、把快满 60 秒的卡住计时清零；
      真的退出了也只晚 3 秒说。

    不广播、不打印。schema / why 留给调用方打终端行。"""

    def __init__(self, stale_sec=STALE_SEC, stuck_sec=STUCK_SEC):
        self.stale_sec = stale_sec
        self.stuck_sec = stuck_sec
        self._seen = None             # (updated_at 的值, 从哪一刻起一直是它)；None = 还没连上过 / 刚清零
        self._stopped_since = {}      # product_id → [从哪一刻起满足卡住条件, 最后一次在列表里看到它]
        self._misses = 0              # 连续几次没查到
        self._last = None             # 上一次交出去的状态
        self.schema = None            # 最近一次连上时读到的 schema
        self.why = None               # 最近一次发布的 bad_response 的原因代码

    def update(self, kind, value, mono):
        norm = normalize(value) if kind == "ok" else None
        if kind == "ok" and norm is None:
            kind, value = "bad_response", "not_object"
        if kind != "ok":
            self._misses += 1
            if self._misses == 1 and self._last is not None and self._last["conn"] == "ok":
                return self._last
            self._seen = None
            self._stopped_since = {}
            self.why = value if kind == "bad_response" else None
            state = empty_state(kind if kind in ("unreachable", "bad_response") else "unreachable", self.why)
            self._last = state
            return state
        self._misses = 0
        self.why = None
        self.schema = norm.pop("_schema")
        updated = norm.pop("_updated_s")
        if self._seen is None or self._seen[0] != updated:
            self._seen = (updated, mono)
        stale = mono - self._seen[1] > self.stale_sec
        self._stuck_clock(norm, mono)
        norm["products"].sort(key=_rank)
        state = dict(norm, conn="ok", why=None, stale=stale,
                     updated_ms=int(round(updated * 1000)) if (stale and updated is not None) else None,
                     stuck_count=sum(1 for p in norm["products"] if p["stuck"]))
        state["attention"] = attention(state)
        self._last = state
        return state

    def _stuck_clock(self, norm, mono):
        """给这一跳的每个商品标 stuck，顺手维护 _stopped_since（规则见类的文档串）。同一个 id 出现几次时，
        有一次满足条件就算满足（不让同 id 的另一张卡片把计时清掉）。"""
        table = self._stopped_since
        if not (norm.get("running") is True and norm.get("paused_reason") is None):
            table.clear()                 # 全局条件不满足：不在列表里的也一起清
        present, candidates = set(), set()
        for product in norm["products"]:
            pid = product["id"]
            if pid is None:
                continue
            present.add(pid)
            if stuck_candidate(product, norm):
                candidates.add(pid)
        for pid in present - candidates:
            table.pop(pid, None)          # 看到它不再满足条件
        for pid in candidates:
            entry = table.get(pid)
            if entry is None or mono - entry[1] > STUCK_FORGET_SEC:
                table[pid] = [mono, mono]     # 新来的，或隔了太久才回来：从这一刻起计
            else:
                entry[1] = mono
        for product in norm["products"]:
            pid = product["id"]
            product["stuck"] = (pid in candidates and stuck_candidate(product, norm)
                                and mono - table[pid][0] >= self.stuck_sec)
        for pid in [k for k, (_start, seen) in table.items() if mono - seen > STUCK_FORGET_SEC]:
            del table[pid]
        if len(table) > STUCK_TRACK_MAX:
            keep = sorted(table, key=lambda k: table[k][1], reverse=True)[:STUCK_TRACK_MAX]
            self._stopped_since = {k: table[k] for k in keep}
