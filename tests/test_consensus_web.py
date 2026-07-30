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
            script_text = await script.text()
            self.assertIn(
                "document.body.dataset.outcome",
                script_text,
            )
            self.assertIn("if (!document.hidden && !dashboard.hidden)", script_text)
            self.assertIn("payloadSignature", script_text)
            self.assertIn('id="data-loading"', index_text)
            self.assertIn("requestedBillId", script_text)
            self.assertIn('id="copy-bill-link"', index_text)
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
            self.assertIn("ambientDriftPrimary", stylesheet_text)
            self.assertIn("@media (hover: hover)", stylesheet_text)
            self.assertIn("color-scheme: dark", stylesheet_text)
            self.assertIn("background-color: #080b0c", stylesheet_text)
            self.assertIn("min-height: 100dvh", stylesheet_text)

            login_page = await client.get("/login")
            self.assertEqual(login_page.status, 200)
            login_text = await login_page.text()
            self.assertIn("T·ID", login_text)
            self.assertIn('name="pin"', login_text)

            admin = await client.get("/admin")
            self.assertEqual(admin.status, 200)
            admin_text = await admin.text()
            self.assertIn("Ядерный Реактор", admin_text)
            self.assertIn('class="icon-button panel-switch" href="/"', admin_text)
            self.assertIn('id="reactor-link"', index_text)
            self.assertIn(
                "html,body{background:#080b0c;color:#edf1eb}",
                admin_text,
            )
            self.assertIn('id="screen-treasury"', admin_text)
            self.assertIn('id="screen-craft"', admin_text)
            self.assertIn('id="screen-discord"', admin_text)
            self.assertIn('id="screen-market"', admin_text)
            self.assertIn('id="screen-sgl"', admin_text)
            self.assertIn('id="screen-media"', admin_text)
            self.assertIn('id="screen-system"', admin_text)
            self.assertIn('id="copy-section-link"', admin_text)
            self.assertIn('id="copy-detail-link"', admin_text)
            self.assertIn('id="notification-toggle"', admin_text)
            self.assertIn('id="gate-state"', admin_text)
            self.assertIn('class="gate-check"', admin_text)
            self.assertIn('id="toast-title"', admin_text)
            self.assertIn("signal-composer-head", admin_text)

            admin_script = await client.get("/assets/admin.js")
            self.assertEqual(admin_script.status, 200)
            admin_script_text = await admin_script.text()
            self.assertIn("/api/admin/overview", admin_script_text)
            self.assertIn("/api/admin/media/command", admin_script_text)
            self.assertIn("/api/admin/link/", admin_script_text)
            self.assertIn("parseAdminRoute", admin_script_text)
            self.assertIn("const REFRESH_INTERVAL = 10000", admin_script_text)
            self.assertIn("pollGlobalActivity", admin_script_text)
            self.assertIn("playNotificationSound", admin_script_text)
            self.assertIn("activitySignature", admin_script_text)
            self.assertIn("market-signal-list", admin_script_text)
            self.assertIn(
                'appState.loading || byId("admin-shell").hidden',
                admin_script_text,
            )
            self.assertIn('error.payload?.error === "too_many_attempts"', admin_script_text)
            self.assertNotIn("innerHTML", admin_script_text)
            admin_stylesheet = await client.get("/assets/admin.css")
            self.assertEqual(admin_stylesheet.status, 200)
            admin_stylesheet_text = await admin_stylesheet.text()
            self.assertIn("prefers-reduced-motion", admin_stylesheet_text)
            self.assertIn('body[data-section="treasury"]', admin_stylesheet_text)
            self.assertIn("adminAmbient", admin_stylesheet_text)
            self.assertIn("@media (hover: hover)", admin_stylesheet_text)
            self.assertIn("@media (max-width: 480px)", admin_stylesheet_text)
            self.assertIn("overflow-x: hidden", admin_stylesheet_text)
            self.assertIn("Segoe UI Variable Display", admin_stylesheet_text)
            self.assertIn(".signal-card", admin_stylesheet_text)
            self.assertIn(".toast-copy", admin_stylesheet_text)
            self.assertIn("@keyframes gateOrbit", admin_stylesheet_text)
            self.assertNotIn("Georgia", admin_stylesheet_text)
            self.assertNotRegex(
                admin_stylesheet_text,
                r"font-size:\s*(?:7|8|9|10)px",
            )

            egg = await client.get("/egg")
            self.assertEqual(egg.status, 200)
            egg_text = await egg.text()
            self.assertIn("ЗИГМУНД ПРАВОСУДОВ", egg_text)
            self.assertIn("ЯЙЦА НА СТОЛ", egg_text)
            self.assertIn("БОТ ПОКАЗЫВАЕТ ДЕМКУ", egg_text)
            self.assertIn(
                "html,body{background:#000;color:#f5f7ed}",
                egg_text,
            )
            self.assertIn(
                "'sha256-kivcxaEPD+v/Ecc3Z+TNAW/Uf1rs+0/EwVf6c/m1dKc='",
                egg.headers["Content-Security-Policy"],
            )
            self.assertIn('id="egg-entry"', egg_text)
            self.assertIn('id="egg-enter"', egg_text)
            self.assertIn('src="/assets/egg.js"', egg_text)
            self.assertIn('src="/assets/zigmund-murchalki.mp3"', egg_text)
            self.assertNotIn(" autoplay", egg_text)

            egg_script = await client.get("/assets/egg.js")
            self.assertEqual(egg_script.status, 200)
            egg_script_text = await egg_script.text()
            self.assertIn("createAnalyser", egg_script_text)
            self.assertIn("--energy", egg_script_text)
            self.assertIn("--bass-punch", egg_script_text)
            self.assertIn("bassEnvelope", egg_script_text)
            self.assertIn("punchTarget", egg_script_text)
            self.assertIn("getByteFrequencyData", egg_script_text)
            self.assertIn("equalizerRanges", egg_script_text)
            self.assertIn("updateEqualizer", egg_script_text)
            self.assertIn("--bar-level", egg_script_text)
            self.assertIn("startExperience", egg_script_text)
            self.assertIn("Promise.all", egg_script_text)
            self.assertNotIn("void startSound()", egg_script_text)

            egg_audio = await client.get(
                "/assets/zigmund-murchalki.mp3",
                headers={"Range": "bytes=0-1023"},
            )
            self.assertEqual(egg_audio.status, 206)
            self.assertEqual(len(await egg_audio.read()), 1024)

            egg_stylesheet = await client.get("/assets/egg.css")
            self.assertEqual(egg_stylesheet.status, 200)
            egg_stylesheet_text = await egg_stylesheet.text()
            self.assertIn("height: 100dvh", egg_stylesheet_text)
            self.assertIn(".egg-entry", egg_stylesheet_text)
            self.assertIn("@keyframes shockwave", egg_stylesheet_text)
            self.assertIn("@keyframes stage-hit", egg_stylesheet_text)
            self.assertIn("@keyframes announcement-hit", egg_stylesheet_text)
            self.assertIn("var(--bass-punch)", egg_stylesheet_text)
            self.assertIn("@supports not (backdrop-filter", egg_stylesheet_text)
            self.assertNotRegex(
                egg_stylesheet_text,
                r"(?m)^\s*(?:translate|scale):",
            )
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

    async def test_persistent_login_uses_profile_credential_and_admin_role(self) -> None:
        storage.configure_web_credential(77, 42, "operator", "12345678")
        member = SimpleNamespace(
            id=42,
            display_name="Оператор",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )
        guild = SimpleNamespace(
            id=77,
            name="Товарищество",
            get_member=lambda user_id: member if user_id == 42 else None,
            fetch_member=AsyncMock(return_value=member),
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)
        app = create_consensus_web_app(bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            denied = await client.post(
                "/auth/login?next=/admin",
                data={"login": "operator", "pin": "00000000"},
                allow_redirects=False,
            )
            self.assertEqual(denied.status, 303)
            self.assertIn("error=invalid", denied.headers["Location"])

            accepted = await client.post(
                "/auth/login?next=/admin",
                data={"login": "operator", "pin": "12345678"},
                allow_redirects=False,
            )
            self.assertEqual(accepted.status, 303)
            self.assertEqual(accepted.headers["Location"], "/admin")
            self.assertIn("tmod_consensus_session=", accepted.headers["Set-Cookie"])

            member.guild_permissions.administrator = False
            not_admin = await client.post(
                "/auth/login?next=/admin",
                data={"login": "operator", "pin": "12345678"},
                allow_redirects=False,
            )
            self.assertEqual(not_admin.status, 303)
            self.assertIn("error=administrator", not_admin.headers["Location"])
        finally:
            await client.close()

    async def test_admin_center_requires_personal_administrator_session(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        administrator = self._principal()
        regular_member = self._principal(user_id=2)
        regular_member.member.guild_permissions.administrator = False
        try:
            unauthorized = await client.get("/api/admin/overview")
            self.assertEqual(unauthorized.status, 401)

            with patch(
                "modules.consensus_web._runtime_token",
                "test-access-token-123456",
            ):
                legacy = await client.get(
                    "/api/admin/overview",
                    headers={"Authorization": "Bearer test-access-token-123456"},
                )
            self.assertEqual(legacy.status, 403)
            self.assertEqual(
                (await legacy.json())["error"],
                "personal_login_required",
            )

            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=regular_member),
            ):
                denied = await client.get("/api/admin/overview")
            self.assertEqual(denied.status, 403)
            self.assertEqual(
                (await denied.json())["error"],
                "administrator_required",
            )

            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=administrator),
            ):
                overview = await client.get("/api/admin/overview?days=30")
                actions = await client.get("/api/admin/actions")
                finance = await client.get("/api/admin/finance")
                crafts = await client.get("/api/admin/crafts")
                discord_audit = await client.get("/api/admin/discord")
                registry = await client.get("/api/admin/registry")
                market = await client.get("/api/admin/market")
                bills = await client.get("/api/admin/bills")
                sgl = await client.get("/api/admin/sgl")
                members = await client.get("/api/admin/members")
                communications = await client.get("/api/admin/communications")
                profile = await client.get("/api/admin/profile")
                system = await client.get("/api/admin/system")
                media = await client.get("/api/admin/media")
                linked_bill = await client.get(
                    f"/api/admin/link/bill/{self.bill.id}",
                )
                missing_link = await client.get("/api/admin/link/bill/999999")

            for response in (
                overview,
                actions,
                finance,
                crafts,
                discord_audit,
                registry,
                market,
                bills,
                sgl,
                members,
                communications,
                profile,
                system,
                media,
                linked_bill,
            ):
                self.assertEqual(response.status, 200)
                self.assertTrue((await response.json())["viewer"]["administrator"])
            overview_payload = await overview.json()
            self.assertIn("counts", overview_payload)
            self.assertIn("system", overview_payload)
            self.assertIn("capabilities", await registry.json())
            self.assertEqual((await market.json())["server_id"], "RU15")
            self.assertEqual((await bills.json())["items"][0]["summary"], self.bill.summary)
            self.assertIn("outbox_status", await system.json())
            self.assertEqual(
                (await linked_bill.json())["item"]["title"],
                self.bill.title,
            )
            self.assertEqual(missing_link.status, 404)
            self.assertEqual(
                (await missing_link.json())["error"],
                "linked_record_not_found",
            )
        finally:
            await client.close()

    async def test_admin_media_commands_require_csrf_and_runtime(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        administrator = self._principal()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=administrator),
            ):
                without_csrf = await client.post(
                    "/api/admin/media/command",
                    headers={"X-Idempotency-Key": "media-command-without-csrf"},
                    json={"target": "music", "action": "pause"},
                )
                unavailable = await client.post(
                    "/api/admin/media/command",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "media-command-no-runtime",
                    },
                    json={"target": "music", "action": "pause"},
                )
            self.assertEqual(without_csrf.status, 403)
            self.assertEqual((await without_csrf.json())["error"], "csrf_failed")
            self.assertEqual(unavailable.status, 400)
            self.assertIn("недоступен", (await unavailable.json())["message"])
        finally:
            await client.close()

    async def test_admin_broadcast_is_confirmed_durable_and_idempotent(self) -> None:
        senator = SimpleNamespace(id=44, display_name="Сенатор", bot=False)
        role = SimpleNamespace(members=[senator])
        guild = SimpleNamespace(
            id=77,
            name="Товарищество",
            chunked=True,
            members=[senator],
            channels=[],
            get_role=lambda role_id: role,
        )
        self.bot.get_guild = lambda guild_id: guild if guild_id == 77 else None
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        administrator = self._principal()
        headers = {
            "X-CSRF-Token": "csrf-test-token",
            "X-Idempotency-Key": "web-broadcast-idempotent",
        }
        body = {
            "kind": "consensus",
            "title": "Собираемся на консенсус",
            "body": "Откройте личный пульт и подтвердите участие.",
            "link_url": "https://tvr.lat/",
            "confirmed": True,
        }
        try:
            with (
                patch(
                    "modules.consensus_web.resolve_principal",
                    AsyncMock(return_value=administrator),
                ),
                patch("modules.consensus_admin_web.wake_delivery_worker") as wake,
            ):
                first = await client.post(
                    "/api/admin/communications/send",
                    headers=headers,
                    json=body,
                )
                second = await client.post(
                    "/api/admin/communications/send",
                    headers=headers,
                    json=body,
                )
            self.assertEqual(first.status, 200)
            self.assertEqual(second.status, 200)
            self.assertEqual(
                (await first.json())["broadcast"]["id"],
                (await second.json())["broadcast"]["id"],
            )
            self.assertEqual(storage.broadcast_report(guild_id=77)["recipient_count"], 1)
            wake.assert_called_once()
        finally:
            await client.close()

    async def test_admin_profile_updates_only_authenticated_owner(self) -> None:
        character = storage.add_profile_character(77, 1, "Web Hero", "77101")
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        administrator = self._principal()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=administrator),
            ):
                preference = await client.post(
                    "/api/admin/profile/preference",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "profile-preference-test",
                    },
                    json={"preference": "dm_market", "enabled": False},
                )
                visibility = await client.post(
                    "/api/admin/profile/character",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "profile-character-test",
                    },
                    json={
                        "character_id": character.id,
                        "is_public": False,
                    },
                )
            self.assertEqual(preference.status, 200)
            self.assertFalse((await preference.json())["profile"]["dm_market"])
            self.assertEqual(visibility.status, 200)
            self.assertFalse((await visibility.json())["character"]["is_public"])
            profile, characters = storage.get_profile_snapshot(77, 1)
            self.assertIsNotNone(profile)
            self.assertFalse(profile.dm_market)
            self.assertFalse(characters[0].is_public)
        finally:
            await client.close()

    async def test_admin_market_alert_lifecycle_is_personal(self) -> None:
        storage.market_replace_snapshot(
            server_id="RU15",
            category="items",
            server_name="RU15",
            source_updated_at="2026-07-30T12:00:00+00:00",
            period_days=1,
            items=[
                {
                    "item_id": 501,
                    "external_id": "web-alert-item",
                    "item_name": "Тестовый сплав",
                    "total_count": 12,
                    "sold_count": 2,
                    "average_price": 100_000,
                    "min_price": 90_000,
                    "max_price": 120_000,
                }
            ],
        )
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        administrator = self._principal()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=administrator),
            ):
                created = await client.post(
                    "/api/admin/market/alert",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "market-alert-create",
                    },
                    json={
                        "action": "upsert",
                        "server_id": "RU15",
                        "category": "items",
                        "item_id": 501,
                        "target_price": 95_000,
                        "min_quantity": 2,
                    },
                )
                alert_id = int((await created.json())["alert"]["id"])
                paused = await client.post(
                    "/api/admin/market/alert",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "market-alert-pause",
                    },
                    json={"action": "pause", "alert_id": alert_id},
                )
                deleted = await client.post(
                    "/api/admin/market/alert",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "market-alert-delete",
                    },
                    json={"action": "delete", "alert_id": alert_id},
                )
            self.assertEqual(created.status, 200)
            self.assertEqual(paused.status, 200)
            self.assertEqual((await paused.json())["alert"]["status"], "paused")
            self.assertEqual(deleted.status, 200)
            self.assertIsNone(storage.market_get_alert(1, "RU15", 501))
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
