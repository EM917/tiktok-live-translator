@echo off
REM Windows 双击启动器：双击本文件即可启动 TikTok 直播同传
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
    python main.py
) else (
    py -3 main.py
)
if %errorlevel% neq 0 pause
