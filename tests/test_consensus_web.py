import tempfile
import unittest
from decimal import Decimal
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfReader

import storage
from modules.consensus_admin_web import _json_ready
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
    _canonical_surface_location,
    _request_remote,
    _result_payload,
    build_consensus_web_state,
    consensus_web_url,
    create_consensus_web_app,
)
from modules.consensus_web_auth import (
    ConsensusWebAuthError,
    ConsensusWebPrincipal,
    LEGACY_SESSION_COOKIE,
    SESSION_COOKIE,
    account_cookie_domain,
    clear_session_cookie,
    consume_entry_ticket,
    create_entry_ticket,
    create_session_token,
    request_public_host,
    request_public_secure,
    resolve_principal,
    set_session_cookie,
)
from modules.consensus_web_control import (
    ConsensusWebCommandError,
    consensus_web_capabilities,
    execute_consensus_web_command,
)
from modules.tvrs_discussion import publish_discussion_message


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
            revision=3,
        )
        self.session.votes = {1: "yes", 4: "no"}
        active_sessions[77] = self.session
        self.bot = SimpleNamespace(
            get_guild=lambda guild_id: (
                SimpleNamespace(id=77, name="Товарищество") if guild_id == 77 else None
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

    async def test_admin_json_boundary_normalizes_postgres_decimals(self) -> None:
        payload = _json_ready({
            "counts": Decimal("12"),
            "finance": {"balance": Decimal("1250.75")},
            "series": (Decimal("1"), Decimal("2.5")),
            "checked_at": datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        })

        self.assertEqual(payload["counts"], 12)
        self.assertEqual(payload["finance"]["balance"], 1250.75)
        self.assertEqual(payload["series"], [1, 2.5])
        self.assertEqual(payload["checked_at"], "2026-08-30T12:00:00+00:00")

    async def test_state_exposes_progress_but_not_live_vote_directions(self) -> None:
        state = await build_consensus_web_state(self.bot, 77)  # type: ignore[arg-type]

        self.assertTrue(state["active"])
        self.assertEqual(state["session"]["voting"]["received"], 2)
        self.assertEqual(state["session"]["voting"]["expected"], 4)
        self.assertEqual(state["session"]["blocks"]["first"], "hidden")
        self.assertEqual(state["session"]["integrity"]["status"], "nominal")
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

    async def test_state_exposes_upcoming_consensus_schedule(self) -> None:
        storage.save_consensus_schedule(
            guild_id=77,
            plenary_number=7,
            title="Седьмой пленарный консенсус",
            description="Рассмотрение очереди законопроектов.",
            invitation_text="Просим прибыть заранее.",
            scheduled_for=datetime.now(timezone.utc) + timedelta(days=1),
            duration_minutes=90,
            voice_channel_id=88,
            actor_id=1,
            actor_display="Председатель",
        )

        state = await build_consensus_web_state(self.bot, 77)  # type: ignore[arg-type]

        self.assertEqual(state["schedule"]["plenary_number"], 7)
        self.assertEqual(state["schedule"]["duration_minutes"], 90)
        self.assertEqual(state["schedule"]["title"], "Седьмой пленарный консенсус")

    async def test_fixed_result_exposes_exact_percentages_and_restores_bill_text(
        self,
    ) -> None:
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
                    '{"first":"yes","second":"yes","third":"no","consensus":"yes"}'
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
        directions = {
            item["id"]: item.get("vote")
            for item in state["session"]["participants"]
        }
        self.assertEqual(directions[1], "yes")
        self.assertEqual(directions[4], "no")
        self.assertIsNone(directions[2])

    async def test_confirmed_member_receives_private_web_ballot_state(self) -> None:
        state = await build_consensus_web_state(  # type: ignore[arg-type]
            self.bot,
            77,
            principal=self._principal(user_id=4),
        )

        self.assertTrue(state["viewer"]["participant"])
        self.assertTrue(state["viewer"]["confirmed"])
        self.assertTrue(state["viewer"]["ballot_available"])
        self.assertTrue(state["viewer"]["can_vote"])
        self.assertEqual(state["viewer"]["vote"], "no")
        self.assertEqual(
            state["capabilities"],
            ["participant_vote", "request_discussion"],
        )
        self.assertNotIn("'vote':", str(state["session"]["participants"]))

    async def test_scheduled_ballot_stays_locked_until_final_window(self) -> None:
        schedule = storage.save_consensus_schedule(
            guild_id=77,
            plenary_number=6,
            title="Шестой пленарный консенсус",
            description="Проверка защищённого допуска.",
            invitation_text="Подтвердите участие в Discord.",
            scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=5),
            duration_minutes=90,
            voice_channel_id=88,
            actor_id=1,
            actor_display="Председатель",
        )
        storage.start_consensus_schedule(
            77,
            session_key=self.session.session_key,
            schedule_id=schedule["id"],
        )

        state = await build_consensus_web_state(  # type: ignore[arg-type]
            self.bot,
            77,
            principal=self._principal(user_id=4),
        )

        self.assertFalse(state["viewer"]["ballot_available"])
        self.assertFalse(state["viewer"]["can_vote"])
        self.assertEqual(state["capabilities"], [])
        self.assertIsNotNone(state["viewer"]["ballot_unlock_at"])

    async def test_scheduled_ballot_opens_for_confirmed_member_in_final_window(
        self,
    ) -> None:
        schedule = storage.save_consensus_schedule(
            guild_id=77,
            plenary_number=6,
            title="Шестой пленарный консенсус",
            description="Проверка защищённого допуска.",
            invitation_text="Подтвердите участие в Discord.",
            scheduled_for=datetime.now(timezone.utc) + timedelta(seconds=60),
            duration_minutes=90,
            voice_channel_id=88,
            actor_id=1,
            actor_display="Председатель",
        )
        storage.start_consensus_schedule(
            77,
            session_key=self.session.session_key,
            schedule_id=schedule["id"],
        )

        state = await build_consensus_web_state(  # type: ignore[arg-type]
            self.bot,
            77,
            principal=self._principal(user_id=4),
        )

        self.assertTrue(state["viewer"]["ballot_available"])
        self.assertTrue(state["viewer"]["can_vote"])
        self.assertEqual(
            state["capabilities"],
            ["participant_vote", "request_discussion"],
        )

    async def test_nonmember_remains_broadcast_only(self) -> None:
        state = await build_consensus_web_state(  # type: ignore[arg-type]
            self.bot,
            77,
            principal=self._principal(user_id=99),
        )

        self.assertFalse(state["viewer"]["participant"])
        self.assertFalse(state["viewer"]["ballot_available"])
        self.assertFalse(state["viewer"]["can_vote"])
        self.assertIsNone(state["viewer"]["vote"])
        self.assertEqual(state["capabilities"], [])

    def test_leader_capability_matrix_covers_every_live_stage(self) -> None:
        principal = self._principal()
        expected = {
            "registration": {"start_vote", "resend_invitations", "cancel_session"},
            "presentation": {"open_vote", "pause", "finish_session"},
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
        bill = dict(self.session.current_bill or {})
        for stage, actions in expected.items():
            with self.subTest(stage=stage):
                self.session.stage = stage  # type: ignore[assignment]
                self.session.current_bill = None if stage in {"registration", "after_result"} else dict(bill)
                self.session.pending_action = (
                    {
                        "kind": "vote",
                        "forced": True,
                        "actor_id": 1,
                        "bill_id": int(bill["id"]),
                        "claimed_at": "2026-07-28T12:00:00+00:00",
                    }
                    if stage == "finalizing"
                    else None
                )
                self.session.previous_stage = "voting" if stage == "paused" else None
                self.session.discussion_type = "Правовая" if stage == "discussion" else None
                self.session.discussion_initiator_id = 4 if stage == "discussion" else None
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

        self.session.stage = "voting"
        self.session.current_bill = dict(bill)
        self.session.previous_stage = None
        observer = self._principal(user_id=4)
        self.assertEqual(
            consensus_web_capabilities(
                mode="live",
                session=self.session,
                principal=observer,
            ),
            ["participant_vote", "request_discussion"],
        )

    async def test_corrupt_roster_is_read_only_in_web_console(self) -> None:
        self.session.participants[2].voting_block = "first"
        principal = self._principal()

        self.assertEqual(
            consensus_web_capabilities(
                mode="live",
                session=self.session,
                principal=principal,
            ),
            [],
        )
        with self.assertRaises(ConsensusWebCommandError) as raised:
            await execute_consensus_web_command(  # type: ignore[arg-type]
                self.bot,
                SimpleNamespace(id=77),
                principal,
                mode="live",
                action="finalize_vote",
                session_key=self.session.session_key,
                revision=self.session.revision,
                bill_id=self.bill.id,
                payload={"confirm": True},
            )

        self.assertEqual(raised.exception.code, "consensus_integrity_locked")
        self.assertIn("voting_blocks_invalid", raised.exception.details["issues"])

    async def test_host_timer_accepts_custom_duration_and_replace_mode(self) -> None:
        timer = AsyncMock()
        guild = SimpleNamespace(id=77, name="Товарищество", get_channel=lambda _id: None)
        with patch(
            "modules.consensus_web_control.set_vote_timer",
            new=timer,
        ):
            message = await execute_consensus_web_command(  # type: ignore[arg-type]
                self.bot,
                guild,
                self._principal(),
                mode="live",
                action="set_timer",
                session_key=self.session.session_key,
                revision=self.session.revision,
                bill_id=self.bill.id,
                payload={"seconds": 725, "mode": "replace"},
            )

        self.assertEqual(message, "Таймер установлен на 725 секунд.")
        timer.assert_awaited_once_with(
            self.bot,
            guild,
            self.session,
            725,
            expected_bill_id=self.bill.id,
            expected_revision=self.session.revision,
            replace=True,
        )

    async def test_host_updates_bound_session_passport_with_revision_lock(self) -> None:
        guild = SimpleNamespace(id=77, name="Товарищество", get_channel=lambda _id: None)
        schedule = storage.save_consensus_schedule(
            guild_id=77,
            plenary_number=6,
            title="Шестой пленарный консенсус",
            description="Повестка",
            invitation_text="Приглашение",
            scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=10),
            duration_minutes=90,
            voice_channel_id=88,
            actor_id=1,
            actor_display="Председатель",
        )
        started = storage.start_consensus_schedule(
            77,
            session_key=self.session.session_key,
            schedule_id=schedule["id"],
        )

        self.assertIn(
            "update_session_settings",
            consensus_web_capabilities(
                mode="live",
                session=self.session,
                principal=self._principal(),
            ),
        )
        message = await execute_consensus_web_command(  # type: ignore[arg-type]
            self.bot,
            guild,
            self._principal(),
            mode="live",
            action="update_session_settings",
            session_key=self.session.session_key,
            revision=self.session.revision,
            bill_id=self.bill.id,
            payload={
                "title": "Особое пленарное заседание",
                "duration_minutes": 135,
                "schedule_revision": started["revision"],
            },
        )

        self.assertIn("Особое пленарное заседание", message)
        updated = storage.get_consensus_schedule_for_session(
            77,
            self.session.session_key,
        )
        self.assertEqual(updated["title"], "Особое пленарное заседание")
        self.assertEqual(updated["duration_minutes"], 135)

    async def test_participant_vote_uses_shared_coordinator_without_revision_conflict(self) -> None:
        principal = self._principal(user_id=4)

        def cast_vote(session, user_id, vote, *, actor):
            self.assertEqual(user_id, 4)
            self.assertEqual(actor.user_id, 4)
            session.votes[user_id] = vote
            session.revision += 1
            return False

        with (
            patch(
                "modules.consensus_web_control.coordinator.cast_vote",
                side_effect=cast_vote,
            ) as mutation,
            patch(
                "modules.consensus_web_control.update_host_vote_message",
                new=AsyncMock(),
            ),
        ):
            message = await execute_consensus_web_command(  # type: ignore[arg-type]
                self.bot,
                self.bot.get_guild(77),
                principal,
                mode="live",
                action="participant_vote",
                session_key=self.session.session_key,
                revision=self.session.revision - 100,
                bill_id=self.bill.id,
                payload={"vote": "yes"},
            )

        self.assertEqual(message, "Ваш голос принят и синхронизирован с Discord.")
        self.assertEqual(self.session.votes[4], "yes")
        mutation.assert_called_once()

    async def test_senator_can_request_discussion_from_web_ballot(self) -> None:
        principal = self._principal(user_id=4)
        with patch(
            "modules.consensus_web_control.request_discussion",
            new=AsyncMock(),
        ) as request:
            message = await execute_consensus_web_command(  # type: ignore[arg-type]
                self.bot,
                self.bot.get_guild(77),
                principal,
                mode="live",
                action="request_discussion",
                session_key=self.session.session_key,
                revision=self.session.revision,
                bill_id=self.bill.id,
                payload={},
            )

        self.assertIn("Запрос дискуссии принят", message)
        request.assert_awaited_once()

    async def test_web_discussion_message_is_attributed_in_discord(self) -> None:
        self.session.stage = "discussion"
        self.session.discussion_channel_id = 555
        self.session.discussion_allowed_user_ids = {4}
        sent = SimpleNamespace(id=987)
        channel = SimpleNamespace(send=AsyncMock(return_value=sent))
        bot = SimpleNamespace(
            get_channel=lambda channel_id: channel if channel_id == 555 else None,
        )

        message_id = await publish_discussion_message(  # type: ignore[arg-type]
            bot,
            self.session,
            self.session.participants[4],
            "Моя позиция по существу законопроекта.",
            expected_bill_id=self.bill.id,
        )

        self.assertEqual(message_id, 987)
        kwargs = channel.send.await_args.kwargs
        self.assertEqual(
            kwargs["embed"].description,
            "Моя позиция по существу законопроекта.",
        )
        self.assertIn("Участник 4", kwargs["embed"].author.name)
        self.assertIn("через веб-панель", kwargs["embed"].footer.text)
        self.assertFalse(kwargs["allowed_mentions"].everyone)

    async def test_web_discussion_composer_uses_current_allowed_roster(self) -> None:
        self.session.stage = "discussion"
        self.session.discussion_channel_id = 555
        self.session.discussion_type = "Правовая"
        self.session.discussion_initiator_id = 4
        self.session.discussion_allowed_user_ids = {4}
        principal = self._principal(user_id=4)
        self.assertEqual(
            consensus_web_capabilities(
                mode="live",
                session=self.session,
                principal=principal,
            ),
            ["post_discussion_message"],
        )
        publish = AsyncMock(return_value=987)
        with patch(
            "modules.consensus_web_control.publish_discussion_message",
            new=publish,
        ):
            response = await execute_consensus_web_command(  # type: ignore[arg-type]
                self.bot,
                self.bot.get_guild(77),
                principal,
                mode="live",
                action="post_discussion_message",
                session_key=self.session.session_key,
                revision=self.session.revision,
                bill_id=self.bill.id,
                payload={"content": "Аргумент из веб-панели"},
            )

        self.assertIn("опубликовано", response)
        publish.assert_awaited_once()

    async def test_last_web_vote_runs_normal_vote_finalization(self) -> None:
        principal = self._principal(user_id=4)
        finalization = AsyncMock()

        with (
            patch(
                "modules.consensus_web_control.coordinator.cast_vote",
                return_value=True,
            ),
            patch(
                "modules.consensus_web_control.finalize_current_vote",
                new=finalization,
            ),
        ):
            await execute_consensus_web_command(  # type: ignore[arg-type]
                self.bot,
                self.bot.get_guild(77),
                principal,
                mode="live",
                action="participant_vote",
                session_key=self.session.session_key,
                revision=self.session.revision,
                bill_id=self.bill.id,
                payload={"vote": "abstain"},
            )

        finalization.assert_awaited_once_with(
            self.bot,
            self.bot.get_guild(77),
            self.session,
            forced=False,
            expected_bill_id=self.bill.id,
        )

    async def test_web_vote_rejects_ballot_from_previous_bill(self) -> None:
        with self.assertRaises(ConsensusWebCommandError) as raised:
            await execute_consensus_web_command(  # type: ignore[arg-type]
                self.bot,
                self.bot.get_guild(77),
                self._principal(user_id=4),
                mode="live",
                action="participant_vote",
                session_key=self.session.session_key,
                revision=self.session.revision,
                bill_id=self.bill.id + 100,
                payload={"vote": "yes"},
            )

        self.assertEqual(raised.exception.code, "stale_bill")
        self.assertEqual(self.session.votes[4], "no")

    async def test_result_screen_can_present_next_bill_with_visible_result_id(self) -> None:
        self.session.stage = "after_result"
        self.session.current_bill = None
        self.session.votes.clear()
        self.session.results.append(
            LiveResult(
                bill_id=self.bill.id,
                bill_number=self.bill.bill_number,
                title=self.bill.title,
                status="accepted",
                internal_percent=75.0,
                overall_percent=75.0,
                internal_active=True,
                votes={1: "yes", 2: "yes", 3: "yes", 4: "no"},
                source_channel_id=88,
                source_message_id=99,
                required_percent=50.0,
                opposed_percent=25.0,
                block_votes={
                    "first": "yes",
                    "second": "yes",
                    "third": "yes",
                    "consensus": "no",
                },
            )
        )
        guild = SimpleNamespace(
            id=77,
            name="Товарищество",
            get_channel=lambda _id: SimpleNamespace(id=88),
        )
        state = await build_consensus_web_state(  # type: ignore[arg-type]
            self.bot,
            77,
            principal=self._principal(),
        )
        visible_bill_id = int(state["session"]["current_bill"]["id"])
        begin_next = AsyncMock()

        with patch(
            "modules.consensus_web_control.begin_next_bill_vote",
            new=begin_next,
        ):
            message = await execute_consensus_web_command(  # type: ignore[arg-type]
                self.bot,
                guild,
                self._principal(),
                mode="live",
                action="next_bill",
                session_key=self.session.session_key,
                revision=self.session.revision,
                bill_id=visible_bill_id,
                payload={},
            )

        self.assertEqual(message, "Следующий законопроект представлен; воут пока закрыт.")
        begin_next.assert_awaited_once_with(
            self.bot,
            guild,
            self.session,
            guild.get_channel(88),
            expected_stage="after_result",
            expected_result_bill_id=self.bill.id,
            expected_revision=self.session.revision,
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

    def test_tmod_account_cookie_is_shared_only_inside_configured_domain(self) -> None:
        with patch.dict("os.environ", {"TMOD_ACCOUNT_COOKIE_DOMAIN": ".tvr.lat"}):
            self.assertEqual(account_cookie_domain("atlas.tvr.lat"), ".tvr.lat")
            self.assertEqual(account_cookie_domain("tvr.lat:443"), ".tvr.lat")
            self.assertIsNone(account_cookie_domain("tvr.lat.attacker.example"))
            self.assertIsNone(account_cookie_domain("127.0.0.1:8787"))

            shared = web.Response()
            set_session_cookie(
                shared,
                "signed-token",
                secure=True,
                request_host="reactor.tvr.lat",
            )
            morsel = shared.cookies[SESSION_COOKIE]
            self.assertEqual(morsel["domain"], ".tvr.lat")
            self.assertEqual(morsel["httponly"], True)
            self.assertEqual(morsel["secure"], True)
            self.assertEqual(morsel["samesite"], "Lax")

            cleared = web.Response()
            clear_session_cookie(
                cleared,
                secure=True,
                request_host="consensus.tvr.lat",
            )
            self.assertIn(SESSION_COOKIE, cleared.cookies)
            self.assertIn(LEGACY_SESSION_COOKIE, cleared.cookies)
            self.assertEqual(cleared.cookies[SESSION_COOKIE]["domain"], ".tvr.lat")

    async def test_public_surfaces_redirect_to_their_canonical_domains(self) -> None:
        direct = SimpleNamespace(
            path="/atlas",
            query={},
            host="tvr.lat",
            rel_url="/atlas?screen=ai",
        )
        self.assertEqual(
            _canonical_surface_location(direct),  # type: ignore[arg-type]
            "https://dash.tvr.lat/atlas?screen=ai",
        )

        forwarded = SimpleNamespace(
            path="/atlas",
            query={},
            host="tmod-discord-bot:8788",
            rel_url="/atlas",
            remote="127.0.0.1",
            secure=False,
            headers={
                "X-TMod-Forwarded-Host": "tvr.lat",
                "X-TMod-Forwarded-Proto": "https",
            },
        )
        self.assertEqual(request_public_host(forwarded), "tvr.lat")  # type: ignore[arg-type]
        self.assertTrue(request_public_secure(forwarded))  # type: ignore[arg-type]
        self.assertEqual(
            _canonical_surface_location(forwarded),  # type: ignore[arg-type]
            "https://dash.tvr.lat/atlas",
        )

        privacy = SimpleNamespace(
            path="/privacy",
            query={},
            host="home.tvr.lat",
            rel_url="/privacy",
        )
        self.assertEqual(
            _canonical_surface_location(privacy),  # type: ignore[arg-type]
            "https://tvr.lat/privacy",
        )

        billing_privacy = SimpleNamespace(
            path="/privacy",
            query={},
            host="atlas.tvr.lat",
            rel_url="/privacy",
        )
        self.assertIsNone(
            _canonical_surface_location(billing_privacy),  # type: ignore[arg-type]
        )

        # A marker from a public/untrusted hop must not override the internal
        # host, preventing clients from spoofing cookie scope or routing.
        untrusted = SimpleNamespace(
            host="tmod-discord-bot:8788",
            remote="8.8.8.8",
            secure=False,
            headers={"X-TMod-Forwarded-Host": "atlas.tvr.lat"},
        )
        self.assertEqual(request_public_host(untrusted), "tmod-discord-bot:8788")  # type: ignore[arg-type]

        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            atlas = await client.get(
                "/atlas",
                headers={"Host": "tvr.lat"},
                allow_redirects=False,
            )
            games = await client.get(
                "/games/demo-match",
                headers={"Host": "atlas.tvr.lat"},
                allow_redirects=False,
            )
            ticket = await client.get(
                "/auth/ticket?ticket=unused&next=/admin",
                headers={"Host": "consensus.tvr.lat"},
                allow_redirects=False,
            )
            canonical = await client.get(
                "/atlas",
                headers={"Host": "dash.tvr.lat", "X-TMod-Desktop-Version": "0.3.3"},
                allow_redirects=False,
            )
            sgl = await client.get(
                "/sgl",
                headers={"Host": "reactor.tvr.lat"},
                allow_redirects=False,
            )
            ovr = await client.get(
                "/ovr",
                headers={"Host": "reactor.tvr.lat"},
                allow_redirects=False,
            )
            admission = await client.get(
                "/admission",
                headers={"Host": "tvr.lat"},
                allow_redirects=False,
            )
            billing_privacy_page = await client.get(
                "/privacy",
                headers={"Host": "atlas.tvr.lat"},
                allow_redirects=False,
            )
            billing_privacy_text = await billing_privacy_page.text()
            ecosystem = await client.get(
                "/ecosystem",
                headers={"Host": "tvr.lat"},
                allow_redirects=False,
            )
            ecosystem_text = await ecosystem.text()
            senate = await client.get(
                "/senate",
                headers={"Host": "senate.tvr.lat"},
                allow_redirects=False,
            )
            senate_text = await senate.text()
            legacy_reactor = await client.get(
                "/reactor",
                headers={"Host": "tvr.lat"},
                allow_redirects=False,
            )
            atlas_store = await client.get(
                "/atlas-billing",
                headers={"Host": "atlas.tvr.lat"},
                allow_redirects=False,
            )
            atlas_store_text = await atlas_store.text()
        finally:
            await client.close()

        self.assertEqual(atlas.status, 308)
        self.assertEqual(atlas.headers["Location"], "https://dash.tvr.lat/atlas")
        self.assertEqual(games.status, 308)
        self.assertEqual(games.headers["Location"], "https://home.tvr.lat/games/demo-match")
        self.assertEqual(ticket.status, 308)
        self.assertIn("https://reactor.tvr.lat/auth/ticket?", ticket.headers["Location"])
        self.assertEqual(canonical.status, 200)
        self.assertEqual(sgl.status, 308)
        self.assertEqual(sgl.headers["Location"], "https://sgl.tvr.lat/sgl")
        self.assertEqual(ovr.status, 308)
        self.assertEqual(ovr.headers["Location"], "https://ovr.tvr.lat/ovr")
        self.assertEqual(admission.status, 308)
        self.assertEqual(
            admission.headers["Location"],
            "https://phx.tvr.lat/admission",
        )
        self.assertEqual(billing_privacy_page.status, 200)
        self.assertIn(
            "Политика обработки персональных данных",
            billing_privacy_text,
        )
        self.assertEqual(ecosystem.status, 200)
        self.assertIn("Технологии,", ecosystem_text)
        self.assertIn("T-Mod Home", ecosystem_text)
        self.assertEqual(senate.status, 200)
        self.assertIn("Продолжить в T-Mod Desktop", senate_text)
        self.assertEqual(legacy_reactor.status, 308)
        self.assertEqual(
            legacy_reactor.headers["Location"],
            "https://home.tvr.lat/reactor",
        )
        self.assertEqual(atlas_store.status, 200)
        self.assertIn("ATLAS INTELLIGENCE", atlas_store_text)
        self.assertIn("Магазин и управление Atlas Token", atlas_store_text)

    async def test_ovr_portal_requires_manual_section_grant(self) -> None:
        regular_member = self._principal(user_id=2)
        regular_member.member.guild_permissions.administrator = False
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=regular_member),
            ):
                denied_page = await client.get(
                    "/ovr", headers={"Host": "ovr.tvr.lat"}
                )
                denied_api = await client.get(
                    "/api/ovr", headers={"Host": "ovr.tvr.lat"}
                )

                storage.web_set_section_grant(
                    77,
                    2,
                    "ovr",
                    enabled=True,
                    granted_by_id=1,
                )
                allowed_page = await client.get(
                    "/ovr", headers={"Host": "ovr.tvr.lat"}
                )
                allowed_api = await client.get(
                    "/api/ovr", headers={"Host": "ovr.tvr.lat"}
                )
                created_response = await client.post(
                    "/api/ovr",
                    headers={
                        "Host": "ovr.tvr.lat",
                        "X-CSRF-Token": regular_member.csrf_token,
                    },
                    json={
                        "action": "create",
                        "first_name": "Jimmy",
                        "last_name": "McGill",
                        "static_id": "456",
                        "discord_text": "jimmy",
                        "case_kind": "admission",
                        "priority": "important",
                        "objective": "Проверить биографию кандидата.",
                    },
                )
                created_payload = await created_response.json()
                detail_response = await client.get(
                    f"/api/ovr?case_id={created_payload['case']['id']}",
                    headers={"Host": "ovr.tvr.lat"},
                )
                detail_payload = await detail_response.json()
                claim_response = await client.post(
                    "/api/ovr",
                    headers={
                        "Host": "ovr.tvr.lat",
                        "X-CSRF-Token": regular_member.csrf_token,
                    },
                    json={
                        "action": "claim",
                        "case_id": created_payload["case"]["id"],
                        "expected_revision": created_payload["case"]["revision"],
                    },
                )
                claim_payload = await claim_response.json()
                denied_payload = await denied_api.json()
                allowed_page_text = await allowed_page.text()
                allowed_payload = await allowed_api.json()

        self.assertEqual(denied_page.status, 403)
        self.assertEqual(denied_api.status, 403)
        self.assertEqual(denied_payload["error"], "ovr_access_required")
        self.assertEqual(allowed_page.status, 200)
        self.assertIn("ВНЕШНЯЯ РАЗВЕДКА", allowed_page_text)
        self.assertEqual(allowed_api.status, 200)
        self.assertTrue(allowed_payload["full_access"])
        self.assertEqual(created_response.status, 200)
        self.assertEqual(detail_response.status, 200)
        self.assertEqual(detail_payload["detail"]["case"]["case_kind"], "admission")
        self.assertEqual(claim_response.status, 200)
        self.assertEqual(claim_payload["detail"]["case"]["status"], "screening")

    async def test_ovr_report_requires_matching_password_and_returns_encrypted_pdf(self) -> None:
        principal = self._principal(user_id=12)
        principal.member.guild_permissions.administrator = False
        storage.web_set_section_grant(
            77,
            12,
            "ovr",
            enabled=True,
            granted_by_id=1,
        )
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=principal),
            ):
                created = await client.post(
                    "/api/ovr",
                    headers={
                        "Host": "ovr.tvr.lat",
                        "X-CSRF-Token": principal.csrf_token,
                    },
                    json={
                        "action": "create",
                        "first_name": "Kim",
                        "last_name": "Wexler",
                        "static_id": "710",
                        "discord_text": "kim",
                        "case_kind": "background",
                        "priority": "normal",
                        "objective": "Подготовить архивное досье.",
                    },
                )
                case_id = (await created.json())["case"]["id"]
                mismatch = await client.post(
                    f"/api/ovr/{case_id}/report.pdf",
                    headers={
                        "Host": "ovr.tvr.lat",
                        "X-CSRF-Token": principal.csrf_token,
                    },
                    json={
                        "password": "first-password",
                        "password_confirmation": "second-password",
                    },
                )
                report = await client.post(
                    f"/api/ovr/{case_id}/report.pdf",
                    headers={
                        "Host": "ovr.tvr.lat",
                        "X-CSRF-Token": principal.csrf_token,
                    },
                    json={
                        "password": "OVR-web-2026",
                        "password_confirmation": "OVR-web-2026",
                    },
                )
                report_body = await report.read()

        self.assertEqual(mismatch.status, 400)
        self.assertEqual(report.status, 200)
        self.assertEqual(report.content_type, "application/pdf")
        self.assertIn("no-store", report.headers["Cache-Control"])
        self.assertIn("attachment", report.headers["Content-Disposition"])
        reader = PdfReader(BytesIO(report_body))
        self.assertTrue(reader.is_encrypted)
        self.assertGreater(int(reader.decrypt("OVR-web-2026")), 0)
        self.assertGreaterEqual(len(reader.pages), 3)

    async def test_sgl_surface_has_public_bureau_landing(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            page = await client.get("/sgl", headers={"Host": "sgl.tvr.lat"})
            page_text = await page.text()
            registry = await client.get("/api/sgl/bootstrap", headers={"Host": "sgl.tvr.lat"})
            payload = await registry.json()
            redesigned_assets = {
                name: await client.get(f"/sgl/assets/{name}")
                for name in ("site.css", "app-ui.css", "site.js", "app-ui.js", "fonts.css")
            }
            sgl_font = await client.get("/sgl/assets/fonts/inter-400-cyrillic.woff2")
            rejected_font = await client.get("/sgl/assets/fonts/not-a-font.ttf")

        self.assertEqual(page.status, 200)
        self.assertIn("T-Mod SGL", page_text)
        self.assertEqual(registry.status, 200)
        self.assertEqual(payload["mode"], "public")
        self.assertIn("discord.com", payload["public"]["discord_url"])
        self.assertIn("discord.com/users/", payload["public"]["secretary_url"])
        self.assertTrue(all(response.status == 200 for response in redesigned_assets.values()))
        self.assertEqual(sgl_font.status, 200)
        self.assertEqual(sgl_font.content_type, "font/woff2")
        self.assertIn("immutable", sgl_font.headers.get("Cache-Control", ""))
        self.assertEqual(rejected_font.status, 404)

    async def test_sgl_registry_uses_shared_administrator_identity(self) -> None:
        principal = self._principal(user_id=42)
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=principal),
            ):
                response = await client.get(
                    "/api/sgl/bootstrap", headers={"Host": "sgl.tvr.lat"}
                )
                payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "management")
        self.assertTrue(payload["viewer"]["administrator"])
        self.assertEqual(payload["counts"]["all"], 0)

    async def test_sgl_public_landing_hides_registry_from_regular_member(self) -> None:
        principal = self._principal(user_id=43)
        principal.member.guild_permissions.administrator = False
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=principal),
            ):
                response = await client.get(
                    "/api/sgl/bootstrap", headers={"Host": "sgl.tvr.lat"}
                )
                payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "public")
        self.assertNotIn("cases", payload)

    async def test_existing_tmod_account_session_skips_repeated_login(self) -> None:
        principal = self._principal(user_id=42)
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=principal),
            ):
                atlas = await client.get(
                    "/login?next=/atlas",
                    allow_redirects=False,
                )
                admin = await client.get(
                    "/login?next=/admin",
                    allow_redirects=False,
                )

            self.assertEqual(atlas.status, 303)
            self.assertEqual(atlas.headers["Location"], "/atlas")
            self.assertEqual(admin.status, 303)
            self.assertEqual(admin.headers["Location"], "/admin")
        finally:
            await client.close()

    async def test_legacy_host_cookie_cannot_restore_shared_logout(self) -> None:
        legacy_token, _ = create_session_token(guild_id=77, user_id=42)
        request = SimpleNamespace(cookies={LEGACY_SESSION_COOKIE: legacy_token})

        principal = await resolve_principal(  # type: ignore[arg-type]
            request,
            self.bot,  # type: ignore[arg-type]
            guild_id=77,
        )

        self.assertIsNone(principal)

    async def test_zero_account_session_does_not_require_character(self) -> None:
        storage.configure_web_credential(77, 4242, "external.user", "12345678")
        credential = storage.get_web_credential(77, 4242)
        self.assertIsNotNone(credential)
        token, _ = create_session_token(
            guild_id=77,
            user_id=4242,
            session_version=int(credential.session_version),
        )
        request = SimpleNamespace(cookies={SESSION_COOKIE: token})
        bot = SimpleNamespace(get_guild=lambda _guild_id: None)

        principal = await resolve_principal(  # type: ignore[arg-type]
            request,
            bot,
            guild_id=77,
        )

        self.assertIsNotNone(principal)
        self.assertEqual(principal.account_tier, "zero")
        self.assertEqual(principal.user_id, 4242)

    async def test_shared_identity_does_not_bypass_admin_permissions(self) -> None:
        principal = self._principal(user_id=42)
        principal.member.guild_permissions.administrator = False
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=principal),
            ):
                response = await client.get(
                    "/login?next=/admin",
                    allow_redirects=False,
                )

            self.assertEqual(response.status, 200)
            self.assertIn("T-Mod Account", await response.text())
        finally:
            await client.close()

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
            self.assertIn('id="experience-switch"', index_text)
            self.assertIn('id="ballot-screen"', index_text)
            self.assertIn('id="ballot-choices"', index_text)
            self.assertIn('id="ballot-sound-toggle"', index_text)
            self.assertIn('id="ballot-deadline"', index_text)
            self.assertIn('id="ballot-discussion"', index_text)
            self.assertIn('id="ballot-request-discussion"', index_text)
            self.assertIn('id="ballot-discussion-message"', index_text)
            self.assertIn('id="observer-schedule"', index_text)
            self.assertIn('/assets/chamber.css', index_text)
            self.assertIn('id="vote-confirm-dialog"', index_text)
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
            self.assertEqual(
                index.headers["Permissions-Policy"],
                "camera=(), geolocation=(), payment=(), usb=()",
            )
            self.assertIn('href="/assets/fonts.css"', index_text)

            script = await client.get("/assets/app.js")
            self.assertEqual(script.status, 200)
            self.assertIn("max-age=300", script.headers["Cache-Control"])
            script_text = await script.text()
            self.assertIn(
                "document.body.dataset.outcome",
                script_text,
            )
            self.assertIn("if (!document.hidden && !dashboard.hidden)", script_text)
            self.assertIn("if (document.hidden) return", script_text)
            self.assertIn('window.addEventListener("pagehide"', script_text)
            self.assertIn("payloadSignature", script_text)
            self.assertIn('id="data-loading"', index_text)
            self.assertIn("requestedBillId", script_text)
            self.assertIn('"participant_vote"', script_text)
            self.assertIn("renderBallot", script_text)
            self.assertIn("renderBallotDiscussion", script_text)
            self.assertIn('"post_discussion_message"', script_text)
            self.assertIn("maybePlayConsensusCue", script_text)
            self.assertNotIn("innerHTML", script_text)
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
            discussion_stylesheet = await client.get("/assets/consensus-v4.css")
            self.assertEqual(discussion_stylesheet.status, 200)
            self.assertIn(
                ".ballot-discussion-composer",
                await discussion_stylesheet.text(),
            )
            self.assertIn("color-scheme: dark", stylesheet_text)
            self.assertIn("background-color: #080b0c", stylesheet_text)
            self.assertIn("min-height: 100dvh", stylesheet_text)
            self.assertIn(".experience-switch", stylesheet_text)
            self.assertIn(".ballot-screen", stylesheet_text)
            self.assertIn(".ballot-choice", stylesheet_text)

            login_page = await client.get("/login")
            self.assertEqual(login_page.status, 200)
            login_text = await login_page.text()
            self.assertIn("T·ID", login_text)
            self.assertIn('name="pin"', login_text)

            host_redirect = await client.get("/host", allow_redirects=False)
            self.assertEqual(host_redirect.status, 303)
            self.assertEqual(host_redirect.headers["Location"], "/login?next=/host")
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self._principal()),
            ):
                host_page = await client.get("/host")
            self.assertEqual(host_page.status, 200)
            host_text = await host_page.text()
            self.assertIn("машина ведения заседания", host_text)
            self.assertIn('id="host-live-text"', host_text)
            self.assertIn('id="host-alternate-live"', host_text)
            self.assertIn('id="host-agenda-list"', host_text)

            host_script = await client.get("/assets/host.js")
            self.assertEqual(host_script.status, 200)
            host_script_text = await host_script.text()
            self.assertIn("derivePrompt", host_script_text)
            self.assertIn("promptSpeechVariants", host_script_text)
            self.assertIn("choosePromptVariant", host_script_text)
            self.assertIn("stableVariantIndex", host_script_text)
            self.assertIn("hydrateAgenda", host_script_text)
            self.assertIn("schedulePoll", host_script_text)
            self.assertIn("1500", host_script_text)
            self.assertNotIn("innerHTML", host_script_text)

            host_stylesheet = await client.get("/assets/host.css")
            self.assertEqual(host_stylesheet.status, 200)
            host_stylesheet_text = await host_stylesheet.text()
            self.assertIn(".host-live-text", host_stylesheet_text)
            self.assertIn("prefers-reduced-motion", host_stylesheet_text)
            self.assertIn("min-height: 100dvh", host_stylesheet_text)

            admin = await client.get("/admin")
            self.assertEqual(admin.status, 200)
            admin_text = await admin.text()
            self.assertIn("Ядерный Реактор", admin_text)
            self.assertIn(
                'class="icon-button panel-switch" href="https://consensus.tvr.lat/"',
                admin_text,
            )
            self.assertIn('id="reactor-link"', index_text)
            self.assertIn("host-script-link", script_text)
            self.assertIn('href="/assets/favicon.svg"', index_text)
            self.assertIn('src="/assets/tab-signal.js"', index_text)
            self.assertIn(
                "html,body{background:#080b0c;color:#edf1eb}",
                admin_text,
            )
            self.assertIn('id="screen-treasury"', admin_text)
            self.assertIn('id="screen-craft"', admin_text)
            self.assertIn('id="screen-discord"', admin_text)
            self.assertIn('id="screen-market"', admin_text)
            self.assertIn('id="consensus-schedule-form"', admin_text)
            self.assertIn('id="schedule-invite"', admin_text)
            self.assertIn('id="screen-sgl"', admin_text)
            self.assertIn('id="screen-media"', admin_text)
            self.assertIn('id="screen-system"', admin_text)
            self.assertIn('id="screen-minecraft"', admin_text)
            self.assertIn('data-minecraft-tab="files"', admin_text)
            self.assertIn('id="minecraft-file-list"', admin_text)
            self.assertIn('id="minecraft-plugin-list"', admin_text)
            self.assertIn('id="minecraft-console-form"', admin_text)
            self.assertIn('id="minecraft-backup-list"', admin_text)
            self.assertIn('id="minecraft-log-output"', admin_text)
            self.assertIn('id="minecraft-editor-dialog"', admin_text)
            self.assertIn('id="command-dialog"', admin_text)
            self.assertIn('id="reactor-inbox-dialog"', admin_text)
            self.assertIn('id="reactor-confirm-dialog"', admin_text)
            self.assertIn('href="/assets/favicon.svg"', admin_text)
            self.assertIn('src="/assets/tab-signal.js"', admin_text)
            self.assertIn('id="copy-section-link"', admin_text)
            self.assertIn('id="copy-detail-link"', admin_text)
            self.assertIn('id="notification-toggle"', admin_text)
            self.assertIn('id="gate-state"', admin_text)
            self.assertIn('class="gate-check"', admin_text)
            self.assertIn('id="toast-title"', admin_text)
            self.assertIn('href="/assets/fonts.css"', admin_text)

            reactor = await client.get("/reactor")
            self.assertEqual(reactor.status, 200)
            reactor_text = await reactor.text()
            self.assertIn("Личный Реактор", reactor_text)
            self.assertIn('id="portal-canvas"', reactor_text)
            self.assertIn('id="treasury"', reactor_text)
            self.assertIn('id="legislation"', reactor_text)
            self.assertIn('id="editor-workspace"', reactor_text)
            self.assertIn('href="/assets/portal-theme.css?v=3"', reactor_text)
            self.assertIn('class="editor-console-bar"', reactor_text)
            self.assertIn('class="preview-seal"', reactor_text)
            self.assertNotIn('data-portal-widget="market"', reactor_text)
            portal_theme = await client.get("/assets/portal-theme.css")
            self.assertEqual(portal_theme.status, 200)
            portal_theme_text = await portal_theme.text()
            self.assertIn('@font-face', portal_theme_text)
            self.assertIn('font-family: "Unbounded"', portal_theme_text)
            self.assertIn("@keyframes core-pulse", portal_theme_text)
            self.assertIn(".preview-paper", portal_theme_text)
            self.assertIn("@media (prefers-reduced-motion: reduce)", portal_theme_text)
            self.assertNotRegex(
                portal_theme_text,
                r"font-size:\s*(?:7|8|9)px",
            )
            portal_font = await client.get("/assets/manrope-cyrillic.woff2")
            self.assertEqual(portal_font.status, 200)
            self.assertEqual(portal_font.content_type, "font/woff2")
            self.assertIn("immutable", portal_font.headers["Cache-Control"])
            reactor_script = await client.get("/assets/reactor.js")
            self.assertEqual(reactor_script.status, 200)
            reactor_script_text = await reactor_script.text()
            self.assertIn("/api/admin/reactor/search", reactor_script_text)
            self.assertIn(
                "/api/admin/reactor/minecraft/upload",
                reactor_script_text,
            )
            self.assertIn(
                "/api/admin/reactor/minecraft/backups",
                reactor_script_text,
            )
            self.assertIn("silent && document.hidden", reactor_script_text)
            self.assertIn('window.addEventListener("pagehide"', reactor_script_text)
            self.assertIn("switchMinecraftTab", reactor_script_text)
            self.assertNotIn("innerHTML", reactor_script_text)
            tab_signal = await client.get("/assets/tab-signal.js")
            self.assertEqual(tab_signal.status, 200)
            tab_signal_text = await tab_signal.text()
            self.assertIn("TModTabSignal", tab_signal_text)
            self.assertIn("visibilitychange", tab_signal_text)
            self.assertNotIn("innerHTML", tab_signal_text)
            favicon = await client.get("/assets/favicon.svg")
            self.assertEqual(favicon.status, 200)
            self.assertEqual(favicon.content_type, "image/svg+xml")
            self.assertIn("<svg", await favicon.text())
            automatic_favicon = await client.get("/favicon.ico")
            self.assertEqual(automatic_favicon.status, 200)
            self.assertIn("signal-composer-head", admin_text)

            admin_script = await client.get("/assets/admin.js")
            self.assertEqual(admin_script.status, 200)
            admin_script_text = await admin_script.text()
            self.assertIn("/api/admin/overview", admin_script_text)
            self.assertIn("/api/admin/media/command", admin_script_text)
            self.assertIn("/api/admin/link/", admin_script_text)
            self.assertIn("parseAdminRoute", admin_script_text)
            self.assertIn("const REFRESH_INTERVALS = Object.freeze", admin_script_text)
            self.assertIn("scheduleAutoRefresh", admin_script_text)
            self.assertIn("GLOBAL_ACTIVITY_INTERVAL = 60000", admin_script_text)
            self.assertIn("pollGlobalActivity", admin_script_text)
            self.assertIn("playNotificationSound", admin_script_text)
            self.assertIn("activitySignature", admin_script_text)
            self.assertIn("loadedSections: new Set()", admin_script_text)
            self.assertIn(
                'fetchJSON("/api/admin/access/self")',
                admin_script_text,
            )
            self.assertIn("TModReactor?.activateMinecraft", admin_script_text)
            self.assertIn("market-signal-list", admin_script_text)
            self.assertIn("/api/admin/bills/schedule", admin_script_text)
            self.assertIn("renderConsensusSchedule", admin_script_text)
            self.assertIn('!byId("admin-shell").hidden', admin_script_text)
            self.assertIn("!document.hidden", admin_script_text)
            self.assertIn(
                'error.payload?.error === "too_many_attempts"', admin_script_text
            )
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
            self.assertIn('"Unbounded"', admin_stylesheet_text)
            self.assertIn(".signal-card", admin_stylesheet_text)
            self.assertIn(".toast-copy", admin_stylesheet_text)
            self.assertIn("@keyframes gateOrbit", admin_stylesheet_text)
            self.assertNotIn("Georgia", admin_stylesheet_text)
            shared_fonts = await client.get("/assets/fonts.css")
            self.assertEqual(shared_fonts.status, 200)
            shared_fonts_text = await shared_fonts.text()
            self.assertIn('font-family: "Manrope"', shared_fonts_text)
            self.assertIn('font-family: "Unbounded"', shared_fonts_text)
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

            public = await client.get("/api/state")
            self.assertEqual(public.status, 200)
            public_payload = await public.json()
            self.assertFalse(public_payload["viewer"]["authenticated"])
            self.assertEqual(public_payload["session"]["participants"], [])

            with patch(
                "modules.consensus_web._runtime_token", "test-access-token-123456"
            ):
                allowed = await client.get(
                    "/api/state",
                    headers={"Authorization": "Bearer test-access-token-123456"},
                )
            self.assertEqual(allowed.status, 200)
            payload = await allowed.json()
            self.assertEqual(payload["session"]["plenary_number"], 6)
            self.assertIn(
                payload["cache_state"], {"fresh", "refreshed", "stale"}
            )
            self.assertEqual(
                allowed.headers["X-T-Mod-Cache"], payload["cache_state"]
            )
            self.assertEqual(allowed.headers["Cache-Control"], "private, no-store")
            self.assertIn("app;dur=", allowed.headers["Server-Timing"])
            self.assertEqual(allowed.headers["X-Frame-Options"], "DENY")
        finally:
            await client.close()

    async def test_member_reactor_exposes_revisioned_bill_workspace_api(self) -> None:
        principal = self._principal(user_id=42)
        principal.member.guild_permissions.administrator = False
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=principal),
            ):
                catalog = await client.get("/api/reactor/legislation")
                rejected = await client.post(
                    "/api/reactor/legislation",
                    json={"action": "create"},
                )
                created = await client.post(
                    "/api/reactor/legislation",
                    json={"action": "create"},
                    headers={"X-CSRF-Token": principal.csrf_token},
                )
                workspace = (await created.json())["workspace"]
                saved = await client.post(
                    "/api/reactor/legislation",
                    json={
                        "action": "save",
                        "workspace_id": workspace["id"],
                        "expected_revision": workspace["revision"],
                        "idea": "Создать справочник участников Товарищества.",
                        "desired_outcome": "Упростить знакомство и координацию.",
                        "constraints_text": "Без закрытых данных.",
                        "title": "О справочнике участников",
                        "summary": "Создать единый открытый справочник участников Товарищества.",
                        "materials": "",
                        "implementation_plan": "Подготовить форму и открыть справочник.",
                        "leadership_actions": "Назначить ответственного за актуальность.",
                    },
                    headers={"X-CSRF-Token": principal.csrf_token},
                )

            self.assertEqual(catalog.status, 200)
            self.assertIn("bills", await catalog.json())
            self.assertEqual(rejected.status, 403)
            self.assertEqual(created.status, 200)
            self.assertEqual(saved.status, 200)
            saved_workspace = (await saved.json())["workspace"]
            self.assertTrue(saved_workspace["ready"])
            self.assertGreater(saved_workspace["revision"], workspace["revision"])
        finally:
            await client.close()

    async def test_member_reactor_completes_required_web_onboarding(self) -> None:
        principal = self._principal(user_id=42)
        storage.require_member_directory(77, 42, prompted=True)
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=principal),
            ):
                before = await client.get("/api/reactor/home")
                completed = await client.post(
                    "/api/reactor/onboarding",
                    json={
                        "action": "complete",
                        "preferred_name": "Иван",
                        "character_nickname": "Saul Goodman",
                        "character_static": "263345",
                        "biography": "Участник Товарищества",
                        "contribution": "Работаю с правовыми проектами",
                        "responsibilities": "Законодательство и заседания",
                        "membership_since": "2026-08-11",
                    },
                    headers={"X-CSRF-Token": principal.csrf_token},
                )
                after = await client.get("/api/reactor/home?fresh=1")

            self.assertTrue((await before.json())["onboarding"]["required"])
            self.assertEqual(completed.status, 200)
            payload = await completed.json()
            self.assertEqual(payload["profile"]["preferred_name"], "Иван")
            self.assertEqual(payload["characters"][0]["static_id"], "263345")
            self.assertTrue(payload["nickname"]["exempt"])
            refreshed = await after.json()
            self.assertFalse(refreshed["onboarding"]["required"])
            self.assertEqual(refreshed["mandate"]["preferred_name"], "Иван")
        finally:
            await client.close()

    async def test_persistent_login_uses_profile_credential_and_admin_role(
        self,
    ) -> None:
        storage.add_profile_character(77, 42, "Operator Test", "42001")
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
        bot = SimpleNamespace(
            get_guild=lambda guild_id: guild if guild_id == 77 else None
        )
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
            self.assertIn("tmod_account_session=", accepted.headers["Set-Cookie"])

            member.guild_permissions.administrator = False
            not_admin = await client.post(
                "/auth/login?next=/admin",
                data={"login": "operator", "pin": "12345678"},
                allow_redirects=False,
            )
            self.assertEqual(not_admin.status, 303)
            self.assertIn("error=administrator", not_admin.headers["Location"])

            storage.web_set_section_grant(
                77,
                42,
                "craft",
                enabled=True,
                granted_by_id=1,
            )
            delegated = await client.post(
                "/auth/login?next=/admin",
                data={"login": "operator", "pin": "12345678"},
                allow_redirects=False,
            )
            self.assertEqual(delegated.status, 303)
            self.assertEqual(delegated.headers["Location"], "/admin")
        finally:
            await client.close()

    async def test_zero_account_can_open_only_whitelisted_atlas(self) -> None:
        storage.add_profile_character(77, 4242, "External User", "424200")
        storage.configure_web_credential(77, 4242, "external.user", "12345678")
        storage.web_set_section_grant(
            77,
            4242,
            "atlas_ai",
            enabled=True,
            granted_by_id=1,
        )
        bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        app = create_consensus_web_app(bot, guild_id=77)  # type: ignore[arg-type]
        async with TestClient(TestServer(app)) as client:
            accepted = await client.post(
                "/auth/login?next=/atlas",
                data={"login": "external.user", "pin": "12345678"},
                allow_redirects=False,
            )
            self.assertEqual(accepted.status, 303)
            self.assertEqual(accepted.headers["Location"], "/atlas")
            atlas = await client.get("/api/atlas/bootstrap")
            atlas_payload = await atlas.json()
            protected = await client.get("/api/state")
            protected_payload = await protected.json()

        self.assertEqual(atlas.status, 200)
        self.assertEqual(atlas_payload["viewer"]["account_tier"], "zero")
        self.assertEqual(protected.status, 200)
        self.assertFalse(protected_payload["viewer"]["authenticated"])

    async def test_three_bad_pins_warn_owner_and_require_discord_reset(self) -> None:
        storage.add_profile_character(77, 42, "Operator Test", "42001")
        storage.configure_web_credential(77, 42, "operator", "12345678")
        member = SimpleNamespace(
            id=42,
            display_name="Оператор",
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
            send=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=77,
            name="Товарищество",
            get_member=lambda user_id: None,
            fetch_member=AsyncMock(return_value=member),
        )
        bot = SimpleNamespace(
            get_guild=lambda guild_id: guild if guild_id == 77 else None
        )
        app = create_consensus_web_app(bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            locations = []
            for _ in range(3):
                response = await client.post(
                    "/auth/login?next=/reactor",
                    data={"login": "operator", "pin": "00000000"},
                    allow_redirects=False,
                )
                locations.append(response.headers["Location"])

            self.assertIn("error=invalid", locations[0])
            self.assertIn("error=invalid", locations[1])
            self.assertIn("error=reset_required", locations[2])
            self.assertEqual(member.send.await_count, 3)
            self.assertGreaterEqual(guild.fetch_member.await_count, 3)
            credential = storage.get_web_credential(77, 42)
            self.assertIsNotNone(credential)
            self.assertTrue(credential.reset_required)
            inbox = storage.reactor_list_notifications(77, 42)
            self.assertEqual(inbox["unread"], 1)
            self.assertEqual(inbox["items"][0]["severity"], "critical")

            blocked = await client.post(
                "/auth/login?next=/reactor",
                data={"login": "operator", "pin": "12345678"},
                allow_redirects=False,
            )
            self.assertIn("error=reset_required", blocked.headers["Location"])

            storage.configure_web_credential(77, 42, "operator", "87654321")
            accepted = await client.post(
                "/auth/login?next=/reactor",
                data={"login": "operator", "pin": "87654321"},
                allow_redirects=False,
            )
            self.assertEqual(accepted.headers["Location"], "/reactor")
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
                "section_access_required",
            )

            storage.web_set_section_grant(
                77,
                2,
                "craft",
                enabled=True,
                granted_by_id=1,
            )
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=regular_member),
            ):
                delegated_craft = await client.get("/api/admin/crafts")
                delegated_finance = await client.get("/api/admin/finance")
                delegated_access = await client.get("/api/admin/access/self")
            self.assertEqual(delegated_craft.status, 200)
            self.assertFalse(
                (await delegated_craft.json())["viewer"]["administrator"]
            )
            self.assertEqual(delegated_finance.status, 403)
            self.assertEqual((await delegated_access.json())["sections"], ["craft"])

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
                reactor_attention = await client.get("/api/admin/reactor/attention")
                reactor_health = await client.get("/api/admin/reactor/health")
                reactor_search = await client.get(
                    "/api/admin/reactor/search?q=следующий"
                )
                reactor_events = await client.get("/api/admin/reactor/events")
                reactor_home = await client.get("/api/reactor/home")
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
                reactor_attention,
                reactor_health,
                reactor_search,
                reactor_events,
                reactor_home,
                linked_bill,
            ):
                self.assertEqual(response.status, 200)
                self.assertTrue((await response.json())["viewer"]["administrator"])
            overview_payload = await overview.json()
            self.assertIn("counts", overview_payload)
            self.assertIn("system", overview_payload)
            self.assertIn(
                overview_payload["cache_state"], {"fresh", "refreshed", "stale"}
            )
            self.assertIn(
                overview.headers["X-T-Mod-Cache"],
                {"fresh", "refreshed", "stale"},
            )
            self.assertIn("capabilities", await registry.json())
            self.assertEqual((await market.json())["server_id"], "RU15")
            self.assertEqual(
                (await bills.json())["items"][0]["summary"], self.bill.summary
            )
            system_payload = await system.json()
            self.assertIn("outbox_status", system_payload)
            self.assertIn("database", system_payload["reliability"])
            self.assertIn("domains", system_payload["reliability"])
            self.assertIn("update", system_payload["reliability"])
            attention_payload = await reactor_attention.json()
            health_payload = await reactor_health.json()
            self.assertIn("health", attention_payload)
            self.assertIn(
                attention_payload["cache_state"],
                {"fresh", "refreshed", "stale"},
            )
            self.assertIn("components", health_payload)
            self.assertIn(
                health_payload["cache_state"],
                {"fresh", "refreshed", "stale"},
            )
            self.assertTrue((await reactor_search.json())["items"])
            self.assertIn("items", await reactor_events.json())
            reactor_home_payload = await reactor_home.json()
            self.assertIn(
                reactor_home_payload["cache_state"],
                {"fresh", "refreshed", "stale"},
            )
            self.assertEqual(
                reactor_home.headers["X-T-Mod-Cache"],
                reactor_home_payload["cache_state"],
            )
            self.assertIn("consensus", reactor_home_payload)
            self.assertIn("treasury", reactor_home_payload)
            self.assertIn("legislation", reactor_home_payload)
            self.assertIn("bills", reactor_home_payload["legislation"])
            self.assertNotIn("market_alerts", reactor_home_payload)
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

    async def test_section_access_notifies_recipient_by_dm(self) -> None:
        recipient = SimpleNamespace(
            id=2,
            display_name="Получатель",
            send=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=77,
            name="Товарищество",
            get_member=lambda user_id: recipient if int(user_id) == 2 else None,
            fetch_member=AsyncMock(return_value=recipient),
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        app = create_consensus_web_app(bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self._principal()),
            ):
                response = await client.post(
                    "/api/admin/access",
                    headers={"X-CSRF-Token": "csrf-test-token"},
                    json={"user_id": 2, "section": "minecraft", "enabled": True},
                )
            self.assertEqual(response.status, 200)
            payload = await response.json()
            self.assertTrue(payload["changed"])
            self.assertTrue(payload["dm_sent"])
            self.assertEqual(payload["grants"][0]["section_label"], "Minecraft")
            recipient.send.assert_awaited_once()
            embed = recipient.send.await_args.kwargs["embed"]
            self.assertIn("Minecraft", embed.description)

            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self._principal()),
            ):
                duplicate = await client.post(
                    "/api/admin/access",
                    headers={"X-CSRF-Token": "csrf-test-token"},
                    json={"user_id": 2, "section": "minecraft", "enabled": True},
                )
            duplicate_payload = await duplicate.json()
            self.assertFalse(duplicate_payload["changed"])
            self.assertFalse(duplicate_payload["dm_sent"])
            self.assertIn("уже был выдан", duplicate_payload["message"])
            recipient.send.assert_awaited_once()
        finally:
            await client.close()

    async def test_section_access_stays_successful_when_secondary_delivery_fails(self) -> None:
        recipient = SimpleNamespace(
            id=2,
            display_name="Получатель",
            send=AsyncMock(side_effect=RuntimeError("discord transport lost")),
        )
        guild = SimpleNamespace(
            id=77,
            name="Товарищество",
            get_member=lambda user_id: recipient if int(user_id) == 2 else None,
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        app = create_consensus_web_app(bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with (
                patch(
                    "modules.consensus_web.resolve_principal",
                    AsyncMock(return_value=self._principal()),
                ),
                patch(
                    "modules.consensus_admin_web.activity_storage.bot_record_action",
                    side_effect=RuntimeError("audit temporarily busy"),
                ),
            ):
                response = await client.post(
                    "/api/admin/access",
                    headers={"X-CSRF-Token": "csrf-test-token"},
                    json={"user_id": 2, "section": "minecraft", "enabled": True},
                )
            payload = await response.json()
            self.assertEqual(response.status, 200)
            self.assertTrue(payload["changed"])
            self.assertFalse(payload["dm_sent"])
            self.assertIn("аудит", payload["warning"])
            self.assertEqual(
                [item["section"] for item in storage.web_section_grants(77, 2)],
                ["minecraft"],
            )
        finally:
            await client.close()

    async def test_section_access_rejects_non_admin_csrf_and_unknown_section(self) -> None:
        guild = SimpleNamespace(id=77, name="Товарищество", get_member=lambda _id: None)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        app = create_consensus_web_app(bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            non_admin = self._principal(user_id=9)
            non_admin.member.guild_permissions.administrator = False
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=non_admin),
            ):
                forbidden = await client.post(
                    "/api/admin/access",
                    headers={"X-CSRF-Token": "csrf-test-token"},
                    json={"user_id": 2, "section": "minecraft", "enabled": True},
                )
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=self._principal()),
            ):
                csrf = await client.post(
                    "/api/admin/access",
                    json={"user_id": 2, "section": "minecraft", "enabled": True},
                )
                invalid = await client.post(
                    "/api/admin/access",
                    headers={"X-CSRF-Token": "csrf-test-token"},
                    json={"user_id": 2, "section": "../../system", "enabled": True},
                )
            self.assertEqual(forbidden.status, 403)
            self.assertEqual(csrf.status, 403)
            self.assertEqual(invalid.status, 400)
            self.assertEqual(storage.web_section_grants(77, 2), [])
        finally:
            await client.close()

    async def test_minecraft_file_manager_is_admin_only_and_sandboxed(self) -> None:
        minecraft_root = Path(self.temp_dir.name) / "minecraft"
        (minecraft_root / "plugins").mkdir(parents=True)
        (minecraft_root / "server.properties").write_text(
            "motd=Old\nrcon.password=never-expose\n",
            encoding="utf-8",
        )
        administrator = self._principal()
        with patch.dict(
            "os.environ",
            {"MINECRAFT_DATA_DIR": str(minecraft_root)},
        ):
            app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                denied = await client.get("/api/admin/reactor/minecraft/files")
                self.assertEqual(denied.status, 401)
                with patch(
                    "modules.consensus_web.resolve_principal",
                    AsyncMock(return_value=administrator),
                ):
                    listing = await client.get(
                        "/api/admin/reactor/minecraft/files?path=",
                    )
                    traversal = await client.get(
                        "/api/admin/reactor/minecraft/files?path=../outside",
                    )
                    opened = await client.get(
                        "/api/admin/reactor/minecraft/file?path=server.properties",
                    )
                    protected_download = await client.get(
                        "/api/admin/reactor/minecraft/download?path=server.properties",
                    )
                    opened_payload = await opened.json()
                    saved = await client.post(
                        "/api/admin/reactor/minecraft/file",
                        headers={
                            "X-CSRF-Token": "csrf-test-token",
                            "X-Idempotency-Key": "minecraft-save-config-1",
                        },
                        json={
                            "action": "save",
                            "path": "server.properties",
                            "content": opened_payload["item"]["content"].replace(
                                "motd=Old",
                                "motd=New",
                            ),
                            "etag": opened_payload["item"]["etag"],
                        },
                    )
                    form = FormData()
                    form.add_field("path", "plugins")
                    form.add_field("overwrite", "false")
                    form.add_field(
                        "file",
                        b"test-plugin",
                        filename="PanelTest.jar",
                        content_type="application/java-archive",
                    )
                    uploaded = await client.post(
                        "/api/admin/reactor/minecraft/upload",
                        headers={
                            "X-CSRF-Token": "csrf-test-token",
                            "X-Idempotency-Key": "minecraft-upload-plugin-1",
                        },
                        data=form,
                    )
                    plugins = await client.get(
                        "/api/admin/reactor/minecraft/plugins",
                    )
                    backup = await client.post(
                        "/api/admin/reactor/minecraft/backups",
                        headers={
                            "X-CSRF-Token": "csrf-test-token",
                            "X-Idempotency-Key": "minecraft-create-backup-1",
                        },
                        json={"action": "create", "label": "api-test"},
                    )

                self.assertEqual(listing.status, 200)
                self.assertEqual(traversal.status, 400)
                self.assertEqual(protected_download.status, 403)
                self.assertNotIn("never-expose", opened_payload["item"]["content"])
                self.assertEqual(saved.status, 200)
                self.assertIn(
                    "rcon.password=never-expose",
                    (minecraft_root / "server.properties").read_text(encoding="utf-8"),
                )
                self.assertEqual(uploaded.status, 200)
                self.assertTrue(
                    (minecraft_root / "plugins" / "PanelTest.jar").is_file()
                )
                self.assertEqual(
                    (await plugins.json())["items"][0]["display_name"], "PanelTest"
                )
                self.assertEqual(backup.status, 200)
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

    async def test_reactor_domain_commands_share_finance_craft_and_bill_state(self) -> None:
        recipe = storage.craft_create_recipe(
            guild_id=77,
            product_name="Панельный сплав",
            treasury_cost_per_unit=0,
            duration_minutes_per_unit=5,
            max_batch_size=10,
            materials=[("Железо", 2)],
            created_by_id=1,
            created_by_display="Администратор",
        )
        plan = storage.craft_create_plan(
            guild_id=77,
            recipe_id=recipe["id"],
            channel_id=88,
            attempts_total=5,
            responsible_id=1,
            responsible_display="Администратор",
            created_by_id=1,
            created_by_display="Администратор",
        )
        admin_bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=88,
            author_id=6,
            author_display="Редактор",
            title="Проект для административной правки",
            summary="Этот проект не находится в активной сессии консенсуса.",
            materials=None,
        )
        storage.tvrs_mark_bill_status(self.bill.id, "voting")
        material = plan["materials"][0]
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        administrator = self._principal()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=administrator),
            ):
                finance = await client.post(
                    "/api/admin/finance/command",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "reactor-finance-snapshot-1",
                    },
                    json={"action": "snapshot", "amount": 500_000, "confirmed": True},
                )
                craft = await client.post(
                    "/api/admin/crafts/command",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "reactor-craft-purchase-1",
                    },
                    json={
                        "action": "purchase",
                        "plan_id": plan["id"],
                        "plan_material_id": material["id"],
                        "quantity": 12,
                        "total_cost": 0,
                        "confirmed": True,
                    },
                )
                recipe_from_web = await client.post(
                    "/api/admin/crafts/command",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "reactor-craft-recipe-1",
                    },
                    json={
                        "action": "create_recipe",
                        "product_name": "Рецепт из Реактора",
                        "treasury_cost_per_unit": 100,
                        "duration_minutes_per_unit": 3,
                        "max_batch_size": 4,
                        "materials": [
                            {"material_name": "Сталь", "quantity_per_unit": 2},
                        ],
                        "confirmed": True,
                    },
                )
                bill = await client.post(
                    "/api/admin/bills/command",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "reactor-bill-update-1",
                    },
                    json={
                        "action": "update",
                        "bill_number": admin_bill.bill_number,
                        "field": "title",
                        "value": "Обновлено из Ядерного Реактора",
                        "confirmed": True,
                    },
                )
                locked_bill = await client.post(
                    "/api/admin/bills/command",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "reactor-bill-locked-1",
                    },
                    json={
                        "action": "update",
                        "bill_number": self.bill.bill_number,
                        "field": "title",
                        "value": "Это изменение должно быть заблокировано",
                        "confirmed": True,
                    },
                )

            self.assertEqual(finance.status, 200)
            self.assertEqual(craft.status, 200)
            self.assertEqual(recipe_from_web.status, 200)
            self.assertEqual(bill.status, 200)
            self.assertEqual(locked_bill.status, 400)
            self.assertIn("защищён", (await locked_bill.json())["message"])
            self.assertEqual(
                storage.finance_get_latest_state(77)["estimated_balance"],
                500_000,
            )
            self.assertEqual(
                storage.craft_get_plan(plan["id"])["materials"][0]["stock_quantity"],
                12,
            )
            self.assertEqual(
                storage.tvrs_get_bill_by_number(77, admin_bill.bill_number)["title"],
                "Обновлено из Ядерного Реактора",
            )
            self.assertTrue(
                any(
                    item["product_name"] == "Рецепт из Реактора"
                    for item in storage.craft_list_recipes(77, active_only=False)
                )
            )
        finally:
            await client.close()

    async def test_reactor_can_plan_and_cancel_consensus(self) -> None:
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        administrator = self._principal()
        scheduled_for = (datetime.now() + timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        try:
            with (
                patch(
                    "modules.consensus_web.resolve_principal",
                    AsyncMock(return_value=administrator),
                ),
                patch(
                    "modules.consensus_admin_web.sync_schedule_discord_event",
                    AsyncMock(side_effect=lambda _guild, schedule: schedule),
                ),
                patch(
                    "modules.consensus_admin_web.cancel_schedule_discord_event",
                    AsyncMock(),
                ),
            ):
                created = await client.post(
                    "/api/admin/bills/schedule",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "reactor-consensus-schedule-create-1",
                    },
                    json={
                        "action": "save",
                        "title": "Девятый пленарный консенсус",
                        "plenary_number": 9,
                        "scheduled_for": scheduled_for,
                        "duration_minutes": 120,
                        "voice_channel_id": 88,
                        "description": "Проверка повестки.",
                        "invitation_text": "Просим прибыть заранее.",
                    },
                )
                registry = await client.get("/api/admin/bills")
                created_payload = await created.json()
                cancelled = await client.post(
                    "/api/admin/bills/schedule",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "reactor-consensus-schedule-cancel-1",
                    },
                    json={"action": "cancel", "confirmed": True},
                )

            self.assertEqual(created.status, 200)
            self.assertEqual(created_payload["schedule"]["plenary_number"], 9)
            self.assertEqual(registry.status, 200)
            registry_payload = await registry.json()
            self.assertTrue(registry_payload["can_manage_schedule"])
            self.assertEqual(registry_payload["schedule"]["title"], "Девятый пленарный консенсус")
            self.assertEqual(cancelled.status, 200)
            self.assertEqual((await cancelled.json())["schedule"]["status"], "cancelled")
            self.assertIsNone(storage.get_upcoming_consensus_schedule(77))
        finally:
            await client.close()

    async def test_delegated_craft_operator_can_act_only_inside_granted_section(self) -> None:
        recipe = storage.craft_create_recipe(
            guild_id=77,
            product_name="Делегированный крафт",
            treasury_cost_per_unit=0,
            duration_minutes_per_unit=5,
            max_batch_size=10,
            materials=[("Медь", 1)],
            created_by_id=1,
            created_by_display="Администратор",
        )
        plan = storage.craft_create_plan(
            guild_id=77,
            recipe_id=recipe["id"],
            channel_id=88,
            attempts_total=2,
            responsible_id=2,
            responsible_display="Оператор",
            created_by_id=1,
            created_by_display="Администратор",
        )
        storage.web_set_section_grant(77, 2, "craft", enabled=True, granted_by_id=1)
        operator = self._principal(user_id=2)
        operator.member.guild_permissions.administrator = False
        app = create_consensus_web_app(self.bot, guild_id=77)  # type: ignore[arg-type]
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with patch(
                "modules.consensus_web.resolve_principal",
                AsyncMock(return_value=operator),
            ):
                allowed = await client.post(
                    "/api/admin/crafts/command",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "delegated-craft-inventory-1",
                    },
                    json={
                        "action": "inventory",
                        "plan_id": plan["id"],
                        "material_quantities": {
                            str(plan["materials"][0]["id"]): 2,
                        },
                        "product_quantity": 0,
                        "confirmed": True,
                    },
                )
                denied = await client.post(
                    "/api/admin/finance/command",
                    headers={
                        "X-CSRF-Token": "csrf-test-token",
                        "X-Idempotency-Key": "delegated-finance-denied-1",
                    },
                    json={"action": "snapshot", "amount": 1, "confirmed": True},
                )
            self.assertEqual(allowed.status, 200)
            self.assertEqual(denied.status, 403)
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
            self.assertEqual(
                storage.broadcast_report(guild_id=77)["recipient_count"], 1
            )
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

    async def test_bill_catalog_and_detail_expose_only_guild_public_record(
        self,
    ) -> None:
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
            with patch(
                "modules.consensus_web._runtime_token", "test-access-token-123456"
            ):
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

    async def test_simulation_is_selectable_without_masking_live_consensus(
        self,
    ) -> None:
        simulation = ConsensusSimulation(
            guild_id=77,
            leader_id=100,
            leader_display="Учебный ведущий",
        )
        simulation.confirm_all()
        simulation.begin_voting()
        simulation.open_voting()
        simulation.cast_leader_vote("yes")
        register_consensus_simulation(simulation)

        default_state = await build_consensus_web_state(self.bot, 77)  # type: ignore[arg-type]
        simulation_state = await build_consensus_web_state(  # type: ignore[arg-type]
            self.bot,
            77,
            mode="simulation",
            principal=self._principal(user_id=100),
        )

        self.assertEqual(default_state["mode"], "live")
        self.assertEqual(default_state["session"]["key"], "web-test")
        self.assertEqual(simulation_state["mode"], "simulation")
        self.assertTrue(simulation_state["active"])
        self.assertTrue(simulation_state["session"]["key"].startswith("simulation:"))
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
            with patch(
                "modules.consensus_web._runtime_token", "test-access-token-123456"
            ):
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

        await execute_consensus_web_command(  # type: ignore[arg-type]
            self.bot,
            self.bot.get_guild(77),
            principal,
            mode="simulation",
            action="start_vote",
            session_key=simulation.session.session_key,
            revision=simulation.session.revision,
            bill_id=0,
            payload={},
        )
        self.assertEqual(simulation.session.stage, "presentation")
        self.assertIn(
            "open_vote",
            consensus_web_capabilities(
                mode="simulation",
                session=simulation.session,
                principal=principal,
            ),
        )
        await execute_consensus_web_command(  # type: ignore[arg-type]
            self.bot,
            self.bot.get_guild(77),
            principal,
            mode="simulation",
            action="open_vote",
            session_key=simulation.session.session_key,
            revision=simulation.session.revision,
            bill_id=int(simulation.session.current_bill["id"]),
            payload={},
        )
        self.assertEqual(simulation.session.stage, "voting")

    async def test_invited_simulation_senator_can_vote_from_web(self) -> None:
        simulation = ConsensusSimulation(
            guild_id=77,
            leader_id=1,
            leader_display="Ведущий",
            invited_participants=(
                LiveParticipant(2, "Сенатор 2", "<@2>", "senator"),
                LiveParticipant(3, "Сенатор 3", "<@3>", "senator"),
            ),
        )
        simulation.confirm_participant(2)
        simulation.confirm_participant(3)
        simulation.confirm_next()
        simulation.begin_voting()
        simulation.open_voting()
        register_consensus_simulation(simulation)
        principal = self._principal(2)

        self.assertEqual(
            consensus_web_capabilities(
                mode="simulation",
                session=simulation.session,
                principal=principal,
            ),
            ["participant_vote", "request_discussion"],
        )
        state = await build_consensus_web_state(  # type: ignore[arg-type]
            self.bot,
            77,
            mode="simulation",
            principal=principal,
        )
        self.assertTrue(state["viewer"]["can_vote"])
        await execute_consensus_web_command(  # type: ignore[arg-type]
            self.bot,
            self.bot.get_guild(77),
            principal,
            mode="simulation",
            action="participant_vote",
            session_key=simulation.session.session_key,
            revision=simulation.session.revision,
            bill_id=int(simulation.session.current_bill["id"]),
            payload={"vote": "yes"},
        )
        self.assertEqual(simulation.session.votes[2], "yes")

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
