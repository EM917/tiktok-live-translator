"""闪购助手（本机另一个程序）：找程序、发 GET /status、把回话规整成推给控制页的那份 dict；
0.8.0 起另有控制接口，这里读控制密钥、发 POST /start、/stop、/resume（见「控制」一节）。

找程序（直播助手平台轮廓 §5.2「程序名、候选位置」「本机指定位置」、§7.2 第 2 步）：先看 settings.json 的
flash_sale.app_path（本机指定的位置，configured_app 只认完整路径、名字是 APP_NAME 的），再看 /Applications、
~/Applications；按完整路径判断、按完整路径 open -a，不跑 mdfind、不按 bundle id。settings 由调用方读好交进来，
这里不读文件。

这里只有纯函数和一个记计时的小类，不依赖 Pipeline，不广播、不打印；接线在 app/pipeline_flash.py。
不是界面文件：界面文字在前端按代码映射（web/flash.js），这里只产出代码、数字和闪购助手给的数据。

接口契约（闪购助手那边定的）：
- `GET http://127.0.0.1:9334/status`，只监听本机；Host 必须是 127.0.0.1:9334 或 localhost:9334，
  aiohttp 默认发的就是前者。CORS 只放 8765，所以页面不直连，由后端来取（控制端口可能漂到 8766–8774）。
- 连不上（连接被拒）就当作闪购助手没在运行：它和专用 Chrome 同生共死，Chrome 一关它就退出。
  连得上却不回话不算「没在运行」，见 fetch_status。
- `updated_at` 每次成功判断都刷新（后台每 2 秒一次），UTC、以 Z 结尾；page=error 时不刷新；
  还没判断过时为 null。陈旧就看它多久没变，见 StatusTracker。
- 代码表（page、paused_reason、state、level，0.8.2 起的商品 skip）以契约为准；认不出的代码原样（清洗后）
  交给前端显示。旧版没有的字段（skip、position）当 null。

9334 上回话的不一定是闪购助手（本机任何进程都能占这个端口），所以读体限长、宽容解析：
不认识的键丢掉，类型不对的当 None，字符串截长、去掉控制字符。

控制（契约「控制(0.8.0 起)」）：
- 地址写死 127.0.0.1:9334（CONTROL_BASE），动作写死白名单 start / stop / resume（CONTROL_ACTIONS）；
  页面发来的消息只决定是哪一个动作，进不了地址、请求头和请求体。
- 防冒充的条件逐条照做：Host 是 127.0.0.1:9334（aiohttp 默认就这么发）；不带 Origin、不带 Sec-Fetch-*
  （aiohttp 本来就不发）；Authorization: Bearer <密钥>；Content-Type: application/json；请求体 {"source":"tlt"}。
- 密钥每次发之前读一遍（read_control_token），读不到就不发。密钥的值不进任何返回值以外的地方：这里不打印、
  不放进异常，调用方也只拿到原因代码。aiohttp 的 ClientResponseError 的 repr 里带着请求头（也就带着
  Authorization），所以 post_control 自己把 aiohttp 的异常全部换成代码，一个都不往外抛。
- 不跟随重定向、不读环境变量里的代理（trust_env=False：设了 HTTP_PROXY 的机器上，带密钥的请求不能交给代理）、
  不存 cookie；连接 2 秒、读 2 秒，总时长最多 4 秒。
"""
import asyncio
import errno
import json
import math
import os
import re
import stat
import sys
import unicodedata
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
# open 失败时从它的标准错误里读出的系统错误号最多留几个（launch-8：照实写给人，不解释），标准错误进终端时最多留几个字
OPEN_CODES_MAX = 3
OPEN_OUTPUT_SHOW = 300
# settings.json 里同传关于闪购助手的本机设置（§5.8：第 11 步迁到 tools 键）。第 2 步只有程序位置这一项，只读不写
SETTINGS_KEY = "flash_sale"
SETTINGS_APP_PATH = "app_path"
APP_PATH_MAX = 4096      # 比这还长的不是手写的路径

# 控制（契约「控制(0.8.0 起)」）。地址写死；tests/conftest.py 把它换成没人听的端口，测试绝不碰真的 9334
CONTROL_BASE = "http://127.0.0.1:9334"
CONTROL_ACTIONS = ("start", "stop", "resume")
# 发布闸（先例 i18n.I18N_ENABLED）：开始、继续会让闪购助手对观众做事，等动作记录、指令来源这些（直播助手平台轮廓
# 第 3 步）都合进来再打开（§9.2 第 8 条，用户 10-29 同意）。关着时页面不画「开始」「继续」，后端也不发；停止不受它管
# （停是安全的方向）。改成 True 就是开闸；回退就是改回 False。测闸某一边的语义要显式 monkeypatch 它
START_ENABLED = False
# 「开始」点两下，两下都要到后端（直播助手平台轮廓 §4.4、§5.6，§9.2 第 6 条，用户 10-29 同意）：页面第一下发
# {"step": "arm", "id": 编号}，第二下发 {"step": "fire", "id": 同一个编号}；后端记下 arm 到的时刻（单调时钟），fire 离它
# START_GUARD_SEC 秒以内（双击一次）或 START_CONFIRM_SEC 秒以上的、编号对不上的、前面没有 arm 的，一律不转发
# （not_confirmed）。同前端 FLASH_START_GUARD_MS、FLASH_START_CONFIRM_MS（tests/test_flash_sale.py 钉着两边一样）。
# 编号由页面生成，只用来把 fire 和它前面那个 arm 对上，不是口令
START_GUARD_SEC = 0.4
START_CONFIRM_SEC = 6
ARM_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")
CONTROL_BODY = b'{"source":"tlt"}'
CONTROL_REPLY_MAX = 4096     # 回话只有 {"ok":…,"error":…} 这么一小段
# 指令被接受以后最多等这么久看它生效，这期间每 3 秒查一次 /status（契约：开始后真实页面一般 5–15 秒 running
# 才变 true，超过 30 秒还不是就当这次没成功）
CONTROL_WAIT_SEC = 30
# 停止没被接受时再试（直播助手平台轮廓 §5.5、§9.2 第 7 条，用户 10-29 同意）：连不上、发出了没回话、闪购助手回这几种
# 「过一会儿就能收」的代码，STOP_RETRY_SEC 秒内每 STOP_RETRY_GAP_SEC 秒再试一次（闪购助手两次指令之间要隔 2 秒），
# 直到收到别的结果。awaiting_approval、captcha 不重试：要人去专用 Chrome 里处理。别的指令一律不自动重发
STOP_RETRY_SEC = 30
STOP_RETRY_GAP_SEC = 3
STOP_RETRY_CODES = ("unreachable", "no_answer", "busy", "loading", "no_response")
# flash_control_result 的 error 里后端自己的代码。其余的要么是 http_<状态码>，要么是闪购助手 409 给的代码
# （原样、清洗过）。前端按这张表认「是我们这边的」还是「闪购助手没有接受」：
#   no_status    开始、继续：最近一次 /status 不是连上并读懂了，或者不像闪购助手的回话（StatusTracker.control_refusal）；
#                停止：发之前当场读的那一次没读懂、或 schema 不是 1（stop_precheck）。都没读密钥、没发
#   no_token     控制密钥读不出：没发
#   unreachable  连不上 9334            no_answer  连上了，没有回话            bad_reply  回话读不出
#   no_control   404 / 405：这个版本没有控制接口
#   failed       后端自己出错（终端有异常的类型名）
#   not_seen     已接受，但 CONTROL_WAIT_SEC 秒内没看到生效（接受之后另发的第二条结果）
#   local_only   开始、继续来自没带 Origin 的本机进程（app/server.py CONTROL_SOURCE）：本机脚本只能停，没发
#   not_confirmed  开始没有对得上的两下（START_GUARD_SEC、START_CONFIRM_SEC）：没发
CONTROL_OWN_ERRORS = ("no_status", "no_token", "unreachable", "no_answer", "bad_reply", "no_control",
                      "failed", "not_seen", "local_only", "not_confirmed")
# 控制密钥：~/Library/Application Support/FlashSaleHelper/control-token，64 位十六进制（32 字节），权限 600
TOKEN_DIR = ("Library", "Application Support", "FlashSaleHelper")
TOKEN_NAME = "control-token"
TOKEN_READ_MAX = 1025        # 最多读这么多：读满了就是文件太长（合法的文件 64 个字符，最多再带个换行）
_TOKEN = re.compile(r"[0-9A-Fa-f]{64}")

# 一场最多 300 个商品（闪购助手会话 10-27 告知，实际见过 103 个）。300 个带长标题的商品
# 约 300 KB，读体上限留足余量，挡的只是撑爆内存的回话
MAX_BYTES = 1024 * 1024
MAX_PRODUCTS = 300
MAX_ALERTS = 20          # 契约：最多 20 条，最早的在前
MAX_TITLE = 200
MAX_TEXT = 500
MAX_CODE = 40
MAX_VERSION = 20

# 契约（0.9.0）/status 的 schema 和全部 page 代码。发控制指令（带着密钥）之前拿来认回话的像不像闪购助手
# （StatusTracker.control_refusal）；别处认不出的 page 照旧原样交给页面显示
CONTROL_SCHEMA = 1
PAGE_CODES = ("ok", "no_page", "login_required", "loading", "no_list", "captcha", "error")
PAGE_ATTENTION = ("login_required", "captcha", "error")
# 开始、继续被接受后等生效的那 30 秒里不算要人处理的 page（见 attention）
START_QUIET_PAGES = ("loading", "error", "no_list")

# 推给页面前商品的排序（闪购助手会话 10-27 定的优先级）：要人看的在前。同一档里按卡片序号 position 排、
# 没有序号的在后，再相同就保持闪购助手给的顺序（product_sort_key）。
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
    """要看的两个位置：/Applications 和 ~/Applications。只看这两处，不跑 mdfind。本机指定的位置另算（configured_app）。"""
    out = [APP_PATH]
    try:
        base = Path.home() if home is None else Path(home)
        out.append(base / "Applications" / APP_NAME)
    except (RuntimeError, KeyError, OSError):
        pass                      # 取不到家目录：只看 /Applications
    return tuple(out)


def configured_app(saved):
    """settings.json 里本机指定的位置（{"flash_sale": {"app_path": "…/闪购助手.app"}}）→ (路径, None)；没指定 → (None, None)；
    指定了却用不了 → (None, 原因代码)。saved 是 load_settings 读出来的整份（调用方读，这里不碰文件）。
    - 没有这个键、键是 null、app_path 缺了、是 null 或空串：没指定；
    - bad_value：flash_sale 不是对象，app_path 不是字符串、带 NUL、长过 APP_PATH_MAX；
    - not_absolute：展开 ~ 以后还不是完整路径；
    - wrong_name：最后一截不是 APP_NAME（§5.2：只认名字对得上的 .app。iCloud 桌面上整文件夹替换留下的「闪购助手 2.app」
      也不认：双击到旧的那份，同传显示的版本就和在跑的对不上）。
    存不存在不在这里看（find_app 看），所以打开浮层时再找，指定的位置装上了就认得。"""
    if not isinstance(saved, dict):
        return None, None
    block = saved.get(SETTINGS_KEY)
    if block is None:
        return None, None
    if not isinstance(block, dict):
        return None, "bad_value"
    value = block.get(SETTINGS_APP_PATH)
    if value is None or value == "":
        return None, None
    if not isinstance(value, str) or "\x00" in value or len(value) > APP_PATH_MAX:
        return None, "bad_value"
    try:
        expanded = os.path.expanduser(value)
        path = Path(expanded)
    except (RuntimeError, KeyError, OSError, ValueError):
        return None, "bad_value"
    if not os.path.isabs(expanded):
        return None, "not_absolute"
    if unicodedata.normalize("NFC", path.name) != unicodedata.normalize("NFC", APP_NAME):
        return None, "wrong_name"
    return path, None


def app_search_paths(candidates=None, configured=None):
    """按先后要看的位置：本机指定的（有的话）在前，再是候选位置（缺省 app_candidates()）。"""
    rest = tuple(app_candidates() if candidates is None else candidates)
    return rest if configured is None else (configured,) + rest


def find_app(platform=None, candidates=None, configured=None):
    """找到了就返回 .app 的路径（第一个是文件夹的），没找到（或不是 macOS）返回 None。configured 是本机指定的位置
    （configured_app 认过的），排在最前；不存在、不是文件夹就跳过，接着看候选位置。启动时、打开闪购浮层时各判一次
    （app/pipeline_flash.py），只看文件系统。"""
    if (sys.platform if platform is None else platform) != "darwin":
        return None
    for path in app_search_paths(candidates, configured):
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


# NSError 的「Code=-10669」（Error Domain=NSOSStatusErrorDomain Code=-10669 …，嵌套的 NSUnderlyingError 里还有一层），
# 旧系统的「LSOpenURLsWithRole() failed with error -10810」。别的数（_LSLine=4087、地址 0x600…）不算
_OPEN_CODE = re.compile(r"(?:\bCode=|\berror )(-?\d{1,10})\b")
_SPACE = re.compile(r"\s+")


def open_error_codes(text):
    """open 失败时标准错误里系统给的错误号（launch-8）：按出现的先后、去重，最多 OPEN_CODES_MAX 个，一个都没有返回 ()。
    只认上面两种写法，不解释它们是什么意思（CLAUDE.md 第八条：只报看到的）。"""
    if not isinstance(text, str):
        return ()
    out = []
    for match in _OPEN_CODE.finditer(text):
        code = int(match.group(1))
        if code not in out:
            out.append(code)
        if len(out) >= OPEN_CODES_MAX:
            break
    return tuple(out)


def open_output_text(raw):
    """open 写到标准错误的字节 → 进终端的一行：按 UTF-8 解（坏字节换成 U+FFFD），空白并成一个空格，去掉控制字符，
    长过 OPEN_OUTPUT_SHOW 个字就截断、末尾加「…」。None、空的给空串。"""
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    text = _TEXT_DROP.sub("", _SPACE.sub(" ", text)).strip()
    return text if len(text) <= OPEN_OUTPUT_SHOW else text[:OPEN_OUTPUT_SHOW] + "…"


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


# ---- 控制：读密钥、发 POST ------------------------------------------------------------------

def control_token_path(home=None):
    """控制密钥文件的位置；取不到家目录返回 None。每次调用时现算、不写成模块常量：tests/conftest.py 把这个函数
    换成指向不存在的路径，测试里任何一条路（包括漏传路径的调用）都读不到本机真的密钥。"""
    try:
        base = Path.home() if home is None else Path(home)
    except (RuntimeError, KeyError, OSError):
        return None
    return base.joinpath(*TOKEN_DIR, TOKEN_NAME)


def read_control_token(path=None):
    """读控制密钥 → (密钥, None)，读不到 → (None, 原因代码)。每次发控制请求之前调一次（契约：每次请求前读一遍，
    删掉文件后闪购助手下次启动会换一个新的）。

    - O_NOFOLLOW 打开：文件本身是符号链接就不读（symlink），免得被换成指向别处的链接；O_NONBLOCK 让管道、
      设备文件打开时不卡住事件循环，打开后 fstat 确认是普通文件（not_file；Windows 打开目录直接被拒，也算 not_file）；
    - 最多读 TOKEN_READ_MAX 字节，读满就是太长（too_long）；按 ASCII 解码，去掉首尾空白和换行后必须正好
      64 位十六进制（format）；
    - 不存在 missing，取不到家目录 no_home，其余打不开、读出错的 unreadable。
    原因代码里绝不带文件内容。不查权限位：读的一方查 600 并不能让密钥更安全（能读到这个文件的进程本来就能读）。"""
    path = control_token_path() if path is None else path
    if path is None:
        return None, "no_home"
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return None, "missing"
        if exc.errno in (errno.ELOOP, errno.EMLINK):     # O_NOFOLLOW 碰上符号链接：macOS、Linux 是 ELOOP
            return None, "symlink"
        if exc.errno == errno.EACCES and os.path.isdir(path):   # Windows 打开目录是 EACCES（macOS、Linux 打得开，下面 fstat 判）
            return None, "not_file"
        return None, "unreadable"
    except (TypeError, ValueError):
        return None, "unreadable"
    data = b""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "not_file"
        while len(data) < TOKEN_READ_MAX:
            chunk = os.read(fd, TOKEN_READ_MAX - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)
    if len(data) >= TOKEN_READ_MAX:
        return None, "too_long"
    try:
        text = data.decode("ascii").strip(" \t\r\n")
    except UnicodeDecodeError:
        return None, "format"
    if not _TOKEN.fullmatch(text):
        return None, "format"
    return text, None


def control_url(action, base=None):
    """动作 → 控制地址。动作只认 CONTROL_ACTIONS 里的原样字符串（大小写、路径、参数一概不认），不认就抛 ValueError
    （异常文字里不带传进来的东西）。base 缺省时在调用这一刻取 CONTROL_BASE（同 fetch_status 的 STATUS_URL）。"""
    if not isinstance(action, str) or action not in CONTROL_ACTIONS:
        raise ValueError("not a control action")
    return (CONTROL_BASE if base is None else base) + "/" + action


def _json_object(body):
    if not isinstance(body, (bytes, bytearray)):
        return None
    try:
        doc = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def control_reply(status, body, action):
    """控制请求的回话 → None（已接受）或错误代码：
    - 200 且 {"ok": true}（带了 accepted 就得是这个动作）→ None；200 但内容不是这样 → bad_reply；
    - 409 → 闪购助手给的代码（同 /status 的代码字段清洗，认不出的原样交给前端）；没有能读的代码 → http_409；
    - 404、405 → no_control：这个版本的闪购助手没有控制接口（0.8.0 以前只有 GET /status）；
    - 其余（403 防冒充没过、3xx 不跟随、5xx……）→ http_<状态码>。"""
    doc = _json_object(body)
    if status == 200:
        if doc is None or doc.get("ok") is not True or doc.get("accepted", action) != action:
            return "bad_reply"
        return None
    if status == 409:
        return (_code(doc.get("error")) if doc is not None else None) or "http_409"
    if status in (404, 405):
        return "no_control"
    return "http_%d" % status


async def post_control(action, token, base=None, timeout=HTTP_TIMEOUT_SEC, limit=CONTROL_REPLY_MAX):
    """发一次 POST /<action> → None（闪购助手接受了，200）或错误代码。只发不等：契约里接口马上返回，
    之后的进展看 /status。

    错误代码：control_reply 的那几种；no_token（传进来的不是 64 位十六进制，不发）；unreachable（连不上，
    或连接阶段就超时）；no_answer（连上了，timeout 秒内没回完，或没回话就断开）；bad_reply（回的不是 HTTP、
    回到一半断开、太长）。aiohttp 的异常一律在这里换成代码、不往外抛：它们的 repr 里带着请求头，也就带着密钥。
    动作不在白名单里照样抛 ValueError（调用方先查过，走不到这里）。取消照样往外抛。

    请求：Host 127.0.0.1:<端口>（aiohttp 默认），Authorization: Bearer <密钥>，Content-Type: application/json，
    请求体 {"source":"tlt"}；不带 Origin、Sec-Fetch-*、Cookie（DummyCookieJar）；不跟随重定向；
    trust_env=False（不读环境变量里的代理，也不读 .netrc）；连接、读各 timeout 秒，总时长最多 2×timeout。
    每次一个短命 session，同 fetch_status。"""
    import aiohttp
    from .nethttp import read_all

    url = control_url(action, base)
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return "no_token"
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    timeouts = aiohttp.ClientTimeout(total=timeout * 2, connect=timeout, sock_read=timeout)
    connect_timeout = getattr(aiohttp, "ConnectionTimeoutError", None) or _NeverRaised
    try:
        async with aiohttp.ClientSession(timeout=timeouts, trust_env=False,
                                         cookie_jar=aiohttp.DummyCookieJar()) as session:
            async with session.post(url, data=CONTROL_BODY, headers=headers, allow_redirects=False) as resp:
                status = resp.status
                body = await read_all(resp, limit)
    except asyncio.CancelledError:
        raise
    # 先后同 fetch_status：ClientConnectorError 是 ClientOSError 的子类，ConnectionTimeoutError 是
    # asyncio.TimeoutError 的子类，都得排在前面
    except aiohttp.ClientConnectorError:
        return "unreachable"
    except connect_timeout:
        return "unreachable"
    except (asyncio.TimeoutError, aiohttp.ServerDisconnectedError, aiohttp.ClientOSError):
        return "no_answer"
    except (aiohttp.ClientPayloadError, aiohttp.ClientResponseError):
        return "bad_reply"
    except (aiohttp.ClientError, OSError, ValueError):
        return "unreachable"
    if body is None:
        return "bad_reply"
    return control_reply(status, body, action)


def status_refusal(kind, value):
    """一次 /status（fetch_status 的 (kind, value)）像不像闪购助手的回话：None = 像；否则是原因代码，同
    StatusTracker.control_refusal 的三条：not_ok（没连上、没读懂、不是 JSON 对象）、schema（schema 不是 CONTROL_SCHEMA）、
    page（page 不是契约里的代码）。启动时没找到 .app 的话，凭这一次决定要不要照样显示闪购按钮（launch-7，用户选 B）。"""
    norm = normalize(value) if kind == "ok" else None
    if norm is None:
        return "not_ok"
    if norm["_schema"] != CONTROL_SCHEMA:
        return "schema"
    if norm["page"] not in PAGE_CODES:
        return "page"
    return None


def stop_precheck(kind, value):
    """停止发之前当场读的那一次 /status（fetch_status 的 (kind, value)）→ None（可以读密钥、发）或
    (错误代码, 终端带的原因, 要不要再试)：
    - 连不上 → ("unreachable", None, True)；
    - 连得上，没拿到能用的回话（bad_response，或者回的不是 JSON 对象）→ ("no_status", 原因代码, True)；
    - 读懂了，schema 不是 CONTROL_SCHEMA（缺了也算）→ ("no_status", "schema", False)：回话不像闪购助手的（同
      StatusTracker.control_refusal 的 schema 那一条），密钥不给它，也不再试。
    page 不看：认不出的 page 代码只影响显示，不挡停止（直播助手平台轮廓 §5.5）。不看上一次轮询读得怎么样
    （StatusTracker.control_refusal 只管开始、继续）：读失败一次以后要隔 15 秒才再读，这 15 秒里停止不该被挡回来。"""
    if kind == "unreachable":
        return "unreachable", None, True
    norm = normalize(value) if kind == "ok" else None
    if norm is None:
        why = value if kind == "bad_response" and isinstance(value, str) else "not_object"
        return "no_status", why, True
    if norm["_schema"] != CONTROL_SCHEMA:
        return "no_status", "schema", False
    return None


def stop_should_retry(error):
    """停止的这个结果要不要再试：STOP_RETRY_CODES 里的才再试（接受了、别的拒绝、出错都不再试）。"""
    return error in STOP_RETRY_CODES


def control_done(action, state):
    """推给页面的这份状态说明这个动作已经生效了吗（等生效的那 30 秒里每一跳查一次，生效了就收掉）：
    - start、resume：running 是 true，且 paused_reason 不是 user_interaction（继续要的就是从这种暂停里出来；
      开始的时候本来不会是它：暂停着点开始，闪购助手回 409 paused）；
    - stop：running 是 false。
    只认 conn=ok 的状态：连不上、读不出时看不到，不算生效。前端 flash.js flashControlDone 一字不差，
    tests/fixtures/flash_sale/shared-rules.json 的 control_done 两边各测一遍。"""
    if not isinstance(state, dict) or state.get("conn") != "ok":
        return False
    running = state.get("running")
    if action == "stop":
        return running is False
    if action in ("start", "resume"):
        return running is True and state.get("paused_reason") != "user_interaction"
    return False


def control_settled(action, state):
    """这个动作生效了以后（control_done），等生效的那个窗口能不能收（app/pipeline_flash.py _flash_check_control）：
    - stop：生效就收（停止没有安静窗口）；
    - start、resume：还要等页面稳下来，连上了（conn=ok）、不陈旧、page 不在 START_QUIET_PAGES 里。POST /start 刷新
      页面，闪购助手刷新完自己接着运行，running 会先变 true：刷新期间报 loading（updated_at 可能停着），列表回来之前
      是 no_list。只看 running 就收的话，no_list 加 running=true 马上变橙、出红字（见 attention），开始后的那段过渡
      就白设了（2026-10-28 审查）。一直稳不下来的，等满 CONTROL_WAIT_SEC 照样收，之后照原规则提醒。
    只给后端用：页面不判这个，跟着后端发来的 control 走（control 收掉之前 flashStarting 一直为真）。"""
    if action not in ("start", "resume"):
        return True
    if not isinstance(state, dict) or state.get("conn") != "ok":
        return False
    return not state.get("stale") and state.get("page") not in START_QUIET_PAGES


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
        # 闪购助手 0.8.2 起才有，旧版没有这两个键：都当 null。position 是卡片左边的序号（最后一次在屏幕上
        # 看到这张卡片时的值），skip 是「故意不续开」的原因代码（slot_1、no_stock、max_renewals、
        # auto_start_off、no_position，0.9.2 起还有 next_to_pin，0.9.2.2 起改成 top_n、不再发 slot_1 和 next_to_pin；认不出的原样交给前端）。哪个商品标 slot_1、
        # next_to_pin、no_position，0.9.2 和 0.9.2.1 不一样（0.9.2.1 起有置顶时只标 next_to_pin，没置顶（或看不出）时才标
        # slot_1、no_position；docs/flash-sale.md §3），这里不分版本：不为 null 就不算卡住
        "position": _int(raw.get("position")),
        "skip": _code(raw.get("skip")),
    }


def _alert(raw):
    return {"at_ms": _ms(raw.get("at")), "level": _code(raw.get("level")),
            "text": _text(raw.get("text"), MAX_TEXT)}


def normalize(raw):
    """/status 的 JSON → 规整后的 dict；raw 不是对象返回 None（调用方记成 bad_response / not_object）。

    带两个以 _ 开头的内部键，不发给页面：_schema（终端提示用）、_updated_s（判陈旧用）。
    商品保持闪购助手给的顺序、最多 MAX_PRODUCTS 个（排序由 StatusTracker 做，它要先算 stuck，见 product_sort_key）；
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
    """这个商品此刻是否满足「卡住」的条件（还没算时长）：state=stopped、enabled=true、skip=null，
    且闪购助手在运行（running=true）、没暂停（paused_reason=null）。skip 不为 null 是闪购助手说了
    故意不续开（1 号位、没库存……），停着是它定的，不是卡住。前端 flash.js flashWaitingRenewal 和这里一字不差，
    tests/fixtures/flash_sale/shared-rules.json 的 waiting_renewal 两边各测一遍。"""
    return (product.get("state") == "stopped" and product.get("enabled") is True
            and product.get("skip") is None
            and state.get("running") is True and state.get("paused_reason") is None)


def starting(state):
    """开始或继续已被闪购助手接受、还在等它生效（control 是 {"action": "start" 或 "resume", "phase": "waiting"}；
    等生效的窗口最多 CONTROL_WAIT_SEC 秒：生效了而且页面稳下来了（control_settled）、或等满了，app/pipeline_flash.py
    就把 control 收掉。running 已经变 true、页面还在刷新的那几秒仍算在里面）。
    前端 flash.js flashStarting 一字不差，tests/fixtures/flash_sale/shared-rules.json 的 attention 两边各测一遍。"""
    ctl = state.get("control") if isinstance(state, dict) else None
    return (isinstance(ctl, dict) and ctl.get("phase") == "waiting"
            and ctl.get("action") in ("start", "resume"))


def attention(state):
    """顶栏按钮要不要变色：返回原因代码表，空表表示不提醒。顺序就是浮层顶部主句的优先级
    （闪购助手会话 10-27 定稿）：
    - stale：连得上，但超过 STALE_SEC 秒没更新；
    - page：page 是 login_required / captcha / error，或 page=no_list 且 running=true；
    - paused：paused_reason 不是 null 也不是 not_started（认不出的代码也算）；
    - stuck：有商品卡住（stopped 且该续开，连续 STUCK_SEC 秒，见 StatusTracker）；
    - bad_response：9334 上有东西，但没拿到能用的回话（读不出、或者连上了不回话，原因在 why）。
    不变色：连不上（连接被拒）、正在查看、not_started、loading、no_page、enabled=false 的 stopped、
    ended_by_user、no_sale、unknown。「有没看过的报警」由前端判断，不在这里。

    开始、继续被接受后等生效的那 30 秒（starting）里，stale 和 page 是 START_QUIET_PAGES 的不算：POST /start
    先刷新页面，刷新期间 /status 会短暂报 loading、error（这时 updated_at 不刷新，15 秒后还会变陈旧），列表回来之前
    是 no_list。用户 10-27 定的：这段时间浮层写「正在开始…」，按钮不变橙、不出红字，免得刚点完开始看到红字又去点
    停止。running 变 true（生效）不收这个窗口：刷新以后闪购助手接着运行，running 先变 true、列表还没回来，要等页面
    稳下来（control_settled）。等满 30 秒还这样（control 收掉了），照原规则提醒。stale、page 这两个观察照旧留在状态里（stale、page
    字段不改），只是不进这张表。login_required、captcha、暂停、卡住、读不出照常提醒：那是要人去处理的。"""
    conn = state.get("conn")
    if conn == "bad_response":
        return ["bad_response"]
    if conn != "ok":
        return []
    quiet = starting(state)
    out = []
    if state.get("stale") and not quiet:
        out.append("stale")
    page = state.get("page")
    if quiet and page in START_QUIET_PAGES:
        pass
    elif page in PAGE_ATTENTION or (page == "no_list" and state.get("running") is True):
        out.append("page")
    paused = state.get("paused_reason")
    if paused is not None and paused != "not_started":
        out.append("paused")
    if any(p.get("stuck") for p in state.get("products") or ()):
        out.append("stuck")
    return out


def empty_state(conn, why=None):
    """conn 不是 ok 时推给页面的形状：商品和记录清空，不留旧数据（免得中控照着几分钟前的状态做事）。
    why 只在 bad_response 时有：fetch_status 给的原因代码（页面按它挑一句话，no_answer 单独一句）。
    control 是控制指令的进度，这里和 StatusTracker 一律给 None；app/pipeline_flash.py 发布前叠上真的那一份
    （{"action", "phase"}，见那里的 _flash_compose）。"""
    state = {
        "conn": conn, "why": why if conn == "bad_response" else None, "stale": False, "updated_ms": None,
        "app_version": None, "page": None, "running": None, "paused_reason": None, "products": [],
        "products_total": 0, "alerts": [], "stuck_count": 0, "control": None,
    }
    state["attention"] = attention(state)
    return state


def _rank(product):
    if product.get("stuck"):
        return _RANK_STUCK
    return _RANK.get(product.get("state"), _RANK_OTHER)


def product_sort_key(product):
    """推给页面前商品的先后：先按 _rank 分档；同一档里按 position（卡片序号）从小到大，null 排在最后；
    再相同就保持闪购助手给的顺序（list.sort 是稳定排序）。前端 flash.js flashSortProducts 照同一个规则排，
    tests/fixtures/flash_sale/shared-rules.json 的 sort 两边各测一遍。"""
    position = product.get("position")
    known = isinstance(position, int) and not isinstance(position, bool)
    return (_rank(product), not known, position if known else 0)


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

    def last_ok(self):
        """最近一次查 /status 连上并读懂了吗。「连上以后第一次没查到」照旧交回的是上一份 ok 状态，但这里是 False。"""
        return self._misses == 0 and self._last is not None and self._last.get("conn") == "ok"

    def control_refusal(self):
        """发开始、继续（带着密钥）之前看的：None = 可以发；否则是不发的原因代码（终端用，页面一律收到 no_status）。
        停止不看这里，发之前当场读一次（stop_precheck）：
        - not_ok：最近一次查 /status 不是连上并读懂了（last_ok 为 False）。密钥只给刚刚还在好好回 /status 的那个对端；
        - schema：连上读懂了，但 schema 不是 CONTROL_SCHEMA；
        - page：连上读懂了，但 page 不是契约里的代码（PAGE_CODES；缺了也算）。
        后两条挡的是 9334 上碰巧回了一个 JSON 对象的别的服务：normalize 收任何对象，{"status":"ok"} 甚至 {} 也算
        「连上并读懂了」，契约的回话却一定带 schema=1 和七个 page 代码之一。挡不住存心冒充的进程（照着契约回就是了），
        见 docs/flash-sale.md §9.4。代价：闪购助手以后改了 schema、加了 page 代码，控制会一直 no_status，直到这里跟上
        （终端那一行带着原因；schema 不是 1 另有一行）。"""
        if not self.last_ok():
            return "not_ok"
        if self.schema != CONTROL_SCHEMA:
            return "schema"
        if self._last.get("page") not in PAGE_CODES:
            return "page"
        return None

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
        norm["products"].sort(key=product_sort_key)
        state = dict(norm, conn="ok", why=None, stale=stale,
                     updated_ms=int(round(updated * 1000)) if (stale and updated is not None) else None,
                     stuck_count=sum(1 for p in norm["products"] if p["stuck"]), control=None)
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
