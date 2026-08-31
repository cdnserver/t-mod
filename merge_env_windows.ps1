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
$originalText = $updatedText
if ($updatedText -match '(?m)^SGBUREAU_EMBED_COLOR=0xD4AF37[ \t]*\r?$') {
    $updatedText = $updatedText -replace '(?m)^SGBUREAU_EMBED_COLOR=0xD4AF37[ \t]*\r?$', 'SGBUREAU_EMBED_COLOR=0xD9D9D9'
}

# One-time voice latency migration. Only exact former defaults are replaced;
# deliberate custom values stay untouched.
$updatedText = $updatedText -replace '(?m)^MUSIC_STT_MODEL=openai/whisper-large-v3[ \t]*\r?$', 'MUSIC_STT_MODEL=openai/gpt-4o-mini-transcribe'
$updatedText = $updatedText -replace '(?m)^MUSIC_STT_MODEL=openai/gpt-4o-mini-transcribe[ \t]*\r?$', 'MUSIC_STT_MODEL=qwen/qwen3-asr-flash-2026-02-10'
$updatedText = $updatedText -replace '(?m)^MUSIC_STT_FALLBACK_MODELS=qwen/qwen3-asr-flash-2026-02-10,openai/gpt-4o-transcribe[ \t]*\r?$', 'MUSIC_STT_FALLBACK_MODELS=openai/gpt-4o-mini-transcribe,openai/gpt-4o-transcribe'
$updatedText = $updatedText -replace '(?m)^MUSIC_SPEECH_SILENCE_SECONDS=0\.9[ \t]*\r?$', 'MUSIC_SPEECH_SILENCE_SECONDS=0.5'
$updatedText = $updatedText -replace '(?m)^MUSIC_SPEECH_SILENCE_SECONDS=0\.6[ \t]*\r?$', 'MUSIC_SPEECH_SILENCE_SECONDS=0.5'
$updatedText = $updatedText -replace '(?m)^MUSIC_STT_TIMEOUT_SECONDS=35[ \t]*\r?$', 'MUSIC_STT_TIMEOUT_SECONDS=8'
$updatedText = $updatedText -replace '(?m)^MUSIC_STT_TIMEOUT_SECONDS=15[ \t]*\r?$', 'MUSIC_STT_TIMEOUT_SECONDS=8'
$updatedText = $updatedText -replace '(?m)^MUSIC_STT_QUEUE_LIMIT=4[ \t]*\r?$', 'MUSIC_STT_QUEUE_LIMIT=8'
if ($updatedText -ne $originalText) {
    Set-Content -Encoding UTF8 $TargetPath $updatedText
}
