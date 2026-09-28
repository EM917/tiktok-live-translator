"""桌面页 web/*.js 里的 getElementById、index.html 里的 aria-controls，
目标必须真的在 index.html 里存在。

起因：这次把设置行拆进 web/settings-rows.js、改折叠行接线时，engineer.md #4
是靠临时脚本人肉核对了一遍「110 个 id，JS 引用的 102 个全部命中」——核对完
脚本就扔了，下一次改 index.html 删掉或改名一个 id，getElementById 会静默
返回 null，对应的开关/摘要/折叠行从此失效且没有任何报错，aria-controls
挂空目标则是读屏用户会直接感知到的无障碍缺陷。两者都不该只靠人工核对。

跳过 web/viewer.js：手机页是独立页面 viewer.html，元素 id 集合跟桌面页
index.html 完全不是一回事（tests/test_viewer_routes.py、
tests/test_viewer_texts.py 已经覆盖手机页自己的一致性）。
"""
import re

import pytest

from app import viewer as viewer_mod

WEB_DIR = viewer_mod.WEB_DIR
INDEX_HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8")

# index.html 里所有带 id 的元素——标签名、属性顺序都不固定，只找 id="..." 本身。
HTML_IDS = set(re.findall(r'\bid="([^"]+)"', INDEX_HTML))

GET_ELEMENT_BY_ID = re.compile(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""")
ARIA_CONTROLS = re.compile(r'\baria-controls="([^"]+)"')


def desktop_js_files():
    """web/*.js，排除手机页专用的 viewer.js。"""
    return sorted(p for p in WEB_DIR.glob("*.js") if p.name != "viewer.js")


def test_the_repo_actually_has_element_ids_to_check_against():
    """防止 index.html 路径读错、正则写坏时这份测试自己假装通过。"""
    assert len(HTML_IDS) > 50, "index.html 里的 id 数量少得反常，检查 WEB_DIR/正则"


@pytest.mark.parametrize("path", desktop_js_files(), ids=lambda p: p.name)
def test_every_get_element_by_id_target_exists_in_index_html(path):
    text = path.read_text(encoding="utf-8")
    targets = set(GET_ELEMENT_BY_ID.findall(text))
    missing = sorted(targets - HTML_IDS)
    assert not missing, "{} 引用了 index.html 里不存在的 id: {}".format(path.name, missing)


def test_every_aria_controls_target_exists_in_index_html():
    targets = set(ARIA_CONTROLS.findall(INDEX_HTML))
    assert targets, "index.html 里没有找到任何 aria-controls，检查正则/文件内容"
    missing = sorted(targets - HTML_IDS)
    assert not missing, "aria-controls 指向了 index.html 里不存在的 id: {}".format(missing)
