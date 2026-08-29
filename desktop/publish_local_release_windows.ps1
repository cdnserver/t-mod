[CmdletBinding()]
param(
    [string]$ReleaseRepository = "cdnserver/t-mod-releases"
)

$ErrorActionPreference = "Stop"
$Desktop = Split-Path -Parent $MyInvocation.MyCommand.Path
$Package = Get-Content (Join-Path $Desktop "package.json") -Raw | ConvertFrom-Json
$Version = [string]$Package.version
$Tag = "v$Version"

foreach ($Command in @("node", "pnpm", "gh")) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Command is required to build T-Mod Desktop locally"
    }
}
gh auth status | Out-Null

Push-Location $Desktop
try {
    Write-Host "[T-Mod Desktop] Testing $Version" -ForegroundColor Cyan
    pnpm install --frozen-lockfile
    pnpm test
    pnpm typecheck
    pnpm build
    pnpm check:bundle
    if (Test-Path "release") { Remove-Item "release" -Recurse -Force }

    Write-Host "[T-Mod Desktop] Building Windows x64 installer locally" -ForegroundColor Cyan
    $env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
    pnpm exec electron-builder --win nsis --x64 --publish never

    $Notes = @"
T-Mod Desktop $Version · Beta

- устойчивое подключение и авторизация без ложного экрана «Нет связи»;
- Atlas Overlay стабильнее запускается в GTA V, Majestic и RAGE MP;
- исправлены исчезновения оверлея, масштабирование и несколько мониторов;
- Atlas получил восстановленный поиск по полной нормативной базе.
"@
    $NotesPath = Join-Path $env:TEMP "tmod-desktop-$Version-notes.md"
    Set-Content -Path $NotesPath -Value $Notes -Encoding utf8
    gh release view $Tag --repo $ReleaseRepository *> $null
    if ($LASTEXITCODE -eq 0) {
        gh release edit $Tag --repo $ReleaseRepository --title "T-Mod Desktop $Version · Beta" --notes-file $NotesPath --latest
    } else {
        gh release create $Tag --repo $ReleaseRepository --title "T-Mod Desktop $Version · Beta" --notes-file $NotesPath --latest
    }
    $Files = @(
        Get-ChildItem "release\*.exe", "release\*.exe.blockmap", "release\latest.yml" -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName
    )
    if ($Files.Count -eq 0) { throw "No Windows installers were produced" }
    gh release upload $Tag @Files --repo $ReleaseRepository --clobber
    Write-Host "[T-Mod Desktop] Windows Beta $Version published." -ForegroundColor Green
} finally {
    Pop-Location
}
