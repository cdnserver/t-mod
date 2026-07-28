import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsConsensusLauncherTests(unittest.TestCase):
    def test_standard_launcher_configures_and_checks_consensus_panel(self) -> None:
        launcher = (ROOT / "run_windows.bat").read_text(encoding="utf-8")

        self.assertIn("configure_consensus_windows.ps1", launcher)
        self.assertIn("--force-recreate", launcher)
        self.assertIn("http://127.0.0.1:8787/api/health", launcher)
        self.assertIn("http://t.consensus:8787", launcher)
        self.assertIn("consensus_server_ip.txt", launcher)
        self.assertIn("!CONSENSUS_SERVER_IP!:8787", launcher)
        self.assertNotIn("http://SERVER_LAN_IP:8787", launcher)

    def test_network_configuration_is_idempotent_and_private(self) -> None:
        script = (ROOT / "configure_consensus_windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('WireGuardSubnet = "10.8.0.0/24"', script)
        self.assertIn('RemoteAddress "LocalSubnet"', script)
        self.assertIn('Profile "Private"', script)
        self.assertIn('-EdgeTraversalPolicy Block', script)
        self.assertIn("Test-FirewallConfiguration", script)
        self.assertIn("Test-HostsConfiguration", script)
        self.assertIn("Start-Process", script)
        self.assertIn("-Verb RunAs", script)
        self.assertIn("AddressOutputPath", script)
        self.assertNotIn("PrivateKey", script)

    def test_network_configuration_enforces_container_settings(self) -> None:
        script = (ROOT / "configure_consensus_windows.ps1").read_text(
            encoding="utf-8"
        )
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn('CONSENSUS_WEB_ENABLED = "true"', script)
        self.assertIn('CONSENSUS_WEB_HOST = "0.0.0.0"', script)
        self.assertIn("CONSENSUS_WEB_PORT", script)
        self.assertIn('"8787:8787"', compose)


if __name__ == "__main__":
    unittest.main()
