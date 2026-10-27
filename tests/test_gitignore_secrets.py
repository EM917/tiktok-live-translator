""".gitignore 要盖住所有可能含密钥或个人数据的本地文件。

app/settings.py 写 settings.json 时会留下两种旁路文件：原子写入的临时文件，和读到损坏的
settings.json 时改名留下的备份（settings.json.corrupt-<时间>）。它们装着整份设置，包括
Claude / DeepL 的密钥，却不叫 settings.json，.gitignore 原来只盖了后者——一次 `git add -A`
就能把密钥推上去。

文件名从 app/settings.py 真的写出来的结果里取，不在测试里手抄：改了命名方式，这里跟着红。
用 `git check-ignore --no-index` 问 git 本身，而不是自己解析 .gitignore。
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _git_ignores(path):
    """True/False；不在 git 仓库里（比如从压缩包解出来的）就跳过这条测试。"""
    try:
        done = subprocess.run(["git", "check-ignore", "--no-index", "-q", "--", path],
                              cwd=str(ROOT), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("没有可用的 git")
    if done.returncode == 128:
        pytest.skip("{} 不在 git 仓库里".format(ROOT))
    return done.returncode == 0


def _leftovers_written_by_settings(monkeypatch, tmp_path):
    """真的让 settings.py 写一次、再读一个坏文件，记下它碰过的所有文件名。"""
    from app import settings

    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    # 读到坏文件会在模块里记下备份名；用完还原，别让别的测试读到这里留下的
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    seen = set()
    real_replace = os.replace

    def spy(src, dst):
        seen.update((Path(src).name, Path(dst).name))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    settings.save_setting("claude_api_key", "sk-ant-test-not-a-real-key")
    (tmp_path / "settings.json").write_bytes(b"{ not json")
    settings.load_settings()                         # 损坏 -> 改名备份
    (tmp_path / "settings.json").write_bytes(b"{ still not json")
    settings.load_settings()                         # 同一秒再来一次 -> 带 -2 的备份名
    seen.discard("settings.json")
    return sorted(seen)


def test_settings_leftovers_are_found_by_the_probe(monkeypatch, tmp_path):
    """探针本身要有效，不然下面的断言是空转：至少得看到临时文件和备份两种。"""
    names = _leftovers_written_by_settings(monkeypatch, tmp_path)
    assert "settings.json.tmp" in names
    assert any(n.startswith("settings.json.corrupt-") for n in names), names


def test_every_file_settings_py_leaves_behind_is_ignored(monkeypatch, tmp_path):
    for name in _leftovers_written_by_settings(monkeypatch, tmp_path):
        assert _git_ignores(name), "{} 里可能有密钥，却没被 .gitignore 盖住".format(name)


@pytest.mark.parametrize("name", [
    "settings.json",
    "settings.json.tmp",
    "settings.json.corrupt-20261016-101010",
    "settings.json.corrupt-20261016-101010-2",
])
def test_settings_files_are_ignored(name):
    assert _git_ignores(name)


@pytest.mark.parametrize("name", [
    "transcripts/2026-10-16-brisa.md",       # 字幕记录：含观众昵称和弹幕
    "logs/session-20261016-101010.jsonl",    # 审计日志
    "brands/my-brand.txt",                   # 品牌词表副本
    "profiles/someone.txt",                  # 主播专属词表副本
    "banned_terms.txt",                      # 用户自己的违禁词表
    "glossary.txt",
])
def test_other_personal_files_stay_ignored(name):
    assert _git_ignores(name)


@pytest.mark.parametrize("name", [
    "brands/my-brand.example.txt",           # 模板要入库
    "profiles/someone.example.txt",
    "settings.example.json",
    "app/settings.py",
])
def test_templates_and_code_are_not_swallowed(name):
    assert not _git_ignores(name)


def test_no_tracked_file_is_hidden_by_the_ignore_rules():
    """新规则不能把已经入库的文件变成「被忽略」：`git ls-files -ci` 列的就是这种文件。"""
    try:
        done = subprocess.run(["git", "ls-files", "-ci", "--exclude-standard"],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("没有可用的 git")
    if done.returncode != 0:
        pytest.skip("{} 不在 git 仓库里".format(ROOT))
    assert done.stdout.split() == []
