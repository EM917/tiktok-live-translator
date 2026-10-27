"""I1（2026-10-21 专家审查）：首次安装在装 pip 的那一步被打断，.venv 永久缺 pip。

venv.create 先写 pyvenv.cfg 和 bin/python，最后才跑 ensurepip；强制退出、杀进程组都会让
ensurepip 跟着父进程一起死，留下一个版本对得上、能跑、却既没有 pip 也没有组件的 .venv。以前
venv_needs_rebuild 在版本一致时直接放过，原地补装每次都报 No module named pip，对话框还说
「请检查网络连接」。

钉住（app/bootstrap.py）：
- 版本一致、pip 和组件都 import 不了：重建；pip 或组件还在：不重建，照旧原地补装；
- 装之前先探 pip，缺了在 .venv 里跑 ensurepip 原地补（不联网），补回来照常装；
- 补不回来写重建记号、抛 PipMissing，下次 venv_needs_rebuild 据此重建（组件都在也重建）；
  记号在但 pip 已经回来了：删记号、不重建；记号在、pip 探测本身出错：不重建、不删记号；
- 失败说明认出缺 pip，不提网络；记号写成了说下次打开会重建，没写成给删 .venv 的步骤；英文同样。

venv 都建在 tmp_path 里（不带 pip、不起 ensurepip、不连网），绝不碰项目自己的 .venv。
"""
import importlib.util
import json
import os
import subprocess
import venv as venv_mod
from pathlib import Path

import pytest

from app import bootstrap, i18n

NO_CORE = "import tlt_core_that_is_not_installed"


def _bare_venv(path):
    """一个真的 venv，不带 pip。返回 (python, site-packages)。"""
    venv_mod.create(path, with_pip=False, symlinks=(os.name != "nt"))
    py = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    out = subprocess.run([str(py), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return py, Path(out)


def _fake_pip(site):
    (site / "pip").mkdir(parents=True, exist_ok=True)
    (site / "pip" / "__init__.py").write_text("", encoding="utf-8")


def _fake_core(site):
    (site / "tlt_core_probe.py").write_text("", encoding="utf-8")
    return "import tlt_core_probe"


@pytest.fixture
def project(tmp_path):
    """临时项目根：.venv 是一个不带 pip 的真 venv，旁边一份 requirements.txt。"""
    root = tmp_path / "project"
    root.mkdir()
    (root / "requirements.txt").write_text("aiohttp>=3.9\n", encoding="utf-8")
    py, site = _bare_venv(root / ".venv")
    here = bootstrap.interpreter_version(py)[1]
    return root, py, site, here


# ---- 重建判断 ------------------------------------------------------------------------------

def test_pip_probe_answers_yes_no_or_unknown(project, tmp_path):
    _root, py, site, _here = project
    assert bootstrap.pip_importable(py) is False
    _fake_pip(site)
    assert bootstrap.pip_importable(py) is True
    # 解释器不在、或者只给了个名字：不知道，不当成缺 pip（不按 PATH 去找同名程序）
    assert bootstrap.pip_importable(tmp_path / "no-python") is None
    assert bootstrap.pip_importable("py") is None


def test_same_version_without_pip_or_components_is_rebuilt(project):
    _root, py, _site, here = project
    assert bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=NO_CORE)
    # 进程本来就跑在 .venv / .app 壳里：照旧不从里面清空自己
    assert not bootstrap.venv_needs_rebuild(True, here, here, own_env=True, python=py,
                                            ready_import=NO_CORE)
    # 读不到版本、没给解释器：没有证据，照旧不删
    assert not bootstrap.venv_needs_rebuild(True, None, here, python=py, ready_import=NO_CORE)
    assert not bootstrap.venv_needs_rebuild(True, here, here)


def test_same_version_with_components_but_no_pip_is_kept(project):
    """组件都在、只是没有 pip：能直接跑，不清空重下 1.4 GB；要补装时由 ensure_pip 原地补。"""
    _root, py, site, here = project
    ready = _fake_core(site)
    assert not bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=ready)


def test_same_version_with_pip_but_no_components_is_kept(project):
    """pip 在、组件没装完（装 requirements 时被打断）：原地补装走得通，不重建。"""
    _root, py, site, here = project
    _fake_pip(site)
    assert not bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=NO_CORE)


# ---- 原地补 pip ----------------------------------------------------------------------------

class Recorder:
    """假的 run_logged：记下每条命令，按 codes 里的规则返回退出码。"""

    def __init__(self, ensurepip=0, pip=0):
        self.calls = []
        self.codes = {"ensurepip": ensurepip, "pip": pip}

    def __call__(self, cmd, log_path):
        self.calls.append(list(cmd))
        return (self.codes["ensurepip"] if "ensurepip" in cmd else self.codes["pip"]), ""


def _probe(*answers):
    """依次回答「有没有 pip」，最后一个答案一直重复。"""
    seq = list(answers)

    def probe(python):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return probe


def test_missing_pip_is_put_back_in_place_before_installing(project, capsys):
    root, py, _site, _here = project
    run = Recorder()
    log = root / "logs" / "bootstrap-x.log"
    result = bootstrap.install_requirements(py, root, ["aiohttp"], [], log_path=log, run=run,
                                            probe=_probe(False, True))
    assert result == {"full_ok": True, "failed_optional": []}
    assert run.calls[0] == [str(py), "-m", "ensurepip", "--default-pip"]     # 先补 pip，不联网
    assert run.calls[1][1:4] == ["-m", "pip", "install"] and "-r" in run.calls[1]
    assert not (root / ".venv" / bootstrap.NEEDS_REBUILD).exists()
    assert bootstrap.requirements_pending(root) is False                      # 指纹照常记上
    out = capsys.readouterr().out
    assert "[初始化] 运行环境里缺少 pip，正在原地补装（不联网）…" in out and "pip 已补回" in out


def test_pip_present_or_unknown_goes_straight_to_pip(project):
    root, py, _site, _here = project
    for answer in (True, None):          # 有 pip；探测不了（没有证据不动环境）
        run = Recorder()
        bootstrap.install_requirements(py, root, ["aiohttp"], [], run=run, probe=_probe(answer))
        assert not any("ensurepip" in c for c in run.calls)


@pytest.mark.parametrize("ensurepip_code, after", [(1, False), (0, False)],
                         ids=["ensurepip-failed", "ensurepip-ok-but-still-no-pip"])
def test_unrecoverable_pip_writes_the_marker_and_the_next_launch_rebuilds(project, ensurepip_code,
                                                                         after, capsys):
    root, py, site, here = project
    ready = _fake_core(site)             # 组件都在：没有记号时不会重建
    assert not bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=ready)
    run = Recorder(ensurepip=ensurepip_code)
    log = root / "logs" / "bootstrap-x.log"
    with pytest.raises(bootstrap.PipMissing) as info:
        bootstrap.install_requirements(py, root, ["aiohttp"], [], log_path=log, run=run,
                                       probe=_probe(False, after), now=1_800_000_000.0)
    assert [c for c in run.calls if "pip" in c[1:3]] == []          # 没 pip 就不去跑 pip install
    marker = root / ".venv" / bootstrap.NEEDS_REBUILD
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert set(data) == {"at", "kind"} and data["kind"] == "pip_missing"
    assert info.value.log_path == log and info.value.marked is True
    assert "[初始化] pip 原地补装没有成功，下次启动会重建运行环境" in capsys.readouterr().out
    # 下次启动：记号在、pip 还是没有——重建，「下次打开会自动重建」这句话属实
    assert bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=ready)
    # 版本读不到也认记号；进程跑在环境里面时不清（下次从启动器进来再重建）
    assert bootstrap.venv_needs_rebuild(True, None, None, python=py, ready_import=ready)
    assert not bootstrap.venv_needs_rebuild(True, here, here, own_env=True, python=py,
                                            ready_import=ready)
    assert marker.exists()


def test_a_marker_with_an_inconclusive_pip_probe_deletes_nothing(project, monkeypatch):
    """记号在，但这次 `import pip` 的探测本身出错（这里让它超时）：不知道 pip 在不在，就不按记号
    重建、也不删记号——和 pip_importable / venv_salvageable「没有证据不删东西」的约定一样
    （审查小问题：以前只要探测不是 True 就判重建）。探测恢复正常、pip 确实没有，照旧重建。"""
    root, py, site, here = project
    ready = _fake_core(site)
    assert bootstrap.mark_needs_rebuild(root)
    marker = root / ".venv" / bootstrap.NEEDS_REBUILD
    real_run = subprocess.run

    def pip_probe_times_out(cmd, *args, **kwargs):
        if list(cmd)[-2:] == ["-c", "import pip"]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)

    with monkeypatch.context() as stuck:
        stuck.setattr(bootstrap.subprocess, "run", pip_probe_times_out)
        assert bootstrap.pip_importable(py) is None
        assert not bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=ready)
        # 版本读不到时也一样：只有记号、没有确认缺 pip，不删
        assert not bootstrap.venv_needs_rebuild(True, None, None, python=py, ready_import=ready)
        assert marker.exists()
    assert bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=ready)
    assert marker.exists()


def test_a_marker_is_dropped_once_pip_is_back(project):
    """有人手动补回了 pip：记号作废，不白下 1.4 GB。"""
    root, py, site, here = project
    assert bootstrap.mark_needs_rebuild(root)
    marker = root / ".venv" / bootstrap.NEEDS_REBUILD
    assert bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=_fake_core(site))
    _fake_pip(site)
    assert not bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=NO_CORE)
    assert not marker.exists()


def test_the_marker_is_not_written_into_a_missing_venv(tmp_path):
    assert bootstrap.mark_needs_rebuild(tmp_path) is False
    assert not (tmp_path / ".venv").exists()


def test_a_rebuild_clears_the_marker(project):
    """记号放在 .venv 里：main.py 的重建（venv.create clear=True）把它一起清掉，不用另删。"""
    root, _py, _site, _here = project
    assert bootstrap.mark_needs_rebuild(root)
    venv_mod.create(root / ".venv", with_pip=False, clear=True, symlinks=(os.name != "nt"))
    assert not (root / ".venv" / bootstrap.NEEDS_REBUILD).exists()


# ---- 失败说明 ------------------------------------------------------------------------------

PIP_MISSING_ZH = ("程序的运行环境里缺少安装工具 pip，原地补装没有成功。\n"
                  "下次打开程序会自动重建运行环境（约 1.4 GB，需要联网）。")
PIP_MISSING_EN = ("The app’s runtime is missing pip, the tool that installs its components, and "
                  "reinstalling pip in place didn’t work.\nThe next time you open the app, it "
                  "rebuilds the runtime automatically (about 1.4 GB, needs an internet "
                  "connection).")
# 重建记号没写成：下次启动不会自动重建，不承诺，给照做的步骤
PIP_UNMARKED_ZH = ("程序的运行环境里缺少安装工具 pip，原地补装没有成功。\n"
                   "请删掉程序文件夹里的 .venv 文件夹，再重新打开程序，它会重新安装运行组件"
                   "（约 1.4 GB，需要联网）。")
PIP_UNMARKED_EN = ("The app’s runtime is missing pip, the tool that installs its components, and "
                   "reinstalling pip in place didn’t work.\nDelete the .venv folder in the app "
                   "folder, then open the app again. It reinstalls its components (about 1.4 GB, "
                   "needs an internet connection).")


def _refuse_marker_writes(monkeypatch):
    """写重建记号时报 PermissionError（.venv 只读那种）；别的文件照常写。"""
    real_write = Path.write_text

    def refuse(self, *args, **kwargs):
        if self.name == bootstrap.NEEDS_REBUILD:
            raise PermissionError(13, "Permission denied", str(self))
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse)


def test_the_dialog_for_missing_pip_says_what_happens_next_not_check_the_network(tmp_path):
    log = tmp_path / "logs" / "bootstrap-x.log"
    text = bootstrap.failure_text(bootstrap.PipMissing(log, marked=True), python_version="3.13",
                                  root=tmp_path)
    assert text == PIP_MISSING_ZH + "\n详细记录：{}".format(log)
    english = i18n.text(text, i18n.EN)
    assert english == PIP_MISSING_EN + "\nLog file: {}".format(log)
    assert not i18n.CJK.search(english)
    for t in (text, english):
        assert "网络" not in t and "network" not in t.lower() and "pip install" not in t
    assert bootstrap.failure_text(bootstrap.PipMissing(marked=True)) == PIP_MISSING_ZH
    # 没说记号写没写成：按没写成算，不承诺下次会重建
    assert bootstrap.failure_text(bootstrap.PipMissing()) == PIP_UNMARKED_ZH
    assert i18n.text(i18n.of(bootstrap.PipMissing()), i18n.EN) == \
        "pip is missing from .venv and couldn’t be reinstalled in place"


def test_a_marker_that_could_not_be_written_is_not_promised(project, monkeypatch, capsys):
    """审查小问题：记号写不上（.venv 只读、写文件报错）时，下次启动不会自动重建，以前终端和对话框
    照样说「下次会重建」。现在终端和对话框都改给删 .venv 再打开的步骤。"""
    root, py, site, here = project
    ready = _fake_core(site)
    _refuse_marker_writes(monkeypatch)
    run = Recorder(ensurepip=1)
    log = root / "logs" / "bootstrap-x.log"
    with pytest.raises(bootstrap.PipMissing) as info:
        bootstrap.install_requirements(py, root, ["aiohttp"], [], log_path=log, run=run,
                                       probe=_probe(False))
    assert info.value.marked is False
    assert not (root / ".venv" / bootstrap.NEEDS_REBUILD).exists()
    out = capsys.readouterr().out
    assert ("[初始化] pip 原地补装没有成功，重建记号也没写上：请删掉程序文件夹里的 .venv 文件夹，"
            "再重新打开程序") in out
    assert "下次启动会重建" not in out
    text = bootstrap.failure_text(info.value, python_version=here, root=root)
    assert text == PIP_UNMARKED_ZH + "\n详细记录：{}".format(log)
    english = i18n.text(text, i18n.EN)
    assert english == PIP_UNMARKED_EN + "\nLog file: {}".format(log)
    assert not i18n.CJK.search(english)
    for t in (text, english):
        assert "网络" not in t and "network" not in t.lower() and "pip install" not in t
    # 对得上：没有记号，下次启动确实不会因为缺 pip 重建（组件都在）
    assert not bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=ready)


def test_a_no_module_named_pip_failure_without_a_marker_gives_the_steps(project, monkeypatch):
    """PipFailed 的输出里是 No module named pip、补写记号又没写成（或者不知道项目根）：同样不承诺。"""
    root, py, _site, here = project
    tail = "{}: No module named pip".format(py)
    failed = bootstrap.PipFailed(1, tail)
    assert bootstrap.failure_text(failed, python_version=here) == PIP_UNMARKED_ZH
    _refuse_marker_writes(monkeypatch)
    assert bootstrap.failure_text(failed, python_version=here, root=root) == PIP_UNMARKED_ZH
    assert not (root / ".venv" / bootstrap.NEEDS_REBUILD).exists()


def test_a_pip_failure_saying_no_module_named_pip_is_explained_the_same_way(project):
    """万一 pip 的输出里就是 No module named pip（以前落到「请检查网络连接」那一句）：
    说法同上，而且补写记号，下次启动真的会重建。"""
    root, py, site, here = project
    ready = _fake_core(site)
    tail = "{}: No module named pip".format(py)
    failed = bootstrap.PipFailed(1, tail, root / "logs" / "bootstrap-x.log")
    text = bootstrap.failure_text(failed, python_version=here, root=root)
    assert text.startswith(PIP_MISSING_ZH) and "网络" not in text
    assert bootstrap.venv_needs_rebuild(True, here, here, python=py, ready_import=ready)
    # 别的 pip 失败照旧按原来的分类说
    other = bootstrap.failure_text(bootstrap.PipFailed(2, "ERROR: something new"),
                                   python_version="3.13")
    assert "检查网络" in other and "pip 返回 2" in other


# ---- 真跑一次 ensurepip（只在临时 venv 里，不联网） -----------------------------------------

@pytest.mark.skipif(importlib.util.find_spec("ensurepip") is None, reason="这个 Python 没带 ensurepip")
def test_real_ensurepip_puts_pip_back_into_a_bare_venv(project):
    """用真的 run_logged 和真的探测：ensurepip 用 Python 自带的 pip 安装包，几秒钟、不联网。
    只装 pip，不跑 pip install（那一步要联网），所以直接调 ensure_pip。"""
    root, py, _site, _here = project
    log = root / "logs" / "bootstrap-ensurepip.log"
    log.parent.mkdir()
    assert bootstrap.pip_importable(py) is False
    assert bootstrap.ensure_pip(py, root, log) is True
    assert bootstrap.pip_importable(py) is True
    assert "ensurepip" in log.read_text(encoding="utf-8")                  # 输出进了 bootstrap 日志
    assert not (root / ".venv" / bootstrap.NEEDS_REBUILD).exists()
