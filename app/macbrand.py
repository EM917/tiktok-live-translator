"""macOS 上给应用窗口一个 Dock 图标和菜单栏名字。

只依赖标准库和随 pywebview 一起安装的 PyObjC；main.py 在起窗口前调用。
"""
import sys
from pathlib import Path

_KEEP = []   # 图像与通知观察者的引用：回调在启动完成后才触发，之前不能被回收


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
    image = None
    for rel in ("TikTok Live Translator.app/Contents/Resources/AppIcon.icns",
                "assets/icon-1024.png"):
        path = Path(root) / rel
        if not path.exists():
            continue
        try:
            image = NSImage.alloc().initWithContentsOfFile_(str(path))
        except Exception:
            image = None
        if image:
            break
    if not image:
        return
    app = NSApplication.sharedApplication()
    try:
        app.setApplicationIconImage_(image)
    except Exception:
        pass
    # Dock 上那块图标是应用「完成启动」时才创建的：在那之前设的图标会被按
    # bundle 查到的（没有 bundle → 空白文档）盖掉。2026-09-08 实录：菜单栏名字
    # 改对了，Dock 仍是白板。所以订阅 didFinishLaunching，在主队列上再设一次。
    try:
        from AppKit import NSApplicationDidFinishLaunchingNotification
        from Foundation import NSNotificationCenter, NSOperationQueue

        def reapply(_note):
            try:
                app.setApplicationIconImage_(image)
                app.dockTile().display()
            except Exception:
                pass

        token = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            NSApplicationDidFinishLaunchingNotification, None,
            NSOperationQueue.mainQueue(), reapply)
        _KEEP.extend([image, token])          # 别让 GC 把图和观察者收走
    except Exception:
        pass
