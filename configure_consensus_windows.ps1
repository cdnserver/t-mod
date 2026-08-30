[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetEnvPath,

    [string]$WireGuardSubnet = "10.8.0.0/24",
    [int]$Port = 8787,
    [string]$ConsensusHostName = "t.consensus",
    [string]$AddressOutputPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$wireGuardRuleName = "T-Mod Consensus via WireGuard"
$localRuleName = "T-Mod Consensus local network"
$hostsPath = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-EnvConfiguration {
    if (!(Test-Path -LiteralPath $TargetEnvPath)) {
        return $false
    }

    $expected = @{
        CONSENSUS_WEB_ENABLED = "true"
        CONSENSUS_WEB_HOST = "0.0.0.0"
        CONSENSUS_WEB_PORT = [string]$Port
        CONSENSUS_WEB_PUBLIC_NAME = $ConsensusHostName
    }
    $actual = @{}
    foreach ($line in (Get-Content -LiteralPath $TargetEnvPath -Encoding UTF8)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $actual[$Matches[1]] = $Matches[2].Trim()
        }
    }
    foreach ($key in $expected.Keys) {
        if (!$actual.ContainsKey($key) -or $actual[$key] -ne $expected[$key]) {
            return $false
        }
    }
    return $true
}

function Test-HostsConfiguration {
    if (!(Test-Path -LiteralPath $hostsPath)) {
        return $false
    }
    $escapedHostName = [regex]::Escape($ConsensusHostName)
    return [bool](Select-String `
        -LiteralPath $hostsPath `
        -Pattern "^\s*127\.0\.0\.1\s+${escapedHostName}(?:\s|$)" `
        -Quiet)
}

function Test-FirewallConfiguration {
    try {
        $wireGuardRule = Get-NetFirewallRule `
            -DisplayName $wireGuardRuleName `
            -ErrorAction Stop |
            Where-Object { $_.Enabled -eq "True" -and $_.Direction -eq "Inbound" }
        $localRule = Get-NetFirewallRule `
            -DisplayName $localRuleName `
            -ErrorAction Stop |
            Where-Object {
                $_.Enabled -eq "True" -and
                $_.Direction -eq "Inbound" -and
                $_.Profile -eq "Any"
            }
        if (!$wireGuardRule -or !$localRule) {
            return $false
        }

        $wireGuardPort = $wireGuardRule | Get-NetFirewallPortFilter |
            Where-Object { $_.Protocol -eq "TCP" -and [string]$_.LocalPort -eq [string]$Port }
        $localPort = $localRule | Get-NetFirewallPortFilter |
            Where-Object { $_.Protocol -eq "TCP" -and [string]$_.LocalPort -eq [string]$Port }
        return [bool]$wireGuardPort -and [bool]$localPort
    }
    catch {
        return $false
    }
}

function Set-EnvConfiguration {
    $expected = [ordered]@{
        CONSENSUS_WEB_ENABLED = "true"
        CONSENSUS_WEB_HOST = "0.0.0.0"
        CONSENSUS_WEB_PORT = [string]$Port
        CONSENSUS_WEB_PUBLIC_NAME = $ConsensusHostName
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
        [string]$RemoteAddress,

        [Parameter(Mandatory = $true)]
        [string]$Profile
    )

    Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue

    New-NetFirewallRule `
        -DisplayName $DisplayName `
        -Description "Allows the private T-Mod consensus dashboard only from trusted local networks." `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -RemoteAddress $RemoteAddress `
        -Profile $Profile `
        -EdgeTraversalPolicy Block | Out-Null
}

function Set-HostsConfiguration {
    $escapedHostName = [regex]::Escape($ConsensusHostName)
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in (Get-Content -LiteralPath $hostsPath -Encoding UTF8)) {
        if (
            !$line.TrimStart().StartsWith("#") -and
            $line -match "(?i)(?:^|\s)${escapedHostName}(?:\s|$)"
        ) {
            continue
        }
        $lines.Add($line)
    }
    $lines.Add("127.0.0.1`t$ConsensusHostName")

    $ascii = [Text.Encoding]::ASCII
    [IO.File]::WriteAllLines($hostsPath, $lines, $ascii)
    Clear-DnsClientCache | Out-Null
}

function Get-PrimaryLanAddress {
    try {
        return Get-NetIPConfiguration |
            Where-Object {
                $_.NetAdapter.Status -eq "Up" -and
                $null -ne $_.IPv4DefaultGateway
            } |
            ForEach-Object { $_.IPv4Address.IPAddress } |
            Where-Object {
                $_ -and
                $_ -ne "127.0.0.1" -and
                $_ -notlike "169.254.*"
            } |
            Select-Object -First 1
    }
    catch {
        return $null
    }
}

$alreadyConfigured = (
    (Test-EnvConfiguration) -and
    (Test-HostsConfiguration) -and
    (Test-FirewallConfiguration)
)

if (!$alreadyConfigured -and !(Test-IsAdministrator)) {
    Write-Host "  [INFO] Windows administrator approval is required once."
    $arguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" " +
        "-TargetEnvPath `"$TargetEnvPath`" " +
        "-WireGuardSubnet `"$WireGuardSubnet`" " +
        "-Port $Port " +
        "-ConsensusHostName `"$ConsensusHostName`""
    )
    if ($AddressOutputPath) {
        $arguments += " -AddressOutputPath `"$AddressOutputPath`""
    }
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
        -DisplayName $wireGuardRuleName `
        -RemoteAddress $WireGuardSubnet `
        -Profile "Any"
    Set-FirewallRule `
        -DisplayName $localRuleName `
        -RemoteAddress "LocalSubnet" `
        -Profile "Any"
    Set-HostsConfiguration
    Write-Host "  [OK] Consensus network access configured."
}
else {
    Write-Host "  [OK] Consensus network access is already configured."
}

$lanAddress = Get-PrimaryLanAddress
Write-Host "  [OK] Server address: http://${ConsensusHostName}:$Port"
if ($lanAddress) {
    if ($AddressOutputPath) {
        $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText(
            $AddressOutputPath,
            [string]$lanAddress,
            $utf8WithoutBom
        )
    }
    Write-Host "  [OK] LAN/WireGuard address: http://${lanAddress}:$Port"
}
elseif ($AddressOutputPath -and (Test-Path -LiteralPath $AddressOutputPath)) {
    Remove-Item -LiteralPath $AddressOutputPath -Force
}
Write-Host "  [OK] WireGuard clients allowed: $WireGuardSubnet"
