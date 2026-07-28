import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

import storage
from modules.consensus_core import (
    LiveConsensusSession,
    LiveParticipant,
)
from modules.consensus_runtime import active_sessions
from modules.consensus_web import (
    build_consensus_web_state,
    consensus_web_url,
    create_consensus_web_app,
)


def _participant(user_id: int, block: str | None = None) -> LiveParticipant:
    return LiveParticipant(
        user_id=user_id,
        display_name=f"Участник {user_id}",
        mention=f"<@{user_id}>",
        kind="chair" if block else "senator",
        voting_block=block,  # type: ignore[arg-type]
        confirmed=True,
        dm_message_id=1000 + user_id,
    )


class ConsensusWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "consensus-web-test.db"
        storage.init_db()
        storage.tvrs_create_bill(
            guild_id=77,
            channel_id=88,
            author_id=5,
            author_display="Автор",
            title="Следующий проект",
            summary="Проект находится в очереди для следующего рассмотрения.",
            materials=None,
        )
        self.session = LiveConsensusSession(
            session_key="web-test",
            guild_id=77,
            channel_id=88,
            leader_id=1,
            leader_display="Ведущий",
            plenary_number=6,
            participants={
                1: _participant(1, "first"),
                2: _participant(2, "second"),
                3: _participant(3, "third"),
                4: _participant(4),
            },
            current_bill={
                "id": 20,
                "bill_number": 20,
                "title": "Текущий проект",
                "channel_id": 88,
                "message_id": 99,
            },
            stage="voting",
        )
        self.session.votes = {1: "yes", 4: "no"}
        active_sessions[77] = self.session
        self.bot = SimpleNamespace(
            get_guild=lambda guild_id: (
                SimpleNamespace(id=77, name="Товарищество")
                if guild_id == 77
                else None
            )
        )

    def tearDown(self) -> None:
        active_sessions.pop(77, None)
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_state_exposes_progress_but_not_live_vote_directions(self) -> None:
        state = await build_consensus_web_state(self.bot, 77)  # type: ignore[arg-type]

        self.assertTrue(state["active"])
        self.assertEqual(state["session"]["voting"]["received"], 2)
        self.assertEqual(state["session"]["voting"]["expected"], 4)
        self.assertEqual(state["session"]["blocks"]["first"], "hidden")
        self.assertEqual(len(state["queue"]), 1)
        rendered = str(state["session"]["participants"])
        self.assertNotIn("'vote':", rendered)
        self.assertNotIn("'yes'", rendered)
        self.assertNotIn("'no'", rendered)

    async def test_http_api_requires_token_and_serves_dashboard(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            index = await client.get("/")
            self.assertEqual(index.status, 200)
            self.assertIn("T·Consensus", await index.text())

            denied = await client.get("/api/state")
            self.assertEqual(denied.status, 401)

            with patch("modules.consensus_web._runtime_token", "test-access-token-123456"):
                allowed = await client.get(
                    "/api/state",
                    headers={"Authorization": "Bearer test-access-token-123456"},
                )
            self.assertEqual(allowed.status, 200)
            payload = await allowed.json()
            self.assertEqual(payload["session"]["plenary_number"], 6)
            self.assertEqual(allowed.headers["X-Frame-Options"], "DENY")
        finally:
            await client.close()

    def test_public_https_url_replaces_local_display_address(self) -> None:
        with patch(
            "modules.consensus_web.CONSENSUS_WEB_PUBLIC_URL",
            "https://consensus.example.com",
        ):
            self.assertEqual(
                consensus_web_url(),
                "https://consensus.example.com",
            )


if __name__ == "__main__":
    unittest.main()
