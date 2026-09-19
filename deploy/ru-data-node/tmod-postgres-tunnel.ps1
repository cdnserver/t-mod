param(
    [string]$RemoteHost = "135.106.222.199",
    [int]$RemotePort = 22,
    [string]$RemoteUser = "root",
    [string]$IdentityFile = "$env:USERPROFILE\.ssh\sigma",
    [int]$LocalPort = 55432
)

$ErrorActionPreference = "Stop"
$persistentRoot = $env:TMOD_PERSISTENT_DIR
if ([string]::IsNullOrWhiteSpace($persistentRoot)) {
    $persistentRoot = "$env:USERPROFILE\Documents\SGLDiscordBot"
}
$stateRoot = Join-Path $persistentRoot "runtime"
New-Item -ItemType Directory -Force $stateRoot | Out-Null
$logPath = Join-Path $stateRoot "postgres-ru-tunnel.log"

while ($true) {
    $arguments = @(
        "-N", "-T",
        "-i", $IdentityFile,
        "-p", [string]$RemotePort,
        "-L", "127.0.0.1:${LocalPort}:127.0.0.1:5432",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=yes",
        "${RemoteUser}@${RemoteHost}"
    )
    "$(Get-Date -Format o) starting tunnel" | Add-Content -Encoding UTF8 $logPath
    & ssh.exe @arguments 2>&1 | Add-Content -Encoding UTF8 $logPath
    "$(Get-Date -Format o) tunnel exited with $LASTEXITCODE; retrying" | Add-Content -Encoding UTF8 $logPath
    Start-Sleep -Seconds 5
}
