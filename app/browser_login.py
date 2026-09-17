"""借用浏览器里现成的 TikTok 登录：读得到就拼 Cookie 头，读不到就说清楚是哪一种读不到。

2026-09-17 实测（macOS 27，房间 @daisycabral_ 在播）：五层解析全部没拿到地址，
两个借登录的层记下的都是「no_cookie」。用同一个库单独量出来的却是两回事：
Chrome 是 FileNotFoundError（系统不让列它的数据目录，yt-dlp 于是找不到 cookie 库），
Safari 是 PermissionError。日志写「没有 cookie」，实际是「系统拒绝读取」——中控
照着日志去登录 TikTok 是白忙。所以这里把「读不到」分开记。

同一天复查又量出三件事，都是「话说得和看到的不一样」：
  * 钥匙串不给「Chrome Safe Storage」的密钥时，yt-dlp 把解不开的 cookie 一条条丢掉、交回
    一个没有 tiktok.com 条目的 jar——这里于是说「里面没有 tiktok.com 的 cookie，去登录」，
    而同一个库里 sessionid 的名字明明在。现在读取前后各看一眼名字，对不上就记 cannot_decrypt。
  * Chrome 有好几个个人资料时，yt-dlp（不给 profile）和自检都只看最近写过的那个 Cookies
    文件，结论随 mtime 翻来翻去。现在每个个人资料都看，读取时把有登录的那个目录交给 yt-dlp。
  * 「完全磁盘访问权限」里该加的不是这个 .app：见 fda_targets()。

两条铁律：
  * cookie 的**值**只拼进发给 TikTok 的 Cookie 头，绝不进日志、print、审计、异常文本。
    这里对外给出的只有代码（ASCII 短词）和 cookie 的**名字**。
  * probe() 供启动自检用：只看读不读得到、有没有登录 cookie 的名字，绝不解密——
    解密 Chrome 的 cookie 值要向钥匙串要「Chrome Safe Storage」，启动时会弹对话框。
"""
import os
import sqlite3
import struct
import sys
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
CANNOT_DECRYPT = "cannot_decrypt"      # 库里有 tiktok.com 的 cookie 名字，解密后的结果里却没有它们
KEYCHAIN_WAIT = "keychain_wait"        # 读取在预算内没有返回（resolver.BROWSER_ATTEMPT_TIMEOUT）
READABLE = "readable"                  # 只有 probe() 会给：读得到，但没能看出有没有登录
NOT_READ = "not_read"                  # 测试环境 / TLT_NO_BROWSER：没有去读

LoginRead = namedtuple("LoginRead", "header code")

LABELS = {"chrome": "Chrome", "safari": "Safari", "firefox": "Firefox",
          "edge": "Edge", "brave": "Brave", "chromium": "Chromium"}

_CHROMIUM_DIRS = {"chrome": "Google/Chrome", "edge": "Microsoft Edge",
                  "brave": "BraveSoftware/Brave-Browser", "chromium": "Chromium"}
# 钥匙串里放 cookie 密钥的那一项（yt-dlp 向 security 命令要的就是它），对话框上写的也是这个名字
_KEYCHAIN_ITEMS = {"chrome": "Chrome Safe Storage", "edge": "Microsoft Edge Safe Storage",
                   "brave": "Brave Safe Storage", "chromium": "Chromium Safe Storage"}
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
    但没有登录 cookie 时 header 照旧给出（和以前一样带上去），code 记 not_logged_in。

    读之前先只看一眼名字（_names_view，不解密）：哪个个人资料里有 TikTok 登录就让读取函数
    读哪个；读完再对一下——库里有名字、解密后的结果里没有，是「值没能解开」，不是「没有 cookie」。"""
    try:
        extractor = _load_extractor()
    except Exception as exc:
        return LoginRead(None, error_code(exc))
    if extractor is None:
        return LoginRead(None, NOT_READ)
    seen_code, profile = _names_view(browser)
    try:
        # profile 是个人资料目录的完整路径（yt-dlp 认路径）；没看出来就不给，它自己取最新的库
        jar = extractor(browser, profile=profile) if profile else extractor(browser)
        pairs, names = [], set()
        for c in jar:
            if _is_tiktok(c.domain):
                pairs.append("{}={}".format(c.name, c.value))
                names.add(c.name)
    except Exception as exc:
        return LoginRead(None, classify_exception(browser, exc))
    header = "; ".join(pairs) or None
    if any(n in names for n in LOGIN_COOKIE_NAMES):
        return LoginRead(header, OK)
    # Chromium 系的 cookie 值是加密的，密钥在钥匙串里；拿不到密钥时 yt-dlp 不报错，只是把
    # 解不开的条目丢掉。库里看得到登录 cookie 的名字（或者看得到 tiktok.com 的名字而解密后
    # 一条都没有）——说的就不能是「没有 cookie」「没登录」。解出来的那部分照旧带上去。
    if browser in _KEYCHAIN_ITEMS and (seen_code == OK
                                       or (seen_code == NOT_LOGGED_IN and not pairs)):
        return LoginRead(header, CANNOT_DECRYPT)
    return LoginRead(header, NOT_LOGGED_IN if pairs else NO_TIKTOK)


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


_CHROMIUM_SQL = "SELECT DISTINCT name FROM cookies WHERE host_key LIKE '%tiktok.com'"
_FIREFOX_SQL = "SELECT DISTINCT name FROM moz_cookies WHERE host LIKE '%tiktok.com'"
_RANK = {OK: 3, NOT_LOGGED_IN: 2, NO_TIKTOK: 1}


def _sqlite_store(browser):
    """cookie 放在 SQLite 里的浏览器：(根目录, 每个个人资料里库的相对位置, 只查名字的 SQL)。
    Safari（不是 SQLite）、没有数据目录、不该读的时候返回 None。"""
    dirs = store_dirs(browser)
    if not dirs or browser == "safari" or not os.path.isdir(dirs[0]):
        return None
    if browser == "firefox":
        return dirs[0], ("cookies.sqlite",), _FIREFOX_SQL
    return dirs[0], (os.path.join("Network", "Cookies"), "Cookies"), _CHROMIUM_SQL


def _scan_profiles(root, relative_candidates, sql):
    """根目录下**每个**个人资料各看一遍名字 → [(代码, 库的 mtime, 个人资料目录)]。

    以前和 yt-dlp 一样只看最新的那个库：登录在「Profile 1」、最后落盘的是「Default」时，
    结论是「没有 cookie」，两个文件的 mtime 一换又变成「读到了登录」。
    某个库看不成（损坏、读不了）不挡别的；一个都没看成，把第一个异常交给调用方归类。"""
    views, first_error = [], None
    for entry in sorted(os.listdir(root)):      # 列不了：PermissionError 交给调用方归类
        profile = os.path.join(root, entry)
        db = _newest([p for p in (os.path.join(profile, rel) for rel in relative_candidates)
                      if os.path.isfile(p)])
        if db is None:
            continue
        try:
            with open(db, "rb") as f:           # 先确认文件本身让不让读（只读 16 字节文件头）
                f.read(16)
            views.append((_names_code(_sqlite_names(db, sql)), os.stat(db).st_mtime, profile))
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if not views and first_error is not None:
        raise first_error
    return views


def _best(views):
    """有登录的 > 有 tiktok.com 的 > 什么都没有的；同一档里取库最近写过的那个。"""
    return max(views, key=lambda v: (_RANK.get(v[0], 0), v[1]))


def _probe_sqlite(root, relative_candidates, sql):
    views = _scan_profiles(root, relative_candidates, sql)
    return _best(views)[0] if views else NO_DATA


def _names_view(browser):
    """(只看名字得到的代码, 该让读取函数读的个人资料目录)。不解密、不碰钥匙串、绝不抛异常：
    看不成就是 (None, None)，读取照旧进行，读不到的原因由读取函数自己的异常去归类。
    目录只在里面看得到 tiktok.com 的名字时给出——都没有，就和以前一样让 yt-dlp 取最新的库。"""
    try:
        store = _sqlite_store(browser)
        views = _scan_profiles(*store) if store else []
    except Exception:
        return None, None
    if not views:
        return None, None
    code, _mtime, profile = _best(views)
    return code, (profile if _RANK.get(code, 0) >= _RANK[NOT_LOGGED_IN] else None)


def login_profile(browser):
    """里面看得到 TikTok cookie 名字的那个个人资料目录，没有（或看不成）为 None。
    `yt-dlp --cookies-from-browser` 那一层用它，和 read_login() 读同一个个人资料。"""
    return _names_view(browser)[1]


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
        store = _sqlite_store(browser)
        return _probe_sqlite(*store) if store else NO_DATA
    except Exception as exc:
        return classify_exception(browser, exc)


# ---- 给中控看的话：只写观察和能照做的一步 ----------------------------------

FDA_OBSERVED = "系统不允许本程序读取浏览器数据。"


def fda_targets(environ=None, executable=None):
    """「完全磁盘访问权限」里要加的文件：解释器的真实路径，启动用的那个在前、现在跑的在后。

    以前的步骤写的是「加入 TikTok Live Translator」。2026-09-17 本机 TCC 日志量到的是另一回事：
    .app 的 CFBundleExecutable 是 bash 脚本，exec 进 python，main.py 再 execv 进 .venv、
    execve 进 .app 里的 python 链接——系统从头到尾没登记过这个 bundle（9 小时的 TCC 日志里
    bundle id 和 .app 的路径出现 0 次），登记的是解释器文件的**路径**：
        AUTHREQ_SUBJECT: subject=/opt/homebrew/Cellar/python@3.14/…/bin/python3.14   （启动用的）
        binary_path=/opt/anaconda3/bin/python3.13                                     （现在跑的）
        TCCDEvent type=Modify, service=kTCCServiceSystemPolicyAllFiles, identifier_type=Path
    那两条按路径登记的开关打开之后的第一场，日志里才不再有 Operation not permitted。
    两个路径里系统到底认哪一个还没有单独量过，所以两个都列；路径每次现算——
    Homebrew 升级、重建 .venv 之后它会变。"""
    from .macbundle import LAUNCH_PYTHON_ENV

    env = os.environ if environ is None else environ
    targets = []
    for path in (env.get(LAUNCH_PYTHON_ENV), executable or sys.executable):
        if not path:
            continue
        real = os.path.realpath(path)
        if real not in targets:
            targets.append(real)
    return targets


def fda_steps(targets=None):
    """系统拒绝读取时能照做的一步。路径用「」括起来，方便中控整段选中复制。"""
    targets = fda_targets() if targets is None else list(targets)
    return ("到「系统设置」→「隐私与安全性」→「完全磁盘访问权限」，点「+」，在选文件的窗口里按 "
            "⌘⇧G，粘贴下面的路径后回车、点「打开」，再把它的开关打开{}：{}。"
            "列表里显示的名字是 {}，不是本程序的名字。用 Start.command 启动的话把「终端」"
            "也加进去。然后完全退出程序再打开。".format(
                "（每个路径各做一遍）" if len(targets) > 1 else "",
                "、".join("「{}」".format(t) for t in targets),
                "、".join(os.path.basename(t) for t in targets)))


def decrypt_steps(browsers):
    """cookie 的值没能解开时能照做的一步：钥匙串里那一项的名字按浏览器给。"""
    items = "、".join("「{}」".format(_KEYCHAIN_ITEMS[b]) for b in browsers
                      if b in _KEYCHAIN_ITEMS)
    return ("再点一次「开始翻译」；屏幕上如果出现「钥匙串」对话框（要访问{}），"
            "输入这台电脑的登录密码后点「始终允许」。".format(items or "浏览器的 Safe Storage"))


LOGIN_STEPS = "在 Chrome（或 Safari）里登录 TikTok 后再试。"
KEYCHAIN_STEPS = "屏幕上如果有「钥匙串」对话框，点「始终允许」后再试。"

_OBSERVED = {
    OK: "读到了 TikTok 登录",
    BLOCKED: "系统拒绝读取",
    NO_DATA: "没有找到它的 cookie 数据",
    NO_TIKTOK: "能读取，里面没有 tiktok.com 的 cookie",
    NOT_LOGGED_IN: "能读取，没有 TikTok 登录 cookie（sessionid 等）",
    CANNOT_DECRYPT: "能读取，cookie 的值没能解开",
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
    login = login or {}
    codes = set(login.values())
    steps = []
    if BLOCKED in codes:
        steps.append(FDA_OBSERVED + fda_steps())
    if codes & {NOT_LOGGED_IN, NO_TIKTOK, NO_DATA}:
        steps.append(LOGIN_STEPS)
    if CANNOT_DECRYPT in codes:
        steps.append(decrypt_steps([b for b, c in login.items() if c == CANNOT_DECRYPT]))
    if KEYCHAIN_WAIT in codes:
        steps.append(KEYCHAIN_STEPS)
    if not steps and any(str(c).startswith("error:") for c in codes):
        steps.append("可以把这条信息反馈给开发者。")
    return "".join(steps)


SAFARI_LOGIN_STEPS = "请在 Safari 里登录 TikTok。"
SELFCHECK_POINTER = "（这些步骤在自检「浏览器登录态」一行里也有。）"


def no_login_notice(login):
    """一个可用的 TikTok 登录都没读到时，挂在界面顶部的那条持续提示（不挡开播，读到登录
    就撤）。写三件事：看到了什么、这意味着哪类直播间拿不到、能照做的步骤。

    2026-09-17 实测：有的直播间，TikTok 给未登录访问的直播页里没有流地址，带登录去抓才有。
    macOS 上程序先读 Safari（见 resolver._installed_browsers），所以步骤说的是 Safari。"""
    login = {b: c for b, c in (login or {}).items() if c != NOT_READ}
    codes = set(login.values())
    steps = [SAFARI_LOGIN_STEPS]
    if BLOCKED in codes:
        steps.append(FDA_OBSERVED + FDA_STEPS + SELFCHECK_POINTER)
    if KEYCHAIN_WAIT in codes:
        steps.append(KEYCHAIN_STEPS)
    return ("程序没有读到浏览器里的 TikTok 登录——{}。有的直播间 TikTok 只把流地址给已登录的"
            "观众，读到登录之前这类直播间可能解析不出流地址；其余直播间照常监听，监听不会"
            "因此停下。{}读到登录后这条提示自动消失。".format(
                observed_text(login) or "没有找到可读取的浏览器", "".join(steps)))


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
