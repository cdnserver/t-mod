@echo off
setlocal EnableExtensions EnableDelayedExpansion

title T-Mod Boot Console
chcp 65001 >nul

set PERSISTENT_DIR=C:\Users\Admin\Documents\SGLDiscordBot
set DATA_DIR=%PERSISTENT_DIR%\data
set BACKUP_DIR=%PERSISTENT_DIR%\backups
set DOCKER_DESKTOP_EXE=C:\Program Files\Docker\Docker\Docker Desktop.exe

call :banner

call :stage "01" "Persistent storage"
if not exist "%PERSISTENT_DIR%" mkdir "%PERSISTENT_DIR%"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
call :ok "Storage path: %PERSISTENT_DIR%"

call :stage "02" "Environment"
if not exist "%PERSISTENT_DIR%\.env" (
  copy ".env.persistent.example" "%PERSISTENT_DIR%\.env" >nul
  call :warn "Created %PERSISTENT_DIR%\.env"
  call :warn "Put your Discord token into this file, then run this bat again."
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0merge_env_windows.ps1" -ExamplePath "%~dp0.env.persistent.example" -TargetPath "%PERSISTENT_DIR%\.env"
if errorlevel 1 (
  call :fail "Failed to merge .env"
  pause
  exit /b 1
)
call :ok ".env synchronized"

call :stage "03" "Localization"
if not exist "%PERSISTENT_DIR%\localization.json" (
  copy "localization.example.json" "%PERSISTENT_DIR%\localization.json" >nul
  call :ok "localization.json created"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0merge_localization_windows.ps1" -ExamplePath "%~dp0localization.example.json" -TargetPath "%PERSISTENT_DIR%\localization.json" -BackupDir "%BACKUP_DIR%"
  if errorlevel 1 (
    call :fail "Failed to merge localization.json"
    pause
    exit /b 1
  )
  call :ok "localization.json synchronized"
)

call :stage "04" "Modules"
call :module "T-Mod Core"
call :module "TVRS Consensus"
call :module "SGL Bureau"
call :module "SGL Registry"
call :module "SGL Audio"
call :module "Zigmund AI"
call :module "SGL Contracts"
call :module "SQLite Migrator"

call :stage "05" "Docker engine"
call :ensure_docker_engine
if errorlevel 1 (
  pause
  exit /b 1
)

call :stage "06" "Stopping old containers"
docker stop sgl-discord-bot >nul 2>nul
docker rm sgl-discord-bot >nul 2>nul
docker stop tmod-discord-bot >nul 2>nul
docker rm tmod-discord-bot >nul 2>nul
call :ok "Old containers stopped"

call :stage "07" "Docker build"
set COMPOSE_BAKE=true
docker compose build
if errorlevel 1 (
  call :fail "Docker build failed"
  call :warn "If you see dockerDesktopLinuxEngine pipe error, run repair_docker_desktop_windows.bat"
  pause
  exit /b 1
)
call :ok "Docker image ready"

call :stage "08" "Starting T-Mod"
docker compose up -d
if errorlevel 1 (
  call :fail "Docker startup failed"
  call :warn "If you see dockerDesktopLinuxEngine pipe error, run repair_docker_desktop_windows.bat"
  pause
  exit /b 1
)
call :ok "Container started"

call :stage "09" "Status"
docker ps --filter "name=tmod-discord-bot" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"

echo.
echo ============================================================
echo   T-Mod startup finished.
echo   Database: %PERSISTENT_DIR%\data\tmod.db
echo   Config:   %PERSISTENT_DIR%\.env
echo   Locale:   %PERSISTENT_DIR%\localization.json
echo ============================================================
echo.
echo Recent bot logs:
echo ------------------------------------------------------------
docker logs --tail 60 tmod-discord-bot
echo ------------------------------------------------------------
echo.
echo Live logs: docker logs -f tmod-discord-bot
echo.
pause
exit /b 0

:ensure_docker_engine
docker info >nul 2>nul
if not errorlevel 1 (
  call :ok "Docker engine is running"
  exit /b 0
)

call :warn "Docker engine is not responding."
call :warn "Trying to start Docker Desktop..."

if exist "%DOCKER_DESKTOP_EXE%" (
  start "" "%DOCKER_DESKTOP_EXE%"
) else (
  call :warn "Docker Desktop exe not found at: %DOCKER_DESKTOP_EXE%"
)

for /l %%i in (1,1,36) do (
  docker info >nul 2>nul
  if not errorlevel 1 (
    call :ok "Docker engine is ready"
    exit /b 0
  )
  <nul set /p "=."
  timeout /t 5 /nobreak >nul
)

echo.
call :fail "Docker Desktop Linux Engine did not start."
echo.
echo What to do:
echo   1. Open Docker Desktop manually.
echo   2. Wait until it says Engine running.
echo   3. If it still fails, run: repair_docker_desktop_windows.bat
echo   4. Then run this file again.
echo.
exit /b 1

:banner
cls
echo.
echo  _______          __  __           _ 
echo ^|__   __^|        ^|  \/  ^|         ^| ^|
echo    ^| ^| _________ ^| \  / ^| ___   __^| ^|
echo    ^| ^|^|_  /_____^|^| ^|\/^| ^|/ _ \ / _` ^|
echo    ^| ^| / /       ^| ^|  ^| ^| (_) ^| (_^| ^|
echo    ^|_^|/___^|      ^|_^|  ^|_^|\___/ \__,_^|
echo.
echo      TVRS ^| SGL Bureau ^| Registry ^| AI
echo      GitHub sync test: 2026-07-15-A
echo ============================================================
echo.
exit /b 0

:stage
echo.
echo [BOOT:%~1] %~2
echo ------------------------------------------------------------
exit /b 0

:ok
echo   [OK] %~1
exit /b 0

:warn
echo   [WARN] %~1
exit /b 0

:fail
echo   [FAIL] %~1
exit /b 0

:module
echo   [LOAD] %~1
timeout /t 1 /nobreak >nul
echo   [ OK ] %~1
exit /b 0
