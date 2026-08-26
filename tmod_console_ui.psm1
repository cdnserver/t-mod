Set-StrictMode -Version 2.0

function Get-TModTheme {
    param([string]$Name = "aurora")
    $themes = @{
        aurora = @{ Accent = [ConsoleColor]::Cyan; Accent2 = [ConsoleColor]::Blue; Glow = [ConsoleColor]::DarkCyan; Good = [ConsoleColor]::Green; Warn = [ConsoleColor]::Yellow; Bad = [ConsoleColor]::Red }
        reactor = @{ Accent = [ConsoleColor]::Green; Accent2 = [ConsoleColor]::DarkGreen; Glow = [ConsoleColor]::DarkCyan; Good = [ConsoleColor]::Green; Warn = [ConsoleColor]::Yellow; Bad = [ConsoleColor]::Red }
        atlas = @{ Accent = [ConsoleColor]::Blue; Accent2 = [ConsoleColor]::Magenta; Glow = [ConsoleColor]::DarkBlue; Good = [ConsoleColor]::Cyan; Warn = [ConsoleColor]::Yellow; Bad = [ConsoleColor]::Red }
        ember = @{ Accent = [ConsoleColor]::Yellow; Accent2 = [ConsoleColor]::DarkRed; Glow = [ConsoleColor]::DarkYellow; Good = [ConsoleColor]::Green; Warn = [ConsoleColor]::Yellow; Bad = [ConsoleColor]::Red }
    }
    $key = $Name.ToLowerInvariant()
    if (-not $themes.ContainsKey($key)) { $key = "aurora" }
    $theme = $themes[$key]
    $theme.Name = $key
    return $theme
}

function Initialize-TModConsole {
    param([string]$Title = "T-Mod Control")
    try {
        [Console]::InputEncoding = New-Object System.Text.UTF8Encoding
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
        [Console]::Title = $Title
        [Console]::CursorVisible = $false
        if ([Console]::WindowWidth -lt 104 -and [Console]::LargestWindowWidth -ge 104) {
            [Console]::SetWindowSize(104, [Math]::Min([Console]::LargestWindowHeight, [Math]::Max(30, [Console]::WindowHeight)))
        }
    }
    catch {}
}

function Get-TModConsoleWidth {
    try { return [Math]::Max(78, [Math]::Min(118, [Console]::WindowWidth - 1)) }
    catch { return 96 }
}

function Write-TModRule {
    param([hashtable]$Theme, [string]$Character = "─")
    if (-not $Theme) { $Theme = Get-TModTheme }
    Write-Host ($Character * (Get-TModConsoleWidth)) -ForegroundColor $Theme.Glow
}

function Write-TModLogo {
    param([hashtable]$Theme)
    if (-not $Theme) { $Theme = Get-TModTheme }
    Write-Host "  ████████╗      ███╗   ███╗ ██████╗ ██████╗ " -ForegroundColor $Theme.Accent
    Write-Host "  ╚══██╔══╝      ████╗ ████║██╔═══██╗██╔══██╗" -ForegroundColor $Theme.Accent
    Write-Host "     ██║   █████╗██╔████╔██║██║   ██║██║  ██║" -ForegroundColor White
    Write-Host "     ██║   ╚════╝██║╚██╔╝██║██║   ██║██║  ██║" -ForegroundColor White
    Write-Host "     ██║         ██║ ╚═╝ ██║╚██████╔╝██████╔╝" -ForegroundColor $Theme.Glow
    Write-Host "     ╚═╝         ╚═╝     ╚═╝ ╚═════╝ ╚═════╝ " -ForegroundColor $Theme.Glow
}

function Write-TModBadge {
    param(
        [string]$Text,
        [ValidateSet("good", "warn", "bad", "info", "muted")][string]$Kind = "info",
        [hashtable]$Theme,
        [switch]$NoNewline
    )
    if (-not $Theme) { $Theme = Get-TModTheme }
    $color = switch ($Kind) {
        "good" { $Theme.Good }
        "warn" { $Theme.Warn }
        "bad" { $Theme.Bad }
        "muted" { [ConsoleColor]::DarkGray }
        default { $Theme.Accent }
    }
    Write-Host (" {0} " -f $Text.ToUpperInvariant()) -ForegroundColor Black -BackgroundColor $color -NoNewline:$NoNewline
}

function Write-TModHeader {
    param(
        [string]$Section = "CONTROL CENTER",
        [string]$Version = "1.0.0",
        [string]$Context = "",
        [hashtable]$Theme
    )
    if (-not $Theme) { $Theme = Get-TModTheme }
    Write-Host ""
    Write-TModLogo -Theme $Theme
    Write-Host ""
    Write-Host ("  {0}" -f $Section) -NoNewline -ForegroundColor White
    Write-Host ("  /  v{0}" -f $Version) -NoNewline -ForegroundColor DarkGray
    if ($Context) { Write-Host ("  /  {0}" -f $Context) -ForegroundColor $Theme.Glow } else { Write-Host "" }
    Write-TModRule -Theme $Theme
}

function Show-TModIntro {
    param([hashtable]$Theme, [switch]$Disabled, [string]$Mode = "CONTROL CENTER")
    if ($Disabled -or [Console]::IsOutputRedirected) { return }
    if (-not $Theme) { $Theme = Get-TModTheme }
    try { [Console]::CursorVisible = $false } catch {}
    $frames = @(
        @{ Star = "                                      ·"; Title = "" },
        @{ Star = "                                ·    ╱"; Title = "" },
        @{ Star = "                         ✦─────────╱"; Title = "T — M O D" },
        @{ Star = "              ────────────◈────────────"; Title = $Mode },
        @{ Star = "           SYSTEM FABRIC SYNCHRONIZED"; Title = "READY" }
    )
    foreach ($frame in $frames) {
        Clear-Host
        Write-Host ""; Write-Host ""; Write-Host ""; Write-Host ""
        Write-Host ("  {0}" -f $frame.Star) -ForegroundColor $Theme.Accent
        Write-Host ""
        Write-Host ("  {0}" -f $frame.Title) -ForegroundColor White
        Start-Sleep -Milliseconds 115
    }
    Start-Sleep -Milliseconds 130
}

function Show-TModTransition {
    param([string]$Label, [hashtable]$Theme, [switch]$Disabled)
    if ($Disabled -or [Console]::IsOutputRedirected) { return }
    if (-not $Theme) { $Theme = Get-TModTheme }
    $glyphs = @("·", "∙", "◦", "○", "◌", "◉")
    foreach ($glyph in $glyphs) {
        Write-Host ("`r  {0}  {1}   " -f $glyph, $Label) -NoNewline -ForegroundColor $Theme.Accent
        Start-Sleep -Milliseconds 42
    }
    Write-Host ""
}

function Write-TModMetric {
    param(
        [string]$Label,
        [string]$Value,
        [ValidateSet("good", "warn", "bad", "info", "muted")][string]$Kind = "info",
        [hashtable]$Theme,
        [int]$Width = 22
    )
    if (-not $Theme) { $Theme = Get-TModTheme }
    Write-Host ("  {0,-$Width}" -f $Label) -NoNewline -ForegroundColor DarkGray
    $color = switch ($Kind) {
        "good" { $Theme.Good }; "warn" { $Theme.Warn }; "bad" { $Theme.Bad }; "muted" { [ConsoleColor]::DarkGray }; default { $Theme.Accent }
    }
    Write-Host $Value -ForegroundColor $color
}

function Write-TModCardRow {
    param([object[]]$Cards, [hashtable]$Theme)
    if (-not $Theme) { $Theme = Get-TModTheme }
    foreach ($card in $Cards) {
        $kind = if ($card.Kind) { [string]$card.Kind } else { "info" }
        Write-Host "  ┌─ " -NoNewline -ForegroundColor $Theme.Glow
        Write-Host ([string]$card.Title) -NoNewline -ForegroundColor White
        Write-Host "  " -NoNewline
        Write-TModBadge -Text ([string]$card.Value) -Kind $kind -Theme $Theme -NoNewline
        Write-Host "  ─┐  " -NoNewline -ForegroundColor $Theme.Glow
    }
    Write-Host ""
}

function Select-TModMenu {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][object[]]$Items,
        [int]$InitialIndex = 0,
        [string]$Subtitle = "↑ ↓ выбрать  ·  Enter открыть  ·  Esc назад",
        [scriptblock]$Header,
        [scriptblock]$OnRender,
        [hashtable]$Hotkeys = @{},
        [hashtable]$Theme,
        [string]$Footer = ""
    )
    if (-not $Theme) { $Theme = Get-TModTheme }
    if ($Items.Count -eq 0) { return $null }
    $index = [Math]::Max(0, [Math]::Min($InitialIndex, $Items.Count - 1))
    try { [Console]::CursorVisible = $false } catch {}
    while ($true) {
        Clear-Host
        if ($Header) { & $Header }
        Write-Host ("  {0}" -f $Title) -ForegroundColor White
        Write-Host ("  {0}" -f $Subtitle) -ForegroundColor DarkGray
        if ($OnRender) { Write-Host ""; & $OnRender }
        Write-Host ""
        for ($itemIndex = 0; $itemIndex -lt $Items.Count; $itemIndex++) {
            $item = $Items[$itemIndex]
            $label = [string]$item.Label
            $hint = [string]$item.Hint
            if ($itemIndex -eq $index) {
                Write-Host "  › " -NoNewline -ForegroundColor $Theme.Accent
                Write-Host (" {0} " -f $label) -NoNewline -ForegroundColor Black -BackgroundColor $Theme.Accent
                if ($hint) { Write-Host ("  {0}" -f $hint) -ForegroundColor $Theme.Glow } else { Write-Host "" }
            }
            else {
                Write-Host ("    {0}" -f $label) -ForegroundColor Gray
                if ($hint) { Write-Host ("      {0}" -f $hint) -ForegroundColor DarkGray }
            }
        }
        if ($Footer) {
            Write-Host ""
            Write-TModRule -Theme $Theme -Character "·"
            Write-Host ("  {0}" -f $Footer) -ForegroundColor DarkGray
        }
        $key = [Console]::ReadKey($true)
        if ($Hotkeys.ContainsKey([string]$key.Key)) { return $Hotkeys[[string]$key.Key] }
        switch ($key.Key) {
            "UpArrow" { $index = if ($index -le 0) { $Items.Count - 1 } else { $index - 1 } }
            "DownArrow" { $index = if ($index -ge $Items.Count - 1) { 0 } else { $index + 1 } }
            "PageUp" { $index = [Math]::Max(0, $index - 5) }
            "PageDown" { $index = [Math]::Min($Items.Count - 1, $index + 5) }
            "Home" { $index = 0 }
            "End" { $index = $Items.Count - 1 }
            "Enter" { return $Items[$index].Value }
            "Escape" { return $null }
        }
    }
}

function Wait-TModKey {
    param([string]$Message = "Нажмите любую клавишу, чтобы вернуться", [hashtable]$Theme)
    if ([Console]::IsInputRedirected) { return }
    if (-not $Theme) { $Theme = Get-TModTheme }
    Write-Host ""
    Write-Host ("  {0}" -f $Message) -ForegroundColor $Theme.Glow
    [Console]::ReadKey($true) | Out-Null
}

Export-ModuleMember -Function Get-TModTheme, Initialize-TModConsole, Get-TModConsoleWidth, Write-TModRule, Write-TModLogo, Write-TModBadge, Write-TModHeader, Show-TModIntro, Show-TModTransition, Write-TModMetric, Write-TModCardRow, Select-TModMenu, Wait-TModKey
