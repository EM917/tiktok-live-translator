"""双击到窗口出来之间：一个看得见的提示，和各段耗时的时间点（app/bootstrap.py，main.py 只做调用）。

2026-10-29 新 Mac 上从「聚焦搜索」点开 .app：半分钟到一分钟里什么窗口都没有，用户以为点了没反应。那天 pip
本身不到一两秒（logs/bootstrap-*.log 同一秒建、同一秒写完），5 秒才弹的「正在补装」根本没机会出来；没有就绪
指纹时的子进程 import 检查、exec 进 .app 之后的 import、起后端、等后端最多 60 秒再开窗口，这几段都没有提示。
各段花了多久日志里也没有，那 30–60 秒花在哪儿查不出来。钉住：

* 「正在启动」提示：中英文整句；延迟 4 秒的 osascript（启动快于 4 秒，窗口出来时它还没弹就被关掉）；
* exec 前不关它，PID 经环境变量交给最终进程；最终进程关之前核对它确实是自己的子进程、确实是 osascript；
* 时间点写进 logs/bootstrap-*.log（同一次启动的几跳写同一个文件），只写中文，两种界面语言下逐字节相同；
* main.py 的接线（读语法树，不 import）：第一跳开始时起提示和日志，import 检查、pip、exec、后端就绪、窗口
  创建都记时间点，exec 前交出提示而不是关掉，开窗口后、出错弹框前、退出时关掉。

全部在 tmp_path 里；不起真的对话框、不杀任何真进程（ps 和 kill 都换成替身）。
"""
import ast
import re
import signal
from pathlib import Path

import pytest

from app import bootstrap, i18n, native_dialog

REPO = Path(__file__).resolve().parent.parent
EN = i18n.EN


@pytest.fixture(autouse=True)
def _chinese(monkeypatch):
    monkeypatch.setitem(i18n._state, "lang", i18n.ZH)
    monkeypatch.setitem(i18n._state, "override", None)


# ---- 「正在启动」提示 ----------------------------------------------------------------------------

def test_the_launch_notice_reads_in_both_languages():
    assert bootstrap.LAUNCH_TEXT == "正在启动 TikTok 直播同传，窗口准备好后会自己打开。"
    assert i18n.text(bootstrap.LAUNCH_TEXT, EN) == ("Starting TikTok Live Translator. The window opens on "
                                                    "its own when it’s ready.")


def test_the_launch_dialog_waits_four_seconds_inside_osascript_and_then_gives_up_by_itself():
    argv = bootstrap.launch_dialog_argv()
    assert argv[:3] == ["osascript", "-e", "delay 4"]
    assert argv[3] == "-e"
    assert argv[4] == native_dialog.osascript_argv(bootstrap.LAUNCH_TEXT, button=native_dialog.INFO_BUTTON,
                                                   icon="note", giving_up=300)[2]
    assert argv[4].startswith('display dialog "正在启动 TikTok 直播同传，窗口准备好后会自己打开。"')
    assert argv[4].endswith(" giving up after 300")
    english = bootstrap.launch_dialog_argv(lang=EN)[4]
    assert 'display dialog "Starting TikTok Live Translator.' in english


def test_osascript_without_a_delay_is_unchanged():
    """不带 delay 的调用（安装提示、致命错误）和以前逐字节相同。"""
    argv = native_dialog.osascript_argv("x", button=native_dialog.INFO_BUTTON, icon="note", giving_up=600)
    assert argv == ["osascript", "-e", 'display dialog "x" with title "TikTok 直播同传" buttons {"知道了"} '
                                       'default button 1 with icon note giving up after 600']
    plain = native_dialog.osascript_argv("x", button=native_dialog.INFO_BUTTON, icon="note")
    delayed = native_dialog.osascript_argv("x", button=native_dialog.INFO_BUTTON, icon="note", delay=2.5)
    assert delayed == ["osascript", "-e", "delay 2.5"] + plain[1:]


# ---- 交给最终进程、由它核对后关掉 -------------------------------------------------------------

class Ps:
    """ps -o ppid=,comm= -p PID 的替身：{pid: (ppid, 命令)}；不在表里就是查不到。"""

    def __init__(self, table):
        self.table = table
        self.asked = []

    def __call__(self, pid):
        self.asked.append(pid)
        return self.table.get(pid)


def test_the_handed_over_pid_rides_in_the_environment():
    env = {}
    bootstrap.hand_off_dialog(4321, environ=env)
    assert env == {bootstrap.LAUNCH_DIALOG_ENV: "4321"}
    bootstrap.hand_off_dialog(None, environ=env)          # 没有开着的提示框：什么都不交，旧的也清掉
    assert env == {}


def test_the_final_process_closes_its_own_osascript_child(monkeypatch):
    env = {bootstrap.LAUNCH_DIALOG_ENV: "4321"}
    killed, reaped = [], []
    ps = Ps({4321: (100, "/usr/bin/osascript")})
    assert bootstrap.close_handed_dialog(environ=env, own_pid=100, ps=ps,
                                         kill=lambda pid, sig: killed.append((pid, sig)),
                                         reap=reaped.append) is True
    assert killed == [(4321, signal.SIGTERM)] and reaped == [4321]
    assert bootstrap.LAUNCH_DIALOG_ENV not in env          # 只关一次，也不往子进程里传


@pytest.mark.parametrize("row", [
    (999, "/usr/bin/osascript"),          # 不是自己的子进程（PID 被别人用了）
    (100, "/bin/zsh"),                    # 是自己的子进程，但不是 osascript
    None,                                 # 查不到（已经没了）
], ids=["other-parent", "not-osascript", "gone"])
def test_anything_else_under_that_pid_is_left_alone(row):
    env = {bootstrap.LAUNCH_DIALOG_ENV: "4321"}
    killed = []
    ps = Ps({4321: row} if row else {})
    assert bootstrap.close_handed_dialog(environ=env, own_pid=100, ps=ps,
                                         kill=lambda pid, sig: killed.append(pid),
                                         reap=lambda pid: None) is False
    assert killed == [] and bootstrap.LAUNCH_DIALOG_ENV not in env


@pytest.mark.parametrize("value", ["", "abc", "-5", "0"])
def test_a_garbled_pid_is_ignored(value):
    env = {bootstrap.LAUNCH_DIALOG_ENV: value}
    ps = Ps({})
    assert bootstrap.close_handed_dialog(environ=env, own_pid=100, ps=ps, kill=None, reap=None) is False
    assert ps.asked == [] and env == {}


def test_nothing_handed_over_means_nothing_to_do():
    assert bootstrap.close_handed_dialog(environ={}, own_pid=100, ps=Ps({}), kill=None, reap=None) is False


def test_a_zombie_osascript_that_already_finished_is_still_recognised():
    """用户早就点了「知道了」：osascript 已经退出、还没被收，ps 里命令名带括号。照样收掉，不留僵尸进程。"""
    env = {bootstrap.LAUNCH_DIALOG_ENV: "4321"}
    reaped = []
    ps = Ps({4321: (100, "(osascript)")})
    assert bootstrap.close_handed_dialog(environ=env, own_pid=100, ps=ps, kill=lambda pid, sig: None,
                                         reap=reaped.append) is True
    assert reaped == [4321]


def test_parse_ps_output():
    assert bootstrap._parse_ps("  100 /usr/bin/osascript\n") == (100, "/usr/bin/osascript")
    assert bootstrap._parse_ps("") is None
    assert bootstrap._parse_ps("garbage") is None


# ---- 时间点 ----------------------------------------------------------------------------------

LINE = re.compile(r"^\[启动计时\] \d\d:\d\d:\d\d\.\d{3} \+\d+\.\d 秒 .+$")


def test_one_launch_writes_its_marks_into_one_bootstrap_log(tmp_path):
    env = {}
    path = bootstrap.start_launch_log(tmp_path, environ=env, now=1000.0)
    assert path.parent == tmp_path / "logs" and re.fullmatch(r"bootstrap-\d{8}-\d{6}\.log", path.name)
    assert env[bootstrap.LAUNCH_LOG_ENV] == str(path) and env[bootstrap.LAUNCH_T0_ENV] == "1000.0"
    bootstrap.launch_mark("第一跳开始", environ=env, now=1000.0)
    bootstrap.launch_mark("import 检查开始", environ=env, now=1000.25)
    # 下一跳（exec 之后）拿着同一份环境变量接着写
    after_exec = dict(env)
    bootstrap.launch_mark("后端就绪（端口 8765）", environ=after_exec, now=1031.0)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and all(LINE.match(x) for x in lines), lines
    assert [x.split(" 秒 ", 1)[1] for x in lines] == ["第一跳开始", "import 检查开始", "后端就绪（端口 8765）"]
    assert [x.split(" +")[1].split(" ")[0] for x in lines] == ["0.0", "0.2", "31.0"]


def test_pip_output_lands_in_the_same_file(tmp_path):
    env = {}
    path = bootstrap.start_launch_log(tmp_path, environ=env, now=1000.0)
    assert bootstrap.launch_log_path(environ=env) == path
    assert bootstrap.launch_log_path(environ={}) is None


def test_marks_without_a_log_do_nothing_and_never_raise(tmp_path):
    assert bootstrap.launch_mark("第一跳开始", environ={}) is None
    env = {bootstrap.LAUNCH_LOG_ENV: str(tmp_path / "no" / "such" / "dir" / "x.log"),
           bootstrap.LAUNCH_T0_ENV: "not-a-number"}
    assert bootstrap.launch_mark("第一跳开始", environ=env, now=5.0) is None


def test_end_launch_stops_later_restarts_from_writing_into_this_log():
    env = {bootstrap.LAUNCH_LOG_ENV: "/x.log", bootstrap.LAUNCH_T0_ENV: "1.0", "OTHER": "1"}
    bootstrap.end_launch(environ=env)
    assert env == {"OTHER": "1"}


def test_the_marks_are_chinese_in_both_ui_languages(tmp_path, monkeypatch):
    """第九条：日志永远是中文。英文界面下写的时间点和中文界面下逐字节相同。"""
    out = {}
    for lang in (i18n.ZH, EN):
        monkeypatch.setitem(i18n._state, "lang", lang)
        env = {}
        root = tmp_path / lang
        path = bootstrap.start_launch_log(root, environ=env, now=1000.0)
        bootstrap.launch_mark("窗口创建", environ=env, now=1002.0)
        out[lang] = path.read_text(encoding="utf-8")
    assert out[i18n.ZH] == out[EN] and "窗口创建" in out[EN]


# ---- main.py 的接线（读语法树，不 import）-----------------------------------------------------

MAIN = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))


def _func(name):
    return next(n for n in ast.walk(MAIN) if isinstance(n, ast.FunctionDef) and n.name == name)


def _text(arg):
    """字符串常量，或 "…{}…".format(...) 里的那个模板。"""
    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "format":
        arg = arg.func.value
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _calls(node):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            args = [t for t in (_text(a) for a in n.args) if t is not None]
            out.append((name, args))
    return out


def _marks(node):
    return [args[0] for name, args in _calls(node) if name == "launch_mark" and args]


def test_ensure_env_starts_the_notice_and_the_log_on_the_first_hop_and_marks_each_step():
    calls = [name for name, _ in _calls(_func("ensure_env"))]
    assert "start_launch_log" in calls and "_start_launch_dialog" in calls
    marks = " | ".join(_marks(_func("ensure_env")))
    for event in ("第一跳开始", "import 检查开始", "import 检查结束", "pip 开始", "pip 结束", "exec"):
        assert event in marks, (event, marks)


def test_ensure_env_hands_the_notice_over_instead_of_closing_it_before_exec():
    calls = [name for name, _ in _calls(_func("ensure_env"))]
    assert "_hand_off_dialogs" in calls
    assert "_close_info_dialog" not in calls


def test_the_final_process_marks_backend_and_window_and_then_closes_the_notice():
    window = _func("run_with_window")
    marks = " | ".join(_marks(window))
    assert "后端就绪" in marks and "窗口创建" in marks
    names = [name for name, _ in _calls(window)]
    assert names.count("_close_launch_dialog") >= 2          # 开到已有实例上、自己开窗口
    assert "end_launch" in names
    # 窗口建好之后、进 GUI 循环之前关：webview.start() 要等窗口关了才返回
    body = ast.unparse(window)
    created = body.index("window = webview.create_window(i18n.text(APP_NAME, lang), url")
    assert created < body.index("_close_launch_dialog()", created) < body.index("webview.start(**", created)


def test_fatal_alerts_close_the_notice_first_and_the_final_process_closes_it_on_exit():
    assert "_close_launch_dialog" in [name for name, _ in _calls(_func("_fail_alert"))]
    main_calls = [name for name, _ in _calls(_func("main"))]
    assert "register" in main_calls                         # atexit.register(_close_launch_dialog)
    assert "最终进程开始" in " | ".join(_marks(_func("main")))


def test_the_install_notice_replaces_the_launch_notice():
    assert "_close_launch_dialog" in [name for name, _ in _calls(_func("_first_run_dialog"))]


def test_main_uses_only_the_bootstrap_helpers_for_the_handoff():
    """PID 的环境变量名只在 bootstrap 里：main.py 不自己拼。"""
    text = (REPO / "main.py").read_text(encoding="utf-8")
    assert "TLT_LAUNCH_DIALOG_PID" not in text and "TLT_LAUNCH_LOG" not in text
    assert bootstrap.LAUNCH_DIALOG_ENV == "TLT_LAUNCH_DIALOG_PID"
