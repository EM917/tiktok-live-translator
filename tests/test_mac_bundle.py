"""app/macbundle.py：让进程住进 .app 的那层壳。

钉住：三样壳文件的生成与幂等刷新；没 venv 就不生成；只在 macOS、没有防循环
变量、不在 pytest、不带无窗口参数、且当前不在 bundle 里时才重新执行；
execve 的参数形状；壳没备好时静默放弃。测试里绝不真的 exec。"""
import os
import sys
from pathlib import Path

import pytest

from app import macbundle

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
