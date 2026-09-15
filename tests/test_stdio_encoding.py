"""Windows 上重定向 stdout 的编码护栏（app/stdio.py）。

2026-09-15 真实 Windows 11（中文、ANSI 代码页 936）实测：
`main.py --doctor 2>&1 | Out-File doctor.log` 崩在体检报告的第一个 ✅ 上、exit 1、
报告一行都没写出来。管道/重定向的 stdout 在 Windows 上是「ANSI 代码页 + strict」，
交互控制台走 UTF-16 控制台 API 所以永远看不到——开发机、mac、CI 全都测不出来。

这些用例守三件事：
  1. 护栏确实把 strict 变成 replace，而**编码不变**（中控读的中文仍是 GBK 字节，
     换成 UTF-8 会让 PowerShell/cmd 捕获的日志整篇乱码）；
  2. 护栏自己永不抛、也不覆盖操作员显式设过的 errors；
  3. 每一个会打印给人看的进程入口都装上了护栏——尤其 main.py 要在模块级、
     在任何可能打印的语句之前调用（execv 进 .venv、一键更新重启都会再跑一遍），
     而弹幕/WebKit 子进程反过来必须固定 UTF-8（父进程按 UTF-8 逐行 json.loads）。

不 import main（它在模块级就跑 ensure_env、会把 pytest 进程 execv 掉），所以
main.py 只做源码级检查。
"""
import ast
import asyncio
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.stdio import harden_stdio, utf8_stdio

ROOT = Path(__file__).resolve().parent.parent
# 一行里三类真实内容：doctor 的状态图标、西语 ASR 文本、中控读的中文文案。
# 前两类 GBK 编不了，第三类能——护栏不该为了前两类牺牲第三类。
LINE = "✅ ñ 中文"


def _gbk_strict():
    """复现 Windows 上重定向后的 sys.stdout：代码页编码 + errors='strict'。"""
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict", newline="")


def _written(stream):
    stream.flush()
    return stream.buffer.getvalue()


# ---- 1. 护栏本身 ----------------------------------------------------------------------

def test_a_gbk_strict_stream_raises_before_the_guard_and_not_after():
    before = _gbk_strict()
    with pytest.raises(UnicodeEncodeError):
        print(LINE, file=before)

    after = _gbk_strict()
    assert harden_stdio([after]) == [after]
    print(LINE, file=after)                      # 不抛就是修好了
    raw = _written(after)
    assert "中文".encode("gbk") in raw           # 中文仍按代码页落字节
    assert "中文".encode("utf-8") not in raw     # 没有偷偷改成 UTF-8（否则捕获日志全乱码）
    assert raw.endswith(b"\n")
    assert (after.encoding, after.errors) == ("gbk", "replace")


@pytest.mark.parametrize("text", [
    "[警报] 疑似违禁词「niño」（fuzzy）：¿Qué tal? 🔴",   # pipeline._emit_original，报警链路上
    "[健康] 🔴 识别严重积压",                             # _announce_health
    "[自检] ✅ ⚠️ ❌",                                    # run_selfcheck 的状态图标
    "[错误] yt-dlp: ���",                  # 子进程字节 errors='replace' 解出来的 U+FFFD
    "词表里这条写法应当放进 profiles/pestañas.txt",       # _begin_session 的词表警告
])
def test_real_runtime_lines_do_not_raise_after_the_guard(text):
    """崩的不只是 emoji：西语 ñ ¿ ¡、以及子进程输出解码出的 U+FFFD，GBK 都编不了。"""
    stream = _gbk_strict()
    with pytest.raises(UnicodeEncodeError):
        print(text, file=stream)
    guarded = _gbk_strict()
    harden_stdio([guarded])
    print(text, file=guarded)
    assert _written(guarded)


def test_the_guard_skips_streams_it_must_not_touch():
    assert harden_stdio([None]) == []                    # pythonw：sys.stdout 是 None
    assert harden_stdio([io.StringIO()]) == []           # 没有 reconfigure：IDLE、pytest 捕获对象

    chosen = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="backslashreplace")
    assert harden_stdio([chosen]) == []                  # 操作员/别的护栏定过：不覆盖
    assert chosen.errors == "backslashreplace"

    class Cranky:
        errors = "strict"

        def reconfigure(self, **kwargs):
            raise ValueError("不支持")

    assert harden_stdio([Cranky()]) == []                # 护栏自己永远不抛


def test_the_guard_keeps_an_operator_chosen_encoding():
    """PYTHONUTF8=1 / PYTHONIOENCODING=utf-8 的编码要保住，只换 errors。"""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    assert harden_stdio([stream]) == [stream]
    assert (stream.encoding, stream.errors) == ("utf-8", "replace")


def test_the_guard_is_idempotent():
    stream = _gbk_strict()
    assert harden_stdio([stream]) == [stream]
    assert harden_stdio([stream]) == []


def test_default_streams_are_stdout_and_stderr(monkeypatch):
    out, err = _gbk_strict(), _gbk_strict()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert harden_stdio() == [out, err]


# ---- 2. 崩过的那份报告 ----------------------------------------------------------------

def test_doctor_survives_a_gbk_strict_stdout(monkeypatch):
    """--doctor 的报告是 setup.ps1 第 [5/5] 步的产物，也是排障第一手材料。"""
    from app import hwdetect
    from app import translator

    monkeypatch.setattr(translator, "_ollama_models_or_none", lambda: None)   # 不碰网络

    crashy = _gbk_strict()
    monkeypatch.setattr(sys, "stdout", crashy)
    with pytest.raises(UnicodeEncodeError):
        hwdetect.doctor()

    guarded = _gbk_strict()
    monkeypatch.setattr(sys, "stdout", guarded)
    harden_stdio([guarded])
    hwdetect.doctor()
    raw = _written(guarded)
    assert "环境体检".encode("gbk") in raw
    assert "推荐配置".encode("gbk") in raw       # 崩点之后的内容也写全了


def test_doctor_in_a_real_gbk_strict_pipe_exits_zero_with_the_guard():
    """VM 上实测的那条命令（PYTHONIOENCODING=gbk:strict + 管道），逐字复现。"""
    body = "import sys; sys.path.insert(0, {!r}); ".format(str(ROOT))
    env = dict(os.environ, PYTHONIOENCODING="gbk:strict", PYTHONUTF8="0")

    crash = subprocess.run([sys.executable, "-c", body + "from app import hwdetect; hwdetect.doctor()"],
                           capture_output=True, env=env, timeout=120)
    assert crash.returncode != 0
    assert b"UnicodeEncodeError" in crash.stderr

    guarded = subprocess.run(
        [sys.executable, "-c", body + "from app.stdio import harden_stdio; harden_stdio(); "
                                      "from app import hwdetect; hwdetect.doctor()"],
        capture_output=True, env=env, timeout=120)
    assert guarded.returncode == 0, guarded.stderr
    assert b"UnicodeEncodeError" not in guarded.stderr
    assert "推荐配置".encode("gbk") in guarded.stdout


# ---- 3. 每个进程入口都装上了 ----------------------------------------------------------

def _tree(path):
    src = path.read_text(encoding="utf-8")
    return src, ast.parse(src)


def _calls(node):
    """node 子树里被调用的名字（属性调用只取最后一段：sys.exit -> exit）。"""
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _index_of_call(body, name):
    for i, node in enumerate(body):
        if name in _calls(node):
            return i
    return None


def _is_docstring(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
        and isinstance(node.value.value, str)


# 这些在模块级一跑就可能写标准流：护栏必须排在它们前面
CAN_PRINT = {"print", "exit", "ensure_env", "_fail_alert", "_first_run_dialog", "_info_dialog"}


def test_main_calls_the_guard_before_anything_can_print():
    """入口进程（启动器的 Python、execv 进 .venv 之后、一键更新重启之后、窗口模式的
    同进程后台线程）全靠 main.py 这一句：它必须在模块级、在第一次可能打印之前。"""
    src, tree = _tree(ROOT / "main.py")
    body = tree.body
    guard = _index_of_call(body, "harden_stdio")
    assert guard is not None, "main.py 必须在模块级调用 harden_stdio()"
    assert "from app.stdio import harden_stdio" in src

    for node in body[:guard]:
        assert isinstance(node, (ast.Import, ast.ImportFrom)) or _is_docstring(node), \
            "护栏之前只允许 docstring 和 import，实际是 " + ast.dump(node)[:120]

    runnable = [(i, n) for i, n in enumerate(body)
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    noisy = [i for i, n in runnable if _calls(n) & CAN_PRINT]
    assert noisy, "main.py 模块级不再有可能打印的语句？用例的假设过期了"
    assert guard < min(noisy)

    ensure = _index_of_call(body, "ensure_env")
    assert ensure is not None and guard < ensure, "护栏必须早于 ensure_env（安装日志也会打印）"


def test_every_relaunch_re_enters_main_py():
    """execv/execve 是新进程：只继承句柄和环境，护栏要靠 main.py 顶上那句再跑一遍。
    （别改成靠导出 PYTHONIOENCODING——那会顺带改掉子进程和用户自己设的值。）"""
    seen = []
    for rel in ("main.py", "app/updater.py", "app/macbundle.py"):
        src, tree = _tree(ROOT / rel)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not ({"execv", "execve"} & _calls(node)):
                continue
            segment = ast.get_source_segment(src, node) or ""
            assert "main.py" in segment, (rel, node.name, node.lineno)
            seen.append((rel, node.name))
    assert len(seen) >= 3, "应当覆盖首装 execv、一键更新重启、mac bundle 重启：" + repr(seen)


def test_comment_worker_keeps_utf8_instead_of_the_code_page():
    """弹幕 worker 的 stdout 是给父进程 json.loads 的字节流，不是给人读的终端：
    这里必须固定 UTF-8，套上 main.py 那种「保留代码页」护栏会把西语弹幕重新弄丢。"""
    src, tree = _tree(ROOT / "app" / "comment_worker.py")
    assert "harden_stdio" not in _calls(tree)

    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert "_utf8_stdio" in _calls(func.body[0]), "main() 第一句就要装护栏"

    stream = _gbk_strict()
    assert utf8_stdio([stream]) == [stream]
    print("niño 🎉", file=stream)
    assert _written(stream).decode("utf-8").strip() == "niño 🎉"


def test_webkit_fetch_also_fixes_utf8_before_it_emits():
    """今天只在 macOS 上被调起，父进程同样按 UTF-8 解它那一行 JSON。"""
    src, tree = _tree(ROOT / "app" / "webkit_fetch.py")
    assert "harden_stdio" not in _calls(tree)
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert "utf8_stdio" in _calls(func.body[0]), "main() 第一句就要装护栏"


def test_every_tool_script_hardens_stdio_before_it_prints():
    """tools/ 只由人跑，但 `python3 tools/x.py > out.txt` 在 Windows 上是同一个坑。"""
    missing = []
    for path in sorted((ROOT / "tools").glob("*.py")):
        if path.name == "_benchdata.py":
            continue                                    # 纯库，没有输出
        src, tree = _tree(path)
        guard = _index_of_call(tree.body, "harden_stdio")
        if guard is None:
            missing.append(path.name)
            continue
        runnable = [(i, n) for i, n in enumerate(tree.body)
                    if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        noisy = [i for i, n in runnable if _calls(n) & CAN_PRINT]
        assert not noisy or guard < min(noisy), path.name
    assert not missing, "这些工具还会在重定向时崩：" + ", ".join(missing)


# 这两个子进程是我们自己的代码，出口自带 UTF-8 护栏，不需要父进程再设环境变量
_CHILD_FIXES_ITS_OWN_STDIO = ("app.comment_worker", "app.webkit_fetch")


def test_python_children_we_decode_as_utf8_are_told_to_write_utf8():
    """第三方子进程（yt-dlp、pip）不是我们的代码：Windows 上它们按 ANSI 代码页写，
    我们却按 UTF-8 解，报错原话会先烂成一串 U+FFFD——而 U+FFFD 自己也是 GBK
    编不出来的字符，等于把别人的错误信息变成我们的第二个编码坑。"""
    checked = []
    for path in sorted((ROOT / "app").glob("*.py")):
        _, tree = _tree(path)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "create_subprocess_exec" not in _calls(func):
                continue
            # 只看 AST，不看源码文本：注释里提一句 PYTHONIOENCODING 不算数
            attrs = {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
            texts = {n.value for n in ast.walk(func)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            texts |= {n.arg for n in ast.walk(func)          # dict(os.environ, PYTHONIOENCODING=…)
                      if isinstance(n, ast.keyword) and n.arg}
            if "PIPE" not in attrs or "executable" not in attrs:
                continue                          # 输出没进管道，或不是 Python 子进程（git/ffmpeg）
            if any(mod in texts for mod in _CHILD_FIXES_ITS_OWN_STDIO):
                continue                          # 我们自己的 worker，出口自带 UTF-8 护栏
            assert "PYTHONIOENCODING" in texts, (path.name, func.name, func.lineno)
            checked.append((path.name, func.name))
    assert len(checked) >= 3, "至少要盖住 yt-dlp 解析和两处 pip：" + repr(checked)


def test_ytdlp_child_is_spawned_with_utf8_output(monkeypatch):
    from app import resolver

    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"https://pull.example.com/a.flv\n", b""

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    code, out, err = asyncio.run(resolver._run_ytdlp("https://www.tiktok.com/@a/live"))

    assert (code, out.strip()) == (0, "https://pull.example.com/a.flv")
    assert (captured["env"] or {}).get("PYTHONIOENCODING") == "utf-8"
    assert captured["env"].get("PATH") == os.environ.get("PATH")   # 只加一项，不替掉整个环境
