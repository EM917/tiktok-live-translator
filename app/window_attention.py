"""窗口被别的软件盖住时，新报警要在桌面窗口的标题上看得见。

页面在后台时会把 document.title 改成「(N) 疑似违禁词」（web/alerts.js），浏览器标签页
上看得到；但桌面窗口（pywebview）的原生标题只在建窗口和 set_title 时设置，从不跟随
document.title（6.2.1 的 cocoa、winforms、edgechromium、gtk、qt 都没有这条同步）。
所以页面经 pywebview 的 JS 桥把「后台期间来了几条新报警」告诉这里，由这里改原生标题。
标题里只有条数，没有词条。

放在 app/ 里是为了能测：main.py 导入时会 execv 进虚拟环境，测试不能 import 它。
main.py 的 run_with_window 只负责调 expose_attention。

标题交给窗口之前按界面语言渲染成普通 str。页面重载后经 app/window_lang.py 告诉这里自己
是什么语言（set_lang），标题跟着换：基底加上此刻未看的条数。
"""
import threading

from . import i18n
from .i18n import APP_NAME

DEFAULT_TITLE = APP_NAME      # 旧名字，保留为 APP_NAME 的别名
MAX_COUNT = 999


def attention_title(count, base=APP_NAME):
    """与 web/alerts.js 的 alertTitle 同一个样子；0 条就是原标题。"""
    return "({}) 疑似违禁词 · {}".format(count, base) if count > 0 else base


class WindowAttention:
    """页面经 JS 桥调 set_attention(count, page, seq)。

    pywebview 每次 JS 调用都新开一个线程执行，先发的不一定先执行：同一个页面里 seq
    不大于已应用过的就丢掉，免得「3 条」晚于「0 条」生效——中控回到窗口后标题还挂着
    提醒，而页面认为已经清掉、不会再发。page 是页面每次加载时随机取的标识：刷新后的
    页面 seq 从头数，也要认。"""

    def __init__(self, window, base=APP_NAME, lang=None):
        self._window = window
        self._base = base
        self._lang = i18n.current() if lang is None else lang
        self._lock = threading.Lock()
        self._page = None
        self._seq = None
        self._count = 0
        # 建窗口时给的就是原标题。存渲染后的普通 str：Bi 按中文比较相等，存它的话换成英文后
        # 「标题没变」的判断会把该设的英文标题拦掉
        self._shown = i18n.text(base, self._lang)

    def _show(self, title):
        """在锁里调：标题（按当前语言渲染后）和窗口上的不一样才设，设上了返回 True。"""
        text = i18n.text(title, self._lang)
        if text == self._shown:
            return False
        try:
            # 在锁里设：两次调用的先后就是标题最终的先后。各平台的 set_title 都会
            # 自己切到界面线程（cocoa callAfter、winforms Invoke），这里是 JS 桥的
            # 工作线程，不是事件循环
            self._window.set_title(text)
        except Exception:
            return False            # 窗口已经关了之类：下次调用再试
        self._shown = text
        return True

    def set_attention(self, count, page=None, seq=None):
        try:
            n = max(0, min(int(count), MAX_COUNT))
            seq = None if seq is None else int(seq)
        except (TypeError, ValueError, OverflowError):
            return False
        page = str(page or "")[:64]
        with self._lock:
            if seq is not None:
                if page == self._page and self._seq is not None and seq <= self._seq:
                    return False
                self._page, self._seq = page, seq
            self._count = n
            return self._show(attention_title(n, self._base))

    def set_lang(self, lang):
        """页面告诉窗口它现在是什么语言（只认 "en"，其余一律中文）：按新语言重设标题，
        基底加上此刻未看的条数。标题真的变了才返回 True。"""
        lang = i18n.EN if lang == i18n.EN else i18n.ZH
        with self._lock:
            self._lang = lang
            return self._show(attention_title(self._count, self._base))


def expose_attention(window, base=APP_NAME):
    """把 set_attention 挂到窗口的 JS 桥上（页面里是 window.pywebview.api.set_attention），
    返回 WindowAttention；这个 pywebview 没有 expose 或挂不上时返回 None——标题提醒
    就只在浏览器里有，窗口照常打开。要在 webview.start() 之前调。"""
    expose = getattr(window, "expose", None)
    if not callable(expose):
        return None
    attention = WindowAttention(window, base)

    # 挂普通函数而不是绑定方法：pywebview 按 __name__ 起名、按参数表生成 JS 函数，
    # 绑定方法的参数表里会多出一个 self
    def set_attention(count=0, page=None, seq=None):
        return attention.set_attention(count, page, seq)

    try:
        expose(set_attention)
    except Exception:
        return None
    return attention
