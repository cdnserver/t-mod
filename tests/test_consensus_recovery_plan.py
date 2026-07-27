from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules.consensus_core import LiveConsensusSession, LiveParticipant
from modules.consensus_health import assess_consensus_health
from modules.consensus_operational import collect_consensus_operational_state
from modules.consensus_recovery_plan import (
    ConsensusOperationalState,
    build_consensus_recovery_plan,
)
from modules.tvrs_discussion import session_voice_quorum_ready


def session(*, stage: str = "voting") -> LiveConsensusSession:
    participants = {
        1: LiveParticipant(1, "Ведущий", "<@1>", "chair", confirmed=True),
        2: LiveParticipant(2, "Председатель", "<@2>", "chair", confirmed=True),
        3: LiveParticipant(3, "Сенатор", "<@3>", "senator", confirmed=True),
    }
    current_bill = (
        {"id": 10, "bill_number": 8, "title": "Проект"}
        if stage in {"voting", "finalizing", "discussion_type", "discussion", "paused"}
        else None
    )
    return LiveConsensusSession(
        session_key="77:recovery-plan",
        guild_id=77,
        channel_id=100,
        leader_id=1,
        leader_display="Ведущий",
        plenary_number=5,
        participants=participants,
        stage=stage,
        current_bill=current_bill,
        revision=4,
    )


class ConsensusRecoveryPlanTests(unittest.TestCase):
    def test_matrix_maps_live_failures_to_safe_automatic_and_manual_actions(self) -> None:
        current = session(stage="discussion")
        current.discussion_type = "Открытая"
        operational = ConsensusOperationalState(
            leader_present=False,
            leader_in_voice=False,
            voice_channel_available=True,
            quorum_ready=False,
            discussion_channel_available=False,
            public_channel_available=True,
            missing_member_ids=(3,),
            delivery_counts=(("dead", 2),),
        )

        plan = build_consensus_recovery_plan(
            current,
            assess_consensus_health(current),
            operational,
        )
        codes = {item.code for item in plan.actions}

        self.assertTrue(
            {
                "pause_for_quorum",
                "replace_missing_leader",
                "end_missing_discussion",
                "missing_roster_members",
                "requeue_dead_deliveries",
            }.issubset(codes)
        )
        self.assertIn("pause_for_quorum", {item.code for item in plan.automatic})
        self.assertIn("replace_missing_leader", {item.code for item in plan.manual})

    def test_critical_finalization_is_never_retried_by_guessing(self) -> None:
        current = session(stage="finalizing")
        current.pending_action = {"kind": "unknown", "bill_id": 10}

        plan = build_consensus_recovery_plan(current, assess_consensus_health(current))
        codes = {item.code for item in plan.actions}

        self.assertIn("manual_integrity_review", codes)
        self.assertNotIn("retry_finalization", codes)

    def test_healthy_session_has_monitor_action_only(self) -> None:
        current = session()
        operational = ConsensusOperationalState(
            leader_present=True,
            leader_in_voice=True,
            voice_channel_available=True,
            quorum_ready=True,
            public_channel_available=True,
        )

        plan = build_consensus_recovery_plan(
            current,
            assess_consensus_health(current),
            operational,
        )

        self.assertEqual([item.code for item in plan.actions], ["monitor"])

    def test_non_quorum_stages_are_not_paused_by_missing_voice_channel(self) -> None:
        for stage in ("registration", "after_result", "finalizing"):
            with self.subTest(stage=stage):
                current = session(stage=stage)
                if stage == "finalizing":
                    current.pending_action = {"kind": "vote", "bill_id": 10}
                operational = ConsensusOperationalState(
                    leader_present=True,
                    leader_in_voice=False,
                    voice_channel_available=False,
                    quorum_ready=False,
                    public_channel_available=True,
                    host_control_bound=True,
                )

                plan = build_consensus_recovery_plan(
                    current,
                    assess_consensus_health(current),
                    operational,
                )

                self.assertNotIn("pause_for_quorum", {item.code for item in plan.actions})

    def test_manual_pause_is_never_scheduled_for_automatic_resume(self) -> None:
        current = session(stage="paused")
        current.pause_is_automatic = False
        operational = ConsensusOperationalState(
            leader_present=True,
            leader_in_voice=True,
            voice_channel_available=True,
            quorum_ready=True,
            public_channel_available=True,
            host_control_bound=True,
        )

        plan = build_consensus_recovery_plan(
            current,
            assess_consensus_health(current),
            operational,
        )

        self.assertNotIn("resume_after_quorum", {item.code for item in plan.actions})

    def test_voice_quorum_requires_the_current_leader(self) -> None:
        current = session()

        class FakeVoiceChannel:
            members = [
                SimpleNamespace(id=2, bot=False),
                SimpleNamespace(id=3, bot=False),
            ]

        guild = SimpleNamespace(get_channel=lambda _channel_id: FakeVoiceChannel())
        with patch("modules.tvrs_discussion.discord.VoiceChannel", FakeVoiceChannel):
            ready, reason = session_voice_quorum_ready(guild, current)  # type: ignore[arg-type]

        self.assertFalse(ready)
        self.assertIn("Ведущий отсутствует", reason)


class ConsensusOperationalTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_between_bills_has_no_missing_vote_controls(self) -> None:
        current = session(stage="paused")
        current.current_bill = None
        current.previous_stage = "after_result"

        class FakeVoiceChannel:
            members = [
                SimpleNamespace(id=1, bot=False),
                SimpleNamespace(id=2, bot=False),
                SimpleNamespace(id=3, bot=False),
            ]

        guild = SimpleNamespace(
            get_channel=lambda channel_id: (
                SimpleNamespace(id=100) if channel_id == 100 else FakeVoiceChannel()
            ),
            get_member=lambda user_id: SimpleNamespace(id=user_id),
        )
        with (
            patch("modules.consensus_operational.discord.VoiceChannel", FakeVoiceChannel),
            patch("modules.tvrs_discussion.discord.VoiceChannel", FakeVoiceChannel),
            patch(
                "modules.consensus_operational._outbox_storage.delivery_outbox_consensus_status",
                return_value={"counts": {}},
            ),
        ):
            operational = await collect_consensus_operational_state(guild, current)  # type: ignore[arg-type]

        self.assertEqual(operational.missing_control_user_ids, ())

    async def test_live_diagnostics_report_quorum_controls_members_and_deliveries(self) -> None:
        current = session()

        class FakeVoiceChannel:
            members = [
                SimpleNamespace(id=1, bot=False),
                SimpleNamespace(id=2, bot=False),
            ]

        def get_channel(channel_id: int):
            if channel_id == 100:
                return SimpleNamespace(id=100)
            return FakeVoiceChannel()

        guild = SimpleNamespace(
            get_channel=get_channel,
            get_member=lambda user_id: SimpleNamespace(id=user_id) if user_id in {1, 2} else None,
        )
        with (
            patch("modules.consensus_operational.discord.VoiceChannel", FakeVoiceChannel),
            patch("modules.tvrs_discussion.discord.VoiceChannel", FakeVoiceChannel),
            patch(
                "modules.consensus_operational._outbox_storage.delivery_outbox_consensus_status",
                return_value={
                    "counts": {"pending": 2, "dead": 1},
                    "dead_errors": ["Discord Forbidden"],
                },
            ),
        ):
            operational = await collect_consensus_operational_state(guild, current)  # type: ignore[arg-type]

        self.assertTrue(operational.leader_present)
        self.assertTrue(operational.leader_in_voice)
        self.assertFalse(operational.quorum_ready)
        self.assertEqual(operational.missing_member_ids, (3,))
        self.assertEqual(operational.missing_control_user_ids, (2, 3))
        self.assertEqual(dict(operational.delivery_counts), {"dead": 1, "pending": 2})
        self.assertEqual(operational.dead_delivery_errors, ("Discord Forbidden",))


if __name__ == "__main__":
    unittest.main()
