"""硬件检测与自动配置：根据这台电脑的芯片/GPU/核数/内存，推荐能实时跑的最优配置。

设计原则：宁可保守也不能丢字幕——推荐的模型必须让识别速度稳定快于直播（RTF < 1）。
实测锚点（2026-08，90s 真实直播素材）：
  * Apple M3 Pro + MLX GPU + large-v3      → RTF 0.21  ✅
  * Apple M3 Pro + CPU  + large-v3-turbo   → RTF 0.99  ⚠️ 临界
  * Apple M3 Pro + CPU  + large-v3         → RTF 1.22  ❌ 丢段
"""
import os
import platform
import shutil
import sys


def _ram_gb():
    try:
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) / 1024 ** 3
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / 1024 ** 2
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys / 1024 ** 3
    except Exception:
        pass
    return 0.0


def detect():
    """探测当前机器，返回一个 dict。"""
    machine = platform.machine().lower()
    is_mac = sys.platform == "darwin"
    apple_silicon = is_mac and machine in ("arm64", "aarch64")
    has_mlx = False
    if apple_silicon:
        try:
            import mlx_whisper  # noqa: F401
            has_mlx = True
        except ImportError:
            pass
    from .ffmpeg_bin import find_ffmpeg

    return {
        "os": {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, sys.platform),
        "machine": machine,
        "apple_silicon": apple_silicon,
        "has_mlx": has_mlx,
        "has_cuda": shutil.which("nvidia-smi") is not None,
        "cores": os.cpu_count() or 4,
        "ram_gb": round(_ram_gb(), 1),
        "has_ffmpeg": find_ffmpeg() is not None,
    }


def recommend(info=None, backend=None, device=None):
    """返回推荐配置 dict：backend / model / device / compute_type / note。

    backend/device 传入用户显式指定的值（None 或 "auto" 表示未指定）——四个字段是
    一个联动整体，用户锁定其中一个时，其余字段必须围绕它重新推导，不能沿用为
    另一套硬件路径算出的值（否则会拼出「CPU 跑 large-v3」这类会丢字幕的组合）。
    """
    info = info or detect()
    if backend in (None, "auto"):
        backend = "mlx" if info["has_mlx"] else "ct2"

    if backend == "mlx":
        return {"backend": "mlx", "model": "large-v3", "device": "auto",
                "compute_type": "auto",
                "note": "Apple Silicon GPU（MLX）：直接跑最准的 large-v3"}

    # ---- ct2（faster-whisper）----
    if device in (None, "auto"):
        device = "cuda" if info["has_cuda"] else "cpu"
    if device == "cuda":
        return {"backend": "ct2", "model": "large-v3", "device": "cuda",
                "compute_type": "float16",
                "note": "NVIDIA GPU：CUDA 跑 large-v3"}
    # CPU 路径
    if info["apple_silicon"]:
        note = ("Apple Silicon CPU：turbo 临界实时" +
                ("" if info["has_mlx"] else
                 "——装 mlx-whisper 可改用 GPU 跑 large-v3"))
        return {"backend": "ct2", "model": "large-v3-turbo", "device": "cpu",
                "compute_type": "auto", "note": note}
    cores, ram = info["cores"], info["ram_gb"]
    if cores >= 8 and ram >= 8:
        return {"backend": "ct2", "model": "small", "device": "cpu",
                "compute_type": "int8",
                "note": "普通 CPU（{}核/{:.0f}GB）：small+int8 保实时；"
                        "识别不够准可手动试 --model medium".format(cores, ram)}
    return {"backend": "ct2", "model": "base", "device": "cpu",
            "compute_type": "int8",
            "note": "低配 CPU（{}核/{:.0f}GB）：base+int8，优先保证字幕不掉队".format(cores, ram)}


def doctor():
    """`python main.py --doctor`：打印体检报告，返回 exit code。"""
    info = detect()
    rec = recommend(info)
    print("=" * 56)
    print("tiktok-live-translator 环境体检 / Environment Doctor")
    print("=" * 56)
    print("系统 OS         : {} ({})".format(info["os"], info["machine"]))
    print("CPU 核数 Cores  : {}".format(info["cores"]))
    print("内存 RAM        : {:.1f} GB".format(info["ram_gb"]))
    print("Apple Silicon   : {}".format("✅" if info["apple_silicon"] else "—"))
    print("MLX GPU 后端    : {}".format("✅ 已安装" if info["has_mlx"] else
                                        ("⚠️ 未安装（建议 pip install mlx-whisper）"
                                         if info["apple_silicon"] else "—")))
    print("NVIDIA CUDA     : {}".format("✅" if info["has_cuda"] else "—"))
    from .ffmpeg_bin import ffmpeg_source
    src = ffmpeg_source()
    print("ffmpeg          : {}".format(
        "✅ {}".format(src) if src else
        "❌ 未找到（运行 setup 脚本，或 pip install imageio-ffmpeg）"))

    # 翻译引擎探测（依赖未装时跳过，doctor 本身必须零依赖可运行）
    try:
        from .translator import _ollama_has_gemma
        gemma = _ollama_has_gemma()
        print("本地翻译 Gemma  : {}".format(
            "✅ Ollama + TranslateGemma 就绪" if gemma else
            "— 未检测到（将用 Google 免费接口；本地化安装见 README「翻译引擎」）"))
    except ImportError:
        print("本地翻译 Gemma  : ？（依赖未安装，先运行 setup.sh / setup.ps1）")

    try:
        import aiohttp  # noqa: F401
        deps_ok = True
    except ImportError:
        deps_ok = False
    print("Python 依赖     : {}".format(
        "✅" if deps_ok else "❌ 未安装（运行 setup.sh / setup.ps1，"
                            "或 pip install -r requirements.txt）"))

    print("-" * 56)
    print("推荐配置 Recommended:")
    print("  后端 backend  : {}".format(rec["backend"]))
    print("  模型 model    : {}".format(rec["model"]))
    if rec["device"] != "auto":
        print("  设备 device   : {}".format(rec["device"]))
    if rec["compute_type"] != "auto":
        print("  精度 compute  : {}".format(rec["compute_type"]))
    print("  说明          : {}".format(rec["note"]))
    print("-" * 56)
    print("以上即默认值——直接运行即可，无需手动传参：")
    print("  python main.py https://www.tiktok.com/@主播/live")
    if not info["has_ffmpeg"]:
        print("\n⚠️ 未找到 ffmpeg——运行 setup 脚本或 pip install -r requirements.txt 即可自动获得。")
        return 1
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return 1
    return 0
