@echo off
REM Windows 双击启动器：双击本文件即可启动 TikTok 直播同传
chcp 65001 >nul
title TikTok 直播同传
echo ==============================================
echo   TikTok 直播同传 正在启动…
echo   这个黑色窗口是翻译引擎，请保持打开；
echo   字幕会显示在弹出的应用窗口里。
echo ==============================================
cd /d "%~dp0"

REM 先试 py 启动器：python.org 的安装包默认自带，不依赖「Add to PATH」勾选。
REM 与 macOS 侧同样要求 3.9+——版本太旧就落到下一个候选，不能死在这里
py -3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if %errorlevel%==0 (
    py -3 main.py %*
    goto :after
)

REM 再实测 python 是否真能用——微软商店的假 python 会在这里失败，
REM 不能只用 where 判断（假 python 也在 PATH 上，where 永远成功）
python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if %errorlevel%==0 (
    python main.py %*
    goto :after
)

echo.
echo [未检测到 Python] 需要先安装 Python 才能运行（免费，约 2 分钟）：
echo   1. 已为你打开下载页 https://www.python.org/downloads/
echo   2. 下载 Windows 安装包并完成安装（按默认选项装即可）
echo   3. 装好后重新双击本文件
echo.
echo （在微软商店里搜索 Python 安装也可以）
start https://www.python.org/downloads/
pause
exit /b 1

:after
if %errorlevel% neq 0 (
    echo.
    echo 程序已退出。上面如有报错信息，请截图反馈；直接关闭本窗口即可。
    pause
)
