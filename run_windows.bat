@echo off
setlocal EnableExtensions EnableDelayedExpansion

if not defined TMOD_SKIP_BUILD set TMOD_SKIP_BUILD=0
if not defined TMOD_NONINTERACTIVE set TMOD_NONINTERACTIVE=0
if not defined TMOD_TRANSACTIONAL_UPDATE set TMOD_TRANSACTIONAL_UPDATE=0

title T-Mod Boot Console
chcp 65001 >nul

set PERSISTENT_DIR=C:\Users\Admin\Documents\SGLDiscordBot
set "COMPOSE_ENV_FILES=%PERSISTENT_DIR%\.env"
set DATA_DIR=%PERSISTENT_DIR%\data
set BACKUP_DIR=%PERSISTENT_DIR%\backups
set CADDY_DIR=%PERSISTENT_DIR%\caddy
set CADDY_DATA_DIR=%CADDY_DIR%\data
set CADDY_CONFIG_DIR=%CADDY_DIR%\config
set MINECRAFT_DIR=%PERSISTENT_DIR%\minecraft
set SECRETS_DIR=%PERSISTENT_DIR%\secrets
set MINECRAFT_RCON_SECRET=%SECRETS_DIR%\minecraft-rcon-password.txt
set MINECRAFT_SUPERVISOR_SECRET=%SECRETS_DIR%\minecraft-supervisor-token.txt
set POSTGRES_SECRET=%SECRETS_DIR%\postgres-password.txt
set MINECRAFT_SECRETS_MARKER=%TEMP%\tmod-minecraft-secrets-changed.flag
set MINECRAFT_SECRETS_CHANGED=0
set DOCKER_DESKTOP_EXE=C:\Program Files\Docker\Docker\Docker Desktop.exe
set TMOD_RELEASE=unknown
for /f "delims=" %%H in ('git -C "%~dp0" rev-parse --short=12 HEAD 2^>nul') do set TMOD_RELEASE=%%H

call :banner

call :stage "01" "Persistent storage"
if not exist "%PERSISTENT_DIR%" mkdir "%PERSISTENT_DIR%"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
if not exist "%CADDY_DATA_DIR%" mkdir "%CADDY_DATA_DIR%"
if not exist "%CADDY_CONFIG_DIR%" mkdir "%CADDY_CONFIG_DIR%"
if not exist "%MINECRAFT_DIR%" mkdir "%MINECRAFT_DIR%"
if not exist "%SECRETS_DIR%" mkdir "%SECRETS_DIR%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_minecraft_secrets_windows.ps1" -RconPath "%MINECRAFT_RCON_SECRET%" -SupervisorPath "%MINECRAFT_SUPERVISOR_SECRET%" -ChangedMarkerPath "%MINECRAFT_SECRETS_MARKER%"
if errorlevel 1 (
  call :fail "Failed to verify Minecraft control secrets"
  call :pause_if_interactive
  exit /b 1
)
if exist "%MINECRAFT_SECRETS_MARKER%" set MINECRAFT_SECRETS_CHANGED=1
call :ok "Minecraft control secrets verified outside Git"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_postgres_secret_windows.ps1" -SecretPath "%POSTGRES_SECRET%"
if errorlevel 1 (
  call :fail "Failed to verify PostgreSQL secret"
  call :pause_if_interactive
  exit /b 1
)
call :ok "PostgreSQL secret verified outside Git"
call :ok "Storage path: %PERSISTENT_DIR%"

call :stage "02" "Environment"
if not exist "%PERSISTENT_DIR%\.env" (
  copy ".env.persistent.example" "%PERSISTENT_DIR%\.env" >nul
  call :warn "Created %PERSISTENT_DIR%\.env"
  call :warn "Put your Discord token into this file, then run this bat again."
  echo.
  call :pause_if_interactive
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0merge_env_windows.ps1" -ExamplePath "%~dp0.env.persistent.example" -TargetPath "%PERSISTENT_DIR%\.env"
if errorlevel 1 (
  call :fail "Failed to merge .env"
  call :pause_if_interactive
  exit /b 1
)
call :ok ".env synchronized"

call :stage "03" "Direct HTTPS domain"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure_direct_web_windows.ps1" -TargetEnvPath "%PERSISTENT_DIR%\.env" -PublicDomain "tvr.lat"
if errorlevel 1 (
  call :fail "Failed to configure the direct HTTPS domain"
  call :warn "Approve the Windows administrator prompt and run this file again."
  call :pause_if_interactive
  exit /b 1
)
call :ok "Caddy HTTPS route ready"

call :stage "04" "Minecraft public port"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure_minecraft_windows.ps1" -ServerHostName "mc.tvr.lat" -Port 25565
if errorlevel 1 (
  call :fail "Failed to configure the Minecraft public port"
  call :warn "Approve the Windows administrator prompt and run this file again."
  call :pause_if_interactive
  exit /b 1
)
call :ok "Minecraft port ready"

call :stage "05" "Localization"
if not exist "%PERSISTENT_DIR%\localization.json" (
  copy "localization.example.json" "%PERSISTENT_DIR%\localization.json" >nul
  call :ok "localization.json created"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0merge_localization_windows.ps1" -ExamplePath "%~dp0localization.example.json" -TargetPath "%PERSISTENT_DIR%\localization.json" -BackupDir "%BACKUP_DIR%"
  if errorlevel 1 (
    call :fail "Failed to merge localization.json"
    call :pause_if_interactive
    exit /b 1
  )
  call :ok "localization.json synchronized"
)

call :stage "06" "Modules"
call :module "T-Mod Core"
call :module "TVRS Consensus"
call :module "SGL Bureau"
call :module "SGL Registry"
call :module "SGL Audio"
call :module "T-Mod Music"
call :module "Zigmund AI"
call :module "SGL Contracts"
call :module "PostgreSQL 17"
call :module "SQLite to PostgreSQL Migrator"
call :module "T-Mod Web Gateway"
call :module "T-Mod Maintenance Worker"
call :module "Minecraft Paper 26.1.2-74"
call :module "Minecraft Lifecycle Supervisor"

call :stage "07" "Docker engine"
call :ensure_docker_engine
if errorlevel 1 (
  call :pause_if_interactive
  exit /b 1
)

call :stage "08" "Retired service cleanup"
docker stop sgl-discord-bot >nul 2>nul
docker rm sgl-discord-bot >nul 2>nul
docker stop discord-browser-stream >nul 2>nul
docker rm discord-browser-stream >nul 2>nul
docker image rm tmod-browser-stream:latest >nul 2>nul
if exist "%PERSISTENT_DIR%\browser-stream.env" del /Q "%PERSISTENT_DIR%\browser-stream.env" >nul 2>nul
if exist "%PERSISTENT_DIR%\browser-stream" rmdir /S /Q "%PERSISTENT_DIR%\browser-stream" >nul 2>nul
call :ok "Retired services cleaned without stopping live Minecraft"

call :stage "09" "Docker build"
if "%TMOD_SKIP_BUILD%"=="1" (
  call :ok "Pre-tested Docker image selected by Safe Update"
) else (
  set COMPOSE_BAKE=true
  docker compose build
  if errorlevel 1 (
    call :fail "Docker build failed"
    call :warn "If you see dockerDesktopLinuxEngine pipe error, run repair_docker_desktop_windows.bat"
    call :pause_if_interactive
    exit /b 1
  )
  call :ok "Docker image ready"
)

call :stage "10" "Starting T-Mod and Minecraft"
rem Start the database first and detect the one-time SQLite import.  The old
rem Discord container must be stopped before the importer opens SQLite, or a
rem message arriving during COPY could exist only in the legacy database.
docker compose up -d tmod-postgres
if errorlevel 1 (
  call :fail "PostgreSQL startup failed"
  call :pause_if_interactive
  exit /b 1
)
set POSTGRES_READY=0
for /l %%i in (1,1,60) do (
  docker inspect --format "{{.State.Health.Status}}" tmod-postgres 2>nul | findstr /I /X /C:"healthy" >nul
  if not errorlevel 1 (
    set POSTGRES_READY=1
    goto :postgres_ready
  )
  timeout /t 2 /nobreak >nul
)
:postgres_ready
if not "%POSTGRES_READY%"=="1" (
  call :fail "PostgreSQL did not become healthy within 120 seconds"
  docker logs --tail 100 tmod-postgres
  call :pause_if_interactive
  exit /b 1
)
set POSTGRES_MIGRATION_REQUIRED=1
docker exec tmod-postgres psql -U tmod -d tmod -tAc "SELECT 1 FROM tmod_platform_migrations WHERE key='sqlite-to-postgresql-v1'" 2>nul | findstr /X /C:"1" >nul
if not errorlevel 1 set POSTGRES_MIGRATION_REQUIRED=0
if "%POSTGRES_MIGRATION_REQUIRED%"=="1" (
  call :warn "First PostgreSQL import detected; freezing the SQLite writer"
  docker stop tmod-discord-bot tmod-web tmod-worker >nul 2>nul
)
if "%MINECRAFT_SECRETS_CHANGED%"=="1" (
  call :warn "Minecraft control secret changed; one controlled restart is required"
  docker compose up -d --force-recreate minecraft minecraft-supervisor
  if errorlevel 1 (
    call :fail "Minecraft secret synchronization restart failed"
    call :pause_if_interactive
    exit /b 1
  )
)
docker compose up -d --remove-orphans
if errorlevel 1 (
  call :fail "Docker startup failed"
  echo.
  echo Minecraft supervisor status:
  docker compose ps minecraft-supervisor
  echo.
  echo Minecraft supervisor logs:
  docker compose logs --no-color --tail 80 minecraft-supervisor
  call :warn "If you see dockerDesktopLinuxEngine pipe error, run repair_docker_desktop_windows.bat"
  call :pause_if_interactive
  exit /b 1
)
call :ok "Container started"

rem A bind-mounted Caddyfile can change without Compose recreating Caddy.
rem Validate it first, then restart so new subdomains receive certificates.
docker exec tmod-caddy caddy validate --config /etc/caddy/Caddyfile
if errorlevel 1 (
  call :fail "Caddy configuration validation failed"
  call :pause_if_interactive
  exit /b 1
)
docker compose restart tmod-caddy
if errorlevel 1 (
  call :fail "Caddy restart failed"
  call :pause_if_interactive
  exit /b 1
)
call :ok "HTTPS routes refreshed"

call :stage "11" "Minecraft RCON verification"
call :check_minecraft_rcon
if errorlevel 1 (
  call :pause_if_interactive
  exit /b 1
)

call :stage "12" "Consensus health check"
call :check_consensus_health
if errorlevel 1 if "%TMOD_TRANSACTIONAL_UPDATE%"=="1" (
  call :fail "Post-deploy health check failed; Safe Update will roll back"
  exit /b 1
)

call :stage "13" "Status"
docker compose ps

for %%D in ("%~dp0..") do set "DESKTOP_LAUNCHER=%%~fD\T-Mod Control.bat"
copy /y "%~dp0tmod_control_windows.bat" "%DESKTOP_LAUNCHER%" >nul 2>nul
if errorlevel 1 (
  call :warn "T-Mod Control launcher could not be refreshed"
) else (
  if exist "%%~fD\Start T-Mod.bat" del /Q "%%~fD\Start T-Mod.bat" >nul 2>nul
  call :ok "T-Mod Control refreshed: %DESKTOP_LAUNCHER%"
)

if exist "%~dp0configure_auto_update_windows.ps1" (
  powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0configure_auto_update_windows.ps1" -ProjectDir "%~dp0" -IntervalMinutes 2
  if errorlevel 1 (
    call :warn "Automatic GitHub update watcher could not be registered"
  ) else (
    call :ok "Automatic updates enabled: origin/main is checked every 2 minutes"
  )
)

echo.
echo ============================================================
echo   T-Mod startup finished.
echo   Database: PostgreSQL 17 ^(private Docker volume^)
echo   Archive:  %PERSISTENT_DIR%\data\tmod.db ^(original SQLite, preserved^)
echo   Config:   %PERSISTENT_DIR%\.env
echo   Locale:   %PERSISTENT_DIR%\localization.json
echo   Reactor:  https://reactor.tvr.lat
echo   Member:   https://tvr.lat
echo   Consensus:https://consensus.tvr.lat
echo   Zigmund:  https://zigmund.tvr.lat
echo   MC:       mc.tvr.lat ^(Paper 26.1.2-74, 25565/TCP^)
echo   MC data:  %MINECRAFT_DIR%
echo   HTTPS:    Caddy on public ports 80/443
echo   Origin:   http://127.0.0.1:8787 ^(never forward this port^)
echo   Updates:  origin/main checked every 2 minutes
echo ============================================================
echo.
echo Recent bot logs:
echo ------------------------------------------------------------
docker logs --tail 60 tmod-discord-bot
echo ------------------------------------------------------------
echo.
echo Live logs: docker logs -f tmod-discord-bot
echo Web gateway logs: docker logs -f tmod-web
echo Database logs: docker logs -f tmod-postgres
echo Minecraft logs: docker logs -f minecraft
echo.
call :pause_if_interactive
exit /b 0

:check_minecraft_rcon
for /l %%i in (1,1,60) do (
  docker inspect --format "{{.State.Health.Status}}" minecraft 2>nul | findstr /I /X /C:"healthy" >nul
  if not errorlevel 1 (
    docker exec minecraft rcon-cli list >nul 2>nul
    if not errorlevel 1 (
      call :ok "Minecraft RCON secret accepted"
      exit /b 0
    )
    call :fail "Minecraft is healthy, but RCON authentication failed."
    call :warn "The generated secret and server.properties are not synchronized."
    docker logs --tail 80 minecraft
    exit /b 1
  )
  <nul set /p "=."
  timeout /t 5 /nobreak >nul
)
echo.
call :fail "Minecraft did not become healthy within 300 seconds."
docker logs --tail 80 minecraft
exit /b 1

:check_consensus_health
for /l %%i in (1,1,24) do (
  powershell -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/health?ready=1' -TimeoutSec 3; if ($r.status -eq 'ok' -and $r.discord_ready) { exit 0 } } catch {}; exit 1" >nul 2>nul
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
exit /b 1

:pause_if_interactive
if not "%TMOD_NONINTERACTIVE%"=="1" pause
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
