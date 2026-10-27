"""在 .app 壳里跑 pip 用 .venv/bin/python，不用壳里那个（macbundle.pip_python）。

2026-10-26 启动时的 yt-dlp 自动更新，是在壳里的 python 下跑的 pip（那个 sys.prefix 是 Contents）：
yt-dlp 命令和 man / 补全文件写进了 .app 的 Contents/bin、Contents/share，要换掉的旧版被 pip 判成
「outside environment」没卸，.venv 里新旧两份 dist-info 并存。

钉住：在壳里时，程序自己跑的每一处 pip（yt-dlp 保鲜；TikTokLive、mlx-whisper、一键更新共用的
_pip_install）和给中控抄的 pip 命令，用的都是 .venv/bin/python；不在壳里时照旧是 sys.executable。

不 import main，不连网络，不真起 pip：壳在 tmp_path 里搭，sys.prefix 换成它的 Contents，PyPI 和
pip 子进程都是替身。
"""
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import macbundle, selfcheck
from app import settings as settings_mod
from app import updater as updater_mod
from app.updater import Updater
from tests.helpers import run

REPO = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000.0

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="符号链接语义不同；功能仅 macOS 生效")


def _venv(root):
    v = root / ".venv"
    (v / "bin").mkdir(parents=True)
    (v / "bin" / "python").write_text("#!/bin/sh\n")
    (v / "lib" / "python3.13" / "site-packages").mkdir(parents=True)
    (v / "pyvenv.cfg").write_text("home = /opt/x/bin\nversion = 3.13.5\n")
    return v


@pytest.fixture
def in_shell(monkeypatch, tmp_path):
    """进程「跑在壳里」：sys.prefix 是 .app 的 Contents，sys.executable 是 Contents/MacOS/python。
    不搭符号链接——pip_python 只比 sys.prefix 和看 .venv/bin/python 在不在，Windows 跑器上也能测接线。"""
    _venv(tmp_path)
    contents = tmp_path / macbundle.APP_DIR_NAME / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(contents))
    monkeypatch.setattr(sys, "executable", str(contents / "MacOS" / "python"))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(updater_mod, "ROOT", tmp_path)
    monkeypatch.setattr(selfcheck, "ROOT", tmp_path)
    return tmp_path / ".venv" / "bin" / "python"


# ---- pip_python 本身 ------------------------------------------------------------------------------

@posix_only
def test_inside_the_real_shell_pip_runs_with_the_venv_python(tmp_path):
    _venv(tmp_path)
    shell_python = macbundle.ensure_bundle_shell(tmp_path)
    contents = tmp_path / macbundle.APP_DIR_NAME / "Contents"
    want = str(tmp_path / ".venv" / "bin" / "python")
    assert macbundle.pip_python(tmp_path, prefix=str(contents), executable=str(shell_python)) == want
    # sys.prefix 没规范化过（带 ..）也认得出
    assert macbundle.pip_python(tmp_path, prefix=str(contents / "MacOS" / ".."),
                                executable=str(shell_python)) == want


def test_outside_the_shell_pip_runs_with_sys_executable(tmp_path):
    _venv(tmp_path)
    exe = "/opt/py/bin/python3"
    assert macbundle.pip_python(tmp_path, prefix=str(tmp_path / ".venv"), executable=exe) == exe
    assert macbundle.pip_python(tmp_path, prefix="/opt/py", executable=exe) == exe     # 用户自己的 Python
    # 另一份程序文件夹的壳不算：比的是这一份的 Contents
    other = tmp_path / "other" / macbundle.APP_DIR_NAME / "Contents"
    other.mkdir(parents=True)
    assert macbundle.pip_python(tmp_path, prefix=str(other), executable=exe) == exe


@posix_only
def test_without_a_venv_python_it_falls_back_to_sys_executable(tmp_path):
    _venv(tmp_path)
    shell_python = macbundle.ensure_bundle_shell(tmp_path)
    (tmp_path / ".venv" / "bin" / "python").unlink()
    contents = tmp_path / macbundle.APP_DIR_NAME / "Contents"
    assert macbundle.pip_python(tmp_path, prefix=str(contents),
                                executable=str(shell_python)) == str(shell_python)


# ---- 程序自己跑的 pip --------------------------------------------------------------------------

def _week_old_release(days=20):
    when = datetime.fromtimestamp(NOW - days * 86400, tz=timezone.utc)
    return [{"packagetype": "bdist_wheel", "yanked": False,
             "upload_time_iso_8601": when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}]


def test_ytdlp_freshen_inside_the_shell_runs_pip_with_the_venv_python(monkeypatch, in_shell):
    env = {"yt-dlp": "2026.8.19", "curl_cffi": "0.16.1"}
    monkeypatch.setattr(updater_mod, "component_version", lambda name: env.get(name))
    spawned = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*args, **kw):
        spawned.append(args)
        env["yt-dlp"] = args[-1].split("==", 1)[1]
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    u = Updater(SimpleNamespace(config={}))      # 装好之后只读一眼当前状态
    u._stream_active = lambda: False

    async def fake_pypi(dist):
        return {"2026.9.1": _week_old_release()}, None

    u._pypi_releases = fake_pypi
    assert run(u.freshen_ytdlp(reason="periodic", now=NOW)) == "installed"
    assert len(spawned) == 1
    assert spawned[0][:4] == (str(in_shell), "-m", "pip", "install")
    assert spawned[0][-1] == "yt-dlp[curl-cffi]==2026.9.1"


def test_shared_pip_install_inside_the_shell_runs_with_the_venv_python(monkeypatch, in_shell):
    """TikTokLive 的装/升、mlx-whisper 的后台重试、一键更新的 pip -r 都走 _pip_install。"""
    spawned = []

    class Proc:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*args, **kw):
        spawned.append(args)
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert run(Updater(server=None)._pip_install(["TikTokLive==7.0.1"], "测试")) == 0
    assert len(spawned) == 1
    assert spawned[0][:4] == (str(in_shell), "-m", "pip", "install")


# ---- 给中控抄的 pip 命令 -----------------------------------------------------------------------

def test_manual_pip_commands_inside_the_shell_name_the_venv_python(monkeypatch, in_shell):
    shell_python = sys.executable
    monkeypatch.setattr(updater_mod, "_on_windows", lambda: False)
    update = Updater(server=None)._manual_command(pip=True)
    assert update.endswith('"{}" -m pip install -r requirements.txt -c constraints.txt'.format(in_shell))
    monkeypatch.setattr(updater_mod, "_on_windows", lambda: True)
    assert '& "{}" -m pip install -r requirements.txt'.format(in_shell) in Updater(server=None)._manual_command(
        pip=True)
    fix = str(selfcheck._pip_command("TikTokLive", upgrade=True))
    assert '"{}" -m pip install -U "TikTokLive"'.format(in_shell) in fix
    assert all(shell_python not in cmd for cmd in (update, fix))


# ---- 以后新加的 pip 调用 ----------------------------------------------------------------------

def test_no_pip_subprocess_is_started_with_sys_executable():
    """以后再加一处后台 pip，也别直接拿 sys.executable 起：在壳里就又是 Contents/MacOS/python。"""
    pattern = re.compile(r"sys\.executable\s*,\s*[\"']-m[\"']\s*,\s*[\"']pip[\"']")
    offenders = [str(path.relative_to(REPO)) for path in sorted((REPO / "app").rglob("*.py"))
                 if pattern.search(path.read_text(encoding="utf-8"))]
    offenders += ["main.py"] if pattern.search((REPO / "main.py").read_text(encoding="utf-8")) else []
    assert offenders == []

