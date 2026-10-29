#!/usr/bin/env python3
"""推送前扫真名：git pre-push 钩子的本体。

为什么有它：仓库是公开的，推上去就删不干净（提交历史、PR 页面、别人的 fork 都留着）。真实的
主播账号名、品牌名、店铺名不进仓库，测试样例一律虚构。以前靠推之前手动扫一遍，漏一次就公开了。

名单不在仓库里。每台电脑自己放一份，用环境变量 TLT_REALNAMES 指它的路径：每行一个，空行和
`#` 开头的行忽略，不分大小写、按子串匹配。没设 TLT_REALNAMES、名单不存在或是空的：只打印一句
提醒，照常放行（CI、别人的克隆没有名单，推送不受影响）。

扫这次要推、远端还没有的每个提交：
  * 提交说明；
  * diff 里新增的行（删掉的行不扫：删真名正是要做的事）。合并提交只看合并时自己写的那部分
    （git 的 --cc），从别的分支带进来的不重扫：那些提交要么已经在远端，要么也在这次要推的提交里；
  * 新加、改名、复制出来的文件路径；
  * 推附注标签时，标签说明。
命中时只打印打码后的词（首字母加长度，如 a7）、提交号、文件和行号，退出码 1，git 就不推。
打印的路径里如果也带着名单里的词，同样打码。名单本身绝不打印。
扫的时候出错（git 读不出提交等）退出码 2，也不推：这是挡真名的闸，宁可多挡一次。

装法、误报怎么办见 docs/prepush-realnames.md。不经 git、手动扫一段提交：

    TLT_REALNAMES=/path/to/names.txt python3 tools/prepush_realnames.py --range origin/main..HEAD

只用标准库，也不 import 本仓库的东西：装好的钩子用的是拷到仓库外的那一份，推哪个分支都一样跑。
"""
import argparse
import os
import re
import subprocess
import sys

ENV_VAR = "TLT_REALNAMES"
ZERO = re.compile(r"^0+$")
# 普通 diff 是 @@ -a,b +c,d @@；合并提交的 --cc 是 @@@ -a,b -c,d +e,f @@@（@ 比父提交数多一个）
HUNK = re.compile(r"^(@{2,}) (.*?) @{2,}")
# 比这还短的条目不用：一个字母会命中几乎每一行
MIN_NAME_LEN = 2
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def harden_stdio():
    """Windows 上输出被重定向时默认是 ANSI 代码页 + strict，中文一行就崩（同 app/stdio.py；
    这里不 import 它，理由见文件头）。只把出错策略换成 replace。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and getattr(stream, "errors", "") == "strict":
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def say(msg=""):
    print(msg, file=sys.stderr)


# ---- 名单与匹配 --------------------------------------------------------------------------------

def load_names(path):
    """读名单：去掉空行、注释行和太短的条目，统一 casefold、去重。文件不在返回 None。"""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    names = []
    for line in lines:
        name = line.strip().casefold()
        if len(name) >= MIN_NAME_LEN and not name.startswith("#") and name not in names:
            names.append(name)
    return names


def mask(name):
    """首字母加长度，如 b15。报命中时只给这个，名单里的词不出现在输出里。"""
    return "{}{}".format(name[:1], len(name))


def find_names(text, names):
    """text 里出现了名单里的哪些词（不分大小写、子串），按名单顺序、不重复。"""
    folded = (text or "").casefold()
    return [n for n in names if n in folded]


def mask_text(text, names):
    """把 text 里名单中的词换成 <打码>，用来打印路径。长的先换，免得短词把长词拆开。"""
    out = text or ""
    for name in sorted(names, key=len, reverse=True):
        out = re.sub(re.escape(name), "<{}>".format(mask(name)), out, flags=re.IGNORECASE)
    return out


# ---- 读 git -----------------------------------------------------------------------------------

class ScanError(Exception):
    pass


def git(args, cwd=None):
    """跑一条 git，返回 stdout（utf-8 解码）。失败抛 ScanError。"""
    cmd = ["git", "-c", "core.quotepath=false", "-c", "i18n.logOutputEncoding=utf-8"] + list(args)
    try:
        res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise ScanError("跑不了 git：{}".format(exc)) from exc
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ScanError("git {} 失败：{}".format(args[0], err[-1] if err else res.returncode))
    return res.stdout.decode("utf-8", "replace")


def _commit_exists(sha, cwd=None):
    try:
        git(["cat-file", "-e", sha + "^{commit}"], cwd=cwd)
        return True
    except ScanError:
        return False


def commits_to_push(local_sha, remote_sha, remote, cwd=None):
    """一条 ref 更新要推上去、远端还没有的提交。删分支返回空。

    远端这条分支原来的提交、以及本机知道的远端所有分支上的提交都不算：rebase 到新 main 上再
    强推时，main 上那些提交早就公开了，不该再挡一次（远端名不是配置过的名字，比如直接推到一个
    地址，就按所有远端算）。"""
    if ZERO.match(local_sha):
        return []
    spec = [local_sha]
    if not ZERO.match(remote_sha) and _commit_exists(remote_sha, cwd):
        spec.append("^" + remote_sha)
    remotes = set(git(["remote"], cwd=cwd).split())
    spec += ["--not", ("--remotes=" + remote) if remote in remotes else "--remotes"]
    return git(["rev-list"] + spec, cwd=cwd).split()


def _unquote(raw):
    """去掉 git 给特殊路径加的 C 风格引号（core.quotepath=false 时只有引号、反斜杠、控制字符才加）。"""
    raw = raw.rstrip("\t")
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw
    body, out, i = raw[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
            if re.match(r"[0-7]{3}", body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def scan_patch(patch, names):
    """扫 `git diff-tree -p -U0 --cc` 的输出里新增的行。返回 [(路径, 新文件里的行号, 词)]。

    合并提交（--cc）每行前面有几列 + / - / 空格，一列一个父提交：只有每列都是 + 的才是合并时
    新写的；有一列是空格的是从某一边带过来的，那一边自己的提交已经扫过（或已在远端）。"""
    hits = []
    path, parents, new_line, in_header = "", 1, 0, False
    for line in patch.splitlines():
        if line.startswith(("diff --git ", "diff --cc ", "diff --combined ")):
            in_header = True
            continue
        m = HUNK.match(line)
        if m:
            parents = len(m.group(1)) - 1
            new_line = int(m.group(2).split()[-1].lstrip("+").split(",")[0] or 0)
            in_header = False
            continue
        if in_header:
            if line.startswith("+++ "):
                target = _unquote(line[4:])
                path = target[2:] if target.startswith("b/") else target
            continue
        if line.startswith("\\"):                       # \ No newline at end of file
            continue
        prefix, body = line[:parents], line[parents:]
        if "-" in prefix:
            continue                                     # 新文件里没有这一行
        if prefix and set(prefix) == {"+"}:
            hits.extend((path, new_line, n) for n in find_names(body, names))
        new_line += 1
    return hits


def new_paths(sha, cwd=None):
    """这个提交新加、改名、复制出来的文件路径。合并提交只算对每个父提交都是新加的。"""
    out = git(["diff-tree", "-r", "--cc", "-M", "-C", "--root", "--name-status", "-z",
               "--no-commit-id", sha], cwd=cwd)
    fields, paths, i = out.split("\0"), [], 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        if status[:1] in "RC" and len(status) > 1 and status[1:].isdigit():
            paths.append(fields[i + 2])                  # R100、源路径、新路径
            i += 3
            continue
        if set(status) == {"A"}:
            paths.append(fields[i + 1])
        i += 2
    return paths


def scan_commit(sha, names, cwd=None):
    """一个提交：说明 + 新路径 + 新增的行。返回 [(地点, 词)]；地点给人看，里面的路径已打码。"""
    found = []
    message = git(["log", "-1", "--format=%B", "--no-show-signature", sha], cwd=cwd)
    found.extend(("提交说明", n) for n in find_names(message, names))
    for path in new_paths(sha, cwd):
        found.extend(("文件路径 " + mask_text(path, names), n) for n in find_names(path, names))
    patch = git(["diff-tree", "-p", "-U0", "--cc", "-M", "--root", "--no-color", "--no-ext-diff",
                 "--no-textconv", "--no-commit-id", sha], cwd=cwd)
    for path, line_no, n in scan_patch(patch, names):
        found.append(("{}:{}".format(mask_text(path, names), line_no), n))
    return found


def scan_tag(sha, names, cwd=None):
    """附注标签的说明。轻量标签（直接指向提交）没有说明，返回空。"""
    if git(["cat-file", "-t", sha], cwd=cwd).strip() != "tag":
        return []
    parts = git(["cat-file", "tag", sha], cwd=cwd).split("\n\n", 1)
    return [("标签说明", n) for n in find_names(parts[1] if len(parts) > 1 else "", names)]


def parse_updates(text):
    """pre-push 的 stdin：每行「本地 ref 本地提交 远端 ref 远端提交」。"""
    return [tuple(line.split()) for line in (text or "").splitlines() if len(line.split()) == 4]


def scan_updates(updates, remote, names, cwd=None):
    """扫这次推送的每一条 ref 更新。返回 (扫过的提交数, [(提交号, 地点, 词)])。"""
    seen, hits = set(), []
    for local_ref, local_sha, _remote_ref, remote_sha in updates:
        if ZERO.match(local_sha):
            continue                                     # 删远端分支，没有新内容
        if local_ref.startswith("refs/tags/"):
            hits.extend((local_sha[:7], where, n) for where, n in scan_tag(local_sha, names, cwd))
        for sha in commits_to_push(local_sha, remote_sha, remote, cwd):
            if sha not in seen:
                seen.add(sha)
                hits.extend((sha[:7], where, n) for where, n in scan_commit(sha, names, cwd))
    return len(seen), hits


def scan_ranges(ranges, names, cwd=None):
    """手动模式：扫 rev-list 给出的这几段提交。返回值同 scan_updates。"""
    shas = []
    for rng in ranges:
        shas += [s for s in git(["rev-list"] + rng.split(), cwd=cwd).split() if s not in shas]
    return len(shas), [(s[:7], where, n) for s in shas for where, n in scan_commit(s, names, cwd)]


# ---- 入口 --------------------------------------------------------------------------------------

def main(argv=None, stdin=None):
    harden_stdio()
    ap = argparse.ArgumentParser(description="推送前扫真名（git pre-push 钩子）")
    ap.add_argument("remote", nargs="?", default="origin", help="git 传来的远端名")
    ap.add_argument("url", nargs="?", default="", help="git 传来的远端地址（不用）")
    ap.add_argument("--range", dest="ranges", action="append", default=[],
                    help="不读 stdin，直接扫这段提交（如 origin/main..HEAD），可写多次")
    ap.add_argument("--names", help="名单路径（默认读环境变量 {}）".format(ENV_VAR))
    args = ap.parse_args(argv)

    path = args.names or os.environ.get(ENV_VAR, "")
    names = load_names(os.path.expanduser(path)) if path else None
    if not names:
        why = ("没设 {}".format(ENV_VAR) if not path else
               "名单 {} 不存在".format(path) if names is None else "名单 {} 是空的".format(path))
        say("提醒：{}，这次推送没扫真名（照常推）。装法见 docs/prepush-realnames.md。".format(why))
        return 0

    try:
        if args.ranges:
            n_commits, hits = scan_ranges(args.ranges, names)
        else:
            text = (stdin if stdin is not None else sys.stdin).read()
            n_commits, hits = scan_updates(parse_updates(text), args.remote, names)
    except ScanError as exc:
        say("推送前扫真名：扫的时候出错（{}），这次不推。".format(exc))
        say("查清楚以后可以 git push --no-verify 跳过这一次。")
        return 2

    if not hits:
        say("推送前扫真名：{} 个提交，没有命中。".format(n_commits))
        return 0
    say("推送前扫真名：{} 个提交里命中 {} 处，这次不推。".format(n_commits, len(hits)))
    for sha, where, name in hits:
        say("  {}  {}  {}".format(sha, where, mask(name)))
    say("词已打码（首字母加长度），对照名单（${}）看是哪一个。".format(ENV_VAR))
    say("改掉再推：还没推过的提交可以 git commit --amend 或 git rebase 改；已经推过的历史不改写。")
    say("确认是误报（比如虚构的词碰巧撞上）：git push --no-verify 跳过这一次。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
