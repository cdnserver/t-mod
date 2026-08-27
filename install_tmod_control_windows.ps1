param(
    [string]$ProjectDir = $PSScriptRoot,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
$manifestPath = Join-Path $ProjectDir "tmod_control_version.json"
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "T-Mod Control manifest was not found" }
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$downloadUrl = [string]$manifest.download_url
$expectedHash = ([string]$manifest.sha256).ToLowerInvariant()
$expectedSize = [long]$manifest.size_bytes
if ($downloadUrl -notmatch '^https://github\.com/cdnserver/t-mod-releases/releases/download/control-v[0-9.]+/T-Mod-Control\.exe$') {
    throw "T-Mod Control manifest contains a disallowed download URL"
}
if ($expectedHash -notmatch '^[a-f0-9]{64}$' -or $expectedSize -lt 5MB) { throw "T-Mod Control manifest is invalid" }

$desktopDir = [Environment]::GetFolderPath("Desktop")
$target = Join-Path $desktopDir "T-Mod Control.exe"
$pending = "$target.next"
$legacy = Join-Path $desktopDir "T-Mod Control.bat"

function Test-ControlBinary([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -ne $expectedSize) { return $false }
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -lt 2 -or $bytes[0] -ne 0x4D -or $bytes[1] -ne 0x5A) { return $false }
    return ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -eq $expectedHash)
}

if (Test-ControlBinary $target) {
    Remove-Item -LiteralPath $pending -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $legacy -Force -ErrorAction SilentlyContinue
    if (-not $Quiet) { Write-Host "[T-MOD CONTROL] Version v$($manifest.version) is already installed." -ForegroundColor Green }
    exit 0
}
if (Test-ControlBinary $pending) {
    Remove-Item -LiteralPath $legacy -Force -ErrorAction SilentlyContinue
    if (-not $Quiet) { Write-Host "[T-MOD CONTROL] Version v$($manifest.version) will be applied on the next launch." -ForegroundColor Cyan }
    exit 0
}

$temporary = Join-Path $env:TEMP ("T-Mod-Control-{0}.download" -f [guid]::NewGuid().ToString("N"))
try {
    Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -OutFile $temporary -TimeoutSec 180
    if (-not (Test-ControlBinary $temporary)) { throw "Downloaded EXE failed format or checksum validation" }
    try {
        Move-Item -LiteralPath $temporary -Destination $target -Force
        if (-not $Quiet) { Write-Host "[T-MOD CONTROL] v$($manifest.version) installed: $target" -ForegroundColor Green }
    }
    catch {
        if (-not (Test-Path -LiteralPath $target)) { throw }
        Move-Item -LiteralPath $temporary -Destination $pending -Force
        if (-not $Quiet) { Write-Host "[T-MOD CONTROL] v$($manifest.version) downloaded and will be applied on the next launch." -ForegroundColor Cyan }
    }
    Remove-Item -LiteralPath $legacy -Force -ErrorAction SilentlyContinue
}
finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}
