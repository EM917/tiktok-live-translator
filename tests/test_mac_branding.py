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
