"""G5 静态页英文渲染模拟（spec §12.1）。

按 web/i18n.js 的 applyStatic 同一套规则，在 Python 里把 index.html / viewer.html 的 data-en*
代进去，跳过数据区子树的文字、<script>、<style>、注释，看整页还剩不剩中文。数据区两种标法
（用户决定 6）：名字标 translate="no"；字幕、弹幕、报警原话这类正文留给浏览器翻译，只带
class i18n-data（tests/i18n_rules.py 的 DATA_TEXT_CLASS）。
只对写了 <!-- i18n: done --> 的页面生效；没标记的页面整条跳过。

另有一条无条件的：inject_lang 靠 `<html lang="zh-CN">` 这个字面量换语言，两个页面里它都必须
恰好出现一次——改成别的写法，英文界面会静默失效（页面照样是中文，不报错）。"""
import textwrap

import pytest

from app import i18n
from tests import i18n_rules as rules
from tools import i18n_pairs as P

PAGES = ("web/index.html", "web/viewer.html")


@pytest.mark.parametrize("path", PAGES)
def test_the_html_lang_tag_is_there_exactly_once(path):
    src = P.read(path)
    assert src.count('<html lang="zh-CN">') == 1
    assert '<html lang="en">' in i18n.inject_lang(src, i18n.EN)


@pytest.mark.parametrize("path", PAGES)
def test_a_marked_page_renders_without_chinese(path):
    src = P.read(path)
    if not P.is_marked(path, src):
        pytest.skip("{} 还没写 <!-- i18n: done -->".format(path))
    left = P.render_static_en(src)
    assert not left, "\n".join("{}:{} {} {!r}".format(path, *x) for x in left)


@pytest.mark.parametrize("path", PAGES)
def test_the_pages_parse_into_a_real_tree(path):
    """解析器读错了（比如把整页当成一段文字），上面那条会假装通过。"""
    root = P.parse_html(P.read(path))
    tags = [el.tag for el in P.iter_elements(root)]
    assert tags[0] == "html" and "title" in tags and "body" in tags and len(tags) > 50


def _left(src):
    return [(line, where, text) for line, where, text in P.render_static_en(textwrap.dedent(src))]


def test_text_and_attributes_are_swapped_like_apply_static():
    src = '''
        <html lang="zh-CN"><head><title data-en="TikTok Live Translator">TikTok 直播同传</title></head>
        <body>
        <button id="jump" title="回到最新" data-en-title="Jump to latest" data-en="Jump to Latest"><svg><use href="#i"/></svg>回到最新</button>
        <input placeholder="粘贴 API 密钥" data-en-placeholder="Paste API key">
        <p data-en-html="Edit <code>banned_terms.txt</code>, one term per line.">编辑 <code>banned_terms.txt</code>，一行一个词。</p>
        <span data-en="Ready"></span>
        </body></html>
    '''
    assert _left(src) == []


def test_leftover_chinese_is_reported_with_where_it_is():
    src = '''
        <html lang="zh-CN"><body>
        <span>待机</span>
        <button title="停止当前直播间" data-en="Stop">停止</button>
        <p data-en="Stop">停止<b>x</b>然后开始</p>
        <p data-en-html="Stop <b>停</b>">停止</p>
        </body></html>
    '''
    got = _left(src)
    assert [(line, where) for line, where, _ in got] == [
        (3, "<span>"), (4, "<button title>"), (5, "<p>"), (6, "<b>")]
    assert got[2][2] == "然后开始"


def test_translate_no_skips_text_but_not_its_own_attributes():
    src = '''
        <html lang="zh-CN"><body>
        <select aria-label="目标语言" translate="no"><option title="简体">中文</option></select>
        <select aria-label="目标语言" data-en-aria-label="Translate to" translate="no">
          <option>日本語</option>
        </select>
        <div translate="no"><span>主播名</span></div>
        </body></html>
    '''
    assert [(line, where) for line, where, _ in _left(src)] == [(3, "<select aria-label>")]


def test_the_body_text_class_skips_text_like_translate_no():
    """正文不标 translate="no"（要留给浏览器翻译），只带 class i18n-data：子树文字照样豁免，
    自己的属性照查；class 要整词匹配。"""
    src = '''
        <html lang="zh-CN"><body>
        <div class="live-translated i18n-data"><span title="译文">这款面霜</span></div>
        <div class="i18n-data" title="大字幕">这款面霜</div>
        <div class="i18n-database">这款面霜</div>
        </body></html>
    '''
    assert [(line, where) for line, where, _ in _left(src)] == [(4, "<div title>"), (5, "<div>")]


def test_the_body_text_class_is_one_name_everywhere():
    """正文的豁免 class 写在五处：规则表（G4/G5）、scan.js（G10）、app.js 与 viewer.js 的 markBody、
    index.html 的底部大字幕。改名漏掉一处，要么页面上的正文被当成漏翻，要么豁免悄悄失效。
    底部大字幕只放译文和原文，所以它们不标 translate="no"，带这个 class。"""
    cls = rules.DATA_TEXT_CLASS
    assert 'var DATA_CLASS = "{}";'.format(cls) in P.read("tests/i18n_dom/scan.js")
    for path in ("web/app.js", "web/viewer.js"):
        assert 'el.classList.toggle("{}", isData);'.format(cls) in P.read(path), path
    root = P.parse_html(P.read("web/index.html"))
    marked = [el for el in P.iter_elements(root) if cls in (el.get("class") or "").split()]
    assert [el.get("id") for el in marked] == ["live-translated", "live-original"]
    assert [el.get("translate") for el in marked] == [None, None]


def test_scripts_styles_and_comments_are_not_page_text():
    src = '''
        <html lang="zh-CN"><head><style>/* 中文 */</style></head><body>
        <!-- 注释 -->
        <script>document.title = "请不要直接打开这个文件";</script>
        </body></html>
    '''
    assert _left(src) == []
