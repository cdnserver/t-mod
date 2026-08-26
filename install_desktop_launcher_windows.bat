@echo off
setlocal EnableExtensions DisableDelayedExpansion

title Install T-Mod Control
chcp 65001 >nul

set "SOURCE_FILE=%~dp0tmod_control_windows.bat"
for %%D in ("%~dp0..") do set "DESKTOP_DIR=%%~fD"
set "TARGET_FILE=%DESKTOP_DIR%\T-Mod Control.bat"

echo.
echo ============================================================
echo   T-Mod Control Installer
echo ============================================================
echo.

if not exist "%SOURCE_FILE%" goto source_missing

copy /y "%SOURCE_FILE%" "%TARGET_FILE%" >nul
if errorlevel 1 goto copy_failed

if exist "%~dp0configure_auto_update_windows.ps1" (
  powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0configure_auto_update_windows.ps1" -ProjectDir "%~dp0" -IntervalMinutes 2
  if errorlevel 1 echo [WARN] Automatic update watcher will be installed by the next T-Mod start.
)

echo [OK] Desktop launcher installed:
echo      %TARGET_FILE%
echo.
echo Open "T-Mod Control.bat" on the Desktop. Use arrow keys to
echo update, start, diagnose and manage individual services.
echo Safe automatic updates from origin/main remain enabled.
echo.
pause
exit /b 0

:source_missing
echo [FAIL] Source launcher was not found:
echo        %SOURCE_FILE%
goto failed

:copy_failed
echo [FAIL] Could not create:
echo        %TARGET_FILE%

:failed
echo.
pause
exit /b 1
