"""本地翻译模型的自动就绪：不让用户为了「离线翻译」去开终端。

背景：没装 Ollama 的机器会退回 Google 免费接口——它按 IP 限流，长时间监听
经常整段翻译失败。而原来的提示是让用户自己去装 Ollama、再敲一行 ollama pull。
对一个连终端是什么都不知道的人来说，这等于永远用不上本地翻译。

这里做三件事，按代价从小到大：
  1. Ollama 在跑但没有模型 —— 直接用它的 HTTP 接口把模型拉下来（免终端）。
     我们本来就会自动下 3GB 的 Whisper 模型，再下 1.1GB 的翻译模型是同一件事。
  2. Ollama 装了但没启动 —— 帮他启动。
  3. Ollama 压根没装 —— 只能引导。macOS 的包 179MB 可以自动装，
     Windows 的安装器 1.5GB 且要管理员授权，装不了，老实说清楚。
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

OLLAMA_DOWNLOAD = {
    "darwin": "https://ollama.com/download/Ollama-darwin.zip",
    "win32": "https://ollama.com/download/OllamaSetup.exe",
}


def base_url():
    return os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


async def is_running(timeout=2):
    import aiohttp

    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(base_url() + "/api/tags") as r:
                return r.status == 200
    except Exception:
        return False


# macOS 上 Ollama 的命令行工具在应用包的 Contents/Resources/ollama。
# 但用户未必把 app 拖进「应用程序」——留在「下载」里双击也能用，
# 而且不打开它一次的话，命令行工具根本不会装到 PATH 上。
_MAC_APP_DIRS = ("/Applications", "~/Applications", "~/Downloads", "~/Desktop")


def find_binary():
    """本机装没装 Ollama。

    找不到不等于没装：用户可能把 app 放在任何地方。所以 macOS 上还会走
    LaunchServices（`open -a Ollama`），那条路不依赖我们猜对路径。
    """
    exe = shutil.which("ollama")            # brew 装的、或开过一次装了命令行工具的
    if exe:
        return exe
    if sys.platform != "darwin":
        return None
    for d in _MAC_APP_DIRS:
        cand = Path(d).expanduser() / "Ollama.app" / "Contents" / "Resources" / "ollama"
        if cand.exists():
            return str(cand)
    return None


def _mac_app_exists():
    """LaunchServices 认不认识 Ollama——它在哪个目录都算。

    比自己猜路径可靠：用户把 app 放在哪儿都行，只要 macOS 索引过它。
    """
    if sys.platform != "darwin":
        return False
    if find_binary():
        return True
    try:
        import subprocess
        r = subprocess.run(["mdfind", "kMDItemCFBundleIdentifier == 'com.electron.ollama'"
                            " || kMDItemFSName == 'Ollama.app'"],
                           capture_output=True, timeout=8)
        return bool(r.stdout.strip())
    except Exception:
        return False


def is_installed():
    return find_binary() is not None or _mac_app_exists()


async def start(timeout=25):
    """启动已安装但没在跑的 Ollama，等到接口真的能通为止。"""
    if await is_running():
        return True
    exe = find_binary()
    if exe is None and not _mac_app_exists():
        return False
    try:
        if sys.platform == "darwin":
            # open -a 走 LaunchServices：app 放在哪个目录都能启动，
            # 不用我们猜路径，也不要求用户先把它拖进「应用程序」
            await asyncio.create_subprocess_exec(
                "open", "-a", "Ollama",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        elif exe is None:
            return False
        else:
            # serve 要活得比我们久，别绑在本进程的输出上
            await asyncio.create_subprocess_exec(
                exe, "serve",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except Exception:
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(1.0)
        if await is_running():
            return True
    return False


async def pull(model, on_progress=None):
    """通过 HTTP 接口拉模型。on_progress(百分比, 已下载MB, 总MB) 用于界面进度。

    走接口而不是 `ollama pull` 命令：用户不必开终端，我们也能把进度显示在
    页面上——这和 Whisper 模型的下载进度是同一种体验。
    """
    import aiohttp

    body = json.dumps({"model": model, "stream": True})
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_read=120)) as s:
            async with s.post(base_url() + "/api/pull", data=body) as r:
                if r.status != 200:
                    return False
                async for raw in r.content:
                    if not raw.strip():
                        continue
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("error"):
                        return False
                    total, done = msg.get("total"), msg.get("completed")
                    if on_progress and total:
                        on_progress(100.0 * (done or 0) / total,
                                    (done or 0) / 1e6, total / 1e6)
                    if msg.get("status") == "success":
                        return True
        return await is_running()
    except Exception:
        return False


def install_hint():
    """没装 Ollama 时给用户的话。分平台说实话，别承诺做不到的事。"""
    url = OLLAMA_DOWNLOAD.get(sys.platform)
    if sys.platform == "darwin":
        return ("到 ollama.com 下载 Ollama（约 179 MB），拖进「应用程序」打开一次，"
                "然后重开本程序——翻译模型会自动下载，不用敲任何命令。", url)
    if sys.platform == "win32":
        return ("到 ollama.com 下载并安装 Ollama（安装器约 1.5 GB，需要管理员权限，"
                "所以本程序无法代劳），装完重开本程序——翻译模型会自动下载。", url)
    return ("按 ollama.com 上的说明装好 Ollama 后重开本程序，翻译模型会自动下载。",
            "https://ollama.com/download")
