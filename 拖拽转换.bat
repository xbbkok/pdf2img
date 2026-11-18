@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem 把 PDF 文件拖到本 bat 上即可转换；也可以直接双击，会把当前目录下所有 PDF 转成 PNG。
if "%~1"=="" (
    echo 正在转换当前目录下的全部 PDF -^> out\  ...
    python "pdf2img.py" . -o "out" -f png
) else (
    python "pdf2img.py" %* -o "out" -f png
)

echo.
echo 转换结束，输出目录：%~dp0out\
echo.
pause
