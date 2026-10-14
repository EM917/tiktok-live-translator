"""弹幕留意在 web/index.html 里的初始状态（CLAUDE.md 第六条：不要把它放在一进来就看到的位置）。

JS 在 hello 之后才会切换 hidden，所以接线测试（用真的 index.html 搭假 DOM、再推消息）看不到「还没连上」
和「连不上」那一刻页面是什么样。这里直接读静态 HTML：顶部条、抽屉、状态胶囊、重新启用、浮钮红点
一开始都是 hidden；设置行的正文默认收起；开始面板里和弹幕留意有关的东西只有设置行那一块。

纯静态：不起浏览器、不 import main.py。
"""
from html.parser import HTMLParser
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

# 直播中才出现的：待机页面和连不上的页面上都不能有它们
LIVE_ONLY = ("guard-bar", "guard-drawer", "guard-state", "guard-reenable-cap", "comment-fab-guard")
# 其余带 hidden 起步的：按状态才露出来
STARTS_HIDDEN = LIVE_ONLY + ("guard-body", "guard-reenable", "guard-key-row", "guard-privacy", "guard-clear-auto",
                             "guard-pane-marked")


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.by_id = {}                 # id -> (classes, attrs, 祖先的 id 列表)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.by_id[attrs["id"]] = (set((attrs.get("class") or "").split()), attrs,
                                       [a for _, a in self.stack if a])
        if tag not in VOID:
            self.stack.append((tag, attrs.get("id")))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def page():
    parser = Page()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser


def test_the_live_only_pieces_and_the_conditional_ones_start_hidden():
    by_id = page().by_id
    for ident in STARTS_HIDDEN:
        assert ident in by_id, ident
        assert "hidden" in by_id[ident][0], ident + " 一开始必须是 hidden"


def test_the_settings_row_body_starts_collapsed():
    by_id = page().by_id
    assert by_id["guard-head"][1].get("aria-expanded") == "false"
    assert "hidden" in by_id["guard-body"][0]


def test_nothing_live_only_sits_under_the_start_panel():
    by_id = page().by_id
    for ident in LIVE_ONLY:
        assert "start-panel" not in by_id[ident][2], ident + " 不能放进开始面板"


def test_in_the_start_panel_only_the_settings_row_carries_comment_review_ids():
    parser = page()
    inside = [ident for ident, (_, _, ancestors) in parser.by_id.items()
              if ident.startswith("guard-") and "start-panel" in ancestors]
    assert inside, "设置行应该在开始面板的设置分组里"
    assert all(ident == "guard-card" or "guard-card" in parser.by_id[ident][2] for ident in inside), inside
