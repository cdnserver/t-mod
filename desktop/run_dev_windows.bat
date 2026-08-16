@echo off
setlocal
cd /d "%~dp0"
call corepack enable >nul 2>nul
call corepack prepare pnpm@11.19.0 --activate || exit /b 1
call pnpm install || exit /b 1
call pnpm dev
