# i18n: done
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

from .i18n import L

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

def _under(home, relative):
    """home 下的相对路径，按本机的分隔符拼。常量里的相对路径用 / 写；直接整串交给
    os.path.join 在 Windows 上会拼出正反斜杠混用的路径，和 pathlib 给的规范路径做字符串
    比较就对不上（2026-09-17 Windows CI：9 个用例因此失败）。"""
    return os.path.join(home, *relative.split("/"))


def store_dirs(browser):
    """这个浏览器放 cookie 的目录（macOS）。其它平台返回空：「目录在却不让列」是
    macOS 隐私保护的表现，别的平台没有这一类。"""
    home = _home()
    if home is None or sys.platform != "darwin":
        return ()
    if browser == "safari":
        return tuple(os.path.dirname(_under(home, f)) for f in _SAFARI_FILES)
    if browser == "firefox":
        return (_under(home, "Library/Application Support/Firefox/Profiles"),)
    sub = _CHROMIUM_DIRS.get(browser)
    return (_under(home, "Library/Application Support/" + sub),) if sub else ()


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
    header = "; ".join(pairs) or None            # i18n: data（Cookie 头，只发给 TikTok）
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
    files = [_under(home, f) for f in _SAFARI_FILES]
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

FDA_OBSERVED = L("系统不允许本程序读取浏览器数据。",
                 "macOS didn’t allow the app to read browser data. ")   # 英文尾部留空格：后面紧接着拼 fda_steps()


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
    建 .venv 的那个 Python 升级、被卸掉，或重建 .venv 之后它会变。启动器有 .venv 时先用
    建它的那个解释器（tools/find_python.sh），装上、升级别的 Python 不再把启动用的换掉
    （2026-10-19 Homebrew 顺带装上 python@3.15 之后，启动用的从 3.14 变成了 3.15，而 3.15 不在
    这项权限的列表里）。"""
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


# 「终端」这个 app 的位置（macOS 10.15 起在 /System 下）。从终端启动时第 2 步要把它也加进去
TERMINAL_APP = "/System/Applications/Utilities/Terminal.app"


def started_from_terminal(environ=None):
    """本程序是不是从终端里启动的（双击 Start.command、在终端里运行 python3 main.py）。

    2026-10-29：中控从「聚焦搜索」点 .app 启动，「完全磁盘访问权限」那几步却照样写着「用 Start.command 启动的话
    把「终端」也加进去」，照着做不下来。终端会给在它里面跑的程序设 TERM（「终端」和 iTerm2 还设 TERM_PROGRAM），
    环境变量跟着 exec 一路带到最终进程；从访达、程序坞、聚焦搜索打开 .app 时，系统给的环境里没有这两个。
    只看环境变量、不看 isatty：`| tee` 这类重定向下 stdout 不是终端，程序照样是从终端启动的。"""
    env = os.environ if environ is None else environ
    return bool(env.get("TERM_PROGRAM") or env.get("TERM"))


def quit_step(terminal=None):
    """「完全退出本程序再打开」那一步，按启动方式写。terminal 为 None 时按 started_from_terminal()。"""
    terminal = started_from_terminal() if terminal is None else terminal
    if terminal:
        return L("完全退出本程序，再从终端启动它（比如双击 Start.command）；系统要「终端」退出并重新打开时，照做。",
                 "Quit the app completely, then start it from a terminal again (for example, with "
                 "Start.command). If macOS asks to quit and reopen Terminal, do that.")
    return L("按 ⌘Q 完全退出本程序，再打开它。", "Press ⌘Q to quit the app completely, then open it again.")


def fda_steps(targets=None, terminal=None):
    """系统拒绝读取时能照做的几步：头一句、编号的三步、两行补充。

    2026-10-29 新 Mac 上，中控照着以前那一整段长文（系统设置路径、+、⌘⇧G、粘贴、回车、打开、开关、列表里叫
    python3.x、已在列表要先删再加、Start.command 要加「终端」、完全退出重开、权限范围）做不下来。现在：
      * 头一句一行（「照下面三步给 Python 打开…」），「没有读到 TikTok 登录」那条持续提示把它接在第一行末尾
        （no_login_notice）——页面折叠时只露第一行，动作要在那一行里；
      * 三步各占一行、带编号：打开那一页；点「+」加 Python（路径在下面，每个路径单独一行）；完全退出再打开；
      * 「终端」那几句只对从终端启动的人说（started_from_terminal；terminal 显式给的话按给的）：从 .app 启动时
        系统登记的是解释器的路径（fda_targets 的说明），「终端」不相干；
      * 后面两行：列表里已经有它、开关也开着还是这样，删掉再做一次第 2 步（专家审查 I8；只写看得到的和能做的，
        不写为什么，「解释器换了文件，系统按指纹认它」没实测过）；权限范围（D6，用户 10-22 按推荐「维持授权，把
        范围讲清楚」）一字不改、单独占最后一行，后面接的别的步骤都另起一行（steps_text、no_login_notice）。

    路径行照旧整行就是「路径」（专家审查 I8）：解析失败的横幅里这几行由页面换成可复制的代码行（status.paths，
    app/pipeline.py _fda_paths；web/app.js 只按那个结构化字段认路径行，不从文字里猜），自检那一行和终端里照文字分行
    显示。⌘⇧G 手动粘贴是眼下唯一的走法；以后有了「打开设置」「在访达中显示」的按钮，这条走法挪进 README 当备用。"""
    targets = fda_targets() if targets is None else list(targets)
    terminal = started_from_terminal() if terminal is None else terminal
    names = L("、", ", ").join(os.path.basename(t) for t in targets)
    paths = L("\n", "\n").join(L("「{}」", "“{}”").format(t) for t in targets)
    each = L("（每个路径各做一遍）", " (do this for each path)") if len(targets) > 1 else ""
    lines = [
        L("照下面三步给 Python 打开「完全磁盘访问权限」：", "Follow these three steps to turn on Full Disk Access for Python:"),
        L("1. 打开「系统设置」→「隐私与安全性」→「完全磁盘访问权限」。",
          "1. Open System Settings > Privacy & Security > Full Disk Access."),
        L("2. 点「+」，在选文件的窗口里按 ⌘⇧G，粘贴下面的路径后回车、点「打开」，再把它的开关打开{}：",
          "2. Click +. In the file window, press ⌘⇧G, paste the path below, press Return, click Open, "
          "and turn on its switch{}:").format(each),
        paths,
        L("列表里显示的名字是 {}，不是本程序的名字。", "The list shows {}, not the app’s name.").format(names),
    ]
    if terminal:
        lines[-1] = lines[-1] + _space_before(
            L("本程序是从终端启动的：「终端」也照这样加进去，按 ⌘⇧G 时粘贴「{}」。",
              "The app was started from a terminal. Add Terminal the same way: after ⌘⇧G, paste "
              "“{}”.").format(TERMINAL_APP))
    lines += [
        L("3. ", "3. ") + quit_step(terminal),
        L("列表里已经有它、开关也开着还是这样：选中它点「−」删掉，再做一次第 2 步。",
          "If it’s already in the list with its switch on and you still see this, select it, click −, and "
          "do step 2 again."),
        L("这项权限给的是 Python 解释器，不只是本程序：用它运行的任何脚本都能读取这台电脑上受保护的数据。"
          "不再需要借用浏览器里的 TikTok 登录时，可以回到同一个列表把它的开关关掉。",
          "This permission goes to the Python interpreter, not just this app, so any script it runs can read "
          "protected data on this computer. When you no longer need the TikTok sign-in from your browser, you "
          "can turn its switch off in the same list."),
    ]
    return L("\n", "\n").join(lines)


def decrypt_steps(browsers):
    """cookie 的值没能解开时能照做的一步：钥匙串里那一项的名字按浏览器给。"""
    items = L("、", ", ").join(L("「{}」", "“{}”").format(_KEYCHAIN_ITEMS[b]) for b in browsers
                              if b in _KEYCHAIN_ITEMS)
    return L("再点一次「开始翻译」；屏幕上如果出现「钥匙串」对话框（要访问{}），"
             "输入这台电脑的登录密码后点「始终允许」。",
             "Click Start again. If a keychain dialog asks for access to {}, enter this "
             "computer’s login password and click Always Allow.").format(
                 items or L("浏览器的 Safe Storage", "the browser’s Safe Storage"))


LOGIN_STEPS = L("在 Chrome（或 Safari）里登录 TikTok 后再试。",
                "Sign in to TikTok in Chrome (or Safari), then try again.")
KEYCHAIN_STEPS = L("屏幕上如果有「钥匙串」对话框，点「始终允许」后再试。",
                   "If a keychain dialog is on screen, click Always Allow, then try again.")

# 每条拼在「Chrome：」后面，英文小写开头（冒号后接的不是完整句子）。
# BLOCKED 不点名谁拒绝了谁：observed_text 按浏览器逐条拼，写死浏览器名会让另一条说错
_OBSERVED = {
    OK: L("读到了 TikTok 登录", "found a TikTok sign-in"),
    BLOCKED: L("系统拒绝读取", "macOS didn’t allow access"),
    NO_DATA: L("没有找到它的 cookie 数据", "no cookie data found"),
    NO_TIKTOK: L("能读取，里面没有 tiktok.com 的 cookie", "readable, but no tiktok.com cookies"),
    NOT_LOGGED_IN: L("能读取，没有 TikTok 登录 cookie（sessionid 等）",
                     "readable, but no TikTok sign-in cookie (sessionid or similar)"),
    CANNOT_DECRYPT: L("能读取，cookie 的值没能解开", "readable, but the cookie values couldn’t be decrypted"),
    KEYCHAIN_WAIT: L("读取在限时内没有返回", "reading didn’t finish in time"),
    READABLE: L("能读取，没能看出有没有登录 TikTok",
                "readable, but couldn’t tell whether you’re signed in to TikTok"),
    NOT_READ: L("未读取", "not read"),
}


def label(browser):
    return LABELS.get(browser, str(browser))


def describe(code):
    code = str(code)
    if code.startswith("error:"):
        return L("读取出错（{}）", "error while reading ({})").format(code[len("error:"):])
    return _OBSERVED.get(code, L("读取结果 {}", "read result {}").format(code))


def observed_text(login):
    """{浏览器: 代码} → 「Chrome：系统拒绝读取；Safari：系统拒绝读取」。"""
    return L("；", "; ").join(L("{}：{}", "{}: {}").format(label(b), describe(c))
                             for b, c in (login or {}).items())


def steps_text(login, lines=False):
    """和这些观察对应的、能照做的步骤（可能不止一步）。

    lines=True（解析失败那段话，browser_only_advice）：带「完全磁盘访问权限」那几步（分了行，最后一行是权限
    范围）时每一步另起一行——「在 Chrome 里登录」「钥匙串」接在权限范围那句后面，读起来像是在接着讲权限（审查
    第 1 轮）。自检那一行（app/selfcheck.py）照旧接在后面：它的英文由 tests/test_i18n_backend_en.py 钉着
    （本批文件清单外），改成分行记在顺延事项里。"""
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
        steps.append(L("可以把这条信息反馈给开发者。", "You can report this message to the developer."))
    if lines and any("\n" in step for step in steps):
        return L("\n", "\n").join(steps)
    return L("", " ").join(steps)            # 中文句子直接相连，英文句子之间要空格


def _after_lead(text):
    """接在 pipeline.browser_only_message 第一句（「接口不给流地址」那句，第八条的说明）后面的那一段。

    这一段分了行（带「完全磁盘访问权限」那几步）时，前面另起一行，让第一句单独成为第一行：横幅超过 3 行时页面
    只露第一行和「显示全部步骤」（web/app.js renderStatusBanner，专家审查 U10），那句说明不能被折进去。"""
    return L("\n", "\n") + text


def _space_before(text):
    """英文非空时在前面加一个空格（英文句子之间要空格），中文原样（中文句子直接相连）。
    登记在 tests/i18n_rules.py 的 ZH_IDENTITY_CALLS：中文模式下 str(_space_before(x)) == x。"""
    return L("", " ") + text if text else text


SAFARI_LOGIN_STEPS = L("请在 Safari 里登录 TikTok。", "Sign in to TikTok in Safari.")
# 中控点名了别的浏览器时，登录优先那一步读的就是它（resolver._login_first_browsers），叫人去登录的那一步按它写
_SIGN_IN_STEPS = L("请在 {} 里登录 TikTok。", "Sign in to TikTok in {}.")
# 读得到（或者还没有 cookie 库）、只是里面没有可用的登录：能照做的一步是去登录
_SIGN_IN_CODES = (NOT_LOGGED_IN, NO_TIKTOK, NO_DATA, READABLE)


def no_login_notice(login, seen=None):
    """登录优先那一步（resolver.LOGIN_LAYER）没读到可用的 TikTok 登录时，挂在界面顶部的那条持续提示（不挡开播，
    那一步读到登录就撤）。写三件事：那一步读了什么、看到了什么；能照做的步骤；这意味着哪类直播间拿不到。

    login 是那一步的观察 {浏览器: 代码}：没点名浏览器时只有 Safari（resolver._login_first_browsers）。seen 是同一次
    解析里各层的观察合在一起（pipeline._login_seen），只用来照实补一句「后面几步借用了 Chrome 里的登录」。

    2026-10-29 新 Mac 上的第一场：这条提示的头一句是「程序没有读到浏览器里的 TikTok 登录——Safari：系统拒绝读取」，
    第一行的动作是「请在 Safari 里登录 TikTok」；同一时刻自检写着「Chrome：读到了 TikTok 登录」，后面几层也借用了
    Chrome 的登录。中控 Safari 里早就登录着，照第一行去做是白忙，真正要做的授权几步折在「显示全部步骤」后面；最后
    一行指向的自检「浏览器登录态」那一行，因为 Chrome 读到了登录是「通过」、不带步骤，直播中设置分组又整块收着。现在：
      * 头一句只说登录优先那一步读了什么、看到了什么，不再说「没有读到浏览器里的 TikTok 登录」；
      * 系统拒绝读取时，第一行的动作就是授权：fda_steps 的头一句接在第一行末尾，几步和路径在下面
        （页面上超过 3 行的持续提示只露第一行和「显示全部步骤」，web/app.js foldIncident）；
      * 「请在 Safari 里登录」只在那一步读得到、里面没有登录时出现；
      * 不再指向自检那一行：步骤就在这条提示里。
    影响哪类直播间和「自动消失」放在最后（分行时单独成最后一行，不接在权限范围那句后面，审查第 1 轮）。"""
    login = {b: c for b, c in (login or {}).items() if c != NOT_READ}
    codes = set(login.values())
    head = _first_step_head(login) + _space_before(_borrowed_later(login, seen))
    if BLOCKED in codes:
        lines = [head + _space_before(fda_steps())]
        if KEYCHAIN_WAIT in codes:
            lines.append(KEYCHAIN_STEPS)
        lines.append(_NO_LOGIN_IMPACT + _space_before(_NO_LOGIN_TAIL))
        return L("\n", "\n").join(lines)
    steps = []
    sign_in = [b for b, c in login.items() if c in _SIGN_IN_CODES]
    if sign_in:
        steps.append(SAFARI_LOGIN_STEPS if sign_in == ["safari"]
                     else _SIGN_IN_STEPS.format(L("、", ", ").join(label(b) for b in sign_in)))
    if CANNOT_DECRYPT in codes:
        steps.append(decrypt_steps([b for b, c in login.items() if c == CANNOT_DECRYPT]))
    if KEYCHAIN_WAIT in codes:
        steps.append(KEYCHAIN_STEPS)
    # 只有一行：中文句子直接相连，英文句子之间一个空格
    return (head + _space_before(_NO_LOGIN_IMPACT) + _space_before(L("", " ").join(steps))
            + _space_before(_NO_LOGIN_TAIL))


def _first_step_head(login):
    """持续提示的头一句：登录优先那一步读了哪个浏览器、看到了什么。"""
    if not login:
        return L("解析流地址时，登录优先这一步没有找到可读取的浏览器。",
                 "When looking up the stream URL, the app’s first step didn’t find a browser it could "
                 "read a TikTok sign-in from.")
    return L("解析流地址时，登录优先这一步只读 {} 里的 TikTok 登录——{}。",
             "When looking up the stream URL, the app first reads the TikTok sign-in from {} only. "
             "{}.").format(L("、", ", ").join(label(b) for b in login), observed_text(login))


def _borrowed_later(login, seen):
    """同一次解析里，登录优先那一步以外的浏览器读到了登录（后面几步借用了它）：照实说一句。没有就是空串。"""
    others = [b for b, c in (seen or {}).items() if c == OK and b not in login]
    if not others:
        return ""
    return L("同一次解析里，后面几步借用了 {} 里的 TikTok 登录。",
             "Later steps of the same lookup used the TikTok sign-in from {}.").format(
                 L("、", ", ").join(label(b) for b in others))


_NO_LOGIN_IMPACT = L("有的直播间 TikTok 只把流地址给已登录的观众，这一步读到登录之前，这类直播间可能解析不出"
                     "流地址；其余直播间照常监听，监听不会因此停下。",
                     "For some live streams, TikTok provides the stream URL only to signed-in viewers, so "
                     "those live streams may not get a stream URL until this step finds a sign-in. Other "
                     "live streams are monitored as usual.")
_NO_LOGIN_TAIL = L("读到登录后这条提示自动消失。", "This notice disappears once a sign-in is found.")


def browser_only_advice(login):
    """解析以 kind=browser_only 收场时，附在固定话术后面的那一段：两个借登录的层
    看到了什么、对应能做什么。没有观察（没试浏览器）返回空串。"""
    login = {b: c for b, c in (login or {}).items() if c != NOT_READ}
    if not login:
        return ""
    borrowed = [b for b, c in login.items() if c == OK]
    # 英文开头留空格：这一段夹在 pipeline.browser_only_message 的两句之间，前一句以句号结尾
    if borrowed:
        return L("已借用 {} 里的 TikTok 登录再试，TikTok 仍然没有给出流地址。",
                 " Tried again with the TikTok sign-in from {}. TikTok still didn’t provide a "
                 "stream URL.").format(L("、", ", ").join(label(b) for b in borrowed))
    # 英文只说「找过」：观察里可能一个登录都没读到。步骤为空（比如只有 READABLE）时英文不留尾部空格。
    # 步骤分了行（带「完全磁盘访问权限」那几步）时，观察那句和步骤之间也另起一行，最后也换行：pipeline 接在
    # 后面的「想现在就看：粘 .flv」是另一件事，接在权限范围或「在 Chrome 里登录」后面读起来像是同一件事（审查
    # 第 1 轮）。那一段的英文开头带一个空格（见 browser_only_message），横幅按行画时每行去掉首尾空白
    # （web/app.js renderStatusBanner）；终端和审计只写中文
    steps = steps_text(login, lines=True)
    if "\n" not in steps:
        return _space_before(L("借用浏览器里的 TikTok 登录时观察到——{}。{}",
                               "Looked for a TikTok sign-in in your browsers. {}.{}").format(
            observed_text(login), _space_before(steps)))
    return _after_lead(L("借用浏览器里的 TikTok 登录时观察到——{}。\n{}\n",
                         "Looked for a TikTok sign-in in your browsers. {}.\n{}\n").format(
        observed_text(login), steps))
