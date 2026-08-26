# TModRemoteClient
param(
    [string]$Action = "menu",
    [string]$Service = "",
    [string]$Group = "",
    [switch]$SkipUpdateCheck
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$script:ClientVersion = "1.0.0"
$script:AppDir = Join-Path $env:LOCALAPPDATA "TModRemote"
$script:ConfigPath = Join-Path $script:AppDir "config.json"
$script:UpdateStatePath = Join-Path $script:AppDir "update-state.json"
$script:ManifestUrl = "https://raw.githubusercontent.com/cdnserver/t-mod/main/tmod_remote_version.json"
$script:Services = @(
    "tmod-postgres", "tmod-db-migrate", "tmod-discord-bot", "tmod-web", "tmod-worker",
    "atlas-qdrant", "atlas-forum-browser", "tmod-caddy", "minecraft", "minecraft-supervisor"
)
$script:Groups = @("core", "atlas", "minecraft")
$script:RemoteActions = @(
    "status", "update", "start", "stop", "restart",
    "service-start", "service-stop", "service-restart", "service-update",
    "service-logs", "service-logs-follow",
    "group-start", "group-stop", "group-restart",
    "diagnostics", "backup", "db-status", "db-check", "caddy-reload", "auto-update", "version"
)

function Write-RemoteBrand {
    param([string]$Section = "REMOTE CONTROL")
    Clear-Host
    Write-Host ""
    Write-Host "  ████████╗      ███╗   ███╗ ██████╗ ██████╗ " -ForegroundColor Cyan
    Write-Host "  ╚══██╔══╝      ████╗ ████║██╔═══██╗██╔══██╗" -ForegroundColor Cyan
    Write-Host "     ██║   █████╗██╔████╔██║██║   ██║██║  ██║" -ForegroundColor White
    Write-Host "     ██║   ╚════╝██║╚██╔╝██║██║   ██║██║  ██║" -ForegroundColor White
    Write-Host "     ██║         ██║ ╚═╝ ██║╚██████╔╝██████╔╝" -ForegroundColor DarkCyan
    Write-Host "     ╚═╝         ╚═╝     ╚═╝ ╚═════╝ ╚═════╝ " -ForegroundColor DarkCyan
    Write-Host ""
    Write-Host ("  {0}  /  v{1}" -f $Section, $script:ClientVersion) -ForegroundColor DarkGray
    Write-Host ("─" * 92) -ForegroundColor DarkGray
}

function Select-RemoteItem {
    param([string]$Title, [object[]]$Items, [string]$Subtitle = "↑ ↓ выбрать  ·  Enter открыть  ·  Esc назад")
    $index = 0
    try { [Console]::CursorVisible = $false } catch {}
    while ($true) {
        Write-RemoteBrand
        Write-Host ("  {0}" -f $Title) -ForegroundColor White
        Write-Host ("  {0}" -f $Subtitle) -ForegroundColor DarkGray
        Write-Host ""
        for ($itemIndex = 0; $itemIndex -lt $Items.Count; $itemIndex++) {
            $item = $Items[$itemIndex]
            if ($itemIndex -eq $index) {
                Write-Host "  › " -NoNewline -ForegroundColor Cyan
                Write-Host ([string]$item.Label) -NoNewline -ForegroundColor Black -BackgroundColor Cyan
                Write-Host ("  {0}" -f [string]$item.Hint) -ForegroundColor DarkCyan
            }
            else {
                Write-Host ("    {0}" -f [string]$item.Label) -ForegroundColor Gray
                if ($item.Hint) { Write-Host ("      {0}" -f [string]$item.Hint) -ForegroundColor DarkGray }
            }
        }
        $key = [Console]::ReadKey($true)
        switch ($key.Key) {
            "UpArrow" { $index = if ($index -eq 0) { $Items.Count - 1 } else { $index - 1 } }
            "DownArrow" { $index = if ($index -eq $Items.Count - 1) { 0 } else { $index + 1 } }
            "Home" { $index = 0 }
            "End" { $index = $Items.Count - 1 }
            "Enter" { return $Items[$index].Value }
            "Escape" { return $null }
        }
    }
}

function Wait-RemoteKey {
    Write-Host ""
    Write-Host "  Нажмите любую клавишу, чтобы вернуться" -ForegroundColor DarkGray
    [Console]::ReadKey($true) | Out-Null
}

function Confirm-RemoteAction {
    param([string]$Title, [string]$Description)
    $answer = Select-RemoteItem $Title @(
        [pscustomobject]@{ Label = "Продолжить"; Hint = "Подтверждаю удалённое действие"; Value = "yes" },
        [pscustomobject]@{ Label = "Отмена"; Hint = "Ничего не менять"; Value = "no" }
    ) $Description
    return $answer -eq "yes"
}

function Escape-PowerShellLiteral {
    param([string]$Value)
    return $Value.Replace("'", "''")
}

function Test-RemoteConfig {
    param($Config)
    if (-not $Config) { return $false }
    if ([string]$Config.host -notmatch "^[a-zA-Z0-9.-]+$") { return $false }
    if ([int]$Config.port -lt 1 -or [int]$Config.port -gt 65535) { return $false }
    if ([string]$Config.user -notmatch "^[a-zA-Z0-9_.-]+$") { return $false }
    if (-not [string]$Config.project_dir) { return $false }
    return $true
}

function Get-RemoteConfig {
    if (-not (Test-Path -LiteralPath $script:ConfigPath)) { return $null }
    try {
        $config = Get-Content -Raw -LiteralPath $script:ConfigPath | ConvertFrom-Json
        if (Test-RemoteConfig $config) { return $config }
    }
    catch {}
    return $null
}

function Save-RemoteConfig {
    param($Config)
    New-Item -ItemType Directory -Path $script:AppDir -Force | Out-Null
    $temporary = "$($script:ConfigPath).tmp"
    $encoding = New-Object System.Text.UTF8Encoding
    [IO.File]::WriteAllText($temporary, ($Config | ConvertTo-Json -Depth 5), $encoding)
    Move-Item -LiteralPath $temporary -Destination $script:ConfigPath -Force
}

function Read-Setting {
    param([string]$Prompt, [string]$Default)
    Write-Host ("  {0}" -f $Prompt) -ForegroundColor Gray
    Write-Host ("  По умолчанию: {0}" -f $Default) -ForegroundColor DarkGray
    $value = Read-Host "  >"
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function New-RemoteConfiguration {
    if (-not (Get-Command "ssh.exe" -ErrorAction SilentlyContinue) -or -not (Get-Command "ssh-keygen.exe" -ErrorAction SilentlyContinue)) {
        throw "Нужен компонент Windows OpenSSH Client (ssh.exe и ssh-keygen.exe)"
    }
    Write-RemoteBrand -Section "FIRST CONNECTION"
    Write-Host "  T-Mod Remote работает через SSH внутри WireGuard." -ForegroundColor White
    Write-Host "  Пароль не сохраняется. После привязки используется отдельный ключ." -ForegroundColor DarkGray
    Write-Host ""
    $remoteAddress = Read-Setting "Адрес сервера WireGuard" "10.8.0.1"
    $remotePortText = Read-Setting "SSH-порт" "22"
    $remoteUser = Read-Setting "Пользователь Windows на сервере" "Admin"
    $projectDir = Read-Setting "Папка проекта на сервере" "C:\Users\Admin\Desktop\esgiel"
    $persistentDir = Read-Setting "Папка данных на сервере" "C:\Users\Admin\Documents\SGLDiscordBot"
    $keyDir = Join-Path $script:AppDir "keys"
    $keyPath = Join-Path $keyDir "id_ed25519"
    $config = [pscustomobject]@{
        host = $remoteAddress
        port = [int]$remotePortText
        user = $remoteUser
        project_dir = $projectDir
        persistent_dir = $persistentDir
        key_path = $keyPath
        created_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    if (-not (Test-RemoteConfig $config)) { throw "Параметры подключения имеют неверный формат" }
    New-Item -ItemType Directory -Path $keyDir -Force | Out-Null
    if (-not (Test-Path -LiteralPath $keyPath)) {
        $safeKeyPath = $keyPath.Replace('"', '\"')
        $keyProcess = Start-Process -FilePath "ssh-keygen.exe" -ArgumentList ('-q -t ed25519 -N "" -C "tmod-remote" -f "{0}"' -f $safeKeyPath) -NoNewWindow -Wait -PassThru
        if ($keyProcess.ExitCode -ne 0) { throw "Не удалось создать SSH-ключ" }
    }
    Save-RemoteConfig $config
    Write-Host ""
    Write-Host "  Конфигурация сохранена." -ForegroundColor Green
    Write-Host "  Следующий шаг установит публичный ключ на сервер." -ForegroundColor Yellow
    Write-Host "  Windows может один раз запросить пароль пользователя Admin." -ForegroundColor DarkGray
    Wait-RemoteKey
    Install-RemotePublicKey $config
    return $config
}

function Get-SshBaseArguments {
    param($Config, [switch]$AllowPassword)
    $arguments = @(
        "-p", [string]$Config.port,
        "-o", "ConnectTimeout=8",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new"
    )
    if (Test-Path -LiteralPath ([string]$Config.key_path)) { $arguments += @("-i", [string]$Config.key_path) }
    if (-not $AllowPassword) { $arguments += @("-o", "BatchMode=yes") }
    return $arguments
}

function Invoke-RemotePowerShell {
    param($Config, [string]$Command, [switch]$AllowPassword)
    $bytes = [Text.Encoding]::Unicode.GetBytes($Command)
    $encoded = [Convert]::ToBase64String($bytes)
    $arguments = Get-SshBaseArguments $Config -AllowPassword:$AllowPassword
    $arguments += ("{0}@{1}" -f $Config.user, $Config.host)
    $arguments += @("powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded)
    & ssh.exe @arguments | Out-Host
    $code = $LASTEXITCODE
    return $code
}

function Install-RemotePublicKey {
    param($Config)
    $publicKeyPath = "$($Config.key_path).pub"
    if (-not (Test-Path -LiteralPath $publicKeyPath)) { throw "Публичный ключ не найден" }
    $publicKey = (Get-Content -Raw -LiteralPath $publicKeyPath).Trim()
    $safeKey = Escape-PowerShellLiteral $publicKey
    $command = @"
`$ErrorActionPreference = 'Stop'
`$key = '$safeKey'
`$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
`$principal = New-Object Security.Principal.WindowsPrincipal(`$identity)
`$isAdministrator = `$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
`$target = if (`$isAdministrator) { Join-Path `$env:ProgramData 'ssh\administrators_authorized_keys' } else { Join-Path `$env:USERPROFILE '.ssh\authorized_keys' }
New-Item -ItemType Directory -Path (Split-Path -Parent `$target) -Force | Out-Null
if (-not (Test-Path -LiteralPath `$target)) { New-Item -ItemType File -Path `$target -Force | Out-Null }
`$existing = Get-Content -LiteralPath `$target -ErrorAction SilentlyContinue
if (`$existing -notcontains `$key) { Add-Content -LiteralPath `$target -Encoding ASCII -Value `$key }
if (`$isAdministrator) { & icacls.exe `$target /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null }
Write-Output 'TMOD_KEY_INSTALLED'
"@
    Write-RemoteBrand -Section "KEY PAIRING"
    Write-Host "  Подключаюсь к $($Config.user)@$($Config.host)..." -ForegroundColor Cyan
    $code = Invoke-RemotePowerShell $Config $command -AllowPassword
    if ($code -ne 0) { throw "Сервер не принял SSH-ключ" }
    Write-Host "  Ключ установлен. Пароль больше не нужен." -ForegroundColor Green
    Wait-RemoteKey
}

function Get-ServerCommand {
    param($Config, [string]$RemoteAction, [string]$RemoteService = "", [string]$RemoteGroup = "", [switch]$AsJson)
    if ($script:RemoteActions -notcontains $RemoteAction) { throw "Недопустимое удалённое действие" }
    if ($RemoteService -and $script:Services -notcontains $RemoteService) { throw "Неизвестный сервис" }
    if ($RemoteGroup -and $script:Groups -notcontains $RemoteGroup) { throw "Неизвестный контур" }
    $project = Escape-PowerShellLiteral ([string]$Config.project_dir)
    $persistent = Escape-PowerShellLiteral ([string]$Config.persistent_dir)
    $actionLiteral = Escape-PowerShellLiteral $RemoteAction
    $serviceLiteral = Escape-PowerShellLiteral $RemoteService
    $groupLiteral = Escape-PowerShellLiteral $RemoteGroup
    $jsonSwitch = if ($AsJson) { " -Json" } else { "" }
    return @"
`$ErrorActionPreference = 'Stop'
`$OutputEncoding = New-Object System.Text.UTF8Encoding
[Console]::OutputEncoding = `$OutputEncoding
`$control = Join-Path '$project' 'tmod_control_windows.ps1'
if (-not (Test-Path -LiteralPath `$control)) { throw 'T-Mod Control отсутствует на сервере. Сначала обновите репозиторий.' }
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `$control -ProjectDir '$project' -PersistentDir '$persistent' -Action '$actionLiteral' -Service '$serviceLiteral' -Group '$groupLiteral' -NoAnimation$jsonSwitch
exit `$LASTEXITCODE
"@
}

function Invoke-ServerAction {
    param($Config, [string]$RemoteAction, [string]$RemoteService = "", [string]$RemoteGroup = "", [switch]$AsJson)
    $command = Get-ServerCommand $Config $RemoteAction $RemoteService $RemoteGroup -AsJson:$AsJson
    return (Invoke-RemotePowerShell $Config $command)
}

function Test-RemoteConnection {
    param($Config)
    if (-not (Get-Command "ssh.exe" -ErrorAction SilentlyContinue)) { return $false }
    $arguments = Get-SshBaseArguments $Config
    $arguments += ("{0}@{1}" -f $Config.user, $Config.host)
    $arguments += @("echo", "TMOD_REMOTE_OK")
    $output = & ssh.exe @arguments 2>$null
    return ($LASTEXITCODE -eq 0 -and ($output | Out-String) -match "TMOD_REMOTE_OK")
}

function Get-RemoteManifest {
    try { return (Invoke-RestMethod -Uri $script:ManifestUrl -TimeoutSec 6) }
    catch { return $null }
}

function Update-RemoteClient {
    param([switch]$Quiet)
    $manifest = Get-RemoteManifest
    if (-not $manifest) {
        if (-not $Quiet) { Write-Host "  GitHub сейчас недоступен; текущая версия сохранена." -ForegroundColor Yellow }
        return $false
    }
    if ([version]$manifest.version -le [version]$script:ClientVersion) {
        if (-not $Quiet) { Write-Host "  Установлена актуальная версия T-Mod Remote." -ForegroundColor Green }
        return $false
    }
    $runningInstalledCopy = $PSScriptRoot.TrimEnd("\") -ieq $script:AppDir.TrimEnd("\")
    if (-not $runningInstalledCopy) {
        if (-not $Quiet) { Write-Host "  Обновление найдено. Запустите установленный T-Mod Remote с рабочего стола." -ForegroundColor Yellow }
        return $false
    }
    foreach ($fileName in @("tmod_remote_windows.ps1", "tmod_remote_windows.bat")) {
        $url = [string]$manifest.files.$fileName
        if (-not $url.StartsWith("https://raw.githubusercontent.com/cdnserver/t-mod/")) { throw "Манифест содержит недопустимый адрес" }
        $temporary = Join-Path $script:AppDir ("{0}.new" -f $fileName)
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $temporary -TimeoutSec 15
        if ((Get-Item -LiteralPath $temporary).Length -lt 100) { throw "Загруженный файл повреждён: $fileName" }
        if ($fileName.EndsWith(".ps1") -and -not ((Get-Content -Raw -LiteralPath $temporary) -match "TModRemoteClient")) { throw "Проверка клиента не пройдена" }
        $expectedHash = [string]$manifest.sha256.$fileName
        $actualHash = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($expectedHash -notmatch "^[a-fA-F0-9]{64}$" -or $actualHash -ne $expectedHash.ToLowerInvariant()) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
            throw "Контрольная сумма обновления не совпала: $fileName"
        }
        Move-Item -LiteralPath $temporary -Destination (Join-Path $script:AppDir $fileName) -Force
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $script:AppDir "tmod_remote_version.json") -Encoding UTF8
    if (-not $Quiet) { Write-Host "  T-Mod Remote обновлён до v$($manifest.version). Изменения включатся при следующем запуске." -ForegroundColor Green }
    return $true
}

function Invoke-AutomaticUpdateCheck {
    if ($SkipUpdateCheck) { return }
    $check = $true
    if (Test-Path -LiteralPath $script:UpdateStatePath) {
        try {
            $state = Get-Content -Raw -LiteralPath $script:UpdateStatePath | ConvertFrom-Json
            $lastCheck = [datetime]::Parse([string]$state.checked_at).ToUniversalTime()
            $check = ((Get-Date).ToUniversalTime() - $lastCheck).TotalHours -ge 6
        }
        catch {}
    }
    if (-not $check) { return }
    try { Update-RemoteClient -Quiet | Out-Null } catch {}
    New-Item -ItemType Directory -Path $script:AppDir -Force | Out-Null
    @{ checked_at = (Get-Date).ToUniversalTime().ToString("o") } | ConvertTo-Json | Set-Content -LiteralPath $script:UpdateStatePath -Encoding UTF8
}

function Show-RemoteServices {
    param($Config)
    while ($true) {
        $items = @()
        foreach ($serviceName in $script:Services) { $items += [pscustomobject]@{ Label = $serviceName; Hint = "Удалённое управление"; Value = $serviceName } }
        $items += [pscustomobject]@{ Label = "Назад"; Hint = "Главное меню"; Value = "back" }
        $selectedService = Select-RemoteItem "Сервисы домашнего сервера" $items
        if (-not $selectedService -or $selectedService -eq "back") { return }
        $selectedAction = Select-RemoteItem $selectedService @(
            [pscustomobject]@{ Label = "Запустить"; Hint = "Создать при необходимости"; Value = "service-start" },
            [pscustomobject]@{ Label = "Перезапустить"; Hint = "Мягкий restart"; Value = "service-restart" },
            [pscustomobject]@{ Label = "Остановить"; Hint = "Данные сохраняются"; Value = "service-stop" },
            [pscustomobject]@{ Label = "Обновить сервис"; Hint = "Образ и контейнер"; Value = "service-update" },
            [pscustomobject]@{ Label = "Последние логи"; Hint = "160 строк"; Value = "service-logs" },
            [pscustomobject]@{ Label = "Живые логи"; Hint = "Ctrl+C завершает"; Value = "service-logs-follow" },
            [pscustomobject]@{ Label = "Назад"; Hint = "К сервисам"; Value = "back" }
        )
        if (-not $selectedAction -or $selectedAction -eq "back") { continue }
        Write-RemoteBrand -Section "REMOTE OPERATION"
        Invoke-ServerAction $Config $selectedAction $selectedService | Out-Null
        Wait-RemoteKey
    }
}

function Show-RemoteGroups {
    param($Config)
    $selectedGroup = Select-RemoteItem "Контуры домашнего сервера" @(
        [pscustomobject]@{ Label = "Ядро T-Mod"; Hint = "Discord, Web, Worker, PostgreSQL, Caddy"; Value = "core" },
        [pscustomobject]@{ Label = "Atlas"; Hint = "Qdrant и форумный браузер"; Value = "atlas" },
        [pscustomobject]@{ Label = "Minecraft"; Hint = "Сервер и supervisor"; Value = "minecraft" },
        [pscustomobject]@{ Label = "Назад"; Hint = "Главное меню"; Value = "back" }
    )
    if (-not $selectedGroup -or $selectedGroup -eq "back") { return }
    $selectedAction = Select-RemoteItem ("Контур: {0}" -f $selectedGroup) @(
        [pscustomobject]@{ Label = "Запустить"; Hint = "Только этот контур"; Value = "group-start" },
        [pscustomobject]@{ Label = "Перезапустить"; Hint = "Только этот контур"; Value = "group-restart" },
        [pscustomobject]@{ Label = "Остановить"; Hint = "Остальные продолжат работу"; Value = "group-stop" },
        [pscustomobject]@{ Label = "Назад"; Hint = "К контурам"; Value = "back" }
    )
    if (-not $selectedAction -or $selectedAction -eq "back") { return }
    Write-RemoteBrand -Section "REMOTE OPERATION"
    Invoke-ServerAction $Config $selectedAction "" $selectedGroup | Out-Null
    Wait-RemoteKey
}

function Show-RemoteData {
    param($Config)
    while ($true) {
        $selectedAction = Select-RemoteItem "Защита данных домашнего сервера" @(
            [pscustomobject]@{ Label = "Создать резервную копию"; Hint = "Согласованный backup PostgreSQL"; Value = "backup" },
            [pscustomobject]@{ Label = "Состояние резервов"; Hint = "Последние копии и свободное место"; Value = "db-status" },
            [pscustomobject]@{ Label = "Полная проверка базы"; Hint = "Может занять несколько минут"; Value = "db-check" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главное меню"; Value = "back" }
        )
        if (-not $selectedAction -or $selectedAction -eq "back") { return }
        Write-RemoteBrand -Section "REMOTE DATA GUARD"
        Invoke-ServerAction $Config $selectedAction | Out-Null
        Wait-RemoteKey
    }
}

function Show-RemoteMenu {
    $config = Get-RemoteConfig
    if (-not $config) { $config = New-RemoteConfiguration }
    Invoke-AutomaticUpdateCheck
    while ($true) {
        $online = Test-RemoteConnection $config
        $connectionHint = if ($online) { "$($config.host) · WireGuard/SSH online" } else { "$($config.host) · нет соединения" }
        $selection = Select-RemoteItem "Удалённый центр управления" @(
            [pscustomobject]@{ Label = "Обзор сервера"; Hint = $connectionHint; Value = "status" },
            [pscustomobject]@{ Label = "Безопасно обновить и запустить"; Hint = "GitHub, тесты, backup и rollback"; Value = "update" },
            [pscustomobject]@{ Label = "Запустить установленную версию"; Hint = "Полный серверный запуск"; Value = "start" },
            [pscustomobject]@{ Label = "Сервисы"; Hint = "Контейнеры и логи"; Value = "services" },
            [pscustomobject]@{ Label = "Контуры"; Hint = "Ядро, Atlas, Minecraft"; Value = "groups" },
            [pscustomobject]@{ Label = "Диагностика"; Hint = "API, Docker, диск и ошибки"; Value = "diagnostics" },
            [pscustomobject]@{ Label = "Защита данных"; Hint = "Backup, состояние и полная проверка"; Value = "data" },
            [pscustomobject]@{ Label = "Проверить и применить Caddy"; Hint = "Валидация перед reload"; Value = "caddy-reload" },
            [pscustomobject]@{ Label = "Открыть сервисы в браузере"; Hint = "T-Mod, Reactor, Consensus, Atlas, SGL"; Value = "open-sites" },
            [pscustomobject]@{ Label = "Включить автообновление сервера"; Hint = "Безопасная проверка origin/main"; Value = "auto-update" },
            [pscustomobject]@{ Label = "Перезапустить всю систему"; Hint = "Все существующие контейнеры"; Value = "restart" },
            [pscustomobject]@{ Label = "Остановить всю систему"; Hint = "Контейнеры остановятся, данные сохранятся"; Value = "stop" },
            [pscustomobject]@{ Label = "Обновить T-Mod Remote"; Hint = "Проверить стабильный канал клиента"; Value = "self-update" },
            [pscustomobject]@{ Label = "Настроить подключение"; Hint = "WireGuard, SSH, пути и новый ключ"; Value = "configure" },
            [pscustomobject]@{ Label = "Выход"; Hint = "Закрыть T-Mod Remote"; Value = "exit" }
        )
        if (-not $selection -or $selection -eq "exit") { return }
        if ($selection -eq "services") { Show-RemoteServices $config; continue }
        if ($selection -eq "groups") { Show-RemoteGroups $config; continue }
        if ($selection -eq "data") { Show-RemoteData $config; continue }
        if ($selection -eq "configure") { $config = New-RemoteConfiguration; continue }
        if ($selection -eq "open-sites") {
            foreach ($url in @("https://tvr.lat", "https://reactor.tvr.lat", "https://consensus.tvr.lat", "https://atlas.tvr.lat", "https://sgl.tvr.lat")) { Start-Process $url }
            continue
        }
        if ($selection -eq "stop" -and -not (Confirm-RemoteAction "Остановить домашний сервер T-Mod?" "Все контейнеры проекта будут остановлены; данные сохранятся.")) { continue }
        Write-RemoteBrand -Section "REMOTE OPERATION"
        if ($selection -eq "self-update") {
            try { Update-RemoteClient | Out-Null } catch { Write-Host ("  Ошибка обновления: {0}" -f $_.Exception.Message) -ForegroundColor Red }
            Wait-RemoteKey
            continue
        }
        if (-not $online) {
            Write-Host "  Сервер недоступен. Проверьте WireGuard и SSH." -ForegroundColor Red
            Write-Host ("  Адрес: {0}:{1}" -f $config.host, $config.port) -ForegroundColor DarkGray
            Wait-RemoteKey
            continue
        }
        try { Invoke-ServerAction $config $selection | Out-Null }
        catch { Write-Host ("  Ошибка: {0}" -f $_.Exception.Message) -ForegroundColor Red }
        Wait-RemoteKey
    }
}

try {
    New-Item -ItemType Directory -Path $script:AppDir -Force | Out-Null
    if ($Action -eq "menu") {
        if ([Console]::IsInputRedirected) { throw "Интерактивное меню требует окно консоли" }
        Show-RemoteMenu
        exit 0
    }
    $config = Get-RemoteConfig
    if (-not $config) { throw "T-Mod Remote ещё не настроен" }
    if ($Action -eq "self-update") { Update-RemoteClient | Out-Null; exit 0 }
    if ($script:RemoteActions -notcontains $Action) { throw "Недопустимое действие" }
    exit (Invoke-ServerAction $config $Action $Service $Group)
}
catch {
    Write-Host ("[T-MOD REMOTE] {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
finally { try { [Console]::CursorVisible = $true } catch {} }
