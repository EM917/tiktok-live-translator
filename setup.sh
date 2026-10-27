#!/usr/bin/env bash
# tiktok-live-translator 一键安装（macOS / Linux）
# 用法: bash setup.sh
set -e
cd "$(dirname "$0")"

echo "==> [1/5] 检查 Python ..."
PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  echo "❌ 未找到 python3，请先安装 Python 3.10+（macOS: brew install python / Linux: 发行版包管理器）"
  exit 1
fi
# 下限 3.10（用户 10-22 决定 D14，与 app/bootstrap.MIN_PYTHON 一致）
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "❌ Python 版本过低（需要 3.10+），当前: $("$PY" --version)"; exit 1; }
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
# -c constraints.txt：测试过的那一套版本（I4，写法和理由见那个文件的开头）
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt -c constraints.txt
echo "    ✅ 依赖安装完成"

echo "==> [4/5] 下载 RNNoise 人声降噪模型 ..."
mkdir -p models
# 固定到上游的一个提交，下载后核对 sha256（专家审查 I5、计划审查 Q9）。和 app/pipeline.py 的 RNNOISE_URL、
# RNNOISE_SHA256 是同一对，tests/test_setup_rnnoise_pin.py 钉着两边一致。先下到 .part，对上了才改名；
# 对不上就不留文件，第一次开播时程序会自己再下载一次
RNNOISE_URL="https://raw.githubusercontent.com/GregorR/rnnoise-models/6c2c1301c30a6be5ddf93704a693f4956ce2aa55/beguiling-drafter-2018-08-30/bd.rnnn"
RNNOISE_SHA256="ae3f7411e1e6a884f839a4a145c394408398f09854dbc1216ee02faafc98a17b"
if [ ! -f models/bd.rnnn ]; then
  if curl -sfL -o models/bd.rnnn.part "$RNNOISE_URL"; then
    got=$( (shasum -a 256 models/bd.rnnn.part 2>/dev/null || sha256sum models/bd.rnnn.part) | cut -d' ' -f1 )
    if [ "$got" = "$RNNOISE_SHA256" ]; then
      mv models/bd.rnnn.part models/bd.rnnn && echo "    ✅ models/bd.rnnn"
    else
      rm -f models/bd.rnnn.part
      echo "    ⚠️ 下载下来的文件和校验值对不上，没有保存（第一次开播时程序会再下载一次）"
    fi
  else
    rm -f models/bd.rnnn.part
    echo "    ⚠️ 下载失败（不影响使用，只是没有背景音乐降噪；稍后可重跑 setup.sh）"
  fi
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
