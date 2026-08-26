param([string]$SourceDir = $PSScriptRoot)

$ErrorActionPreference = "Stop"
$SourceDir = (Resolve-Path -LiteralPath $SourceDir).Path
$installDir = Join-Path $env:LOCALAPPDATA "TModRemote"
$desktopDir = [Environment]::GetFolderPath("Desktop")
$requiredFiles = @("tmod_remote_windows.ps1", "tmod_remote_windows.bat", "tmod_remote_version.json")

New-Item -ItemType Directory -Path $installDir -Force | Out-Null
foreach ($fileName in $requiredFiles) {
    $sourcePath = Join-Path $SourceDir $fileName
    if (-not (Test-Path -LiteralPath $sourcePath)) { throw "Не найден файл T-Mod Remote: $sourcePath" }
    Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $installDir $fileName) -Force
}

$desktopLauncher = Join-Path $desktopDir "T-Mod Remote.bat"
Copy-Item -LiteralPath (Join-Path $SourceDir "tmod_remote_windows.bat") -Destination $desktopLauncher -Force

Write-Host "[T-MOD REMOTE] Installed: $installDir" -ForegroundColor Green
Write-Host "[T-MOD REMOTE] Desktop: $desktopLauncher" -ForegroundColor Green
Write-Host "The first launch opens the WireGuard/SSH setup wizard." -ForegroundColor DarkGray
