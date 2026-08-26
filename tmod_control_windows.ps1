param(
    [string]$ProjectDir = $PSScriptRoot,
    [string]$PersistentDir = "$env:USERPROFILE\Documents\SGLDiscordBot",
    [string]$Action = "menu",
    [string]$Service = "",
    [string]$Group = "",
    [switch]$NoAnimation,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$script:ControlVersion = "1.0.0"
$script:ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
$script:PersistentDir = $PersistentDir
$script:LogDir = Join-Path $PersistentDir "logs"
$script:LogPath = Join-Path $script:LogDir "tmod-control.log"
$script:OriginalCursorVisible = $true

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
    "caddy-reload", "open-sites", "auto-update", "version"
)

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
    try { return [Math]::Max(76, [Math]::Min(118, [Console]::WindowWidth - 1)) }
    catch { return 92 }
}

function Write-Rule {
    param([ConsoleColor]$Color = [ConsoleColor]::DarkGray)
    Write-Host (([char]0x2500).ToString() * (Get-ConsoleWidth)) -ForegroundColor $Color
}

function Write-Brand {
    param([string]$Section = "CONTROL PLANE")
    Write-Host ""
    Write-Host "  ████████╗      ███╗   ███╗ ██████╗ ██████╗ " -ForegroundColor Cyan
    Write-Host "  ╚══██╔══╝      ████╗ ████║██╔═══██╗██╔══██╗" -ForegroundColor Cyan
    Write-Host "     ██║   █████╗██╔████╔██║██║   ██║██║  ██║" -ForegroundColor White
    Write-Host "     ██║   ╚════╝██║╚██╔╝██║██║   ██║██║  ██║" -ForegroundColor White
    Write-Host "     ██║         ██║ ╚═╝ ██║╚██████╔╝██████╔╝" -ForegroundColor DarkCyan
    Write-Host "     ╚═╝         ╚═╝     ╚═╝ ╚═════╝ ╚═════╝ " -ForegroundColor DarkCyan
    Write-Host ""
    Write-Host ("  {0}  /  v{1}" -f $Section, $script:ControlVersion) -ForegroundColor DarkGray
    Write-Rule
}

function Show-Intro {
    if ($NoAnimation -or $env:TMOD_NO_ANIMATION -eq "1" -or [Console]::IsOutputRedirected) { return }
    try { $script:OriginalCursorVisible = [Console]::CursorVisible; [Console]::CursorVisible = $false } catch {}
    $frames = @(
        "                         ·        ",
        "                    ·    ╱         ",
        "                ·       ╱          ",
        "             ✦─────────╱           ",
        "         T — M O D   ONLINE         "
    )
    foreach ($frame in $frames) {
        Clear-Host
        Write-Host ""
        Write-Host ""
        Write-Host $frame -ForegroundColor Cyan
        Start-Sleep -Milliseconds 105
    }
    Start-Sleep -Milliseconds 120
}

function Wait-ForKey {
    param([string]$Message = "Нажмите любую клавишу, чтобы вернуться")
    if ([Console]::IsInputRedirected) { return }
    Write-Host ""
    Write-Host ("  {0}" -f $Message) -ForegroundColor DarkGray
    [Console]::ReadKey($true) | Out-Null
}

function Select-ControlItem {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][object[]]$Items,
        [int]$InitialIndex = 0,
        [string]$Subtitle = "↑ ↓ выбрать  ·  Enter открыть  ·  Esc назад"
    )
    if ($Items.Count -eq 0) { return $null }
    $index = [Math]::Max(0, [Math]::Min($InitialIndex, $Items.Count - 1))
    try { [Console]::CursorVisible = $false } catch {}
    while ($true) {
        Clear-Host
        Write-Brand
        Write-Host ("  {0}" -f $Title) -ForegroundColor White
        Write-Host ("  {0}" -f $Subtitle) -ForegroundColor DarkGray
        Write-Host ""
        for ($itemIndex = 0; $itemIndex -lt $Items.Count; $itemIndex++) {
            $item = $Items[$itemIndex]
            $label = [string]$item.Label
            $hint = [string]$item.Hint
            if ($itemIndex -eq $index) {
                Write-Host "  › " -NoNewline -ForegroundColor Cyan
                Write-Host $label -NoNewline -ForegroundColor Black -BackgroundColor Cyan
                if ($hint) { Write-Host ("  {0}" -f $hint) -ForegroundColor DarkCyan }
                else { Write-Host "" }
            }
            else {
                Write-Host ("    {0}" -f $label) -ForegroundColor Gray
                if ($hint) { Write-Host ("      {0}" -f $hint) -ForegroundColor DarkGray }
            }
        }
        $key = [Console]::ReadKey($true)
        switch ($key.Key) {
            "UpArrow" { $index = if ($index -le 0) { $Items.Count - 1 } else { $index - 1 } }
            "DownArrow" { $index = if ($index -ge $Items.Count - 1) { 0 } else { $index + 1 } }
            "Home" { $index = 0 }
            "End" { $index = $Items.Count - 1 }
            "Enter" { return $Items[$index].Value }
            "Escape" { return $null }
        }
    }
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
    if (-not (Get-Command "git.exe" -ErrorAction SilentlyContinue) -and -not (Get-Command "git" -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ Commit = "unknown"; Branch = "git unavailable" }
    }
    $commit = (& git -C $script:ProjectDir rev-parse --short=12 HEAD 2>$null | Select-Object -First 1)
    if (-not $commit) { $commit = "unknown" }
    $branch = (& git -C $script:ProjectDir branch --show-current 2>$null | Select-Object -First 1)
    if (-not $branch) { $branch = "detached" }
    return [pscustomobject]@{ Commit = $commit.Trim(); Branch = $branch.Trim() }
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

function Invoke-ControlAction {
    param([string]$RequestedAction, [string]$RequestedService = "", [string]$RequestedGroup = "")
    if ($script:AllowedActions -notcontains $RequestedAction) { throw "Недопустимое действие: $RequestedAction" }
    if ($RequestedService -and $script:Services -notcontains $RequestedService) { throw "Неизвестный сервис: $RequestedService" }
    if ($RequestedGroup -and -not $script:Groups.ContainsKey($RequestedGroup)) { throw "Неизвестная группа: $RequestedGroup" }
    Write-ControlLog ("action={0} service={1} group={2}" -f $RequestedAction, $RequestedService, $RequestedGroup)
    $dockerActions = @(
        "stop", "restart", "service-start", "service-stop", "service-restart", "service-update",
        "service-logs", "service-logs-follow", "group-start", "group-stop", "group-restart", "caddy-reload"
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

function Show-MainMenu {
    Show-Intro
    while ($true) {
        $selection = Select-ControlItem -Title "Центр управления" -Items @(
            [pscustomobject]@{ Label = "Обзор системы"; Hint = "Версия, Docker и здоровье каждого сервиса"; Value = "status" },
            [pscustomobject]@{ Label = "Безопасно обновить и запустить"; Hint = "GitHub → тесты → backup → запуск → rollback при ошибке"; Value = "update" },
            [pscustomobject]@{ Label = "Запустить установленную версию"; Hint = "Полная подготовка и запуск Docker-системы"; Value = "start" },
            [pscustomobject]@{ Label = "Сервисы"; Hint = "Управление отдельными контейнерами и логами"; Value = "services" },
            [pscustomobject]@{ Label = "Контуры"; Hint = "Ядро T-Mod, Atlas или Minecraft"; Value = "groups" },
            [pscustomobject]@{ Label = "Диагностика"; Hint = "Сеть, API, диск и свежие ошибки"; Value = "diagnostics" },
            [pscustomobject]@{ Label = "Защита данных"; Hint = "Резервные копии и проверка базы"; Value = "data" },
            [pscustomobject]@{ Label = "Проверить и применить Caddy"; Hint = "Валидация конфигурации перед reload"; Value = "caddy-reload" },
            [pscustomobject]@{ Label = "Открыть сервисы в браузере"; Hint = "T-Mod, Reactor, Consensus, Atlas, SGL"; Value = "open-sites" },
            [pscustomobject]@{ Label = "Включить автообновление"; Hint = "Безопасная проверка origin/main каждые 2 минуты"; Value = "auto-update" },
            [pscustomobject]@{ Label = "Перезапустить всю систему"; Hint = "Перезапуск существующих контейнеров"; Value = "restart" },
            [pscustomobject]@{ Label = "Остановить всю систему"; Hint = "Контейнеры остановятся, данные сохранятся"; Value = "stop" },
            [pscustomobject]@{ Label = "Выход"; Hint = "Закрыть T-Mod Control"; Value = "exit" }
        )
        if (-not $selection -or $selection -eq "exit") { return }
        if ($selection -eq "services") { Show-ServiceMenu; continue }
        if ($selection -eq "groups") { Show-GroupMenu; continue }
        if ($selection -eq "data") { Show-DataMenu; continue }
        if ($selection -eq "stop" -and -not (Confirm-ControlAction "Остановить T-Mod?" "Все контейнеры проекта будут остановлены; данные останутся на месте.")) { continue }
        Clear-Host; Write-Brand -Section "OPERATION"
        try {
            $exitCode = Invoke-ControlAction $selection
            if ($exitCode -eq 0 -and $selection -notin @("status", "diagnostics")) { Write-Host "  Операция завершена." -ForegroundColor Green }
            elseif ($exitCode -ne 0) { Write-Host ("  Операция завершилась с кодом {0}." -f $exitCode) -ForegroundColor Red }
        }
        catch {
            Write-ControlLog ("error action={0}: {1}" -f $selection, $_.Exception.Message)
            Write-Host ("  Ошибка: {0}" -f $_.Exception.Message) -ForegroundColor Red
        }
        Wait-ForKey
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
