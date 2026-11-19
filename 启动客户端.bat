@echo off
rem djkit - desktop client launcher
rem Keep CRLF line endings: LF-only batch files misbehave with labels and goto.
setlocal EnableExtensions

cd /d "%~dp0"

set "PYW="
set "CAND1=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"
if exist "%CAND1%" set "PYW=%CAND1%"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PYW if exist "C:\Python313\pythonw.exe" set "PYW=C:\Python313\pythonw.exe"
if not defined PYW if exist "C:\Python312\pythonw.exe" set "PYW=C:\Python312\pythonw.exe"
if not defined PYW for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"

if not defined PYW goto nopython

rem --check: print what would be launched and exit (for troubleshooting)
if /i "%~1"=="--check" (
  echo pythonw: %PYW%
  echo project: %CD%
  exit /b 0
)

rem start the app; pythonw has no console, so this window closes right away
start "" "%PYW%" -m djkit app
exit /b 0

:nopython
echo.
echo pythonw.exe not found.
echo.
echo Install Python and the dependencies first, inside this folder:
echo     pip install -r requirements.txt
echo.
pause
exit /b 1
