from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from modules.consensus_core import LiveConsensusSession, LiveParticipant, LiveResult
from modules.consensus_runtime import registry
from modules.delivery_outbox import (
    DeliveryDeferred,
    DeliveryPermanentFailure,
    OutboxMessage,
)
from modules.tvrs_control import (
    _raise_classified_discord_error,
    deliver_consensus_control_dm,
    deliver_consensus_control_notice,
)
from modules.tvrs_delivery import (
    build_control_dm_deliveries,
    build_control_notice_deliveries,
    build_result_deliveries,
    make_result_delivery_handler,
)
from modules.tvrs_recovery import (
    recover_consensus_session,
    reconcile_consensus_liveness,
    reconcile_current_consensus_deliveries,
)


def _session(*, stage: str = "registration") -> LiveConsensusSession:
    participants = {
        1: LiveParticipant(
            user_id=1,
            display_name="Ведущий",
            mention="<@1>",
            kind="chair",
            permanent=True,
            confirmed=True,
        ),
        2: LiveParticipant(
            user_id=2,
            display_name="Сенатор",
            mention="<@2>",
            kind="senator",
            confirmed=stage != "registration",
        ),
    }
    current = LiveConsensusSession(
        session_key="77:delivery-recovery",
        guild_id=77,
        channel_id=100,
        leader_id=1,
        leader_display="Ведущий",
        plenary_number=4,
        participants=participants,
        stage=stage,
        revision=1,
    )
    if stage != "registration":
        current.current_bill = {
            "id": 10,
            "bill_number": 9,
            "title": "Надёжное восстановление",
        }
    return current


class ConsensusDeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        registry.sessions.clear()

    async def test_deleted_registration_panel_gets_fresh_repair_generation(self) -> None:
        current = _session()
        current.participants[2].dm_message_id = 9002
        bot = SimpleNamespace(add_view=lambda *args, **kwargs: None)
        guild = SimpleNamespace(id=77)
        ensured: list[dict] = []

        def ensure_current(**kwargs):
            ensured.append(kwargs)
            if ":projection-r" in kwargs["dedupe_key"]:
                # This semantic projection was already delivered before the
                # user deleted the message. Recovery must use a fresh repair.
                return ({"status": "delivered"}, False)
            return ({"status": "pending"}, True)

        with (
            patch(
                "modules.tvrs_recovery._saved_control_message_exists",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "modules.tvrs_recovery._outbox_storage.delivery_outbox_ensure_current",
                side_effect=ensure_current,
            ),
            patch(
                "modules.tvrs_recovery._enqueue_semantic_delivery",
                new=AsyncMock(return_value=False),
            ),
            patch("modules.tvrs_recovery.wake_delivery_worker") as wake,
        ):
            queued = await reconcile_current_consensus_deliveries(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                current,
                verify_discord_messages=True,
                retry_permanent_failures=True,
            )

        self.assertEqual(queued, 1)
        self.assertEqual(len(ensured), 2)
        repair = next(item for item in ensured if ":repair-r" in item["dedupe_key"])
        self.assertNotIn("recovery-control-v2", repair["dedupe_key"])
        self.assertEqual(repair["payload"]["user_id"], 2)
        wake.assert_called_once()

    async def test_recovery_checks_quorum_before_resuming_liveness(self) -> None:
        current = _session(stage="voting")
        order: list[str] = []

        async def lose_quorum(*_args) -> None:
            order.append("quorum")
            current.stage = "paused"

        async def liveness(*_args) -> bool:
            order.append(f"liveness:{current.stage}")
            return False

        guild = SimpleNamespace(id=77, get_channel=lambda _channel_id: None)
        with (
            patch(
                "modules.tvrs_recovery.repair_safe_consensus_invariants",
                new=AsyncMock(return_value=()),
            ),
            patch(
                "modules.tvrs_recovery.check_realtime_quorum",
                new=AsyncMock(side_effect=lose_quorum),
            ),
            patch(
                "modules.tvrs_recovery.reconcile_consensus_liveness",
                new=AsyncMock(side_effect=liveness),
            ),
            patch(
                "modules.tvrs_recovery.requeue_consensus_dead_deliveries",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "modules.tvrs_recovery.reconcile_current_consensus_deliveries",
                new=AsyncMock(return_value=0),
            ),
        ):
            await recover_consensus_session(
                SimpleNamespace(),  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                current,
                verify_discord_messages=False,
                retry_permanent_failures=False,
            )

        self.assertEqual(order, ["quorum", "liveness:paused"])

    async def test_startup_reprojects_existing_voting_panel_instead_of_only_registering_view(self) -> None:
        current = _session(stage="voting")
        current.revision = 17
        current.participants[2].vote_message_id = 9002
        current.participants[2].vote_bill_id = 10
        add_view = MagicMock()
        bot = SimpleNamespace(add_view=add_view)
        guild = SimpleNamespace(id=77)
        ensured: list[dict] = []

        def ensure_current(**kwargs):
            ensured.append(kwargs)
            return ({"status": "pending"}, True)

        with (
            patch(
                "modules.tvrs_recovery._saved_control_message_exists",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "modules.tvrs_recovery._outbox_storage.delivery_outbox_ensure_current",
                side_effect=ensure_current,
            ),
            patch(
                "modules.tvrs_recovery._enqueue_semantic_delivery",
                new=AsyncMock(return_value=False),
            ),
            patch("modules.tvrs_recovery._cleanup_restored_control_copies", new=AsyncMock()),
        ):
            queued = await reconcile_current_consensus_deliveries(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                current,
                verify_discord_messages=True,
                retry_permanent_failures=True,
            )

        self.assertEqual(queued, 1)
        self.assertEqual(len(ensured), 1)
        self.assertIn(":projection-r17-voting:", ensured[0]["dedupe_key"])
        self.assertEqual(ensured[0]["payload"]["bill_id"], 10)
        add_view.assert_called_once()

    async def test_watchdog_projection_key_is_stable_for_same_revision_and_stage(self) -> None:
        current = _session(stage="paused")
        current.revision = 41
        current.participants[2].vote_message_id = 9002
        current.participants[2].vote_bill_id = 10
        bot = SimpleNamespace(add_view=MagicMock())
        guild = SimpleNamespace(id=77)
        keys: list[str] = []
        inserted: set[str] = set()

        def ensure_current(**kwargs):
            key = str(kwargs["dedupe_key"])
            keys.append(key)
            created = key not in inserted
            inserted.add(key)
            return ({"status": "pending" if created else "delivered"}, created)

        with (
            patch(
                "modules.tvrs_recovery._outbox_storage.delivery_outbox_ensure_current",
                side_effect=ensure_current,
            ),
            patch(
                "modules.tvrs_recovery._enqueue_semantic_delivery",
                new=AsyncMock(return_value=False),
            ),
        ):
            first = await reconcile_current_consensus_deliveries(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                current,
                verify_discord_messages=False,
                retry_permanent_failures=False,
            )
            second = await reconcile_current_consensus_deliveries(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                current,
                verify_discord_messages=False,
                retry_permanent_failures=False,
            )

        self.assertEqual((first, second), (1, 0))
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 1)
        self.assertIn(":projection-r41-paused:", keys[0])
        bot.add_view.assert_not_called()

    async def test_periodic_watchdog_skips_known_permanent_dm_failure(self) -> None:
        current = _session()
        current.participants[2].dm_failed = True
        bot = SimpleNamespace(add_view=lambda *args, **kwargs: None)
        guild = SimpleNamespace(id=77)

        with (
            patch(
                "modules.tvrs_recovery._outbox_storage.delivery_outbox_ensure_current"
            ) as ensure_current,
            patch(
                "modules.tvrs_recovery._enqueue_semantic_delivery",
                new=AsyncMock(return_value=False),
            ),
        ):
            queued = await reconcile_current_consensus_deliveries(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                current,
                verify_discord_messages=False,
                retry_permanent_failures=False,
            )

        self.assertEqual(queued, 0)
        ensure_current.assert_not_called()

    async def test_periodic_watchdog_does_not_retry_closed_dm_discussion_invite(self) -> None:
        current = _session(stage="discussion")
        current.discussion_channel_id = 501
        current.discussion_type = "Правовая"
        current.discussion_allowed_user_ids = {2}
        current.participants[2].dm_failed = True
        bot = SimpleNamespace(add_view=lambda *args, **kwargs: None)
        guild = SimpleNamespace(id=77)

        with (
            patch(
                "modules.tvrs_recovery._outbox_storage.delivery_outbox_ensure_current"
            ) as ensure_current,
            patch(
                "modules.tvrs_recovery._enqueue_semantic_delivery",
                new=AsyncMock(return_value=False),
            ),
        ):
            queued = await reconcile_current_consensus_deliveries(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                current,
                verify_discord_messages=False,
                retry_permanent_failures=False,
            )

        self.assertEqual(queued, 0)
        ensure_current.assert_not_called()

    async def test_closed_dm_is_terminal_and_persists_server_fallback_state(self) -> None:
        current = _session()
        registry.add(current)
        job = build_control_dm_deliveries(current, phase="registration")[0]
        response = SimpleNamespace(status=403, reason="Forbidden")
        forbidden = discord.Forbidden(
            response,
            {"message": "Cannot send messages to this user", "code": 50007},
        )
        member = SimpleNamespace(
            dm_channel=None,
            create_dm=AsyncMock(side_effect=forbidden),
        )
        guild = SimpleNamespace(
            get_member=lambda user_id: member if user_id == 2 else None,
            fetch_member=AsyncMock(return_value=member),
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)
        message = OutboxMessage(
            id=1,
            topic=job["topic"],
            dedupe_key=job["dedupe_key"],
            payload=dict(job["payload"]),
            attempts=1,
            max_attempts=120,
            lease_token="lease",
            priority=job["priority"],
            supersede_key=job["supersede_key"],
        )

        with (
            patch(
                "modules.tvrs_control._outbox_storage.delivery_outbox_is_current_supersession",
                return_value=True,
            ),
            patch("modules.tvrs_control.queue_lines", return_value="Очередь"),
            patch(
                "modules.tvrs_control._consensus.save",
                return_value={"revision": 2},
            ) as save,
        ):
            with self.assertRaises(DeliveryPermanentFailure):
                await deliver_consensus_control_dm(message, bot)  # type: ignore[arg-type]

        self.assertTrue(current.participants[2].dm_failed)
        self.assertEqual(save.call_args.args[1], "registration_control_unavailable")
        self.assertEqual(save.call_args.kwargs["details"]["fallback"], "server_portal")

    async def test_invalid_form_body_is_classified_as_permanent(self) -> None:
        invalid_form = discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request", headers={}),
            {"message": "Invalid Form Body", "code": 50035},
        )

        with self.assertRaises(DeliveryPermanentFailure) as raised:
            _raise_classified_discord_error(invalid_form)

        self.assertIs(raised.exception.__cause__, invalid_form)

    async def test_invalid_panel_payload_does_not_mislabel_member_dm_as_closed(self) -> None:
        current = _session(stage="voting")
        current.participants[2].vote_message_id = 9002
        current.participants[2].vote_bill_id = 10
        registry.add(current)
        job = build_control_dm_deliveries(current, phase="voting")[0]
        invalid_form = discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request", headers={}),
            {"message": "Invalid Form Body", "code": 50035},
        )
        panel = SimpleNamespace(id=9002, edit=AsyncMock(side_effect=invalid_form))
        dm_channel = SimpleNamespace(id=500, fetch_message=AsyncMock(return_value=panel))
        member = SimpleNamespace(
            dm_channel=dm_channel,
            create_dm=AsyncMock(return_value=dm_channel),
            send=AsyncMock(),
        )
        guild = SimpleNamespace(
            get_member=lambda user_id: member if user_id == 2 else None,
            fetch_member=AsyncMock(return_value=member),
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)
        message = OutboxMessage(
            id=1,
            topic=job["topic"],
            dedupe_key=job["dedupe_key"],
            payload=dict(job["payload"]),
            attempts=1,
            max_attempts=120,
            lease_token="lease",
            priority=job["priority"],
            supersede_key=job["supersede_key"],
        )

        with (
            patch(
                "modules.tvrs_control._outbox_storage.delivery_outbox_is_current_supersession",
                return_value=True,
            ),
            patch(
                "modules.tvrs_control.build_dm_vote_embed",
                return_value=discord.Embed(title="Broken projection"),
            ),
            patch("modules.tvrs_control._consensus.save") as save,
        ):
            with self.assertRaises(DeliveryPermanentFailure):
                await deliver_consensus_control_dm(message, bot)  # type: ignore[arg-type]

        self.assertFalse(current.participants[2].dm_failed)
        save.assert_not_called()

    async def test_rate_limit_is_deferred_until_discord_retry_after(self) -> None:
        limited = discord.HTTPException(
            SimpleNamespace(
                status=429,
                reason="Too Many Requests",
                headers={"Retry-After": "3.5"},
            ),
            {"message": "You are being rate limited", "code": 0},
        )
        fixed_now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)

        with patch("modules.discord_delivery.datetime") as clock:
            clock.now.return_value = fixed_now
            with self.assertRaises(DeliveryDeferred) as raised:
                _raise_classified_discord_error(limited)

        self.assertEqual(raised.exception.reason, "discord_rate_limit")
        self.assertEqual(
            raised.exception.available_at,
            fixed_now + timedelta(seconds=3.5),
        )
        self.assertIs(raised.exception.__cause__, limited)

    async def test_server_error_remains_retryable(self) -> None:
        unavailable = discord.HTTPException(
            SimpleNamespace(status=503, reason="Service Unavailable", headers={}),
            {"message": "upstream unavailable", "code": 0},
        )

        with self.assertRaises(discord.HTTPException) as raised:
            _raise_classified_discord_error(unavailable)

        self.assertIs(raised.exception, unavailable)

    async def test_panel_deleted_between_fetch_and_edit_is_recreated(self) -> None:
        current = _session(stage="voting")
        current.participants[2].vote_message_id = 9002
        current.participants[2].vote_bill_id = 10
        registry.add(current)
        job = build_control_dm_deliveries(current, phase="voting")[0]
        not_found = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"),
            {"message": "Unknown Message", "code": 10008},
        )
        old_panel = SimpleNamespace(id=9002, edit=AsyncMock(side_effect=not_found))
        new_panel = SimpleNamespace(id=9010, edit=AsyncMock())
        dm_channel = SimpleNamespace(
            id=500,
            fetch_message=AsyncMock(return_value=old_panel),
        )
        member = SimpleNamespace(
            dm_channel=dm_channel,
            create_dm=AsyncMock(return_value=dm_channel),
            send=AsyncMock(return_value=new_panel),
        )
        guild = SimpleNamespace(
            get_member=lambda user_id: member if user_id == 2 else None,
            fetch_member=AsyncMock(return_value=member),
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)
        message = OutboxMessage(
            id=1,
            topic=job["topic"],
            dedupe_key=job["dedupe_key"],
            payload=dict(job["payload"]),
            attempts=1,
            max_attempts=120,
            lease_token="lease",
            priority=job["priority"],
            supersede_key=job["supersede_key"],
        )

        with (
            patch(
                "modules.tvrs_control._outbox_storage.delivery_outbox_is_current_supersession",
                return_value=True,
            ),
            patch(
                "modules.tvrs_control.build_dm_vote_embed",
                return_value=discord.Embed(title="Голосование"),
            ),
            patch(
                "modules.tvrs_control._consensus.save",
                return_value={"revision": 2},
            ),
            patch("modules.tvrs_control._schedule_control_cleanup") as cleanup,
        ):
            receipt = await deliver_consensus_control_dm(message, bot)  # type: ignore[arg-type]

        self.assertEqual(receipt.message_id, 9010)
        self.assertEqual(current.participants[2].vote_message_id, 9010)
        self.assertFalse(current.participants[2].dm_failed)
        member.send.assert_awaited_once()
        cleanup.assert_called_once_with(dm_channel, 9010)

    async def test_superseded_control_waiter_cannot_overwrite_new_generation(self) -> None:
        current = _session(stage="voting")
        current.participants[2].vote_message_id = 9002
        current.participants[2].vote_bill_id = 10
        registry.add(current)
        job = build_control_dm_deliveries(current, phase="voting")[0]
        first_lookup_started = asyncio.Event()
        release_first_lookup = asyncio.Event()
        calls = 0

        async def fetch_message(message_id: int):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_lookup_started.set()
                await release_first_lookup.wait()
            return panel

        panel = SimpleNamespace(id=9002, edit=AsyncMock(), delete=AsyncMock())
        dm_channel = SimpleNamespace(id=500, fetch_message=AsyncMock(side_effect=fetch_message))
        member = SimpleNamespace(
            dm_channel=dm_channel,
            create_dm=AsyncMock(return_value=dm_channel),
            send=AsyncMock(),
        )
        guild = SimpleNamespace(
            get_member=lambda user_id: member if user_id == 2 else None,
            fetch_member=AsyncMock(return_value=member),
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)
        old = OutboxMessage(
            id=1,
            topic=job["topic"],
            dedupe_key=f"{job['dedupe_key']}:old",
            payload={**dict(job["payload"]), "content": "old"},
            attempts=1,
            max_attempts=120,
            lease_token="old-lease",
            priority=job["priority"],
            supersede_key=job["supersede_key"],
        )
        new = OutboxMessage(
            id=2,
            topic=job["topic"],
            dedupe_key=f"{job['dedupe_key']}:new",
            payload={**dict(job["payload"]), "content": "new"},
            attempts=1,
            max_attempts=120,
            lease_token="new-lease",
            priority=job["priority"],
            supersede_key=job["supersede_key"],
        )
        current_owner = {1: True, 2: True}

        def is_current(item_id: int, **kwargs) -> bool:
            return current_owner[int(item_id)]

        with (
            patch(
                "modules.tvrs_control._outbox_storage.delivery_outbox_is_current_supersession",
                side_effect=is_current,
            ),
            patch(
                "modules.tvrs_control.build_dm_vote_embed",
                return_value=discord.Embed(title="Голосование"),
            ),
            patch("modules.tvrs_control._consensus.save", return_value={"revision": 2}),
        ):
            old_task = asyncio.create_task(deliver_consensus_control_dm(old, bot))  # type: ignore[arg-type]
            await first_lookup_started.wait()
            new_task = asyncio.create_task(deliver_consensus_control_dm(new, bot))  # type: ignore[arg-type]
            current_owner[1] = False
            release_first_lookup.set()
            await asyncio.gather(old_task, new_task)

        panel.edit.assert_awaited_once()
        self.assertEqual(panel.edit.await_args.kwargs["content"], "new")
        member.send.assert_not_awaited()

    async def test_old_result_rechecks_panel_ownership_after_discord_fetch(self) -> None:
        current = _session(stage="voting")
        current.stage = "after_result"
        current.current_bill = None
        current.participants[2].vote_message_id = 9002
        current.participants[2].vote_bill_id = 10
        registry.add(current)
        result = LiveResult(
            bill_id=10,
            bill_number=9,
            title="Предыдущий проект",
            status="accepted",
            internal_percent=100.0,
            overall_percent=51.0,
            internal_active=True,
            votes={1: "yes", 2: "yes"},
        )
        job = next(
            item
            for item in build_result_deliveries(
                current,
                result,
                participant_content="Голосование завершено.",
            )
            if item["payload"].get("destination_user_id") == 2
        )
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()
        old_panel = SimpleNamespace(id=9002, edit=AsyncMock())

        fetch_count = 0

        async def fetch_message(message_id: int):
            nonlocal fetch_count
            self.assertEqual(message_id, 9002)
            fetch_count += 1
            if fetch_count == 1:
                fetch_started.set()
                await release_fetch.wait()
            return old_panel

        dm_channel = SimpleNamespace(
            id=500,
            fetch_message=AsyncMock(side_effect=fetch_message),
        )
        new_result_message = SimpleNamespace(id=9010)
        member = SimpleNamespace(
            dm_channel=dm_channel,
            create_dm=AsyncMock(return_value=dm_channel),
            send=AsyncMock(return_value=new_result_message),
        )
        guild = SimpleNamespace(
            get_member=lambda user_id: member if user_id == 2 else None,
            fetch_member=AsyncMock(return_value=member),
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 77 else None)
        message = OutboxMessage(
            id=8,
            topic=job["topic"],
            dedupe_key=job["dedupe_key"],
            payload=dict(job["payload"]),
            attempts=1,
            max_attempts=12,
            lease_token="result-lease",
            priority=0,
            supersede_key=None,
        )

        with (
            patch(
                "modules.tvrs_delivery.storage.tvrs_live_result_for_bill",
                return_value={"id": 1},
            ),
            patch(
                "modules.tvrs_control._outbox_storage.delivery_outbox_is_current_supersession",
                return_value=True,
            ),
            patch(
                "modules.tvrs_control.build_dm_vote_embed",
                return_value=discord.Embed(title="Следующее голосование"),
            ),
            patch("modules.tvrs_control._consensus.save", return_value={"revision": 2}),
        ):
            delivery = asyncio.create_task(make_result_delivery_handler(bot)(message))
            await fetch_started.wait()
            # The next bill becomes durable while Discord is still resolving
            # the message that carried the previous bill's buttons.
            current.stage = "voting"
            current.current_bill = {
                "id": 11,
                "bill_number": 10,
                "title": "Следующий проект",
            }
            control_job = build_control_dm_deliveries(current, phase="voting")[0]
            control_message = OutboxMessage(
                id=9,
                topic=control_job["topic"],
                dedupe_key=control_job["dedupe_key"],
                payload=dict(control_job["payload"]),
                attempts=1,
                max_attempts=120,
                lease_token="control-lease",
                priority=control_job["priority"],
                supersede_key=control_job["supersede_key"],
            )
            control = asyncio.create_task(
                deliver_consensus_control_dm(control_message, bot)  # type: ignore[arg-type]
            )
            await asyncio.sleep(0)
            # Result and control use the same per-user projection lock. The
            # newer control cannot interleave while the result owns fetch/edit.
            self.assertEqual(dm_channel.fetch_message.await_count, 1)
            release_fetch.set()
            receipt = await delivery
            control_receipt = await control

        self.assertEqual(receipt.message_id, 9010)
        self.assertEqual(control_receipt.message_id, 9002)
        old_panel.edit.assert_awaited_once()
        self.assertIsNotNone(old_panel.edit.await_args.kwargs["view"])
        member.send.assert_awaited_once()
        self.assertEqual(current.participants[2].vote_bill_id, 11)

    async def test_notice_finishes_when_private_control_is_permanently_unavailable(self) -> None:
        current = _session(stage="voting")
        current.participants[2].dm_failed = True
        registry.add(current)
        job = build_control_notice_deliveries(current, bill_id=10)[0]
        message = OutboxMessage(
            id=4,
            topic=job["topic"],
            dedupe_key=job["dedupe_key"],
            payload=dict(job["payload"]),
            attempts=1,
            max_attempts=60,
            lease_token="notice-lease",
            priority=job["priority"],
            supersede_key=job["supersede_key"],
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: None)

        with patch(
            "modules.tvrs_control._outbox_storage.delivery_outbox_latest_supersession",
            return_value={"status": "dead"},
        ):
            receipt = await deliver_consensus_control_notice(message, bot)  # type: ignore[arg-type]

        self.assertIsNone(receipt.message_id)

    async def test_notice_finishes_for_terminal_control_without_dm_failure_flag(self) -> None:
        current = _session(stage="voting")
        self.assertFalse(current.participants[2].dm_failed)
        registry.add(current)
        job = build_control_notice_deliveries(current, bill_id=10)[0]
        message = OutboxMessage(
            id=5,
            topic=job["topic"],
            dedupe_key=job["dedupe_key"],
            payload=dict(job["payload"]),
            attempts=1,
            max_attempts=60,
            lease_token="terminal-notice-lease",
            priority=job["priority"],
            supersede_key=job["supersede_key"],
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: None)

        for terminal_status in ("dead", "cancelled"):
            with self.subTest(status=terminal_status), patch(
                "modules.tvrs_control._outbox_storage.delivery_outbox_latest_supersession",
                return_value={"status": terminal_status},
            ):
                receipt = await deliver_consensus_control_notice(message, bot)  # type: ignore[arg-type]
                self.assertIsNone(receipt.message_id)

    async def test_notice_keeps_waiting_while_control_delivery_is_retryable(self) -> None:
        current = _session(stage="voting")
        registry.add(current)
        job = build_control_notice_deliveries(current, bill_id=10)[0]
        message = OutboxMessage(
            id=6,
            topic=job["topic"],
            dedupe_key=job["dedupe_key"],
            payload=dict(job["payload"]),
            attempts=1,
            max_attempts=60,
            lease_token="pending-notice-lease",
            priority=job["priority"],
            supersede_key=job["supersede_key"],
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: None)

        with patch(
            "modules.tvrs_control._outbox_storage.delivery_outbox_latest_supersession",
            return_value={"status": "retry"},
        ), self.assertRaises(DeliveryDeferred) as raised:
            await deliver_consensus_control_notice(message, bot)  # type: ignore[arg-type]

        self.assertEqual(raised.exception.reason, "control_panel_pending")

    async def test_liveness_finalizes_a_fully_voted_session_after_callback_loss(self) -> None:
        current = _session(stage="voting")
        current.votes = {1: "yes", 2: "yes"}
        bot = SimpleNamespace()
        guild = SimpleNamespace(id=77)

        with patch(
            "modules.tvrs_recovery.finalize_current_vote",
            new=AsyncMock(),
        ) as finalize:
            changed = await reconcile_consensus_liveness(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                current,
            )

        self.assertTrue(changed)
        finalize.assert_awaited_once_with(
            bot,
            guild,
            current,
            forced=False,
            expected_bill_id=10,
        )

    async def test_liveness_recreates_a_lost_future_timer_task(self) -> None:
        current = _session(stage="voting")
        current.timer_seconds = 120
        current.timer_deadline = datetime.now(timezone.utc) + timedelta(seconds=120)
        current.timer_task = None
        bot = SimpleNamespace()
        guild = SimpleNamespace(id=77)

        with patch("modules.tvrs_recovery.schedule_vote_timer_task") as schedule:
            changed = await reconcile_consensus_liveness(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                current,
            )

        self.assertTrue(changed)
        schedule.assert_called_once()
        self.assertEqual(schedule.call_args.kwargs["bill_id"], 10)
        self.assertGreater(schedule.call_args.args[3], 0)

    async def test_liveness_routes_oral_finalization_through_durable_recovery(self) -> None:
        current = _session(stage="finalizing")
        current.revision = 1
        current.pending_action = {
            "kind": "oral",
            "bill_id": 10,
            "actor_id": 1,
            "actor_display": "Ведущий",
            "oral_status": "accepted",
            "oral_note": "Решение заседания",
        }
        bot = SimpleNamespace()
        guild = SimpleNamespace(id=77)

        with patch(
            "modules.tvrs_recovery.retry_pending_finalization_once",
            new=AsyncMock(return_value=True),
        ) as retry:
            changed = await reconcile_consensus_liveness(
                bot,  # type: ignore[arg-type]
                guild,  # type: ignore[arg-type]
                current,
            )

        self.assertTrue(changed)
        retry.assert_awaited_once_with(bot, guild, current)

    async def test_liveness_blocks_ambiguous_finalization(self) -> None:
        current = _session(stage="finalizing")
        current.pending_action = {"kind": "unknown", "bill_id": 10}

        with patch(
            "modules.tvrs_recovery.retry_pending_finalization_once",
            new=AsyncMock(),
        ) as retry:
            changed = await reconcile_consensus_liveness(
                SimpleNamespace(),  # type: ignore[arg-type]
                SimpleNamespace(id=77),  # type: ignore[arg-type]
                current,
            )

        self.assertFalse(changed)
        retry.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
