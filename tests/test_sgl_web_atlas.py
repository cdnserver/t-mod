import gc
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import storage
from modules.consensus_web import create_consensus_web_app
from modules.consensus_web_auth import ConsensusWebPrincipal
from persistence import sgl_repository


class SGLWebAtlasTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_data_dir, self.old_database_file = storage.DATA_DIR, storage.DATABASE_FILE
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "sgl-web-atlas.db"
        storage.init_db()
        reserved = sgl_repository.reserve_sgl_case(
            guild_id=66, client_id=101, client_display="Client",
            lead_lawyer_id=42, lead_lawyer_display="Lawyer",
            secretary_id=None, secretary_display=None,
            created_by_id=42, created_by_display="Lawyer",
        )
        self.case = sgl_repository.attach_sgl_case_channel(reserved.id, 404)
        assert self.case is not None
        sgl_repository.record_sgl_case_message(
            case=self.case, origin="discord", discord_message_id=800,
            author_id=101, author_display="Client", author_avatar_url=None,
            author_is_bot=False, content="Есть скриншот и видеозапись.",
        )
        self.bot = SimpleNamespace(get_guild=lambda _guild_id: None)

    def tearDown(self) -> None:
        storage.DATA_DIR, storage.DATABASE_FILE = self.old_data_dir, self.old_database_file
        gc.collect()
        self.temp_dir.cleanup()

    @staticmethod
    def _principal() -> ConsensusWebPrincipal:
        member = SimpleNamespace(
            id=42, display_name="Lawyer",
            guild_permissions=SimpleNamespace(administrator=True), roles=[],
        )
        return ConsensusWebPrincipal(
            user_id=42, guild_id=66, display_name="Lawyer",
            csrf_token="csrf-test-token", member=member,  # type: ignore[arg-type]
        )

    async def test_case_atlas_stores_the_analysis_in_the_case_journal(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=66)  # type: ignore[arg-type]
        answer = {
            "answer": "Риск: нужна дата события.", "citations": [{"title": "Правило"}],
            "model": "atlas-claims", "response_mode": "balanced", "latency_ms": 10,
        }
        async with TestClient(TestServer(app)) as client:
            with patch("modules.consensus_web.resolve_principal", AsyncMock(return_value=self._principal())), patch("modules.sgl_web.atlas_answer", AsyncMock(return_value=answer)) as call:
                response = await client.post(
                    f"/api/sgl/cases/{self.case.case_number}/atlas",
                    headers={"Host": "sgl.tvr.lat", "X-CSRF-Token": "csrf-test-token"},
                    json={"question": "Найди риски", "kind": "risks", "model": "atlas-claims"},
                )
                payload = await response.json()
                detail_response = await client.get(
                    f"/api/sgl/cases/{self.case.case_number}", headers={"Host": "sgl.tvr.lat"}
                )
                detail = await detail_response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["note"]["kind"], "risks")
        self.assertEqual(payload["note"]["answer"], answer["answer"])
        self.assertIn("ВНУТРЕННИЙ ПАКЕТ SGL", call.await_args.args[1])
        self.assertEqual(detail_response.status, 200)
        self.assertEqual(detail["ai_notes"][0]["answer"], answer["answer"])


if __name__ == "__main__":
    unittest.main()
