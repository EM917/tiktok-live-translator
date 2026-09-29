"""app/instance.py：双击第二次时认出端口上已经在跑的本程序。

以前 main.py 只看页面开头有没有「直播同传」：界面文字一换成英文（或标题改名），第二次双击就
认不出自己，会再起一个后端。现在认 web/index.html 里与语言无关的 <meta name="tlt-app">，
旧字样也照认——升级时可能还有旧版本的实例在跑，它的页面上没有这个 meta。
"""
import ast
from pathlib import Path

import pytest

from app import instance
from app.instance import looks_like_us

REPO = Path(__file__).resolve().parent.parent


def _index_head():
    """main.py 实际读到的样子：页面前 PROBE_BYTES 字节，解码时坏字节替换掉。"""
    return (REPO / "web" / "index.html").read_bytes()[:instance.PROBE_BYTES].decode(errors="replace")


def test_new_fingerprint_works_without_any_chinese():
    page = ('<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="UTF-8">\n  '
            + instance.META + '\n  <title>TikTok Live Translator</title>')
    assert looks_like_us(page)


def test_an_instance_from_before_the_meta_is_still_recognised():
    page = '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n  <meta charset="UTF-8">\n  <title>TikTok 直播同传</title>'
    assert instance.MARKER not in page
    assert looks_like_us(page)


@pytest.mark.parametrize("page", [
    "",
    None,
    "<!DOCTYPE html><html><head><title>Some Other App</title>",
    '<meta name="tlt-application" content="x">',
    "Not Found",
])
def test_other_pages_are_not_us(page):
    assert not looks_like_us(page)


def test_index_html_carries_the_meta_right_after_charset():
    head = _index_head()
    assert '<meta charset="UTF-8">\n  ' + instance.META in head
    assert looks_like_us(head)


def test_index_html_is_recognised_by_the_meta_alone():
    """把中文字样拿掉（界面英文化之后的样子），光靠 meta 也认得出。"""
    head = _index_head().replace(instance.LEGACY_MARKER, "Live Translator")
    assert instance.LEGACY_MARKER not in head
    assert looks_like_us(head)


def test_main_probes_with_the_shared_fingerprint():
    """main.py 测不了（import 就 execv）：读语法树，确认探测用的是这里的函数和字节数，
    不再自己写中文字样。"""
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    func = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_existing_instance_url")
    called = {n.func.id for n in ast.walk(func)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "looks_like_us" in called
    reads = [n for n in ast.walk(func) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "read"]
    assert [getattr(r.args[0], "id", None) for r in reads] == ["PROBE_BYTES"]
    strings = [n.value for n in ast.walk(func)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not [s for s in strings if instance.LEGACY_MARKER in s]
