# i18n: done
"""一键更新之后的存活确认，新版本起不来就自动退回更新前的版本（专家审查 I6；用户 2026-10-22 定 D10
「自动退回更新前的版本」）。

以前一键更新合并完新代码就直接 execv：新版本要是一启动就崩，暂停中的直播不会恢复，也回不到旧版，
只能等人手动处理。现在分三步：

  arm()        一键更新合并成功、execv 之前（app/updater.py）：在 settings.json 里留一个记号
               update_guard = {from, to, from_version, to_version, at, boots: 0, resume}。from/to 是
               更新前后的提交号；resume 是那一刻「重启后接着监听」记号的副本（见 on_boot）；
  on_boot()    新版本每次真正启动时（main.py，确认没有别的实例在跑之后）：boots 加 1。boots 到 2
               还没被清掉，说明新版本上一次启动之后本机页面一直没连上——这一次就不再跑新代码：
               git reset --keep 回到 from，清掉记号，交回一句对话框文字，由 main.py 弹窗并重新 exec；
  mark_ready() 本机页面第一次连上控制服务时（app/server.py）：清掉记号，新版本算是能用了。不挂在
               「重启后接着监听」之后：没在监听时根本走不到那一步，记号就永远清不掉了。

不退回的几种情形（D10：「双击两次、强行退出不算；程序文件夹被手动改过就不退」，计划审查 P5）：
- 别的实例在跑。main.py 只在单实例检查之后调 on_boot；同时启动的两个进程，单实例检查都看不见
  对方，这里再加一把进程锁：拿到锁的那个进程一直拿着，直到退出（崩溃时系统自动放锁）。拿不到锁
  就什么都不做，不计数；
- 强退：页面连上之后记号就清了，之后强退不算数；
- HEAD 已经不是 to：有人手动换过版本，记号作废；
- 工作区有改动（--untracked-files=no，和一键更新的预检同一个口径）：不碰，记号作废；
- git 不可用、reset 失败：记号作废，照常启动新版本。

只依赖标准库。git 调用都有时限。对话框文字在这里（L()），弹不弹、怎么弹由 main.py 决定；终端行永远
是中文。
"""
import errno
import hashlib
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .i18n import L
from .settings import load_settings, save_setting

ROOT = Path(__file__).resolve().parent.parent
KEY = "update_guard"
RESUME_KEY = "resume_after_update"   # app/pipeline.py 的「重启后接着监听」记号
# 启动计数到这个数、记号还在（新版本上一次启动之后页面一直没连上），就退回（D10）。第 1 次是一键更新
# 自己的 execv——新版本真正跑的只有这一次；第 2 次是中控再打开程序：这一次不再跑新代码，直接退回。
# 和 D10 字面「连续两次启动页面都没连上」的差别：新版本只跑过一次（计划写的就是 boots ≥ 2）。所以新版本
# 在页面连上之前被强退一次，下一次启动也会退回——「强退不算」只管页面连上之后
ROLLBACK_AT_BOOTS = 2
GIT_TIMEOUT_SEC = 30
# 记号里的 from/to 是完整的提交号（SHA-1 40 位，SHA-256 仓库 64 位）。from 会原样交给 git reset --keep：
# 不认得的（比如以 - 开头，会被 git 当成选项）整个记号作废，不碰 git
_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# 本进程的状态：on_boot 计过数（这个进程就是被看着的那个新版本）才算 armed；锁文件的句柄一直拿着
_state = {"armed": None, "lock": None}


class BootOutcome:
    """on_boot 的结果。kind：none / invalid / other-instance / counted / moved / dirty / git-failed /
    reset-failed / rolled-back。rolled_back 时 message 是给对话框的那句话（L()）。"""

    def __init__(self, kind, message=None):
        self.kind = kind
        self.message = message

    @property
    def rolled_back(self):
        return self.kind == "rolled-back"

    def __repr__(self):
        return "BootOutcome({!r})".format(self.kind)


def arm(from_commit, to_commit, from_version, to_version, now=None):
    """一键更新合并成功、execv 之前调用：留下记号。同时把「重启后接着监听」记号抄一份进去——新版本
    第一次启动时可能已经读掉了它（读到即删）、然后才崩，退回旧版本时要把它还回去，暂停中的监听才接得上
    （过没过期仍由旧版本按原来的 at 判断）。"""
    resume = load_settings().get(RESUME_KEY)
    save_setting(KEY, {"from": str(from_commit), "to": str(to_commit),
                       "from_version": str(from_version or ""), "to_version": str(to_version or ""),
                       "at": time.time() if now is None else now, "boots": 0,
                       "resume": resume if isinstance(resume, dict) else None})


def disarm():
    if load_settings().get(KEY) is not None:
        save_setting(KEY, None)


def _read():
    """读记号：没有返回 None；内容不对（不是字典、from/to 不是完整的提交号）返回 False。"""
    guard = load_settings().get(KEY)
    if guard is None:
        return None
    if not isinstance(guard, dict):
        return False
    for key in ("from", "to"):
        if not isinstance(guard.get(key), str) or not _COMMIT_RE.fullmatch(guard[key]):
            return False
    return guard


def _lock_path(root):
    """锁文件放在系统临时目录（每个用户各一份），按程序目录区分：不在程序文件夹里多留一个文件。"""
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "tlt-update-guard-{}.lock".format(digest)


def _take_lock(root):
    """拿本进程的锁，拿到返回 True。一直拿着：进程退出或崩溃时系统自动放掉。
    拿锁这件事本身出错（临时目录写不了）也返回 True：锁只防同时启动的第二个进程，没有它照旧按单实例
    检查的结果走，不能因此卡住启动。"""
    if _state["lock"] is not None:
        return True
    path = _lock_path(root)
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return True
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        # 被别的进程拿着：POSIX 是 EWOULDBLOCK/EAGAIN，Windows 的 msvcrt 报 EACCES 或 EDEADLK。
        # 别的错（文件系统不支持锁）当作没有锁这道保险，照常往下走
        return os.name != "nt" and exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)
    _state["lock"] = fd
    return True


def release_lock():
    """重新 exec 之前放锁（Windows 上起子进程接着跑时，父进程还活着）。POSIX 上 exec 会关掉这个句柄。"""
    fd = _state["lock"]
    _state["lock"] = None
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _git(root, *args):
    """跑一条 git，返回 (退出码, stdout)；起不来、超时返回 (None, "")。不等凭据。"""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    try:
        r = subprocess.run(["git", *args], cwd=str(root), env=env, capture_output=True,
                           timeout=GIT_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return r.returncode, r.stdout.decode("utf-8", errors="replace")


def _short(commit):
    return str(commit or "")[:8]


def rollback_message(from_version, to_version):
    """退回之后弹的那句话。只写看到的（页面没连上）和程序做了什么（退回、重新打开），不猜为什么。"""
    return L("一键更新到 v{} 之后，新版本上次启动时字幕界面一直没有连上。这次启动已自动退回更新前的 "
             "v{}，程序会重新打开。",
             "After the update to v{}, the caption window never connected the last time the new "
             "version started. On this launch, the app went back to v{}, the version before the "
             "update, and is opening again.").format(
        str(to_version or "?").lstrip("vV"), str(from_version or "?").lstrip("vV"))


def on_boot(root=None, others_running=None, git=None):
    """新版本启动时调用（main.py，确认没有别的实例在跑之后、起服务之前）。见模块说明。

    others_running：可选，返回 True 表示端口上已经有本程序在跑（--no-open、--url 两种模式不做单实例
    检查，由调用方补一次探测）。git：测试换替身用，签名同 _git。"""
    root = ROOT if root is None else root
    git = _git if git is None else git
    guard = _read()
    if guard is None:
        return BootOutcome("none")
    if guard is False:
        disarm()
        print("[警告] 一键更新留下的启动检查记号内容不对，已清掉")
        return BootOutcome("invalid")
    if not _take_lock(root):
        print("[信息] 另一个实例正在启动：这次启动不计入一键更新后的启动检查")
        return BootOutcome("other-instance")
    if others_running is not None:
        try:
            busy = bool(others_running())
        except Exception:
            busy = False
        if busy:
            release_lock()
            print("[信息] 端口上已有本程序在跑：这次启动不计入一键更新后的启动检查")
            return BootOutcome("other-instance")
    outcome = _count_or_roll_back(root, guard, git)
    if outcome.kind != "counted" and not outcome.rolled_back:
        release_lock()           # 记号已经作废：照常启动，这把锁没用了
    return outcome


def _count_or_roll_back(root, guard, git):
    try:
        boots = int(guard.get("boots") or 0) + 1
    except (TypeError, ValueError):
        boots = 1
    to_version = guard.get("to_version") or "?"
    from_version = guard.get("from_version") or "?"
    if boots < ROLLBACK_AT_BOOTS:
        save_setting(KEY, dict(guard, boots=boots))
        _state["armed"] = (guard.get("to"), guard.get("at"))
        print("[信息] 一键更新到 v{} 之后第 {} 次启动：字幕界面连上之后就算更新成功".format(
            to_version, boots))
        return BootOutcome("counted")
    # 新版本上一次启动之后页面一直没连上：先确认工作区还是一键更新留下的样子，才退回
    code, head = git(root, "rev-parse", "HEAD")
    if code is None or code != 0:
        disarm()
        print("[警告] 一键更新到 v{} 之后页面一直没连上，但读不到当前的提交号（git 返回 {}），"
              "没有自动退回".format(to_version, code))
        return BootOutcome("git-failed")
    if head.strip() != guard["to"]:
        disarm()
        print("[信息] 程序目录已经不是一键更新到的那个提交（现在 {}，更新到的是 {}），不自动退回".format(
            _short(head.strip()), _short(guard["to"])))
        return BootOutcome("moved")
    code, dirty = git(root, "status", "--porcelain", "--untracked-files=no")
    if code is None or code != 0:
        disarm()
        print("[警告] 一键更新到 v{} 之后页面一直没连上，但查不了程序文件有没有改动（git 返回 {}），"
              "没有自动退回".format(to_version, code))
        return BootOutcome("git-failed")
    if dirty.strip():
        disarm()
        print("[警告] 一键更新到 v{} 之后页面一直没连上，但程序文件夹里有改动过的文件，没有自动退回".format(
            to_version))
        return BootOutcome("dirty")
    code, _ = git(root, "reset", "--keep", guard["from"])
    if code != 0:
        disarm()
        print("[警告] 一键更新到 v{} 之后页面一直没连上，退回 v{}（{}）没成功（git 返回 {}），照常启动".format(
            to_version, from_version, _short(guard["from"]), code))
        return BootOutcome("reset-failed")
    # 第一次启动可能已经读掉了「重启后接着监听」记号（读到即删）、然后才出事：还回去，旧版本照原来的
    # 时间判断过没过期。记号里注明是退回来的（rolled_back_from）：旧版本恢复监听时状态行不能说「已更新到」
    resume = load_settings().get(RESUME_KEY)
    if not isinstance(resume, dict):
        resume = guard.get("resume")
    if isinstance(resume, dict):
        save_setting(RESUME_KEY, dict(resume, rolled_back_from=str(guard.get("to_version") or "")))
    disarm()
    print("[警告] 一键更新到 v{} 之后，新版本上次启动时字幕界面一直没有连上：已退回更新前的 v{}"
          "（git reset --keep {}），重新启动…".format(to_version, from_version, _short(guard["from"])))
    return BootOutcome("rolled-back", rollback_message(from_version, to_version))


def mark_ready():
    """本机页面第一次连上（app/server.py）：本进程计过数的话，清掉记号。没计过（不是被看着的那个
    进程，或者根本没有记号）什么都不做、也不读设置。返回是否清了。"""
    armed = _state["armed"]
    if armed is None:
        return False
    _state["armed"] = None
    # 锁只防「新版本起来、页面还没连上」那段时间里同时启动的第二个进程；就绪了就放掉。Windows 上一键
    # 更新的重启是先起新进程、旧进程随后才退，旧进程要是还拿着锁，新进程会以为别的实例在启动、不计数
    release_lock()
    guard = load_settings().get(KEY)
    if isinstance(guard, dict) and (guard.get("to"), guard.get("at")) == armed:
        save_setting(KEY, None)
        print("[信息] 字幕界面已连上：一键更新到 v{} 成功".format(guard.get("to_version") or "?"))
        return True
    return False
