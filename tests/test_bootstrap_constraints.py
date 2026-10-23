"""最低 Python 提到 3.10（2026-10-21 专家审查 I4 的待决定 D14，用户 10-22 选了「最低提到 3.10」）。

main.py 在 3.9 上不再往下跑，弹出「需要升级」和照做的步骤（app/bootstrap.refuse_old_python）；
.venv 是 3.9 建的时，换了新 Python 启动会重建它，不会再换进 3.9 里退出。main.py 在 3.9 上要能
走到版本检查：它和检查之前 import 的模块都不能用 3.10 才有的语法。

全部在 tmp_path 里，不碰项目自己的 .venv，不连网，不起 pip。
"""
import ast
import os
import subprocess
from pathlib import Path

import pytest

from app import bootstrap, i18n

REPO = Path(__file__).resolve().parent.parent


# ---- D14：最低 Python 3.10 --------------------------------------------------------------------

def test_the_minimum_is_310():
    assert bootstrap.MIN_PYTHON == (3, 10)
    assert bootstrap.python_too_old((3, 9, 18))
    assert not bootstrap.python_too_old((3, 10, 0))
    assert not bootstrap.python_too_old((3, 13, 5))


def _main_tree():
    return ast.parse((REPO / "main.py").read_text(encoding="utf-8"))


def test_main_checks_the_version_with_the_bootstrap_minimum_and_refuses_through_bootstrap():
    """main.py 的版本检查用的就是 bootstrap.MIN_PYTHON 那个数，不过就交给 refuse_old_python。"""
    for node in _main_tree().body:
        if isinstance(node, ast.If) and "sys.version_info" in ast.unparse(node.test):
            assert ast.unparse(node.test) == "sys.version_info < {}".format(bootstrap.MIN_PYTHON)
            assert "refuse_old_python" in ast.unparse(node)
            return
    raise AssertionError("main.py 里没有版本检查")


def _modules_before_the_version_check():
    """main.py 在版本检查之前 import 的本项目模块，连同它们模块级 import 的本项目模块。"""
    tree = _main_tree()
    found = []
    for node in tree.body:
        if isinstance(node, ast.If) and "sys.version_info" in ast.unparse(node.test):
            found += [n for n in ast.walk(node) if isinstance(n, ast.ImportFrom)]
            break
        found += [n for n in ast.walk(node) if isinstance(n, (ast.Import, ast.ImportFrom))]
    todo = []
    for node in found:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "app":
            if node.module == "app":
                todo += ["app/{}.py".format(a.name) for a in node.names]
            else:
                todo.append(node.module.replace(".", "/") + ".py")
    seen = set()
    while todo:
        rel = todo.pop()
        if rel in seen or not (REPO / rel).exists():
            continue
        seen.add(rel)
        for node in ast.parse((REPO / rel).read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                if node.module:
                    todo.append("app/{}.py".format(node.module))
                else:
                    todo += ["app/{}.py".format(a.name) for a in node.names]
    return sorted(seen)


def test_the_files_that_run_before_the_version_check_still_parse_as_python_39():
    """3.9 上要能走到版本检查、弹出「需要升级」：main.py 和检查之前 import 的模块（含
    refuse_old_python 所在的 bootstrap）都不能用 3.10 才有的语法（match 这类；ast 的 feature_version
    是尽力而为，括号里的多个 with 它查不出），也不能在签名或注解里写 X | Y（3.9 上定义函数时就报错，
    feature_version 也不管它，所以另外查）。CI 不再有 3.9 的跑器，这里是唯一看门的。"""
    files = ["main.py"] + _modules_before_the_version_check()
    assert {"app/i18n.py", "app/native_dialog.py", "app/bootstrap.py"} <= set(files)
    for rel in files:
        source = (REPO / rel).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel, feature_version=(3, 9))
        future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                     and any(a.name == "annotations" for a in n.names) for n in tree.body)
        if future:
            continue
        for node in ast.walk(tree):
            notes = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                notes = [a.annotation for a in args.posonlyargs + args.args + args.kwonlyargs]
                notes += [node.returns]
            elif isinstance(node, ast.AnnAssign):
                notes = [node.annotation]
            for note in notes:
                if note is not None:
                    assert not any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
                                   for n in ast.walk(note)), (rel, node.lineno)


@pytest.mark.parametrize("darwin", [True, False], ids=["macos", "windows"])
def test_the_upgrade_text_says_what_was_seen_and_what_to_do(darwin):
    text = bootstrap.python_too_old_text("3.9", darwin=darwin)
    assert "3.9" in text and "3.10" in text and "需要升级" in text and "python.org" in text
    en = i18n.text(text, i18n.EN)
    assert not i18n.CJK.search(en) and "3.9" in en and "3.10" in en and "python.org" in en
    assert ("完全磁盘访问权限" in text) is darwin
    assert ("Full Disk Access" in en) is darwin
    for word in ("可能", "似乎", "might", "maybe", "probably"):
        assert word not in text and word not in en


class Dialogs:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kw):
        self.calls.append(("osascript", argv))
        return subprocess.CompletedProcess(argv, 0)

    def box(self, text, title):
        self.calls.append(("messagebox", text, title))


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_refusing_prints_chinese_and_shows_a_dialog_without_a_console(platform, capsys, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    d = Dialogs()
    code = bootstrap.refuse_old_python((3, 9, 18), has_console=False, sys_platform=platform,
                                       run=d.run, messagebox=d.box)
    assert code == 1
    out = capsys.readouterr().out
    assert "需要升级" in out and "3.9" in out                 # 终端永远是中文
    expected = bootstrap.python_too_old_text("3.9", darwin=platform == "darwin")
    if platform == "darwin":
        assert d.calls and d.calls[0][0] == "osascript"
        from app import native_dialog
        assert d.calls[0][1] == native_dialog.osascript_argv(
            expected, button=native_dialog.FAIL_BUTTON, icon="caution")
    elif platform == "win32":
        from app import native_dialog
        assert d.calls == [("messagebox",) + native_dialog.messagebox_args(expected)]
    else:
        assert d.calls == []


def test_refusing_in_a_terminal_or_in_ci_only_prints(capsys, monkeypatch):
    d = Dialogs()
    monkeypatch.delenv("CI", raising=False)
    assert bootstrap.refuse_old_python((3, 9, 18), has_console=True, sys_platform="darwin",
                                       run=d.run, messagebox=d.box) == 1
    monkeypatch.setenv("CI", "1")
    assert bootstrap.refuse_old_python((3, 9, 18), has_console=False, sys_platform="darwin",
                                       run=d.run, messagebox=d.box) == 1
    assert d.calls == []
    assert capsys.readouterr().out.count("需要升级") == 2


def test_a_failing_dialog_does_not_hide_the_exit(capsys, monkeypatch):
    monkeypatch.delenv("CI", raising=False)

    def broken(*a, **k):
        raise OSError("no osascript")
    assert bootstrap.refuse_old_python((3, 9, 18), has_console=False, sys_platform="darwin",
                                       run=broken, messagebox=broken) == 1


def test_a_venv_built_by_39_is_rebuilt_by_a_newer_launch(tmp_path):
    """升级前的 .venv 是 3.9 建的：启动器跳过它、用新装的 Python 启动 main.py 时，ensure_env 要重建
    .venv——以前组件和 pip 都在就原地补装，然后换进 3.9 的 .venv，在版本检查上退出，永远起不来。"""
    assert bootstrap.venv_needs_rebuild(True, "3.9", "3.9", python=tmp_path / "py")
    assert bootstrap.venv_needs_rebuild(True, None, "3.9", python=tmp_path / "py")
    # 进程本来就在 .venv 里：照旧不从里面清空自己（那样的进程过不了 main.py 的版本检查）
    assert not bootstrap.venv_needs_rebuild(True, "3.9", "3.9", own_env=True, python=tmp_path / "py")
    # 3.10 起照旧按原来的判断（没有证据不重建）
    assert not bootstrap.venv_needs_rebuild(True, "3.10", "3.10", python=None)


def test_the_ready_fast_path_never_skips_into_a_39_venv(tmp_path):
    """就绪指纹（I9）对得上也不行：实际跑出来是 3.9，就交给全检查（那里会重建）。"""
    root = tmp_path / "project"
    venv = root / ".venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    py.parent.mkdir(parents=True)
    py.write_bytes(b"not python")
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.9.6\n", encoding="utf-8")
    (root / "requirements.txt").write_text("aiohttp>=3.9\n", encoding="utf-8")
    bootstrap.write_requirements_stamp(root, bootstrap.requirements_file_digest(root))
    imports = "import aiohttp"
    assert bootstrap.note_ready(root, imports, prefix=venv)
    assert not bootstrap.ready_fast_path(root, imports, actual_version="3.9")
