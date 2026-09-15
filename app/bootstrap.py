"""启动自举（main.py 的 ensure_env）里能单独测试的判断、文案和安装步骤。

main.py 在模块级就跑 ensure_env()，测试一 import 它就会被 execv 成 .venv 里的程序
（见 tests/test_bug_sweep2.py 里的说明），所以逻辑放在这里，main.py 只做调用。
这个模块在依赖还没装齐时就要能导入：只用标准库，兼容 Python 3.9。

这里管的几件事：
  * requirements 指纹：`pip install -r` 返回 0 之后记下清单的 sha256。清单变了
    （一键更新、手动 git pull）而 pip 没装上时，下次启动据此补装——以前启动只查
    固定的几个模块，新加的依赖永远没人补，「重新打开会自动继续安装」的承诺落空。
  * pip 输出落盘：logs/bootstrap-*.log，同时照样打到终端。双击 .app 启动时 stdout
    是 /dev/null，以前装失败了什么都不留。
  * 失败说明：按 pip 输出里看到的字样分成磁盘满、连不上软件包服务器、没有适用于
    当前 Python 的安装包三类，只写观察到的和能做的事；认不出的给通用说明，都附日志位置。
  * mlx-whisper 放弃记号：JSON（at / pip_exit / note），旧版本写的纯文本也认。
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

APP_DIR_NAME = "TikTok Live Translator.app"      # 与 app/macbundle.py 一致

REQ_STAMP = ".requirements.sha256"               # 放在 .venv 里：环境重建，指纹跟着没
REQ_ATTEMPT = ".requirements-attempt.json"
# 同一份清单装失败后多久内不再为它重跑 pip -r（真缺核心依赖时不受这个限制）
REQ_RETRY_SEC = 24 * 3600
MLX_GIVEUP = ".mlx-unavailable"
MLX_RETRY_SEC = 24 * 3600
KEEP_LOGS = 10
TAIL_LINES = 40

_WAIT = "完成后字幕窗口会自动打开——请耐心等待，不要重复打开程序。"


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


def requirements_digest(content):
    """清单指纹。换行统一成 LF：Windows 上工作区文件可能被检出成 CRLF，而一键更新
    用 `git show` 拿到的是仓库里的 LF 版本——同一份清单不能算出两个指纹。"""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def requirements_file_digest(root):
    try:
        return requirements_digest((Path(root) / "requirements.txt").read_bytes())
    except OSError:
        return None


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
    if (_read_text(venv / REQ_STAMP) or "").strip() == current:
        return False
    attempt = _read_json(venv / REQ_ATTEMPT)
    if attempt and attempt.get("digest") == current:
        at = _as_float(attempt.get("at"))
        now = time.time() if now is None else now
        if at is not None and 0 <= now - at < REQ_RETRY_SEC:
            return False
    return True


# ---- Python 版本漂移 ----------------------------------------------------------------------

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


def install_dialog_text(recorded=None, actual=None, venv_existed=False,
                        requirements_only=False):
    """安装开始时给中控看的那句话。只说观察到的事：清单变了、Python 版本变了、
    环境里缺组件、还是第一次装。"""
    if requirements_only:
        return "组件清单和上次安装时不一样了，正在补装运行组件（一般不到一分钟）。\n" + _WAIT
    if recorded and actual and recorded != actual:
        return ("Python 版本从 {} 变成了 {}，需要重新安装运行组件（约需 2–5 分钟，"
                "取决于网速）。\n".format(recorded, actual) + _WAIT)
    if venv_existed:
        return "运行组件不完整，正在自动补装（约需 2–5 分钟，取决于网速）。\n" + _WAIT
    return "首次运行：正在自动安装运行组件（约需 2–5 分钟，取决于网速）。\n" + _WAIT


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
            fh.write("$ {}\n".format(" ".join(str(c) for c in cmd)).encode("utf-8"))
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
        return code, "\n".join(tail)
    except Exception as exc:
        if fh is not None:
            try:
                fh.write("启动失败：{!r}\n".format(exc).encode("utf-8"))
            except OSError:
                pass
        raise
    finally:
        if fh is not None:
            try:
                fh.write("[退出码 {}]\n".format(code).encode("utf-8"))
                fh.close()
            except OSError:
                pass


class PipFailed(Exception):
    """核心依赖都没装上。带着退出码、输出末尾和日志位置，供 failure_text 分类。"""

    def __init__(self, exit_code, tail="", log_path=None):
        super().__init__("pip 返回 {}".format(exit_code))
        self.exit_code = exit_code
        self.tail = tail or ""
        self.log_path = log_path


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
_ADVANCED = "（进阶：也可手动运行 setup.sh / setup.ps1，或 pip install -r requirements.txt）"


def _free_gb(root):
    try:
        return shutil.disk_usage(str(root)).free / 1024 ** 3
    except (OSError, ValueError):
        return None


def pip_failure_text(tail, exit_code, log_path=None, python_version=None, root=None):
    """把一次失败的 pip 说成中控看得懂的话。

    只按输出里真实出现的字样分类，不猜：断网时 pip 最后一行也是「No matching
    distribution found」，所以先看有没有连接失败的字样，再谈 Python 版本。认不出的
    保留原来的网络提示，但一律附上日志位置，不再把 CalledProcessError 原文塞给中控。"""
    low = (tail or "").lower()
    if any(m in low for m in _DISK_MARKS):
        free = _free_gb(root) if root is not None else None
        head = ("安装运行组件时磁盘空间不够（pip 报告写不进去）。运行环境约需 1.4 GB，"
                "语音和翻译模型另需约 4 GB{}。\n腾出空间后重新打开本程序，会自动继续安装。"
                .format("，现在剩余 {:.1f} GB".format(free) if free is not None else ""))
    elif _PY_MARK in low or (any(m in low for m in _NO_DIST_MARKS)
                             and not any(m in low for m in _NET_MARKS)):
        head = ("pip 找不到适用于当前 Python {} 的安装包。\n"
                "请到 python.org 安装 Python 3.12 或 3.13，然后重新打开本程序。"
                .format(python_version or ""))
    elif any(m in low for m in _NET_MARKS):
        head = ("自动安装未完成：pip 连不上软件包服务器。\n"
                "请检查网络连接，然后重新打开本程序——会自动从中断处继续安装。")
    else:
        head = ("自动安装未完成（pip 返回 {}）。\n"
                "请检查网络连接，然后重新打开本程序——会自动从中断处继续安装。"
                .format(exit_code))
    where = "详细记录：{}\n".format(log_path) if log_path else ""
    return head + "\n" + where + _ADVANCED


def failure_text(exc, log_path=None, python_version=None, root=None):
    """ensure_env 里任何异常给中控的说明。不是 pip 本身失败的（建环境、起进程出错），
    把原话和堆栈写进日志，对话框里给日志位置。"""
    if isinstance(exc, PipFailed):
        return pip_failure_text(exc.tail, exc.exit_code, exc.log_path or log_path,
                                python_version, root)
    if log_path is None and root is not None:
        log_path = new_log_path(root, "bootstrap")
    written = False
    if log_path is not None:
        try:
            with open(str(log_path), "a", encoding="utf-8") as fh:
                fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            written = True
        except OSError:
            written = False
    detail = "详细记录：{}\n".format(log_path) if written else "（{}）\n".format(exc)
    return ("自动安装未完成。\n请检查网络连接，然后重新打开本程序——会自动从中断处继续安装。\n"
            + detail + _ADVANCED)


def install_requirements(pip_python, root, core_deps, optional_deps, log_path=None,
                         run=run_logged, now=None):
    """ensure_env 的安装步骤：整份 requirements；不行就先保核心依赖，再逐个装可选组件。

    整份 -r 返回 0 才写清单指纹；没成就记下这次失败。mlx-whisper 单独也装不上时写放弃
    记号（JSON，程序空闲时由 Updater.retry_mlx_install 在后台重试）。核心依赖都装不上
    抛 PipFailed。返回 {"full_ok": bool, "failed_optional": [...]}。"""
    root = Path(root)
    base = [str(pip_python), "-m", "pip", "install", "--disable-pip-version-check"]
    digest = requirements_file_digest(root)
    code, _tail = run(base + ["-r", str(root / "requirements.txt")], log_path)
    if code == 0:
        write_requirements_stamp(root, digest)
        return {"full_ok": True, "failed_optional": []}
    record_requirements_failure(root, digest, code, now=now)
    # 整份装不上时，先保证核心依赖，再**逐个**试可选包：一个包装不上不该连累其余。
    # 真实后果：一台 M4 Mac 因为某个可选包编译失败，mlx-whisper 跟着没装上，整机掉到
    # CPU 路径跑 large-v3-turbo（hwdetect 实测 RTF 0.99），而屏幕上什么也没说。
    print("[初始化] 完整安装失败，改为逐个安装…")
    code, tail = run(base + list(core_deps), log_path)
    if code != 0:
        raise PipFailed(code, tail, log_path)
    failed = []
    for optional in optional_deps:
        c, _t = run(base + [optional], log_path)
        if c != 0:
            print("[初始化] 可选组件 {} 没装上，其余功能不受影响".format(optional))
            failed.append(optional)
            if optional == "mlx-whisper":
                write_mlx_giveup(root / ".venv" / MLX_GIVEUP, c,
                                 "首次安装时单独安装 mlx-whisper 没成功", now=now)
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
    try:
        day = datetime.fromtimestamp(info["ts"]).strftime("%Y-%m-%d") if info["ts"] else "之前"
    except (OverflowError, OSError, ValueError):
        day = "之前"
    code = "（pip 返回 {}）".format(info["pip_exit"]) if info["pip_exit"] is not None else ""
    return ("{} 安装 GPU 加速组件没成功{}，程序启动时不会再自动重试；没在监听时每天在后台"
            "重试一次，装好后会提示".format(day, code))
