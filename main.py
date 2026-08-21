#!/usr/bin/env python3
"""TikTok 直播同传 —— 本地实时字幕翻译工具入口。

用法示例：
    python main.py https://www.tiktok.com/@somebody/live          # 翻译成中文（默认）
    python main.py https://www.tiktok.com/@somebody/live --target ja
    python main.py --demo                                          # 演示模式，只看 UI
"""
import argparse
import asyncio
import shutil
import sys
import webbrowser

from app.translator import TRANSLATOR_CHOICES


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
    p.add_argument("--no-open", action="store_true", help="启动后不要自动打开浏览器")
    return p.parse_args()


async def main_async(args):
    from app.pipeline import Pipeline
    from app.server import CaptionServer

    server = CaptionServer(port=args.port)
    try:
        pipeline = Pipeline(args, server)
    except RuntimeError as exc:   # 例如缺少翻译引擎的 API Key
        sys.exit(str(exc))
    server.on_control = pipeline.handle_control
    await server.start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"字幕界面已启动: {url}")
    if not args.no_open:
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


def _ffmpeg_hint():
    if sys.platform == "darwin":
        return "brew install ffmpeg"
    if sys.platform.startswith("linux"):
        return "sudo apt install ffmpeg（Debian/Ubuntu，其他发行版用对应包管理器）"
    return "参考 https://ffmpeg.org/download.html"


def main():
    args = parse_args()
    if args.doctor:
        from app.hwdetect import doctor
        sys.exit(doctor())
    if not args.demo:
        missing = []
        if shutil.which("ffmpeg") is None:
            missing.append("未找到 ffmpeg，请安装：" + _ffmpeg_hint())
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
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
