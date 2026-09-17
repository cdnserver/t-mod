param(
    [string]$ProjectDir = $PSScriptRoot,
    [string]$PersistentDir = "$env:USERPROFILE\Documents\SGLDiscordBot",
    [string]$Branch = "main",
    [string]$Remote = "origin",
    [ValidateRange(15, 600)][int]$GitTimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$UpdateRoot = Join-Path $PersistentDir "updates"
$WatcherStatePath = Join-Path $UpdateRoot "watcher.json"
$SafeUpdateStatusPath = Join-Path $UpdateRoot "status.json"
$LogPath = Join-Path $UpdateRoot "watcher.log"
$WatcherMutex = New-Object System.Threading.Mutex($false, "Local\TModAutoUpdateWatcher")
$MutexAcquired = $WatcherMutex.WaitOne(0)
if (-not $MutexAcquired) { exit 0 }

function Write-WatcherState {
    param(
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][string]$Message,
        [string]$CurrentCommit = "unknown",
        [string]$RemoteCommit = "unknown"
    )
    New-Item -ItemType Directory -Path $UpdateRoot -Force | Out-Null
    $payload = @{
        state = $State
        message = $Message
        current_commit = $CurrentCommit
        remote_commit = $RemoteCommit
        checked_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $temporary = "$WatcherStatePath.tmp"
    $utf8NoBom = New-Object System.Text.UTF8Encoding
    [IO.File]::WriteAllText(
        $temporary,
        ($payload | ConvertTo-Json -Depth 4),
        $utf8NoBom
    )
    Move-Item -LiteralPath $temporary -Destination $WatcherStatePath -Force
}

function Write-WatcherLog([string]$Message) {
    New-Item -ItemType Directory -Path $UpdateRoot -Force | Out-Null
    if ((Test-Path -LiteralPath $LogPath) -and (Get-Item $LogPath).Length -gt 1MB) {
        Move-Item -LiteralPath $LogPath -Destination "$LogPath.previous" -Force
    }
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value (
        "[{0}] {1}" -f (Get-Date).ToUniversalTime().ToString("o"), $Message
    )
}

function Get-RemoteCommitBounded {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$RemoteName,
        [Parameter(Mandatory = $true)][string]$BranchName,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $process.StartInfo.FileName = "git.exe"
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $safeDirectory = $Directory.Replace('"', '\"')
    $safeRemote = $RemoteName.Replace('"', '\"')
    $safeBranch = $BranchName.Replace('"', '\"')
    $process.StartInfo.Arguments = (
        '-C "{0}" -c credential.interactive=never -c http.lowSpeedLimit=1 ' +
        '-c http.lowSpeedTime=20 ls-remote --exit-code "{1}" "refs/heads/{2}"'
    ) -f $safeDirectory, $safeRemote, $safeBranch
    try {
        if (-not $process.Start()) { return $null }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            & taskkill.exe /PID $process.Id /T /F *> $null
            $process.WaitForExit(5000) | Out-Null
            Write-WatcherLog "Git remote check timed out after ${TimeoutSeconds}s"
            return $null
        }
        if ($process.ExitCode -ne 0) {
            $errorText = $stderr.Result.Trim()
            if ($errorText) { Write-WatcherLog "Git remote check failed: $errorText" }
            return $null
        }
        $line = ($stdout.Result -split "`r?`n" | Select-Object -First 1)
        if (-not $line) { return $null }
        return (($line -split "\s+")[0]).Trim()
    }
    catch {
        Write-WatcherLog "Git remote check exception: $($_.Exception.Message)"
        return $null
    }
    finally { $process.Dispose() }
}

try {
    $ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
    $UpdaterPath = Join-Path $ProjectDir "safe_update_windows.ps1"
    $GuardedLauncherPath = Join-Path $ProjectDir "launch_tmod_guarded_windows.ps1"
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir ".git"))) {
        throw "Project directory is not a Git repository: $ProjectDir"
    }
    if (-not (Test-Path -LiteralPath $UpdaterPath)) {
        throw "Safe updater is missing: $UpdaterPath"
    }
    if (-not (Test-Path -LiteralPath $GuardedLauncherPath)) {
        throw "Guarded launcher is missing: $GuardedLauncherPath"
    }

    $CurrentCommit = (& git -C $ProjectDir rev-parse HEAD 2>$null | Select-Object -First 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $CurrentCommit) {
        throw "Could not read the installed Git commit"
    }

    $previousPrompt = $env:GIT_TERMINAL_PROMPT
    $env:GIT_TERMINAL_PROMPT = "0"
    try {
        $RemoteCommit = Get-RemoteCommitBounded `
            -Directory $ProjectDir `
            -RemoteName $Remote `
            -BranchName $Branch `
            -TimeoutSeconds $GitTimeoutSeconds
    }
    finally { $env:GIT_TERMINAL_PROMPT = $previousPrompt }

    if (-not $RemoteCommit) {
        Write-WatcherState -State "offline" -Message "GitHub временно недоступен; работающая версия не изменена." -CurrentCommit $CurrentCommit
        exit 0
    }
    if ($CurrentCommit -eq $RemoteCommit) {
        Write-WatcherState -State "current" -Message "Установлена актуальная версия." -CurrentCommit $CurrentCommit -RemoteCommit $RemoteCommit
        exit 0
    }

    $dirty = (& git -C $ProjectDir status --porcelain --untracked-files=normal | Out-String).Trim()
    if ($dirty) {
        Write-WatcherState -State "blocked_local_changes" -Message "Новая версия найдена, но серверная копия содержит локальные изменения." -CurrentCommit $CurrentCommit -RemoteCommit $RemoteCommit
        exit 0
    }

    if (Test-Path -LiteralPath $SafeUpdateStatusPath) {
        try {
            $previousStatus = Get-Content -Raw -LiteralPath $SafeUpdateStatusPath | ConvertFrom-Json
            if (@("rolled_back", "failed") -contains [string]$previousStatus.state -and $previousStatus.new_commit -eq $RemoteCommit) {
                Write-WatcherState -State "release_quarantined" -Message "Этот релиз уже был отклонён или не смог безопасно восстановиться; ожидается следующий коммит." -CurrentCommit $CurrentCommit -RemoteCommit $RemoteCommit
                exit 0
            }
        }
        catch {}
    }

    $shortCurrent = $CurrentCommit.Substring(0, [Math]::Min(12, $CurrentCommit.Length))
    $shortRemote = $RemoteCommit.Substring(0, [Math]::Min(12, $RemoteCommit.Length))
    Write-WatcherState -State "update_detected" -Message "Найден релиз $shortRemote; запущено безопасное обновление." -CurrentCommit $CurrentCommit -RemoteCommit $RemoteCommit
    Write-WatcherLog "Update detected: $shortCurrent -> $shortRemote"

    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $GuardedLauncherPath `
        -ProjectDir $ProjectDir `
        -PersistentDir $PersistentDir `
        -Branch $Branch `
        -Remote $Remote
    $updateExitCode = $LASTEXITCODE

    $installedCommit = (& git -C $ProjectDir rev-parse HEAD 2>$null | Select-Object -First 1).Trim()
    if ($updateExitCode -eq 0 -and $installedCommit -eq $RemoteCommit) {
        Write-WatcherState -State "updated" -Message "Релиз $shortRemote проверен и запущен." -CurrentCommit $installedCommit -RemoteCommit $RemoteCommit
        Write-WatcherLog "Update completed: $shortRemote"
        exit 0
    }

    Write-WatcherState -State "not_applied" -Message "Новый релиз не был применён; сохранена предыдущая рабочая версия." -CurrentCommit $installedCommit -RemoteCommit $RemoteCommit
    Write-WatcherLog "Update was not applied (exit=$updateExitCode, installed=$installedCommit, target=$shortRemote)"
    exit 0
}
catch {
    $failure = "{0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message
    try {
        Write-WatcherState -State "error" -Message "Проверка обновлений завершилась ошибкой; работающая версия не изменена."
        Write-WatcherLog $failure
    }
    catch {}
    exit 0
}
finally {
    if ($MutexAcquired) {
        $WatcherMutex.ReleaseMutex()
        $WatcherMutex.Dispose()
    }
}
