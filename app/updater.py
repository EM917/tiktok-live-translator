"""自动更新：启动时检查 GitHub 最新 release，UI 一键更新（git 安装）并自动重启。

安全边界：只做 fast-forward，且要求工作区干净——绝不覆盖用户的本地改动；
ZIP 下载（无 .git）的安装只提示去下载页，不尝试自动更新。

一键更新的顺序（见 Updater._apply_inner）：先 `git fetch` 取新版本（只动网络，监听
照常），确认能快进了才暂停监听；先按新版本的清单装依赖，装好了才合并代码并重启。
任何一步失败，工作区还是旧代码，监听在当前版本上恢复。
"""
import asyncio
import importlib.metadata
import importlib.util
import math
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .bootstrap import (MLX_GIVEUP, clear_mlx_giveup, is_apple_silicon, mlx_retry_due,
                        new_log_path, read_mlx_giveup, requirements_digest, write_mlx_giveup,
                        write_requirements_stamp)
from .settings import load_settings, save_setting

ROOT = Path(__file__).resolve().parent.parent
# 距上次提示不足这么久，就只静默刷新版本信息，不再弹醒目提示
QUIET_NOTICE_SEC = 6 * 3600

# 弹幕组件 TikTokLive 的最低版本。2026-09-14 同房间、同分钟、只换版本的配对实测：
# Euler 签名服务把连接派到备用推流线路时，7.0.0 把回传的路由参数原样拼进握手
# 地址，握手 HTTP 400；7.0.1（2026-09-10，上游 PR #377）做了编码，连上并收到评论。
TIKTOKLIVE_MIN = (7, 0, 1)
TIKTOKLIVE_SPEC = "TikTokLive>=7.0.1,<8"
# 评论连接被拒时找补丁版本的冷却：pip 要去问 PyPI，拒绝又往往连着来
TIKTOKLIVE_FRESHEN_COOLDOWN_SEC = 6 * 3600
# 更新检查失败（断网、PyPI 不通、pip 出错）后多久可以再试：失败不进六小时冷却，
# 否则网络一分钟后就恢复了，修复版本却要再等六小时才拿得到
TIKTOKLIVE_FRESHEN_RETRY_SEC = 600


def tiktoklive_version():
    """已安装的 TikTokLive 版本号；没装返回 None。只读包元数据、不 import 这个库
    （它要求 Python 3.10+，主进程不碰它，见 comment_source.py 模块说明）。"""
    importlib.invalidate_caches()        # pip 刚装完/升完，元数据查找的路径缓存可能是旧的
    try:
        return importlib.metadata.version("TikTokLive")
    except Exception:
        return None


def tiktoklive_outdated(version):
    """低于 TIKTOKLIVE_MIN 算过旧。认不出的版本号不算过旧——宁可不升，也不因为
    一个怪版本号反复拉 pip。"""
    if not version:
        return False
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", str(version))
    if not m:
        return False
    return tuple(int(x or 0) for x in m.groups()) < TIKTOKLIVE_MIN


def _version_tuple(version):
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", str(version or ""))
    return tuple(int(x or 0) for x in m.groups()) if m else None


async def _reap(proc):
    """kill 之后必须再 wait 一次去 reap，否则残留僵尸进程。"""
    try:
        proc.kill()
    except Exception:
        pass
    try:
        await proc.wait()
    except Exception:
        pass


def _open_pip_log(log_path, args):
    if not log_path:
        return None
    try:
        fh = open(str(log_path), "ab")
        fh.write("$ pip install {}  ({})\n".format(
            " ".join(str(a) for a in args),
            datetime.now().isoformat(timespec="seconds")).encode("utf-8"))
        fh.flush()        # 子进程接着往同一个文件追加，先把这行落下去
        return fh
    except OSError:
        return None


def _close_pip_log(fh, outcome):
    if fh is None:
        return
    try:
        fh.write("[pip 结束：{}]\n".format(outcome).encode("utf-8"))
        fh.close()
    except (OSError, ValueError):
        pass


async def _call_announce(announce):
    try:
        await announce()
    except Exception:
        pass


def _setting_float(key):
    try:
        return float(load_settings().get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


# 一键更新各步的时限。fetch 在监听照常进行时跑，卡住也只是这次不更新；合并和 pip
# 跑在监听暂停之后，必须有上限，超时就在当前版本上把监听恢复起来
FETCH_TIMEOUT_SEC = 90
GIT_LOCAL_TIMEOUT_SEC = 120
UPDATE_PIP_TIMEOUT_SEC = 600
# 空闲时后台补装 mlx-whisper：要下 torch 等几百 MB；一开始监听就撤
MLX_RETRY_TIMEOUT_SEC = 1800
PIP_ABORT_POLL_SEC = 2.0
PIP_ABORTED = "aborted"
# 带 curl-cffi 扩展：让 pip 按 yt-dlp 自己声明的范围装配 curl_cffi（与 requirements.txt 一致）
YTDLP_SPEC = "yt-dlp[curl-cffi]"
# 连续这么多天没能连上更新服务器，就在页脚版本号旁边提一句（不进自检、不进横幅）
UPDATE_CHECK_STALE_DAYS = 14


def component_version(dist):
    """已安装组件的版本号（只读包元数据，不 import）；没装返回 None。"""
    importlib.invalidate_caches()
    for name in dict.fromkeys((dist, dist.replace("-", "_"), dist.replace("_", "-"))):
        try:
            return importlib.metadata.version(name)
        except Exception:
            continue
    return None


def update_check_ok_iso(settings=None):
    """最近一次成功连上更新服务器的时间（ISO），从没成功过返回 None。"""
    settings = load_settings() if settings is None else settings
    try:
        ts = float(settings.get("update_check_ok_at") or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def update_check_note(settings=None, now=None):
    """连续 UPDATE_CHECK_STALE_DAYS 天没能连上更新服务器时，页脚上的那句话；否则 None。

    从上次成功算起；从没成功过的机器从第一次失败算起。只写观察到的：多少天、最近
    一次返回了什么。不进自检面板——连不上 GitHub 的网络上它会一直黄着，把中控训练成
    无视 WARN。"""
    settings = load_settings() if settings is None else settings
    err = settings.get("update_check_error")
    if not isinstance(err, dict):
        return None
    try:
        ok_at = float(settings.get("update_check_ok_at") or 0)
        since = ok_at or float(err.get("since") or 0)
    except (TypeError, ValueError):
        return None
    if since <= 0:
        return None
    now = time.time() if now is None else now
    days = int((now - since) // 86400)
    if days < UPDATE_CHECK_STALE_DAYS:
        return None
    return "已 {} 天没能连上更新服务器（最近一次：{}）".format(
        days, err.get("status_or_exc") or "未知")


def _git_tail(err):
    lines = [ln.strip() for ln in (err or "").strip().splitlines() if ln.strip()]
    return " / ".join(lines[-2:])


def _log_label(path):
    """给中控看的日志位置：项目目录下的相对路径。"""
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


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
        self._tiktoklive_freshen_task = None     # 在途的更新检查：并发的调用方跟着等同一次结果
        self._tiktoklive_announces = []          # 在途检查要通知的调用方（跟着等的也算）
        self._tiktoklive_checking = False        # 在途检查是否已经到了「真的去查」那一步
        self._tiktoklive_freshen_failed_at = 0.0
        self._pip_tasks = set()                  # 在跑的 pip 任务，保引用防 GC
        # freshen 与一键更新共用：pip 不能并发写环境。惰性初始化——
        # 3.9 的 asyncio.Lock() 构造时就要绑事件循环
        self._pip_lock = None
        # 管线提供的钩子（main.py 调 attach_pipeline 接上）：此刻有没有在监听、
        # 把组件升级写进当前这场审计、持续提示
        self._stream_active = None
        self._on_component_updated = None
        self._incident = None

    def attach_pipeline(self, pipeline):
        """main.py 在两者都建好后调用。后台装组件不能压在直播上，组件升级要进这场的审计。"""
        self._stream_active = getattr(pipeline, "_stream_active", None)
        self._on_component_updated = getattr(pipeline, "note_component_updated", None)
        self._incident = getattr(pipeline, "_incident", None)

    def _get_pip_lock(self):
        if self._pip_lock is None:
            self._pip_lock = asyncio.Lock()
        return self._pip_lock

    async def watch(self, first_delay=2.0, interval=6 * 3600):
        """启动后检查一次，之后每 6 小时复查——常开不关的用户也能及时看到新版本。"""
        await self._publish_check_health()     # 很久没连上更新服务器的话，页面一打开就能看到
        await self.check_and_notify(delay=first_delay)
        await self.freshen_ytdlp(reason="periodic")   # 顺带保鲜最易腐坏的组件
        await self.retry_mlx_install()
        while True:
            await asyncio.sleep(interval)
            if self.latest is None:      # 已经提示过就不再重复打扰
                await self.check_and_notify(delay=0)
            await self.freshen_ytdlp(reason="periodic")
            await self.retry_mlx_install()

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
                # 拿到锁之后再读「升级前」的版本：等锁期间别的 pip（一键更新）可能已经改过
                before = {name: component_version(name) for name in ("yt-dlp", "curl_cffi")}
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install", "-U",
                    "--disable-pip-version-check", YTDLP_SPEC,
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
            for name, was in before.items():
                self._note_component_update(name, was, component_version(name), reason)
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

    async def _pip_install(self, args, label, timeout=600, log_path=None, abort_if=None):
        """跑一次 `pip install <args>`，返回退出码；超时或异常返回 None；abort_if 叫停时
        返回 PIP_ABORTED。log_path 给了就把 pip 的输出写进去（默认丢弃）；abort_if 每
        PIP_ABORT_POLL_SEC 秒问一次，返回真就 kill 掉 pip（后台装大组件时一开始监听就撤）。

        pip 跑在独立任务里，从拿锁到 pip 退出都持有 pip 锁；调用方被取消（中控点停止、
        换房间、一键更新会先停直播）只结束「等」，不结束 pip，也不放锁。以前锁随调用方
        的取消一起释放，pip 却还在写环境：一键更新的 pip 紧接着拿到锁并发写同一个 venv，
        随后 execv 重启（2026-09-15 审查实测复现）。"""
        task = asyncio.ensure_future(
            self._pip_install_locked(args, label, timeout, log_path, abort_if))
        self._pip_tasks.add(task)
        task.add_done_callback(self._pip_tasks.discard)
        return await asyncio.shield(task)

    async def _pip_install_locked(self, args, label, timeout, log_path=None, abort_if=None):
        """与 freshen_ytdlp、一键更新共用 pip 锁（pip 不能并发写环境）。超时 kill 之后
        必须再 wait 一次去 reap，否则残留僵尸进程。"""
        async with self._get_pip_lock():
            if abort_if is not None and abort_if():
                return PIP_ABORTED       # 等锁的这段时间里情况变了：不开始
            log = _open_pip_log(log_path, args)
            outcome = "异常"
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install",
                    "--disable-pip-version-check", *args,
                    stdout=log if log is not None else asyncio.subprocess.DEVNULL,
                    stderr=(asyncio.subprocess.STDOUT if log is not None
                            else asyncio.subprocess.DEVNULL),
                )
                try:
                    if abort_if is None:
                        await asyncio.wait_for(proc.wait(), timeout=timeout)
                    elif await self._wait_or_abort(proc, timeout, abort_if):
                        outcome = "被叫停"
                        print("[信息] {}：已停下".format(label))
                        return PIP_ABORTED
                except asyncio.TimeoutError:
                    await _reap(proc)
                    outcome = "超时"
                    print("[警告] {}超时，已放弃本次尝试".format(label))
                    return None
                outcome = "返回 {}".format(proc.returncode)
            except Exception as exc:
                print("[警告] {}异常: {}".format(label, exc))
                return None
            finally:
                _close_pip_log(log, outcome)
        importlib.invalidate_caches()   # pip 刚改完环境，find_spec/元数据的路径缓存可能是旧的
        return proc.returncode

    @staticmethod
    async def _wait_or_abort(proc, timeout, abort_if):
        """等 pip 结束，每 PIP_ABORT_POLL_SEC 秒问一次 abort_if。被叫停返回 True（pip 已 kill
        并 reap）；总时长用完抛 TimeoutError。按轮数计时，测试换掉 wait_for 就不用真等。"""
        rounds = max(1, int(math.ceil(float(timeout) / PIP_ABORT_POLL_SEC)))
        for _ in range(rounds):
            try:
                await asyncio.wait_for(proc.wait(), timeout=PIP_ABORT_POLL_SEC)
                return False
            except asyncio.TimeoutError:
                try:
                    stop = abort_if()
                except Exception:
                    stop = True
                if stop:
                    await _reap(proc)
                    return True
        raise asyncio.TimeoutError()

    def _busy_for_background_pip(self):
        """后台装大组件之前和装的过程中反复问：在监听、或在一键更新，就不装。
        判断不了（管线钩子没接上、钩子自己出错）按「在忙」算。"""
        if getattr(self, "_applying", False):
            return True
        active = getattr(self, "_stream_active", None)
        if active is None:
            return True
        try:
            return bool(active())
        except Exception:
            return True

    async def _persistent_note(self, key, level, text):
        hook = getattr(self, "_incident", None)
        if hook is not None:
            await hook(key, level, text)
        else:
            await self._notice(text)

    def _note_component_update(self, name, before, after, reason):
        """组件版本真的变了：打一行，并交给管线写进当前这场的审计（有的话）。"""
        if not after or after == before:
            return
        print("[信息] 组件 {} 已从 {} 更新到 {}（{}）".format(name, before, after, reason))
        hook = getattr(self, "_on_component_updated", None)
        if hook is None:
            return
        try:
            hook(name, before, after, reason)
        except Exception as exc:
            print("[警告] 组件更新没能写进审计: {}".format(exc))

    async def retry_mlx_install(self, now=None):
        """首次安装时没装上 mlx-whisper（留了放弃记号）的 Apple Silicon 机器：没在监听时，
        距上次失败满 24 小时就在后台再装一次。返回结果字符串（测试用）。

        不在启动时（ensure_env）重试：torch 等要下几百 MB，会在中控正要开播时把启动挡住
        几分钟。一开始监听（或点了一键更新）就 kill 掉 pip：CPU 识别本来只是勉强跟得上，
        再压一个安装上去就会积压丢段。已装的 numpy 钉在当前版本——这个进程正用着它。"""
        if not is_apple_silicon():
            return "not-applicable"
        marker = ROOT / ".venv" / MLX_GIVEUP
        info = read_mlx_giveup(marker)
        if info is None:
            return "no-marker"
        importlib.invalidate_caches()
        if importlib.util.find_spec("mlx_whisper") is not None:
            clear_mlx_giveup(marker)          # 已经装上了（比如手动装过），记号过时
            return "present"
        if not mlx_retry_due(info, now=now):
            return "cooldown"
        if self._busy_for_background_pip():
            return "busy"
        args = ["mlx-whisper"]
        numpy_version = component_version("numpy")
        if numpy_version:
            args.append("numpy=={}".format(numpy_version))
        code = await self._pip_install(
            args, "GPU 加速组件 mlx-whisper 后台安装", timeout=MLX_RETRY_TIMEOUT_SEC,
            log_path=new_log_path(ROOT, "mlx-retry"), abort_if=self._busy_for_background_pip)
        if code == PIP_ABORTED:
            print("[信息] 开始监听或更新了，GPU 加速组件的后台安装已停下，下次空闲时再试")
            return "aborted"
        importlib.invalidate_caches()
        if code == 0 and importlib.util.find_spec("mlx_whisper") is not None:
            clear_mlx_giveup(marker)
            await self._persistent_note("mlx-ready", "info",
                                        "GPU 加速组件已装好，下次停播后重开程序生效")
            return "installed"
        write_mlx_giveup(marker, code if isinstance(code, int) else None,
                         "后台安装没成功" + ("（超时）" if code is None else ""))
        print("[警告] GPU 加速组件 mlx-whisper 后台安装没成功（pip 返回 {}），"
              "24 小时后空闲时再试".format(code))
        return "failed"

    async def _pip_capture(self, args, timeout=120):
        """跑一次不改环境的 pip 命令（不拿 pip 锁），返回 (退出码, stdout, stderr)；
        超时或起不来返回 (None, "", 原因)。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except Exception as exc:
            return None, "", str(exc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return None, "", "timeout"
        return (proc.returncode, out.decode("utf-8", errors="replace"),
                err.decode("utf-8", errors="replace"))

    async def _tiktoklive_index_versions(self):
        """问包索引：7.x 里最新的正式版 TikTokLive 是哪个。返回 (reachable, latest)。

        必须先问索引再决定：索引连不上时 `pip install -U` 会退回已装的版本并返回 0，
        看起来和「已是最新」一模一样（2026-09-15 复查实测：重试 5 次后退出码 0）。
        reachable=False 表示连不上；pip 太旧不认识 index 子命令时返回 (None, None)，
        调用方退回直接 install。"""
        code, out, err = await self._pip_capture(
            ["index", "versions", "TikTokLive", "--disable-pip-version-check"])
        if code is None:
            return False, None
        if code != 0:
            low = err.lower()
            if "unknown command" in low or "no such command" in low:
                return None, None
            return False, None
        m = re.search(r"Available versions:\s*(.+)", out)
        versions = [v.strip() for v in m.group(1).split(",")] if m else []
        for v in versions:          # pip 按新到旧列出；只认 7.x 正式版（与 TIKTOKLIVE_SPEC 一致）
            if re.fullmatch(r"7(\.\d+){1,2}", v):
                return True, v
        return True, None

    async def ensure_tiktoklive(self, reason="startup"):
        """按需安装弹幕组件 TikTokLive（可选依赖，仅 comment_worker.py 子进程用）。

        仿 freshen_ytdlp：装过/装不了就别反复折腾 pip。与 freshen_ytdlp 不同的是
        这里的「一小时内不重试」持久化进 settings.json——弹幕来源的供给回调
        (`CommentSource.on_provision`) 可能在同一场直播里被反复触发（每次
        `worker_available()` 仍为假），不能靠内存里的时间戳，重启后又能立刻
        重试是好事，但同一次运行内不该每次都拉一次 pip。

        已经装了、但低于 TIKTOKLIVE_MIN 的也在这里升。2026-09-14 实录：这里以前
        只看「装没装」，老安装一直停在 7.0.0；评论服务改走备用线路后每次连接都是
        HTTP 400，而修好它的 7.0.1 四天前就已发布。升级失败不改变返回值：旧版本
        仍然装着，签名服务给的是 TikTok 真实地址时它照样能连。

        Python < 3.10 直接放弃——TikTokLive 7.x 要求 3.10+，这是本项目唯一
        允许高于 3.9 的可选组件，装不上不影响主链路（弹幕来源据此转成
        `unavailable` 状态，见 comment_source.py 的 _supervise）。
        """
        if sys.version_info < (3, 10):
            return False
        if importlib.util.find_spec("TikTokLive") is not None:
            current = tiktoklive_version()
            if tiktoklive_outdated(current):
                await self._upgrade_outdated_tiktoklive(current, reason)
            return True
        now = time.time()
        if now - _setting_float("tiktoklive_install_attempted_at") < 3600:
            return False
        save_setting("tiktoklive_install_attempted_at", now)
        code = await self._pip_install(
            [TIKTOKLIVE_SPEC], "弹幕组件 TikTokLive 安装（{}）".format(reason))
        if code is None:
            return False
        if code == 0 and importlib.util.find_spec("TikTokLive") is not None:
            print("[信息] 已安装弹幕组件 TikTokLive（{}）".format(reason))
            return True
        print("[警告] 弹幕组件 TikTokLive 安装失败（pip 返回 {}，{}）".format(code, reason))
        return False

    async def _upgrade_outdated_tiktoklive(self, current, reason):
        """把低于最低版本的 TikTokLive 升上去。一小时冷却持久化，理由同安装。"""
        now = time.time()
        if now - _setting_float("tiktoklive_upgrade_attempted_at") < 3600:
            print("[信息] 弹幕组件 TikTokLive {} 低于最低版本；一小时内已试过升级，这次跳过（{}）"
                  .format(current, reason))
            return False
        save_setting("tiktoklive_upgrade_attempted_at", now)
        need = ".".join(str(x) for x in TIKTOKLIVE_MIN)
        print("[信息] 弹幕组件 TikTokLive {} 低于 {}，后台升级中（{}）".format(current, need, reason))
        code = await self._pip_install(
            [TIKTOKLIVE_SPEC], "弹幕组件 TikTokLive 升级（{}）".format(reason))
        after = tiktoklive_version()
        if code == 0 and after and not tiktoklive_outdated(after):
            print("[信息] 弹幕组件 TikTokLive 已从 {} 升级到 {}".format(current, after))
            return True
        print("[警告] 弹幕组件 TikTokLive 没能升级（pip 返回 {}，当前 {}），先继续用现有版本"
              .format(code, after))
        return False

    async def freshen_tiktoklive(self, reason="comment-rejected", announce=None):
        """评论连接被服务端拒绝时，看 TikTokLive 有没有补丁版本，有就升。

        和「解析连续失败就升 yt-dlp」是同一个思路：对接的接口一变，修复通常先出现
        在上游的补丁版本里（2026-09-10 的 7.0.1 就是），而老安装从来不会自己去拿。
        只在 7.x 之内升（TIKTOKLIVE_SPEC 限定 <8），依赖只在必要时才动。

        并发的调用方跟着等同一次检查（不重复拉 pip，也不会在检查还没完时误判
        「没有更新」）。announce 是可选的协程函数：只有真的去查时才调用；跟着等的
        调用方也会被通知，它的面板同样说「正在检查」。

        返回 {"outcome": ..., "before": ..., "after": ...}，outcome 取值：
          upgraded / no-update / pip-failed / cooldown / recently-failed /
          not-installed / unsupported。"""
        task = self._tiktoklive_freshen_task
        if task is None or task.done():
            self._tiktoklive_announces = [announce] if announce is not None else []
            self._tiktoklive_checking = False
            task = asyncio.ensure_future(self._freshen_tiktoklive(reason))
            self._tiktoklive_freshen_task = task
        elif announce is not None:
            if self._tiktoklive_checking:
                await _call_announce(announce)          # 已经在查了：马上告诉这位
            else:
                self._tiktoklive_announces.append(announce)   # 还没到那一步：到时一起通知
        return await asyncio.shield(task)

    async def _freshen_tiktoklive(self, reason):
        if sys.version_info < (3, 10):
            return {"outcome": "unsupported"}
        before = tiktoklive_version()
        if before is None:
            return {"outcome": "not-installed"}
        now = time.time()
        # 冷却只记「检查成功完成」的那一次；失败走下面的短重试间隔
        if now - _setting_float("tiktoklive_freshen_at") < TIKTOKLIVE_FRESHEN_COOLDOWN_SEC:
            return {"outcome": "cooldown", "before": before}
        if now - self._tiktoklive_freshen_failed_at < TIKTOKLIVE_FRESHEN_RETRY_SEC:
            return {"outcome": "recently-failed", "before": before}
        try:
            self._tiktoklive_checking = True
            for a in list(self._tiktoklive_announces):
                await _call_announce(a)
            reachable, latest = await self._tiktoklive_index_versions()
            if reachable is False:
                self._tiktoklive_freshen_failed_at = time.time()
                print("[警告] 弹幕组件 TikTokLive 更新检查没成功：包索引连不上（当前 {}），{} 分钟后可再试（{}）"
                      .format(before, TIKTOKLIVE_FRESHEN_RETRY_SEC // 60, reason))
                return {"outcome": "pip-failed", "before": before, "after": before,
                        "code": "index-unreachable"}
            # 只有索引明确给出了 7.x 最新版、且不比已装的新，才跳过 install；
            # 索引通了但读不出版本（格式变了）就交给 pip 自己判断
            if reachable and latest is not None and \
                    (_version_tuple(latest) or ()) <= (_version_tuple(before) or ()):
                save_setting("tiktoklive_freshen_at", time.time())
                print("[信息] 弹幕组件 TikTokLive {} 已是可用的最新版本（索引最新 7.x：{}，{}）"
                      .format(before, latest, reason))
                return {"outcome": "no-update", "before": before, "after": before}
            code = await self._pip_install(
                ["-U", "--upgrade-strategy", "only-if-needed", TIKTOKLIVE_SPEC],
                "弹幕组件 TikTokLive 更新检查（{}）".format(reason))
            after = tiktoklive_version()
            if code != 0:
                self._tiktoklive_freshen_failed_at = time.time()
                print("[警告] 弹幕组件 TikTokLive 更新检查没成功（pip 返回 {}，当前 {}），{} 分钟后可再试（{}）"
                      .format(code, after, TIKTOKLIVE_FRESHEN_RETRY_SEC // 60, reason))
                return {"outcome": "pip-failed", "before": before, "after": after, "code": code}
            save_setting("tiktoklive_freshen_at", time.time())
            if after and after != before:
                print("[信息] 已自动更新弹幕组件 TikTokLive：{} 更新到 {}（{}）".format(before, after, reason))
                return {"outcome": "upgraded", "before": before, "after": after}
            print("[信息] 弹幕组件 TikTokLive {} 已是可用的最新版本（{}）".format(after, reason))
            return {"outcome": "no-update", "before": before, "after": after}
        finally:
            self._tiktoklive_checking = False

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
                        await self._record_check("HTTP {}".format(resp.status))
                        if manual:
                            await self._notice(
                                "检查更新失败（GitHub 返回 {}）".format(resp.status))
                        return
                    data = await resp.json()
        except Exception as exc:
            await self._record_check(
                "超时" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__)
            if manual:
                await self._notice("检查更新失败（网络不可达）")
            return
        await self._record_check(None)
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

    async def _record_check(self, error):
        """记下这次检查更新的结果（手动、自动都记）。成功：记时间、清掉错误；失败：记
        {at, since, status_or_exc}，since 是这一串连续失败里第一次的时间。以前失败一律
        无声，settings 里也没有成功时间，一台机器几个月连不上更新服务器也没人知道。"""
        try:
            now = time.time()
            if error is None:
                save_setting("update_check_ok_at", now)
                if load_settings().get("update_check_error") is not None:
                    save_setting("update_check_error", None)
            else:
                prev = load_settings().get("update_check_error")
                since = prev.get("since") if isinstance(prev, dict) else None
                save_setting("update_check_error", {"at": now, "since": since or now,
                                                    "status_or_exc": str(error)[:80]})
            await self._publish_check_health()
        except Exception as exc:
            print("[警告] 记录更新检查结果失败: {}".format(exc))

    async def _publish_check_health(self):
        """页脚版本号旁边那句「已 N 天没能连上更新服务器」。存进 config（刷新页面还在），
        没变化就不广播。"""
        server = self.server
        config = getattr(server, "config", None)
        if server is None or not isinstance(config, dict):
            return
        note = update_check_note()
        value = {"note": note} if note else None
        if config.get("update_check") == value:
            return
        await server.broadcast({"type": "config", "update_check": value})

    async def _git(self, *args, timeout=GIT_LOCAL_TIMEOUT_SEC):
        """跑一条 git，返回 (退出码, stdout, stderr)；起不来返回 (None, "", 原因)，超时
        返回 (None, "", "timeout")。

        GIT_TERMINAL_PROMPT=0：要凭据时直接失败，不在 Start.command 的终端里等人输入；
        http.lowSpeedLimit/lowSpeedTime：连着但 30 秒里每秒不到 1 KB 就放弃。以前
        `git pull` 没有任何时限：网络卡住时监听早已停下，界面还显示「直播中」。"""
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=30", *args,
                cwd=str(ROOT), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            return None, "", str(exc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _reap(proc)
            return None, "", "timeout"
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def apply(self, live=None, pause=None, resume=None, before_restart=None):
        """一键更新，成功后原地重启进程。

        管线传入的钩子（见 Pipeline._apply_update），都可以不传：
          live()                       此刻有没有在监听；
          await pause(from_v, to_v)    新版本取下来、确认能快进之后才调：停监听，返回
                                       恢复监听要用的信息（没在监听返回 None）；
          await resume(token, text)    停了监听之后更新没成：在当前版本上恢复监听并说明；
          before_restart(token, to_v)  重启前留下「重启后接着监听」的记号。"""
        if self.latest is None or self._applying:
            return   # 重复点击「一键更新」不能并发跑两次 git/pip
        self._applying = True
        try:
            await self._apply_inner(live=live, pause=pause, resume=resume,
                                    before_restart=before_restart)
        finally:
            self._applying = False

    def _manual_command(self, discard=False, pip=False):
        """一条可以原样粘进终端的完整命令，路径是这台机器上的真实路径。

        不写成「请自行 git pull」——那句话假设用户知道项目在哪、知道要先 cd。
        真实案例里，一台机器就因为这句话卡在旧版本上，而它需要的正是能修好
        这个判断的那次更新。"""
        path = str(ROOT)
        if discard:
            return 'cd "{}" && git checkout -- . && git pull --ff-only'.format(path)
        if pip:
            return 'cd "{}" && git pull --ff-only && "{}" -m pip install -r requirements.txt'.format(
                path, sys.executable)
        return 'cd "{}" && git pull --ff-only'.format(path)

    async def precheck(self):
        """「能不能更」与「更」分开：调用方先问这个，通过了才停直播管线。
        以前是先停管线再查工作区，被脏文件挡住时直播已经没人听了，界面上
        只剩一句「本次没有自动更新」。这里的每条失败出口都自带可照做的命令。"""
        if self.latest is None:
            return False
        if not self.latest.get("can_auto"):
            await self.server.status(
                "idle", "当前是 ZIP 安装，无法自动更新——请到 GitHub 下载新版本：{}".format(
                    self.latest["url"]))
            return False
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
            return False
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
            return False
        return True

    async def _not_updated(self, text, live):
        """还没停监听时的失败出口。直播中只发一次性提示——监听照常，不能把界面状态打回
        待机；没在监听时照旧给状态和一条能照做的命令（更新器不能把自己锁死）。"""
        print("[警告] 一键更新没完成：{}（手动更新：{}）".format(text, self._manual_command()))
        await self.server.broadcast({"type": "update_aborted"})
        if live:
            await self._notice("更新没完成：{}。监听没有中断，停播后可以再点「一键更新」".format(text))
            return
        await self.server.status(
            "idle", "更新没能完成（{}）。复制下面这行到「终端」里执行通常就能解决：".format(text),
            command=self._manual_command())

    async def _update_failed(self, text, token, resume, command):
        """装依赖或合并失败的出口。工作区还是旧代码：停了的监听在当前版本上恢复，
        没在监听的给状态和手动命令。"""
        print("[警告] " + text)
        await self.server.broadcast({"type": "update_aborted"})
        if token is not None and resume is not None:
            await resume(token, text)
            return
        await self.server.status(
            "idle", text + "。复制下面这行到「终端」里执行可以手动更新：", command=command)

    async def _install_new_requirements(self, requirements, log_path):
        """合并之前，按新版本的清单装依赖（清单来自 `git show`，写成临时文件给 pip -r）。
        清单是空的就不用装，返回 0。"""
        if not requirements.strip():
            return 0
        fd, tmp = tempfile.mkstemp(prefix="tlt-requirements-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(requirements)
            return await self._pip_install(["-r", tmp], "新版本依赖安装",
                                           timeout=UPDATE_PIP_TIMEOUT_SEC, log_path=log_path)
        except OSError as exc:
            print("[警告] 写临时组件清单失败: {}".format(exc))
            return None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    async def _apply_inner(self, live=None, pause=None, resume=None, before_restart=None):
        if not await self.precheck():
            return

        def is_live():
            try:
                return bool(live()) if live is not None else False
            except Exception:
                return False

        await self.server.broadcast({"type": "updating"})
        from_version = local_version()

        # 1. 取新版本：只动网络和 .git，工作区不变，监听照常
        code, _, err = await self._git("fetch", timeout=FETCH_TIMEOUT_SEC)
        if code != 0:
            why = ("{} 秒内没下载完".format(FETCH_TIMEOUT_SEC) if err == "timeout"
                   else _git_tail(err) or "git 返回 {}".format(code))
            await self._not_updated("没能从 GitHub 取到新版本（{}）".format(why), is_live())
            return
        # 与 `git pull` 同一个目标：当前分支跟踪的远端分支。解析成提交号，后面几步都认
        # 这一个提交（本地实测：分离 HEAD 时 FETCH_HEAD 全是 not-for-merge，靠不住）
        code, target, err = await self._git("rev-parse", "--verify", "@{u}")
        target = target.strip()
        if code != 0 or not target:
            await self._not_updated("找不到这份安装跟踪的远端分支（{}）".format(
                _git_tail(err) or "git 返回 {}".format(code)), is_live())
            return
        _, version_text, _ = await self._git("show", "{}:VERSION".format(target))
        to_version = version_text.strip() or "?"
        code, requirements, err = await self._git("show", "{}:requirements.txt".format(target))
        if code != 0:
            await self._not_updated("读不到新版本的组件清单（{}）".format(
                _git_tail(err) or "git 返回 {}".format(code)), is_live())
            return
        code, _, err = await self._git("merge-base", "--is-ancestor", "HEAD", target)
        if code != 0:
            await self._not_updated("本地代码没法直接快进到新版本（{}）".format(
                _git_tail(err) or "git 返回 {}".format(code)), is_live())
            return
        if self._get_pip_lock().locked():
            await self._not_updated("后台正在安装别的组件，等它装完再点「一键更新」", is_live())
            return

        # 2. 确认能更新了，才停监听
        token = await pause(from_version, to_version) if pause is not None else None
        if token is None:
            await self.server.status("connecting", "正在安装新版本的依赖…")

        # 3. 先按新版本的清单装依赖，装好了才合并：新代码绝不在缺依赖的环境里落地。
        # 以前是先 pull 再装、不看 pip 的退出码，装失败也照样重启进新代码
        log_path = new_log_path(ROOT, "update")
        code = await self._install_new_requirements(requirements, log_path)
        if code != 0:
            text = "新版本需要的组件没装上（pip 返回 {}{}），继续使用当前版本 v{}".format(
                "超时" if code is None else code,
                "，详情见 " + _log_label(log_path) if log_path else "", from_version)
            await self._update_failed(text, token, resume, self._manual_command(pip=True))
            return

        exec_error = None
        # 合并到重启之间不许别的 pip 插进来改环境：execv 不能在另一个 pip 半途时重启进程
        async with self._get_pip_lock():
            code, _, err = await self._git("merge", "--ff-only", target)
            if code == 0:
                if requirements.strip():
                    write_requirements_stamp(ROOT, requirements_digest(requirements))
                if token is not None and before_restart is not None:
                    before_restart(token, to_version)
                await self.server.status(
                    "connecting" if token is not None else "idle",
                    "更新完成，正在自动重启…" + ("重启后自动恢复监听" if token is not None else ""))
                print("[信息] 已更新到最新版本，重启进程…")
                await asyncio.sleep(0.6)
                try:
                    from .relaunch import exec_args
                    os.execv(sys.executable, exec_args(
                        [sys.executable, str(ROOT / "main.py")] + sys.argv[1:]))
                except Exception as exc:
                    exec_error = exc
        if code != 0:
            text = "新版本的代码没能合并（{}），继续使用当前版本 v{}".format(
                _git_tail(err) or "git 返回 {}".format(code), from_version)
            await self._update_failed(text, token, resume, self._manual_command())
            return
        if exec_error is None:
            return          # 真的 execv 不会回到这里
        # execv 失败（极少见）：代码已经是新的，这个进程还是旧的——不能让用户以为更新丢了
        text = "更新已下载完成，但自动重启失败（{}）——请手动关掉再重新打开程序".format(exec_error)
        print("[警告] " + text)
        await self.server.broadcast({"type": "update_aborted"})
        if token is not None and resume is not None:
            await resume(token, text)
        else:
            await self.server.status("idle", text)
