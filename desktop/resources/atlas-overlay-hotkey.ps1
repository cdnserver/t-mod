param(
    [Parameter(Mandatory = $true)]
    [string]$Hotkey,
    [ValidateRange(8, 100)]
    [int]$PollMilliseconds = 12
)

$ErrorActionPreference = "Stop"

function Fail([string]$Code) {
    [Console]::Out.WriteLine("error:$Code")
    [Console]::Out.Flush()
    exit 2
}

try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class TModAtlasHotkeyState {
    [DllImport("user32.dll")]
    public static extern short GetAsyncKeyState(int virtualKey);
}
"@

    $tokens = @($Hotkey.Split('+') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($tokens.Count -lt 1 -or $tokens.Count -gt 4) { Fail "invalid_hotkey" }

    $modifierMap = @{
        "control" = 0x11; "ctrl" = 0x11; "commandorcontrol" = 0x11; "cmdorctrl" = 0x11
        "shift" = 0x10
        "alt" = 0x12; "option" = 0x12; "altgr" = 0x12
        "super" = 0x5B; "command" = 0x5B; "cmd" = 0x5B
    }
    $keyMap = @{
        "space" = 0x20; "tab" = 0x09; "insert" = 0x2D; "delete" = 0x2E
        "home" = 0x24; "end" = 0x23; "pageup" = 0x21; "pagedown" = 0x22
        "up" = 0x26; "down" = 0x28; "left" = 0x25; "right" = 0x27
    }

    $keys = New-Object System.Collections.Generic.List[int]
    foreach ($modifier in $tokens[0..([Math]::Max(0, $tokens.Count - 2))]) {
        if ($tokens.Count -eq 1) { break }
        $normalized = $modifier.ToLowerInvariant()
        if (-not $modifierMap.ContainsKey($normalized)) { Fail "invalid_modifier" }
        $keys.Add([int]$modifierMap[$normalized])
    }

    $primary = $tokens[$tokens.Count - 1].ToLowerInvariant()
    if ($keyMap.ContainsKey($primary)) {
        $keys.Add([int]$keyMap[$primary])
    } elseif ($primary -match '^f([1-9]|1[0-9]|2[0-4])$') {
        $keys.Add(0x6F + [int]$Matches[1])
    } elseif ($primary -match '^[a-z0-9]$') {
        $keys.Add([int][char]$primary.ToUpperInvariant())
    } else {
        Fail "invalid_primary_key"
    }

    # Pressing the configured PTT chord plus Space opens Atlas' manual query
    # surface. It deliberately works as a second mode of the same binding, so
    # it never steals a normal GTA key or requires a second global shortcut.
    $manualPromptSupported = -not $keys.Contains(0x20)
    $pressed = $false
    $manualPrompt = $false
    while ($true) {
        $allDown = $true
        foreach ($virtualKey in $keys) {
            if (([TModAtlasHotkeyState]::GetAsyncKeyState($virtualKey) -band 0x8000) -eq 0) {
                $allDown = $false
                break
            }
        }
        $spaceDown = $manualPromptSupported -and (([TModAtlasHotkeyState]::GetAsyncKeyState(0x20) -band 0x8000) -ne 0)
        if ($allDown -and $spaceDown -and -not $manualPrompt) {
            $manualPrompt = $true
            if ($pressed) {
                $pressed = $false
                [Console]::Out.WriteLine("cancel")
                [Console]::Out.Flush()
            }
            [Console]::Out.WriteLine("text")
            [Console]::Out.Flush()
        } elseif ($allDown -and -not $pressed -and -not $manualPrompt) {
            $pressed = $true
            [Console]::Out.WriteLine("down")
            [Console]::Out.Flush()
        } elseif (-not $allDown -and $pressed) {
            $pressed = $false
            [Console]::Out.WriteLine("up")
            [Console]::Out.Flush()
        } elseif (-not $allDown -and $manualPrompt) {
            $manualPrompt = $false
        }
        Start-Sleep -Milliseconds $PollMilliseconds
    }
} catch {
    Fail "runtime_failure"
}
