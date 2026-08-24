"""启动自检：确认每项能力**真的在工作**，而不是「配置看起来对」。

这个模块的存在理由是一次真实事故：RNNoise 降噪从来没跑起来过。模型文件
存在、代码路径也走到了，但文件是残缺的（下载时短读），于是每次启动都
静默降级为「本次不降噪」——只在一行日志里说了句「下载失败或校验未过」，
而那行日志没人看。工具带着「已经在降噪」的假象跑了很久。

由此定下三条原则：

  1. **实测，不查配置**。降噪不是看文件在不在，而是真的用 0.1 秒静音跑一遍
     arnndn；翻译不是看有没有配引擎，而是真的 ping 一次。
  2. **静默降级必须变成可见状态**。每一项都有明确的 ok / warn / fail，
     并且推到界面上，而不是只 print。
  3. **区分「主动关掉」和「悄悄坏了」**。用户显式 --denoise off 是 ok；
     模型坏了导致降不了噪是 fail。两者绝不能显示成同一个样子。
"""
import asyncio
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK, WARN, FAIL = "ok", "warn", "fail"


def _check(name, level, detail, fix=""):
    return {"name": name, "level": level, "detail": detail, "fix": fix}


async def _to_thread(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


async def check_ffmpeg():
    from .ffmpeg_bin import ffmpeg_source, find_ffmpeg
    if find_ffmpeg() is None:
        return _check("音频组件 ffmpeg", FAIL, "找不到 ffmpeg，无法拉取直播音频",
                      "关闭程序后重新打开，会自动补装")
    return _check("音频组件 ffmpeg", OK, "可用（{}）".format(ffmpeg_source()))


async def check_denoise(args):
    """降噪是对抗背景音乐的第一道防线，必须实测能初始化。"""
    from .pipeline import DENOISE_MODEL, _arnndn_probe
    if getattr(args, "denoise", "auto") == "off":
        return _check("人声降噪", OK, "已按 --denoise off 主动关闭")
    if not DENOISE_MODEL.exists():
        return _check("人声降噪", WARN, "降噪模型尚未下载（首次开播时自动下载）")
    if await _to_thread(_arnndn_probe, str(DENOISE_MODEL)):
        size = DENOISE_MODEL.stat().st_size // 1024
        return _check("人声降噪", OK, "实测可用（模型 {} KB）".format(size))
    return _check("人声降噪", FAIL,
                  "模型存在，但实测跑不起来——背景音乐不会被抑制",
                  "删除 models/bd.rnnn 后重新开始，程序会重新下载")


async def check_asr(args):
    backend = getattr(args, "backend", "auto")
    try:
        from .hwdetect import recommend
        rec = recommend(backend=backend)
    except Exception as exc:
        return _check("语音识别", FAIL, "硬件探测失败：{}".format(exc))
    model = getattr(args, "model", None) or rec["model"]
    if rec["backend"] == "mlx":
        try:
            import mlx_whisper  # noqa: F401
        except ImportError:
            return _check("语音识别", FAIL, "mlx 后端不可用", "pip install mlx-whisper")
    else:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return _check("语音识别", FAIL, "faster-whisper 不可用",
                          "关闭程序后重新打开，会自动补装")
    cached = _model_cached(model)
    detail = "{} + {}（{}）".format(rec["backend"], model,
                                   "模型已下载" if cached else "首次开播需先下载模型")
    return _check("语音识别", OK if cached else WARN, detail)


def _model_cached(model):
    hf = os.environ.get("HF_HOME")
    hub = Path(hf) / "hub" if hf else Path.home() / ".cache" / "huggingface" / "hub"
    if not hub.exists():
        return False
    key = model.replace("large-v3-turbo", "large-v3-turbo").lower()
    for entry in hub.iterdir():
        if key in entry.name.lower():
            return True
    return False


async def check_translator(args):
    name = getattr(args, "translator", "auto")
    if name == "none":
        return _check("翻译引擎", OK, "已按 --translator none 主动关闭")
    try:
        from .translator import _ollama_has_gemma
    except Exception as exc:
        return _check("翻译引擎", FAIL, "翻译模块加载失败：{}".format(exc))
    resolved = name
    if name == "auto":
        resolved = "gemma" if await _to_thread(_ollama_has_gemma) else "google"
    if resolved == "gemma":
        if await _to_thread(_ollama_has_gemma):
            return _check("翻译引擎", OK, "本地 TranslateGemma（离线、无限流）")
        return _check("翻译引擎", FAIL, "指定了 gemma 但 Ollama 里找不到模型",
                      "启动 Ollama 并执行 ollama pull translategemma:4b")
    if resolved == "google":
        return _check("翻译引擎", WARN,
                      "Google 免费接口：会按 IP 限流，长时间监听容易整段翻译失败",
                      "改用 --translator gemma（本地、不限流）")
    key = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(resolved)
    if key and not os.environ.get(key):
        return _check("翻译引擎", FAIL, "{} 需要环境变量 {}".format(resolved, key))
    return _check("翻译引擎", OK, "{}（付费 API）".format(resolved))


async def check_watchlist(detector):
    """违禁词表为空 = 这个工具的核心功能没有生效。"""
    if detector is None or not detector.enabled:
        return _check("违禁词表", FAIL,
                      "词表为空——本工具不会发出任何违禁词报警",
                      "编辑 banned_terms.txt 后重新「开始翻译」")
    return _check("违禁词表", OK, "{} 条已生效".format(detector.count))


async def check_glossary(glossary):
    if glossary is None or not glossary.enabled:
        return _check("领域词表", WARN, "未配置——商品名/行话可能被译错",
                      "编辑 glossary.txt（可选，但能明显改善译文）")
    return _check("领域词表", OK, "{} 条已生效".format(len(glossary.entries)))


async def check_audit():
    from .audit import LOG_DIR
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        probe = LOG_DIR / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return _check("审计日志", OK, "可写入 logs/")
    except OSError as exc:
        return _check("审计日志", WARN,
                      "logs/ 不可写（{}）——漏报将无法事后追溯".format(exc))


async def check_resolver():
    """能不能拿到直播流地址。TikTok 对未登录请求会把在播房间报成未开播。"""
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return _check("直播流解析", FAIL, "缺少 yt-dlp",
                      "关闭程序后重新打开，会自动补装")
    from .resolver import _installed_browsers
    browsers = await _to_thread(_installed_browsers)
    if browsers:
        return _check("直播流解析", OK,
                      "yt-dlp 可用；匿名失败时会借用 {} 的登录状态"
                      .format(" / ".join(browsers)))
    return _check("直播流解析", WARN,
                  "yt-dlp 可用，但找不到可借用登录状态的浏览器——"
                  "TikTok 常把在播房间报成「未开播」",
                  "在 Chrome/Safari 里登录一次 TikTok")


async def check_disk():
    try:
        free = shutil.disk_usage(ROOT).free / 1024 ** 3
    except OSError:
        return _check("磁盘空间", WARN, "无法读取磁盘剩余空间")
    if free < 3:
        return _check("磁盘空间", FAIL,
                      "仅剩 {:.1f} GB——识别模型需要 1–3 GB".format(free))
    if free < 8:
        return _check("磁盘空间", WARN, "剩余 {:.1f} GB，偏紧".format(free))
    return _check("磁盘空间", OK, "剩余 {:.0f} GB".format(free))


async def run_all(args, detector=None, glossary=None):
    """跑完所有自检。任一项抛异常都不影响其余项——自检自己绝不能拖垮启动。"""
    tasks = [
        check_ffmpeg(), check_denoise(args), check_asr(args),
        check_translator(args), check_watchlist(detector),
        check_glossary(glossary), check_audit(), check_resolver(), check_disk(),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    checks = []
    for r in results:
        if isinstance(r, BaseException):
            checks.append(_check("自检项", WARN, "检查本身出错：{}".format(r)))
        else:
            checks.append(r)
    return checks


def summarize(checks):
    fails = sum(1 for c in checks if c["level"] == FAIL)
    warns = sum(1 for c in checks if c["level"] == WARN)
    return {"total": len(checks), "fail": fails, "warn": warns,
            "ok": len(checks) - fails - warns}
