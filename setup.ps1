# tiktok-live-translator 一键安装（Windows PowerShell）
# 用法: 右键“使用 PowerShell 运行”，或在终端执行  powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> [1/5] 检查 Python ..."
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
  Write-Host "❌ 未找到 python，请先安装 Python 3.9+ ： https://www.python.org/downloads/ （安装时勾选 Add to PATH）"
  exit 1
}
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) { Write-Host "❌ Python 版本过低（需要 3.9+）"; exit 1 }
Write-Host "    ✅ $(python --version)"

Write-Host "==> [2/5] 检查 ffmpeg ..."
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ff) {
  Write-Host "    ✅ 使用系统 ffmpeg"
} else {
  Write-Host "    未检测到系统 ffmpeg——没关系，依赖里自带静态版（imageio-ffmpeg），无需手动安装"
}

Write-Host "==> [3/5] 创建虚拟环境并安装依赖 ..."
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check -U pip
.\.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check -r requirements.txt
Write-Host "    ✅ 依赖安装完成"

Write-Host "==> [4/5] 下载 RNNoise 人声降噪模型 ..."
New-Item -ItemType Directory -Force -Path models | Out-Null
if (-not (Test-Path "models\bd.rnnn")) {
  try {
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/GregorR/rnnoise-models/master/beguiling-drafter-2018-08-30/bd.rnnn" -OutFile "models\bd.rnnn"
    Write-Host "    ✅ models\bd.rnnn"
  } catch {
    Write-Host "    ⚠️ 下载失败（不影响使用，只是没有背景音乐降噪）"
  }
} else { Write-Host "    ✅ 已存在，跳过" }

Write-Host "==> [5/5] 环境体检 ..."
.\.venv\Scripts\python.exe main.py --doctor

Write-Host ""
Write-Host "============================================================"
Write-Host "安装完成！使用方法："
Write-Host "  界面演示:      .\.venv\Scripts\python.exe main.py --demo"
Write-Host "  翻译直播间:    .\.venv\Scripts\python.exe main.py https://www.tiktok.com/@主播/live"
Write-Host "  （有 NVIDIA 显卡会自动启用 CUDA 加速）"
Write-Host "详细说明见 README.md"
Write-Host "============================================================"
