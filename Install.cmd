@echo off
rem Double-click to install or update harvesting-dune. Runs scripts\install.ps1 and keeps a log.
rem Extra arguments go to install.ps1, for example: Install.cmd -NoAutorun -NonInteractive
setlocal EnableExtensions DisableDelayedExpansion
title harvesting-dune setup
set "HD_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "HD_SRC=%~dp0"
set "HD_LOG=%TEMP%\harvesting-dune-install.log"
set "HD_PAUSE=1"
echo(%*| "%SystemRoot%\System32\findstr.exe" /i /c:"-NonInteractive" >nul && set "HD_PAUSE="

if not exist "%~dp0scripts\install.ps1" goto notextracted
if not exist "%HD_PS%" goto nopowershell

echo Installing harvesting-dune. The first time, this takes a few minutes.
echo.
rem files from a downloaded ZIP carry the "from the internet" mark: remove it from our own files
"%HD_PS%" -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath $env:HD_SRC -Recurse -File | Unblock-File -ErrorAction SilentlyContinue"
"%HD_PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -FromPackage -InstallUv -LogFile "%HD_LOG%" %*
set "HD_RC=%ERRORLEVEL%"
echo.
if not "%HD_RC%"=="0" goto failed
echo The install log is here:
echo   "%HD_LOG%"
if defined HD_PAUSE pause
exit /b 0

:failed
echo The install did not finish (exit code %HD_RC%). The reason and the fix are above.
echo The install log is here:
echo   "%HD_LOG%"
if not defined HD_PAUSE exit /b %HD_RC%
echo Press a key to open the log in Notepad.
pause >nul
start "" notepad.exe "%HD_LOG%"
exit /b %HD_RC%

:notextracted
echo This folder does not contain the full harvesting-dune program.
echo Windows does this when you open Install.cmd inside the ZIP file.
echo How to fix: right-click the ZIP file, select "Extract All", open the new folder,
echo and double-click Install.cmd there.
if defined HD_PAUSE pause
exit /b 1

:nopowershell
echo Windows PowerShell was not found on this PC.
echo How to fix: install the Windows updates. Windows 10 and 11 include Windows PowerShell 5.1.
if defined HD_PAUSE pause
exit /b 1
