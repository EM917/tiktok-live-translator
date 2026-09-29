# i18n: done
"""启动期的系统对话框（安装提示、缺组件、致命错误）：只负责组装交给系统的参数。

main.py 的 _info_dialog / _fail_alert 是「同一句话既 print 又弹框」。弹框的命令原来就拼在
main.py 里，而测试不能 import main.py（模块级跑 ensure_env()，会把 pytest 进程 execv 掉），
所以命令长什么样一直没有测试。现在组装挪到这里，main.py 只留起进程和全局句柄。

只依赖标准库：自举期（还在系统 Python 里、.venv 没装好）main.py 就要用它。
所有文字交给系统前都经 i18n.text()，是普通 str；中文模式下与原来 main.py 拼出来的逐字节相同
（tests/test_native_dialog.py）。
"""
from . import i18n
from .i18n import APP_NAME, L

INFO_BUTTON = L("知道了", "OK")      # 安装提示、「已经在运行」这类说明
FAIL_BUTTON = L("好", "OK")          # 致命错误


def esc_osa(text):
    """AppleScript 字符串字面量里的反斜杠和双引号。"""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def osascript_argv(message, *, button, icon, giving_up=None, lang=None):
    """macOS display dialog 的 argv。标题恒为 APP_NAME；giving_up 秒后自己消失（None 表示不消失）。"""
    script = 'display dialog "{}" with title "{}" buttons {{"{}"}} default button 1 with icon {}'.format(
        esc_osa(i18n.text(message, lang)), esc_osa(i18n.text(APP_NAME, lang)),
        esc_osa(i18n.text(button, lang)), icon)
    if giving_up:
        script += " giving up after {}".format(int(giving_up))
    return ["osascript", "-e", script]


def messagebox_args(message, lang=None):
    """Windows MessageBoxW 的 (正文, 标题)。"""
    return i18n.text(message, lang), i18n.text(APP_NAME, lang)
