"""app/fdlimit.py：启动时把文件句柄的软上限提到 min(硬上限, 8192)（专家审查 S1）。

背景：从 Finder 启动的 .app 继承 launchd 的软上限 256；局域网里一台设备对手机同看端口挂
150 条空连接，审查用程序自己的解释器实测 open/spawn/socket 全部 ERR24。连接数的上限在
app/viewer.py（tests/test_viewer_hub.py 钉着），这里钉「上限本身抬上去了」以及它的几条约定：
只抬不降、硬上限是天花板、没有 resource 模块就跳过、任何错误都只放进返回值。

不 import main（CLAUDE.md：import 就 execv）：main.py 的接线读语法树。
"""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import fdlimit

REPO = Path(__file__).resolve().parent.parent
INF_MAC = 2 ** 63 - 1        # macOS 的 RLIM_INFINITY
INF_LINUX = -1               # Linux 的 Python 读出来是 -1


class FakeResource:
    """resource 模块的替身：记下 setrlimit 的每一次调用。"""

    RLIMIT_NOFILE = 8

    def __init__(self, soft, hard, infinity=INF_MAC, set_error=None, get_error=None):
        self.RLIM_INFINITY = infinity
        self.limits = (soft, hard)
        self.calls = []
        self._set_error = set_error
        self._get_error = get_error

    def getrlimit(self, which):
        assert which == self.RLIMIT_NOFILE
        if self._get_error is not None:
            raise self._get_error
        return self.limits

    def setrlimit(self, which, value):
        assert which == self.RLIMIT_NOFILE
        self.calls.append(value)
        if self._set_error is not None:
            raise self._set_error
        self.limits = tuple(value)


@pytest.mark.parametrize("infinity", [INF_MAC, INF_LINUX])
def test_the_launchd_default_is_raised_to_8192_when_the_hard_limit_is_infinite(infinity):
    """本机的实际组合：launchctl limit maxfiles 是 256 / unlimited。"""
    fake = FakeResource(256, infinity, infinity=infinity)
    result = fdlimit.raise_nofile_limit(resource_mod=fake)
    assert fake.calls == [(8192, infinity)]          # 硬上限原样带回去，不碰它
    assert result.status == "raised"
    assert (result.soft_before, result.soft_after, result.target) == (256, 8192, 8192)
    assert result.error is None


def test_a_finite_hard_limit_is_the_ceiling():
    fake = FakeResource(256, 1024)
    result = fdlimit.raise_nofile_limit(resource_mod=fake)
    assert fake.calls == [(1024, 1024)]
    assert (result.status, result.soft_after, result.target) == ("raised", 1024, 1024)


def test_a_hard_limit_above_the_target_still_stops_at_8192():
    fake = FakeResource(1024, 65536)
    result = fdlimit.raise_nofile_limit(resource_mod=fake)
    assert fake.calls == [(8192, 65536)]
    assert result.status == "raised"


@pytest.mark.parametrize("soft,hard", [
    (8192, INF_MAC),           # 已经正好
    (1048576, INF_MAC),        # 终端里 ulimit -n 很大：只抬不降
    (65536, 65536),
    (256, 256),                # 硬上限就这么低：没有可做的，不算失败
    (INF_MAC, INF_MAC),        # 软上限本身就是无穷大
])
def test_a_soft_limit_that_is_already_high_enough_is_left_alone(soft, hard):
    fake = FakeResource(soft, hard)
    result = fdlimit.raise_nofile_limit(resource_mod=fake)
    assert fake.calls == []
    assert result.status == "unchanged"
    assert result.soft_after == soft
    assert result.error is None


def test_a_refused_setrlimit_is_reported_not_raised():
    """macOS 上软上限高过 kern.maxfilesperproc 时 setrlimit 报 EINVAL（Python 里是 ValueError）。"""
    fake = FakeResource(256, INF_MAC, set_error=ValueError("not allowed to raise maximum limit"))
    result = fdlimit.raise_nofile_limit(resource_mod=fake)
    assert result.status == "failed"
    assert result.soft_before == 256 and result.soft_after == 256 and result.target == 8192
    assert "ValueError" in result.error and "not allowed" in result.error


def test_an_oserror_from_getrlimit_is_reported_not_raised():
    fake = FakeResource(256, INF_MAC, get_error=OSError(1, "Operation not permitted"))
    result = fdlimit.raise_nofile_limit(resource_mod=fake)
    assert result.status == "failed"
    assert result.soft_before is None
    assert "Operation not permitted" in result.error
    assert fake.calls == []


def test_no_resource_module_means_skip(monkeypatch):
    """Windows 没有 resource 模块，也没有这个上限：跳过，不报错。"""
    monkeypatch.setitem(sys.modules, "resource", None)     # 让 import resource 抛 ImportError
    result = fdlimit.raise_nofile_limit()
    assert result.status == "unsupported"
    assert result.error is None


def test_the_module_has_no_chinese_string_literals():
    """约定（修复计划 cross_cutting 6）：fdlimit 不放中文字面量，中文由调用方打印。
    文档字符串和注释不算——G4 也不扫它们。"""
    tree = ast.parse((REPO / "app" / "fdlimit.py").read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            assert not any("一" <= ch <= "鿿" for ch in node.value), node.value


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 没有 RLIMIT_NOFILE")
def test_the_real_limit_is_raised_in_a_process_that_starts_at_256():
    """真调一次，但在子进程里：不改 pytest 进程自己的上限。子进程先把软上限压到 256
    （本机从 Finder 启动的 .app 拿到的就是这个数），再跑 raise_nofile_limit。"""
    code = (
        "import json, resource, sys\n"
        "soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (min(256, soft), hard))\n"
        "from app.fdlimit import raise_nofile_limit\n"
        "r = raise_nofile_limit()\n"
        "print(json.dumps({'status': r.status, 'after': r.soft_after, 'target': r.target,\n"
        "                  'now': resource.getrlimit(resource.RLIMIT_NOFILE)[0]}))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["status"] in ("raised", "unchanged"), got
    assert got["now"] == got["after"] == got["target"]
    assert got["target"] <= fdlimit.TARGET


# ---- main.py 的接线（读语法树）----
def _top_level_call_line(tree, name):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == name):
                return node.lineno
    return None


def test_main_raises_the_limit_right_after_hardening_stdio_and_before_anything_execs():
    """要赶在任何东西开始占句柄之前，又要在 harden_stdio 之后（失败时要打一行警告）。"""
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    harden = _top_level_call_line(tree, "harden_stdio")
    raise_ = _top_level_call_line(tree, "raise_nofile_limit")
    ensure = _top_level_call_line(tree, "ensure_env")
    assert None not in (harden, raise_, ensure)
    assert harden < raise_ < ensure


def test_main_prints_a_chinese_warning_only_when_raising_failed():
    """失败时由 main.py 打一行中文警告（终端永远是中文）；只看 status == "failed"。"""
    source = (REPO / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    guards = [node for node in tree.body if isinstance(node, ast.If)
              and "failed" in ast.get_source_segment(source, node.test)
              and "_NOFILE" in ast.get_source_segment(source, node.test)]
    assert len(guards) == 1
    body = ast.get_source_segment(source, guards[0])
    assert "print(" in body and "[警告]" in body
