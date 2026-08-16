@echo off
setlocal
cd /d "%~dp0"

where node >nul 2>nul || (
  echo [T-Mod Desktop] Node.js 22 or newer is required.
  pause
  exit /b 1
)

call corepack enable >nul 2>nul
call corepack prepare pnpm@11.19.0 --activate || exit /b 1
call pnpm install || exit /b 1
call pnpm test || exit /b 1
call pnpm typecheck || exit /b 1
call pnpm dist || exit /b 1

echo.
echo [T-Mod Desktop] Installer is ready in desktop\release.
pause
