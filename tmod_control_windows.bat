@echo off
setlocal EnableExtensions DisableDelayedExpansion

title T-Mod Control
chcp 65001 >nul

set "LAUNCHER_DIR=%~dp0"
set "PROJECT_DIR="

if exist "%LAUNCHER_DIR%tmod_control_windows.ps1" set "PROJECT_DIR=%LAUNCHER_DIR%"
if not defined PROJECT_DIR if exist "%LAUNCHER_DIR%esgiel\tmod_control_windows.ps1" set "PROJECT_DIR=%LAUNCHER_DIR%esgiel\"
if not defined PROJECT_DIR if exist "%USERPROFILE%\Desktop\esgiel\tmod_control_windows.ps1" set "PROJECT_DIR=%USERPROFILE%\Desktop\esgiel\"

if not defined PROJECT_DIR (
  echo.
  echo  [T-MOD CONTROL] Project not found.
  echo  Expected: %USERPROFILE%\Desktop\esgiel
  echo.
  pause
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%tmod_control_windows.ps1" -ProjectDir "%PROJECT_DIR%" %*
set "TMOD_EXIT=%ERRORLEVEL%"
if not "%TMOD_EXIT%"=="0" (
  echo.
  echo  T-Mod Control finished with code %TMOD_EXIT%.
  pause
)
exit /b %TMOD_EXIT%
