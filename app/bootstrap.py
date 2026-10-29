# i18n: done
"""启动自举（main.py 的 ensure_env）里能单独测试的判断、文案和安装步骤。

main.py 在模块级就跑 ensure_env()，测试一 import 它就会被 execv 成 .venv 里的程序
（见 tests/test_bug_sweep2.py 里的说明），所以逻辑放在这里，main.py 只做调用。
这个模块在依赖还没装齐时就要能导入：只用标准库。还要兼容 Python 3.9——本程序最低要 3.10
（用户 10-22 决定 D14），3.9 上 main.py 的版本检查正是用这里的 refuse_old_python 告诉中控要升级、
怎么升级；tests/test_bootstrap_constraints.py 按 3.9 的语法解析这个文件和 main.py。

这里管的几件事：
  * requirements 指纹：`pip install -r` 返回 0 之后记下清单的 sha256。清单变了
    （一键更新、手动 git pull）而 pip 没装上时，下次启动据此补装——以前启动只查
    固定的几个模块，新加的依赖永远没人补，「重新打开会自动继续安装」的承诺落空。
  * 版本锁（I4，用户决定 D8）：每一次 pip install 都带 -c constraints.txt（测试过的那一套版本，
    写法和理由见那个文件的开头）；指纹把 constraints.txt 一起算进去，锁的版本一变，已有安装下次
    启动补装一次，不重建。
  * 最低 Python（D14）：3.10。3.9 上 main.py 不往下跑，弹出「需要升级」和照做的步骤；.venv 是 3.9
    建的，换了新 Python 启动时重建它。
  * pip 输出落盘：logs/bootstrap-*.log，同时照样打到终端。双击 .app 启动时 stdout
    是 /dev/null，以前装失败了什么都不留。
  * 失败说明：按 pip 输出里看到的字样分成磁盘满、连不上软件包服务器、没有适用于
    当前 Python 的安装包三类，只写观察到的和能做的事；认不出的给通用说明，都附日志位置。
  * mlx-whisper 放弃记号：JSON（at / pip_exit / note），旧版本写的纯文本也认。
  * .venv 里没有 pip（首次安装在 ensurepip 那一步被打断，I1）：装之前先在 .venv 里跑 ensurepip
    原地补；补不回来写重建记号（.venv/.needs-rebuild），下次启动据此重建；记号写不上时
    失败说明改给删 .venv 的步骤，不承诺下次会重建。
  * 就绪指纹（I9）：最终进程确认组件齐了，就把 .venv 此刻的样子记进 .venv/.ready-fingerprint。
    下次启动的第一跳看指纹没变，就不再起子进程跑一遍约 2 秒的 import 检查和 pip 探测。
  * 推迟安装（I7）：现有的 .venv 能跑、只是清单变了要跑 pip，而端口上已经有本程序在跑（它可能
    正用着这个 .venv），这次不装，等没有程序在跑时再装。这一轮刚建的 .venv、核心组件 import 不齐的
    .venv 不推迟：不装就起不来。
"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

from .i18n import L, of

APP_DIR_NAME = "TikTok Live Translator.app"      # 与 app/macbundle.py 一致

REQ_STAMP = ".requirements.sha256"               # 放在 .venv 里：环境重建，指纹跟着没
CONSTRAINTS = "constraints.txt"                   # 测试过的那一套版本（I4），在项目根，和 requirements.txt 并列
# 本程序要求的最低 Python（D14）。main.py 的版本检查、venv_needs_rebuild、ready_fast_path 都看它；
# 再提时同步改 tools/find_python.sh、Start.bat、setup.sh、setup.ps1、README 和 CI 矩阵
MIN_PYTHON = (3, 10)
REQ_ATTEMPT = ".requirements-attempt.json"
# 同一份清单装失败后多久内不再为它重跑 pip -r（真缺核心依赖时不受这个限制）
REQ_RETRY_SEC = 24 * 3600
MLX_GIVEUP = ".mlx-unavailable"
MLX_RETRY_SEC = 24 * 3600
# .venv 里原地补不回 pip 时写的记号（JSON：at / kind）。放在 .venv 里：重建（venv.create clear=True）
# 时跟着清掉，不用另删
NEEDS_REBUILD = ".needs-rebuild"
# 就绪指纹（I9，JSON）。放在 .venv 里：重建时跟着清掉
READY_FINGERPRINT = ".ready-fingerprint"
READY_FINGERPRINT_VERSION = 1
# 上一跳因为端口上已有本程序在跑、把安装推迟了（I7）：值是推迟的那个进程的 PID。只在本项目
# .venv/.app 壳里、而且 PID 等于自己的进程认它（POSIX 上 exec 不换 PID；Windows 上永远对不上，
# 靠最终进程自己再判一次，见 install_deferred）；最终进程在 main() 里把它从环境里拿掉，不让它跟着
# 一键更新的重启和子进程传下去
DEFER_ENV = "TLT_INSTALL_DEFERRED"
KEEP_LOGS = 10
TAIL_LINES = 40

# 下面这些句子由 main.py 的 _info_dialog / _fail_alert 弹成系统对话框（同时 print 到终端，终端是中文）
_WAIT = L("完成后字幕窗口会自动打开——请耐心等待，不要重复打开程序。",
          "The caption window opens automatically when it’s done. Don’t open the app again "
          "in the meantime.")


def is_apple_silicon():
    return sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _read_json(path):
    text = _read_text(path)
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---- requirements 指纹 -------------------------------------------------------------------

def managed_env(root, prefix=None):
    """当前解释器用的是不是本项目自己的 .venv：直接跑 .venv 里的 python，或者经 .app
    壳里的 python（那个 sys.prefix 是 Contents，site-packages 经链接落回 .venv）。

    用户用自己装齐依赖的 Python 跑时不归这里管：以前那样能直接跑，现在也不该因为
    缺一个清单指纹就去建一个 1.4 GB 的 .venv。"""
    root = Path(root)
    try:
        here = Path(prefix if prefix is not None else sys.prefix).resolve()
        return here in ((root / ".venv").resolve(),
                        (root / APP_DIR_NAME / "Contents").resolve())
    except (OSError, RuntimeError):
        return False


def _lf_bytes(content):
    if isinstance(content, str):
        content = content.encode("utf-8")
    return content.replace(b"\r\n", b"\n")


# 两份内容之间的分隔：两个文件里都不会出现 NUL，挪动分界（把清单的最后几行算到锁文件里）
# 算不出同一个指纹
_CONSTRAINTS_SEP = b"\0" + CONSTRAINTS.encode("ascii") + b"\0"


def requirements_digest(content, constraints=None):
    """清单指纹。换行统一成 LF：Windows 上工作区文件可能被检出成 CRLF，而一键更新
    用 `git show` 拿到的是仓库里的 LF 版本——同一份清单不能算出两个指纹。

    constraints 是 constraints.txt 的内容（I4）：一起算进去，锁的版本一变，指纹就跟着变，已有的
    安装下次启动补装一次（plan_install 的「清单变了」那一种，不重建）。None 表示没有这个文件
    （老版本、新版本里没带）：这时和加锁以前逐字节同一个指纹，已有安装不会白补装一次。"""
    data = _lf_bytes(content)
    if constraints is not None:
        data += _CONSTRAINTS_SEP + _lf_bytes(constraints)
    return hashlib.sha256(data).hexdigest()


def requirements_file_digest(root):
    """工作区里 requirements.txt 加 constraints.txt 的指纹。constraints.txt 不在就只算清单；
    在却读不了（权限、坏盘）返回 None——pip -c 一样读不了它，不知道该装成什么样，和清单读不了时一样
    不给指纹（requirements_pending 据此不触发补装）。"""
    root = Path(root)
    try:
        content = (root / "requirements.txt").read_bytes()
    except OSError:
        return None
    try:
        constraints = (root / CONSTRAINTS).read_bytes()
    except FileNotFoundError:
        constraints = None
    except OSError:
        return None
    return requirements_digest(content, constraints)


def constraints_args(root):
    """交给 pip install 的 -c 参数（I4）：项目根有 constraints.txt 就是 ["-c", 它的路径]，没有就是空。
    用绝对路径：pip 的工作目录不一定是项目根。"""
    path = Path(root) / CONSTRAINTS
    return ["-c", str(path)] if path.is_file() else []


def recorded_requirements_digest(root):
    """上次在 .venv 里 `pip install -r` 装成功的那份清单的指纹；没有记录返回 None。"""
    return (_read_text(Path(root) / ".venv" / REQ_STAMP) or "").strip() or None


# 装不上程序照样能跑的组件：mlx-whisper 退回 CPU 识别，TikTokLive 只影响观众弹幕，
# pywebview 退回浏览器界面。一键更新时整份清单装不上，只要除它们之外的都装上了，
# 新代码照样可以落地（Updater._install_new_requirements）。
OPTIONAL_DISTS = ("mlx-whisper", "tiktoklive", "pywebview")


def _dist_key(name):
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def requirement_name(line):
    """清单里一行的包名（小写、连字符规范化）；注释、空行、pip 选项返回 None。"""
    text = line.split("#", 1)[0].strip()
    if not text or text.startswith("-"):
        return None
    m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", text)
    return _dist_key(m.group(0)) if m else None


def filter_requirements(content, drop):
    """去掉 drop 里这些包的行，其余原样保留（注释、pip 选项、环境标记都不动）。"""
    drop = {_dist_key(d) for d in drop}
    kept = [ln for ln in str(content).splitlines() if requirement_name(ln) not in drop]
    return "\n".join(kept) + "\n"  # i18n: data


def skipped_for_this_machine(root):
    """这台机器上 `pip install -r` 时要跳过的包。留了 mlx-whisper 放弃记号的，启动和一键
    更新都不再装它：它要连带下载 torch 等几百 MB，放在启动时会把开播挡上几分钟，放在
    一键更新里会让暂停的监听一直等着。它由 Updater.retry_mlx_install 在空闲时后台重试。"""
    return ("mlx-whisper",) if (Path(root) / ".venv" / MLX_GIVEUP).exists() else ()


def write_requirements_stamp(root, digest):
    """只在 `pip install -r` 返回 0 之后调用。.venv 不存在（用户自己的 Python）时不写，
    免得凭空建出一个只装着指纹的 .venv 目录。"""
    venv = Path(root) / ".venv"
    if not digest or not venv.is_dir():
        return False
    try:
        (venv / REQ_STAMP).write_text(digest + "\n", encoding="utf-8")
    except OSError:
        return False
    try:
        (venv / REQ_ATTEMPT).unlink()
    except OSError:
        pass
    return True


def record_requirements_failure(root, digest, exit_code, now=None):
    """记下「这份清单装失败过」：24 小时内启动不再为同一份清单重跑几分钟的 pip。"""
    venv = Path(root) / ".venv"
    if not digest or not venv.is_dir():
        return False
    data = {"digest": digest, "at": time.time() if now is None else now,
            "exit": exit_code if isinstance(exit_code, int) else None}
    try:
        (venv / REQ_ATTEMPT).write_text(json.dumps(data) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def forget_requirements_failure(root):
    """启动时撞上 ImportError（新依赖确实没装上、程序起不来）就调用：删掉失败记录，
    下次启动立刻重跑 pip -r。24 小时不重试只该用在「装不上但程序照样能跑」的时候——
    起不来还不重试，「重新打开会自动继续安装」就又落空了。"""
    try:
        (Path(root) / ".venv" / REQ_ATTEMPT).unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def requirements_pending(root, now=None):
    """requirements.txt 和上次在 .venv 里装成功的那份不一样，需要再跑一次 pip -r。

    同一份清单 24 小时内已经装失败过的不算：原因（没有这个 Python 的安装包、网络）
    重开一次通常不会变，每次启动都重跑只会挡住开播。真缺核心依赖时 import 检查
    照样会触发安装，不受这里影响。"""
    current = requirements_file_digest(root)
    if current is None:
        return False
    venv = Path(root) / ".venv"
    if recorded_requirements_digest(root) == current:
        return False
    attempt = _read_json(venv / REQ_ATTEMPT)
    if attempt and attempt.get("digest") == current:
        at = _as_float(attempt.get("at"))
        now = time.time() if now is None else now
        if at is not None and 0 <= now - at < REQ_RETRY_SEC:
            return False
    return True


def requirements_current(root, prefix=None, now=None):
    """main.py 启动第一道闸门（_deps_ok）的最后一问：清单是不是已经在本项目 .venv 里装好了。
    用户自己装齐依赖的 Python 不归这里管，判断本身出错也不挡启动——都算「装好了」。"""
    try:
        if not managed_env(root, prefix):
            return True
        return not requirements_pending(root, now=now)
    except Exception:
        return True


# 核心模块都在、只是清单变了的时候，已经装着的包 pip 几秒就核对完：过这么久还没结束才弹
# 「正在补装」，免得每次启动都闪一下提示框
REQUIREMENTS_DIALOG_DELAY_SEC = 5.0


def plan_install(imports_ok, root, recorded=None, actual=None, now=None):
    """ensure_env 确认 .venv 能跑之后：要不要跑 pip、弹什么话、过多久弹。

    imports_ok 是 .venv 里的 python 能不能 import 核心模块；recorded/actual 是 pyvenv.cfg
    记的和解释器实际的 Python 主次版本。返回 None（不用装）或 {"text", "delay"}。"""
    if imports_ok and not requirements_pending(root, now=now):
        return None
    text = install_dialog_text(recorded, actual, venv_existed=recorded is not None,
                               requirements_only=bool(imports_ok))
    return {"text": text, "delay": REQUIREMENTS_DIALOG_DELAY_SEC if imports_ok else 0.0}


# ---- 就绪指纹（I9） -------------------------------------------------------------------------
#
# 以前每次启动都经过三次解释器：启动器选的 Python → .venv/bin/python → .app 壳里的 python。第一跳
# 每次都起一个子进程跑一遍核心组件的 import（实测 1.9–2.4 秒）、版本一致时还要探一次 pip，第二跳
# 进程自己再 import 一遍（_deps_ok）才发现该进 .app 壳——约 4 秒白做，屏幕上什么都没有。
#
# 现在最终进程（组件都 import 成功、清单也装好了）把 .venv 此刻的样子写进 .ready-fingerprint；
# 下次启动的第一跳只读几个文件比一比，没变就不再检查，直接换进最终进程。比的是：
#   * requirements.txt 的指纹、.requirements.sha256 记的上次装成功的那份：清单一变就重新检查；
#   * pyvenv.cfg 的内容：重建过、setup.sh 用别的 Python 重跑过都会改它；
#   * .venv/bin/python 链接终点的真实路径、大小、修改时间。只看 pyvenv.cfg 不够（计划审查 Q8）：
#     2026-10-20 那次 conda 原地把 python3 换成了新的次版本，cfg 一个字没变，.venv 却跑在新版本上、
#     组件和 pip 全都找不到。链接终点换了文件，这三样至少有一样会变。读这三样不用起子进程；
#   * 要 import 的那几个模块（main._ready_import）：Apple 芯片上 mlx-whisper 放弃记号删掉之后
#     要求变了，也要重新检查。
# 指纹对得上但组件其实坏了（有人手动删了包）：最终进程 import 失败时删掉指纹（forget_ready），
# 当场照旧补装，下次启动也重新检查——见 main.py 的 ensure_env 和 _forget_install_failure。

def venv_python(root):
    """本项目 .venv 里的解释器（与 main._venv_python 一致）。"""
    sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return Path(root) / ".venv" / sub


def _file_sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def ready_fingerprint(root, ready_import, python=None):
    """.venv 此刻的样子（dict，可以原样写成 JSON）。只读文件、不起子进程。

    python 默认是 .venv 里的解释器。它的链接终点读不到（链到的 Python 被卸掉）、或者 pyvenv.cfg
    读不到，返回 None：没有指纹可比，照旧检查。"""
    root = Path(root)
    py = Path(python) if python is not None else venv_python(root)
    try:
        real = os.path.realpath(str(py))
        st = os.stat(real)
    except (OSError, ValueError):
        return None
    cfg = _file_sha256(root / ".venv" / "pyvenv.cfg")
    if cfg is None:
        return None
    return {"v": READY_FINGERPRINT_VERSION,
            "requirements": requirements_file_digest(root),
            "stamp": recorded_requirements_digest(root),
            "pyvenv_cfg": cfg,
            "python": {"path": real, "size": st.st_size, "mtime_ns": st.st_mtime_ns},
            "imports": str(ready_import or "")}


def read_ready_fingerprint(root):
    return _read_json(Path(root) / ".venv" / READY_FINGERPRINT)


def note_ready(root, ready_import, prefix=None, python=None):
    """最终进程确认组件都 import 成功、清单也装好了之后调用：记下就绪指纹。返回写没写成（内容
    没变也算成）。

    只在本项目自己的 .venv/.app 壳里记（managed_env）：用户自己装齐依赖的 Python 跑起来，
    说明不了 .venv 的事。先写临时文件再换名：两个实例同时启动时不会读到写了一半的 JSON。"""
    if not managed_env(root, prefix):
        return False
    fp = ready_fingerprint(root, ready_import, python)
    if fp is None:
        return False
    path = Path(root) / ".venv" / READY_FINGERPRINT
    if _read_json(path) == fp:
        return True
    tmp = path.with_name("{}.{}.tmp".format(path.name, os.getpid()))
    try:
        tmp.write_text(json.dumps(fp, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def forget_ready(root, prefix=None):
    """在本项目的 .venv/.app 壳里发现组件不齐（import 失败、要补装）：删掉就绪指纹，下次启动的
    第一跳照旧检查。不在本项目环境里（第一跳还在启动器选的 Python 上）什么都不做——那里 import
    不了是常态，不说明 .venv 坏了。"""
    if not managed_env(root, prefix):
        return False
    try:
        (Path(root) / ".venv" / READY_FINGERPRINT).unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def ready_fast_path(root, ready_import, *, actual_version, python=None, now=None):
    """第一跳能不能跳过检查（子进程 import 和 pip 探测），直接换进 .venv。全都满足才行：
      * actual_version（interpreter_version 实际跑出来的主次版本）和 pyvenv.cfg 记的一样。纵深防御：
        链接终点「真实路径、大小、修改时间都没变」是启发式，有的包管理器换文件时保留修改时间；
        次版本一漂移，这一条照样把第一跳送去全检查（venv_needs_rebuild 在那里探 pip、决定重建）。
        代价：cfg 记的和实际跑的长期不一致（setup.sh 用别的次版本不带 --clear 重跑过，见
        venv_needs_rebuild）时每次都全检查，和没有指纹以前一样慢，不会更糟；
      * 没有重建记号——有的话要让 venv_needs_rebuild 去探 pip、决定重建；
      * 清单不用补装（requirements_pending，和 plan_install 用的同一个判断：24 小时内为同一份
        清单装失败过的不算要装，过了 24 小时照旧重试）；
      * 指纹算得出来，而且和最终进程上次记下的一样；
      * 实际版本不低于本程序要求的最低版本（MIN_PYTHON，D14）。3.9 建的 .venv 指纹照样对得上，
        走捷径就会直接换进 3.9、在 main.py 的版本检查上退出，永远起不来——要交给全检查，
        venv_needs_rebuild 在那里决定用这次启动的新 Python 重建。
    解释器能不能跑不在这里看：调用方先跑 interpreter_version（几十毫秒），跑不起来照旧重建。
    actual_version 没有默认值：调用方必须把实际版本交进来，不能漏掉这一条。"""
    venv = Path(root) / ".venv"
    if actual_version is None or actual_version != pyvenv_version(venv):
        return False
    if version_below_minimum(actual_version):
        return False
    if (venv / NEEDS_REBUILD).exists():
        return False
    if requirements_pending(root, now=now):
        return False
    fp = ready_fingerprint(root, ready_import, python)
    return fp is not None and read_ready_fingerprint(root) == fp


# ---- 推迟安装（I7） ----------------------------------------------------------------------
#
# 以前只有「清空重建」那一支先探端口；清单变了要补装（pip -r）的那一支不探，于是直播中从
# Start.command、Start.bat 或命令行再开一次，pip 会在直播实例正用着的 .venv 里升级、替换包，
# 它之后的延迟导入可能拿到装了一半的东西。现在现有的 .venv 能跑、只是清单变了、端口上有本程序
# 在跑时，这次不装，直接换进 .venv（最终进程会认出已有实例、把窗口叫到前台），等下次没有实例时再装。
#
# 只推迟「不装也能跑」的那种。这一轮刚建的 .venv（比如新解压的一份第一次打开，而旧的那份还在
# 8765 上直播）不可能有人在用，推迟了最终进程缺组件起不来，旧实例退出之前这份一直装不上；核心组件
# import 不齐的 .venv 不装同样起不来。这两种照旧装。

def defer_install(plan, root, instance_running, *, imports_ok, venv_created):
    """要不要把这次安装推迟。plan 是 plan_install 的结果；imports_ok 是 .venv 里的 python 能不能
    import 核心组件（和 plan_install 用的同一个结果）；venv_created 是这一轮启动是不是刚
    venv.create 过 .venv。instance_running 是探端口的函数（只在真要装、而且能推迟的时候才调：
    平时启动不多探一次）。探测本身出错按「没探到」处理，照旧安装——和以前一样。

    两个关键字参数都没有默认值：调用方必须说清这一轮的 .venv 能不能跑，不能让「刚建的空环境」
    默认落进推迟。"""
    if plan is None or not imports_ok or venv_created:
        return False
    if not (Path(root) / ".venv").is_dir():
        return False
    try:
        return bool(instance_running())
    except Exception:
        return False


def mark_install_deferred(environ=None, pid=None):
    """推迟了安装、马上要换进 .venv：把本进程的 PID 记进环境变量，换进去的那个进程据此不再装。"""
    env = os.environ if environ is None else environ
    env[DEFER_ENV] = str(os.getpid() if pid is None else pid)


def install_deferred(root, environ=None, pid=None, prefix=None):
    """这个进程是不是上一跳推迟了安装之后换进来的：是的话 ensure_env 什么都不做。

    要同时满足：跑在本项目的 .venv/.app 壳里；环境变量里记的 PID 就是本进程。认 PID 是为了
    别让一个残留的环境变量（从终端里继承来的）让以后每次启动都不装。

    只有 POSIX 认得出来：exec 换进 .venv/bin/python 或 .app 壳都不换 PID。Windows 上认不出来，
    也不去猜：第一跳起子进程等着（app/relaunch.replace_process），子进程是 .venv\\Scripts\\python.exe——
    标准库 venv 在 Windows 上默认把 venvlauncher.exe 复制成它（Lib/venv/__init__.py 的 copy_sources），
    这个重定向器再起一个基础 python.exe 跑 main.py（multiprocessing/popen_spawn_win32.py 里 bpo-35797
    的注释说的就是它）。真正跑 main.py 的进程，父进程是重定向器，第一跳是祖父进程，PID 和父 PID
    都对不上。Windows 靠兜底：最终进程跑在 .venv 里，_deps_ok 因为清单待装照样不过，ensure_env 自己
    再走一遍子进程 import 检查和 defer_install，照样不跑 pip——代价是多一次约 2 秒的检查，
    推迟那行字打两遍（tests/test_bootstrap_plan.py 钉着这条兜底在 main.py 里的位置）。"""
    env = os.environ if environ is None else environ
    value = str(env.get(DEFER_ENV) or "").strip()
    if not value or not managed_env(root, prefix):
        return False
    return value == str(os.getpid() if pid is None else pid)


def clear_install_deferred(environ=None):
    """最终进程里调用（main() 进 .app 壳之后）：记号到此为止，不传给一键更新的重启和子进程。"""
    env = os.environ if environ is None else environ
    env.pop(DEFER_ENV, None)


# ---- Python 版本漂移 ----------------------------------------------------------------------

def _major_minor(text):
    """"3.9" / "3.13" → (3, 9) / (3, 13)；认不出返回 None。"""
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\s*", str(text or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def version_below_minimum(text):
    """主次版本字符串（interpreter_version / pyvenv_version 给的那种）低于 MIN_PYTHON 吗。
    认不出的不算低：没有证据不删东西。"""
    mm = _major_minor(text)
    return mm is not None and mm < MIN_PYTHON


def pyvenv_version(venv_dir):
    """pyvenv.cfg 里记的建环境时的 Python 主次版本（"3.13"），读不到返回 None。
    标准库 venv 写 `version = 3.13.5`，virtualenv 写 `version_info = 3.13.5.final.0`。"""
    text = _read_text(Path(venv_dir) / "pyvenv.cfg")
    if not text:
        return None
    for key in ("version", "version_info"):
        m = re.search(r"^\s*{}\s*=\s*(\d+)\.(\d+)".format(key), text, re.M)
        if m:
            return "{}.{}".format(m.group(1), m.group(2))
    return None


def interpreter_version(python, timeout=30):
    """实际跑一下这个解释器：返回 (能跑, "3.14")，跑不起来返回 (False, None)。

    以前只跑 `import sys` 判断能不能用，而 .venv 链到一个不带版本号的 python3 时，
    底下的 Python 升了次版本照样「能用」——顺手把版本带回来，不多起一个进程。"""
    try:
        r = subprocess.run([str(python), "-c",
                            "import sys; print('%d.%d' % sys.version_info[:2])"],
                           capture_output=True, timeout=timeout)
    except Exception:
        return False, None
    if r.returncode != 0:
        return False, None
    out = r.stdout.decode("ascii", errors="replace").strip()
    return True, (out if re.fullmatch(r"\d+\.\d+", out) else None)


def venv_needs_rebuild(usable, recorded=None, actual=None, own_env=False, python=None,
                       ready_import=None):
    """ensure_env 要不要用这次启动的解释器把 .venv 从头重建（venv.create(clear=True)）。

    usable/actual 是 interpreter_version(.venv/bin/python) 的结果，recorded 是 pyvenv.cfg
    记的主次版本，own_env 是当前进程本来就跑在 .venv 或 .app 壳里（managed_env）。
    python/ready_import 是 .venv/bin/python 和核心组件的 import 语句，两个版本都读得到时拿来探测；
    重建记号（NEEDS_REBUILD）也按 python 找：它在 python 往上两级的 .venv 里。

    跑不起来要重建：链到的 Python 被卸掉、上次建到一半。能跑、但次版本和 cfg 记的不一样，
    而且在它实际跑的这个版本下组件和 pip 都 import 不了，也要重建：.venv/bin/python3 链到
    一个不带版本号的 python3（比如 conda 的 /opt/anaconda3/bin/python3），底下的 Python 原地
    升了次版本。这时它只找 lib/python<新版本>/site-packages，装过的组件和 pip 都在
    lib/python<旧版本> 下——原地 `python -m pip install` 只会报 No module named pip
    （2026-10-20 实测），每次启动都卡在同一个安装失败上。重建同时改写 pyvenv.cfg，启动器
    （tools/find_python.sh）下次启动又能认出建它的那个解释器，「完全磁盘访问权限」跟着固定在它上面。

    光看版本不一致不够（2026-10-20 复审实测）：在已有的 .venv 上不带 --clear 用另一个次版本
    再跑一次 `python3 -m venv .venv`（setup.sh 就是这么写的），CPython 改写 pyvenv.cfg，却不
    替换已经在的 bin/python 链接——cfg 记着 3.14，.venv 照旧跑 3.13，组件和 pip 一样不少。
    那时清空就是把一个好好的环境删掉、联网重装约 1.4 GB。所以版本不一致时先探测
    （venv_salvageable）：能 import pip 或能 import 核心组件，就照旧原地补装。

    版本一致时也探测（I1，2026-10-21 审查）：venv.create 先写 pyvenv.cfg 和 bin/python，最后才跑
    ensurepip，首次安装在装 pip 那一步被打断（强制退出、杀进程组，ensurepip 跟着父进程一起死），
    留下的 .venv 版本对得上、能跑，却既没有 pip 也没有组件。以前这里直接放过，原地补装每次都报
    No module named pip，对话框还叫人检查网络，重开多少次都一样。pip 和组件都 import 不了，
    就没有什么可留的，重建。

    install_requirements 原地补 pip 失败时写下的重建记号也认：记号在、pip 还是 import 不了，
    就重建——那时组件可能都在（venv_salvageable 会说用得上），可清单变了要补装也没有 pip 可用，
    失败说明里「下次打开程序会自动重建」这句话靠这里兑现。记号在但 pip 已经回来了（有人手动补过），
    删掉记号照常判断，不白下 1.4 GB。

    两个版本有一个读不到、或者没给 python 就不重建，照旧原地补装：没有证据不删东西——有重建记号、
    而且探测确认 pip 还是 import 不了时除外（上一段），那时版本读不读得到都重建。记号在、但 pip 探测
    本身出错（超时、起不来）时不按记号重建：不知道就不删，记号留着下次再看，这次照常走下面的判断。
    当前进程就在 .venv/.app 壳里时不重建：那是从环境里面把自己的环境清空（记号留着，下次从启动器
    进来时再重建）。

    .venv 实际跑的 Python 低于本程序要求的最低版本（MIN_PYTHON，用户 10-22 决定 D14）也重建，不管
    组件和 pip 在不在：最低版本提到 3.10 以后，3.9 建的 .venv 一个字没变、照样能 import，以前这里
    原地补装完就换进去，在 main.py 的版本检查上退出——中控按提示装了新 Python 再打开，还是一样。
    走到这里的进程自己过了版本检查，用它重建正好；启动器（tools/find_python.sh）也不再挑 3.9。"""
    if not usable:
        return True
    if own_env:
        return False
    if version_below_minimum(actual):
        return True
    if python is not None and _rebuild_marked(python):
        return True
    if not (recorded and actual) or python is None:
        return False
    return not venv_salvageable(python, ready_import)


def _marker_for(python):
    """.venv/bin/python（Windows 是 .venv/Scripts/python.exe）→ .venv/.needs-rebuild。不解开链接：
    .venv/bin/python 链到的是建环境的那个 Python，解开就出了 .venv。"""
    return Path(python).parent.parent / NEEDS_REBUILD


def _rebuild_marked(python):
    """有重建记号、而且探测确认 pip 还是 import 不了（pip_importable 返回 False）。pip 回来了就删掉
    记号，返回 False。探测本身出错（返回 None：起不来、超时）按「不知道」处理：不重建、不删记号，
    下次启动再看——和 pip_importable、venv_salvageable 的约定一样，没有证据不删东西。"""
    marker = _marker_for(python)
    if not marker.exists():
        return False
    has_pip = pip_importable(python)
    if has_pip is True:
        try:
            marker.unlink()
        except OSError:
            pass
        return False
    return has_pip is False


def mark_needs_rebuild(root, now=None):
    """在 <root>/.venv 里写重建记号（JSON：at / kind）。.venv 不在就不写（不凭空建目录）。"""
    venv = Path(root) / ".venv"
    if not venv.is_dir():
        return False
    data = {"at": datetime.fromtimestamp(time.time() if now is None else now)
            .isoformat(timespec="seconds"), "kind": "pip_missing"}
    try:
        (venv / NEEDS_REBUILD).write_text(json.dumps(data) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def pip_importable(python, timeout=60):
    """这个解释器能不能 import pip：True / False；探测本身出错（解释器文件不在、起不来、超时）
    返回 None——不知道，不当成缺 pip。一般几十毫秒。只认给出的那个文件，不按 PATH 去找同名程序。"""
    if not Path(python).exists():
        return None
    try:
        r = subprocess.run([str(python), "-c", "import pip"], capture_output=True, timeout=timeout)
    except Exception:
        return None
    return r.returncode == 0


def venv_salvageable(python, ready_import=None, timeout=60):
    """版本不一致的 .venv 在它实际跑的版本下还用不用得上：能 import pip（原地补装走得通），
    或者能执行 ready_import（核心组件都在，直接能跑），就算用得上。

    先探 pip：一般几十毫秒，setup.sh 重跑留下的那种环境每次启动都要过这一步。探测本身出错
    （起不来、超时）也算用得上——没有证据不删东西，后面原地补装失败时照旧弹出说明。"""
    for code in ("import pip", ready_import):
        if not code:
            continue
        try:
            r = subprocess.run([str(python), "-c", code], capture_output=True, timeout=timeout)
        except Exception:
            return True
        if r.returncode == 0:
            return True
    return False


def rebuild_blocked_text():
    """ensure_env 要清空重建 .venv，端口上却已经有本程序在跑：这次不清，请中控先退出它。
    只说观察到的事和能做的事——端口上那个程序是不是正用着这个 .venv，从这边看不出来。"""
    return L("程序已经在运行。这次打开需要重新安装运行组件，要先清空 .venv 文件夹，"
             "正在运行的程序可能还在用它，所以这次没有安装。\n"
             "请先退出正在运行的程序，再重新打开本程序。",
             "The app is already running. Opening it again needs the app’s components "
             "reinstalled, which first empties the .venv folder, and the running app may still "
             "be using it. Nothing was installed this time.\n"
             "Quit the running app first, then open the app again.")


def install_dialog_text(recorded=None, actual=None, venv_existed=False,
                        requirements_only=False):
    """安装开始时给中控看的那句话。只说观察到的事：清单变了、Python 版本变了、
    环境里缺组件、还是第一次装。"""
    if requirements_only:
        return L("组件清单和上次安装时不一样了，正在补装运行组件（一般不到一分钟）。\n",
                 "The list of components changed since the last install. Installing what’s "
                 "missing (usually under a minute).\n") + _WAIT
    if recorded and actual and recorded != actual:
        return (L("Python 版本从 {} 变成了 {}，需要重新安装运行组件（约需 2–5 分钟，"
                  "取决于网速）。\n",
                  "Python changed from {} to {}, so the app’s components need to be reinstalled "
                  "(about 2–5 minutes, depending on your connection).\n")
                .format(recorded, actual) + _WAIT)
    if venv_existed:
        return L("运行组件不完整，正在自动补装（约需 2–5 分钟，取决于网速）。\n",
                 "Some of the app’s components are missing. Installing them now (about "
                 "2–5 minutes, depending on your connection).\n") + _WAIT
    return L("首次运行：正在自动安装运行组件（约需 2–5 分钟，取决于网速）。\n",
             "First launch: installing the app’s components (about 2–5 minutes, depending on "
             "your connection).\n") + _WAIT


# ---- 最低 Python（D14） ---------------------------------------------------------------------
#
# 用户 10-22 决定：最低版本从 3.9 提到 3.10（观众弹幕本来就要 3.10）。3.9 上 main.py 在版本检查那里
# 停下，调 refuse_old_python：终端打印、没有终端时弹系统对话框（双击 .app 启动、或一键更新后重启时
# stdout 是 /dev/null，只 print 的话程序就无声消失了）。这一段只用标准库和 i18n / native_dialog，
# 3.9 上要能 import（文件头）。

def python_too_old(version_info=None):
    """这个解释器低于本程序要求的最低版本吗。"""
    v = sys.version_info if version_info is None else version_info
    return tuple(v[:2]) < MIN_PYTHON


def python_upgrade_steps(darwin=None, windows=None, inline=False):
    """升级 Python 的照做步骤（D14）：python_too_old_text 的后半段，自检里 3.9 那一行
    （selfcheck.check_comments）也给这一段。只写能做的事。

    装好新 Python 之后重新打开就行，不用手动删 .venv：启动器（tools/find_python.sh、Start.bat）
    不再挑低于 3.10 的解释器，换成新的那个启动后，venv_needs_rebuild 认出 .venv 是旧版本建的、
    用新的重建（所以这里不能用 selfcheck.python_switch_steps：那一段要人删 .venv，是给 Rosetta
    那种版本够、启动器照样认 .venv 的情况的）。在终端里直接跑 main.py 的人要自己换解释器，所以单写
    一句，例子按平台写：Windows 上 python.org 的安装程序装的是 py 启动器，没有 python3.13 这个命令。
    macOS 的「完全磁盘访问权限」按解释器文件登记（browser_login.fda_targets）：换了 Python，新的
    那个不在列表里，要登录才给流地址的直播间就拿不到流——和 selfcheck.python_switch_steps 同一个提醒。
    inline：自检那一格不保留换行，那句提醒接在同一段里；对话框里另起一行。"""
    darwin = sys.platform == "darwin" if darwin is None else darwin
    windows = sys.platform == "win32" if windows is None else windows
    need = "{}.{}".format(*MIN_PYTHON)  # i18n: data（版本号）
    example = "py -3.13 main.py" if windows else "python3.13 main.py"  # i18n: data（命令行）
    steps = L("请从 python.org 下载安装 Python {} 或更新的版本，装好后重新打开程序：它会用新的 Python "
              "重新安装运行组件（约 1.4 GB，需要联网）。在终端里运行的，改用新装的 Python 运行 main.py"
              "（例如 {}）。",
              "Install Python {} or later from python.org, then open the app again. It reinstalls "
              "its components with the new Python (about 1.4 GB, needs an internet connection). If "
              "you run the app from a terminal, run main.py with the new Python (for example, "
              "{}).").format(need, example)
    if not darwin:
        return steps
    # 路径和步骤指向窗口顶部那条提示，不指向自检「浏览器登录态」那一行：直播中设置分组整块收着，那一行看不见
    # （2026-10-29 时那一行在别的浏览器读到登录时还不带步骤，见 selfcheck.python_switch_steps）
    fda = L("换了 Python 之后，要把新的 Python 加进「完全磁盘访问权限」一次：点「开始翻译」后，系统拒绝"
            "读取浏览器数据时，程序窗口顶部那条提示里有路径和步骤；不加的话，TikTok 要求登录才给流地址的"
            "直播间拿不到流。",
            "After switching, add the new Python to Full Disk Access once. After you click "
            "Start, if macOS doesn’t allow the app to read browser data, the notice at the top "
            "of the app’s window shows the path and steps. Without it, the app can’t get the "
            "stream for live streams where TikTok provides the stream URL only to signed-in "
            "viewers.")
    if inline:
        return L("{}{}", "{} {}").format(steps, fda)
    return steps + "\n" + fda


def python_too_old_text(current, darwin=None, windows=None):
    """「需要升级」和照做的步骤（python_upgrade_steps）。只写看到的（现在用的是哪个版本）和能做的事。"""
    need = "{}.{}".format(*MIN_PYTHON)  # i18n: data（版本号）
    head = L("这个版本的程序需要 Python {} 或更新的版本，现在用的是 Python {}，需要升级。\n",
             "This version of the app needs Python {} or later. It’s running on Python {}, "
             "which needs an upgrade.\n").format(need, current)
    return head + python_upgrade_steps(darwin, windows)


def _stdio_is_terminal():
    """stdout 或 stderr 连着终端（和 main._has_console 同一个判断）。"""
    try:
        return any(s is not None and s.isatty() for s in (sys.stdout, sys.stderr))
    except Exception:
        return False


def _windows_messagebox(text, title):
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x30)


def refuse_old_python(version_info=None, *, has_console=None, sys_platform=None,
                      run=subprocess.run, messagebox=None):
    """main.py 的版本检查不过时调用：终端打印「需要升级」（终端永远是中文），没有终端时再弹系统
    对话框，返回给 sys.exit 的退出码 1。CI 里只打印、不弹（同 main._fail_alert）。弹框失败不挡退出。"""
    v = sys.version_info if version_info is None else version_info
    sys_platform = sys.platform if sys_platform is None else sys_platform
    text = python_too_old_text("{}.{}".format(v[0], v[1]), darwin=sys_platform == "darwin",
                               windows=sys_platform == "win32")
    try:
        print("⚠️ " + text, flush=True)
    except Exception:
        pass
    has_console = _stdio_is_terminal() if has_console is None else has_console
    if os.environ.get("CI") or has_console:
        return 1
    try:
        from . import native_dialog
        if sys_platform == "darwin":
            run(native_dialog.osascript_argv(text, button=native_dialog.FAIL_BUTTON, icon="caution"),
                capture_output=True, timeout=600)
        elif sys_platform == "win32":
            (messagebox or _windows_messagebox)(*native_dialog.messagebox_args(text))
    except Exception:
        pass
    return 1


# ---- pip 输出落盘与失败说明 -----------------------------------------------------------------

def new_log_path(root, kind, now=None, keep=KEEP_LOGS):
    """logs/<kind>-<时间>.log。同类旧日志只留最近 keep 个。logs/ 建不了返回 None。"""
    d = Path(root) / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        old = sorted(d.glob("{}-*.log".format(kind)))
        for f in old[:max(0, len(old) - max(0, keep - 1))]:
            try:
                f.unlink()
            except OSError:
                pass
    except OSError:
        return None
    stamp = datetime.fromtimestamp(time.time() if now is None else now).strftime("%Y%m%d-%H%M%S")
    return d / "{}-{}.log".format(kind, stamp)


def run_logged(cmd, log_path=None, echo=True, popen=subprocess.Popen):
    """跑一个命令，输出同时写进日志文件和终端，返回 (退出码, 最后几十行)。

    只写文件的话 Start.command 窗口里就看不到安装进度，所以两边都写。起不来的
    异常照常抛出（调用方统一处理），抛之前把原话记进日志。"""
    tail = deque(maxlen=TAIL_LINES)
    fh = None
    if log_path is not None:
        try:
            fh = open(str(log_path), "ab")
            fh.write("$ {}\n".format(" ".join(str(c) for c in cmd)).encode("utf-8"))  # i18n: data
            fh.flush()
        except OSError:
            fh = None
    code = None
    try:
        proc = popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT,
                     env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        for raw in iter(proc.stdout.readline, b""):
            if fh is not None:
                try:
                    fh.write(raw)
                except OSError:
                    pass
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            tail.append(line)
            if echo:
                try:
                    print(line, flush=True)
                except Exception:
                    pass
        proc.stdout.close()
        code = proc.wait()
        return code, "\n".join(tail)  # i18n: data
    except Exception as exc:
        if fh is not None:
            try:
                fh.write("启动失败：{!r}\n".format(exc).encode("utf-8"))  # i18n: terminal
            except OSError:
                pass
        raise
    finally:
        if fh is not None:
            try:
                fh.write("[退出码 {}]\n".format(code).encode("utf-8"))  # i18n: terminal
                fh.close()
            except OSError:
                pass


class PipFailed(Exception):
    """核心依赖都没装上。带着退出码、输出末尾和日志位置，供 failure_text 分类。"""

    def __init__(self, exit_code, tail="", log_path=None):
        super().__init__(L("pip 返回 {}", "pip returned {}").format(exit_code))
        self.exit_code = exit_code
        self.tail = tail or ""
        self.log_path = log_path


class PipMissing(Exception):
    """.venv 里的 python 没有 pip，在 .venv 里跑 ensurepip 也没补回来。抛之前试着写了重建记号
    （mark_needs_rebuild），marked 是写成没有：写成了下次启动 venv_needs_rebuild 据此重建；没写成
    （.venv 不在、写文件报错）下次不会自动重建，failure_text 据此换成照做的步骤。默认按没写成算：
    不知道就不承诺。带着日志位置给 failure_text。"""

    def __init__(self, log_path=None, marked=False):
        super().__init__(L(".venv 里没有 pip，原地补装没有成功",
                           "pip is missing from .venv and couldn’t be reinstalled in place"))
        self.log_path = log_path
        self.marked = bool(marked)


# `python -m pip` 在没有 pip 的解释器上说的那句（"/…/python: No module named pip"）
_NO_PIP_MARK = "no module named pip"


def pip_missing_text(log_path=None, marked=False):
    """缺 pip、原地补不回来时给中控的说明。按真实行为写：重建记号写成了（marked），说下次打开会
    自动重建；没写成，下次启动不会自动重建，不能这么承诺，改给照做的步骤——删掉 .venv 再打开，
    启动时找不到 .venv 就会从头建（venv_needs_rebuild 的第一条）。
    不叫人检查网络——这一步不联网，网络好坏都一样；也不给手动 pip install 的进阶说明，没有 pip
    那条路走不通。"""
    head = L("程序的运行环境里缺少安装工具 pip，原地补装没有成功。",
             "The app’s runtime is missing pip, the tool that installs its components, and "
             "reinstalling pip in place didn’t work.")
    if marked:
        head += L("\n下次打开程序会自动重建运行环境（约 1.4 GB，需要联网）。",
                  "\nThe next time you open the app, it rebuilds the runtime automatically "
                  "(about 1.4 GB, needs an internet connection).")
    else:
        head += L("\n请删掉程序文件夹里的 .venv 文件夹，再重新打开程序，它会重新安装运行组件"
                  "（约 1.4 GB，需要联网）。",
                  "\nDelete the .venv folder in the app folder, then open the app again. It "
                  "reinstalls its components (about 1.4 GB, needs an internet connection).")
    # 日志位置单独一行放在最后，末尾不带换行：对话框底下不留一行空白
    return head + (L("\n详细记录：{}", "\nLog file: {}").format(log_path) if log_path else "")


_DISK_MARKS = ("no space left on device", "errno 28", "not enough space on the disk",
               "winerror 112")
_NET_MARKS = ("newconnectionerror", "failed to establish a new connection",
              "temporary failure in name resolution", "name or service not known",
              "nodename nor servname", "getaddrinfo failed", "max retries exceeded",
              "connecttimeouterror", "readtimeouterror", "read timed out",
              "connection timed out", "network is unreachable", "connection reset",
              "connection refused", "proxyerror", "sslerror", "certificate verify failed",
              "connection broken", "incompleteread", "remote end closed connection")
_NO_DIST_MARKS = ("no matching distribution found", "could not find a version that satisfies")
# pip 的两种说法：「Package 'x' requires a different Python」和
# 「Ignored the following versions that require a different python version」
_PY_MARK = "a different python"
_ADVANCED = L("（进阶：也可手动运行 setup.sh / setup.ps1，或 pip install -r requirements.txt）",
              "(Advanced: you can also run setup.sh / setup.ps1, or pip install -r "
              "requirements.txt)")


def _free_gb(root):
    try:
        return shutil.disk_usage(str(root)).free / 1024 ** 3
    except (OSError, ValueError):
        return None


SUPPORTED_PYTHONS = ("3.12", "3.13")


def pip_failure_text(tail, exit_code, log_path=None, python_version=None, root=None):
    """把一次失败的 pip 说成中控看得懂的话。

    只按输出里真实出现的字样分类，不猜：断网时 pip 最后一行也是「No matching
    distribution found」，所以先看有没有连接失败的字样。「没找到安装包」也可能来自
    暂时没同步的镜像或代理，所以只有当前 Python 不在测试过的版本里时才说成 Python 版本
    的事，说的时候给出真能换掉 Python 的做法（.venv 里的解释器不会自己换）。认不出的
    给通用说明，一律附上日志位置，不再把 CalledProcessError 原文塞给中控。"""
    low = (tail or "").lower()
    network = any(m in low for m in _NET_MARKS)
    no_dist = _PY_MARK in low or any(m in low for m in _NO_DIST_MARKS)
    if any(m in low for m in _DISK_MARKS):
        free = _free_gb(root) if root is not None else None
        head = (L("安装运行组件时磁盘空间不够（pip 报告写不进去）。运行环境约需 1.4 GB，"
                  "语音和翻译模型另需约 4 GB{}。\n腾出空间后重新打开本程序，会自动继续安装。",
                  "There wasn’t enough disk space to install the app’s components (pip reported "
                  "it couldn’t write). The app needs about 1.4 GB, plus about 4 GB for the "
                  "speech and translation models{}.\nFree up space, then open the app again. "
                  "Installation continues automatically.")
                .format(L("，现在剩余 {:.1f} GB", " ({:.1f} GB available now)").format(free)
                        if free is not None else ""))
    elif (no_dist and (_PY_MARK in low or not network) and python_version
          and python_version not in SUPPORTED_PYTHONS):
        head = (L("pip 找不到适用于当前 Python {} 的安装包。\n"
                  "本程序在 Python {} 上测试过：装好其中一个后，删掉程序目录里的 .venv 文件夹，"
                  "再用那个 Python 运行一次 main.py（例如 python3.13 main.py），会用它重新安装"
                  "运行组件。",
                  "pip couldn’t find packages for Python {}.\n"
                  "The app is tested with these Python versions: {}. Install one of them, delete "
                  "the .venv folder in the app folder, then run main.py once with that Python "
                  "(for example, python3.13 main.py). It reinstalls the app’s components with "
                  "that Python.")
                .format(python_version, L("、", ", ").join(SUPPORTED_PYTHONS)))
    elif network:
        head = L("自动安装未完成：pip 连不上软件包服务器。\n"
                 "请检查网络连接，然后重新打开本程序——会自动从中断处继续安装。",
                 "Installation didn’t finish: pip couldn’t reach the package server.\n"
                 "Check your network connection, then open the app again. Installation picks "
                 "up where it left off.")
    elif no_dist:
        head = (L("自动安装未完成：pip 没找到所需组件的安装包（pip 返回 {}）。\n"
                  "重新打开本程序会再试一次；一直这样的话，请把下面的记录发给开发者。",
                  "Installation didn’t finish: pip couldn’t find packages for the required "
                  "components (pip returned {}).\nOpen the app again to retry. If this keeps "
                  "happening, send the log below to the developer.")
                .format(exit_code))
    else:
        head = (L("自动安装未完成（pip 返回 {}）。\n"
                  "请检查网络连接，然后重新打开本程序——会自动从中断处继续安装。",
                  "Installation didn’t finish (pip returned {}).\n"
                  "Check your network connection, then open the app again. Installation picks "
                  "up where it left off.")
                .format(exit_code))
    where = L("详细记录：{}\n", "Log file: {}\n").format(log_path) if log_path else ""
    return head + "\n" + where + _ADVANCED


def failure_text(exc, log_path=None, python_version=None, root=None):
    """ensure_env 里任何异常给中控的说明。不是 pip 本身失败的（建环境、起进程出错），
    把原话和堆栈写进日志，对话框里给日志位置。

    缺 pip（I1）单独说：以前 PipFailed 的输出里只有 No module named pip，认不出来，落到最后那句
    「请检查网络连接」，中控查了网络、重开，还是一样。install_requirements 遇到缺 pip 抛的是
    PipMissing（带着记号写没写成）；万一 PipFailed 的输出里出现这句，这里补写记号。两条路都按记号
    写没写成选说法：写成了才说「下次会重建」，没写成给删 .venv 的步骤。"""
    if isinstance(exc, PipMissing):
        return pip_missing_text(exc.log_path or log_path, marked=exc.marked)
    if isinstance(exc, PipFailed) and _NO_PIP_MARK in exc.tail.lower():
        marked = mark_needs_rebuild(root) if root is not None else False
        return pip_missing_text(exc.log_path or log_path, marked=marked)
    if isinstance(exc, PipFailed):
        return pip_failure_text(exc.tail, exc.exit_code, exc.log_path or log_path,
                                python_version, root)
    if log_path is None and root is not None:
        log_path = new_log_path(root, "bootstrap")
    written = False
    if log_path is not None:
        try:
            with open(str(log_path), "a", encoding="utf-8") as fh:
                fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))  # i18n: data
            written = True
        except OSError:
            written = False
    detail = (L("详细记录：{}\n", "Log file: {}\n").format(log_path) if written
              else L("（{}）\n", "({})\n").format(of(exc)))
    return (L("自动安装未完成。\n请检查网络连接，然后重新打开本程序——会自动从中断处继续安装。\n",
              "Installation didn’t finish.\nCheck your network connection, then open the app "
              "again. Installation picks up where it left off.\n")
            + detail + _ADVANCED)


def ensure_pip(pip_python, root, log_path=None, run=run_logged, probe=pip_importable, now=None):
    """跑 pip 之前先看 .venv 里有没有 pip（I1）。没有就在 .venv 里跑 `python -m ensurepip
    --default-pip`：用的是 Python 自带的 pip 安装包，不联网，几秒钟，输出照样进 bootstrap 日志。
    补回来了返回 True；补不回来写重建记号、抛 PipMissing（带着记号写没写成）。记号没写成时终端和
    对话框都不说「下次会重建」，改给删 .venv 的步骤。探测本身出错（probe 返回 None）不当成
    缺 pip，照旧往下装：没有证据不动环境，真装不上时照旧弹失败说明。

    为什么先原地补而不是直接重建（2026-10-21 计划审查 Q3）：组件都在、只是 pip 没了的 .venv，
    venv_needs_rebuild 不会重建（它能跑），而清单一变要补装就卡在 No module named pip 上；
    原地补几秒钟，重建要联网下约 1.4 GB。"""
    if probe(pip_python) is not False:
        return True
    print("[初始化] 运行环境里缺少 pip，正在原地补装（不联网）…")
    try:
        code, _tail = run([str(pip_python), "-m", "ensurepip", "--default-pip"], log_path)
    except Exception:
        code = None
    if code == 0 and probe(pip_python) is True:
        print("[初始化] pip 已补回")
        return True
    marked = mark_needs_rebuild(root, now=now)
    if marked:
        print("[初始化] pip 原地补装没有成功，下次启动会重建运行环境")
    else:
        print("[初始化] pip 原地补装没有成功，重建记号也没写上：请删掉程序文件夹里的 .venv 文件夹，"
              "再重新打开程序")
    raise PipMissing(log_path, marked=marked)


def _fallback_step(run, cmd, pinned, log_path):
    """install_requirements 回退路径上的一步：先带 -c（pinned）装；带着装不上，就不带 -c 再装一次这一步，
    和加锁以前一样（理由见 install_requirements）。返回 (退出码, 输出末尾, 这一轮后面几步的 -c)：
    不带 -c 才装上的，后面几步也直接不带，省得每一步都先等一次带 -c 的失败（没联网时 pip 每次都要
    重试几轮才报错）；两次都没装上的，后面照旧先带 -c。pinned 是空的（没有 constraints.txt）时
    只装一次，和以前一样。"""
    code, tail = run(cmd + pinned, log_path)
    if code == 0 or not pinned:
        return code, tail, pinned
    print("[初始化] 按 constraints.txt 锁定的版本没装上，这一步改为不锁版本再装一次（和加锁以前一样）…")
    code, tail = run(cmd, log_path)
    return code, tail, ([] if code == 0 else pinned)


def install_requirements(pip_python, root, core_deps, optional_deps, log_path=None,
                         run=run_logged, now=None, probe=pip_importable):
    """ensure_env 的安装步骤：整份 requirements；不行就先保核心依赖，再逐个装可选组件。

    整份 -r 返回 0 才写清单指纹；没成就记下这次失败。mlx-whisper 单独也装不上时写放弃
    记号（JSON，程序空闲时由 Updater.retry_mlx_install 在后台重试）。核心依赖都装不上
    抛 PipFailed。返回 {"full_ok": bool, "failed_optional": [...]}。

    开始之前先 ensure_pip：.venv 里没有 pip 时原地补，补不回来抛 PipMissing（试着写了记号）。

    有放弃记号时 -r 用去掉 mlx-whisper 那一行的副本：否则记号机器每 24 小时（清单指纹
    一直记不上）就在启动时重下一遍 torch，把开播挡上几分钟。指纹照样按真正的
    requirements.txt 算——装成功了，启动就不再为这份清单补装。

    每一次 pip 都先带 -c constraints.txt（I4）：整份、只装核心、逐个装可选组件都带。以前只给下限，
    新装机拿到的是 PyPI 当天最新、从没一起跑过的组合；逐个装那几步不带的话，回退路径装出来的
    又是没测过的版本。项目根没有 constraints.txt 时照旧不带。

    回退路径上带 -c 的一步装不上时，这一步不带 -c 再装一次（_fallback_step）：已有的安装第一次
    碰到锁（从加锁以前的版本升级上来，或者锁的版本改了）时要补装，装着的版本和锁的不一样就得联网
    换版本；这时没联网，带 -c 的每一步都装不上，以前（加锁以前）核心组件照样「已经装好」、程序照常
    启动，加了锁却会抛 PipFailed、起不来。不带 -c 的那次和加锁以前一模一样，所以不比以前差。
    整份 -r 不这样重试：它成功了就写指纹，不带锁装成的也会记成「按锁装好了」，以后再也不按锁补装。
    这一轮整份没成，失败已经记下：24 小时内启动不再补装，过了 24 小时再按锁整份装一次。"""
    root = Path(root)
    ensure_pip(pip_python, root, log_path, run=run, probe=probe, now=now)
    base = [str(pip_python), "-m", "pip", "install", "--disable-pip-version-check"]
    pinned = constraints_args(root)
    digest = requirements_file_digest(root)
    req_file = root / "requirements.txt"
    filtered = None
    skip = skipped_for_this_machine(root)
    if skip:
        try:
            filtered = root / ".venv" / ".requirements-install.txt"
            filtered.write_text(filter_requirements(req_file.read_text(encoding="utf-8"), skip),
                                encoding="utf-8")
            req_file = filtered
        except (OSError, ValueError):
            filtered = None          # 写不了副本就用原文件：最多像以前一样多装一次
    try:
        code, _tail = run(base + ["-r", str(req_file)] + pinned, log_path)
    finally:
        if filtered is not None:
            try:
                filtered.unlink()
            except OSError:
                pass
    if code == 0:
        write_requirements_stamp(root, digest)
        return {"full_ok": True, "failed_optional": []}
    record_requirements_failure(root, digest, code, now=now)
    # 整份装不上时，先保证核心依赖，再**逐个**试可选包：一个包装不上不该连累其余。
    # 真实后果：一台 M4 Mac 因为某个可选包编译失败，mlx-whisper 跟着没装上，整机掉到
    # CPU 路径跑 large-v3-turbo（hwdetect 实测 RTF 0.99），而屏幕上什么也没说。
    print("[初始化] 完整安装失败，改为逐个安装…")
    code, tail, pinned = _fallback_step(run, base + list(core_deps), pinned, log_path)
    if code != 0:
        raise PipFailed(code, tail, log_path)
    failed = []
    for optional in optional_deps:
        c, _t, pinned = _fallback_step(run, base + [optional], pinned, log_path)
        if c != 0:
            print("[初始化] 可选组件 {} 没装上，其余功能不受影响".format(optional))
            failed.append(optional)
            if optional == "mlx-whisper":
                write_mlx_giveup(root / ".venv" / MLX_GIVEUP, c,
                                 "首次安装时单独安装 mlx-whisper 没成功", now=now)  # i18n: data
    return {"full_ok": False, "failed_optional": failed}


# ---- mlx-whisper 放弃记号 -----------------------------------------------------------------

def write_mlx_giveup(path, pip_exit, note, now=None):
    """放弃记号写成 JSON：{"at": ISO 时间, "pip_exit": 整数或 null, "note": 说明}。"""
    data = {"at": datetime.fromtimestamp(time.time() if now is None else now)
            .isoformat(timespec="seconds"),
            "pip_exit": pip_exit if isinstance(pip_exit, int) and not isinstance(pip_exit, bool)
            else None,
            "note": str(note or "")[:200]}
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def read_mlx_giveup(path):
    """读放弃记号；没有返回 None。旧版本写的纯文本（「装不上，改用 CPU 后端」）也认：
    at 为 None，ts 取文件修改时间。返回 {"at", "pip_exit", "note", "ts"}。"""
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    text = _read_text(p) or ""
    info = {"at": None, "pip_exit": None, "note": "", "ts": None}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        info["at"] = str(data["at"]) if data.get("at") else None
        pip_exit = data.get("pip_exit")
        if isinstance(pip_exit, int) and not isinstance(pip_exit, bool):
            info["pip_exit"] = pip_exit
        info["note"] = str(data.get("note") or "")
    else:
        info["note"] = text.strip()
    ts = None
    if info["at"]:
        try:
            ts = datetime.fromisoformat(info["at"]).timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            ts = None
    info["ts"] = ts if ts is not None else mtime
    return info


def clear_mlx_giveup(path):
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def mlx_retry_due(info, now=None):
    """距离上次失败（记号里的时间）够 24 小时了吗。时间读不出来或在未来，算到期。"""
    if info is None:
        return False
    ts = info.get("ts")
    if not ts:
        return True
    now = time.time() if now is None else now
    return not (0 <= now - ts < MLX_RETRY_SEC)


def mlx_giveup_note(root):
    """自检「语音识别」一行用：有放弃记号时说清楚哪天、pip 返回什么、程序接下来会做什么。
    没有记号返回 None。以前那一行说「重新打开会自动补装」，而记号在的时候重开根本不装。"""
    info = read_mlx_giveup(Path(root) / ".venv" / MLX_GIVEUP)
    if info is None:
        return None
    # 英文把日期和 pip 的返回码收进同一对括号：(attempted 2026-09-20, pip returned 1)。
    # 句末不加句号：自检那一行（selfcheck.check_asr）还要在后面接一句
    try:
        day = (datetime.fromtimestamp(info["ts"]).strftime("%Y-%m-%d") if info["ts"]
               else L("之前", "earlier"))
    except (OverflowError, OSError, ValueError):
        day = L("之前", "earlier")
    code = (L("（pip 返回 {}）", ", pip returned {}").format(info["pip_exit"])
            if info["pip_exit"] is not None else "")
    return (L("{} 安装 GPU 加速组件没成功{}，程序启动时不会再自动重试；没在监听时每天在后台"
              "重试一次，装好后会提示",
              "GPU acceleration couldn’t be installed (attempted {}{}). The app won’t retry "
              "when it starts. While it isn’t monitoring, it retries once a day in the "
              "background and lets you know once it’s installed").format(day, code))
