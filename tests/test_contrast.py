"""文字对比度（专家审查 V1、V2）：直接读 web/style.css 和 web/viewer.css 的令牌，按 WCAG 2.x 的相对亮度公式算。

- V1：出错的持续提示条、检测降级条是白字红底。深色模式下以前直接用 --fail #ff6961（那是给红字用的亮红），
  白字只有 2.82:1；改成专门的 --fail-bar（深色 #b3000f，7.18:1），浅色仍是 #d70015，外观不变。
- V2：手机页的正文（断线时的「状态未知」、隐藏报警的说明、时间戳、「翻译中…」、弹幕原文、只看模式、提示音说明）
  以前用 --label-3（只给占位符/装饰的那一档，浅色约 2:1、深色约 3:1），改成 --label-2。

半透明的字色先按 alpha 叠到它实际所在的底色上再算（底色也从样式表里读：顶栏、底栏、报警卡片各有各的底）。
不起浏览器、不 import main.py。真页面上的计算色在 PR 的无头 Chrome 对照里量过。
"""
import re
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DARK = "prefers-color-scheme: dark"


# ---- 样式表 -----------------------------------------------------------------------------------

def parse_rules(css):
    """[(选择器列表, {属性: 值}, 外面包着的 @ 规则前导)]。只认这两份样式表用到的写法：规则不嵌套，
    @media 里面是普通规则；@keyframes 整块跳过。"""
    text = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, stack, prelude, i = [], [], "", 0
    while i < len(text):
        ch = text[i]
        if ch == "{":
            head = " ".join(prelude.split())
            prelude = ""
            if head.startswith("@keyframes"):
                depth, i = 1, i + 1
                while depth:
                    depth += {"{": 1, "}": -1}.get(text[i], 0)
                    i += 1
                continue
            if head.startswith("@"):
                stack.append(head)
                i += 1
                continue
            end = text.index("}", i)
            decls = {}
            for part in text[i + 1:end].split(";"):
                if ":" in part:
                    key, value = part.split(":", 1)
                    decls[key.strip()] = " ".join(value.split())
            out.append(([s.strip() for s in head.split(",")], decls, " ".join(stack)))
            i = end + 1
            continue
        if ch == "}":
            if stack:
                stack.pop()
            prelude = ""
        else:
            prelude += ch
        i += 1
    return out


def read(name):
    return (WEB_DIR / name).read_text(encoding="utf-8")


RULES = {name: parse_rules(read(name)) for name in ("style.css", "viewer.css")}


def decls(sheet, selector):
    """顶层（不在 @media 里）选择器列表里含这一项的规则，声明合起来（后写的盖先写的）。"""
    merged = {}
    for sels, body, at in RULES[sheet]:
        if not at and selector in sels:
            merged.update(body)
    return merged


def tokens(sheet, dark):
    """:root 里的令牌；深色时再用 @media (prefers-color-scheme: dark) 里的 :root 盖一遍。"""
    out = {}
    for sels, body, at in RULES[sheet]:
        if sels == [":root"] and (not at or (dark and DARK in at)):
            out.update({k: v for k, v in body.items() if k.startswith("--")})
    return out


# ---- 颜色 ---------------------------------------------------------------------------------------

def resolve(value, toks):
    """把 var(--x) 换成令牌的值（令牌里还套着 var 的接着换）。"""
    for _ in range(10):
        m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
        if not m:
            return value.strip()
        value = toks[m.group(1)]
    raise AssertionError("var() 套得太深：" + value)


def color(value, toks, behind=None):
    """(r, g, b)：#rgb / #rrggbb / rgb() / rgba()（按 alpha 叠在 behind 上）/
    color-mix(in srgb, X N%, Y)（这份样式表里 Y 都是不透明的）。"""
    value = resolve(value, toks)
    if value in ("#fff", "white"):
        value = "#ffffff"
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    if m:
        return tuple(int(m.group(1)[k:k + 2], 16) for k in (0, 2, 4))
    m = re.fullmatch(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", value)
    if m:
        rgb = tuple(float(m.group(k)) for k in (1, 2, 3))
        alpha = float(m.group(4)) if m.group(4) is not None else 1.0
        if alpha < 1:
            assert behind is not None, "半透明的颜色要给底色：" + value
            return over(rgb, alpha, behind)
        return rgb
    m = re.fullmatch(r"color-mix\(in srgb,\s*(var\(--[\w-]+\)|#[0-9a-fA-F]{6})\s+([\d.]+)%,\s*(var\(--[\w-]+\)|#[0-9a-fA-F]{6})\)",
                     value)
    if m:
        a, b = color(m.group(1), toks), color(m.group(3), toks)
        return over(a, float(m.group(2)) / 100, b)
    raise AssertionError("认不出的颜色：" + value)


def over(fg, alpha, bg):
    return tuple(fg[k] * alpha + bg[k] * (1 - alpha) for k in range(3))


def luminance(rgb):
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def background(sheet, selector, toks):
    rule = decls(sheet, selector)
    value = rule.get("background-color") or rule.get("background")
    assert value, "{} 的 {} 没有背景".format(sheet, selector)
    return color(value, toks, behind=color(decls(sheet, "body")["background"], toks))


def test_wcag_formula_and_parser_on_known_values():
    """公式本身：黑白 21:1；审查报告里的几个数（白字配 #d70015 是 5.38、配 #ff6961 是 2.82）照算得出来。"""
    assert ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    assert round(ratio((255, 255, 255), (0xd7, 0x00, 0x15)), 2) == 5.38
    assert round(ratio((255, 255, 255), (0xff, 0x69, 0x61)), 2) == 2.82
    assert round(ratio((255, 255, 255), (0xb3, 0x00, 0x0f)), 2) == 7.18
    rules = parse_rules("/* a { } */ .a, .b { color: red; margin: 0 } @media (x) { .c { top: 1px } }"
                        " @keyframes k { from { top: 0 } to { top: 1px } } .d { left: 0 }")
    assert [(sels, at) for sels, _, at in rules] == [([".a", ".b"], ""), ([".c"], "@media (x)"), ([".d"], "")]
    # 两份样式表都读得出深浅两套令牌（不然下面全是空转）
    for sheet in RULES:
        assert tokens(sheet, False)["--fail"] == "#d70015"
        assert tokens(sheet, True)["--fail"] == "#ff6961"


# ---- V1：白字红底的整条提示 ---------------------------------------------------------------------

V1_BARS = [("style.css", ".incident.error"), ("style.css", ".health-bar.degraded"),
           ("viewer.css", ".incident.error"), ("viewer.css", ".health-line.degraded")]


@pytest.mark.parametrize("sheet,selector", V1_BARS)
def test_red_bars_use_the_bar_token_with_white_text(sheet, selector):
    rule = decls(sheet, selector)
    assert rule.get("color") == "#fff"
    assert rule.get("background") == "var(--fail-bar)", "不能再用 --fail：深色下它是给红字用的亮红"
    if "border-bottom-color" in rule:
        assert rule["border-bottom-color"] == "var(--fail-bar)"


@pytest.mark.parametrize("sheet,selector", V1_BARS)
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_red_bars_reach_4_5_to_1_in_both_appearances(sheet, selector, dark):
    toks = tokens(sheet, dark)
    rule = decls(sheet, selector)
    got = ratio(color(rule["color"], toks), background(sheet, selector, toks))
    assert got >= 4.5, "{} {}（{}）白字只有 {:.2f}:1".format(sheet, selector, "深色" if dark else "浅色", got)


@pytest.mark.parametrize("sheet", ["style.css", "viewer.css"])
def test_bar_token_light_is_unchanged_and_dark_is_the_deeper_red(sheet):
    """浅色外观一个像素不变（--fail-bar 就是浅色的 --fail）；深色是 #b3000f（7.18:1）。"""
    assert tokens(sheet, False)["--fail-bar"] == tokens(sheet, False)["--fail"] == "#d70015"
    assert tokens(sheet, True)["--fail-bar"] == "#b3000f"
    assert ratio((255, 255, 255), color("var(--fail-bar)", tokens(sheet, True))) >= 7


@pytest.mark.parametrize("sheet", ["style.css", "viewer.css"])
def test_no_white_text_sits_on_the_red_text_token(sheet):
    """以后新加的白字红底也别再落到 --fail 上：两份样式表里任何 color 是白色的规则，背景都不能是 --fail。"""
    for sels, body, _ in RULES[sheet]:
        if body.get("color") in ("#fff", "#ffffff", "white"):
            bg = body.get("background") or body.get("background-color") or ""
            assert "var(--fail)" not in bg, "{} {} 白字配 --fail（深色下 2.82:1）".format(sheet, ", ".join(sels))


# ---- V2：手机页的正文不用 --label-3 ---------------------------------------------------------------

# (承载正文的选择器, 它所在的那一块的选择器——背景从那里读)
V2_TEXT = [
    (".stream-text.stale", ".top-bar"),        # 断线时的「状态未知」
    (".alert-local-note", "body"),             # 「只在这台手机上隐藏……」
    (".alert-zh.pending", ".alert-item"),      # 报警卡片里的「翻译中…」
    (".cap-meta", ".caption-section"),         # 字幕的时间和语言码
    (".cap-trans.pending", ".caption-section"),
    (".cap-trans.skipped", ".caption-section"),  # 翻译因积压被跳过（F4，同桌面的灰角标，不用提醒色）
    (".cmt-zh.pending", "body"),
    (".cmt-orig", "body"),                     # 弹幕西语原文
    (".ro-note", ".bottom-bar"),               # 「只看模式 · 不能操作」
    (".ring-note", "body"),                    # 提示音说明
]
# --label-3 还能用的地方：装饰（分隔点）、禁用态。报警卡片右上角的 ×（.alert-dismiss）以前也在这里，
# 可它是能点的图形控件，--label-3 在卡片底上不到 WCAG 1.4.11 的 3:1；#148 改成了 --label-2，下面那条按 3:1 钉着
LABEL3_ALLOWED = {".sep", ".cap-retranslate:disabled"}


@pytest.mark.parametrize("selector,_", V2_TEXT)
def test_phone_body_text_uses_label_2(selector, _):
    assert decls("viewer.css", selector).get("color") == "var(--label-2)", selector


def test_phone_label_3_is_left_only_for_decoration_icons_and_disabled():
    used = {sel for sels, body, at in RULES["viewer.css"] for sel in sels
            if not sel.startswith(":root") and any("var(--label-3)" in v for v in body.values())}
    assert used <= LABEL3_ALLOWED, "这些选择器还在用 --label-3：{}".format(sorted(used - LABEL3_ALLOWED))


@pytest.mark.parametrize("selector,block", V2_TEXT)
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_phone_body_text_reaches_4_5_to_1_on_its_own_background(selector, block, dark):
    toks = tokens("viewer.css", dark)
    bg = background("viewer.css", block, toks)
    fg = color(decls("viewer.css", selector)["color"], toks, behind=bg)
    got = ratio(fg, bg)
    assert got >= 4.5, "{}（{}，底色 {}）只有 {:.2f}:1".format(selector, "深色" if dark else "浅色", block, got)


@pytest.mark.parametrize("dark,want,before", [(False, 4.76, 1.91), (True, 6.16, 2.94)], ids=["light", "dark"])
def test_phone_alert_dismiss_icon_reaches_3_to_1_on_the_alert_card(dark, want, before):
    """报警卡片右上角的 ×（.alert-dismiss）是能点的图形控件，WCAG 1.4.11 要 3:1。以前用 --label-3：浅色卡片底上
    1.91:1、深色 2.94:1（b2-contrast 第 2 轮审查记下）；#148 改成 --label-2。#148 时这个文件不在那一批的清单里，
    检查先写在 tests/viewer.test.mjs，现在挪回这里（顺延事项）。卡片底是按主题预先混好的 --tint-fail-8"""
    toks = tokens("viewer.css", dark)
    assert decls("viewer.css", ".alert-dismiss")["color"] == "var(--label-2)"
    bg = background("viewer.css", ".alert-item", toks)
    got = ratio(color("var(--label-2)", toks, behind=bg), bg)
    assert got >= 3, "{} × 只有 {:.2f}:1".format("深色" if dark else "浅色", got)
    assert round(got, 2) == want
    assert round(ratio(color("var(--label-3)", toks, behind=bg), bg), 2) == before


def test_phone_label_2_on_the_page_background_matches_the_review_numbers():
    """审查报告给的数：页面底色上浅色 5.16:1、深色 6.59:1（以前 --label-3 是 1.96:1 / 3.00:1）。"""
    for dark, want, before in ((False, 5.16, 1.96), (True, 6.59, 3.00)):
        toks = tokens("viewer.css", dark)
        bg = color(decls("viewer.css", "body")["background"], toks)
        assert round(ratio(color("var(--label-2)", toks, behind=bg), bg), 2) >= want
        assert round(ratio(color("var(--label-3)", toks, behind=bg), bg), 2) == before


# ---- 顶栏「闪购」按钮的提醒态与浮层里的橙字（docs/flash-sale.md） -------------------------------------------
# 按钮要人处理时橙字配 16% 橙底（叠在顶栏 --bar-bg 上）；不用红字配淡红底：那一对深色只有 4.27:1。
# 浮层里「要处理」的主句、已停止的药丸是橙字配浮层底 --card。都要正文的 4.5:1

@pytest.mark.parametrize("dark,want", [(False, 4.64), (True, 4.98)], ids=["light", "dark"])
def test_flash_button_attention_state_reaches_4_5_to_1(dark, want):
    toks = tokens("style.css", dark)
    rule = decls("style.css", "#flash-btn.attn")
    assert rule["color"] == "var(--orange-ink)" and rule["background-color"] == "var(--a-orange-16)"
    bar = color("var(--bar-bg)", toks)
    bg = color(rule["background-color"], toks, behind=bar)
    got = ratio(color(rule["color"], toks, behind=bg), bg)
    assert got >= 4.5, "{} 只有 {:.2f}:1".format("深色" if dark else "浅色", got)
    assert round(got, 2) == want


@pytest.mark.parametrize("selector", [".flash-headline.attn", ".flash-state[data-tone=\"attn\"]"])
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_flash_panel_orange_text_reaches_4_5_to_1_on_the_popover(selector, dark):
    toks = tokens("style.css", dark)
    bg = color("var(--card)", toks)
    got = ratio(color(decls("style.css", selector)["color"], toks, behind=bg), bg)
    assert got >= 4.5, "{}（{}）只有 {:.2f}:1".format(selector, "深色" if dark else "浅色", got)
