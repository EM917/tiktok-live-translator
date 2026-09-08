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
        app.setApplicationIconImage_(image)   # 启动弹跳阶段 Dock 画的就是它
    except Exception:
        pass
    # 启动完成后 Dock 磁贴的底图换成系统登记给这个进程的图标——没有 bundle 的
    # python 进程登记到的是那张空白文档（2026-09-08 用 NSRunningApplication.icon
    # 栅格化验证过）。setApplicationIconImage_ 只是叠在底图上，图标的透明边距
    # 会透出下面的白，看起来就是「带白框、还变大了」。给磁贴一个 contentView
    # 才是整块替换：透明处露出的是 Dock 背景，不是文档图。磁贴在完成启动时
    # 才存在，所以订阅 didFinishLaunching 在主队列上装。
    def install_tile():
        try:
            from AppKit import NSImageScaleProportionallyUpOrDown, NSImageView, NSMakeRect
            tile = app.dockTile()
            size = tile.size()
            view = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, size.width, size.height))
            view.setImage_(image)
            view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
            tile.setContentView_(view)
            tile.display()
            _KEEP.append(view)
        except Exception:
            pass

    try:
        from AppKit import NSApplicationDidFinishLaunchingNotification
        from Foundation import NSNotificationCenter, NSOperationQueue

        token = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            NSApplicationDidFinishLaunchingNotification, None,
            NSOperationQueue.mainQueue(), lambda _note: install_tile())
        _KEEP.extend([image, token])          # 别让 GC 把图和观察者收走
    except Exception:
        pass
