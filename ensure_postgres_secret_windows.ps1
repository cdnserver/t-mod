param(
    [Parameter(Mandatory = $true)][string]$SecretPath
)

$ErrorActionPreference = "Stop"
$directory = Split-Path -Parent $SecretPath
New-Item -ItemType Directory -Force -Path $directory | Out-Null
if (Test-Path -LiteralPath $SecretPath) {
    $current = (Get-Content -Raw -LiteralPath $SecretPath).Trim()
    if ($current.Length -ge 32) { exit 0 }
}

$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$secret = -join ($bytes | ForEach-Object { $_.ToString("x2") })
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($SecretPath, $secret, $utf8NoBom)
