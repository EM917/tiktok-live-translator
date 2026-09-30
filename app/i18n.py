"""界面文字的中英双语对。规则只有一条：会显示在界面上的中文，写成 L("中文", "English")。

L() 的结果**就是那句中文**（str 子类 Bi）：== / hash / in / print / json.dumps / 写审计
全按中文走，和没加 L 之前一样。英文只在「发往界面」的那一刻由 render() 换出来
（app/server.py 广播与 hello、app/viewer.py filter_payload、原生窗口/对话框/通知）。

会丢英文的写法（得到普通中文 str，英文界面上这一句就露中文，不会报错，G2/G4/G9 会拦）：
f-string、普通模板 "…{}".format(bi)、切片、.strip()、re.sub、str(exc)、"sep".join(含 Bi 的列表)。
拼接用 L 模板的 .format / + / L(sep, sep_en).join；异常文字用 of(exc)；清洗截断用 bimap。

只依赖标准库、兼容 Python 3.9：main.py 在自举阶段（还在系统 Python 里、.venv 没装好）
就要 import 它，给安装提示、缺组件、致命错误这几个对话框定语言。

语言怎么定（spec §3）：
- boot(ROOT)：每个进程都跑一次，**只读**。记下装机状态与系统语言（跨 exec 用环境变量带着走），
  自己读 settings.json 的字节，不调 load_settings、不改名、不写回——损坏的设置文件要留给
  最终进程去备份，否则「设置文件损坏，已备份为…」的提示和审计里的备份名会随 exec 丢掉。
- settle(ROOT)：只在最终进程里跑一次。这里才第一次 load_settings，闸开着时才迁移写 ui_lang；
  新装且界面是英文时，顺带把没设置过的翻译目标语言默认成 English（提交 T）。结束时把装机状态
  记成 existing：之后 exec 出来的进程（一键更新重启）不再算新装。
"""
import json
import os
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

I18N_ENABLED = True           # 发布闸，Z1 打开。改回 False 就回到纯中文：关着时（且没有一次性覆盖）界面恒为中文
ZH, EN = "zh", "en"
CHOICES = ("system", ZH, EN)
BUNDLE_ID = "io.github.em917.tiktok-live-translator"
# CJK 字符与 CJK 标点：U+3000–303F 符号标点、3040–30FF 假名、3400–4DBF 扩展 A、
# 4E00–9FFF 基本区、F900–FAFF 兼容区、FF00–FFEF 全角。英文臂里出现它们就是漏翻（spec R3）
CJK = re.compile("[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]")
ENV_OVERRIDE = "TLT_UI_LANG"         # 一次性覆盖（开发、截图）；命令行 --ui-lang 同义
ENV_SYSTEM = "TLT_SYSTEM_LANG"       # system_lang() 的结果跨 exec 缓存：zh / en / none
ENV_INSTALL = "TLT_INSTALL_STATE"    # 第一个进程看到的装机状态：fresh / existing（只记第一次）；
                                    # settle 之后改成 existing
# 「装成功过」的记号：bootstrap 只在 `pip install -r` 返回 0 之后才往 .venv 里写它（app/bootstrap.py
# 的 REQ_STAMP，tests/test_i18n_core.py 钉着两边同名）。这里直接写文件名：本模块只依赖标准库，
# 自举期还在系统 Python 里就要 import，不能 import bootstrap
INSTALL_STAMP = ".requirements.sha256"
NET = None                    # 测试运行时网："report" / "strict"；生产为 None，零开销
_state = {"choice": "system", "lang": ZH, "system": None, "override": None}
_bad_once = set()
_bad_lock = threading.Lock()


class Bi(str):
    """一句中文，身上挂着它的英文。"""

    def __new__(cls, zh, en):
        obj = str.__new__(cls, zh)
        # 英文缺了（None）就用中文顶上：英文出错只会退回中文，绝不会让界面上出现 "None"
        obj.en = str(_en(en)) if en is not None else str(obj)
        return obj

    def __reduce__(self):                        # copy/deepcopy/pickle 不丢英文
        return (Bi, (str(self), self.en))

    def format(self, *args, **kwargs):
        # 中文一路与原来的 "…".format(...) 完全相同：出错照旧抛，行为不变
        zh = str.format(self, *[_zh(a) for a in args], **{k: _zh(v) for k, v in kwargs.items()})
        try:
            en = self.en.format(*[_en(a) for a in args], **{k: _en(v) for k, v in kwargs.items()})
        except Exception as exc:                 # 英文模板写坏：退回中文，绝不影响直播链路
            _bad_pair(self, exc)
            en = zh
        return Bi(zh, en)

    def __add__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return Bi(str.__add__(self, _zh(other)), self.en + _en(other))

    def __radd__(self, other):                   # "前缀" + bi：子类的反射方法先于 str 拼接（3.9 起如此）
        if not isinstance(other, str):
            return NotImplemented
        return Bi(_zh(other) + str(self), _en(other) + self.en)

    def join(self, items):                       # L("、", ", ").join(names)；同分隔符写 L("\n", "\n")
        items = list(items)
        return Bi(str.join(self, [_zh(x) for x in items]), self.en.join([_en(x) for x in items]))


def _zh(x):
    return str(x) if isinstance(x, Bi) else x


def _en(x):
    return x.en if isinstance(x, Bi) else x


def L(zh, en):
    return Bi(zh, en)


def LN(n, zh, en_one, en_many):
    """中文不分单复数；英文按 n 选一句。占位符照常由 .format 填。"""
    return Bi(zh, en_one if n == 1 else en_many)


# 程序名，窗口标题、系统通知、启动期对话框、菜单栏兜底等处共用这一个。
# 交给系统之前一律经 text() 渲染成普通 str（main.py、native_dialog、window_attention、
# alert_notify、macbrand）
APP_NAME = L("TikTok 直播同传", "TikTok Live Translator")


def of(exc):
    """异常里的界面文字：raise X(L(...)) 抛出来的保留英文；其余照旧 str(exc)。"""
    args = getattr(exc, "args", ())
    return args[0] if len(args) == 1 and isinstance(args[0], Bi) else str(exc)


def bimap(fn, text):
    """对中英两句各做一次同样的处理（去查询串、截断……）。"""
    return Bi(fn(str(text)), fn(text.en)) if isinstance(text, Bi) else fn(text)


def enabled():
    """双语机制是否对用户生效：闸开着，或者这次启动带了一次性覆盖。"""
    return I18N_ENABLED or _state["override"] is not None


def current():
    return _state["lang"]


def render(obj, lang=None):
    """发往界面前的最后一步。中文原样返回（`is` 同一对象，零开销）；英文换出 .en。
    认不出的语言一律当中文：宁可露中文，不可出错。"""
    lang = lang or _state["lang"]
    if lang != EN:
        return obj
    if isinstance(obj, Bi):
        return obj.en
    if isinstance(obj, dict):
        return {k: render(v, lang) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [render(v, lang) for v in obj]
    return obj


def text(obj, lang=None):
    """原生出口专用：永远交出普通 str（PyObjC/pythonnet/ctypes 收到 str 子类的行为没验证过）。"""
    value = render(obj, lang)
    return str(value) if isinstance(value, str) else value


def inject_lang(html, lang, marker=False):
    """桌面页和手机页共用：只改 <html> 开标签，不在服务端做任何别的改写。
    marker=True（只给手机页）时加 data-i18n="on"，viewer.js 见到它才读本机选择、才显示切换。"""
    tag = '<html lang="{}"{}>'.format("en" if lang == EN else "zh-CN", ' data-i18n="on"' if marker else "")
    return html.replace('<html lang="zh-CN">', tag, 1)


def _bad_pair(bi, exc):
    """英文模板写坏了（占位符对不上、格式规格不合）：终端打一行，同一个模板只打一次。
    测试里 NET == "strict" 时直接抛，让写坏的模板当场红。"""
    if NET == "strict":
        raise exc
    key = (str(bi), bi.en)
    with _bad_lock:
        if key in _bad_once:
            return
        _bad_once.add(key)
    try:
        print("[i18n] 英文模板出错：{}（{}: {}）".format(str(bi)[:40], type(exc).__name__, exc))
    except Exception:            # 终端写不了也不能连累调用方
        pass


# ---- 生效语言（spec §3.1） -------------------------------------------------------------

def _lang_of_tag(tag):
    """语言标签 → zh / en：zh 开头（含 zh-Hant、zh-HK、zh-TW）是中文，其余一律英文。"""
    return ZH if str(tag).strip().lower().startswith("zh") else EN


def _resolve():
    """按 §3.1 的顺序定生效语言：一次性覆盖 > 闸 > 设置里的 zh/en > 跟随系统（检测失败按中文）。
    设置缺失时 _state["choice"] 里已经是迁移规则算出来的值（见 _choice_from）。"""
    if _state["override"] in (ZH, EN):
        return _state["override"]
    return _saved_lang()


def _saved_lang():
    """不看一次性覆盖、只按闸与设置（加系统语言）解析出的语言，即下次正常启动时的界面语言。"""
    if not I18N_ENABLED:
        return ZH
    choice = _state["choice"]
    if choice in (ZH, EN):
        return choice
    return _state["system"] if _state["system"] in (ZH, EN) else ZH


def _migrated_choice(env):
    """设置里还没有 ui_lang 时该写什么（用户 09-28 决定 1）：新装跟随系统，其余固定中文。
    环境变量缺失（比如没经过 boot）也按老装机处理：宁可保持中文，不让界面突然变英文。"""
    return "system" if env.get(ENV_INSTALL) == "fresh" else ZH


def _defaults_to_english_captions(loaded, env):
    """提交 T（用户 09-28 决定 3）：闸开着、新装、界面解析为英文、设置里从没有过 target_lang，
    翻译目标语言默认写 English。老装机和已经选过目标语言的一律不动。loaded 是 settle 读到的、
    迁移之前的设置。

    只在第一次启动那一次 settle 里成立，两道条件各管一半：
    - loaded 里还没有 ui_lang：T 与 ui_lang 迁移发生在同一次 settle 里，之后设置里总有 ui_lang；
    - boot 判的是 fresh：settle 结束时把环境变量改成 existing。一键更新用 os.execv 重启
      （updater.py），环境整条继承，不改的话重启后的进程还算新装——用户第一次启动后在设置里把
      界面改成英文、没动过目标语言，更新重启就会被静默写成英文字幕。
    看的是设置解析出的语言、不看一次性覆盖：覆盖不落盘（--ui-lang 用于截图、开发），
    不能拿它去定一个会落盘的默认值。"""
    return (I18N_ENABLED and env.get(ENV_INSTALL) == "fresh" and "ui_lang" not in loaded
            and "target_lang" not in loaded and _saved_lang() == EN)


def _choice_from(data, env):
    value = data.get("ui_lang") if isinstance(data, dict) else None
    return value if value in CHOICES else _migrated_choice(env)


def _system_from(env):
    value = env.get(ENV_SYSTEM)
    return value if value in (ZH, EN) else None


def _override_from(argv, env):
    """一次性覆盖：命令行 --ui-lang zh|en 优先，其次环境变量 TLT_UI_LANG。不落盘。
    直接读 argv：boot 跑在 argparse 和自举对话框之前。取值不合法就当没给。"""
    cli = None
    args = [str(a) for a in (argv or [])]
    for i, arg in enumerate(args):
        if arg == "--ui-lang" and i + 1 < len(args):
            cli = args[i + 1]
        elif arg.startswith("--ui-lang="):
            cli = arg.split("=", 1)[1]
    if cli in (ZH, EN):
        return cli
    value = str(env.get(ENV_OVERRIDE) or "").strip().lower()
    return value if value in (ZH, EN) else None


def _exists(path):
    try:
        return path.exists()
    except OSError:              # 看不了就当它在：判成老装机，界面保持中文
        return True


def _peek_settings(path):
    """只读地看一眼 settings.json：任何异常都当作空设置。不改名、不写回、不碰损坏标记。"""
    try:
        data = json.loads(path.read_bytes())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def boot(root, argv=None, environ=None):
    """每个进程都跑一次（main.py 里紧跟 remember_launch_python），**只读**，绝不抛异常。

    1. 装机状态只记第一次：settings.json 存在（不管内容好坏）或 .venv 里有「装成功过」的记号
       （INSTALL_STAMP）记 existing，否则 fresh。必须在第一个进程里判——新装机上 ensure_env
       会先建出 .venv、装完写记号再 execv。光有 .venv 目录不算：setup.sh / setup.ps1 一开头就建
       .venv，再用它跑 main.py --doctor；第一次启动装到一半失败（断网、中途退出）也会留下 .venv。
       这两种都还没真正用过，按老装机判会把英文系统上的新用户永远固定成中文界面、中文字幕。
    2. 系统语言每次启动只检测一次，结果写进环境变量，exec 之后的进程直接用。
    3. 自己读 settings.json 的字节定出语言，供自举期的对话框用。返回生效语言。"""
    env = os.environ if environ is None else environ
    argv = sys.argv if argv is None else argv
    try:
        root = Path(root)
        path = root / "settings.json"
        if not env.get(ENV_INSTALL):
            stamp = root / ".venv" / INSTALL_STAMP
            env[ENV_INSTALL] = "existing" if _exists(path) or _exists(stamp) else "fresh"
    except Exception:
        return _state["lang"]
    try:
        if not env.get(ENV_SYSTEM):
            env[ENV_SYSTEM] = system_lang() or "none"
    except Exception:            # 检测本身不抛；这里防的是替身。没记下就留给下一个进程再测
        pass
    try:
        _state.update(override=_override_from(argv, env), system=_system_from(env),
                      choice=_choice_from(_peek_settings(path), env))
        _state["lang"] = _resolve()
    except Exception:
        pass
    return _state["lang"]


def settle(root, environ=None):
    """只在最终进程里跑一次（main() 里 relaunch_inside_bundle 之后、parse_args 之前），绝不抛异常。

    这里才第一次 load_settings()：文件损坏就在这个进程里备份，界面提示和审计的备份名都拿得到。
    闸开着、设置里还没有 ui_lang 时迁移，写一次（新装 system，其余 zh）；闸关着一个字节都不写——
    否则闸关着那几周的新装机会被写成 system，闸一开就突然变英文。
    同样只在闸开着时：新装、界面解析为英文、没设置过 target_lang，再写 target_lang="en"。
    这发生在 main.py 读 target_lang 之前，所以第一次启动就是英文字幕。返回生效语言。
    结束时（不论成败）把装机状态记成 existing：这之后 exec 出来的进程（一键更新重启）不再算
    新装。否则闸关着时装上、用了几周的机器，一键更新到开闸的版本时会按新装迁移成跟随系统，
    界面突然变英文（用户决定 1）；T 也会在更新重启时再判一次。
    root 与 boot 对称，由调用方传 ROOT；设置文件以 app.settings.SETTINGS_FILE 为准（就是 ROOT 下那个）。"""
    env = os.environ if environ is None else environ
    try:
        from . import settings as _settings
        loaded = data = _settings.load_settings()
        if I18N_ENABLED and "ui_lang" not in data:
            value = _migrated_choice(env)
            _settings.save_setting("ui_lang", value)
            data = dict(data, ui_lang=value)
        _state.update(system=_system_from(env), choice=_choice_from(data, env))
        _state["lang"] = _resolve()
        if _defaults_to_english_captions(loaded, env):   # 放在语言定下之后：写失败也不影响界面语言
            _settings.save_setting("target_lang", EN)
    except Exception:
        pass
    try:
        env[ENV_INSTALL] = "existing"
    except Exception:
        pass
    return _state["lang"]


def set_choice(choice):
    """界面上换了语言设置：更新全局状态，返回新的生效语言。落盘由调用方做。"""
    if choice in CHOICES:
        _state["choice"] = choice
        _state["lang"] = _resolve()
    return _state["lang"]


def config_info():
    """并进 server.config 的语言字段（只在 enabled() 时并入，spec §3.5）。
    ui_lang_system 是「跟随系统」此刻会解析成的语言（检测失败就是 zh），供设置行摘要用。"""
    return {"ui_lang": _state["lang"], "ui_lang_setting": _state["choice"],
            "ui_lang_system": _state["system"] or ZH, "ui_lang_available": enabled(),
            "ui_lang_locked": _state["override"] is not None}


@contextmanager
def use(lang):
    """测试用：临时把界面语言定成 lang。按一次性覆盖处理，所以里面 enabled() 为真。"""
    if lang not in (ZH, EN):
        raise ValueError("use() takes 'zh' or 'en', got {!r}".format(lang))
    saved = dict(_state)
    _state.update(override=lang, lang=lang)
    try:
        yield lang
    finally:
        _state.update(saved)


# ---- 系统语言（spec §3.2，只在后端做） ------------------------------------------------

# `defaults read … AppleLanguages` 的输出形如 ("en-US", "zh-Hans-US")；不带连字符的标签不加引号
_APPLE_FIRST = re.compile(r'\(\s*"?([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*)')


def _mac_lang():
    """先看系统设置里给这个 App 单独选的语言，再看全局首选语言。都读不到返回 None。"""
    for argv in (["defaults", "read", BUNDLE_ID, "AppleLanguages"],
                 ["defaults", "read", "-g", "AppleLanguages"]):
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=2)
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        match = _APPLE_FIRST.search(proc.stdout or "")
        if match:
            return _lang_of_tag(match.group(1))
    return None


def _env_lang(environ):
    """其它平台：依次看 LANGUAGE、LC_ALL、LC_MESSAGES、LANG；C/POSIX 不算语言，跳过。"""
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        first = next((p.strip() for p in str(environ.get(name) or "").split(":") if p.strip()), "")
        if not first or first in ("C", "POSIX") or first.startswith("C."):
            continue
        return _lang_of_tag(first)
    return None


def system_lang():
    """系统界面语言：zh / en；检测不了返回 None（按中文处理）。绝不抛异常。

    不用 locale.getdefaultlocale()：3.11 起弃用，而且取的是区域格式，不是界面语言。
    Windows 取显示语言 GetUserDefaultUILanguage，主语言号 0x04 是中文（简繁都算）。"""
    try:
        if sys.platform == "darwin":
            return _mac_lang()
        if sys.platform == "win32":
            import ctypes
            code = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return ZH if (int(code) & 0x3FF) == 0x04 else EN
        return _env_lang(os.environ)
    except Exception:
        return None


def accept_lang(header):
    """手机页用：按 Accept-Language 的 q 值取第一项，zh* 为中文，其余英文；缺失或读不懂按中文。"""
    best, best_q = None, 0.0
    try:
        for part in str(header or "").split(","):
            fields = part.split(";")
            tag = fields[0].strip()
            if not tag or tag == "*":
                continue
            q = 1.0
            for field in fields[1:]:
                key, _, value = field.strip().partition("=")
                if key.strip().lower() == "q":
                    try:
                        q = float(value.strip())
                    except ValueError:
                        q = 0.0
            if q > best_q:                       # 严格大于：同分时保留先出现的
                best, best_q = tag, q
    except Exception:
        return ZH
    return ZH if best is None else _lang_of_tag(best)


# ---- G9 运行时网（spec §12.1；只在测试里开，生产 NET 为 None） ---------------------------

# 每种发往桌面界面的消息里，哪些字段是界面散文（会原样显示给中控的句子）。
# 路径写法："a.b" 进字典，"a[]" 逐个列表元素，"*" 字典里的每个值。
# 新加一种消息类型要登记在这里或 DATA_ONLY：没登记的类型，运行时网记一条违例。
_CHECKS = ("checks[].name", "checks[].detail", "checks[].fix")
_ENGINE = ("active_label", "note")
_DISK = ("items[].label", "items[].note")        # hf / ollama 的 label 是模型名：ASCII，不会误报
_VIEWER = ("note", "rotate_confirm", "error")
UI_FIELDS = {
    "status": ("detail",),
    "notice": ("text",),
    "incident": ("text",),
    "health": ("text",),
    "caption": ("why",),
    "caption_update": ("why",),
    "alert": ("why",),
    "alert_update": ("why",),
    "comment_source": ("detail",),
    "selfcheck": _CHECKS,
    "engine": _ENGINE,
    "disk": _DISK,
    "viewer": _VIEWER,
    # config 的各个键并进 server.config，hello 时整份重放
    "config": (("status.detail", "incidents.*.text", "comment_detail", "update_check.note")
               + tuple("selfcheck." + p for p in _CHECKS)
               + tuple("engine." + p for p in _ENGINE)
               + tuple("disk." + p for p in _DISK)
               + tuple("viewer." + p for p in _VIEWER)),
}
# 只带数字、开关或数据（主播名、词条、字幕和弹幕的原文译文、GitHub 上的版本说明）的消息类型
DATA_ONLY = frozenset({
    "update_available", "updating", "update_aborted", "glossary_migration", "comment",
    "comment_update", "stats", "watchlist", "alert_mode", "recent_rooms", "session_break",
})
# check_outbound 记下的违例：(kind, type, path, text, test)。kind 是 "unregistered"（没登记的
# 消息类型）或 "plain"（界面字段上的普通中文 str，即漏写了 L()）。tests/conftest.py 汇总。
NET_HITS = []
_NET_PIPES = ("server.py", "i18n.py", "viewer.py")   # 广播本身的管道：往上找真正的发出者
_NET_WRAPPERS = ("broadcast", "status", "fanout")    # 测试里 RecordingServer 一类的包装同理
_net_paths = {}


def ui_values(msg):
    """msg 里登记为界面文字的字段，逐个交出 (路径, 值)。认不出的类型什么都不交。"""
    if not isinstance(msg, dict):
        return
    for path in UI_FIELDS.get(msg.get("type"), ()):
        for value in _walk(msg, path.split(".")):
            yield path, value


def _walk(node, parts):
    if not parts:
        yield node
        return
    head, rest = parts[0], parts[1:]
    if head == "*":
        if isinstance(node, dict):
            for value in node.values():
                yield from _walk(value, rest)
        return
    many = head.endswith("[]")
    key = head[:-2] if many else head
    if not isinstance(node, dict) or key not in node:
        return
    child = node[key]
    if not many:
        yield from _walk(child, rest)
    elif isinstance(child, (list, tuple)):
        for value in child:
            yield from _walk(value, rest)


def _net_frame(frame):
    """(这一帧是否在 tests/ 下, 是否属于广播管道)，按代码对象缓存。"""
    code = frame.f_code
    hit = _net_paths.get(code)
    if hit is None:
        import asyncio
        here = os.path.dirname(os.path.abspath(__file__))
        tests = os.path.normcase(os.path.join(os.path.dirname(here), "tests")) + os.sep
        name = os.path.normcase(os.path.abspath(code.co_filename))
        pipes = {os.path.normcase(os.path.join(here, n)) for n in _NET_PIPES}
        loop = os.path.normcase(os.path.dirname(os.path.abspath(asyncio.__file__))) + os.sep
        piped = (name in pipes or name.startswith(loop)
                 or (code.co_name in _NET_WRAPPERS and "self" in code.co_varnames[:1]))
        hit = _net_paths[code] = (name.startswith(tests), piped)
    return hit


def _fed_by_test(frame):
    """这条消息是不是测试直接喂给 broadcast / fanout 的夹具（如「电脑休眠过」这类中文 text）。

    从调用者往上走：跳过广播管道本身（server.py、i18n.py、viewer.py）、asyncio 的事件循环
    以及测试里对 broadcast / status 的包装，第一个剩下的帧在 tests/ 下就是夹具，跳过不查。
    谁 await 这条广播就算谁的：生产函数交回协程、由测试 await 的（handle_control 的几支），
    也算测试喂的——这是漏网而不是误报，G7 / G10 另外兜着。"""
    while frame is not None:
        in_tests, piped = _net_frame(frame)
        if not piped:
            return in_tests
        frame = frame.f_back
    return False


def check_outbound(msg):
    """G9 运行时网：生产代码发往界面的消息里，界面文字字段上出现普通中文 str（不是 Bi）
    就是漏写了 L()，记一条违例；没登记的消息类型也记。只查中文臂，不查 Bi 英文里的
    CJK（那可能是数据，归 G7）。

    生产里 NET 为 None，第一行就返回。只记不抛：违例是否让用例失败由 tests/conftest.py 定，
    绝不能因为这张网让一条广播在半路抛异常。"""
    if NET is None:
        return
    try:
        if not isinstance(msg, dict) or _fed_by_test(sys._getframe(1)):
            return
        mtype = msg.get("type")
        test = os.environ.get("PYTEST_CURRENT_TEST", "")
        if mtype not in UI_FIELDS and mtype not in DATA_ONLY:
            NET_HITS.append(("unregistered", str(mtype), "", "", test))
            return
        for path, value in ui_values(msg):
            if isinstance(value, str) and not isinstance(value, Bi) and CJK.search(value):
                NET_HITS.append(("plain", mtype, path, value[:40], test))
    except Exception:
        pass
