"""浮窗宿主（app/float_window.py）：建窗、销毁、位置和大小的记忆。

窗口全是替身：建窗函数记下每次调用，窗口的事件是能手动触发的假 Event，线程用同步的 spawn 顶替，
去抖定时器用能手动触发的假 Timer。不 import webview（CI 没装），也不开任何真窗口。
"""
import math
import threading
from types import SimpleNamespace

import pytest

from app import float_window as fw
from app import i18n, settings

SCREEN = [(0, 0, 1440, 900)]


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self, *args):
        for fn in list(self.handlers):
            fn(*args)

    def wait(self, timeout=None):
        return True


class FakeWindow:
    def __init__(self, title, url, kwargs, close_on_destroy=False):
        self.title, self.url, self.kwargs = title, url, kwargs
        self.events = SimpleNamespace(moved=FakeEvent(), resized=FakeEvent(), closed=FakeEvent(),
                                      shown=FakeEvent())
        self.destroyed = 0
        self.native = None
        self._close_on_destroy = close_on_destroy

    def destroy(self):
        self.destroyed += 1
        if self._close_on_destroy:       # 真窗口销毁之后 closed 事件会随后到
            self.events.closed.fire()


class Factory:
    def __init__(self, fail=None, none=False, close_on_destroy=False):
        self.calls, self.windows = [], []
        self.fail, self.none, self.close_on_destroy = fail, none, close_on_destroy

    def __call__(self, title, url, **kwargs):
        self.calls.append((title, url, kwargs))
        if self.fail is not None:
            raise self.fail
        if self.none:
            return None
        window = FakeWindow(title, url, kwargs, self.close_on_destroy)
        self.windows.append(window)
        return window


class FakeTimer:
    made = []

    def __init__(self, interval, fn):
        self.interval, self.fn = interval, fn
        self.started = self.cancelled = False
        self.daemon = False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn()


class Store:
    """settings.json 的替身：load / save 两个函数共用一个 dict。"""

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.saves = []

    def load(self):
        return dict(self.data)

    def save(self, key, value):
        self.data[key] = value
        self.saves.append((key, value))


def make_host(factory=None, store=None, platform="linux", screens=SCREEN, spawn=lambda fn: fn(),
              mac_tweaks=None):
    FakeTimer.made = []
    factory = factory or Factory()
    store = store or Store()
    changes = []
    host = fw.FloatWindowHost(
        factory, "http://127.0.0.1:8765/", platform=platform, screens=lambda: screens,
        load=store.load, save=store.save, spawn=spawn, timer_factory=FakeTimer,
        mac_tweaks=mac_tweaks, on_change=lambda is_open, error=None: changes.append((is_open, error)))
    return host, factory, store, changes


# ---- 位置和大小的校验 ---------------------------------------------------------------------

def test_no_saved_geometry_gives_the_default_size_and_lets_the_system_place_it():
    got = fw.normalize_geometry(None, SCREEN)
    assert got == {"width": fw.DEFAULT_SIZE[0], "height": fw.DEFAULT_SIZE[1], "x": None, "y": None}
    assert fw.normalize_geometry("garbage", SCREEN) == got
    assert fw.normalize_geometry([1, 2], SCREEN) == got


def test_a_valid_saved_geometry_is_kept():
    raw = {"x": 300, "y": 120, "width": 520, "height": 360}
    assert fw.normalize_geometry(raw, SCREEN) == raw


@pytest.mark.parametrize("bad", [True, False, None, "400", math.nan, math.inf, -5, 0,
                                 fw.MIN_SIZE[0] - 1, fw.MAX_SIZE[0] + 1, [400], {"w": 1}])
def test_a_bad_width_is_not_clamped_it_falls_back_to_the_default_size(bad):
    # 夹到边界会把一个坏值写成「看起来像用户选的」；宽高要么一起合法，要么一起用默认
    got = fw.normalize_geometry({"x": 100, "y": 100, "width": bad, "height": 300}, SCREEN)
    assert (got["width"], got["height"]) == fw.DEFAULT_SIZE


def test_a_bad_height_also_resets_the_width():
    got = fw.normalize_geometry({"width": 700, "height": fw.MAX_SIZE[1] + 1}, SCREEN)
    assert (got["width"], got["height"]) == fw.DEFAULT_SIZE


def test_float_numbers_round_like_the_page_does():
    got = fw.normalize_geometry({"x": 10.4, "y": 20.6, "width": 400.5, "height": 300.49}, SCREEN)
    assert got == {"x": 10, "y": 21, "width": 401, "height": 300}


@pytest.mark.parametrize("x,y", [
    (5000, 100),             # 显示器拔了：窗口在右边很远
    (1440 - 79, 100),        # 只剩 79px 在屏幕里，标题栏够不着
    (-1000, 100),            # 在左边很远
    (100, -1),               # 标题栏跑到屏幕顶端之上
    (100, 900 - 39),         # 标题栏在屏幕底边之下
    (100, 5000),
])
def test_a_position_off_the_only_screen_is_dropped_but_the_size_is_kept(x, y):
    got = fw.normalize_geometry({"x": x, "y": y, "width": 500, "height": 300}, SCREEN)
    assert (got["x"], got["y"]) == (None, None)
    assert (got["width"], got["height"]) == (500, 300)


def test_a_window_hanging_a_bit_over_the_left_edge_is_still_reachable():
    got = fw.normalize_geometry({"x": -400, "y": 50, "width": 500, "height": 300}, SCREEN)
    assert (got["x"], got["y"]) == (-400, 50)       # 右边 100px 还在屏幕里


@pytest.mark.parametrize("raw", [{"x": 100, "width": 500, "height": 300},
                                 {"y": 100, "width": 500, "height": 300},
                                 {"x": "100", "y": 100, "width": 500, "height": 300},
                                 {"x": True, "y": 100, "width": 500, "height": 300},
                                 {"x": 10 ** 9, "y": 100, "width": 500, "height": 300}])
def test_half_a_position_or_a_nonsense_position_is_no_position(raw):
    got = fw.normalize_geometry(raw, SCREEN)
    assert (got["x"], got["y"]) == (None, None)


@pytest.mark.parametrize("screens", [None, [], [(0, 0, 0, 0)], ["bad"], [(1, 2)]])
def test_without_a_usable_screen_list_the_position_is_not_trusted(screens):
    got = fw.normalize_geometry({"x": 100, "y": 100, "width": 500, "height": 300}, screens)
    assert (got["x"], got["y"]) == (None, None)


def test_with_several_screens_the_whole_bounding_box_counts():
    two = [(0, 0, 1440, 900), (1440, 0, 1920, 1080)]
    right = fw.normalize_geometry({"x": 2000, "y": 100, "width": 500, "height": 300}, two)
    assert (right["x"], right["y"]) == (2000, 100)
    gone = fw.normalize_geometry({"x": 4000, "y": 100, "width": 500, "height": 300}, two)
    assert (gone["x"], gone["y"]) == (None, None)
    # 第二块屏在主屏上方：pywebview 的 y 是负的（左上角坐标），多块屏时放宽到包围盒
    above = [(0, 0, 1440, 900), (0, 900, 1440, 900)]
    up = fw.normalize_geometry({"x": 100, "y": -500, "width": 500, "height": 300}, above)
    assert (up["x"], up["y"]) == (100, -500)


def test_screens_from_webview_reads_the_list_and_survives_a_platform_without_it():
    screens = [SimpleNamespace(x=0, y=0, width=1440, height=900)]
    assert fw.screens_from_webview(SimpleNamespace(screens=screens))() == [(0, 0, 1440, 900)]

    class NoScreens:
        @property
        def screens(self):
            raise RuntimeError("no gui yet")

    assert fw.screens_from_webview(NoScreens())() == []


# ---- macOS 的坐标（多块屏） ---------------------------------------------------------------
# pywebview 的 moved(x, y) 和 create_window(x, y) 在原点不是 (0, 0) 的屏幕上对不齐（见 app/float_window.py）。
# 这里的屏幕：主屏 1440x900 在 (0, 0)；外接屏 1920x1080 放在它右边、上沿对齐，所以原点是 (1440, -180)

P_OBJ, E_OBJ, T_OBJ = (SimpleNamespace(name=n) for n in ("primary", "external", "above"))
MAC_P = fw.ScreenBox(0, 0, 1440, 900, P_OBJ)
MAC_E = fw.ScreenBox(1440, -180, 1920, 1080, E_OBJ)
MAC_T = fw.ScreenBox(-240, 900, 1920, 1080, T_OBJ)             # 在主屏正上方的一块 1920x1080
MAC_RIGHT = [MAC_P, MAC_E]
MAC_ABOVE = [MAC_P, MAC_T]


def reported_by_pywebview(left, top, screen):
    """pywebview 6.2.1 cocoa.py windowDidMove_ 对一个全局左上角 (left, top) 会报出的 (x, y)：
    x = 全局 origin.x，y = 所在屏的高 - 窗口高 - 全局 origin.y（不含屏的 origin.y）；
    窗口的 origin.y = top - 窗口高，所以 y = 所在屏的高 - top。"""
    return left, screen[3] - top


def placed_by_pywebview(screen, x, y):
    """pywebview 6.2.1 cocoa.py move()：create_window(screen=, x=, y=) 摆到的全局左上角。"""
    sx, sy, _sw, sh = tuple(screen)
    return sx + x, sy + sh - y


def test_on_the_main_screen_the_reported_position_is_already_the_global_one():
    x, y = reported_by_pywebview(200, 800, MAC_P)
    assert (x, y) == (200, 100)
    assert fw.mac_top_left(x, y, 460, 330, MAC_RIGHT) == (200, 800)


def test_on_an_external_screen_whose_origin_is_not_zero_the_reported_position_is_converted():
    # 窗口在外接屏上，距那块屏左边 200、距上沿 100：全局左上角 (1640, -180 + 1080 - 100 = 800)
    x, y = reported_by_pywebview(1640, 800, MAC_E)
    assert (x, y) == (1640, 280)
    assert fw.mac_top_left(x, y, 460, 330, MAC_RIGHT) == (1640, 800)


def test_on_a_screen_above_the_main_one_the_reported_y_is_negative_and_still_converts():
    top = 900 + 1080 - 100                                   # 距上面那块屏的上沿 100
    x, y = reported_by_pywebview(300, top, MAC_T)
    assert y == -800
    assert fw.mac_top_left(x, y, 460, 330, MAC_ABOVE) == (300, top)


def test_a_reported_position_that_fits_no_screen_is_not_converted():
    assert fw.mac_top_left(9000, 10, 460, 330, MAC_RIGHT) is None
    assert fw.mac_top_left(100, 10, 460, 330, []) is None
    assert fw.mac_top_left(100, 10, 460, 330, None) is None


@pytest.mark.parametrize("screens,left,top,expected", [
    (MAC_RIGHT, 200, 800, P_OBJ), (MAC_RIGHT, 1640, 800, E_OBJ), (MAC_RIGHT, 3200, 800, E_OBJ),
    (MAC_ABOVE, 300, 1880, T_OBJ), (MAC_ABOVE, 300, 700, P_OBJ),
])
def test_a_saved_global_position_is_given_back_to_pywebview_on_its_own_screen(screens, left, top, expected):
    obj, x, y = fw.mac_create_position(left, top, 460, 330, screens)
    assert obj is expected
    box = next(b for b in screens if b.screen is expected)
    assert placed_by_pywebview(box, x, y) == (left, top)       # pywebview 摆回的正是记下的那一点


@pytest.mark.parametrize("screens,left,top", [
    ([MAC_P], 1440 * 3, 800),                  # 位置在右边很远（外接屏拔了）
    ([MAC_P], 1640, 800),                      # 外接屏拔了，只剩主屏
    ([MAC_P], -2000, 800),
    ([MAC_P], 200, 900 + 1),                   # 窗口上沿在主屏上沿之上
    ([MAC_P], 200, 40 - 1),                    # 标题栏在主屏下沿之下
    (MAC_RIGHT, 3360 - 79, 700),               # 外接屏上只剩 79px，标题栏够不着
])
def test_a_saved_position_that_no_screen_shows_is_dropped(screens, left, top):
    got = fw.normalize_geometry({"x": left, "y": top, "width": 460, "height": 330}, screens, mac=True)
    assert (got["x"], got["y"]) == (None, None)
    assert (got["width"], got["height"]) == (460, 330)


def test_mac_geometry_is_checked_screen_by_screen_not_by_bounding_box():
    kept = fw.normalize_geometry({"x": 1640, "y": 800, "width": 460, "height": 330}, MAC_RIGHT, mac=True)
    assert (kept["x"], kept["y"]) == (1640, 800)
    # 副屏在主屏的右上方：整体包围盒里有一块哪块屏都不是的空地
    diagonal = [MAC_P, fw.ScreenBox(1440, 900, 1920, 1080, E_OBJ)]
    raw = {"x": 100, "y": 1500, "width": 460, "height": 330}
    assert fw.normalize_geometry(raw, diagonal, mac=True)["x"] is None
    assert fw.normalize_geometry(raw, diagonal)["x"] == 100          # 非 macOS 的粗判断放得过


def mac_host(saved=None, screens=MAC_RIGHT, factory=None, anchor=None):
    store = Store({fw.SETTINGS_KEY: saved} if saved else None)
    host, factory, store, changes = make_host(factory=factory, store=store, platform="darwin",
                                              screens=screens, mac_tweaks=lambda w: None)
    host.anchor = anchor
    return host, factory, store, changes


def test_a_saved_position_on_the_external_screen_comes_back_on_the_external_screen():
    host, factory, _s, _c = mac_host({"x": 1640, "y": 800, "width": 460, "height": 330})
    host.request_open()
    kwargs = factory.calls[0][2]
    assert kwargs["screen"] is E_OBJ and (kwargs["x"], kwargs["y"]) == (200, 100)
    assert placed_by_pywebview(MAC_E, kwargs["x"], kwargs["y"]) == (1640, 800)


def test_dragging_on_the_external_screen_is_saved_as_the_global_position_and_reopened_there():
    host, factory, store, _c = mac_host({"x": 1640, "y": 800, "width": 460, "height": 330})
    host.request_open()
    window = factory.windows[0]
    # 用户把窗口拖到外接屏上距左 300、距上沿 150 的地方：全局左上角 (1740, 750)；pywebview 报 (1740, 330)
    window.events.moved.fire(*reported_by_pywebview(1740, 750, MAC_E))
    host.request_close()
    assert store.data[fw.SETTINGS_KEY] == {"x": 1740, "y": 750, "width": 460, "height": 330}
    host2, factory2, _s2, _c2 = mac_host(store.data[fw.SETTINGS_KEY])
    host2.request_open()
    kwargs = factory2.calls[0][2]
    assert kwargs["screen"] is E_OBJ
    assert placed_by_pywebview(MAC_E, kwargs["x"], kwargs["y"]) == (1740, 750)


def test_a_move_that_fits_no_known_screen_is_not_saved():
    host, factory, store, _c = mac_host()
    host.request_open()
    factory.windows[0].events.moved.fire(90000, 5)           # 超出合理范围
    factory.windows[0].events.moved.fire(9000, 5)            # 不在任何已知屏幕上
    host.request_close()
    assert store.saves == []


def test_the_first_open_sits_in_the_bottom_right_corner_of_the_only_screen():
    host, factory, store, _c = mac_host(screens=[MAC_P])
    host.request_open()
    kwargs = factory.calls[0][2]
    assert kwargs["screen"] is P_OBJ
    assert (kwargs["x"], kwargs["y"]) == (1440 - 460 - fw.DEFAULT_MARGIN[0], 900 - 330 - fw.DEFAULT_MARGIN[1])
    left, top = placed_by_pywebview(MAC_P, kwargs["x"], kwargs["y"])
    assert left + 460 <= 1440 and top - 330 >= 0 and top <= 900 - fw.MENU_BAR_ROOM
    host.request_close()
    assert store.saves == []          # 没动过就不写：下次还是这个默认位置，不是被「记住」的


def test_with_several_screens_the_first_open_goes_to_the_screen_the_main_window_is_on():
    on_external = mac_host(anchor=lambda: (1800, 1000))[0]
    on_external.request_open()
    kwargs = on_external._create.calls[0][2]
    assert kwargs["screen"] is E_OBJ
    assert (kwargs["x"], kwargs["y"]) == (1920 - 460 - fw.DEFAULT_MARGIN[0], 1080 - 330 - fw.DEFAULT_MARGIN[1])
    on_main = mac_host(anchor=lambda: (100, 1000))[0]
    on_main.request_open()
    assert on_main._create.calls[0][2]["screen"] is P_OBJ


@pytest.mark.parametrize("anchor", [None, lambda: None, lambda: (float("nan"), 1000), lambda: (99999, 100),
                                    lambda: 1 / 0])
def test_with_several_screens_and_no_usable_main_window_position_the_system_places_it(anchor):
    host, factory, _s, _c = mac_host(anchor=anchor)
    host.request_open()
    kwargs = factory.calls[0][2]
    assert host.is_open() and "x" not in kwargs and "y" not in kwargs and "screen" not in kwargs


def test_a_pywebview_without_the_screen_argument_gets_no_position_at_all():
    def old_create(title, url, width=1, height=1, x=None, y=None, on_top=False, min_size=None, resizable=True,
                   focus=True, transparent=False, background_color="#fff"):
        calls.append({"x": x, "y": y})
        return FakeWindow(title, url, {})
    calls = []
    host, _f, _s, _c = make_host(factory=old_create, platform="darwin", screens=[MAC_P],
                                 mac_tweaks=lambda w: None)
    host.request_open()
    assert calls == [{"x": None, "y": None}]          # 相对那块屏的 x、y 不能单独用，也不能当成相对主屏


def test_a_screen_without_a_screen_object_cannot_be_placed_on_so_the_system_does():
    host, factory, _s, _c = make_host(platform="darwin", screens=[(0, 0, 1440, 900)], mac_tweaks=lambda w: None)
    host.request_open()
    kwargs = factory.calls[0][2]
    assert "x" not in kwargs and "screen" not in kwargs


def test_other_platforms_still_pass_the_saved_position_as_is_and_have_no_default_corner():
    host, factory, _s, _c = make_host(store=Store({fw.SETTINGS_KEY: {"x": 200, "y": 80, "width": 600,
                                                                      "height": 400}}),
                                      screens=[MAC_P])
    host.request_open()
    kwargs = factory.calls[0][2]
    assert (kwargs["x"], kwargs["y"]) == (200, 80) and "screen" not in kwargs
    fresh, factory2, _s2, _c2 = make_host(screens=[MAC_P])
    fresh.request_open()
    assert "x" not in factory2.calls[0][2] and "screen" not in factory2.calls[0][2]


def test_screens_from_webview_keeps_the_screen_objects_for_macos_placement():
    objs = [SimpleNamespace(x=0, y=0, width=1440, height=900), SimpleNamespace(x=1440, y=-180, width=1920, height=1080)]
    got = fw.screens_from_webview(SimpleNamespace(screens=objs))()
    assert got == [(0, 0, 1440, 900), (1440, -180, 1920, 1080)]
    assert [b.screen for b in got] == objs
    assert fw.mac_create_position(1640, 800, 460, 330, got)[0] is objs[1]
    # 五元组（最后一项是 Screen 对象）也认
    assert fw.mac_create_position(200, 800, 460, 330, [(0, 0, 1440, 900, "P")])[0] == "P"


def test_supported_kwargs_drops_what_an_older_pywebview_does_not_take():
    def old(title, url, width=1, height=1, on_top=False):
        pass

    got = fw.supported_kwargs(old, {"width": 1, "focus": False, "transparent": True, "on_top": True})
    assert got == {"width": 1, "on_top": True}
    assert fw.supported_kwargs(lambda **kw: None, {"a": 1}) == {"a": 1}      # **kwargs 全收


# ---- 建窗 ---------------------------------------------------------------------------------

def test_open_creates_one_window_with_the_float_url_title_and_flags():
    host, factory, _store, changes = make_host()
    host.request_open()
    assert host.is_open() and changes == [(True, None)]
    (title, url, kwargs), = factory.calls
    assert title == "浮窗"
    assert url == "http://127.0.0.1:8765/float?solid=1"
    assert kwargs["on_top"] is True and kwargs["resizable"] is True
    assert kwargs["focus"] is False               # 只是显示：不抢操作员的键盘焦点
    assert kwargs["transparent"] is False
    assert kwargs["background_color"] == fw.SOLID_BACKGROUND
    assert kwargs["min_size"] == fw.MIN_SIZE
    assert (kwargs["width"], kwargs["height"]) == fw.DEFAULT_SIZE
    assert "x" not in kwargs and "y" not in kwargs
    assert type(title) is str                      # 交给原生窗口的永远是普通 str


def test_the_title_follows_the_interface_language():
    with i18n.use("en"):
        host, factory, _s, _c = make_host()
        host.request_open()
    assert factory.calls[0][0] == "Floating Window" and type(factory.calls[0][0]) is str


def test_macos_gets_a_transparent_window_everything_else_a_solid_one():
    host, factory, _s, _c = make_host(platform="darwin", mac_tweaks=lambda w: None)
    host.request_open()
    (_t, url, kwargs), = factory.calls
    assert url.endswith("/float?solid=0")
    assert kwargs["transparent"] is True


def test_a_saved_geometry_is_used_for_the_next_window():
    store = Store({fw.SETTINGS_KEY: {"x": 200, "y": 80, "width": 600, "height": 400}})
    host, factory, _s, _c = make_host(store=store)
    host.request_open()
    kwargs = factory.calls[0][2]
    assert (kwargs["x"], kwargs["y"], kwargs["width"], kwargs["height"]) == (200, 80, 600, 400)


def test_a_saved_position_that_is_now_off_screen_is_left_to_the_system():
    store = Store({fw.SETTINGS_KEY: {"x": 5000, "y": 80, "width": 600, "height": 400}})
    host, factory, _s, _c = make_host(store=store)
    host.request_open()
    kwargs = factory.calls[0][2]
    assert "x" not in kwargs and (kwargs["width"], kwargs["height"]) == (600, 400)


def test_a_screen_list_that_raises_does_not_stop_the_window():
    def boom():
        raise RuntimeError("no screens")

    host, factory, _s, _c = make_host()
    host._screens = boom
    host.request_open()
    assert host.is_open() and "x" not in factory.calls[0][2]


def test_opening_twice_makes_one_window():
    host, factory, _s, changes = make_host()
    host.request_open()
    host.request_open()
    assert len(factory.calls) == 1 and changes == [(True, None)]


def test_a_window_that_fails_to_open_reports_a_plain_observation_and_stays_closed(capsys):
    host, _f, _s, changes = make_host(factory=Factory(fail=RuntimeError("no display")))
    host.request_open()
    assert not host.is_open()
    (is_open, error), = changes
    assert is_open is False and error == fw.OPEN_FAILED
    assert error.en == "Couldn’t open the floating window."
    assert "[警告] 打开浮窗失败：no display" in capsys.readouterr().out
    # 失败之后不会因为 want 还挂着而反复重试
    host.request_close()
    assert changes == [(False, error)]


def test_a_window_that_never_gets_shown_is_a_failure_not_a_lit_button(capsys):
    # create_window 立刻返回，建窗在 GUI 线程上稍后才做；那边失败时 shown 永远不会来
    factory = Factory()
    original = factory.__call__

    def never_shown(title, url, **kwargs):
        window = original(title, url, **kwargs)
        window.events.shown.wait = lambda timeout=None: False
        return window
    host, _f, _s, changes = make_host(factory=never_shown)
    host.request_open()
    assert not host.is_open()
    assert changes == [(False, fw.OPEN_FAILED)]
    assert "窗口没有在 8.0 秒内显示出来" in capsys.readouterr().out
    assert factory.windows[0].destroyed == 1          # 事后清理一下，免得它迟到地冒出来


def test_create_window_returning_none_is_a_failure_too():
    host, _f, _s, changes = make_host(factory=Factory(none=True))
    host.request_open()
    assert not host.is_open() and changes[0][0] is False and changes[0][1] == fw.OPEN_FAILED


# ---- 关窗 ---------------------------------------------------------------------------------

def test_request_close_destroys_the_window_and_the_late_closed_event_is_ignored(capsys):
    host, factory, _s, changes = make_host(factory=Factory(close_on_destroy=True))
    host.request_open()
    host.request_close()
    assert not host.is_open() and factory.windows[0].destroyed == 1
    assert changes == [(True, None), (False, None)]      # closed 事件再来也不会通知第二次
    out = capsys.readouterr().out
    assert out.count("[信息] 浮窗已关闭") == 1


def test_closing_a_window_that_is_not_open_does_nothing():
    host, _f, _s, changes = make_host()
    host.request_close()
    assert changes == []


def test_the_user_closing_the_title_bar_closes_it_for_good_and_it_can_open_again():
    host, factory, store, changes = make_host()
    host.request_open()
    first = factory.windows[0]
    first.events.resized.fire(700, 420)
    first.events.closed.fire()
    assert not host.is_open() and changes == [(True, None), (False, None)]
    assert store.data[fw.SETTINGS_KEY]["width"] == 700       # 关窗时立刻记下，不等去抖
    assert first.destroyed == 0                              # 用户已经关了，不再销毁一次
    host.request_open()
    assert host.is_open() and len(factory.calls) == 2


def test_destroy_raising_still_ends_closed(capsys):
    host, factory, _s, changes = make_host()
    host.request_open()

    def boom():
        raise RuntimeError("already gone")
    factory.windows[0].destroy = boom
    host.request_close()
    assert not host.is_open() and changes[-1] == (False, None)
    assert "[警告] 关闭浮窗出错：already gone" in capsys.readouterr().out


def test_the_last_request_wins_when_toggles_queue_up():
    jobs = []
    host, factory, _s, _c = make_host(spawn=jobs.append)
    host.request_open()
    host.request_close()
    host.request_open()
    for job in jobs:
        job()
    assert host.is_open() and len(factory.calls) == 1

    jobs.clear()
    host.request_close()
    host.request_open()
    host.request_close()
    for job in jobs:
        job()
    assert not host.is_open()


def test_open_then_close_before_the_worker_runs_never_creates_a_window():
    jobs = []
    host, factory, _s, changes = make_host(spawn=jobs.append)
    host.request_open()
    host.request_close()           # 还没开出来：什么也不用销毁，worker 跑起来发现 want 是关
    for job in jobs:
        job()
    assert factory.calls == [] and changes == []


def test_shutdown_closes_synchronously_and_refuses_later_opens():
    host, factory, _s, changes = make_host()
    host.request_open()
    host.shutdown()
    assert not host.is_open() and factory.windows[0].destroyed == 1
    host.request_open()
    assert len(factory.calls) == 1 and not host.is_open()
    host.shutdown()                # 重复调用没事
    assert changes == [(True, None), (False, None)]


def test_shutdown_without_a_window_is_a_no_op():
    host, _f, _s, changes = make_host()
    host.shutdown()
    assert changes == []


def test_a_failing_on_change_never_breaks_the_host(capsys):
    host, _f, _s, _c = make_host()

    def boom(is_open, error=None):
        raise ValueError("listener bug")
    host.on_change = boom
    host.request_open()
    assert host.is_open()
    assert "[警告] 浮窗状态通知失败：listener bug" in capsys.readouterr().out


# ---- 记住位置和大小 -----------------------------------------------------------------------

def test_moves_and_resizes_are_debounced_into_one_write():
    host, factory, store, _c = make_host()
    host.request_open()
    window = factory.windows[0]
    window.events.moved.fire(100, 100)
    window.events.moved.fire(110, 105)
    window.events.resized.fire(640, 380)
    assert len(FakeTimer.made) == 3
    assert [t.cancelled for t in FakeTimer.made] == [True, True, False]
    assert all(t.interval == fw.DEBOUNCE_SEC and t.started and t.daemon for t in FakeTimer.made)
    assert store.saves == []                                  # 还在动：一次都没写
    FakeTimer.made[-1].fire()
    assert store.saves == [(fw.SETTINGS_KEY, {"x": 110, "y": 105, "width": 640, "height": 380})]


def test_an_unchanged_geometry_is_not_written_again():
    host, factory, store, _c = make_host()
    host.request_open()
    window = factory.windows[0]
    window.events.moved.fire(100, 100)
    FakeTimer.made[-1].fire()
    window.events.moved.fire(100, 100)
    FakeTimer.made[-1].fire()
    host._flush()
    assert len(store.saves) == 1


def test_closing_flushes_without_waiting_for_the_timer():
    host, factory, store, _c = make_host()
    host.request_open()
    factory.windows[0].events.moved.fire(321, 123)
    assert store.saves == []
    host.request_close()
    assert store.data[fw.SETTINGS_KEY]["x"] == 321 and store.data[fw.SETTINGS_KEY]["y"] == 123
    assert FakeTimer.made[-1].cancelled           # 关窗后定时器不再留着


def test_nonsense_events_are_ignored():
    host, factory, store, _c = make_host()
    host.request_open()
    window = factory.windows[0]
    window.events.resized.fire(10, 10)            # 比最小尺寸还小
    window.events.resized.fire(float("nan"), 300)
    window.events.moved.fire("a", None)
    window.events.moved.fire(10 ** 9, 5)
    host.request_close()
    assert store.saves == []                      # 什么有效的都没变：一次都不写


def test_events_from_a_window_that_is_gone_are_ignored():
    host, factory, store, _c = make_host()
    host.request_open()
    old = factory.windows[0]
    host.request_close()
    n = len(store.saves)
    old.events.moved.fire(5, 5)
    old.events.resized.fire(900, 900)
    assert len(store.saves) == n
    host.request_open()
    new = factory.windows[1]
    old.events.moved.fire(7, 7)                   # 旧窗口的迟到事件不能算进新窗口
    new.events.moved.fire(11, 12)
    host.request_close()
    assert store.data[fw.SETTINGS_KEY]["x"] == 11


def test_a_save_that_raises_is_reported_and_retried_later(capsys):
    host, factory, store, _c = make_host()
    host.request_open()
    calls = []

    def flaky(key, value):
        calls.append(value)
        if len(calls) == 1:
            raise OSError("disk full")
        store.save(key, value)
    host._save = flaky
    factory.windows[0].events.moved.fire(50, 60)
    host._flush()
    assert "[警告] 记住浮窗位置失败：disk full" in capsys.readouterr().out
    host._flush()                                  # 没写成功就不算 saved：下一次还会写
    assert store.data[fw.SETTINGS_KEY]["x"] == 50


def test_the_geometry_round_trips_through_the_real_settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    host, factory, _s, _c = make_host(store=SimpleNamespace(load=settings.load_settings,
                                                            save=settings.save_setting))
    host.request_open()
    window = factory.windows[0]
    window.events.moved.fire(240, 90)
    window.events.resized.fire(555, 333)
    host.request_close()
    assert settings.load_settings()[fw.SETTINGS_KEY] == {"x": 240, "y": 90, "width": 555, "height": 333}

    host2, factory2, _s2, _c2 = make_host(store=SimpleNamespace(load=settings.load_settings,
                                                                save=settings.save_setting))
    host2.request_open()
    kwargs = factory2.calls[0][2]
    assert (kwargs["x"], kwargs["y"], kwargs["width"], kwargs["height"]) == (240, 90, 555, 333)


def test_a_hand_edited_garbage_value_in_settings_is_survived(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text('{"float_window": "oops", "other": 1}', encoding="utf-8")
    host, factory, _s, _c = make_host(store=SimpleNamespace(load=settings.load_settings,
                                                            save=settings.save_setting))
    host.request_open()
    assert (factory.calls[0][2]["width"], factory.calls[0][2]["height"]) == fw.DEFAULT_SIZE


def test_save_setting_from_two_threads_keeps_both_keys(tmp_path, monkeypatch):
    """浮窗的去抖定时器是第二个写 settings.json 的线程：读-改-写不串行化，两个键会互相冲掉。"""
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    start = threading.Barrier(2)

    def writer(prefix):
        start.wait()
        for i in range(60):
            settings.save_setting("{}{}".format(prefix, i), i)

    threads = [threading.Thread(target=writer, args=(p,)) for p in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = settings.load_settings()
    assert len([k for k in data if k.startswith("a")]) == 60
    assert len([k for k in data if k.startswith("b")]) == 60


# ---- 主窗口关了，浮窗跟着关 -----------------------------------------------------------------

def test_closing_the_main_window_closes_the_float_and_arms_the_backstop():
    host, factory, _s, changes = make_host()
    host.request_open()
    main = SimpleNamespace(events=SimpleNamespace(closed=FakeEvent()))
    stuck_calls, timers = [], []

    class T:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.daemon, self.started = delay, fn, False, False
            timers.append(self)

        def start(self):
            self.started = True

    assert fw.follow_main_window(main, host, stuck=lambda: stuck_calls.append(1), timer_factory=T)
    main.events.closed.fire()
    assert factory.windows[0].destroyed == 1 and changes[-1] == (False, None)
    (timer,) = timers
    assert timer.delay == fw.STUCK_EXIT_SEC and timer.daemon and timer.started
    assert stuck_calls == []                      # 正常退出时主线程早就 os._exit 了，兜底不会走到
    timer.fn()
    assert stuck_calls == [1]
    host.request_open()
    assert len(factory.calls) == 1                # 主窗口已经关了：不再开新的


def test_follow_main_window_without_a_stuck_handler_or_events():
    host, _f, _s, _c = make_host()
    assert fw.follow_main_window(SimpleNamespace(), host) is False
    assert fw.follow_main_window(SimpleNamespace(events=SimpleNamespace(closed=FakeEvent())),
                                 None) is False
    main = SimpleNamespace(events=SimpleNamespace(closed=FakeEvent()))
    assert fw.follow_main_window(main, host) is True
    main.events.closed.fire()                     # 没有兜底也不抛


# ---- macOS 的收尾调整 -----------------------------------------------------------------------

class FakeNative:
    def __init__(self):
        self.calls = []

    def setCollectionBehavior_(self, value):
        self.calls.append(("behavior", value))

    def setAppearance_(self, value):
        self.calls.append(("appearance", value))


def test_the_mac_tweaks_join_all_spaces_and_go_dark():
    native = FakeNative()
    fw.apply_mac_tweaks(native, "dark")
    assert native.calls == [("behavior", fw.MAC_JOIN_ALL_SPACES), ("appearance", "dark")]
    assert fw.MAC_JOIN_ALL_SPACES == 257          # CanJoinAllSpaces | FullScreenAuxiliary


def test_the_mac_tweaks_are_handed_to_the_main_thread_once_the_window_is_shown():
    native, queued = FakeNative(), []
    window = SimpleNamespace(events=SimpleNamespace(shown=FakeEvent()), native=native)
    assert fw.mac_float_tweaks(window, call_after=queued.append, dark_appearance="dark") is True
    assert native.calls == []                     # 交给主线程，不在这个线程里碰 AppKit
    queued[0]()
    assert [c[0] for c in native.calls] == ["behavior", "appearance"]


def test_the_mac_tweaks_give_up_quietly(capsys):
    never = FakeEvent()
    never.wait = lambda timeout=None: False
    assert fw.mac_float_tweaks(SimpleNamespace(events=SimpleNamespace(shown=never), native=1),
                               call_after=lambda f: None, dark_appearance="d") is False
    no_native = SimpleNamespace(events=SimpleNamespace(shown=FakeEvent()), native=None)
    assert fw.mac_float_tweaks(no_native, call_after=lambda f: None, dark_appearance="d") is False

    def boom(fn):
        raise RuntimeError("no appkit")
    ok = SimpleNamespace(events=SimpleNamespace(shown=FakeEvent()), native=FakeNative())
    assert fw.mac_float_tweaks(ok, call_after=boom, dark_appearance="d") is False
    assert "[警告] 浮窗的 macOS 收尾调整没有生效：no appkit" in capsys.readouterr().out


def test_only_the_mac_host_runs_the_tweaks_and_never_under_the_lock():
    ran = []
    host, factory, _s, _c = make_host(platform="darwin", mac_tweaks=ran.append)
    host.request_open()
    assert ran == [factory.windows[0]]
    host2, _f2, _s2, _c2 = make_host(platform="linux", mac_tweaks=None)
    host2.request_open()                          # 非 macOS 没有收尾调整，也不会因此出错
    assert host2.is_open()
