param(
    [string]$ExamplePath,
    [string]$TargetPath
)

if (!(Test-Path $ExamplePath)) { exit 0 }
if (!(Test-Path $TargetPath)) {
    Copy-Item $ExamplePath $TargetPath -Force
    exit 0
}

$targetText = Get-Content -Raw -Encoding UTF8 $TargetPath
$existing = @{}
foreach ($line in ($targetText -split "`r?`n")) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $existing[$Matches[1]] = $true
    }
}

$toAppend = New-Object System.Collections.Generic.List[string]
foreach ($line in (Get-Content -Encoding UTF8 $ExamplePath)) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $key = $Matches[1]
        if (!$existing.ContainsKey($key)) {
            $toAppend.Add($line)
            $existing[$key] = $true
        }
    }
}

if ($toAppend.Count -gt 0) {
    Add-Content -Encoding UTF8 $TargetPath ""
    Add-Content -Encoding UTF8 $TargetPath "# Added by T-Mod update"
    foreach ($line in $toAppend) { Add-Content -Encoding UTF8 $TargetPath $line }
}

# One-time visual migration: old SGL Bureau gold accent becomes light gray.
# User-custom colors are preserved unless they are exactly the old default.
$updatedText = Get-Content -Raw -Encoding UTF8 $TargetPath
if ($updatedText -match '(?m)^SGBUREAU_EMBED_COLOR=0xD4AF37\s*$') {
    $updatedText = $updatedText -replace '(?m)^SGBUREAU_EMBED_COLOR=0xD4AF37\s*$', 'SGBUREAU_EMBED_COLOR=0xD9D9D9'
    Set-Content -Encoding UTF8 $TargetPath $updatedText
}

