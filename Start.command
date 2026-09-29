#!/usr/bin/env bash
# macOS 双击启动器：在 Finder 里双击本文件即可启动 TikTok 直播同传。
# 自动寻找本机已安装的 Python；找不到时弹窗引导去官网安装。
cd "$(dirname "$0")"

echo "=============================================="
echo "  TikTok 直播同传 正在启动…"
echo "  这个窗口是翻译引擎，请保持打开；"
echo "  字幕会显示在弹出的应用窗口里。"
echo "=============================================="

# find_python() 和 .app 里的启动脚本共用一份，见 tools/find_python.sh。
if [ ! -f "tools/find_python.sh" ]; then
  echo ""
  echo "[缺文件] tools/find_python.sh 不见了，项目文件不完整，无法探测 Python。"
  echo "  请重新下载/克隆完整的项目文件夹后再试。"
  exit 1
fi
. "tools/find_python.sh"

PY="$(find_python)" || {
  echo ""
  echo "[未检测到 Python] 需要先安装 Python 才能运行（免费，约 2 分钟）："
  echo "  1. 已为你打开下载页 https://www.python.org/downloads/"
  echo "  2. 下载 macOS 安装包并完成安装"
  echo "  3. 装好后重新双击本文件即可"
  open "https://www.python.org/downloads/"
  # 上面的终端说明保持中文；这个对话框在 Python 之前弹出、读不到界面语言设置，
  # 所以中文一段、英文一段并列（用户 09-28 决定 4；与 .app 启动器的同一句一致）
  osascript -e 'display dialog "需要先安装 Python 才能运行本工具（免费，约 2 分钟）。\n\n已为你打开下载页面 python.org/downloads——下载 macOS 安装包并安装，装好后重新双击启动即可。\n\nPython is needed to run this app (free, about 2 minutes).\n\nThe python.org/downloads page is now open. Download and install the macOS installer, then double-click the app again." with title "TikTok 直播同传 · TikTok Live Translator" buttons {"OK"} default button 1 with icon note' >/dev/null 2>&1
  exit 1
}

exec "$PY" main.py "$@"
