"""自动更新：启动时检查 GitHub 最新 release，UI 一键更新（git 安装）并自动重启。

安全边界：只做 `git pull --ff-only`，且要求工作区干净——绝不覆盖用户的本地改动；
ZIP 下载（无 .git）的安装只提示去下载页，不尝试自动更新。
"""
import asyncio
import importlib.util
import os
import re
import sys
import time
from pathlib import Path

from .settings import load_settings, save_setting

ROOT = Path(__file__).resolve().parent.parent
# 距上次提示不足这么久，就只静默刷新版本信息，不再弹醒目提示
QUIET_NOTICE_SEC = 6 * 3600
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
        self._applying = False
        self._freshening = False
        self._freshen_attempted_at = 0.0     # 失败也要冷却，别反复拉起注定失败的 pip
        # freshen 与一键更新共用：pip 不能并发写环境。惰性初始化——
        # 3.9 的 asyncio.Lock() 构造时就要绑事件循环
        self._pip_lock = None

    def _get_pip_lock(self):
        if self._pip_lock is None:
            self._pip_lock = asyncio.Lock()
        return self._pip_lock

    async def watch(self, first_delay=2.0, interval=6 * 3600):
        """启动后检查一次，之后每 6 小时复查——常开不关的用户也能及时看到新版本。"""
        await self.check_and_notify(delay=first_delay)
        await self.freshen_ytdlp(reason="periodic")   # 顺带保鲜最易腐坏的组件
        while True:
            await asyncio.sleep(interval)
            if self.latest is None:      # 已经提示过就不再重复打扰
                await self.check_and_notify(delay=0)
            await self.freshen_ytdlp(reason="periodic")

    async def _notice(self, text):
        """一次性提示（手动检查更新的结果等）。刻意不走 status 通道：status 会
        切换整个 UI 状态机并被持久化进 config——直播中点一下「检查更新」就会把
        页面打回「待机」、停止按钮消失，而管线其实还在跑。"""
        await self.server.broadcast({"type": "notice", "text": text})

    async def freshen_ytdlp(self, reason="periodic"):
        """后台升级 yt-dlp。它的 TikTok 提取器是全项目最易腐坏的一环，而常规
        更新通道（跟随本项目发版的 pip install -r）不会主动升它——老安装会
        在某天集体拉流失败。两个触发源：

          periodic          —— 启动/每 6 小时检查时，距上次升级超过 7 天就升；
          resolve-failures  —— 连续解析失败（非「没开播」类）时，6 小时冷却。

        yt-dlp 是每次解析都新起的子进程（resolver.py 用 -m yt_dlp），升级完
        下一次点「开始翻译」就生效，无需重启本程序。"""
        if self._freshening:
            return
        min_age = 6 * 3600 if reason == "resolve-failures" else 7 * 86400
        last = load_settings().get("ytdlp_updated_at") or 0
        try:
            last = float(last)
        except (TypeError, ValueError):
            last = 0
        now = time.time()
        if now - last < min_age:
            return
        if now - self._freshen_attempted_at < 600:   # 上次尝试（含失败）10 分钟内不重试
            return
        self._freshening = True
        self._freshen_attempted_at = now
        try:
            async with self._get_pip_lock():
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install", "-U",
                    "--disable-pip-version-check", "yt-dlp",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
                except asyncio.TimeoutError:
                    proc.kill()          # pip 卡死不能拖垮 watch 循环
                    print("[警告] yt-dlp 自动更新超时，已放弃本次尝试")
                    return
            if proc.returncode != 0:
                print("[警告] yt-dlp 自动更新失败（pip 返回 {}）".format(proc.returncode))
                return
            save_setting("ytdlp_updated_at", time.time())
            text = out.decode(errors="replace")
            m = re.search(r"Successfully installed .*?yt.dlp-(\S+)", text)
            if m:
                print("[信息] 已自动更新 yt-dlp 到 {}（{}）".format(m.group(1), reason))
                # 只有在没有活跃直播时才广播提示——绝不能把 live/connecting
                # 状态顶掉，误导用户去重启一条正常工作的管线
                current = (self.server.config.get("status") or {}).get("state")
                if reason == "resolve-failures" and current in (None, "idle", "error"):
                    await self.server.status(
                        "idle", "已自动更新直播解析组件（yt-dlp {}）——"
                                "请重新点「开始翻译」试试。".format(m.group(1)))
        except Exception as exc:
            print("[警告] yt-dlp 自动更新异常: {}".format(exc))
        finally:
            self._freshening = False

    async def ensure_tiktoklive(self, reason="startup"):
        """按需安装弹幕组件 TikTokLive（可选依赖，仅 comment_worker.py 子进程用）。

        仿 freshen_ytdlp：装过/装不了就别反复折腾 pip。与 freshen_ytdlp 不同的是
        这里的「一小时内不重试」持久化进 settings.json——弹幕来源的供给回调
        (`CommentSource.on_provision`) 可能在同一场直播里被反复触发（每次
        `worker_available()` 仍为假），不能靠内存里的时间戳，重启后又能立刻
        重试是好事，但同一次运行内不该每次都拉一次 pip。

        Python < 3.10 直接放弃——TikTokLive 7.x 要求 3.10+，这是本项目唯一
        允许高于 3.9 的可选组件，装不上不影响主链路（弹幕来源据此转成
        `unavailable` 状态，见 comment_source.py 的 _supervise）。
        """
        if sys.version_info < (3, 10):
            return False
        if importlib.util.find_spec("TikTokLive") is not None:
            return True
        last = load_settings().get("tiktoklive_install_attempted_at") or 0
        try:
            last = float(last)
        except (TypeError, ValueError):
            last = 0
        now = time.time()
        if now - last < 3600:
            return False
        save_setting("tiktoklive_install_attempted_at", now)
        async with self._get_pip_lock():
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install",
                    "--disable-pip-version-check", "TikTokLive>=7,<8",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=600)
                except asyncio.TimeoutError:
                    proc.kill()          # pip 卡死不能拖垮调用方（弹幕来源的监督协程）
                    try:
                        await proc.wait()   # 必须 reap，否则残留一个僵尸进程
                    except Exception:
                        pass
                    print("[警告] 弹幕组件 TikTokLive 安装超时，已放弃本次尝试（{}）"
                          .format(reason))
                    return False
            except Exception as exc:
                print("[警告] 弹幕组件 TikTokLive 安装异常: {}".format(exc))
                return False
        importlib.invalidate_caches()   # pip 刚装完，find_spec 的路径缓存可能还是旧的
        if proc.returncode == 0 and importlib.util.find_spec("TikTokLive") is not None:
            print("[信息] 已安装弹幕组件 TikTokLive（{}）".format(reason))
            return True
        print("[警告] 弹幕组件 TikTokLive 安装失败（pip 返回 {}，{}）"
              .format(proc.returncode, reason))
        return False

    async def check_and_notify(self, delay=2.0, manual=False):
        """检查一次最新版本；网络失败/限流一律无声跳过（手动检查时会回报结果）。"""
        if delay:
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
                        if manual:
                            await self._notice(
                                "检查更新失败（GitHub 返回 {}）".format(resp.status))
                        return
                    data = await resp.json()
        except Exception:
            if manual:
                await self._notice("检查更新失败（网络不可达）")
            return
        tag = str(data.get("tag_name") or "")
        if not tag or _parse(tag) <= _parse(local_version()):
            if manual:
                await self._notice("已是最新版本 v{}".format(local_version()))
            return
        self.latest = {
            "version": tag,
            "notes": (data.get("body") or "")[:500],
            "url": data.get("html_url") or RELEASES_URL,
            "can_auto": (ROOT / ".git").exists(),
        }
        # 提示降噪。这个项目的更新提示是**真实的打扰**——它会在直播中控的
        # 界面上弹出来。连续发几个 patch 的日子（真实发生过：一天 12 个 release，
        # 全部来自实盘暴露的问题），不降噪就是把用户轰一遍。
        #
        # 规则：短时间内已经提示过就只静默刷新版本信息，让「一键更新」按钮
        # 保持可用，但不再弹醒目提示。手动点「检查更新」永远给回应——
        # 那是用户主动问的。
        quiet = False
        if not manual:
            last = load_settings().get("update_notice_at") or 0
            quiet = (time.time() - last) < QUIET_NOTICE_SEC
            if not quiet:
                save_setting("update_notice_at", time.time())

        payload = dict(self.latest)
        payload["type"] = "update_available"
        payload["quiet"] = quiet
        self.server.config["update"] = self.latest
        await self.server.broadcast(payload)
        if not quiet:
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
        if self.latest is None or self._applying:
            return   # 重复点击「一键更新」不能并发跑两次 git/pip
        self._applying = True
        try:
            await self._apply_inner()
        finally:
            self._applying = False

    def _manual_command(self, discard=False):
        """一条可以原样粘进终端的完整命令，路径是这台机器上的真实路径。

        不写成「请自行 git pull」——那句话假设用户知道项目在哪、知道要先 cd。
        真实案例里，一台机器就因为这句话卡在旧版本上，而它需要的正是能修好
        这个判断的那次更新。"""
        path = str(ROOT)
        if discard:
            return 'cd "{}" && git checkout -- . && git pull --ff-only'.format(path)
        return 'cd "{}" && git pull --ff-only'.format(path)

    async def _apply_inner(self):
        if not self.latest.get("can_auto"):
            await self.server.status(
                "idle", "当前是 ZIP 安装，无法自动更新——请到 GitHub 下载新版本：{}".format(
                    self.latest["url"]))
            return
        await self.server.broadcast({"type": "updating"})
        # --untracked-files=no 是关键：未跟踪的文件 git pull 根本不会动它，
        # 拿它们挡住更新纯属误伤。真实案例：用户目录里多了一个 .run.log 和两个
        # 词表备份，自动更新就此彻底罢工，而给出的提示是「请自行处理后 git pull」
        # ——对一个不会用终端的人来说这是个死胡同。
        # 万一某个未跟踪文件真的和新版本里的文件重名，下面的 git pull 会自己
        # 报错，那时再把 git 的原话转述给用户。
        code, out, _ = await self._git("status", "--porcelain", "--untracked-files=no")
        if code != 0:
            await self.server.status(
                "idle", "这台电脑上找不到 git，程序没法自己更新。"
                        "可以到 GitHub 下载新版压缩包，或者装好 git 后执行：",
                command=self._manual_command())
            return
        if out.strip():
            # porcelain 是「两位状态 + 空格 + 文件名」，而未暂存修改的第一位
            # 就是空格——所以只能逐行去尾部空白，绝不能对整段 strip()，
            # 否则第一行会被削掉行首空格，文件名跟着少一个字母（app/ → pp/）。
            files = [ln[3:].rstrip() for ln in out.splitlines()[:4] if len(ln) > 3]
            await self.server.status(
                "idle",                      # 只是没更新，程序本身好好的，别报「出错了」
                "本次没有自动更新：这几个程序文件被改过，直接更新会覆盖掉它们——{}。"
                "如果不是你有意改的，复制下面这行到「终端」里执行，"
                "就能放弃这些改动并完成更新："
                .format("、".join(files) or "（若干文件）"),
                command=self._manual_command(discard=True))
            return
        code, _, err = await self._git("pull", "--ff-only")
        if code != 0:
            tail = err.strip().splitlines()[-2:]
            await self.server.status(
                "idle", "更新没能完成（{}）。复制下面这行到「终端」里执行通常就能解决："
                        .format(" / ".join(tail)),
                command=self._manual_command())
            return
        # 新版本可能带来新依赖——重启前先装上（失败不阻塞，重启后 bootstrap 兜底）。
        # 与 freshen_ytdlp 共用 _pip_lock：两个 pip 并发写环境会装出残缺的包，
        # execv 也不能在另一个 pip 半途时重启进程（孤儿 pip 会继续改写新环境）
        await self.server.status("connecting", "正在安装新版本的依赖…")
        async with self._get_pip_lock():
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install",
                    "--disable-pip-version-check", "-r", str(ROOT / "requirements.txt"),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except Exception:
                pass
            await self.server.status("idle", "更新完成，正在自动重启…")
            print("[信息] 已更新到最新版本，重启进程…")
            await asyncio.sleep(0.6)
            try:
                os.execv(sys.executable,
                         [sys.executable, str(ROOT / "main.py")] + sys.argv[1:])
            except Exception as exc:
                # execv 失败（极少见）不能让用户以为更新丢了——代码其实已经拉下来了
                await self.server.status(
                    "idle", "更新已下载完成，但自动重启失败（{}）——"
                            "请手动关掉再重新打开程序".format(exc))
