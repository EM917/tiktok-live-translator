"""app/native_dialog.py：启动期对话框（安装提示、缺组件、致命错误）的命令组装。

以前命令直接拼在 main.py 的 _info_dialog / _fail_alert 里，而 main.py 测不了（import 就
execv），所以对话框长什么样从来没有测试。组装挪进 app/native_dialog.py 之后，这里钉两件事：

1. 中文模式下拼出来的东西与搬家前逐字节相同。下面的 _before_* 是搬家前（f57639e）main.py 里
   _esc_osa、_info_dialog、_fail_alert 那几处的原样抄写，**不许跟着新代码改**——它们就是基准；
2. main.py 真的在用它，参数也没变：读语法树，不 import。
"""
import ast
from pathlib import Path

import pytest

from app import i18n, native_dialog
from app.i18n import L

REPO = Path(__file__).resolve().parent.parent


# ---- 搬家前 main.py 的原样 ------------------------------------------------------------------

def _before_esc_osa(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _before_info_argv(message):
    return ["osascript", "-e",
            'display dialog "{}" with title "TikTok 直播同传" buttons {{"知道了"}} '
            'default button 1 with icon note giving up after 600'.format(_before_esc_osa(message))]


def _before_fail_argv(message):
    return ["osascript", "-e",
            'display dialog "{}" with title "TikTok 直播同传" buttons {{"好"}} '
            'default button 1 with icon caution'.format(_before_esc_osa(message))]


def _before_messagebox(message):
    return message, "TikTok 直播同传"           # MessageBoxW(None, message, APP_NAME, 0x30)


MESSAGES = [
    "首次运行：正在自动安装运行组件（约需 2–5 分钟，取决于网速）。\n完成后会自动打开窗口。",
    "无法启动本地服务：端口 8765–8774 全部被占用（[Errno 48] Address already in use）。\n"
    "请关闭占用这些端口的程序后重新打开。",
    'C:\\Users\\Zhang San\\.venv 里有 "双引号"、已经转义过的 \\" 和结尾的反斜杠\\',
    "程序已经在运行，这次给的直播间地址没有自动开始。\n请在已打开的窗口里输入。",
    "⚠️ niño 🎉 {} {0} %s",
    "",
]


@pytest.fixture(autouse=True)
def _chinese(monkeypatch):
    """闸关着时生产里的样子：界面语言是中文，没有一次性覆盖。"""
    monkeypatch.setitem(i18n._state, "lang", i18n.ZH)
    monkeypatch.setitem(i18n._state, "override", None)


def _all_plain_str(values):
    return all(type(v) is str for v in values)


@pytest.mark.parametrize("message", MESSAGES)
def test_info_dialog_argv_is_byte_identical_to_before(message):
    argv = native_dialog.osascript_argv(message, button=native_dialog.INFO_BUTTON,
                                        icon="note", giving_up=600)
    assert argv == _before_info_argv(message)
    assert _all_plain_str(argv)


@pytest.mark.parametrize("message", MESSAGES)
def test_fail_alert_argv_is_byte_identical_to_before(message):
    argv = native_dialog.osascript_argv(message, button=native_dialog.FAIL_BUTTON, icon="caution")
    assert argv == _before_fail_argv(message)
    assert _all_plain_str(argv)


@pytest.mark.parametrize("message", MESSAGES)
def test_windows_messagebox_args_are_unchanged(message):
    args = native_dialog.messagebox_args(message)
    assert args == _before_messagebox(message)
    assert _all_plain_str(args)


def test_a_bilingual_message_reaches_the_os_as_the_same_plain_chinese():
    """print 取中文、对话框也是这句中文；交给系统的是普通 str，不是 str 子类。"""
    message = MESSAGES[2]
    bi = L(message, 'English with "quotes" and a backslash\\')
    info = native_dialog.osascript_argv(bi, button=native_dialog.INFO_BUTTON, icon="note", giving_up=600)
    fail = native_dialog.osascript_argv(bi, button=native_dialog.FAIL_BUTTON, icon="caution")
    box = native_dialog.messagebox_args(bi)
    assert info == _before_info_argv(message) and _all_plain_str(info)
    assert fail == _before_fail_argv(message) and _all_plain_str(fail)
    assert box == _before_messagebox(message) and _all_plain_str(box)


def test_esc_osa_escapes_backslashes_before_quotes():
    assert native_dialog.esc_osa('a\\"b') == 'a\\\\\\"b'
    assert native_dialog.esc_osa("无需转义") == "无需转义"


# ---- main.py 的接线（不 import，读语法树） -----------------------------------------------

def _main_function(name):
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _dotted(node):
    """a.b.c 这种名字链还原成字符串；别的表达式返回 None。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id] + parts[::-1])


def _calls(func, dotted):
    """func 里所有调用名以 dotted 结尾的调用（ctypes.windll.user32.MessageBoxW 认 user32.MessageBoxW）。"""
    return [n for n in ast.walk(func) if isinstance(n, ast.Call)
            and ("." + (_dotted(n.func) or "")).endswith("." + dotted)]


def _shape(call):
    """(位置参数, {关键字: 值})：字面量取值，名字链取字符串。"""
    def value(node):
        try:
            return ast.literal_eval(node)
        except ValueError:
            return _dotted(node) or ast.dump(node)
    return [value(a) for a in call.args], {k.arg: value(k.value) for k in call.keywords}


def test_main_info_dialog_uses_the_note_dialog_that_gives_up_after_ten_minutes():
    (call,) = _calls(_main_function("_info_dialog"), "native_dialog.osascript_argv")
    assert _shape(call) == (["message"], {"button": "native_dialog.INFO_BUTTON",
                                          "icon": "note", "giving_up": 600})


def test_main_fail_alert_uses_the_caution_dialog_and_the_messagebox_args():
    func = _main_function("_fail_alert")
    (call,) = _calls(func, "native_dialog.osascript_argv")
    assert _shape(call) == (["message"], {"button": "native_dialog.FAIL_BUTTON",
                                          "icon": "caution"})          # 不自己消失
    (box,) = _calls(func, "native_dialog.messagebox_args")
    assert _shape(box) == (["message"], {})
    # text, title = native_dialog.messagebox_args(message)
    # MessageBoxW(None, text, title, 0x30)：正文、标题按这个顺序原样交给系统
    (assign,) = [n for n in ast.walk(func) if isinstance(n, ast.Assign) and n.value is box]
    names = [_dotted(e) for e in assign.targets[0].elts]
    (native,) = _calls(func, "user32.MessageBoxW")
    args, kwargs = _shape(native)
    assert (args, kwargs) == ([None] + names + [0x30], {})               # 0x30：MB_ICONWARNING


def test_main_no_longer_builds_its_own_dialog_scripts():
    src = (REPO / "main.py").read_text(encoding="utf-8")
    strings = [n.value for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not [s for s in strings if "display dialog" in s]
    assert "_esc_osa" not in src


# ---- 英文（M11）------------------------------------------------------------------------------

def test_english_dialogs_have_an_english_title_and_button_and_escape_the_text():
    bi = L("无法启动本地服务", 'Couldn’t start "the local service" at C:\\x\\')
    body = 'Couldn’t start \\"the local service\\" at C:\\\\x\\\\'
    info = native_dialog.osascript_argv(bi, button=native_dialog.INFO_BUTTON, icon="note",
                                        giving_up=600, lang="en")
    assert info == ["osascript", "-e",
                    'display dialog "{}" with title "TikTok Live Translator" buttons {{"OK"}} '
                    'default button 1 with icon note giving up after 600'.format(body)]
    fail = native_dialog.osascript_argv(bi, button=native_dialog.FAIL_BUTTON, icon="caution",
                                        lang="en")
    assert fail == ["osascript", "-e",
                    'display dialog "{}" with title "TikTok Live Translator" buttons {{"OK"}} '
                    'default button 1 with icon caution'.format(body)]
    box = native_dialog.messagebox_args(bi, lang="en")
    assert box == (bi.en, "TikTok Live Translator")
    for values in (info, fail, box):
        assert _all_plain_str(values) and not i18n.CJK.search("".join(values))
    with i18n.use("en"):                      # 缺省是当前界面语言
        assert native_dialog.osascript_argv(bi, button=native_dialog.FAIL_BUTTON,
                                            icon="caution") == fail


def test_the_buttons_are_bilingual_and_still_chinese_by_default():
    assert (native_dialog.INFO_BUTTON, native_dialog.FAIL_BUTTON) == ("知道了", "好")
    assert (i18n.text(native_dialog.INFO_BUTTON, "en"), i18n.text(native_dialog.FAIL_BUTTON, "en")) \
        == ("OK", "OK")


# ---- 自举期对话框的文字（app/bootstrap.py，M11）----------------------------------------------
# 中文写死的是改造前的原样：终端 print 和中文对话框都是它，不许跟着代码改

WAIT_ZH = "完成后字幕窗口会自动打开——请耐心等待，不要重复打开程序。"
ADVANCED_ZH = "（进阶：也可手动运行 setup.sh / setup.ps1，或 pip install -r requirements.txt）"
RETRY_ZH = "请检查网络连接，然后重新打开本程序——会自动从中断处继续安装。"


def _english(text):
    rendered = i18n.text(text, "en")
    assert not i18n.CJK.search(rendered), rendered
    return rendered


def test_install_dialog_texts_are_unchanged_in_chinese_and_read_in_english():
    from app import bootstrap

    cases = [
        (bootstrap.install_dialog_text("3.13", "3.13", True, requirements_only=True),
         "组件清单和上次安装时不一样了，正在补装运行组件（一般不到一分钟）。\n" + WAIT_ZH,
         "The list of components changed since the last install."),
        (bootstrap.install_dialog_text("3.12", "3.13", venv_existed=True),
         "Python 版本从 3.12 变成了 3.13，需要重新安装运行组件（约需 2–5 分钟，取决于网速）。\n"
         + WAIT_ZH, "Python changed from 3.12 to 3.13,"),
        (bootstrap.install_dialog_text("3.13", "3.13", venv_existed=True),
         "运行组件不完整，正在自动补装（约需 2–5 分钟，取决于网速）。\n" + WAIT_ZH,
         "Some of the app’s components are missing."),
        (bootstrap.install_dialog_text(None, "3.13"),
         "首次运行：正在自动安装运行组件（约需 2–5 分钟，取决于网速）。\n" + WAIT_ZH,
         "First launch: installing the app’s components"),
    ]
    for text, zh, en_head in cases:
        assert text == zh
        english = _english(text)
        assert english.startswith(en_head)
        assert english.endswith("\nThe caption window opens automatically when it’s done. "
                                "Don’t open the app again in the meantime.")


def test_pip_failure_texts_are_unchanged_in_chinese_and_read_in_english(monkeypatch):
    from app import bootstrap

    monkeypatch.setattr(bootstrap, "_free_gb", lambda root: 3.5)
    log = "/tmp/logs/bootstrap-20260929-101010.log"
    where = "\n详细记录：{}\n".format(log) + ADVANCED_ZH
    cases = [
        (("OSError: [Errno 28] No space left on device", 1, log, "3.13", "/"),
         "安装运行组件时磁盘空间不够（pip 报告写不进去）。运行环境约需 1.4 GB，语音和翻译模型另需约 "
         "4 GB，现在剩余 3.5 GB。\n腾出空间后重新打开本程序，会自动继续安装。",
         "There wasn’t enough disk space to install the app’s components (pip reported it "
         "couldn’t write). The app needs about 1.4 GB, plus about 4 GB for the speech and "
         "translation models (3.5 GB available now).\n"),
        (("ERROR: No matching distribution found for numpy", 1, log, "3.14", None),
         "pip 找不到适用于当前 Python 3.14 的安装包。\n本程序在 Python 3.12、3.13 上测试过：装好其中"
         "一个后，删掉程序目录里的 .venv 文件夹，再用那个 Python 运行一次 main.py（例如 python3.13 "
         "main.py），会用它重新安装运行组件。",
         "pip couldn’t find packages for Python 3.14.\nThe app is tested with these Python "
         "versions: 3.12, 3.13."),
        (("NewConnectionError: Failed to establish a new connection", 1, log, "3.13", None),
         "自动安装未完成：pip 连不上软件包服务器。\n" + RETRY_ZH,
         "Installation didn’t finish: pip couldn’t reach the package server.\n"),
        (("ERROR: No matching distribution found for numpy", 1, log, "3.13", None),
         "自动安装未完成：pip 没找到所需组件的安装包（pip 返回 1）。\n重新打开本程序会再试一次；"
         "一直这样的话，请把下面的记录发给开发者。",
         "Installation didn’t finish: pip couldn’t find packages for the required components "
         "(pip returned 1).\n"),
        (("ERROR: something new", 2, log, "3.13", None),
         "自动安装未完成（pip 返回 2）。\n" + RETRY_ZH,
         "Installation didn’t finish (pip returned 2).\n"),
    ]
    for args, head, en_head in cases:
        text = bootstrap.pip_failure_text(*args)
        assert text == head + where
        english = _english(text)
        assert english.startswith(en_head), english
        assert english.endswith("\nLog file: {}\n(Advanced: you can also run setup.sh / "
                                "setup.ps1, or pip install -r requirements.txt.)".format(log))
    no_disk_number = bootstrap.pip_failure_text("No space left on device", 1, None, "3.13", None)
    assert "约 4 GB。\n腾出空间" in no_disk_number and "继续安装。\n（进阶" in no_disk_number
    assert "translation models.\nFree up space" in _english(no_disk_number)


def test_other_install_failures_read_in_both_languages(tmp_path):
    from app import bootstrap

    unlogged = bootstrap.failure_text(RuntimeError("boom"))
    assert unlogged == "自动安装未完成。\n" + RETRY_ZH + "\n（boom）\n" + ADVANCED_ZH
    assert _english(unlogged) == (
        "Installation didn’t finish.\nCheck your network connection, then open the app again. "
        "Installation picks up where it left off.\n(boom)\n(Advanced: you can also run "
        "setup.sh / setup.ps1, or pip install -r requirements.txt.)")
    logged = bootstrap.failure_text(RuntimeError("boom"), root=tmp_path)
    assert logged.startswith("自动安装未完成。\n" + RETRY_ZH + "\n详细记录：")
    assert "\nLog file: " in _english(logged)
    failed = bootstrap.PipFailed(3, "ERROR: something new")
    assert str(failed) == "pip 返回 3"
    assert i18n.text(i18n.of(failed), "en") == "pip returned 3"
    assert bootstrap.failure_text(failed, python_version="3.13") == \
        "自动安装未完成（pip 返回 3）。\n" + RETRY_ZH + "\n" + ADVANCED_ZH
