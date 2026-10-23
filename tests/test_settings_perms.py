"""settings.json 只给本账户读写（专家审查 S8 第 2 点）。

这个文件里存着 DeepL / Claude 的密钥。以前 save_setting 按默认 umask 建临时文件再替换，落盘是 0644，
同一台电脑上的其它账户都读得到。现在临时文件一建出来就是 0600，换上去的就是这个文件。

上次写到一半中断留下的 .json.tmp 是 0644，os.open 的权限参数管不到已经存在的文件：复用它（O_TRUNC），
密钥就会先写进一个 0644 的文件。所以 POSIX 上先删掉残留、再用 O_CREAT | O_EXCL 新建，绝不往别人
建的文件里写。Windows 照旧 O_TRUNC（mode 只认只读位，权限归 ACL 管）。"""
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


def _spy_open(monkeypatch):
    """记下 settings 模块每次 os.open 的 flags、mode 参数，以及刚打开时这个文件的真实权限和 inode。"""
    calls = []
    real_open = os.open

    def spy(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        st = os.fstat(fd)
        calls.append({"flags": flags, "mode": mode, "st_mode": stat.S_IMODE(st.st_mode),
                      "ino": st.st_ino})
        return fd

    monkeypatch.setattr(settings.os, "open", spy)
    return calls


@posix_only
def test_a_stale_world_readable_temp_file_is_never_reused(settings_file, monkeypatch, tmp_path):
    """上次中断留下一个 0644 的 .json.tmp：这次新建一个 0600 的，密钥一个字节也不进那个 0644 文件。"""
    settings_file.write_text("{}", encoding="utf-8")
    stale = settings_file.with_suffix(".json.tmp")
    stale.write_text("{", encoding="utf-8")
    os.chmod(str(stale), 0o644)
    witness = tmp_path / "stale-witness"          # 硬链接：残留文件那个 inode 被删名之后还看得到
    os.link(str(stale), str(witness))
    stale_ino = os.stat(str(witness)).st_ino
    calls = _spy_open(monkeypatch)

    settings.save_setting("api_keys", {"deepl": "secret-key"})

    assert len(calls) == 1
    call = calls[0]
    assert call["flags"] & os.O_CREAT and call["flags"] & os.O_EXCL
    assert not call["flags"] & os.O_TRUNC
    assert call["mode"] == 0o600
    assert call["st_mode"] == 0o600                # 刚建出来、还没写一个字节时就是 0600
    assert call["ino"] != stale_ino                # 是新文件，不是复用的残留
    assert witness.read_text(encoding="utf-8") == "{"     # 残留的 0644 文件里没写进密钥
    assert _mode(witness) == 0o644
    assert _mode(settings_file) == 0o600
    assert os.stat(str(settings_file)).st_ino == call["ino"]   # 换上去的就是新建的那个
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "api_keys": {"deepl": "secret-key"}}


@posix_only
def test_a_temp_file_that_reappears_is_left_alone_and_the_save_is_skipped(settings_file, monkeypatch):
    """删完到新建之间有人又建了 .json.tmp（这里用「删不掉」模拟）：O_EXCL 报错，这一次不写，
    不往那个文件里写密钥，也不抛（写设置失败本来就静默忽略）。下一次保存照常。"""
    settings_file.write_text(json.dumps({"x": 0}), encoding="utf-8")
    stale = settings_file.with_suffix(".json.tmp")
    stale.write_text("{", encoding="utf-8")
    os.chmod(str(stale), 0o644)
    real_unlink = os.unlink
    monkeypatch.setattr(settings.os, "unlink", lambda path: None)

    settings.save_setting("api_keys", {"deepl": "secret-key"})      # 不抛

    assert stale.read_text(encoding="utf-8") == "{"
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"x": 0}
    monkeypatch.setattr(settings.os, "unlink", real_unlink)
    settings.save_setting("y", 1)
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"x": 0, "y": 1}
    assert _mode(settings_file) == 0o600


@posix_only
def test_no_leftover_temp_file_is_fine(settings_file, monkeypatch):
    calls = _spy_open(monkeypatch)
    settings.save_setting("x", 1)
    settings.save_setting("y", 2)                  # 第二次：上一次的临时文件已经换走了，照样新建
    assert [c["st_mode"] for c in calls] == [0o600, 0o600]
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"x": 1, "y": 2}


def test_windows_keeps_reusing_the_temp_file(settings_file, monkeypatch):
    """Windows 照旧：O_CREAT | O_TRUNC 复用残留，不先删、不用 O_EXCL。"""
    unlinks = []
    monkeypatch.setattr(settings, "_WINDOWS", True)
    monkeypatch.setattr(settings.os, "unlink", lambda path: unlinks.append(path))
    stale = settings_file.with_suffix(".json.tmp")
    stale.write_text("{", encoding="utf-8")
    calls = _spy_open(monkeypatch)
    settings.save_setting("x", 1)
    assert unlinks == []
    assert len(calls) == 1
    assert calls[0]["flags"] & os.O_TRUNC and not calls[0]["flags"] & os.O_EXCL
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"x": 1}


def test_the_written_bytes_are_unchanged(settings_file):
    """换成 os.open + fdopen 之后写出的内容和以前 open(..., "w") 一样：缩进 2、中文不转义。"""
    settings.save_setting("recent", "主播")
    text = settings_file.read_text(encoding="utf-8")
    assert text == json.dumps({"recent": "主播"}, ensure_ascii=False, indent=2)
