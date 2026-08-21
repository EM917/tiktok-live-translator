"""自动更新：启动时检查 GitHub 最新 release，UI 一键更新（git 安装）并自动重启。

安全边界：只做 `git pull --ff-only`，且要求工作区干净——绝不覆盖用户的本地改动；
ZIP 下载（无 .git）的安装只提示去下载页，不尝试自动更新。
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "EM917/tiktok-live-translator"
API_LATEST = "https://api.github.com/repos/{}/releases/latest".format(REPO)
RELEASES_URL = "https://github.com/{}/releases/latest".format(REPO)


def local_version():
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def _parse(version):
    version = version.strip().lstrip("vV")
    parts = []
    for piece in version.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


class Updater:
    def __init__(self, server):
        self.server = server
        self.latest = None

    async def check_and_notify(self, delay=2.0):
        """启动后静默检查一次；网络失败/限流一律无声跳过。"""
        await asyncio.sleep(delay)
        try:
            import aiohttp

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as session:
                async with session.get(
                    API_LATEST, headers={"Accept": "application/vnd.github+json"}
                ) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
        except Exception:
            return
        tag = str(data.get("tag_name") or "")
        if not tag or _parse(tag) <= _parse(local_version()):
            return
        self.latest = {
            "version": tag,
            "notes": (data.get("body") or "")[:500],
            "url": data.get("html_url") or RELEASES_URL,
            "can_auto": (ROOT / ".git").exists(),
        }
        payload = dict(self.latest)
        payload["type"] = "update_available"
        self.server.config["update"] = self.latest
        await self.server.broadcast(payload)
        print("[信息] 发现新版本 {}（当前 v{}）——可在页面上一键更新".format(
            tag, local_version()))

    async def _git(self, *args):
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def apply(self):
        """一键更新：仅 fast-forward pull，成功后原地重启进程。"""
        if self.latest is None:
            return
        if not self.latest.get("can_auto"):
            await self.server.status(
                "idle", "当前是 ZIP 安装，无法自动更新——请到 GitHub 下载新版本：{}".format(
                    self.latest["url"]))
            return
        await self.server.broadcast({"type": "updating"})
        code, out, _ = await self._git("status", "--porcelain")
        if code != 0:
            await self.server.status("error", "git 不可用，无法自动更新，请手动 git pull")
            return
        if out.strip():
            await self.server.status(
                "error", "检测到本地有未提交的修改，为避免覆盖已取消自动更新——"
                         "请自行处理后 git pull")
            return
        code, _, err = await self._git("pull", "--ff-only")
        if code != 0:
            tail = err.strip().splitlines()[-2:]
            await self.server.status("error", "更新失败：{}".format(" / ".join(tail)))
            return
        await self.server.status("idle", "更新完成，正在自动重启…")
        print("[信息] 已更新到最新版本，重启进程…")
        await asyncio.sleep(0.6)
        os.execv(sys.executable,
                 [sys.executable, str(ROOT / "main.py")] + sys.argv[1:])
