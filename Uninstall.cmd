@echo off
rem Double-click to uninstall harvesting-dune. Your learned data (the data folder) stays.
rem Extra arguments go to install.ps1, for example: Uninstall.cmd -NonInteractive
setlocal EnableExtensions DisableDelayedExpansion
title harvesting-dune uninstall
set "HD_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "HD_LOG=%TEMP%\harvesting-dune-uninstall.log"
set "HD_PAUSE=1"
echo(%*| "%SystemRoot%\System32\findstr.exe" /i /c:"-NonInteractive" >nul && set "HD_PAUSE="

if not exist "%~dp0scripts\install.ps1" goto notextracted
if not exist "%HD_PS%" goto nopowershell

echo This removes harvesting-dune: autorun and the program files. Your data folder stays.
if defined HD_PAUSE echo To cancel, close this window. To continue,
if defined HD_PAUSE pause
"%HD_PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -Uninstall -FromPackage -LogFile "%HD_LOG%" %*
set "HD_RC=%ERRORLEVEL%"
echo.
if not "%HD_RC%"=="0" echo The uninstall did not finish (exit code %HD_RC%). The reason is above.
echo The log is here:
echo   "%HD_LOG%"
if defined HD_PAUSE pause
exit /b %HD_RC%

:notextracted
echo This folder does not contain the harvesting-dune scripts.
echo How to fix: use Uninstall.cmd in the extracted ZIP folder or in the install folder.
if defined HD_PAUSE pause
exit /b 1

:nopowershell
echo Windows PowerShell was not found on this PC.
if defined HD_PAUSE pause
exit /b 1
