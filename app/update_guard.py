# i18n: done
"""一键更新之后的存活确认，新版本起不来就自动退回更新前的版本（专家审查 I6；用户 2026-10-22 定 D10
「自动退回更新前的版本」）。

以前一键更新合并完新代码就直接 execv：新版本要是一启动就崩，暂停中的直播不会恢复，也回不到旧版，
只能等人手动处理。现在分三步：

  arm()        一键更新合并成功、execv 之前（app/updater.py）：在 settings.json 里留一个记号
               update_guard = {from, to, from_version, to_version, at, boots: 0, resume}。from/to 是
               更新前后的提交号；resume 是那一刻「重启后接着监听」记号的副本（见 on_boot）；
  on_boot()    新版本每次真正启动时（main.py，确认没有别的实例在跑之后）：boots 加 1。boots 到
               ROLLBACK_AT_BOOTS（3）还没被清掉，说明新版本真的跑了两次、两次本机页面都没连上——这一次
               就不再跑新代码：git reset --keep 回到 from，清掉记号，交回一句对话框文字，由 main.py 弹窗
               （Mac、Windows 都弹，见 windows_notice）并重新 exec；
  mark_ready() 本机页面第一次连上控制服务时（app/server.py）：清掉记号，新版本算是能用了。不挂在
               「重启后接着监听」之后：没在监听时根本走不到那一步，记号就永远清不掉了。

不退回的几种情形（D10：「双击两次、强行退出不算；程序文件夹被手动改过就不退」，计划审查 P5）：
- 新版本只跑过一次就没了（页面连上之前崩了一次、被强退一次）：下一次照常跑新版本，那一次页面连上了
  就清记号（用户 2026-10-26 按 D10 原话定）；
- 别的实例在跑。main.py 只在单实例检查之后调 on_boot；同时启动的两个进程，单实例检查都看不见
  对方，这里再加一把进程锁：拿到锁的那个进程一直拿着，直到退出（崩溃时系统自动放锁）。拿不到锁
  就什么都不做，不计数；
- 强退：页面连上之后记号就清了，之后强退不算数；
- HEAD 已经不是 to：有人手动换过版本，记号作废；
- 工作区有改动（--untracked-files=no，和一键更新的预检同一个口径）：不碰，记号作废；
- git 不可用、reset 失败：记号作废，照常启动新版本。

只依赖标准库。git 调用除了 reset 都有时限（reset 为什么不设，见 GIT_TIMEOUT_SEC）。对话框文字在这里
（L()）；Mac 上弹不弹由 main.py 的 _info_dialog 决定，Windows 的系统消息框也在这里（windows_notice）。
终端行永远是中文。
"""
import errno
import hashlib
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .i18n import L
from .settings import load_settings, save_setting

ROOT = Path(__file__).resolve().parent.parent
KEY = "update_guard"
RESUME_KEY = "resume_after_update"   # app/pipeline.py 的「重启后接着监听」记号
# 启动计数到这个数、记号还在，就退回（D10；用户 2026-10-26 按原话定：「新版本要真的跑两次、页面都没连上，
# 第三次打开才退回」）。第 1 次是一键更新自己的 execv，第 2 次是中控再打开一次——这两次跑的都是新代码；
# 第 3 次不再跑新代码，直接退回。两次之间页面连上过一次，记号就清了，不会走到第 3 次。所以页面连上之前
# 偶尔强退一次、崩一次，下一次照常跑新版本，不会被误退。
# （#145 第 1、2 轮是 2：新版本只跑一次，第二次打开就退回，强退一次就会退回去。）
ROLLBACK_AT_BOOTS = 3
# 只读的 git（rev-parse、status）的时限：超时被杀不留任何东西
GIT_TIMEOUT_SEC = 30
# 不设时限的 git 子命令。git reset --keep 改的是工作区和 index：subprocess 的时限一到就杀进程，杀在半路会
# 留下一半旧一半新的程序文件，外加 .git/index.lock——之后每一条改仓库的 git（下一次一键更新、手动命令）
# 都会因为这把锁失败，得有人手动删（#145 审查）。它只在本机读写，不联网、不等凭据；index.lock 已经在的话
# 它马上报错退出，不会等。真卡住（比如程序文件夹在出了问题的网络盘上）时，终端最后一行是「正在退回…」，
# 中控能看到卡在哪一步；强退的后果和被我们杀掉一样，但不会是程序自己在它干到一半时动手
_GIT_NO_TIMEOUT = frozenset({"reset"})
# 记号里的 from/to 是完整的提交号（SHA-1 40 位，SHA-256 仓库 64 位）。from 会原样交给 git reset --keep：
# 不认得的（比如以 - 开头，会被 git 当成选项）整个记号作废，不碰 git
_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# 本进程的状态：on_boot 计过数（这个进程就是被看着的那个新版本）才算 armed；锁文件的句柄一直拿着
_state = {"armed": None, "lock": None}


class BootOutcome:
    """on_boot 的结果。kind：none / invalid / other-instance / counted / moved / dirty / git-failed /
    reset-failed / rolled-back。rolled_back 时 message 是给对话框的那句话（L()）；拼这句话本身出了错时
    是 None（文件已经退回，照样要重新 exec）。"""

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
        return not _held_elsewhere(exc.errno, os.name == "nt")
    _state["lock"] = fd
    return True


def _held_elsewhere(code, windows):
    """拿锁失败的 errno 是不是「被别的进程拿着」：POSIX 是 EWOULDBLOCK/EAGAIN（个别系统 EACCES），Windows 的
    msvcrt 报 EACCES 或 EDEADLK。别的错（文件系统不支持锁）不算：当作没有锁这道保险，照常往下走——两种系统
    同一个口径（以前 Windows 上任何错都当成别的实例，这次启动不计数，也就永远不会退回）。"""
    held = (errno.EACCES, errno.EDEADLK) if windows else (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)
    return code in held


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
    """跑一条 git，返回 (退出码, stdout)；起不来、超时返回 (None, "")。不等凭据。
    reset 不设时限（见 _GIT_NO_TIMEOUT），其余 GIT_TIMEOUT_SEC。"""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    timeout = None if args and args[0] in _GIT_NO_TIMEOUT else GIT_TIMEOUT_SEC
    try:
        r = subprocess.run(["git", *args], cwd=str(root), env=env, capture_output=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return r.returncode, r.stdout.decode("utf-8", errors="replace")


def _short(commit):
    return str(commit or "")[:8]


def rollback_message(from_version, to_version):
    """退回之后弹的那句话。只写看到的（两次启动页面都没连上）和程序做了什么（退回、重新打开），不猜为什么。"""
    return L("一键更新到 v{} 之后，新版本启动了两次，字幕界面都没有连上。这次启动已自动退回更新前的 "
             "v{}，程序会重新打开。",
             "After the update to v{}, the new version started twice and the caption window "
             "didn’t connect either time. On this launch, the app went back to v{}, the version "
             "before the update, and is opening again.").format(
        str(to_version or "?").lstrip("vV"), str(from_version or "?").lstrip("vV"))


# Windows 消息框的样式：信息图标、拉到前台、置顶（旧版本的窗口随后起来，不能把它盖住）
MB_ICONINFORMATION = 0x40
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000


def _start_daemon(fn):
    threading.Thread(target=fn, name="update-rollback-notice", daemon=True).start()


def windows_notice(message, os_name=None, environ=None, windll=None, start=None):
    """Windows 上退回之后用系统自带的消息框（user32.MessageBoxW）说明已退回哪个版本（用户 2026-10-26 定：
    Windows 也弹窗）。Mac 不走这里，照旧由 main.py 的 _info_dialog 弹。

    MessageBoxW 要等人点「确定」才返回，所以另起一个守护线程弹，这里马上返回：旧版本要马上起来，暂停中的
    监听才赶得上「重启后接着监听」的时限（和 Mac 的对话框一样不等人点）。Windows 上重新打开是起子进程、
    本进程一直等它（app/relaunch.replace_process），消息框就一直留到点掉、或程序退出。

    不看有没有终端：Windows 上从 Start.bat 打开，黑窗口总是在的，按终端判断就永远不会弹。CI 里不弹。
    弹不出来（没有 ctypes.windll、调用出错）就算了：那句话 main.py 已经打印过（终端是中文）。
    返回是否交给了系统。os_name / environ / windll / start 只给测试换替身用。"""
    if (os_name or os.name) != "nt" or not message:
        return False
    if (os.environ if environ is None else environ).get("CI"):
        return False
    try:
        if windll is None:
            import ctypes
            windll = ctypes.windll
        box = windll.user32.MessageBoxW
        from .native_dialog import messagebox_args
        text, title = messagebox_args(message)
    except Exception:
        return False

    def show():
        try:
            box(None, text, title, MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST)
        except Exception:
            pass

    try:
        (start or _start_daemon)(show)
    except Exception:
        return False
    return True


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
        if boots > 1:
            _give_back_resume(guard)
        print("[信息] 一键更新到 v{} 之后第 {} 次启动：字幕界面连上之后就算更新成功{}".format(
            to_version, boots, "；这一次还连不上的话，下次打开会自动退回 v{}".format(from_version)
            if boots == ROLLBACK_AT_BOOTS - 1 else ""))
        return BootOutcome("counted")
    # 新版本两次启动页面都没连上：先确认工作区还是一键更新留下的样子，才退回
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
    # git reset --keep 不设时限（见 _GIT_NO_TIMEOUT）：真卡住时终端最后一行是这一句。终端没了（print 抛
    # OSError）也照样退回：这一行打不出来不能挡住退回
    try:
        print("[信息] 一键更新到 v{} 之后新版本两次启动字幕界面都没连上：正在退回更新前的 v{}（{}）…".format(
            to_version, from_version, _short(guard["from"])))
    except Exception:
        pass
    code, _ = git(root, "reset", "--keep", guard["from"])
    if code != 0:
        disarm()
        print("[警告] 一键更新到 v{} 之后页面一直没连上，退回 v{}（{}）没成功（git 返回 {}），照常启动".format(
            to_version, from_version, _short(guard["from"]), code))
        return BootOutcome("reset-failed")
    # 从这里起程序文件已经是更新前的版本：后面几步（还记号、清记号、打一行、拼对话框文字）不管哪一步出错，
    # 都照样按「已退回」交回去，main.py 据此重新 exec。异常要是从这里漏出去，会被 main.py 的兜底吃掉、
    # 接着往下跑——内存里的新代码跑在已经退回的旧文件上（第 2 轮审查）
    try:
        _settle_after_reset(guard, to_version, from_version)
    except Exception as exc:
        try:
            print("[警告] 已退回更新前的 v{}，收尾时出错（{}）".format(from_version, type(exc).__name__))
        except Exception:
            pass
    try:
        message = rollback_message(from_version, to_version)
    except Exception:
        message = None
    return BootOutcome("rolled-back", message)


def _give_back_resume(guard):
    """新版本第 2 次启动（还算数、不退回）：上一次启动可能已经读掉了「重启后接着监听」记号（读到即删）、
    页面没连上就没了。把副本还回去，这一次照样能接着听——不然暂停中的监听要等到第 3 次退回时才还回去，
    那时往往已经过了 5 分钟。过没过期照旧按原来的 at 判断；记号还在（上一次没读到它）就原样不动。"""
    try:
        if load_settings().get(RESUME_KEY) is None and isinstance(guard.get("resume"), dict):
            save_setting(RESUME_KEY, dict(guard["resume"]))
            print("[信息] 上一次启动读掉了「重启后接着监听」的记号、页面没连上就退出了：已把记号还回去")
    except Exception as exc:
        print("[警告] 还回「重启后接着监听」的记号时出错（{}），这次不接着监听".format(type(exc).__name__))


def _settle_after_reset(guard, to_version, from_version):
    """git reset --keep 已经成功之后的收尾。新版本启动时可能已经读掉了「重启后接着监听」记号（读到即删）、
    然后才出事：还回去，旧版本照原来的时间判断过没过期。记号里注明是退回来的（rolled_back_from）：旧版本
    恢复监听时状态行不能说「已更新到」。"""
    resume = load_settings().get(RESUME_KEY)
    if not isinstance(resume, dict):
        resume = guard.get("resume")
    if isinstance(resume, dict):
        save_setting(RESUME_KEY, dict(resume, rolled_back_from=str(guard.get("to_version") or "")))
    disarm()
    print("[警告] 一键更新到 v{} 之后，新版本启动了两次，字幕界面都没有连上：已退回更新前的 v{}"
          "（git reset --keep {}），重新启动…".format(to_version, from_version, _short(guard["from"])))


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
