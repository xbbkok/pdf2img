@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "pdf2img_gui.py"
    exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "pdf2img_gui.py"
    exit /b 0
)

echo.
echo 未找到 Python。请先安装 Python 3.9 或更高版本（安装时勾选 Add Python to PATH）。
echo 安装后重新运行本文件即可。
echo.
pause
