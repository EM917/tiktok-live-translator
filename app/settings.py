"""settings.json 读写：界面偏好与组件状态的小型持久化。

只依赖标准库——启动自举阶段（依赖还没装齐时）也要能安全导入。
"""
import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"
# 设置文件的权限位：只给本账户读写（见 _create_private）。os.open 的 mode 只在新建文件时生效，
# 还要再被 umask 过一遍——umask 只会把它收得更窄，不会放宽
_PRIVATE_MODE = 0o600
_WINDOWS = os.name == "nt"      # Windows 上照旧复用临时文件（见 _create_private）

# 本次运行里发现 settings.json 内容损坏时备份成的文件名，以及界面提示是否已发过
_corrupt = {"backup": None, "announced": False}
# 最近一次读取时 settings.json 在、却读不出来：(路径, 系统或解析器的原话)；读出来了、或文件不在是 None。
# 这时 save_setting 不写回（见 _load）
_unreadable = {"problem": None}
# save_setting 是「读全部 → 改一项 → 整个写回」：两个线程同时写不同的键，后写的会把先写的那一项冲掉。
# 以前写设置的都在事件循环线程上；浮窗记位置和大小的定时器线程（app/float_window.py）是第二个写手，
# 所以进程内串行化。跨进程（双击第二次）不在这里管
_save_lock = threading.Lock()


def load_settings():
    """读出全部设置。文件缺失、读不了、或是合法 JSON 但不是对象时返回空 dict。

    解析不了（不是合法 JSON、编码认不出、0 字节）时，先把文件改名备份成
    settings.json.corrupt-<时间>，再返回空 dict。以前直接返回 {}：启动几秒内
    必然有一次 save_setting（更新器记时间戳、开播记房间）在这个 {} 上合并写回，
    DeepL 等密钥、引擎选择、最近直播间被静默抹掉，连原文件都不剩。
    改名而不是复制：之后的读取看到的是「没有文件」，不会每读一次备份一份。

    读不了（文件在，系统不让读、路径上是个文件夹……）同样返回空 dict，但不改名，记进
    unreadable_settings()，save_setting 也不写回——见 _load。"""
    return _load()[0]


def _load():
    """(设置, 能不能写回)。能不能写回为 False：文件在、却没读出它的内容——读不了，或者解析不了又没能
    改名备份——这时手里的 {} 不是文件里的设置，拿它合并写回就把文件盖掉了。

    以前读不了也当成没有文件：文件读不了、文件夹却可写（chmod 000 一类）时，下一次 save_setting
    就把空设置加一项换上去，密钥、引擎、最近直播间全没了。只有「文件不在」才是真的没有设置。"""
    try:
        raw = SETTINGS_FILE.read_bytes()
    except FileNotFoundError:    # 含所在文件夹不在
        _note_unreadable(None)
        return {}, True
    except OSError as exc:
        _note_unreadable(_os_words(exc))
        return {}, False
    try:
        # 整段字节交给 json，由它认编码：带 BOM 的 UTF-8（PowerShell 5.1 的
        # Set-Content -Encoding UTF8、老版记事本）和 UTF-16（PowerShell 5.1 的 > 或
        # Out-File、记事本存成「Unicode」）都读得出来。手改粘密钥正是这样存出来的，
        # 合法的文件不能被当成损坏挪走
        data = json.loads(raw)
    except Exception as exc:     # JSONDecodeError、UnicodeDecodeError……
        moved = _backup_corrupt(raw)
        _note_unreadable(None if moved else str(exc))
        return {}, moved
    _note_unreadable(None)
    return (data if isinstance(data, dict) else {}), True


def _os_words(exc):
    """系统的原话，不带路径（提示里另写路径）：[Errno 13] Permission denied。"""
    if exc.errno is not None and exc.strerror:
        return "[Errno {}] {}".format(exc.errno, exc.strerror)
    return str(exc) or type(exc).__name__


def _note_unreadable(detail):
    problem = None if detail is None else (str(SETTINGS_FILE), detail)
    if problem is not None and problem != _unreadable["problem"]:
        print("[警告] 读不出 {}：{}。在它能读出来之前，设置不写回，免得盖掉原有内容".format(*problem))
    _unreadable["problem"] = problem


def unreadable_settings():
    """最近一次读取时 settings.json 在、却读不出来：返回 (路径, 系统或解析器的原话)；读出来了、或者
    文件不在，返回 None。这期间 save_setting 不写回。只报观察到的，不说为什么读不了。"""
    return _unreadable["problem"]


def _backup_corrupt(raw):
    """把解析不了的 settings.json 改名备份。返回原处还有没有这份坏文件：False 是还在（没能挪走），
    这时写回会把它盖掉。"""
    try:
        if SETTINGS_FILE.read_bytes() != raw:
            return False         # 读完之后文件已被别处改写过：别把新文件当坏的挪走，这一次也别写
    except FileNotFoundError:
        return True              # 并发的另一次读取刚改名备份过
    except OSError:
        return False
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for n in range(1, 100):
        backup = SETTINGS_FILE.with_name("{}.corrupt-{}{}".format(
            SETTINGS_FILE.name, stamp, "" if n == 1 else "-{}".format(n)))
        if backup.exists():
            continue
        try:
            os.replace(str(SETTINGS_FILE), str(backup))
        except FileNotFoundError:    # 并发的另一次读取刚改过名
            return True
        except OSError:
            return False
        if _corrupt["backup"] is None:
            _corrupt["backup"] = backup.name
        print("[警告] settings.json 内容损坏，已备份为 {}".format(backup.name))
        return True
    return False


def corrupt_backup_name():
    """本次运行里 settings.json 被判定损坏后备份成的文件名；没发生过是 None。"""
    return _corrupt["backup"]


def take_corrupt_notice():
    """同上，但每次运行只交出一次：界面提示发一遍，不是每读一次设置发一遍。"""
    if _corrupt["backup"] is None or _corrupt["announced"]:
        return None
    _corrupt["announced"] = True
    return _corrupt["backup"]


DEFAULT_SOURCE_LANG = "es,en"


def resolve_source(cli_value, saved):
    """启动时决定主播语言：CLI 显式指定 > 界面上次的选择 > 西语+英语。

    最后一档是 "es,en" 而不是不限制的 auto：这个产品就是给西语带货直播做合规
    监听的，首次使用不该让每个 2.5 秒片段都在全部语言里重新猜一次——实测 auto
    档一场里 22.7% 的段被打上无关语言标签（en/pt/hi/tr…），纯标点垃圾字幕和按
    错误语言翻出来的译文全从这里来。真要不限制地逐段自动检测，在界面里选
    「自动检测」，这个选择会被记住（返回 None 即自动检测）。

    值也可以是逗号分隔的语言码列表，如默认值 "es,en"：意思是「自动检测，但只在
    列表里的语言中选」——带货主播西语夹英语是常态，这样既不用每次手动切换，也
    不会被贴上无关语言的标签。列表的校验、裁剪和「检测语言不对就强制重转」的
    逻辑都在 app.asr._parse_language_spec / Transcriber.transcribe，这里只管
    原样传下去：单一语种码和 "auto" 的行为和以前完全一样。
    """
    if cli_value:
        # "auto" 要归一成 None——Whisper 的 language 参数只认语言码或 None
        return None if str(cli_value) == "auto" else cli_value
    s = str(saved or "").strip()
    if s == "auto":
        return None               # 用户明确选过自动检测，尊重它
    return s[:12] if s else DEFAULT_SOURCE_LANG


# 字幕字号（界面顶栏的滑块，决定底部大字幕的字号，历史卡片和弹幕栏按同一比例跟着变）。
# 范围与 web/index.html 里 #font-size 的 min/max 一致。以前只记在浏览器的 localStorage 里：
# 它按「协议+主机+端口」隔离，端口一漂移（8765 被占用时程序自动改用下一个）调好的字号就没了。
# 现在记在 settings.json，localStorage 只作为服务端没有记录时的退路。
CAPTION_SIZE_MIN = 20
CAPTION_SIZE_MAX = 60


def normalize_caption_size(value):
    """把页面发来的（或 settings.json 里读到的）字号变成整数；不是合法字号返回 None。

    只认 CAPTION_SIZE_MIN..MAX 之间的数：设置文件可能被手改过，消息也可能来自旧页面。
    越界、非数字、bool、NaN 一律当没有，不是夹到边界——夹了会把一个坏值写成「看起来像用户选的」。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    size = int(math.floor(number + 0.5))          # 四舍五入（同页面里的 Math.round，不用银行家舍入）
    if size < CAPTION_SIZE_MIN or size > CAPTION_SIZE_MAX:
        return None
    return size


def saved_caption_size():
    """settings.json 里记着的字号；没记过或记坏了是 None（界面保持今天的默认字号）。"""
    return normalize_caption_size(load_settings().get("caption_size"))


# 首页「最近直播间」最多留几个。够一屏点选即可，多了首页反而乱。
RECENT_ROOMS_MAX = 8


def push_recent_room(streamer, url, limit=RECENT_ROOMS_MAX):
    """把一个刚打开的直播间记进「最近直播间」，返回更新后的列表（新的在前）。

    按主播名去重：同一个主播反复打开只留最近一条，不然列表几分钟就被
    同一个人刷满。只收有主播名的房间——直接粘 .flv 流地址没有主播身份，
    进了列表也只能显示一串地址，反而是给中控添乱。写失败静默忽略：
    这是锦上添花，不能拖累开播。
    """
    from datetime import datetime
    streamer = (streamer or "").strip()
    url = (url or "").strip()
    if not streamer or not url:
        return recent_rooms()
    data = load_settings()
    items = data.get("recent_rooms")
    items = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    # 同名的旧记录先剔掉，再把这一条放到最前
    items = [x for x in items if x.get("streamer") != streamer]
    items.insert(0, {"streamer": streamer, "url": url[:500],
                     "at": datetime.now().isoformat(timespec="seconds")})
    items = items[:max(1, int(limit))]
    save_setting("recent_rooms", items)
    return items


def recent_rooms(limit=RECENT_ROOMS_MAX):
    """读「最近直播间」，过滤掉结构不对的条目，最多返回 limit 条。"""
    items = load_settings().get("recent_rooms")
    if not isinstance(items, list):
        return []
    clean = [{"streamer": x.get("streamer", ""), "url": x.get("url", ""),
              "at": x.get("at", "")}
             for x in items
             if isinstance(x, dict) and x.get("streamer") and x.get("url")]
    return clean[:max(1, int(limit))]


# 按主播记住「本场品牌」选的品牌词表。键是主播用户名（小写），值是品牌 id；
# 空串表示「不限」，写空串时直接删掉这个键（不留一条「不限」的记录，
# 没记过和记了「不限」在读取时是同一件事）。读/改/写模式与「最近直播间」
# 那组函数一致：先读全部设置，取出这一项的映射，改动单个主播的那一条，
# 再整体写回——不能直接 save_setting("brands.allura", ...)，settings.json
# 里没有嵌套路径这种写法。
def save_streamer_brand(streamer, brand):
    """记住（或清除）某个主播这场选的品牌词表，返回更新后的完整映射。"""
    streamer = str(streamer or "").strip().lower()
    if not streamer:
        return streamer_brands()
    data = load_settings()
    raw = data.get("brands")
    brands = ({k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
              if isinstance(raw, dict) else {})
    brand = str(brand or "").strip()
    if brand:
        brands[streamer] = brand
    else:
        brands.pop(streamer, None)     # 「不限」：不留占位记录
    save_setting("brands", brands)
    return brands


def streamer_brands():
    """读「按主播记住的品牌词表」映射，过滤掉结构不对的条目（不是 dict 就当
    空，键或值不是字符串就整条丢弃——设置文件可能被手改过）。"""
    raw = load_settings().get("brands")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def save_setting(key, value):
    """写入单个设置项（读-合并-原子替换），返回写没写成。写失败不抛——
    持久化是锦上添花，不能因为磁盘/权限问题影响主流程。

    文件在、却读不出来（unreadable_settings() 不是 None）时不写：手里没有文件里原有的设置，
    合并写回就把它们盖掉了。界面上的提示见 app/pipeline.py 的 _announce_settings_unreadable。"""
    with _save_lock:
        data, writable = _load()
        if not writable:
            return False
        data[key] = value
        try:
            tmp = SETTINGS_FILE.with_suffix(".json.tmp")
            fd = _create_private(tmp)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=2))
                f.flush()
                # 先落盘再替换：写完就断电时，不至于换上一个 0 字节的 settings.json
                os.fsync(f.fileno())
            # 换上去的就是刚建的这个 0600 文件：升级前就是 0644 的 settings.json 随之被替掉，不用再 chmod
            os.replace(tmp, SETTINGS_FILE)
        except OSError:
            return False
        return True


def _create_private(tmp):
    """新建临时文件、返回可写的 fd。POSIX 上这个文件从建出来那一刻起就只给本账户读写（0600）：
    settings.json 里存着 DeepL / Claude 的密钥，以前按默认 umask 建成 0644，同一台电脑上别的账户
    也读得到。

    不复用旧文件：上次写到一半中断（断电、被强杀）留下的 .json.tmp 是按老办法建的 0644，os.open
    的 mode 管不到已经存在的文件，带 O_TRUNC 打开它，密钥就会先写进一个 0644 的文件。所以先删掉
    残留，再用 O_CREAT | O_EXCL 新建：O_EXCL 保证拿到的是这一次新建的文件——删完到建之间被别人
    抢先建了、或者这个路径是符号链接，都直接报错，这一次不写（save_setting 照旧静默忽略），绝不
    写进别人建的文件。

    Windows 照旧（O_CREAT | O_TRUNC，复用残留）：那边 mode 只认只读位、权限归 ACL 管，先删再建
    换不来更窄的权限。O_BINARY（只有 Windows 有）：和 open() 一样让底层 fd 不做换行转换，换行只由
    文本层处理一次，写出的字节和以前相同。"""
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if _WINDOWS:
        return os.open(str(tmp), flags | os.O_TRUNC, _PRIVATE_MODE)
    try:
        os.unlink(str(tmp))
    except FileNotFoundError:
        pass
    return os.open(str(tmp), flags | os.O_EXCL, _PRIVATE_MODE)
