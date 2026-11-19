@echo off
chcp 65001 >nul 2>&1
setlocal
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
pushd "%~dp0"
"%PY%" -m djkit %*
popd
