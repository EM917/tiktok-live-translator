"""settings.json 只给本账户读写（专家审查 S8 第 2 点）。

这个文件里存着 DeepL / Claude 的密钥。以前 save_setting 按默认 umask 建临时文件再替换，落盘是 0644，
同一台电脑上的其它账户都读得到。现在临时文件一建出来就是 0600，替换之后再补一次 chmod：上次中断
留下的 .json.tmp、升级前就有的 settings.json 都是 0644，os.open 的权限参数管不到已经存在的文件。
Windows 上 chmod 只认只读位，跳过（权限归 ACL 管）。"""
import json
import os
import stat
import sys

import pytest

from app import settings

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Windows 不用 POSIX 权限位")


def _mode(path):
    return stat.S_IMODE(os.stat(str(path)).st_mode)


@pytest.fixture
def settings_file(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_FILE", path)
    return path


@posix_only
def test_a_new_settings_file_is_private(settings_file):
    settings.save_setting("translator", "deepl")
    assert _mode(settings_file) == 0o600
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"translator": "deepl"}
    assert not settings_file.with_suffix(".json.tmp").exists()


@posix_only
def test_an_old_world_readable_file_is_tightened_on_the_next_save(settings_file):
    settings_file.write_text(json.dumps({"api_keys": {"deepl": "k"}}), encoding="utf-8")
    os.chmod(str(settings_file), 0o644)
    stale = settings_file.with_suffix(".json.tmp")       # 上次写到一半中断留下的，0644
    stale.write_text("{", encoding="utf-8")
    os.chmod(str(stale), 0o644)
    settings.save_setting("target_lang", "en")
    assert _mode(settings_file) == 0o600
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "api_keys": {"deepl": "k"}, "target_lang": "en"}


@posix_only
def test_the_temporary_file_is_private_from_the_moment_it_exists(settings_file, monkeypatch):
    """不留「先 0644 写进密钥、再 chmod」的空档：替换那一刻临时文件就已经是 0600。"""
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(_mode(src))
        return real_replace(src, dst)

    monkeypatch.setattr(settings.os, "replace", spy)
    settings.save_setting("x", 1)
    assert seen == [0o600]


@posix_only
def test_a_failing_chmod_never_blocks_the_save(settings_file, monkeypatch):
    def refuse(path, mode):
        raise PermissionError("not allowed")

    monkeypatch.setattr(settings.os, "chmod", refuse)
    settings.save_setting("x", 1)                         # 不抛
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"x": 1}


def test_windows_skips_chmod(settings_file, monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "_WINDOWS", True)
    monkeypatch.setattr(settings.os, "chmod", lambda *a: calls.append(a))
    settings.save_setting("x", 1)
    assert calls == []
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"x": 1}


def test_the_written_bytes_are_unchanged(settings_file):
    """换成 os.open + fdopen 之后写出的内容和以前 open(..., "w") 一样：缩进 2、中文不转义。"""
    settings.save_setting("recent", "主播")
    text = settings_file.read_text(encoding="utf-8")
    assert text == json.dumps({"recent": "主播"}, ensure_ascii=False, indent=2)
