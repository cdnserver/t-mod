[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetEnvPath,

    [string]$PublicDomain = "tvr.lat"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$httpsRuleName = "T-Mod Direct HTTPS"
$httpRuleName = "T-Mod ACME HTTP"
$obsoleteRuleNames = @(
    "T-Mod Consensus via WireGuard",
    "T-Mod Consensus local network"
)

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Get-EnvConfiguration {
    $actual = @{}
    if (!(Test-Path -LiteralPath $TargetEnvPath)) {
        return $actual
    }
    foreach ($line in (Get-Content -LiteralPath $TargetEnvPath -Encoding UTF8)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $actual[$Matches[1]] = $Matches[2].Trim()
        }
    }
    return $actual
}

function Test-EnvConfiguration {
    $actual = Get-EnvConfiguration
    $expected = @{
        CONSENSUS_WEB_ENABLED = "true"
        CONSENSUS_WEB_HOST = "0.0.0.0"
        CONSENSUS_WEB_PORT = "8787"
        CONSENSUS_WEB_PUBLIC_NAME = "consensus.${PublicDomain}"
        CONSENSUS_WEB_PUBLIC_URL = "https://consensus.${PublicDomain}"
        REACTOR_WEB_PUBLIC_URL = "https://reactor.${PublicDomain}"
        PORTAL_WEB_PUBLIC_URL = "https://${PublicDomain}"
    }
    foreach ($key in $expected.Keys) {
        if (!$actual.ContainsKey($key) -or $actual[$key] -ne $expected[$key]) {
            return $false
        }
    }
    return $true
}

function Test-FirewallRule {
    param(
        [Parameter(Mandatory = $true)]
        [string]$DisplayName,

        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    try {
        $rule = Get-NetFirewallRule `
            -DisplayName $DisplayName `
            -ErrorAction Stop |
            Where-Object {
                $_.Enabled -eq "True" -and
                $_.Direction -eq "Inbound" -and
                $_.Action -eq "Allow"
            }
        if (!$rule) {
            return $false
        }
        $portFilter = $rule | Get-NetFirewallPortFilter |
            Where-Object {
                $_.Protocol -eq "TCP" -and
                [string]$_.LocalPort -eq [string]$Port
            }
        return [bool]$portFilter
    }
    catch {
        return $false
    }
}

function Test-ObsoleteFirewallRulesAbsent {
    foreach ($ruleName in $obsoleteRuleNames) {
        if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
            return $false
        }
    }
    return $true
}

function Set-EnvConfiguration {
    $expected = [ordered]@{
        CONSENSUS_WEB_ENABLED = "true"
        CONSENSUS_WEB_HOST = "0.0.0.0"
        CONSENSUS_WEB_PORT = "8787"
        CONSENSUS_WEB_PUBLIC_NAME = "consensus.${PublicDomain}"
        CONSENSUS_WEB_PUBLIC_URL = "https://consensus.${PublicDomain}"
        REACTOR_WEB_PUBLIC_URL = "https://reactor.${PublicDomain}"
        PORTAL_WEB_PUBLIC_URL = "https://${PublicDomain}"
    }
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in (Get-Content -LiteralPath $TargetEnvPath -Encoding UTF8)) {
        $lines.Add($line)
    }

    foreach ($key in $expected.Keys) {
        $found = $false
        $pattern = "^\s*" + [regex]::Escape($key) + "\s*="
        for ($index = 0; $index -lt $lines.Count; $index++) {
            if ($lines[$index] -match $pattern) {
                $lines[$index] = "${key}=$($expected[$key])"
                $found = $true
            }
        }
        if (!$found) {
            $lines.Add("${key}=$($expected[$key])")
        }
    }

    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($TargetEnvPath, $lines, $utf8WithoutBom)
}

function Set-FirewallRule {
    param(
        [Parameter(Mandatory = $true)]
        [string]$DisplayName,

        [Parameter(Mandatory = $true)]
        [int]$Port,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule `
        -DisplayName $DisplayName `
        -Description $Description `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -RemoteAddress Any `
        -Profile Any `
        -EdgeTraversalPolicy Block | Out-Null
}

$alreadyConfigured = (
    (Test-EnvConfiguration) -and
    (Test-FirewallRule -DisplayName $httpsRuleName -Port 443) -and
    (Test-FirewallRule -DisplayName $httpRuleName -Port 80) -and
    (Test-ObsoleteFirewallRulesAbsent)
)

if (!$alreadyConfigured -and !(Test-IsAdministrator)) {
    Write-Host "  [INFO] Windows administrator approval is required once."
    $arguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" " +
        "-TargetEnvPath `"$TargetEnvPath`" " +
        "-PublicDomain `"$PublicDomain`""
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

if (!$alreadyConfigured) {
    Set-EnvConfiguration
    Set-FirewallRule `
        -DisplayName $httpsRuleName `
        -Port 443 `
        -Description "Public HTTPS entry for the T-Mod Caddy reverse proxy."
    Set-FirewallRule `
        -DisplayName $httpRuleName `
        -Port 80 `
        -Description "HTTP redirect and ACME certificate validation for T-Mod."
    foreach ($ruleName in $obsoleteRuleNames) {
        Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
            Remove-NetFirewallRule -ErrorAction SilentlyContinue
    }
}

Write-Host "  [OK] Member Reactor: https://${PublicDomain}"
Write-Host "  [OK] Nuclear Reactor: https://reactor.${PublicDomain}"
Write-Host "  [OK] Consensus: https://consensus.${PublicDomain}"
Write-Host "  [OK] Zigmund: https://zigmund.${PublicDomain}"
Write-Host "  [OK] T-Mod firewall rules allow public web ports 80/TCP and 443/TCP."
Write-Host "  [OK] Internal T-Mod port 8787 remains bound to localhost."
