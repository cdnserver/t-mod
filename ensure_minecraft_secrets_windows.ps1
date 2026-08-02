param(
    [Parameter(Mandatory = $true)]
    [string]$RconPath,

    [Parameter(Mandatory = $true)]
    [string]$SupervisorPath,

    [string]$ChangedMarkerPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:SecretChanged = $false

if (-not [string]::IsNullOrWhiteSpace($ChangedMarkerPath) -and
    (Test-Path -LiteralPath $ChangedMarkerPath)) {
    Remove-Item -LiteralPath $ChangedMarkerPath -Force
}

function Ensure-SecretFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $parent = [IO.Path]::GetDirectoryName($Path)
    if ([string]::IsNullOrWhiteSpace($parent)) {
        throw "$Label secret path has no parent directory: $Path"
    }
    [IO.Directory]::CreateDirectory($parent) | Out-Null

    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if ($item.PSIsContainer) {
            $children = @(Get-ChildItem -LiteralPath $Path -Force)
            if ($children.Count -gt 0) {
                throw "$Label secret path is a non-empty directory: $Path"
            }
            Remove-Item -LiteralPath $Path -Force
            Write-Host "[WARN] Repaired an empty directory left at the $Label secret path"
        }
        else {
            $existing = [IO.File]::ReadAllText($Path).Trim()
            if ($existing -match '\A[0-9a-fA-F]{64}\z') {
                Write-Host "[OK] $Label secret is valid"
                return
            }
            Write-Host "[WARN] Replacing an empty or invalid $Label secret"
        }
    }

    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    $secret = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    $temporaryPath = "$Path.tmp-$PID-$([Guid]::NewGuid().ToString('N'))"
    try {
        [IO.File]::WriteAllText(
            $temporaryPath,
            $secret,
            (New-Object Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
    Write-Host "[OK] Generated a new $Label secret outside Git"
    $script:SecretChanged = $true
}

Ensure-SecretFile -Path $RconPath -Label "Minecraft RCON"
Ensure-SecretFile -Path $SupervisorPath -Label "Minecraft supervisor"

if ($script:SecretChanged -and -not [string]::IsNullOrWhiteSpace($ChangedMarkerPath)) {
    [IO.File]::WriteAllText(
        $ChangedMarkerPath,
        "changed",
        (New-Object Text.UTF8Encoding($false))
    )
}
