#!/usr/bin/env bash
# macOS 双击启动器：在 Finder 里双击本文件即可启动 TikTok 直播同传。
# 自动寻找本机已安装的 Python；找不到时弹窗引导去官网安装。
cd "$(dirname "$0")"

echo "=============================================="
echo "  TikTok 直播同传 正在启动…"
echo "  这个窗口就是程序本身，请保持打开，"
echo "  关掉它程序就会退出。"
echo "  字幕会显示在弹出的应用窗口里。"
echo "=============================================="

# find_python() 和 .app 里的启动脚本共用一份，见 tools/find_python.sh。传项目根：有 .venv
# 时先用建它的那个解释器。
if [ ! -f "tools/find_python.sh" ]; then
  echo ""
  echo "[缺文件] tools/find_python.sh 不见了，项目文件不完整，无法探测 Python。"
  echo "  请重新下载/克隆完整的项目文件夹后再试。"
  exit 1
fi
# [ -f ] 只看得到文件在不在，读不读得到要真的打开一次：以前读不了时 find_python 没定义，下面那一支打印
# 「未检测到 Python」、还打开 python.org，其实这一步根本没去找 Python（.app 启动器 2026-10-29 同一个问题，
# 见那边的注释）。「终端」整个读不了这个文件夹时连本文件都打不开、走不到这里（sandbox-exec 拒掉整个文件夹
# 的 file-read-data 实测：bash 在窗口里报「本文件路径: Operation not permitted」，退出码 126）；
# 这里接住的是单个文件读不了的情况
if ! err="$( { : < "tools/find_python.sh"; } 2>&1 )"; then
  err="${err##*: }"
  echo ""
  echo "[读不了程序文件] tools/find_python.sh 打不开，系统返回「${err:-?}」，这一步没有去找 Python。"
  echo "  请确认这个文件夹里的文件本账户读得到，或重新下载/克隆完整的项目文件夹后再试。"
  exit 1
fi
. "tools/find_python.sh"
if ! type find_python >/dev/null 2>&1; then
  echo ""
  echo "[文件不完整] tools/find_python.sh 里没有探测 Python 的那一段。"
  echo "  请重新下载/克隆完整的项目文件夹后再试。"
  exit 1
fi

PY="$(find_python "$PWD")" || {
  echo ""
  echo "[未检测到 Python 3.10 或更新的版本] 需要先安装 Python 3.10 或更新的版本才能运行（免费，约 2 分钟）："
  echo "  1. 已为你打开下载页 https://www.python.org/downloads/"
  echo "  2. 下载 macOS 安装包并完成安装"
  echo "  3. 装好后重新双击本文件即可"
  open "https://www.python.org/downloads/"
  # 上面的终端说明保持中文；这个对话框在 Python 之前弹出、读不到界面语言设置，
  # 所以中文一段、英文一段并列（用户 09-28 决定 4；与 .app 启动器的同一句一致）
  osascript -e 'display dialog "需要先安装 Python 3.10 或更新的版本才能运行本工具（免费，约 2 分钟）。\n\n已为你打开下载页面 python.org/downloads——下载 macOS 安装包并安装，装好后重新双击启动即可。\n\nPython 3.10 or later is needed to run this app (free, about 2 minutes).\n\nThe python.org/downloads page is now open. Download and install the macOS installer, then double-click the app again." with title "TikTok 直播同传 · TikTok Live Translator" buttons {"OK"} default button 1 with icon note' >/dev/null 2>&1
  exit 1
}

exec "$PY" main.py "$@"
