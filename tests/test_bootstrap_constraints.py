"""依赖版本锁与最低 Python（2026-10-21 专家审查 I4；用户决定 D8「按 Python 版本分开锁」、
D14「最低提到 3.10」）。

I4：requirements.txt 只写下限，新装机拿到的是 PyPI 当天最新、从没一起跑过的组合。现在 constraints.txt
记着主人那台机器上实际跑着的那一套，所有 `pip install -r` 都带 `-c constraints.txt`。钉住：
- constraints.txt 本身：每一条锁死（==）的都带 python_version 标记，而且起点不早于 3.11（3.10 只设
  上界）；每个锁死的包都有一条不带标记的上界；yt-dlp、curl_cffi、TikTokLive 不锁；标记在几种机器上
  求值不出错（Linux 的内核版本号不是合法版本号，不能拿 platform_release 比大小）；
- 指纹把 constraints.txt 也算进去：锁的版本一变，已有安装下次启动补装一次（plan_install 给的是
  「清单变了」那一种，不重建）；没有 constraints.txt 时指纹和以前逐字节相同；
- bootstrap.install_requirements 的每一次 pip（整份、只装核心、逐个装可选组件）都带 -c；
- 自检和一键更新给的手动命令带 -c。

D14：main.py 在 3.9 上不再往下跑，弹出「需要升级」和照做的步骤（app/bootstrap.refuse_old_python）；
.venv 是 3.9 建的时，换了新 Python 启动会重建它，不会再换进 3.9 里退出。main.py 在 3.9 上要能
走到版本检查：它和检查之前 import 的模块都不能用 3.10 才有的语法。

全部在 tmp_path 里，不碰项目自己的 .venv，不连网，不起 pip。
"""
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app import bootstrap, i18n

REPO = Path(__file__).resolve().parent.parent
CONSTRAINTS = REPO / "constraints.txt"
REQUIREMENTS = REPO / "requirements.txt"
CHASED = ("yt-dlp", "curl-cffi", "tiktoklive")


def _canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _entries():
    """constraints.txt 的每一条：(包名, 版本说明, 标记或 None)。"""
    out = []
    for raw in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        spec, _, marker = line.partition(";")
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)", spec.strip())
        out.append((_canon(m.group(1)), m.group(2).strip(), marker.strip() or None))
    return out


# ---- constraints.txt 本身 -------------------------------------------------------------------

def test_every_pinned_version_carries_a_python_version_marker_starting_at_311_or_later():
    pinned = [e for e in _entries() if e[1].startswith("==")]
    assert len(pinned) >= 20
    for name, spec, marker in pinned:
        assert marker and "python_version" in marker, (name, spec)
        floors = re.findall(r'python_version >= "3\.(\d+)"', marker)
        assert floors and all(int(f) >= 11 for f in floors), (name, marker)
        assert 'python_version < "3.' in marker, (name, marker)     # 不管还没出安装包的新 Python


def test_every_pinned_package_also_has_an_unconditional_upper_bound_except_calendar_versions():
    entries = _entries()
    pinned = {n for n, s, _ in entries if s.startswith("==")}
    bounded = {n for n, s, m in entries if s.startswith("<") and m is None}
    calendar = {"packaging", "flatbuffers", "fsspec", "regex"}
    assert pinned - bounded == calendar & pinned


def test_the_upper_bound_is_the_next_major_of_the_pinned_version():
    entries = _entries()
    pins = {n: s[2:] for n, s, _ in entries if s.startswith("==")}
    for name, spec, _ in entries:
        if spec.startswith("<"):
            assert spec == "<{}".format(int(pins[name].split(".")[0]) + 1), (name, spec)


def test_the_chased_components_are_not_constrained():
    names = {n for n, _, _ in _entries()}
    for name in CHASED:
        assert name not in names, name


def test_every_requirement_is_covered_or_deliberately_left_free():
    """requirements.txt 里的每个直接依赖要么锁着，要么就是上面三个追新的。"""
    pinned = {n for n, s, _ in _entries() if s.startswith("==")}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        name = bootstrap.requirement_name(line)
        if name:
            assert name in pinned or name in CHASED, name


def test_each_package_appears_at_most_once_per_kind():
    seen = set()
    for name, spec, _ in _entries():
        key = (name, spec[:2] if spec.startswith("==") else spec[:1])
        assert key not in seen, key
        seen.add(key)


ENVS = {
    "apple-silicon-macos-27": dict(sys_platform="darwin", platform_machine="arm64",
                                   python_version="3.13", python_full_version="3.13.5",
                                   platform_version="Darwin Kernel Version 26.0.0: x",
                                   platform_release="26.0.0"),
    "apple-silicon-macos-13": dict(sys_platform="darwin", platform_machine="arm64",
                                   python_version="3.13", python_full_version="3.13.5",
                                   platform_version="Darwin Kernel Version 22.6.0: x",
                                   platform_release="22.6.0"),
    "intel-mac": dict(sys_platform="darwin", platform_machine="x86_64", python_version="3.13",
                      python_full_version="3.13.1", platform_version="Darwin Kernel Version 24.0.0: x",
                      platform_release="24.0.0"),
    "windows-311": dict(sys_platform="win32", platform_machine="AMD64", python_version="3.11",
                        python_full_version="3.11.9", platform_version="10.0.26100",
                        platform_release="11"),
    "windows-310": dict(sys_platform="win32", platform_machine="AMD64", python_version="3.10",
                        python_full_version="3.10.11", platform_version="10.0.19045",
                        platform_release="10"),
    "linux": dict(sys_platform="linux", platform_machine="x86_64", python_version="3.13",
                  python_full_version="3.13.0", platform_version="#45-Ubuntu SMP PREEMPT_DYNAMIC",
                  platform_release="6.8.0-45-generic"),
    "python-315": dict(sys_platform="darwin", platform_machine="arm64", python_version="3.15",
                       python_full_version="3.15.0", platform_version="Darwin Kernel Version 26.0.0: x",
                       platform_release="26.0.0"),
}


def _pinned_in(env_name):
    pytest.importorskip("pip")
    from pip._vendor.packaging.markers import Marker     # pip 求值用的就是它自带的这份

    env = dict(os_name="posix", platform_system="", platform_python_implementation="CPython",
               implementation_name="cpython", implementation_version="3.13.0", extra="",
               **ENVS[env_name])
    return {n for n, s, m in _entries() if s.startswith("==") and Marker(m).evaluate(env)}


@pytest.mark.parametrize("env_name", sorted(ENVS))
def test_markers_evaluate_on_every_kind_of_machine(env_name):
    """pip 对每一条标记求值都不能出错——Linux 的内核版本号 6.8.0-45-generic 不是合法版本号，拿
    platform_release 比大小会让 pip 当场报错，整份安装都装不上。"""
    _pinned_in(env_name)


def test_the_pins_apply_where_the_tested_set_has_installers_and_nowhere_else():
    assert {"numpy", "torch", "onnxruntime", "faster-whisper"} <= _pinned_in("apple-silicon-macos-27")
    assert {"onnxruntime", "faster-whisper", "ctranslate2"} <= _pinned_in("windows-311")
    # numpy 2.5 要 Python 3.12：3.11 上只设上界
    assert "numpy" not in _pinned_in("windows-311")
    # macOS 13 及更早的 Apple 芯片：只在 macOS 14 起才有安装包的那几个不锁，其余照锁
    old_mac = _pinned_in("apple-silicon-macos-13")
    assert not old_mac & {"onnxruntime", "torch", "av", "mlx", "mlx-metal"}
    assert "faster-whisper" in old_mac
    for env_name in ("intel-mac", "windows-310", "linux", "python-315"):
        assert _pinned_in(env_name) == set(), env_name


# ---- 指纹 ------------------------------------------------------------------------------------

def _project(tmp_path, constraints="numpy==2.5.2; python_version >= \"3.12\"\n"):
    root = tmp_path / "project"
    (root / ".venv").mkdir(parents=True)
    (root / "requirements.txt").write_text("aiohttp>=3.9\nnumpy>=1.24\n", encoding="utf-8")
    if constraints is not None:
        (root / "constraints.txt").write_text(constraints, encoding="utf-8")
    return root


def test_without_constraints_the_digest_is_the_old_one(tmp_path):
    """没有 constraints.txt（老版本、ZIP 里没带）：指纹和以前逐字节相同，已有安装不白补装一次。"""
    root = _project(tmp_path, constraints=None)
    req = (root / "requirements.txt").read_bytes()
    import hashlib
    assert bootstrap.requirements_file_digest(root) == hashlib.sha256(req).hexdigest()
    assert bootstrap.requirements_digest(req) == hashlib.sha256(req).hexdigest()


def test_constraints_change_the_digest_and_crlf_does_not(tmp_path):
    root = _project(tmp_path)
    req = (root / "requirements.txt").read_text(encoding="utf-8")
    con = (root / "constraints.txt").read_text(encoding="utf-8")
    with_c = bootstrap.requirements_file_digest(root)
    assert with_c == bootstrap.requirements_digest(req, con)
    assert with_c != bootstrap.requirements_digest(req)
    assert with_c != bootstrap.requirements_digest(req, con.replace("2.5.2", "2.5.3"))
    # Windows 检出成 CRLF 的工作区和 git show 拿到的 LF 版本是同一份
    assert bootstrap.requirements_digest(req.replace("\n", "\r\n"), con.replace("\n", "\r\n")) == with_c
    # 两份内容不能挪来挪去还算同一个指纹
    assert bootstrap.requirements_digest(req + con, "") != with_c


def test_an_unreadable_constraints_file_gives_no_digest(tmp_path, monkeypatch):
    """constraints.txt 在、却读不了：pip -c 一样读不了，不知道该装成什么样——不给指纹（不触发补装），
    和 requirements.txt 读不了时一样。"""
    root = _project(tmp_path)
    real = Path.read_bytes

    def flaky(self):
        if self.name == "constraints.txt":
            raise PermissionError("denied")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", flaky)
    assert bootstrap.requirements_file_digest(root) is None
    assert bootstrap.requirements_pending(root) is False


def test_a_changed_lock_reinstalls_once_in_place_without_rebuilding(tmp_path):
    """已有安装（清单装成功过、组件都在）拿到新的 constraints.txt：下次启动补装一次（「清单变了」那句，
    5 秒后才弹），不重建；装成功写下新指纹之后，再启动就不装了。"""
    root = _project(tmp_path, constraints=None)
    bootstrap.write_requirements_stamp(root, bootstrap.requirements_file_digest(root))
    assert bootstrap.plan_install(True, root, "3.13", "3.13") is None
    (root / "constraints.txt").write_text('numpy==2.5.2; python_version >= "3.12"\n', encoding="utf-8")
    plan = bootstrap.plan_install(True, root, "3.13", "3.13")
    assert plan is not None and plan["delay"] == bootstrap.REQUIREMENTS_DIALOG_DELAY_SEC
    assert plan["text"].startswith("组件清单和上次安装时不一样了")
    assert not bootstrap.venv_needs_rebuild(True, "3.13", "3.13", python=None)
    bootstrap.write_requirements_stamp(root, bootstrap.requirements_file_digest(root))
    assert bootstrap.plan_install(True, root, "3.13", "3.13") is None


# ---- pip 命令都带 -c ---------------------------------------------------------------------------

class Run:
    """install_requirements 的 run 替身：记下每条命令，按 codes 依次返回退出码。"""

    def __init__(self, *codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, cmd, log_path=None):
        self.calls.append([str(c) for c in cmd])
        return (self.codes.pop(0) if self.codes else 0), ""


def _install(root, run):
    return bootstrap.install_requirements("/venv/bin/python", root, ["aiohttp>=3.9"],
                                          ["curl_cffi>=0.7", "pywebview>=5"],
                                          run=run, probe=lambda py: True)


def test_the_full_install_passes_the_constraints(tmp_path):
    root = _project(tmp_path)
    run = Run(0)
    assert _install(root, run)["full_ok"]
    cmd = run.calls[0]
    assert cmd[cmd.index("-r") + 1] == str(root / "requirements.txt")
    assert cmd[cmd.index("-c") + 1] == str(root / "constraints.txt")
    assert (root / ".venv" / bootstrap.REQ_STAMP).read_text(encoding="utf-8").strip() == \
        bootstrap.requirements_file_digest(root)


def test_the_fallback_installs_pass_the_constraints_too(tmp_path):
    """整份装不上时改为先装核心、再逐个装可选组件：每一次都带 -c，不然逐个装的那几个又是 PyPI 最新的。"""
    root = _project(tmp_path)
    run = Run(1, 0, 0, 1)
    result = _install(root, run)
    assert result == {"full_ok": False, "failed_optional": ["pywebview>=5"]}
    assert len(run.calls) == 4
    for cmd in run.calls:
        assert cmd[cmd.index("-c") + 1] == str(root / "constraints.txt"), cmd


def test_the_mlx_giveup_copy_still_uses_the_real_constraints(tmp_path):
    root = _project(tmp_path)
    (root / ".venv" / bootstrap.MLX_GIVEUP).write_text("{}", encoding="utf-8")
    run = Run(0)
    _install(root, run)
    cmd = run.calls[0]
    assert cmd[cmd.index("-r") + 1] != str(root / "requirements.txt")      # 去掉 mlx-whisper 的副本
    assert cmd[cmd.index("-c") + 1] == str(root / "constraints.txt")


def test_without_a_constraints_file_no_c_flag_is_passed(tmp_path):
    root = _project(tmp_path, constraints=None)
    run = Run(0)
    _install(root, run)
    assert "-c" not in run.calls[0]


def test_the_repo_ships_a_constraints_file_next_to_requirements():
    assert bootstrap.constraints_args(REPO) == ["-c", str(REPO / "constraints.txt")]


# ---- 手动命令 ----------------------------------------------------------------------------------

def test_selfcheck_manual_commands_carry_the_constraints(monkeypatch):
    from app import selfcheck
    monkeypatch.setattr(sys, "executable", "/opt/py/bin/python3")
    for os_name, prefix in (("posix", '"/opt/py/bin/python3"'), ("nt", '& "/opt/py/bin/python3"')):
        monkeypatch.setattr(selfcheck.os, "name", os_name)
        cmd = str(selfcheck._pip_command("mlx-whisper", upgrade=False))
        assert prefix + ' -m pip install "mlx-whisper" -c "{}"'.format(REPO / "constraints.txt") in cmd


def test_selfcheck_manual_command_without_a_constraints_file(monkeypatch, tmp_path):
    from app import selfcheck
    monkeypatch.setattr(selfcheck, "ROOT", tmp_path)
    monkeypatch.setattr(selfcheck.os, "name", "posix")
    assert " -c " not in str(selfcheck._pip_command("mlx-whisper", upgrade=False))


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


# ---- 一键更新：按新版本的 constraints.txt 装、记指纹 ----------------------------------------------

@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """同 tests/test_resilience_update.py 的 isolated：设置文件和项目目录都在临时目录里。"""
    from app import settings as settings_mod
    from app import updater
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(updater, "ROOT", tmp_path)
    return tmp_path


CONS = 'aiohttp==3.14.3; python_version >= "3.11"\naiohttp<4\n'


def _update(monkeypatch, isolated, overrides=None, pip_code=0, managed=False):
    """跑一次一键更新（假 git、假 pip）。managed：当成本项目自己的 .venv（读写清单指纹）。"""
    from app import updater
    from tests.helpers import run
    from tests.test_resilience_update import RecordingServer, make_updater
    server, events = RecordingServer(), []
    u, pip_calls, execs = make_updater(monkeypatch, server, events, git_overrides=overrides,
                                       pip_code=pip_code)
    if managed:
        (isolated / ".venv").mkdir(exist_ok=True)
        monkeypatch.setattr(updater, "managed_env", lambda root: True)
    seen = []
    real = u._pip_install

    async def capture(args, label, **kw):
        if "-c" in args:
            seen.append(Path(args[args.index("-c") + 1]).read_text(encoding="utf-8"))
        else:
            seen.append(None)
        return await real(args, label, **kw)
    u._pip_install = capture
    run(u.apply())
    return server, pip_calls, execs, seen


def test_the_update_installs_with_the_new_versions_constraints(monkeypatch, isolated):
    """合并之前按新版本的清单装依赖：锁文件也从新版本里拿（git show），不是工作区里旧的那份。"""
    from tests.test_resilience_update import REQS
    (isolated / "constraints.txt").write_text("old==1\n", encoding="utf-8")
    _server, pip_calls, execs, seen = _update(
        monkeypatch, isolated, {"show:constraints.txt": (0, CONS, "")}, managed=True)
    assert seen == [CONS] and len(execs) == 1
    assert pip_calls[0]["args"][0] == "-r" and pip_calls[0]["requirements"] == REQS
    stamp = (isolated / ".venv" / bootstrap.REQ_STAMP).read_text(encoding="utf-8").strip()
    assert stamp == bootstrap.requirements_digest(REQS, CONS)


def test_the_core_only_retry_keeps_the_constraints(monkeypatch, isolated):
    from tests.test_resilience_update import OPTIONAL_REQS
    _server, pip_calls, _execs, seen = _update(
        monkeypatch, isolated, {"show:requirements.txt": (0, OPTIONAL_REQS, ""),
                                "show:constraints.txt": (0, CONS, "")}, pip_code=[1, 0])
    assert len(pip_calls) == 2 and seen == [CONS, CONS]


def test_a_target_without_constraints_installs_like_before(monkeypatch, isolated):
    """新版本里没有 constraints.txt（git show 失败）：照以前那样只带 -r，指纹也是以前那种。"""
    from tests.test_resilience_update import REQS
    _server, pip_calls, _execs, seen = _update(monkeypatch, isolated, managed=True)
    assert seen == [None] and pip_calls[0]["args"] == ["-r", pip_calls[0]["args"][1]]
    stamp = (isolated / ".venv" / bootstrap.REQ_STAMP).read_text(encoding="utf-8").strip()
    assert stamp == bootstrap.requirements_digest(REQS)


def test_an_unchanged_requirements_and_lock_skip_pip(monkeypatch, isolated):
    """清单和锁都和上次装成功的一样：不跑 pip。只有锁变了：要跑。"""
    from tests.test_resilience_update import REQS
    (isolated / ".venv").mkdir()
    bootstrap.write_requirements_stamp(isolated, bootstrap.requirements_digest(REQS, CONS))
    _s, pip_calls, execs, _seen = _update(monkeypatch, isolated,
                                          {"show:constraints.txt": (0, CONS, "")}, managed=True)
    assert pip_calls == [] and len(execs) == 1
    bootstrap.write_requirements_stamp(isolated, bootstrap.requirements_digest(REQS, CONS))
    _s, pip_calls, _e, seen = _update(monkeypatch, isolated,
                                      {"show:constraints.txt": (0, CONS.replace("3.14.3", "3.14.4"), "")},
                                      managed=True)
    assert len(pip_calls) == 1 and "3.14.4" in seen[0]


@pytest.mark.parametrize("windows", [False, True], ids=["terminal", "powershell"])
def test_the_manual_update_command_carries_the_constraints(monkeypatch, isolated, windows):
    from app import updater
    monkeypatch.setattr(updater, "_on_windows", lambda: windows)
    cmd = updater.Updater.__new__(updater.Updater)._manual_command(pip=True)
    assert "-m pip install -r requirements.txt -c constraints.txt" in cmd
    assert "-c constraints.txt" not in updater.Updater.__new__(updater.Updater)._manual_command()
