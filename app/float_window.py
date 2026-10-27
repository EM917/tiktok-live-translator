# i18n: done
"""浮窗：直播中把最新字幕和最近几条弹幕放进一个小的、始终在最上面的窗口。

只做显示，不碰识别、检测、审计、翻译：窗口里的页面（web/float.html）自己连 WebSocket、只收不发，
服务端对它的连接另有一套隔离（app/server.py 的 role=float）。这个模块只管「窗口本身」：
什么时候建、什么时候销毁、位置和大小怎么记；外加一件和窗口有关的事——监听自己停了时页面上的结束卡片
有个「回到主窗口」按钮，它经 pywebview 的 js_api（FloatApi）调到这里的 focus_main，把主窗口提到最前。
这条路只在本机两扇窗口之间，不经过 WebSocket，也不碰管线。

放在 app/ 里是为了能测：main.py 导入时会 execv 进虚拟环境，测试不能 import 它；也不 import webview
（CI 没装），建窗函数由 main.py 注入。

线程：建窗、销毁一律在短命的工作线程里做（pywebview 的 create_window 在 start() 之后从非主线程调用会
立刻建窗，winforms 上还会等；事件循环线程上不能有 GUI 调用，否则会拖识别）。窗口的 moved / resized /
closed 事件由 pywebview 每次新开一个线程触发，处理函数只改内存、重置去抖定时器。

不用无边框：cocoa 的 easy_drag 靠 convertBaseToScreen_（自 10.7 起弃用），无边框还会把红绿灯按钮藏掉，
拖动和关闭都靠不住。保留原生标题栏，拖动、缩放、关闭都是系统自带的。透明只开 macOS；别的平台
webview 背景叠在不透明的 Form 上，并不真透，页面用不透明深色底（?solid=1）。
"""
import functools
import inspect
import math
import sys
import threading

from . import i18n
from .i18n import L
from .settings import load_settings, save_setting

SETTINGS_KEY = "float_window"
FLOAT_PATH = "/float"
FLOAT_TITLE = L("浮窗", "Floating Window")
OPEN_FAILED = L("无法打开浮窗。", "Couldn’t open the Floating Window.")

DEFAULT_SIZE = (460, 330)
MIN_SIZE = (260, 140)
MAX_SIZE = (2400, 1600)
POSITION_LIMIT = 20000
VISIBLE_MARGIN = (80, 40)       # 窗口至少要有这么宽、这么高的一块落在屏幕里，标题栏才够得着
DEBOUNCE_SEC = 0.8              # 位置、大小停稳这么久才写 settings.json
STUCK_EXIT_SEC = 10.0           # 主窗口关了、10 秒后 GUI 循环还没退出，走一遍收尾直接退出
SHOWN_WAIT_SEC = 8.0            # create_window 返回后最多等这么久窗口才算真的显示出来
SOLID_BACKGROUND = "#101218"
DEFAULT_MARGIN = (24, 110)      # 第一次开（没记过位置）：贴屏幕右下角，离右边 24、离下边 110（让开 Dock）
MENU_BAR_ROOM = 40              # 靠上放时离屏幕顶端至少这么多（菜单栏）
LIMITS = {"x": (-POSITION_LIMIT, POSITION_LIMIT), "y": (-POSITION_LIMIT, POSITION_LIMIT),
          "width": (MIN_SIZE[0], MAX_SIZE[0]), "height": (MIN_SIZE[1], MAX_SIZE[1])}

# macOS：NSWindowCollectionBehaviorCanJoinAllSpaces(1<<0) | FullScreenAuxiliary(1<<8)。
# 没有它们，别的软件全屏时（独占一个 Space）置顶窗口不会跟过去
MAC_JOIN_ALL_SPACES = (1 << 0) | (1 << 8)


# ---- 位置和大小的校验 -------------------------------------------------------------------

def _number(value, low, high):
    """整数（或整数值的浮点）且在 [low, high] 里才算；bool、NaN、字符串一律当没有。
    不夹到边界：夹了会把一个坏值写成「看起来像用户选的」（同 settings.normalize_caption_size）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    number = int(math.floor(value + 0.5))
    return number if low <= number <= high else None


def _boxes(screens):
    boxes = []
    for item in screens or ():
        try:
            x, y, w, h = (int(v) for v in item[:4])
        except (TypeError, ValueError):
            continue
        if w > 0 and h > 0:
            boxes.append((x, y, w, h))
    return boxes


def on_screen(x, y, width, screens):
    """位置还落在已知屏幕上吗（非 macOS 用；macOS 见 mac_screen_for）。粗略判断，目的只是显示器拔了之后
    不把窗口开到屏幕外：一块屏时严格判断，多块屏时放宽到整体包围盒。"""
    boxes = _boxes(screens)
    if not boxes:
        return False
    left = min(b[0] for b in boxes)
    right = max(b[0] + b[2] for b in boxes)
    top = min(b[1] for b in boxes)
    bottom = max(b[1] + b[3] for b in boxes)
    need_w, need_h = VISIBLE_MARGIN
    if x + width < left + need_w or x > right - need_w:
        return False
    if len(boxes) == 1:
        return top <= y <= bottom - need_h
    span = bottom - top
    return top - span + need_h <= y <= bottom - need_h


def normalize_geometry(raw, screens=None, mac=False):
    """settings.json 里记的浮窗位置和大小 → {"width","height","x","y"}，x、y 可能是 None（让系统摆）。

    宽高必须同时合法，否则用默认大小；位置要同时有 x、y、落在屏幕上才用。读不到屏幕列表也不用位置：
    宁可让系统摆，不赌它还在屏幕里。mac=True：x、y 是 Cocoa 全局坐标里窗口的左上角（见下面的
    macOS 坐标一节），逐块屏幕判断；否则是 pywebview 的窗口坐标（on_screen）。"""
    raw = raw if isinstance(raw, dict) else {}
    width = _number(raw.get("width"), MIN_SIZE[0], MAX_SIZE[0])
    height = _number(raw.get("height"), MIN_SIZE[1], MAX_SIZE[1])
    if width is None or height is None:
        width, height = DEFAULT_SIZE
    x = _number(raw.get("x"), -POSITION_LIMIT, POSITION_LIMIT)
    y = _number(raw.get("y"), -POSITION_LIMIT, POSITION_LIMIT)
    if x is None or y is None:
        x = y = None
    elif mac:
        if mac_screen_for(x, y, width, height, screens) is None:
            x = y = None
    elif not on_screen(x, y, width, screens):
        x = y = None
    return {"width": width, "height": height, "x": x, "y": y}


# ---- macOS 的坐标 -------------------------------------------------------------------------
# pywebview 6.2.1 的 cocoa 后端（webview/platforms/cocoa.py），三处对不上：
#   moved(x, y)       x = 窗口 frame.origin.x（Cocoa 全局坐标，原点在主屏左下角），
#                     y = 窗口所在那块屏的高 - 窗口高 - frame.origin.y（没有加那块屏自己的 origin.y）；
#   create_window(x, y)  落在 (screen.origin.x + x, screen.origin.y + screen.height - y)，
#                     screen 是 create_window(screen=) 传的那块，没传就是当时的 NSScreen.mainScreen()。
# 只有那块屏的原点恰好是 (0, 0)（单屏，或窗口在主屏上）时，把 moved 报的数原样喂回 create_window 才对得上；
# 窗口在原点不是 (0, 0) 的外接屏上，整体会偏一块屏。所以记的不是 moved 报的数，而是换算出来的
# Cocoa 全局左上角 (left, top)（top 是窗口上沿，y 向上），开窗时再按「它在哪块屏上」换成
# create_window(screen=, x=, y=)。不认识 screen= 的老版本 pywebview 就不传位置，让系统摆。

class ScreenBox(tuple):
    """一块屏幕：(x, y, 宽, 高)，另带 pywebview 的 Screen 对象（macOS 开窗时 screen= 要用它）。
    比较、解包都和普通四元组一样。"""

    def __new__(cls, x, y, width, height, screen=None):
        box = super().__new__(cls, (x, y, width, height))
        box.screen = screen
        return box


def _mac_screens(screens):
    """[(x, y, 宽, 高, Screen 对象或 None)]；坐标是 Cocoa 全局的（y 向上）。认不出的条目跳过。"""
    out = []
    for item in screens or ():
        try:
            x, y, w, h = (int(v) for v in item[:4])
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        obj = getattr(item, "screen", None)
        if obj is None and len(item) > 4:
            obj = item[4]
        out.append((x, y, w, h, obj))
    return out


def _overlap(ax, ay, aw, ah, bx, by, bw, bh):
    w = min(ax + aw, bx + bw) - max(ax, bx)
    h = min(ay + ah, by + bh) - max(ay, by)
    return w * h if w > 0 and h > 0 else 0


def mac_top_left(x, y, width, height, screens):
    """pywebview 在 macOS 上 moved(x, y) 报的数 → 窗口左上角的 Cocoa 全局坐标 (left, top)；认不出就是 None。

    窗口上沿 top = 窗口所在屏幕的高 - y。窗口在哪块屏上要从屏幕列表里推：逐块屏幕试一遍 top，
    取窗口矩形和这块屏重叠最多的那块。"""
    best = None
    for sx, sy, sw, sh, _obj in _mac_screens(screens):
        top = sh - y
        area = _overlap(x, top - height, width, height, sx, sy, sw, sh)
        if area > 0 and (best is None or area > best[0]):
            best = (area, top)
    return None if best is None else (x, best[1])


def mac_screen_for(left, top, width, height, screens):
    """窗口左上角 (left, top)（Cocoa 全局）该放在哪块屏上：标题栏够得着（边距同 on_screen）的屏里，
    窗口和它重叠最多的那块；没有就是 None。返回 (x, y, 宽, 高, Screen 对象)。"""
    need_w, need_h = VISIBLE_MARGIN
    best = None
    for box in _mac_screens(screens):
        sx, sy, sw, sh, _obj = box
        if left + width < sx + need_w or left > sx + sw - need_w:
            continue
        if top > sy + sh or top < sy + need_h:
            continue
        area = _overlap(left, top - height, width, height, sx, sy, sw, sh)
        if best is None or area > best[0]:
            best = (area, box)
    return None if best is None else best[1]


def mac_create_position(left, top, width, height, screens):
    """记下的 Cocoa 全局左上角 → (Screen 对象, x, y)，交给 create_window(screen=, x=, y=)。
    pywebview 会把它摆回 (screen.x + x, screen.y + screen.height - y)，正好是 (left, top)。
    屏幕不在了、或没有 Screen 对象可传，返回 None。"""
    box = mac_screen_for(left, top, width, height, screens)
    if box is None or box[4] is None:
        return None
    sx, sy, sw, sh, obj = box
    return obj, left - sx, sy + sh - top


def mac_default_position(width, height, screens, anchor=None):
    """没记过位置时第一次开：贴着屏幕右下角（不再压在屏幕正中间）。返回 (Screen 对象, x, y) 或 None。

    哪块屏：只有一块就是它；多块时是主窗口所在的那块（anchor() 给主窗口的 (x, 宽)，x 是 Cocoa 全局的），
    认不出主窗口在哪块屏上就返回 None，让系统摆——系统摆在有键盘焦点的主窗口那块屏的正中间。"""
    boxes = _mac_screens(screens)
    chosen = None
    if len(boxes) == 1:
        chosen = boxes[0]
    elif len(boxes) > 1 and anchor is not None:
        try:
            ax, aw = anchor()
            center = float(ax) + float(aw) / 2.0
        except Exception:
            center = None
        if center is not None and math.isfinite(center):
            for box in boxes:
                if box[0] <= center < box[0] + box[2]:
                    chosen = box
                    break
    if chosen is None or chosen[4] is None:
        return None
    sx, sy, sw, sh, obj = chosen
    return (obj, max(0, sw - width - DEFAULT_MARGIN[0]),
            max(MENU_BAR_ROOM, sh - height - DEFAULT_MARGIN[1]))


def mac_window_frame(window):
    """macOS 上这扇窗此刻真实的位置和大小，按 settings.json 的记法：{"x": 左边, "y": 上沿（都是 Cocoa
    全局坐标，y 向上）, "width", "height"}（宽高是含标题栏的 frame，同 create_window 的 width / height
    和 resized 事件）。读不到（没有 native、读的时候抛了、数不合理）是 None。

    记位置以它为准，moved / resized 事件只当「动过了」的信号，不信它报的数。2026-10-16 真机实测：第一次
    开在右下角，没动它就关，记下的位置比窗口真实的位置高 84pt、左 1pt，再开就跳了上去；之后开关不再漂。
    哪一条事件报了那个数没有查实，但已知有一条路会报旧数：pywebview 6.2.1 的 Event.set 每次新开一个线程，
    **到那个线程跑起来时**才去遍历处理函数，建窗过程中（BrowserView.__init__ 里先 setFrameOrigin_ 到屏幕
    原点、再 setFrameTopLeftPoint_ 到目标位置）的 moved 就可能落进刚挂上的处理函数，报的是早就不是现在的
    位置。所以不管事件说了什么，记下的都是窗口真实的 frame。读法同 pywebview 自己的 get_position：在调用
    线程上直接读 NSWindow.frame()。"""
    native = getattr(window, "native", None)
    if native is None:
        return None
    try:
        frame = native.frame()
        left, bottom = frame.origin.x, frame.origin.y
        width, height = frame.size.width, frame.size.height
        top = bottom + height
    except Exception:
        return None
    out = {"x": _number(left, *LIMITS["x"]), "y": _number(top, *LIMITS["y"]),
           "width": _number(width, *LIMITS["width"]), "height": _number(height, *LIMITS["height"])}
    return None if None in out.values() else out


def screens_from_webview(webview):
    """pywebview 的屏幕列表 → [ScreenBox]（比较起来就是 (x, y, 宽, 高)）。读不到（还没初始化、平台不支持）返回空列表。"""
    def read():
        try:
            return [ScreenBox(s.x, s.y, s.width, s.height, s) for s in webview.screens]
        except Exception:
            return []
    return read


# ---- 开浮窗别清掉共用的 WebKit 数据（macOS） ------------------------------------------------
# pywebview 6.2.1 的 cocoa 后端：每建一个窗口，BrowserView.__init__（主线程上）只要 _state['private_mode']
# 是真（main.py 的 webview.start() 没传它，默认就是真），就对 WKWebsiteDataStore.defaultDataStore() 调
# removeDataOfTypes(全部类型, 自 1970 年起)。这个 store 按 bundle id 存，整个 .app 共用一份：主页面的
# localStorage / sessionStorage 在里面，app/webkit_fetch.py（第 2 层流地址解析，同一个 bundle 里的子进程，
# 刻意 private_mode=False）攒下的 TikTok cookie 也在里面——它的注释写着干净的 store 有的房间 20 秒拿不到、
# 带 cookie 2 秒就位。启动开主窗口时清一次本来就有；直播中开浮窗会再清一次，之后这次运行里的
# WebKit 层解析（断流重连、换主播）就从空 store 开始。
# 所以浮窗建窗之前把 private_mode 改成 False，不改回：start() 之后这个进程只会再建浮窗，cocoa 只在
# BrowserView.__init__ 里读它一次；_state 是 ImmutableDict，改已有的键是允许的。只在 macOS、只在核对过
# 源码的 pywebview 大版本上做；别的平台（edgechromium 读它三处，有一处在之后的异步回调里）、别的版本照旧。
VERIFIED_PYWEBVIEW_MAJOR = "6"


def keep_website_data(webview, platform=None, version=None):
    """给 FloatWindowHost 用的 create_window：macOS + pywebview 6 上，建窗之前关掉 private_mode，
    这样开浮窗不会清掉主页面和流地址解析共用的 WebKit 数据。别的情况原样返回 webview.create_window。
    签名照旧（functools.wraps）：supported_kwargs 看的还是 pywebview 自己的参数表。"""
    create = webview.create_window
    if (sys.platform if platform is None else platform) != "darwin":
        return create
    if version is None:
        try:
            from importlib.metadata import version as dist_version
            version = dist_version("pywebview")
        except Exception:
            version = ""
    if str(version).split(".")[0] != VERIFIED_PYWEBVIEW_MAJOR:
        return create

    @functools.wraps(create)
    def create_keeping_data(*args, **kwargs):
        try:
            state = webview._state
            if state["private_mode"]:
                state["private_mode"] = False
        except Exception as exc:
            print("[警告] 没能关掉 pywebview 的隐私模式，打开浮窗会清掉 WebKit 数据：{}".format(exc))
        return create(*args, **kwargs)

    return create_keeping_data


def supported_kwargs(fn, kwargs):
    """fn 认的参数才传：requirements 只要求 pywebview>=5，多传一个老版本不认的参数会抛 TypeError。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


# ---- macOS 的收尾调整（尽力而为） ---------------------------------------------------------

def apply_mac_tweaks(native, dark_appearance=None):
    """在主线程上调：跟着任何 Space 走、能浮在全屏软件上面；标题栏用深色外观，和深色的页面连成一体。"""
    native.setCollectionBehavior_(MAC_JOIN_ALL_SPACES)
    if dark_appearance is not None:
        native.setAppearance_(dark_appearance)


def mac_float_tweaks(window, wait=5.0, call_after=None, dark_appearance=None):
    """窗口真的显示出来之后，把 apply_mac_tweaks 交给主线程做。任何一步不行都只打一行警告：
    置顶本身（on_top）已经在建窗时设好了，这里只是锦上添花。返回是否交给了主线程。"""
    try:
        if not window.events.shown.wait(wait):
            return False
        native = getattr(window, "native", None)
        if native is None:
            return False
        if call_after is None:
            from PyObjCTools import AppHelper
            call_after = AppHelper.callAfter
        if dark_appearance is None:
            import AppKit
            dark_appearance = AppKit.NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
        call_after(lambda: apply_mac_tweaks(native, dark_appearance))
        return True
    except Exception as exc:
        print("[警告] 浮窗的 macOS 收尾调整没有生效：{}".format(exc))
        return False


# ---- 回到主窗口（结束卡片的按钮） ----------------------------------------------------------

def bring_to_front(window, mac=None, call_after=None):
    """把一扇 pywebview 窗口提到最前、让程序到前台（pywebview 的 show：cocoa 上是 makeKeyAndOrderFront
    加 activateIgnoringOtherApps，winforms 上是 Show + Activate；两边都自己切到界面线程）。

    macOS 上窗口缩进了 Dock 的先还原。只在真的缩进去了时才调 restore：winforms 的 restore 会把最大化的
    窗口改回普通大小，所以别的平台只调 show。

    调用方是 pywebview JS 桥的工作线程（FloatApi.focus_main），不是主线程。macOS 上「缩进去没有」要问 AppKit
    （native.isMiniaturized()），所以整件事——读它、restore、show——交给主线程做（AppHelper.callAfter，
    call_after 是测试替身用的）；restore / show 在主线程上照样由 pywebview 自己再排一次 callAfter，先后不变。
    别的平台、或者窗口还没有 native 时，这里不碰原生对象，直接调 show。

    返回是否做成：macOS 上是有没有交给主线程，主线程上那一步出错只打一行警告（见 _front_on_main）；
    任何一步不行都只打一行警告，不抛。"""
    if window is None:
        return False
    mac = (sys.platform == "darwin") if mac is None else mac
    try:
        native = getattr(window, "native", None) if mac else None
        if native is None:
            window.show()
            return True
        if call_after is None:
            from PyObjCTools import AppHelper
            call_after = AppHelper.callAfter
        call_after(functools.partial(_front_on_main, window, native))
        return True
    except Exception as exc:
        print("[警告] 没能把主窗口提到前面：{}".format(exc))
        return False


def _front_on_main(window, native):
    """bring_to_front 交给主线程的那一步（macOS）：缩进 Dock 的先还原，再 show。异常不往 AppKit 的
    事件循环里抛，只打一行警告。pywebview 的 restore / show 要先等窗口的 shown 事件：主窗口早就显示过、
    这个事件不会再清掉，所以在主线程上调不会干等。"""
    try:
        if native.isMiniaturized():
            window.restore()
        window.show()
    except Exception as exc:
        print("[警告] 没能把主窗口提到前面：{}".format(exc))


class FloatApi:
    """浮窗页的 js_api：页面里是 window.pywebview.api.focus_main()，只有这一个。

    pywebview 把 js_api 对象上不以下划线开头的属性都挂到页面上（属性是对象的还会往里递归），
    所以宿主存在 _host 里，对页面只露 focus_main。pywebview 每次 JS 调用都新开一个线程执行，
    这里不碰事件循环。"""

    def __init__(self, host):
        self._host = host

    def focus_main(self):
        return self._host.focus_main()


# ---- 窗口宿主 ----------------------------------------------------------------------------

def _spawn_thread(fn):
    threading.Thread(target=fn, daemon=True).start()


_DEFAULT_TWEAKS = object()      # mac_tweaks 没传：macOS 上用 mac_float_tweaks；传 None 是明确关掉
_DEFAULT_FRAME = object()       # frame_reader 没传：macOS 上用 mac_window_frame；传 None 是只信事件报的数


class FloatWindowHost:
    """浮窗的建立、销毁和位置记忆。create_window 是 pywebview 的 webview.create_window（main.py 注入）。

    对外只有 request_open / request_close / shutdown / is_open / focus_main 和 on_change 回调：
    - request_*：不阻塞，记下「最后想要的状态」，由工作线程把窗口调到那个状态（连点开、关、开，
      以最后一条为准）；
    - shutdown：同步，主窗口关了之后调，保证浮窗一起关（否则 GUI 循环不会退出）；
    - focus_main：浮窗页结束卡片的「回到主窗口」经 js_api 调到这里（FloatApi），把 main_window 提到最前；
    - on_change(is_open, error)：状态变了（任何线程里调），error 是要告诉用户的一句双语话或 None。"""

    def __init__(self, create_window, base_url, platform=None, screens=None, load=None, save=None,
                 spawn=None, timer_factory=None, mac_tweaks=_DEFAULT_TWEAKS, on_change=None,
                 shown_wait=SHOWN_WAIT_SEC, frame_reader=_DEFAULT_FRAME, call_after=None):
        self._create = create_window
        self._shown_wait = shown_wait
        self._base_url = str(base_url).rstrip("/")
        self._mac = (sys.platform if platform is None else platform) == "darwin"
        self._transparent = self._mac
        self._screens = screens or (lambda: [])
        self._load = load or load_settings
        self._save = save or save_setting
        self._spawn = spawn or _spawn_thread
        self._timer_factory = timer_factory or threading.Timer
        # macOS 的收尾调整（加入所有桌面空间、深色标题栏）尽力而为、没在真机上验证过：
        # 不传就是 macOS 上用 mac_float_tweaks，传 None 关掉
        if mac_tweaks is _DEFAULT_TWEAKS:
            mac_tweaks = mac_float_tweaks if self._transparent else None
        self._mac_tweaks = mac_tweaks
        # 窗口此刻真实的位置和大小（同 settings.json 的记法），记位置以它为准、事件只当信号（见 mac_window_frame）。
        # 别的平台事件报的数和 create_window 的 x、y 是同一套，也没有实测出问题，照旧用事件的数
        if frame_reader is _DEFAULT_FRAME:
            frame_reader = mac_window_frame if self._mac else None
        self._frame_reader = frame_reader
        self.on_change = on_change
        # 主窗口的 (x, 宽)，macOS 多块屏时第一次开靠它认出主窗口在哪块屏上（main.py 在建好主窗口之后设）
        self.anchor = None
        # 主窗口本身：focus_main 要把它提到前面。follow_main_window 挂主窗口的 closed 时顺手存在这里，
        # 不用改 main.py；主窗口关了就清掉
        self.main_window = None
        self._call_after = call_after           # 交给主线程的函数（AppHelper.callAfter）；None 是用时再取，测试传替身
        self._api = FloatApi(self)
        self._lock = threading.Lock()           # 串行化建窗 / 销毁
        self._state = threading.Lock()          # 保护下面几个字段，持有时间极短
        self._window = None
        self._want = False
        self._dead = False
        self._geom = {}
        self._screens_seen = []                 # 开窗时的屏幕列表，moved 事件换算位置用
        self._saved = None
        self._timer = None

    # -- 对外 --
    def url(self):
        return "{}{}?solid={}".format(self._base_url, FLOAT_PATH, 0 if self._transparent else 1)

    def is_open(self):
        return self._window is not None

    def request_open(self):
        if self._dead:
            return
        self._want = True
        self._spawn(self._reconcile)

    def request_close(self):
        self._want = False
        if self._window is None:
            return              # 没开、也没在开：什么都不用做（每条非直播状态都会来问一次）
        self._spawn(self._reconcile)

    def focus_main(self):
        """结束卡片的「回到主窗口」（经 FloatApi 从 JS 桥的线程里调）：把主窗口提到最前（macOS 上交给主线程做，
        见 bring_to_front）。浮窗本身不动——中控接着开下一场时它还在原处。没有主窗口（还没挂上、已经关了）
        返回 False。"""
        return bring_to_front(self.main_window, mac=self._mac, call_after=self._call_after)

    def shutdown(self):
        """同步收掉浮窗，之后不再接受 request_open。主窗口 closed 的处理函数里调。"""
        self._dead = True
        self._want = False
        self._reconcile()

    # -- 内部 --
    def _reconcile(self):
        with self._lock:
            if self._dead:
                # request_open 先看 _dead、再置 _want：shutdown 正好跑在这两步之间的话，_want 会在它之后
                # 又变成 True。主窗口已经关了，这里拦住，别在一个没有主窗口的进程里建出浮窗
                self._want = False
            while self._want != (self._window is not None):
                if self._want:
                    if not self._open_locked():
                        return
                else:
                    self._close_locked()

    def _window_kwargs(self, geom, screens=()):
        kwargs = {"width": geom["width"], "height": geom["height"], "min_size": MIN_SIZE,
                  "resizable": True, "on_top": True,
                  # 不抢键盘焦点：它只是显示，操作员正在别的窗口里打字
                  "focus": False, "transparent": self._transparent,
                  "background_color": "#000000" if self._transparent else SOLID_BACKGROUND,
                  # 结束卡片的「回到主窗口」：页面里只多一个 pywebview.api.focus_main（见 FloatApi）。
                  # 不认 js_api 的老版本由 supported_kwargs 去掉，页面就不画这个按钮
                  "js_api": self._api}
        if self._mac:
            place = self._mac_place(geom, screens)
            if place is not None:
                kwargs["screen"], kwargs["x"], kwargs["y"] = place
        elif geom["x"] is not None and geom["y"] is not None:
            kwargs["x"], kwargs["y"] = geom["x"], geom["y"]
        accepted = supported_kwargs(self._create, kwargs)
        if "screen" in kwargs and "screen" not in accepted:
            # x、y 是相对 screen 的：不认 screen= 的老版本只能让系统摆
            accepted.pop("x", None)
            accepted.pop("y", None)
        return accepted

    def _mac_place(self, geom, screens):
        """macOS 开窗的 (Screen 对象, x, y)：记过位置就还原它，没记过就贴右下角；认不出就是 None（系统摆）。"""
        if geom["x"] is not None and geom["y"] is not None:
            return mac_create_position(geom["x"], geom["y"], geom["width"], geom["height"], screens)
        return mac_default_position(geom["width"], geom["height"], screens, self.anchor)

    def _open_locked(self):
        try:
            try:
                screens = self._screens()
            except Exception:
                screens = []
            geom = normalize_geometry(self._load().get(SETTINGS_KEY), screens, mac=self._mac)
            window = self._create(i18n.text(FLOAT_TITLE), self.url(),
                                  **self._window_kwargs(geom, screens))
            if window is None:
                raise RuntimeError("create_window 没有返回窗口")  # i18n: terminal
            # create_window 在 start() 之后从非主线程调用时立刻返回，窗口是在 GUI 线程上稍后才建的；
            # 建窗在那边失败（后端不支持某个参数之类）的话 shown 永远不会来。等到它真的显示了才算开成，
            # 否则页面上的按钮会亮着，屏幕上却什么也没有
            shown = getattr(getattr(window, "events", None), "shown", None)
            if shown is not None and not shown.wait(self._shown_wait):
                self._spawn(lambda: self._destroy_quietly(window))
                raise RuntimeError("窗口没有在 {} 秒内显示出来".format(self._shown_wait))  # i18n: terminal
        except Exception as exc:
            self._want = False
            print("[警告] 打开浮窗失败：{}".format(exc))
            self._notify(False, OPEN_FAILED)
            return False
        # 窗口真的显示出来了：它此刻的 frame 就是起点。以后存不存、存什么都跟它比——没动过就不写
        # （没记过位置的第一次开，关掉也不会凭空记下一个位置，下次还是默认的角落）
        real = self._read_frame(window)
        with self._state:
            self._window = window
            self._geom = {k: v for k, v in geom.items() if v is not None}
            if real is not None:
                self._geom = dict(real)
            self._saved = dict(self._geom)
            self._screens_seen = list(screens)
        self._attach(window)
        if self._mac_tweaks is not None:
            self._spawn(lambda: self._mac_tweaks(window))
        print("[信息] 浮窗已打开")
        self._notify(True, None)
        return True

    @staticmethod
    def _destroy_quietly(window):
        try:
            window.destroy()
        except Exception:
            pass

    def _close_locked(self):
        with self._state:
            window = self._window
            # 先摘掉再销毁：销毁引来的 closed 事件（有的平台同步到，有的稍后到）认出这扇窗已经
            # 不是 _window，不会当成「用户关的」再通知一次
            self._window = None
        if window is None:
            return
        self._cancel_timer()
        self._flush(window)         # 销毁之前先把最后的位置和大小记下来（读的是这时真实的 frame）
        try:
            window.destroy()
        except Exception as exc:
            print("[警告] 关闭浮窗出错：{}".format(exc))
        print("[信息] 浮窗已关闭")
        self._notify(False, None)

    def _attach(self, window):
        events = getattr(window, "events", None)

        def on_moved(x, y):
            if self._mac:
                pos = self._mac_stored_position(x, y)
                if pos is None:
                    return
                x, y = pos
            self._note(window, ("x", x), ("y", y))

        def on_resized(width, height):
            self._note(window, ("width", width), ("height", height))

        def on_closed():
            self._closed(window)

        for name, handler in (("moved", on_moved), ("resized", on_resized), ("closed", on_closed)):
            event = getattr(events, name, None)
            if event is None:
                continue
            try:
                event += handler
            except Exception as exc:
                print("[警告] 浮窗的 {} 事件没挂上：{}".format(name, exc))

    def _mac_stored_position(self, x, y):
        """moved 事件报的 (x, y) → 要记的 Cocoa 全局左上角；报的不合法或认不出在哪块屏上就是 None。"""
        x, y = _number(x, *LIMITS["x"]), _number(y, *LIMITS["y"])
        if x is None or y is None:
            return None
        with self._state:
            width = self._geom.get("width", DEFAULT_SIZE[0])
            height = self._geom.get("height", DEFAULT_SIZE[1])
            screens = self._screens_seen
        return mac_top_left(x, y, width, height, screens)

    def _note(self, window, *pairs):
        """窗口动了：(键, 值) 成对到——位置一对、大小一对。一对里有一个不合法，整对都不要
        （只记下半个位置，下次开窗就是个歪的）。"""
        values = {}
        for key, value in pairs:
            number = _number(value, *LIMITS[key])
            if number is None:
                return
            values[key] = number
        with self._state:
            if window is not self._window:
                return
            self._geom.update(values)
            self._arm_timer(window)

    def _closed(self, window):
        """用户点了标题栏的关闭，或系统把窗口关了。我们自己销毁的那一条已经在 _close_locked 里
        处理过（_window 不再是它），这里直接返回。"""
        with self._state:
            if self._window is not window:
                return
            self._window = None
            self._want = False
        self._cancel_timer()
        self._flush(window)         # 关掉的窗口对象还在（pywebview 设了 releasedWhenClosed=False），frame 照样读得到
        print("[信息] 浮窗已关闭")
        self._notify(False, None)

    # -- 记住位置和大小 --
    def _arm_timer(self, window=None):
        """在 _state 里调。window：动的是哪扇窗，定时器到点时读它的 frame（它已经不是当前那扇就不读）。"""
        if self._timer is not None:
            self._timer.cancel()
        timer = self._timer_factory(DEBOUNCE_SEC, functools.partial(self._flush, window, True))
        try:
            timer.daemon = True
        except Exception:
            pass
        self._timer = timer
        timer.start()

    def _cancel_timer(self):
        with self._state:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _read_frame(self, window):
        """window 此刻真实的位置和大小（frame_reader 给的，见 mac_window_frame）；没有读法或读不到是 None。"""
        if self._frame_reader is None or window is None:
            return None
        try:
            return self._frame_reader(window)
        except Exception:
            return None

    def _flush(self, window=None, live=False):
        """把位置和大小写进 settings.json（和上次写的一样就不写）。window：读得到真实 frame 时以它为准，
        事件报的数只是读不到时的退路。live=True 是去抖定时器到点：那扇窗已经不是当前的（关了、又开了一扇
        新的）就什么都不做——关窗那一路已经替它写过了，读它的 frame 只会把旧窗的位置写进新窗的记录。"""
        real = self._read_frame(window)
        with self._state:
            if live and window is not self._window:
                return
            if real is not None:
                self._geom.update(real)
            geom = dict(self._geom)
            if geom == self._saved:
                return
        width = _number(geom.get("width"), *LIMITS["width"])
        height = _number(geom.get("height"), *LIMITS["height"])
        if width is None or height is None:
            return
        record = {"width": width, "height": height, "x": geom.get("x"), "y": geom.get("y")}
        try:
            self._save(SETTINGS_KEY, record)
        except Exception as exc:
            print("[警告] 记住浮窗位置失败：{}".format(exc))
            return
        with self._state:
            self._saved = geom

    def _notify(self, is_open, error):
        callback = self.on_change
        if callback is None:
            return
        try:
            callback(is_open, error)
        except Exception as exc:
            print("[警告] 浮窗状态通知失败：{}".format(exc))


def follow_main_window(window, host, stuck=None, delay=STUCK_EXIT_SEC, timer_factory=None):
    """主窗口关了，浮窗跟着关；顺手把主窗口存到 host.main_window 上（结束卡片的「回到主窗口」要用，
    main.py 只调这一个函数，不用为它再改）。

    cocoa / winforms 都是「所有窗口都关了才停 GUI 循环」：浮窗开着时关主窗口，webview.start() 不返回，
    stop_after_close 不执行，直播管线在后台继续跑。所以主窗口 closed 时要把浮窗销毁。
    stuck：销毁了浮窗 delay 秒后 GUI 循环还没退出（start() 一直不返回）才会调的收尾函数——
    正常情况下主线程早就 os._exit 了，这个定时器根本不会走到。返回是否挂上了。"""
    if host is None:
        return False
    host.main_window = window
    closed = getattr(getattr(window, "events", None), "closed", None)
    if closed is None:
        return False

    def on_main_closed():
        host.main_window = None     # 关掉的窗口不再往前提
        host.shutdown()
        if stuck is not None:
            timer = (timer_factory or threading.Timer)(delay, stuck)
            try:
                timer.daemon = True
            except Exception:
                pass
            timer.start()

    try:
        closed += on_main_closed
    except Exception:
        return False
    return True
