#!/usr/bin/env bash
# 供 Start.command 和 TikTokLiveTranslator.app 的启动脚本一起 source 的探测函数。
# 这份文件本身不执行任何东西——只定义 find_python，调用方 source 之后自己调用。
#
# 按常见安装位置寻找 Python 3.9+。顺序很重要：python.org / Homebrew 的真实
# 安装优先；苹果自带的 /usr/bin/python3 只是开发者工具的垫片，未装过 Xcode
# 命令行工具时一执行就会弹「安装开发者工具」对话框，必须先确认装过才能碰。
find_python() {
  local candidates=() p v devdir
  # python.org 官方框架：全部版本按新到旧依次候选（老版本过不了 ≥3.9 检查时
  # 还能落到下一个，不能只挑一个）
  for v in $(ls /Library/Frameworks/Python.framework/Versions 2>/dev/null \
             | grep -E '^3\.[0-9]+$' | sort -t. -k2,2nr); do
    candidates+=("/Library/Frameworks/Python.framework/Versions/$v/bin/python3")
  done
  candidates+=(/opt/homebrew/bin/python3 /usr/local/bin/python3)
  # /usr/bin/python3 垫片：必须确认它真正派发到的文件存在才能执行——
  # xcode-select -p 只回显配置路径、不校验目录还在不在
  devdir="$(xcode-select -p 2>/dev/null)"
  if [ -n "$devdir" ] && [ -x "$devdir/usr/bin/python3" ]; then
    candidates+=(/usr/bin/python3)
  fi
  for p in "${candidates[@]}"; do
    if [ -x "$p" ] && "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      echo "$p"
      return 0
    fi
  done
  # 终端登录 shell 里 PATH 是全的，pyenv/conda 等安装也一并尝试
  p="$(command -v python3 2>/dev/null)"
  if [ -n "$p" ] && [ "$p" != "/usr/bin/python3" ] && \
     "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    echo "$p"
    return 0
  fi
  return 1
}
