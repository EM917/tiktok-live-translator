"""英文界面的原生出口（spec §8，G12 里 C4 的那一份）：窗口标题、页面告诉窗口语言、关窗确认框、
系统通知，以及 main.py 的接线。

原生层（PyObjC / pythonnet / ctypes / osascript / PowerShell）收到 str 子类的行为没验证过，所以
这里每一处都断言交给系统的是普通 str（`type(x) is str`）。中文模式下与改造前逐字节相同。

机制部分的用例把双语对临时换进去（替身表、带引号和撇号的刁钻英文），验证的是渲染与转义
本身；M11 之后生产里的程序名、窗口标题、关窗框、通知都已是 L()，另有用例直接钉生产文字
（中文与改造前逐字节相同，英文照 docs/i18n-style.md 与 tests/i18n_golden.json）。
"""
import ast
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import alert_notify, i18n, window_attention, window_close, window_lang
from app.i18n import CJK, L

REPO = Path(__file__).resolve().parent.parent
APP = L("TikTok 直播同传", "TikTok Live Translator")


def _plain(values):
    return all(type(v) is str for v in values)


# ---- 关窗确认框 ---------------------------------------------------------------------------

BILINGUAL_CLOSE = {
    "global.quitConfirmation": L("正在监听直播，关闭窗口会停止违禁词监听。确定关闭？",
                                 "Close the window? This stops monitoring the live stream, "
                                 "including banned-term detection."),
    "global.quit": L("关闭", "Close"),
    "global.cancel": L("取消", "Cancel"),
}


def test_close_localization_in_chinese_is_the_table_itself_as_plain_str():
    got = window_close.close_localization()
    assert got == window_close.CLOSE_LOCALIZATION and _plain(got.values())
    assert window_close.close_localization("zh") == got


def test_close_localization_renders_every_key_as_plain_str(monkeypatch):
    monkeypatch.setattr(window_close, "CLOSE_LOCALIZATION", BILINGUAL_CLOSE)
    en = window_close.close_localization("en")
    assert set(en) == set(BILINGUAL_CLOSE) and _plain(en.values())
    assert not [v for v in en.values() if CJK.search(v)]
    assert en["global.cancel"] == "Cancel"
    zh = window_close.close_localization("zh")
    assert zh["global.cancel"] == "取消" and _plain(zh.values())


def test_localization_kwargs_pass_the_rendered_table(monkeypatch):
    monkeypatch.setattr(window_close, "CLOSE_LOCALIZATION", BILINGUAL_CLOSE)

    def create_window(title, url, localization=None):
        pass

    got = window_close.localization_kwargs(create_window, "en")["localization"]
    assert got["global.quit"] == "Close" and _plain(got.values())
    with i18n.use("en"):                      # 缺省是当前界面语言
        assert window_close.localization_kwargs(create_window) == {"localization": got}


# pywebview 6.2.1 的 webview/localization.py 默认表里与关窗、JS confirm() 有关的四个键
# （建窗口时整张表复制成 window.localization）
PYWEBVIEW_DEFAULT = {"global.quitConfirmation": "Do you really want to quit?",
                     "global.ok": "OK", "global.quit": "Quit", "global.cancel": "Cancel"}


def _create_window(title, url, localization=None):
    pass


@pytest.mark.parametrize("gate", ["closed", "zh", "en"])
def test_the_second_window_gets_both_chinese_buttons_whatever_the_gate(gate):
    """M11 给关窗表补上了 global.ok（spec §8.2 标明的中文可见修正）：第二个实例的窗口从此传
    localization，JS confirm() 的两个按钮从 pywebview 自带的「OK / Cancel」一起换成
    「好 / 取消」。这处变化落在 M11，与闸无关——开闸（Z1）时中文界面上什么都不再变。
    （M11 之前的样子，即表里没有 global.ok 时一个键都不传，由下一个用例用替身表钉着。）"""
    assert window_close.CLOSE_LOCALIZATION["global.ok"] == "好"
    if gate == "closed":
        loc = window_close.confirm_localization_kwargs(_create_window, "zh")["localization"]
    else:
        with i18n.use(gate):                 # 闸开着（一次性覆盖同样让 enabled() 为真）
            loc = window_close.confirm_localization_kwargs(_create_window, "zh")["localization"]
    assert (loc["global.ok"], loc["global.cancel"]) == ("好", "取消") and _plain(loc.values())
    en = window_close.confirm_localization_kwargs(_create_window, "en")["localization"]
    assert (en["global.ok"], en["global.cancel"]) == ("OK", "Cancel") and _plain(en.values())


@pytest.mark.parametrize("lang", ["zh", "en", None])
def test_without_ok_in_the_table_the_second_window_keeps_pywebview_buttons(monkeypatch, lang):
    """表里没有 global.ok 时一个键都不传：只换掉 Cancel 会变成「OK / 取消」混排。"""
    table = {k: v for k, v in window_close.CLOSE_LOCALIZATION.items() if k != "global.ok"}
    monkeypatch.setattr(window_close, "CLOSE_LOCALIZATION", table)
    assert window_close.confirm_localization_kwargs(_create_window, lang) == {}
    with i18n.use("zh"):
        assert window_close.confirm_localization_kwargs(_create_window, lang) == {}


def test_the_real_close_dialog_reads_in_both_languages():
    """M11：生产里的关窗表就是双语对（上面几个用例的替身表与它的英文一致）。"""
    zh = window_close.close_localization("zh")
    assert zh == {"global.quitConfirmation": "正在监听直播，关闭窗口会停止违禁词监听。确定关闭？",
                  "global.quit": "关闭", "global.cancel": "取消", "global.ok": "好"}
    en = window_close.close_localization("en")
    assert en == dict({k: v.en for k, v in BILINGUAL_CLOSE.items()}, **{"global.ok": "OK"})
    assert _plain(zh.values()) and _plain(en.values())
    assert not [v for v in en.values() if CJK.search(v)]


def test_the_second_window_gets_both_buttons_once_the_table_has_ok(monkeypatch):
    """M11 补上 global.ok 之后，两个按钮一起换（spec §8.2 标明的中文可见修正），与闸无关。"""
    monkeypatch.setattr(window_close, "CLOSE_LOCALIZATION",
                        dict(BILINGUAL_CLOSE, **{"global.ok": L("好", "OK")}))
    zh = window_close.confirm_localization_kwargs(_create_window, "zh")["localization"]
    assert zh["global.ok"] == "好" and zh["global.cancel"] == "取消" and _plain(zh.values())
    en = window_close.confirm_localization_kwargs(_create_window, "en")["localization"]
    assert en["global.ok"] == "OK" and en["global.cancel"] == "Cancel"
    assert window_close.confirm_localization_kwargs(lambda title, url: None, "zh") == {}


# ---- 窗口标题 -----------------------------------------------------------------------------

class FakeWindow:
    def __init__(self, localization=None):
        self.titles = []
        self.exposed = {}
        if localization is not None:
            self.localization = localization

    def expose(self, *fns):
        for fn in fns:
            self.exposed[fn.__name__] = fn

    def set_title(self, title):
        self.titles.append(title)


@pytest.fixture
def bilingual_titles(monkeypatch):
    """M11 之后的样子：程序名和「(N) 疑似违禁词」都是双语对。"""
    template = L("({}) 疑似违禁词 · {}", "({}) Possible Banned Terms · {}")
    monkeypatch.setattr(window_attention, "attention_title",
                        lambda n, base=APP: template.format(n, base) if n > 0 else base)


def test_the_title_follows_the_page_language_and_keeps_the_unseen_count(bilingual_titles):
    window = FakeWindow()
    attention = window_attention.WindowAttention(window, base=APP)
    assert attention.set_attention(2, "p", 1) is True
    assert window.titles == ["(2) 疑似违禁词 · TikTok 直播同传"]
    assert attention.set_lang("en") is True
    assert window.titles[-1] == "(2) Possible Banned Terms · TikTok Live Translator"
    # Bi 按中文比较相等：_shown 存的是渲染后的普通 str，否则上面那次会被当成「没变」拦掉
    assert attention.set_lang("en") is False and len(window.titles) == 2
    assert attention.set_attention(0, "p", 2) is True
    assert window.titles[-1] == "TikTok Live Translator"
    assert attention.set_lang("zh") is True and window.titles[-1] == "TikTok 直播同传"
    assert attention.set_lang("fr") is False            # 只认 en，其余一律中文
    assert _plain(window.titles)


def _golden():
    import json
    return json.loads((REPO / "tests" / "i18n_golden.json").read_text(encoding="utf-8"))


def test_the_real_app_name_and_window_title_match_the_golden_sentences():
    """spec §8.1：Python 的模板与 web/alerts.js 的拼接中文键不同、G3 聚合不到，改由同一份金句
    两边各钉一次（node 那边是 tests/alerts.en.test.mjs）。"""
    golden = _golden()
    assert i18n.APP_NAME == "TikTok 直播同传"
    assert i18n.text(i18n.APP_NAME, "en") == golden["app_name_en"]
    title = window_attention.attention_title(2)
    assert i18n.text(title, "zh") == "(2) 疑似违禁词 · TikTok 直播同传"
    assert i18n.text(title, "en") == golden["window_title_2_en"]
    assert window_attention.attention_title(0) is i18n.APP_NAME
    assert window_attention.DEFAULT_TITLE is i18n.APP_NAME


def test_the_real_title_follows_the_page_language():
    window = FakeWindow()
    attention = window_attention.WindowAttention(window)
    assert attention.set_attention(3, "p", 1) is True
    assert attention.set_lang("en") is True
    assert attention.set_attention(0, "p", 2) is True
    assert window.titles == ["(3) 疑似违禁词 · TikTok 直播同传",
                             "(3) Possible Banned Terms · TikTok Live Translator",
                             "TikTok Live Translator"]
    assert _plain(window.titles)


def test_a_window_attention_starts_in_the_language_of_the_process(bilingual_titles):
    window = FakeWindow()
    with i18n.use("en"):
        attention = window_attention.WindowAttention(window, base=APP)
    assert attention.set_attention(0, "p", 1) is False  # 建窗口时给的就是英文原标题
    assert attention.set_attention(1, "p", 2) is True
    assert window.titles == ["(1) Possible Banned Terms · TikTok Live Translator"]


# ---- 页面告诉窗口语言 --------------------------------------------------------------------

def test_the_page_updates_the_title_and_the_close_dialog_in_place(bilingual_titles, monkeypatch):
    monkeypatch.setattr(window_close, "CLOSE_LOCALIZATION", BILINGUAL_CLOSE)
    table = window_close.localization_kwargs(lambda title, url, localization=None: None,
                                             "zh")["localization"]
    table["global.ok"] = "OK"                    # pywebview 自己的键：不许被动
    window = FakeWindow(localization=table)
    attention = window_attention.expose_attention(window, base=APP)
    call = window_lang.expose_window_lang(window, attention)
    assert window.exposed["set_window_lang"] is call
    with i18n.use("zh"):                         # 双语机制生效（闸开着或一次性覆盖）
        assert call("en") is True
    assert window.localization is table          # 原地更新：cocoa 关窗时读的就是这一个 dict
    assert table["global.cancel"] == "Cancel" and table["global.ok"] == "OK"
    assert _plain(table.values())
    assert window.titles == ["TikTok Live Translator"]
    with i18n.use("en"):
        assert call("zh") is True
    assert table["global.cancel"] == "取消" and window.titles[-1] == "TikTok 直播同传"


def test_with_the_gate_closed_a_chinese_page_changes_nothing(monkeypatch):
    """闸关着、没有一次性覆盖：界面恒为中文，窗口保持建窗时的样子。第二个实例的窗口
    今天没有传 localization，不能被这里改出新的按钮文字来。"""
    window = FakeWindow(localization={"global.cancel": "Cancel"})
    attention = window_attention.expose_attention(window)
    call = window_lang.expose_window_lang(window, attention)
    for lang in ("zh", None, "fr", 3):
        assert call(lang) is False
    assert window.localization == {"global.cancel": "Cancel"} and window.titles == []


def test_a_window_built_without_our_localization_keeps_pywebview_buttons(bilingual_titles,
                                                                         monkeypatch):
    """第二个实例的窗口建窗时没传 localization（localized=False）：闸开着时换语言，标题照换，
    pywebview 自带的 localization 一个键都不动——pywebview 6 的实例 dict 总在，往里写就会把
    JS confirm() 的 Cancel 改成「取消」，而且是在开闸（Z1）时。"""
    monkeypatch.setattr(window_close, "CLOSE_LOCALIZATION", BILINGUAL_CLOSE)
    window = FakeWindow(localization=dict(PYWEBVIEW_DEFAULT))
    attention = window_attention.expose_attention(window, base=APP)
    call = window_lang.expose_window_lang(window, attention, localized=False)
    with i18n.use("zh"):                          # 闸开着
        assert call("en") is True
        assert call("zh") is True
        assert call("zh") is False
    assert window.localization == PYWEBVIEW_DEFAULT
    assert window.titles == ["TikTok Live Translator", "TikTok 直播同传"]


def test_windows_without_an_instance_localization_are_skipped_silently():
    """pywebview 5 没有 window.localization 实例 dict：跳过，关窗框等下次启动才换语言。"""
    with i18n.use("en"):
        assert window_lang.apply_window_lang(SimpleNamespace(), None, "en") is False
        assert window_lang.apply_window_lang(SimpleNamespace(localization=None), None,
                                             "en") is False

        class Broken:
            def set_lang(self, lang):
                raise RuntimeError("窗口已经关了")

        assert window_lang.apply_window_lang(SimpleNamespace(), Broken(), "en") is False
    assert window_lang.expose_window_lang(SimpleNamespace(), None) is None   # 没有 expose

    def refuse(fn):
        raise TypeError("不收")

    assert window_lang.expose_window_lang(SimpleNamespace(expose=refuse), None) is None


def test_the_bridge_function_is_a_plain_function_with_one_argument():
    """pywebview 按 __name__ 起名、按参数表生成 JS 函数：绑定方法会多出一个 self。"""
    import inspect

    window = FakeWindow()
    call = window_lang.expose_window_lang(window, None)
    assert call.__name__ == "set_window_lang"
    assert list(inspect.signature(call).parameters) == ["lang"]


# ---- 系统通知 -----------------------------------------------------------------------------

def _before_command(platform, which, title, text):
    """改造前（7476efe）alert_notify.command 的原样：中文模式下新旧必须逐字节相同。"""
    if platform == "darwin":
        return ["osascript", "-e",
                'display notification "{}" with title "{}"'.format(text, title)]
    if platform.startswith("win"):
        script = (alert_notify._WIN_TOAST.replace("@TITLE@", title).replace("@TEXT@", text)
                  .replace("@APP@", alert_notify._WIN_APP_ID))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                "-EncodedCommand", encoded]
    exe = which("notify-send")
    return [exe, title, text] if exe else None


def _which(name):
    return "/usr/bin/" + name


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_chinese_notifications_are_byte_identical_to_before(platform):
    got = alert_notify.command(platform, which=_which)
    assert got == _before_command(platform, _which, alert_notify.TITLE, alert_notify.TEXT)
    assert _plain(got)
    assert alert_notify.command("linux", which=lambda name: None) is None


TRICKY = L("有新的疑似违禁词报警，请查看窗口",
           'New alert for "Tom’s" \'shop\' <b> & C:\\x ‘a’ ‚b‛. Open the window.')


@pytest.fixture
def bilingual_notice(monkeypatch):
    monkeypatch.setattr(alert_notify, "TITLE", APP)
    monkeypatch.setattr(alert_notify, "TEXT", TRICKY)


def test_mac_notifications_escape_backslashes_and_quotes(bilingual_notice):
    (osa, flag, script) = alert_notify.command("darwin", lang="en")
    assert (osa, flag) == ("osascript", "-e") and type(script) is str
    body = TRICKY.en.replace("\\", "\\\\").replace('"', '\\"')
    assert script == 'display notification "{}" with title "TikTok Live Translator"'.format(body)
    assert alert_notify.command("darwin")[-1] == \
        'display notification "有新的疑似违禁词报警，请查看窗口" with title "TikTok 直播同传"'


def _ps_single_quoted(script, start):
    """PowerShell 单引号字符串从 start（开引号之后）读到哪里结束：这四种弯引号和 ' 一样，
    写两遍是字面量，单独一个就结束字符串。返回字符串内容（写两遍的已还原成一个）。"""
    quotes = alert_notify._PS_QUOTES
    out, i = [], start
    while i < len(script):
        ch = script[i]
        if ch in quotes:
            if i + 1 < len(script) and script[i + 1] in quotes:
                out.append(ch)
                i += 2
                continue
            return "".join(out)
        out.append(ch)
        i += 1
    raise AssertionError("字符串没有结束")


def test_windows_toasts_escape_xml_and_double_every_single_quote(bilingual_notice):
    cmd = alert_notify.command("win32", lang="en")
    assert _plain(cmd)
    script = base64.b64decode(cmd[-1]).decode("utf-16-le")
    head = "$x.LoadXml('"
    xml = _ps_single_quoted(script, script.index(head) + len(head))
    # 撇号没有提前结束字符串：读出来的正好是整段 toast XML
    assert xml.startswith("<toast>") and xml.endswith("</toast>")
    text = xml.split("<text>")[2].split("</text>")[0]
    assert text == (TRICKY.en.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    .replace('"', "&quot;"))
    assert "<text>TikTok Live Translator</text>" in xml
    for quote in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        assert TRICKY.en.count(quote) and script.count(quote * 2) >= TRICKY.en.count(quote)


def test_the_real_notification_reads_in_both_languages():
    assert alert_notify.command("darwin", lang="en") == [
        "osascript", "-e", 'display notification "New banned-term alert. Open the window to '
                           'review it." with title "TikTok Live Translator"']
    assert alert_notify.command("linux", which=_which, lang="en") == [
        "/usr/bin/notify-send", "TikTok Live Translator",
        "New banned-term alert. Open the window to review it."]
    script = base64.b64decode(alert_notify.command("win32", lang="en")[-1]).decode("utf-16-le")
    assert ("<text>TikTok Live Translator</text><text>New banned-term alert. Open the window to "
            "review it.</text>") in script
    assert not CJK.search(script)
    # 中文：与改造前逐字节相同（上面 test_chinese_notifications_are_byte_identical_to_before）
    assert alert_notify.command("linux", which=_which) == [
        "/usr/bin/notify-send", "TikTok 直播同传", "有新的疑似违禁词报警，请查看窗口"]


def test_the_menu_bar_fallback_name_is_rendered_as_plain_str(monkeypatch, tmp_path):
    """没进 .app 的兜底（app/macbrand.py）：CFBundleName 交给 PyObjC 的是普通 str，按启动时的语言。"""
    import sys
    import types

    from app import macbrand

    monkeypatch.setattr(sys, "platform", "darwin")
    info = {}

    class FakeBundle:
        @staticmethod
        def mainBundle():
            return FakeBundle()

        def bundlePath(self):
            return "/usr/local/bin"

        def infoDictionary(self):
            return info

    appkit = types.ModuleType("AppKit")
    appkit.NSApplication = object
    appkit.NSImage = object
    foundation = types.ModuleType("Foundation")
    foundation.NSBundle = FakeBundle
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    names = {}
    for lang in ("zh", "en"):
        with i18n.use(lang):
            macbrand.brand_mac_app(tmp_path)            # 没有图标文件：设完名字就返回
        names[lang] = (info["CFBundleName"], info["CFBundleDisplayName"])
    assert names == {"zh": ("TikTok 直播同传", "TikTok 直播同传"),
                     "en": ("TikTok Live Translator", "TikTok Live Translator")}
    assert _plain(names["zh"] + names["en"])


def test_linux_notifications_pass_the_text_unescaped(bilingual_notice):
    cmd = alert_notify.command("linux", which=_which, lang="en")
    assert cmd == ["/usr/bin/notify-send", "TikTok Live Translator", TRICKY.en] and _plain(cmd)


def test_the_notifier_renders_at_send_time(bilingual_notice):
    sent = []
    notifier = alert_notify.AlertNotifier(is_enabled=lambda: True, platform="darwin",
                                          runner=lambda cmd, **k: sent.append(cmd))
    assert notifier.send() is True
    with i18n.use("en"):
        assert notifier.send() is True
    assert "有新的疑似违禁词报警" in sent[0][-1] and "Tom’s" in sent[1][-1]


# ---- main.py 的接线（不 import，读语法树） -----------------------------------------------

def _main():
    return ast.parse((REPO / "main.py").read_text(encoding="utf-8"))


def _function(tree, name):
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _call_name(node):
    if not isinstance(node, ast.Call):
        return None
    func, parts = node.func, []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    return ".".join(([func.id] if isinstance(func, ast.Name) else []) + parts[::-1])


def _calls(node, name):
    return [n for n in ast.walk(node) if _call_name(n) == name]


def _first_line(node, name):
    return min(c.lineno for c in _calls(node, name))


def test_main_boots_the_language_read_only_before_anything_can_exec():
    """boot 在模块级、ROOT 定下之后、ensure_env（可能 execv 进 .venv）之前，每个进程都跑。"""
    tree = _main()
    body = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    boot = [i for i, n in enumerate(body) if _calls(n, "i18n.boot")]
    root = [i for i, n in enumerate(body) if isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) == "ROOT" for t in n.targets)]
    ensure = [i for i, n in enumerate(body) if _calls(n, "ensure_env")]
    assert len(boot) == 1 and root[0] < boot[0] < ensure[0]
    (call,) = _calls(body[boot[0]], "i18n.boot")
    assert [getattr(a, "id", None) for a in call.args] == ["ROOT"]


def test_main_settles_in_the_final_process_before_reading_arguments():
    """settle 第一次 load_settings：必须在 relaunch_inside_bundle（execve）之后，
    parse_args 和别的读设置之前（spec §3.3）。"""
    func = _function(_main(), "main")
    assert (_first_line(func, "relaunch_inside_bundle") < _first_line(func, "i18n.settle")
            < _first_line(func, "parse_args") < _first_line(func, "_load_settings"))


def test_main_registers_the_one_off_ui_language_option():
    (call,) = [c for c in _calls(_function(_main(), "parse_args"), "p.add_argument")
               if c.args and getattr(c.args[0], "value", None) == "--ui-lang"]
    kwargs = {k.arg: k.value for k in call.keywords}
    assert ast.literal_eval(kwargs["choices"]) == ("zh", "en")


def test_the_language_fields_join_the_config_only_when_enabled():
    """闸关着、没有一次性覆盖时 hello 里的 config 一个键都不多（不变量 1）。"""
    func = next(n for n in _main().body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "main_async")
    (merge,) = _calls(func, "server.config.update")
    guards = [n for n in ast.walk(func) if isinstance(n, ast.If) and merge in ast.walk(n)]
    assert guards and _call_name(guards[-1].test) == "i18n.enabled"
    assert _call_name(merge.args[0]) == "i18n.config_info"


def test_both_windows_render_the_title_and_expose_the_language_bridge():
    func = _function(_main(), "run_with_window")
    windows = _calls(func, "webview.create_window")
    assert len(windows) == 2
    for call in windows:
        title = call.args[0]
        assert _call_name(title) == "i18n.text"
        assert [getattr(a, "id", None) for a in title.args] == ["APP_NAME", "lang"]
    bridges = _calls(func, "expose_window_lang")
    assert len(bridges) == 2
    for call in bridges:
        assert _call_name(call.args[1]) == "expose_attention"


def test_the_second_window_ties_its_localization_to_the_table_not_the_gate():
    """第二个实例的窗口今天没有 localization（JS confirm 用 pywebview 自带的文字）。传不传由
    confirm_localization_kwargs 按关窗表里有没有 global.ok 决定（M11），不看闸：否则开闸（Z1）
    会顺带改出中文可见的按钮文字。建窗时传没传，也要告诉 JS 桥（localized=bool(loc)）。"""
    func = _function(_main(), "run_with_window")
    branch = next(n for n in func.body if isinstance(n, ast.If)
                  and _calls(n, "webview.create_window"))
    (create,) = _calls(branch, "webview.create_window")
    assert [kw.value.id for kw in create.keywords if kw.arg is None] == ["loc"]
    assigns = [n for n in ast.walk(branch) if isinstance(n, ast.Assign)
               and [getattr(t, "id", None) for t in n.targets] == ["loc"]]
    assert len(assigns) == 1
    assert _call_name(assigns[0].value) == "confirm_localization_kwargs"
    assert not _calls(branch, "i18n.enabled") and not _calls(branch, "localization_kwargs")
    (bridge,) = _calls(branch, "expose_window_lang")
    (localized,) = [kw.value for kw in bridge.keywords if kw.arg == "localized"]
    assert _call_name(localized) == "bool" and localized.args[0].id == "loc"
