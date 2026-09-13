[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetEnvPath,

    [string]$PublicHostName = "tmod.rundans.lat",
    [string]$OriginUrl = "http://127.0.0.1:8787"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$publicUrl = "https://$PublicHostName"
$legacyFirewallRules = @(
    "T-Mod Consensus via WireGuard",
    "T-Mod Consensus local network"
)

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-CloudflaredContext {
    $service = Get-CimInstance Win32_Service |
        Where-Object { $_.Name -ieq "cloudflared" } |
        Select-Object -First 1
    if (!$service) {
        throw "Windows service 'cloudflared' was not found."
    }

    $commandLine = [string]$service.PathName
    $executablePath = ""
    if ($commandLine -match '^\s*"([^"]*cloudflared(?:\.exe)?)"') {
        $executablePath = $Matches[1]
    }
    elseif ($commandLine -match '^\s*([^\s]*cloudflared(?:\.exe)?)') {
        $executablePath = $Matches[1]
    }

    $configPath = ""
    if (
        $commandLine -match
        '(?i)--config(?:=|\s+)(?:"([^"]+)"|([^\s]+))'
    ) {
        $configPath = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
    }

    $candidatePaths = @(
        $configPath,
        (Join-Path $env:SystemRoot "System32\config\systemprofile\.cloudflared\config.yml"),
        (Join-Path $env:SystemRoot "System32\config\systemprofile\.cloudflared\config.yaml"),
        (Join-Path $env:USERPROFILE ".cloudflared\config.yml"),
        (Join-Path $env:USERPROFILE ".cloudflared\config.yaml"),
        (Join-Path $env:ProgramData "cloudflared\config.yml"),
        (Join-Path $env:ProgramData "cloudflared\config.yaml")
    ) | Where-Object { $_ }

    $configPath = $candidatePaths |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if (!$configPath) {
        throw (
            "cloudflared config.yml was not found. Service command: " +
            $commandLine
        )
    }

    if (!$executablePath -or !(Test-Path -LiteralPath $executablePath)) {
        $command = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
        if ($command) {
            $executablePath = $command.Source
        }
    }
    if (!$executablePath -or !(Test-Path -LiteralPath $executablePath)) {
        throw "cloudflared.exe was not found."
    }

    return [pscustomobject]@{
        ServiceName = [string]$service.Name
        ServiceState = [string]$service.State
        ExecutablePath = [string]$executablePath
        ConfigPath = [string]$configPath
    }
}

function Test-EnvConfiguration {
    if (!(Test-Path -LiteralPath $TargetEnvPath)) {
        return $false
    }
    foreach ($line in (Get-Content -LiteralPath $TargetEnvPath -Encoding UTF8)) {
        if (
            $line -match '^\s*CONSENSUS_WEB_PUBLIC_URL\s*=(.*)$' -and
            $Matches[1].Trim() -eq $publicUrl
        ) {
            return $true
        }
    }
    return $false
}

function Get-RouteState {
    param([Parameter(Mandatory = $true)][string]$ConfigPath)

    $lines = @(Get-Content -LiteralPath $ConfigPath -Encoding UTF8)
    $hostPattern = (
        '^(?<indent>\s*)-\s*hostname:\s*["'']?' +
        [regex]::Escape($PublicHostName) +
        '["'']?(?:\s+#.*)?\s*$'
    )
    for ($index = 0; $index -lt $lines.Count; $index++) {
        $hostMatch = [regex]::Match(
            $lines[$index],
            $hostPattern,
            [Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
        if (!$hostMatch.Success) {
            continue
        }

        $indent = $hostMatch.Groups["indent"].Value
        $nextRulePattern = "^" + [regex]::Escape($indent) + "-\s*"
        for ($cursor = $index + 1; $cursor -lt $lines.Count; $cursor++) {
            if ($lines[$cursor] -match $nextRulePattern) {
                break
            }
            if ($lines[$cursor] -match '^\s*service:\s*(\S+)\s*(?:#.*)?$') {
                return [pscustomobject]@{
                    Found = $true
                    HostIndex = $index
                    ServiceIndex = $cursor
                    Service = [string]$Matches[1]
                    Indent = $indent
                }
            }
        }
        return [pscustomobject]@{
            Found = $true
            HostIndex = $index
            ServiceIndex = -1
            Service = ""
            Indent = $indent
        }
    }
    return [pscustomobject]@{
        Found = $false
        HostIndex = -1
        ServiceIndex = -1
        Service = ""
        Indent = ""
    }
}

function Test-CloudflareConfiguration {
    try {
        $context = Get-CloudflaredContext
        $route = Get-RouteState -ConfigPath $context.ConfigPath
        return (
            $route.Found -and
            $route.Service -eq $OriginUrl -and
            $context.ServiceState -eq "Running"
        )
    }
    catch {
        return $false
    }
}

function Set-EnvConfiguration {
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in (Get-Content -LiteralPath $TargetEnvPath -Encoding UTF8)) {
        $lines.Add($line)
    }

    $found = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match '^\s*CONSENSUS_WEB_PUBLIC_URL\s*=') {
            $lines[$index] = "CONSENSUS_WEB_PUBLIC_URL=$publicUrl"
            $found = $true
        }
    }
    if (!$found) {
        $lines.Add("CONSENSUS_WEB_PUBLIC_URL=$publicUrl")
    }

    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($TargetEnvPath, $lines, $utf8WithoutBom)
}

function Set-CloudflareRoute {
    param([Parameter(Mandatory = $true)]$Context)

    $configPath = [string]$Context.ConfigPath
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in (Get-Content -LiteralPath $configPath -Encoding UTF8)) {
        $lines.Add($line)
    }

    if (!($lines | Where-Object { $_ -match '^\s*-\s*service:\s*http_status:' })) {
        throw "cloudflared ingress has no final http_status catch-all rule."
    }

    $route = Get-RouteState -ConfigPath $configPath
    if ($route.Found) {
        $serviceLine = "$($route.Indent)  service: $OriginUrl"
        if ($route.ServiceIndex -ge 0) {
            $lines[$route.ServiceIndex] = $serviceLine
        }
        else {
            $lines.Insert($route.HostIndex + 1, $serviceLine)
        }
    }
    else {
        $ingressIndex = -1
        $ingressIndent = ""
        for ($index = 0; $index -lt $lines.Count; $index++) {
            if ($lines[$index] -match '^(?<indent>\s*)ingress:\s*(?:#.*)?$') {
                $ingressIndex = $index
                $ingressIndent = $Matches["indent"]
                break
            }
        }
        if ($ingressIndex -lt 0) {
            throw "cloudflared config has no ingress section."
        }
        $ruleIndent = "${ingressIndent}  "
        $lines.Insert(
            $ingressIndex + 1,
            "${ruleIndent}- hostname: $PublicHostName"
        )
        $lines.Insert(
            $ingressIndex + 2,
            "${ruleIndent}  service: $OriginUrl"
        )
    }

    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupPath = "${configPath}.tmod-backup-${timestamp}"
    Copy-Item -LiteralPath $configPath -Destination $backupPath -Force

    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($configPath, $lines, $utf8WithoutBom)

    $validationOutput = & $Context.ExecutablePath `
        tunnel `
        --config $configPath `
        ingress validate 2>&1
    if ($LASTEXITCODE -ne 0) {
        Copy-Item -LiteralPath $backupPath -Destination $configPath -Force
        throw (
            "cloudflared rejected the updated config. Original restored. " +
            ($validationOutput -join " ")
        )
    }

    try {
        Restart-Service -Name $Context.ServiceName -Force
        $service = Get-Service -Name $Context.ServiceName
        $service.WaitForStatus(
            [ServiceProcess.ServiceControllerStatus]::Running,
            [TimeSpan]::FromSeconds(30)
        )
    }
    catch {
        Copy-Item -LiteralPath $backupPath -Destination $configPath -Force
        Restart-Service -Name $Context.ServiceName -Force -ErrorAction SilentlyContinue
        throw "cloudflared restart failed; original config was restored."
    }

    Write-Host "  [OK] cloudflared config backup: $backupPath"
}

function Ensure-CloudflareDnsRoute {
    param([Parameter(Mandatory = $true)]$Context)

    if (Resolve-DnsName $PublicHostName -ErrorAction SilentlyContinue) {
        Write-Host "  [OK] Cloudflare DNS route already resolves."
        return
    }

    $configText = Get-Content -LiteralPath $Context.ConfigPath -Raw -Encoding UTF8
    if ($configText -notmatch '(?m)^\s*tunnel:\s*["'']?([^"'']+?)["'']?\s*$') {
        Write-Warning "Tunnel ID was not found; add the hostname route in Cloudflare Dashboard."
        return
    }
    $tunnelIdentifier = $Matches[1].Trim()
    $routeArguments = New-Object System.Collections.Generic.List[string]
    $routeArguments.Add("tunnel")
    $originCertificate = Join-Path `
        (Split-Path -Parent $Context.ConfigPath) `
        "cert.pem"
    if (Test-Path -LiteralPath $originCertificate) {
        $routeArguments.Add("--origincert")
        $routeArguments.Add($originCertificate)
    }
    $routeArguments.Add("route")
    $routeArguments.Add("dns")
    $routeArguments.Add($tunnelIdentifier)
    $routeArguments.Add($PublicHostName)
    $routeOutput = & $Context.ExecutablePath @routeArguments 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [OK] Cloudflare DNS route created."
        return
    }
    Write-Warning (
        "DNS route could not be created automatically. Add Published application " +
        "'$PublicHostName -> $OriginUrl' in Cloudflare Dashboard. " +
        ($routeOutput -join " ")
    )
}

$alreadyConfigured = (
    (Test-EnvConfiguration) -and
    (Test-CloudflareConfiguration)
)

if (!$alreadyConfigured -and !(Test-IsAdministrator)) {
    Write-Host "  [INFO] Cloudflare configuration needs administrator approval once."
    $arguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" " +
        "-TargetEnvPath `"$TargetEnvPath`" " +
        "-PublicHostName `"$PublicHostName`" " +
        "-OriginUrl `"$OriginUrl`""
    )
    try {
        $process = Start-Process `
            -FilePath "powershell.exe" `
            -ArgumentList $arguments `
            -Verb RunAs `
            -Wait `
            -PassThru
        exit $process.ExitCode
    }
    catch {
        Write-Error "Administrator approval was cancelled or failed: $($_.Exception.Message)"
        exit 1
    }
}

$context = Get-CloudflaredContext
$route = Get-RouteState -ConfigPath $context.ConfigPath
if (!$route.Found -or $route.Service -ne $OriginUrl) {
    Set-CloudflareRoute -Context $context
    $context = Get-CloudflaredContext
}
elseif ($context.ServiceState -ne "Running") {
    Start-Service -Name $context.ServiceName
    (Get-Service -Name $context.ServiceName).WaitForStatus(
        [ServiceProcess.ServiceControllerStatus]::Running,
        [TimeSpan]::FromSeconds(30)
    )
}

Set-EnvConfiguration
Ensure-CloudflareDnsRoute -Context $context

Get-NetFirewallRule -DisplayName $legacyFirewallRules -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue

Write-Host "  [OK] Consensus domain: $publicUrl"
Write-Host "  [OK] Tunnel origin: $OriginUrl"
Write-Host "  [OK] Other cloudflared ingress routes were preserved."
