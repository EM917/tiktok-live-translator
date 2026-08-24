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


def _importable(name):
    import importlib
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def _ffmpeg_runs(exe):
    """真的执行一次 ffmpeg -version。

    只解析出路径是不够的：macOS 上 imageio-ffmpeg 带的静态二进制会被 Gatekeeper
    隔离，下载不全或丢了可执行位也一样——文件在，一跑就失败。那种情况下如果
    这里报绿，锅会甩到降噪头上（降噪探测真的会跑 ffmpeg，于是它变红），
    用户按提示去删降噪模型，永远修不好。"""
    import subprocess
    try:
        r = subprocess.run([exe, "-version"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


async def check_ffmpeg():
    from .ffmpeg_bin import ffmpeg_source, find_ffmpeg
    exe = find_ffmpeg()
    if exe is None:
        return _check("音频组件 ffmpeg", FAIL, "找不到 ffmpeg，无法拉取直播音频",
                      "关闭程序后重新打开，会自动补装")
    if not await _to_thread(_ffmpeg_runs, exe):
        return _check("音频组件 ffmpeg", FAIL,
                      "找到了 ffmpeg 但跑不起来——直播音频拉不下来",
                      "macOS 若提示「无法验证开发者」，在系统设置→隐私与安全性里放行；"
                      "或用 brew install ffmpeg 装一个系统版")
    return _check("音频组件 ffmpeg", OK, "实测可用（{}）".format(ffmpeg_source()))


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
        # device 必须一起传：recommend 把 backend/model/device/compute_type 当成
        # 一组互相牵连的值重算。少传 device，自检算出的就不是待会儿真正要加载的
        # 那套配置——报「ct2 + large-v3」而管线加载的是别的，等于白检。
        rec = recommend(backend=backend, device=getattr(args, "device", "auto"))
    except Exception as exc:
        return _check("语音识别", FAIL, "硬件探测失败：{}".format(exc))
    model = getattr(args, "model", None) or rec["model"]
    # 这几个库第一次 import 要几百毫秒到一秒，放线程里做——run_selfcheck 特意
    # 挂在后台就是为了不挡住界面，在协程里同步 import 等于白挂
    if rec["backend"] == "mlx":
        if not await _to_thread(_importable, "mlx_whisper"):
            return _check("语音识别", FAIL, "苹果芯片加速组件未装上",
                          "关闭程序后重新打开，会自动补装；若反复出现请反馈给开发者")
    elif not await _to_thread(_importable, "faster_whisper"):
        return _check("语音识别", FAIL, "faster-whisper 不可用",
                      "关闭程序后重新打开，会自动补装")
    cached = _model_cached(model, rec["backend"])
    detail = "{} + {}（{}）".format(rec["backend"], model,
                                   "模型已下载" if cached else "首次开播需先下载模型")
    return _check("语音识别", OK if cached else WARN, detail)


def _hub_dirs():
    """HuggingFace 缓存目录。三个环境变量都要认，顺序与 huggingface_hub 一致。"""
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        val = os.environ.get(var)
        if val:
            return Path(val)
    hf = os.environ.get("HF_HOME")
    return Path(hf) / "hub" if hf else Path.home() / ".cache" / "huggingface" / "hub"


def _model_cached(model, backend):
    """模型是否真的下全了。

    这里踩过三个坑，都会让界面报绿而第一次开播卡在下载上：
      * 子串匹配：`"large-v3" in "…large-v3-turbo"` 为真，turbo 的缓存会冒充
        large-v3；
      * 后端不分：ct2 的 Systran 仓库和 MLX 的 mlx-community 仓库是两个 3GB
        的不同东西，名字里都含 large-v3；
      * 下了一半：HF 一开始传就建好目录结构，得看 snapshots 里有没有落成的
        文件、以及还有没有 .incomplete 残留。
    所以按后端解析出**确切的仓库名**，再验完整性。"""
    from .asr import _MLX_REPOS
    if backend == "mlx":
        repo = _MLX_REPOS.get(model, model)
    else:
        repo = model if "/" in model else "Systran/faster-whisper-" + model
    target = "models--" + repo.replace("/", "--")
    hub = _hub_dirs()
    entry = hub / target
    if not entry.is_dir():
        return False
    try:
        blobs = entry / "blobs"
        if blobs.is_dir() and any(b.name.endswith(".incomplete") for b in blobs.iterdir()):
            return False        # 还在下载中
        snaps = entry / "snapshots"
        if not snaps.is_dir():
            return False
        return any(any(rev.iterdir()) for rev in snaps.iterdir() if rev.is_dir())
    except OSError:
        return False


async def check_translator(args):
    name = getattr(args, "translator", "auto")
    if name == "none":
        return _check("翻译引擎", OK, "已按 --translator none 主动关闭")
    try:
        from .translator import _ollama_has_gemma, _ollama_has_hymt2
    except Exception as exc:
        return _check("翻译引擎", FAIL, "翻译模块加载失败：{}".format(exc))
    has_big = await _to_thread(_ollama_has_hymt2, True)
    has_hymt2 = await _to_thread(_ollama_has_hymt2)
    has_gemma = await _to_thread(_ollama_has_gemma)
    resolved = name
    if name == "auto":
        resolved = ("hymt2-7b" if has_big else
                    "hymt2" if has_hymt2 else
                    "gemma" if has_gemma else "google")
    if resolved == "hymt2-7b":
        if has_big:
            return _check("翻译引擎", OK, "本地 Hy-MT2 7B（离线、无限流、术语最准）")
        return _check("翻译引擎", FAIL, "指定了 Hy-MT2 7B，但 Ollama 里没有这个模型",
                      "先打开 Ollama，再执行一次 "
                      "ollama pull hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M")
    if resolved == "hymt2":
        if has_hymt2:
            return _check("翻译引擎", OK, "本地 Hy-MT2 1.8B（离线、无限流）")
        return _check("翻译引擎", FAIL, "指定了 Hy-MT2，但 Ollama 里没有这个模型",
                      "先打开 Ollama，再执行一次 "
                      "ollama pull hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M")
    if resolved == "gemma":
        if has_gemma:
            return _check("翻译引擎", OK, "本地 TranslateGemma（离线、无限流）")
        return _check("翻译引擎", FAIL, "指定了本地翻译，但 Ollama 里没有这个模型",
                      "先打开 Ollama，再执行一次 ollama pull translategemma:4b")
    if resolved == "google":
        return _check("翻译引擎", WARN,
                      "正在用 Google 免费接口：会按 IP 限流，长时间监听容易"
                      "整段翻译失败（违禁词报警不受影响，它不依赖翻译）",
                      "想换成完全本地、不限流的翻译：装 Ollama（ollama.com），"
                      "装完执行一次 "
                      "ollama pull hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M"
                      "（约 1.1 GB），然后重开本程序即可自动切换")
    key = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(resolved)
    if key and not os.environ.get(key):
        return _check("翻译引擎", FAIL,
                      "{} 需要先设置环境变量 {}".format(resolved, key),
                      "没有这个密钥的话，把翻译引擎留在默认的「自动」即可")
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
    if not await _to_thread(_importable, "yt_dlp"):     # yt-dlp 导入很重
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
    """跑完所有自检。任一项抛异常都不影响其余项——自检自己绝不能拖垮启动。

    探测崩了算 **FAIL，不是 WARN**：崩了意味着这项能力压根没被验证过，和
    「验过了，有点小问题」是两回事。之前记成 WARN，界面上就会出现「✅ 自检
    通过，1 项提醒」而那一项其实是没检成——这正是本模块要消灭的那种「看起来
    没事」。名字也必须带上，否则只剩一个「自检项」，用户不知道是哪块没验。"""
    probes = [
        ("音频组件 ffmpeg", check_ffmpeg()),
        ("人声降噪", check_denoise(args)),
        ("语音识别", check_asr(args)),
        ("翻译引擎", check_translator(args)),
        ("违禁词表", check_watchlist(detector)),
        ("领域词表", check_glossary(glossary)),
        ("审计日志", check_audit()),
        ("直播流解析", check_resolver()),
        ("磁盘空间", check_disk()),
    ]
    results = await asyncio.gather(*(c for _, c in probes), return_exceptions=True)
    checks = []
    for (name, _), r in zip(probes, results):
        if isinstance(r, BaseException):
            checks.append(_check(name, FAIL,
                                 "这一项没能检查完（{}）——它是好是坏都不知道".format(r),
                                 "把这条信息反馈给开发者；这不影响其它功能"))
        else:
            checks.append(r)
    return checks


def summarize(checks):
    fails = sum(1 for c in checks if c["level"] == FAIL)
    warns = sum(1 for c in checks if c["level"] == WARN)
    return {"total": len(checks), "fail": fails, "warn": warns,
            "ok": len(checks) - fails - warns}
