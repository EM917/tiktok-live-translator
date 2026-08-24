#!/usr/bin/env python3
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

ROOT = Path(__file__).resolve().parent

if sys.version_info < (3, 9):
    sys.exit("需要 Python 3.9 或更高版本（当前 {}.{}）".format(*sys.version_info[:2]))


def _deps_ok():
    try:
        import aiohttp  # noqa: F401
        import faster_whisper  # noqa: F401
        import numpy  # noqa: F401
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    # ffmpeg 也是硬依赖：不查它的话，安装中途断掉会让「重开自动补装」的承诺落空
    try:
        import imageio_ffmpeg  # noqa: F401
        return True
    except ImportError:
        import shutil
        return shutil.which("ffmpeg") is not None


def _venv_python():
    sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return ROOT / ".venv" / sub


def _venv_usable(vpy):
    """光看路径存在不够——上次安装被中断会留下残缺的 venv，必须实际跑一下确认。"""
    if not vpy.exists():
        return False
    try:
        return subprocess.run([str(vpy), "-c", "import sys"],
                              capture_output=True, timeout=30).returncode == 0
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


def _esc_osa(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


_INFO_DIALOG = None


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
            ["osascript", "-e",
             'display dialog "{}" with title "TikTok 直播同传" buttons {{"知道了"}} '
             'default button 1 with icon note giving up after 600'.format(_esc_osa(message))],
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


def _fail_alert(message):
    """报告致命问题：终端打印之外，无终端时必须弹系统对话框——否则用户只会看到
    程序无声消失。"""
    print("⚠️ " + message)
    if os.environ.get("CI") or _has_console():
        return
    try:
        if sys.platform == "darwin":
            _close_info_dialog()
            subprocess.run(
                ["osascript", "-e",
                 'display dialog "{}" with title "TikTok 直播同传" buttons {{"好"}} '
                 'default button 1 with icon caution'.format(_esc_osa(message))],
                capture_output=True, timeout=60)
        elif os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "TikTok 直播同传", 0x30)
    except Exception:
        pass


def _acquire_bootstrap_lock():
    """首次安装要几分钟且窗口模式下毫无提示，用户很容易再双击一次。
    用锁文件串行化：第二个进程等第一个装完，而不是并发写坏同一个 .venv。

    返回 (状态, 锁路径)：
      ("acquired", path) 拿到锁，可以安装，用完要删；
      ("ready", None)    等待期间别的实例已装好，直接用；
      ("busy", None)     等太久还没轮到——绝不能自己再跑一遍 pip；
      ("nolock", None)   建不了锁文件（只读目录等），退化为尽力而为。
    """
    import time

    lock = ROOT / ".venv.lock"
    deadline = time.time() + 900     # 最多等 15 分钟
    while time.time() < deadline:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return ("acquired", lock)
        except FileExistsError:
            try:                      # 陈旧锁（上次安装崩溃残留）超过 20 分钟就抢占
                if time.time() - lock.stat().st_mtime > 1200:
                    lock.unlink()
                    continue
            except OSError:
                continue
            print("[初始化] 另一个实例正在安装依赖，等待它完成…")
            time.sleep(3)
            if _deps_ok():
                return ("ready", None)
        except OSError:
            return ("nolock", None)
    return ("busy", None)


def ensure_env():
    """零手动安装：缺依赖时自动创建虚拟环境、装齐 requirements，然后换进新环境继续跑。"""
    if _deps_ok():
        return
    _info_dialog("首次运行：正在自动安装运行组件（约需 2–5 分钟，取决于网速）。\n"
                 "完成后字幕窗口会自动打开——请耐心等待，不要重复打开程序。")
    vpy = _venv_python()
    in_project_venv = Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()
    lock = None
    if not in_project_venv:
        state, lock = _acquire_bootstrap_lock()
        if state == "busy":
            _fail_alert("另一个实例仍在安装组件（已等待 15 分钟）。请等它装完后再打开本程序，"
                        "避免两边同时安装把环境写坏。")
            return
    try:
        if _deps_ok():        # 等锁期间别的实例已经装好了
            return
        if not in_project_venv and not _venv_usable(vpy):
            print("[初始化] 首次运行：正在创建虚拟环境（仅需一次，可能几分钟）…")
            import venv

            venv.create(ROOT / ".venv", with_pip=True, clear=True)
        pip_python = sys.executable if in_project_venv else str(vpy)
        check = subprocess.run(
            [pip_python, "-c",
             "import aiohttp, numpy, faster_whisper, yt_dlp, imageio_ffmpeg"],
            capture_output=True,
        )
        if check.returncode != 0:
            print("[初始化] 正在安装依赖（含内置 ffmpeg，需要几分钟，仅首次）…")
            full = subprocess.run(
                [pip_python, "-m", "pip", "install", "--disable-pip-version-check",
                 "-r", str(ROOT / "requirements.txt")],
            )
            if full.returncode != 0:
                # 个别可选包（如 mlx-whisper）在老 Python/老系统上可能没有轮子——
                # 退一步只装核心依赖，工具仍可用（识别走 faster-whisper CPU 后端）
                print("[初始化] 完整安装失败，改装核心依赖…")
                subprocess.run(
                    [pip_python, "-m", "pip", "install", "--disable-pip-version-check",
                     "aiohttp>=3.9", "numpy>=1.24", "faster-whisper>=1.0",
                     "yt-dlp", "imageio-ffmpeg>=0.5"],
                    check=True,
                )
        if not in_project_venv:
            if lock is not None:      # execv 不会执行 finally，先手动释放锁
                try:
                    lock.unlink()
                except OSError:
                    pass
                lock = None
            _close_info_dialog()      # 马上换进新环境开窗口，安装提示框可以收了
            os.execv(str(vpy), [str(vpy), str(ROOT / "main.py")] + sys.argv[1:])
    except Exception as exc:
        _fail_alert("自动安装未完成（{}）。\n"
                    "请检查网络连接，然后重新打开本程序——会自动从中断处继续安装。\n"
                    "（进阶：也可手动运行 setup.sh / setup.ps1，"
                    "或 pip install -r requirements.txt）".format(exc))
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass


if "--doctor" not in sys.argv:
    ensure_env()

try:
    from app.translator import TRANSLATOR_CHOICES  # noqa: E402
except ImportError as exc:
    _fail_alert("组件尚未安装完成，程序暂时无法启动（{}）。\n"
                "请检查网络后重新打开本程序，会自动继续安装。".format(exc))
    sys.exit(1)


def _load_settings():
    """settings.json：界面里改过的偏好（如目标语言）跨重启保留。"""
    from app.settings import load_settings
    return load_settings()


def parse_args():
    p = argparse.ArgumentParser(
        prog="tiktok-live-translator",
        description="监听 TikTok 直播间，把主播的语音实时转写并翻译成字幕（全部本地运行，浏览器 UI 展示）。",
    )
    p.add_argument("url", nargs="?", default=None,
                   help="直播间地址（可选——不填则启动后在网页里输入），"
                        "例如 https://www.tiktok.com/@user/live；也可以直接给 .flv/.m3u8 流地址")
    p.add_argument("--target", default=None,
                   help="目标语言代码，默认 zh-CN（简体中文）；界面里改过的话记住上次的选择")
    p.add_argument("--source", default=None, help="主播语言代码（默认自动检测），例如 en/ja/ko")
    p.add_argument("--model", default=None,
                   help="whisper 模型：tiny/base/small/medium/large-v3/large-v3-turbo。"
                        "默认：mlx 后端用 large-v3（GPU 跑得动最准的），ct2 后端用 large-v3-turbo")
    p.add_argument("--backend", default="auto", choices=["auto", "mlx", "ct2"],
                   help="识别后端：mlx（Apple GPU，Mac 推荐）/ ct2（faster-whisper，CPU/CUDA）。"
                        "auto=装了 mlx-whisper 就用 mlx")
    p.add_argument("--beam", type=int, default=5,
                   help="beam search 宽度，越大越准越慢，默认 5（设 1 即贪心解码）")
    p.add_argument("--asr-temperature", type=float, default=None, dest="asr_temperature",
                   help="识别解码温度，默认 0（只解码一次）。Whisper 默认会在质量不达标时"
                        "用更高温度重解码最多 6 次，音乐段上实测能让单次识别涨到 25 秒")
    p.add_argument("--context", action="store_true",
                   help="开启滚动上下文（把上一段识别结果喂给下一段）。默认关闭："
                        "实测它会诱发复读死循环，反而大幅拉低召回率")
    p.add_argument("--device", default="auto", help="识别设备，默认 auto（Mac 上即 CPU）")
    p.add_argument("--compute-type", default="auto", dest="compute_type",
                   help="faster-whisper compute_type，默认 auto；CPU 上想更快可用 int8")
    p.add_argument("--translator", default="auto", choices=TRANSLATOR_CHOICES,
                   help="翻译引擎：auto=本地有 TranslateGemma 就用它，否则 google（默认）"
                        "/ gemma / google / claude / openai / none")
    p.add_argument("--port", type=int, default=8765, help="本地 UI 端口，默认 8765")
    p.add_argument("--denoise", choices=["auto", "on", "off"], default="auto",
                   help="RNNoise 人声降噪，抑制背景音乐/噪声（auto=模型文件存在即开启，默认）")
    p.add_argument("--cookies-browser", default="auto", dest="cookies_browser",
                   help="匿名解析失败时借用哪个浏览器的 TikTok 登录状态："
                        "auto（默认，依次尝试并记住有效的那个）/ chrome / safari / "
                        "firefox / edge / none（完全不读浏览器 cookie）")
    p.add_argument("--cookies", default=None,
                   help="可选：传给 yt-dlp 的 cookies.txt 路径（地区受限的直播间可能需要）")
    p.add_argument("--glossary", default=None,
                   help="领域词表路径，默认项目目录下的 glossary.txt"
                        "（首次运行会从 glossary.example.txt 生成）")
    p.add_argument("--banned-terms", default=None, dest="banned_terms",
                   help="违禁词表路径，默认项目目录下的 banned_terms.txt"
                        "（首次运行会从 banned_terms.example.txt 生成）")
    p.add_argument("--demo", action="store_true",
                   help="演示模式：不连直播，用内置台词驱动 UI（用来验证界面和浏览器插件）")
    p.add_argument("--doctor", action="store_true",
                   help="环境体检：检测本机硬件并打印推荐配置，不启动服务")
    p.add_argument("--browser", action="store_true",
                   help="在浏览器里打开界面（默认在独立应用窗口中打开）")
    p.add_argument("--no-open", action="store_true", help="启动后不要自动打开浏览器/窗口")
    return p.parse_args()


def _existing_instance_url(base_port):
    """本程序是否已在运行。是则返回其 UI 地址，否则 None。
    用于双击两次的场景：复用已有实例，而不是再起一个后端。
    必须扫完整个自动回退区间（base_port 起 10 个端口）——已有实例可能因
    base_port 被第三方占用而跑在后面的端口上。先用 TCP 探测过滤（本机关闭
    端口瞬时拒绝），只对开着的端口发 HTTP 验指纹，扫描耗时可忽略。"""
    import socket
    import urllib.request
    for port in range(base_port, base_port + 10):
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                continue
        url = "http://127.0.0.1:{}".format(port)
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if "直播同传" in resp.read(4096).decode(errors="replace"):
                    return url
        except Exception:
            pass
    return None


async def main_async(args, state=None):
    from app.pipeline import Pipeline
    from app.server import CaptionServer
    from app.updater import Updater, local_version

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
    if server is None:
        if state is not None:
            state["failed"] = True   # 告诉窗口线程别开窗口了
        _fail_alert("无法启动本地服务：端口 {}–{} 全部被占用（{}）。\n"
                    "请关闭占用这些端口的程序后重新打开。".format(
                        args.port, args.port + 9, last_exc))
        sys.exit(1)
    server.config["version"] = local_version()
    try:
        pipeline = Pipeline(args, server)
    except RuntimeError as exc:   # 例如缺少翻译引擎的 API Key
        if state is not None:
            state["failed"] = True
        _fail_alert(str(exc))
        sys.exit(1)
    updater = Updater(server)
    pipeline.updater = updater
    server.on_control = pipeline.handle_control
    if state is not None:
        state["loop"] = asyncio.get_running_loop()
        state["pipeline"] = pipeline
        state["ready_port"] = args.port   # 窗口线程以此为准（端口可能已自动切换）
    await pipeline._publish_watchlist()   # 首页要立刻显示违禁词表状态
    update_watch = asyncio.ensure_future(updater.watch())  # noqa: F841
    url = f"http://127.0.0.1:{args.port}"
    print(f"字幕界面已启动: {url}")
    if state is None and not args.no_open:
        webbrowser.open(url)

    if args.demo:
        await pipeline.start_demo()
    elif args.url:
        await pipeline.start_stream(args.url)
    else:
        await server.status("idle", "在页面里输入直播间地址开始翻译")
        print("未指定直播间地址——在打开的网页里输入地址点「开始翻译」即可。")

    # 直播结束后保留 UI（可以继续翻看历史字幕 / 换房间），Ctrl-C 退出
    await asyncio.Event().wait()


def main():
    args = parse_args()
    if args.target is None:   # 未显式传参：用界面里上次选的语言，都没有则简体中文
        saved = _load_settings().get("target_lang")
        args.target = (str(saved)[:12] if saved else "zh-CN")
    if args.doctor:
        from app.hwdetect import doctor
        sys.exit(doctor())
    if not args.demo:
        missing = []
        from app.ffmpeg_bin import find_ffmpeg
        if find_ffmpeg() is None:
            missing.append("组件 ffmpeg 缺失：请关闭程序后重新打开，会自动补装。"
                           "（进阶：pip install -r requirements.txt）")
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            missing.append("组件 faster-whisper 缺失：请关闭程序后重新打开，会自动补装。"
                           "（进阶：pip install -r requirements.txt 或运行 setup 脚本）")
        if missing:
            if args.url:
                _fail_alert("\n".join(missing))
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
                    print("[信息] 检测到程序已在运行，直接打开现有界面 " + existing)
                    webbrowser.open(existing)
                    return
            asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n已退出。")
        # 模型加载跑在默认执行器的非 daemon 线程上（且被 shield 刻意保活），
        # 正常退出要 join 它——首次下载时按 Ctrl-C 会静默卡住好几分钟。
        # 与窗口模式（run_with_window 末尾）一致，直接硬退。
        sys.stdout.flush()
        os._exit(0)


def run_with_window(args):
    """桌面应用模式：后台线程跑服务，主线程开原生窗口（pywebview 要求主线程）。"""
    import threading
    import time

    import webview

    # 已有实例在跑（比如双击了两次）？直接把窗口开到现有实例上，不再起后端
    existing = _existing_instance_url(args.port)
    if existing:
        print("[信息] 检测到程序已在运行，打开已有实例的窗口")
        webview.create_window("TikTok 直播同传", existing,
                              width=1000, height=760, min_size=(420, 480))
        webview.start()
        return

    state = {}

    def backend():
        try:
            asyncio.run(main_async(args, state))
        except SystemExit as exc:
            print(str(exc))
            os._exit(1)
        except Exception as exc:
            _fail_alert("后台服务异常退出：{}".format(exc))
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

    url = "http://127.0.0.1:{}".format(state["ready_port"])
    try:
        webview.create_window("TikTok 直播同传", url,
                              width=1000, height=760, min_size=(420, 480))
        webview.start()
    except Exception as exc:
        # 本机没有可用的 webview 后端（如部分 Linux 桌面）——退回浏览器
        print("[信息] 无法创建应用窗口（{}），改在浏览器中打开".format(exc))
        webbrowser.open(url)
        thread.join()
        return

    # 窗口被关闭：停掉直播管线（终止 ffmpeg 子进程）后退出
    loop = state.get("loop")
    pipeline = state.get("pipeline")
    if loop is not None and pipeline is not None:
        try:
            asyncio.run_coroutine_threadsafe(
                pipeline.stop_stream(quiet=True), loop
            ).result(timeout=5)
        except Exception:
            pass
    os._exit(0)


if __name__ == "__main__":
    main()
