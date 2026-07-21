from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from modules.consensus_core import ConsensusStateError, LiveConsensusSession, LiveParticipant
from modules.consensus_runtime import registry as consensus_registry, session_lock
from modules.consensus_service import ConsensusActor
from modules.tvrs_config import TVRS_PERMANENT_CHAIR_ID
from modules.tvrs_consensus_views import (
    TVRSDiscussionTypeView,
    TVRSHostVoteView,
    TVRSRegistrationView,
    TVRSStartCurrentRosterConfirmView,
    TVRSVoteView,
)
from modules.tvrs_discussion import (
    check_realtime_quorum,
    end_discussion,
    start_discussion_channel,
    update_all_vote_dms,
)
from modules.tvrs_decision import apply_veto_for_actor


def _participant(user_id: int, kind: str) -> LiveParticipant:
    return LiveParticipant(
        user_id=user_id,
        display_name=f"Участник {user_id}",
        mention=f"<@{user_id}>",
        kind=kind,  # type: ignore[arg-type]
        confirmed=True,
        permanent=user_id == 1,
    )


def _discussion_session() -> LiveConsensusSession:
    session = LiveConsensusSession(
        session_key="77:faults",
        guild_id=77,
        channel_id=100,
        leader_id=1,
        leader_display="Ведущий",
        plenary_number=4,
        participants={
            1: _participant(1, "chair"),
            2: _participant(2, "chair"),
            3: _participant(3, "senator"),
        },
    )
    session.stage = "discussion_type"
    session.current_bill = {
        "id": 10,
        "bill_number": 9,
        "title": "Проверка отказоустойчивости",
    }
    return session


class ConsensusLifecycleFaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_veto_cannot_report_success_while_vote_finalization_owns_session(self) -> None:
        session = _discussion_session()
        session.stage = "finalizing"
        session.pending_action = {"kind": "vote", "bill_id": 10}

        with self.assertRaisesRegex(ConsensusStateError, "вето не применено"):
            await apply_veto_for_actor(
                SimpleNamespace(),  # type: ignore[arg-type]
                SimpleNamespace(id=77),  # type: ignore[arg-type]
                session,
                ConsensusActor(TVRS_PERMANENT_CHAIR_ID, "Председатель"),
                expected_bill_id=10,
            )

        self.assertEqual(session.stage, "finalizing")
        self.assertEqual(session.pending_action, {"kind": "vote", "bill_id": 10})

    async def test_after_result_does_not_auto_pause_when_people_leave_voice(self) -> None:
        session = _discussion_session()
        session.stage = "after_result"
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace()

        with (
            patch("modules.tvrs_discussion.session_voice_quorum_ready") as quorum,
            patch("modules.tvrs_discussion.pause_session", new=AsyncMock()) as pause,
        ):
            await check_realtime_quorum(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                session,
            )

        quorum.assert_not_called()
        pause.assert_not_awaited()

    async def test_automatic_quorum_pause_resumes_when_quorum_returns(self) -> None:
        session = _discussion_session()
        session.stage = "paused"
        session.previous_stage = "voting"
        session.pause_is_automatic = True

        with (
            patch(
                "modules.tvrs_discussion.session_voice_quorum_ready",
                return_value=(True, "Кворум восстановлен"),
            ),
            patch("modules.tvrs_discussion.resume_session", new=AsyncMock()) as resume,
        ):
            await check_realtime_quorum(
                SimpleNamespace(),  # type: ignore[arg-type]
                SimpleNamespace(id=77),  # type: ignore[arg-type]
                session,
            )

        resume.assert_awaited_once()

    async def test_discussion_channel_race_deletes_orphan_and_reports_stale_state(self) -> None:
        class FakeCategory:
            pass

        session = _discussion_session()
        category = FakeCategory()
        created = SimpleNamespace(id=501, delete=AsyncMock(), send=AsyncMock())

        async def create_channel(*args, **kwargs):
            session.stage = "paused"
            return created

        guild = SimpleNamespace(
            id=77,
            default_role=object(),
            me=None,
            get_channel=lambda channel_id: category,
            get_role=lambda role_id: None,
            create_text_channel=AsyncMock(side_effect=create_channel),
        )
        bot = SimpleNamespace(fetch_channel=AsyncMock())

        with patch("modules.tvrs_discussion.discord.CategoryChannel", FakeCategory):
            with self.assertRaisesRegex(ConsensusStateError, "лишний канал уже удалён"):
                await start_discussion_channel(
                    bot,  # type: ignore[arg-type]
                    guild,  # type: ignore[arg-type]
                    session,
                    "Правовая",
                    expected_bill_id=10,
                )

        created.delete.assert_awaited_once_with(
            reason="TVRS discussion state changed before activation"
        )
        created.send.assert_not_awaited()

    async def test_end_discussion_never_announces_before_durable_commit(self) -> None:
        session = _discussion_session()
        session.stage = "discussion"
        session.discussion_channel_id = 501
        channel = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(id=77, get_channel=lambda channel_id: channel)

        with patch(
            "modules.tvrs_discussion._consensus.end_discussion",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await end_discussion(
                    SimpleNamespace(),  # type: ignore[arg-type]
                    guild,  # type: ignore[arg-type]
                    session,
                    expected_stage="discussion",
                    expected_bill_id=10,
                )

        channel.send.assert_not_awaited()

    async def test_end_discussion_announces_after_durable_commit(self) -> None:
        session = _discussion_session()
        session.stage = "discussion"
        session.discussion_channel_id = 501
        order: list[str] = []

        async def announce(*args, **kwargs) -> None:
            order.append("announce")

        def commit(current, **kwargs) -> None:
            order.append("commit")
            current.stage = "voting"

        channel = SimpleNamespace(send=AsyncMock(side_effect=announce))
        guild = SimpleNamespace(id=77, get_channel=lambda channel_id: channel)
        with (
            patch(
                "modules.tvrs_discussion._consensus.end_discussion",
                side_effect=commit,
            ),
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()),
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()),
            patch("modules.tvrs_discussion.check_realtime_quorum", new=AsyncMock()),
            patch("modules.tvrs_discussion.wake_operations_worker"),
        ):
            await end_discussion(
                SimpleNamespace(),  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                session,
                expected_stage="discussion",
                expected_bill_id=10,
            )

        self.assertEqual(order, ["commit", "announce"])

    async def test_vote_refresh_only_enqueues_durable_projection(self) -> None:
        session = _discussion_session()
        guild = SimpleNamespace(
            fetch_member=AsyncMock(side_effect=AssertionError("direct Discord I/O is forbidden")),
        )

        with patch(
            "modules.tvrs_control.enqueue_current_control_projection",
            new=AsyncMock(),
            create=True,
        ) as enqueue:
            await update_all_vote_dms(guild, session, content="Состояние изменилось")  # type: ignore[arg-type]

        enqueue.assert_awaited_once_with(guild, session, content="Состояние изменилось")
        guild.fetch_member.assert_not_awaited()

    async def test_discussion_channel_503_keeps_state_retryable(self) -> None:
        class FakeCategory:
            pass

        session = _discussion_session()
        category = FakeCategory()
        error = discord.DiscordServerError(
            SimpleNamespace(status=503, reason="Service Unavailable"),
            {"message": "temporary outage", "code": 0},
        )
        guild = SimpleNamespace(
            id=77,
            default_role=object(),
            me=None,
            get_channel=lambda channel_id: category,
            get_role=lambda role_id: None,
            create_text_channel=AsyncMock(side_effect=error),
        )
        bot = SimpleNamespace(fetch_channel=AsyncMock())

        with (
            patch("modules.tvrs_discussion.discord.CategoryChannel", FakeCategory),
            patch("modules.tvrs_discussion._consensus.begin_discussion") as begin,
        ):
            with self.assertRaisesRegex(ConsensusStateError, "повторите попытку"):
                await start_discussion_channel(
                    bot,  # type: ignore[arg-type]
                    guild,  # type: ignore[arg-type]
                    session,
                    "Правовая",
                    expected_bill_id=10,
                )

        self.assertEqual(session.stage, "discussion_type")
        self.assertIsNone(session.discussion_channel_id)
        begin.assert_not_called()

    async def test_failed_durable_discussion_activation_removes_orphan_channel(self) -> None:
        class FakeCategory:
            pass

        session = _discussion_session()
        category = FakeCategory()
        created = SimpleNamespace(
            id=501,
            delete=AsyncMock(),
            send=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=77,
            default_role=object(),
            me=None,
            get_channel=lambda channel_id: category,
            get_role=lambda role_id: None,
            create_text_channel=AsyncMock(return_value=created),
        )
        bot = SimpleNamespace(fetch_channel=AsyncMock())

        with (
            patch("modules.tvrs_discussion.discord.CategoryChannel", FakeCategory),
            patch(
                "modules.tvrs_discussion._consensus.begin_discussion",
                side_effect=RuntimeError("database unavailable"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await start_discussion_channel(
                    bot,  # type: ignore[arg-type]
                    guild,  # type: ignore[arg-type]
                    session,
                    "Правовая",
                    expected_bill_id=10,
                )

        self.assertEqual(session.stage, "discussion_type")
        self.assertIsNone(session.discussion_channel_id)
        created.delete.assert_awaited_once_with(reason="TVRS discussion activation failed")

    async def test_discussion_invites_are_committed_as_durable_jobs(self) -> None:
        class FakeCategory:
            pass

        session = _discussion_session()
        category = FakeCategory()
        created = SimpleNamespace(
            id=501,
            delete=AsyncMock(),
            send=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=77,
            default_role=object(),
            me=None,
            get_channel=lambda channel_id: category,
            get_role=lambda role_id: None,
            fetch_member=AsyncMock(side_effect=AssertionError("no direct DM lookup")),
            create_text_channel=AsyncMock(return_value=created),
        )
        bot = SimpleNamespace(fetch_channel=AsyncMock())

        def activate(current, discussion_type, **kwargs) -> None:
            current.stage = "discussion"
            current.discussion_type = discussion_type
            current.discussion_channel_id = kwargs["channel_id"]
            current.discussion_allowed_user_ids = set(kwargs["allowed_user_ids"])

        with (
            patch("modules.tvrs_discussion.discord.CategoryChannel", FakeCategory),
            patch(
                "modules.tvrs_discussion._consensus.begin_discussion",
                side_effect=activate,
            ) as begin,
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()),
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()),
            patch("modules.tvrs_discussion.wake_operations_worker"),
        ):
            await start_discussion_channel(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                session,
                "Правовая",
                expected_bill_id=10,
            )

        self.assertEqual(session.stage, "discussion")
        deliveries = begin.call_args.kwargs["deliveries"]
        self.assertEqual(len(deliveries), 2)
        self.assertTrue(
            all(job["topic"] == "tvrs.consensus.discussion-invite.v1" for job in deliveries)
        )
        self.assertEqual(
            {job["payload"]["user_id"] for job in deliveries},
            {2, 3},
        )
        guild.fetch_member.assert_not_awaited()
        created.delete.assert_not_awaited()
        created.send.assert_awaited_once()

    async def test_leave_then_join_race_does_not_create_false_automatic_pause(self) -> None:
        session = _discussion_session()
        session.stage = "voting"
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace()
        voice = {"ready": False}

        def quorum_state(*args) -> tuple[bool, str]:
            return voice["ready"], "Кворум восстановлен" if voice["ready"] else "Кворум утрачен"

        with (
            patch(
                "modules.tvrs_discussion.session_voice_quorum_ready",
                side_effect=quorum_state,
            ) as quorum,
            patch("modules.tvrs_discussion._consensus.pause") as pause,
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()) as project,
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()) as host,
        ):
            lock = session_lock(77)
            await lock.acquire()
            try:
                check = asyncio.create_task(
                    check_realtime_quorum(
                        bot,  # type: ignore[arg-type]
                        guild,  # type: ignore[arg-type]
                        session,
                    )
                )
                while quorum.call_count < 1:
                    await asyncio.sleep(0)
                voice["ready"] = True
                lock.release()
                await check
            finally:
                if lock.locked():
                    lock.release()

        self.assertEqual(quorum.call_count, 2)
        self.assertEqual(session.stage, "voting")
        pause.assert_not_called()
        project.assert_not_awaited()
        host.assert_not_awaited()


class ConsensusVoteUiFaultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        consensus_registry.sessions.clear()
        consensus_registry._locks.clear()

    async def asyncTearDown(self) -> None:
        consensus_registry.sessions.clear()
        consensus_registry._locks.clear()

    @staticmethod
    def _interaction(user_id: int, guild: object) -> SimpleNamespace:
        client = SimpleNamespace(get_guild=lambda guild_id: guild)
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name=f"Участник {user_id}"),
            guild=guild,
            channel=SimpleNamespace(id=100),
            client=client,
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    async def test_unconfirmed_invitee_requires_explicit_current_roster_confirmation(self) -> None:
        session = _discussion_session()
        session.stage = "registration"
        session.current_bill = None
        session.participants[4] = LiveParticipant(
            user_id=4,
            display_name="Приглашённый 4",
            mention="<@4>",
            kind="chair",
            confirmed=False,
        )
        consensus_registry.add(session)
        guild = SimpleNamespace(id=77)
        interaction = self._interaction(1, guild)

        with (
            patch(
                "modules.tvrs_consensus_views.session_voice_quorum_ready",
                return_value=(True, ""),
            ),
            patch(
                "modules.tvrs_consensus_views.begin_next_bill_vote",
                new=AsyncMock(),
            ) as begin,
        ):
            view = TVRSRegistrationView(session.session_key)
            await view.start_vote.callback(interaction)

            begin.assert_not_awaited()
            warning = interaction.response.send_message.await_args
            self.assertTrue(warning.kwargs["ephemeral"])
            self.assertIn("Не все приглашённые", warning.args[0])
            confirm_view = warning.kwargs["view"]
            self.assertIsInstance(confirm_view, TVRSStartCurrentRosterConfirmView)

            confirm_interaction = self._interaction(1, guild)
            await confirm_view.children[0].callback(confirm_interaction)

        confirm_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        begin.assert_awaited_once_with(
            confirm_interaction.client,
            guild,
            session,
            confirm_interaction.channel,
            expected_stage="registration",
        )

    async def test_roster_confirmation_expires_when_a_participant_confirms(self) -> None:
        session = _discussion_session()
        session.stage = "registration"
        session.current_bill = None
        session.participants[4] = LiveParticipant(
            user_id=4,
            display_name="Приглашённый 4",
            mention="<@4>",
            kind="chair",
            confirmed=False,
        )
        consensus_registry.add(session)
        guild = SimpleNamespace(id=77)
        confirm_view = TVRSStartCurrentRosterConfirmView(
            session.session_key,
            session.leader_id,
            confirmed_user_ids={1, 2, 3},
        )
        session.participants[4].confirmed = True
        interaction = self._interaction(1, guild)

        with patch(
            "modules.tvrs_consensus_views.begin_next_bill_vote",
            new=AsyncMock(),
        ) as begin:
            await confirm_view.children[0].callback(interaction)

        begin.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        self.assertIn(
            "Состав успел измениться",
            interaction.response.send_message.await_args.args[0],
        )

    async def test_discussion_request_reports_safe_state_error_after_defer(self) -> None:
        session = _discussion_session()
        session.stage = "voting"
        consensus_registry.add(session)
        guild = SimpleNamespace(id=77)
        interaction = self._interaction(3, guild)

        with patch(
            "modules.tvrs_consensus_views.request_discussion",
            new=AsyncMock(
                side_effect=ConsensusStateError("Этап уже изменился.")
            ),
        ):
            await TVRSVoteView(
                session.session_key,
                3,
                bill_id=10,
            )._discussion_callback(interaction)

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            "Этап уже изменился.",
            ephemeral=True,
        )

    async def test_participant_discussion_type_reports_creation_error(self) -> None:
        session = _discussion_session()
        consensus_registry.add(session)
        guild = SimpleNamespace(id=77)
        interaction = self._interaction(3, guild)
        view = TVRSDiscussionTypeView(session.session_key, 3, bill_id=10)

        with patch(
            "modules.tvrs_consensus_views.start_discussion_channel",
            new=AsyncMock(
                side_effect=ConsensusStateError("Канал дискуссии недоступен.")
            ),
        ):
            await view.children[0].callback(interaction)

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            "Канал дискуссии недоступен.",
            ephemeral=True,
        )

    async def test_host_discussion_type_reports_creation_error(self) -> None:
        session = _discussion_session()
        consensus_registry.add(session)
        guild = SimpleNamespace(id=77)
        interaction = self._interaction(1, guild)
        view = TVRSHostVoteView(session.session_key)
        type_button = next(item for item in view.children if item.label == "Правовая")

        with patch(
            "modules.tvrs_consensus_views.start_discussion_channel",
            new=AsyncMock(
                side_effect=ConsensusStateError("Канал дискуссии недоступен.")
            ),
        ):
            await type_button.callback(interaction)

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            "Канал дискуссии недоступен.",
            ephemeral=True,
        )

    async def test_registration_cancel_closes_public_card(self) -> None:
        session = _discussion_session()
        session.stage = "registration"
        session.current_bill = None
        consensus_registry.add(session)
        guild = SimpleNamespace(id=77)
        interaction = self._interaction(1, guild)

        def cancel(current, **kwargs) -> None:
            current.stage = "cancelled"
            current.finished = True

        with (
            patch(
                "modules.tvrs_consensus_views._consensus.finish_atomically",
                side_effect=cancel,
            ),
            patch(
                "modules.tvrs_consensus_portal.ensure_public_consensus_card",
                new=AsyncMock(),
            ) as public_card,
            patch(
                "modules.tvrs_consensus_views.ensure_sticky_message",
                new=AsyncMock(),
            ),
            patch("modules.tvrs_consensus_views.wake_operations_worker"),
        ):
            await TVRSRegistrationView(session.session_key).cancel.callback(interaction)

        public_card.assert_awaited_once_with(
            interaction.client,
            guild,
            session,
            terminal=True,
        )
        self.assertNotIn(guild.id, consensus_registry.sessions)

    async def test_participant_last_vote_finalizes_before_dm_or_host_projection(self) -> None:
        session = _discussion_session()
        session.stage = "voting"
        session.current_bill = {"id": 10, "bill_number": 9, "title": "Проект"}
        consensus_registry.add(session)
        guild = SimpleNamespace(id=77)
        interaction = self._interaction(2, guild)
        interaction.message.edit = AsyncMock(
            side_effect=discord.DiscordException("DM projection failed")
        )

        with (
            patch(
                "modules.tvrs_consensus_views._consensus.cast_vote",
                return_value=True,
            ),
            patch(
                "modules.tvrs_consensus_views.update_host_vote_message",
                new=AsyncMock(side_effect=discord.DiscordException("host projection failed")),
            ) as host_projection,
            patch(
                "modules.tvrs_consensus_views.finalize_current_vote",
                new=AsyncMock(),
            ) as finalize,
        ):
            await TVRSVoteView(session.session_key, 2)._cast(interaction, "yes")

        finalize.assert_awaited_once_with(
            interaction.client,
            guild,
            session,
            forced=False,
            expected_bill_id=10,
        )
        interaction.message.edit.assert_not_awaited()
        host_projection.assert_not_awaited()

    async def test_host_last_vote_finalizes_before_host_panel_projection(self) -> None:
        session = _discussion_session()
        session.stage = "voting"
        session.current_bill = {"id": 10, "bill_number": 9, "title": "Проект"}
        consensus_registry.add(session)
        guild = SimpleNamespace(id=77)
        interaction = self._interaction(1, guild)
        interaction.edit_original_response = AsyncMock(
            side_effect=discord.DiscordException("host projection failed")
        )

        with (
            patch(
                "modules.tvrs_consensus_views._consensus.cast_vote",
                return_value=True,
            ),
            patch(
                "modules.tvrs_consensus_views.finalize_current_vote",
                new=AsyncMock(),
            ) as finalize,
        ):
            await TVRSHostVoteView(session.session_key).host_yes(interaction)

        finalize.assert_awaited_once_with(
            interaction.client,
            guild,
            session,
            forced=False,
            expected_bill_id=10,
        )
        interaction.edit_original_response.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
