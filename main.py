#!/usr/bin/env python3
# i18n: done
"""TikTok 直播同传 —— 本地实时字幕翻译工具入口。

用法示例：
    python main.py https://www.tiktok.com/@somebody/live          # 翻译成中文（默认）
    python main.py https://www.tiktok.com/@somebody/live --target ja
    python main.py --demo                                          # 演示模式，只看 UI
"""
import argparse
import asyncio
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from app import i18n, native_dialog
from app.fdlimit import raise_nofile_limit
from app.i18n import APP_NAME, L, of
from app.macbundle import remember_launch_python
from app.runlog import start as start_run_log
from app.stdio import harden_stdio

# 必须是 import 之后的第一句可执行语句：下面的版本检查、ensure_env() 的安装提示、--doctor 的
# 体检报告都会写标准流，而 Windows 上重定向/管道的 stdout 默认是 ANSI 代码页 +
# errors='strict'——一个 ✅ 或一个西语 ñ 就让进程带着 UnicodeEncodeError 退出
# （2026-09-15 中文 Windows 11 实测，见 app/stdio.py）。首次安装后 execv 进 .venv、
# 一键更新后重启，都会重新从这里跑一遍；窗口模式的后台线程与本进程共用这两个流。
harden_stdio()

# 运行日志（app/runlog.py）：双击 .app 启动时标准输出、标准错误接在 /dev/null 上，下面几百处 print
# 哪儿都不留。macOS 上没有终端时另抄一份到 ~/Library/Logs/TikTokLiveTranslator/run.log（落盘前去密钥，
# 按大小轮转），原来的流照写。紧跟护栏、赶在第一次可能打印之前；每一跳 exec 之后都从这里重新装一遍。永不抛
start_run_log(Path(__file__).resolve().parent)

# 文件句柄上限（专家审查 S1）：从 Finder 启动的 .app 继承 launchd 的软上限 256，局域网里
# 一台设备对手机同看端口挂上百条空连接，之后新开的文件、ffmpeg、网络连接就全部 EMFILE。
# 放在这么前面：要赶在任何东西开始占句柄之前；setrlimit 跨 execv 保留，进 .venv 后再跑一遍
# 也只是读到「已经够高」。失败不拦启动，只打一行——见 app/fdlimit.py
_NOFILE = raise_nofile_limit()
if _NOFILE.status == "failed":
    print("[警告] 没能把同时打开文件数的上限从 {} 提高到 {}，系统返回：{}".format(
        _NOFILE.soft_before if _NOFILE.soft_before is not None else "?",
        _NOFILE.target, _NOFILE.error))

# 启动用的解释器路径：要在任何 exec（ensure_env 进 .venv、main() 进 .app）之前记下，
# 「完全磁盘访问权限」的步骤里要把它原样告诉中控（见 app/macbundle.py、browser_login.fda_targets）
remember_launch_python()

ROOT = Path(__file__).resolve().parent

# 界面语言（app/i18n.py）：每个进程都跑一次，只读——记下装机状态与系统语言（经环境变量带过
# 下面的 execv / execve），给自举期的对话框定语言。settings.json 在这里只看不改：损坏的文件
# 要留给最终进程去备份（main() 里的 settle），否则备份提示会随 exec 丢掉
i18n.boot(ROOT)

# 最低 Python 3.10（用户 10-22 决定 D14，数字与 app/bootstrap.MIN_PYTHON 一致）。以前低于最低版本时
# 只往终端写一句再退出：双击 .app、一键更新后重启时 stdout 是 /dev/null，程序就无声消失了。现在
# 打印之外没有终端时弹系统对话框，写明需要升级和照做的步骤（bootstrap.refuse_old_python）。这个
# 文件和上面 import 的模块都要能被 3.9 解析，否则走不到这里（tests/test_bootstrap_constraints.py）
if sys.version_info < (3, 10):
    from app.bootstrap import refuse_old_python
    sys.exit(refuse_old_python())


_CORE_DEPS = ["aiohttp>=3.9", "numpy>=1.24", "faster-whisper>=1.0",
              "yt-dlp", "imageio-ffmpeg>=0.5"]
# mlx-whisper 装不上时留个记号（JSON：at / pip_exit / note，见 app/bootstrap.py），
# 免得每次启动都重试一遍、把启动挡上几分钟（老系统上它确实可能没有轮子）。
# 程序空闲时每天在后台重试一次（Updater.retry_mlx_install）；删掉这个文件也会再试。
_MLX_GIVEUP = ROOT / ".venv" / ".mlx-unavailable"


def _wants_mlx():
    """这台机器是否**应该**用 GPU 后端。

    对 Apple Silicon 来说 mlx-whisper 不是可有可无：没有它就得用 CPU 跑，
    识别慢一倍以上、还只能用较小的模型（hwdetect 实测 RTF 0.99，勉强跟得上，
    跑久了必然积压）。所以它要算进「依赖齐不齐」里。"""
    import platform as _p

    return (sys.platform == "darwin"
            and _p.machine().lower() in ("arm64", "aarch64")
            and not _MLX_GIVEUP.exists())


def _optional_deps():
    """可选组件，逐个装——一个失败不该连累其余。"""
    out = ["mlx-whisper"] if _wants_mlx() else []
    return out + ["curl_cffi>=0.7", "pywebview>=5"]


def _ready_modules():
    mods = ["aiohttp", "numpy", "faster_whisper", "yt_dlp", "imageio_ffmpeg"]
    if _wants_mlx():
        mods.append("mlx_whisper")
    return mods


def _ready_import():
    return "import " + ", ".join(_ready_modules())  # i18n: data（交给 python -c 的语句）


def _deps_ok():
    try:
        import aiohttp  # noqa: F401
        import faster_whisper  # noqa: F401
        import numpy  # noqa: F401
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    # Apple Silicon 上缺 mlx-whisper 也算依赖不全。以前不查，于是一次退化成
    # 「只装核心依赖」的安装之后，这个闸门永远返回 True，mlx-whisper 再也没有
    # 机会被补上——机器就一直用着 CPU 路径，而屏幕上什么也没说。
    if _wants_mlx():
        try:
            import mlx_whisper  # noqa: F401
        except ImportError:
            return False
    # ffmpeg 也是硬依赖：不查它的话，安装中途断掉会让「重开自动补装」的承诺落空
    try:
        import imageio_ffmpeg  # noqa: F401
    except ImportError:
        import shutil
        if shutil.which("ffmpeg") is None:
            return False
    return _requirements_current()


def _requirements_current():
    """requirements.txt 和上次在本项目 .venv 里装成功的那份一致吗（app/bootstrap.py）。

    以前这里只查固定的几个模块：一键更新或 git pull 带来新依赖、而 pip 没装上时，
    重开程序既不补装、也说「会自动继续安装」，于是每次启动都卡在同一个 ImportError。
    用户自己装齐依赖的 Python 不归这里管；判断本身出错不挡启动。"""
    try:
        from app import bootstrap
        return bootstrap.requirements_current(ROOT)
    except Exception:
        return True


def _venv_python():
    sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return ROOT / ".venv" / sub


def _note_ready():
    """_deps_ok() 刚通过：在本项目的 .venv/.app 壳里时记下就绪指纹（I9，见 bootstrap.note_ready），
    下次启动的第一跳就不用再起子进程查一遍。只在第一跳要 import 的模块这里也都 import 成功了才记：
    _deps_ok 对 imageio_ffmpeg 网开一面（系统里有 ffmpeg 就行），第一跳的检查不会，那种时候记了
    指纹，第一跳就再也不补装它了。出错不挡启动。"""
    try:
        if not all(m in sys.modules for m in _ready_modules()):
            return
        from app import bootstrap
        bootstrap.note_ready(ROOT, _ready_import())
    except Exception:
        pass


def _forget_ready():
    """在本项目的 .venv/.app 壳里发现组件不齐：删掉就绪指纹，下次启动重新检查（I9）。
    第一跳（启动器选的 Python）上什么都不做，见 bootstrap.forget_ready。"""
    try:
        from app import bootstrap
        bootstrap.forget_ready(ROOT)
    except Exception:
        pass


def _install_deferred():
    """上一跳因为端口上已有本程序在跑，把安装推迟了（I7，见 bootstrap.install_deferred）。
    只有 POSIX 认得出来；Windows 上的最终进程认不出，在 ensure_env 里自己再判一次、照样不装。"""
    try:
        from app import bootstrap
        return bootstrap.install_deferred(ROOT)
    except Exception:
        return False


def _has_console():
    """stdout 或 stderr 任一连着终端就算有终端——`| tee log` 这类重定向下
    不该弹系统对话框。"""
    try:
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and stream.isatty():
                return True
    except Exception:
        pass
    return False


_INFO_DIALOG = None
_LAUNCH_DIALOG = None     # 第一跳起的「正在启动」提示（osascript 进程），见 _start_launch_dialog
_FIRST_RUN_SHOWN = False


def _info_dialog(message):
    """无终端可看时（双击 .app 启动）弹一个非阻塞的系统提示框；有终端只打印。"""
    global _INFO_DIALOG
    print(message)
    if os.environ.get("CI"):
        return   # CI/无头自动化环境：只留打印，不能弹阻塞对话框
    if _has_console() or sys.platform != "darwin":
        return   # Windows 走 Start.bat，有黑窗口能看到 print
    try:
        _INFO_DIALOG = subprocess.Popen(
            native_dialog.osascript_argv(message, button=native_dialog.INFO_BUTTON,
                                         icon="note", giving_up=600),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        _INFO_DIALOG = None


def _close_info_dialog():
    global _INFO_DIALOG
    if _INFO_DIALOG is not None and _INFO_DIALOG.poll() is None:
        try:
            _INFO_DIALOG.terminate()
        except Exception:
            pass
    _INFO_DIALOG = None


def _start_launch_dialog():
    """第一跳一开始：「正在启动」的非阻塞提示，osascript 自己等 4 秒再弹（app/bootstrap.launch_dialog_argv）。
    启动快于 4 秒，窗口出来时它还没弹就被关掉了。只在 macOS、没有终端、不是 CI 时起；上一跳已经起过（环境变量里
    有它的 PID）就不再起。2026-10-29：从「聚焦搜索」点开 .app 之后半分钟到一分钟没有任何窗口，用户以为没反应。"""
    global _LAUNCH_DIALOG
    if os.environ.get("CI") or _has_console() or sys.platform != "darwin":
        return
    from app import bootstrap
    if os.environ.get(bootstrap.LAUNCH_DIALOG_ENV):
        return
    try:
        _LAUNCH_DIALOG = subprocess.Popen(bootstrap.launch_dialog_argv(),
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        _LAUNCH_DIALOG = None


def _close_launch_dialog():
    """关掉「正在启动」的提示：本进程自己起的直接关；上一跳交过来的（exec 前没关，PID 在环境变量里）核对过
    确实是自己的 osascript 子进程再关（app/bootstrap.close_handed_dialog）。开好窗口、要弹出错对话框、
    要换成安装说明、进程退出时都走这里，重复调用没有副作用。"""
    global _LAUNCH_DIALOG
    if _LAUNCH_DIALOG is not None and _LAUNCH_DIALOG.poll() is None:
        try:
            _LAUNCH_DIALOG.terminate()
        except Exception:
            pass
    _LAUNCH_DIALOG = None
    try:
        from app import bootstrap
        bootstrap.close_handed_dialog()
    except Exception:
        pass


def _hand_off_dialogs():
    """exec 之前：开着的提示框不关——换进最终进程之后还要 import、起后端、等后端才开窗口，以前这一段什么提示都
    没有。安装说明开着就交它（它说的「完成后字幕窗口会自动打开」这时照样成立），否则交「正在启动」；PID 放进
    环境变量（execve 不换 PID，它仍是最终进程的子进程），最终进程开好窗口后关。交不出去的那个关掉。"""
    global _INFO_DIALOG, _LAUNCH_DIALOG
    from app import bootstrap
    alive = [d for d in (_INFO_DIALOG, _LAUNCH_DIALOG) if d is not None and d.poll() is None]
    keep = alive[0] if alive else None
    for d in alive[1:]:
        try:
            d.terminate()
        except Exception:
            pass
    bootstrap.hand_off_dialog(keep.pid if keep is not None else None)
    _INFO_DIALOG = _LAUNCH_DIALOG = None


def _fail_alert(message):
    """报告致命问题：终端打印之外，无终端时必须弹系统对话框——否则用户只会看到
    程序无声消失。"""
    print("⚠️ " + message)
    _close_launch_dialog()        # 「正在启动」不能和出错的对话框叠在一起，也不能在出错之后还挂着
    if os.environ.get("CI") or _has_console():
        return
    try:
        if sys.platform == "darwin":
            _close_info_dialog()
            subprocess.run(
                native_dialog.osascript_argv(message, button=native_dialog.FAIL_BUTTON,
                                             icon="caution"),
                capture_output=True, timeout=60)
        elif os.name == "nt":
            import ctypes
            text, title = native_dialog.messagebox_args(message)
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x30)
    except Exception:
        pass


def _acquire_bootstrap_lock():
    """首次安装要几分钟且窗口模式下毫无提示，用户很容易再双击一次。
    用锁文件串行化：第二个进程等第一个装完，而不是并发写坏同一个 .venv。
    状态含义见 app/bootlock.py（最多等 15 分钟）。"""
    from app.bootlock import acquire

    return acquire(ROOT / ".venv.lock", _deps_ok)


def _first_run_dialog(text=None):
    """「正在安装…」只在真要创建环境/装依赖时弹。以前放在 ensure_env
    开头：.app 里的 python 没装齐 sys.path 时 _deps_ok() 为假，于是**每次**
    双击都闪一下这个对话框，两三秒后才发现什么都不用装——用户以为又在重装。
    text 由 app/bootstrap.install_dialog_text 按实际情况给（首次运行、Python 版本
    变了、组件清单变了……）。"""
    global _FIRST_RUN_SHOWN
    if _FIRST_RUN_SHOWN:
        return
    _FIRST_RUN_SHOWN = True
    _close_launch_dialog()        # 「正在启动」换成安装说明
    _info_dialog(text or L("首次运行：正在自动安装运行组件（约需 2–5 分钟，取决于网速）。\n"
                           "完成后字幕窗口会自动打开——请耐心等待，不要重复打开程序。",
                           "First launch: installing the app’s components (about 2–5 minutes, "
                           "depending on your connection).\nThe caption window opens "
                           "automatically when it’s done. Don’t open the app again in the "
                           "meantime."))


def _existing_instance_url(base_port):
    """本程序是否已在运行。是则返回 app/instance.Found(UI 地址, 是不是这个文件夹里的这一份, 对方的版本号)，
    否则 None。Found 是非空元组、永远为真，只问「有没有」的几处照旧当真假用。
    用于双击两次的场景：复用已有实例，而不是再起一个后端。
    必须扫完整个自动回退区间（base_port 起 10 个端口）——已有实例可能因
    base_port 被第三方占用而跑在后面的端口上。先用 TCP 探测过滤（本机关闭
    端口瞬时拒绝），只对开着的端口发 HTTP 验指纹（app/instance.py），扫描耗时可忽略。
    是不是这一份看首页的响应头（X-TLT-Install，加这个头之前的版本没有它，这时为 None）：
    2026-10-29 双击打开时窗口被开到了另一个文件夹里的副本上，看起来像界面自己变了语言。"""
    import socket
    import urllib.request

    from app.instance import PROBE_BYTES, Found, looks_like_us, peer_version, same_install
    for port in range(base_port, base_port + 10):
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                continue
        url = "http://127.0.0.1:{}".format(port)
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if looks_like_us(resp.read(PROBE_BYTES).decode(errors="replace")):
                    return Found(url, same_install(resp.headers, ROOT), peer_version(resp.headers))
        except Exception:
            pass
    return None


def ensure_env():
    """零手动安装：缺依赖时自动创建虚拟环境、装齐 requirements，然后换进新环境继续跑。

    判断、文案和安装步骤在 app/bootstrap.py——这里没法测试（import main 就会跑到这儿）。

    就绪指纹（I9）：组件齐了的进程记下 .venv 的样子；第一跳看它没变就跳过子进程检查，直接换进
    最终进程（macOS 开窗口时直接进 .app 壳）。推迟安装（I7）：现有 .venv 能跑、只是清单变了、而本程序
    已经在跑时这次不装，换进去的那个进程也不装（POSIX 凭 _install_deferred 认出来；Windows 认不出，
    最终进程自己再走一遍检查和 defer_install，照样不装）。"""
    if _install_deferred():
        return
    if _deps_ok():
        _note_ready()
        return
    # 本项目的环境里 import 不齐（或清单要补装）：指纹作废，下次启动的第一跳照旧检查。第一跳
    # 在启动器选的 Python 上 import 不了是常态，那里不删（_forget_ready 只在 .venv/.app 壳里删）
    _forget_ready()
    vpy = _venv_python()
    in_project_venv = Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()
    if not in_project_venv:
        # 第一跳：这一次启动的时间点从这里记起（logs/bootstrap-*.log，之后几跳接着写），没有终端时起「正在启动」
        # 的提示。2026-10-29 那次半分钟到一分钟没有窗口，日志里却没有一个时间点，花在哪儿查不出来
        from app import bootstrap as boot
        boot.start_launch_log(ROOT)
        boot.launch_mark("第一跳开始（{}）".format(sys.executable))  # i18n: terminal
        _start_launch_dialog()
    lock = None
    ready = False         # 第一跳确认指纹没变：不再检查、不装，直接换进 .venv（I9）
    deferred = False      # 要装，但本程序已经在跑：这次不装（I7）
    venv_created = False  # 这一轮刚 venv.create 过：空环境，不装就起不来，不能推迟（I7）
    if not in_project_venv:
        state, lock = _acquire_bootstrap_lock()
        if state == "busy":
            _fail_alert(L("另一个实例仍在安装组件（已等待 15 分钟）。请等它装完后再打开本程序，"
                          "避免两边同时安装把环境写坏。",
                          "Another copy of the app is still installing components (waited 15 "
                          "minutes). Wait for it to finish, then open the app again, so the two "
                          "don’t install at the same time and damage the setup."))
            return
    log_path = None
    target_version = None
    try:
        from app import bootstrap

        if _deps_ok():        # 等锁期间别的实例已经装好了
            return
        here = "{}.{}".format(*sys.version_info[:2])
        recorded = bootstrap.pyvenv_version(ROOT / ".venv")    # 建环境时的 Python 版本
        if in_project_venv:
            target_version = here
        else:
            # 光看路径存在不够：上次安装被中断会留下残缺的 venv，链到的 Python 被卸掉
            # 也一样——必须实际跑一下确认，顺便拿到它现在的版本。能跑、但次版本和
            # pyvenv.cfg 记的不一样、在这个版本下组件和 pip 又都 import 不了，也重建：原地补装
            # 找不到 pip。组件或 pip 还在就不清（判断见 bootstrap.venv_needs_rebuild）
            usable, target_version = (bootstrap.interpreter_version(vpy)
                                      if vpy.exists() else (False, None))
            # I9：解释器照旧实际跑一下（几十毫秒；跑不起来要重建，指纹看不出这个），其余的看就绪指纹：
            # 和最终进程上次记下的一样，就不再起子进程 import 一遍核心组件（约 2 秒）、不再探 pip
            # 实际跑出来的主次版本也要和 pyvenv.cfg 记的一样（ready_fast_path 里比）：指纹的链接终点
            # 比的是启发式，次版本漂移不能走捷径
            ready = usable and bootstrap.ready_fast_path(ROOT, _ready_import(),
                                                         actual_version=target_version, python=vpy)
            if not ready:
                if bootstrap.venv_needs_rebuild(usable, recorded, target_version,
                                                own_env=bootstrap.managed_env(ROOT),
                                                python=vpy, ready_import=_ready_import()):
                    # 清空之前先看端口上有没有本程序在跑：直播中再双击一次把窗口叫到前台就会走到
                    # 这里——ensure_env 在模块级就跑，比 main() 里认出已有实例早。有就不清：clear=True
                    # 会连它正用着的 site-packages 一起删掉，它之后的延迟导入全部失败，还要在直播时
                    # 联网拉约 1.4 GB。.venv 还不存在就没东西可删。8765 是 parse_args 的默认端口
                    # （这时还没解析参数），双击启动用的就是它
                    if (ROOT / ".venv").is_dir() and _existing_instance_url(8765):
                        _fail_alert(bootstrap.rebuild_blocked_text())
                        sys.exit(1)
                    _first_run_dialog(bootstrap.install_dialog_text(
                        recorded, here, venv_existed=recorded is not None))
                    print("[初始化] 正在创建虚拟环境（仅需一次，可能几分钟）…")
                    import venv

                    venv.create(ROOT / ".venv", with_pip=True, clear=True)
                    target_version = here
                    venv_created = True
        pip_python = sys.executable if in_project_venv else str(vpy)
        plan = None
        imports_ok = False
        if not ready:
            bootstrap.launch_mark("import 检查开始")  # i18n: terminal
            check = subprocess.run(
                [pip_python, "-c",
                 _ready_import()],
                capture_output=True,
            )
            imports_ok = check.returncode == 0
            bootstrap.launch_mark("import 检查结束（退出码 {}）".format(check.returncode))  # i18n: terminal
            # 核心模块都在、只是 requirements.txt 和上次装成功的那份不一样（更新带来了
            # 新依赖）：也要补装。以前这一步只看 import，新依赖永远没人装。
            # 装不装、弹什么、过多久弹，在 bootstrap.plan_install 里（这里测不了）
            plan = bootstrap.plan_install(imports_ok, ROOT, recorded, target_version)
        else:
            bootstrap.launch_mark("就绪指纹没变，跳过 import 检查")  # i18n: terminal
        # I7：现有的 .venv 能跑、只是清单变了要补装，而端口上已经有本程序在跑（直播中从 Start.command、
        # Start.bat 或命令行又开了一次），它可能正用着这个 .venv：pip 换掉它脚下的包，它之后的延迟导入
        # 可能拿到装了一半的东西。这次不装，照常换进 .venv——最终进程会认出已有实例、把窗口叫到前台；
        # 换进去的那个进程也不装（POSIX 凭环境变量认出来；Windows 上认不出，那个进程跑在 .venv 里，
        # 走到这里自己再判一次）。等下次没有实例在跑时再装。这一轮刚建的 .venv、核心组件 import 不齐的
        # 不推迟：不装就起不来（新解压的一份第一次打开、旧的那份还在直播，就是这种）。
        # 这一句不能挪进 `if not in_project_venv` 或 `if not ready`：Windows 的推迟全靠它在最终进程里再跑一次。
        # 端口探测只在真要装、而且能推迟时才做
        if bootstrap.defer_install(plan, ROOT, lambda: _existing_instance_url(8765),
                                   imports_ok=imports_ok, venv_created=venv_created):
            print("[初始化] 运行组件需要补装，但本程序已经在运行、正用着这些组件，这次先不装；"
                  "退出正在运行的程序后再打开，会自动补装")
            plan = None
            deferred = True
        if plan is not None:
            timer = None
            if plan["delay"] > 0:
                import threading
                timer = threading.Timer(plan["delay"], _first_run_dialog, args=(plan["text"],))
                timer.daemon = True
                timer.start()
            else:
                _first_run_dialog(plan["text"])
            print("[初始化] 正在安装依赖（含内置 ffmpeg，可能需要几分钟）…")
            # pip 的输出同时进终端和 logs/bootstrap-*.log：双击 .app 时 stdout 是
            # /dev/null，以前装失败了什么都不留
            # 这一次启动的日志已经建了（第一跳）就接着写，时间点和 pip 输出在同一个文件里
            log_path = bootstrap.launch_log_path() or bootstrap.new_log_path(ROOT, "bootstrap")
            bootstrap.launch_mark("pip 开始")  # i18n: terminal
            try:
                bootstrap.install_requirements(pip_python, ROOT, _CORE_DEPS, _optional_deps(),
                                               log_path=log_path)
            finally:
                bootstrap.launch_mark("pip 结束")  # i18n: terminal
                if timer is not None:
                    timer.cancel()
        if not in_project_venv:
            if lock is not None:      # execv 不会执行 finally，先手动释放锁
                try:
                    lock.unlink()
                except OSError:
                    pass
                lock = None
            # 提示框不关：换进去之后还要 import、起后端、等后端才开窗口。PID 交给最终进程，它开好窗口再关
            _hand_off_dialogs()
            bootstrap.launch_mark("换进最终进程（exec）")  # i18n: terminal
            # exec 不替 Python 冲输出缓冲：stdout 不是终端（`| tee`、重定向到文件）时，上面打的
            # [初始化] 几行会跟着丢掉
            from app.relaunch import flush_std
            flush_std()
            if deferred:
                bootstrap.mark_install_deferred()
            # I9：macOS 开窗口时直接进 .app 壳里的 python——和 main() 开头是同一个函数，只是提前到
            # 这里：以前先 execv 进 .venv/bin/python，那个进程再 import 一遍组件（约 2 秒）才在 main()
            # 里进壳。壳里的 python 链回 .venv/bin/python，sys.prefix 是 Contents、site-packages 落回
            # .venv；「完全磁盘访问权限」认的启动解释器在顶上 exec 之前已经记下了。不该进壳（--browser
            # 一类参数、不是 macOS）、壳没备好或起不来时它会返回，照旧进 .venv/bin/python
            if sys.platform == "darwin":
                from app.macbundle import relaunch_inside_bundle
                relaunch_inside_bundle(ROOT)
            argv = [str(vpy), str(ROOT / "main.py")] + sys.argv[1:]
            if os.name == "nt":
                # I11：Windows 上 CRT 的 execv 是「起新进程、自己立刻以 0 退出」，Start.bat 等的进程
                # 马上结束，「出错就 pause」永远轮不到。改成起子进程、等它、带着它的退出码退出（不会返回）
                from app.relaunch import replace_process
                replace_process(argv)
            from app.relaunch import exec_args
            # 进壳没成功时 relaunch_inside_bundle 刚打了一行「[信息] 未能以应用身份重新启动」：
            # 在上面那次冲缓冲之后，exec 前再冲一次，stdout 是管道时它才不会丢
            flush_std()
            os.execv(str(vpy), exec_args(argv))
    except Exception as exc:
        # 按 pip 输出里真实出现的字样说明（磁盘满 / 连不上软件包服务器 / 没有适用于
        # 这个 Python 的安装包），附日志位置——不再把 CalledProcessError 原文给中控
        try:
            from app import bootstrap
            message = bootstrap.failure_text(exc, log_path, python_version=target_version,
                                             root=ROOT)
        except Exception:
            message = (L("自动安装未完成（{}）。\n"
                         "请检查网络连接，然后重新打开本程序——会自动从中断处继续安装。\n"
                         "（进阶：也可手动运行 setup.sh / setup.ps1，"
                         "或 pip install -r requirements.txt -c constraints.txt）",
                         "Installation didn’t finish ({}).\n"
                         "Check your network connection, then open the app again. Installation "
                         "picks up where it left off.\n"
                         "(Advanced: you can also run setup.sh / setup.ps1, or pip install -r "
                         "requirements.txt -c constraints.txt)").format(of(exc)))
        _fail_alert(message)
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass


if "--doctor" not in sys.argv:
    ensure_env()

def _forget_install_failure():
    """程序因为缺模块起不来：让下次启动立刻重试安装（见 bootstrap.forget_requirements_failure），
    并删掉就绪指纹（I9）——否则下次启动的第一跳看指纹没变，不检查就直接进来，又撞上同一个缺模块。"""
    try:
        from app.bootstrap import forget_requirements_failure
        forget_requirements_failure(ROOT)
    except Exception:
        pass
    _forget_ready()


try:
    from app.translator import TRANSLATOR_CHOICES, restore_engine  # noqa: E402
except ImportError as exc:
    _forget_install_failure()
    _fail_alert(L("组件尚未安装完成，程序暂时无法启动（{}）。\n"
                  "请检查网络后重新打开本程序，会自动继续安装。",
                  "The app’s components aren’t fully installed yet, so it can’t start ({}).\n"
                  "Check your network, then open the app again. Installation continues "
                  "automatically.").format(of(exc)))
    sys.exit(1)


def _load_settings():
    """settings.json：界面里改过的偏好（如目标语言）跨重启保留。"""
    from app.settings import load_settings
    return load_settings()


def parse_args():
    p = argparse.ArgumentParser(  # i18n: terminal
        prog="tiktok-live-translator",
        description="监听 TikTok 直播间，把主播的语音实时转写并翻译成字幕（全部本地运行，浏览器 UI 展示）。",
    )
    p.add_argument("url", nargs="?", default=None,  # i18n: terminal
                   help="直播间地址（可选——不填则启动后在网页里输入），"
                        "例如 https://www.tiktok.com/@user/live；也可以直接给 .flv/.m3u8 流地址")
    p.add_argument("--target", default=None,  # i18n: terminal
                   help="字幕译成哪种语言（语言代码），默认 zh-CN（简体中文）；界面里改过的话记住上次的选择。"
                        "顶栏「字幕译成」菜单只列 zh-CN/zh-TW/en/es，这里给别的代码（如 ja）照样生效，菜单里会多出这一项")
    p.add_argument("--source", default=None,  # i18n: terminal
                   help="主播语言代码，例如 es/en/ja；也可以是逗号分隔的列表如 es,en"
                        "（自动检测但只在列表里选，最多 4 个，第一个是主语言，"
                        "检测到列表外的语言会强制重转一遍）。默认记住界面里上次的选择，"
                        "从未选过则是 es,en（想不限制地逐段自动检测请传 auto 或在界面里选）")
    p.add_argument("--model", default=None,  # i18n: terminal
                   help="whisper 模型：tiny/base/small/medium/large-v3/large-v3-turbo。"
                        "默认：mlx 后端用 large-v3（GPU 跑得动最准的），ct2 后端用 large-v3-turbo")
    p.add_argument("--backend", default="auto", choices=["auto", "mlx", "ct2"],  # i18n: terminal
                   help="识别后端：mlx（Apple GPU，Mac 推荐）/ ct2（faster-whisper，CPU/CUDA）。"
                        "auto=装了 mlx-whisper 就用 mlx")
    p.add_argument("--beam", type=int, default=5,  # i18n: terminal
                   help="beam search 宽度，越大越准越慢，默认 5（设 1 即贪心解码）")
    p.add_argument("--asr-temperature", type=float, default=None, dest="asr_temperature",  # i18n: terminal
                   help="识别解码温度，默认 0（只解码一次）。Whisper 默认会在质量不达标时"
                        "用更高温度重解码最多 6 次，音乐段上实测能让单次识别涨到 25 秒")
    p.add_argument("--context", action="store_true",  # i18n: terminal
                   help="开启滚动上下文（把上一段识别结果喂给下一段）。默认关闭："
                        "实测它会诱发复读死循环，反而大幅拉低召回率")
    p.add_argument("--device", default="auto", help="识别设备，默认 auto（Mac 上即 CPU）")  # i18n: terminal
    p.add_argument("--compute-type", default="auto", dest="compute_type",  # i18n: terminal
                   help="faster-whisper compute_type，默认 auto；CPU 上想更快可用 int8")
    p.add_argument("--translator", default=None, choices=TRANSLATOR_CHOICES,  # i18n: terminal
                   help="翻译引擎：auto / hymt2 / hymt2-7b / gemma / deepl / google / "
                        "claude / openai / none。默认记住界面里上次的选择，"
                        "没有选过则 auto（本地有模型用本地，否则 google）")
    p.add_argument("--port", type=int, default=8765, help="本地 UI 端口，默认 8765")  # i18n: terminal
    p.add_argument("--denoise", choices=["auto", "on", "off"], default="auto",  # i18n: terminal
                   help="RNNoise 人声降噪，抑制背景音乐/噪声（auto=模型文件存在即开启，默认）")
    p.add_argument("--cookies-browser", default="auto", dest="cookies_browser",  # i18n: terminal
                   help="解析直播流时借用哪个浏览器的 TikTok 登录状态："
                        "auto（默认；macOS 上解析的第一步只读 Safari，读到登录就不读别的浏览器，"
                        "其它浏览器只在匿名方式都没拿到地址之后才读）/ "
                        "chrome / safari / firefox / edge / none（完全不读浏览器 cookie）。"
                        "显式指定时优先于 settings.json 里的 cookies_browser_only")
    p.add_argument("--cookies", default=None,  # i18n: terminal
                   help="可选：传给 yt-dlp 的 cookies.txt 路径（yt-dlp 报告直播间要登录才能看时用）")
    p.add_argument("--glossary", default=None,  # i18n: terminal
                   help="领域词表路径，默认项目目录下的 glossary.txt"
                        "（首次运行会从 glossary.example.txt 生成）")
    p.add_argument("--banned-terms", default=None, dest="banned_terms",  # i18n: terminal
                   help="违禁词表路径，默认项目目录下的 banned_terms.txt"
                        "（首次运行会从 banned_terms.example.txt 生成）")
    p.add_argument("--no-comments", action="store_false", dest="comments",  # i18n: terminal
                   help="不抓取观众评论（默认抓取）")
    p.add_argument("--demo", action="store_true",  # i18n: terminal
                   help="演示模式：不连直播，用内置台词驱动 UI（用来验证界面）")
    p.add_argument("--doctor", action="store_true",  # i18n: terminal
                   help="环境体检：检测本机硬件并打印推荐配置，不启动服务")
    p.add_argument("--browser", action="store_true",  # i18n: terminal
                   help="在浏览器里打开界面（默认在独立应用窗口中打开）")
    p.add_argument("--no-open", action="store_true", help="启动后不要自动打开浏览器/窗口")  # i18n: terminal
    # boot() 已经直接从 sys.argv 读过它（要赶在这里和自举对话框之前）；这里登记只为不报「未知参数」
    p.add_argument("--ui-lang", dest="ui_lang", choices=("zh", "en"), default=None,  # i18n: terminal
                   help="一次性指定界面语言，不改设置")
    return p.parse_args()


def _say(text):
    """收尾路上的终端行：终端可能已经没了（关了终端窗口），打不出来也不许抛。"""
    try:
        print(text, flush=True)
    except Exception:
        pass


def _recover_unclosed_audits(port):
    """上一次没写结尾的审计（崩溃、强杀、断电时留下的）在末尾补一条 session_end（专家审查 B5，规则见
    app/audit_recover.py）。在起服务、开第一场之前做；端口上有本程序在跑（--no-open、--url 不做单实例检查）
    就整个不补。只补这个功能上线之后产生的文件（D16），任何错误都不拦启动。"""
    import time

    try:
        from app import audit_recover
        from app.audit import LOG_DIR
        from app.settings import load_settings, save_setting
        report = audit_recover.recover_at_startup(
            LOG_DIR, load_settings, save_setting, time.time(),
            others_running=lambda: _existing_instance_url(port) is not None)
    except Exception as exc:
        print("[警告] 检查上次没写结尾的审计时出错（{}），这次不补".format(type(exc).__name__))
        return
    if report.recovered:
        print("[信息] {} 份审计上次没写结尾，已在末尾补一条 session_end（reason=unclean_exit）：{}".format(
            len(report.recovered), "、".join(report.recovered)))
    if report.errors:
        print("[警告] {} 份审计没能补写结尾：{}".format(
            len(report.errors), "、".join("{}（{}）".format(n, e) for n, e in report.errors)))


def _check_update_guard(others_running=None):
    """一键更新之后的存活确认（专家审查 I6，规则见 app/update_guard.py）：新版本启动了两次、字幕界面都没
    连上，第三次打开就退回更新前的版本，弹窗说明（Mac、Windows 都弹），然后重新 exec。

    只在确认没有别的实例在跑之后调（计划审查 P5：在单实例检查之前计数，双击两次就会把一个好好的新版本
    退回去）：窗口模式在 run_with_window 的检查之后，浏览器模式在 main() 的检查之后；--no-open、--url
    不做单实例检查，由 others_running 探一次端口。任何出错都不拦启动。"""
    try:
        from app import update_guard
        outcome = update_guard.on_boot(ROOT, others_running=others_running)
    except Exception as exc:
        print("[警告] 一键更新后的启动检查出错（{}），这次跳过".format(type(exc).__name__))
        return
    if not outcome.rolled_back:
        return
    # 对话框不等人点：旧版本马上起来，暂停中的监听才赶得上「重启后接着监听」的时限。程序文件已经退回了：
    # 弹窗这一步出错（比如终端断了、print 抛 OSError）也不能挡住下面的重新 exec。
    # _info_dialog 先打印、Mac 上没终端时弹框（不变）；Windows 另外用系统自带的消息框弹（用户 2026-10-26 定），
    # 弹不出来就只剩那一行打印。两步各自兜底：打印抛了错，Windows 的消息框照样弹
    try:
        if outcome.message:
            _info_dialog(outcome.message)
    except Exception:
        pass
    try:
        if outcome.message:
            update_guard.windows_notice(outcome.message)
    except Exception:
        pass
    update_guard.release_lock()
    from app.relaunch import exec_args, flush_std, replace_process
    argv = [sys.executable, str(ROOT / "main.py")] + sys.argv[1:]
    flush_std()
    try:
        if os.name == "nt":
            replace_process(argv)          # 同 ensure_env：Windows 上起子进程、等它（I11）
        os.execv(sys.executable, exec_args(argv))
    except Exception as exc:
        # 程序文件已经退回了，内存里却还是新版本的代码：不能接着往下跑。说清楚、退出；再开一次就是旧版本
        print("[错误] 已退回更新前的版本，但重新启动没成功（{}）".format(type(exc).__name__))
        _fail_alert(L("已退回更新前的版本，但程序没能自己重新打开（{}）。请再打开一次本程序。",
                      "The app went back to the version before the update but couldn’t reopen itself "
                      "({}). Open the app again.").format(type(exc).__name__))
        raise SystemExit(1) from None


def _note_exit_signal(name):
    """第一次收到退出信号：SIGHUP（终端窗口关了）先把标准流换成 /dev/null——之后再往那个终端写就是
    EIO，收尾路上任何一行 print 都会抛（app/exit_signals.silence_stdio）。"""
    from app import exit_signals
    if name == "SIGHUP":
        exit_signals.silence_stdio()
    _say("[信息] 收到 {}：先停止监听、写好这一场的审计结尾，然后退出".format(name))


def _exit_again(name):
    """收尾途中又收到退出信号（比如又按了一次 Ctrl-C）：不再等停管线，只等一下在途的证据写盘（最多 2 秒，
    同 main.py 里别的 os._exit），然后退出。审计没写完的那一场，下次启动补一条结尾（app/audit_recover.py）。
    窗口模式下由 WakeupBridge 的读线程调：第一次的收尾在另一个线程里跑，读线程收尾途中照样读得到这一个。
    同一次退出的回声（关终端窗口时 zsh 下的第二个 SIGHUP、同名信号 0.3 秒内又来一个）不走这里，走
    _echo_exit_signal（app/exit_signals.Dispatch，第 2 轮审查）。"""
    from app.window_close import flush_evidence
    _say("[警告] 又收到 {}：不再等收尾，直接退出".format(name))
    flush_evidence()
    os._exit(1)


def _echo_exit_signal(name):
    """同一次退出的回声（app/exit_signals.Dispatch）：第一次的收尾照常走，不打印。是 SIGHUP 就把标准流换成
    /dev/null：第一次是别的信号（比如先按了 Ctrl-C）、收尾途中又关了终端窗口时，之后往那个终端写就是 EIO，
    和 _note_exit_signal 同一个道理。"""
    if name == "SIGHUP":
        from app import exit_signals
        exit_signals.silence_stdio()


def _exit_stuck():
    """浏览器和命令行模式：收到退出信号 LOOP_DEADLINE_SEC 秒了进程还在（收尾没走完，或者根本没开始）——看门狗
    线程调（app/exit_signals.Watchdog）。只等一下在途的证据写盘，然后退出；这一场没写完的审计结尾下次启动补。"""
    from app.exit_signals import LOOP_DEADLINE_SEC
    from app.window_close import flush_evidence
    _say("[警告] 收到退出信号 {:g} 秒了还没退出：不再等收尾，直接退出（没写完的审计结尾，下次启动补）".format(
        LOOP_DEADLINE_SEC))
    flush_evidence()
    os._exit(1)


async def _shutdown_on_signal(pipeline, name):
    """浏览器和命令行模式收到退出信号：停管线、写 session_end（reason=signal，带信号名）、等证据写盘，
    然后退出。和关窗口同一套（window_close.close_session，有时限）；不回到 asyncio.run 的收尾——它要等
    默认线程池里的模型加载线程（首次下载时要好几分钟）。"""
    from app.window_close import close_session, flush_evidence
    _note_exit_signal(name)
    try:
        await close_session(pipeline, reason="signal", fields={"signal": name})
    except Exception as exc:
        _say("[警告] 收尾时出错: {}".format(type(exc).__name__))
    flush_evidence()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(0)


async def main_async(args, state=None):
    from app.pipeline import Pipeline
    from app.server import CaptionServer
    from app.updater import Updater, local_version

    # 上一次没写结尾的审计补一条（确认没有别的实例在跑之后、开第一场之前）
    _recover_unclosed_audits(args.port)
    # 端口被占用（常见于别的软件恰好占了 8765）时自动向后找空闲端口，
    # 不让用户接触 --port 这种命令行概念
    server = None
    last_exc = None
    for port in range(args.port, args.port + 10):
        candidate = CaptionServer(port=port)
        try:
            await candidate.start()
        except OSError as exc:
            last_exc = exc
            continue
        server = candidate
        if port != args.port:
            print("[信息] 端口 {} 被占用，已自动改用 {}".format(args.port, port))
        args.port = port
        break
    if server is not None and args.no_open:
        # --no-open 不开页面，可能一直没有页面来连：一键更新后的启动检查（app/update_guard.py）在这种
        # 模式下以「控制服务起来了」为准，否则这样启动两次之后，第三次打开会把一个好好的新版本退回去
        from app import update_guard
        update_guard.mark_ready()
    if server is None:
        if state is not None:
            state["failed"] = True   # 告诉窗口线程别开窗口了
        _fail_alert(L("无法启动本地服务：端口 {}–{} 全部被占用（{}）。\n"
                      "请关闭占用这些端口的程序后重新打开。",
                      "Couldn’t start the local service: ports {}–{} are all in use ({}).\n"
                      "Quit the apps using these ports, then open the app again.").format(
                        args.port, args.port + 9, last_exc))
        sys.exit(1)
    server.config["version"] = local_version()
    # 界面语言的几个字段（设置行、页面重载用）。闸关着、没有一次性覆盖时一个键都不加：
    # hello 里的 config 与改造前逐字节相同
    if i18n.enabled():
        server.config.update(i18n.config_info())
    try:
        pipeline = Pipeline(args, server)
    except RuntimeError as exc:   # 例如缺少翻译引擎的 API Key
        if state is not None:
            state["failed"] = True
        _fail_alert(of(exc))
        sys.exit(1)
    updater = Updater(server)
    pipeline.updater = updater
    # 后台装组件要知道此刻有没有在监听；组件升级要写进当前这场的审计
    updater.attach_pipeline(pipeline)
    server.on_control = pipeline.handle_control
    server.on_client_dropped = pipeline.on_ui_client_dropped
    # 手机同看：上次中控开着就接着开（默认关闭）。放在这里而不是更后面，是因为
    # 它要在界面第一次拿 hello 之前把 config["viewer"] 填好——否则卡片会先闪一下
    # 「关闭」再跳成「已打开」。restore 自己吞掉所有异常，不会挡住启动
    await pipeline.restore_viewer_share()
    # 顶栏「闪购」：macOS 上装了闪购助手才往 config 里写 flash_sale、起轮询（只读 127.0.0.1:9334/status）。
    # 同样要赶在第一次 hello 之前，按钮才不会晚一拍冒出来。自己吞掉所有异常，不会挡住启动。
    # 演示模式不接：--demo 是拿来截图、录屏验界面的，9334 上是真实店铺的真实商品，不能进截图
    if not args.demo:
        await pipeline.attach_flash_sale()
    if state is not None:
        state["loop"] = asyncio.get_running_loop()
        state["pipeline"] = pipeline
        state["ready_port"] = args.port   # 窗口线程以此为准（端口可能已自动切换）
    await pipeline._publish_watchlist()   # 首页要立刻显示违禁词表状态
    # 引擎状态也要在启动时就进 config：以前只在开播/手动切换时发布，
    # 于是重启后面板显示的是 HTML 里的默认「自动」，恢复的引擎和
    # 「缺密钥已回退」的提示都要等到开播才看得见——恢复了但看不见，
    # 等于没恢复
    await pipeline._publish_engine()
    # 自检放后台跑：有几项要实际执行探测（ffmpeg、Ollama），
    # 不能让它们拖慢界面打开
    pipeline._selfcheck_task = asyncio.ensure_future(pipeline.run_selfcheck())
    # 本地翻译的自动就绪也放后台：下载可能要几分钟，不能挡住界面
    pipeline._provision_task = asyncio.ensure_future(
        pipeline.ensure_local_translator())
    # 弹幕组件（TikTokLive）同理放后台装：装不上也不耽误开播，弹幕来源会
    # 自己退化成「不可用」（见 CommentSource._supervise）
    # 已经装了但版本过旧的也在这里升（见 Pipeline.provision_comments）
    pipeline._comments_provision_task = asyncio.ensure_future(
        pipeline.provision_comments())
    update_watch = asyncio.ensure_future(updater.watch())  # noqa: F841
    url = f"http://127.0.0.1:{args.port}"
    print(f"字幕界面已启动: {url}")
    if state is None and not args.no_open:
        webbrowser.open(url)
        _close_launch_dialog()

    if args.demo:
        await pipeline.start_demo()
    elif args.url:
        await pipeline.start_stream(args.url)
    elif await pipeline.resume_after_update():
        # 一键更新前正在监听、几分钟内重启回来了：接着听那个房间（记号读到即删）
        print("[信息] 一键更新前正在监听，已自动恢复")
    else:
        await server.status("idle", L("在页面里输入直播间地址开始翻译", "Enter a live link to start."))
        print("未指定直播间地址——在打开的网页里输入地址点「开始翻译」即可。")

    # 浏览器和命令行模式：关终端窗口（SIGHUP）、kill（SIGTERM）、Ctrl-C 先把这一场的审计收尾再退出
    # （专家审查 B5，app/exit_signals.py）：信号到了取消这个任务，下面接住后走 _shutdown_on_signal。
    # 窗口模式在 run_with_window 里接（主线程是 pywebview 的界面循环，这个事件循环在后台线程）
    got_signal = {}
    if state is None:
        from app import exit_signals
        main_task = asyncio.current_task()

        def on_signal(name):
            got_signal["name"] = name
            main_task.cancel()

        # 看门狗：事件循环被卡住、取消主任务这一步跑不到时，收到信号 LOOP_DEADLINE_SEC 秒后照样退出（第 1 轮审查）
        watchdog = exit_signals.Watchdog(exit_signals.LOOP_DEADLINE_SEC, _exit_stuck)
        watchdog.start()
        exit_signals.install_loop_handlers(asyncio.get_running_loop(),
                                           exit_signals.Dispatch(on_signal, _exit_again, on_echo=_echo_exit_signal),
                                           watchdog=watchdog)
    # 直播结束后保留 UI（可以继续翻看历史字幕 / 换房间），Ctrl-C 退出
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        if not got_signal:
            raise
        await _shutdown_on_signal(pipeline, got_signal["name"])     # 不返回：os._exit
    finally:
        # 退出时把手机同看的监听面收掉：0.0.0.0 上的端口不该比进程活得久。
        # 窗口模式走 app/window_close.close_session，这里管 CLI / --browser。
        # 尽力而为——任何失败都不许拖住退出
        try:
            await pipeline.stop_viewer_share("shutdown")
        except Exception as exc:
            print("[警告] 收尾时关闭手机同看出错: {}".format(exc))
        pipeline.stop_flash_watch()      # 闪购助手的状态轮询：可重入，绝不抛


def main():
    # macOS：先住进 .app 再往下走——系统按 Info.plist 登记名字和图标，Dock、
    # 菜单栏、悬停名原生正确（见 app/macbundle.py）。execve 成功就不会返回。
    if sys.platform == "darwin":
        from app.macbundle import relaunch_inside_bundle
        relaunch_inside_bundle(ROOT)
    # 推迟安装的记号（I7）到最终进程为止：不传给一键更新的重启和以后起的子进程
    from app.bootstrap import clear_install_deferred, launch_mark
    clear_install_deferred()
    # 上一跳交过来的「正在启动」提示：开好窗口时关（run_with_window）；走不到那一步（浏览器模式、出错、
    # 直接退出）也在进程退出时关掉，不留一个挂着的「正在启动」
    # （双击两次、浏览器模式下打开已有实例那条路，main() 直接 return，就靠这一句关）
    import atexit
    atexit.register(_close_launch_dialog)
    launch_mark("最终进程开始（{}）".format(sys.executable))  # i18n: terminal
    # 到这里一定是最终进程了：本进程第一次读设置（损坏就在这里备份，界面提示和审计都拿得到），
    # 闸开着、设置里还没有 ui_lang 时迁移写一次，然后定下界面语言
    i18n.settle(ROOT)
    args = parse_args()
    if args.target is None:   # 未显式传参：用界面里上次选的语言，都没有则简体中文
        saved = _load_settings().get("target_lang")
        args.target = (str(saved)[:12] if saved else "zh-CN")
    # 翻译引擎同理：界面里选过的引擎重启后必须还在。以前只存不读，
    # 每次重启都静默回到 auto——用户以为在用 DeepL，实际跑的是本地 1.8B
    args.translator, warn = restore_engine(
        args.translator, _load_settings().get("translator"))
    if warn:
        print("[警告] " + warn)
    # 只 print 不够：窗口应用没有可见终端，回退提示必须能到界面上。
    # 挂在 args 上，_publish_engine 会把它带进引擎面板
    args.translator_note = warn
    # 主播语言：CLI 显式指定 > 界面上次的选择 > 西语（产品面向西语带货直播，
    # 首启不该逐段猜语言——为什么，见 resolve_source 的说明）。
    # 「请求了什么」在决策发生的这一刻记下来，供审计并排记录 requested 与
    # active。事后重算是错的：settings 可能被之后的操作改写，重算会伪造出
    # 「请求 A 实际跑 B」的假警报——这个字段存在的意义恰恰是抓真警报
    from app.settings import resolve_source
    saved_source = _load_settings().get("source_lang")
    args.source_requested = str(args.source or saved_source or "default")
    args.source = resolve_source(args.source, saved_source)
    if args.doctor:
        from app.hwdetect import doctor
        sys.exit(doctor())
    if not args.demo:
        missing = []
        from app.ffmpeg_bin import find_ffmpeg
        if find_ffmpeg() is None:
            missing.append(L("组件 ffmpeg 缺失：请关闭程序后重新打开，会自动补装。"
                             "（进阶：pip install -r requirements.txt -c constraints.txt）",
                             "Audio (ffmpeg) is missing. Quit and reopen the app to install it "
                             "automatically. (Advanced: pip install -r requirements.txt -c "
                             "constraints.txt)"))
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            missing.append(L("组件 faster-whisper 缺失：请关闭程序后重新打开，会自动补装。"
                             "（进阶：pip install -r requirements.txt -c constraints.txt "
                             "或运行 setup 脚本）",
                             "faster-whisper is missing. Quit and reopen the app to install it "
                             "automatically. (Advanced: pip install -r requirements.txt -c "
                             "constraints.txt, or run the setup script.)"))
        if missing:
            if args.url:
                _fail_alert(L("\n", "\n").join(missing))
                sys.exit(1)
            for m in missing:
                print("⚠️ " + m)
    use_window = not args.browser and not args.no_open
    if use_window:
        try:
            import webview  # noqa: F401
        except ImportError:
            use_window = False
    try:
        if use_window:
            run_with_window(args)
        else:
            # 浏览器模式下双击两次：同样复用已有实例（窗口模式在 run_with_window 里处理）
            if not args.no_open and args.url is None:
                existing = _existing_instance_url(args.port)
                if existing:
                    print("[信息] 检测到程序已在运行，直接打开现有界面 " + existing.url)
                    # 不是这个文件夹里的这一份：照旧打开它，但说一声（同 run_with_window）
                    from app.instance import other_install_text
                    note = other_install_text(existing)
                    if note is not None:
                        _info_dialog(note)
                    webbrowser.open(existing.url)
                    return
                _check_update_guard()
            else:
                # --no-open、--url 不做单实例检查：一键更新后的启动检查自己探一次端口
                _check_update_guard(lambda: _existing_instance_url(args.port) is not None)
            asyncio.run(main_async(args))
    except KeyboardInterrupt:
        # 识别证据在每一场自己的证据线程里写：asyncio.run 收尾只等默认线程池，下面的 os._exit
        # 谁都不等。还没写完的 wav 和索引行最多再等 2 秒（app/window_close.flush_evidence）
        from app.window_close import flush_evidence
        flush_evidence()
        print("\n已退出。")
        # 模型加载跑在默认执行器的非 daemon 线程上（且被 shield 刻意保活），
        # 正常退出要 join 它——首次下载时按 Ctrl-C 会静默卡住好几分钟。
        # 与窗口模式（run_with_window 末尾）一致，直接硬退。
        sys.stdout.flush()
        os._exit(0)


_SIGNAL_BRIDGE = []


def _install_window_signals(finish):
    """窗口模式的退出信号（app/exit_signals.py）：Ctrl-C 不让 pywebview 交给 PyObjC 的 mach 中断（那条路直接
    exit()），三个信号都经 WakeupBridge 送到后台线程，由 finish.finish_signal 收尾、退出。主线程上调。"""
    from app import exit_signals

    def on_first(name):
        _note_exit_signal(name)
        finish.finish_signal(name)

    bridge = exit_signals.WakeupBridge(exit_signals.Dispatch(on_first, _exit_again, on_echo=_echo_exit_signal))
    if bridge.install():
        _SIGNAL_BRIDGE.append(bridge)
        # 桥装上了才把 PyObjC 的 mach 中断换掉：装不上时换掉它，Ctrl-C 在 Cocoa 主循环里就彻底没反应了
        # （Python 层的处理函数在那里不执行），不换至少还能经 NSApp terminate: 退出（第 1 轮审查）
        exit_signals.keep_sigint_from_cocoa()
    else:
        print("[警告] 没能接住退出信号：关终端窗口、kill 时这一场的审计不会写结尾（下次启动补一条）")


def run_with_window(args):
    """桌面应用模式：后台线程跑服务，主线程开原生窗口（pywebview 要求主线程）。"""
    import threading
    import time

    import webview

    # 已有实例在跑（比如双击了两次）？直接把窗口开到现有实例上，不再起后端
    existing = _existing_instance_url(args.port)
    if existing:
        print("[信息] 检测到程序已在运行，打开已有实例的窗口")
        # 端口上那一份不是这个文件夹里的（新下了一版、旧的还开着；或者开着另一个副本）：照旧打开它，
        # 但说清楚打开的是哪一份，不然看起来像「界面自己变了」（app/instance.py other_install_text）。
        # 和下面 --url 那句并进同一个框：_info_dialog 同一时刻只管一个
        from app.instance import other_install_text
        notes = []
        other = other_install_text(existing)
        if other is not None:
            notes.append(other)
        if args.url:
            # 地址没有送进那个实例：明说，别让用户以为已经在监听
            notes.append(L("程序已经在运行，这次给的直播间地址没有自动开始。\n"
                           "请在已打开的窗口里粘贴地址后点「开始翻译」。",
                           "The app is already running, so this live link didn’t start "
                           "automatically.\nPaste the link in the open window, then click Start."))
        if notes:
            _info_dialog(L("\n\n", "\n\n").join(notes))
        from app.macbrand import brand_mac_app
        from app.window_attention import expose_attention
        from app.window_close import confirm_localization_kwargs
        from app.window_lang import expose_window_lang
        brand_mac_app(ROOT)
        lang = i18n.current()
        # 这个窗口不 guard_close（后端在另一个进程里），localization 只给页面里 JS confirm()
        # 的按钮用。今天没传、用 pywebview 自带的文字；关窗表里有了 global.ok（M11）才传，与闸无关
        loc = confirm_localization_kwargs(webview.create_window, lang)
        window = webview.create_window(i18n.text(APP_NAME, lang), existing.url,
                                       width=1000, height=760, min_size=(420, 480), **loc)
        _close_launch_dialog()
        # 语言切换发生在另一个进程里，这个进程收不到：页面重载后经 JS 桥告诉窗口。
        # 建窗时没传 localization 的，切换时也不往里写（app/window_lang.py）
        expose_window_lang(window, expose_attention(window), localized=bool(loc))
        webview.start()
        return

    # 确认没有别的实例在跑了：一键更新后的启动检查（两次启动页面都没连上，第三次就退回，见 _check_update_guard）
    _check_update_guard()
    state = {}
    from app.window_close import (FinishOnce, guard_close, hook_app_terminate, localization_kwargs,
                                  stop_after_close)
    # 关窗之后的收尾只走一遍：start() 返回之后的主线程、浮窗的 10 秒兜底、⌘Q、退出信号，谁先到谁做
    # （app/window_close.py）。退出信号那条路给 finish 传 reason="signal" 和信号名
    finish = FinishOnce(lambda **kw: stop_after_close(state.get("loop"), state.get("pipeline"), **kw))
    # 关终端窗口（SIGHUP）、kill（SIGTERM）、Ctrl-C：主线程马上要进 pywebview 的界面循环，Python 层的信号处理
    # 在那里不执行（2026-10-23 真 pywebview 实测），所以经 set_wakeup_fd 交给一个后台线程收尾（专家审查 B5）。
    # 要赶在起后台服务之前装：启动途中收到的也走收尾（那时还没有管线，直接退出）
    _install_window_signals(finish)

    def backend():
        # 两处 os._exit(1) 之前都先 flush_evidence：后台服务半路异常退出时，这一场的证据线程里可能还有
        # 没写完的（启动失败时一份都没有，立刻返回）。见 app/window_close.flush_evidence
        from app.window_close import flush_evidence
        try:
            asyncio.run(main_async(args, state))
        except SystemExit as exc:
            print(str(exc))
            flush_evidence()
            os._exit(1)
        except Exception as exc:
            if isinstance(exc, ImportError):
                _forget_install_failure()      # 缺模块：下次启动立刻重试安装
            _fail_alert(L("后台服务异常退出：{}", "The background service quit unexpectedly: {}")
                        .format(of(exc)))
            flush_evidence()
            os._exit(1)

    thread = threading.Thread(target=backend, daemon=True)
    thread.start()

    # 等后端真正就绪。不能用「端口能连上」判断：8765 可能被别的软件占着
    # （后端会自动换端口），连上的未必是我们自己的服务。
    deadline = time.time() + 60
    while time.time() < deadline and thread.is_alive():
        if state.get("ready_port") or state.get("failed"):
            break
        time.sleep(0.3)

    if not state.get("ready_port"):
        # 后端没起来（失败对话框已经弹了/正在弹）：不开窗口，等它自己退出
        thread.join()
        return

    from app.bootstrap import end_launch, launch_mark
    launch_mark("后端就绪（端口 {}）".format(state["ready_port"]))  # i18n: terminal
    url = "http://127.0.0.1:{}".format(state["ready_port"])
    from app.macbrand import brand_mac_app
    from app.window_attention import expose_attention
    from app.window_lang import expose_window_lang
    brand_mac_app(ROOT)
    # 浮窗（只显示，见 app/float_window.py）：宿主挂到管线上，hello 里才会带 float_window，顶栏才画
    # 「浮窗」按钮。要在开主窗口之前挂：页面一加载就会读 hello。任何失败都只是没有这个功能
    float_host = None
    try:
        from app.float_window import FloatWindowHost, keep_website_data, screens_from_webview
        if state.get("pipeline") is not None:
            # keep_website_data：macOS 上开浮窗不再清掉共用的 WebKit 数据（主页面的 localStorage、
            # 流地址解析攒的 cookie），见 app/float_window.py
            float_host = FloatWindowHost(keep_website_data(webview), url,
                                         screens=screens_from_webview(webview))
            state["pipeline"].attach_float_host(float_host, state.get("loop"))
    except Exception as exc:
        float_host = None
        print("[警告] 浮窗不可用：{}".format(exc))
    try:
        # 标题和关窗确认框先用启动时的界面语言；页面加载好后经 JS 桥告诉窗口它的语言（运行中
        # 在设置里换了语言，页面会重载），标题与确认框再跟着换（app/window_lang.py）
        lang = i18n.current()
        # 正在监听时关窗口先确认；待机时直接关（判断和文案见 app/window_close.py）
        loc = localization_kwargs(webview.create_window, lang)
        window = webview.create_window(i18n.text(APP_NAME, lang), url,
                                       width=1000, height=760, min_size=(420, 480), **loc)
        launch_mark("窗口创建")  # i18n: terminal
        end_launch()
        _close_launch_dialog()    # 窗口马上就出来（webview.start 要等窗口关了才返回，所以在它之前关）
        guard_close(window, state.get("pipeline"))
        if float_host is not None:
            # 主窗口一关浮窗跟着关：否则 GUI 循环不退出，下面的收尾（停管线）不会执行。
            # 兜底：浮窗销毁了 10 秒、start() 还没返回，就在定时器线程里走同样的收尾后直接退出
            # （终端留一行警告；主线程已经在收尾就什么都不做）
            from app.float_window import follow_main_window
            follow_main_window(window, float_host, stuck=finish.finish_stuck)
            # 多块屏时第一次开浮窗靠它认出主窗口在哪块屏上（在开窗的工作线程里读，不碰事件循环）
            float_host.anchor = lambda: (window.x, window.width)
        # 窗口被盖住时新报警改原生标题：pywebview 的标题不跟页面的 document.title
        expose_window_lang(window, expose_attention(window))
        # ⌘Q、程序坞里的「退出」：NSApp terminate: 直接 exit()，start() 不返回（2026-10-23 真 pywebview 6.2.1
        # 实测），下面那句 finish.finish() 走不到。在 willTerminate 通知里（主线程、exit 之前）同步收尾
        hook_app_terminate(finish.finish)
        webview.start(**({} if loc else localization_kwargs(webview.start, lang)))
    except Exception as exc:
        # 本机没有可用的 webview 后端（如部分 Linux 桌面）——退回浏览器
        print("[信息] 无法创建应用窗口（{}），改在浏览器中打开".format(exc))
        if float_host is not None:
            state["pipeline"].detach_float_host()    # 浏览器里没有浮窗：别让页面画出按钮
        webbrowser.open(url)
        _close_launch_dialog()
        thread.join()
        return

    # 窗口被关闭：记下原因、停掉直播管线（终止 ffmpeg 子进程）后退出。
    # 停得慢时先让审计收尾，session_end 不能等进程退出。浮窗的兜底已经在收尾的话，在这里等它退出进程
    finish.finish()


if __name__ == "__main__":
    main()
