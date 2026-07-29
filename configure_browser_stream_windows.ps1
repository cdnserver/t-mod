[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetEnvPath,

    [Parameter(Mandatory = $true)]
    [string]$BrowserEnvPath,

    [Parameter(Mandatory = $true)]
    [string]$TemplateEnvPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-EnvValues {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $values = @{}
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $values[$Matches[1]] = $Matches[2].Trim()
        }
    }
    return $values
}

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Key,

        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $lines.Add($line)
    }
    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*="
    $found = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match $pattern) {
            $lines[$index] = "${Key}=${Value}"
            $found = $true
        }
    }
    if (!$found) {
        $lines.Add("${Key}=${Value}")
    }
    $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllLines($Path, $lines, $utf8WithoutBom)
}

function New-ControlToken {
    $bytes = [byte[]]::new(32)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

if (!(Test-Path -LiteralPath $TargetEnvPath)) {
    Write-Error "Persistent T-Mod .env was not found: $TargetEnvPath"
    exit 1
}
if (!(Test-Path -LiteralPath $TemplateEnvPath)) {
    Write-Error "Browser client environment template was not found: $TemplateEnvPath"
    exit 1
}
if (!(Test-Path -LiteralPath $BrowserEnvPath)) {
    Copy-Item -LiteralPath $TemplateEnvPath -Destination $BrowserEnvPath
    Write-Host "  [NEW] Created isolated browser-stream.env."
}

$commonValues = Read-EnvValues -Path $TargetEnvPath
$enabled = (
    $commonValues.ContainsKey("BROWSER_STREAM_ENABLED") -and
    $commonValues["BROWSER_STREAM_ENABLED"] -match '^(true|1|yes|on)$'
)
if (!$enabled) {
    Write-Host "  [SKIP] Discord browser client is disabled."
    exit 0
}

$browserValues = Read-EnvValues -Path $BrowserEnvPath
$userToken = if ($browserValues.ContainsKey("BROWSER_STREAM_DISCORD_TOKEN")) {
    $browserValues["BROWSER_STREAM_DISCORD_TOKEN"]
} else {
    ""
}
$botToken = if ($commonValues.ContainsKey("DISCORD_TOKEN")) {
    $commonValues["DISCORD_TOKEN"]
} else {
    ""
}
if (!$userToken -or $userToken -match '^(YOUR_|PASTE_|CHANGE_)') {
    Write-Error "Set BROWSER_STREAM_DISCORD_TOKEN in the isolated browser-stream.env."
    exit 1
}
if ($botToken -and $userToken -eq $botToken) {
    Write-Error "BROWSER_STREAM_DISCORD_TOKEN must never equal the T-Mod bot token."
    exit 1
}

$allowed = if ($browserValues.ContainsKey("BROWSER_STREAM_ALLOWED_USER_IDS")) {
    $browserValues["BROWSER_STREAM_ALLOWED_USER_IDS"]
} else {
    ""
}
$allowedIds = @($allowed.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (!$allowedIds.Count -or @($allowedIds | Where-Object { $_ -notmatch '^\d{17,20}$' }).Count) {
    Write-Error "BROWSER_STREAM_ALLOWED_USER_IDS must contain comma-separated Discord IDs."
    exit 1
}

$controlToken = if ($commonValues.ContainsKey("BROWSER_STREAM_CONTROL_TOKEN")) {
    $commonValues["BROWSER_STREAM_CONTROL_TOKEN"]
} else {
    ""
}
if ($controlToken.Length -lt 32) {
    $controlToken = New-ControlToken
    Set-EnvValue -Path $TargetEnvPath -Key "BROWSER_STREAM_CONTROL_TOKEN" -Value $controlToken
    Write-Host "  [OK] Generated the private browser-client control token."
}
Set-EnvValue -Path $BrowserEnvPath -Key "BROWSER_STREAM_CONTROL_TOKEN" -Value $controlToken

Write-Host "  [OK] Discord browser client configuration is ready."
Write-Host "  [INFO] User credentials are isolated from the T-Mod container."
Write-Host "  [INFO] The client uses a separate user account and an unofficial Discord protocol."
