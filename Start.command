#!/usr/bin/env bash
# macOS 双击启动器：在 Finder 里双击本文件即可启动 TikTok 直播同传
cd "$(dirname "$0")"
exec python3 main.py
