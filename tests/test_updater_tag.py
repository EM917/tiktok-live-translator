"""一键更新只到 release 的标签（专家审查 S3）。

钉住的问题：提示来自 GitHub 的 releases/latest，合并的却是跟踪分支的末端（@{u}）。v0.18.0 之后 main
上已经多了 50 个没发版的提交，两边 VERSION 都是 0.18.0：落后于最新 release 的安装点一键更新，会把这些
提交一并装上。现在 fetch --tags --force，目标是那个标签：标签要在跟踪的远端分支上、能快进，才更新到它；
当前代码已经包含它时按「已是最新」处理，不暂停、不重启。

这里用真的 git：临时目录里一个当「GitHub」的裸仓库、一份发版用的克隆、一份当用户安装的克隆。不连网，
不起 pip（换成替身），不真 execv（换成记录）。git 的全局、系统配置都隔开（签名、钩子不会混进来）。
"""
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from app import settings as settings_mod
from app import updater as updater_mod
from app.updater import Updater, release_tag
from tests.helpers import run
from tests.test_resilience_update import RecordingServer

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout.strip()


def _commit(repo, name, version=None, text="x"):
    (repo / name).write_text(text, encoding="utf-8")
    if version is not None:
        (repo / "VERSION").write_text(version + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add " + name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(monkeypatch, tmp_path):
    """origin（裸仓库）+ dev（发版的人）+ install（用户的安装，停在 v1.0.0）。"""
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    for key, value in {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    origin = tmp_path / "origin.git"
    dev = tmp_path / "dev"
    install = tmp_path / "install"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(dev))
    c = {}
    c["v1.0.0"] = _commit(dev, "requirements.txt", "1.0.0", "aiohttp>=3.9\n")
    _git(dev, "tag", "v1.0.0")
    c["v1.1.0"] = _commit(dev, "feature.txt", "1.1.0")
    _git(dev, "tag", "-a", "v1.1.0", "-m", "release 1.1.0")      # 带注释的标签也要能解析
    c["unreleased"] = _commit(dev, "unreleased.txt")             # main 上发版之后、还没发版的提交
    _git(dev, "push", "-q", "origin", "main", "--tags")
    _git(tmp_path, "clone", "-q", str(origin), str(install))
    _git(install, "reset", "-q", "--hard", c["v1.0.0"])          # 用户停在 v1.0.0（跟踪 origin/main）
    for t in ("v1.1.0",):                                          # 用户这份还没见过新标签
        _git(install, "tag", "-d", t)

    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(updater_mod, "ROOT", install)
    monkeypatch.setattr(updater_mod, "local_version",
                        lambda: (install / "VERSION").read_text(encoding="utf-8").strip())
    monkeypatch.setattr(updater_mod, "managed_env", lambda root: False)
    execs = []
    monkeypatch.setattr(updater_mod.os, "execv", lambda exe, argv: execs.append(list(argv)))
    real_sleep = updater_mod.asyncio.sleep
    monkeypatch.setattr(updater_mod.asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    return {"origin": origin, "dev": dev, "install": install, "commits": c, "execs": execs,
            "tmp": tmp_path}


def _updater(version):
    server = RecordingServer()
    u = Updater(server)
    u.latest = {"version": version, "can_auto": True, "url": "http://x/releases/latest"}
    pips = []

    async def fake_pip(args, label, timeout=600, log_path=None, abort_if=None):
        pips.append(list(args))
        return 0

    u._pip_install = fake_pip
    return u, server, pips


def test_unreleased_commits_on_main_are_not_installed(world):
    """main 上有没发版的提交：只快进到标签那个提交，不到 main 的末端。"""
    install, c = world["install"], world["commits"]
    u, server, _pips = _updater("v1.1.0")

    run(u.apply())

    assert _git(install, "rev-parse", "HEAD") == c["v1.1.0"]
    assert (install / "feature.txt").exists() and not (install / "unreleased.txt").exists()
    assert _git(install, "rev-parse", "origin/main") == c["unreleased"]   # 取下来了，但没合并
    assert world["execs"] and "main.py" in " ".join(world["execs"][0])
    assert server.statuses()[-1][1] == "更新完成，正在自动重启…"


def test_a_tag_that_is_not_on_the_tracked_branch_is_refused(world, capsys):
    """标签打在别的分支上（不在主线）：不更新到它，给下载页、不给 merge 命令——照着
    `git merge --ff-only 标签` 做，这份安装就离开了主线，下一个发在主线上的版本一键更新、手动命令都没法
    快进（第 1 轮审查；下面实测）。"""
    dev, install, c = world["dev"], world["install"], world["commits"]
    _git(dev, "checkout", "-q", "-b", "side", c["v1.0.0"])
    _commit(dev, "side.txt", "1.2.0")
    _git(dev, "tag", "v1.2.0")
    _git(dev, "push", "-q", "origin", "side", "--tags")
    u, server, pips = _updater("v1.2.0")

    run(u.apply())

    assert _git(install, "rev-parse", "HEAD") == c["v1.0.0"]      # 没动
    assert world["execs"] == [] and pips == []
    state, detail, command = server.statuses()[-1]
    assert state == "idle" and command is None
    assert detail == ("本次没有更新（版本 v1.2.0 不在这份安装跟踪的远端分支上）。新版本可以到 GitHub 下载："
                      "http://x/releases/latest")
    assert "git merge" not in capsys.readouterr().out                # 终端里也不给那条命令
    # 为什么不给 merge 命令：照做之后，主线上的下一个版本没法快进
    detour = world["tmp"] / "detour"
    shutil.copytree(str(install), str(detour))
    for step in updater_mod._git_steps("v1.2.0"):
        _git(detour, *shlex.split(step)[1:])
    r = subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", c["v1.1.0"]], cwd=str(detour),
                       capture_output=True)
    assert r.returncode == 1


def test_an_upstream_tag_that_moved_does_not_block_the_update(world):
    """上游挪过一个旧标签（同名标签打到了别的提交）：不带 --force 的 `git fetch --tags` 会以
    「would clobber existing tag」整个失败，一键更新卡在「没能从 GitHub 取到新版本」、手动命令也一样，而且
    不会自己好（第 1 轮审查实测）。带 --force：一键更新和手动命令都照常到新标签，本地那个旧标签跟着上游。"""
    dev, install, c = world["dev"], world["install"], world["commits"]
    _git(dev, "tag", "-f", "v1.0.0", c["unreleased"])
    _git(dev, "push", "-q", "--force", "origin", "refs/tags/v1.0.0")
    plain, manual = world["tmp"] / "plain", world["tmp"] / "manual"
    shutil.copytree(str(install), str(plain))
    shutil.copytree(str(install), str(manual))
    # 这台 git 上，不带 --force 确实会失败（这个用例造的正是那种情形）
    r = subprocess.run(["git", "fetch", "--tags"], cwd=str(plain), capture_output=True, text=True)
    assert r.returncode != 0 and "clobber" in r.stderr, (r.returncode, r.stderr)
    u, server, _pips = _updater("v1.1.0")

    run(u.apply())

    assert _git(install, "rev-parse", "HEAD") == c["v1.1.0"]
    assert world["execs"] and server.statuses()[-1][1] == "更新完成，正在自动重启…"
    assert _git(install, "rev-parse", "v1.0.0^{commit}") == c["unreleased"]
    # 手动命令（逐条跑，不经 shell）同样走得通；取新版本那一步和一键更新是同一条
    assert shlex.split(updater_mod._git_steps("v1.1.0")[0])[1:] == list(updater_mod.FETCH_ARGS)
    for step in updater_mod._git_steps("v1.1.0"):
        _git(manual, *shlex.split(step)[1:])
    assert _git(manual, "rev-parse", "HEAD") == c["v1.1.0"]


def test_head_that_already_contains_the_tag_is_up_to_date_and_does_not_restart(world):
    """当前代码已经包含提示里的那个 release（跟踪分支上发版之后的提交已经在本地）：已是最新，
    不暂停、不重启，这次的更新提示收起来。"""
    dev, install, c = world["dev"], world["install"], world["commits"]
    _git(install, "fetch", "-q", "origin")
    _git(install, "reset", "-q", "--hard", c["unreleased"])
    _commit(dev, "later.txt")                                     # main 又往前走了一个提交
    _git(dev, "push", "-q", "origin", "main")
    u, server, pips = _updater("v1.1.0")
    server.config["update"] = dict(u.latest)

    run(u.apply())

    assert _git(install, "rev-parse", "HEAD") == c["unreleased"]
    assert world["execs"] == [] and pips == []
    assert server.of_type("update_aborted")
    assert server.of_type("notice")[-1]["text"] == "已是最新版本 v1.1.0"
    assert "update" not in server.config and u.latest is None


def test_a_tag_that_cannot_be_fast_forwarded_to_is_refused(world):
    """本地有自己的提交（和标签分叉）：没法快进，不动。"""
    install, c = world["install"], world["commits"]
    _commit(install, "local.txt")
    u, server, pips = _updater("v1.1.0")

    run(u.apply())

    assert world["execs"] == [] and pips == []
    state, detail, command = server.statuses()[-1]
    assert "本地代码没法直接快进到新版本" in detail
    assert command.endswith("git fetch --tags --force && git merge --ff-only v1.1.0")
    assert _git(install, "rev-parse", "HEAD") != c["v1.1.0"]


def test_a_release_whose_tag_never_arrives_is_refused(world):
    """GitHub 上有这个 release，取下来的标签里却没有它：不更新，也不退回去合并跟踪分支。"""
    u, server, pips = _updater("v9.0.0")

    run(u.apply())

    assert world["execs"] == [] and pips == []
    assert "取下来的版本里没有标签 v9.0.0" in server.statuses()[-1][1]
    assert _git(world["install"], "rev-parse", "HEAD") == world["commits"]["v1.0.0"]


def test_release_tag_accepts_only_plain_tag_names():
    assert release_tag("v0.19.0") == "v0.19.0"
    assert release_tag("0.19.0-rc.1") == "0.19.0-rc.1"
    for bad in (None, "", "v1;rm -rf ~", "-v1", "v1..2", "a b", "v1.lock", "v1/2", "$(x)", "v`1`"):
        assert release_tag(bad) is None, bad


def test_the_manual_command_goes_to_the_tag_on_both_shells(monkeypatch, tmp_path):
    monkeypatch.setattr(updater_mod, "ROOT", tmp_path)
    u = Updater(RecordingServer())
    u.latest = {"version": "v0.19.0"}
    assert u._manual_command() == 'cd "{}" && git fetch --tags --force && git merge --ff-only v0.19.0'.format(
        tmp_path)
    assert u._manual_command(discard=True) == (
        'cd "{}" && git checkout -- . && git fetch --tags --force && git merge --ff-only v0.19.0'.format(tmp_path))
    assert u._manual_command(pip=True).endswith(
        'git merge --ff-only v0.19.0 && "{}" -m pip install -r requirements.txt -c constraints.txt'
        .format(updater_mod.sys.executable))
    monkeypatch.setattr(updater_mod, "_on_windows", lambda: True)
    ps = u._manual_command()
    assert ps == ('cd "{}"; if ($?) {{ git fetch --tags --force }}; if ($?) {{ git merge --ff-only v0.19.0 }}'
                  .format(tmp_path))
    assert "&&" not in ps
    # 还不知道是哪个版本（没检查到新版本）：照旧 git pull
    u.latest = None
    assert "git pull --ff-only" in u._manual_command()


def test_an_unusable_tag_name_is_refused_before_touching_git(world):
    u, server, pips = _updater("v1;touch pwned")
    run(u.apply())
    assert world["execs"] == [] and pips == []
    state, detail, command = server.statuses()[-1]
    assert "认不出新版本的标签" in detail and command is None
    assert not (Path(os.getcwd()) / "pwned").exists()
