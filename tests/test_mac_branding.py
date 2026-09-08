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


def test_reapplies_icon_after_the_app_finishes_launching(monkeypatch, tmp_path):
    """Dock 图标在应用完成启动时才创建，之前设的会被盖掉（2026-09-08 实录：名字对了、
    图标仍是白板）。订阅 didFinishLaunching 后必须在回调里再设一次。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    icons = []
    observers = []

    class FakeImage:
        @staticmethod
        def alloc():
            return FakeImage()

        def initWithContentsOfFile_(self, path):
            return self

    class FakeTile:
        def display(self):
            pass

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
    foundation = types.ModuleType("Foundation")
    foundation.NSBundle = FakeBundle
    foundation.NSNotificationCenter = FakeCenter
    foundation.NSOperationQueue = FakeQueue
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "icon-1024.png").write_bytes(b"x")

    macbrand.brand_mac_app(tmp_path)
    assert len(icons) == 1                                   # 启动前先设一次
    assert observers and observers[0][0] == "NSApplicationDidFinishLaunchingNotification"
    observers[0][1](None)                                    # 模拟完成启动的通知
    assert len(icons) == 2                                   # 回调里再设一次
