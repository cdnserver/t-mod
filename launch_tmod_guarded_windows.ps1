param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [string]$PersistentDir = "$env:USERPROFILE\Documents\SGLDiscordBot",
    [string]$Branch = "main",
    [string]$Remote = "origin",
    [ValidateRange(300, 7200)][int]$UpdateTimeoutSeconds = 1800,
    [ValidateRange(120, 3600)][int]$FallbackTimeoutSeconds = 1200
)

$ErrorActionPreference = "Stop"
$GuardStatePath = Join-Path $PersistentDir "updates\launcher_guard.json"

function Quote-NativeArgument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Write-GuardState([string]$State, [string]$Message) {
    $directory = Split-Path -Parent $GuardStatePath
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $payload = @{
        state = $State
        message = $Message
        updated_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $temporary = "$GuardStatePath.tmp"
    $utf8NoBom = New-Object System.Text.UTF8Encoding
    [IO.File]::WriteAllText($temporary, ($payload | ConvertTo-Json), $utf8NoBom)
    Move-Item -LiteralPath $temporary -Destination $GuardStatePath -Force
}

function Invoke-BoundedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $process.StartInfo.FileName = $File
    $process.StartInfo.Arguments = $Arguments
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $false
    try {
        if (-not $process.Start()) {
            return @{ exit_code = 125; timed_out = $false }
        }
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            & taskkill.exe /PID $process.Id /T /F *> $null
            $process.WaitForExit(5000) | Out-Null
            return @{ exit_code = 124; timed_out = $true }
        }
        return @{ exit_code = $process.ExitCode; timed_out = $false }
    }
    finally { $process.Dispose() }
}

function Start-InstalledRuntime {
    $previousSkip = $env:TMOD_SKIP_BUILD
    $previousNonInteractive = $env:TMOD_NONINTERACTIVE
    $previousTransactional = $env:TMOD_TRANSACTIONAL_UPDATE
    try {
        # Rebuild from the installed Git revision. This prevents an interrupted
        # candidate build from ever becoming the image that is started.
        $env:TMOD_SKIP_BUILD = "0"
        $env:TMOD_NONINTERACTIVE = "1"
        $env:TMOD_TRANSACTIONAL_UPDATE = "1"
        $runtimePath = Join-Path $ProjectDir "run_windows.bat"
        if (-not (Test-Path -LiteralPath $runtimePath)) {
            throw "Installed runtime launcher is missing: $runtimePath"
        }
        # A spawned Process does not reliably inherit PowerShell's temporary
        # Push-Location on Windows. Pass an absolute batch path to cmd instead.
        $runtimeArguments = '/d /s /c ""{0}""' -f $runtimePath.Replace('"', '""')
        return Invoke-BoundedProcess `
            -File "cmd.exe" `
            -Arguments $runtimeArguments `
            -TimeoutSeconds $FallbackTimeoutSeconds
    }
    finally {
        $env:TMOD_SKIP_BUILD = $previousSkip
        $env:TMOD_NONINTERACTIVE = $previousNonInteractive
        $env:TMOD_TRANSACTIONAL_UPDATE = $previousTransactional
    }
}

try {
    $ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
    $safeUpdater = Join-Path $ProjectDir "safe_update_windows.ps1"
    if (-not (Test-Path -LiteralPath $safeUpdater)) {
        throw "Safe updater is missing: $safeUpdater"
    }
    $arguments = @(
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy Bypass",
        "-File $(Quote-NativeArgument $safeUpdater)",
        "-ProjectDir $(Quote-NativeArgument $ProjectDir)",
        "-PersistentDir $(Quote-NativeArgument $PersistentDir)",
        "-Branch $(Quote-NativeArgument $Branch)",
        "-Remote $(Quote-NativeArgument $Remote)"
    ) -join " "
    $update = Invoke-BoundedProcess `
        -File "powershell.exe" `
        -Arguments $arguments `
        -TimeoutSeconds $UpdateTimeoutSeconds
    if (-not $update.timed_out -and $update.exit_code -eq 0) {
        Write-GuardState "success" "Безопасный запуск завершён штатно."
        exit 0
    }

    $reason = if ($update.timed_out) {
        "Обновление превысило ${UpdateTimeoutSeconds} секунд и было остановлено."
    }
    else {
        "Безопасное обновление завершилось с кодом $($update.exit_code)."
    }
    Write-Host "[LAUNCH GUARD] $reason Starting the installed release." -ForegroundColor Yellow
    Write-GuardState "fallback" $reason
    $fallback = Start-InstalledRuntime
    if ($fallback.timed_out) {
        Write-GuardState "fallback_timeout" "Запуск установленной версии превысил ${FallbackTimeoutSeconds} секунд."
        exit 1
    }
    if ($fallback.exit_code -ne 0) {
        Write-GuardState "fallback_failed" "Установленная версия завершила запуск с кодом $($fallback.exit_code)."
        exit $fallback.exit_code
    }
    Write-GuardState "fallback_started" "После ошибки обновления запущена установленная версия."
    exit 0
}
catch {
    $failure = "{0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message
    try { Write-GuardState "error" $failure } catch {}
    Write-Host "[LAUNCH GUARD] $failure" -ForegroundColor Red
    exit 1
}
