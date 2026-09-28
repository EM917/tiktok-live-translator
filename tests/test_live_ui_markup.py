"""直播中界面的图标与浮层结构（web/index.html + web/app.js + web/live-ui.js）。

图标集中在 index.html 顶部的 .icon-sprite 里，静态 DOM 与 app.js 动态生成的都用
<use href="#i-…"> 引用：引用了不存在的 symbol 不会报错，只会画出一块空白——跟
getElementById 拿到 null 一样静默（见 tests/test_dom_id_refs.py 的起因）。
"""
import re

import pytest

from app import viewer as viewer_mod

WEB_DIR = viewer_mod.WEB_DIR
HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8")
APP = (WEB_DIR / "app.js").read_text(encoding="utf-8")
LIVE_UI = (WEB_DIR / "live-ui.js").read_text(encoding="utf-8")

SYMBOLS = set(re.findall(r'<symbol id="i-([a-z-]+)"', HTML))


def strip_html_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def strip_js_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", text)


def test_the_sprite_is_really_there():
    assert len(SYMBOLS) >= 8, SYMBOLS


def test_every_use_in_html_points_to_a_symbol():
    used = set(re.findall(r'<use href="#i-([a-z-]+)"', HTML))
    assert used, "index.html 里没有找到任何 <use href>，检查正则"
    assert not sorted(used - SYMBOLS)


def test_every_icon_name_in_js_points_to_a_symbol():
    names = set(re.findall(r'(?:iconEl|setIconText)\([^"\n]*"([a-z-]+)"', APP))
    names |= set(re.findall(r'return "([a-z-]+)";', LIVE_UI.split("function barIconFor")[1].split("function ")[0]))
    assert {"warn", "octagon", "info", "arrow-clockwise"} <= names, names
    assert not sorted(names - SYMBOLS)


# emoji（含 ⚠ ✅ ❌ 所在的杂项符号区）与以前当图标用的 × ↓ ↻：Windows 上 emoji 换成
# Segoe 画风，跟线性图标并排是两套语言。不拦排版用的 →（迁移词表确认框里的
# 「条目 → profiles/x.txt」是文字，不是图标）和 ⌘（「按 ⌘C」是键名）
BANNED_GLYPHS = re.compile("[\U0001F300-\U0001FAFF☀-➿×↓↻]")


@pytest.mark.parametrize("name,text,strip", [
    ("index.html", HTML, strip_html_comments),
    ("app.js", APP, strip_js_comments),
    ("live-ui.js", LIVE_UI, strip_js_comments),
], ids=["index.html", "app.js", "live-ui.js"])
def test_no_emoji_or_symbol_glyphs_in_desktop_ui(name, text, strip):
    body = strip(text)
    if name == "live-ui.js":
        # STATUS_EMOJI_PREFIX 本身就是用来去掉后端文字里这些字符的
        body = body.replace(body[body.index("var STATUS_EMOJI_PREFIX"):body.index("function stripStatusEmoji")], "")
    found = sorted(set(BANNED_GLYPHS.findall(body)))
    assert not found, "{} 里还有 emoji/符号: {}".format(name, found)


def header_span():
    start = HTML.index('<header class="topbar">')
    return start, HTML.index("</header>", start)


@pytest.mark.parametrize("panel,trigger", [("share-panel", "share-btn"), ("switch-panel", "switch-btn")])
def test_popovers_live_in_the_topbar_right_after_their_trigger(panel, trigger):
    """浮层挂在顶栏里（top:100% 才是顶栏底边，不推字幕区），并且紧跟在触发按钮
    后面——Tab 顺序：按钮 → 浮层内容 → 下一个顶栏按钮。挪回 <header> 外面就又
    变成把字幕区往下推的横带（designer.md #11）"""
    start, end = header_span()
    t = HTML.index('id="{}"'.format(trigger))
    p = HTML.index('id="{}"'.format(panel))
    assert start < t < p < end
    between = strip_html_comments(HTML[t:p])
    assert between.count("<button") == 0, "触发按钮和浮层之间不该夹别的按钮"
    assert 'aria-controls="{}"'.format(panel) in HTML[HTML.rindex("<button", 0, t):t + 400]
