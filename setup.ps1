# tiktok-live-translator 一键安装（Windows PowerShell）
# 用法: 右键“使用 PowerShell 运行”，或在终端执行  powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> [1/5] 检查 Python ..."
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
  Write-Host "❌ 未找到 python，请先安装 Python 3.10+ ： https://www.python.org/downloads/ （安装时勾选 Add to PATH）"
  exit 1
}
# 下限 3.10（用户 10-22 决定 D14，与 app/bootstrap.MIN_PYTHON 一致）
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { Write-Host "❌ Python 版本过低（需要 3.10+）"; exit 1 }
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
# -c constraints.txt：测试过的那一套版本（I4，写法和理由见那个文件的开头）
.\.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check -r requirements.txt -c constraints.txt
Write-Host "    ✅ 依赖安装完成"

Write-Host "==> [4/5] 下载 RNNoise 人声降噪模型 ..."
New-Item -ItemType Directory -Force -Path models | Out-Null
# 固定到上游的一个提交，下载后核对 sha256（专家审查 I5、计划审查 Q9）。和 app/pipeline.py 的 RNNOISE_URL、
# RNNOISE_SHA256 是同一对，tests/test_setup_rnnoise_pin.py 钉着两边一致。先下到 .part，对上了才改名；
# 对不上就不留文件，第一次开播时程序会自己再下载一次
$RnnoiseUrl = "https://raw.githubusercontent.com/GregorR/rnnoise-models/6c2c1301c30a6be5ddf93704a693f4956ce2aa55/beguiling-drafter-2018-08-30/bd.rnnn"
$RnnoiseSha256 = "ae3f7411e1e6a884f839a4a145c394408398f09854dbc1216ee02faafc98a17b"
if (-not (Test-Path "models\bd.rnnn")) {
  try {
    Invoke-WebRequest -Uri $RnnoiseUrl -OutFile "models\bd.rnnn.part"
    $got = (Get-FileHash -Algorithm SHA256 "models\bd.rnnn.part").Hash.ToLower()
    if ($got -eq $RnnoiseSha256) {
      Move-Item -Force "models\bd.rnnn.part" "models\bd.rnnn"
      Write-Host "    ✅ models\bd.rnnn"
    } else {
      Remove-Item -Force "models\bd.rnnn.part"
      Write-Host "    ⚠️ 下载下来的文件和校验值对不上，没有保存（第一次开播时程序会再下载一次）"
    }
  } catch {
    Remove-Item -Force -ErrorAction SilentlyContinue "models\bd.rnnn.part"
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
