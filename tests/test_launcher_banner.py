"""双击启动器（Start.command / Start.bat）在终端里打印的开场说明，以及 Start.bat 的编码。

那个终端窗口就是程序本身：关掉它，程序（连同字幕窗口）就退出。以前的说明写「这个窗口是
翻译引擎」，和设置里的「翻译引擎」（Hy-MT2、DeepL……）撞名，读起来像关掉它只是关了翻译。
终端说明按用户决定保持中文（决定 4，见 tests/test_mac_bundle.py），这里不查英文。

Start.bat 另有编码约束：cmd.exe 只认 CRLF（.gitattributes 里 *.bat eol=crlf），中文靠前面的
chcp 65001 按 UTF-8 输出；文件本身是不带 BOM 的 UTF-8——带 BOM 时 cmd 会把第一行连同那三个
字节当成一条认不出的命令，@echo off 不生效。改文案时这几样都不能跟着变。"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
START_COMMAND = ROOT / "Start.command"
START_BAT = ROOT / "Start.bat"
BOM = b"\xef\xbb\xbf"


def banner(path):
    """两行 ==== 之间 echo 出来的文字，一行一句（去掉 echo、引号和缩进）。"""
    lines = path.read_bytes().decode("utf-8").splitlines()
    rules = [i for i, line in enumerate(lines) if re.fullmatch(r'echo "?=+"?', line.strip())]
    assert len(rules) >= 2, path
    out = []
    for line in lines[rules[0] + 1:rules[1]]:
        assert line.startswith("echo "), line
        out.append(line[len("echo "):].strip().strip('"').strip())
    return out


@pytest.mark.parametrize("path", [START_COMMAND, START_BAT], ids=lambda p: p.name)
def test_the_launcher_says_its_window_is_the_app_itself(path):
    lines = banner(path)
    text = "".join(lines)
    assert "引擎" not in text                 # 「翻译引擎」是设置里选 Hy-MT2 / DeepL 的那一项
    assert "就是程序本身" in text and "关掉它程序就会退出" in text
    assert "请保持打开" in text
    assert lines[-1] == "字幕会显示在弹出的应用窗口里。"


def test_the_two_launchers_say_the_same_thing():
    """两份各有各的说法（Windows 那份多「黑色」两个字），别的逐字相同。"""
    assert [s.replace("黑色窗口", "窗口") for s in banner(START_BAT)] == banner(START_COMMAND)


def test_start_bat_stays_utf8_without_bom_and_switches_the_console_to_utf8_first():
    raw = START_BAT.read_bytes()
    assert not raw.startswith(BOM)
    lines = raw.decode("utf-8").splitlines()
    assert lines[0] == "@echo off"
    chcp = lines.index("chcp 65001 >nul")
    shown = [i for i, line in enumerate(lines)
             if not line.startswith("REM ") and any(ord(ch) > 127 for ch in line)]
    assert shown and chcp < shown[0]          # 第一句要显示的中文之前已经切到 UTF-8


def test_start_bat_keeps_crlf_in_every_checkout():
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("不是 git 检出")
    out = subprocess.run([git, "check-attr", "eol", "--", "Start.bat"], cwd=ROOT,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "Start.bat: eol: crlf"
    raw = START_BAT.read_bytes()             # 属性写着 crlf，检出的文件就该每行都是 CRLF
    assert raw.endswith(b"\r\n")
    assert raw.count(b"\n") == raw.count(b"\r\n") == raw.count(b"\r")
