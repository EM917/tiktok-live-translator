#!/usr/bin/env bash
# 发版辅助：给已存在的 GitHub release 附上带顶层目录的源码 ZIP。
# 用法：bash release.sh v0.8.0
set -euo pipefail

TAG="${1:?用法: bash release.sh v0.8.0}"
NAME="TikTok-Live-Translator-${TAG}"

command -v gh >/dev/null || { echo "需要 GitHub CLI（brew install gh）"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh 未登录：先运行 gh auth login"; exit 1; }

trap 'rm -f "${NAME}.zip"' EXIT   # 失败也不留残余 zip 在仓库根目录
git archive --format=zip --prefix="TikTok Live Translator/" -o "${NAME}.zip" "$TAG"
gh release upload "$TAG" "${NAME}.zip" --clobber
echo "已上传 ${NAME}.zip 到 release ${TAG}"
