"""README 的加粗在 GitHub 上真的是加粗，不是两颗字面的星号。

CommonMark 的规则：`**` 能不能开始加粗、能不能结束加粗，看它两边的字符。中文里最常见的坑是
把 `**` 放在「」外面：「点**「换主播」**可以」里，前一个 `**` 左边是汉字、右边是「，不能开始；
后一个左边是」、右边是汉字，不能结束——GitHub 于是原样显示星号，还把两对之间的文字加粗了。
写成「点「**换主播**」可以」就对了（2026-09-29 用 GitHub 自己的渲染器核对过两种写法）。

这里按规范的 left/right-flanking 定义逐个检查 `**`：同一段里按出现顺序一开一合配对，
开的那个必须能开、合的那个必须能合。不引入 Markdown 库，规则本身只有几行。"""
import re
import unicodedata
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCS = ["README.md", "README.zh-CN.md", "docs/engine-benchmarks.md",
        "docs/engine-benchmarks.zh-CN.md"]

_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_BLOCK_START = re.compile(r"^\s*(?:[-*+] |\d+\. |#{1,6} |\||>)")


def _is_space(ch):
    return ch is None or ch.isspace()


def _is_punct(ch):
    return ch is not None and unicodedata.category(ch)[0] in "PS"


def _can_open(before, after):
    """left-flanking：后面不是空白，且（后面不是标点，或前面是空白/标点）。"""
    return not _is_space(after) and (
        not _is_punct(after) or _is_space(before) or _is_punct(before))


def _can_close(before, after):
    """right-flanking：前面不是空白，且（前面不是标点，或后面是空白/标点）。"""
    return not _is_space(before) and (
        not _is_punct(before) or _is_space(after) or _is_punct(after))


def _blocks(text):
    """(起始行号, 文本) 的段落：空行、围栏代码块、列表项/标题/表格行/引用各自断开。"""
    blocks, cur, start, fence = [], [], 0, False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fence = not fence
            if cur:
                blocks.append((start, "\n".join(cur)))
            cur = []
            continue
        if fence:
            continue
        if not line.strip() or _BLOCK_START.match(line):
            if cur:
                blocks.append((start, "\n".join(cur)))
            cur = []
            if not line.strip():
                continue
        if not cur:
            start = number
        cur.append(line)
    if cur:
        blocks.append((start, "\n".join(cur)))
    return blocks


def bad_bold(text):
    """[(行号, 附近的文字)]：按 CommonMark 规则开不了或合不上的 `**`。"""
    out = []
    for start, block in _blocks(text):
        # 行内代码里的星号不算；反引号本身留着，它是 `**` 旁边真实的字符
        block = _CODE_SPAN.sub(lambda m: m.group(1) + "x" * len(m.group(2)) + m.group(1),
                               block)
        runs = [m.start() for m in re.finditer(r"(?<!\*)\*\*(?!\*)", block)]
        for i, pos in enumerate(runs):
            before = block[pos - 1] if pos else None
            after = block[pos + 2] if pos + 2 < len(block) else None
            ok = _can_open(before, after) if i % 2 == 0 else _can_close(before, after)
            if not ok:
                line = start + block.count("\n", 0, pos)
                out.append((line, block[max(0, pos - 12):pos + 14].replace("\n", " ")))
    return out


@pytest.mark.parametrize("name", DOCS)
def test_bold_markers_render_as_bold(name):
    assert bad_bold((REPO / name).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("sample, bad", [
    ("直播中点**「换主播」**可以改听另一个主播。", True),       # 2026-09-29 审查抓到的写法
    ("直播中点「**换主播**」可以改听另一个主播。", False),
    ("开关在**「违禁词报警」**一行。", True),
    ("**违禁词报警默认是关的。** 开关在首页。", False),
    ("<a id=\"x\"></a>**Can colleagues watch?** Yes.", False),
    ("Click **Phone Viewing** in the top bar.", False),
    ("`a**b` is code, not bold.", False),
])
def test_the_check_knows_the_cjk_bracket_trap(sample, bad):
    assert bool(bad_bold(sample)) is bad
