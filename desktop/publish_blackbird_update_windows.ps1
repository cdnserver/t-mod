[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [string]$PersistentDir = $(if ($env:TMOD_PERSISTENT_DIR) { $env:TMOD_PERSISTENT_DIR } else { Join-Path $env:USERPROFILE "Documents\SGLDiscordBot" })
)

$ErrorActionPreference = "Stop"
$Desktop = $PSScriptRoot
if (-not $SkipBuild) {
    & (Join-Path $Desktop "build_blackbird_windows.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Blackbird build failed" }
}

$Release = Join-Path $Desktop "release-blackbird"
$Manifest = Join-Path $Release "latest.yml"
if (-not (Test-Path $Manifest -PathType Leaf)) { throw "latest.yml is missing; no update can be published" }
$Installer = @(Get-ChildItem $Release -Filter "BLACKBIRD-Private-Setup-*.exe" -File |
    Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1)
if ($Installer.Count -ne 1) { throw "Blackbird installer is missing" }
$Blockmap = "$($Installer[0].FullName).blockmap"
if (-not (Test-Path $Blockmap -PathType Leaf)) { throw "Blackbird blockmap is missing" }
$Yaml = Get-Content $Manifest -Raw -Encoding UTF8
if (-not $Yaml.Contains($Installer[0].Name)) { throw "Update manifest does not name the selected installer" }

$Target = Join-Path $PersistentDir "blackbird-updates"
$Stage = Join-Path $Target (".stage-" + [guid]::NewGuid().ToString("N"))
New-Item $Stage -ItemType Directory -Force | Out-Null
try {
    foreach ($Source in @($Installer[0].FullName, $Blockmap)) {
        $Name = Split-Path $Source -Leaf
        Copy-Item $Source (Join-Path $Stage $Name)
        if ((Get-FileHash $Source -Algorithm SHA256).Hash -ne (Get-FileHash (Join-Path $Stage $Name) -Algorithm SHA256).Hash) {
            throw "Staged file failed checksum verification: $Name"
        }
        Move-Item (Join-Path $Stage $Name) (Join-Path $Target $Name) -Force
    }
    Copy-Item $Manifest (Join-Path $Stage "latest.yml")
    $NextManifest = Join-Path $Target ("latest." + [guid]::NewGuid().ToString("N") + ".tmp")
    Move-Item (Join-Path $Stage "latest.yml") $NextManifest
    $CurrentManifest = Join-Path $Target "latest.yml"
    if (Test-Path $CurrentManifest -PathType Leaf) {
        [System.IO.File]::Replace($NextManifest, $CurrentManifest, (Join-Path $Target "latest.previous.yml"))
    } else {
        Move-Item $NextManifest $CurrentManifest
    }
    Write-Host "[BLACKBIRD] Update staged: $($Installer[0].Name)" -ForegroundColor Green
    Write-Host "[BLACKBIRD] Feed: $Target" -ForegroundColor Green
} finally {
    Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue
}
