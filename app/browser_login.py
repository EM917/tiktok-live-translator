"""借用浏览器里现成的 TikTok 登录：读得到就拼 Cookie 头，读不到就说清楚是哪一种读不到。

2026-09-17 实测（macOS 27，房间 @daisycabral_ 在播）：五层解析全部没拿到地址，
两个借登录的层记下的都是「no_cookie」。用同一个库单独量出来的却是两回事：
Chrome 是 FileNotFoundError（系统不让列它的数据目录，yt-dlp 于是找不到 cookie 库），
Safari 是 PermissionError。日志写「没有 cookie」，实际是「系统拒绝读取」——中控
照着日志去登录 TikTok 是白忙。所以这里把「读不到」分开记。

两条铁律：
  * cookie 的**值**只拼进发给 TikTok 的 Cookie 头，绝不进日志、print、审计、异常文本。
    这里对外给出的只有代码（ASCII 短词）和 cookie 的**名字**。
  * probe() 供启动自检用：只看读不读得到、有没有登录 cookie 的名字，绝不解密——
    解密 Chrome 的 cookie 值要向钥匙串要「Chrome Safe Storage」，启动时会弹对话框。
"""
import os
import sqlite3
import struct
from collections import namedtuple
from pathlib import Path

# TikTok 登录后才有的 cookie 名字
LOGIN_COOKIE_NAMES = ("sessionid", "sessionid_ss", "sid_tt", "sid_guard")

# 进审计 resolve 记录的代码：短、ASCII、别改名
OK = "ok"                              # 读到了 TikTok 登录
BLOCKED = "blocked_by_system"          # 系统拒绝读取（PermissionError，或数据目录在却不让列）
NO_DATA = "no_browser_data"            # 没装，或没有 cookie 库
NO_TIKTOK = "no_tiktok_cookie"         # 读得到，里面没有 tiktok.com 的 cookie
NOT_LOGGED_IN = "not_logged_in"        # 有 tiktok.com 的 cookie，但没有登录 cookie
KEYCHAIN_WAIT = "keychain_wait"        # 读取在预算内没有返回（resolver.BROWSER_ATTEMPT_TIMEOUT）
READABLE = "readable"                  # 只有 probe() 会给：读得到，但没能看出有没有登录
NOT_READ = "not_read"                  # 测试环境 / TLT_NO_BROWSER：没有去读

LoginRead = namedtuple("LoginRead", "header code")

LABELS = {"chrome": "Chrome", "safari": "Safari", "firefox": "Firefox",
          "edge": "Edge", "brave": "Brave", "chromium": "Chromium"}

_CHROMIUM_DIRS = {"chrome": "Google/Chrome", "edge": "Microsoft Edge",
                  "brave": "BraveSoftware/Brave-Browser", "chromium": "Chromium"}
_SAFARI_FILES = ("Library/Cookies/Cookies.binarycookies",
                 "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies")


def _guarded():
    """测试里绝不碰真实的浏览器数据（PYTEST_CURRENT_TEST），TLT_NO_BROWSER 同理。"""
    return bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TLT_NO_BROWSER"))


def _home():
    """浏览器数据所在的用户目录；不该读的时候返回 None。测试要验证分类逻辑就
    monkeypatch 这个函数指到临时目录。"""
    return None if _guarded() else os.path.expanduser("~")


def _load_extractor():
    """yt-dlp 的 cookie 读取函数（**会解密**，Chrome 上会向钥匙串要密钥）。不该读的
    时候返回 None。测试 monkeypatch 这个函数换成假的。"""
    if _guarded():
        return None
    from yt_dlp.cookies import extract_cookies_from_browser
    return extract_cookies_from_browser


def error_code(exc):
    """归不了类的异常只留类名：异常文本不往外带（里面是什么我们管不了）。"""
    return "error:" + type(exc).__name__


# ---- 数据目录 ----------------------------------------------------------

def store_dirs(browser):
    """这个浏览器放 cookie 的目录（macOS）。其它平台返回空：「目录在却不让列」是
    macOS 隐私保护的表现，别的平台没有这一类。"""
    import sys

    home = _home()
    if home is None or sys.platform != "darwin":
        return ()
    if browser == "safari":
        return tuple(os.path.dirname(os.path.join(home, f)) for f in _SAFARI_FILES)
    if browser == "firefox":
        return (os.path.join(home, "Library/Application Support/Firefox/Profiles"),)
    sub = _CHROMIUM_DIRS.get(browser)
    return (os.path.join(home, "Library/Application Support", sub),) if sub else ()


def _listing_refused(path):
    """目录在，但系统不让列。macOS 27 上 Chrome 就是这个样子：os.walk 把列目录的
    错误吞掉，yt-dlp 看到的是「找不到 cookie 库」。"""
    try:
        os.listdir(path)
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def _missing_store_code(browser):
    if any(_listing_refused(d) for d in store_dirs(browser)):
        return BLOCKED
    return NO_DATA


def classify_exception(browser, exc):
    """读 cookie 时抛出的异常 → 代码。只看类型、errno 和一句固定的系统原话，
    不把异常文本带出去。"""
    import errno

    if (isinstance(exc, PermissionError)
            or getattr(exc, "errno", None) in (errno.EPERM, errno.EACCES)
            or "operation not permitted" in str(exc).lower()):
        return BLOCKED
    if isinstance(exc, FileNotFoundError):
        return _missing_store_code(browser)
    return error_code(exc)


def classify_stderr(browser, text):
    """`yt-dlp --cookies-from-browser` 子进程的 stderr → 代码；stderr 说的不是读
    cookie 的事就返回 None（这一层不知道登录状态，不硬给一个）。"""
    for line in (text or "").splitlines():
        low = line.lower()
        if "cookie" not in low:
            continue
        if "operation not permitted" in low or "permission denied" in low:
            return BLOCKED
        if "could not find" in low and "cookies database" in low:
            return _missing_store_code(browser)
    return None


# ---- 读取（会解密）：解析流地址时用 ---------------------------------------

def _is_tiktok(domain):
    return bool(domain) and "tiktok.com" in domain


def read_login(browser):
    """读这个浏览器的 TikTok cookie，返回 LoginRead(header, code)。

    header：拼好的 Cookie 头（只发给 TikTok），读不到为 None。有 tiktok.com 的 cookie
    但没有登录 cookie 时 header 照旧给出（和以前一样带上去），code 记 not_logged_in。"""
    try:
        extractor = _load_extractor()
    except Exception as exc:
        return LoginRead(None, error_code(exc))
    if extractor is None:
        return LoginRead(None, NOT_READ)
    try:
        jar = extractor(browser)
        pairs, names = [], set()
        for c in jar:
            if _is_tiktok(c.domain):
                pairs.append("{}={}".format(c.name, c.value))
                names.add(c.name)
    except Exception as exc:
        return LoginRead(None, classify_exception(browser, exc))
    if not pairs:
        return LoginRead(None, NO_TIKTOK)
    logged_in = any(n in names for n in LOGIN_COOKIE_NAMES)
    return LoginRead("; ".join(pairs), OK if logged_in else NOT_LOGGED_IN)


# ---- 探测（不解密）：启动自检用 -------------------------------------------

def _names_code(names):
    if names is None:
        return READABLE
    if not names:
        return NO_TIKTOK
    return OK if any(n in names for n in LOGIN_COOKIE_NAMES) else NOT_LOGGED_IN


def _newest(paths):
    best, best_m = None, None
    for p in paths:
        try:
            m = os.stat(p).st_mtime
        except OSError:
            continue
        if best is None or m > best_m:
            best, best_m = p, m
    return best


def _sqlite_names(path, sql):
    """只读 + immutable 打开（不加锁、不建 -wal/-journal，浏览器开着也能读），只查名字。
    immutable 不看 WAL：刚登录、还没落进主库的 cookie 这里看不到。"""
    uri = Path(path).as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return {str(row[0]) for row in conn.execute(sql)}
    finally:
        conn.close()


def _probe_sqlite(root, relative_candidates, sql):
    entries = os.listdir(root)          # PermissionError 交给 probe() 归类
    found = []
    for entry in entries:
        for rel in relative_candidates:
            p = os.path.join(root, entry, rel)
            if os.path.isfile(p):
                found.append(p)
    db = _newest(found)                 # 和 yt-dlp 一样取最新的那个库
    if db is None:
        return NO_DATA
    with open(db, "rb") as f:           # 先确认文件本身让不让读（只读 16 字节文件头）
        f.read(16)
    return _names_code(_sqlite_names(db, sql))


def _safari_names(data):
    """Cookies.binarycookies 里 tiktok.com 的 cookie 名字。这个文件不加密，不碰钥匙串。
    格式读不懂返回 None（只能说「读得到」）。"""
    def cstr(buf, start):
        end = buf.index(b"\x00", start)
        return buf[start:end].decode("utf-8", "replace")

    try:
        if data[:4] != b"cook":
            return None
        pages = struct.unpack(">I", data[4:8])[0]
        sizes = struct.unpack(">{}I".format(pages), data[8:8 + 4 * pages])
        names, cursor = set(), 8 + 4 * pages
        for size in sizes:
            page = data[cursor:cursor + size]
            cursor += size
            count = struct.unpack("<I", page[4:8])[0]
            for off in struct.unpack("<{}I".format(count), page[8:8 + 4 * count]):
                length = struct.unpack("<I", page[off:off + 4])[0]
                rec = page[off:off + length]
                domain_off, name_off = struct.unpack("<II", rec[16:24])
                if _is_tiktok(cstr(rec, domain_off)):
                    names.add(cstr(rec, name_off))
        return names
    except (struct.error, ValueError, IndexError):
        return None


def _probe_safari(home):
    files = [os.path.join(home, f) for f in _SAFARI_FILES]
    present = [f for f in files if os.path.isfile(f)]
    if not present:
        return _missing_store_code("safari")
    with open(present[0], "rb") as f:
        data = f.read()
    return _names_code(_safari_names(data))


def probe(browser):
    """这个浏览器的 cookie 库读不读得到、里面有没有 TikTok 登录 cookie 的**名字**。
    不解密、不调 yt-dlp 的读取函数、不碰钥匙串。返回代码。"""
    home = _home()
    if home is None:
        return NOT_READ
    try:
        if browser == "safari":
            return _probe_safari(home)
        dirs = store_dirs(browser)
        if not dirs:
            return NO_DATA
        if not os.path.isdir(dirs[0]):
            return NO_DATA
        if browser == "firefox":
            return _probe_sqlite(dirs[0], ("cookies.sqlite",),
                                 "SELECT DISTINCT name FROM moz_cookies "
                                 "WHERE host LIKE '%tiktok.com'")
        return _probe_sqlite(dirs[0], (os.path.join("Network", "Cookies"), "Cookies"),
                             "SELECT DISTINCT name FROM cookies "
                             "WHERE host_key LIKE '%tiktok.com'")
    except Exception as exc:
        return classify_exception(browser, exc)


# ---- 给中控看的话：只写观察和能照做的一步 ----------------------------------

FDA_OBSERVED = "系统不允许本程序读取浏览器数据。"
FDA_STEPS = ("到「系统设置」→「隐私与安全性」→「完全磁盘访问权限」，点「+」加入"
             "「TikTok Live Translator」并打开开关（用 Start.command 启动的话把「终端」"
             "也加进去），然后完全退出程序再打开。")
LOGIN_STEPS = "在 Chrome（或 Safari）里登录 TikTok 后再试。"
KEYCHAIN_STEPS = "屏幕上如果有「钥匙串」对话框，点「始终允许」后再试。"

_OBSERVED = {
    OK: "读到了 TikTok 登录",
    BLOCKED: "系统拒绝读取",
    NO_DATA: "没有找到它的 cookie 数据",
    NO_TIKTOK: "能读取，里面没有 tiktok.com 的 cookie",
    NOT_LOGGED_IN: "能读取，没有 TikTok 登录 cookie（sessionid 等）",
    KEYCHAIN_WAIT: "读取在限时内没有返回",
    READABLE: "能读取，没能看出有没有登录 TikTok",
    NOT_READ: "未读取",
}


def label(browser):
    return LABELS.get(browser, str(browser))


def describe(code):
    code = str(code)
    if code.startswith("error:"):
        return "读取出错（{}）".format(code[len("error:"):])
    return _OBSERVED.get(code, "读取结果 {}".format(code))


def observed_text(login):
    """{浏览器: 代码} → 「Chrome：系统拒绝读取；Safari：系统拒绝读取」。"""
    return "；".join("{}：{}".format(label(b), describe(c)) for b, c in (login or {}).items())


def steps_text(login):
    """和这些观察对应的、能照做的步骤（可能不止一步）。"""
    codes = set((login or {}).values())
    steps = []
    if BLOCKED in codes:
        steps.append(FDA_OBSERVED + FDA_STEPS)
    if codes & {NOT_LOGGED_IN, NO_TIKTOK, NO_DATA}:
        steps.append(LOGIN_STEPS)
    if KEYCHAIN_WAIT in codes:
        steps.append(KEYCHAIN_STEPS)
    if not steps and any(str(c).startswith("error:") for c in codes):
        steps.append("可以把这条信息反馈给开发者。")
    return "".join(steps)


def browser_only_advice(login):
    """解析以 kind=browser_only 收场时，附在固定话术后面的那一段：两个借登录的层
    看到了什么、对应能做什么。没有观察（没试浏览器）返回空串。"""
    login = {b: c for b, c in (login or {}).items() if c != NOT_READ}
    if not login:
        return ""
    borrowed = [b for b, c in login.items() if c == OK]
    if borrowed:
        return "已借用 {} 里的 TikTok 登录再试，TikTok 仍然没有给出流地址。".format(
            "、".join(label(b) for b in borrowed))
    return "借用浏览器里的 TikTok 登录时观察到——{}。{}".format(
        observed_text(login), steps_text(login))
