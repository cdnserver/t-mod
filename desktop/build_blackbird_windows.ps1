[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:TMOD_DESKTOP_EDITION = "blackbird"
$env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
$env:CI = "true"
function Invoke-BuildStep([string]$Name, [string[]]$Arguments) {
    Write-Host "[BLACKBIRD] $Name" -ForegroundColor Cyan
    & pnpm.cmd @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Name failed with exit code $LASTEXITCODE" }
}
Invoke-BuildStep "Dependencies" @("install", "--frozen-lockfile")
Invoke-BuildStep "Typecheck" @("typecheck")
Invoke-BuildStep "Tests" @("test")
Invoke-BuildStep "Application" @("build:blackbird")
Invoke-BuildStep "Bundle validation" @("check:bundle")
Invoke-BuildStep "Windows x64 installer" @("exec", "electron-builder", "--config", "electron-builder.blackbird.yml", "--win", "nsis", "--x64", "--publish", "never")
Write-Host "[BLACKBIRD] Installer ready in release-blackbird" -ForegroundColor Green
