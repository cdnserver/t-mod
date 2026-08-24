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
    $editModeSupported = -not $keys.Contains(0x09)
    $pressed = $false
    $manualPrompt = $false
    $editChord = $false
    $editMode = $false
    $editKeys = @{
        "move:left" = 0x25; "move:up" = 0x26; "move:right" = 0x27; "move:down" = 0x28
        "scale:up" = 0x6B; "scale:down" = 0x6D
        "width:up" = 0xDD; "width:down" = 0xDB
    }
    $editPressed = @{}
    foreach ($command in $editKeys.Keys) { $editPressed[$command] = $false }
    $finishPressed = $false
    while ($true) {
        $allDown = $true
        foreach ($virtualKey in $keys) {
            if (([TModAtlasHotkeyState]::GetAsyncKeyState($virtualKey) -band 0x8000) -eq 0) {
                $allDown = $false
                break
            }
        }
        $spaceDown = $manualPromptSupported -and (([TModAtlasHotkeyState]::GetAsyncKeyState(0x20) -band 0x8000) -ne 0)
        $tabDown = $editModeSupported -and (([TModAtlasHotkeyState]::GetAsyncKeyState(0x09) -band 0x8000) -ne 0)
        if ($editMode) {
            foreach ($command in $editKeys.Keys) {
                $down = ([TModAtlasHotkeyState]::GetAsyncKeyState([int]$editKeys[$command]) -band 0x8000) -ne 0
                if ($down -and -not $editPressed[$command]) {
                    $editPressed[$command] = $true
                    [Console]::Out.WriteLine($command)
                    [Console]::Out.Flush()
                } elseif (-not $down) {
                    $editPressed[$command] = $false
                }
            }
            # OEM +/- are accepted in addition to the numeric keypad.
            $oemPlus = ([TModAtlasHotkeyState]::GetAsyncKeyState(0xBB) -band 0x8000) -ne 0
            $oemMinus = ([TModAtlasHotkeyState]::GetAsyncKeyState(0xBD) -band 0x8000) -ne 0
            if ($oemPlus -and -not $editPressed["oem-plus"]) {
                $editPressed["oem-plus"] = $true
                [Console]::Out.WriteLine("scale:up")
                [Console]::Out.Flush()
            } elseif (-not $oemPlus) { $editPressed["oem-plus"] = $false }
            if ($oemMinus -and -not $editPressed["oem-minus"]) {
                $editPressed["oem-minus"] = $true
                [Console]::Out.WriteLine("scale:down")
                [Console]::Out.Flush()
            } elseif (-not $oemMinus) { $editPressed["oem-minus"] = $false }

            $finishDown = (([TModAtlasHotkeyState]::GetAsyncKeyState(0x0D) -band 0x8000) -ne 0) -or (([TModAtlasHotkeyState]::GetAsyncKeyState(0x1B) -band 0x8000) -ne 0)
            if ($finishDown -and -not $finishPressed) {
                $finishPressed = $true
                $editMode = $false
                [Console]::Out.WriteLine("edit-done")
                [Console]::Out.Flush()
            } elseif (-not $finishDown) { $finishPressed = $false }
        } elseif ($allDown -and $tabDown -and -not $editChord) {
            $editChord = $true
            if ($pressed) {
                $pressed = $false
                [Console]::Out.WriteLine("cancel")
                [Console]::Out.Flush()
            }
            $editMode = $true
            [Console]::Out.WriteLine("edit")
            [Console]::Out.Flush()
        } elseif ($allDown -and $spaceDown -and -not $manualPrompt) {
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
        } elseif (-not $allDown -and $editChord) {
            $editChord = $false
        }
        Start-Sleep -Milliseconds $PollMilliseconds
    }
} catch {
    Fail "runtime_failure"
}
