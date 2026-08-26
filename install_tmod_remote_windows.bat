@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Install T-Mod Remote
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_tmod_remote_windows.ps1" -SourceDir "%~dp0"
if errorlevel 1 (
  echo.
  echo  T-Mod Remote installation failed.
  pause
  exit /b 1
)
echo.
echo  T-Mod Remote installed on the Desktop.
pause
exit /b 0
