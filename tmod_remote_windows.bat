@echo off
setlocal EnableExtensions DisableDelayedExpansion

title T-Mod Remote
chcp 65001 >nul

set "REMOTE_SCRIPT=%~dp0tmod_remote_windows.ps1"
if not exist "%REMOTE_SCRIPT%" set "REMOTE_SCRIPT=%LOCALAPPDATA%\TModRemote\tmod_remote_windows.ps1"

if not exist "%REMOTE_SCRIPT%" (
  echo.
  echo  [T-MOD REMOTE] Client is not installed.
  echo  Run install_tmod_remote_windows.bat from the T-Mod repository.
  echo.
  pause
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%REMOTE_SCRIPT%" %*
set "TMOD_REMOTE_EXIT=%ERRORLEVEL%"
if not "%TMOD_REMOTE_EXIT%"=="0" (
  echo.
  echo  T-Mod Remote finished with code %TMOD_REMOTE_EXIT%.
  pause
)
exit /b %TMOD_REMOTE_EXIT%
