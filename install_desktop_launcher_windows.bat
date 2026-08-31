@echo off
setlocal EnableExtensions DisableDelayedExpansion

title Install T-Mod Control Center
chcp 65001 >nul

set "INSTALLER=%~dp0install_tmod_control_windows.ps1"

echo.
echo ============================================================
echo   T-Mod Control Center / Native EXE
echo ============================================================
echo.

if not exist "%INSTALLER%" goto source_missing
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%" -ProjectDir "%~dp0"
if errorlevel 1 goto copy_failed

if exist "%~dp0configure_auto_update_windows.ps1" (
  powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0configure_auto_update_windows.ps1" -ProjectDir "%~dp0" -IntervalMinutes 2
  if errorlevel 1 echo [WARN] Automatic update watcher will be installed by the next T-Mod start.
)

echo [OK] Native Control Center installed on the Desktop.
echo.
echo Open "T-Mod Control.exe" on the Desktop. Use arrow keys to
echo update, start, diagnose and manage individual services.
echo Safe automatic updates from origin/main remain enabled.
echo.
pause
exit /b 0

:source_missing
echo [FAIL] Native installer was not found:
echo        %INSTALLER%
goto failed

:copy_failed
echo [FAIL] Could not install T-Mod Control.exe.

:failed
echo.
pause
exit /b 1
