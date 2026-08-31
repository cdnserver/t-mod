# TModRemoteClient
param(
    [string]$Action = "menu",
    [string]$Service = "",
    [string]$Group = "",
    [switch]$SkipUpdateCheck
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$script:ClientVersion = "1.1.3"
$script:AppDir = Join-Path $env:LOCALAPPDATA "TModRemote"
$script:ConfigPath = Join-Path $script:AppDir "config.json"
$script:UpdateStatePath = Join-Path $script:AppDir "update-state.json"
$script:ManifestUrl = "https://raw.githubusercontent.com/cdnserver/t-mod/main/tmod_remote_version.json"
$script:LastUpdateCheckSucceeded = $false
$uiModule = Join-Path $PSScriptRoot "tmod_console_ui.psm1"
if (-not (Test-Path -LiteralPath $uiModule)) {
    $uiBootstrapUrl = "https://raw.githubusercontent.com/cdnserver/t-mod/main/tmod_console_ui.psm1"
    $uiBootstrapHash = "e64a8287eeec9faf74a764380c30c965e78d3003f5a9436c569b52a58f252316"
    $uiTemporary = "$uiModule.new"
    Invoke-WebRequest -UseBasicParsing -Uri $uiBootstrapUrl -OutFile $uiTemporary -TimeoutSec 15
    $uiActualHash = (Get-FileHash -LiteralPath $uiTemporary -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($uiActualHash -ne $uiBootstrapHash) { Remove-Item -LiteralPath $uiTemporary -Force -ErrorAction SilentlyContinue; throw "Модуль интерфейса не прошёл проверку SHA-256" }
    Move-Item -LiteralPath $uiTemporary -Destination $uiModule -Force
}
Import-Module $uiModule -Force
$script:Theme = Get-TModTheme "aurora"
Initialize-TModConsole -Title "T-Mod Remote Control"
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
    "diagnostics", "backup", "db-status", "db-check", "caddy-reload", "auto-update",
    "auto-update-status", "auto-update-disable", "update-status", "git-status", "domain-check",
    "resources", "error-log", "export-diagnostics", "docker-clean", "version"
)

function Write-RemoteBrand {
    param([string]$Section = "REMOTE CONTROL")
    Clear-Host
    Write-TModHeader -Section $Section -Version $script:ClientVersion -Context "WIREGUARD / SSH" -Theme $script:Theme
}

function Select-RemoteItem {
    param(
        [string]$Title,
        [object[]]$Items,
        [string]$Subtitle = "↑ ↓ выбрать  ·  Enter открыть  ·  Esc назад",
        [scriptblock]$OnRender,
        [hashtable]$Hotkeys = @{},
        [string]$Footer = ""
    )
    $header = { Write-RemoteBrand }
    return (Select-TModMenu -Title $Title -Items $Items -Subtitle $Subtitle -Header $header -OnRender $OnRender -Hotkeys $Hotkeys -Theme $script:Theme -Footer $Footer)
}

function Wait-RemoteKey {
    Wait-TModKey -Theme $script:Theme
}

function Confirm-RemoteAction {
    param([string]$Title, [string]$Description)
    $answer = Select-RemoteItem $Title @(
        [pscustomobject]@{ Label = "Продолжить"; Hint = "Подтверждаю удалённое действие"; Value = "yes" },
        [pscustomobject]@{ Label = "Отмена"; Hint = "Ничего не менять"; Value = "no" }
    ) -Subtitle $Description
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
        if (Test-RemoteConfig $config) {
            if (-not ($config.PSObject.Properties.Name -contains "theme")) { $config | Add-Member -NotePropertyName theme -NotePropertyValue "aurora" }
            if (-not ($config.PSObject.Properties.Name -contains "animations")) { $config | Add-Member -NotePropertyName animations -NotePropertyValue $true }
            return $config
        }
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
        theme = "aurora"
        animations = $true
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
    param(
        $Config,
        [string]$Command,
        [switch]$AllowPassword,
        [switch]$Follow,
        [ValidateRange(30, 2400)][int]$TimeoutSeconds = 300
    )
    $bytes = [Text.Encoding]::Unicode.GetBytes($Command)
    $encoded = [Convert]::ToBase64String($bytes)
    $arguments = Get-SshBaseArguments $Config -AllowPassword:$AllowPassword
    $arguments += ("{0}@{1}" -f $Config.user, $Config.host)
    $arguments += @("powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded)
    # A follow-mode log stream is intentionally unbounded and is stopped by
    # Ctrl+C. Every other SSH command gets a hard wall-clock deadline, so a
    # half-open WireGuard tunnel cannot freeze the control console forever.
    if ($Follow) {
        & ssh.exe @arguments | Out-Host
        return $LASTEXITCODE
    }
    if ($AllowPassword) {
        # The first key-pairing command must keep the interactive password
        # prompt. Start-Process still gives it a bounded wait.
        $argumentLine = ($arguments | ForEach-Object {
            $item = [string]$_
            if ($item -match '[\s"]') { '"' + $item.Replace('"', '\\"') + '"' } else { $item }
        }) -join ' '
        $process = Start-Process -FilePath "ssh.exe" -ArgumentList $argumentLine -NoNewWindow -PassThru
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            throw "SSH-команда превысила лимит ${TimeoutSeconds}с"
        }
        return $process.ExitCode
    }
    $job = Start-Job -ScriptBlock {
        param([string[]]$SshArguments)
        $captured = (& ssh.exe @SshArguments 2>&1 | Out-String)
        [pscustomobject]@{ ExitCode = [int]$LASTEXITCODE; Output = $captured }
    } -ArgumentList (,[string[]]$arguments)
    try {
        if (-not (Wait-Job -Job $job -Timeout $TimeoutSeconds)) {
            Stop-Job -Job $job -Force -ErrorAction SilentlyContinue
            throw "SSH-команда превысила лимит ${TimeoutSeconds}с"
        }
        $result = Receive-Job -Job $job | Select-Object -Last 1
        if ($result -and $result.PSObject.Properties.Name -contains "Output") {
            [string]$result.Output | Out-Host
            return [int]$result.ExitCode
        }
        return 1
    }
    finally {
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    }
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

function Get-RemoteActionTimeoutSeconds {
    param([string]$RemoteAction)
    switch ($RemoteAction) {
        "update" { return 1800 }
        "service-update" { return 1800 }
        "start" { return 900 }
        "restart" { return 900 }
        "group-start" { return 900 }
        "group-restart" { return 900 }
        "backup" { return 900 }
        "db-check" { return 900 }
        "docker-clean" { return 600 }
        default { return 300 }
    }
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
    # Do not emit `-Service ''` / `-Group ''`: Windows PowerShell can parse
    # those as a parameter with no argument when the command is reconstructed
    # through SSH.  Optional values must be omitted entirely for actions such
    # as status, diagnostics and a full safe update.
    $serviceArgument = if ($RemoteService) { " -Service '$serviceLiteral'" } else { "" }
    $groupArgument = if ($RemoteGroup) { " -Group '$groupLiteral'" } else { "" }
    $jsonSwitch = if ($AsJson) { " -Json" } else { "" }
    $taskTimeoutSeconds = Get-RemoteActionTimeoutSeconds $RemoteAction
    $taskWaitSeconds = $taskTimeoutSeconds + 90
    $jsonRequest = if ($AsJson) { '$true' } else { '$false' }

    # Live logs intentionally remain a direct SSH stream.  Docker logs do not
    # call the credential helper, while an interactive task cannot be safely
    # attached to or cancelled with Ctrl+C from this console.
    if ($RemoteAction -eq "service-logs-follow") {
        return @"
`$ErrorActionPreference = 'Stop'
`$ProgressPreference = 'SilentlyContinue'
`$OutputEncoding = New-Object System.Text.UTF8Encoding
[Console]::OutputEncoding = `$OutputEncoding
`$control = Join-Path '$project' 'tmod_control_windows.ps1'
if (-not (Test-Path -LiteralPath `$control)) { throw 'T-Mod Control отсутствует на сервере. Сначала обновите репозиторий.' }
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `$control -ProjectDir '$project' -PersistentDir '$persistent' -Action '$actionLiteral'$serviceArgument$groupArgument -NoAnimation$jsonSwitch
exit `$LASTEXITCODE
"@
    }

    return @"
`$ErrorActionPreference = 'Stop'
`$ProgressPreference = 'SilentlyContinue'
`$OutputEncoding = New-Object System.Text.UTF8Encoding
[Console]::OutputEncoding = `$OutputEncoding
`$control = Join-Path '$project' 'tmod_control_windows.ps1'
`$runner = Join-Path '$project' 'scripts\tmod_remote_interactive_runner.ps1'
if (-not (Test-Path -LiteralPath `$control)) { throw 'T-Mod Control отсутствует на сервере. Сначала обновите репозиторий.' }
if (-not (Test-Path -LiteralPath `$runner)) { throw 'Interactive T-Mod Remote runner отсутствует. Сначала обновите репозиторий.' }
`$taskRoot = Join-Path '$persistent' 'control\remote-tasks'
New-Item -ItemType Directory -Path `$taskRoot -Force | Out-Null
`$requestId = [guid]::NewGuid().ToString('D')
`$requestPath = Join-Path `$taskRoot ("`$requestId.request.json")
`$resultPath = Join-Path `$taskRoot ("`$requestId.result.json")
`$request = [ordered]@{
    schema = 'tmod-remote-interactive-v1'
    request_id = `$requestId
    project_dir = '$project'
    persistent_dir = '$persistent'
    action = '$actionLiteral'
    service = '$serviceLiteral'
    group = '$groupLiteral'
    timeout_seconds = $taskTimeoutSeconds
    json = $jsonRequest
}
`$requestTemporary = "`$requestPath.`$PID.tmp"
[IO.File]::WriteAllText(`$requestTemporary, (`$request | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding(`$false)))
Move-Item -LiteralPath `$requestTemporary -Destination `$requestPath -Force
`$taskUser = "{0}\{1}" -f `$env:COMPUTERNAME, `$env:USERNAME
`$taskName = "T-Mod Remote `$requestId"
`$powerShell = Join-Path `$env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
`$taskArguments = ('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -RequestPath "{1}" -ResultPath "{2}"' -f `$runner, `$requestPath, `$resultPath)
`$taskCreated = `$false
`$exitCode = 1
try {
    # A no-trigger task can only be started explicitly below.  This avoids a
    # second delayed run if the SSH connection disappears before cleanup.
    # Interactive + Highest deliberately uses the logged-in desktop token:
    # Docker Desktop's credential helper cannot work in the SSH token.
    `$taskAction = New-ScheduledTaskAction -Execute `$powerShell -Argument `$taskArguments
    `$taskPrincipal = New-ScheduledTaskPrincipal -UserId `$taskUser -LogonType Interactive -RunLevel Highest
    `$taskDefinition = New-ScheduledTask -Action `$taskAction -Principal `$taskPrincipal
    Register-ScheduledTask -TaskName `$taskName -InputObject `$taskDefinition -Force | Out-Null
    `$taskCreated = `$true
    Start-ScheduledTask -TaskName `$taskName
    `$deadline = [DateTime]::UtcNow.AddSeconds($taskWaitSeconds)
    `$result = `$null
    while ([DateTime]::UtcNow -lt `$deadline) {
        if (Test-Path -LiteralPath `$resultPath) {
            try {
                `$result = Get-Content -LiteralPath `$resultPath -Raw -Encoding UTF8 | ConvertFrom-Json
                break
            }
            catch {
                # The runner publishes atomically; a retry only handles a
                # short antivirus/file-indexing race on Windows.
            }
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not `$result) {
        Stop-ScheduledTask -TaskName `$taskName -ErrorAction SilentlyContinue
        throw "Интерактивная задача T-Mod не вернула результат за $taskWaitSeconds секунд. Проверьте, что на сервере есть сеанс `$taskUser."
    }
    if ([string]`$result.schema -ne 'tmod-remote-interactive-v1' -or [string]`$result.request_id -ne `$requestId) {
        throw "Интерактивная задача T-Mod вернула недействительный результат."
    }
    if (`$null -ne `$result.text -and [string]`$result.text) {
        [Console]::Out.Write([string]`$result.text)
        if (-not ([string]`$result.text).EndsWith("`n")) { [Console]::Out.WriteLine() }
    }
    `$exitCode = [int]`$result.exit_code
}
finally {
    if (`$taskCreated) { Unregister-ScheduledTask -TaskName `$taskName -Confirm:`$false -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath `$requestTemporary, `$requestPath, `$resultPath -Force -ErrorAction SilentlyContinue
}
exit `$exitCode
"@
}

function Invoke-ServerAction {
    param($Config, [string]$RemoteAction, [string]$RemoteService = "", [string]$RemoteGroup = "", [switch]$AsJson)
    $command = Get-ServerCommand $Config $RemoteAction $RemoteService $RemoteGroup -AsJson:$AsJson
    if ($RemoteAction -eq "service-logs-follow") {
        return (Invoke-RemotePowerShell $Config $command -Follow)
    }
    # The task itself owns the action budget.  SSH gets two short grace
    # windows: one for task start/result publication and one for cleanup.
    $sshTimeout = (Get-RemoteActionTimeoutSeconds $RemoteAction) + 180
    return (Invoke-RemotePowerShell $Config $command -TimeoutSeconds $sshTimeout)
}

function Test-RemoteConnection {
    param($Config)
    if (-not (Get-Command "ssh.exe" -ErrorAction SilentlyContinue)) { return $false }
    $arguments = Get-SshBaseArguments $Config
    $arguments += ("{0}@{1}" -f $Config.user, $Config.host)
    $arguments += @("echo", "TMOD_REMOTE_OK")
    $job = Start-Job -ScriptBlock {
        param([string[]]$SshArguments)
        $output = (& ssh.exe @SshArguments 2>$null | Out-String)
        [pscustomobject]@{ ExitCode = [int]$LASTEXITCODE; Output = $output }
    } -ArgumentList (,[string[]]$arguments)
    try {
        if (-not (Wait-Job -Job $job -Timeout 20)) {
            Stop-Job -Job $job -Force -ErrorAction SilentlyContinue
            return $false
        }
        $result = Receive-Job -Job $job | Select-Object -Last 1
        return [bool]($result -and $result.ExitCode -eq 0 -and [string]$result.Output -match "TMOD_REMOTE_OK")
    }
    finally {
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    }
}

function Get-RemoteManifest {
    try { return (Invoke-RestMethod -Uri $script:ManifestUrl -TimeoutSec 6) }
    catch { return $null }
}

function Update-RemoteClient {
    param([switch]$Quiet)
    $script:LastUpdateCheckSucceeded = $false
    $mutex = New-Object System.Threading.Mutex($false, "Local\TModRemoteClientUpdate")
    $lockAcquired = $false
    try {
        try {
            $lockAcquired = $mutex.WaitOne(0)
        }
        catch [System.Threading.AbandonedMutexException] {
            # The previous updater died while holding the mutex. The OS has
            # transferred ownership to this process, so recovery is safe.
            $lockAcquired = $true
        }
        if (-not $lockAcquired) {
            if (-not $Quiet) { Write-Host "  Обновление уже выполняется в другом окне." -ForegroundColor Yellow }
            return $false
        }
    $manifest = Get-RemoteManifest
    if (-not $manifest) {
        if (-not $Quiet) { Write-Host "  GitHub сейчас недоступен; текущая версия сохранена." -ForegroundColor Yellow }
        return $false
    }
    if ([version]$manifest.version -le [version]$script:ClientVersion) {
        $script:LastUpdateCheckSucceeded = $true
        if (-not $Quiet) { Write-Host "  Установлена актуальная версия T-Mod Remote." -ForegroundColor Green }
        return $false
    }
    $runningBundlePath = [string]$env:TMOD_REMOTE_BUNDLE_PATH
    if (-not $runningBundlePath -or -not (Test-Path -LiteralPath $runningBundlePath)) {
        if (-not $Quiet) { Write-Host "  Обновление найдено. Запустите единый файл T-Mod Remote.bat." -ForegroundColor Yellow }
        return $false
    }
    foreach ($fileName in @("tmod_remote_windows.bat")) {
        $url = [string]$manifest.files.$fileName
        if (-not $url.StartsWith("https://raw.githubusercontent.com/cdnserver/t-mod/")) { throw "Манифест содержит недопустимый адрес" }
        $temporary = "$runningBundlePath.next.download.$PID.$([guid]::NewGuid().ToString('N'))"
        $nextPath = "$runningBundlePath.next"
        $readyPath = "$runningBundlePath.next.ready"
        $readyTemporary = "$readyPath.$PID.tmp"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $temporary -TimeoutSec 15
        if ((Get-Item -LiteralPath $temporary).Length -lt 100) { throw "Загруженный файл повреждён: $fileName" }
        if (-not ((Get-Content -Raw -LiteralPath $temporary) -match ":__TMOD_REMOTE_PAYLOAD__")) { throw "Проверка клиента не пройдена" }
        $expectedHash = [string]$manifest.sha256.$fileName
        $actualHash = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($expectedHash -notmatch "^[a-fA-F0-9]{64}$" -or $actualHash -ne $expectedHash.ToLowerInvariant()) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
            throw "Контрольная сумма обновления не совпала: $fileName"
        }
        if (Test-Path -LiteralPath $nextPath) {
            [IO.File]::Replace($temporary, $nextPath, $null, $true)
        } else {
            Move-Item -LiteralPath $temporary -Destination $nextPath -Force
        }
        [IO.File]::WriteAllText(
            $readyTemporary,
            ("{0}`n{1}" -f [string]$manifest.version, $expectedHash.ToLowerInvariant()),
            (New-Object System.Text.UTF8Encoding($false))
        )
        if (Test-Path -LiteralPath $readyPath) {
            [IO.File]::Replace($readyTemporary, $readyPath, $null, $true)
        } else {
            Move-Item -LiteralPath $readyTemporary -Destination $readyPath -Force
        }
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $script:AppDir "tmod_remote_version.json") -Encoding UTF8
    $script:LastUpdateCheckSucceeded = $true
    if (-not $Quiet) { Write-Host "  T-Mod Remote v$($manifest.version) подготовлен. При следующем запуске единый файл обновится сам." -ForegroundColor Green }
    return $true
    }
    finally {
        if ($lockAcquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
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
    if (-not $script:LastUpdateCheckSucceeded) { return }
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

function Invoke-RemoteInteractiveAction {
    param($Config, [string]$RemoteAction, [string]$Section = "REMOTE OPERATION")
    Show-TModTransition -Label $Section -Theme $script:Theme -Disabled:(-not [bool]$Config.animations)
    Write-RemoteBrand -Section $Section
    try {
        $code = Invoke-ServerAction $Config $RemoteAction
        if ($code -eq 0 -and $RemoteAction -notin @("status", "diagnostics", "update-status", "git-status", "domain-check", "resources", "error-log", "auto-update-status")) {
            Write-Host "  Операция завершена." -ForegroundColor $script:Theme.Good
        }
        elseif ($code -ne 0) { Write-Host ("  Сервер вернул код {0}." -f $code) -ForegroundColor $script:Theme.Bad }
    }
    catch { Write-Host ("  Ошибка: {0}" -f $_.Exception.Message) -ForegroundColor $script:Theme.Bad }
    Wait-RemoteKey
}

function Show-RemotePower {
    param($Config)
    while ($true) {
        $action = Select-RemoteItem "Питание домашнего сервера" @(
            [pscustomobject]@{ Label = "Запустить установленную версию"; Hint = "Полная подготовка и запуск"; Value = "start" },
            [pscustomobject]@{ Label = "Перезапустить всю систему"; Hint = "Пересоздание с зависимостями"; Value = "restart" },
            [pscustomobject]@{ Label = "Остановить всю систему"; Hint = "Данные сохраняются"; Value = "stop" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главный экран"; Value = "back" }
        ) -Footer "Удалённое выключение Windows намеренно не выполняется этим пультом."
        if (-not $action -or $action -eq "back") { return }
        if ($action -in @("restart", "stop") -and -not (Confirm-RemoteAction "Подтвердить действие?" "Сервисы временно станут недоступны.")) { continue }
        Invoke-RemoteInteractiveAction $Config $action "REMOTE POWER"
    }
}

function Show-RemoteUpdates {
    param($Config)
    while ($true) {
        $action = Select-RemoteItem "Центр обновлений домашнего сервера" @(
            [pscustomobject]@{ Label = "Безопасно обновить и запустить"; Hint = "Тесты, backup, healthcheck и rollback"; Value = "update" },
            [pscustomobject]@{ Label = "История обновлений"; Hint = "Updater, watcher и launch guard"; Value = "update-status" },
            [pscustomobject]@{ Label = "Состояние Git"; Hint = "Commit, origin/main и рабочая копия"; Value = "git-status" },
            [pscustomobject]@{ Label = "Состояние автообновления"; Hint = "Windows Task Scheduler"; Value = "auto-update-status" },
            [pscustomobject]@{ Label = "Включить автообновление"; Hint = "Проверка раз в 2 минуты"; Value = "auto-update" },
            [pscustomobject]@{ Label = "Приостановить автообновление"; Hint = "Текущая версия продолжит работу"; Value = "auto-update-disable" },
            [pscustomobject]@{ Label = "Обновить клиент Remote"; Hint = "Стабильный канал с SHA-256"; Value = "self-update" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главный экран"; Value = "back" }
        ) -Footer "Серверное обновление и обновление Remote — независимые защищённые контуры."
        if (-not $action -or $action -eq "back") { return }
        if ($action -eq "auto-update-disable" -and -not (Confirm-RemoteAction "Приостановить автообновление?" "Сервер останется на текущей версии.")) { continue }
        if ($action -eq "self-update") {
            Write-RemoteBrand -Section "REMOTE UPDATE"
            try { Update-RemoteClient | Out-Null } catch { Write-Host ("  Ошибка: {0}" -f $_.Exception.Message) -ForegroundColor $script:Theme.Bad }
            Wait-RemoteKey
            continue
        }
        Invoke-RemoteInteractiveAction $Config $action "REMOTE UPDATE CENTER"
    }
}

function Show-RemoteObservability {
    param($Config)
    while ($true) {
        $action = Select-RemoteItem "Наблюдение за домашним сервером" @(
            [pscustomobject]@{ Label = "Полная диагностика"; Hint = "Docker, API, Discord и диск"; Value = "diagnostics" },
            [pscustomobject]@{ Label = "Поток инцидентов"; Hint = "Критические записи контейнеров за час"; Value = "error-log" },
            [pscustomobject]@{ Label = "Ресурсы"; Hint = "CPU, RAM, сеть и диск"; Value = "resources" },
            [pscustomobject]@{ Label = "Экспортировать отчёт"; Hint = "Файл останется на домашнем сервере"; Value = "export-diagnostics" },
            [pscustomobject]@{ Label = "Очистить старый Docker cache"; Hint = "Без volumes и рабочих контейнеров"; Value = "docker-clean" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главный экран"; Value = "back" }
        )
        if (-not $action -or $action -eq "back") { return }
        if ($action -eq "docker-clean" -and -not (Confirm-RemoteAction "Очистить старый Docker cache?" "Удаляется только неиспользуемое старше 7 дней.")) { continue }
        Invoke-RemoteInteractiveAction $Config $action "REMOTE OBSERVABILITY"
    }
}

function Show-RemoteNetwork {
    param($Config)
    while ($true) {
        $action = Select-RemoteItem "Сеть и публичные сервисы" @(
            [pscustomobject]@{ Label = "Проверить все домены"; Hint = "HTTPS и задержка"; Value = "domain-check" },
            [pscustomobject]@{ Label = "Проверить и применить Caddy"; Hint = "Validate перед reload"; Value = "caddy-reload" },
            [pscustomobject]@{ Label = "Открыть сервисы локально"; Hint = "T-Mod, Reactor, Consensus, Atlas и SGL"; Value = "open-sites" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главный экран"; Value = "back" }
        )
        if (-not $action -or $action -eq "back") { return }
        if ($action -eq "open-sites") {
            foreach ($url in @("https://tvr.lat", "https://reactor.tvr.lat", "https://consensus.tvr.lat", "https://atlas.tvr.lat", "https://sgl.tvr.lat")) { Start-Process $url }
            continue
        }
        Invoke-RemoteInteractiveAction $Config $action "REMOTE NETWORK"
    }
}

function Show-RemoteSettings {
    param($Config)
    while ($true) {
        $action = Select-RemoteItem "Настройки T-Mod Remote" @(
            [pscustomobject]@{ Label = "Aurora"; Hint = "Холодный основной стиль"; Value = "theme-aurora" },
            [pscustomobject]@{ Label = "Reactor"; Hint = "Зелёный инженерный контур"; Value = "theme-reactor" },
            [pscustomobject]@{ Label = "Atlas"; Hint = "Сине-фиолетовый контур"; Value = "theme-atlas" },
            [pscustomobject]@{ Label = "Ember"; Hint = "Янтарный аварийный контур"; Value = "theme-ember" },
            [pscustomobject]@{ Label = "Анимации"; Hint = $(if ([bool]$Config.animations) { "Включены" } else { "Выключены" }); Value = "animations" },
            [pscustomobject]@{ Label = "Переподключить сервер"; Hint = "WireGuard, SSH, пути и новый ключ"; Value = "configure" },
            [pscustomobject]@{ Label = "Назад"; Hint = "Главный экран"; Value = "back" }
        )
        if (-not $action -or $action -eq "back") { return $Config }
        if ($action.StartsWith("theme-")) {
            $Config.theme = $action.Substring("theme-".Length)
            $script:Theme = Get-TModTheme ([string]$Config.theme)
            Save-RemoteConfig $Config
            Show-TModTransition -Label ("THEME / {0}" -f ([string]$Config.theme).ToUpperInvariant()) -Theme $script:Theme -Disabled:(-not [bool]$Config.animations)
            continue
        }
        if ($action -eq "animations") { $Config.animations = -not [bool]$Config.animations; Save-RemoteConfig $Config; continue }
        if ($action -eq "configure") { return (New-RemoteConfiguration) }
    }
}

function Show-RemoteMenu {
    $config = Get-RemoteConfig
    if (-not $config) { $config = New-RemoteConfiguration }
    $script:Theme = Get-TModTheme ([string]$config.theme)
    Invoke-AutomaticUpdateCheck
    Show-TModIntro -Theme $script:Theme -Disabled:(-not [bool]$config.animations) -Mode "REMOTE CONTROL"
    while ($true) {
        $online = Test-RemoteConnection $config
        $connectionHint = if ($online) { "$($config.host) · WireGuard/SSH online" } else { "$($config.host) · нет соединения" }
        $remoteDashboard = {
            Write-TModCardRow -Theme $script:Theme -Cards @(
                [pscustomobject]@{ Title = "CLIENT"; Value = ("v{0}" -f $script:ClientVersion); Kind = "info" },
                [pscustomobject]@{ Title = "TUNNEL"; Value = $(if ($online) { "online" } else { "offline" }); Kind = $(if ($online) { "good" } else { "bad" }) },
                [pscustomobject]@{ Title = "TARGET"; Value = [string]$config.host; Kind = $(if ($online) { "good" } else { "warn" }) }
            )
        }
        $selection = Select-RemoteItem "Удалённый центр управления" @(
            [pscustomobject]@{ Label = "Обзор сервера"; Hint = $connectionHint; Value = "status" },
            [pscustomobject]@{ Label = "Безопасно обновить"; Hint = "GitHub, тесты, backup и rollback"; Value = "update" },
            [pscustomobject]@{ Label = "Питание системы"; Hint = "Запуск, restart и остановка"; Value = "power" },
            [pscustomobject]@{ Label = "Сервисы"; Hint = "Каждый контейнер и живые логи"; Value = "services" },
            [pscustomobject]@{ Label = "Контуры"; Hint = "Ядро T-Mod, Atlas и Minecraft"; Value = "groups" },
            [pscustomobject]@{ Label = "Центр обновлений"; Hint = "История, Git, watcher и клиент"; Value = "updates" },
            [pscustomobject]@{ Label = "Наблюдение"; Hint = "Диагностика, инциденты и ресурсы"; Value = "observability" },
            [pscustomobject]@{ Label = "Защита данных"; Hint = "Backup, состояние и полная проверка"; Value = "data" },
            [pscustomobject]@{ Label = "Сеть"; Hint = "Домены, Caddy и публичные сервисы"; Value = "network" },
            [pscustomobject]@{ Label = "Настройки"; Hint = "Темы, анимации и подключение"; Value = "settings" },
            [pscustomobject]@{ Label = "Выход"; Hint = "Закрыть T-Mod Remote"; Value = "exit" }
        ) -OnRender $remoteDashboard -Hotkeys @{ R = "refresh"; U = "updates"; D = "diagnostics"; Q = "exit" } -Footer "R обновить  ·  U обновления  ·  D диагностика  ·  Q выход"
        if (-not $selection -or $selection -eq "exit") { return }
        if ($selection -eq "refresh") { continue }
        if ($selection -eq "settings") { $config = Show-RemoteSettings $config; $script:Theme = Get-TModTheme ([string]$config.theme); continue }
        if ($selection -eq "updates") { Show-RemoteUpdates $config; continue }
        if (-not $online) {
            Write-RemoteBrand -Section "CONNECTION LOST"
            Write-Host "  Сервер недоступен. Проверьте WireGuard и SSH." -ForegroundColor $script:Theme.Bad
            Write-Host ("  Адрес: {0}:{1}" -f $config.host, $config.port) -ForegroundColor DarkGray
            Wait-RemoteKey
            continue
        }
        if ($selection -eq "power") { Show-RemotePower $config; continue }
        if ($selection -eq "services") { Show-RemoteServices $config; continue }
        if ($selection -eq "groups") { Show-RemoteGroups $config; continue }
        if ($selection -eq "observability") { Show-RemoteObservability $config; continue }
        if ($selection -eq "data") { Show-RemoteData $config; continue }
        if ($selection -eq "network") { Show-RemoteNetwork $config; continue }
        Invoke-RemoteInteractiveAction $config $selection $(if ($selection -eq "diagnostics") { "REMOTE DIAGNOSTICS" } else { "REMOTE SAFE UPDATE" })
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
