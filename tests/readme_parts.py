"""README.md 是一页两种语言：英文说明在前，从 `<a id="zh"></a>` 起是中文说明。

README.zh-CN.md 只剩一个跳转页（见 tests/test_readme_one_page.py）。读 README 的测试用这里的
`readme_part()` 取其中一种语言的那一半，别再各自去读 README.zh-CN.md。

中文那一半前面垫了和英文那一半一样多的空行，所以 `bad_bold` 之类按行号报告的检查，
报出来的仍是 README.md 里的真实行号。
"""
import re
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ZH_MARK = '<a id="zh"></a>'

_HEADING = re.compile(r"^(#{1,6}) +(.*?) *#*$")
_EXPLICIT_ID = re.compile(r'<a\s+(?:id|name)="([^"]+)"')
_FRAGMENT_LINK = re.compile(r"\]\(#([^)\s]+)\)")


def readme_text():
    return (REPO / "README.md").read_text(encoding="utf-8")


def split_readme(text=None):
    """(英文那一半, 中文那一半)。标记必须恰好出现一次，否则两半的边界无从说起。"""
    text = readme_text() if text is None else text
    assert text.count(ZH_MARK) == 1, "README.md 里 %s 必须恰好出现一次" % ZH_MARK
    en, zh = text.split(ZH_MARK, 1)
    return en, ZH_MARK + zh


def readme_part(lang):
    """lang 是 "en" 或 "zh"。中文那一半前面补足空行，行号和 README.md 对得上。"""
    en, zh = split_readme()
    if lang == "en":
        return en
    return "\n" * en.count("\n") + zh


def github_slug(heading):
    """GitHub 给标题生成的锚点：小写，去掉标点和符号，空格变连字符。"""
    heading = re.sub(r"`([^`]*)`", r"\1", heading)
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    heading = heading.replace("**", "").replace("*", "")
    out = []
    for ch in heading.lower():
        if ch in " -":
            out.append("-")
        elif unicodedata.category(ch)[0] in "LMN" or unicodedata.category(ch) == "Pc":
            out.append(ch)
    return "".join(out)


def scan_page(text):
    """[(id, 行号, 种类)]、[(链接目标, 行号)]、[(级别, 标题, 行号)]，都按文档顺序。

    id 的规则照 GitHub：标题的 id 是 github_slug，重复的依次加 -1、-2；显式写的
    `<a id="x">` 原样；片段链接跳到第一个同 id 的元素——所以两半里重复的 id，
    后一半的链接会落到前一半去，这正是要查的事。"""
    ids, links, headings, seen, fence = [], [], [], {}, False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        match = _HEADING.match(line)
        if match:
            base = github_slug(match.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            ids.append((base if count == 0 else "%s-%d" % (base, count), number, "heading"))
            headings.append((len(match.group(1)), match.group(2), number))
        ids += [(m.group(1), number, "anchor") for m in _EXPLICIT_ID.finditer(line)]
        links += [(m.group(1), number) for m in _FRAGMENT_LINK.finditer(line)]
    return ids, links, headings


def first_line_with_id(ids, target):
    for found, number, _kind in ids:
        if found == target:
            return number
    return None
