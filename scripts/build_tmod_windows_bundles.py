#!/usr/bin/env python3
"""Build the two portable, single-file Windows control consoles."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_SOURCE = ROOT / "tmod_console_ui.psm1"
REMOTE_MANIFEST = ROOT / "tmod_remote_version.json"


def _split_param_block(source: str) -> tuple[str, str]:
    match = re.search(r"(?m)^param\(", source)
    if not match:
        raise ValueError("PowerShell source has no param block")
    start = match.start()
    depth = 0
    end = None
    for index in range(match.start() + len("param"), len(source)):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise ValueError("PowerShell param block is not closed")
    prefix = source[:start].strip()
    param_block = source[start:end]
    rest = source[end:].lstrip("\r\n")
    if prefix:
        rest = f"{prefix}\n{rest}"
    return param_block, rest


def _remove_ui_loader(source: str) -> str:
    lines = source.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.startswith("$uiModule = ")),
        None,
    )
    if start is None:
        raise ValueError("UI loader start was not found")
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].startswith("Import-Module $uiModule")
        ),
        None,
    )
    if end is None:
        raise ValueError("UI loader end was not found")
    return "\n".join(lines[:start] + lines[end + 1 :]) + "\n"


def _embedded_ui() -> str:
    # Source scripts are deliberately UTF-8 with BOM for Windows PowerShell
    # 5.1.  ``utf-8-sig`` accepts both source files and strips that marker
    # before they are embedded into a single portable payload.
    source = UI_SOURCE.read_text(encoding="utf-8-sig")
    source = re.sub(r"(?m)^Export-ModuleMember\b.*$", "", source)
    return source.strip()


def _payload(source_path: Path) -> str:
    param_block, body = _split_param_block(source_path.read_text(encoding="utf-8-sig"))
    body = _remove_ui_loader(body)
    return (
        f"{param_block}\n\n"
        "# Embedded T-Mod Console UI. Generated; edit tmod_console_ui.psm1.\n"
        f"{_embedded_ui()}\n\n"
        f"{body}"
    )


def _launcher(title: str, marker: str, payload: str, *, remote: bool) -> str:
    # Windows PowerShell 5.1 treats UTF-8 without a BOM as the active ANSI
    # code page. The embedded script contains Cyrillic and box-drawing glyphs,
    # so the BOM is required for the extracted portable payload to parse.
    encoded = base64.b64encode(payload.encode("utf-8-sig")).decode("ascii")
    lines = [
        "@echo off",
        "setlocal EnableExtensions DisableDelayedExpansion",
        "",
        f"title {title}",
        "chcp 65001 >nul",
    ]
    if remote:
        # A running batch file cannot safely replace itself.  The launched
        # helper first validates the staged artifact and then retries an NTFS
        # atomic File.Replace after this cmd process has exited.  Both the
        # producer (tmod_remote_windows.ps1) and this generated consumer use
        # the same .next + .next.ready transaction.
        update_script = r'''
$ErrorActionPreference = 'Stop'
$current = $env:TMOD_REMOTE_UPDATE_CURRENT
$next = $env:TMOD_REMOTE_UPDATE_NEXT
$ready = $env:TMOD_REMOTE_UPDATE_READY
if (-not (Test-Path -LiteralPath $next) -or -not (Test-Path -LiteralPath $ready)) { exit 0 }
if (((Get-Date) - (Get-Item -LiteralPath $next).LastWriteTime).TotalHours -gt 24) {
    Remove-Item -LiteralPath $next, $ready -Force -ErrorAction SilentlyContinue
    exit 0
}
$parts = @(Get-Content -LiteralPath $ready -ErrorAction Stop)
if ($parts.Count -lt 2 -or ([string]$parts[1]).Trim() -notmatch '^[a-fA-F0-9]{64}$') { exit 2 }
$expected = ([string]$parts[1]).Trim().ToLowerInvariant()
$actual = (Get-FileHash -LiteralPath $next -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { Remove-Item -LiteralPath $next, $ready -Force -ErrorAction SilentlyContinue; exit 3 }
$mutex = New-Object System.Threading.Mutex($false, 'Local\TModRemoteBundleApply')
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { exit 4 }
    $backup = "$current.previous"
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
            [IO.File]::Replace($next, $current, $backup, $true)
            Remove-Item -LiteralPath $ready, $backup -Force -ErrorAction SilentlyContinue
            Start-Process -FilePath $current
            exit 0
        }
        catch { Start-Sleep -Milliseconds 800 }
    }
    exit 5
}
finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
'''.strip()
        encoded_update_script = base64.b64encode(update_script.encode("utf-16le")).decode("ascii")
        lines.extend(
            [
                'set "TMOD_REMOTE_BUNDLE_PATH=%~f0"',
                'if exist "%~f0.next" if exist "%~f0.next.ready" (',
                '  set "TMOD_REMOTE_UPDATE_CURRENT=%~f0"',
                '  set "TMOD_REMOTE_UPDATE_NEXT=%~f0.next"',
                '  set "TMOD_REMOTE_UPDATE_READY=%~f0.next.ready"',
                f'  start "" /b powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded_update_script}',
                "  exit /b 0",
                ")",
            ]
        )
    else:
        lines.extend(
            [
                'set "LAUNCHER_DIR=%~dp0"',
                'set "PROJECT_DIR="',
                'if exist "%LAUNCHER_DIR%docker-compose.yml" set "PROJECT_DIR=%LAUNCHER_DIR%"',
                r'if not defined PROJECT_DIR if exist "%LAUNCHER_DIR%esgiel\docker-compose.yml" set "PROJECT_DIR=%LAUNCHER_DIR%esgiel"',
                r'if not defined PROJECT_DIR if exist "%USERPROFILE%\Desktop\esgiel\docker-compose.yml" set "PROJECT_DIR=%USERPROFILE%\Desktop\esgiel"',
                "if not defined PROJECT_DIR (",
                "  echo.",
                "  echo  [T-MOD CONTROL] Project not found.",
                r"  echo  Expected: %USERPROFILE%\Desktop\esgiel",
                "  echo.",
                "  pause",
                "  exit /b 1",
                ")",
            ]
        )
    lines.extend(
        [
            'set "TMOD_BUNDLE_FILE=%~f0"',
            f'set "TMOD_PAYLOAD_FILE=%TEMP%\\{marker.strip(":_").lower()}-%RANDOM%-%RANDOM%.ps1"',
            f'powershell.exe -NoLogo -NoProfile -NonInteractive -Command "$raw=[IO.File]::ReadAllText($env:TMOD_BUNDLE_FILE); $marker=\'{marker}\'; $offset=$raw.LastIndexOf($marker, [StringComparison]::Ordinal); if($offset -lt 0){{exit 41}}; $encoded=$raw.Substring($offset+$marker.Length) -replace \'\\s\',\'\'; [IO.File]::WriteAllBytes($env:TMOD_PAYLOAD_FILE,[Convert]::FromBase64String($encoded))"',
            "if errorlevel 1 (",
            "  echo.",
            "  echo  [T-MOD] Embedded control payload is damaged.",
            "  pause",
            "  exit /b 41",
            ")",
        ]
    )
    if remote:
        lines.append('powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%TMOD_PAYLOAD_FILE%" %*')
    else:
        lines.append('powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%TMOD_PAYLOAD_FILE%" -ProjectDir "%PROJECT_DIR%" %*')
    lines.extend(
        [
            'set "TMOD_EXIT=%ERRORLEVEL%"',
            'del /q "%TMOD_PAYLOAD_FILE%" >nul 2>nul',
            'if not "%TMOD_EXIT%"=="0" (',
            "  echo.",
            f"  echo  {title} finished with code %TMOD_EXIT%.",
            "  pause",
            ")",
            "exit /b %TMOD_EXIT%",
            marker,
        ]
    )
    lines.extend(textwrap.wrap(encoded, width=120))
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    builds = (
        (
            ROOT / "tmod_control_windows.ps1",
            ROOT / "tmod_control_windows.bat",
            "T-Mod Control",
            ":__TMOD_CONTROL_PAYLOAD__",
            False,
        ),
        (
            ROOT / "tmod_remote_windows.ps1",
            ROOT / "tmod_remote_windows.bat",
            "T-Mod Remote",
            ":__TMOD_REMOTE_PAYLOAD__",
            True,
        ),
    )
    for source, destination, title, marker, remote in builds:
        destination.write_text(
            _launcher(title, marker, _payload(source), remote=remote),
            encoding="utf-8",
            newline="",
        )
        print(f"built {destination.name}")
    control_payload = ROOT / "control-center" / "TMod.Control" / "Generated" / "TModControlPayload.ps1"
    control_payload.parent.mkdir(parents=True, exist_ok=True)
    control_payload.write_bytes(_payload(ROOT / "tmod_control_windows.ps1").encode("utf-8-sig"))
    print(f"built {control_payload.relative_to(ROOT)}")
    # Keep the public remote manifest tied to the exact generated single
    # file.  Otherwise a legitimate regeneration makes every auto-update
    # fail its checksum until someone remembers a separate manual edit.
    manifest = json.loads(REMOTE_MANIFEST.read_text(encoding="utf-8"))
    remote_bundle = ROOT / "tmod_remote_windows.bat"
    manifest.setdefault("sha256", {})[remote_bundle.name] = hashlib.sha256(
        remote_bundle.read_bytes()
    ).hexdigest()
    REMOTE_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"updated {REMOTE_MANIFEST.name}")


if __name__ == "__main__":
    main()
