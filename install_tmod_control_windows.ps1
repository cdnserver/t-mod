param(
    [string]$ProjectDir = $PSScriptRoot,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
$manifestPath = Join-Path $ProjectDir "tmod_control_version.json"
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "Манифест T-Mod Control не найден" }
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$downloadUrl = [string]$manifest.download_url
$expectedHash = ([string]$manifest.sha256).ToLowerInvariant()
$expectedSize = [long]$manifest.size_bytes
if ($downloadUrl -notmatch '^https://github\.com/cdnserver/t-mod-releases/releases/download/control-v[0-9.]+/T-Mod-Control\.exe$') {
    throw "Манифест T-Mod Control содержит недопустимый адрес"
}
if ($expectedHash -notmatch '^[a-f0-9]{64}$' -or $expectedSize -lt 5MB) { throw "Манифест T-Mod Control повреждён" }

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
    if (-not $Quiet) { Write-Host "[T-MOD CONTROL] Уже установлена актуальная версия v$($manifest.version)." -ForegroundColor Green }
    exit 0
}
if (Test-ControlBinary $pending) {
    Remove-Item -LiteralPath $legacy -Force -ErrorAction SilentlyContinue
    if (-not $Quiet) { Write-Host "[T-MOD CONTROL] Обновление v$($manifest.version) применится при следующем запуске." -ForegroundColor Cyan }
    exit 0
}

$temporary = Join-Path $env:TEMP ("T-Mod-Control-{0}.download" -f [guid]::NewGuid().ToString("N"))
try {
    Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -OutFile $temporary -TimeoutSec 180
    if (-not (Test-ControlBinary $temporary)) { throw "Контрольная сумма или формат загруженного EXE не совпали" }
    try {
        Move-Item -LiteralPath $temporary -Destination $target -Force
        if (-not $Quiet) { Write-Host "[T-MOD CONTROL] v$($manifest.version) установлена: $target" -ForegroundColor Green }
    }
    catch {
        if (-not (Test-Path -LiteralPath $target)) { throw }
        Move-Item -LiteralPath $temporary -Destination $pending -Force
        if (-not $Quiet) { Write-Host "[T-MOD CONTROL] v$($manifest.version) загружена и включится при следующем запуске." -ForegroundColor Cyan }
    }
    Remove-Item -LiteralPath $legacy -Force -ErrorAction SilentlyContinue
}
finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}
