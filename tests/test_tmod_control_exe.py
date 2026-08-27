import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "control-center" / "TMod.Control"


class TModControlExeTests(unittest.TestCase):
    def test_project_builds_one_self_contained_windows_executable(self) -> None:
        project = (PROJECT / "TMod.Control.csproj").read_text(encoding="utf-8")
        self.assertIn("<TargetFramework>net8.0</TargetFramework>", project)
        self.assertIn("<PublishSingleFile>true</PublishSingleFile>", project)
        self.assertIn("<SelfContained>true</SelfContained>", project)
        self.assertIn("<ApplicationIcon>../../desktop/resources/icon.ico</ApplicationIcon>", project)
        self.assertIn("Generated/TModControlPayload.ps1", project)

    def test_native_frontend_keeps_operations_allowlisted(self) -> None:
        backend = (PROJECT / "Backend.cs").read_text(encoding="utf-8")
        program = (PROJECT / "Program.cs").read_text(encoding="utf-8")
        self.assertIn("info.ArgumentList.Add", backend)
        self.assertNotIn("Arguments =", backend)
        self.assertIn('StartInfo("status", json: true)', backend)
        self.assertIn("Services.Distinct().Count()", backend)
        self.assertIn("TryApplyPendingUpdate", program)
        for action in ("update", "diagnostics", "backup", "db-check", "domain-check"):
            self.assertIn(f'"{action}"', program)

    def test_ui_is_animated_adaptive_and_keyboard_driven(self) -> None:
        ui = (PROJECT / "TerminalUi.cs").read_text(encoding="utf-8")
        self.assertIn("DrawComet", ui)
        self.assertIn("SafeWindowWidth", ui)
        self.assertIn("firstVisible", ui)
        self.assertIn("ConsoleKey.UpArrow", ui)
        self.assertIn("ConsoleKey.DownArrow", ui)
        self.assertIn("ConsoleKey.Enter", ui)
        self.assertIn("ConsoleKey.Escape", ui)
        self.assertNotIn("Console.Clear()", ui)

    def test_installer_verifies_release_before_atomic_install(self) -> None:
        installer = (ROOT / "install_tmod_control_windows.ps1").read_text(
            encoding="utf-8"
        )
        manifest = json.loads((ROOT / "tmod_control_version.json").read_text())
        self.assertRegex(manifest["sha256"], r"^[a-f0-9]{64}$")
        self.assertGreater(manifest["size_bytes"], 5 * 1024 * 1024)
        self.assertRegex(
            manifest["download_url"],
            r"^https://github\.com/cdnserver/t-mod-releases/releases/download/control-v[0-9.]+/T-Mod-Control\.exe$",
        )
        self.assertIn("Get-FileHash", installer)
        self.assertIn("0x4D", installer)
        self.assertIn("0x5A", installer)
        self.assertIn('"$target.next"', installer)
        self.assertIn("Move-Item", installer)
        self.assertNotIn("Invoke-Expression", installer)
        self.assertNotRegex(installer, re.compile(r"github\.com/(?!cdnserver/t-mod-releases)"))

    def test_embedded_payload_has_bom_and_no_ui_import_dependency(self) -> None:
        payload = (PROJECT / "Generated" / "TModControlPayload.ps1").read_bytes()
        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        text = payload.decode("utf-8-sig")
        self.assertIn("param(", text)
        self.assertIn("[string]$Action", text)
        self.assertNotIn("Import-Module $uiModule", text)


if __name__ == "__main__":
    unittest.main()
