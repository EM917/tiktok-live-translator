"""tools/find_python.sh：有 .venv 时，启动器先用建它的那个解释器。

2026-10-19 Homebrew 升级 yt-dlp 时顺带装上 python@3.15，/opt/homebrew/bin/python3 从 3.14
变成 3.15；启动器按安装位置挑解释器，程序改由 python3.15 启动。系统的「完全磁盘访问权限」
按启动用的解释器路径登记（app/browser_login.fda_targets），python3.15 没有，Safari 的登录态
就读不到了。.venv 本身是 /opt/anaconda3/bin/python3.13 建的，一点没变。

钉住：没有 .venv（新装机）走原来的顺序；pyvenv.cfg 的 executable 优先，再到 home 下的
python<主.次>、python3；解释器不在了、次版本变了、低于 3.9、cfg 不记版本、相对路径、
指回项目自己（.venv / .app 里的链接）都落回原来的顺序；路径里有空格、home 是链接都照常；
/usr/bin 下的垫片不确认装过开发者工具绝不执行；两个启动器都把项目根传进来，并 exec 选中的
那个解释器。

用 /bin/bash 真跑（macOS 上就是系统自带的 3.2）；解释器全是假的 sh 脚本：打印一个版本号，
并把自己的路径和参数记进日志。find_installed_python 探测的是本机真实的安装位置，这里换成
只打印 FALLBACK 的假函数——测试不碰本机的任何 Python。"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FINDER = ROOT / "tools" / "find_python.sh"
APP_DIR = "TikTok Live Translator.app"
LAUNCHER = ROOT / APP_DIR / "Contents" / "MacOS" / "TikTokLiveTranslator"
START_COMMAND = ROOT / "Start.command"
BASH = "/bin/bash"

pytestmark = pytest.mark.skipif(sys.platform == "win32" or not os.path.exists(BASH),
                                reason="启动器是 macOS 的 bash 脚本")

FALLBACK = "FALLBACK"
# 假解释器的第一行：把 "$0 参数…" 记进日志，参数之间用 \037 隔开（-c 的代码里有空格）
_LOG_SELF = ('{ printf "%s" "$0"; for a in "$@"; do printf "\\037%s" "$a"; done; '
             'printf "\\n"; } >> "$TLT_PROBE_LOG"\n')


class Env:
    """一个临时世界：项目目录、项目外的「已安装的 Python」、记录每次执行的日志。"""

    def __init__(self, tmp_path, project="project"):
        self.tmp = tmp_path
        self.root = tmp_path / project
        self.root.mkdir(parents=True)
        self.bin = tmp_path / "fake-bin"
        self.bin.mkdir()
        self.log = tmp_path / "probes.log"
        self.devdir = None
        # 假的 xcode-select：默认什么都不打印（没装开发者工具）
        xs = self.bin / "xcode-select"
        xs.write_text('#!/bin/sh\n[ -n "$TLT_FAKE_DEVDIR" ] || exit 2\necho "$TLT_FAKE_DEVDIR"\n',
                      encoding="utf-8")
        xs.chmod(0o755)

    def python(self, path, version, ok=True):
        """假解释器：把 "$0 参数…" 记进日志，打印版本号，按 ok 决定退出码。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + _LOG_SELF + "echo %s\nexit %d\n" % (version, 0 if ok else 1),
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def venv(self, cfg_text, root=None):
        venv = (root or self.root) / ".venv"
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "pyvenv.cfg").write_bytes(cfg_text.encode("utf-8"))   # 原样写：要测 CRLF
        return venv

    def env(self):
        env = {"PATH": "{}:/usr/bin:/bin".format(self.bin), "HOME": str(self.tmp),
               "TLT_PROBE_LOG": str(self.log)}
        if self.devdir is not None:
            env["TLT_FAKE_DEVDIR"] = str(self.devdir)
        return env

    def find(self, *args, cwd=None):
        """source 真的 find_python.sh，把兜底换成假的，再调 find_python。返回 (退出码, 输出)。"""
        script = ('. "$1"; shift; find_installed_python() { echo ' + FALLBACK + '; }; '
                  'find_python "$@"')
        proc = subprocess.run([BASH, "-c", script, "bash", str(FINDER)] + [str(a) for a in args],
                              env=self.env(), cwd=str(cwd or self.tmp),
                              capture_output=True, timeout=60)
        assert proc.stderr == b"", proc.stderr.decode("utf-8", "replace")
        return proc.returncode, proc.stdout.decode("utf-8").rstrip("\n")

    def probed(self):
        """按顺序被执行过的假解释器路径。"""
        if not self.log.exists():
            return []
        return [line.split("\037")[0] for line in self.log.read_text(encoding="utf-8").splitlines()]


def _cfg(home=None, version="3.13.5", executable=None, key="version"):
    lines = []
    if home is not None:
        lines.append("home = {}".format(home))
    lines.append("include-system-site-packages = false")
    if version is not None:
        lines.append("{} = {}".format(key, version))
    if executable is not None:
        lines.append("executable = {}".format(executable))
    return "\n".join(lines) + "\n"


@pytest.fixture
def w(tmp_path):
    return Env(tmp_path)


# ---- 新装机：行为不变 ---------------------------------------------------------------------

def test_fresh_install_without_a_venv_uses_the_installed_python_order(w):
    assert w.find(w.root) == (0, FALLBACK)
    assert w.probed() == []


def test_a_venv_folder_without_pyvenv_cfg_counts_as_fresh(w):
    (w.root / ".venv" / "bin").mkdir(parents=True)
    assert w.find(w.root) == (0, FALLBACK)


def test_no_python_anywhere_still_fails(w):
    script = '. "$1"; find_installed_python() { return 1; }; find_python "$2"'
    proc = subprocess.run([BASH, "-c", script, "bash", str(FINDER), str(w.root)],
                          env=w.env(), capture_output=True, timeout=60)
    assert proc.returncode == 1 and proc.stdout == b""


# ---- 有 .venv：用建它的那个 ---------------------------------------------------------------

def test_the_venv_creator_wins_over_the_installed_order(w):
    """10-19 的情形：.venv 是 anaconda 的 3.13 建的，Homebrew 的 python3 已经是 3.15。"""
    conda = w.tmp / "anaconda3" / "bin"
    exe = w.python(conda / "python3.13", "3.13")
    w.python(conda / "python3", "3.13")
    w.venv(_cfg(home=conda, executable=exe))
    assert w.find(w.root) == (0, str(exe))
    assert w.probed() == [str(exe)]          # 只跑了它一个，兜底一次也没进


def test_the_probe_asks_for_the_version_and_the_39_floor(w):
    exe = w.python(w.tmp / "py" / "bin" / "python3.13", "3.13")
    w.venv(_cfg(home=exe.parent, executable=exe))
    w.find(w.root)
    args = w.log.read_text(encoding="utf-8").splitlines()[0].split("\037")[1:]
    assert args[0] == "-c"
    assert "sys.version_info[:2]" in args[1] and "(3, 9)" in args[1]


def test_missing_executable_falls_to_home_python_with_the_version(w):
    """Homebrew 的 executable 记的是 Cellar 里带补丁号的真实路径，小版本升级、清理之后就没了；
    home 下的 python3.14 还在。"""
    home = w.tmp / "homebrew" / "bin"
    py314 = w.python(home / "python3.14", "3.14")
    w.python(home / "python3", "3.15")
    w.venv(_cfg(home=home, version="3.14.7",
                executable=w.tmp / "homebrew" / "Cellar" / "python@3.14" / "3.14.7" / "bin" / "python3.14"))
    assert w.find(w.root) == (0, str(py314))


def test_home_python3_is_used_when_it_still_has_the_recorded_version(w):
    home = w.tmp / "py" / "bin"
    py3 = w.python(home / "python3", "3.13")
    w.venv(_cfg(home=home, executable=home / "gone"))
    assert w.find(w.root) == (0, str(py3))
    assert w.probed() == [str(py3)]


def test_home_only_cfg_from_an_older_venv(w):
    """3.9 / 3.10 的 venv 不写 executable，只有 home。"""
    home = w.tmp / "Library" / "Frameworks" / "Python.framework" / "Versions" / "3.10" / "bin"
    py310 = w.python(home / "python3.10", "3.10")
    w.venv(_cfg(home=home, version="3.10.11"))
    assert w.find(w.root) == (0, str(py310))


def test_virtualenv_version_info_key(w):
    exe = w.python(w.tmp / "py" / "bin" / "python3.12", "3.12")
    w.venv(_cfg(home=exe.parent, version="3.12.4.final.0", executable=exe, key="version_info"))
    assert w.find(w.root) == (0, str(exe))


def test_cfg_with_crlf_odd_spacing_and_no_final_newline(w):
    exe = w.python(w.tmp / "py" / "bin" / "python3.13", "3.13")
    w.venv("home=  {}/ \r\n  version   =3.13.5\t\r\nexecutable =   {}  \r".format(exe.parent, exe))
    assert w.find(w.root) == (0, str(exe))


def test_trailing_slash_on_home(w):
    home = w.tmp / "py" / "bin"
    py = w.python(home / "python3.13", "3.13")
    w.venv(_cfg(home=str(home) + "/"))
    assert w.find(w.root) == (0, str(py))


def test_spaces_in_every_path(tmp_path):
    w = Env(tmp_path, project="My Projects/tiktok live translator")
    home = tmp_path / "Application Support" / "py 3.13" / "bin"
    exe = w.python(home / "python3.13", "3.13")
    w.venv(_cfg(home=home, executable=exe))
    assert w.find(w.root) == (0, str(exe))


def test_symlinked_home_keeps_the_path_as_written(w):
    real = w.tmp / "opt" / "python@3.13" / "3.13.5" / "bin"
    w.python(real / "python3.13", "3.13")
    link = w.tmp / "opt" / "current"
    link.symlink_to(real, target_is_directory=True)
    w.venv(_cfg(home=link))
    assert w.find(w.root) == (0, str(link / "python3.13"))


def test_the_root_defaults_to_the_current_directory(w):
    exe = w.python(w.tmp / "py" / "bin" / "python3.13", "3.13")
    w.venv(_cfg(home=exe.parent, executable=exe))
    assert w.find(cwd=w.root) == (0, str(exe))


def test_extra_stdout_lines_before_the_version_are_tolerated(w):
    exe = w.tmp / "py" / "bin" / "python3.13"
    exe.parent.mkdir(parents=True)
    exe.write_text('#!/bin/sh\necho "site hook says hi"\necho 3.13\n', encoding="utf-8")
    exe.chmod(0o755)
    w.venv(_cfg(home=exe.parent, executable=exe))
    assert w.find(w.root) == (0, str(exe))


# ---- 落回原来的顺序 -----------------------------------------------------------------------

def test_creator_gone_falls_back(w):
    gone = w.tmp / "anaconda3" / "bin"
    w.venv(_cfg(home=gone, executable=gone / "python3.13"))
    assert w.find(w.root) == (0, FALLBACK)
    assert w.probed() == []


def test_minor_version_changed_under_the_same_path_falls_back(w):
    """建环境用的路径还在，但上面已经是 3.15 了：那不是建环境的解释器。"""
    home = w.tmp / "homebrew" / "bin"
    py3 = w.python(home / "python3", "3.15")
    w.venv(_cfg(home=home, version="3.14.7", executable=py3))
    assert w.find(w.root) == (0, FALLBACK)
    assert w.probed() == [str(py3), str(py3)]   # executable 一次、home/python3 一次


def test_python_older_than_39_falls_back(w):
    exe = w.python(w.tmp / "py" / "bin" / "python3.8", "3.8", ok=False)
    w.venv(_cfg(home=exe.parent, version="3.8.18", executable=exe))
    assert w.find(w.root) == (0, FALLBACK)


def test_interpreter_that_fails_to_run_falls_back(w):
    exe = w.python(w.tmp / "py" / "bin" / "python3.13", "3.13", ok=False)
    w.venv(_cfg(home=exe.parent, executable=exe))
    assert w.find(w.root) == (0, FALLBACK)


@pytest.mark.parametrize("version", [None, "", "unknown", "3", "3.x.1"], ids=repr)
def test_cfg_without_a_usable_version_is_not_trusted(w, version):
    exe = w.python(w.tmp / "py" / "bin" / "python3.13", "3.13")
    w.venv(_cfg(home=exe.parent, executable=exe, version=version))
    assert w.find(w.root) == (0, FALLBACK)
    assert w.probed() == []


def test_relative_paths_in_cfg_are_ignored(w):
    """相对路径按当前目录解析，指到哪儿说不准。这里它恰好能解析到项目外一个能用的假解释器。"""
    w.python(w.tmp / "bin" / "python3.13", "3.13")
    w.venv(_cfg(home="bin", executable="bin/python3.13"))
    assert w.find(w.root, cwd=w.tmp) == (0, FALLBACK)
    assert w.probed() == []


def test_a_directory_named_like_the_interpreter_is_not_run(w):
    exe = w.tmp / "py" / "bin" / "python3.13"
    exe.mkdir(parents=True)
    w.venv(_cfg(home=exe.parent, executable=exe))
    assert w.find(w.root) == (0, FALLBACK)


def test_cfg_pointing_at_the_venv_itself_falls_back(w):
    venv = w.venv(_cfg(home=w.root / ".venv" / "bin", executable=w.root / ".venv" / "bin" / "python"))
    w.python(venv / "bin" / "python", "3.13")
    w.python(venv / "bin" / "python3.13", "3.13")
    w.python(venv / "bin" / "python3", "3.13")
    assert w.find(w.root) == (0, FALLBACK)
    assert w.probed() == []                      # .venv 里的一个都没执行


def test_cfg_pointing_at_the_app_bundle_python_falls_back(w):
    macos = w.root / APP_DIR / "Contents" / "MacOS"
    w.python(macos / "python", "3.13")
    w.venv(_cfg(home=macos, executable=macos / "python"))
    assert w.find(w.root) == (0, FALLBACK)
    assert w.probed() == []


def test_a_link_from_outside_into_the_project_is_still_the_project(w):
    venv = w.venv(_cfg())
    w.python(venv / "bin" / "python3.13", "3.13")
    alias = w.tmp / "elsewhere"
    alias.symlink_to(venv / "bin", target_is_directory=True)
    w.venv(_cfg(home=alias, executable=alias / "python3.13"))
    assert w.find(w.root) == (0, FALLBACK)
    assert w.probed() == []


def test_project_reached_through_a_link_is_still_the_project(w):
    venv = w.venv(_cfg())
    w.python(venv / "bin" / "python3.13", "3.13")
    w.venv(_cfg(home=venv / "bin", executable=venv / "bin" / "python3.13"))
    link_root = w.tmp / "project-link"
    link_root.symlink_to(w.root, target_is_directory=True)
    assert w.find(link_root) == (0, FALLBACK)
    assert w.probed() == []


def test_a_neighbour_folder_sharing_the_prefix_is_not_inside(w):
    """项目叫 project、解释器在 project-pythons 里：前缀相同不等于在项目里面。"""
    exe = w.python(w.tmp / "project-pythons" / "bin" / "python3.13", "3.13")
    w.venv(_cfg(home=exe.parent, executable=exe))
    assert w.find(w.root) == (0, str(exe))


# ---- /usr/bin 的垫片 ----------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists("/usr/bin/python3"), reason="这台机器没有 /usr/bin/python3")
def test_usr_bin_shim_is_not_run_without_developer_tools(w):
    """pyvenv.cfg 指到 /usr/bin 时，没装开发者工具就碰它会弹「安装开发者工具」。
    假的 xcode-select 报一个没有 python3 的目录：/usr/bin/python3 不该被执行，于是落回兜底。"""
    w.devdir = w.tmp / "CommandLineTools"
    w.devdir.mkdir()
    version = "3.9.6"     # macOS 命令行工具的 python3；这里绝不执行垫片去问
    if sys.platform != "darwin":
        # Linux 上 /usr/bin/python3 是真的：记成它的版本，检查要是失效，这里会选中它而变红
        real = subprocess.run(["/usr/bin/python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                              capture_output=True, timeout=60)
        if real.returncode == 0:
            version = real.stdout.decode().strip() + ".0"
    w.venv(_cfg(home="/usr/bin", version=version, executable="/usr/bin/python3"))
    assert w.find(w.root) == (0, FALLBACK)


# ---- 文件本身 -----------------------------------------------------------------------------

def test_the_finder_parses_in_system_bash():
    proc = subprocess.run([BASH, "-n", str(FINDER)], capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def test_sourcing_the_finder_runs_nothing(w):
    proc = subprocess.run([BASH, "-c", '. "$1"', "bash", str(FINDER)], env=w.env(),
                          capture_output=True, timeout=30)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")


def test_no_bash4_only_features():
    """macOS 的 /bin/bash 是 3.2：这些写法在那里是语法错误或行为不同。"""
    text = FINDER.read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] if line.lstrip().startswith("#") else line
                     for line in text.splitlines())
    for banned in ("declare -A", "local -A", "mapfile", "readarray", ",,}", "^^}", "${!", "&>>",
                   "|&", "coproc", "local -n", "declare -n"):
        assert banned not in code, banned


# ---- 两个启动器：把项目根传进来，并 exec 选中的那个 -----------------------------------------

def _launcher_project(w):
    """把真的两个启动器和真的 find_python.sh 放进临时项目；.venv 是项目外一个假 python 建的。"""
    macos = w.root / APP_DIR / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    shutil.copy2(LAUNCHER, macos / LAUNCHER.name)
    shutil.copy2(START_COMMAND, w.root / START_COMMAND.name)
    (w.root / "tools").mkdir()
    shutil.copy2(FINDER, w.root / "tools" / FINDER.name)
    (w.root / "main.py").write_text("raise SystemExit('不该跑到这里')\n", encoding="utf-8")
    exe = w.python(w.tmp / "anaconda 3" / "bin" / "python3.13", "3.13")
    w.venv(_cfg(home=exe.parent, executable=exe))
    return exe


def _run(w, script, *args):
    # PATH 里只有假的 osascript / open 和真的 dirname：万一走到「找不到 Python」也不弹框
    launch_bin = w.tmp / "launch-bin"
    launch_bin.mkdir()
    for name in ("osascript", "open"):
        fake = launch_bin / name
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
    (launch_bin / "dirname").symlink_to(shutil.which("dirname"))
    env = {"PATH": str(launch_bin), "HOME": str(w.tmp), "TLT_PROBE_LOG": str(w.log)}
    proc = subprocess.run([BASH, str(script)] + list(args), env=env, cwd=str(w.tmp),
                          capture_output=True, timeout=60)
    lines = w.log.read_text(encoding="utf-8").splitlines() if w.log.exists() else []
    return proc, [line.split("\037") for line in lines]


def test_the_app_launcher_execs_the_venv_creator(w):
    exe = _launcher_project(w)
    proc, calls = _run(w, w.root / APP_DIR / "Contents" / "MacOS" / LAUNCHER.name)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert calls[-1] == [str(exe), "main.py"]
    assert [c[0] for c in calls] == [str(exe), str(exe)]   # 探测一次、exec 一次


def test_start_command_execs_the_venv_creator_with_its_args(w):
    exe = _launcher_project(w)
    proc, calls = _run(w, w.root / START_COMMAND.name, "--demo", "two words")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert calls[-1] == [str(exe), "main.py", "--demo", "two words"]


def test_both_launchers_pass_the_project_root():
    for script in (LAUNCHER, START_COMMAND):
        text = script.read_text(encoding="utf-8")
        calls = [line.strip() for line in text.splitlines() if "find_python" in line and "$(" in line]
        assert len(calls) == 1, (script.name, calls)
        assert calls[0].startswith('PY="$(find_python "$'), (script.name, calls[0])
