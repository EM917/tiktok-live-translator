"""app.macbrand.brand_mac_app：Dock 图标与菜单栏名字只是锦上添花，绝不能挡住启动。

2026-09-08 实录：venv 指向的 anaconda python 没有 bundle，Dock 里是一块空白
文档图标，菜单栏写着「python」。修法是在起窗口前自己设——这里钉住它在
非 macOS、缺 PyObjC、图标加载失败时都静默返回。"""
import sys
import types

from app import macbrand


def test_noop_off_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # AppKit 不该被碰：塞一个一 import 就炸的假模块
    monkeypatch.setitem(sys.modules, "AppKit", None)
    assert macbrand.brand_mac_app("/nonexistent") is None


def test_survives_missing_pyobjc(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", None)        # import 抛 ImportError
    monkeypatch.setitem(sys.modules, "Foundation", None)
    assert macbrand.brand_mac_app("/nonexistent") is None


def test_sets_icon_and_name_through_fake_appkit(monkeypatch, tmp_path):
    """用假的 AppKit/Foundation 验证调用序列：名字写进 info 字典、图标设到 NSApplication。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = {"icon": None}
    info = {}

    class FakeImage:
        @staticmethod
        def alloc():
            return FakeImage()

        def initWithContentsOfFile_(self, path):
            return self if path.endswith(".icns") else None

    class FakeApp:
        @staticmethod
        def sharedApplication():
            return FakeApp()

        def setApplicationIconImage_(self, image):
            calls["icon"] = image

    class FakeBundle:
        @staticmethod
        def mainBundle():
            return FakeBundle()

        def infoDictionary(self):
            return info

    appkit = types.ModuleType("AppKit")
    appkit.NSApplication = FakeApp
    appkit.NSImage = FakeImage
    foundation = types.ModuleType("Foundation")
    foundation.NSBundle = FakeBundle
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    icns = tmp_path / "TikTok Live Translator.app" / "Contents" / "Resources"
    icns.mkdir(parents=True)
    (icns / "AppIcon.icns").write_bytes(b"x")
    macbrand.brand_mac_app(tmp_path)
    assert info["CFBundleName"] == "TikTok 直播同传"
    assert isinstance(calls["icon"], FakeImage)


def test_installs_a_dock_tile_view_after_the_app_finishes_launching(monkeypatch, tmp_path):
    """没有 bundle 的进程，启动完成后 Dock 磁贴的底图是系统登记的空白文档，
    setApplicationIconImage_ 只能叠在上面、透明边距透出白（2026-09-08 用
    NSRunningApplication.icon 验证）。完成启动后必须给磁贴装 contentView 整块替换。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    icons = []
    observers = []

    class FakeImage:
        @staticmethod
        def alloc():
            return FakeImage()

        def initWithContentsOfFile_(self, path):
            return self

    class FakeSize:
        width, height = 128.0, 128.0

    class FakeTile:
        content = None

        def size(self):
            return FakeSize()

        def setContentView_(self, view):
            FakeTile.content = view

        def display(self):
            pass

    class FakeImageView:
        @staticmethod
        def alloc():
            return FakeImageView()

        def initWithFrame_(self, frame):
            return self

        def setImage_(self, image):
            self.image = image

        def setImageScaling_(self, mode):
            self.scaling = mode

    class FakeApp:
        @staticmethod
        def sharedApplication():
            return FakeApp()

        def setApplicationIconImage_(self, image):
            icons.append(image)

        def dockTile(self):
            return FakeTile()

    class FakeCenter:
        @staticmethod
        def defaultCenter():
            return FakeCenter()

        def addObserverForName_object_queue_usingBlock_(self, name, obj, queue, block):
            observers.append((name, block))
            return "token"

    class FakeQueue:
        @staticmethod
        def mainQueue():
            return "main"

    class FakeBundle:
        @staticmethod
        def mainBundle():
            return FakeBundle()

        def infoDictionary(self):
            return {}

    appkit = types.ModuleType("AppKit")
    appkit.NSApplication = FakeApp
    appkit.NSImage = FakeImage
    appkit.NSApplicationDidFinishLaunchingNotification = "NSApplicationDidFinishLaunchingNotification"
    appkit.NSImageView = FakeImageView
    appkit.NSImageScaleProportionallyUpOrDown = 3
    appkit.NSMakeRect = lambda x, y, w, h: (x, y, w, h)
    foundation = types.ModuleType("Foundation")
    foundation.NSBundle = FakeBundle
    foundation.NSNotificationCenter = FakeCenter
    foundation.NSOperationQueue = FakeQueue
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "icon-1024.png").write_bytes(b"x")

    macbrand.brand_mac_app(tmp_path)
    assert len(icons) == 1                                   # 启动前先设一次（弹跳阶段用）
    assert observers and observers[0][0] == "NSApplicationDidFinishLaunchingNotification"
    assert FakeTile.content is None                          # 磁贴在完成启动前不存在，不能碰
    observers[0][1](None)                                    # 模拟完成启动的通知
    # 完成启动后装的是磁贴 contentView（整块替换底图），而不是再叠一次图标
    assert isinstance(FakeTile.content, FakeImageView)
    assert FakeTile.content.image is icons[0]
    assert len(icons) == 1
