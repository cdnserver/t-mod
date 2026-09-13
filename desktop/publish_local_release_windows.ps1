[CmdletBinding()]
param(
    [string]$ReleaseRepository = "cdnserver/t-mod-releases",
    [switch]$BuildOnly
)

$ErrorActionPreference = "Stop"
$Desktop = Split-Path -Parent $MyInvocation.MyCommand.Path
$Package = Get-Content (Join-Path $Desktop "package.json") -Raw | ConvertFrom-Json
$Version = [string]$Package.version
$Tag = "v$Version"
$IsDev = $Version -match '-dev\.'
$Channel = if ($IsDev) { "dev" } else { "latest" }
$ReleaseTitle = if ($IsDev) { "T-Mod Desktop $Version · Dev" } else { "T-Mod Desktop $Version · Beta" }

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE" }
}

foreach ($Command in @("node", "pnpm")) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Command is required to build T-Mod Desktop locally"
    }
}
if (-not $BuildOnly) {
    if (-not (Get-Command "gh" -ErrorAction SilentlyContinue)) {
        throw "gh is required to publish T-Mod Desktop"
    }
    gh auth status | Out-Null
    Assert-NativeSuccess "GitHub authentication"
}

Push-Location $Desktop
try {
    Write-Host "[T-Mod Desktop] Testing $Version" -ForegroundColor Cyan
    pnpm install --frozen-lockfile
    Assert-NativeSuccess "Dependency installation"
    pnpm test
    Assert-NativeSuccess "Desktop tests"
    pnpm typecheck
    Assert-NativeSuccess "Desktop typecheck"
    pnpm build
    Assert-NativeSuccess "Desktop build"
    pnpm check:bundle
    Assert-NativeSuccess "Bundle check"
    if (Test-Path "release") { Remove-Item "release" -Recurse -Force }

    Write-Host "[T-Mod Desktop] Building Windows x64 installer locally" -ForegroundColor Cyan
    $env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
    pnpm exec electron-builder --win nsis --x64 --publish never
    Assert-NativeSuccess "Windows installer build"

    if ($BuildOnly) {
        Write-Host "[T-Mod Desktop] Windows $Channel $Version built locally." -ForegroundColor Green
        return
    }

    $Notes = @"
T-Mod Desktop $Version · $(if ($IsDev) { "Dev" } else { "Beta" })

- глобальная блокировка охватывает известные установки и связанные аккаунты Desktop;
- подтверждённый отпечаток устройства препятствует обходу простым переустановлением;
- решение администратора синхронизируется с веб-сервисами и Discord.
"@
    $NotesPath = Join-Path $env:TEMP "tmod-desktop-$Version-notes.md"
    Set-Content -Path $NotesPath -Value $Notes -Encoding utf8
    gh release view $Tag --repo $ReleaseRepository *> $null
    if ($LASTEXITCODE -ne 0) {
        if ($IsDev) {
            gh release create $Tag --repo $ReleaseRepository --title $ReleaseTitle --notes-file $NotesPath --prerelease --latest=false
        } else {
            gh release create $Tag --repo $ReleaseRepository --title $ReleaseTitle --notes-file $NotesPath
        }
        Assert-NativeSuccess "Create release"
    }
    $Manifest = if ($IsDev) { "release\dev.yml" } else { "release\latest.yml" }
    $Files = @(
        Get-ChildItem "release\*.exe", "release\*.exe.blockmap", $Manifest -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName
    )
    if (-not (Test-Path $Manifest) -or -not ($Files | Where-Object { $_ -like "*.exe" })) {
        throw "Windows installer or $Channel update manifest is missing"
    }
    gh release upload $Tag @Files --repo $ReleaseRepository --clobber
    Assert-NativeSuccess "Upload Windows installer"
    if (-not $IsDev) {
        gh release edit $Tag --repo $ReleaseRepository --latest
        Assert-NativeSuccess "Mark Beta as latest"
    }
    Write-Host "[T-Mod Desktop] Windows $Channel $Version published." -ForegroundColor Green
} finally {
    Pop-Location
}
