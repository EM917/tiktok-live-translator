#!/usr/bin/env bash
# macOS 双击启动器：在 Finder 里双击本文件即可启动 TikTok 直播同传。
# 自动寻找本机已安装的 Python；找不到时弹窗引导去官网安装。
cd "$(dirname "$0")"

echo "=============================================="
echo "  TikTok 直播同传 正在启动…"
echo "  这个窗口是翻译引擎，请保持打开；"
echo "  字幕会显示在弹出的应用窗口里。"
echo "=============================================="

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

PY="$(find_python)" || {
  echo ""
  echo "[未检测到 Python] 需要先安装 Python 才能运行（免费，约 2 分钟）："
  echo "  1. 已为你打开下载页 https://www.python.org/downloads/"
  echo "  2. 下载 macOS 安装包并完成安装"
  echo "  3. 装好后重新双击本文件即可"
  open "https://www.python.org/downloads/"
  osascript -e 'display dialog "需要先安装 Python 才能运行本工具（免费，约 2 分钟）。\n\n已为你打开下载页面 python.org/downloads——下载 macOS 安装包并安装，装好后重新双击启动即可。" with title "TikTok 直播同传" buttons {"好"} default button 1 with icon note' >/dev/null 2>&1
  exit 1
}

exec "$PY" main.py "$@"
