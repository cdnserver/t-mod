@echo off
setlocal EnableExtensions DisableDelayedExpansion

title T-Mod Update and Start
chcp 65001 >nul

set "REPO_URL=https://github.com/cdnserver/t-mod.git"
set "BRANCH=main"
set "SELF_DIR=%~dp0"

rem The launcher can be run either from the repository or from the Desktop.
if exist "%SELF_DIR%.git" if exist "%SELF_DIR%run_windows.bat" goto running_from_repo
set "PROJECT_DIR=%SELF_DIR%esgiel"
goto project_path_ready

:running_from_repo
for %%D in ("%SELF_DIR%.") do set "PROJECT_DIR=%%~fD"

:project_path_ready
set "LOG_DIR=%USERPROFILE%\Documents\SGLDiscordBot"
set "LOG_FILE=%LOG_DIR%\launcher.log"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul

call :banner
call :log "Launcher started. Project: %PROJECT_DIR%"

where git >nul 2>nul
if errorlevel 1 goto git_not_found

if exist "%PROJECT_DIR%\.git" goto repository_ready
if exist "%PROJECT_DIR%" goto backup_non_repository
goto clone_repository

:backup_non_repository
call :timestamp
set "OLD_PROJECT_DIR=%PROJECT_DIR%_backup_%STAMP%"
echo [WARN] The project folder is not a Git repository.
echo [INFO] Moving it to:
echo        %OLD_PROJECT_DIR%
move "%PROJECT_DIR%" "%OLD_PROJECT_DIR%" >nul
if errorlevel 1 goto backup_failed
call :log "Non-Git project folder moved to %OLD_PROJECT_DIR%"

:clone_repository
echo [GIT] Project is missing. Cloning %BRANCH% from GitHub...
git clone --branch "%BRANCH%" --single-branch "%REPO_URL%" "%PROJECT_DIR%"
if errorlevel 1 goto clone_failed
call :log "Repository cloned from %REPO_URL%"
goto repository_ready

:repository_ready
if not exist "%PROJECT_DIR%\safe_update_windows.ps1" goto legacy_update
if not exist "%PROJECT_DIR%\launch_tmod_guarded_windows.ps1" goto legacy_update
echo [SAFE UPDATE] Transactional updater enabled.
call :log "Starting transactional update and guarded deployment"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\launch_tmod_guarded_windows.ps1" -ProjectDir "%PROJECT_DIR%" -PersistentDir "%USERPROFILE%\Documents\SGLDiscordBot" -Branch "%BRANCH%"
set "BOT_EXIT_CODE=%ERRORLEVEL%"
call :log "Transactional updater finished with exit code %BOT_EXIT_CODE%"
if not "%BOT_EXIT_CODE%"=="0" goto fatal_error
exit /b 0

:legacy_update
echo [WARN] Safe updater is missing. Repository files will not be modified.
call :log "Safe updater is missing; starting current version without update"
goto start_bot

:git_not_found
echo [WARN] Git for Windows was not found.
if exist "%PROJECT_DIR%\run_windows.bat" goto start_without_git
echo [FAIL] Install Git for Windows and run this launcher again.
call :log "Git was not found and no installed project is available"
goto fatal_error

:start_without_git
echo [WARN] The update check is skipped. Starting the installed version.
call :log "Git was not found; starting current version without update"
goto start_bot

:update_failed
echo [WARN] The project could not be updated safely.
if exist "%PROJECT_DIR%\run_windows.bat" goto start_after_update_error
call :log "Update failed and run_windows.bat is missing"
goto fatal_error

:start_after_update_error
echo [WARN] Starting the currently installed version.
call :log "Update failed; starting current version"
goto start_bot

:backup_failed
echo [FAIL] Could not back up the existing project folder.
call :log "Could not back up non-Git project folder"
goto fatal_error

:clone_failed
echo [FAIL] Could not clone the project from GitHub.
echo [INFO] Repository: %REPO_URL%
call :log "Repository clone failed"
goto fatal_error

:start_bot
if not exist "%PROJECT_DIR%\run_windows.bat" goto run_file_missing
echo.
echo [START] Running T-Mod through Docker Desktop...
echo [PATH]  %PROJECT_DIR%
echo ------------------------------------------------------------
call :log "Calling run_windows.bat"
pushd "%PROJECT_DIR%"
call "%PROJECT_DIR%\run_windows.bat"
set "BOT_EXIT_CODE=%ERRORLEVEL%"
popd
call :log "run_windows.bat finished with exit code %BOT_EXIT_CODE%"
exit /b %BOT_EXIT_CODE%

:run_file_missing
echo [FAIL] run_windows.bat is missing from:
echo        %PROJECT_DIR%
call :log "run_windows.bat is missing"
goto fatal_error

:fatal_error
echo.
echo T-Mod was not started.
echo Log: %LOG_FILE%
echo.
pause
exit /b 1

:timestamp
set "STAMP="
for /f "delims=" %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss" 2^>nul') do set "STAMP=%%T"
if not defined STAMP set "STAMP=manual_backup"
exit /b 0

:log
if defined LOG_FILE >>"%LOG_FILE%" echo [%date% %time%] %~1
exit /b 0

:banner
cls
echo.
echo ============================================================
echo   T-Mod - GitHub Update and Docker Desktop Start
echo ============================================================
echo.
exit /b 0
