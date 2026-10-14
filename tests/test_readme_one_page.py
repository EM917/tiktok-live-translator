"""README.md 一页两种语言：英文在前、中文在后，两边按小节互相跳转。

跳转全靠显式锚点。GitHub 给重名的标题 id 加 -1、-2，显式写的 `<a id>` 却原样保留，
而片段链接落在文档里第一个同 id 的元素上——合并成一页之后，只要中文那一半有个 id 和英文那一半
重名，中文的链接就会悄悄落到英文那一半去。程序不报错，CI 也不报错，只有读的人发现。
所以这里把三件事钉住：两种语言的小节一一配对；整页没有重复的 id；每个链接落在该落的那一半。
"""
import re

from tests.readme_parts import (REPO, ZH_MARK, first_line_with_id, github_slug, readme_text,
                                scan_page)

# 旧中文版的地址不能再被引用：它现在只是个跳转页，跳转页没有那些锚点
OLD_ZH_README = "README.zh" + "-CN.md"
SCANNED_DIRS = ["docs", "comment_guard", "brands", "app", "tools", "web"]
SCANNED_SUFFIXES = {".md", ".py", ".js", ".html"}

_EN_JUMP = re.compile(r"^<sub>\[中文\]\(#zh-([a-z0-9-]+)\)</sub>$")
_ZH_JUMP = re.compile(r"^<sub>\[English\]\(#en-([a-z0-9-]+)\)</sub>$")


def _page():
    text = readme_text()
    lines = text.splitlines()
    zh_line = next(n for n, line in enumerate(lines, 1) if line == ZH_MARK)
    ids, links, headings = scan_page(text)
    return lines, zh_line, ids, links, headings


def _sections(lines, headings, lo, hi, lang, jump):
    """半页里每个二级标题：(标题, 标题前的锚点 slug, 标题后的跳转 slug)。找不到的写 None。"""
    anchor = re.compile(r'^<a id="%s-([a-z0-9-]+)"></a>$' % lang)
    out = []
    for level, text, number in headings:
        if level != 2 or not lo <= number < hi:
            continue
        before = anchor.match(lines[number - 3]) if number >= 3 and lines[number - 2] == "" else None
        after = jump.match(lines[number + 1]) if lines[number] == "" and number + 1 < len(lines) else None
        out.append((text, before and before.group(1), after and after.group(1)))
    return out


def test_the_two_languages_pair_section_by_section():
    lines, zh_line, _ids, _links, headings = _page()
    en = _sections(lines, headings, 1, zh_line, "en", _EN_JUMP)
    zh = _sections(lines, headings, zh_line, len(lines) + 1, "zh", _ZH_JUMP)

    for lang, sections in (("English", en), ("Chinese", zh)):
        assert sections, lang
        broken = [t for t, before, after in sections if not before or not after]
        assert not broken, "%s 小节缺锚点或跳转行：%s" % (lang, broken)
        assert all(before == after for _t, before, after in sections), \
            "%s 小节的锚点和跳转行 slug 对不上" % lang

    en_slugs = [before for _t, before, _a in en]
    zh_slugs = [before for _t, before, _a in zh]
    assert len(set(en_slugs)) == len(en_slugs)
    assert en_slugs == zh_slugs, (
        "两边的小节要一一配对、顺序相同。只有英文有：%s；只有中文有：%s"
        % (sorted(set(en_slugs) - set(zh_slugs)), sorted(set(zh_slugs) - set(en_slugs))))
    # 配对的 slug 取自英文标题；改了英文标题的话，这里提醒把锚点一起改
    for (text, slug, _a), (_zt, zslug, _b) in zip(en, zh):
        assert slug == zslug
        assert slug == github_slug(text).replace("--", "-"), (text, slug)


def test_no_id_is_used_twice_on_the_page():
    _lines, _zh_line, ids, _links, _headings = _page()
    seen, dup = {}, {}
    for found, number, _kind in ids:
        if found in seen:
            dup.setdefault(found, [seen[found]]).append(number)
        seen.setdefault(found, number)
    assert dup == {}, "整页重名的 id（行号）：%s" % dup


def test_every_link_lands_in_the_half_it_means_to():
    _lines, zh_line, ids, links, _headings = _page()
    wrong, missing = [], []
    for target, number in links:
        here = "en" if number < zh_line else "zh"
        landed = first_line_with_id(ids, target)
        if landed is None:
            missing.append((number, target))
            continue
        there = "en" if landed < zh_line else "zh"
        # 带 zh / zh- 前缀的目标在中文那一半，带 en / en- 前缀的在英文那一半（英文页的 [中文](#zh-…) 和
        # 中文页自己的 #zh-phone-viewing 都是这样）；不带前缀的标题 slug 和旧锚点，一律要留在链接所在的那一半
        prefix = target.split("-")[0]
        want = prefix if prefix in ("zh", "en") else here
        if there != want:
            wrong.append((number, target, "落在 %s 半页，应该在 %s" % (there, want)))
    assert missing == [], "找不到目标的链接（行号, 目标）：%s" % missing
    assert wrong == [], "链接落点不对（行号, 目标, 原因）：%s" % wrong


def test_the_language_lines_jump_both_ways():
    text = readme_text()
    assert "**Language / 语言：English | [中文](#zh)**" in text
    assert "**Language / 语言：[English](#en) | 中文**" in text
    assert text.count('<a id="en"></a>') == 1 and text.count(ZH_MARK) == 1


def test_the_old_chinese_readme_is_only_a_pointer():
    stub = (REPO / OLD_ZH_README).read_text(encoding="utf-8")
    assert "](README.md#zh)" in stub
    assert len(stub.splitlines()) <= 10, "旧的中文 README 现在只该是个跳转页，正文在 README.md 里"


def test_nothing_points_at_the_old_chinese_readme():
    stale = []
    files = [REPO / "README.md", REPO / "main.py"]
    for name in SCANNED_DIRS:
        files += [p for p in (REPO / name).rglob("*") if p.is_file() and p.suffix in SCANNED_SUFFIXES]
    for path in files:
        if OLD_ZH_README in path.read_text(encoding="utf-8", errors="replace"):
            stale.append(str(path.relative_to(REPO)))
    assert stale == [], "这些文件还在提旧的中文 README，改成 README.md#zh-…：%s" % stale
