"""tools/prepush_realnames.py：推送前扫真名的钩子。

用真的 git：临时目录里一个当「GitHub」的裸仓库和一份克隆，名单是虚构的（zorbaflux、
Acme Fictional Shop、虚构小铺都是编的，不是任何真实主播、品牌或店铺）。git 的全局、系统配置
隔开，签名、钩子、autocrlf 不会混进来。不连网。"""
import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools import prepush_realnames as pr

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "prepush_realnames.py"
ZERO = "0" * 40
NAMES = "# 虚构名单\n\nZorbaflux\nacme fictional shop\n虚构小铺\nx\n"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout.strip()


def _commit(repo, rel, text, message="change"):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(monkeypatch, tmp_path):
    """origin（裸仓库，main 上已有一个提交）+ dev（克隆，当前目录）+ 虚构名单。"""
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    for key, value in {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", pr.ENV_VAR):
        monkeypatch.delenv(key, raising=False)
    origin, dev = tmp_path / "origin.git", tmp_path / "dev"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(dev))
    _commit(dev, "README.md", "hello\n", "start")
    _git(dev, "push", "-q", "origin", "main")
    names = tmp_path / "names.txt"
    names.write_text(NAMES, encoding="utf-8")
    monkeypatch.chdir(dev)
    return {"dev": dev, "origin": origin, "names": names}


def _hook(world, *updates, names=True):
    """像 git 那样调钩子：参数是远端名和地址，stdin 是 ref 更新。返回 (退出码, 输出)。"""
    argv = ["origin", str(world["origin"])]
    if names:
        argv += ["--names", str(world["names"])]
    stdin = io.StringIO("".join("{} {} {} {}\n".format(*u) for u in updates))
    err = io.StringIO()
    old, sys.stderr = sys.stderr, err
    try:
        code = pr.main(argv, stdin=stdin)
    finally:
        sys.stderr = old
    return code, err.getvalue()


def _new_branch(world, branch="feature"):
    sha = _git(world["dev"], "rev-parse", "HEAD")
    return ("refs/heads/" + branch, sha, "refs/heads/" + branch, ZERO)


def _no_plain_names(out):
    folded = out.casefold()
    for name in ("zorbaflux", "acme fictional shop", "虚构小铺"):
        assert name not in folded, "输出里出现了名单原文"


# ---- 名单与打码 --------------------------------------------------------------------------------

def test_load_names_skips_comments_blanks_short_entries_and_folds_case(tmp_path):
    p = tmp_path / "n.txt"
    p.write_text("﻿# 注释\n\nZorbaflux\nzorbaflux\n  Acme Fictional Shop  \nx\n", encoding="utf-8")
    assert pr.load_names(str(p)) == ["zorbaflux", "acme fictional shop"]
    assert pr.load_names(str(tmp_path / "missing.txt")) is None


def test_mask_gives_first_letter_and_length_only():
    assert pr.mask("zorbaflux") == "z9"
    assert pr.mask("虚构小铺") == "虚4"
    names = ["zorbaflux", "zorba"]
    assert pr.mask_text("profiles/ZorbaFlux.txt", names) == "profiles/<z9>.txt"


def test_separators_inside_a_name_are_interchangeable():
    """名单写空格或连字符，代码里的标识符、文件名写下划线、点或连在一起，照样命中。"""
    names = ["acme fictional shop"]
    for text in ("ACME_FICTIONAL_SHOP = 1", "acme-fictional-shop", "class AcmeFictionalShop:",
                 "acme.fictional shop"):
        assert pr.find_names(text, names) == names, text
    assert pr.find_names("acme fictional", names) == []
    assert pr.mask_text("profiles/Acme_Fictional_Shop.txt", names) == "profiles/<a19>.txt"


def test_spellings_of_one_name_collapse_and_one_spot_counts_once(tmp_path):
    p = tmp_path / "n.txt"
    p.write_text("Acme-Fictional-Shop\nacme fictional shop\n.zorbaflux\nzorbaflux\nzorba\n--\n",
                 encoding="utf-8")
    names = pr.load_names(str(p))
    assert names == ["acme-fictional-shop", "zorbaflux", "zorba"]
    assert pr.find_names("zorbaflux sells at AcmeFictionalShop", names) == [
        "acme-fictional-shop", "zorbaflux"]
    assert pr.find_names("zorba alone", names) == ["zorba"]
    assert pr.find_names("anything", ["--"]) == []


def test_combined_diff_only_flags_lines_the_merge_itself_wrote():
    patch = ("diff --cc f.txt\nindex a,b..c\n--- a/f.txt\n+++ b/f.txt\n"
             "@@@ -4,0 -3,1 +4,2 @@@\n+ zorbaflux from one side\n++zorbaflux written in the merge\n")
    assert pr.scan_patch(patch, ["zorbaflux"]) == [("f.txt", 5, "zorbaflux")]


def test_header_lines_inside_a_hunk_are_content():
    """新增的一行正好以 "++ " 开头时，diff 里是 "+++ ..."：那是内容，不是文件头。"""
    patch = ("diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1,0 +1,2 @@\n"
             "+first\n+++ zorbaflux\n")
    assert pr.scan_patch(patch, ["zorbaflux"]) == [("f.txt", 2, "zorbaflux")]


def test_quoted_paths_are_unquoted():
    assert pr._unquote('"b/tab\\tname \\"q\\".txt"') == 'b/tab\tname "q".txt'
    assert pr._unquote('"b/\\344\\275\\240.txt"') == "b/你.txt"
    assert pr._unquote("b/plain name.txt") == "b/plain name.txt"


# ---- 当钩子跑 ----------------------------------------------------------------------------------

def test_name_in_added_line_message_and_path_blocks_the_push(world):
    dev = world["dev"]
    _commit(dev, "tests/fixture.py", "HOST = 'ZorbaFlux'\n")
    _commit(dev, "docs/x.md", "nothing here\n", "Mention acme fictional shop in the docs")
    _commit(dev, "profiles/zorbaflux.txt", "chile => 辣椒\n")
    _commit(dev, "notes.md", "第一行\n来自虚构小铺的货\n")
    _commit(dev, "app/shops.py", "\nACME_FICTIONAL_SHOP = 3\n")
    code, out = _hook(world, _new_branch(world))
    assert code == 1
    assert "tests/fixture.py:1  z9" in out
    assert "提交说明  a19" in out
    assert "文件路径 profiles/<z9>.txt  z9" in out
    assert "notes.md:2  虚4" in out
    assert "app/shops.py:2  a19" in out
    assert "命中 5 处" in out and "这次不推" in out
    _no_plain_names(out)


def test_clean_push_passes(world):
    _commit(world["dev"], "app.py", "print('hola')\n")
    code, out = _hook(world, _new_branch(world))
    assert code == 0
    assert "1 个提交，没有命中" in out


def test_no_list_reminds_and_lets_the_push_through(world, monkeypatch):
    _commit(world["dev"], "fixture.py", "HOST = 'zorbaflux'\n")
    code, out = _hook(world, _new_branch(world), names=False)
    assert code == 0 and "没设 TLT_REALNAMES" in out
    monkeypatch.setenv(pr.ENV_VAR, str(world["names"].parent / "nope.txt"))
    code, out = _hook(world, _new_branch(world), names=False)
    assert code == 0 and "不存在" in out and "照常推" in out
    empty = world["names"].parent / "empty.txt"
    empty.write_text("# 只有注释\n", encoding="utf-8")
    monkeypatch.setenv(pr.ENV_VAR, str(empty))
    code, out = _hook(world, _new_branch(world), names=False)
    assert code == 0 and "是空的" in out


def test_removing_a_name_is_not_a_hit(world):
    dev = world["dev"]
    _commit(dev, "old.py", "HOST = 'zorbaflux'\nOTHER = 1\n")
    _git(dev, "push", "-q", "origin", "main")                  # 名单出现之前就公开了的
    _commit(dev, "old.py", "HOST = 'fictional'\nOTHER = 1\n", "Replace the host name")
    code, out = _hook(world, _new_branch(world))
    assert code == 0, out


def test_only_commits_the_remote_lacks_are_scanned(world):
    dev = world["dev"]
    first = _commit(dev, "a.py", "HOST = 'zorbaflux'\n")
    _git(dev, "push", "-q", "--no-verify", "origin", "HEAD:refs/heads/feature")
    _commit(dev, "b.py", "print('nada')\n")
    head = _git(dev, "rev-parse", "HEAD")
    code, out = _hook(world, ("refs/heads/feature", head, "refs/heads/feature", first))
    assert code == 0 and "1 个提交" in out, out


def test_merging_main_does_not_rescan_main(world):
    dev = world["dev"]
    _git(dev, "checkout", "-q", "-b", "feature")
    _commit(dev, "feature.py", "x = 1\n")
    _git(dev, "push", "-q", "origin", "feature")
    _git(dev, "checkout", "-q", "main")
    _commit(dev, "main.py", "HOST = 'zorbaflux'\n")            # 已经在远端 main 上了
    _git(dev, "push", "-q", "origin", "main")
    _git(dev, "checkout", "-q", "feature")
    old = _git(dev, "rev-parse", "HEAD")
    _git(dev, "merge", "-q", "--no-edit", "main")
    head = _git(dev, "rev-parse", "HEAD")
    code, out = _hook(world, ("refs/heads/feature", head, "refs/heads/feature", old))
    assert code == 0, out


def test_a_rebase_onto_main_and_force_push_does_not_rescan_main(world):
    dev = world["dev"]
    _git(dev, "checkout", "-q", "-b", "feature")
    _commit(dev, "feature.py", "x = 1\n")
    _git(dev, "push", "-q", "origin", "feature")
    old = _git(dev, "rev-parse", "HEAD")
    _git(dev, "checkout", "-q", "main")
    _commit(dev, "main.py", "HOST = 'zorbaflux'\n")
    _git(dev, "push", "-q", "origin", "main")
    _git(dev, "checkout", "-q", "feature")
    _git(dev, "rebase", "-q", "main")
    head = _git(dev, "rev-parse", "HEAD")
    code, out = _hook(world, ("refs/heads/feature", head, "refs/heads/feature", old))
    assert code == 0 and "1 个提交" in out, out


def test_deleting_a_remote_branch_scans_nothing(world):
    code, out = _hook(world, ("(delete)", ZERO, "refs/heads/feature", "a" * 40))
    assert code == 0, out


def test_annotated_tag_message_is_scanned(world):
    dev = world["dev"]
    _git(dev, "tag", "-a", "v9", "-m", "Release for Zorbaflux")
    tag = _git(dev, "rev-parse", "v9")
    code, out = _hook(world, ("refs/tags/v9", tag, "refs/tags/v9", ZERO))
    assert code == 1 and "标签说明  z9" in out
    _no_plain_names(out)


def test_unreadable_commit_refuses_the_push(world):
    code, out = _hook(world, ("refs/heads/feature", "b" * 40, "refs/heads/feature", ZERO))
    assert code == 2 and "扫的时候出错" in out


def test_range_mode_scans_without_stdin(world):
    _commit(world["dev"], "fixture.py", "HOST = 'zorbaflux'\n")
    err = io.StringIO()
    old, sys.stderr = sys.stderr, err
    try:
        code = pr.main(["--names", str(world["names"]), "--range", "origin/main..HEAD"])
    finally:
        sys.stderr = old
    assert code == 1 and "fixture.py:1  z9" in err.getvalue()


@pytest.mark.skipif(sys.platform == "win32",
                    reason="钩子走 sh；逻辑由上面直接喂 stdin 的用例在各平台覆盖")
def test_installed_hook_stops_a_real_push(world, monkeypatch):
    """装成真的 pre-push 钩子：git push 被拦，远端没有这个分支；没有名单时照常推上去。
    跑的是单独一个进程、-I（不带本仓库的路径），证明脚本不依赖仓库里的别的东西。"""
    dev = world["dev"]
    hook = dev / ".git" / "hooks" / "pre-push"
    hook.write_text('#!/bin/sh\nexec "{}" -I "{}" "$@"\n'.format(
        Path(sys.executable).as_posix(), SCRIPT.as_posix()), encoding="utf-8")
    hook.chmod(0o755)
    _commit(dev, "fixture.py", "HOST = 'zorbaflux'\n")
    monkeypatch.setenv(pr.ENV_VAR, str(world["names"]))
    r = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/feature"], cwd=str(dev),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode != 0
    assert "fixture.py:1  z9" in r.stderr
    _no_plain_names(r.stderr)
    assert _git(world["origin"], "branch", "--list", "feature") == ""
    monkeypatch.delenv(pr.ENV_VAR)
    r = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/feature"], cwd=str(dev),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert "没扫真名" in r.stderr
