param(
    [string]$ProjectDir = $PSScriptRoot,
    [ValidateRange(1, 60)][int]$IntervalMinutes = 2,
    [string]$TaskName = "T-Mod Auto Update"
)

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
$WatcherPath = Join-Path $ProjectDir "watch_tmod_updates_windows.ps1"
if (-not (Test-Path -LiteralPath $WatcherPath)) {
    throw "Automatic update watcher is missing: $WatcherPath"
}

$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$taskCommand = '"{0}" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{1}"' -f $PowerShellExe, $WatcherPath
$taskScheduler = Join-Path $env:SystemRoot "System32\schtasks.exe"

$output = & $taskScheduler /Create /F /TN $TaskName /SC MINUTE /MO $IntervalMinutes /RL LIMITED /TR $taskCommand 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Task Scheduler rejected the T-Mod update watcher: $($output | Out-String)"
}

Write-Host "[AUTO UPDATE] Watching origin/main every $IntervalMinutes minute(s)."
Write-Host "[AUTO UPDATE] Task: $TaskName"
Write-Host "[AUTO UPDATE] New commits use Safe Update with tests, DB backup and rollback."
