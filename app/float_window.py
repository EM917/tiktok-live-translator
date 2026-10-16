# i18n: done
"""浮窗：直播中把最新字幕和最近几条弹幕放进一个小的、始终在最上面的窗口。

只做显示，不碰识别、检测、审计、翻译：窗口里的页面（web/float.html）自己连 WebSocket、只收不发，
服务端对它的连接另有一套隔离（app/server.py 的 role=float）。这个模块只管「窗口本身」：
什么时候建、什么时候销毁、位置和大小怎么记。

放在 app/ 里是为了能测：main.py 导入时会 execv 进虚拟环境，测试不能 import 它；也不 import webview
（CI 没装），建窗函数由 main.py 注入。

线程：建窗、销毁一律在短命的工作线程里做（pywebview 的 create_window 在 start() 之后从非主线程调用会
立刻建窗，winforms 上还会等；事件循环线程上不能有 GUI 调用，否则会拖识别）。窗口的 moved / resized /
closed 事件由 pywebview 每次新开一个线程触发，处理函数只改内存、重置去抖定时器。

不用无边框：cocoa 的 easy_drag 靠 convertBaseToScreen_（自 10.7 起弃用），无边框还会把红绿灯按钮藏掉，
拖动和关闭都靠不住。保留原生标题栏，拖动、缩放、关闭都是系统自带的。透明只开 macOS；别的平台
webview 背景叠在不透明的 Form 上，并不真透，页面用不透明深色底（?solid=1）。
"""
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
OPEN_FAILED = L("无法打开浮窗。", "Couldn’t open the floating window.")

DEFAULT_SIZE = (460, 330)
MIN_SIZE = (260, 140)
MAX_SIZE = (2400, 1600)
POSITION_LIMIT = 20000
VISIBLE_MARGIN = (80, 40)       # 窗口至少要有这么宽、这么高的一块落在屏幕里，标题栏才够得着
DEBOUNCE_SEC = 0.8              # 位置、大小停稳这么久才写 settings.json
STUCK_EXIT_SEC = 10.0           # 主窗口关了、10 秒后 GUI 循环还没退出，走一遍收尾直接退出
SHOWN_WAIT_SEC = 8.0            # create_window 返回后最多等这么久窗口才算真的显示出来
SOLID_BACKGROUND = "#101218"
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
            x, y, w, h = (int(v) for v in item)
        except (TypeError, ValueError):
            continue
        if w > 0 and h > 0:
            boxes.append((x, y, w, h))
    return boxes


def on_screen(x, y, width, screens):
    """位置还落在已知屏幕上吗。粗略判断，目的只是显示器拔了之后不把窗口开到屏幕外。

    pywebview 的窗口坐标是左上角、y 向下，而 cocoa 的屏幕列表是左下角原点的全局坐标，
    多显示器时两者对不齐：一块屏时严格判断，多块屏时放宽到整体包围盒。"""
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


def normalize_geometry(raw, screens=None):
    """settings.json 里记的浮窗位置和大小 → {"width","height","x","y"}，x、y 可能是 None（让系统摆）。

    宽高必须同时合法，否则用默认大小；位置要同时有 x、y、落在屏幕上才用。读不到屏幕列表也不用位置：
    宁可让系统摆，不赌它还在屏幕里。"""
    raw = raw if isinstance(raw, dict) else {}
    width = _number(raw.get("width"), MIN_SIZE[0], MAX_SIZE[0])
    height = _number(raw.get("height"), MIN_SIZE[1], MAX_SIZE[1])
    if width is None or height is None:
        width, height = DEFAULT_SIZE
    x = _number(raw.get("x"), -POSITION_LIMIT, POSITION_LIMIT)
    y = _number(raw.get("y"), -POSITION_LIMIT, POSITION_LIMIT)
    if x is None or y is None or not on_screen(x, y, width, screens):
        x = y = None
    return {"width": width, "height": height, "x": x, "y": y}


def screens_from_webview(webview):
    """pywebview 的屏幕列表 → [(x, y, 宽, 高)]。读不到（还没初始化、平台不支持）返回空列表。"""
    def read():
        try:
            return [(s.x, s.y, s.width, s.height) for s in webview.screens]
        except Exception:
            return []
    return read


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


# ---- 窗口宿主 ----------------------------------------------------------------------------

def _spawn_thread(fn):
    threading.Thread(target=fn, daemon=True).start()


class FloatWindowHost:
    """浮窗的建立、销毁和位置记忆。create_window 是 pywebview 的 webview.create_window（main.py 注入）。

    对外只有 request_open / request_close / shutdown / is_open 和 on_change 回调：
    - request_*：不阻塞，记下「最后想要的状态」，由工作线程把窗口调到那个状态（连点开、关、开，
      以最后一条为准）；
    - shutdown：同步，主窗口关了之后调，保证浮窗一起关（否则 GUI 循环不会退出）；
    - on_change(is_open, error)：状态变了（任何线程里调），error 是要告诉用户的一句双语话或 None。"""

    def __init__(self, create_window, base_url, platform=None, screens=None, load=None, save=None,
                 spawn=None, timer_factory=None, mac_tweaks=None, on_change=None,
                 shown_wait=SHOWN_WAIT_SEC):
        self._create = create_window
        self._shown_wait = shown_wait
        self._base_url = str(base_url).rstrip("/")
        self._transparent = (sys.platform if platform is None else platform) == "darwin"
        self._screens = screens or (lambda: [])
        self._load = load or load_settings
        self._save = save or save_setting
        self._spawn = spawn or _spawn_thread
        self._timer_factory = timer_factory or threading.Timer
        self._mac_tweaks = mac_tweaks if mac_tweaks is not None else (
            mac_float_tweaks if self._transparent else None)
        self.on_change = on_change
        self._lock = threading.Lock()           # 串行化建窗 / 销毁
        self._state = threading.Lock()          # 保护下面几个字段，持有时间极短
        self._window = None
        self._want = False
        self._dead = False
        self._geom = {}
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

    def shutdown(self):
        """同步收掉浮窗，之后不再接受 request_open。主窗口 closed 的处理函数里调。"""
        self._dead = True
        self._want = False
        self._reconcile()

    # -- 内部 --
    def _reconcile(self):
        with self._lock:
            while self._want != (self._window is not None):
                if self._want:
                    if not self._open_locked():
                        return
                else:
                    self._close_locked()

    def _window_kwargs(self, geom):
        kwargs = {"width": geom["width"], "height": geom["height"], "min_size": MIN_SIZE,
                  "resizable": True, "on_top": True,
                  # 不抢键盘焦点：它只是显示，操作员正在别的窗口里打字
                  "focus": False, "transparent": self._transparent,
                  "background_color": "#000000" if self._transparent else SOLID_BACKGROUND}
        if geom["x"] is not None and geom["y"] is not None:
            kwargs["x"], kwargs["y"] = geom["x"], geom["y"]
        return supported_kwargs(self._create, kwargs)

    def _open_locked(self):
        try:
            try:
                screens = self._screens()
            except Exception:
                screens = []
            geom = normalize_geometry(self._load().get(SETTINGS_KEY), screens)
            window = self._create(i18n.text(FLOAT_TITLE), self.url(), **self._window_kwargs(geom))
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
        with self._state:
            self._window = window
            self._geom = {k: v for k, v in geom.items() if v is not None}
            self._saved = dict(self._geom)
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
        self._flush()               # 销毁之前先把最后的位置和大小记下来
        try:
            window.destroy()
        except Exception as exc:
            print("[警告] 关闭浮窗出错：{}".format(exc))
        print("[信息] 浮窗已关闭")
        self._notify(False, None)

    def _attach(self, window):
        events = getattr(window, "events", None)

        def on_moved(x, y):
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
            self._arm_timer()

    def _closed(self, window):
        """用户点了标题栏的关闭，或系统把窗口关了。我们自己销毁的那一条已经在 _close_locked 里
        处理过（_window 不再是它），这里直接返回。"""
        with self._state:
            if self._window is not window:
                return
            self._window = None
            self._want = False
        self._cancel_timer()
        self._flush()
        print("[信息] 浮窗已关闭")
        self._notify(False, None)

    # -- 记住位置和大小 --
    def _arm_timer(self):
        """在 _state 里调。"""
        if self._timer is not None:
            self._timer.cancel()
        timer = self._timer_factory(DEBOUNCE_SEC, self._flush)
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

    def _flush(self):
        with self._state:
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
    """主窗口关了，浮窗跟着关。

    cocoa / winforms 都是「所有窗口都关了才停 GUI 循环」：浮窗开着时关主窗口，webview.start() 不返回，
    stop_after_close 不执行，直播管线在后台继续跑。所以主窗口 closed 时要把浮窗销毁。
    stuck：销毁了浮窗 delay 秒后 GUI 循环还没退出（start() 一直不返回）才会调的收尾函数——
    正常情况下主线程早就 os._exit 了，这个定时器根本不会走到。返回是否挂上了。"""
    closed = getattr(getattr(window, "events", None), "closed", None)
    if closed is None or host is None:
        return False

    def on_main_closed():
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
