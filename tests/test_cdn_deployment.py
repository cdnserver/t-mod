from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CDN = ROOT / "deploy" / "cdn"


class CdnDeploymentTests(unittest.TestCase):
    def test_edge_is_read_only_and_contains_no_application_database(self) -> None:
        compose = (CDN / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(":/srv/cdn:ro", compose)
        self.assertIn("read_only: true", compose)
        self.assertNotIn("postgres", compose.casefold())
        self.assertNotIn(".env_file", compose)

    def test_caddy_has_tls_health_security_and_cache_contracts(self) -> None:
        caddy = (CDN / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("{$CDN_DOMAIN:cdn.tvr.lat}", caddy)
        self.assertIn("/cdn-health", caddy)
        self.assertIn("max-age=31536000, immutable", caddy)
        self.assertIn("X-Content-Type-Options", caddy)
        self.assertIn("/.env", caddy)

    def test_publish_is_atomic_and_writes_checksums(self) -> None:
        publish = (CDN / "publish.sh").read_text(encoding="utf-8")
        self.assertIn("sha256sum", publish)
        self.assertIn("mv -Tf", publish)
        self.assertIn("Release already exists", publish)


if __name__ == "__main__":
    unittest.main()

