import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import storage
from discord.ui.view import ViewStore
from modules import tvrs
from modules.consensus_core import (
    ConsensusRules,
    ConsensusStateError,
    LiveConsensusSession,
    LiveParticipant,
    LiveResult,
    session_from_snapshot,
    session_to_snapshot,
)
from modules.consensus_repository import StorageConsensusRepository
from modules.consensus_service import (
    ConsensusActor,
    ConsensusCoordinator,
    ConsensusSessionRegistry,
)
from modules.tvrs import (
    TVRSConfirmView,
    TVRSHostVoteView,
    TVRSVoteView,
    _consensus,
    _consensus_recovery_tasks,
    _consensus_registry,
    _finalization_retry_tasks,
    _restored_consensus_guilds,
    finalize_current_vote,
    pause_session,
    restore_tvrs_consensus_sessions,
)
from modules.tvrs_delivery import TVRS_BILL_PUBLICATION_TOPIC


class MemoryRepository:
    def __init__(self) -> None:
        self.revision = 0
        self.events: list[dict[str, Any]] = []
        self.snapshots: dict[int, dict[str, Any]] = {}

    def save(
        self,
        session: LiveConsensusSession,
        event_type: str,
        *,
        actor: ConsensusActor | None = None,
        stage_from: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.revision += 1
        snapshot = session_to_snapshot(session)
        snapshot["revision"] = self.revision
        self.snapshots[session.guild_id] = snapshot
        self.events.append(
            {
                "event_type": event_type,
                "stage_from": stage_from,
                "stage_to": session.stage,
                "actor_id": actor.user_id if actor else None,
                "details": details or {},
            }
        )
        return {"revision": self.revision}

    def active_snapshots(self, guild_id: int | None = None) -> list[dict[str, Any]]:
        values = list(self.snapshots.values())
        return values if guild_id is None else [item for item in values if item["guild_id"] == guild_id]


def participant(user_id: int, kind: str, *, confirmed: bool = True) -> LiveParticipant:
    return LiveParticipant(
        user_id=user_id,
        display_name=f"Участник {user_id}",
        mention=f"<@{user_id}>",
        kind=kind,  # type: ignore[arg-type]
        confirmed=confirmed,
        permanent=user_id == 1,
    )


def session(*, guild_id: int = 77) -> LiveConsensusSession:
    people = {
        1: participant(1, "chair"),
        2: participant(2, "chair"),
        3: participant(3, "senator"),
    }
    return LiveConsensusSession(
        session_key=f"{guild_id}:test",
        guild_id=guild_id,
        channel_id=100,
        leader_id=1,
        leader_display="Ведущий",
        plenary_number=4,
        participants=people,
    )


class ConsensusCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryRepository()
        self.coordinator = ConsensusCoordinator(self.repository)
        self.session = session()
        self.actor = ConsensusActor(1, "Ведущий")

    def test_vote_is_claimed_once_and_survives_snapshot_restore(self) -> None:
        bill = {"id": 10, "bill_number": 9, "title": "Новый порядок", "summary": "Описание"}
        self.coordinator.begin_bill(self.session, bill, actor=self.actor)
        self.session.participants[2].vote_message_id = 7002
        self.session.participants[2].vote_bill_id = 10
        self.assertFalse(self.coordinator.cast_vote(self.session, 1, "yes", actor=self.actor))
        self.assertFalse(self.coordinator.cast_vote(self.session, 2, "no", actor=ConsensusActor(2, "Председатель")))
        self.assertTrue(self.coordinator.cast_vote(self.session, 3, "yes", actor=ConsensusActor(3, "Сенатор")))

        pending = self.coordinator.claim_finalization(
            self.session,
            kind="vote",
            actor=self.actor,
            forced=False,
        )
        same_pending = self.coordinator.claim_finalization(
            self.session,
            kind="vote",
            actor=self.actor,
            forced=False,
        )
        self.assertEqual(pending, same_pending)
        self.assertEqual(self.session.stage, "finalizing")

        restored = session_from_snapshot(session_to_snapshot(self.session))
        self.assertEqual(restored.pending_action["bill_id"], 10)  # type: ignore[index]
        self.assertEqual(restored.participants[2].vote_message_id, 7002)
        self.assertEqual(restored.participants[2].vote_bill_id, 10)
        result = LiveResult(
            bill_id=10,
            bill_number=9,
            title="Новый порядок",
            status="accepted",
            internal_percent=100.0,
            overall_percent=51.0,
            internal_active=True,
            votes=dict(restored.votes),
        )
        self.coordinator.complete_result(restored, result, event_type="vote_finalized", actor=self.actor)
        self.assertEqual(restored.stage, "after_result")
        self.assertIsNone(restored.current_bill)
        self.assertIsNone(restored.pending_action)
        self.assertEqual([item.bill_id for item in restored.results], [10])

    def test_discussion_and_pause_resume_follow_explicit_transitions(self) -> None:
        bill = {"id": 10, "bill_number": 9, "title": "Новый порядок"}
        self.coordinator.begin_bill(self.session, bill, actor=self.actor)
        senator = self.session.participants[3]
        self.coordinator.request_discussion(self.session, senator)
        self.assertEqual(self.session.stage, "discussion_type")
        self.coordinator.begin_discussion(
            self.session,
            "Правовая",
            channel_id=500,
            allowed_user_ids={2, 3},
        )
        self.assertEqual(self.session.stage, "discussion")
        self.coordinator.pause(
            self.session,
            "Кворум утрачен",
            automatic=True,
            actor=None,
        )
        self.assertEqual(self.session.stage, "paused")
        self.assertEqual(self.coordinator.resume(self.session, actor=self.actor), "discussion")
        self.coordinator.end_discussion(self.session, actor=self.actor)
        self.assertEqual(self.session.stage, "voting")

    def test_invalid_vote_and_transition_are_rejected(self) -> None:
        with self.assertRaises(ConsensusStateError):
            self.coordinator.cast_vote(self.session, 1, "abstain", actor=self.actor)
        with self.assertRaises(ConsensusStateError):
            self.coordinator.claim_finalization(self.session, kind="vote", actor=self.actor)

    def test_veto_requires_explicit_application_authorization(self) -> None:
        bill = {"id": 10, "bill_number": 9, "title": "Защищённое решение"}
        self.coordinator.begin_bill(self.session, bill, actor=self.actor)
        with self.assertRaises(ConsensusStateError):
            self.coordinator.claim_finalization(
                self.session,
                kind="veto",
                actor=self.actor,
            )
        self.assertEqual(self.session.stage, "voting")
        self.coordinator.claim_finalization(
            self.session,
            kind="veto",
            actor=self.actor,
            veto_authorized=True,
        )
        self.assertEqual(self.session.stage, "finalizing")

    def test_rules_are_frozen_inside_session_snapshot(self) -> None:
        self.session.rules = ConsensusRules(
            version=2,
            minimum_chairs=1,
            minimum_senators=0,
            senators_must_be_odd=False,
            internal_activation_strictly_above=60.0,
            chair_yes_weight=60.0,
            internal_consensus_weight=0.0,
            acceptance_percent=60.0,
        )
        restored = session_from_snapshot(session_to_snapshot(self.session))
        self.assertEqual(restored.rules.version, 2)
        self.assertEqual(restored.rules.chair_yes_weight, 60.0)
        self.assertEqual(restored.rules.internal_consensus_weight, 0.0)

    def test_registry_allows_only_one_active_session_per_guild(self) -> None:
        registry = ConsensusSessionRegistry()
        registry.add(self.session)
        other = session(guild_id=77)
        other.session_key = "77:other"
        with self.assertRaises(ConsensusStateError):
            registry.add(other)

    def test_failed_persistence_rolls_back_bill_and_vote_mutations(self) -> None:
        bill = {"id": 10, "bill_number": 9, "title": "Откат состояния"}
        initial = session_to_snapshot(self.session)
        with patch.object(self.repository, "save", side_effect=RuntimeError("database offline")):
            with self.assertRaises(RuntimeError):
                self.coordinator.begin_bill(self.session, bill, actor=self.actor)
        self.assertEqual(session_to_snapshot(self.session), initial)

        self.coordinator.begin_bill(self.session, bill, actor=self.actor)
        before_vote = session_to_snapshot(self.session)
        with patch.object(self.repository, "save", side_effect=RuntimeError("database offline")):
            with self.assertRaises(RuntimeError):
                self.coordinator.cast_vote(self.session, 2, "yes", actor=ConsensusActor(2, "Участник"))
        self.assertEqual(session_to_snapshot(self.session), before_vote)
        self.coordinator.cast_vote(self.session, 2, "yes", actor=ConsensusActor(2, "Участник"))
        self.assertEqual(self.session.votes[2], "yes")


class ConsensusStorageIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.old_activity_file = storage.LEGACY_ACTIVITY_FILE
        self.old_reset_id = storage.CONSENSUS_V2_RESET_ID
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "consensus-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        storage.LEGACY_ACTIVITY_FILE = self.old_activity_file
        storage.CONSENSUS_V2_RESET_ID = self.old_reset_id
        self.temp_dir.cleanup()

    def test_result_upsert_creates_one_row(self) -> None:
        values = {
            "guild_id": 77,
            "session_key": "77:test",
            "plenary_number": 4,
            "bill_id": 10,
            "bill_number": 9,
            "bill_title": "Новый порядок",
            "status": "accepted",
            "internal_percent": 100.0,
            "overall_percent": 51.0,
            "internal_active": True,
            "votes_json": '{"1":"yes"}',
        }
        first = storage.tvrs_save_live_result(**values)
        second = storage.tvrs_save_live_result(**{**values, "overall_percent": 98.0})
        self.assertEqual(first, second)
        row = storage.tvrs_live_result_for_bill("77:test", 10)
        self.assertIsNotNone(row)
        self.assertEqual(row["overall_percent"], 98.0)  # type: ignore[index]

    def test_bill_creation_and_publication_intent_commit_atomically_and_dedupe_retry(self) -> None:
        values = {
            "guild_id": 77,
            "channel_id": 100,
            "author_id": 44,
            "author_display": "Автор",
            "title": "Надёжная подача проекта",
            "summary": "Карточка публикуется через долговечную очередь",
            "materials": None,
            "delivery_topic": TVRS_BILL_PUBLICATION_TOPIC,
        }
        first, first_delivery, first_created = storage.tvrs_create_bill_with_publication(**values)
        second, second_delivery, second_created = storage.tvrs_create_bill_with_publication(**values)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first_delivery["id"], second_delivery["id"])
        self.assertEqual(storage.delivery_outbox_counts(), {"pending": 1})
        pending_bill = storage.tvrs_get_bill_dict_by_id(first.id)
        self.assertIsNone(pending_bill["message_id"])  # type: ignore[index]
        self.assertEqual(pending_bill["status"], "publishing")  # type: ignore[index]
        self.assertEqual(storage.tvrs_queue_bills(77), [])

        storage.tvrs_complete_bill_publication(first.id, 7001, 100)

        published_bill = storage.tvrs_get_bill_dict_by_id(first.id)
        self.assertEqual(published_bill["status"], "draft")  # type: ignore[index]
        self.assertEqual(published_bill["message_id"], 7001)  # type: ignore[index]
        self.assertEqual([item["id"] for item in storage.tvrs_queue_bills(77)], [first.id])

    def test_bill_creation_rolls_back_if_publication_intent_cannot_be_saved(self) -> None:
        with patch(
            "persistence.tvrs_repository.delivery_outbox_enqueue_in_connection",
            side_effect=RuntimeError("outbox unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                storage.tvrs_create_bill_with_publication(
                    guild_id=77,
                    channel_id=100,
                    author_id=44,
                    author_display="Автор",
                    title="Не должен сохраниться",
                    summary="Транзакция должна откатиться полностью",
                    materials=None,
                    delivery_topic=TVRS_BILL_PUBLICATION_TOPIC,
                )

        self.assertEqual(storage.tvrs_recent_bills(77), [])
        self.assertEqual(storage.delivery_outbox_counts(), {})

    def test_repeated_identical_submission_requeues_dead_publication(self) -> None:
        values = {
            "guild_id": 77,
            "channel_id": 100,
            "author_id": 44,
            "author_display": "Автор",
            "title": "Повтор публикации",
            "summary": "Тот же проект продолжает прежнее задание",
            "materials": None,
            "delivery_topic": TVRS_BILL_PUBLICATION_TOPIC,
            "max_attempts": 1,
        }
        bill, delivery, _ = storage.tvrs_create_bill_with_publication(**values)
        claimed = storage.delivery_outbox_claim(worker_id="test", limit=1, lease_seconds=30)[0]
        storage.delivery_outbox_mark_failed(
            int(delivery["id"]),
            lease_token=str(claimed["lease_token"]),
            error="temporary terminal state",
            retry_at=storage.utc_now_iso(),
            permanent=True,
        )

        repeated, refreshed, created = storage.tvrs_create_bill_with_publication(**values)

        self.assertEqual(repeated.id, bill.id)
        self.assertFalse(created)
        self.assertEqual(refreshed["status"], "retry")
        self.assertEqual(refreshed["attempts"], 0)

    def test_bill_submission_is_blocked_by_durable_active_session(self) -> None:
        coordinator = ConsensusCoordinator(StorageConsensusRepository())
        current = session()
        coordinator.save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))

        with self.assertRaisesRegex(ValueError, "bill_submission_locked_by_active_consensus"):
            storage.tvrs_create_bill_with_publication(
                guild_id=77,
                channel_id=100,
                author_id=44,
                author_display="Автор",
                title="Поздняя подача",
                summary="Не должна попасть внутрь уже начатого пленума",
                materials=None,
                delivery_topic=TVRS_BILL_PUBLICATION_TOPIC,
            )

        self.assertEqual(storage.tvrs_recent_bills(77), [])

    def test_admin_delete_waits_for_open_bill_delivery(self) -> None:
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=44,
            author_display="Автор",
            title="Проект с ожидающей публикацией",
            summary="Админ не должен создать призрачную карточку",
            materials=None,
        )
        row = storage.delivery_outbox_enqueue(
            topic=TVRS_BILL_PUBLICATION_TOPIC,
            dedupe_key=f"test:bill:{bill.id}:publication",
            payload={
                "payload_version": 1,
                "guild_id": 77,
                "channel_id": 100,
                "bill": {"id": bill.id},
            },
            max_attempts=1,
        )

        with self.assertRaisesRegex(ValueError, "tvrs_delivery_pending"):
            storage.tvrs_delete_bill_by_number(77, bill.bill_number)

        claimed = storage.delivery_outbox_claim(worker_id="test", limit=1, lease_seconds=30)[0]
        storage.delivery_outbox_mark_failed(
            int(row["id"]),
            lease_token=str(claimed["lease_token"]),
            error="terminal",
            retry_at=storage.utc_now_iso(),
            permanent=True,
        )
        deleted = storage.tvrs_delete_bill_by_number(77, bill.bill_number)
        self.assertEqual(deleted["id"], bill.id)  # type: ignore[index]

    def test_repository_round_trip_preserves_revision(self) -> None:
        repository = StorageConsensusRepository()
        coordinator = ConsensusCoordinator(repository)
        current = session()
        row = coordinator.save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        self.assertEqual(row["revision"], 1)
        restored = session_from_snapshot(repository.active_snapshots(77)[0])
        self.assertEqual(restored.revision, 1)

    def _prepared_finalization(
        self,
        *,
        kind: str = "vote",
    ) -> tuple[ConsensusCoordinator, LiveConsensusSession, storage.TVRSBill, LiveResult]:
        repository = StorageConsensusRepository()
        coordinator = ConsensusCoordinator(repository)
        current = session()
        coordinator.save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Атомарный проект",
            summary="Проверка единой транзакции",
            materials=None,
        )
        coordinator.begin_bill(
            current,
            storage.tvrs_get_bill_dict_by_id(bill.id),  # type: ignore[arg-type]
            actor=ConsensusActor(1, "Ведущий"),
        )
        storage.tvrs_mark_bill_status(bill.id, "voting")
        current.votes = {1: "yes", 2: "no", 3: "yes"}
        coordinator.claim_finalization(
            current,
            kind=kind,
            actor=ConsensusActor(1, "Ведущий"),
            veto_authorized=kind == "veto",
            oral_authorized=kind == "oral",
            action_details=(
                {"oral_status": "accepted", "oral_note": "Решение принято очно"}
                if kind == "oral"
                else None
            ),
        )
        result = LiveResult(
            bill_id=bill.id,
            bill_number=bill.bill_number,
            title=bill.title,
            status="accepted",
            internal_percent=100.0,
            overall_percent=51.0,
            internal_active=True,
            votes=dict(current.votes),
        )
        return coordinator, current, bill, result

    def test_oral_finalization_is_atomic_audited_and_distinct_from_votes(self) -> None:
        coordinator, current, bill, _ = self._prepared_finalization(kind="oral")
        result = LiveResult(
            bill_id=bill.id,
            bill_number=bill.bill_number,
            title=bill.title,
            status="accepted",
            internal_percent=0.0,
            overall_percent=0.0,
            internal_active=False,
            votes=dict(current.votes),
            resolution_method="oral",
            resolution_note="Решение принято очно",
            resolved_by_id=1,
            resolved_by_display="Ведущий",
        )

        coordinator.complete_result_atomically(
            current,
            result,
            bill_status="accepted",
            result_summary="принят • устное решение",
            event_type="oral_result_recorded",
            actor=ConsensusActor(1, "Ведущий"),
        )

        persisted = storage.tvrs_live_result_for_bill(current.session_key, bill.id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["resolution_method"], "oral")  # type: ignore[index]
        self.assertEqual(persisted["resolution_note"], "Решение принято очно")  # type: ignore[index]
        self.assertEqual(persisted["resolved_by_id"], 1)  # type: ignore[index]
        self.assertEqual(current.results[-1].resolution_method, "oral")
        self.assertEqual(storage.tvrs_consensus_events(current.session_key)[-1]["event_type"], "oral_result_recorded")

    def test_atomic_finalization_commits_result_session_bill_event_and_delivery(self) -> None:
        coordinator, current, bill, result = self._prepared_finalization()
        previous_revision = current.revision
        receipt = coordinator.complete_result_atomically(
            current,
            result,
            bill_status="accepted",
            result_summary="принят",
            event_type="vote_finalized",
            actor=ConsensusActor(1, "Ведущий"),
            deliveries=[
                {
                    "topic": "test.consensus",
                    "dedupe_key": f"consensus:{current.session_key}:{bill.id}:public",
                    "payload": {"bill_id": bill.id},
                }
            ],
        )

        self.assertFalse(receipt["idempotent"])
        self.assertEqual(current.stage, "after_result")
        self.assertEqual(current.revision, previous_revision + 1)
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(bill.id)["status"], "accepted")  # type: ignore[index]
        self.assertEqual(storage.tvrs_live_result_for_bill(current.session_key, bill.id)["status"], "accepted")  # type: ignore[index]
        self.assertEqual(storage.delivery_outbox_counts(), {"pending": 1})
        self.assertEqual(storage.tvrs_consensus_events(current.session_key)[-1]["event_type"], "vote_finalized")

    def test_atomic_finalization_rolls_back_everything_when_delivery_is_invalid(self) -> None:
        coordinator, current, bill, result = self._prepared_finalization()
        previous_revision = current.revision
        with self.assertRaises(ValueError):
            coordinator.complete_result_atomically(
                current,
                result,
                bill_status="accepted",
                result_summary="принят",
                event_type="vote_finalized",
                deliveries=[{"topic": "", "dedupe_key": "broken", "payload": {}}],
            )

        self.assertEqual(current.stage, "finalizing")
        self.assertEqual(current.revision, previous_revision)
        self.assertIsNone(storage.tvrs_live_result_for_bill(current.session_key, bill.id))
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(bill.id)["status"], "voting")  # type: ignore[index]
        self.assertEqual(storage.delivery_outbox_counts(), {})
        self.assertNotEqual(storage.tvrs_consensus_events(current.session_key)[-1]["event_type"], "vote_finalized")

    def test_veto_retry_is_idempotent(self) -> None:
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Проект с вето",
            summary="Описание",
            materials=None,
        )
        first = storage.tvrs_create_retry_bill(bill.id, 1, "ППС")
        second = storage.tvrs_create_retry_bill(bill.id, 1, "ППС")
        self.assertIsNotNone(first)
        self.assertEqual(first["id"], second["id"])  # type: ignore[index]
        self.assertEqual(first["status"], "pending_veto")  # type: ignore[index]
        self.assertNotIn(int(first["id"]), {int(item["id"]) for item in storage.tvrs_queue_bills(77)})  # type: ignore[index]
        self.assertEqual(len(storage.tvrs_recent_bills(77)), 2)

    def test_veto_retry_becomes_visible_only_with_atomic_result_commit(self) -> None:
        coordinator, current, bill, _ = self._prepared_finalization(kind="veto")
        retry = storage.tvrs_create_retry_bill(bill.id, 1, "ППС")
        self.assertIsNotNone(retry)
        result = LiveResult(
            bill_id=bill.id,
            bill_number=bill.bill_number,
            title=bill.title,
            status="vetoed",
            internal_percent=0.0,
            overall_percent=0.0,
            internal_active=False,
            votes=dict(current.votes),
            veto_by_id=1,
            retry_bill_number=int(retry["bill_number"]),  # type: ignore[index]
        )
        with self.assertRaises(ValueError):
            coordinator.complete_result_atomically(
                current,
                result,
                bill_status="vetoed",
                result_summary="вето",
                event_type="veto_applied",
                actor=ConsensusActor(1, "ППС"),
                deliveries=[{"topic": "", "dedupe_key": "invalid", "payload": {}}],
            )
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(int(retry["id"]))["status"], "pending_veto")  # type: ignore[index]
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(bill.id)["status"], "voting")  # type: ignore[index]

        coordinator.complete_result_atomically(
            current,
            result,
            bill_status="vetoed",
            result_summary="вето",
            event_type="veto_applied",
            actor=ConsensusActor(1, "ППС"),
        )
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(int(retry["id"]))["status"], "requeued")  # type: ignore[index]
        self.assertIn(int(retry["id"]), {int(item["id"]) for item in storage.tvrs_queue_bills(77)})  # type: ignore[index]

    def test_begin_bill_atomically_commits_state_bill_and_invites(self) -> None:
        repository = StorageConsensusRepository()
        coordinator = ConsensusCoordinator(repository)
        current = session()
        coordinator.save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Атомарное начало",
            summary="Состояние и приглашения",
            materials=None,
        )
        delivery = {
            "topic": "test.control",
            "dedupe_key": f"begin:{current.session_key}:{bill.id}",
            "payload": {"guild_id": 77, "bill_id": bill.id},
        }
        receipt = coordinator.begin_bill_atomically(
            current,
            storage.tvrs_get_bill_dict_by_id(bill.id),  # type: ignore[arg-type]
            actor=ConsensusActor(1, "Ведущий"),
            deliveries=[delivery],
        )
        self.assertFalse(receipt["idempotent"])
        self.assertEqual(current.stage, "voting")
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(bill.id)["status"], "voting")  # type: ignore[index]
        self.assertEqual(storage.delivery_outbox_counts(), {"pending": 1})
        with self.assertRaisesRegex(ValueError, "locked_by_active_consensus"):
            storage.tvrs_delete_bill_by_number(77, bill.bill_number)
        with self.assertRaisesRegex(ValueError, "locked_by_active_consensus"):
            storage.tvrs_update_bill_field(77, bill.bill_number, "title", "Опасная правка")

    def test_hidden_veto_retry_is_protected_from_admin_mutation(self) -> None:
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Проект с защищённым повтором",
            summary="Описание",
            materials=None,
        )
        retry = storage.tvrs_create_retry_bill(bill.id, 1, "ППС")
        self.assertIsNotNone(retry)
        with self.assertRaisesRegex(ValueError, "locked_by_active_consensus"):
            storage.tvrs_delete_bill_by_number(77, int(retry["bill_number"]))  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "locked_by_active_consensus"):
            storage.tvrs_update_bill_field(
                77,
                int(retry["bill_number"]),  # type: ignore[index]
                "status",
                "requeued",
            )

    def test_decided_bill_stays_protected_until_plenary_is_finished(self) -> None:
        coordinator, current, bill, result = self._prepared_finalization()
        coordinator.complete_result_atomically(
            current,
            result,
            bill_status="accepted",
            result_summary="принят",
            event_type="vote_finalized",
            actor=ConsensusActor(1, "Ведущий"),
        )
        self.assertEqual(current.stage, "after_result")

        with self.assertRaisesRegex(ValueError, "bill_locked_by_active_consensus"):
            storage.tvrs_delete_bill_by_number(77, bill.bill_number)
        with self.assertRaisesRegex(ValueError, "bill_locked_by_active_consensus"):
            storage.tvrs_update_bill_field(77, bill.bill_number, "status", "requeued")

        next_bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Следующий проект",
            summary="Защита решений всего пленарного заседания",
            materials=None,
        )
        coordinator.begin_bill_atomically(
            current,
            storage.tvrs_get_bill_dict_by_id(next_bill.id),  # type: ignore[arg-type]
            actor=ConsensusActor(1, "Ведущий"),
            deliveries=[],
        )
        with self.assertRaisesRegex(ValueError, "bill_locked_by_active_consensus"):
            storage.tvrs_update_bill_field(77, bill.bill_number, "title", "Поздняя правка")

    def test_universal_undo_cannot_restore_bill_over_active_consensus(self) -> None:
        repository = StorageConsensusRepository()
        coordinator = ConsensusCoordinator(repository)
        current = session()
        coordinator.save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=44,
            author_display="Редактор",
            title="Исходное название",
            summary="Проверка безопасной отмены",
            materials=None,
        )
        storage.tvrs_update_bill_field(
            77,
            bill.bill_number,
            "title",
            "Название для голосования",
            actor_id=44,
            actor_display="Редактор",
        )
        action = storage.bot_list_actions(77, actor_id=44, module="tvrs", limit=1)[0]
        coordinator.begin_bill_atomically(
            current,
            storage.tvrs_get_bill_dict_by_id(bill.id),  # type: ignore[arg-type]
            actor=ConsensusActor(1, "Ведущий"),
            deliveries=[],
        )

        with self.assertRaisesRegex(ValueError, "bot_action_locked_by_active_consensus"):
            storage.bot_undo_action(
                guild_id=77,
                target_actor_id=44,
                undone_by_id=44,
                undone_by_display="Редактор",
                reason="Не должна пройти",
                log_channel_id=200,
                admin_user_id=300,
                channel_id=100,
                action_id=int(action["id"]),
            )

        persisted_bill = storage.tvrs_get_bill_dict_by_id(bill.id)
        self.assertEqual(persisted_bill["status"], "voting")  # type: ignore[index]
        self.assertEqual(persisted_bill["title"], "Название для голосования")  # type: ignore[index]
        durable = repository.active_snapshots(77)[0]
        self.assertEqual(durable["stage"], "voting")
        self.assertEqual(durable["current_bill"]["title"], "Название для голосования")

    def test_begin_and_finish_transactions_roll_back_invalid_delivery(self) -> None:
        repository = StorageConsensusRepository()
        coordinator = ConsensusCoordinator(repository)
        current = session()
        coordinator.save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Проверка отката",
            summary="Ни одного частичного состояния",
            materials=None,
        )
        initial = session_to_snapshot(current)
        with self.assertRaises(ValueError):
            coordinator.begin_bill_atomically(
                current,
                storage.tvrs_get_bill_dict_by_id(bill.id),  # type: ignore[arg-type]
                actor=ConsensusActor(1, "Ведущий"),
                deliveries=[{"topic": "", "dedupe_key": "bad", "payload": {}}],
            )
        self.assertEqual(session_to_snapshot(current), initial)
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(bill.id)["status"], "draft")  # type: ignore[index]

        coordinator.begin_bill_atomically(
            current,
            storage.tvrs_get_bill_dict_by_id(bill.id),  # type: ignore[arg-type]
            actor=ConsensusActor(1, "Ведущий"),
            deliveries=[],
        )
        voting = session_to_snapshot(current)
        with self.assertRaises(ValueError):
            coordinator.finish_atomically(
                current,
                actor=ConsensusActor(1, "Ведущий"),
                deliveries=[{"topic": "", "dedupe_key": "bad-finish", "payload": {}}],
            )
        self.assertEqual(session_to_snapshot(current), voting)
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(bill.id)["status"], "voting")  # type: ignore[index]
        self.assertEqual(storage.tvrs_get_next_plenary_number(77, 4), 4)

        coordinator.finish_atomically(
            current,
            actor=ConsensusActor(1, "Ведущий"),
            deliveries=[
                {
                    "topic": "test.summary",
                    "dedupe_key": f"finish:{current.session_key}",
                    "payload": {"guild_id": 77},
                }
            ],
        )
        self.assertTrue(current.finished)
        self.assertEqual(current.stage, "finished")
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(bill.id)["status"], "requeued")  # type: ignore[index]
        self.assertEqual(storage.tvrs_get_next_plenary_number(77, 4), 5)
        self.assertEqual(repository.active_snapshots(77), [])
        self.assertEqual(storage.delivery_outbox_counts(), {"pending": 1})
        stale = session_from_snapshot(voting)
        with self.assertRaises(RuntimeError):
            repository.save(stale, "stale_receipt_after_finish")
        self.assertEqual(repository.active_snapshots(77), [])
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(bill.id)["status"], "requeued")  # type: ignore[index]

    def test_generic_session_save_uses_revision_compare_and_swap(self) -> None:
        repository = StorageConsensusRepository()
        coordinator = ConsensusCoordinator(repository)
        current = session()
        coordinator.save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        first_writer = session_from_snapshot(session_to_snapshot(current))
        stale_writer = session_from_snapshot(session_to_snapshot(current))

        repository.save(first_writer, "first_writer")
        with self.assertRaisesRegex(RuntimeError, "revision_conflict"):
            repository.save(stale_writer, "stale_writer")

        snapshots = repository.active_snapshots(77)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(int(snapshots[0]["revision"]), 2)
        self.assertEqual(storage.tvrs_consensus_events(current.session_key)[-1]["event_type"], "first_writer")

    def test_corrupt_session_is_quarantined_and_bill_returns_to_queue(self) -> None:
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Проект в повреждённой сессии",
            summary="Описание",
            materials=None,
        )
        current = session()
        current.stage = "voting"
        current.current_bill = storage.tvrs_get_bill_dict_by_id(bill.id)
        _consensus.save(current, "bill_voting_started", actor=ConsensusActor(1, "Ведущий"))
        storage.tvrs_mark_bill_status(bill.id, "voting")

        self.assertTrue(storage.tvrs_consensus_quarantine_session(current.session_key, "bad snapshot"))
        self.assertEqual(StorageConsensusRepository().active_snapshots(77), [])
        self.assertEqual(storage.tvrs_get_bill_dict_by_id(bill.id)["status"], "requeued")  # type: ignore[index]
        events = storage.tvrs_consensus_events(current.session_key)
        self.assertEqual(events[-1]["event_type"], "session_quarantined")

    def test_persisted_takeover_survives_restart_without_changing_roster(self) -> None:
        repository = StorageConsensusRepository()
        coordinator = ConsensusCoordinator(repository)
        current = session()
        coordinator.save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        original_roster = set(current.participants)

        coordinator.transfer_leadership(
            current,
            new_leader_id=2,
            new_leader_display="Новый ведущий",
            actor=ConsensusActor(2, "Новый ведущий"),
        )

        restored = session_from_snapshot(repository.active_snapshots(77)[0])
        self.assertEqual(restored.leader_id, 2)
        self.assertEqual(restored.leader_display, "Новый ведущий")
        self.assertEqual(set(restored.participants), original_roster)
        self.assertEqual(storage.tvrs_consensus_events(current.session_key)[-1]["event_type"], "leadership_transferred")

    def test_malformed_snapshot_is_returned_for_quarantine(self) -> None:
        current = session()
        StorageConsensusRepository().save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        with storage._db_lock, storage.connect() as con:
            con.execute(
                "UPDATE tvrs_consensus_sessions SET snapshot_json = ? WHERE session_key = ?",
                ("{broken", current.session_key),
            )
            con.commit()

        snapshots = StorageConsensusRepository().active_snapshots(77)
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(snapshots[0]["corrupt_snapshot_json"])
        with self.assertRaises(ConsensusStateError):
            session_from_snapshot(snapshots[0])

    def test_destructive_reset_creates_backup_and_runs_once(self) -> None:
        storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Старый проект",
            summary="Будет удалён миграцией",
            materials=None,
        )
        storage.CONSENSUS_V2_RESET_ID = "consensus-test-reset-with-backup"
        storage.init_db()

        self.assertEqual(storage.tvrs_recent_bills(77), [])
        backups = list((storage.DATA_DIR / "backups").glob("tmod-before-consensus-reset-*.db"))
        self.assertEqual(len(backups), 1)
        first_size = backups[0].stat().st_size
        storage.init_db()
        self.assertEqual(len(list((storage.DATA_DIR / "backups").glob("*.db"))), 1)
        self.assertEqual(backups[0].stat().st_size, first_size)

    def test_destructive_reset_backs_up_registration_session_without_bills(self) -> None:
        coordinator = ConsensusCoordinator(StorageConsensusRepository())
        coordinator.save(session(), "session_created", actor=ConsensusActor(1, "Ведущий"))
        storage.CONSENSUS_V2_RESET_ID = "consensus-test-session-only-reset"

        storage.init_db()

        backups = list((storage.DATA_DIR / "backups").glob("tmod-before-consensus-reset-*.db"))
        self.assertEqual(len(backups), 1)
        with storage._db_lock, storage.connect() as con:
            remaining = con.execute("SELECT COUNT(*) AS n FROM tvrs_consensus_sessions").fetchone()
        self.assertEqual(int(remaining["n"]), 0)

    def test_legacy_result_dedup_backs_up_and_keeps_latest_result(self) -> None:
        migration_key = storage._consensus_result_dedup_meta_key(storage.CONSENSUS_RESULT_DEDUP_ID)
        now = storage.utc_now_iso()
        with storage._db_lock, storage.connect() as con:
            con.execute("DROP INDEX IF EXISTS idx_tvrs_live_results_once")
            con.execute("DELETE FROM meta WHERE key = ?", (migration_key,))
            for percent in (51.0, 88.0):
                con.execute(
                    """
                    INSERT INTO tvrs_live_results(
                        guild_id, session_key, plenary_number, bill_id, bill_number,
                        bill_title, status, internal_percent, overall_percent,
                        internal_active, votes_json, created_at
                    ) VALUES(77, '77:legacy', 4, 10, 9, 'Legacy', 'accepted',
                             100.0, ?, 1, '{"1":"yes"}', ?)
                    """,
                    (percent, now),
                )
            con.commit()

        storage.init_db()

        backups = list(
            (storage.DATA_DIR / "backups").glob("tmod-before-consensus-result-dedup-*.db")
        )
        self.assertEqual(len(backups), 1)
        with storage._db_lock, sqlite3.connect(backups[0]) as backup:
            self.assertEqual(
                int(
                    backup.execute(
                        "SELECT COUNT(*) FROM tvrs_live_results WHERE session_key = '77:legacy' AND bill_id = 10"
                    ).fetchone()[0]
                ),
                2,
            )
        latest = storage.tvrs_live_result_for_bill("77:legacy", 10)
        self.assertEqual(float(latest["overall_percent"]), 88.0)  # type: ignore[index]


class ConsensusInteractionAckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _consensus_registry.sessions.clear()
        _consensus_registry._locks.clear()
        self.current = session()
        self.current.stage = "voting"
        self.current.current_bill = {"id": 10, "bill_number": 9, "title": "Быстрый ACK"}
        _consensus_registry.add(self.current)

    async def asyncTearDown(self) -> None:
        for task in list(_finalization_retry_tasks.values()):
            task.cancel()
        if _finalization_retry_tasks:
            await asyncio.gather(*_finalization_retry_tasks.values(), return_exceptions=True)
        _finalization_retry_tasks.clear()
        _consensus_registry.sessions.clear()
        _consensus_registry._locks.clear()

    def interaction(self, user_id: int, acknowledged: threading.Event) -> SimpleNamespace:
        async def defer(**kwargs) -> None:
            acknowledged.set()

        return SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name=f"Участник {user_id}"),
            response=SimpleNamespace(defer=AsyncMock(side_effect=defer), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
            client=SimpleNamespace(get_guild=lambda guild_id: None),
            edit_original_response=AsyncMock(),
            guild=SimpleNamespace(id=77),
        )

    async def test_participant_vote_acknowledges_before_persistent_mutation(self) -> None:
        acknowledged = threading.Event()
        interaction = self.interaction(2, acknowledged)

        def cast_vote(*args, **kwargs) -> bool:
            self.assertTrue(acknowledged.is_set())
            return False

        with (
            patch("modules.tvrs_consensus_views._consensus.cast_vote", side_effect=cast_vote) as mutation,
            patch("modules.tvrs_presentation.queue_short_lines", return_value="Очередь пуста."),
        ):
            await TVRSVoteView(self.current.session_key, 2)._cast(interaction, "yes")  # type: ignore[arg-type]

        self.assertEqual(interaction.response.defer.await_count, 1)
        mutation.assert_called_once()

    async def test_confirmation_acknowledges_before_persistent_mutation(self) -> None:
        self.current.stage = "registration"
        self.current.current_bill = None
        self.current.participants[2].confirmed = False
        acknowledged = threading.Event()
        interaction = self.interaction(2, acknowledged)

        def confirm(*args, **kwargs) -> bool:
            self.assertTrue(acknowledged.is_set())
            return True

        with patch("modules.tvrs_consensus_views._consensus.confirm_participant", side_effect=confirm) as mutation:
            await TVRSConfirmView(self.current.session_key, 2).confirm(interaction)  # type: ignore[arg-type]

        self.assertEqual(interaction.response.defer.await_count, 1)
        mutation.assert_called_once()
        interaction.edit_original_response.assert_awaited_once()

    async def test_failed_ack_prevents_vote_mutation(self) -> None:
        acknowledged = threading.Event()
        interaction = self.interaction(2, acknowledged)
        interaction.response.defer = AsyncMock(side_effect=RuntimeError("interaction expired"))
        mutation = MagicMock(return_value=False)

        with patch("modules.tvrs_consensus_views._consensus.cast_vote", mutation):
            with self.assertRaises(RuntimeError):
                await TVRSVoteView(self.current.session_key, 2)._cast(interaction, "yes")  # type: ignore[arg-type]

        mutation.assert_not_called()

    async def test_vote_button_from_previous_bill_cannot_vote_on_next_bill(self) -> None:
        old_view = TVRSVoteView(self.current.session_key, 2)
        self.current.current_bill = {"id": 11, "bill_number": 10, "title": "Следующий проект"}
        interaction = self.interaction(2, threading.Event())

        with patch("modules.tvrs_consensus_views._consensus.cast_vote") as mutation:
            await old_view._cast(interaction, "yes")  # type: ignore[arg-type]

        mutation.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        self.assertEqual(self.current.votes, {})

    async def test_reused_vote_message_keeps_old_wire_id_on_old_generation(self) -> None:
        message_id = 9002
        old_view = TVRSVoteView(self.current.session_key, 2)
        old_custom_id = f"tvrs_vote_yes:{self.current.session_key}:10:2"
        self.current.current_bill = {"id": 11, "bill_number": 10, "title": "Следующий проект"}
        new_view = TVRSVoteView(self.current.session_key, 2)
        new_custom_id = f"tvrs_vote_yes:{self.current.session_key}:11:2"
        store = ViewStore(MagicMock())

        store.add_view(old_view, message_id=message_id)
        store.add_view(new_view, message_id=message_id)

        routes = store._views[message_id]
        button_type = discord.ComponentType.button.value
        self.assertIs(routes[(button_type, old_custom_id)].view, old_view)
        self.assertIs(routes[(button_type, new_custom_id)].view, new_view)
        self.assertNotEqual(old_custom_id, new_custom_id)

    async def test_legacy_recovery_button_refreshes_without_casting_vote(self) -> None:
        restored_view = tvrs.TVRSRestoredVoteView(
            self.current.session_key,
            2,
            bill_id=10,
        )
        legacy_yes = next(
            item
            for item in restored_view.children
            if item.custom_id == f"tvrs_vote_yes:{self.current.session_key}:2"
        )
        interaction = self.interaction(2, threading.Event())
        interaction.response.edit_message = AsyncMock()

        with (
            patch("modules.tvrs_consensus_views._consensus.cast_vote") as mutation,
            patch("modules.tvrs_presentation.queue_short_lines", return_value="Очередь пуста."),
        ):
            await legacy_yes.callback(interaction)  # type: ignore[arg-type]

        mutation.assert_not_called()
        interaction.response.defer.assert_not_awaited()
        interaction.response.edit_message.assert_awaited_once()

    async def test_host_panel_from_previous_bill_cannot_finalize_next_bill(self) -> None:
        old_view = TVRSHostVoteView(self.current.session_key)
        self.current.current_bill = {"id": 11, "bill_number": 10, "title": "Следующий проект"}
        interaction = self.interaction(1, threading.Event())

        await old_view.finish(interaction)  # type: ignore[arg-type]

        self.assertEqual(self.current.stage, "voting")
        self.assertEqual(self.current.current_bill["id"], 11)
        self.assertEqual(self.current.results, [])


class ConsensusRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.old_activity_file = storage.LEGACY_ACTIVITY_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "consensus-recovery-test.db"
        storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
        storage.init_db()
        _consensus_registry.sessions.clear()
        _consensus_registry._locks.clear()
        _restored_consensus_guilds.clear()
        _consensus_recovery_tasks.clear()

    async def asyncTearDown(self) -> None:
        for task in list(_finalization_retry_tasks.values()):
            task.cancel()
        if _finalization_retry_tasks:
            await asyncio.gather(*_finalization_retry_tasks.values(), return_exceptions=True)
        _finalization_retry_tasks.clear()
        for task in list(_consensus_recovery_tasks.values()):
            task.cancel()
        if _consensus_recovery_tasks:
            await asyncio.gather(*_consensus_recovery_tasks.values(), return_exceptions=True)
        _consensus_recovery_tasks.clear()
        _consensus_registry.sessions.clear()
        _consensus_registry._locks.clear()
        _restored_consensus_guilds.clear()
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        storage.LEGACY_ACTIVITY_FILE = self.old_activity_file
        self.temp_dir.cleanup()

    async def test_registration_buttons_are_restored_after_restart(self) -> None:
        current = session()
        current.participants[2].confirmed = False
        current.participants[2].dm_message_id = 9002
        StorageConsensusRepository().save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))

        added_views: list[tuple[object, int | None]] = []
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace(
            get_guild=lambda guild_id: guild if guild_id == 77 else None,
            add_view=lambda view, message_id=None: added_views.append((view, message_id)),
        )
        restored = await restore_tvrs_consensus_sessions(bot)  # type: ignore[arg-type]

        self.assertEqual(restored, 1)
        self.assertIsNotNone(_consensus_registry.get(77))
        self.assertEqual([message_id for _, message_id in added_views], [9002])

    async def test_vote_recovery_accepts_legacy_and_v2_wire_ids(self) -> None:
        current = session()
        current.stage = "voting"
        current.current_bill = {"id": 10, "bill_number": 9, "title": "Активный проект"}
        current.participants[2].vote_message_id = 9002
        current.participants[2].vote_bill_id = 10
        StorageConsensusRepository().save(
            current,
            "bill_voting_started",
            actor=ConsensusActor(1, "Ведущий"),
        )

        added_views: list[tuple[object, int | None]] = []
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace(
            get_guild=lambda guild_id: guild if guild_id == 77 else None,
            add_view=lambda view, message_id=None: added_views.append((view, message_id)),
        )

        restored = await restore_tvrs_consensus_sessions(bot)  # type: ignore[arg-type]

        self.assertEqual(restored, 1)
        self.assertEqual([message_id for _, message_id in added_views], [9002])
        custom_ids = {str(item.custom_id) for item in added_views[0][0].children}  # type: ignore[attr-defined]
        self.assertIn(f"tvrs_vote_yes:{current.session_key}:2", custom_ids)
        self.assertIn(f"tvrs_vote_yes:{current.session_key}:10:2", custom_ids)

    async def test_vote_recovery_replaces_panel_from_previous_bill_once(self) -> None:
        current = session()
        current.stage = "voting"
        current.current_bill = {"id": 10, "bill_number": 9, "title": "Новый проект"}
        current.participants[2].vote_message_id = 9002
        current.participants[2].vote_bill_id = 9
        current.participants[3].vote_message_id = 9003
        current.participants[3].vote_bill_id = 10
        StorageConsensusRepository().save(
            current,
            "bill_voting_started",
            actor=ConsensusActor(1, "Ведущий"),
        )

        added_views: list[tuple[object, int | None]] = []
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace(
            get_guild=lambda guild_id: guild if guild_id == 77 else None,
            add_view=lambda view, message_id=None: added_views.append((view, message_id)),
        )

        restored = await restore_tvrs_consensus_sessions(bot)  # type: ignore[arg-type]

        self.assertEqual(restored, 1)
        self.assertEqual([message_id for _, message_id in added_views], [9003])
        self.assertEqual(storage.delivery_outbox_counts(), {"pending": 5})
        with storage._db_lock, storage.connect() as connection:
            rows = connection.execute(
                "SELECT topic, dedupe_key, payload_json, priority, supersede_key "
                "FROM delivery_outbox WHERE status = 'pending'"
            ).fetchall()
        control_rows = [
            row for row in rows if row["topic"] == "tvrs.consensus.control-dm.v1"
        ]
        self.assertEqual(len(control_rows), 2)
        self.assertEqual(
            {row["topic"] for row in rows},
            {
                "tvrs.consensus.control-dm.v1",
                "tvrs.consensus.control-notice.v1",
                "tvrs.consensus.phase-announcement.v1",
            },
        )
        payloads = [json.loads(row["payload_json"]) for row in control_rows]
        self.assertEqual({payload["user_id"] for payload in payloads}, {2, 3})
        self.assertTrue(all(payload["bill_id"] == 10 for payload in payloads))
        self.assertTrue(all(row["priority"] == 200 for row in control_rows))
        self.assertEqual(
            {row["supersede_key"] for row in control_rows},
            {
                "consensus:77:test:control:2",
                "consensus:77:test:control:3",
            },
        )

    async def test_transient_view_restore_failure_retries_without_new_ready_event(self) -> None:
        current = session()
        current.participants[2].confirmed = False
        current.participants[2].dm_message_id = 9002
        StorageConsensusRepository().save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        guild = SimpleNamespace(id=77)
        attempts = 0

        def add_view(view, message_id=None) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("Discord cache not ready")

        bot = SimpleNamespace(
            get_guild=lambda guild_id: guild if guild_id == 77 else None,
            add_view=add_view,
            is_closed=lambda: False,
        )
        with (
            patch("modules.tvrs_recovery.log_technical_event", new=AsyncMock(return_value=True)),
            patch("modules.tvrs_recovery.asyncio.sleep", new=AsyncMock(return_value=None)),
            patch("modules.tvrs_recovery.traceback.print_exc"),
        ):
            restored = await restore_tvrs_consensus_sessions(bot)  # type: ignore[arg-type]
            self.assertEqual(restored, 0)
            await _consensus_recovery_tasks[77]

        self.assertGreaterEqual(attempts, 2)
        self.assertIn(77, _restored_consensus_guilds)

    async def test_transient_initial_snapshot_read_retries_without_new_ready_event(self) -> None:
        bot = SimpleNamespace(
            get_guild=lambda guild_id: None,
            is_closed=lambda: False,
        )
        with (
            patch(
                "modules.tvrs_recovery._consensus_repository.active_snapshots",
                side_effect=[RuntimeError("temporary sqlite read failure"), []],
            ) as read_snapshots,
            patch("modules.tvrs_recovery.asyncio.sleep", new=AsyncMock(return_value=None)),
            patch("modules.tvrs_recovery.traceback.print_exc"),
        ):
            restored = await restore_tvrs_consensus_sessions(bot)  # type: ignore[arg-type]
            self.assertEqual(restored, 0)
            retry_task = _consensus_recovery_tasks[0]
            await retry_task

        self.assertEqual(read_snapshots.call_count, 2)
        self.assertNotIn(0, _consensus_recovery_tasks)

    async def test_legacy_after_result_backfills_only_idempotent_dm_edits(self) -> None:
        current = session()
        current.stage = "after_result"
        current.participants[2].vote_message_id = 9202
        current.participants[3].vote_message_id = 9203
        current.results = [
            LiveResult(
                bill_id=10,
                bill_number=9,
                title="Итог до outbox",
                status="accepted",
                internal_percent=100.0,
                overall_percent=51.0,
                internal_active=True,
                votes={1: "yes", 2: "no", 3: "yes"},
            )
        ]
        StorageConsensusRepository().save(current, "vote_finalized", actor=ConsensusActor(1, "Ведущий"))
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace(
            get_guild=lambda guild_id: guild if guild_id == 77 else None,
            add_view=lambda view, message_id=None: None,
        )

        restored = await restore_tvrs_consensus_sessions(bot)  # type: ignore[arg-type]

        self.assertEqual(restored, 1)
        self.assertEqual(storage.delivery_outbox_counts(), {"pending": 2})
        with storage._db_lock, storage.connect() as con:
            rows = con.execute("SELECT payload_json FROM delivery_outbox ORDER BY id").fetchall()
        self.assertTrue(all('"destination":"participant_dm"' in row["payload_json"] for row in rows))

    async def test_concurrent_finalization_creates_one_result(self) -> None:
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Однократный итог",
            summary="Проверка одновременного завершения",
            materials=None,
        )
        current = session()
        current.stage = "voting"
        current.current_bill = storage.tvrs_get_bill_dict_by_id(bill.id)
        current.votes = {1: "yes", 2: "no", 3: "yes"}
        _consensus_registry.add(current)
        _consensus.save(current, "bill_voting_started", actor=ConsensusActor(1, "Ведущий"))
        get_channel = MagicMock(return_value=None)
        guild = SimpleNamespace(id=77, get_channel=get_channel)
        bot = SimpleNamespace()

        await asyncio.gather(
            finalize_current_vote(bot, guild, current, forced=False),  # type: ignore[arg-type]
            finalize_current_vote(bot, guild, current, forced=False),  # type: ignore[arg-type]
        )

        self.assertEqual(current.stage, "after_result")
        self.assertEqual(len(current.results), 1)
        self.assertIsNotNone(storage.tvrs_live_result_for_bill(current.session_key, bill.id))
        self.assertEqual(storage.delivery_outbox_counts(), {"pending": 3})
        # Only the winning finalizer projects the canonical public card.
        get_channel.assert_called_once_with(100)
        events = storage.tvrs_consensus_events(current.session_key)
        self.assertEqual(sum(item["event_type"] == "vote_finalized" for item in events), 1)

    async def test_vote_and_pause_are_serialized_without_losing_live_or_durable_state(self) -> None:
        current = session()
        current.stage = "voting"
        current.current_bill = {"id": 10, "bill_number": 9, "title": "Проверка блокировки"}
        _consensus_registry.add(current)
        _consensus.save(current, "bill_voting_started", actor=ConsensusActor(1, "Ведущий"))
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=2, display_name="Участник 2"),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
            client=bot,
        )
        entered_vote = threading.Event()
        release_vote = threading.Event()
        original_cast_vote = _consensus.cast_vote

        def slow_cast_vote(*args, **kwargs) -> bool:
            entered_vote.set()
            if not release_vote.wait(timeout=5):
                raise TimeoutError("test did not release vote mutation")
            return original_cast_vote(*args, **kwargs)

        with (
            patch("modules.tvrs_consensus_views._consensus.cast_vote", side_effect=slow_cast_vote),
            patch("modules.tvrs_discussion.update_all_vote_dms", new=AsyncMock()),
            patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()),
        ):
            vote_task = asyncio.create_task(
                TVRSVoteView(current.session_key, 2)._cast(interaction, "yes")  # type: ignore[arg-type]
            )
            self.assertTrue(await asyncio.to_thread(entered_vote.wait, 3))
            pause_task = asyncio.create_task(
                pause_session(bot, guild, current, "Проверка кворума", automatic=True)  # type: ignore[arg-type]
            )
            await asyncio.sleep(0)
            self.assertFalse(pause_task.done())
            release_vote.set()
            await asyncio.gather(vote_task, pause_task)

        self.assertEqual(current.stage, "paused")
        self.assertEqual(current.votes, {2: "yes"})
        durable = StorageConsensusRepository().active_snapshots(77)[0]
        self.assertEqual(durable["stage"], "paused")
        self.assertEqual(durable["votes"], {"2": "yes"})
        self.assertEqual(int(durable["revision"]), int(current.revision))

    async def test_registration_to_voting_rechecks_voice_quorum_inside_session_lock(self) -> None:
        current = session()
        _consensus_registry.add(current)
        _consensus.save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Проверка кворума",
            summary="Участник вышел, пока действие ожидало блокировку",
            materials=None,
        )
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace()
        quorum = {"ready": True}
        lock = _consensus_registry.lock(77)
        await lock.acquire()
        try:
            with patch(
                "modules.tvrs_control.session_voice_quorum_ready",
                side_effect=lambda guild, session: (
                    quorum["ready"],
                    "Кворум есть" if quorum["ready"] else "Кворум утрачен",
                ),
            ):
                task = asyncio.create_task(
                    tvrs.begin_next_bill_vote(bot, guild, current, SimpleNamespace())  # type: ignore[arg-type]
                )
                await asyncio.sleep(0)
                quorum["ready"] = False
                lock.release()
                await task
        finally:
            if lock.locked():
                lock.release()

        self.assertEqual(current.stage, "registration")
        self.assertIsNone(current.current_bill)
        self.assertEqual(storage.tvrs_queue_bills(77)[0]["status"], "draft")

    async def test_empty_queue_finish_cannot_close_bill_started_after_queue_read(self) -> None:
        current = session()
        _consensus_registry.add(current)
        _consensus.save(current, "session_created", actor=ConsensusActor(1, "Ведущий"))
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace()
        original_finish_session = tvrs.finish_session

        async def start_bill_before_stale_finish(*args, **kwargs) -> None:
            bill = await asyncio.to_thread(
                storage.tvrs_create_bill,
                guild_id=77,
                channel_id=100,
                author_id=1,
                author_display="Автор",
                title="Опубликован между проверкой и завершением",
                summary="Параллельный запуск не должен быть закрыт старым решением пустой очереди",
                materials=None,
            )
            async with tvrs.consensus_session_lock(77):
                await asyncio.to_thread(
                    _consensus.begin_bill_atomically,
                    current,
                    storage.tvrs_get_bill_dict_by_id(bill.id),
                    actor=ConsensusActor(1, "Ведущий"),
                    deliveries=(),
                )
            await original_finish_session(*args, **kwargs)

        with (
            patch("modules.tvrs_control.session_voice_quorum_ready", return_value=(True, "Кворум есть")),
            patch("modules.tvrs_control.finish_session", new=AsyncMock(side_effect=start_bill_before_stale_finish)),
            patch("modules.tvrs_decision.ensure_sticky_message", new=AsyncMock()),
        ):
            await tvrs.begin_next_bill_vote(bot, guild, current, SimpleNamespace())  # type: ignore[arg-type]

        self.assertEqual(current.stage, "voting")
        self.assertIsNotNone(current.current_bill)
        durable = StorageConsensusRepository().active_snapshots(77)[0]
        self.assertEqual(durable["stage"], "voting")
        self.assertEqual(int(durable["current_bill"]["id"]), int(current.current_bill["id"]))

    async def test_paused_resume_rechecks_voice_quorum_inside_session_lock(self) -> None:
        current = session()
        current.stage = "paused"
        current.previous_stage = "voting"
        current.current_bill = {"id": 10, "bill_number": 9, "title": "Проект на паузе"}
        _consensus_registry.add(current)
        _consensus.save(current, "session_paused", actor=ConsensusActor(1, "Ведущий"))
        guild = SimpleNamespace(id=77)
        bot = SimpleNamespace()
        quorum = {"ready": True}
        lock = _consensus_registry.lock(77)
        await lock.acquire()
        try:
            with (
                patch(
                    "modules.tvrs_discussion.session_voice_quorum_ready",
                    side_effect=lambda guild, session: (
                        quorum["ready"],
                        "Кворум есть" if quorum["ready"] else "Кворум утрачен",
                    ),
                ),
                patch("modules.tvrs_discussion.update_host_vote_message", new=AsyncMock()),
            ):
                task = asyncio.create_task(
                    tvrs.resume_session(bot, guild, current)  # type: ignore[arg-type]
                )
                await asyncio.sleep(0)
                quorum["ready"] = False
                lock.release()
                await task
        finally:
            if lock.locked():
                lock.release()

        self.assertEqual(current.stage, "paused")
        durable = StorageConsensusRepository().active_snapshots(77)[0]
        self.assertEqual(durable["stage"], "paused")

    async def test_finalization_failure_is_retried_without_restart(self) -> None:
        bill = storage.tvrs_create_bill(
            guild_id=77,
            channel_id=100,
            author_id=1,
            author_display="Автор",
            title="Автовосстановление",
            summary="Первый commit временно падает",
            materials=None,
        )
        current = session()
        current.stage = "voting"
        current.current_bill = storage.tvrs_get_bill_dict_by_id(bill.id)
        current.votes = {1: "yes", 2: "no", 3: "yes"}
        _consensus_registry.add(current)
        _consensus.save(current, "bill_voting_started", actor=ConsensusActor(1, "Ведущий"))
        guild = SimpleNamespace(id=77, get_channel=lambda channel_id: None)
        bot = SimpleNamespace()
        original_commit = _consensus.complete_result_atomically
        attempts = 0
        retry_gate = asyncio.Event()

        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary database outage")
            return original_commit(*args, **kwargs)

        async def gated_sleep(delay: float) -> None:
            await retry_gate.wait()

        with (
            patch("modules.tvrs_decision._consensus.complete_result_atomically", side_effect=fail_once),
            patch("modules.tvrs_control.asyncio.sleep", side_effect=gated_sleep),
        ):
            with self.assertRaises(RuntimeError):
                await finalize_current_vote(bot, guild, current, forced=False)  # type: ignore[arg-type]
            retry_task = _finalization_retry_tasks[current.session_key]
            self.assertEqual(current.stage, "finalizing")
            retry_gate.set()
            await retry_task

        self.assertEqual(attempts, 2)
        self.assertEqual(current.stage, "after_result")
        self.assertIsNotNone(storage.tvrs_live_result_for_bill(current.session_key, bill.id))

    async def test_recoverable_discussion_menu_fits_discord_rows(self) -> None:
        current = session()
        current.stage = "discussion_type"
        current.current_bill = {"id": 10, "bill_number": 9, "title": "Обсуждение"}
        current.discussion_initiator_id = 3
        _consensus_registry.add(current)

        view = TVRSHostVoteView(current.session_key)
        self.assertEqual(len(view.children), 7)
        self.assertTrue(all((item.row or 0) <= 4 for item in view.children))
        self.assertLessEqual(sum((item.row or 0) == 0 for item in view.children), 5)
        self.assertLessEqual(sum((item.row or 0) == 1 for item in view.children), 5)


if __name__ == "__main__":
    unittest.main()
