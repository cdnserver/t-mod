@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Always resolve relative paths from the checked-out project, even when
rem started from a shortcut, Task Scheduler, or another current directory.
cd /d "%~dp0"
if errorlevel 1 (
  echo [FAIL] Could not enter the T-Mod project directory: %~dp0
  exit /b 1
)

if not defined TMOD_SKIP_BUILD set TMOD_SKIP_BUILD=0
if not defined TMOD_NONINTERACTIVE set TMOD_NONINTERACTIVE=0
if not defined TMOD_TRANSACTIONAL_UPDATE set TMOD_TRANSACTIONAL_UPDATE=0

title T-Mod Boot Console
chcp 65001 >nul

if not defined TMOD_PERSISTENT_DIR set "TMOD_PERSISTENT_DIR=%USERPROFILE%\Documents\SGLDiscordBot"
set "PERSISTENT_DIR=%TMOD_PERSISTENT_DIR%"
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
  copy "%~dp0.env.persistent.example" "%PERSISTENT_DIR%\.env" >nul
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
  copy "%~dp0localization.example.json" "%PERSISTENT_DIR%\localization.json" >nul
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
    rem Docker Desktop's BuildKit credential helper cannot be opened from
    rem some SSH, Task Scheduler and service sessions. All T-Mod images use
    rem public registries, so retry through Docker's local-image legacy path
    rem instead of making a healthy remote updater fail on wincred/desktop.
    call :warn "Docker build failed; retrying through the local-image builder"
    call :docker_build_without_windows_credentials
    if errorlevel 1 (
      call :fail "Docker build failed"
      call :warn "If you see dockerDesktopLinuxEngine pipe error, run repair_docker_desktop_windows.bat"
      call :pause_if_interactive
      exit /b 1
    )
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
rem Docker reports a container as Running before its healthcheck has passed.
rem On a busy Docker Desktop disk, PostgreSQL checkpoints can delay that probe
rem far beyond the old 4s/120s budget. Require both the Docker health state
rem and an actual local PostgreSQL connection before booting application code.
call :wait_for_postgres_health 30
if not errorlevel 1 goto :postgres_ready

rem Existing containers keep their old healthcheck until they are recreated.
rem When PostgreSQL itself answers a direct SQL probe but Docker still says
rem unhealthy, refresh only its container so the new healthcheck applies. The
rem named volume is retained; stop dependent writers first for a clean handoff.
call :postgres_accepts_connections
if not errorlevel 1 (
  call :warn "PostgreSQL accepts SQL, but Docker health is stale; refreshing its healthcheck once"
  docker compose stop tmod-discord-bot tmod-web tmod-worker >nul 2>nul
  docker compose up -d --no-deps --force-recreate tmod-postgres
  if errorlevel 1 (
    call :fail "PostgreSQL healthcheck refresh failed"
    call :postgres_diagnostics
    call :pause_if_interactive
    exit /b 1
  )
) else (
  rem A database that is still recovering must not be interrupted. Continue
  rem with a longer bounded wait and report the real healthcheck details if it
  rem never answers, rather than killing a valid recovery in progress.
  call :warn "PostgreSQL is still recovering; extending the bounded readiness wait"
)

rem The database is stored on a Windows-hosted Docker volume and can take a
rem few minutes to recover after a large checkpoint. Give the current or
rem refreshed healthcheck a bounded eight-minute window rather than treating a
rem live database as failed after two minutes.
call :wait_for_postgres_health 96
if errorlevel 1 (
  call :fail "PostgreSQL did not become healthy after bounded readiness checks"
  call :postgres_diagnostics
  call :pause_if_interactive
  exit /b 1
)
:postgres_ready
rem Verify the application-side secret path through the effective Compose
rem configuration. This catches stale or missing Docker Desktop bind mounts
rem before Python starts and emits a clear boot-stage failure.
docker compose run --rm --no-deps tmod-db-migrate python -c "from pathlib import Path; p=Path('/app/persistent/secrets/postgres-password.txt'); assert p.is_file() and len(p.read_text(encoding='utf-8').strip()) >= 32, p"
if errorlevel 1 (
  call :fail "PostgreSQL secret is not visible inside application containers"
  call :warn "Expected host file: %POSTGRES_SECRET%"
  call :pause_if_interactive
  exit /b 1
)
call :ok "PostgreSQL secret mount verified inside Docker"
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
rem Always replace the application containers. A failed transactional update
rem can otherwise leave a stopped bot carrying the previous Compose mounts and
rem environment even after the repository was updated. Persistent data and the
rem PostgreSQL volume are not removed.
docker compose rm -s -f tmod-db-migrate tmod-discord-bot tmod-web tmod-worker >nul 2>nul
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

call :stage "10B" "Minecraft runtime health"
call :ensure_minecraft_runtime
if errorlevel 1 (
  call :pause_if_interactive
  exit /b 1
)

call :stage "10A" "Split backend health"
call :ensure_split_runtime
if errorlevel 1 (
  call :pause_if_interactive
  exit /b 1
)

rem A bind-mounted Caddyfile can change without Compose recreating Caddy.
rem Start it explicitly and wait for its healthcheck before validating.
docker compose up -d tmod-caddy
if errorlevel 1 (
  call :fail "Caddy startup failed"
  call :pause_if_interactive
  exit /b 1
)
set CADDY_READY=0
for /l %%i in (1,1,30) do (
  set CADDY_HEALTH=
  for /f "delims=" %%H in ('docker inspect --format "{{.State.Health.Status}}" tmod-caddy 2^>nul') do set CADDY_HEALTH=%%H
  if /I "!CADDY_HEALTH!"=="healthy" (
    set CADDY_READY=1
    goto :caddy_ready
  )
  call :sleep 2
)
:caddy_ready
if not "%CADDY_READY%"=="1" (
  call :fail "Caddy did not become healthy within 60 seconds"
  docker compose ps tmod-caddy
  docker compose logs --no-color --tail 80 tmod-caddy
  call :pause_if_interactive
  exit /b 1
)
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

set "CONTROL_INSTALLER=%~dp0install_tmod_control_windows.ps1"
if exist "%CONTROL_INSTALLER%" (
  powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%CONTROL_INSTALLER%" -ProjectDir "%~dp0" -Quiet
  if errorlevel 1 (
    call :warn "Native T-Mod Control could not be refreshed; server startup remains successful"
  ) else (
    call :ok "T-Mod Control refreshed: native EXE is current"
  )
) else (
  call :warn "Native T-Mod Control installer is missing"
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

:ensure_split_runtime
set SPLIT_RUNTIME_READY=0
for /l %%i in (1,1,30) do (
  set WEB_HEALTH=
  set WORKER_HEALTH=
  for /f "delims=" %%H in ('docker inspect --format "{{.State.Health.Status}}" tmod-web 2^>nul') do set WEB_HEALTH=%%H
  for /f "delims=" %%H in ('docker inspect --format "{{.State.Health.Status}}" tmod-worker 2^>nul') do set WORKER_HEALTH=%%H
  if /I "!WEB_HEALTH!"=="healthy" if /I "!WORKER_HEALTH!"=="healthy" (
    set SPLIT_RUNTIME_READY=1
    goto :split_runtime_ready
  )
  <nul set /p "=."
  call :sleep 2
)

echo.
call :warn "Web or worker health did not converge; performing one controlled repair"
docker compose up -d --no-deps --force-recreate tmod-web tmod-worker
if errorlevel 1 goto :split_runtime_failed
for /l %%i in (1,1,30) do (
  set WEB_HEALTH=
  set WORKER_HEALTH=
  for /f "delims=" %%H in ('docker inspect --format "{{.State.Health.Status}}" tmod-web 2^>nul') do set WEB_HEALTH=%%H
  for /f "delims=" %%H in ('docker inspect --format "{{.State.Health.Status}}" tmod-worker 2^>nul') do set WORKER_HEALTH=%%H
  if /I "!WEB_HEALTH!"=="healthy" if /I "!WORKER_HEALTH!"=="healthy" (
    set SPLIT_RUNTIME_READY=1
    goto :split_runtime_ready
  )
  <nul set /p "=."
  call :sleep 2
)

:split_runtime_failed
echo.
call :fail "The split backend did not become healthy after automatic repair"
docker compose ps -a tmod-web tmod-worker
echo.
echo --- tmod-web ---
docker logs --tail 100 tmod-web 2>&1
echo.
echo --- tmod-worker ---
docker logs --tail 100 tmod-worker 2>&1
exit /b 1

:split_runtime_ready
echo.
call :ok "T-Mod Web and T-Mod Worker are healthy"
exit /b 0

:ensure_minecraft_runtime
set MINECRAFT_RUNTIME_READY=0
rem A cold Paper start routinely needs more than one minute on a Windows
rem Docker host. Wait long enough for the image to initialize before using
rem the stricter RCON verification below.
for /l %%i in (1,1,90) do (
  set MC_HEALTH=
  set MC_SUPERVISOR_HEALTH=
  for /f "delims=" %%H in ('docker inspect --format "{{.State.Health.Status}}" minecraft 2^>nul') do set MC_HEALTH=%%H
  for /f "delims=" %%H in ('docker inspect --format "{{.State.Health.Status}}" minecraft-supervisor 2^>nul') do set MC_SUPERVISOR_HEALTH=%%H
  if /I "!MC_HEALTH!"=="healthy" if /I "!MC_SUPERVISOR_HEALTH!"=="healthy" (
    set MINECRAFT_RUNTIME_READY=1
    goto :minecraft_runtime_ready
  )
  <nul set /p "=."
  call :sleep 2
)
:minecraft_runtime_ready
if "%MINECRAFT_RUNTIME_READY%"=="1" (
  echo.
  call :ok "Minecraft and lifecycle supervisor are healthy"
  exit /b 0
)
echo.
call :fail "Minecraft or lifecycle supervisor did not become healthy within 180 seconds"
docker compose ps -a minecraft minecraft-supervisor
echo.
echo --- minecraft ---
docker compose logs --no-color --tail 80 minecraft 2>&1
echo.
echo --- minecraft-supervisor ---
docker compose logs --no-color --tail 80 minecraft-supervisor 2>&1
exit /b 1

:check_minecraft_rcon
for /l %%i in (1,1,60) do (
  docker inspect --format "{{.State.Health.Status}}" minecraft 2>nul | findstr /I /X /C:"healthy" >nul
  if not errorlevel 1 (
    rem mc-health may become healthy before RCON finishes binding. Retry the
    rem command for a bounded 60 seconds before declaring auth/config failure.
    for /l %%r in (1,1,12) do (
      docker exec minecraft rcon-cli list >nul 2>nul
      if not errorlevel 1 (
        call :ok "Minecraft RCON secret accepted"
        exit /b 0
      )
      call :sleep 5
    )
    call :fail "Minecraft is healthy, but RCON did not respond within 60 seconds."
    call :warn "The generated secret and server.properties may be unsynchronized."
    docker logs --tail 80 minecraft
    exit /b 1
  )
  <nul set /p "=."
  call :sleep 5
)
echo.
call :fail "Minecraft did not become healthy within 300 seconds."
docker logs --tail 80 minecraft
exit /b 1

:check_consensus_health
rem Discord reconciliation, PostgreSQL warm-up and the first web projection can
rem legitimately take longer than 72 seconds after replacing all three split
rem services. Keep rollback bounded, but do not reject a healthy release while
rem the gateway is still warming up.
for /l %%i in (1,1,80) do (
  powershell -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/health?ready=1' -TimeoutSec 3; if ($r.status -eq 'ok' -and $r.discord_ready) { exit 0 } } catch {}; exit 1" >nul 2>nul
  if not errorlevel 1 (
    call :ok "Consensus web panel is healthy"
    exit /b 0
  )
  <nul set /p "=."
  call :sleep 3
)
echo.
call :warn "The bot is running, but the web panel did not answer within 240 seconds."
docker compose ps tmod-discord-bot tmod-web tmod-worker
echo --- tmod-discord-bot ---
docker compose logs --no-color --tail 120 tmod-discord-bot
echo --- tmod-web ---
docker compose logs --no-color --tail 120 tmod-web
echo --- tmod-worker ---
docker compose logs --no-color --tail 120 tmod-worker
exit /b 1

:postgres_accepts_connections
rem Use TCP deliberately: it validates the same listener that the application
rem containers use and avoids a false pass through a Unix socket alone. Then
rem execute a minimal SQL statement: pg_isready alone is not a usable database
rem readiness guarantee if PostgreSQL is still recovering.
docker exec tmod-postgres pg_isready -q -h 127.0.0.1 -p 5432 -U tmod -d tmod -t 15 >nul 2>nul
if errorlevel 1 exit /b 1
rem The SQL process exit code is the readiness signal. Piping its CRLF output
rem through findstr /X is unreliable across Docker Desktop/Windows code pages
rem and previously made a healthy database wait through the full recovery path.
docker exec tmod-postgres psql -v ON_ERROR_STOP=1 -U tmod -d tmod -tAc "SELECT 1" >nul 2>nul
if errorlevel 1 exit /b 1
exit /b 0

:wait_for_postgres_health
set "POSTGRES_HEALTH="
for /l %%i in (1,1,%~1) do (
  set "POSTGRES_HEALTH="
  for /f "delims=" %%H in ('docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}" tmod-postgres 2^>nul') do set "POSTGRES_HEALTH=%%H"
  if /I "!POSTGRES_HEALTH!"=="healthy" (
    call :postgres_accepts_connections
    if not errorlevel 1 exit /b 0
  )
  <nul set /p "=."
  call :sleep 5
)
echo.
exit /b 1

:postgres_diagnostics
echo.
echo --- PostgreSQL status ---
docker compose ps -a tmod-postgres
echo.
echo --- Docker healthcheck details ---
docker inspect --format "{{json .State.Health}}" tmod-postgres 2>nul
echo.
echo --- Direct PostgreSQL probe ---
docker exec tmod-postgres pg_isready -h 127.0.0.1 -p 5432 -U tmod -d tmod -t 15 2>&1
echo.
echo --- Recent PostgreSQL logs ---
docker logs --tail 100 tmod-postgres
exit /b 0

:docker_build_without_windows_credentials
rem BuildKit asks Docker Desktop's session-bound credential helper for public
rem base images. The legacy path uses the local image cache and anonymous pull
rem path instead, which stays available to the T-Mod remote controller.
set "DOCKER_BUILDKIT=0"
set "COMPOSE_DOCKER_CLI_BUILD=0"
set "COMPOSE_BAKE=false"
docker build --pull=false -t tmod-discord-bot:latest .
if errorlevel 1 exit /b 1
docker build --pull=false -t tmod-minecraft-supervisor:latest .\minecraft-supervisor
exit /b %errorlevel%

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
  call :sleep 5
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
call :sleep 1
echo   [ OK ] %~1
exit /b 0

:sleep
set /a "_TMOD_SLEEP_PINGS=%~1+1" >nul 2>&1
ping 127.0.0.1 -n %_TMOD_SLEEP_PINGS% -w 1000 >nul
set "_TMOD_SLEEP_PINGS="
exit /b 0
