"""直播闸：直播或同传程序开着时，测试、Chrome、批量任务一律不跑。

用法（写在同一条命令里，在仓库根目录）：
    python3 -I tools/livecheck.py | grep -qx idle || { echo GATE-SKIP; exit 0; };

只有明确打出 idle 才往下跑：脚本不在、python3 起不来时什么都不打，照样 GATE-SKIP（闸坏了不等于放行）。
打印一行并用退出码表示：
    live stream ...  退出码 0：闪购助手正在续开闪购，或者同传程序在拉直播流
    app main ...     退出码 0：同传程序（main.py）开着
    idle             退出码 1：可以跑
只读：查进程表和闪购助手的 GET /status，不发控制请求，不读密钥。这一次 GET 是 agent 碰 9333、9334 的
唯一例外（CLAUDE.md 第九条）；测试里连 9334 会被测试总网拦下（tests/socket_net.py）。
-I 下也能跑：除了仓库里的 app/stdio.py，只用标准库。
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))          # 仓库根不在 sys.path 里（-I 连脚本所在的 tools/ 也不放）

from app.stdio import harden_stdio                     # noqa: E402

# 重定向、接管道时（Windows 上默认编码是 ANSI 代码页 + strict），中文那一行不该让闸自己崩掉：见 app/stdio.py
harden_stdio()

STATUS_URL = "http://127.0.0.1:9334/status"


def running(pattern):
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def helper_running():
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=2) as r:
            return json.load(r).get("running") is True
    except Exception:
        return False


def verdict():
    """(打印的那一行, 退出码)。先查拉流、再问闪购助手、最后查同传程序，哪个先中就停。"""
    if running("tiktokcdn"):
        return "live stream: 同传程序在拉直播流", 0
    if helper_running():
        return "live stream: 闪购助手正在运行", 0
    if running(r"main\.py") and running("tiktok-live-translator"):
        return "app main: 同传程序开着", 0
    return "idle", 1


def main():
    line, code = verdict()
    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
