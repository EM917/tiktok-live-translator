# i18n: done
"""页面告诉窗口自己是什么语言：原生窗口标题、后台报警时的标题、关窗确认框跟着换。

为什么由页面告诉窗口：双击第二次时，窗口开在另一个进程里（main.py 的 run_with_window 开头
那一支），那个进程收不到第一个进程里的语言切换；界面语言一换页面就整页重载，只有页面知道
自己此刻是什么语言。页面加载好之后经 pywebview 的 JS 桥调 set_window_lang（spec §8.1）。

放在 app/ 里是为了能测：main.py 导入时会 execv 进虚拟环境，测试不能 import 它。
"""
from . import i18n
from .window_close import close_localization


def apply_window_lang(window, attention, lang="zh"):
    """把窗口换成 lang（只认 "en"，其余一律中文）。换了什么返回 True。

    1. 标题：attention.set_lang 按新语言重设，基底加上此刻未看的条数；
    2. 关窗确认框：原地更新 window.localization。pywebview 6 在建窗口时把它复制成这个窗口
       自己的 dict，cocoa 在关窗那一刻才读它、winforms 拿的是同一个 dict 的引用，所以切换时
       更新就生效。不能挪到关窗时的 closing 事件里做：cocoa 先读文字、后触发 closing。
       pywebview 5 没有这个实例 dict（不是 dict 就跳过）：关窗框要等下次启动才换语言。

    中文且闸关着（没有一次性覆盖）时什么都不动：那时界面恒为中文，窗口保持建窗时的样子——
    第二个实例的窗口今天没有传 localization，不能被这里改出一个新的确认框按钮文字来。"""
    lang = i18n.EN if lang == i18n.EN else i18n.ZH
    if lang == i18n.ZH and not i18n.enabled():
        return False
    changed = False
    if attention is not None:
        try:
            changed = bool(attention.set_lang(lang))
        except Exception:
            pass
    localization = getattr(window, "localization", None)
    if isinstance(localization, dict):
        try:
            localization.update(close_localization(lang))
            changed = True
        except Exception:
            pass
    return changed


def expose_window_lang(window, attention):
    """把 set_window_lang 挂到窗口的 JS 桥上（页面里是 window.pywebview.api.set_window_lang），
    返回挂上去的函数；这个 pywebview 没有 expose 或挂不上时返回 None——窗口照常打开，
    标题和关窗框停在建窗口时的语言。要在 webview.start() 之前调。"""
    expose = getattr(window, "expose", None)
    if not callable(expose):
        return None

    # 挂普通函数而不是绑定方法：pywebview 按 __name__ 起名、按参数表生成 JS 函数
    def set_window_lang(lang="zh"):
        return apply_window_lang(window, attention, lang)

    try:
        expose(set_window_lang)
    except Exception:
        return None
    return set_window_lang
