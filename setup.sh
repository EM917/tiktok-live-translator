#!/usr/bin/env bash
# tiktok-live-translator 一键安装（macOS / Linux）
# 用法: bash setup.sh
set -e
cd "$(dirname "$0")"

echo "==> [1/5] 检查 Python ..."
PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  echo "❌ 未找到 python3，请先安装 Python 3.9+（macOS: brew install python / Linux: 发行版包管理器）"
  exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "❌ Python 版本过低（需要 3.9+），当前: $("$PY" --version)"; exit 1; }
echo "    ✅ $("$PY" --version)"

echo "==> [2/5] 检查 ffmpeg ..."
if command -v ffmpeg >/dev/null; then
  echo "    ✅ 使用系统 ffmpeg: $(ffmpeg -version 2>/dev/null | head -1)"
else
  echo "    未检测到系统 ffmpeg——没关系，依赖里自带静态版（imageio-ffmpeg），无需手动安装"
fi

echo "==> [3/5] 创建虚拟环境并安装依赖（Apple Silicon 会自动包含 MLX GPU 后端）..."
"$PY" -m venv .venv
.venv/bin/pip install --quiet --disable-pip-version-check -U pip
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
echo "    ✅ 依赖安装完成"

echo "==> [4/5] 下载 RNNoise 人声降噪模型 ..."
mkdir -p models
if [ ! -f models/bd.rnnn ]; then
  curl -sL -o models/bd.rnnn \
    "https://raw.githubusercontent.com/GregorR/rnnoise-models/master/beguiling-drafter-2018-08-30/bd.rnnn" \
    && echo "    ✅ models/bd.rnnn" \
    || echo "    ⚠️ 下载失败（不影响使用，只是没有背景音乐降噪；稍后可重跑 setup.sh）"
else
  echo "    ✅ 已存在，跳过"
fi

echo "==> [5/5] 环境体检（根据你的电脑硬件自动选择识别后端和模型）..."
.venv/bin/python main.py --doctor || true

cat <<'EOF'

============================================================
安装完成！使用方法：

  先看看界面（不连直播）:
    .venv/bin/python main.py --demo

  翻译真实直播间:
    .venv/bin/python main.py "https://www.tiktok.com/@主播用户名/live"

  （可选）本地离线翻译，质量远好于免费谷歌接口:
    brew install ollama && ollama pull translategemma:4b
    然后保持 ollama serve 在运行，工具会自动用它。

详细说明见 README.md
============================================================
EOF
