#!/usr/bin/env python3
"""抽出全仓的中英对，并跑英文界面的几道静态闸（docs/i18n-style.md；规则表在 tests/i18n_rules.py）。

- G2 成对检查：两臂非空、占位符一致（R2）、英文臂无 CJK 且 emoji 与中文相同（R3）、
  按钮名引用（R6）、产品名映射、禁用词两级（CLAUDE.md 第八条；第八条家族的中文臂也查）。
- G3 同中文→同英文：同一句中文在全仓只许有一种英文。
- G4 覆盖：带 `i18n: done` 标记的文件里，界面中文必须都成对；Python 另查 R11 的四种会丢英文的写法。
  全量（Z0 起）：tests/i18n_rules.py 的 UI_FILES 与标了 done 的文件一一对应，清单外的文件不许有
  没登记（NON_UI_CHINESE）的中文字面量。
- G5 静态页模拟：按 web/i18n.js 的 applyStatic 规则把 data-en* 代进去，看还剩不剩中文。

三种源：Python 走 AST；JS 走下面这个小词法器（认得正则字面量、模板串）；HTML 走 html.parser。
tools/i18n_strip_check.py 与 tools/i18n_const_check.py 也用这里的词法器和解析器。

    python3 tools/i18n_pairs.py            # 全仓：统计 + G2/G3/G4 违例，有违例退出码 1
    python3 tools/i18n_pairs.py --list     # 另外逐条列出所有对（TSV）

只依赖标准库。退出码 0 = 没有违例，1 = 有违例。
"""
import argparse
import ast
import io
import re
import string
import subprocess
import sys
import tokenize
from bisect import bisect_right
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.stdio import harden_stdio                     # noqa: E402

# 重定向到文件时（Windows 上默认 ANSI 代码页 + strict）中文输出不该让检查中途崩掉：见 app/stdio.py
harden_stdio()

from app.i18n import CJK                               # noqa: E402
from tests import i18n_rules as rules                  # noqa: E402

I18N_ATTRS = ("title", "aria-label", "placeholder", "alt")      # 与 web/i18n.js 的 I18N_ATTRS 相同
EN_ATTRS = ("data-en", "data-en-html") + tuple("data-en-" + a for a in I18N_ATTRS)
EMOJI = re.compile("[\u2600-\u27bf\u2b00-\u2bff\U0001f000-\U0001faff]")
_NOTE = re.compile(r"i18n:\s*([\w=]+)")


class Violation(tuple):
    """(path, line, rule, message)。按这个顺序排序、打印。G4 的违例另在 .text 上带着那个中文字面量
    的全文（message 里只有前 40 个字）：清单外文件的登记（rules.NON_UI_CHINESE）按全文对。"""

    def __new__(cls, path, line, rule, message, text=None):
        obj = tuple.__new__(cls, (path, line, rule, message))
        obj.text = text
        return obj

    def __str__(self):
        return "{}:{}  [{}] {}".format(*self)


# =========================================================================================
# JS 词法器
# =========================================================================================

class JSLexError(ValueError):
    pass


class Tok:
    """kind: name / num / str / tmpl / regex / punc。value 是源码原文；模板另有 parts（文字段原文）与 exprs（${} 里的源码）。"""
    __slots__ = ("kind", "value", "start", "end", "line", "parts", "exprs")

    def __init__(self, kind, value, start, end, parts=None, exprs=None):
        self.kind, self.value, self.start, self.end = kind, value, start, end
        self.line = 0
        self.parts, self.exprs = parts or [], exprs or []

    def __repr__(self):
        return "Tok({}, {!r})".format(self.kind, self.value)


_JS_PUNCT = sorted(
    [">>>=", "...", "===", "!==", "**=", "<<=", ">>=", ">>>", "&&=", "||=", "??=", "=>", "==",
     "!=", "<=", ">=", "&&", "||", "??", "?.", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=",
     "|=", "^=", "**", "<<", ">>"] + list("{}()[];,<>+-*/%&|^!~?:=.@#"),
    key=len, reverse=True)
_JS_ID = re.compile("[A-Za-z_$\u0080-\uffff][\\w$\u0080-\uffff]*")
_JS_NUM = re.compile(r"(?:0[xXbBoO][\da-fA-F_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?)n?")
_JS_SPACE = " \t\r\n\v\f\ufeff\u00a0\u2028\u2029"
_REGEX_AFTER = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw",
                "case", "do", "else", "yield", "await"}
JS_KEYWORDS = {"true", "false", "null", "undefined", "typeof", "new", "void", "delete",
               "instanceof", "in", "of", "function", "return", "var", "let", "const", "if",
               "else"}


def _scan_string(src, i):
    quote, j, n = src[i], i + 1, len(src)
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 3 if src.startswith("\r\n", j + 1) else 2
        elif ch == quote:
            return j + 1
        elif ch == "\n":
            break
        else:
            j += 1
    raise JSLexError("字符串没有闭合（第 {} 个字符起）".format(i))


def _scan_regex(src, i):
    j, n, in_class = i + 1, len(src), False
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "\n":
            break
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "/":
            j += 1
            while j < n and src[j].isalpha() and src[j].isascii():
                j += 1
            return j
        j += 1
    raise JSLexError("正则字面量没有闭合（第 {} 个字符起）".format(i))


def _scan_template(src, i):
    j, n, parts, exprs, seg = i + 1, len(src), [], [], i + 1
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
        elif ch == "`":
            parts.append(src[seg:j])
            return j + 1, parts, exprs
        elif src.startswith("${", j):
            parts.append(src[seg:j])
            _, _, k = _lex(src, j + 2, nested=True)
            exprs.append(src[j + 2:k])
            j = seg = k + 1
        else:
            j += 1
    raise JSLexError("模板字符串没有闭合（第 {} 个字符起）".format(i))


def _regex_allowed(toks):
    if not toks:
        return True
    t = toks[-1]
    if t.kind == "name":
        return t.value in _REGEX_AFTER
    if t.kind == "punc":
        return t.value not in (")", "]")
    return False


def _lex(src, i, nested=False):
    toks, comments, depth, n = [], [], 0, len(src)
    while i < n:
        c = src[i]
        if c in _JS_SPACE:
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            comments.append((i, src[i:j]))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise JSLexError("块注释没有闭合（第 {} 个字符起）".format(i))
            comments.append((i, src[i:j + 2]))
            i = j + 2
        elif c in "'\"":
            j = _scan_string(src, i)
            toks.append(Tok("str", src[i:j], i, j))
            i = j
        elif c == "`":
            j, parts, exprs = _scan_template(src, i)
            toks.append(Tok("tmpl", src[i:j], i, j, parts, exprs))
            i = j
        elif _JS_ID.match(src, i):
            m = _JS_ID.match(src, i)
            toks.append(Tok("name", m.group(), i, m.end()))
            i = m.end()
        elif (c.isdigit() or (c == "." and src[i + 1:i + 2].isdigit())) and _JS_NUM.match(src, i):
            m = _JS_NUM.match(src, i)
            toks.append(Tok("num", m.group(), i, m.end()))
            i = m.end()
        elif c == "/" and _regex_allowed(toks):
            j = _scan_regex(src, i)
            toks.append(Tok("regex", src[i:j], i, j))
            i = j
        else:
            if nested and c == "}" and depth == 0:
                return toks, comments, i
            p = next((p for p in _JS_PUNCT if src.startswith(p, i)), None)
            if p is None:
                raise JSLexError("认不出的字符 {!r}（第 {} 个字符）".format(c, i))
            if nested:
                depth += (p == "{") - (p == "}")
            toks.append(Tok("punc", p, i, i + len(p)))
            i += len(p)
    if nested:
        raise JSLexError("模板里的 ${ 没有闭合")
    return toks, comments, i


def lex_js(src):
    """JS 源码 → (tokens, comments)。comments 是 [(行号, 注释原文)]。认不出就抛 JSLexError。"""
    starts = [0] + [m.end() for m in re.finditer("\n", src)]
    toks, comments, _ = _lex(src, 0)
    for t in toks:
        t.line = bisect_right(starts, t.start)
    return toks, [(bisect_right(starts, pos), text) for pos, text in comments]


_JS_ESC = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def js_unescape(body):
    out, i, n = [], 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt == "u" and body[i + 2:i + 3] == "{":
            j = body.index("}", i)
            out.append(chr(int(body[i + 3:j], 16)))
            i = j + 1
        elif nxt == "u":
            out.append(chr(int(body[i + 2:i + 6], 16)))
            i += 6
        elif nxt == "x":
            out.append(chr(int(body[i + 2:i + 4], 16)))
            i += 4
        elif nxt in ("\r", "\n"):                       # 续行
            i += 3 if body.startswith("\r\n", i + 1) else 2
        else:
            out.append(_JS_ESC.get(nxt, nxt))
            i += 2
    return "".join(out)


def js_text(tok):
    """字符串或模板 token 的文字（模板只取 ${} 之外的部分）。"""
    if tok.kind == "str":
        return js_unescape(tok.value[1:-1])
    if tok.kind == "tmpl":
        return "".join(js_unescape(p) for p in tok.parts)
    return ""


def js_call_at(toks, i):
    """toks[i] 是不是一次 L(...)/LN(...) 调用的函数名：是就返回 "L"/"LN"。
    排除 obj.L(...) 与 function L(...) 的定义。"""
    t = toks[i]
    if t.kind != "name" or t.value not in ("L", "LN"):
        return None
    if i + 1 >= len(toks) or toks[i + 1].value != "(":
        return None
    prev = toks[i - 1] if i else None
    if prev is not None and (prev.value in (".", "?.") or prev.value == "function"):
        return None
    return t.value


def js_args(toks, open_idx):
    """从 "(" 开始按顶层逗号切实参，返回 ([实参 token 列表…], 右括号下标)。"""
    args, cur, depth = [], [], 0
    for k in range(open_idx + 1, len(toks)):
        t = toks[k]
        if t.kind == "punc" and t.value in ("(", "[", "{"):
            depth += 1
        elif t.kind == "punc" and t.value in (")", "]", "}"):
            if depth == 0 and t.value == ")":
                if cur:
                    args.append(cur)
                return args, k
            depth -= 1
        elif t.kind == "punc" and t.value == "," and depth == 0:
            args.append(cur)
            cur = []
            continue
        cur.append(t)
    raise JSLexError("调用的括号没有闭合（第 {} 行）".format(toks[open_idx].line))


def js_idents(tokens):
    """一段表达式里引用的名字（a、a.b.c）。模板 ${} 里的也算。"""
    names = set()
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == "tmpl":
            for expr in t.exprs:
                names |= js_idents(lex_js(expr)[0])
        prev = tokens[i - 1] if i else None
        if t.kind == "name" and not (prev is not None and prev.value in (".", "?.")):
            chain = [t.value]
            while (i + 2 < len(tokens) and tokens[i + 1].value in (".", "?.")
                   and tokens[i + 2].kind == "name"):
                chain.append(tokens[i + 2].value)
                i += 2
            if chain[0] not in JS_KEYWORDS:
                names.add(".".join(chain))
        i += 1
    return names


# =========================================================================================
# HTML：一棵够用的树
# =========================================================================================

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
             "source", "track", "wbr"}
RAW_TAGS = {"script", "style"}


class Node:
    def __init__(self, tag, attrs, line, parent=None):
        self.tag, self.attrs, self.line, self.parent = tag, list(attrs), line, parent
        self.children = []

    def get(self, name, default=None):
        for k, v in self.attrs:
            if k == name:
                return v
        return default

    def has(self, name):
        return any(k == name for k, _ in self.attrs)

    def set(self, name, value):
        for i, (k, _) in enumerate(self.attrs):
            if k == name:
                self.attrs[i] = (name, value)
                return
        self.attrs.append((name, value))


class Text:
    def __init__(self, data, line, parent):
        self.data, self.line, self.parent = data, line, parent


class Comment(Text):
    pass


class _TreeBuilder(HTMLParser):
    def __init__(self, root):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.getpos()[0], self.cur)
        self.cur.children.append(node)
        if tag not in VOID_TAGS:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.getpos()[0], self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_data(self, data):
        last = self.cur.children[-1] if self.cur.children else None
        if type(last) is Text:
            last.data += data
        else:
            self.cur.children.append(Text(data, self.getpos()[0], self.cur))

    def handle_comment(self, data):
        self.cur.children.append(Comment(data, self.getpos()[0], self.cur))


def parse_html(src, root=None):
    root = root or Node("#document", [], 1)
    builder = _TreeBuilder(root)
    builder.feed(src)
    builder.close()
    return root


def iter_elements(node):
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from iter_elements(child)


def text_content(node):
    parts = []
    for child in node.children:
        if type(child) is Text:
            parts.append(child.data)
        elif isinstance(child, Node) and child.tag not in RAW_TAGS:
            parts.append(text_content(child))
    return "".join(parts)


def collapse(text):
    return re.sub(r"\s+", " ", text or "").strip()


def own_text(el):
    """applyStatic 的 setOwnText 换掉的那一段：第一段非空的直接子文字节点。"""
    return next((c for c in el.children if type(c) is Text and c.data.strip()), None)


def marker_in_html(src):
    return any(isinstance(n, Comment) and rules.DONE_TAG in {m.group(1) for m in _NOTE.finditer(n.data)}
               for n in _walk(parse_html(src)))


def _walk(node):
    for child in node.children:
        yield child
        if isinstance(child, Node):
            yield from _walk(child)


# =========================================================================================
# 抽对
# =========================================================================================

class Pair:
    """一个中英对。

    zh / ens 是拿来做文字检查的文字：Python 是字面量本身；JS 是一臂里所有字面量拼起来；
    HTML 是元素文字或属性值。ens 对 L 是 [英文]，对 LN 是 [one, many]。
    zh_key / en_key 是 G3 聚合用的键（JS 带变量的臂用 token 原文）。
    fields 是 R2 比的占位符：Python 是 Counter，JS 是名字集合，HTML 为 None（不比）。"""

    def __init__(self, path, line, kind, lang, zh="", ens=(), scope=(), problem=None,
                 zh_key=None, en_key=None, fields=None):
        self.path, self.line, self.kind, self.lang = path, line, kind, lang
        self.zh, self.ens, self.scope, self.problem = zh, list(ens), tuple(scope), problem
        self.zh_key = zh if zh_key is None else zh_key
        self.en_key = (tuple(self.ens) if kind == "LN" else (self.ens[0] if self.ens else "")) \
            if en_key is None else en_key
        self.fields = fields                       # (zh, [每个英文臂]) 或 None

    def __repr__(self):
        return "Pair({}:{} {} {!r} -> {!r})".format(self.path, self.line, self.kind, self.zh,
                                                   self.ens)


def py_call_kind(call):
    f = call.func
    if isinstance(f, ast.Name) and f.id in ("L", "LN"):
        return f.id
    if (isinstance(f, ast.Attribute) and f.attr in ("L", "LN") and isinstance(f.value, ast.Name)
            and f.value.id == "i18n"):
        return f.attr
    return None


def py_format_fields(template):
    """R2：把 "{}" 按出现顺序规范成 "{0}{1}…"，返回 (字段, 格式规格, 转换) 的多重集。
    模板本身不合法（单个花括号、自动编号与手动编号混用）返回 None。"""
    fields, auto, manual = Counter(), 0, False
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return None
    for _, name, spec, conv in parsed:
        if name is None:
            continue
        head = re.match(r"[^.\[]*", name).group()
        if head == "":
            name, auto = str(auto) + name, auto + 1
        elif head.isdigit():
            manual = True
        fields[(name, spec or "", conv or "")] += 1
    if auto and manual:
        return None
    return fields


class _PyPairs(ast.NodeVisitor):
    def __init__(self, path):
        self.path, self.scope, self.pairs = path, [], []

    def _scoped(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Call(self, node):
        kind = py_call_kind(node)
        if kind:
            self.pairs.append(_py_pair(self.path, node, kind, self.scope))
        self.generic_visit(node)


def _py_pair(path, call, kind, scope):
    need, arms = (2, call.args[0:2]) if kind == "L" else (4, call.args[1:4])
    if len(call.args) != need or call.keywords:
        return Pair(path, call.lineno, kind, "py", scope=scope,
                    problem="{}() 要 {} 个位置参数".format(kind, need))
    if not all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in arms):
        return Pair(path, call.lineno, kind, "py", scope=scope,
                    problem="{}() 的中英两臂要写成字符串字面量，检查器才看得见".format(kind))
    zh, ens = arms[0].value, [a.value for a in arms[1:]]
    return Pair(path, call.lineno, kind, "py", zh, ens, scope,
                fields=(py_format_fields(zh), [py_format_fields(e) for e in ens]))


def python_pairs(path, src):
    visitor = _PyPairs(path)
    visitor.visit(ast.parse(src))
    return visitor.pairs


def _js_arm(tokens):
    text = "".join(js_text(t) for t in tokens)
    pure = bool(tokens) and all(t.kind == "str" or (t.kind == "tmpl" and not t.exprs)
                                or (t.kind == "punc" and t.value == "+") for t in tokens)
    return text, (text if pure else " ".join(t.value for t in tokens)), js_idents(tokens)


def js_pairs(path, src):
    toks, _ = lex_js(src)
    pairs = []
    for i in range(len(toks)):
        kind = js_call_at(toks, i)
        if not kind:
            continue
        args, _ = js_args(toks, i + 1)
        need = 2 if kind == "L" else 4
        if len(args) != need:
            pairs.append(Pair(path, toks[i].line, kind, "js",
                              problem="{}() 要 {} 个参数".format(kind, need)))
            continue
        arms = [_js_arm(a) for a in (args if kind == "L" else args[1:])]
        zh_key = arms[0][1]
        en_key = arms[1][1] if kind == "L" else (arms[1][1], arms[2][1])
        pairs.append(Pair(path, toks[i].line, kind, "js", arms[0][0], [a[0] for a in arms[1:]],
                          zh_key=zh_key, en_key=en_key,
                          fields=(arms[0][2], [a[2] for a in arms[1:]])))
    return pairs


def html_pairs(path, src):
    pairs = []
    for el in iter_elements(parse_html(src)):
        scope, node = [], el
        while isinstance(node, Node):
            if node.get("id"):
                scope.append("#" + node.get("id"))
            node = node.parent
        for name, _ in el.attrs:
            if name.startswith("data-en") and name not in EN_ATTRS:
                pairs.append(Pair(path, el.line, "L", "html", scope=scope,
                                  problem="认不出的属性 {}（只有 {}）".format(name, "、".join(EN_ATTRS))))
        if el.has("data-en-html"):
            if el.has("data-en"):
                pairs.append(Pair(path, el.line, "L", "html", scope=scope,
                                  problem="data-en 与 data-en-html 不能写在同一个元素上"))
            value = el.get("data-en-html") or ""
            en_text = collapse(text_content(parse_html(value)))
            pairs.append(Pair(path, el.line, "L", "html", collapse(text_content(el)), [en_text],
                              scope, en_key=collapse(value)))
        elif el.has("data-en"):
            own = own_text(el)
            pairs.append(Pair(path, el.line, "L", "html", collapse(own.data if own else ""),
                              [collapse(el.get("data-en"))], scope))
        for attr in I18N_ATTRS:
            if not el.has("data-en-" + attr):
                continue
            if el.get(attr) is None:
                pairs.append(Pair(path, el.line, "L", "html", scope=scope,
                                  problem="有 data-en-{0} 却没有 {0}".format(attr)))
                continue
            pairs.append(Pair(path, el.line, "L", "html", collapse(el.get(attr)),
                              [collapse(el.get("data-en-" + attr))], scope))
    return pairs


def pairs_of(path, src):
    """按扩展名抽一个文件里的对。path 用仓库相对路径（规则表按它查第八条家族）。"""
    if path.endswith(".py"):
        return python_pairs(path, src)
    if path.endswith(".js"):
        return js_pairs(path, src)
    if path.endswith(".html"):
        return html_pairs(path, src)
    return []


def source_files(root=REPO_ROOT, patterns=rules.PAIR_SOURCES):
    """抽对范围里的文件（仓库相对路径，按字母序）。"""
    found = set()
    for pat in patterns:
        found.update(p.relative_to(root).as_posix() for p in root.glob(pat) if p.is_file())
    return sorted(found)


def read(path, root=REPO_ROOT):
    return (root / path).read_text(encoding="utf-8")


# =========================================================================================
# G2 成对检查、G3 同中文→同英文
# =========================================================================================

def _whole_word(word, text):
    return re.search(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", text) is not None


def _sub_multiset(small, big):
    return all(big[k] >= v for k, v in small.items())


def in_rule8_family(path, scope):
    spec = rules.RULE8_FAMILY.get(path)
    return spec == "*" or (isinstance(spec, tuple) and any(s in scope for s in spec))


def check_pair(p):
    """G2：一个对的全部规则。返回 Violation 列表。"""
    if p.problem:
        return [Violation(p.path, p.line, "R1", p.problem)]
    out = []

    def bad(rule, msg):
        out.append(Violation(p.path, p.line, rule, "{}：{!r}".format(msg, p.zh[:60])))

    arms = [p.zh] + p.ens
    blank = [not a.strip() for a in arms]
    # L("", "")、L("\n", "\n") 这类分隔符对放行；JS 的 L(a, b) 两臂都只有变量，检查器什么也看不见，不放行
    js_vars = p.lang == "js" and bool(p.fields[0] or any(p.fields[1]))
    if any(blank) and not (all(blank) and not js_vars):
        bad("R1", "两臂都要有文字")
    if p.fields is not None:
        zh_f, en_fs = p.fields
        if p.lang == "py" and (zh_f is None or any(f is None for f in en_fs)):
            if not (zh_f is None and all(f is None for f in en_fs)):
                bad("R2", "占位符写法不合法（单个花括号，或 {} 与 {0} 混用）")
        elif p.kind == "L" and zh_f != en_fs[0] and not (
                p.lang == "js" and p.zh in rules.R2_EN_OMITS and en_fs[0] <= zh_f):
            bad("R2", "两臂的占位符不一致 {} ≠ {}".format(_fmt(zh_f), _fmt(en_fs[0])))
        elif p.kind == "LN":
            one, many = en_fs
            if many != zh_f:
                bad("R2", "LN 的 many 臂占位符要与中文相同 {} ≠ {}".format(_fmt(many), _fmt(zh_f)))
            if not (_sub_multiset(one, zh_f) if isinstance(one, Counter) else one <= zh_f):
                bad("R2", "LN 的 one 臂只能用中文里有的占位符 {}".format(_fmt(one)))
    zh_emoji = Counter(EMOJI.findall(p.zh))
    if in_rule8_family(p.path, p.scope):
        m = rules.ZH_LABELS.search(p.zh) or rules.ZH_CAUSAL.search(p.zh)
        if m:
            bad("第八条", "第八条家族的中文里出现了贴标签或猜原因的词 {!r}".format(m.group()))
    for en in p.ens:
        cjk = CJK.findall(en)
        if cjk:
            bad("R3", "英文臂里有中文字符或中文标点 {!r}".format("".join(cjk)))
        if Counter(EMOJI.findall(en)) != zh_emoji:
            bad("R3", "英文臂的 emoji 要与中文相同")
        for name in re.findall("「([^」]+)」", p.zh):
            want = rules.UI_NAMES.get(name)
            if want and not _whole_word(want, en):
                bad("R6", "中文引用了「{}」，英文里要写 {}（不加引号）".format(name, want))
        if rules.PRODUCT_NAME[0] in p.zh and rules.PRODUCT_NAME[1] not in en:
            bad("名称", "中文有产品名，英文要写 {}".format(rules.PRODUCT_NAME[1]))
        if p.zh_key in rules.EN_WORD_EXCEPTIONS:
            continue
        m = rules.EN_LABELS.search(en)
        if m:
            bad("第八条", "英文里出现了贴标签的词 {!r}".format(m.group()))
        m = rules.EN_CAUSAL.search(en) if in_rule8_family(p.path, p.scope) else None
        if m:
            bad("第八条", "第八条家族的英文里出现了猜原因的词 {!r}".format(m.group()))
    return out


def _fmt(fields):
    if isinstance(fields, Counter):
        return sorted("{{{}{}{}}}".format(n, "!" + c if c else "", ":" + s if s else "")
                      for (n, s, c), k in fields.items() for _ in range(k))
    return sorted(fields)


def check_pairs(pairs):
    out = []
    for p in pairs:
        out.extend(check_pair(p))
    return out


def check_consistency(pairs):
    """G3：以中文为键聚合，一句中文对应多种英文就报（登记在 SAME_ZH_DIFFERENT_EN 的除外）。"""
    groups = defaultdict(lambda: defaultdict(list))
    for p in pairs:
        if not p.problem:
            groups[p.zh_key][p.en_key].append(p)
    out = []
    for zh, variants in sorted(groups.items()):
        if len(variants) < 2 or zh in rules.SAME_ZH_DIFFERENT_EN:
            continue
        where = "；".join("{!r} @ {}".format(en, ", ".join("{}:{}".format(q.path, q.line) for q in ps))
                         for en, ps in variants.items())
        first = min((q for ps in variants.values() for q in ps), key=lambda q: (q.path, q.line))
        out.append(Violation(first.path, first.line, "G3",
                             "同一句中文 {!r} 有 {} 种英文：{}".format(zh, len(variants), where)))
    return out


# =========================================================================================
# G4 覆盖（只对带 i18n: done 标记的文件）
# =========================================================================================

def py_notes(src):
    """{行号: {注记}}，以及全文件注记集合。只认真正的注释，不认字符串里的字样。"""
    lines, whole = defaultdict(set), set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            for m in _NOTE.finditer(tok.string):
                lines[tok.start[0]].add(m.group(1))
                whole.add(m.group(1))
    return lines, whole


def js_notes(comments):
    lines, whole = defaultdict(set), set()
    for line, text in comments:
        for m in _NOTE.finditer(text):
            lines[line].add(m.group(1))
            whole.add(m.group(1))
    return lines, whole


def is_marked(path, src):
    """文件有没有写 R9 的迁移完成标记。"""
    if path.endswith(".py"):
        return rules.DONE_TAG in py_notes(src)[1]
    if path.endswith(".js"):
        return rules.DONE_TAG in js_notes(lex_js(src)[1])[1]
    if path.endswith(".html"):
        return marker_in_html(src)
    return False


def _call_name(call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _is_r11_exempt_call(call):
    f = call.func
    if isinstance(f, ast.Name) and f.id in rules.R11_EXEMPT_CALLS:
        return True
    return (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
            and f.value.id in rules.R11_EXEMPT_RECEIVERS)


def _descendants(nodes):
    ids = set()
    for node in nodes:
        for sub in ast.walk(node):
            ids.add(id(sub))
    return ids


def _is_slice(node):
    return isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)


def _parents(tree):
    return {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _qualname(node, parents):
    """node 所在的函数（带外层类名，如 BannedTermDetector.scan）；模块级为空串。"""
    names, up = [], parents.get(id(node))
    while up is not None:
        if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(up.name)
        up = parents.get(id(up))
    return ".".join(reversed(names))


def literal_joins(src):
    """文件里每个 "<字面量>".join(...) 的 (所在函数的限定名, 分隔符)。rules.R11_JOIN_EXEMPT
    的登记拿它核对：登记的那处 join 没了，登记就过期了。"""
    tree = ast.parse(src)
    parents = _parents(tree)
    return {(_qualname(n, parents), n.func.value.value) for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "join" and isinstance(n.func.value, ast.Constant)
            and isinstance(n.func.value.value, str)}


def check_python_coverage(path, src):
    lines, whole = py_notes(src)
    if rules.FILE_TAG in whole:
        return []
    tree = ast.parse(src)
    parents = _parents(tree)

    def annotated(node):
        """这个节点所在的（最内层简单）语句任何一行写了 R8 注记。"""
        spans = [(node.lineno, getattr(node, "end_lineno", node.lineno))]
        up = parents.get(id(node))
        while up is not None and not isinstance(up, ast.stmt):
            up = parents.get(id(up))
        if up is not None and not isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                  ast.ClassDef, ast.If, ast.For, ast.While,
                                                  ast.With, ast.Try, ast.AsyncFor,
                                                  ast.AsyncWith)):
            spans.append((up.lineno, getattr(up, "end_lineno", up.lineno)))
        return any(lines.get(n, set()) & set(rules.NOTE_TAGS)
                   for a, b in spans for n in range(a, b + 1))

    docstrings, arms, printed, r11_exempt = set(), set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
        elif isinstance(node, ast.Call):
            if py_call_kind(node):
                arms.update(id(a) for a in node.args)
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                printed |= _descendants(node.args + [k.value for k in node.keywords])
            if _is_r11_exempt_call(node):
                r11_exempt |= _descendants(node.args + [k.value for k in node.keywords])

    out = []
    join_exempt = rules.R11_JOIN_EXEMPT.get(path, {})

    def bad(node, rule, msg, text=None):
        out.append(Violation(path, node.lineno, rule, msg, text))

    reported = set()                  # 已经按 R11 报过的字面量（f-string 片段、join 的分隔符），不再按 G4 报一遍
    for node in ast.walk(tree):         # 广度优先：父节点先于子节点
        if isinstance(node, ast.JoinedStr):
            parts = [v for v in node.values if isinstance(v, ast.Constant)]
            reported.update(id(v) for v in parts)
            if any(CJK.search(v.value) for v in parts if isinstance(v.value, str)) \
                    and id(node) not in r11_exempt and not annotated(node):
                bad(node, "R11", "带中文的 f-string 会丢英文，改成 L(...).format(...)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and CJK.search(node.value):
            if (id(node) in docstrings or id(node) in arms or id(node) in printed
                    or id(node) in reported
                    or node.value.lstrip().startswith(rules.TERMINAL_PREFIXES) or annotated(node)):
                continue
            bad(node, "G4", "界面中文要写成 L(\"中文\", \"English\")，不上界面的在行尾写 "
                            "# i18n: terminal|audit|data|os：{!r}".format(node.value[:40]),
                node.value)
        elif isinstance(node, ast.Call) and id(node) not in r11_exempt and not annotated(node):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr == "join" and isinstance(f.value, ast.Constant)
                    and isinstance(f.value.value, str)):
                if (_qualname(node, parents), f.value.value) not in join_exempt:
                    bad(node, "R11", "{!r}.join(...) 会丢英文，改成 L(sep, sep_en).join(...)".format(
                        f.value.value))
                reported.add(id(f.value))
            name = _call_name(node)
            if name and (name in rules.SLICE_SINKS or name.startswith(rules.SLICE_SINK_PREFIXES)):
                values = node.args + [k.value for k in node.keywords]
                if name in rules.SLICE_DICT_SINKS:
                    values += [v for a in values if isinstance(a, ast.Dict) for v in a.values]
                if any(_is_slice(v) for v in values):
                    bad(node, "R11", "切片直接交给 {}() 会丢英文，改成 bimap(lambda s: s[:n], x)".format(name))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if not isinstance(sub, ast.Call) or id(sub) in r11_exempt or annotated(sub):
                    continue
                if (isinstance(sub.func, ast.Name)
                        and sub.func.id == "str" and len(sub.args) == 1
                        and isinstance(sub.args[0], ast.Name) and sub.args[0].id == node.name):
                    bad(sub, "R11", "str({}) 会丢英文，改成 of({})".format(node.name, node.name))
                # L(...).format(exc)：Bi.format 把异常对象当普通值填进两臂，format(exc) 就是
                # str(exc)——raise X(L(...)) 带着的英文在这里丢掉，英文界面露中文
                elif (isinstance(sub.func, ast.Attribute) and sub.func.attr == "format"
                      and isinstance(sub.func.value, ast.Call) and py_call_kind(sub.func.value)
                      and any(isinstance(a, ast.Name) and a.id == node.name
                              for a in sub.args + [k.value for k in sub.keywords])):
                    bad(sub, "R11", "L(...).format({0}) 会丢异常里的英文，改成 .format(of({0}))".format(
                        node.name))
    return out


def check_js_coverage(path, src):
    toks, comments = lex_js(src)
    lines, whole = js_notes(comments)
    if rules.FILE_TAG in whole:
        return []
    covered = set()
    for i in range(len(toks)):
        if js_call_at(toks, i):
            _, end = js_args(toks, i + 1)
            covered.update(range(i, end + 1))
    out = []
    for i, t in enumerate(toks):
        if t.kind not in ("str", "tmpl") or i in covered or not CJK.search(js_text(t)):
            continue
        if lines.get(t.line, set()) & set(rules.NOTE_TAGS):
            continue
        out.append(Violation(path, t.line, "G4", "界面中文要写成 L(\"中文\", \"English\")，不上界面的在行尾写 "
                                                 "// i18n: data 一类注记：{}".format(t.value[:40]),
                             js_text(t)))
    return out


def is_data_region(el):
    """R7 数据区：子树里的文字、子孙的属性都不算界面文字（元素自己的属性不在内）。两种标法：
    名字标 translate="no"；字幕、弹幕、报警原话这类正文要留给浏览器翻译，只带
    class rules.DATA_TEXT_CLASS（用户决定 6）。与 tests/i18n_dom/scan.js 的 isData 同一条规则。"""
    return el.get("translate") == "no" or rules.DATA_TEXT_CLASS in (el.get("class") or "").split()


def check_html_coverage(path, src):
    out = []

    def walk(el, in_data, en_html):
        for attr in I18N_ATTRS:
            value = el.get(attr)
            if (value and CJK.search(value) and not in_data and not en_html
                    and not el.has("data-en-" + attr)):
                out.append(Violation(path, el.line, "G4", "<{}> 的 {} 是中文，要配 data-en-{}：{!r}".format(
                    el.tag, attr, attr, value[:40]), value))
        child_data = in_data or is_data_region(el)
        child_eh = en_html or el.has("data-en-html")
        first = own_text(el)
        for child in el.children:
            if isinstance(child, Node):
                walk(child, child_data, child_eh)
            elif (type(child) is Text and CJK.search(child.data) and not child_data and not child_eh
                  and el.tag not in RAW_TAGS and not (child is first and el.has("data-en"))):
                out.append(Violation(path, child.line, "G4", "<{}> 里的中文要配 data-en（或 data-en-html）："
                                                             "{!r}".format(el.tag, collapse(child.data)[:40]),
                                     collapse(child.data)))

    root = parse_html(src)
    for child in root.children:
        if isinstance(child, Node):
            walk(child, False, False)
    return out


def check_coverage(path, src):
    if path.endswith(".py"):
        return check_python_coverage(path, src)
    if path.endswith(".js"):
        return check_js_coverage(path, src)
    if path.endswith(".html"):
        return check_html_coverage(path, src)
    return []


def check_unlisted(path, src):
    """G4 全量（Z0）里 UI_FILES 以外的一个文件：照 G4 的规则扫中文字面量（R11 不查——这些文件里
    没有 L() 的结果流过，"".join 之类拼的是数据），扫出来的每一处都得在 rules.NON_UI_CHINESE
    里按全文登记了理由。返回 (没登记的违例, 登记了却已经不在文件里的字面量)。"""
    listed = {t for texts in rules.NON_UI_CHINESE.get(path, {}).values() for t in texts}
    found = [v for v in check_coverage(path, src) if v[2] == "G4"]
    fresh = [Violation(path, v[1], "G4", "不在 UI_FILES 里的文件出现了中文：{!r}。上界面的话把文件加进 "
                                         "tests/i18n_rules.py 的 UI_FILES、写 i18n: done 并写成 L()；"
                                         "不上界面的在 NON_UI_CHINESE 里登记理由".format(v.text[:40]),
                       v.text)
             for v in found if v.text not in listed]
    return fresh, sorted(listed - {v.text for v in found})


def check_full_coverage(files):
    """G4 全量（spec §12.1 G4，Z0 起）。files 是抽对范围里全部文件的 {路径: 源码}。
    - rules.UI_FILES 与写了 `i18n: done` 的文件一一对应：清单里的都在、都标了，标了的都在清单里；
    - 清单外的文件按 check_unlisted 查；rules.NON_UI_CHINESE 里过期的登记也算违例。"""
    out = []
    listed = set(rules.UI_FILES)
    for path in sorted(listed - set(files)):
        out.append(Violation(path, 0, "G4", "UI_FILES 里的文件不在抽对范围里（改名或删掉了？"
                                            "同步改 tests/i18n_rules.py）"))
    for path in sorted(set(rules.NON_UI_CHINESE) - (set(files) - listed)):
        out.append(Violation(path, 0, "G4", "NON_UI_CHINESE 登记的文件不在抽对范围里、或已在 UI_FILES 里"))
    for path, src in sorted(files.items()):
        marked = is_marked(path, src)
        if path in listed:
            if not marked:
                out.append(Violation(path, 1, "G4", "在 UI_FILES 里，但没写 i18n: done"))
            continue
        if marked:
            out.append(Violation(path, 1, "G4", "写了 i18n: done，但不在 tests/i18n_rules.py 的 "
                                                "UI_FILES 里"))
            continue
        fresh, stale = check_unlisted(path, src)
        out.extend(fresh)
        out.extend(Violation(path, 0, "G4", "NON_UI_CHINESE 登记的字面量已经不在文件里（或已写成 "
                                            "L()、加了注记）：{!r}".format(t[:40])) for t in stale)
    return out


# =========================================================================================
# G5：按 applyStatic 的规则把英文代进静态页
# =========================================================================================

def render_static_en(src):
    """模拟 web/i18n.js 的 applyStatic（英文），返回还剩中文的地方 [(行号, 位置, 文字)]。

    跳过 <script>、<style>、注释，以及数据区（translate="no" 或 class 带 rules.DATA_TEXT_CLASS，
    见 is_data_region）子树里的文字；数据区元素自己的 title/aria-label/placeholder/alt 照查
    （只有子孙的属性豁免）。"""
    root = parse_html(src)
    for el in list(iter_elements(root)):                 # 与 querySelectorAll 一样：先取全，再逐个换
        if el.has("data-en-html"):
            frag = parse_html(el.get("data-en-html") or "", Node("#fragment", [], el.line))
            el.children = frag.children
            for child in el.children:
                child.parent = el
            for node in _walk(el):                        # 行号记在宿主元素上，报错时好找
                node.line = el.line
        elif el.has("data-en"):
            own = own_text(el)
            if own is not None:
                own.data = el.get("data-en") or ""
            else:
                el.children.append(Text(el.get("data-en") or "", el.line, el))
        for attr in I18N_ATTRS:
            if el.has("data-en-" + attr):
                el.set(attr, el.get("data-en-" + attr))
    left = []

    def walk(el, in_data):
        for attr in I18N_ATTRS:
            value = el.get(attr)
            if value and not in_data and CJK.search(value):
                left.append((el.line, "<{} {}>".format(el.tag, attr), value))
        inner = in_data or is_data_region(el)
        for child in el.children:
            if isinstance(child, Node):
                walk(child, inner)
            elif type(child) is Text and not inner and el.tag not in RAW_TAGS and CJK.search(child.data):
                left.append((child.line, "<{}>".format(el.tag), collapse(child.data)))

    for child in root.children:
        if isinstance(child, Node):
            walk(child, False)
    return left


# =========================================================================================
# git 小工具（strip-check / const-check 共用）
# =========================================================================================

def git_show(repo, base, path):
    """基点版本的文件内容；基点里没有这个文件返回 None。"""
    proc = subprocess.run(["git", "-C", str(repo), "show", "{}:{}".format(base, path)],
                          capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8")


def git_has_commit(repo, base):
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                           "{}^{{commit}}".format(base)], capture_output=True)
    return proc.returncode == 0


def changed_files(repo, base, patterns=rules.PAIR_SOURCES):
    """相对基点改过的、在抽对范围里的文件（含工作区里还没提交的改动；不含删掉的）。"""
    proc = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=d",
                           base, "--"], capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError("git diff 失败：{}".format(proc.stderr.strip()))
    names = [n for n in proc.stdout.splitlines() if n]
    return [n for n in names if any(Path(n).match(p) and Path(n).parent == Path(p).parent
                                    for p in patterns)]


# =========================================================================================
# 命令行
# =========================================================================================

def scan(root=REPO_ROOT, paths=None):
    """全仓（或指定文件）抽对并跑 G2/G3/G4。返回 (pairs, violations, 已标记的文件)。
    不指定文件时另跑 G4 全量（check_full_coverage）：只看几个文件时判断不了清单全不全。"""
    full = not paths
    paths = paths or source_files(root)
    pairs, violations, marked, files = [], [], [], {}
    for path in paths:
        src = files[path] = read(path, root)
        pairs.extend(pairs_of(path, src))
        if is_marked(path, src):
            marked.append(path)
            violations.extend(check_coverage(path, src))
    violations.extend(check_pairs(pairs))
    violations.extend(check_consistency(pairs))
    if full:
        violations.extend(check_full_coverage(files))
    return pairs, sorted(set(violations)), marked


def main(argv=None):
    ap = argparse.ArgumentParser(description="抽出全仓的中英对，跑 G2/G3/G4")
    ap.add_argument("paths", nargs="*", help="只看这些文件（仓库相对路径）；默认抽对范围全部")
    ap.add_argument("--list", action="store_true", help="逐条列出所有对（TSV）")
    args = ap.parse_args(argv)
    paths = args.paths or source_files()
    pairs, violations, marked = scan(REPO_ROOT, args.paths or None)
    if args.list:
        for p in pairs:
            print("\t".join([p.path, str(p.line), p.kind, p.zh, " | ".join(p.ens), p.problem or ""]))
    print("扫了 {} 个文件，{} 个中英对；带 i18n: done 标记的 {} 个：{}".format(
        len(paths), len(pairs), len(marked), ", ".join(marked) or "（无）"))
    for v in violations:
        print(v)
    print("违例 {} 条".format(len(violations)) if violations else "没有违例")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
