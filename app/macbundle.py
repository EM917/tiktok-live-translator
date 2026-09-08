"""macOS：让真正跑的那个进程住进 .app 里，系统才会按 Info.plist 登记名字和图标。

没有 bundle 的 python 进程，在 LaunchServices 那里的身份就是可执行文件名：Dock 悬停显示
「python3.13」，登记的图标是空白文档，运行时怎么补都只是叠加（2026-09-08 用
NSRunningApplication.icon 验证过）。py2app 一类打包对这个项目太重（venv 1.4 GB、模型
另算），这里用最薄的一层壳：

    TikTok Live Translator.app/Contents/
        MacOS/python  -> ../../../.venv/bin/python      同一个解释器，只是路径在 bundle 里
        pyvenv.cfg    （.venv/pyvenv.cfg 的副本）        CPython 按被调用路径找它，找到就把
        lib           -> ../../.venv/lib                 Contents 当 venv 前缀，site-packages
                                                          经链接落回 .venv，pip 也装进 .venv

实测（2026-09-08）：经这条路启动，sys.prefix 指向 Contents、是 venv、webview/mlx_whisper
都能 import、pip 的目标是 .venv、用 sys.executable 起的子进程同样是 venv、
NSBundle.mainBundle() 就是 .app。后两样文件含本机路径，启动时生成，不入库。

main.py 启动时若发现自己不在 bundle 里，就用 MacOS/python 重新执行自己一次。
"""
import os
import shutil
import sys
from pathlib import Path

APP_DIR_NAME = "TikTok Live Translator.app"
ENV_GUARD = "TLT_IN_BUNDLE"
# 这些参数下不开应用窗口，进不进 bundle 没有区别，别多一次 exec
NO_WINDOW_FLAGS = ("--browser", "--no-open", "--doctor", "-h", "--help")


def bundle_python(root):
    return Path(root) / APP_DIR_NAME / "Contents" / "MacOS" / "python"


def ensure_bundle_shell(root):
    """把三样壳文件放好；成功返回 bundle 里的 python 路径，否则 None。

    幂等：链接每次重指、pyvenv.cfg 内容不同才重写（venv 重建后 home/version 会变）。
    任何一步失败都返回 None——这层壳是锦上添花，不能挡住启动。"""
    root = Path(root)
    venv = root / ".venv"
    cfg = venv / "pyvenv.cfg"
    if not cfg.is_file() or not (venv / "bin" / "python").exists():
        return None
    contents = root / APP_DIR_NAME / "Contents"
    try:
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        _relink(contents / "MacOS" / "python", os.path.join("..", "..", "..", ".venv", "bin", "python"))
        _relink(contents / "lib", os.path.join("..", "..", ".venv", "lib"), is_dir=True)
        target = contents / "pyvenv.cfg"
        text = cfg.read_text(encoding="utf-8")
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
    except OSError:
        return None
    py = contents / "MacOS" / "python"
    return py if py.exists() else None


def _relink(link, target, is_dir=False):
    """把 link 指到 target（相对路径）。is_dir 只在 Windows 上有意义——那里目录链接
    和文件链接是两种东西；这个功能虽只在 macOS 生效，写对了测试才能跨平台跑。"""
    if link.is_symlink():
        if os.readlink(link) == target:
            return
        link.unlink()
    elif link.exists():
        # 有人放了个真文件/目录在这里：不动它，链接失败由上层判定
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)
        else:
            link.unlink()
    os.symlink(target, link, target_is_directory=is_dir)


def inside_bundle(executable=None):
    exe = Path(executable or sys.executable).resolve(strict=False)
    return any(p.name == APP_DIR_NAME for p in Path(executable or sys.executable).parents) \
        or any(p.name == APP_DIR_NAME for p in exe.parents)


def should_relaunch(root, argv, environ=None, platform=None, executable=None):
    """要不要进 bundle 重新执行一次。纯函数，方便测试。"""
    env = os.environ if environ is None else environ
    if (platform or sys.platform) != "darwin":
        return False
    if env.get(ENV_GUARD) or env.get("PYTEST_CURRENT_TEST"):
        return False
    if any(a in NO_WINDOW_FLAGS for a in argv[1:]):
        return False
    if inside_bundle(executable):
        return False
    return True


def relaunch_inside_bundle(root, argv=None, execve=os.execve):
    """条件满足就 execve 进 bundle 里的 python；不满足或壳没备好就原样返回。"""
    argv = list(sys.argv if argv is None else argv)
    if not should_relaunch(root, argv):
        return False
    py = ensure_bundle_shell(root)
    if py is None:
        return False
    env = dict(os.environ)
    env[ENV_GUARD] = "1"
    script = str(Path(root) / "main.py")
    try:
        execve(str(py), [str(py), script] + argv[1:], env)
    except OSError as exc:
        print("[信息] 未能以应用身份重新启动（{}），按普通进程继续".format(exc))
        return False
    return True   # execve 成功时不会执行到这里；给假的 execve 用
