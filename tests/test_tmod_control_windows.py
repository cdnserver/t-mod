import json
import hashlib
import base64
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_bundle_payload(path: Path, marker: str) -> str:
    bundle = path.read_text(encoding="utf-8")
    encoded = "".join(bundle.rsplit(marker, 1)[1].split())
    payload = base64.b64decode(encoded)
    if not payload.startswith(b"\xef\xbb\xbf"):
        raise AssertionError("portable PowerShell payload must have a UTF-8 BOM")
    return payload.decode("utf-8-sig")


class TModControlWindowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.control = (ROOT / "tmod_control_windows.ps1").read_text(encoding="utf-8")
        self.remote = (ROOT / "tmod_remote_windows.ps1").read_text(encoding="utf-8")
        self.ui = (ROOT / "tmod_console_ui.psm1").read_text(encoding="utf-8")

    def test_local_control_is_the_single_desktop_entrypoint(self) -> None:
        batch = (ROOT / "tmod_control_windows.bat").read_text(encoding="utf-8")
        installer = (ROOT / "install_desktop_launcher_windows.bat").read_text(
            encoding="utf-8"
        )
        runtime = (ROOT / "run_windows.bat").read_text(encoding="utf-8")

        self.assertIn(":__TMOD_CONTROL_PAYLOAD__", batch)
        self.assertIn("%USERPROFILE%\\Desktop\\esgiel", batch)
        self.assertIn("LastIndexOf", batch)
        self.assertIn("TMOD_PAYLOAD_FILE", batch)
        bundled_control = read_bundle_payload(
            ROOT / "tmod_control_windows.bat", ":__TMOD_CONTROL_PAYLOAD__"
        )
        self.assertIn("Show-MainMenu", bundled_control)
        self.assertIn("function Select-TModMenu", bundled_control)
        self.assertNotIn("Import-Module $uiModule", bundled_control)
        self.assertIn("install_tmod_control_windows.ps1", installer)
        self.assertIn("T-Mod Control.exe", installer)
        self.assertIn("install_tmod_control_windows.ps1", runtime)
        self.assertIn("T-Mod Control refreshed: native EXE is current", runtime)
        self.assertNotIn('copy /y "%~dp0tmod_control_windows.bat"', runtime)
        self.assertNotIn("set /p choice", self.control.lower())
        self.assertIn("tmod_console_ui.psm1", self.control)
        self.assertIn('"UpArrow"', self.ui)
        self.assertIn('"DownArrow"', self.ui)
        self.assertIn('"Enter"', self.ui)
        self.assertIn('"Escape"', self.ui)

    def test_all_powershell_sources_are_windows_powershell_safe_utf8(self) -> None:
        """Windows PowerShell 5.1 reads UTF-8 without a BOM as ANSI.

        The operator launchers contain Cyrillic text.  A missing BOM therefore
        corrupts quoted strings before a launcher can even start.  Keep this
        check repository-wide so a new maintenance script cannot reintroduce
        that class of outage.
        """
        ignored_parts = {".git", ".venv", "node_modules", "release", "dist", "build"}
        scripts = sorted(
            [
                path
                for path in [*ROOT.rglob("*.ps1"), *ROOT.rglob("*.psm1")]
                if not any(part in ignored_parts for part in path.relative_to(ROOT).parts)
            ],
            key=lambda path: path.as_posix(),
        )
        self.assertGreater(len(scripts), 0)
        missing_bom = [
            path.relative_to(ROOT).as_posix()
            for path in scripts
            if not path.read_bytes().startswith(b"\xef\xbb\xbf")
        ]
        self.assertEqual(missing_bom, [])

    def test_control_reuses_transactional_update_and_database_guard(self) -> None:
        self.assertIn("launch_tmod_guarded_windows.ps1", self.control)
        self.assertIn("safe update requested", self.control)
        self.assertIn("tmod_db_guard.py", self.control)
        self.assertIn('"backup", "--kind", "manual"', self.control)
        self.assertIn('"check", "--full"', self.control)
        self.assertIn("caddy validate", self.control)
        self.assertIn("caddy reload", self.control)
        self.assertIn("Show-ErrorCenter", self.control)
        self.assertIn("Show-ResourceSnapshot", self.control)
        self.assertIn("Invoke-DomainCheck", self.control)
        self.assertIn("Export-DiagnosticReport", self.control)
        self.assertIn("docker image prune -f", self.control)
        self.assertIn('"until=168h"', self.control)
        self.assertNotIn("docker volume prune", self.control)
        self.assertIn('"--force-recreate", "--remove-orphans"', self.control)
        self.assertNotIn("git reset --hard", self.control)
        self.assertNotIn("docker compose down -v", self.control)
        self.assertIn('if ($Snapshot.State -eq "running") { return "DEGRADED" }', self.control)

    def test_services_and_contours_are_explicitly_allowlisted(self) -> None:
        for service in (
            "tmod-postgres",
            "tmod-discord-bot",
            "tmod-web",
            "tmod-worker",
            "atlas-qdrant",
            "atlas-forum-browser",
            "tmod-caddy",
            "minecraft",
            "minecraft-supervisor",
        ):
            self.assertIn(f'"{service}"', self.control)
        self.assertIn("$script:Services -notcontains $RequestedService", self.control)
        self.assertIn("$script:Groups.ContainsKey($RequestedGroup)", self.control)
        self.assertIn('"core" = @(', self.control)
        self.assertIn('"atlas" = @(', self.control)
        self.assertIn('"minecraft" = @(', self.control)
        self.assertIn('$_ -ne "tmod-db-migrate"', self.control)

    def test_remote_uses_wireguard_ssh_key_and_encoded_commands(self) -> None:
        installer = (ROOT / "install_tmod_remote_windows.ps1").read_text(
            encoding="utf-8"
        )
        remote_batch = (ROOT / "tmod_remote_windows.bat").read_text(encoding="utf-8")

        self.assertIn('"10.8.0.1"', self.remote)
        self.assertIn("ssh-keygen.exe", self.remote)
        self.assertIn("administrators_authorized_keys", self.remote)
        self.assertIn("*S-1-5-32-544:F", self.remote)
        self.assertIn("BatchMode=yes", self.remote)
        self.assertIn("EncodedCommand", self.remote)
        self.assertIn("ConnectTimeout=8", self.remote)
        self.assertIn("ServerAliveInterval=10", self.remote)
        self.assertIn("tmod_control_windows.ps1", self.remote)
        self.assertIn("tmod_console_ui.psm1", self.remote)
        self.assertIn("$script:RemoteActions -notcontains $RemoteAction", self.remote)
        self.assertIn("$script:Services -notcontains $RemoteService", self.remote)
        self.assertNotRegex(self.remote, r"PrivateKey\s*=")
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", self.remote)
        self.assertNotIn("BEGIN PRIVATE KEY", self.remote)
        self.assertIn("LOCALAPPDATA", installer)
        self.assertIn("T-Mod Remote.bat", installer)
        self.assertIn(":__TMOD_REMOTE_PAYLOAD__", remote_batch)
        self.assertIn("TMOD_REMOTE_BUNDLE_PATH", remote_batch)
        self.assertIn("%~f0.next", remote_batch)
        bundled_remote = read_bundle_payload(
            ROOT / "tmod_remote_windows.bat", ":__TMOD_REMOTE_PAYLOAD__"
        )
        self.assertIn("TModRemoteClient", bundled_remote)
        self.assertIn("function Select-TModMenu", bundled_remote)

    def test_remote_docker_commands_use_disposable_public_only_config(self) -> None:
        """SSH must not invoke Docker Desktop's interactive credential helper.

        Docker Desktop commonly stores ``credsStore=desktop`` in the user's
        normal config.  That helper cannot be unlocked by a noninteractive
        SSH logon and makes otherwise-public pulls/builds fail.  The remote
        wrapper must instead create a per-command config with no helper and
        no persisted credentials, then remove it when the action ends.
        """
        self.assertIn('"tmod-docker-public-"', self.remote)
        self.assertIn('"config.json"', self.remote)
        self.assertIn("'{\"auths\":{}}'", self.remote)
        self.assertIn("TMOD_REMOTE_NONINTERACTIVE", self.remote)
        self.assertIn("`$env:DOCKER_CONFIG = `$dockerConfig", self.remote)
        self.assertIn('$serviceArgument = if ($RemoteService)', self.remote)
        self.assertIn('$groupArgument = if ($RemoteGroup)', self.remote)
        self.assertIn("-Action '$actionLiteral'$serviceArgument$groupArgument", self.remote)
        self.assertIn(
            "Remove-Item -LiteralPath `$dockerConfig -Force -Recurse -ErrorAction SilentlyContinue",
            self.remote,
        )

        bundled_remote = read_bundle_payload(
            ROOT / "tmod_remote_windows.bat", ":__TMOD_REMOTE_PAYLOAD__"
        )
        self.assertIn('"tmod-docker-public-"', bundled_remote)
        self.assertIn("'{\"auths\":{}}'", bundled_remote)
        self.assertIn("TMOD_REMOTE_NONINTERACTIVE", bundled_remote)

    def test_remote_client_has_bounded_atomic_self_update(self) -> None:
        manifest = json.loads((ROOT / "tmod_remote_version.json").read_text())
        remote_batch = (ROOT / "tmod_remote_windows.bat").read_text(encoding="utf-8")

        self.assertEqual(manifest["version"], "1.1.2")
        self.assertEqual(manifest["channel"], "stable")
        self.assertEqual(set(manifest["files"]), {"tmod_remote_windows.bat"})
        self.assertIn("cdnserver/t-mod/main", manifest["files"]["tmod_remote_windows.bat"])
        self.assertIn("TimeoutSec 6", self.remote)
        self.assertIn("TimeoutSec 15", self.remote)
        self.assertIn("TModRemoteClient", self.remote)
        self.assertIn("Get-FileHash", self.remote)
        self.assertRegex(manifest["sha256"]["tmod_remote_windows.bat"], r"^[a-f0-9]{64}$")
        for file_name, expected_hash in manifest["sha256"].items():
            actual_hash = hashlib.sha256((ROOT / file_name).read_bytes()).hexdigest()
            self.assertEqual(actual_hash, expected_hash)
        self.assertIn("Move-Item", self.remote)
        self.assertIn("TotalHours -ge 6", self.remote)
        self.assertIn("manifest.version", self.remote)
        self.assertIn(".next.ready", remote_batch)
        bootstrap = re.search(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", remote_batch)
        self.assertIsNotNone(bootstrap)
        bootstrap_script = base64.b64decode(bootstrap.group(1)).decode("utf-16le")
        self.assertIn("[IO.File]::Replace", bootstrap_script)
        self.assertIn("TModRemoteBundleApply", bootstrap_script)

    def test_no_number_driven_menu_is_reintroduced(self) -> None:
        combined = self.control + self.remote + self.ui
        self.assertNotRegex(combined, r"Read-Host\s+[\"'](?:choice|номер|выбор)")
        self.assertGreaterEqual(combined.count('"UpArrow"'), 1)
        self.assertGreaterEqual(combined.count('"DownArrow"'), 1)
        self.assertGreaterEqual(combined.count("›"), 1)

    def test_visual_system_has_themes_cards_transitions_and_hotkeys(self) -> None:
        for theme in ("aurora", "reactor", "atlas", "ember"):
            self.assertIn(theme, self.ui)
        self.assertIn("Show-TModIntro", self.ui)
        self.assertIn("Show-TModTransition", self.ui)
        self.assertIn("Write-TModCardRow", self.ui)
        self.assertIn("Write-TModBadge", self.ui)
        self.assertIn("PageUp", self.ui)
        self.assertIn("PageDown", self.ui)
        self.assertIn('F1 = "help"', self.control)
        self.assertIn('U = "updates"', self.remote)

    def test_noninteractive_actions_remain_machine_callable(self) -> None:
        self.assertRegex(self.control, re.compile(r'\[string\]\$Action\s*=\s*"menu"'))
        self.assertIn('[switch]$Json', self.control)
        self.assertIn("ConvertTo-Json -Depth 5", self.control)
        self.assertIn("-Action '$actionLiteral'", self.remote)
        self.assertIn("-NoAnimation$jsonSwitch", self.remote)


if __name__ == "__main__":
    unittest.main()
