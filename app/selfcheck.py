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

    # Apple Silicon 却在用 CPU 后端 = 这台机器的能力只发挥了一小部分。
    # 实测（hwdetect）CPU 跑 turbo 的 RTF 是 0.99——勉强跟得上，跑久了必然积压，
    # 而且只能用较小的模型。以前这里只如实报「ct2 + large-v3-turbo」，不说
    # 「你本可以快一倍」，用户看不出有什么不对。一台 M4 就这样一直跑在慢路径上。
    try:
        from .hwdetect import detect
        info = detect()
    except Exception:
        info = {}
    if info.get("apple_silicon") and rec["backend"] != "mlx":
        return _check("语音识别", WARN,
                      "这台 Mac 有 GPU 加速能力，但正在用 CPU 识别（{}）"
                      "——慢一倍以上，长时间监听容易积压".format(detail),
                      "关闭程序后重新打开，会自动补装 GPU 加速组件；"
                      "若反复出现请把这句话反馈给开发者")
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


async def check_translator(args, translator=None):
    """报的必须是**管线真正在用的那个引擎**，不是这里重新推导一遍。

    踩过的坑：这里曾经自己复制了一份 auto 的选择逻辑。后来默认档从 7B 改回
    1.8B，只改了 create_translator，这份副本没跟上——于是程序实际用着 1.8B，
    面板却写着「本地 Hy-MT2 7B」。自检报错东西，比不自检更糟：它会让人相信
    一个错误的事实。所以现在直接问已经建好的对象。
    """
    name = getattr(args, "translator", "auto")
    if name == "none" or (translator is None and name == "none"):
        return _check("翻译引擎", OK, "已按 --translator none 主动关闭")

    if translator is not None:
        engine = getattr(translator, "name", "?")
        model = getattr(getattr(translator, "inner", translator), "model", "")
        if engine in ("hymt2", "hymt2-7b"):
            tier = "7B" if "7B" in model else "1.8B"
            if not await _to_thread(_ollama_reachable):
                return _check("翻译引擎", FAIL,
                              "配置的是本地 Hy-MT2 {}，但 Ollama 没在运行".format(tier),
                              "打开 Ollama 后重新点「开始翻译」")
            note = "、术语最准，但更吃内存" if tier == "7B" else ""
            return _check("翻译引擎", OK,
                          "本地 Hy-MT2 {}（离线、无限流{}）".format(tier, note))
        if engine == "gemma":
            if not await _to_thread(_ollama_reachable):
                return _check("翻译引擎", FAIL,
                              "配置的是本地 TranslateGemma，但 Ollama 没在运行",
                              "打开 Ollama 后重新点「开始翻译」")
            return _check("翻译引擎", OK, "本地 TranslateGemma（离线、无限流）")
        if engine == "google":
            from . import localmodel
            # 装好 Ollama 之后模型是程序自己拉的，不用再让用户敲 ollama pull
            hint, _url = localmodel.install_hint()
            return _check("翻译引擎", WARN,
                          "正在用 Google 免费接口：会按 IP 限流，长时间监听容易"
                          "整段翻译失败（违禁词报警不受影响，它不依赖翻译）",
                          "想换成完全本地、不限流的翻译：" + hint)
        if engine == "deepl":
            return await _check_deepl(args, translator)
        key = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(engine)
        if key and not os.environ.get(key):
            return _check("翻译引擎", FAIL,
                          "{} 需要先设置环境变量 {}".format(engine, key),
                          "没有这个密钥的话，把翻译引擎留在默认的「自动」即可")
        return _check("翻译引擎", OK, "{}（付费 API）".format(engine))

    # 没有引擎对象（还没建，或翻译被关掉）——只能就配置说话，不假装知道更多
    if name == "none":
        return _check("翻译引擎", OK, "已按 --translator none 主动关闭")
    return _check("翻译引擎", WARN, "翻译引擎尚未初始化",
                  "点一次「开始翻译」后本项会重新检查")


async def _check_deepl(args, translator):
    """DeepL 的成败几乎全系于原生术语表，所以这里真的去把表建出来。

    实测同一批 60 句：不挂术语表词表遵从率 26.5%，挂上 91.8%。表建不起来
    时 DeepL 照样会返回通顺的中文——商品名全是直译，屏幕上看不出异常。
    这正是自检要防的那类静默退化，只看配置没有用，必须实际建一次。

    探针用 `--source` 指定的语言，没指定就按西语：glossary.txt 是一份西语
    商品词表，而且免费版只有一个术语表槽位——与其等第一句字幕来认领（万一
    那句被判成英语，槽位就被占错了），不如在这里定死。
    """
    from .glossary import load as load_glossary

    inner = getattr(translator, "inner", translator)
    target = getattr(args, "target", "zh-CN")
    source = (getattr(args, "source", None) or "es").lower()
    if not hasattr(inner, "_ensure_glossary"):
        return _check("翻译引擎", OK, "DeepL（付费 API）")
    if target not in inner._GLOSSARY_TARGET:
        return _check("翻译引擎", WARN,
                      "DeepL 已就绪，但 {} 不挂原生术语表（DeepL 的术语表只有"
                      "简体一档），商品名只能靠译后替换兜底".format(target))
    try:
        gid = await inner._ensure_glossary(source, target)
    except Exception as exc:
        return _check("翻译引擎", FAIL, "DeepL 连不上：{}".format(exc),
                      "检查密钥和网络；或把引擎换成本地 Hy-MT2")
    if not gid:
        return _check("翻译引擎", WARN,
                      "DeepL 已就绪，但原生术语表没建起来——商品名会被直译"
                      "（实测词表遵从率会从 91.8% 掉到 26.5%）",
                      "多半是额度或权限问题；本地 Hy-MT2 不受影响")
    n = len(inner.glossary_tsv(load_glossary().entries).splitlines())
    return _check("翻译引擎", OK,
                  "DeepL + 原生术语表（{} 条，{}→{}）".format(n, source, target))


def _ollama_reachable():
    import urllib.request

    base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        urllib.request.urlopen(base + "/api/tags", timeout=2).read()
        return True
    except Exception:
        return False


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


# 一次完整安装的实测占用（见 README「磁盘空间」）：
#   运行环境 1.4 GB + 语音模型 large-v3 2.9 GB + 翻译模型 1.8B 1.1 GB ≈ 6 GB
# 门槛按这个来，别让用户下到一半才发现放不下。
INSTALL_NEED_GB = 6


async def check_disk():
    try:
        free = shutil.disk_usage(ROOT).free / 1024 ** 3
    except OSError:
        return _check("磁盘空间", WARN, "无法读取磁盘剩余空间")
    if free < 3:
        return _check("磁盘空间", FAIL,
                      "仅剩 {:.1f} GB——完整安装需要约 {} GB（运行环境 1.4 + "
                      "语音模型 2.9 + 翻译模型 1.1）".format(free, INSTALL_NEED_GB),
                      "腾出空间后重开程序；各部分体积见 README「磁盘空间」")
    if free < INSTALL_NEED_GB:
        return _check("磁盘空间", WARN,
                      "剩余 {:.1f} GB，完整安装约需 {} GB——模型可能下不全"
                      .format(free, INSTALL_NEED_GB),
                      "腾出空间，或用 --model large-v3-turbo（省约 1.4 GB）")
    return _check("磁盘空间", OK, "剩余 {:.0f} GB".format(free))


async def run_all(args, detector=None, glossary=None, translator=None):
    """跑完所有自检。任一项抛异常都不影响其余项——自检自己绝不能拖垮启动。

    探测崩了算 **FAIL，不是 WARN**：崩了意味着这项能力压根没被验证过，和
    「验过了，有点小问题」是两回事。之前记成 WARN，界面上就会出现「✅ 自检
    通过，1 项提醒」而那一项其实是没检成——这正是本模块要消灭的那种「看起来
    没事」。名字也必须带上，否则只剩一个「自检项」，用户不知道是哪块没验。"""
    probes = [
        ("音频组件 ffmpeg", check_ffmpeg()),
        ("人声降噪", check_denoise(args)),
        ("语音识别", check_asr(args)),
        ("翻译引擎", check_translator(args, translator)),
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
