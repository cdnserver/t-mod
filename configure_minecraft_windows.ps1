[CmdletBinding()]
param(
    [string]$ServerHostName = "mc.tvr.lat",
    [int]$Port = 25565
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ruleName = "T-Mod Minecraft Server"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Test-FirewallRule {
    try {
        $rule = Get-NetFirewallRule `
            -DisplayName $ruleName `
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

if (!(Test-FirewallRule) -and !(Test-IsAdministrator)) {
    Write-Host "  [INFO] Windows administrator approval is required once for Minecraft."
    $arguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" " +
        "-ServerHostName `"$ServerHostName`" " +
        "-Port $Port"
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

if (!(Test-FirewallRule)) {
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Description "Public Minecraft Java server entry for ${ServerHostName}." `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -RemoteAddress Any `
        -Profile Any `
        -EdgeTraversalPolicy Block | Out-Null
}

Write-Host "  [OK] Minecraft public address: ${ServerHostName}"
Write-Host "  [OK] Windows Firewall allows public Minecraft port ${Port}/TCP."
Write-Host "  [OK] DNS and router forwarding must point ${ServerHostName} to this server separately."
