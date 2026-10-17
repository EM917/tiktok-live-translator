"""英文专属的版面规则只作用在英文页上（spec §11、提交 L1：挂在 html[lang="en"] 下，中文版面不动）。

最容易犯的错是选择器列表只给第一项加了前缀：
    html[lang="en"] .ctl-font .a-small, .ctl-font .a-large { display: none; }
第二项对中文页一样生效。中文页上多半看不出来，G10 也只在英文页上量版面，所以这里静态查：
一个选择器列表只要有一项带 html[lang="en"]，每一项都必须以它开头。
"""
import re
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
EN = 'html[lang="en"]'


def selector_lists(css):
    """每条样式规则的选择器列表（@media 里面的也算；@ 规则自己的前导不算）。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for prelude in re.findall(r"([^{}]+)\{", css):
        prelude = prelude.strip()
        if prelude and not prelude.startswith("@"):
            out.append([part.strip() for part in prelude.split(",")])
    return out


def test_selector_lists_are_read_inside_media_queries_too():
    css = """/* x, y { } */
    a, b { color: red; }
    @media (max-width: 1199px) {
      html[lang="en"] .p,
      .q { display: none; }
    }
    @keyframes k { to { opacity: 1; } }"""
    assert selector_lists(css) == [["a", "b"], ['html[lang="en"] .p', ".q"], ["to"]]


@pytest.mark.parametrize("name", ["style.css", "viewer.css"])
def test_english_only_rules_never_reach_the_chinese_page(name):
    lists = selector_lists((WEB_DIR / name).read_text(encoding="utf-8"))
    english = [sel for sel in lists if any(EN in part for part in sel)]
    assert english, "{} 里一条英文专属的规则都没找到，检查解析".format(name)
    leaked = [sel for sel in english if not all(part.startswith(EN) for part in sel)]
    assert not leaked, "{}：这些选择器列表里有一项没带 {} 前缀，会连中文页一起改：{}".format(
        name, EN, leaked)


# L1 落下的英文专属规则（spec §11 第 1、2、4、7 条，外加「品牌标签先让」）。删掉任何一条，
# 英文顶栏在 G10 的五档宽度上就又放不下了——而 G10 只在 CI 的 macOS 跑器上跑
L1_RULES = {
    "style.css": [
        'html[lang="en"] body',
        'html[lang="en"] .active-brand-tag',
        'html[lang="en"] .ctl-font .a-small',
        'html[lang="en"] .ctl-font .a-large',
        'html[lang="en"] #share-btn .btn-label',
        'html[lang="en"] #share-btn .btn-count',
        'html[lang="en"] .sc-item .sc-name',
    ],
    "viewer.css": ['html[lang="en"] body'],
}


@pytest.mark.parametrize("name", sorted(L1_RULES))
def test_the_english_layout_rules_are_there(name):
    selectors = {part for sel in selector_lists((WEB_DIR / name).read_text(encoding="utf-8"))
                 for part in sel}
    missing = [s for s in L1_RULES[name] if s not in selectors]
    assert not missing, "{} 里少了这些英文版面规则：{}".format(name, missing)


# 中文专属的规则（目前只有顶栏「浮窗」两个字，docs/i18n-style.md §4.2）挂在 html[lang^="zh"] 下，
# 同样的陷阱：选择器列表只给第一项加前缀，第二项就连英文页一起改了。英文页的顶栏只量英文页（G10 的
# live-float@宽度），所以这里静态查；写法和上面英文那条一样
ZH = 'html[lang^="zh"]'


def test_chinese_only_rules_never_reach_the_english_page():
    lists = selector_lists((WEB_DIR / "style.css").read_text(encoding="utf-8"))
    chinese = [sel for sel in lists if any(ZH in part for part in sel)]
    assert chinese, "style.css 里一条中文专属的规则都没找到，检查解析"
    leaked = [sel for sel in chinese if not all(part.startswith(ZH) for part in sel)]
    assert not leaked, "这些选择器列表里有一项没带 {} 前缀，会连英文页一起改：{}".format(ZH, leaked)


def test_the_chinese_float_label_rules_are_there():
    """删掉任何一条，中文页的「浮窗」就又只剩图标（或者露字时图标和文字一起挤进顶栏）。
    宽度范围和各规则的内容在 tests/float-button.test.mjs 里钉住。"""
    selectors = {part for sel in selector_lists((WEB_DIR / "style.css").read_text(encoding="utf-8"))
                 for part in sel}
    base = ZH + " body:not(.has-alert-tag) #float-btn"
    missing = [s for s in (base, base + " .i", base + " .btn-label") if s not in selectors]
    assert not missing, "style.css 里少了这些中文版的浮窗规则：{}".format(missing)


def test_the_alert_tag_class_the_float_label_waits_on_is_set_by_app_js():
    """样式表选不到兄弟的兄弟（标签在 .brand、按钮在 .controls），has-alert-tag 只能由脚本给 body 加；
    脚本里没人加，:not(.has-alert-tag) 就永远成立，报警标签亮着时「浮窗」两个字照样挤进已经满的顶栏。"""
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert re.search(r'document\.body\.classList\.toggle\("has-alert-tag",\s*!!show\)', js)
