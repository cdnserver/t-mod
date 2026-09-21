<#
  T-Mod Remote interactive task runner.

  Docker Desktop's credential helper is tied to the logged-in Windows desktop
  session.  This runner is deliberately started by a one-shot Scheduled Task
  with LogonType=Interactive, never directly from the noninteractive SSH
  session.  It accepts only a small JSON request and can execute only the
  allowlisted T-Mod control actions below.
#>
param(
    [Parameter(Mandatory = $true)][string]$RequestPath,
    [Parameter(Mandatory = $true)][string]$ResultPath
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$script:Schema = "tmod-remote-interactive-v1"
$script:MaximumOutputCharacters = 262144
$script:AllowedActions = @(
    "status", "update", "start", "stop", "restart",
    "service-start", "service-stop", "service-restart", "service-update",
    "service-logs",
    "group-start", "group-stop", "group-restart",
    "diagnostics", "backup", "db-status", "db-check", "caddy-reload",
    "auto-update", "auto-update-status", "auto-update-disable", "update-status",
    "git-status", "domain-check", "resources", "error-log", "export-diagnostics",
    "docker-clean", "version"
)
$script:AllowedServices = @(
    "tmod-postgres", "tmod-db-migrate", "tmod-discord-bot", "tmod-web",
    "tmod-api", "tmod-worker", "atlas-qdrant", "atlas-forum-browser", "tmod-caddy",
    "minecraft", "minecraft-supervisor"
)
$script:AllowedGroups = @("core", "atlas", "minecraft")

function ConvertTo-ProcessArgument {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value -or $Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Limit-Text {
    param([AllowNull()][string]$Text)
    if ($null -eq $Text) { return "" }
    if ($Text.Length -le $script:MaximumOutputCharacters) { return $Text }
    $keep = $script:MaximumOutputCharacters - 96
    return "[T-Mod Remote: output was truncated to the newest ${keep} characters.]`r`n" + $Text.Substring($Text.Length - $keep)
}

function Write-RemoteResult {
    param(
        [string]$RequestId,
        [int]$ExitCode,
        [bool]$TimedOut,
        [AllowNull()][string]$Text
    )
    $resultDirectory = Split-Path -Parent $ResultPath
    New-Item -ItemType Directory -Path $resultDirectory -Force | Out-Null
    $payload = [ordered]@{
        schema = $script:Schema
        request_id = $RequestId
        completed_at = (Get-Date).ToUniversalTime().ToString("o")
        exit_code = $ExitCode
        timed_out = $TimedOut
        text = Limit-Text $Text
    }
    $temporary = "${ResultPath}.$PID.$([guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText(
        $temporary,
        ($payload | ConvertTo-Json -Depth 6),
        (New-Object System.Text.UTF8Encoding($false))
    )
    try {
        if (Test-Path -LiteralPath $ResultPath) {
            try {
                [IO.File]::Replace($temporary, $ResultPath, $null, $true)
            }
            catch {
                # The result is disposable and request ids are unique.  A
                # short-lived scanner can still hold an old file open, so use
                # the ordinary replace path as a compatibility fallback.
                Move-Item -LiteralPath $temporary -Destination $ResultPath -Force
            }
        }
        else {
            Move-Item -LiteralPath $temporary -Destination $ResultPath -Force
        }
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Read-RemoteRequest {
    if (-not (Test-Path -LiteralPath $RequestPath)) {
        throw "T-Mod Remote request file was not found"
    }
    if ((Get-Item -LiteralPath $RequestPath).Length -gt 65536) {
        throw "T-Mod Remote request is too large"
    }
    try {
        $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "T-Mod Remote request JSON is invalid"
    }
    if ([string]$request.schema -ne $script:Schema) {
        throw "T-Mod Remote request schema is unsupported"
    }
    $requestId = [string]$request.request_id
    try { [void]([guid]$requestId) }
    catch { throw "T-Mod Remote request id is invalid" }

    $action = [string]$request.action
    $service = [string]$request.service
    $group = [string]$request.group
    if ($script:AllowedActions -notcontains $action) {
        throw "T-Mod Remote action is not allowed"
    }
    if ($service -and $script:AllowedServices -notcontains $service) {
        throw "T-Mod Remote service is not allowed"
    }
    if ($group -and $script:AllowedGroups -notcontains $group) {
        throw "T-Mod Remote group is not allowed"
    }

    try { $timeout = [int]$request.timeout_seconds }
    catch { throw "T-Mod Remote timeout is invalid" }
    if ($timeout -lt 30 -or $timeout -gt 1800) {
        throw "T-Mod Remote timeout is outside the allowed range"
    }

    $project = (Resolve-Path -LiteralPath ([string]$request.project_dir) -ErrorAction Stop).Path
    $persistent = (Resolve-Path -LiteralPath ([string]$request.persistent_dir) -ErrorAction Stop).Path
    $control = Join-Path $project "tmod_control_windows.ps1"
    if (-not (Test-Path -LiteralPath $control -PathType Leaf)) {
        throw "T-Mod Control was not found in the requested project"
    }

    $taskRoot = Join-Path $persistent "control\remote-tasks"
    $expectedRequest = Join-Path $taskRoot "$requestId.request.json"
    $expectedResult = Join-Path $taskRoot "$requestId.result.json"
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath($RequestPath),
        [IO.Path]::GetFullPath($expectedRequest),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "T-Mod Remote request path is not allowed"
    }
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath($ResultPath),
        [IO.Path]::GetFullPath($expectedResult),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "T-Mod Remote result path is not allowed"
    }

    return [pscustomobject]@{
        RequestId = $requestId
        Action = $action
        Service = $service
        Group = $group
        ProjectDir = $project
        PersistentDir = $persistent
        TimeoutSeconds = $timeout
        Json = [bool]$request.json
    }
}

function Invoke-InteractiveControl {
    param($Request)
    $control = Join-Path $Request.ProjectDir "tmod_control_windows.ps1"
    $arguments = @(
        "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", $control,
        "-ProjectDir", $Request.ProjectDir,
        "-PersistentDir", $Request.PersistentDir,
        "-Action", $Request.Action,
        "-NoAnimation"
    )
    if ($Request.Service) { $arguments += @("-Service", $Request.Service) }
    if ($Request.Group) { $arguments += @("-Group", $Request.Group) }
    if ($Request.Json) { $arguments += "-Json" }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $process.StartInfo.FileName = "powershell.exe"
    $process.StartInfo.Arguments = (($arguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join " ")
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    try {
        if (-not $process.Start()) {
            return [pscustomobject]@{ ExitCode = 125; TimedOut = $false; Text = "T-Mod Control process could not start" }
        }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($Request.TimeoutSeconds * 1000)) {
            & taskkill.exe /PID $process.Id /T /F *> $null
            $process.WaitForExit(5000) | Out-Null
            $text = "T-Mod Control exceeded the ${Request.TimeoutSeconds}s limit."
            return [pscustomobject]@{ ExitCode = 124; TimedOut = $true; Text = $text }
        }
        $text = $stdout.Result
        $errorText = $stderr.Result
        if ($errorText) {
            if ($text) { $text += "`r`n" }
            $text += $errorText
        }
        return [pscustomobject]@{ ExitCode = [int]$process.ExitCode; TimedOut = $false; Text = $text }
    }
    finally {
        $process.Dispose()
    }
}

$requestId = "unknown"
$exitCode = 1
$timedOut = $false
$text = ""
try {
    $request = Read-RemoteRequest
    $requestId = $request.RequestId
    $env:TMOD_REMOTE_INTERACTIVE = "1"
    $outcome = Invoke-InteractiveControl $request
    $exitCode = [int]$outcome.ExitCode
    $timedOut = [bool]$outcome.TimedOut
    $text = [string]$outcome.Text
}
catch {
    $text = "T-Mod Remote interactive runner failed: $($_.Exception.Message)"
}

try {
    Write-RemoteResult -RequestId $requestId -ExitCode $exitCode -TimedOut $timedOut -Text $text
}
catch {
    Write-Error "T-Mod Remote could not write its result: $($_.Exception.Message)"
    exit 1
}
exit $exitCode
