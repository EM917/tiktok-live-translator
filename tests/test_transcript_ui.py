"""设置里「字幕记录」一行的静态面貌（web/index.html、web/app.js）。

CLAUDE.md 第六条：当前重心是字幕与翻译体验，报警等次要功能收在设置分组里、默认收起。这一行同样
是次要功能：在设置分组里、默认收起、永不被消息弹开，也不出现在首页一进来就看到的位置。

纯静态：不起浏览器、不 import main.py。接线（点按钮发什么消息、回音写在哪）见 tests/app.en.test.mjs。
"""
import re
from pathlib import Path

from tests.test_guard_index_html import INDEX, page

APP_JS = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")


def test_the_row_lives_in_the_settings_group_and_starts_collapsed():
    by_id = page().by_id
    for ident in ("transcript-card", "transcript-head", "transcript-body", "transcript-summary",
                  "transcript-export", "transcript-dir-btn", "transcript-status"):
        assert ident in by_id, ident
    assert "start-panel" in by_id["transcript-card"][2], "和其余设置行一样在开始面板的设置分组里"
    assert by_id["transcript-head"][1].get("aria-expanded") == "false"
    assert by_id["transcript-head"][1].get("aria-controls") == "transcript-body"
    assert "hidden" in by_id["transcript-body"][0]


def test_everything_in_the_row_is_inside_the_collapsible_body_except_the_head():
    by_id = page().by_id
    for ident in ("transcript-export", "transcript-dir-btn", "transcript-status"):
        assert "transcript-body" in by_id[ident][2], ident + " 要收在可折叠的正文里，不能露在行外面"
    assert "transcript-body" not in by_id["transcript-head"][2]


def test_the_row_sits_between_comment_review_and_storage():
    html = INDEX.read_text(encoding="utf-8")
    order = [html.index('id="{}"'.format(i)) for i in ("guard-card", "transcript-card", "disk-card")]
    assert order == sorted(order)


def test_both_buttons_and_the_row_name_carry_their_english():
    html = INDEX.read_text(encoding="utf-8")
    assert 'data-en="Transcripts">字幕记录<' in html
    assert 'data-en="Export Latest Session">导出最近一场<' in html
    assert 'data-en="Open Folder">打开文件夹<' in html


def test_the_page_never_opens_the_row_on_its_own():
    """会自己展开的只有启动自检和翻译引擎（它们有需要中控立刻看的结论）；字幕记录没有。"""
    assert not re.search(r"setRowOpen\(\s*transcript", APP_JS)
    assert not re.search(r"transcriptBody\.classList\.(remove|toggle)", APP_JS)
