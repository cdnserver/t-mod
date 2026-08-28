from __future__ import annotations

import unittest
from datetime import datetime, timezone

from modules.consensus_core import (
    ConsensusStateError,
    LiveConsensusSession,
    LiveParticipant,
    LiveResult,
    session_from_snapshot,
    session_to_snapshot,
)
from modules.consensus_health import assess_consensus_health
from modules.consensus_service import ConsensusActor, ConsensusCoordinator


class MemoryRepository:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.fail_save = False

    def save(self, session, event_type, *, actor=None, stage_from=None, details=None):
        if self.fail_save:
            raise OSError("database unavailable")
        session.revision += 1
        self.events.append(
            {
                "event_type": event_type,
                "actor": actor,
                "stage_from": stage_from,
                "details": details,
            }
        )
        return {"revision": session.revision}

    def commit_finish(
        self,
        session,
        *,
        expected_revision,
        current_bill_id,
        advance_plenary,
        event_type,
        actor,
        details,
        deliveries,
    ):
        session.revision = expected_revision + 1
        self.events.append(
            {
                "event_type": event_type,
                "actor": actor,
                "details": details,
                "current_bill_id": current_bill_id,
                "advance_plenary": advance_plenary,
            }
        )
        return {"session": {"revision": session.revision}}


def make_session(*, stage: str = "voting") -> LiveConsensusSession:
    participants = {
        1: LiveParticipant(1, "Первый", "<@1>", "chair", confirmed=True),
        2: LiveParticipant(2, "Второй", "<@2>", "chair", confirmed=True),
        3: LiveParticipant(3, "Сенатор", "<@3>", "senator", confirmed=True),
    }
    current_bill = None
    if stage in {"voting", "finalizing", "discussion_type", "discussion", "paused"}:
        current_bill = {"id": 10, "bill_number": 7, "title": "Проект"}
    return LiveConsensusSession(
        session_key="guild:v3:test",
        guild_id=99,
        channel_id=100,
        leader_id=1,
        leader_display="Первый",
        plenary_number=4,
        participants=participants,
        stage=stage,
        current_bill=current_bill,
        revision=3,
    )


class ConsensusLifecycleSafetyTests(unittest.TestCase):
    def test_health_distinguishes_valid_between_bill_pause_from_broken_vote_pause(self) -> None:
        between = make_session(stage="paused")
        between.current_bill = None
        between.previous_stage = "after_result"
        broken = make_session(stage="paused")
        broken.current_bill = None
        broken.previous_stage = "voting"

        between_codes = {item.code for item in assess_consensus_health(between).critical}
        broken_codes = {item.code for item in assess_consensus_health(broken).critical}

        self.assertNotIn("paused_bill_missing", between_codes)
        self.assertIn("paused_bill_missing", broken_codes)

    def test_health_reports_non_numeric_bill_identity_without_crashing(self) -> None:
        session = make_session(stage="voting")
        session.current_bill = {"id": "not-a-number", "bill_number": 7, "title": "Проект"}

        report = assess_consensus_health(session)

        self.assertIn("bill_identity_missing", {item.code for item in report.critical})

    def test_health_blocks_corrupt_discussion_recipients_and_result_status(self) -> None:
        discussion = make_session(stage="discussion")
        discussion.discussion_type = "Правовая"
        discussion.discussion_initiator_id = 3
        discussion.discussion_allowed_user_ids = {3, 999}
        result_session = make_session(stage="after_result")
        result_session.results.append(
            LiveResult(10, 7, "Проект", "unknown", 0, 0, False, {})
        )

        discussion_codes = {
            item.code for item in assess_consensus_health(discussion).critical
        }
        result_codes = {
            item.code for item in assess_consensus_health(result_session).critical
        }

        self.assertIn("discussion_users_outside_roster", discussion_codes)
        self.assertIn("result_status_invalid", result_codes)

    def test_snapshot_rejects_duplicate_participant_and_invalid_bill_identity(self) -> None:
        session = make_session(stage="voting")
        snapshot = session_to_snapshot(session)
        snapshot["participants"].append(dict(snapshot["participants"][0]))
        with self.assertRaisesRegex(ConsensusStateError, "повторяется"):
            session_from_snapshot(snapshot)

        snapshot = session_to_snapshot(session)
        snapshot["current_bill"]["id"] = "broken"
        with self.assertRaisesRegex(ConsensusStateError, "идентификатор"):
            session_from_snapshot(snapshot)

    def test_snapshot_rejects_duplicate_voting_block(self) -> None:
        snapshot = session_to_snapshot(make_session(stage="voting"))
        snapshot["participants"][0]["voting_block"] = "first"
        snapshot["participants"][1]["voting_block"] = "first"

        with self.assertRaisesRegex(ConsensusStateError, "повторяется"):
            session_from_snapshot(snapshot)

    def test_health_locks_ambiguous_roster_and_repeated_current_bill(self) -> None:
        session = make_session(stage="voting")
        session.participants[1].voting_block = "first"
        session.participants[2].voting_block = "first"
        session.results.append(
            LiveResult(10, 7, "Проект", "accepted", 75, 75, True, {})
        )

        codes = {item.code for item in assess_consensus_health(session).critical}

        self.assertIn("voting_blocks_invalid", codes)
        self.assertIn("current_bill_already_resolved", codes)

    def test_domain_rejects_invalid_bill_and_external_discussion_recipient(self) -> None:
        coordinator = ConsensusCoordinator(MemoryRepository())
        session = make_session(stage="after_result")
        session.current_bill = None
        with self.assertRaisesRegex(ConsensusStateError, "номер"):
            coordinator.begin_bill(
                session,
                {"id": 0, "bill_number": 7, "title": "Повреждённый"},
                actor=ConsensusActor(1, "Первый"),
            )

        session = make_session(stage="discussion_type")
        with self.assertRaisesRegex(ConsensusStateError, "подтверждённого состава"):
            coordinator.begin_discussion(
                session,
                "Правовая",
                channel_id=500,
                allowed_user_ids={3, 999},
            )

    def test_resume_refuses_to_guess_missing_previous_stage(self) -> None:
        coordinator = ConsensusCoordinator(MemoryRepository())
        session = make_session(stage="paused")
        session.previous_stage = None

        with self.assertRaisesRegex(ConsensusStateError, "аварийное восстановление"):
            coordinator.resume(session, actor=ConsensusActor(1, "Первый"))

        self.assertEqual(session.stage, "paused")

    def test_safe_repair_clears_stale_pause_flags_outside_pause(self) -> None:
        repository = MemoryRepository()
        coordinator = ConsensusCoordinator(repository)
        session = make_session(stage="voting")
        session.paused_reason = "Старая причина"
        session.pause_is_automatic = True

        repaired = coordinator.repair_safe_invariants(session)

        self.assertIn("stale_pause_state", repaired)
        self.assertIsNone(session.paused_reason)
        self.assertFalse(session.pause_is_automatic)

    def test_zero_second_timer_survives_restart_snapshot(self) -> None:
        session = make_session(stage="voting")
        session.timer_seconds = 0
        session.timer_deadline = datetime.now(timezone.utc)

        restored = session_from_snapshot(session_to_snapshot(session))

        self.assertEqual(restored.timer_seconds, 0)
        self.assertIsNotNone(restored.timer_deadline)

    def test_safe_invariant_repair_is_atomic_and_audited(self) -> None:
        repository = MemoryRepository()
        coordinator = ConsensusCoordinator(repository)
        session = make_session(stage="after_result")
        session.votes = {1: "yes"}
        session.pending_action = {"kind": "vote", "bill_id": 10}
        session.timer_deadline = datetime.now(timezone.utc)
        session.timer_seconds = 30
        session.discussion_channel_id = 555
        session.discussion_note_message_id = 777
        session.participants[2].discussion_message_id = 888

        repaired = coordinator.repair_safe_invariants(session)

        self.assertEqual(
            set(repaired),
            {
                "timer_outside_voting",
                "stale_votes",
                "stale_discussion_state",
                "orphan_pending_action",
            },
        )
        self.assertIsNone(session.timer_deadline)
        self.assertEqual(session.votes, {})
        self.assertIsNone(session.pending_action)
        self.assertIsNone(session.discussion_channel_id)
        self.assertIsNone(session.participants[2].discussion_message_id)
        self.assertEqual(repository.events[-1]["event_type"], "session_invariants_repaired")

    def test_safe_invariant_repair_does_not_guess_critical_business_state(self) -> None:
        repository = MemoryRepository()
        coordinator = ConsensusCoordinator(repository)
        session = make_session(stage="voting")
        session.votes = {999: "maybe"}
        session.pending_action = {"kind": "vote", "bill_id": 10}
        original_bill = dict(session.current_bill or {})

        repaired = coordinator.repair_safe_invariants(session)

        self.assertEqual(repaired, ())
        self.assertEqual(session.current_bill, original_bill)
        self.assertEqual(session.votes, {999: "maybe"})
        self.assertEqual(session.pending_action, {"kind": "vote", "bill_id": 10})
        self.assertEqual(repository.events, [])

    def test_safe_invariant_repair_rolls_back_on_database_failure(self) -> None:
        repository = MemoryRepository()
        repository.fail_save = True
        coordinator = ConsensusCoordinator(repository)
        session = make_session(stage="after_result")
        session.votes = {1: "yes"}

        with self.assertRaises(OSError):
            coordinator.repair_safe_invariants(session)

        self.assertEqual(session.votes, {1: "yes"})

    def test_oral_result_metadata_survives_snapshot_roundtrip(self) -> None:
        session = make_session(stage="after_result")
        session.results.append(
            LiveResult(
                bill_id=10,
                bill_number=7,
                title="Проект",
                status="accepted",
                internal_percent=0,
                overall_percent=0,
                internal_active=False,
                votes={1: "yes"},
                resolution_method="oral",
                resolution_note="Решение принято на очном заседании",
                resolved_by_id=2,
                resolved_by_display="Второй",
            )
        )

        restored = session_from_snapshot(session_to_snapshot(session))

        self.assertEqual(restored.results[0].resolution_method, "oral")
        self.assertEqual(restored.results[0].resolution_note, "Решение принято на очном заседании")
        self.assertEqual(restored.results[0].resolved_by_id, 2)

    def test_legacy_result_defaults_to_vote_and_veto_is_inferred(self) -> None:
        session = make_session(stage="after_result")
        session.results.append(
            LiveResult(10, 7, "Проект", "vetoed", 0, 0, False, {}, veto_by_id=1)
        )
        snapshot = session_to_snapshot(session)
        raw = snapshot["results"][0]
        raw.pop("resolution_method")
        raw.pop("resolution_note")
        raw.pop("resolved_by_id")
        raw.pop("resolved_by_display")

        restored = session_from_snapshot(snapshot)

        self.assertEqual(restored.results[0].resolution_method, "veto")

    def test_takeover_requires_confirmed_chair_from_original_roster(self) -> None:
        repository = MemoryRepository()
        coordinator = ConsensusCoordinator(repository)
        session = make_session()

        changed = coordinator.transfer_leadership(
            session,
            new_leader_id=2,
            new_leader_display="Второй",
            actor=ConsensusActor(2, "Второй"),
        )

        self.assertTrue(changed)
        self.assertEqual(session.leader_id, 2)
        self.assertIsNone(session.host_message_id)
        self.assertEqual(repository.events[-1]["event_type"], "leadership_transferred")
        with self.assertRaises(ConsensusStateError):
            coordinator.transfer_leadership(
                session,
                new_leader_id=3,
                new_leader_display="Сенатор",
                actor=ConsensusActor(3, "Сенатор"),
            )

    def test_takeover_rolls_back_when_persistence_fails(self) -> None:
        repository = MemoryRepository()
        repository.fail_save = True
        coordinator = ConsensusCoordinator(repository)
        session = make_session()
        session.host_message_id = 555

        with self.assertRaises(OSError):
            coordinator.transfer_leadership(
                session,
                new_leader_id=2,
                new_leader_display="Второй",
                actor=ConsensusActor(2, "Второй"),
            )

        self.assertEqual(session.leader_id, 1)
        self.assertEqual(session.host_message_id, 555)

    def test_administrative_abort_is_audited_and_requeues_current_bill(self) -> None:
        repository = MemoryRepository()
        coordinator = ConsensusCoordinator(repository)
        session = make_session()

        coordinator.finish_atomically(
            session,
            actor=ConsensusActor(2, "Второй"),
            cancelled=True,
            reason="Проведено устно",
        )

        self.assertTrue(session.finished)
        self.assertEqual(session.stage, "cancelled")
        event = repository.events[-1]
        self.assertEqual(event["current_bill_id"], 10)
        self.assertFalse(event["advance_plenary"])
        self.assertTrue(event["details"]["administrative"])
        self.assertEqual(event["details"]["reason"], "Проведено устно")

    def test_health_report_detects_cross_stage_corruption(self) -> None:
        session = make_session(stage="finalizing")
        session.pending_action = {
            "kind": "oral",
            "bill_id": 999,
            "oral_status": "",
            "oral_note": "",
        }
        session.votes[777] = "maybe"
        session.timer_seconds = 60

        report = assess_consensus_health(session)
        codes = {item.code for item in report.issues}

        self.assertFalse(report.healthy)
        self.assertIn("votes_outside_roster", codes)
        self.assertIn("invalid_vote_value", codes)
        self.assertIn("finalization_bill_mismatch", codes)
        self.assertIn("oral_resolution_incomplete", codes)
        self.assertIn("partial_timer_state", codes)

    def test_health_report_accepts_normal_voting_state(self) -> None:
        session = make_session(stage="voting")
        session.votes = {1: "yes", 2: "no"}

        report = assess_consensus_health(session)

        self.assertTrue(report.healthy)
        self.assertFalse(report.critical)

    def test_oral_claim_can_close_pause_or_discussion_but_requires_complete_protocol(self) -> None:
        repository = MemoryRepository()
        coordinator = ConsensusCoordinator(repository)
        session = make_session(stage="paused")
        session.previous_stage = "discussion"
        session.discussion_type = "Правовая"
        session.discussion_channel_id = 500
        session.discussion_note_message_id = 600
        session.participants[2].discussion_message_id = 700

        pending = coordinator.claim_finalization(
            session,
            kind="oral",
            actor=ConsensusActor(2, "Второй"),
            oral_authorized=True,
            action_details={
                "oral_status": "accepted",
                "oral_note": "Решение принято участниками очно",
            },
        )

        self.assertEqual(session.stage, "finalizing")
        self.assertEqual(pending["kind"], "oral")
        self.assertEqual(pending["oral_status"], "accepted")
        self.assertIsNone(session.discussion_channel_id)
        self.assertIsNone(session.discussion_type)
        self.assertIsNone(session.discussion_note_message_id)
        self.assertIsNone(session.participants[2].discussion_message_id)

        another = make_session(stage="voting")
        with self.assertRaises(ConsensusStateError):
            coordinator.claim_finalization(
                another,
                kind="oral",
                actor=ConsensusActor(2, "Второй"),
                oral_authorized=True,
                action_details={"oral_status": "accepted", "oral_note": ""},
            )
        with self.assertRaises(ConsensusStateError):
            coordinator.claim_finalization(
                another,
                kind="oral",
                actor=ConsensusActor(2, "Второй"),
                oral_authorized=True,
                action_details={
                    "oral_status": "accepted",
                    "oral_note": "Очно",
                    "bill_id": 999,
                },
            )


if __name__ == "__main__":
    unittest.main()
