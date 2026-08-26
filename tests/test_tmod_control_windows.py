import json
import hashlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TModControlWindowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.control = (ROOT / "tmod_control_windows.ps1").read_text(encoding="utf-8")
        self.remote = (ROOT / "tmod_remote_windows.ps1").read_text(encoding="utf-8")

    def test_local_control_is_the_single_desktop_entrypoint(self) -> None:
        batch = (ROOT / "tmod_control_windows.bat").read_text(encoding="utf-8")
        installer = (ROOT / "install_desktop_launcher_windows.bat").read_text(
            encoding="utf-8"
        )
        runtime = (ROOT / "run_windows.bat").read_text(encoding="utf-8")

        self.assertIn("tmod_control_windows.ps1", batch)
        self.assertIn("%USERPROFILE%\\Desktop\\esgiel", batch)
        self.assertIn("T-Mod Control.bat", installer)
        self.assertIn("T-Mod Control.bat", runtime)
        self.assertIn('del /Q "%%~fD\\Start T-Mod.bat"', runtime)
        self.assertNotIn("set /p choice", self.control.lower())
        self.assertIn('"UpArrow"', self.control)
        self.assertIn('"DownArrow"', self.control)
        self.assertIn('"Enter"', self.control)
        self.assertIn('"Escape"', self.control)

    def test_control_reuses_transactional_update_and_database_guard(self) -> None:
        self.assertIn("launch_tmod_guarded_windows.ps1", self.control)
        self.assertIn("safe update requested", self.control)
        self.assertIn("tmod_db_guard.py", self.control)
        self.assertIn('"backup", "--kind", "manual"', self.control)
        self.assertIn('"check", "--full"', self.control)
        self.assertIn("caddy validate", self.control)
        self.assertIn("caddy reload", self.control)
        self.assertIn('"--force-recreate", "--remove-orphans"', self.control)
        self.assertNotIn("git reset --hard", self.control)
        self.assertNotIn("docker compose down -v", self.control)

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
        self.assertIn("$script:RemoteActions -notcontains $RemoteAction", self.remote)
        self.assertIn("$script:Services -notcontains $RemoteService", self.remote)
        self.assertNotRegex(self.remote, r"PrivateKey\s*=")
        self.assertNotRegex(self.remote, r"[A-Za-z0-9+/]{40,}={0,2}")
        self.assertIn("LOCALAPPDATA", installer)
        self.assertIn("T-Mod Remote.bat", installer)
        self.assertIn("%LOCALAPPDATA%\\TModRemote", remote_batch)

    def test_remote_client_has_bounded_atomic_self_update(self) -> None:
        manifest = json.loads((ROOT / "tmod_remote_version.json").read_text())

        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["channel"], "stable")
        self.assertIn("cdnserver/t-mod/main", manifest["files"]["tmod_remote_windows.ps1"])
        self.assertIn("TimeoutSec 6", self.remote)
        self.assertIn("TimeoutSec 15", self.remote)
        self.assertIn("TModRemoteClient", self.remote)
        self.assertIn("Get-FileHash", self.remote)
        self.assertRegex(manifest["sha256"]["tmod_remote_windows.ps1"], r"^[a-f0-9]{64}$")
        for file_name, expected_hash in manifest["sha256"].items():
            actual_hash = hashlib.sha256((ROOT / file_name).read_bytes()).hexdigest()
            self.assertEqual(actual_hash, expected_hash)
        self.assertIn("Move-Item", self.remote)
        self.assertIn("TotalHours -ge 6", self.remote)
        self.assertIn("manifest.version", self.remote)

    def test_no_number_driven_menu_is_reintroduced(self) -> None:
        combined = self.control + self.remote
        self.assertNotRegex(combined, r"Read-Host\s+[\"'](?:choice|номер|выбор)")
        self.assertGreaterEqual(combined.count('"UpArrow"'), 2)
        self.assertGreaterEqual(combined.count('"DownArrow"'), 2)
        self.assertGreaterEqual(combined.count("›"), 2)

    def test_noninteractive_actions_remain_machine_callable(self) -> None:
        self.assertRegex(self.control, re.compile(r'\[string\]\$Action\s*=\s*"menu"'))
        self.assertIn('[switch]$Json', self.control)
        self.assertIn("ConvertTo-Json -Depth 5", self.control)
        self.assertIn("-Action '$actionLiteral'", self.remote)
        self.assertIn("-NoAnimation$jsonSwitch", self.remote)


if __name__ == "__main__":
    unittest.main()
