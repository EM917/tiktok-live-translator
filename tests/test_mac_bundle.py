"""app/macbundle.py：让进程住进 .app 的那层壳；以及 .app 里入库的静态文件和启动器（N1）。

钉住：三样壳文件的生成与幂等刷新；没 venv 就不生成；只在 macOS、没有防循环
变量、不在 pytest、不带无窗口参数、且当前不在 bundle 里时才重新执行；
execve 的参数形状；壳没备好时静默放弃。测试里绝不真的 exec。

N1（spec §8.5、§8.6）：Info.plist 的三份 InfoPlist.strings 让 Dock、菜单栏里的程序名跟
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
# lproj 名 → Dock、菜单栏里的程序名；与界面上的程序名是同一对（app/i18n.py 的 APP_NAME）。
# 繁体中文系统上界面也是简体中文（_lang_of_tag 把 zh 开头的都算中文），程序名跟界面一致
BUNDLE_NAMES = {"en": i18n.text(i18n.APP_NAME, i18n.EN), "zh-Hans": i18n.text(i18n.APP_NAME, i18n.ZH),
                "zh-Hant": i18n.text(i18n.APP_NAME, i18n.ZH)}
BASH = "/bin/bash"
needs_bash = pytest.mark.skipif(sys.platform == "win32" or not os.path.exists(BASH),
                                reason="启动器是 macOS 的 bash 脚本")
on_macos = pytest.mark.skipif(sys.platform != "darwin", reason="用系统自己的工具核对，只在 macOS 上有")


def test_the_bundle_names_are_the_app_name_pair():
    assert BUNDLE_NAMES == {"en": "TikTok Live Translator", "zh-Hans": "TikTok 直播同传",
                            "zh-Hant": "TikTok 直播同传"}


def test_info_plist_localizes_the_name_but_keeps_the_finder_name():
    info = plistlib.loads((CONTENTS / "Info.plist").read_bytes())
    assert info["CFBundleDevelopmentRegion"] == "en"        # 两种都对不上的系统语言退到英文
    assert info["CFBundleLocalizations"] == ["en", "zh-Hans", "zh-Hant"]
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
@pytest.mark.parametrize("prefs", [["zh-Hans-CN"], ["zh-CN"], ["zh-Hans-US", "en-US"], ["zh-Hant-TW"],
                                   ["zh-TW"], ["zh-HK"], ["zh-Hant-HK"], ["en-US"], ["en-GB", "zh-Hans"],
                                   ["ja-JP"], ["es-MX"]])
def test_the_dock_name_and_the_ui_agree_on_chinese(prefs):
    """复审（english-e2e）：以前只有 en、zh-Hans 两份，繁体中文系统上 macOS 挑 en.lproj，Dock 叫
    TikTok Live Translator，界面却是中文（用户决定 2：中文系统叫「TikTok 直播同传」）。用系统自己
    挑本地化的那个函数核对：程序名是中文，当且仅当界面按系统语言解析成中文。"""
    foundation = pytest.importorskip("Foundation")
    info = plistlib.loads((CONTENTS / "Info.plist").read_bytes())
    picked = foundation.NSBundle.preferredLocalizationsFromArray_forPreferences_(
        info["CFBundleLocalizations"], prefs)
    name = BUNDLE_NAMES[str(picked[0])]
    assert (name == i18n.text(i18n.APP_NAME, i18n.ZH)) is (i18n._lang_of_tag(prefs[0]) == i18n.ZH)


@on_macos
@pytest.mark.parametrize("lang", sorted(BUNDLE_NAMES))
def test_macos_parses_the_strings_the_same_way(lang):
    """系统自己的解析器（plutil 与 CFBundle 同一套）读出的键值和上面一致。"""
    path = CONTENTS / "Resources" / "{}.lproj".format(lang) / "InfoPlist.strings"
    out = subprocess.run(["plutil", "-convert", "json", "-o", "-", str(path)],
                         capture_output=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == _read_strings(path)


# 启动器对话框：(中文, English)。段落之间的 \n 是 AppleScript 字符串里的转义，原样出现在
# osascript 的参数里。NO_MAIN、NO_FINDER 以前叫人放回「tiktok-live-translator 文件夹」，
# 而 release 的 ZIP 解出来叫「TikTok Live Translator」（release.sh 的 --prefix），I3 改成
# 「它原来所在的那个文件夹」；别的字没动
DIALOG_TITLE = "TikTok 直播同传 · TikTok Live Translator"
NO_MAIN = (
    "找不到程序文件。\\n\\n请把「TikTok Live Translator」放回它原来所在的那个文件夹里再双击。"
    "想常驻可以把它拖到 Dock——但不要把它拖出文件夹。",
    "Couldn’t find the app’s files.\\n\\nPut “TikTok Live Translator” back in the "
    "folder it came from, then double-click it again. You can drag it to the Dock, "
    "but don’t move it out of the folder.")
NO_FINDER = (
    "缺少程序文件 tools/find_python.sh，项目文件不完整，无法探测 Python。\\n\\n"
    "请把「TikTok Live Translator」放回它原来所在的那个文件夹里再双击，或重新下载完整项目。",
    "The file tools/find_python.sh is missing. The project folder is incomplete, so the app "
    "can’t check for Python.\\n\\nPut “TikTok Live Translator” back in the folder it came "
    "from and double-click it again, or download a fresh copy of the complete project.")
# I3：macOS 把 app 放到临时位置运行（App Translocation）时的那一句。补救办法按实测只给 xattr
# 这一步（启动器里的注释写了实测经过），命令预填在输入框里
TRANSLOCATED = (
    "macOS 把本程序放到了一个临时位置运行，在那里找不到它所在文件夹里的其它程序文件。\\n\\n"
    "请照做一次：打开「终端」，输入下面这行命令和一个空格，再把 app 所在的那个文件夹拖进终端窗口，"
    "按回车。然后重新双击 app。",
    "macOS is running the app from a temporary location, where it can’t find the other files in "
    "its folder.\\n\\nDo this once: open Terminal, type the command below followed by a space, "
    "drag the folder that contains the app into the Terminal window, and press Return. Then "
    "double-click the app again.")
UNQUARANTINE = "xattr -dr com.apple.quarantine"
# D14（b3-deps）：最低版本提到 3.10，启动器不再认 3.9——只装着 3.9 的 Mac 也会走到这一句，所以写明版本
NO_PYTHON = (
    "需要先安装 Python 3.10 或更新的版本才能运行本工具（免费，约 2 分钟）。\\n\\n已为你打开下载页面 "
    "python.org/downloads——下载 macOS 安装包并安装，装好后重新双击启动即可。",
    "Python 3.10 or later is needed to run this app (free, about 2 minutes).\\n\\nThe "
    "python.org/downloads page is now open. Download and install the macOS installer, then "
    "double-click the app again.")
PYTHON_PAGE = "https://www.python.org/downloads/"
# tools/find_python.sh 打得开，载入以后却没有 find_python（文件被截断、内容不对）。以前这时 find_python
# 报 command not found，照样走进上面 NO_PYTHON 那一支
BROKEN_FINDER = (
    "tools/find_python.sh 读得到，但里面没有探测 Python 的那一段。项目文件不完整，无法探测 Python。\\n\\n"
    "请重新下载完整项目。",
    "tools/find_python.sh can be read, but the part that checks for Python isn’t in it. The project folder "
    "is incomplete, so the app can’t check for Python.\\n\\nDownload a fresh copy of the complete project.")

# ---- 2026-10-29（tccfix）：文件在、读不了 --------------------------------------------------------
# 新 Mac（macOS 26，程序在开着 iCloud 同步的桌面上）从访达、聚焦搜索双击 .app：[ -f ] 都过了（stat 能做），
# `. tools/find_python.sh` 却报 Operation not permitted，find_python 没定义，启动器弹 NO_PYTHON、还打开了
# python.org——Python 早就装好了；从终端用 Start.command 启动一切正常。现在先真的打开一次：读不了就照实说
# 读不了（系统返回的原话照抄），给系统设置和 Start.command 两条路，不提 Python。
# 这几段带路径，作为参数交给 AppleScript（on run argv），不拼进脚本：路径里有引号、反斜杠都原样显示。
# 段落之间是真的换行
FILES_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"
FDA_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
ASK_BUTTONS = ("关闭 · Close", "先用终端打开 · Open in Terminal", "打开系统设置 · Open System Settings")
ASK_SCRIPT = (
    "on run argv",
    'set b to button returned of (display dialog (item 1 of argv) default answer (item 2 of argv) with title "{}" '
    'buttons {{"{}", "{}", "{}"}} default button 3 cancel button 1 with icon caution)'.format(DIALOG_TITLE, *ASK_BUTTONS),
    'if b is "{}" then return "settings"'.format(ASK_BUTTONS[2]),
    'if b is "{}" then return "terminal"'.format(ASK_BUTTONS[1]),
    "end run",
)
SAY_SCRIPT = (
    "on run argv",
    'display dialog (item 1 of argv) with title "{}" buttons {{"OK"}} default button 1 with icon caution'.format(
        DIALOG_TITLE),
    "end run",
)
FOLDER_DENIED = (
    "macOS 没让本程序读它所在的文件夹（{dir}），读文件时系统返回「{why}」。\n\n"
    "照下面做一次：\n"
    "1. 点「打开系统设置」，在「隐私与安全性」→「文件与文件夹」里找到本程序（TikTok Live Translator 或 "
    "TikTok 直播同传），打开它下面{folder}的开关。\n"
    "2. 列表里没有它时，到「隐私与安全性」→「完全磁盘访问权限」，点「+」，在选文件的窗口里按 ⌘⇧G，"
    "粘贴下面输入框里的路径后回车、点「打开」，再把它的开关打开。\n"
    "3. 重新双击本程序。\n\n"
    "也可以点「先用终端打开」：用「终端」运行同一文件夹里的 Start.command，以后也可以自己双击它。"
    "本程序不会自己改系统设置。",
    "macOS didn’t let the app read the folder it’s in ({dir}). Reading a file there returned “{why}”.\n\n"
    "Do this once:\n"
    "1. Click Open System Settings. In Privacy & Security > Files & Folders, find TikTok Live Translator "
    "and turn on {folder}.\n"
    "2. If it isn’t in that list, go to Privacy & Security > Full Disk Access, click +, press ⌘⇧G in the "
    "file window, paste the path from the box below, press Return, click Open, and turn on its switch.\n"
    "3. Double-click the app again.\n\n"
    "Or click Open in Terminal to run Start.command from the same folder in Terminal. You can also "
    "double-click Start.command yourself later. The app never changes system settings itself.")
# 「文件与文件夹」里每个 app 下面的开关名，照抄系统设置（macOS 26 的 SecurityPrivacyExtension 里
# DESKTOP_FOLDER 等几条的 en / zh_CN）；不在这三个文件夹里时不点名
FOLDER_SWITCHES = {
    "Desktop": ("「“桌面”文件夹」", "its Desktop Folder switch"),
    "Documents": ("「“文稿”文件夹」", "its Documents Folder switch"),
    "Downloads": ("「“下载”文件夹」", "its Downloads Folder switch"),
    None: ("本程序所在位置对应的那一项", "the switch under it for the place the app is in"),
}
# 打不开、系统返回的又不是 Operation not permitted：只说读不了和原话，不给系统设置那几步。Permission denied
# 也走这一句（chmod 000 做得出来）：那是文件自己的读写权限，系统设置里的开关管不着
FILE_UNREADABLE = (
    "读不了本程序文件夹里的文件（{file}），系统返回「{why}」。\n\n"
    "可以先双击同一文件夹里的 Start.command，在「终端」里打开本程序。还是不行的话，重新下载完整项目。",
    "Couldn’t read a file in the app’s folder ({file}). The system returned “{why}”.\n\n"
    "You can double-click Start.command in the same folder to open the app in Terminal instead. If that "
    "doesn’t work either, download a fresh copy of the complete project.")
SETTINGS_FAILED = (
    "没能打开「系统设置」（open 返回 {rc}）。可以自己打开「系统设置」→「隐私与安全性」→「文件与文件夹」。",
    "Couldn’t open System Settings (open returned {rc}). You can open System Settings > Privacy & Security > "
    "Files & Folders yourself.")
TERMINAL_FAILED = (
    "没能用「终端」打开 Start.command（open 返回 {rc}）。可以在访达里打开本程序所在的文件夹，双击 Start.command。",
    "Couldn’t open Start.command in Terminal (open returned {rc}). You can open the app’s folder in Finder and "
    "double-click Start.command.")
ARGV_DIALOGS = {"folder_denied": FOLDER_DENIED, "file_unreadable": FILE_UNREADABLE,
                "settings_failed": SETTINGS_FAILED, "terminal_failed": TERMINAL_FAILED}


def _e(lines):
    return [x for line in lines for x in ("-e", line)]


def _ask(root, why, folder=None):
    """权限对话框那一次 osascript：脚本、正文、输入框里预填的 app 路径。"""
    zh_folder, en_folder = FOLDER_SWITCHES[folder]
    zh, en = FOLDER_DENIED
    text = (zh.format(dir=root, why=why, folder=zh_folder) + "\n\n"
            + en.format(dir=root, why=why, folder=en_folder))
    return ["osascript"] + _e(ASK_SCRIPT) + [text, str(root / "app")]


def _say(texts, **fields):
    zh, en = texts
    return ["osascript"] + _e(SAY_SCRIPT) + [zh.format(**fields) + "\n\n" + en.format(**fields)]


def _dialog(texts, icon, answer=None):
    zh, en = texts
    field = ' default answer "{}"'.format(answer) if answer else ""
    return ["osascript", "-e", 'display dialog "{}\\n\\n{}"{} with title "{}" buttons {{"OK"}} '
            "default button 1 with icon {}".format(zh, en, field, DIALOG_TITLE, icon)]


@pytest.mark.parametrize("texts", [NO_MAIN, NO_FINDER, NO_PYTHON, TRANSLOCATED, (UNQUARANTINE, UNQUARANTINE),
                                   BROKEN_FINDER])
def test_each_dialog_is_chinese_then_english(texts):
    """这些对话框在 Python 之前弹出，读不到界面语言设置：两段并列，不按系统语言挑。
    文字直接拼进 AppleScript 的字符串，所以不许有 ASCII 双引号，反斜杠只许是 \\n。"""
    zh, en = texts
    if zh != UNQUARANTINE:          # 预填的命令两种语言一样，只查引号和反斜杠
        for part in zh.split("\\n\\n"):
            assert i18n.CJK.search(part), part
    assert not i18n.CJK.search(en), en
    for text in texts:
        assert '"' not in text and "\\" not in text.replace("\\n", ""), text


def _run_launcher(tmp_path, script, *, answer="", open_rc=0, wrap=()):
    """用 bash 真跑一遍启动脚本；PATH 里只有假的 osascript / open 和真的 dirname。
    返回 (进程结果, 按顺序的 [命令名, 参数…] 列表)。

    answer 是假 osascript 往 stdout 打的那一行（带按钮的对话框里「点了哪个」，空串就是关掉了），
    open_rc 是假 open 的退出码，wrap 套在 bash 前面（sandbox-exec 那一条用）。"""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    tails = {"osascript": '[ -z "$TLT_FAKE_ANSWER" ] || printf \'%s\\n\' "$TLT_FAKE_ANSWER"\n',
             "open": 'exit "${TLT_FAKE_OPEN_RC:-0}"\n'}
    for name in ("osascript", "open"):
        fake = fake_bin / name
        # 参数之间用 \037、每次调用之后用 \036 分隔：AppleScript 里可能有空格和换行
        fake.write_text('#!/bin/bash\n{ printf \'%s\\037\' "${0##*/}" "$@"; printf \'\\036\'; } >> "$TLT_FAKE_LOG"\n'
                        + tails[name], encoding="utf-8")
        fake.chmod(0o755)
    (fake_bin / "dirname").symlink_to(shutil.which("dirname"))
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path), "TLT_FAKE_LOG": str(log),
           "TLT_FAKE_ANSWER": answer, "TLT_FAKE_OPEN_RC": str(open_rc)}
    proc = subprocess.run(list(wrap) + [BASH, str(script)], env=env, cwd=str(tmp_path), capture_output=True,
                          timeout=60)
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
@pytest.mark.parametrize("main_py", [False, True], ids=["bundle-only", "with-main-py"])
def test_a_translocated_launch_says_so_instead_of_asking_to_put_the_app_back(tmp_path, main_py):
    """I3：从「下载」里直接双击刚解压的 app，macOS 可能把 app 单独放到
    /private/var/folders/…/AppTranslocation/<UUID>/d/ 下运行，旁边没有 main.py。以前这里弹
    「找不到程序文件，请放回 tiktok-live-translator 文件夹」——app 本来就在文件夹里，照做没用。
    现在认出路径里的 /AppTranslocation/，说明是临时位置，给出 xattr 那一步（命令预填在输入框里）。
    路径里带空格，和真的 app 名一样；旁边就算有 main.py 也不往下跑（临时位置里不该有它）。"""
    base = tmp_path / "var folders" / "T" / "AppTranslocation" / "6F1C2E0A-0000-4000-8000-000000000001" / "d"
    macos = base / macbundle.APP_DIR_NAME / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    shutil.copy2(LAUNCHER, macos / LAUNCHER.name)
    if main_py:
        (base / "main.py").write_text("raise SystemExit('不该跑到这里')\n", encoding="utf-8")
    proc, calls = _run_launcher(tmp_path, macos / LAUNCHER.name)
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    assert calls == [_dialog(TRANSLOCATED, "caution", UNQUARANTINE)]


@needs_bash
def test_a_folder_merely_named_like_translocation_is_not_mistaken_for_it(tmp_path):
    """只认路径里完整的 /AppTranslocation/ 一节：文件夹名里碰巧带这个词的照旧往下走。"""
    root = _project(tmp_path, main_py=False, finder=False)
    odd = tmp_path / "MyAppTranslocationNotes"
    root.rename(odd)
    proc, calls = _run_launcher(tmp_path, odd / "app" / "Contents" / "MacOS" / LAUNCHER.name)
    assert proc.returncode == 1
    assert calls == [_dialog(NO_MAIN, "caution")]


@needs_bash
def test_start_command_dialog_is_bilingual_but_the_terminal_stays_chinese(tmp_path):
    """用户决定 4：终端说明不英文化，只有缺 Python 的系统对话框中英并列（与启动器同一句）。"""
    root = _project(tmp_path, main_py=True, finder=True)
    proc, calls = _run_launcher(tmp_path, root / START_COMMAND.name)
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    assert calls == [["open", PYTHON_PAGE], _dialog(NO_PYTHON, "note")]
    out = proc.stdout.decode("utf-8")
    assert ("[未检测到 Python 3.10 或更新的版本] 需要先安装 Python 3.10 或更新的版本才能运行"
            "（免费，约 2 分钟）：") in out
    assert "is needed to run" not in out


# ---- tccfix：文件在、读不了时说读不了，不送人去 python.org ---------------------------------------
# chmod 000 做出「在、读不了」（系统返回 Permission denied）；root 不受文件权限管，Windows 不跑 bash
needs_unreadable = pytest.mark.skipif(
    sys.platform == "win32" or not os.path.exists(BASH) or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="要用 chmod 000 做出读不了的文件：Windows 不跑 bash，root 不受文件权限管")
APP_LAUNCHER = ("app", "Contents", "MacOS", LAUNCHER.name)


def _unreadable(path):
    path.chmod(0)
    return path


def _deny_reading(root, *, only=None):
    """sandbox-exec 拒掉读文件内容（file-read-data），stat 照样能做：打开时系统返回 Operation not permitted，
    和 10-29 那台机器上一样（系统的隐私保护走的是同一个 Sandbox 机制）。默认拒整个项目文件夹、只留 app 自己的
    包读得到——那台机器上启动脚本本身读得到，旁边的文件读不到；only 给了就只拒项目里的这一个文件。
    文件夹权限那个对话框只有这样做得出来：macOS 上用不了 sandbox-exec 时算失败，不悄悄跳过（跳过就等于没测）。"""
    sandbox = shutil.which("sandbox-exec")
    assert sandbox, "macOS 上找不到 sandbox-exec"
    probe = subprocess.run([sandbox, "-p", "(version 1)(allow default)", "/usr/bin/true"], capture_output=True,
                           timeout=30)
    assert probe.returncode == 0, probe.stderr.decode("utf-8", "replace")
    real = root.resolve()
    if only:
        rules = '(deny file-read-data (literal "{}"))'.format(real / only)
    else:
        rules = '(deny file-read-data (subpath "{}"))(allow file-read-data (subpath "{}"))'.format(real, real / "app")
    return [sandbox, "-p", "(version 1)(allow default)" + rules]


@needs_unreadable
@pytest.mark.parametrize("name", ["main.py", "tools/find_python.sh"])
def test_a_file_that_is_there_but_unreadable_says_so_instead_of_sending_you_to_python_org(tmp_path, name):
    """2026-10-29：`. tools/find_python.sh` 读不了，find_python 没定义，以前弹「需要先安装 Python」、打开
    python.org。现在 main.py 和 find_python.sh 都先真的打开一次，读不了就照实说哪个文件读不了、系统返回了什么。
    chmod 000 时系统返回 Permission denied：那是文件自己的读写权限，系统设置里的开关管不着，所以不给
    「文件与文件夹」那几步和按钮，什么也不打开。"""
    root = _project(tmp_path, main_py=True, finder=True)
    _unreadable(root / name)
    proc, calls = _run_launcher(tmp_path, root.joinpath(*APP_LAUNCHER))
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    assert calls == [_say(FILE_UNREADABLE, file=str(root / name), why="Permission denied")]


@on_macos
@needs_bash
@pytest.mark.parametrize("only", [None, "main.py", "tools/find_python.sh"], ids=["folder", "main-py", "find-python"])
def test_a_denial_like_the_one_on_the_new_mac(tmp_path, only):
    """和 10-29 那台机器上一样的拒绝：stat 能做，打开读报 Operation not permitted。程序在桌面上。
    整个文件夹读不了、只有 main.py 或只有 find_python.sh 读不了，都弹文件夹权限那一个对话框。"""
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    root = _project(desktop, main_py=True, finder=True)
    proc, calls = _run_launcher(tmp_path, root.joinpath(*APP_LAUNCHER), wrap=_deny_reading(root, only=only))
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    assert calls == [_ask(root, "Operation not permitted", "Desktop")]


@on_macos
@needs_bash
@pytest.mark.parametrize("folder", ["Desktop", "Documents", "Downloads", None],
                         ids=["desktop", "documents", "downloads", "elsewhere"])
def test_the_dialog_names_the_switch_for_the_protected_folder(tmp_path, folder):
    """「文件与文件夹」里每个 app 下面是「“桌面”文件夹」这样的开关：程序在哪个文件夹里就点哪一个的名，
    不在这三个里时不点名。"""
    home_folder = tmp_path / (folder or "elsewhere")
    home_folder.mkdir()
    root = _project(home_folder, main_py=True, finder=True)
    proc, calls = _run_launcher(tmp_path, root.joinpath(*APP_LAUNCHER), wrap=_deny_reading(root))
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    assert calls == [_ask(root, "Operation not permitted", folder)]


@on_macos
@needs_bash
@pytest.mark.parametrize("answer, open_rc, rc, tail", [
    ("settings", 0, 1, lambda root: [["open", FILES_SETTINGS]]),
    # 「文件与文件夹」那一页打不开就退到「完全磁盘访问权限」那一页；也不成就照实说 open 返回了什么
    ("settings", 1, 1, lambda root: [["open", FILES_SETTINGS], ["open", FDA_SETTINGS],
                                     _say(SETTINGS_FAILED, rc=1)]),
    ("terminal", 0, 0, lambda root: [["open", "-a", "Terminal", str(root / START_COMMAND.name)]]),
    ("terminal", 1, 1, lambda root: [["open", "-a", "Terminal", str(root / START_COMMAND.name)],
                                     _say(TERMINAL_FAILED, rc=1)]),
    ("", 0, 1, lambda root: []),            # 点了「关闭」或按了 Esc：什么都不开
], ids=["settings", "settings-open-failed", "terminal", "terminal-open-failed", "close"])
def test_the_folder_dialog_buttons_only_open_things(tmp_path, answer, open_rc, rc, tail):
    """按钮只打开系统设置那一页、或者用「终端」打开同一文件夹里的 Start.command，程序绝不自己改设置。"""
    root = _project(tmp_path, main_py=True, finder=True)
    proc, calls = _run_launcher(tmp_path, root.joinpath(*APP_LAUNCHER), answer=answer, open_rc=open_rc,
                                wrap=_deny_reading(root))
    assert proc.returncode == rc, proc.stderr.decode("utf-8", "replace")
    assert calls == [_ask(root, "Operation not permitted")] + tail(root)


@needs_bash
def test_a_find_python_file_without_the_function_is_not_reported_as_missing_python(tmp_path):
    root = _project(tmp_path, main_py=True, finder=True)
    (root / "tools" / "find_python.sh").write_text("# 被截断了\n", encoding="utf-8")
    proc, calls = _run_launcher(tmp_path, root.joinpath(*APP_LAUNCHER))
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    assert calls == [_dialog(BROKEN_FINDER, "caution")]


@needs_unreadable
def test_start_command_says_it_could_not_read_the_finder_instead_of_sending_you_to_python_org(tmp_path):
    """Start.command 同一个问题：find_python.sh 读不了时以前也打印「未检测到 Python」、打开 python.org。
    终端里的话照旧只写中文（用户决定 4）。"""
    root = _project(tmp_path, main_py=True, finder=True)
    _unreadable(root / "tools" / "find_python.sh")
    proc, calls = _run_launcher(tmp_path, root / START_COMMAND.name)
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    assert calls == []
    out = proc.stdout.decode("utf-8")
    assert "[读不了程序文件] tools/find_python.sh 打不开，系统返回「Permission denied」，这一步没有去找 Python。" in out
    assert "未检测到 Python" not in out and "python.org" not in out


@needs_bash
def test_start_command_with_a_broken_finder_is_not_reported_as_missing_python(tmp_path):
    root = _project(tmp_path, main_py=True, finder=True)
    (root / "tools" / "find_python.sh").write_text("# 被截断了\n", encoding="utf-8")
    proc, calls = _run_launcher(tmp_path, root / START_COMMAND.name)
    assert proc.returncode == 1 and calls == []
    out = proc.stdout.decode("utf-8")
    assert "[文件不完整] tools/find_python.sh 里没有探测 Python 的那一段。" in out
    assert "未检测到 Python" not in out


@pytest.mark.parametrize("key", sorted(ARGV_DIALOGS))
def test_the_argv_dialogs_are_chinese_then_english(key):
    """作为参数交给 AppleScript 的这几段同样中文在前、英文在后，各段都不缺。填进去的路径、原话是数据：
    这里拿 ASCII 的样例填，只查我们自己写的字。"""
    zh, en = ARGV_DIALOGS[key]
    fields = {"dir": "/Users/x/Desktop/app", "file": "/Users/x/Desktop/app/main.py",
              "why": "Operation not permitted", "rc": 1}
    for folder in FOLDER_SWITCHES.values():
        zh_text = zh.format(folder=folder[0], **fields)
        en_text = en.format(folder=folder[1], **fields)
        for part in zh_text.split("\n\n"):
            assert i18n.CJK.search(part), part
        assert not i18n.CJK.search(en_text), en_text
        assert zh_text.count("\n\n") == en_text.count("\n\n")
        for text in (zh_text, en_text):          # 这几种情况下 Python 好好的：不许再提装 Python
            assert "python.org" not in text and "Python 3.10" not in text


@on_macos
@pytest.mark.parametrize("lines", [ASK_SCRIPT, SAY_SCRIPT], ids=["ask", "say"])
def test_the_argv_dialog_scripts_compile_as_applescript(tmp_path, lines):
    if not shutil.which("osacompile"):
        pytest.skip("没有 osacompile")
    proc = subprocess.run(["osacompile"] + _e(lines) + ["-o", str(tmp_path / "dialog.scpt")],
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


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
@pytest.mark.parametrize("texts, icon, answer", [(NO_MAIN, "caution", None), (NO_FINDER, "caution", None),
                                                 (NO_PYTHON, "note", None),
                                                 (TRANSLOCATED, "caution", UNQUARANTINE),
                                                 (BROKEN_FINDER, "caution", None)],
                         ids=["no-main-py", "no-find-python", "no-python", "translocated", "broken-find-python"])
def test_the_dialog_scripts_compile_as_applescript(tmp_path, texts, icon, answer):
    """osacompile 只编译不运行：不弹框。弯引号、· 和中文都在 AppleScript 字符串里。"""
    if not shutil.which("osacompile"):
        pytest.skip("没有 osacompile")
    argv = _dialog(texts, icon, answer)
    proc = subprocess.run(["osacompile", "-e", argv[2], "-o", str(tmp_path / "dialog.scpt")],
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
