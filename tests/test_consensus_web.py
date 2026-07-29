import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import storage
from modules.consensus_core import (
    LiveConsensusSession,
    LiveParticipant,
    LiveResult,
)
from modules.consensus_runtime import active_sessions
from modules.consensus_simulator import (
    ConsensusSimulation,
    clear_consensus_simulation,
    register_consensus_simulation,
)
from modules.consensus_web import (
    _request_remote,
    _result_payload,
    build_consensus_web_state,
    consensus_web_url,
    create_consensus_web_app,
)
from modules.consensus_web_auth import (
    ConsensusWebAuthError,
    ConsensusWebPrincipal,
    consume_entry_ticket,
    create_entry_ticket,
)
from modules.consensus_web_control import (
    consensus_web_capabilities,
    execute_consensus_web_command,
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
        self.bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=88,
            author_id=5,
            author_display="Автор",
            title="Следующий проект",
            summary="Проект находится в очереди для следующего рассмотрения.",
            materials="https://example.com/material",
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
                "id": self.bill.id,
                "bill_number": self.bill.bill_number,
                "title": "Текущий проект",
                "channel_id": 88,
                "message_id": 99,
                "author_id": 5,
                "author_display": "Автор проекта",
                "summary": "Полный публичный текст текущего законопроекта.",
                "materials": "https://example.com/source",
                "decision_category": "ordinary",
                "created_at": "2026-07-28T12:00:00+00:00",
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
        clear_consensus_simulation(77)
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def _principal(self, user_id: int = 1) -> ConsensusWebPrincipal:
        member = SimpleNamespace(
            id=user_id,
            display_name=f"Участник {user_id}",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        return ConsensusWebPrincipal(
            user_id=user_id,
            guild_id=77,
            display_name=member.display_name,
            csrf_token="csrf-test-token",
            member=member,  # type: ignore[arg-type]
        )

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

    async def test_state_exposes_public_bill_details_to_observers(self) -> None:
        state = await build_consensus_web_state(self.bot, 77)  # type: ignore[arg-type]

        bill = state["session"]["current_bill"]
        self.assertEqual(bill["author"]["name"], "Автор проекта")
        self.assertEqual(
            bill["summary"],
            "Полный публичный текст текущего законопроекта.",
        )
        self.assertEqual(bill["materials"], "https://example.com/source")
        self.assertEqual(bill["decision_category"], "ordinary")
        self.assertIsNone(state["session"]["current_result"])

    async def test_fixed_result_exposes_exact_percentages_and_restores_bill_text(self) -> None:
        self.session.current_bill = None
        self.session.stage = "after_result"
        self.session.results.append(
            LiveResult(
                bill_id=self.bill.id,
                bill_number=self.bill.bill_number,
                title=self.bill.title,
                status="accepted",
                internal_percent=66.7,
                overall_percent=75.0,
                internal_active=True,
                votes={1: "yes", 2: "yes", 3: "no", 4: "yes"},
                source_channel_id=88,
                source_message_id=99,
                required_percent=50.0,
                opposed_percent=25.0,
                block_votes={
                    "first": "yes",
                    "second": "yes",
                    "third": "no",
                    "consensus": "yes",
                },
            )
        )

        state = await build_consensus_web_state(self.bot, 77)  # type: ignore[arg-type]

        result = state["session"]["current_result"]
        self.assertEqual(result["overall_percent"], 75.0)
        self.assertEqual(result["internal_percent"], 66.7)
        self.assertEqual(result["opposed_percent"], 25.0)
        self.assertEqual(result["required_percent"], 50.0)
        self.assertEqual(state["session"]["blocks"]["third"], "no")
        self.assertEqual(
            state["session"]["current_bill"]["summary"],
            self.bill.summary,
        )
        self.assertEqual(
            state["session"]["current_bill"]["author"]["name"],
            "Автор",
        )

    def test_persisted_result_payload_reads_database_column_names(self) -> None:
        payload = _result_payload(
            {
                "bill_id": 4,
                "bill_number": 12,
                "bill_title": "Название из протокола",
                "status": "accepted",
                "overall_percent": 75,
                "block_votes_json": (
                    '{"first":"yes","second":"yes",'
                    '"third":"no","consensus":"yes"}'
                ),
            },
            77,
        )

        self.assertEqual(payload["title"], "Название из протокола")
        self.assertEqual(payload["block_votes"]["third"], "no")

    async def test_personal_leader_state_exposes_only_stage_capabilities(self) -> None:
        state = await build_consensus_web_state(  # type: ignore[arg-type]
            self.bot,
            77,
            principal=self._principal(),
        )

        self.assertTrue(state["viewer"]["authenticated"])
        self.assertTrue(state["viewer"]["leader"])
        self.assertIn("leader_vote", state["capabilities"])
        self.assertIn("set_timer", state["capabilities"])
        self.assertNotIn("open_registration", state["capabilities"])
        self.assertNotIn("confirm_participant", state["capabilities"])

    def test_leader_capability_matrix_covers_every_live_stage(self) -> None:
        principal = self._principal()
        expected = {
            "registration": {"start_vote", "resend_invitations", "cancel_session"},
            "voting": {
                "leader_vote",
                "set_timer",
                "finalize_vote",
                "pause",
                "finish_session",
            },
            "finalizing": {"retry_finalization"},
            "discussion_type": {
                "choose_discussion",
                "pause",
                "finish_session",
            },
            "discussion": {"end_discussion", "pause", "finish_session"},
            "paused": {"resume", "finish_session"},
            "after_result": {"next_bill", "finish_session"},
        }
        for stage, actions in expected.items():
            with self.subTest(stage=stage):
                self.session.stage = stage  # type: ignore[assignment]
                self.assertTrue(
                    actions.issubset(
                        set(
                            consensus_web_capabilities(
                                mode="live",
                                session=self.session,
                                principal=principal,
                            )
                        )
                    )
                )

        observer = self._principal(user_id=4)
        self.assertEqual(
            consensus_web_capabilities(
                mode="live",
                session=self.session,
                principal=observer,
            ),
            [],
        )

    def test_entry_ticket_is_single_use_and_guild_scoped(self) -> None:
        ticket = create_entry_ticket(guild_id=77, user_id=1)

        self.assertEqual(
            consume_entry_ticket(ticket, expected_guild_id=77),
            (77, 1),
        )
        with self.assertRaises(ConsensusWebAuthError):
            consume_entry_ticket(ticket, expected_guild_id=77)

        wrong_guild = create_entry_ticket(guild_id=77, user_id=1)
        with self.assertRaises(ConsensusWebAuthError):
            consume_entry_ticket(wrong_guild, expected_guild_id=78)

    async def test_http_api_requires_token_and_serves_dashboard(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            index = await client.get("/")
            self.assertEqual(index.status, 200)
            index_text = await index.text()
            self.assertIn("T·Consensus", index_text)
            self.assertIn('id="observer-screen"', index_text)
            self.assertIn('id="atmosphere"', index_text)
            self.assertIn('id="result-announcer"', index_text)
            self.assertIn('id="bill-dialog"', index_text)
            self.assertIn('id="bill-library-dialog"', index_text)
            self.assertIn(
                '<meta name="theme-color" content="#080b0c">',
                index_text,
            )
            self.assertIn(
                "html,body{background:#080b0c;color:#edf1eb}",
                index_text,
            )
            self.assertIn(
                "'sha256-0IYaU6NkDTflYaDbUR4nMFteY9tDTb1ADhuFP1o95po='",
                index.headers["Content-Security-Policy"],
            )

            script = await client.get("/assets/app.js")
            self.assertEqual(script.status, 200)
            self.assertIn(
                "document.body.dataset.outcome",
                await script.text(),
            )
            stylesheet = await client.get("/assets/style.css")
            self.assertEqual(stylesheet.status, 200)
            stylesheet_text = await stylesheet.text()
            self.assertIn(
                'body[data-outcome="accepted"]',
                stylesheet_text,
            )
            self.assertIn(
                "@media (prefers-reduced-motion: reduce)",
                stylesheet_text,
            )
            self.assertIn("color-scheme: dark", stylesheet_text)
            self.assertIn("background-color: #080b0c", stylesheet_text)
            self.assertIn("min-height: 100dvh", stylesheet_text)

            egg = await client.get("/egg")
            self.assertEqual(egg.status, 200)
            egg_text = await egg.text()
            self.assertIn("T·MOD", egg_text)
            self.assertIn("ЯЙЦА НА СТОЛ", egg_text)
            self.assertIn("БОТ ПОКАЗЫВАЕТ ДЕМКУ", egg_text)
            self.assertNotIn("<script", egg_text)

            custom_egg = await client.get(
                "/egg",
                params={"text": "СБОРЩИК РИСА"},
            )
            self.assertEqual(custom_egg.status, 200)
            self.assertIn("СБОРЩИК РИСА", await custom_egg.text())

            escaped_egg = await client.get(
                "/egg",
                params={"text": "<script>alert(1)</script>"},
            )
            escaped_egg_text = await escaped_egg.text()
            self.assertNotIn("<script>alert(1)</script>", escaped_egg_text)
            self.assertIn("&lt;script&gt;", escaped_egg_text)

            egg_stylesheet = await client.get("/assets/egg.css")
            self.assertEqual(egg_stylesheet.status, 200)
            egg_stylesheet_text = await egg_stylesheet.text()
            self.assertIn("height: 100dvh", egg_stylesheet_text)
            self.assertIn(
                "@media (prefers-reduced-motion: reduce)",
                egg_stylesheet_text,
            )

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

    async def test_bill_catalog_and_detail_expose_only_guild_public_record(self) -> None:
        foreign_bill = storage.tvrs_create_bill(
            guild_id=78,
            channel_id=90,
            author_id=8,
            author_display="Другой сервер",
            title="Чужой проект",
            summary="Этот текст не должен быть доступен серверу 77.",
            materials=None,
        )
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer test-access-token-123456"}
        try:
            with patch(
                "modules.consensus_web._runtime_token",
                "test-access-token-123456",
            ):
                catalog = await client.get("/api/bills", headers=headers)
                detail = await client.get(
                    f"/api/bills/{self.bill.id}",
                    headers=headers,
                )
                foreign = await client.get(
                    f"/api/bills/{foreign_bill.id}",
                    headers=headers,
                )

            self.assertEqual(catalog.status, 200)
            items = (await catalog.json())["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["author"]["name"], "Автор")
            self.assertEqual(detail.status, 200)
            payload = await detail.json()
            self.assertEqual(payload["bill"]["summary"], self.bill.summary)
            self.assertEqual(
                payload["bill"]["materials"],
                "https://example.com/material",
            )
            self.assertIsNone(payload["result"])
            self.assertEqual(foreign.status, 404)
        finally:
            await client.close()

    async def test_legacy_key_cannot_execute_commands(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch("modules.consensus_web._runtime_token", "test-access-token-123456"):
                response = await client.post(
                    "/api/command",
                    headers={
                        "Authorization": "Bearer test-access-token-123456",
                        "X-Idempotency-Key": "legacy-command-123456",
                    },
                    json={"action": "finalize_vote"},
                )
            self.assertEqual(response.status, 403)
            self.assertEqual(
                (await response.json())["error"],
                "personal_login_required",
            )
        finally:
            await client.close()

    async def test_personal_command_requires_csrf_and_is_idempotent(self) -> None:
        principal = self._principal()
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        execute = AsyncMock(return_value="Команда выполнена.")
        try:
            with (
                patch(
                    "modules.consensus_web.resolve_principal",
                    AsyncMock(return_value=principal),
                ),
                patch(
                    "modules.consensus_web.execute_consensus_web_command",
                    execute,
                ),
            ):
                denied = await client.post(
                    "/api/command",
                    headers={"X-Idempotency-Key": "personal-command-no-csrf"},
                    json={},
                )
                self.assertEqual(denied.status, 403)

                headers = {
                    "X-CSRF-Token": "csrf-test-token",
                    "X-Idempotency-Key": "personal-command-idempotent",
                }
                body = {
                    "mode": "live",
                    "action": "leader_vote",
                    "session_key": "web-test",
                    "revision": 0,
                    "bill_id": 20,
                    "payload": {"vote": "yes"},
                }
                first = await client.post("/api/command", headers=headers, json=body)
                second = await client.post("/api/command", headers=headers, json=body)
            self.assertEqual(first.status, 200)
            self.assertEqual(second.status, 200)
            self.assertEqual(execute.await_count, 1)
        finally:
            await client.close()

    async def test_simulation_is_selectable_without_masking_live_consensus(self) -> None:
        simulation = ConsensusSimulation(
            guild_id=77,
            leader_id=100,
            leader_display="Учебный ведущий",
        )
        simulation.confirm_all()
        simulation.begin_voting()
        simulation.cast_leader_vote("yes")
        register_consensus_simulation(simulation)

        default_state = await build_consensus_web_state(self.bot, 77)  # type: ignore[arg-type]
        simulation_state = await build_consensus_web_state(  # type: ignore[arg-type]
            self.bot,
            77,
            mode="simulation",
        )

        self.assertEqual(default_state["mode"], "live")
        self.assertEqual(default_state["session"]["key"], "web-test")
        self.assertEqual(simulation_state["mode"], "simulation")
        self.assertTrue(simulation_state["active"])
        self.assertTrue(
            simulation_state["session"]["key"].startswith("simulation:")
        )
        self.assertEqual(simulation_state["session"]["voting"]["received"], 1)
        self.assertEqual(len(simulation_state["queue"]), 3)
        self.assertEqual(
            simulation_state["available_modes"],
            ["live", "simulation"],
        )

    async def test_http_mode_query_returns_simulation_snapshot(self) -> None:
        simulation = ConsensusSimulation(
            guild_id=77,
            leader_id=100,
            leader_display="Учебный ведущий",
        )
        register_consensus_simulation(simulation)
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch("modules.consensus_web._runtime_token", "test-access-token-123456"):
                response = await client.get(
                    "/api/state?mode=simulation",
                    headers={"Authorization": "Bearer test-access-token-123456"},
                )
            self.assertEqual(response.status, 200)
            payload = await response.json()
            self.assertEqual(payload["mode"], "simulation")
            self.assertEqual(payload["session"]["stage"], "registration")
        finally:
            await client.close()

    async def test_simulation_bill_library_has_openable_full_text(self) -> None:
        simulation = ConsensusSimulation(
            guild_id=77,
            leader_id=100,
            leader_display="Учебный ведущий",
        )
        register_consensus_simulation(simulation)
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer test-access-token-123456"}
        try:
            with patch(
                "modules.consensus_web._runtime_token",
                "test-access-token-123456",
            ):
                catalog = await client.get(
                    "/api/bills?mode=simulation",
                    headers=headers,
                )
                detail = await client.get(
                    f"/api/bills/{900_000 + simulation.bill_number}?mode=simulation",
                    headers=headers,
                )

            self.assertEqual(catalog.status, 200)
            items = (await catalog.json())["items"]
            self.assertTrue(items)
            self.assertTrue(all(item["id"] > 900_000 for item in items))
            self.assertEqual(detail.status, 200)
            self.assertIn(
                "Тестовый проект",
                (await detail.json())["bill"]["summary"],
            )
        finally:
            await client.close()

    async def test_simulation_leader_command_uses_same_simulation_session(self) -> None:
        simulation = ConsensusSimulation(
            guild_id=77,
            leader_id=1,
            leader_display="Ведущий",
        )
        register_consensus_simulation(simulation)
        principal = self._principal()
        capabilities = consensus_web_capabilities(
            mode="simulation",
            session=simulation.session,
            principal=principal,
        )
        self.assertIn("confirm_all", capabilities)

        message = await execute_consensus_web_command(  # type: ignore[arg-type]
            self.bot,
            self.bot.get_guild(77),
            principal,
            mode="simulation",
            action="confirm_all",
            session_key=simulation.session.session_key,
            revision=simulation.session.revision,
            bill_id=0,
            payload={},
        )

        self.assertEqual(message, "Команда симулятора выполнена.")
        self.assertTrue(simulation.session.quorum_ready())

    def test_public_https_url_replaces_local_display_address(self) -> None:
        with patch(
            "modules.consensus_web.CONSENSUS_WEB_PUBLIC_URL",
            "https://consensus.example.com",
        ):
            self.assertEqual(
                consensus_web_url(),
                "https://consensus.example.com",
            )

    def test_direct_reverse_proxy_uses_non_spoofable_forwarded_address(self) -> None:
        request = SimpleNamespace(
            remote="172.20.0.5",
            headers={
                "X-Forwarded-For": "8.8.4.4, 9.9.9.9",
                "CF-Connecting-IP": "1.0.0.1",
            },
        )
        self.assertEqual(_request_remote(request), "9.9.9.9")

    def test_direct_client_cannot_override_remote_address(self) -> None:
        request = SimpleNamespace(
            remote="8.8.8.8",
            headers={"X-Forwarded-For": "1.0.0.1"},
        )
        self.assertEqual(_request_remote(request), "8.8.8.8")


if __name__ == "__main__":
    unittest.main()
