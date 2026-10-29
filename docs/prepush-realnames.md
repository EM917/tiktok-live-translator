# 推送前扫真名：给维护者的说明

这个仓库是公开的，推上去的东西删不干净：提交历史、PR 页面、别人的 fork 都留着。真实的主播账号名、
品牌名、店铺名不进仓库，测试和文档里的例子一律虚构（`acme` 这类）。以前靠推之前手动扫一遍，漏一次
就公开了。`tools/prepush_realnames.py` 是一个 git pre-push 钩子：每次推送前拿本机名单扫一遍这次要推的
提交，扫到就不推。

名单不在仓库里，也不该在任何仓库里。每台开发用的电脑自己放一份。

## 扫什么

这次要推、远端还没有的每个提交：

- 提交说明；
- diff 里新增的行。删掉的行不扫：删真名正是要做的事；
- 新加、改名、复制出来的文件路径（比如 `profiles/<主播>.txt`）；
- 推附注标签时，标签说明。

合并提交只看合并时自己写的那部分（git 的 `--cc`）：把 main 合进分支、或者 rebase 到新 main 上再推，
main 上早已公开的内容不会再挡一次。本机知道的远端分支上已有的提交都不重扫。

匹配不分大小写、按子串。词内部的分隔符（连字符、下划线、点、空格）彼此通用，也可以省掉：名单里写
`acme-fictional-shop`，`acme_fictional_shop`、`Acme Fictional Shop`、`AcmeFictionalShop` 都算命中。
同一处只算一个词。命中时只打印打码后的词（首字母加长度，如 `z9`）、提交号、文件和行号；路径里
带着名单里的词也打码。名单原文不出现在输出里。

| 情况 | 退出码 | 推不推 |
|---|---|---|
| 没有命中 | 0 | 推 |
| 命中 | 1 | 不推 |
| 扫的时候出错（git 读不出提交等） | 2 | 不推（这是挡真名的闸，宁可多挡一次） |
| 没设 `TLT_REALNAMES`、名单不存在或是空的 | 0 | 推，打印一句提醒 |

没有名单的电脑（CI、别人的克隆）推送不受影响。

## 名单

纯文本、UTF-8，每行一个；空行和 `#` 开头的行忽略；一个字符的条目不用（会命中几乎每一行）。用环境变量
`TLT_REALNAMES` 指它的路径。

- 放在仓库外、iCloud 桌面外。macOS 上推荐 `~/Library/Application Support/tlt-dev/realnames.txt`，
  目录 700、文件 600。
- 收什么：主播账号名、品牌名（词表文件名和显示名）、店铺名。名字里有分隔的，照有分隔的写一条就够
  （分隔符换成别的、或者省掉，都由脚本认）；反过来不行：只写连在一起的 `acmefictionalshop`，挡不住
  `acme_fictional_shop`。太短（不到 4 个字符）或就是一个常见词的不收，不然每次推送都误报。
- 名单本身不贴进任何对话、日志、PR、提交说明。要说它有多少条，只说条数。

## 装法（macOS）

钩子装在 `.git/hooks/`，同一个仓库的所有 worktree 共用这一份。钩子调用的是拷到仓库外的那份脚本，
不是工作目录里的 `tools/prepush_realnames.py`：推一个还没有这个文件的旧分支时，钩子照样能跑。

```sh
DEV="$HOME/Library/Application Support/tlt-dev"
mkdir -p "$DEV" && chmod 700 "$DEV"
cp tools/prepush_realnames.py "$DEV/pre-push.py"
touch "$DEV/realnames.txt" && chmod 600 "$DEV/realnames.txt"   # 然后用编辑器填名单
```

再写 `.git/hooks/pre-push`（在主仓库的 `.git` 里，不是 worktree 里的那个 `.git` 文件），`chmod 755`：

```sh
#!/bin/sh
DEV="$HOME/Library/Application Support/tlt-dev"
export TLT_REALNAMES="${TLT_REALNAMES:-$DEV/realnames.txt}"
if [ ! -f "$DEV/pre-push.py" ]; then
  echo "提醒：没找到 $DEV/pre-push.py，这次推送没扫真名（照常推）。" >&2
  exit 0
fi
exec python3 -I "$DEV/pre-push.py" "$@"
```

`python3` 换成这台电脑上确定存在的那个（3.10 以上）。`-I` 让脚本不带当前目录的路径，脚本只用标准库。

`tools/prepush_realnames.py` 改过以后，重新拷一次到 `$DEV/pre-push.py`。

### 装好以后验一次

不用真推。临时名单里放一个虚构的词，在一个临时分支上提交一处带这个词的改动，再像 git 那样直接调钩子：

```sh
printf 'quillmoss_demo\n' > /tmp/test-names.txt
git switch -c tmp/hook-demo
echo 'HOST = "Quillmoss_Demo"' > demo_fixture.py && git add demo_fixture.py && git commit -m demo
echo "refs/heads/tmp/hook-demo $(git rev-parse HEAD) refs/heads/tmp/hook-demo 0000000000000000000000000000000000000000" \
  | TLT_REALNAMES=/tmp/test-names.txt "$(git rev-parse --git-common-dir)/hooks/pre-push" origin x
# 应该打印 demo_fixture.py:1  q14，退出码 1。验完删掉临时分支和临时名单。
```

### Windows

git 在 Git Bash 里跑钩子，上面那段 `.git/hooks/pre-push` 照样能用。把 `DEV` 换成仓库外的一个目录
（比如 `$APPDATA/tlt-dev`），`python3` 换成 `py -3` 或 Python 的完整路径。

## 命中了怎么办

- 还没推过的提交：改掉那几处，用 `git commit --amend` 或 `git rebase` 改写，再推。已经推过的历史不改写。
- 打码只给首字母和长度，对照名单看是哪一个词。
- 确认是误报（虚构的词碰巧和名单里的某条撞上）：这一次用 `git push --no-verify` 跳过。名单里某条
  老误报，就把它从名单里拿掉或者写得更具体。

## 手动扫一段提交

不经 git、不推，直接扫一段提交（比如推一个已经攒了很多提交的分支之前先看一眼）：

```sh
TLT_REALNAMES="$HOME/Library/Application Support/tlt-dev/realnames.txt" \
  python3 tools/prepush_realnames.py --range origin/main..HEAD
```

`--range` 可以写多次。只扫提交，不扫 PR 标题和正文：PR 的标题、正文写之前自己对一遍名单。

## 仓库里已经有的真名

钩子只挡新加的。main 上已经有的（以及历史里的）按「改当前文件、不改历史」处理，由人逐处决定怎么换成
虚构的；不在这个工具里自动改。
