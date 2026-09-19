#!/usr/bin/env python3
"""Prove a "pure move" refactor is lossless: every function/method that
disappeared from a source file between two revisions has its exact source
text (byte-identical) somewhere in one of the destination files, and nothing
else in the source file changed except import lines, class base lists, and
simple module-level re-exports.

Written for CLAUDE.md rule 二 ("大改动拆成两个提交：纯搬移一个、内容改动一个
...搬移那个要能逐行验证无损"). Generic enough to reuse for the next split:

    python3 tools/verify_pure_move.py \\
        --base bf809d3 --source app/pipeline.py \\
        --dest app/viewer_share.py app/pipeline_disk.py

stdlib only (ast, difflib, subprocess for `git show`). Exit code 0 = verified,
1 = a check failed (details printed to stdout).
"""
import argparse
import ast
import difflib
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.stdio import harden_stdio                     # noqa: E402

# 重定向到文件时（Windows 上 `python3 tools/x.py > out.txt` 默认编码是 ANSI
# 代码页 + strict），中文输出不该让这个校验脚本中途崩掉：见 app/stdio.py。
harden_stdio()

# Lines that are allowed to differ in the source file between base and
# current *outside* the spans covered by a removed function/constant: plain
# imports (incl. continuation lines of a parenthesised import), blank lines,
# comment-only lines, and a class statement (base-class list changing).
def _is_allowed_structural_line(line):
    s = line.strip()
    if s == "":
        return True
    if s.startswith("#"):
        return True
    if s.startswith("import ") or s.startswith("from "):
        return True
    if s.startswith("class ") and s.endswith(":"):
        return True
    # Strip a trailing "#"-comment (e.g. a noqa suppression) before judging
    # whether what's left is just import-continuation syntax.
    code_part = s.split("#", 1)[0].strip()
    if code_part == "":
        return True   # comment-only after all (shouldn't happen, s != "")
    # continuation line inside a parenthesised "from x import (...)" block:
    # a bare identifier, optionally with a trailing comma, and nothing else
    # (no "=", no call syntax besides that).
    if code_part in (")",) or (code_part.endswith(",") and "=" not in code_part
                               and "(" not in code_part.rstrip(",")):
        return True
    if code_part.isidentifier():
        return True
    return False


def git_show(ref, path):
    out = subprocess.run(["git", "show", "{}:{}".format(ref, path)],
                          cwd=str(REPO_ROOT), capture_output=True, check=True)
    return out.stdout.decode("utf-8")


def module_functions_and_methods(source):
    """Return {qualname: node} for every top-level function and every method
    of every top-level class in `source`. qualname is "func" or "Class.method".
    """
    tree = ast.parse(source)
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out["{}.{}".format(node.name, sub.name)] = sub
    return out


def module_level_assignments(source):
    """Return {name: node} for simple top-level `NAME = ...` assignments
    (module-level constants), skipping anything inside a class/function."""
    tree = ast.parse(source)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = node
    return out


def class_body_assignments(source):
    """Return {"ClassName.NAME": node} for simple `NAME = ...` assignments
    directly in the body of a top-level class (e.g. Pipeline.LOCAL_ENGINES) —
    the class-level-constant analogue of module_level_assignments. Keyed by
    "ClassName.NAME" so it can be merged into the same dict as module-level
    constants without name collisions; the byte-identical text search below
    doesn't care which class (if any) holds the matching text in a dest file."""
    tree = ast.parse(source)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                        and isinstance(sub.targets[0], ast.Name):
                    out["{}.{}".format(node.name, sub.targets[0].id)] = sub
    return out


def node_span(node):
    """1-indexed inclusive (start, end) line span, including leading
    decorators/comments are NOT included (ast doesn't track leading comments;
    the pure-move script hand-picked spans already account for those)."""
    start = node.lineno
    if getattr(node, "decorator_list", None):
        start = min(d.lineno for d in node.decorator_list)
    return start, node.end_lineno


def check(base_ref, source_path, dest_paths):
    problems = []

    old_text = git_show(base_ref, source_path)
    new_text = Path(REPO_ROOT / source_path).read_text(encoding="utf-8")
    dest_texts = {p: Path(REPO_ROOT / p).read_text(encoding="utf-8") for p in dest_paths}

    old_funcs = module_functions_and_methods(old_text)
    new_funcs = module_functions_and_methods(new_text)
    removed_func_names = set(old_funcs) - set(new_funcs)

    old_consts = module_level_assignments(old_text)
    new_consts = module_level_assignments(new_text)
    # Merge in class-body constants (e.g. a class-level `FOO = 60.0` that
    # moved along with its methods into a mixin) — same removed/kept check,
    # just scoped one level into a top-level class instead of module scope.
    # Keys are "ClassName.NAME" so they can't collide with module-level names.
    old_consts.update(class_body_assignments(old_text))
    new_consts.update(class_body_assignments(new_text))
    removed_const_names = set(old_consts) - set(new_consts)

    print("== removed functions/methods: {} ==".format(len(removed_func_names)))
    verified_spans = []   # (start, end) 1-indexed inclusive, in OLD file line numbers
    for name in sorted(removed_func_names):
        node = old_funcs[name]
        seg = ast.get_source_segment(old_text, node)
        if seg is None:
            problems.append("could not extract source for {}".format(name))
            continue
        found_in = [p for p, t in dest_texts.items() if seg in t]
        span = node_span(node)
        verified_spans.append(span)
        if len(found_in) == 0:
            problems.append("{}: source NOT found byte-identical in any dest file"
                            .format(name))
            print("  FAIL {} (lines {}-{}): not found in dest files".format(name, *span))
        elif len(found_in) > 1:
            problems.append("{}: source appears in more than one dest file: {}"
                            .format(name, found_in))
            print("  WARN {} (lines {}-{}): appears in {}".format(name, span[0], span[1], found_in))
        else:
            print("  ok   {} (lines {}-{}) -> {}".format(name, span[0], span[1], found_in[0]))

    print("== removed module-level constants: {} ==".format(len(removed_const_names)))
    for name in sorted(removed_const_names):
        node = old_consts[name]
        seg = ast.get_source_segment(old_text, node)
        span = node_span(node)
        verified_spans.append(span)
        found_in = [p for p, t in dest_texts.items() if seg is not None and seg in t]
        if not found_in:
            problems.append("{}: constant source NOT found byte-identical in any dest file"
                            .format(name))
            print("  FAIL {} (lines {}-{})".format(name, *span))
        else:
            print("  ok   {} (lines {}-{}) -> {}".format(name, span[0], span[1], found_in[0]))

    # ---- structural diff: everything else in source_path must be untouched,
    # except plain import / class-base / comment / blank lines ----
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    print("== structural diff outside removed spans ==")
    bad_hunks = 0
    def _line_covered(old_lineno_1indexed):
        return any(s <= old_lineno_1indexed <= e for s, e in verified_spans)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        # Deleted/replaced OLD lines: each one must either fall inside some
        # verified removed function/constant span (moved verbatim elsewhere)
        # or pass the plain-structural allow-list (banner comments, blank
        # separator lines between the moved defs, etc.).
        offending = []
        for offset, old_line in enumerate(old_lines[i1:i2]):
            lineno = i1 + offset + 1
            if _line_covered(lineno) or _is_allowed_structural_line(old_line):
                continue
            offending.append(("-", old_line))
        # Inserted/replaced NEW lines: must be plain structural additions
        # (imports, class-base line, comments, blank lines).
        for new_line in new_lines[j1:j2]:
            if not _is_allowed_structural_line(new_line):
                offending.append(("+", new_line))
        if offending:
            bad_hunks += 1
            print("  FAIL hunk old[{}:{}] new[{}:{}]:".format(i1 + 1, i2, j1 + 1, j2))
            for sign, line in offending:
                print("      {} {}".format(sign, line))
            problems.append("unexplained structural change at old[{}:{}]".format(i1 + 1, i2))
    if bad_hunks == 0:
        print("  ok   (all remaining diff lines are imports/class-base/comments/blank)")

    print()
    if problems:
        print("VERIFY FAILED: {} problem(s)".format(len(problems)))
        for p in problems:
            print(" -", p)
        return 1
    print("VERIFY OK: {} functions/methods + {} constants moved losslessly; "
         "no unexplained structural change in {}".format(
             len(removed_func_names), len(removed_const_names), source_path))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="git ref for the pre-move state")
    ap.add_argument("--source", required=True, help="path (repo-relative) of the file moved FROM")
    ap.add_argument("--dest", required=True, nargs="+",
                    help="path(s) (repo-relative) of the file(s) moved TO")
    args = ap.parse_args()
    sys.exit(check(args.base, args.source, args.dest))


if __name__ == "__main__":
    main()
