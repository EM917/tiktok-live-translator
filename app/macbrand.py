"""macOS 上给应用窗口一个 Dock 图标和菜单栏名字。

只依赖标准库和随 pywebview 一起安装的 PyObjC；main.py 在起窗口前调用。
"""
import sys
from pathlib import Path


def brand_mac_app(root):
    """macOS：给 Dock 图标和菜单栏名字。

    拥有窗口的进程是 python，不是那个 .app：venv 指向的 anaconda python 没有
    bundle，系统给这种 GUI 进程画的是一块空白文档图标，菜单栏也只写「python」
    （2026-09-08 实录）。从 .app 双击也一样——启动脚本最后 exec 的还是 python。
    所以在起窗口之前自己把图标和名字设上。pywebview 在 macOS 上本就依赖
    PyObjC，不加依赖；任何一步失败都静默跳过，品牌不能挡住启动。"""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
    except Exception:
        return
    try:
        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "TikTok 直播同传"
        info["CFBundleDisplayName"] = "TikTok 直播同传"
    except Exception:
        pass
    for rel in ("TikTok Live Translator.app/Contents/Resources/AppIcon.icns",
                "assets/icon-1024.png"):
        path = Path(root) / rel
        if not path.exists():
            continue
        try:
            image = NSImage.alloc().initWithContentsOfFile_(str(path))
            if image:
                NSApplication.sharedApplication().setApplicationIconImage_(image)
                return
        except Exception:
            continue
