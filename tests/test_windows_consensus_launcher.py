import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsConsensusLauncherTests(unittest.TestCase):
    def test_standard_launcher_configures_and_checks_consensus_panel(self) -> None:
        launcher = (ROOT / "run_windows.bat").read_text(encoding="utf-8")

        self.assertIn("configure_direct_web_windows.ps1", launcher)
        self.assertNotIn("configure_cloudflare_tunnel_windows.ps1", launcher)
        self.assertIn("--force-recreate", launcher)
        self.assertIn("http://127.0.0.1:8787/api/health", launcher)
        self.assertIn("https://tvr.lat", launcher)
        self.assertNotIn("http://SERVER_LAN_IP:8787", launcher)

    def test_cloudflare_configuration_preserves_and_validates_config(self) -> None:
        script = (ROOT / "configure_cloudflare_tunnel_windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('PublicHostName = "tmod.rundans.lat"', script)
        self.assertIn('OriginUrl = "http://127.0.0.1:8787"', script)
        self.assertIn("tmod-backup-", script)
        self.assertIn("ingress validate", script)
        self.assertIn("Restart-Service", script)
        self.assertIn('$routeArguments.Add("dns")', script)
        self.assertIn("Other cloudflared ingress routes were preserved", script)
        self.assertIn("Start-Process", script)
        self.assertIn("-Verb RunAs", script)
        self.assertNotIn("PrivateKey", script)

    def test_direct_network_configuration_enforces_container_settings(self) -> None:
        script = (ROOT / "configure_direct_web_windows.ps1").read_text(
            encoding="utf-8"
        )
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        caddyfile = (ROOT / "Caddyfile").read_text(encoding="utf-8")
        example = (ROOT / ".env.persistent.example").read_text(encoding="utf-8")

        self.assertIn("CONSENSUS_WEB_PUBLIC_URL", script)
        self.assertIn("T-Mod Direct HTTPS", script)
        self.assertIn("T-Mod ACME HTTP", script)
        self.assertNotIn("PrivateKey", script)
        self.assertIn('"127.0.0.1:8787:8787"', compose)
        self.assertIn('"80:80"', compose)
        self.assertIn('"443:443"', compose)
        self.assertIn("tmod-caddy", compose)
        self.assertIn("reverse_proxy tmod-discord-bot:8787", caddyfile)
        self.assertNotIn("cloudflare", caddyfile.lower())
        self.assertIn(
            "CONSENSUS_WEB_PUBLIC_URL=https://tvr.lat",
            example,
        )


if __name__ == "__main__":
    unittest.main()
