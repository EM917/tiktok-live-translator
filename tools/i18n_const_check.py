#!/usr/bin/env python3
"""纯命名重构的无损证明：把重复的界面字面量收成常量，别的一个节点都没变（spec 的 C3a 提交用）。

收常量会改变每个文件里中文字面量的多重集，所以不能用「字面量不变」来判。这里逐节点比
基点与新版本的 AST（JS 比 token 流），只允许三种差异：

1. 基点的一个字符串字面量，换成了名字、属性或下标（APP_NAME、selfcheck.NAMES["asr"]），
   而这个表达式**按 AST 解析**（不 import：main.py 一 import 就会 execv）得到的值与原字面量相等；
2. 新增的模块级常量赋值（名字在基点的模块层没有出现过）；
3. 新增或扩充的 import（基点有的 import 一个都不能少）。

其余节点必须逐个相等。跨文件的常量按工作区里的新版本解析。

    python3 tools/i18n_const_check.py --base origin/main                  # 默认：相对基点改过的界面源文件
    python3 tools/i18n_const_check.py --base origin/main app/pipeline.py main.py

输出「调用点 → 常量」映射表，贴进 PR。退出码 0 = 只有上面三种差异，1 = 有别的改动，2 = 用法或环境错误。
"""
import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.stdio import harden_stdio                     # noqa: E402

# 重定向到文件时（Windows 上默认 ANSI 代码页 + strict）中文输出不该让检查中途崩掉：见 app/stdio.py
harden_stdio()

from tools import i18n_pairs as pairs                  # noqa: E402

MISSING = object()


class _Module:
    def __init__(self, path):
        self.path = path


class Resolver:
    """不 import，只读 AST，把名字/属性/下标解析成字面量值。读不出来就是 MISSING。"""

    def __init__(self, root, sources=None):
        self.root = Path(root)
        self.sources = dict(sources or {})           # 仓库相对路径 → 源码（给测试和基点用）
        self._info = {}

    def use(self, path, src):
        """指定某个文件按这份源码解析（比对的新版本、测试夹具）。"""
        self.sources[path] = src
        self._info.pop(path, None)

    def _source(self, path):
        if path in self.sources:
            return self.sources[path]
        try:
            return (self.root / path).read_text(encoding="utf-8")
        except OSError:
            return None

    def info(self, path):
        """→ (模块层常量 {名字: 节点}, import 进来的名字 {名字: _Module 或 (模块路径, 属性)})。"""
        if path not in self._info:
            consts, names = {}, {}
            src = self._source(path)
            tree = ast.parse(src) if src is not None else ast.Module(body=[], type_ignores=[])
            for stmt in tree.body:
                if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name)):
                    consts[stmt.targets[0].id] = stmt.value
            for node in ast.walk(tree):                     # 函数里延迟 import 的也算（main.py 常这样写）
                if isinstance(node, ast.ImportFrom):
                    base = self._module_path(path, node.module, node.level)
                    for alias in node.names:
                        target = self._module_path(path, ".".join(filter(None, [node.module, alias.name])),
                                                   node.level)
                        if target is not None and self._source(target) is not None:
                            names[alias.asname or alias.name] = _Module(target)
                        elif base is not None:
                            names[alias.asname or alias.name] = (base, alias.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        target = self._module_path(path, alias.name, 0)
                        if alias.asname and target is not None:
                            names[alias.asname] = _Module(target)
            self._info[path] = (consts, names)
        return self._info[path]

    def _module_path(self, path, module, level):
        parts = (module or "").split(".") if module else []
        if level:
            anchor = Path(path).parent
            for _ in range(level - 1):
                anchor = anchor.parent
            rel = anchor.joinpath(*parts) if parts else anchor
        else:
            rel = Path(*parts) if parts else None
        if rel is None:
            return None
        return rel.with_suffix(".py").as_posix()

    def value(self, node, path, depth=0):
        if depth > 20:
            return MISSING
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.Tuple, ast.List)):
            items = [self.value(e, path, depth + 1) for e in node.elts]
            return MISSING if any(v is MISSING for v in items) else tuple(items)
        if isinstance(node, ast.Dict):
            if any(k is None for k in node.keys):
                return MISSING
            keys = [self.value(k, path, depth + 1) for k in node.keys]
            vals = [self.value(v, path, depth + 1) for v in node.values]
            if any(x is MISSING for x in keys + vals):
                return MISSING
            try:
                return dict(zip(keys, vals))
            except TypeError:
                return MISSING
        if isinstance(node, ast.Name):
            return self.name(node.id, path, depth)
        if isinstance(node, ast.Attribute):
            owner = self.value(node.value, path, depth + 1)
            return self.name(node.attr, owner.path, depth) if isinstance(owner, _Module) else MISSING
        if isinstance(node, ast.Subscript):
            owner = self.value(node.value, path, depth + 1)
            key = self.value(node.slice, path, depth + 1)
            try:
                return owner[key] if isinstance(owner, (dict, tuple)) and key is not MISSING else MISSING
            except (KeyError, IndexError, TypeError):
                return MISSING
        return MISSING

    def name(self, ident, path, depth):
        consts, names = self.info(path)
        if ident in consts:
            return self.value(consts[ident], path, depth + 1)
        target = names.get(ident)
        if isinstance(target, _Module):
            return target
        if isinstance(target, tuple):
            return self.name(target[1], target[0], depth + 1)
        return MISSING


class _Result:
    """一个文件的比对结果：mapping = [(行号, 原字面量, 常量表达式)]，另记新增的常量与 import。"""

    def __init__(self):
        self.mapping, self.added_consts, self.added_imports = [], [], []


class _Mismatch(Exception):
    def __init__(self, base, new, why):
        super().__init__(why)
        self.base, self.new, self.why = base, new, why


def _imports(stmts):
    got = set()
    for s in stmts:
        if isinstance(s, ast.ImportFrom):
            got.update((s.level, s.module, a.name, a.asname) for a in s.names)
        elif isinstance(s, ast.Import):
            got.update((0, None, a.name, a.asname) for a in s.names)
    return got


def _is_import(stmt):
    return isinstance(stmt, (ast.Import, ast.ImportFrom))


class PyComparer(_Result):
    def __init__(self, path, base_tree, resolver):
        super().__init__()
        self.path, self.resolver = path, resolver
        self.base_names = {t.id for s in base_tree.body if isinstance(s, (ast.Assign, ast.AnnAssign))
                           for t in (s.targets if isinstance(s, ast.Assign) else [s.target])
                           if isinstance(t, ast.Name)}
        self.base_names |= {s.name for s in base_tree.body
                            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        self.added_names = set()

    def node(self, b, n):
        if isinstance(b, ast.Constant) and isinstance(b.value, str) and not isinstance(n, ast.Constant):
            if isinstance(n, (ast.Name, ast.Attribute, ast.Subscript)):
                value = self.resolver.value(n, self.path)
                if value is not MISSING and type(value) is str and value == b.value:
                    self.mapping.append((n.lineno, b.value, ast.unparse(n)))
                    return
                raise _Mismatch(b, n, "{} 解析出来的值与原字面量不同".format(ast.unparse(n)))
            raise _Mismatch(b, n, "字面量换成了别的表达式")
        if type(b) is not type(n):
            raise _Mismatch(b, n, "节点类型不同")
        for field in b._fields:
            bv, nv = getattr(b, field, None), getattr(n, field, None)
            if isinstance(bv, list) and bv and isinstance(bv[0], ast.stmt):
                self.body(bv, nv, module=False)
            elif isinstance(bv, list):
                if not isinstance(nv, list) or len(bv) != len(nv):
                    raise _Mismatch(b, n, "{} 的个数不同".format(field))
                for x, y in zip(bv, nv):
                    self._item(x, y, b, n)
            else:
                self._item(bv, nv, b, n)

    def _item(self, x, y, b, n):
        if isinstance(x, ast.AST):
            if not isinstance(y, ast.AST):
                raise _Mismatch(b, n, "节点类型不同")
            self.node(x, y)
        elif x != y or type(x) is not type(y):
            raise _Mismatch(b, n, "值不同：{!r} ≠ {!r}".format(x, y))

    def _new_const(self, stmt):
        """新增的模块级常量：一个名字只赋一次，基点的模块层没有它，值按 AST 解析得出来。"""
        return (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id not in self.base_names | self.added_names
                and self.resolver.value(stmt.value, self.path) is not MISSING)

    def body(self, bs, ns, module):
        if not isinstance(ns, list):
            raise _Mismatch(bs[0], None, "语句块不见了")
        lost = _imports(bs) - _imports(ns)
        if lost:
            raise _Mismatch(next(s for s in bs if _is_import(s)), None,
                            "基点的 import 不见了：{}".format(sorted(str(x) for x in lost)))
        b_rest = [s for s in bs if not _is_import(s)]
        n_rest = []
        for s in ns:
            if _is_import(s):
                if any(x not in _imports(bs) for x in _imports([s])):
                    self.added_imports.append((s.lineno, ast.unparse(s)))
            else:
                n_rest.append(s)
        i = 0
        for s in n_rest:
            if i < len(b_rest):
                saved = (len(self.mapping), len(self.added_consts), len(self.added_imports))
                try:
                    self.node(b_rest[i], s)
                    i += 1
                    continue
                except _Mismatch as exc:
                    del self.mapping[saved[0]:]
                    del self.added_consts[saved[1]:]
                    del self.added_imports[saved[2]:]
                    first = exc
            else:
                first = _Mismatch(None, s, "基点这里没有语句")
            if module and self._new_const(s):
                self.added_consts.append((s.lineno, ast.unparse(s)))
                self.added_names.add(s.targets[0].id)
                continue
            raise first
        if i < len(b_rest):
            raise _Mismatch(b_rest[i], None, "基点的这条语句在新版里没有了")


def compare_python(path, base_src, new_src, resolver):
    """→ (一致吗, PyComparer, 不一致的说明)。本文件的名字按 new_src 解析。"""
    resolver.use(path, new_src)
    base_tree, new_tree = ast.parse(base_src), ast.parse(new_src)
    cmp = PyComparer(path, base_tree, resolver)
    try:
        cmp.body(base_tree.body, new_tree.body, module=True)
    except _Mismatch as exc:
        where = "基点第 {} 行 {}\n    新版第 {} 行 {}".format(
            getattr(exc.base, "lineno", "-"), _clip(exc.base), getattr(exc.new, "lineno", "-"),
            _clip(exc.new))
        return False, cmp, "{}：\n    {}".format(exc.why, where)
    return True, cmp, ""


def _clip(node, limit=200):
    if node is None:
        return "（无）"
    text = ast.unparse(node) if isinstance(node, ast.AST) else str(node)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " …"


# ---- JS：只认「字符串 → 顶层 var 常量」这一种替换，外加新增的顶层 var 常量 ------------------

def _js_consts(toks):
    """顶层 `var|let|const NAME = "字面量";` → ({名字: 值}, [(起, 止) 下标区间])。"""
    consts, spans, depth = {}, [], 0
    for i, t in enumerate(toks):
        if t.kind == "punc" and t.value in "([{":
            depth += 1
        elif t.kind == "punc" and t.value in ")]}":
            depth -= 1
        elif (depth == 0 and t.kind == "name" and t.value in ("var", "let", "const")
              and i + 4 < len(toks) and toks[i + 1].kind == "name" and toks[i + 2].value == "="
              and toks[i + 3].kind == "str" and toks[i + 4].value == ";"):
            consts[toks[i + 1].value] = pairs.js_text(toks[i + 3])
            spans.append((i, i + 5))
    return consts, spans


def compare_js(path, base_src, new_src, resolver=None):
    base, _ = pairs.lex_js(base_src)
    new, _ = pairs.lex_js(new_src)
    base_consts, _ = _js_consts(base)
    new_consts, spans = _js_consts(new)
    cmp = _Result()
    added = set()
    for a, b in spans:
        name = new[a + 1].value
        if name not in base_consts:
            added.update(range(a, b))
            cmp.added_consts.append((new[a].line, " ".join(t.value for t in new[a:b])))
    new = [t for k, t in enumerate(new) if k not in added]
    i = j = 0
    while i < len(base) and j < len(new):
        b, n = base[i], new[j]
        if (b.kind, b.value) == (n.kind, n.value):
            i, j = i + 1, j + 1
            continue
        if (b.kind == "str" and n.kind == "name" and n.value in new_consts
                and new_consts[n.value] == pairs.js_text(b)):
            cmp.mapping.append((n.line, pairs.js_text(b), n.value))
            i, j = i + 1, j + 1
            continue
        return False, cmp, "基点第 {} 行 {}\n    新版第 {} 行 {}".format(b.line, b.value, n.line, n.value)
    if i < len(base) or j < len(new):
        rest = base[i] if i < len(base) else new[j]
        return False, cmp, "第 {} 行之后两边长度不同：{}".format(rest.line, rest.value)
    return True, cmp, ""


COMPARE = {".py": compare_python, ".js": compare_js}


def main(argv=None):
    ap = argparse.ArgumentParser(description="证明一次提交只是把字面量收成了常量")
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
    resolver = Resolver(repo)
    rows, failed = [], []
    for path in files:
        compare = COMPARE.get(Path(path).suffix)
        old = pairs.git_show(repo, args.base, path)
        if compare is None or old is None:
            failed.append((path, "只比基点里已有的 .py / .js 文件"))
            continue
        try:
            new = (repo / path).read_text(encoding="utf-8")
            ok, cmp, why = compare(path, old, new, resolver)
        except (OSError, SyntaxError, ValueError) as exc:
            failed.append((path, "读不了或解析失败：{}".format(exc)))
            continue
        for line, literal, expr in cmp.mapping:
            rows.append((path, line, literal, expr))
        for line, text in cmp.added_consts:
            print("{}:{}  新增常量  {}".format(path, line, _short(text)))
        for line, text in cmp.added_imports:
            print("{}:{}  新增 import  {}".format(path, line, _short(text)))
        if not ok:
            failed.append((path, why))
    print("\n| 调用点 | 原字面量 | 常量 |\n|---|---|---|")
    for path, line, literal, expr in rows:
        print("| `{}:{}` | {} | `{}` |".format(path, line, _short(repr(literal)), expr))
    for path, why in failed:
        print("\n{}：{}".format(path, why))
    print("\n基点 {}：{} 个文件，{} 处字面量换成了常量，{} 个文件有别的改动".format(
        args.base, len(files), len(rows), len(failed)))
    return 1 if failed else 0


def _short(text, limit=80):
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[:limit] + " …"


if __name__ == "__main__":
    sys.exit(main())
