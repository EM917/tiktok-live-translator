"""app/macbundle.py：让进程住进 .app 的那层壳；以及 .app 里入库的静态文件和启动器（N1）。

钉住：三样壳文件的生成与幂等刷新；没 venv 就不生成；只在 macOS、没有防循环
变量、不在 pytest、不带无窗口参数、且当前不在 bundle 里时才重新执行；
execve 的参数形状；壳没备好时静默放弃。测试里绝不真的 exec。

N1（spec §8.5、§8.6）：Info.plist 的两份 InfoPlist.strings 让 Dock、菜单栏里的程序名跟
macOS 系统语言，访达保持文件名；.app 启动器和 Start.command 在 Python 之前弹的对话框
中英并列。启动器是真的用 bash 跑的，但 PATH 里只有假的 osascript / open：不弹框、
不开浏览器。"""
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import i18n, macbundle

# 壳靠符号链接工作，功能只在 macOS 生效；Windows 跑器上 readlink 返回反斜杠、
# 链接权限也不稳定——纯逻辑（should_relaunch）跨平台测，依赖链接的只在 POSIX 上测
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="符号链接语义不同；功能仅 macOS 生效")


def _venv(root, version="3.13.5"):
    v = root / ".venv"
    (v / "bin").mkdir(parents=True)
    (v / "bin" / "python").write_text("#!/bin/sh\n")
    (v / "lib" / "python3.13" / "site-packages").mkdir(parents=True)
    (v / "pyvenv.cfg").write_text("home = /opt/x/bin\nversion = {}\n".format(version))
    return v


@posix_only
def test_shell_is_created_and_points_back_into_the_venv(tmp_path):
    _venv(tmp_path)
    py = macbundle.ensure_bundle_shell(tmp_path)
    contents = tmp_path / macbundle.APP_DIR_NAME / "Contents"
    assert py == contents / "MacOS" / "python" and py.exists()
    assert Path(os.readlink(contents / "MacOS" / "python")).parts == ("..", "..", "..", ".venv", "bin", "python")
    assert Path(os.readlink(contents / "lib")).parts == ("..", "..", ".venv", "lib")
    assert (contents / "lib" / "python3.13" / "site-packages").is_dir()   # 经链接落回 venv
    assert (contents / "pyvenv.cfg").read_text() == (tmp_path / ".venv" / "pyvenv.cfg").read_text()


@posix_only
def test_shell_refreshes_cfg_when_the_venv_changes(tmp_path):
    _venv(tmp_path)
    macbundle.ensure_bundle_shell(tmp_path)
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /opt/y/bin\nversion = 3.14.0\n")
    macbundle.ensure_bundle_shell(tmp_path)
    assert "3.14.0" in (tmp_path / macbundle.APP_DIR_NAME / "Contents" / "pyvenv.cfg").read_text()
    # 再跑一次不报错、链接不变
    assert macbundle.ensure_bundle_shell(tmp_path) is not None


def test_no_shell_without_a_venv(tmp_path):
    assert macbundle.ensure_bundle_shell(tmp_path) is None
    assert not (tmp_path / macbundle.APP_DIR_NAME).exists()


def test_should_relaunch_rules(tmp_path):
    argv = ["main.py"]
    exe_outside = "/opt/anaconda3/bin/python3.13"
    assert macbundle.should_relaunch(tmp_path, argv, environ={}, platform="darwin", executable=exe_outside)
    assert not macbundle.should_relaunch(tmp_path, argv, environ={}, platform="linux", executable=exe_outside)
    assert not macbundle.should_relaunch(tmp_path, argv, environ={macbundle.ENV_GUARD: "1"}, platform="darwin", executable=exe_outside)
    assert not macbundle.should_relaunch(tmp_path, argv, environ={"PYTEST_CURRENT_TEST": "x"}, platform="darwin", executable=exe_outside)
    for flag in ("--browser", "--no-open", "--doctor", "--help"):
        assert not macbundle.should_relaunch(tmp_path, ["main.py", flag], environ={}, platform="darwin", executable=exe_outside)
    inside = str(tmp_path / macbundle.APP_DIR_NAME / "Contents" / "MacOS" / "python")
    assert not macbundle.should_relaunch(tmp_path, argv, environ={}, platform="darwin", executable=inside)


@posix_only
def test_relaunch_execs_bundle_python_with_guard_and_args(tmp_path, monkeypatch):
    _venv(tmp_path)
    calls = []
    monkeypatch.delenv(macbundle.ENV_GUARD, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(macbundle.sys, "platform", "darwin")
    monkeypatch.setattr(macbundle.sys, "executable", "/opt/anaconda3/bin/python3.13")

    def fake_execve(path, argv, env):
        calls.append((path, argv, env.get(macbundle.ENV_GUARD)))

    ok = macbundle.relaunch_inside_bundle(tmp_path, ["main.py", "--port", "8799"], execve=fake_execve)
    assert ok and len(calls) == 1
    path, argv, guard = calls[0]
    assert path.endswith("Contents/MacOS/python")
    assert argv == [path, str(Path(tmp_path) / "main.py"), "--port", "8799"]
    assert guard == "1"                                     # 防循环


def test_relaunch_gives_up_quietly_without_a_venv(tmp_path, monkeypatch):
    monkeypatch.delenv(macbundle.ENV_GUARD, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(macbundle.sys, "platform", "darwin")
    monkeypatch.setattr(macbundle.sys, "executable", "/opt/anaconda3/bin/python3.13")
    calls = []
    assert macbundle.relaunch_inside_bundle(tmp_path, ["main.py"], execve=lambda *a: calls.append(a)) is False
    assert calls == []


@posix_only
def test_relaunch_survives_exec_failure(tmp_path, monkeypatch):
    _venv(tmp_path)
    monkeypatch.delenv(macbundle.ENV_GUARD, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(macbundle.sys, "platform", "darwin")
    monkeypatch.setattr(macbundle.sys, "executable", "/opt/anaconda3/bin/python3.13")

    def boom(*a):
        raise OSError("exec 被拒")

    assert macbundle.relaunch_inside_bundle(tmp_path, ["main.py"], execve=boom) is False


# ---- N1：程序名按系统语言、启动器对话框中英并列（spec §8.5、§8.6，用户 09-28 决定 2/4/5） ----

ROOT = Path(__file__).resolve().parent.parent
CONTENTS = ROOT / macbundle.APP_DIR_NAME / "Contents"
LAUNCHER = CONTENTS / "MacOS" / "TikTokLiveTranslator"
START_COMMAND = ROOT / "Start.command"
# lproj 名 → Dock、菜单栏里的程序名；与界面上的程序名是同一对（app/i18n.py 的 APP_NAME）
BUNDLE_NAMES = {"en": i18n.text(i18n.APP_NAME, i18n.EN), "zh-Hans": i18n.text(i18n.APP_NAME, i18n.ZH)}
BASH = "/bin/bash"
needs_bash = pytest.mark.skipif(sys.platform == "win32" or not os.path.exists(BASH),
                                reason="启动器是 macOS 的 bash 脚本")
on_macos = pytest.mark.skipif(sys.platform != "darwin", reason="用系统自己的工具核对，只在 macOS 上有")


def test_the_bundle_names_are_the_app_name_pair():
    assert BUNDLE_NAMES == {"en": "TikTok Live Translator", "zh-Hans": "TikTok 直播同传"}


def test_info_plist_localizes_the_name_but_keeps_the_finder_name():
    info = plistlib.loads((CONTENTS / "Info.plist").read_bytes())
    assert info["CFBundleDevelopmentRegion"] == "en"        # 两种都对不上的系统语言退到英文
    assert info["CFBundleLocalizations"] == ["en", "zh-Hans"]
    lproj = sorted(p.name[:-len(".lproj")] for p in (CONTENTS / "Resources").glob("*.lproj"))
    assert lproj == sorted(info["CFBundleLocalizations"])
    # 访达保持文件名（用户决定 5）：设了它，中文系统的访达里显示「TikTok 直播同传」，
    # 程序和 README 里「双击 TikTok Live Translator.app」的指引就对不上了
    assert "LSHasLocalizedDisplayName" not in info
    assert info["CFBundleExecutable"] == LAUNCHER.name
    assert info["CFBundleIdentifier"] == "io.github.em917.tiktok-live-translator"


_STRINGS_ENTRY = re.compile(r'"([^"\\]*)"\s*=\s*"([^"\\]*)";')


def _read_strings(path):
    """InfoPlist.strings 的最小解析：只认注释、空行和 "键" = "值"; 三种，别的写法当错。"""
    text = path.read_bytes().decode("utf-8")               # 严格 UTF-8，解不开就红
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _STRINGS_ENTRY.fullmatch(line.strip())
        assert m, "{}：读不懂 {!r}".format(path.name, line)
        assert m.group(1) not in out, "{} 写了两次".format(m.group(1))
        out[m.group(1)] = m.group(2)
    return out


@pytest.mark.parametrize("lang", sorted(BUNDLE_NAMES))
def test_each_lproj_names_the_app_in_its_language(lang):
    path = CONTENTS / "Resources" / "{}.lproj".format(lang) / "InfoPlist.strings"
    name = BUNDLE_NAMES[lang]
    assert _read_strings(path) == {"CFBundleName": name, "CFBundleDisplayName": name}


def test_the_strings_reader_rejects_what_it_cannot_read(tmp_path):
    """上面那条的解析器读不懂就该红，不能把整行跳过去假装通过。"""
    bad = tmp_path / "InfoPlist.strings"
    bad.write_text('/* x */\n"CFBundleName" = "A"\n', encoding="utf-8")     # 少了分号
    with pytest.raises(AssertionError):
        _read_strings(bad)
    bad.write_bytes('"CFBundleName" = "直播";\n'.encode("utf-16"))
    with pytest.raises(UnicodeDecodeError):
        _read_strings(bad)


@on_macos
@pytest.mark.parametrize("lang", sorted(BUNDLE_NAMES))
def test_macos_parses_the_strings_the_same_way(lang):
    """系统自己的解析器（plutil 与 CFBundle 同一套）读出的键值和上面一致。"""
    path = CONTENTS / "Resources" / "{}.lproj".format(lang) / "InfoPlist.strings"
    out = subprocess.run(["plutil", "-convert", "json", "-o", "-", str(path)],
                         capture_output=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == _read_strings(path)


# 启动器对话框：(中文, English)。中文是 N1 之前的原句，一字不动；段落之间的 \n 是 AppleScript
# 字符串里的转义，原样出现在 osascript 的参数里
DIALOG_TITLE = "TikTok 直播同传 · TikTok Live Translator"
NO_MAIN = (
    "找不到程序文件。\\n\\n请把「TikTok Live Translator」放回 tiktok-live-translator 文件夹里再双击。"
    "想常驻可以把它拖到 Dock——但不要把它拖出文件夹。",
    "Couldn’t find the app’s files.\\n\\nPut “TikTok Live Translator” back in the "
    "tiktok-live-translator folder, then double-click it again. You can drag it to the Dock, "
    "but don’t move it out of the folder.")
NO_FINDER = (
    "缺少程序文件 tools/find_python.sh，项目文件不完整，无法探测 Python。\\n\\n"
    "请把「TikTok Live Translator」放回 tiktok-live-translator 文件夹里再双击，或重新下载完整项目。",
    "The file tools/find_python.sh is missing. The project folder is incomplete, so the app "
    "can’t check for Python.\\n\\nPut “TikTok Live Translator” back in the tiktok-live-translator "
    "folder and double-click it again, or download a fresh copy of the complete project.")
NO_PYTHON = (
    "需要先安装 Python 才能运行本工具（免费，约 2 分钟）。\\n\\n已为你打开下载页面 "
    "python.org/downloads——下载 macOS 安装包并安装，装好后重新双击启动即可。",
    "Python is needed to run this app (free, about 2 minutes).\\n\\nThe python.org/downloads "
    "page is now open. Download and install the macOS installer, then double-click the app again.")
PYTHON_PAGE = "https://www.python.org/downloads/"


def _dialog(texts, icon):
    zh, en = texts
    return ["osascript", "-e", 'display dialog "{}\\n\\n{}" with title "{}" buttons {{"OK"}} '
            "default button 1 with icon {}".format(zh, en, DIALOG_TITLE, icon)]


@pytest.mark.parametrize("texts", [NO_MAIN, NO_FINDER, NO_PYTHON])
def test_each_dialog_is_chinese_then_english(texts):
    """这些对话框在 Python 之前弹出，读不到界面语言设置：两段并列，不按系统语言挑。
    文字直接拼进 AppleScript 的字符串，所以不许有 ASCII 双引号，反斜杠只许是 \\n。"""
    zh, en = texts
    for part in zh.split("\\n\\n"):
        assert i18n.CJK.search(part), part
    assert not i18n.CJK.search(en), en
    for text in texts:
        assert '"' not in text and "\\" not in text.replace("\\n", ""), text


def _run_launcher(tmp_path, script):
    """用 bash 真跑一遍启动脚本；PATH 里只有假的 osascript / open 和真的 dirname。
    返回 (进程结果, 按顺序的 [命令名, 参数…] 列表)。"""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    for name in ("osascript", "open"):
        fake = fake_bin / name
        # 参数之间用 \037、每次调用之后用 \036 分隔：AppleScript 里可能有空格和换行
        fake.write_text('#!/bin/bash\n{ printf \'%s\\037\' "${0##*/}" "$@"; printf \'\\036\'; } >> "$TLT_FAKE_LOG"\n',
                        encoding="utf-8")
        fake.chmod(0o755)
    (fake_bin / "dirname").symlink_to(shutil.which("dirname"))
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path), "TLT_FAKE_LOG": str(log)}
    proc = subprocess.run([BASH, str(script)], env=env, cwd=str(tmp_path), capture_output=True, timeout=60)
    records = log.read_text(encoding="utf-8").split("\036") if log.exists() else []
    return proc, [r.split("\037")[:-1] for r in records if r]


def _project(tmp_path, *, main_py, finder):
    """临时项目目录：启动器放在 app/Contents/MacOS/ 下（它按 ../../.. 找项目根）。
    finder 为 True 时放一个永远找不到 Python 的 tools/find_python.sh。"""
    root = tmp_path / "project"
    macos = root / "app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    shutil.copy2(LAUNCHER, macos / LAUNCHER.name)
    shutil.copy2(START_COMMAND, root / START_COMMAND.name)
    if main_py:
        (root / "main.py").write_text("raise SystemExit('不该跑到这里')\n", encoding="utf-8")
    if finder:
        (root / "tools").mkdir()
        (root / "tools" / "find_python.sh").write_text("find_python() { return 1; }\n", encoding="utf-8")
    return root


@needs_bash
@pytest.mark.parametrize("main_py, finder, expected", [
    (False, False, [_dialog(NO_MAIN, "caution")]),
    (True, False, [_dialog(NO_FINDER, "caution")]),
    (True, True, [["open", PYTHON_PAGE], _dialog(NO_PYTHON, "note")]),
], ids=["no-main-py", "no-find-python", "no-python"])
def test_the_app_launcher_dialogs(tmp_path, main_py, finder, expected):
    root = _project(tmp_path, main_py=main_py, finder=finder)
    proc, calls = _run_launcher(tmp_path, root / "app" / "Contents" / "MacOS" / LAUNCHER.name)
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    assert calls == expected


@needs_bash
def test_start_command_dialog_is_bilingual_but_the_terminal_stays_chinese(tmp_path):
    """用户决定 4：终端说明不英文化，只有缺 Python 的系统对话框中英并列（与启动器同一句）。"""
    root = _project(tmp_path, main_py=True, finder=True)
    proc, calls = _run_launcher(tmp_path, root / START_COMMAND.name)
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    assert calls == [["open", PYTHON_PAGE], _dialog(NO_PYTHON, "note")]
    out = proc.stdout.decode("utf-8")
    assert "[未检测到 Python] 需要先安装 Python 才能运行（免费，约 2 分钟）：" in out
    assert "Python is needed" not in out


def test_the_launchers_keep_lf_line_endings_in_every_checkout():
    """启动器是 bash 脚本：core.autocrlf=true 的检出（全局配置写错的 macOS / Linux 克隆）里变成
    CRLF，bash 报「: command not found」「unexpected end of file」，双击 .app 直接失败。
    Start.command 靠 *.command 规则；.app 里的启动器没有扩展名，要按文件名单独固定
    （复审 per-commit-ci）。CI 里只有 Windows 跑器用 autocrlf，而那里不跑 bash，所以在这里钉。"""
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("不是 git 检出")
    paths = [p.relative_to(ROOT).as_posix() for p in (LAUNCHER, START_COMMAND)]
    out = subprocess.run([git, "check-attr", "eol", "--"] + paths, cwd=ROOT,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["{}: eol: lf".format(p) for p in paths]


@needs_bash
@pytest.mark.parametrize("script", [LAUNCHER, START_COMMAND], ids=lambda p: p.name)
def test_the_launchers_parse(script):
    proc = subprocess.run([BASH, "-n", str(script)], capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


@on_macos
@pytest.mark.parametrize("texts, icon", [(NO_MAIN, "caution"), (NO_FINDER, "caution"), (NO_PYTHON, "note")],
                         ids=["no-main-py", "no-find-python", "no-python"])
def test_the_dialog_scripts_compile_as_applescript(tmp_path, texts, icon):
    """osacompile 只编译不运行：不弹框。弯引号、· 和中文都在 AppleScript 字符串里。"""
    if not shutil.which("osacompile"):
        pytest.skip("没有 osacompile")
    argv = _dialog(texts, icon)
    proc = subprocess.run(["osacompile", "-e", argv[2], "-o", str(tmp_path / "dialog.scpt")],
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
