@echo off
chcp 65001 >nul 2>&1
title djkit 解说工坊
cd /d "%~dp0"

rem 优先用项目托管环境（依赖已装好）；找不到才退回 PATH 里的 python
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -m djkit gui %*

echo.
echo 服务已停止。按任意键关闭窗口。
pause >nul
