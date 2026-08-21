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
        return True
    except ImportError:
        return False


def _venv_python():
    sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return ROOT / ".venv" / sub


def ensure_env():
    """零手动安装：缺依赖时自动创建虚拟环境、装齐 requirements，然后换进新环境继续跑。"""
    if _deps_ok():
        return
    vpy = _venv_python()
    in_project_venv = Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()
    try:
        if not in_project_venv and not vpy.exists():
            print("[初始化] 首次运行：正在创建虚拟环境（仅需一次）…")
            import venv

            venv.create(ROOT / ".venv", with_pip=True)
        pip_python = sys.executable if in_project_venv else str(vpy)
        check = subprocess.run(
            [pip_python, "-c", "import aiohttp, numpy, faster_whisper, yt_dlp"],
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
            os.execv(str(vpy), [str(vpy), str(ROOT / "main.py")] + sys.argv[1:])
    except Exception as exc:
        print("⚠️ 自动安装依赖失败（{}）。请手动执行 setup.sh / setup.ps1，"
              "或 pip install -r requirements.txt".format(exc))


if "--doctor" not in sys.argv:
    ensure_env()

from app.translator import TRANSLATOR_CHOICES  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(
        prog="tiktok-live-translator",
        description="监听 TikTok 直播间，把主播的语音实时转写并翻译成字幕（全部本地运行，浏览器 UI 展示）。",
    )
    p.add_argument("url", nargs="?", default=None,
                   help="直播间地址（可选——不填则启动后在网页里输入），"
                        "例如 https://www.tiktok.com/@user/live；也可以直接给 .flv/.m3u8 流地址")
    p.add_argument("--target", default="zh-CN", help="目标语言代码，默认 zh-CN（简体中文）")
    p.add_argument("--source", default=None, help="主播语言代码（默认自动检测），例如 en/ja/ko")
    p.add_argument("--model", default=None,
                   help="whisper 模型：tiny/base/small/medium/large-v3/large-v3-turbo。"
                        "默认：mlx 后端用 large-v3（GPU 跑得动最准的），ct2 后端用 large-v3-turbo")
    p.add_argument("--backend", default="auto", choices=["auto", "mlx", "ct2"],
                   help="识别后端：mlx（Apple GPU，Mac 推荐）/ ct2（faster-whisper，CPU/CUDA）。"
                        "auto=装了 mlx-whisper 就用 mlx")
    p.add_argument("--beam", type=int, default=5,
                   help="beam search 宽度，越大越准越慢，默认 5（设 1 即贪心解码）")
    p.add_argument("--no-context", action="store_true",
                   help="关闭滚动上下文（默认开启：把最近的识别结果喂给下一段提升连贯性）")
    p.add_argument("--device", default="auto", help="识别设备，默认 auto（Mac 上即 CPU）")
    p.add_argument("--compute-type", default="auto", dest="compute_type",
                   help="faster-whisper compute_type，默认 auto；CPU 上想更快可用 int8")
    p.add_argument("--translator", default="auto", choices=TRANSLATOR_CHOICES,
                   help="翻译引擎：auto=本地有 TranslateGemma 就用它，否则 google（默认）"
                        "/ gemma / google / claude / openai / none")
    p.add_argument("--port", type=int, default=8765, help="本地 UI 端口，默认 8765")
    p.add_argument("--denoise", choices=["auto", "on", "off"], default="auto",
                   help="RNNoise 人声降噪，抑制背景音乐/噪声（auto=模型文件存在即开启，默认）")
    p.add_argument("--cookies", default=None,
                   help="可选：传给 yt-dlp 的 cookies.txt 路径（地区受限的直播间可能需要）")
    p.add_argument("--demo", action="store_true",
                   help="演示模式：不连直播，用内置台词驱动 UI（用来验证界面和浏览器插件）")
    p.add_argument("--doctor", action="store_true",
                   help="环境体检：检测本机硬件并打印推荐配置，不启动服务")
    p.add_argument("--browser", action="store_true",
                   help="在浏览器里打开界面（默认在独立应用窗口中打开）")
    p.add_argument("--no-open", action="store_true", help="启动后不要自动打开浏览器/窗口")
    return p.parse_args()


async def main_async(args, state=None):
    from app.pipeline import Pipeline
    from app.server import CaptionServer
    from app.updater import Updater, local_version

    server = CaptionServer(port=args.port)
    server.config["version"] = local_version()
    try:
        pipeline = Pipeline(args, server)
    except RuntimeError as exc:   # 例如缺少翻译引擎的 API Key
        sys.exit(str(exc))
    updater = Updater(server)
    pipeline.updater = updater
    server.on_control = pipeline.handle_control
    await server.start()
    if state is not None:
        state["loop"] = asyncio.get_running_loop()
        state["pipeline"] = pipeline
    update_check = asyncio.ensure_future(updater.check_and_notify())  # noqa: F841
    url = f"http://127.0.0.1:{args.port}"
    print(f"字幕界面已启动: {url}")
    if state is None and not args.no_open:
        webbrowser.open(url)

    if args.demo:
        await pipeline.run_demo()
    elif args.url:
        await pipeline.start_stream(args.url)
    else:
        await server.status("idle", "在页面里输入直播间地址开始翻译")
        print("未指定直播间地址——在打开的网页里输入地址点「开始翻译」即可。")

    # 直播结束后保留 UI（可以继续翻看历史字幕 / 换房间），Ctrl-C 退出
    await asyncio.Event().wait()


def main():
    args = parse_args()
    if args.doctor:
        from app.hwdetect import doctor
        sys.exit(doctor())
    if not args.demo:
        missing = []
        from app.ffmpeg_bin import find_ffmpeg
        if find_ffmpeg() is None:
            missing.append("未找到 ffmpeg——请执行 pip install -r requirements.txt"
                           "（会自动带上内置版），或安装系统 ffmpeg")
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            missing.append("缺少依赖 faster-whisper，请执行：pip install -r requirements.txt"
                           "（或运行 setup.sh / setup.ps1）")
        if missing:
            if args.url:
                sys.exit("\n".join(missing))
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
            asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n已退出。")


def run_with_window(args):
    """桌面应用模式：后台线程跑服务，主线程开原生窗口（pywebview 要求主线程）。"""
    import socket
    import threading
    import time

    import webview

    state = {}

    def backend():
        try:
            asyncio.run(main_async(args, state))
        except SystemExit as exc:
            print(str(exc))
            os._exit(1)
        except Exception as exc:
            print("[错误] 后台服务异常退出: {}".format(exc))
            os._exit(1)

    thread = threading.Thread(target=backend, daemon=True)
    thread.start()

    deadline = time.time() + 60
    while time.time() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", args.port)) == 0:
                break
        time.sleep(0.3)

    url = "http://127.0.0.1:{}".format(args.port)
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
