@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title T-Mod Consensus Emergency Repair
chcp 65001 >nul

set DB_PATH=C:\Users\Admin\Documents\SGLDiscordBot\data\tmod.db

echo.
echo ============================================================
echo   T-Mod emergency repair: cancel stuck finalizing consensus
echo ============================================================
echo.
echo This does not delete the database.
echo It only quarantines active consensus sessions stuck in finalizing.
echo.
echo Stop the bot container before continuing:
echo   docker compose stop tmod-discord-bot
echo.
pause

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0repair_cancel_finalizing_consensus_windows.ps1" -DatabasePath "%DB_PATH%"
if errorlevel 1 (
  echo.
  echo [FAIL] Repair did not complete. Read the message above.
  pause
  exit /b 1
)

echo.
echo [OK] Repair finished. Start the bot with run_windows.bat.
pause
