@echo off
setlocal EnableExtensions EnableDelayedExpansion

title T-Mod Boot Console
chcp 65001 >nul

set PERSISTENT_DIR=C:\Users\Admin\Documents\SGLDiscordBot
set DATA_DIR=%PERSISTENT_DIR%\data
set BACKUP_DIR=%PERSISTENT_DIR%\backups
set CADDY_DIR=%PERSISTENT_DIR%\caddy
set CADDY_DATA_DIR=%CADDY_DIR%\data
set CADDY_CONFIG_DIR=%CADDY_DIR%\config
set BROWSER_STREAM_DATA_DIR=%PERSISTENT_DIR%\browser-stream
set BROWSER_STREAM_ENV=%PERSISTENT_DIR%\browser-stream.env
set DOCKER_DESKTOP_EXE=C:\Program Files\Docker\Docker\Docker Desktop.exe

call :banner

call :stage "01" "Persistent storage"
if not exist "%PERSISTENT_DIR%" mkdir "%PERSISTENT_DIR%"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
if not exist "%CADDY_DATA_DIR%" mkdir "%CADDY_DATA_DIR%"
if not exist "%CADDY_CONFIG_DIR%" mkdir "%CADDY_CONFIG_DIR%"
if not exist "%BROWSER_STREAM_DATA_DIR%" mkdir "%BROWSER_STREAM_DATA_DIR%"
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

set COMPOSE_PROFILES=
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure_browser_stream_windows.ps1" -TargetEnvPath "%PERSISTENT_DIR%\.env" -BrowserEnvPath "%BROWSER_STREAM_ENV%" -TemplateEnvPath "%~dp0discord-browser-stream\.env.example"
if errorlevel 1 (
  call :fail "Discord browser client configuration is incomplete"
  call :warn "When enabled, fill the credentials in %BROWSER_STREAM_ENV%"
  pause
  exit /b 1
)
findstr /R /I /C:"^BROWSER_STREAM_ENABLED=true$" "%PERSISTENT_DIR%\.env" >nul
if not errorlevel 1 (
  set COMPOSE_PROFILES=browser-stream
  call :ok "Discord browser client profile enabled"
) else (
  call :ok "Discord browser client profile disabled"
)

call :stage "03" "Direct HTTPS domain"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure_direct_web_windows.ps1" -TargetEnvPath "%PERSISTENT_DIR%\.env" -PublicDomain "tvr.lat"
if errorlevel 1 (
  call :fail "Failed to configure the direct HTTPS domain"
  call :warn "Approve the Windows administrator prompt and run this file again."
  pause
  exit /b 1
)
call :ok "Caddy HTTPS route ready"

call :stage "04" "Localization"
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

call :stage "05" "Modules"
call :module "T-Mod Core"
call :module "TVRS Consensus"
call :module "SGL Bureau"
call :module "SGL Registry"
call :module "SGL Audio"
call :module "T-Mod Music"
if defined COMPOSE_PROFILES call :module "Discord Browser Client"
call :module "Zigmund AI"
call :module "SGL Contracts"
call :module "SQLite Migrator"

call :stage "06" "Docker engine"
call :ensure_docker_engine
if errorlevel 1 (
  pause
  exit /b 1
)

call :stage "07" "Stopping old containers"
docker stop sgl-discord-bot >nul 2>nul
docker rm sgl-discord-bot >nul 2>nul
docker stop tmod-discord-bot >nul 2>nul
docker rm tmod-discord-bot >nul 2>nul
if not defined COMPOSE_PROFILES (
  docker stop discord-browser-stream >nul 2>nul
  docker rm discord-browser-stream >nul 2>nul
)
call :ok "Old containers stopped"

call :stage "08" "Docker build"
set COMPOSE_BAKE=true
docker compose build
if errorlevel 1 (
  call :fail "Docker build failed"
  call :warn "If you see dockerDesktopLinuxEngine pipe error, run repair_docker_desktop_windows.bat"
  pause
  exit /b 1
)
call :ok "Docker image ready"

call :stage "09" "Starting T-Mod"
docker compose up -d --force-recreate
if errorlevel 1 (
  call :fail "Docker startup failed"
  call :warn "If you see dockerDesktopLinuxEngine pipe error, run repair_docker_desktop_windows.bat"
  pause
  exit /b 1
)
call :ok "Container started"

call :stage "10" "Consensus health check"
call :check_consensus_health

call :stage "11" "Status"
docker compose ps

echo.
echo ============================================================
echo   T-Mod startup finished.
echo   Database: %PERSISTENT_DIR%\data\tmod.db
echo   Config:   %PERSISTENT_DIR%\.env
echo   Locale:   %PERSISTENT_DIR%\localization.json
echo   Panel:    https://tvr.lat
echo   HTTPS:    Caddy on public ports 80/443
echo   Origin:   http://127.0.0.1:8787 ^(never forward this port^)
if defined COMPOSE_PROFILES echo   Screen:   Discord browser client enabled
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

:check_consensus_health
for /l %%i in (1,1,24) do (
  powershell -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/health' -TimeoutSec 3; if ($r.status -eq 'ok') { exit 0 } } catch {}; exit 1" >nul 2>nul
  if not errorlevel 1 (
    call :ok "Consensus web panel is healthy"
    exit /b 0
  )
  <nul set /p "=."
  timeout /t 3 /nobreak >nul
)
echo.
call :warn "The bot is running, but the web panel did not answer within 72 seconds."
call :warn "Check: docker logs --tail 100 tmod-discord-bot"
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
