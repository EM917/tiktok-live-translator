"""启动自举的两处判断（2026-10-21 专家审查 I9、I7），都在 app/bootstrap.py。

I9 每次启动三次解释器跳转、约 4 秒的重型 import 白做：最终进程把 .venv 此刻的样子记进
.venv/.ready-fingerprint，下次启动的第一跳只读文件比一比，没变就跳过子进程 import 检查和 pip 探测。
钉住：指纹由哪些东西组成、只在本项目的 .venv/.app 壳里记和删、哪些变化让它失效——清单、
上次装成功的那份、pyvenv.cfg、.venv/bin/python 链接终点的真实路径/大小/修改时间（计划审查 Q8：
conda 原地升级次版本时 cfg 一个字不变）、要 import 的模块、重建记号、24 小时的重试。

I7 清单变了之后再启动一次，会在直播实例正用着的 .venv 里跑 pip -r：现有 .venv 能跑、只是清单变了、
端口上有本程序在跑时这次不装（defer_install）；这一轮刚建的、核心组件 import 不齐的照旧装。换进去的
那个进程在 POSIX 上凭环境变量里的 PID 认出来、也不装；Windows 上认不出（中间隔着 venv 的重定向器），
靠最终进程在 ensure_env 里自己再判一次——这条兜底在 main.py 里的位置也钉住。

main.py 不能 import（模块级就跑 ensure_env、会 execv），它的接线读语法树。全部在 tmp_path 里，
不碰项目自己的 .venv，不起子进程，不连网。
"""
import ast
import json
import os
import sys
from pathlib import Path

import pytest

from app import bootstrap

REPO = Path(__file__).resolve().parent.parent
IMPORTS = "import aiohttp, numpy, faster_whisper, yt_dlp, imageio_ffmpeg"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="要建符号链接（.venv/bin/python 的形状）")


def _venv_python(root):
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.fixture
def project(tmp_path):
    """临时项目根：清单装好了（有 .requirements.sha256）的 .venv，解释器是个普通文件。"""
    root = tmp_path / "project"
    venv = root / ".venv"
    py = _venv_python(root)
    py.parent.mkdir(parents=True)
    py.write_bytes(b"\x7fELF-not-really-python")
    (venv / "pyvenv.cfg").write_text("home = /opt/x/bin\nversion = 3.13.5\n", encoding="utf-8")
    (root / "requirements.txt").write_text("aiohttp>=3.9\nnumpy>=1.24\n", encoding="utf-8")
    bootstrap.write_requirements_stamp(root, bootstrap.requirements_file_digest(root))
    return root


def _note(root, imports=IMPORTS, python=None):
    return bootstrap.note_ready(root, imports, prefix=root / ".venv", python=python)


# ---- I9 指纹的组成 -----------------------------------------------------------------------------

def test_the_fingerprint_is_made_of_files_only(project):
    py = _venv_python(project)
    fp = bootstrap.ready_fingerprint(project, IMPORTS)
    st = os.stat(os.path.realpath(str(py)))
    assert fp == {
        "v": bootstrap.READY_FINGERPRINT_VERSION,
        "requirements": bootstrap.requirements_file_digest(project),
        "stamp": bootstrap.recorded_requirements_digest(project),
        "pyvenv_cfg": bootstrap._file_sha256(project / ".venv" / "pyvenv.cfg"),
        "python": {"path": os.path.realpath(str(py)), "size": st.st_size,
                   "mtime_ns": st.st_mtime_ns},
        "imports": IMPORTS,
    }
    assert bootstrap.venv_python(project) == py


def test_no_fingerprint_without_an_interpreter_or_pyvenv_cfg(project):
    (project / ".venv" / "pyvenv.cfg").unlink()
    assert bootstrap.ready_fingerprint(project, IMPORTS) is None
    assert _note(project) is False
    assert not (project / ".venv" / bootstrap.READY_FINGERPRINT).exists()


def test_the_final_process_records_it_and_the_first_hop_matches_it(project):
    assert bootstrap.ready_fast_path(project, IMPORTS) is False                                     # 还没记过：照旧检查
    assert _note(project) is True
    path = project / ".venv" / bootstrap.READY_FINGERPRINT
    assert json.loads(path.read_text(encoding="utf-8")) == bootstrap.ready_fingerprint(
        project, IMPORTS)
    assert bootstrap.ready_fast_path(project, IMPORTS) is True
    assert [p.name for p in (project / ".venv").iterdir() if p.name.endswith(".tmp")] == []


def test_recording_the_same_state_twice_does_not_rewrite_the_file(project):
    assert _note(project)
    path = project / ".venv" / bootstrap.READY_FINGERPRINT
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    assert _note(project)
    assert path.stat().st_mtime_ns == 1_000_000_000


def test_only_the_projects_own_environment_records_or_forgets(project, tmp_path):
    """用户自己装齐依赖的 Python 跑起来，说明不了 .venv 的事；第一跳（启动器选的 Python）
    import 不了是常态，也不能因此删指纹。"""
    other = tmp_path / "somebody-elses-python"
    assert bootstrap.note_ready(project, IMPORTS, prefix=other) is False
    assert not (project / ".venv" / bootstrap.READY_FINGERPRINT).exists()
    assert _note(project)
    assert bootstrap.forget_ready(project, prefix=other) is False
    assert bootstrap.ready_fast_path(project, IMPORTS) is True
    # .app 壳里的进程（sys.prefix 是 Contents）也算本项目的环境
    contents = project / bootstrap.APP_DIR_NAME / "Contents"
    contents.mkdir(parents=True)
    assert bootstrap.forget_ready(project, prefix=contents) is True
    assert bootstrap.ready_fast_path(project, IMPORTS) is False
    assert bootstrap.forget_ready(project, prefix=project / ".venv") is True   # 没有也算删成


def test_a_corrupt_fingerprint_is_not_a_match(project):
    (project / ".venv" / bootstrap.READY_FINGERPRINT).write_text("{not json", encoding="utf-8")
    assert bootstrap.ready_fast_path(project, IMPORTS) is False
    assert _note(project) is True                                     # 下一个就绪的进程改写它
    assert bootstrap.ready_fast_path(project, IMPORTS) is True


# ---- I9 哪些变化让指纹失效 ---------------------------------------------------------------------

def test_a_changed_requirements_list_invalidates_it(project):
    assert _note(project)
    with open(project / "requirements.txt", "a", encoding="utf-8") as fh:
        fh.write("yt-dlp\n")
    assert bootstrap.ready_fast_path(project, IMPORTS) is False


def test_a_new_install_stamp_invalidates_it(project):
    """一键更新装完新清单、写了新的 .requirements.sha256：最终进程要重新记一次。"""
    assert _note(project)
    (project / ".venv" / bootstrap.REQ_STAMP).write_text("0" * 64 + "\n", encoding="utf-8")
    fp_before = json.loads((project / ".venv" / bootstrap.READY_FINGERPRINT).read_text("utf-8"))
    assert bootstrap.ready_fingerprint(project, IMPORTS) != fp_before
    assert bootstrap.ready_fast_path(project, IMPORTS) is False


def test_a_rewritten_pyvenv_cfg_invalidates_it(project):
    """setup.sh 用别的次版本不带 --clear 重跑：cfg 改写了，bin/python 没换。"""
    assert _note(project)
    (project / ".venv" / "pyvenv.cfg").write_text("home = /opt/x/bin\nversion = 3.14.0\n",
                                                  encoding="utf-8")
    assert bootstrap.ready_fast_path(project, IMPORTS) is False


def test_a_replaced_interpreter_file_invalidates_it_even_with_the_same_size(project):
    """同一个路径、同样大小，文件被换过（修改时间变了）也算变了。"""
    assert _note(project)
    py = _venv_python(project)
    st = py.stat()
    os.utime(py, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert py.stat().st_size == st.st_size
    assert bootstrap.ready_fast_path(project, IMPORTS) is False


def test_a_resized_interpreter_invalidates_it(project):
    assert _note(project)
    py = _venv_python(project)
    st = py.stat()
    py.write_bytes(py.read_bytes() + b"x")
    os.utime(py, ns=(st.st_atime_ns, st.st_mtime_ns))                 # 修改时间改回去
    assert bootstrap.ready_fast_path(project, IMPORTS) is False


def test_different_modules_to_import_invalidate_it(project):
    """Apple 芯片上删掉 mlx-whisper 放弃记号：要 import 的多了 mlx_whisper，要重新检查。"""
    assert _note(project)
    assert bootstrap.ready_fast_path(project, IMPORTS + ", mlx_whisper") is False


def test_a_rebuild_marker_always_sends_the_first_hop_through_the_checks(project):
    assert _note(project)
    (project / ".venv" / bootstrap.NEEDS_REBUILD).write_text("{}", encoding="utf-8")
    assert bootstrap.ready_fast_path(project, IMPORTS) is False


def test_a_failed_list_is_retried_after_24_hours_even_with_a_fingerprint(project):
    """清单变了、pip 没装上、但程序照样能跑：最终进程照旧记指纹（24 小时内不为同一份清单重跑
    pip，和以前一样）；过了 24 小时第一跳必须照旧检查、重试——指纹不能把重试吃掉。"""
    with open(project / "requirements.txt", "a", encoding="utf-8") as fh:
        fh.write("yt-dlp\n")
    now = 1_800_000_000.0
    bootstrap.record_requirements_failure(project, bootstrap.requirements_file_digest(project), 1,
                                          now=now)
    assert _note(project)
    assert bootstrap.ready_fast_path(project, IMPORTS, now=now + 3600) is True
    assert bootstrap.ready_fast_path(project, IMPORTS, now=now + bootstrap.REQ_RETRY_SEC + 1) is False


@posix_only
def test_conda_upgrading_python_in_place_invalidates_it(tmp_path):
    """计划审查 Q8，2026-10-20 实测过的那种：.venv/bin/python → python3 → 发行版的 python3，
    发行版原地把 python3 换成新次版本。pyvenv.cfg 一个字没变，指纹也得失效。"""
    base = tmp_path / "anaconda3" / "bin"
    base.mkdir(parents=True)
    (base / "python3.13").write_bytes(b"python 3.13")
    (base / "python3").symlink_to("python3.13")
    root = tmp_path / "project"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python3").symlink_to(base / "python3")
    (root / ".venv" / "bin" / "python").symlink_to("python3")
    (root / ".venv" / "pyvenv.cfg").write_text("home = {}\nversion = 3.13.5\n".format(base),
                                               encoding="utf-8")
    (root / "requirements.txt").write_text("aiohttp>=3.9\n", encoding="utf-8")
    bootstrap.write_requirements_stamp(root, bootstrap.requirements_file_digest(root))
    assert _note(root)
    assert bootstrap.ready_fingerprint(root, IMPORTS)["python"]["path"] == str(
        (base / "python3.13").resolve())
    assert bootstrap.ready_fast_path(root, IMPORTS) is True
    cfg = (root / ".venv" / "pyvenv.cfg").read_bytes()

    (base / "python3.14").write_bytes(b"python 3.14")               # conda update python
    (base / "python3").unlink()
    (base / "python3").symlink_to("python3.14")
    assert (root / ".venv" / "pyvenv.cfg").read_bytes() == cfg
    assert bootstrap.ready_fast_path(root, IMPORTS) is False

    (base / "python3").unlink()                                       # Python 被卸掉：链接断了
    (base / "python3.14").unlink()
    assert bootstrap.ready_fingerprint(root, IMPORTS) is None
    assert bootstrap.ready_fast_path(root, IMPORTS) is False


# ---- I7 推迟安装 -------------------------------------------------------------------------------

PLAN = {"text": "x", "delay": 5.0}


def _defer(plan, root, probe, imports_ok=True, venv_created=False):
    return bootstrap.defer_install(plan, root, probe, imports_ok=imports_ok,
                                   venv_created=venv_created)


def test_nothing_to_install_never_probes_the_port(project):
    probes = []
    assert _defer(None, project, lambda: probes.append(1) or True) is False
    assert probes == []


def test_no_venv_yet_never_probes_the_port(tmp_path):
    probes = []
    assert _defer(PLAN, tmp_path, lambda: probes.append(1) or True) is False
    assert probes == []


@pytest.mark.parametrize("running,expect", [(True, True), ("http://127.0.0.1:8766", True),
                                            (None, False), (False, False)])
def test_an_install_waits_while_the_app_answers_on_its_port(project, running, expect):
    assert _defer(PLAN, project, lambda: running) is expect


def test_a_failing_probe_installs_as_before(project):
    def boom():
        raise OSError("probe failed")
    assert _defer(PLAN, project, boom) is False


def test_a_venv_created_in_this_launch_is_never_deferred(project):
    """审查意见：新解压的一份第一次打开，旧的那份还在 8765 上直播。这一轮刚 venv.create 出来的
    空 .venv 不可能有人在用；推迟了，最终进程缺组件起不来，旧实例退出之前这份一直装不上。照旧装，
    端口也不探。"""
    probes = []
    assert (project / ".venv").is_dir()
    assert _defer(PLAN, project, lambda: probes.append(1) or True, venv_created=True) is False
    assert probes == []


def test_an_environment_that_cannot_import_its_components_is_never_deferred(project):
    """核心组件 import 不齐：不装就起不来，推迟只会让最终进程撞上 ImportError。只有「现有环境能跑、
    只是清单变了」才推迟。"""
    probes = []
    plan = {"text": "x", "delay": 0.0}           # plan_install 对 import 不齐给的就是这种（不等）
    assert _defer(plan, project, lambda: probes.append(1) or True, imports_ok=False) is False
    assert _defer(plan, project, lambda: probes.append(1) or True, imports_ok=False,
                  venv_created=True) is False
    assert probes == []


def test_the_caller_must_say_whether_the_venv_can_run(project):
    """imports_ok、venv_created 都没有默认值：漏传就报错，不会让刚建的空环境默认落进推迟。"""
    with pytest.raises(TypeError):
        bootstrap.defer_install(PLAN, project, lambda: True)
    with pytest.raises(TypeError):
        bootstrap.defer_install(PLAN, project, lambda: True, imports_ok=True)


def test_the_next_process_recognises_the_deferral_by_its_own_pid(project):
    """POSIX：exec 换进 .venv/bin/python 或 .app 壳都不换 PID，换进去的就是记下 PID 的那个进程。"""
    env = {}
    bootstrap.mark_install_deferred(env, pid=4242)
    assert env == {bootstrap.DEFER_ENV: "4242"}
    venv = project / ".venv"
    assert bootstrap.install_deferred(project, env, pid=4242, prefix=venv) is True
    # .app 壳里的进程同样认
    contents = project / bootstrap.APP_DIR_NAME / "Contents"
    assert bootstrap.install_deferred(project, env, pid=4242, prefix=contents) is True


def test_on_windows_the_final_process_does_not_recognise_it(project):
    """Windows 上真实的进程关系：第一跳（PID 4242）起 .venv\\Scripts\\python.exe 等着——那是标准库 venv
    复制过来的 venvlauncher 重定向器（PID 5000），它再起基础 python.exe（PID 6000）跑 main.py。
    最终进程的 PID、父 PID 都不是 4242，认不出来；也不去认父 PID（以前那条 ppid 判据在这里永远
    对不上）。Windows 的推迟靠最终进程自己再判一次，见下面 main.py 接线的测试。"""
    env = {bootstrap.DEFER_ENV: "4242"}
    venv = project / ".venv"
    assert bootstrap.install_deferred(project, env, pid=6000, prefix=venv) is False
    assert bootstrap.install_deferred(project, env, pid=5000, prefix=venv) is False


def test_a_leftover_or_foreign_deferral_is_ignored(project, tmp_path):
    venv = project / ".venv"
    stale = {bootstrap.DEFER_ENV: "4242"}
    # 终端里残留的环境变量：PID 对不上，照常检查、照常装
    assert bootstrap.install_deferred(project, stale, pid=7, prefix=venv) is False
    # 不在本项目的环境里（第一跳）不认
    assert bootstrap.install_deferred(project, stale, pid=4242, prefix=tmp_path / "other") is False
    for value in ("", "  "):
        assert bootstrap.install_deferred(project, {bootstrap.DEFER_ENV: value}, pid=4242,
                                          prefix=venv) is False
    assert bootstrap.install_deferred(project, {}, pid=4242, prefix=venv) is False


def test_the_final_process_clears_the_deferral(project):
    env = {bootstrap.DEFER_ENV: "4242", "OTHER": "1"}
    bootstrap.clear_install_deferred(env)
    assert env == {"OTHER": "1"}
    bootstrap.clear_install_deferred(env)                             # 没有也不报错
    assert env == {"OTHER": "1"}


# ---- main.py 的接线（读语法树，不 import） ----------------------------------------------------

def _tree():
    return ast.parse((REPO / "main.py").read_text(encoding="utf-8"))


def _func(tree, name):
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _calls(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)]


def _line(node, name):
    found = _calls(node, name)
    assert found, name
    return min(c.lineno for c in found)


def _enclosing_ifs(func, target):
    """target 外面套着的每一层 if，连同它在哪一支（body / orelse）：[(if 节点, "body"|"orelse"), …]。"""
    def walk(node, chain):
        if node is target:
            return chain
        for field, value in ast.iter_fields(node):
            items = value if isinstance(value, list) else [value]
            for child in items:
                if not isinstance(child, ast.AST):
                    continue
                if isinstance(node, ast.If) and field in ("body", "orelse"):
                    found = walk(child, chain + [(node, field)])
                else:
                    found = walk(child, chain)
                if found is not None:
                    return found
        return None
    found = walk(func, [])
    assert found is not None
    return found


def test_ensure_env_checks_the_deferral_first_and_records_readiness_after_deps_ok():
    ensure = _func(_tree(), "ensure_env")
    first = ensure.body[1]                     # body[0] 是文档字符串
    assert isinstance(first, ast.If) and _calls(first.test, "_install_deferred")
    assert [type(s) for s in first.body] == [ast.Return]
    second = ensure.body[2]
    assert isinstance(second, ast.If) and _calls(second.test, "_deps_ok")
    assert [ast.unparse(s) for s in second.body] == ["_note_ready()", "return"]
    # 本项目环境里 import 不齐：指纹作废（第一跳上 _forget_ready 什么都不做）
    assert ast.unparse(ensure.body[3]) == "_forget_ready()"


def test_the_first_hop_skips_the_import_check_only_on_a_matching_fingerprint():
    src = (REPO / "main.py").read_text(encoding="utf-8")
    ensure = _func(ast.parse(src), "ensure_env")
    (fast,) = _calls(ensure, "ready_fast_path")
    assigned = next(n for n in ast.walk(ensure) if isinstance(n, ast.Assign) and n.value
                    and fast in ast.walk(n.value))
    assert ast.unparse(assigned) == (
        "ready = usable and bootstrap.ready_fast_path(ROOT, _ready_import(), python=vpy)")
    # 解释器照旧实际跑一下（interpreter_version 在指纹之前）
    assert _line(ensure, "interpreter_version") < fast.lineno
    # venv_needs_rebuild（含 pip 探测）、子进程 import 检查和 plan_install 都在 `if not ready:` 里
    guards = [n for n in ast.walk(ensure) if isinstance(n, ast.If)
              and ast.unparse(n.test) == "not ready"]
    inside = [c for g in guards for c in ast.walk(g)]
    for name in ("venv_needs_rebuild", "plan_install"):
        assert all(c in inside for c in _calls(ensure, name)), name
    runs = [c for c in _calls(ensure, "run") if ast.unparse(c.func) == "subprocess.run"]
    assert runs and all(c in inside for c in runs)


def test_the_install_is_deferred_before_pip_runs_and_the_flag_rides_into_the_venv():
    ensure = _func(_tree(), "ensure_env")
    (defer,) = _calls(ensure, "defer_install")
    assert ast.unparse(defer) == (
        "bootstrap.defer_install(plan, ROOT, lambda: _existing_instance_url(8765), "
        "imports_ok=imports_ok, venv_created=venv_created)")
    assert _line(ensure, "plan_install") < defer.lineno < _line(ensure, "install_requirements")
    defer_if = next(n for n in ast.walk(ensure) if isinstance(n, ast.If) and n.test is defer)
    body = [ast.unparse(s) for s in defer_if.body]
    assert body[-2:] == ["plan = None", "deferred = True"]
    assert "[初始化]" in body[0] and "print(" in body[0]
    # 推迟了就记下 PID，再换进 .venv
    (mark,) = _calls(ensure, "mark_install_deferred")
    assert mark.lineno < _line(ensure, "relaunch_inside_bundle") < _line(ensure, "execv")


def test_a_fresh_venv_and_a_failed_import_check_reach_the_deferral_decision():
    """审查意见：判断推迟在 venv.create(clear=True) 之后，刚建的空 .venv 被当成「有人在用」。现在
    venv.create 紧跟着记下 venv_created，子进程 import 检查的结果原样交给 plan_install 和 defer_install。"""
    ensure = _func(_tree(), "ensure_env")
    assigns = {ast.unparse(n) for n in ast.walk(ensure) if isinstance(n, ast.Assign)}
    # 两个都先有「不能推迟」以外的初值：没走到建环境、没跑检查时也有定义
    assert {"venv_created = False", "imports_ok = False"} <= assigns
    (create,) = [c for c in _calls(ensure, "create") if ast.unparse(c.func) == "venv.create"]
    block = next(n for n in ast.walk(ensure) if isinstance(n, ast.If)
                 and any(isinstance(s, ast.Expr) and s.value is create for s in n.body))
    texts = [ast.unparse(s) for s in block.body]
    at = next(i for i, t in enumerate(texts) if t.startswith("venv.create("))
    assert "venv_created = True" in texts[at + 1:]
    # 检查的退出码 → imports_ok → plan_install 和 defer_install 用同一个
    assert "imports_ok = check.returncode == 0" in assigns
    (plan,) = _calls(ensure, "plan_install")
    assert ast.unparse(plan.args[0]) == "imports_ok"


def test_windows_relies_on_the_final_process_running_the_deferral_again():
    """Windows 上最终进程认不出推迟（中间隔着 venv 的重定向器，PID 对不上），靠它自己再判一次：
    它跑在 .venv 里（in_project_venv 为真），_deps_ok 因为清单待装不过，走到 defer_install、照样不装。
    所以 defer_install 不能挪进 `if not in_project_venv` 或 `if not ready`；ready 只有第一跳会设成真；
    最终进程拿自己的解释器跑 import 检查；推迟之后它不再 exec，直接往下走、认出已有实例。"""
    ensure = _func(_tree(), "ensure_env")
    (defer,) = _calls(ensure, "defer_install")
    for node, _branch in _enclosing_ifs(ensure, defer):
        test = ast.unparse(node.test)
        assert "in_project_venv" not in test and "ready" not in test, test
    (fast,) = _calls(ensure, "ready_fast_path")
    branches = [(ast.unparse(n.test), b) for n, b in _enclosing_ifs(ensure, fast)]
    assert ("in_project_venv", "orelse") in branches
    assigns = {ast.unparse(n) for n in ast.walk(ensure) if isinstance(n, ast.Assign)}
    assert "pip_python = sys.executable if in_project_venv else str(vpy)" in assigns
    for name in ("execv", "replace_process", "mark_install_deferred"):
        for call in _calls(ensure, name):
            tests = [ast.unparse(n.test) for n, b in _enclosing_ifs(ensure, call) if b == "body"]
            assert "not in_project_venv" in tests, name


def test_main_clears_the_deferral_in_the_final_process():
    main = _func(_tree(), "main")
    assert (_line(main, "relaunch_inside_bundle") < _line(main, "clear_install_deferred")
            < _line(main, "settle"))


def test_an_import_failure_in_the_final_process_also_forgets_the_fingerprint():
    tree = _tree()
    forget = _func(tree, "_forget_install_failure")
    assert _calls(forget, "forget_requirements_failure") and _calls(forget, "_forget_ready")
    # _note_ready 只在第一跳要 import 的模块也都 import 成功时记（imageio_ffmpeg 可以靠系统 ffmpeg 过 _deps_ok）
    note = _func(tree, "_note_ready")
    assert "sys.modules" in ast.unparse(note) and _calls(note, "_ready_modules")
    assert _calls(note, "note_ready")
