"""规则八的文案闸，按 A8 修订。

拦的是「给不透明失败贴原因」和因果断言句式——这些词每一个都曾被真的写进过代码
或对话：4003110 被先后解释成年龄限制、IP 限流、被我们打坏，三个全错。手机同看
这边的不透明失败是「手机打不开」，程序这边能观察到的只有「没有连接进来」。

**不拦**「防火墙」「Wi-Fi」「路由器」这类名词：说清系统会弹什么框、要连哪个
Wi-Fi，是可观察事实加可做的事，是要写出来的（A8）。
"""
import ast

import pytest

from app import viewer as viewer_mod

ROOT = viewer_mod.WEB_DIR.parent

BANNED = ("年龄", "限流", "封禁", "被墙", "拦截", "AP 隔离",
          "是因为", "应该是", "多半", "可能是", "导致")


def assert_clean(text, where):
    for word in BANNED:
        assert word not in text, (where, word, text[:200])


def operator_strings(source):
    """源码里会被人读到的中文串：字符串字面量，**不含**注释和文档字符串。

    只扫字符串是刻意的。注释和 docstring 是写给改代码的人的，里面正该出现
    「控制端口漂移是因为中控没有别的入口」「限流在 ViewerHub 里做」这种话；
    闸要拦的是发到界面上的句子。
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            out.append(node.value)
    return out


def assert_source_clean(source, where):
    found = operator_strings(source)
    assert found, where
    for text in found:
        assert_clean(text, where)


def section(text, start_marker, end_marker):
    head = text.index(start_marker)
    tail = text.index(end_marker, head)
    return text[head:tail]


def share_card_region(html):
    """index.html 里 #share-card 那一块：从它那一行起，到下一个同缩进的兄弟
    `<div id=` 为止。只扫这一块——别的卡片的文案不是这次改动的责任。"""
    lines = html.splitlines()
    for i, line in enumerate(lines):
        if 'id="share-card"' in line:
            indent = len(line) - len(line.lstrip())
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if "<div id=" in nxt and (len(nxt) - len(nxt.lstrip())) <= indent:
                    return "\n".join(lines[i:j])
            return "\n".join(lines[i:])
    return None


def test_the_viewer_module_texts_are_clean():
    assert_source_clean((ROOT / "app" / "viewer.py").read_text(encoding="utf-8"),
                        "app/viewer.py")


def test_the_new_pipeline_block_is_clean():
    text = (ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
    block = section(text, "# ---- 手机同看（见 app/viewer.py）----",
                    "# ---- 磁盘空间：盘点与可选删除")
    assert "手机同看" in block
    # 段落单独 parse 不了（缩进在类里），补一层壳
    assert_source_clean("class _Scan:\n" + block, "app/pipeline.py 的手机同看段")


def test_the_new_audit_methods_are_clean():
    text = (ROOT / "app" / "audit.py").read_text(encoding="utf-8")
    block = section(text, "    def viewer_share(", "    def window_closed(")
    assert_source_clean("class _Scan:\n" + block, "app/audit.py 的 viewer_* 方法")


def test_every_operator_note_is_clean():
    """模块里那几段整句文案逐条过闸（share_note 组出来的每一种都在这里）。"""
    notes = [getattr(viewer_mod, name) for name in dir(viewer_mod)
             if name.startswith("NOTE_")]
    assert len(notes) >= 10
    for note in notes:
        assert_clean(note, "NOTE 常量")


@pytest.mark.parametrize("relative", ["web/viewer.html", "web/viewer.js"])
def test_the_phone_page_texts_are_clean(relative):
    path = ROOT / relative
    if not path.is_file():
        pytest.skip("{} 还没落地（由另一份改动提供）".format(relative))
    assert_clean(path.read_text(encoding="utf-8"), relative)


def test_the_desktop_share_card_texts_are_clean():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    region = share_card_region(html)
    if region is None:
        pytest.skip("web/index.html 里还没有 #share-card（由另一份改动提供）")
    assert_clean(region, "web/index.html 的 #share-card 段")


def test_viewer_files_are_plain_names_that_really_exist():
    """白名单里的名字既要安全（不含分隔符），也要真的在 web/ 下——
    少一个文件，手机页就是一块空白，而服务端这边看不出任何异常。"""
    for name in viewer_mod.VIEWER_FILES:
        assert "/" not in name and "\\" not in name and ".." not in name, name
        path = viewer_mod.WEB_DIR / name
        if not path.is_file():
            pytest.skip("web/{} 还没落地（由另一份改动提供）".format(name))


def test_the_phone_page_only_uses_the_viewer_asset_prefix():
    path = viewer_mod.WEB_DIR / "viewer.html"
    if not path.is_file():
        pytest.skip("web/viewer.html 还没落地（由另一份改动提供）")
    html = path.read_text(encoding="utf-8")
    assert "/static/" not in html
    for name in ("app.js", "normalize.js", "alerts.js", "follow.js", "style.css"):
        assert name not in html, name


def test_the_help_line_only_states_observations():
    """手机打不开时，程序这边真正知道的只有「没有连接进来」。"""
    assert "只知道没有连接进来" in viewer_mod.NOTE_TROUBLE
    assert "可以依次试" in viewer_mod.NOTE_TROUBLE
    assert_clean(viewer_mod.NOTE_TROUBLE, "NOTE_TROUBLE")
