@echo off
setlocal EnableExtensions

title T-Mod Docker Desktop Repair
chcp 65001 >nul

set DOCKER_DESKTOP_EXE=C:\Program Files\Docker\Docker\Docker Desktop.exe

echo.
echo ============================================================
echo   T-Mod Docker Desktop Repair
echo ============================================================
echo.
echo This will restart WSL and Docker Desktop.
echo It will NOT delete your T-Mod database or bot files.
echo.

echo [1/5] Stopping bot containers...
docker stop tmod-discord-bot >nul 2>nul
docker stop sgl-discord-bot >nul 2>nul

echo [2/5] Shutting down WSL...
wsl --shutdown

echo [3/5] Starting Docker Desktop service if possible...
net start com.docker.service >nul 2>nul

echo [4/5] Launching Docker Desktop...
if exist "%DOCKER_DESKTOP_EXE%" (
  start "" "%DOCKER_DESKTOP_EXE%"
) else (
  echo Docker Desktop executable not found:
  echo %DOCKER_DESKTOP_EXE%
)

echo [5/5] Waiting for Docker engine...
for /l %%i in (1,1,48) do (
  docker info >nul 2>nul
  if not errorlevel 1 (
    echo.
    echo [OK] Docker engine is running.
    echo Now run run_windows.bat again.
    echo.
    pause
    exit /b 0
  )
  <nul set /p "=."
  timeout /t 5 /nobreak >nul
)

echo.
echo [FAIL] Docker engine still does not respond.
echo Open Docker Desktop manually and check Troubleshoot / Restart.
echo If needed, update Docker Desktop or reset WSL integration.
echo.
pause
exit /b 1
