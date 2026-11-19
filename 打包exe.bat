@echo off
chcp 65001 >nul 2>&1
setlocal
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
pushd "%~dp0"

echo [1/2] checking PyInstaller...
"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
  echo   not installed, installing...
  "%PY%" -m pip install pyinstaller
  if errorlevel 1 goto fail
)

echo [2/2] building dist\djkit\djkit.exe ...
"%PY%" -m PyInstaller djkit.spec --noconfirm
if errorlevel 1 goto fail

echo.
echo Done: %~dp0dist\djkit\djkit.exe
echo Size:
dir /s /-c "%~dp0dist\djkit" | find "File(s)"
popd
exit /b 0

:fail
echo.
echo BUILD FAILED - see the output above
popd
exit /b 1
