param(
    [string]$ProjectDir = $PSScriptRoot,
    [string]$PersistentDir = "$env:USERPROFILE\Documents\SGLDiscordBot",
    [string]$Action = "menu",
    [string]$Service = "",
    [string]$Group = "",
    [switch]$NoAnimation,
    [switch]$Json
)

# Embedded T-Mod Console UI. Generated; edit tmod_console_ui.psm1.
Set-StrictMode -Version 2.0

function Get-TModTheme {
    param([string]$Name = "aurora")
    $themes = @{
        aurora = @{ Accent = [ConsoleColor]::Cyan; Accent2 = [ConsoleColor]::Blue; Glow = [ConsoleColor]::DarkCyan; Good = [ConsoleColor]::Green; Warn = [ConsoleColor]::Yellow; Bad = [ConsoleColor]::Red }
        reactor = @{ Accent = [ConsoleColor]::Green; Accent2 = [ConsoleColor]::DarkGreen; Glow = [ConsoleColor]::DarkCyan; Good = [ConsoleColor]::Green; Warn = [ConsoleColor]::Yellow; Bad = [ConsoleColor]::Red }
        atlas = @{ Accent = [ConsoleColor]::Blue; Accent2 = [ConsoleColor]::Magenta; Glow = [ConsoleColor]::DarkBlue; Good = [ConsoleColor]::Cyan; Warn = [ConsoleColor]::Yellow; Bad = [ConsoleColor]::Red }
        ember = @{ Accent = [ConsoleColor]::Yellow; Accent2 = [ConsoleColor]::DarkRed; Glow = [ConsoleColor]::DarkYellow; Good = [ConsoleColor]::Green; Warn = [ConsoleColor]::Yellow; Bad = [ConsoleColor]::Red }
    }
    $key = $Name.ToLowerInvariant()
    if (-not $themes.ContainsKey($key)) { $key = "aurora" }
    $theme = $themes[$key]
    $theme.Name = $key
    return $theme
}

function Initialize-TModConsole {
    param([string]$Title = "T-Mod Control")
    try {
        [Console]::InputEncoding = New-Object System.Text.UTF8Encoding
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
        [Console]::Title = $Title
        [Console]::CursorVisible = $false
        if ([Console]::WindowWidth -lt 104 -and [Console]::LargestWindowWidth -ge 104) {
            [Console]::SetWindowSize(104, [Math]::Min([Console]::LargestWindowHeight, [Math]::Max(30, [Console]::WindowHeight)))
        }
    }
    catch {}
}

function Get-TModConsoleWidth {
    try { return [Math]::Max(78, [Math]::Min(118, [Console]::WindowWidth - 1)) }
    catch { return 96 }
}

function Write-TModRule {
    param([hashtable]$Theme, [string]$Character = "─")
    if (-not $Theme) { $Theme = Get-TModTheme }
    Write-Host ($Character * (Get-TModConsoleWidth)) -ForegroundColor $Theme.Glow
}

function Write-TModLogo {
    param([hashtable]$Theme)
    if (-not $Theme) { $Theme = Get-TModTheme }
    Write-Host "  ████████╗      ███╗   ███╗ ██████╗ ██████╗ " -ForegroundColor $Theme.Accent
    Write-Host "  ╚══██╔══╝      ████╗ ████║██╔═══██╗██╔══██╗" -ForegroundColor $Theme.Accent
    Write-Host "     ██║   █████╗██╔████╔██║██║   ██║██║  ██║" -ForegroundColor White
    Write-Host "     ██║   ╚════╝██║╚██╔╝██║██║   ██║██║  ██║" -ForegroundColor White
    Write-Host "     ██║         ██║ ╚═╝ ██║╚██████╔╝██████╔╝" -ForegroundColor $Theme.Glow
    Write-Host "     ╚═╝         ╚═╝     ╚═╝ ╚═════╝ ╚═════╝ " -ForegroundColor $Theme.Glow
}

function Write-TModBadge {
    param(
        [string]$Text,
        [ValidateSet("good", "warn", "bad", "info", "muted")][string]$Kind = "info",
        [hashtable]$Theme,
        [switch]$NoNewline
    )
    if (-not $Theme) { $Theme = Get-TModTheme }
    $color = switch ($Kind) {
        "good" { $Theme.Good }
        "warn" { $Theme.Warn }
        "bad" { $Theme.Bad }
        "muted" { [ConsoleColor]::DarkGray }
        default { $Theme.Accent }
    }
    Write-Host (" {0} " -f $Text.ToUpperInvariant()) -ForegroundColor Black -BackgroundColor $color -NoNewline:$NoNewline
}

function Write-TModHeader {
    param(
        [string]$Section = "CONTROL CENTER",
        [string]$Version = "1.0.0",
        [string]$Context = "",
        [hashtable]$Theme
    )
    if (-not $Theme) { $Theme = Get-TModTheme }
    Write-Host ""
    Write-TModLogo -Theme $Theme
    Write-Host ""
    Write-Host ("  {0}" -f $Section) -NoNewline -ForegroundColor White
    Write-Host ("  /  v{0}" -f $Version) -NoNewline -ForegroundColor DarkGray
    if ($Context) { Write-Host ("  /  {0}" -f $Context) -ForegroundColor $Theme.Glow } else { Write-Host "" }
    Write-TModRule -Theme $Theme
}

function Show-TModIntro {
    param([hashtable]$Theme, [switch]$Disabled, [string]$Mode = "CONTROL CENTER")
    if ($Disabled -or [Console]::IsOutputRedirected) { return }
    if (-not $Theme) { $Theme = Get-TModTheme }
    try { [Console]::CursorVisible = $false } catch {}
    $frames = @(
        @{ Star = "                                      ·"; Title = "" },
        @{ Star = "                                ·    ╱"; Title = "" },
        @{ Star = "                         ✦─────────╱"; Title = "T — M O D" },
        @{ Star = "              ────────────◈────────────"; Title = $Mode },
        @{ Star = "           SYSTEM FABRIC SYNCHRONIZED"; Title = "READY" }
    )
    foreach ($frame in $frames) {
        Clear-Host
        Write-Host ""; Write-Host ""; Write-Host ""; Write-Host ""
        Write-Host ("  {0}" -f $frame.Star) -ForegroundColor $Theme.Accent
        Write-Host ""
        Write-Host ("  {0}" -f $frame.Title) -ForegroundColor White
        Start-Sleep -Milliseconds 115
    }
    Start-Sleep -Milliseconds 130
}

function Show-TModTransition {
    param([string]$Label, [hashtable]$Theme, [switch]$Disabled)
    if ($Disabled -or [Console]::IsOutputRedirected) { return }
    if (-not $Theme) { $Theme = Get-TModTheme }
    $glyphs = @("·", "∙", "◦", "○", "◌", "◉")
    foreach ($glyph in $glyphs) {
        Write-Host ("`r  {0}  {1}   " -f $glyph, $Label) -NoNewline -ForegroundColor $Theme.Accent
        Start-Sleep -Milliseconds 42
    }
    Write-Host ""
}

function Write-TModMetric {
    param(
        [string]$Label,
        [string]$Value,
        [ValidateSet("good", "warn", "bad", "info", "muted")][string]$Kind = "info",
        [hashtable]$Theme,
        [int]$Width = 22
    )
    if (-not $Theme) { $Theme = Get-TModTheme }
    Write-Host ("  {0,-$Width}" -f $Label) -NoNewline -ForegroundColor DarkGray
    $color = switch ($Kind) {
        "good" { $Theme.Good }; "warn" { $Theme.Warn }; "bad" { $Theme.Bad }; "muted" { [ConsoleColor]::DarkGray }; default { $Theme.Accent }
    }
    Write-Host $Value -ForegroundColor $color
}

function Write-TModCardRow {
    param([object[]]$Cards, [hashtable]$Theme)
    if (-not $Theme) { $Theme = Get-TModTheme }
    foreach ($card in $Cards) {
        $kind = if ($card.Kind) { [string]$card.Kind } else { "info" }
        Write-Host "  ┌─ " -NoNewline -ForegroundColor $Theme.Glow
        Write-Host ([string]$card.Title) -NoNewline -ForegroundColor White
        Write-Host "  " -NoNewline
        Write-TModBadge -Text ([string]$card.Value) -Kind $kind -Theme $Theme -NoNewline
        Write-Host "  ─┐  " -NoNewline -ForegroundColor $Theme.Glow
    }
    Write-Host ""
}

function Select-TModMenu {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][object[]]$Items,
        [int]$InitialIndex = 0,
        [string]$Subtitle = "↑ ↓ выбрать  ·  Enter открыть  ·  Esc назад",
        [scriptblock]$Header,
        [scriptblock]$OnRender,
        [hashtable]$Hotkeys = @{},
        [hashtable]$Theme,
        [string]$Footer = ""
    )
    if (-not $Theme) { $Theme = Get-TModTheme }
    if ($Items.Count -eq 0) { return $null }
    $index = [Math]::Max(0, [Math]::Min($InitialIndex, $Items.Count - 1))
    try { [Console]::CursorVisible = $false } catch {}
    while ($true) {
        Clear-Host
        if ($Header) { & $Header }
        Write-Host ("  {0}" -f $Title) -ForegroundColor White
        Write-Host ("  {0}" -f $Subtitle) -ForegroundColor DarkGray
        if ($OnRender) { Write-Host ""; & $OnRender }
        Write-Host ""
        for ($itemIndex = 0; $itemIndex -lt $Items.Count; $itemIndex++) {
            $item = $Items[$itemIndex]
            $label = [string]$item.Label
            $hint = [string]$item.Hint
            if ($itemIndex -eq $index) {
                Write-Host "  › " -NoNewline -ForegroundColor $Theme.Accent
                Write-Host (" {0} " -f $label) -NoNewline -ForegroundColor Black -BackgroundColor $Theme.Accent
                if ($hint) { Write-Host ("  {0}" -f $hint) -ForegroundColor $Theme.Glow } else { Write-Host "" }
            }
            else {
                Write-Host ("    {0}" -f $label) -ForegroundColor Gray
                if ($hint) { Write-Host ("      {0}" -f $hint) -ForegroundColor DarkGray }
            }
        }
        if ($Footer) {
            Write-Host ""
            Write-TModRule -Theme $Theme -Character "·"
            Write-Host ("  {0}" -f $Footer) -ForegroundColor DarkGray
        }
        $key = [Console]::ReadKey($true)
        if ($Hotkeys.ContainsKey([string]$key.Key)) { return $Hotkeys[[string]$key.Key] }
        switch ($key.Key) {
            "UpArrow" { $index = if ($index -le 0) { $Items.Count - 1 } else { $index - 1 } }
            "DownArrow" { $index = if ($index -ge $Items.Count - 1) { 0 } else { $index + 1 } }
            "PageUp" { $index = [Math]::Max(0, $index - 5) }
            "PageDown" { $index = [Math]::Min($Items.Count - 1, $index + 5) }
            "Home" { $index = 0 }
            "End" { $index = $Items.Count - 1 }
            "Enter" { return $Items[$index].Value }
            "Escape" { return $null }
        }
    }
}

function Wait-TModKey {
    param([string]$Message = "Нажмите любую клавишу, чтобы вернуться", [hashtable]$Theme)
    if ([Console]::IsInputRedirected) { return }
    if (-not $Theme) { $Theme = Get-TModTheme }
    Write-Host ""
    Write-Host ("  {0}" -f $Message) -ForegroundColor $Theme.Glow
    [Console]::ReadKey($true) | Out-Null
}

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$script:ControlVersion = "1.1.0"
$script:ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
$script:PersistentDir = $PersistentDir
$script:LogDir = Join-Path $PersistentDir "logs"
$script:LogPath = Join-Path $script:LogDir "tmod-control.log"
$script:SettingsPath = Join-Path $PersistentDir "control\settings.json"
$script:OriginalCursorVisible = $true
$script:ReleaseCache = $null
$script:ReleaseCacheAt = [datetime]::MinValue

$script:Services = @(
    "tmod-postgres",
    "tmod-db-migrate",
    "tmod-discord-bot",
    "tmod-web",
    "tmod-worker",
    "atlas-qdrant",
    "atlas-forum-browser",
    "tmod-caddy",
    "minecraft",
    "minecraft-supervisor"
)
$script:ExternalServices = @(
    "tmod-postgres",
    "atlas-qdrant",
    "atlas-forum-browser",
    "tmod-caddy",
    "minecraft"
)
$script:Groups = @{
    "core" = @("tmod-postgres", "tmod-db-migrate", "tmod-discord-bot", "tmod-web", "tmod-worker", "tmod-caddy")
    "atlas" = @("atlas-qdrant", "atlas-forum-browser")
    "minecraft" = @("minecraft", "minecraft-supervisor")
}
$script:AllowedActions = @(
    "menu", "status", "update", "start", "stop", "restart",
    "service-start", "service-stop", "service-restart", "service-update",
    "service-logs", "service-logs-follow",
    "group-start", "group-stop", "group-restart",
    "diagnostics", "backup", "db-status", "db-check",
    "caddy-reload", "open-sites", "auto-update", "auto-update-status", "auto-update-disable",
    "update-status", "git-status", "domain-check", "resources", "error-log",
    "export-diagnostics", "docker-clean", "version"
)

function Get-ControlSettings {
    $defaults = [pscustomobject]@{ theme = "aurora"; animations = $true; compact_dashboard = $false }
    if (-not (Test-Path -LiteralPath $script:SettingsPath)) { return $defaults }
    try {
        $loaded = Get-Content -Raw -LiteralPath $script:SettingsPath | ConvertFrom-Json
        if ([string]$loaded.theme -notin @("aurora", "reactor", "atlas", "ember")) { $loaded.theme = "aurora" }
        if ($null -eq $loaded.animations) { $loaded | Add-Member -NotePropertyName animations -NotePropertyValue $true }
        if ($null -eq $loaded.compact_dashboard) { $loaded | Add-Member -NotePropertyName compact_dashboard -NotePropertyValue $false }
        return $loaded
    }
    catch { return $defaults }
}

function Save-ControlSettings {
    New-Item -ItemType Directory -Path (Split-Path -Parent $script:SettingsPath) -Force | Out-Null
    $temporary = "$($script:SettingsPath).tmp"
    $encoding = New-Object System.Text.UTF8Encoding
    [IO.File]::WriteAllText($temporary, ($script:Settings | ConvertTo-Json -Depth 4), $encoding)
    Move-Item -LiteralPath $temporary -Destination $script:SettingsPath -Force
}

$script:Settings = Get-ControlSettings
$script:Theme = Get-TModTheme ([string]$script:Settings.theme)
$script:AnimationDisabled = $NoAnimation -or -not [bool]$script:Settings.animations
Initialize-TModConsole -Title "T-Mod Control Center"

function Write-ControlLog {
    param([string]$Message)
    try {
        New-Item -ItemType Directory -Path $script:LogDir -Force | Out-Null
        if ((Test-Path -LiteralPath $script:LogPath) -and (Get-Item -LiteralPath $script:LogPath).Length -gt 2MB) {
            Move-Item -LiteralPath $script:LogPath -Destination "$($script:LogPath).previous" -Force
        }
        Add-Content -LiteralPath $script:LogPath -Encoding UTF8 -Value (
            "[{0}] {1}" -f (Get-Date).ToUniversalTime().ToString("o"), $Message
        )
    }
    catch {}
}

function Get-ConsoleWidth {
    return (Get-TModConsoleWidth)
}

function Write-Rule {
    param([ConsoleColor]$Color = [ConsoleColor]::DarkGray)
    Write-TModRule -Theme $script:Theme
}

function Write-Brand {
    param([string]$Section = "CONTROL PLANE")
    $release = Get-ReleaseInfo
    Write-TModHeader -Section $Section -Version $script:ControlVersion -Context ("{0}/{1}" -f $release.Branch, $release.Commit) -Theme $script:Theme
}

function Show-Intro {
    Show-TModIntro -Theme $script:Theme -Disabled:($script:AnimationDisabled -or $env:TMOD_NO_ANIMATION -eq "1") -Mode "CONTROL CENTER"
}

function Wait-ForKey {
    param([string]$Message = "Нажмите любую клавишу, чтобы вернуться")
    Wait-TModKey -Message $Message -Theme $script:Theme
}

function Select-ControlItem {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][object[]]$Items,
        [int]$InitialIndex = 0,
        [string]$Subtitle = "↑ ↓ выбрать  ·  Enter открыть  ·  Esc назад",
        [scriptblock]$OnRender,
        [hashtable]$Hotkeys = @{},
        [string]$Footer = ""
    )
    $header = { Write-Brand }
    return (Select-TModMenu -Title $Title -Items $Items -InitialIndex $InitialIndex -Subtitle $Subtitle -Header $header -OnRender $OnRender -Hotkeys $Hotkeys -Theme $script:Theme -Footer $Footer)
}

function Confirm-ControlAction {
    param([string]$Title, [string]$Description)
    $result = Select-ControlItem -Title $Title -Subtitle $Description -Items @(
        [pscustomobject]@{ Label = "Продолжить"; Hint = "Подтверждаю действие"; Value = "yes" },
        [pscustomobject]@{ Label = "Отмена"; Hint = "Ничего не менять"; Value = "no" }
    ) -InitialIndex 1
    return $result -eq "yes"
}

function Test-DockerReady {
    if (-not (Get-Command "docker.exe" -ErrorAction SilentlyContinue) -and -not (Get-Command "docker" -ErrorAction SilentlyContinue)) { return $false }
    & docker info *> $null
    return $LASTEXITCODE -eq 0
}

function Ensure-DockerReady {
    if (Test-DockerReady) { return $true }
    Write-Host "  Docker Desktop не отвечает. Пытаюсь запустить..." -ForegroundColor Yellow
    $dockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path -LiteralPath $dockerDesktop) { Start-Process -FilePath $dockerDesktop | Out-Null }
    for ($attempt = 0; $attempt -lt 36; $attempt++) {
        Start-Sleep -Seconds 5
        if (Test-DockerReady) { return $true }
        Write-Host "." -NoNewline -ForegroundColor DarkCyan
    }
    Write-Host ""
    Write-Host "  Docker Desktop не запустился за 3 минуты." -ForegroundColor Red
    return $false
}

function Invoke-Compose {
    param([Parameter(Mandatory = $true)][string[]]$Arguments, [switch]$IgnoreExitCode)
    Push-Location $script:ProjectDir
    try {
        & docker compose @Arguments | Out-Host
        $code = $LASTEXITCODE
    }
    finally { Pop-Location }
    if (-not $IgnoreExitCode -and $code -ne 0) { throw "docker compose завершился с кодом $code" }
    return $code
}

function Get-ServiceSnapshot {
    $result = @{}
    foreach ($serviceName in $script:Services) {
        $result[$serviceName] = [pscustomobject]@{ Name = $serviceName; State = "absent"; Health = ""; Status = "не создан" }
    }
    if (-not (Test-DockerReady)) { return $result }
    Push-Location $script:ProjectDir
    try { $raw = (& docker compose ps -a --format json 2>$null | Out-String).Trim() }
    finally { Pop-Location }
    if (-not $raw) { return $result }
    $records = @()
    try {
        $decoded = $raw | ConvertFrom-Json
        if ($decoded -is [System.Array]) { $records = $decoded } else { $records = @($decoded) }
    }
    catch {
        foreach ($line in ($raw -split "`r?`n")) {
            try { $records += ($line | ConvertFrom-Json) } catch {}
        }
    }
    foreach ($record in $records) {
        $name = [string]$record.Service
        if ($result.ContainsKey($name)) {
            $result[$name] = [pscustomobject]@{
                Name = $name
                State = ([string]$record.State).ToLowerInvariant()
                Health = ([string]$record.Health).ToLowerInvariant()
                Status = [string]$record.Status
            }
        }
    }
    return $result
}

function Get-StateLabel {
    param($Snapshot)
    if ($Snapshot.State -eq "running" -and ($Snapshot.Health -eq "healthy" -or -not $Snapshot.Health)) { return "ONLINE" }
    if ($Snapshot.State -eq "running" -and $Snapshot.Health -eq "starting") { return "STARTING" }
    if ($Snapshot.State -eq "exited" -and $Snapshot.Name -eq "tmod-db-migrate") { return "DONE" }
    if ($Snapshot.State -eq "exited") { return "STOPPED" }
    if ($Snapshot.State -eq "paused") { return "PAUSED" }
    return "OFFLINE"
}

function Get-StateColor {
    param([string]$Label)
    if ($Label -eq "ONLINE" -or $Label -eq "DONE") { return [ConsoleColor]::Green }
    if ($Label -eq "STARTING" -or $Label -eq "PAUSED") { return [ConsoleColor]::Yellow }
    if ($Label -eq "STOPPED") { return [ConsoleColor]::DarkYellow }
    return [ConsoleColor]::DarkGray
}

function Get-ReleaseInfo {
    if ($script:ReleaseCache -and ((Get-Date) - $script:ReleaseCacheAt).TotalSeconds -lt 8) { return $script:ReleaseCache }
    if (-not (Get-Command "git.exe" -ErrorAction SilentlyContinue) -and -not (Get-Command "git" -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ Commit = "unknown"; Branch = "git unavailable" }
    }
    $commit = (& git -C $script:ProjectDir rev-parse --short=12 HEAD 2>$null | Select-Object -First 1)
    if (-not $commit) { $commit = "unknown" }
    $branch = (& git -C $script:ProjectDir branch --show-current 2>$null | Select-Object -First 1)
    if (-not $branch) { $branch = "detached" }
    $script:ReleaseCache = [pscustomobject]@{ Commit = $commit.Trim(); Branch = $branch.Trim() }
    $script:ReleaseCacheAt = Get-Date
    return $script:ReleaseCache
}

function Show-Status {
    param([switch]$AsJson)
    $snapshot = Get-ServiceSnapshot
    $release = Get-ReleaseInfo
    $dockerReady = Test-DockerReady
    if ($AsJson) {
        $states = @()
        foreach ($serviceName in $script:Services) {
            $item = $snapshot[$serviceName]
            $states += [pscustomobject]@{ service = $serviceName; state = $item.State; health = $item.Health; label = (Get-StateLabel $item) }
        }
        [pscustomobject]@{
            ok = $true
            control_version = $script:ControlVersion
            release = $release.Commit
            branch = $release.Branch
            docker_ready = $dockerReady
            services = $states
        } | ConvertTo-Json -Depth 5
        return
    }
    Clear-Host
    Write-Brand -Section "SYSTEM OVERVIEW"
    Write-Host ("  Release  {0} / {1}" -f $release.Branch, $release.Commit) -ForegroundColor White
    Write-Host "  Docker   " -NoNewline -ForegroundColor DarkGray
    if ($dockerReady) { Write-Host "CONNECTED" -ForegroundColor Green } else { Write-Host "UNAVAILABLE" -ForegroundColor Red }
    Write-Host ""
    foreach ($serviceName in $script:Services) {
        $item = $snapshot[$serviceName]
        $label = Get-StateLabel $item
        Write-Host ("  {0,-26}" -f $serviceName) -NoNewline -ForegroundColor Gray
        Write-Host ("{0,-10}" -f $label) -NoNewline -ForegroundColor (Get-StateColor $label)
        if ($item.Health) { Write-Host (" {0}" -f $item.Health) -ForegroundColor DarkGray }
        else { Write-Host "" }
    }
    Write-Rule
    Write-Host "  reactor.tvr.lat  ·  consensus.tvr.lat  ·  atlas.tvr.lat" -ForegroundColor DarkCyan
}

function Invoke-GuardedUpdate {
    if (-not (Ensure-DockerReady)) { return 1 }
    $launcher = Join-Path $script:ProjectDir "launch_tmod_guarded_windows.ps1"
    if (-not (Test-Path -LiteralPath $launcher)) { throw "Защищённый updater не найден: $launcher" }
    Write-ControlLog "safe update requested"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launcher -ProjectDir $script:ProjectDir -PersistentDir $script:PersistentDir -Branch main -Remote origin | Out-Host
    $code = $LASTEXITCODE
    return $code
}

function Invoke-InstalledStart {
    if (-not (Ensure-DockerReady)) { return 1 }
    $runtime = Join-Path $script:ProjectDir "run_windows.bat"
    $oldSkip = $env:TMOD_SKIP_BUILD
    $oldNonInteractive = $env:TMOD_NONINTERACTIVE
    try {
        $env:TMOD_SKIP_BUILD = "0"
        $env:TMOD_NONINTERACTIVE = "1"
        & cmd.exe /d /c ('call "{0}"' -f $runtime) | Out-Host
        $code = $LASTEXITCODE
        return $code
    }
    finally { $env:TMOD_SKIP_BUILD = $oldSkip; $env:TMOD_NONINTERACTIVE = $oldNonInteractive }
}

function Invoke-ServiceUpdate {
    param([string]$ServiceName)
    if (-not (Ensure-DockerReady)) { return 1 }
    if ($script:ExternalServices -contains $ServiceName) {
        Invoke-Compose -Arguments @("pull", $ServiceName) | Out-Null
    }
    else {
        $buildService = $ServiceName
        if ($ServiceName -in @("tmod-db-migrate", "tmod-web", "tmod-worker")) { $buildService = "tmod-discord-bot" }
        Invoke-Compose -Arguments @("build", $buildService) | Out-Null
    }
    Invoke-Compose -Arguments @("up", "-d", "--no-deps", "--force-recreate", $ServiceName) | Out-Null
    return 0
}

function Invoke-DatabaseCommand {
    param([ValidateSet("status", "check", "backup")][string]$DatabaseAction)
    if (-not (Ensure-DockerReady)) { return 1 }
    $arguments = @("exec", "tmod-worker", "python", "/app/scripts/tmod_db_guard.py")
    if ($DatabaseAction -eq "backup") { $arguments += @("backup", "--kind", "manual", "--note", "tmod-control") }
    elseif ($DatabaseAction -eq "check") { $arguments += @("check", "--full") }
    else { $arguments += "status" }
    & docker @arguments | Out-Host
    $code = $LASTEXITCODE
    return $code
}

function Invoke-Diagnostics {
    Clear-Host
    Write-Brand -Section "DIAGNOSTICS"
    $checks = @()
    $checks += [pscustomobject]@{ Name = "Docker engine"; Ok = (Test-DockerReady); Detail = "docker info" }
    try {
        $gateway = Invoke-RestMethod -Uri "http://127.0.0.1:8787/gateway-health" -TimeoutSec 4
        $checks += [pscustomobject]@{ Name = "Web gateway"; Ok = ($gateway.status -eq "ok"); Detail = "127.0.0.1:8787" }
    }
    catch { $checks += [pscustomobject]@{ Name = "Web gateway"; Ok = $false; Detail = $_.Exception.Message } }
    try {
        $ready = Invoke-RestMethod -Uri "http://127.0.0.1:8787/api/health?ready=1" -TimeoutSec 5
        $checks += [pscustomobject]@{ Name = "Discord runtime"; Ok = ($ready.status -eq "ok" -and $ready.discord_ready); Detail = "ready endpoint" }
    }
    catch { $checks += [pscustomobject]@{ Name = "Discord runtime"; Ok = $false; Detail = $_.Exception.Message } }
    $drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($script:PersistentDir).TrimEnd("\").TrimEnd(":")) -ErrorAction SilentlyContinue
    if ($drive) {
        $freeGb = [Math]::Round($drive.Free / 1GB, 1)
        $checks += [pscustomobject]@{ Name = "Свободное место"; Ok = ($drive.Free -gt 10GB); Detail = "$freeGb GB" }
    }
    foreach ($check in $checks) {
        Write-Host ("  {0,-24}" -f $check.Name) -NoNewline -ForegroundColor Gray
        if ($check.Ok) { Write-Host "PASS" -NoNewline -ForegroundColor Green } else { Write-Host "FAIL" -NoNewline -ForegroundColor Red }
        Write-Host ("  {0}" -f $check.Detail) -ForegroundColor DarkGray
    }
    Write-Host ""
    Write-Host "  Последние ошибки контейнеров" -ForegroundColor White
    Write-Rule
    foreach ($container in @("tmod-discord-bot", "tmod-web", "tmod-worker", "tmod-caddy")) {
        Write-Host ("  [{0}]" -f $container) -ForegroundColor Cyan
        & docker logs --since 15m --tail 25 $container 2>&1 | Select-String -Pattern "error|exception|traceback|fatal|panic|503|504" -CaseSensitive:$false | Select-Object -Last 6
    }
}

function Get-JsonFileSafe {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return (Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json) }
    catch { return $null }
}

function Show-UpdateState {
    Clear-Host
    Write-Brand -Section "UPDATE CENTER"
    $safeState = Get-JsonFileSafe (Join-Path $script:PersistentDir "updates\status.json")
    $watcherState = Get-JsonFileSafe (Join-Path $script:PersistentDir "updates\watcher.json")
    $guardState = Get-JsonFileSafe (Join-Path $script:PersistentDir "updates\launcher_guard.json")
    foreach ($entry in @(
        [pscustomobject]@{ Label = "Safe Update"; Data = $safeState },
        [pscustomobject]@{ Label = "GitHub watcher"; Data = $watcherState },
        [pscustomobject]@{ Label = "Launch guard"; Data = $guardState }
    )) {
        Write-Host ("  {0}" -f $entry.Label) -ForegroundColor White
        if ($entry.Data) {
            Write-TModMetric -Label "Состояние" -Value ([string]$entry.Data.state) -Kind $(if ([string]$entry.Data.state -match "success|current|updated|fallback_started") { "good" } elseif ([string]$entry.Data.state -match "error|failed|rolled_back") { "bad" } else { "warn" }) -Theme $script:Theme
            if ($entry.Data.message) { Write-Host ("    {0}" -f [string]$entry.Data.message) -ForegroundColor Gray }
            if ($entry.Data.updated_at) { Write-Host ("    {0}" -f [string]$entry.Data.updated_at) -ForegroundColor DarkGray }
            elseif ($entry.Data.checked_at) { Write-Host ("    {0}" -f [string]$entry.Data.checked_at) -ForegroundColor DarkGray }
        }
        else { Write-Host "    Состояние ещё не записано." -ForegroundColor DarkGray }
        Write-Host ""
    }
}

function Show-GitState {
    Clear-Host
    Write-Brand -Section "RELEASE CHANNEL"
    if (-not (Get-Command "git.exe" -ErrorAction SilentlyContinue) -and -not (Get-Command "git" -ErrorAction SilentlyContinue)) {
        Write-Host "  Git for Windows не найден." -ForegroundColor $script:Theme.Bad
        return
    }
    $release = Get-ReleaseInfo
    $remoteCommit = (& git -C $script:ProjectDir rev-parse --short=12 origin/main 2>$null | Select-Object -First 1)
    $dirty = (& git -C $script:ProjectDir status --porcelain --untracked-files=normal 2>$null | Out-String).Trim()
    Write-TModMetric -Label "Ветка" -Value $release.Branch -Kind "info" -Theme $script:Theme
    Write-TModMetric -Label "Установлено" -Value $release.Commit -Kind "good" -Theme $script:Theme
    Write-TModMetric -Label "Известный origin/main" -Value $(if ($remoteCommit) { $remoteCommit.Trim() } else { "неизвестно" }) -Kind $(if ($remoteCommit -and $remoteCommit.Trim() -ne $release.Commit) { "warn" } else { "good" }) -Theme $script:Theme
    Write-TModMetric -Label "Рабочая копия" -Value $(if ($dirty) { "есть локальные изменения" } else { "чистая" }) -Kind $(if ($dirty) { "warn" } else { "good" }) -Theme $script:Theme
    if ($dirty) { Write-Host ""; Write-Host $dirty -ForegroundColor DarkYellow }
    Write-Host ""
    Write-Host "  Точная проверка GitHub выполняется защищённым updater с ограничением времени." -ForegroundColor DarkGray
}

function Invoke-DomainCheck {
    Clear-Host
    Write-Brand -Section "NETWORK FABRIC"
    foreach ($domain in @("tvr.lat", "reactor.tvr.lat", "consensus.tvr.lat", "atlas.tvr.lat", "sgl.tvr.lat", "ovr.tvr.lat")) {
        $latency = [Diagnostics.Stopwatch]::StartNew()
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri ("https://{0}/gateway-health" -f $domain) -TimeoutSec 7
            $latency.Stop()
            Write-TModMetric -Label $domain -Value ("HTTP {0} · {1} ms" -f [int]$response.StatusCode, $latency.ElapsedMilliseconds) -Kind "good" -Theme $script:Theme -Width 28
        }
        catch {
            $latency.Stop()
            Write-TModMetric -Label $domain -Value ("недоступен · {0}" -f $_.Exception.Message) -Kind "bad" -Theme $script:Theme -Width 28
        }
    }
}

function Show-ResourceSnapshot {
    Clear-Host
    Write-Brand -Section "RESOURCE TELEMETRY"
    if (-not (Test-DockerReady)) { Write-Host "  Docker недоступен." -ForegroundColor Red; return }
    & docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}" | Out-Host
    Write-Host ""
    $driveName = [IO.Path]::GetPathRoot($script:PersistentDir).TrimEnd("\").TrimEnd(":")
    $drive = Get-PSDrive -Name $driveName -ErrorAction SilentlyContinue
    if ($drive) {
        Write-TModMetric -Label "Диск свободен" -Value ("{0:N1} GB" -f ($drive.Free / 1GB)) -Kind $(if ($drive.Free -gt 20GB) { "good" } elseif ($drive.Free -gt 8GB) { "warn" } else { "bad" }) -Theme $script:Theme
        Write-TModMetric -Label "Диск занят" -Value ("{0:N1} GB" -f ($drive.Used / 1GB)) -Kind "info" -Theme $script:Theme
    }
}

function Show-ErrorCenter {
    Clear-Host
    Write-Brand -Section "INCIDENT STREAM"
    foreach ($container in @("tmod-discord-bot", "tmod-web", "tmod-worker", "tmod-caddy", "tmod-postgres", "minecraft")) {
        Write-Host ("  ◈ {0}" -f $container) -ForegroundColor $script:Theme.Accent
        $matches = @(& docker logs --since 60m --tail 240 $container 2>&1 | Select-String -Pattern "error|exception|traceback|fatal|panic|503|504|unhealthy" -CaseSensitive:$false | Select-Object -Last 10)
        if ($matches.Count -eq 0) { Write-Host "    За последний час критических записей нет." -ForegroundColor DarkGreen }
        else { $matches | ForEach-Object { Write-Host ("    {0}" -f $_.Line) -ForegroundColor DarkYellow } }
        Write-Host ""
    }
}

function Export-DiagnosticReport {
    New-Item -ItemType Directory -Path $script:LogDir -Force | Out-Null
    $reportPath = Join-Path $script:LogDir ("diagnostic-{0}.txt" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("T-Mod diagnostic report")
    $lines.Add(("Generated: {0}" -f (Get-Date).ToUniversalTime().ToString("o")))
    $lines.Add(("Control: {0}" -f $script:ControlVersion))
    $lines.Add(("Project: {0}" -f $script:ProjectDir))
    $lines.Add("")
    $lines.Add("=== docker compose ps ===")
    Push-Location $script:ProjectDir
    try { (& docker compose ps -a 2>&1) | ForEach-Object { $lines.Add([string]$_) } }
    finally { Pop-Location }
    foreach ($container in @("tmod-discord-bot", "tmod-web", "tmod-worker", "tmod-caddy")) {
        $lines.Add("")
        $lines.Add(("=== {0} errors ===" -f $container))
        (& docker logs --since 60m --tail 300 $container 2>&1 | Select-String -Pattern "error|exception|traceback|fatal|panic|503|504" -CaseSensitive:$false | Select-Object -Last 40) | ForEach-Object { $lines.Add($_.Line) }
    }
    $encoding = New-Object System.Text.UTF8Encoding
    [IO.File]::WriteAllLines($reportPath, $lines, $encoding)
    Write-Host ("  Отчёт сохранён: {0}" -f $reportPath) -ForegroundColor Green
    return 0
}

function Show-AutoUpdateStatus {
    Clear-Host
    Write-Brand -Section "AUTOMATION"
    & schtasks.exe /Query /TN "T-Mod Auto Update" /FO LIST /V 2>&1 | Out-Host
    return $LASTEXITCODE
}

function Disable-AutoUpdate {
    & schtasks.exe /Change /TN "T-Mod Auto Update" /Disable 2>&1 | Out-Host
    return $LASTEXITCODE
}

function Invoke-SafeDockerCleanup {
    if (-not (Ensure-DockerReady)) { return 1 }
    Write-Host "  Удаляю только неиспользуемые образы и build-cache старше 7 дней." -ForegroundColor Yellow
    Write-Host "  Volumes, базы данных и работающие контейнеры не затрагиваются." -ForegroundColor DarkGray
    & docker image prune -f --filter "until=168h" | Out-Host
    if ($LASTEXITCODE -ne 0) { return $LASTEXITCODE }
    & docker builder prune -f --filter "until=168h" | Out-Host
    return $LASTEXITCODE
}

function Get-DashboardData {
    $snapshot = Get-ServiceSnapshot
    function Get-ContourState([string[]]$Names) {
        $online = 0
        foreach ($name in $Names) {
            $label = Get-StateLabel $snapshot[$name]
            if ($label -in @("ONLINE", "DONE")) { $online++ }
        }
        if ($online -eq $Names.Count) { return [pscustomobject]@{ Value = "online"; Kind = "good" } }
        if ($online -gt 0) { return [pscustomobject]@{ Value = "$online/$($Names.Count)"; Kind = "warn" } }
        return [pscustomobject]@{ Value = "offline"; Kind = "bad" }
    }
    $watcher = Get-JsonFileSafe (Join-Path $script:PersistentDir "updates\watcher.json")
    return [pscustomobject]@{
        Core = Get-ContourState @("tmod-postgres", "tmod-discord-bot", "tmod-web", "tmod-worker", "tmod-caddy")
        Atlas = Get-ContourState @("atlas-qdrant", "atlas-forum-browser")
        Minecraft = Get-ContourState @("minecraft", "minecraft-supervisor")
        Watcher = if ($watcher) { [string]$watcher.state } else { "нет данных" }
    }
}

function Write-MainDashboard {
    param($Dashboard)
    Write-TModCardRow -Theme $script:Theme -Cards @(
        [pscustomobject]@{ Title = "CORE"; Value = $Dashboard.Core.Value; Kind = $Dashboard.Core.Kind },
        [pscustomobject]@{ Title = "ATLAS"; Value = $Dashboard.Atlas.Value; Kind = $Dashboard.Atlas.Kind },
        [pscustomobject]@{ Title = "MINECRAFT"; Value = $Dashboard.Minecraft.Value; Kind = $Dashboard.Minecraft.Kind }
    )
    if (-not [bool]$script:Settings.compact_dashboard) {
        Write-Host ""
        Write-TModMetric -Label "GitHub watcher" -Value $Dashboard.Watcher -Kind $(if ($Dashboard.Watcher -match "current|updated") { "good" } elseif ($Dashboard.Watcher -match "error|blocked") { "warn" } else { "info" }) -Theme $script:Theme
        $events = @()
        if (Test-Path -LiteralPath $script:LogPath) { $events = @(Get-Content -LiteralPath $script:LogPath -Tail 2 -ErrorAction SilentlyContinue) }
        foreach ($eventLine in $events) { Write-Host ("  · {0}" -f $eventLine) -ForegroundColor DarkGray }
    }
}

function Invoke-ControlAction {
    param([string]$RequestedAction, [string]$RequestedService = "", [string]$RequestedGroup = "")
    if ($script:AllowedActions -notcontains $RequestedAction) { throw "Недопустимое действие: $RequestedAction" }
    if ($RequestedService -and $script:Services -notcontains $RequestedService) { throw "Неизвестный сервис: $RequestedService" }
    if ($RequestedGroup -and -not $script:Groups.ContainsKey($RequestedGroup)) { throw "Неизвестная группа: $RequestedGroup" }
    Write-ControlLog ("action={0} service={1} group={2}" -f $RequestedAction, $RequestedService, $RequestedGroup)
    $dockerActions = @(
        "stop", "restart", "service-start", "service-stop", "service-restart", "service-update",
        "service-logs", "service-logs-follow", "group-start", "group-stop", "group-restart", "caddy-reload",
        "resources", "error-log", "export-diagnostics"
    )
    if ($dockerActions -contains $RequestedAction -and -not (Ensure-DockerReady)) { return 1 }
    switch ($RequestedAction) {
        "status" { Show-Status -AsJson:$Json | Out-Host; return 0 }
        "version" { Write-Output $script:ControlVersion | Out-Host; return 0 }
        "update" { return (Invoke-GuardedUpdate) }
        "start" { return (Invoke-InstalledStart) }
        "stop" { Invoke-Compose -Arguments @("stop") | Out-Null; return 0 }
        "restart" { Invoke-Compose -Arguments @("up", "-d", "--force-recreate", "--remove-orphans") | Out-Null; return 0 }
        "service-start" { Invoke-Compose -Arguments @("up", "-d", $RequestedService) | Out-Null; return 0 }
        "service-stop" { Invoke-Compose -Arguments @("stop", $RequestedService) | Out-Null; return 0 }
        "service-restart" { Invoke-Compose -Arguments @("restart", $RequestedService) | Out-Null; return 0 }
        "service-update" { return (Invoke-ServiceUpdate $RequestedService) }
        "service-logs" { Invoke-Compose -Arguments @("logs", "--no-color", "--tail", "160", $RequestedService) -IgnoreExitCode | Out-Null; return 0 }
        "service-logs-follow" { Invoke-Compose -Arguments @("logs", "--tail", "80", "-f", $RequestedService) -IgnoreExitCode | Out-Null; return 0 }
        "group-start" { Invoke-Compose -Arguments (@("up", "-d") + $script:Groups[$RequestedGroup]) | Out-Null; return 0 }
        "group-stop" { Invoke-Compose -Arguments (@("stop") + $script:Groups[$RequestedGroup]) | Out-Null; return 0 }
        "group-restart" {
            $restartServices = @($script:Groups[$RequestedGroup] | Where-Object { $_ -ne "tmod-db-migrate" })
            Invoke-Compose -Arguments (@("restart") + $restartServices) | Out-Null
            return 0
        }
        "diagnostics" { Invoke-Diagnostics; return 0 }
        "update-status" { Show-UpdateState; return 0 }
        "git-status" { Show-GitState; return 0 }
        "domain-check" { Invoke-DomainCheck; return 0 }
        "resources" { Show-ResourceSnapshot; return 0 }
        "error-log" { Show-ErrorCenter; return 0 }
        "export-diagnostics" { return (Export-DiagnosticReport) }
        "docker-clean" { return (Invoke-SafeDockerCleanup) }
        "backup" { return (Invoke-DatabaseCommand "backup") }
        "db-status" { return (Invoke-DatabaseCommand "status") }
        "db-check" { return (Invoke-DatabaseCommand "check") }
        "caddy-reload" {
            & docker exec tmod-caddy caddy validate --config /etc/caddy/Caddyfile
            if ($LASTEXITCODE -ne 0) { return $LASTEXITCODE }
            & docker exec tmod-caddy caddy reload --config /etc/caddy/Caddyfile
            return $LASTEXITCODE
        }
        "open-sites" {
            foreach ($url in @("https://tvr.lat", "https://reactor.tvr.lat", "https://consensus.tvr.lat", "https://atlas.tvr.lat", "https://sgl.tvr.lat")) { Start-Process $url }
            return 0
        }
        "auto-update" {
            & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $script:ProjectDir "configure_auto_update_windows.ps1") -ProjectDir $script:ProjectDir -IntervalMinutes 2
            return $LASTEXITCODE
        }
        "auto-update-status" { return (Show-AutoUpdateStatus) }
        "auto-update-disable" { return (Disable-AutoUpdate) }
    }
    return 1
}

function Show-ServiceMenu {
    while ($true) {
        $snapshot = Get-ServiceSnapshot
        $items = @()
        foreach ($serviceName in $script:Services) {
            $state = Get-StateLabel $snapshot[$serviceName]
            $items += [pscustomobject]@{ Label = $serviceName; Hint = $state; Value = $serviceName }
        }
        $items += [pscustomobject]@{ Label = "Назад"; Hint = "Главное меню"; Value = "back" }
        $selectedService = Select-ControlItem -Title "Сервисы" -Items $items
        if (-not $selectedService -or $selectedService -eq "back") { return }
        $serviceAction = Select-ControlItem -Title $selectedService -Items @(
            [pscustomobject]@{ Label = "Запустить"; Hint = "Создать контейнер при необходимости"; Value = "service-start" },
            [pscustomobject]@{ Label = "Перезапустить"; Hint = "Мягкий restart контейнера"; Value = "service-restart" },
            [pscustomobject]@{ Label = "Остановить"; Hint = "Данные и конфигурация сохранятся"; Value = "service-stop" },
            [pscustomobject]@{ Label = "Обновить сервис"; Hint = "Получить/собрать образ и пересоздать контейнер"; Value = "service-update" },
            [pscustomobject]@{ Label = "Последние логи"; Hint = "160 последних строк"; Value = "service-logs" },
            [pscustomobject]@{ Label = "Живые логи"; Hint = "Ctrl+C завершает просмотр"; Value = "service-logs-follow" },
            [pscustomobject]@{ Label = "Назад"; Hint = "К списку сервисов"; Value = "back" }
        )
        if (-not $serviceAction -or $serviceAction -eq "back") { continue }
        if ($serviceAction -eq "service-stop" -and -not (Confirm-ControlAction "Остановить $selectedService?" "Сервис станет временно недоступен.")) { continue }
        Clear-Host; Write-Brand -Section "SERVICE CONTROL"
        try { $code = Invoke-ControlAction $serviceAction $selectedService; if ($code -eq 0) { Write-Host "  Готово." -ForegroundColor Green } }
        catch { Write-Host ("  Ошибка: {0}" -f $_.Exception.Message) -ForegroundColor Red }
        Wait-ForKey
    }
}

function Show-GroupMenu {
    $groupItems = @(
        [pscustomobject]@{ Label = "Ядро T-Mod"; Hint = "PostgreSQL, Discord, Web, Worker, Caddy"; Value = "core" },
        [pscustomobject]@{ Label = "Atlas"; Hint = "Qdrant и браузер форума"; Value = "atlas" },
        [pscustomobject]@{ Label = "Minecraft"; Hint = "Сервер и supervisor"; Value = "minecraft" },
        [pscustomobject]@{ Label = "Назад"; Hint = "Главное меню"; Value = "back" }
    )
    while ($true) {
        $selectedGroup = Select-ControlItem -Title "Контуры" -Items $groupItems
        if (-not $selectedGroup -or $selectedGroup -eq "back") { return }
        $groupAction = Select-ControlItem -Title ("Контур: {0}" -f $selectedGroup) -Items @(
            [pscustomobject]@{ Label = "Запустить контур"; Hint = "Только выбранная группа"; Value = "group-start" },
            [pscustomobject]@{ Label = "Перезапустить контур"; Hint = "Только выбранная группа"; Value = "group-restart" },
            [pscustomobject]@{ Label = "Остановить контур"; Hint = "Остальные сервисы продолжат работу"; Value = "group-stop" },
            [pscustomobject]@{ Label = "Назад"; Hint = "К контурам"; Value = "back" }
        )
        if (-not $groupAction -or $groupAction -eq "back") { continue }
        if ($groupAction -eq "group-stop" -and -not (Confirm-ControlAction "Остановить контур?" "Будут остановлены только сервисы контура $selectedGroup.")) { continue }
        Clear-Host; Write-Brand -Section "CONTOUR CONTROL"
        try { Invoke-ControlAction $groupAction "" $selectedGroup | Out-Null; Write-Host "  Готово." -ForegroundColor Green }
        catch { Write-Host ("  Ошибка: {0}" -f $_.Exception.Message) -ForegroundColor Red }
        Wait-ForKey
    }
}

function Show-DataMenu {
    while ($true) {
        $dataAction = Select-ControlItem -Title "Защита данных" -Items @(
            [pscustomobject]@{ Label = "Создать резервную копию"; Hint = "Согласованный ручной backup PostgreSQL"; Value = "backup" },
            [pscustomobject]@{ Label = "Состояние резервов"; Hint = "Последние копии, объём и свободное место"; Value = "db-status" },
            [pscustomobject]@{ Label = "Полная проверка базы"; Hint = "Может занять несколько минут"; Value = "db-check" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главное меню"; Value = "back" }
        )
        if (-not $dataAction -or $dataAction -eq "back") { return }
        Clear-Host; Write-Brand -Section "DATA GUARD"
        try { Invoke-ControlAction $dataAction | Out-Null }
        catch { Write-Host ("  Ошибка: {0}" -f $_.Exception.Message) -ForegroundColor Red }
        Wait-ForKey
    }
}

function Invoke-InteractiveAction {
    param([string]$RequestedAction, [string]$Section = "OPERATION")
    Show-TModTransition -Label $Section -Theme $script:Theme -Disabled:$script:AnimationDisabled
    Clear-Host
    Write-Brand -Section $Section
    try {
        $exitCode = Invoke-ControlAction $RequestedAction
        if ($exitCode -eq 0 -and $RequestedAction -notin @("status", "diagnostics", "update-status", "git-status", "domain-check", "resources", "error-log", "auto-update-status")) {
            Write-Host "  Операция завершена." -ForegroundColor $script:Theme.Good
        }
        elseif ($exitCode -ne 0) { Write-Host ("  Операция завершилась с кодом {0}." -f $exitCode) -ForegroundColor $script:Theme.Bad }
    }
    catch {
        Write-ControlLog ("error action={0}: {1}" -f $RequestedAction, $_.Exception.Message)
        Write-Host ("  Ошибка: {0}" -f $_.Exception.Message) -ForegroundColor $script:Theme.Bad
    }
    Wait-ForKey
}

function Show-PowerMenu {
    while ($true) {
        $powerAction = Select-ControlItem -Title "Управление питанием системы" -Items @(
            [pscustomobject]@{ Label = "Запустить установленную версию"; Hint = "Полная подготовка, сборка и запуск"; Value = "start" },
            [pscustomobject]@{ Label = "Перезапустить всю систему"; Hint = "Пересоздание с соблюдением зависимостей"; Value = "restart" },
            [pscustomobject]@{ Label = "Остановить всю систему"; Hint = "Контейнеры остановятся, данные сохранятся"; Value = "stop" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главный экран"; Value = "back" }
        ) -Footer "Данные PostgreSQL, Qdrant и Minecraft никогда не удаляются этим экраном."
        if (-not $powerAction -or $powerAction -eq "back") { return }
        if ($powerAction -in @("restart", "stop") -and -not (Confirm-ControlAction "Подтвердить действие?" "Сервисы временно станут недоступны.")) { continue }
        Invoke-InteractiveAction $powerAction "POWER CONTROL"
    }
}

function Show-UpdateMenu {
    while ($true) {
        $updateAction = Select-ControlItem -Title "Центр обновлений" -Items @(
            [pscustomobject]@{ Label = "Безопасно обновить и запустить"; Hint = "GitHub → тесты → backup → healthcheck → rollback"; Value = "update" },
            [pscustomobject]@{ Label = "История обновлений"; Hint = "Safe Update, watcher и launch guard"; Value = "update-status" },
            [pscustomobject]@{ Label = "Состояние Git"; Hint = "Ветка, commit, origin/main и локальные изменения"; Value = "git-status" },
            [pscustomobject]@{ Label = "Состояние автообновления"; Hint = "Задача Windows Task Scheduler"; Value = "auto-update-status" },
            [pscustomobject]@{ Label = "Включить автообновление"; Hint = "Проверка origin/main раз в 2 минуты"; Value = "auto-update" },
            [pscustomobject]@{ Label = "Приостановить автообновление"; Hint = "Ручной запуск останется доступен"; Value = "auto-update-disable" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главный экран"; Value = "back" }
        ) -Footer "Неудачный релиз автоматически помещается в карантин до следующего коммита."
        if (-not $updateAction -or $updateAction -eq "back") { return }
        if ($updateAction -eq "auto-update-disable" -and -not (Confirm-ControlAction "Приостановить автообновление?" "T-Mod продолжит работать на текущей версии.")) { continue }
        Invoke-InteractiveAction $updateAction "UPDATE CENTER"
    }
}

function Show-ObservabilityMenu {
    while ($true) {
        $observeAction = Select-ControlItem -Title "Наблюдение и диагностика" -Items @(
            [pscustomobject]@{ Label = "Полная диагностика"; Hint = "Docker, Web, Discord, диск и свежие ошибки"; Value = "diagnostics" },
            [pscustomobject]@{ Label = "Поток инцидентов"; Hint = "Ошибки всех ключевых контейнеров за час"; Value = "error-log" },
            [pscustomobject]@{ Label = "Ресурсы"; Hint = "CPU, RAM, сеть и свободное место"; Value = "resources" },
            [pscustomobject]@{ Label = "Экспортировать отчёт"; Hint = "Сохранить диагностический пакет в Documents"; Value = "export-diagnostics" },
            [pscustomobject]@{ Label = "Очистить старый Docker cache"; Hint = "Только неиспользуемое старше 7 дней; volumes не трогаются"; Value = "docker-clean" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главный экран"; Value = "back" }
        ) -Footer "Живые логи конкретного сервиса находятся в разделе «Сервисы»."
        if (-not $observeAction -or $observeAction -eq "back") { return }
        if ($observeAction -eq "docker-clean" -and -not (Confirm-ControlAction "Очистить старый Docker cache?" "Работающие контейнеры и данные не затрагиваются.")) { continue }
        Invoke-InteractiveAction $observeAction "OBSERVABILITY"
    }
}

function Show-NetworkMenu {
    while ($true) {
        $networkAction = Select-ControlItem -Title "Сеть и публичные сервисы" -Items @(
            [pscustomobject]@{ Label = "Проверить все домены"; Hint = "HTTPS-ответ и задержка каждого контура"; Value = "domain-check" },
            [pscustomobject]@{ Label = "Проверить и применить Caddy"; Hint = "Validate перед безопасным reload"; Value = "caddy-reload" },
            [pscustomobject]@{ Label = "Открыть сервисы"; Hint = "T-Mod, Reactor, Consensus, Atlas и SGL"; Value = "open-sites" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главный экран"; Value = "back" }
        ) -Footer "Публичные запросы идут через Caddy; внутренний порт 8787 остаётся на localhost."
        if (-not $networkAction -or $networkAction -eq "back") { return }
        Invoke-InteractiveAction $networkAction "NETWORK FABRIC"
    }
}

function Show-ThemeMenu {
    $themeName = Select-ControlItem -Title "Цветовой контур" -Items @(
        [pscustomobject]@{ Label = "Aurora"; Hint = "Холодный cyan — основной стиль T-Mod"; Value = "aurora" },
        [pscustomobject]@{ Label = "Reactor"; Hint = "Зелёный инженерный контур"; Value = "reactor" },
        [pscustomobject]@{ Label = "Atlas"; Hint = "Синий и фиолетовый интеллект"; Value = "atlas" },
        [pscustomobject]@{ Label = "Ember"; Hint = "Янтарный аварийный контур"; Value = "ember" },
        [pscustomobject]@{ Label = "Назад"; Hint = "Настройки"; Value = "back" }
    )
    if (-not $themeName -or $themeName -eq "back") { return }
    $script:Settings.theme = $themeName
    $script:Theme = Get-TModTheme $themeName
    Save-ControlSettings
    Show-TModTransition -Label ("THEME / {0}" -f $themeName.ToUpperInvariant()) -Theme $script:Theme -Disabled:$script:AnimationDisabled
}

function Show-SettingsMenu {
    while ($true) {
        $settingsAction = Select-ControlItem -Title "Настройки Control Center" -Items @(
            [pscustomobject]@{ Label = "Цветовой контур"; Hint = ("Сейчас: {0}" -f $script:Settings.theme); Value = "theme" },
            [pscustomobject]@{ Label = "Анимации"; Hint = $(if ([bool]$script:Settings.animations) { "Включены" } else { "Выключены" }); Value = "animations" },
            [pscustomobject]@{ Label = "Компактный дашборд"; Hint = $(if ([bool]$script:Settings.compact_dashboard) { "Включён" } else { "Выключен" }); Value = "compact" },
            [pscustomobject]@{ Label = "Переустановить пульт"; Hint = "Проверить и обновить T-Mod Control.exe на рабочем столе"; Value = "launcher" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главный экран"; Value = "back" }
        )
        if (-not $settingsAction -or $settingsAction -eq "back") { return }
        if ($settingsAction -eq "theme") { Show-ThemeMenu; continue }
        if ($settingsAction -eq "animations") {
            $script:Settings.animations = -not [bool]$script:Settings.animations
            $script:AnimationDisabled = $NoAnimation -or -not [bool]$script:Settings.animations
            Save-ControlSettings
            continue
        }
        if ($settingsAction -eq "compact") {
            $script:Settings.compact_dashboard = -not [bool]$script:Settings.compact_dashboard
            Save-ControlSettings
            continue
        }
        if ($settingsAction -eq "launcher") {
            & cmd.exe /d /c ('call "{0}"' -f (Join-Path $script:ProjectDir "install_desktop_launcher_windows.bat")) | Out-Host
            Wait-ForKey
        }
    }
}

function Show-ControlHelp {
    Clear-Host
    Write-Brand -Section "CONTROL MANUAL"
    Write-Host "  БЫСТРЫЕ КЛАВИШИ" -ForegroundColor White
    Write-TModMetric -Label "R" -Value "обновить главный экран" -Kind "info" -Theme $script:Theme
    Write-TModMetric -Label "U" -Value "центр обновлений" -Kind "info" -Theme $script:Theme
    Write-TModMetric -Label "D" -Value "диагностика" -Kind "info" -Theme $script:Theme
    Write-TModMetric -Label "F1" -Value "эта справка" -Kind "info" -Theme $script:Theme
    Write-TModMetric -Label "Q" -Value "выход" -Kind "info" -Theme $script:Theme
    Write-Host ""
    Write-Host "  ПРИНЦИПЫ БЕЗОПАСНОСТИ" -ForegroundColor White
    Write-Host "  · Update не переключает main до прохождения тестов и резервного копирования." -ForegroundColor Gray
    Write-Host "  · Ошибка healthcheck возвращает предыдущие код и Docker-образы." -ForegroundColor Gray
    Write-Host "  · Очистка Docker никогда не удаляет volumes или рабочие контейнеры." -ForegroundColor Gray
    Write-Host "  · Remote принимает только заранее разрешённые действия и сервисы." -ForegroundColor Gray
    Wait-ForKey
}

function Show-MainMenu {
    Show-Intro
    while ($true) {
        $dashboard = Get-DashboardData
        $renderDashboard = { Write-MainDashboard $dashboard }
        $selection = Select-ControlItem -Title "Центр управления" -Items @(
            [pscustomobject]@{ Label = "Обзор системы"; Hint = "Полная карта версии и здоровья сервисов"; Value = "status" },
            [pscustomobject]@{ Label = "Безопасно обновить"; Hint = "Проверенный релиз с backup и rollback"; Value = "update" },
            [pscustomobject]@{ Label = "Питание системы"; Hint = "Запуск, полный restart и остановка"; Value = "power" },
            [pscustomobject]@{ Label = "Сервисы"; Hint = "Каждый контейнер, обновления и живые логи"; Value = "services" },
            [pscustomobject]@{ Label = "Контуры"; Hint = "Ядро T-Mod, Atlas и Minecraft"; Value = "groups" },
            [pscustomobject]@{ Label = "Центр обновлений"; Hint = "История, Git и автоматизация"; Value = "updates" },
            [pscustomobject]@{ Label = "Наблюдение"; Hint = "Диагностика, инциденты и ресурсы"; Value = "observability" },
            [pscustomobject]@{ Label = "Защита данных"; Hint = "Backup, состояние и полная проверка"; Value = "data" },
            [pscustomobject]@{ Label = "Сеть"; Hint = "Домены, Caddy и публичные сервисы"; Value = "network" },
            [pscustomobject]@{ Label = "Настройки"; Hint = "Темы, анимации и плотность интерфейса"; Value = "settings" },
            [pscustomobject]@{ Label = "Справка"; Hint = "Горячие клавиши и принципы безопасности"; Value = "help" },
            [pscustomobject]@{ Label = "Выход"; Hint = "Закрыть T-Mod Control"; Value = "exit" }
        ) -OnRender $renderDashboard -Hotkeys @{ R = "refresh"; U = "updates"; D = "diagnostics"; F1 = "help"; Q = "exit" } -Footer "R обновить  ·  U обновления  ·  D диагностика  ·  F1 помощь  ·  Q выход"
        if (-not $selection -or $selection -eq "exit") { return }
        if ($selection -eq "refresh") { continue }
        if ($selection -eq "power") { Show-PowerMenu; continue }
        if ($selection -eq "services") { Show-ServiceMenu; continue }
        if ($selection -eq "groups") { Show-GroupMenu; continue }
        if ($selection -eq "updates") { Show-UpdateMenu; continue }
        if ($selection -eq "observability") { Show-ObservabilityMenu; continue }
        if ($selection -eq "data") { Show-DataMenu; continue }
        if ($selection -eq "network") { Show-NetworkMenu; continue }
        if ($selection -eq "settings") { Show-SettingsMenu; continue }
        if ($selection -eq "help") { Show-ControlHelp; continue }
        Invoke-InteractiveAction $selection $(if ($selection -eq "diagnostics") { "DIAGNOSTICS" } elseif ($selection -eq "update") { "SAFE UPDATE" } else { "OPERATION" })
    }
}

try {
    if ($script:AllowedActions -notcontains $Action) { throw "Недопустимое действие: $Action" }
    if ($Action -eq "menu") {
        if ([Console]::IsInputRedirected) { throw "Интерактивное меню требует обычное окно консоли" }
        Show-MainMenu
        exit 0
    }
    $resultCode = Invoke-ControlAction $Action $Service $Group
    exit $resultCode
}
catch {
    Write-ControlLog ("fatal: {0}" -f $_.Exception.Message)
    if ($Json) {
        [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
    }
    else { Write-Host ("[T-MOD CONTROL] {0}" -f $_.Exception.Message) -ForegroundColor Red }
    exit 1
}
finally {
    try { [Console]::CursorVisible = $script:OriginalCursorVisible } catch {}
}
