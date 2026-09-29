#!/usr/bin/env python3
"""无损证明：把加了英文的文件「剥回」中文，再与基点逐节点比（CLAUDE.md 第二条在英文界面里的落法）。

M 系列提交只往中文旁边加英文。剥回去之后与基点一模一样，就证明这个提交里中文界面、
终端、审计一个字节都没变：

    python3 tools/i18n_strip_check.py --base origin/main                 # 默认：相对基点改过的界面源文件
    python3 tools/i18n_strip_check.py --base origin/main app/pipeline.py web/app.js

- Python：L(zh, en) → zh；LN(n, zh, …) → zh；of(x) → str(x)；中文恒等函数 f(x, …) → x；
  bimap(F, x) → F(x)；bimap(lambda s: BODY, x) → BODY（s 换成 x）；删掉对 i18n 的 import。比 ast.dump。
- JS：L(a, b) → a；LN(n, a, …) → a。比词法 token 流（注释本来就不算）。
- HTML：删掉 data-en* 属性和 <!-- i18n: … --> 注释，空白按 HTML 的规矩折叠。比 html.parser 事件流。

基点和新版本走同一套剥法。中文恒等函数登记在 tests/i18n_rules.py 的 ZH_IDENTITY_CALLS。
输出是一张 Markdown 表，贴进 PR 描述。退出码 0 = 全部一致，1 = 有文件不一致，2 = 用法或环境错误。
"""
import argparse
import ast
import copy
import difflib
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.stdio import harden_stdio                     # noqa: E402

# 重定向到文件时（Windows 上默认 ANSI 代码页 + strict）中文输出不该让检查中途崩掉：见 app/stdio.py
harden_stdio()

from tests import i18n_rules as rules                  # noqa: E402
from tools import i18n_pairs as pairs                  # noqa: E402

_WS = re.compile(r"\s+")
_KEEP_WS = {"pre", "textarea", "script", "style"}


# ---- Python ----------------------------------------------------------------------------

def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return head + "." + node.attr if head else None
    return None


class _Subst(ast.NodeTransformer):
    def __init__(self, name, value):
        self.name, self.value = name, value

    def visit_Name(self, node):
        if node.id == self.name and isinstance(node.ctx, ast.Load):
            return copy.deepcopy(self.value)
        return node


def _beta(lam, arg):
    """bimap(lambda s: BODY, x) → BODY，s 换成 x。只认单个普通参数的 lambda。"""
    a = lam.args
    if (len(a.args) != 1 or a.vararg or a.kwarg or a.kwonlyargs or a.defaults
            or getattr(a, "posonlyargs", [])):
        return None
    return _Subst(a.args[0].arg, arg).visit(copy.deepcopy(lam.body))


class _PyStripper(ast.NodeTransformer):
    def __init__(self):
        self.counts = Counter()

    def visit_ImportFrom(self, node):
        mod = node.module or ""
        if (node.level and mod == "i18n") or (not node.level and mod == "app.i18n"):
            self.counts["import"] += 1
            return None
        if (node.level == 1 and not mod) or (not node.level and mod == "app"):
            kept = [a for a in node.names if a.name != "i18n"]
            if len(kept) != len(node.names):
                self.counts["import"] += 1
                if not kept:
                    return None
                node.names = kept
        return node

    def visit_Import(self, node):
        kept = [a for a in node.names if a.name != "app.i18n"]
        if len(kept) != len(node.names):
            self.counts["import"] += 1
            if not kept:
                return None
            node.names = kept
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        name = _dotted(node.func)
        args = node.args
        if name in ("L", "i18n.L") and len(args) == 2 and not node.keywords:
            self.counts["L"] += 1
            return args[0]
        if name in ("LN", "i18n.LN") and len(args) == 4 and not node.keywords:
            self.counts["LN"] += 1
            return args[1]
        if name in ("of", "i18n.of") and len(args) == 1 and not node.keywords:
            self.counts["of"] += 1
            return ast.Call(func=ast.Name(id="str", ctx=ast.Load()), args=[args[0]], keywords=[])
        if name in ("bimap", "i18n.bimap") and len(args) == 2 and not node.keywords:
            fn, value = args
            if isinstance(fn, ast.Lambda):
                body = _beta(fn, value)
                if body is not None:
                    self.counts["bimap"] += 1
                    return body
            elif isinstance(fn, (ast.Name, ast.Attribute)):
                self.counts["bimap"] += 1
                return ast.Call(func=fn, args=[value], keywords=[])
        if name in rules.ZH_IDENTITY_CALLS and args:
            self.counts[name] += 1
            return args[0]
        return node


def strip_python(src):
    """→ (剥完的 AST, 各种剥法的次数)。"""
    stripper = _PyStripper()
    tree = stripper.visit(ast.parse(src))
    return tree, stripper.counts


def _stmt_lists(node):
    """一条复合语句里的各个语句块：body、orelse、finalbody、每个 except 的 body。"""
    lists = [getattr(node, f, None) for f in ("body", "orelse", "finalbody")]
    lists += [h.body for h in getattr(node, "handlers", [])]
    return [x for x in lists if isinstance(x, list)]


def _first_py_difference(base, new):
    """两棵树第一处不同的语句：(基点行号, 新行号, 基点源码, 新源码)。复合语句的头相同就往里找。"""
    b_body, n_body = base.body, new.body
    while True:
        sm = difflib.SequenceMatcher(a=[ast.dump(s) for s in b_body],
                                     b=[ast.dump(s) for s in n_body], autojunk=False)
        op = next((o for o in sm.get_opcodes() if o[0] != "equal"), None)
        if op is None:
            return None
        tag, i1, i2, j1, j2 = op
        b = b_body[i1] if i1 < len(b_body) else None
        n = n_body[j1] if j1 < len(n_body) else None
        inner = None
        if tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1 and type(b) is type(n):
            b_lists, n_lists = _stmt_lists(b), _stmt_lists(n)
            if len(b_lists) == len(n_lists):
                inner = next(((x, y) for x, y in zip(b_lists, n_lists)
                              if [ast.dump(s) for s in x] != [ast.dump(s) for s in y]), None)
        if inner is None:
            return (getattr(b, "lineno", "-"), getattr(n, "lineno", "-"),
                    _clip(ast.unparse(b)) if b is not None else "（无）",
                    _clip(ast.unparse(n)) if n is not None else "（无）")
        b_body, n_body = inner


def _clip(text, limit=300):
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " …"


def compare_python(base_src, new_src):
    """→ (一致吗, 剥法计数, 不一致时的说明)。"""
    base, _ = strip_python(base_src)
    new, counts = strip_python(new_src)
    if ast.dump(base) == ast.dump(new):
        return True, counts, ""
    diff = _first_py_difference(base, new)
    if diff is None:
        return False, counts, "模块层以外的差异（docstring 或 import 顺序？）"
    return False, counts, "基点第 {} 行：{}\n    新版第 {} 行：{}".format(diff[0], diff[2], diff[1], diff[3])


# ---- JS --------------------------------------------------------------------------------

def strip_js(tokens, counts=None):
    counts = Counter() if counts is None else counts
    out, i = [], 0
    while i < len(tokens):
        kind = pairs.js_call_at(tokens, i)
        if kind:
            args, end = pairs.js_args(tokens, i + 1)
            if len(args) == (2 if kind == "L" else 4):
                counts[kind] += 1
                out.extend(strip_js(args[0] if kind == "L" else args[1], counts))
                i = end + 1
                continue
        out.append(tokens[i])
        i += 1
    return out


def compare_js(base_src, new_src):
    counts = Counter()
    base = strip_js(pairs.lex_js(base_src)[0])
    new = strip_js(pairs.lex_js(new_src)[0], counts)
    b_key = [(t.kind, t.value) for t in base]
    n_key = [(t.kind, t.value) for t in new]
    if b_key == n_key:
        return True, counts, ""
    sm = difflib.SequenceMatcher(a=b_key, b=n_key, autojunk=False)
    _, i1, i2, j1, j2 = next(o for o in sm.get_opcodes() if o[0] != "equal")
    def show(toks, a, b):
        return " ".join(t.value for t in toks[a:b])[:300] or "（无）"

    return False, counts, "基点第 {} 行：{}\n    新版第 {} 行：{}".format(
        base[i1].line if i1 < len(base) else "-", show(base, i1, max(i2, i1 + 1)),
        new[j1].line if j1 < len(new) else "-", show(new, j1, max(j2, j1 + 1)))


# ---- HTML ------------------------------------------------------------------------------

class _Events(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events, self.lines, self.dropped = [], [], Counter()

    def _add(self, event):
        self.events.append(event)
        self.lines.append(self.getpos()[0])

    def _attrs(self, attrs):
        kept = [(k, v) for k, v in attrs if not k.startswith("data-en")]
        self.dropped["data-en*"] += len(attrs) - len(kept)
        return tuple(kept)

    def handle_starttag(self, tag, attrs):
        self._add(("start", tag, self._attrs(attrs)))

    def handle_startendtag(self, tag, attrs):
        self._add(("startend", tag, self._attrs(attrs)))

    def handle_endtag(self, tag):
        self._add(("end", tag))

    def handle_data(self, data):
        self._add(("data", data))

    def handle_comment(self, data):
        if re.match(r"\s*i18n:", data):
            self.dropped["注记"] += 1
            return
        self._add(("comment", data))

    def handle_decl(self, decl):
        self._add(("decl", decl))

    def handle_pi(self, data):
        self._add(("pi", data))

    def unknown_decl(self, data):
        self._add(("decl", data))


def html_events(src):
    """→ ([(事件, 行号)], 删掉的计数)。相邻文字合并；pre/textarea/script/style 之外的空白折成一个空格。"""
    parser = _Events()
    parser.feed(src)
    parser.close()
    merged = []
    for event, line in zip(parser.events, parser.lines):
        if event[0] == "data" and merged and merged[-1][0][0] == "data":
            merged[-1] = (("data", merged[-1][0][1] + event[1]), merged[-1][1])
        else:
            merged.append((event, line))
    out, raw = [], []
    for event, line in merged:
        if event[0] == "start" and event[1] in _KEEP_WS:
            raw.append(event[1])
        elif event[0] == "end" and raw and raw[-1] == event[1]:
            raw.pop()
        elif event[0] == "data" and not raw:
            event = ("data", _WS.sub(" ", event[1]))
        out.append((event, line))
    return out, parser.dropped


def compare_html(base_src, new_src):
    base, _ = html_events(base_src)
    new, counts = html_events(new_src)
    b_key, n_key = [e for e, _ in base], [e for e, _ in new]
    if b_key == n_key:
        return True, counts, ""
    sm = difflib.SequenceMatcher(a=b_key, b=n_key, autojunk=False)
    _, i1, _, j1, _ = next(o for o in sm.get_opcodes() if o[0] != "equal")
    def at(seq, k):
        return "第 {} 行：{!r}".format(seq[k][1], seq[k][0])[:300] if k < len(seq) else "（无）"

    return False, counts, "基点{}\n    新版{}".format(at(base, i1), at(new, j1))


COMPARE = {".py": compare_python, ".js": compare_js, ".html": compare_html}


# ---- 命令行 -----------------------------------------------------------------------------

def check_file(repo, base, path):
    """→ (一致吗, 剥法计数, 说明)。基点里没有、类型不认识都算不一致。"""
    compare = COMPARE.get(Path(path).suffix)
    if compare is None:
        return False, Counter(), "不认识的文件类型（只比 .py / .js / .html）"
    old = pairs.git_show(repo, base, path)
    if old is None:
        return False, Counter(), "基点 {} 里没有这个文件：新文件不适用剥回检查".format(base)
    try:
        new = (Path(repo) / path).read_text(encoding="utf-8")
    except OSError as exc:
        return False, Counter(), "读不了：{}".format(exc)
    try:
        return compare(old, new)
    except (SyntaxError, ValueError) as exc:
        return False, Counter(), "解析失败：{}".format(exc)


def main(argv=None):
    ap = argparse.ArgumentParser(description="把中英对剥回中文，与基点逐节点比")
    ap.add_argument("--base", default="origin/main", help="基点（git 版本号），默认 origin/main")
    ap.add_argument("--repo", default=str(REPO_ROOT), help="仓库根目录，默认本仓库")
    ap.add_argument("files", nargs="*", help="仓库相对路径；默认：相对基点改过的界面源文件")
    args = ap.parse_args(argv)
    repo = Path(args.repo)
    if not pairs.git_has_commit(repo, args.base):
        print("找不到基点 {}".format(args.base))
        return 2
    try:
        files = args.files or pairs.changed_files(repo, args.base)
    except RuntimeError as exc:
        print(exc)
        return 2
    if not files:
        print("相对 {} 没有改过的界面源文件".format(args.base))
        return 0
    print("| 文件 | 剥掉 | 结果 |\n|---|---|---|")
    failed = []
    for path in files:
        ok, counts, why = check_file(repo, args.base, path)
        stripped = " ".join("{}×{}".format(k, v) for k, v in sorted(counts.items())) or "—"
        print("| `{}` | {} | {} |".format(path, stripped, "与基点一致" if ok else "**不一致**"))
        if not ok:
            failed.append((path, why))
    for path, why in failed:
        print("\n{}：\n    {}".format(path, why))
    print("\n基点 {}：{} 个文件，{} 个不一致".format(args.base, len(files), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
