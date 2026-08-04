import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import storage

from modules.consensus_core import (
    ConsensusRules,
    LiveConsensusSession,
    LiveParticipant,
    LiveResult,
    session_from_snapshot,
    session_to_snapshot,
)
from modules.consensus_runtime import registry
from modules.consensus_v3 import (
    CONSENSUS_ENGINE_VERSION,
    LEGACY_CONSENSUS_ENGINE_VERSION,
    consensus_progress_text,
    evaluate_consensus_preflight,
    resolve_consensus_access,
)
from modules.tvrs_consensus_portal import (
    TVRSConsensusEntryView,
    TVRSObserverView,
    TVRSParticipantPortalView,
    build_observer_embed,
    build_participant_portal_embed,
    ensure_public_consensus_card,
)
from modules.tvrs_consensus_views import (
    TVRSAfterResultView,
    TVRSHostVoteView,
    TVRSRegistrationView,
)
from modules.tvrs_consensus_admin import TVRSConsensusAdminView, build_consensus_admin_embed
from modules.tvrs_hub_views import TVRSMainPanelView
from modules.tvrs_presentation import participant_kind


def participant(user_id: int, kind: str, *, confirmed: bool = True) -> LiveParticipant:
    return LiveParticipant(
        user_id=user_id,
        display_name=f"Участник {user_id}",
        mention=f"<@{user_id}>",
        kind=kind,  # type: ignore[arg-type]
        confirmed=confirmed,
    )


def session() -> LiveConsensusSession:
    return LiveConsensusSession(
        session_key="77:v3:test",
        guild_id=77,
        channel_id=100,
        leader_id=1,
        leader_display="Ведущий",
        plenary_number=4,
        participants={
            1: participant(1, "chair"),
            2: participant(2, "chair"),
            3: participant(3, "senator"),
        },
    )


class ConsensusV3DomainTests(unittest.TestCase):
    def test_entry_has_exactly_one_contextual_primary_action(self) -> None:
        self.assertEqual(
            resolve_consensus_access(None, user_id=1, has_chair_access=True).primary_action,
            "prepare",
        )
        self.assertEqual(
            resolve_consensus_access(None, user_id=9, has_chair_access=False).primary_action,
            "unavailable",
        )
        current = session()
        self.assertEqual(
            resolve_consensus_access(current, user_id=1, has_chair_access=True).primary_action,
            "manage",
        )
        self.assertEqual(
            resolve_consensus_access(current, user_id=3, has_chair_access=False).primary_action,
            "participate",
        )
        self.assertEqual(
            resolve_consensus_access(current, user_id=99, has_chair_access=False).primary_action,
            "observe",
        )

    def test_preflight_requires_queue_quorum_and_leader_presence(self) -> None:
        rules = ConsensusRules()
        ready = evaluate_consensus_preflight(
            [participant(1, "chair"), participant(2, "chair"), participant(3, "senator")],
            queue_count=2,
            leader_id=1,
            rules=rules,
        )
        self.assertTrue(ready.can_open_registration)

        blocked = evaluate_consensus_preflight(
            [participant(2, "chair"), participant(4, "senator")],
            queue_count=0,
            leader_id=1,
            rules=rules,
            voice_error="Минимальный кворум не набран.",
        )
        self.assertFalse(blocked.can_open_registration)
        self.assertTrue(any("очереди" in item for item in blocked.blockers))
        self.assertTrue(any("ведущий" in item.lower() for item in blocked.blockers))

    def test_snapshot_freezes_engine_version_and_legacy_defaults_to_v2(self) -> None:
        current = session()
        snapshot = session_to_snapshot(current)
        self.assertEqual(snapshot["engine_version"], CONSENSUS_ENGINE_VERSION)
        self.assertEqual(
            session_from_snapshot(snapshot).engine_version,
            CONSENSUS_ENGINE_VERSION,
        )
        snapshot.pop("engine_version")
        self.assertEqual(
            session_from_snapshot(snapshot).engine_version,
            LEGACY_CONSENSUS_ENGINE_VERSION,
        )

    def test_progress_uses_stable_user_facing_lifecycle(self) -> None:
        self.assertIn("**1. Регистрация**", consensus_progress_text("registration"))
        self.assertIn("**2. Рассмотрение**", consensus_progress_text("discussion"))
        self.assertIn("**3. Результат**", consensus_progress_text("after_result"))


class ConsensusV3UiTests(unittest.TestCase):
    def tearDown(self) -> None:
        registry.sessions.clear()
        registry._locks.clear()

    def test_administrator_can_lead_without_automatic_voting_block(self) -> None:
        member = SimpleNamespace(
            id=99,
            guild_permissions=SimpleNamespace(administrator=True),
            roles=[],
        )

        self.assertEqual(participant_kind(member), "chair")

    def test_center_never_shows_old_conflicting_buttons(self) -> None:
        async def inspect() -> None:
            idle = TVRSMainPanelView(
                1,
                guild_id=77,
                has_chair_access=True,
                back_to_hub=True,
            )
            labels = {str(item.label) for item in idle.children}
            self.assertIn("Подготовить заседание", labels)
            self.assertNotIn("Открыть заседание", labels)
            self.assertNotIn("Начать консенсус", labels)

            current = session()
            registry.add(current)
            leader = TVRSMainPanelView(1, guild_id=77, has_chair_access=True)
            participant_view = TVRSMainPanelView(3, guild_id=77, has_chair_access=False)
            outsider = TVRSMainPanelView(99, guild_id=77, has_chair_access=False)
            recovery_chair = TVRSMainPanelView(2, guild_id=77, has_chair_access=True)
            self.assertIn("Управлять заседанием", {str(item.label) for item in leader.children})
            self.assertIn("Открыть личный пульт", {str(item.label) for item in participant_view.children})
            self.assertIn("Наблюдать за заседанием", {str(item.label) for item in outsider.children})
            self.assertIn("Восстановление", {str(item.label) for item in recovery_chair.children})
            self.assertNotIn("Восстановление", {str(item.label) for item in outsider.children})

            recovery = TVRSConsensusAdminView(2, current.session_key)
            recovery_labels = {str(item.label) for item in recovery.children}
            self.assertIn("Принять ведение", recovery_labels)
            self.assertIn("Восстановить", recovery_labels)
            self.assertIn("Завершить дискуссию", recovery_labels)
            self.assertIn("Безопасно закрыть", recovery_labels)
            fields = {field.name: field.value for field in build_consensus_admin_embed(current).fields}
            self.assertIn("Диагностика", fields)
            self.assertIn("План восстановления", fields)

        asyncio.run(inspect())

    def test_personal_portal_reuses_live_vote_generation(self) -> None:
        async def inspect() -> None:
            current = session()
            current.stage = "voting"
            current.current_bill = {
                "id": 10,
                "bill_number": 9,
                "title": "V3",
                "summary": "Проверка личного пульта",
            }
            registry.add(current)
            with (
                patch(
                    "modules.tvrs_consensus_portal.queue_short_lines",
                    return_value="Очередь пуста.",
                ),
                patch(
                    "modules.tvrs_presentation.queue_short_lines",
                    return_value="Очередь пуста.",
                ),
            ):
                view = TVRSParticipantPortalView(current.session_key, 3)
                embed = build_participant_portal_embed(current, 3)
            labels = {str(item.label) for item in view.children}
            self.assertTrue(
                {
                    "За",
                    "Против",
                    "Воздержаться",
                    "Дискуссия",
                    "Обновить",
                    "Обзор",
                    "Веб-бюллетень",
                }
                <= labels
            )
            self.assertIn("Личный пульт V3", embed.footer.text)
            self.assertLessEqual(len(view.children), 25)

            observer = build_observer_embed(current)
            self.assertIn("режим наблюдения", observer.footer.text)
            self.assertNotIn("Участник 3", str(observer.to_dict()))
            self.assertNotIn("Внутренний консенсус", str(observer.to_dict()))

            current.stage = "paused"
            current.paused_reason = "Кворум временно утрачен."
            paused_observer = build_observer_embed(current)
            self.assertIn("заседание приостановлено", str(paused_observer.to_dict()))
            self.assertIn("Кворум временно утрачен", str(paused_observer.to_dict()))

        asyncio.run(inspect())

    def test_public_entry_routes_every_request_to_a_fresh_private_panel(self) -> None:
        async def inspect() -> None:
            current = session()
            current.participants[2].confirmed = False
            registry.add(current)
            entry = TVRSConsensusEntryView()
            self.assertIsNone(entry.timeout)
            self.assertEqual(len(entry.children), 1)
            self.assertEqual(entry.children[0].custom_id, "tvrs_consensus_entry:v1")

            def interaction_for(user_id: int, *, guild=True):
                return SimpleNamespace(
                    guild=(SimpleNamespace(id=current.guild_id) if guild else None),
                    user=SimpleNamespace(id=user_id),
                    response=SimpleNamespace(send_message=AsyncMock()),
                )

            with (
                patch(
                    "modules.tvrs_consensus_portal.queue_short_lines",
                    return_value="Очередь пуста.",
                ),
                patch(
                    "modules.tvrs_presentation.queue_short_lines",
                    return_value="Очередь пуста.",
                ),
            ):
                leader_registration = interaction_for(1)
                await entry.open_personal_panel(leader_registration)
                leader_kwargs = leader_registration.response.send_message.await_args.kwargs
                self.assertTrue(leader_kwargs["ephemeral"])
                self.assertIsInstance(leader_kwargs["view"], TVRSRegistrationView)

                registration_participant = interaction_for(2)
                await entry.open_personal_panel(registration_participant)
                registration_kwargs = (
                    registration_participant.response.send_message.await_args.kwargs
                )
                self.assertIsInstance(
                    registration_kwargs["view"],
                    TVRSParticipantPortalView,
                )

                current.stage = "voting"
                current.current_bill = {
                    "id": 10,
                    "bill_number": 9,
                    "title": "Надёжный личный пульт",
                    "summary": "Проверка маршрутизации",
                }

                leader_voting = interaction_for(1)
                await entry.open_personal_panel(leader_voting)
                voting_kwargs = leader_voting.response.send_message.await_args.kwargs
                self.assertIsInstance(voting_kwargs["view"], TVRSHostVoteView)

                confirmed_participant = interaction_for(3)
                await entry.open_personal_panel(confirmed_participant)
                confirmed_kwargs = (
                    confirmed_participant.response.send_message.await_args.kwargs
                )
                self.assertIsInstance(
                    confirmed_kwargs["view"],
                    TVRSParticipantPortalView,
                )

                unconfirmed_participant = interaction_for(2)
                await entry.open_personal_panel(unconfirmed_participant)
                unconfirmed_kwargs = (
                    unconfirmed_participant.response.send_message.await_args.kwargs
                )
                self.assertIsInstance(unconfirmed_kwargs["view"], TVRSObserverView)
                self.assertIn("не подтвердили", unconfirmed_kwargs["content"])

                outsider = interaction_for(99)
                await entry.open_personal_panel(outsider)
                outsider_kwargs = outsider.response.send_message.await_args.kwargs
                self.assertIsInstance(outsider_kwargs["view"], TVRSObserverView)
                self.assertIn("не входите", outsider_kwargs["content"])

                current.stage = "after_result"
                current.results.append(
                    LiveResult(
                        bill_id=10,
                        bill_number=9,
                        title="Надёжный личный пульт",
                        status="accepted",
                        internal_percent=100.0,
                        overall_percent=100.0,
                        internal_active=True,
                        votes={1: "yes", 3: "yes"},
                    )
                )
                leader_result = interaction_for(1)
                await entry.open_personal_panel(leader_result)
                result_kwargs = leader_result.response.send_message.await_args.kwargs
                self.assertIsInstance(result_kwargs["view"], TVRSAfterResultView)

            direct_message = interaction_for(1, guild=False)
            await entry.open_personal_panel(direct_message)
            dm_kwargs = direct_message.response.send_message.await_args.kwargs
            self.assertTrue(dm_kwargs["ephemeral"])
            self.assertIn("только на сервере", dm_kwargs["content"])

        asyncio.run(inspect())

    def test_concurrent_public_updates_do_not_duplicate_the_card(self) -> None:
        async def inspect() -> None:
            current = session()
            metadata: dict[str, str] = {}
            messages: dict[int, object] = {}

            async def send(**_kwargs):
                message = SimpleNamespace(id=501, edit=AsyncMock())
                messages[501] = message
                return message

            async def fetch_message(message_id: int):
                return messages[message_id]

            channel = SimpleNamespace(
                fetch_message=AsyncMock(side_effect=fetch_message),
                send=AsyncMock(side_effect=send),
            )
            guild = SimpleNamespace(
                id=current.guild_id,
                get_channel=lambda _channel_id: channel,
            )
            bot = SimpleNamespace(get_channel=lambda _channel_id: None)
            with (
                patch(
                    "modules.tvrs_consensus_portal._activity_storage.get_meta",
                    side_effect=metadata.get,
                ),
                patch(
                    "modules.tvrs_consensus_portal._activity_storage.set_meta_value",
                    side_effect=metadata.__setitem__,
                ),
            ):
                first, second = await asyncio.gather(
                    ensure_public_consensus_card(
                        bot,
                        guild,  # type: ignore[arg-type]
                        current,
                    ),
                    ensure_public_consensus_card(
                        bot,
                        guild,  # type: ignore[arg-type]
                        current,
                    ),
                )

            self.assertEqual(first.id, second.id)
            channel.send.assert_awaited_once()
            channel.fetch_message.assert_awaited_once_with(501)

        asyncio.run(inspect())

    def test_public_card_reuses_one_canonical_message(self) -> None:
        async def inspect() -> None:
            current = session()
            message = SimpleNamespace(id=500, edit=AsyncMock())
            channel = SimpleNamespace(
                fetch_message=AsyncMock(return_value=message),
                send=AsyncMock(),
            )
            guild = SimpleNamespace(
                id=current.guild_id,
                get_channel=lambda channel_id: (
                    channel if channel_id == current.channel_id else None
                ),
            )
            bot = SimpleNamespace(get_channel=lambda _channel_id: None)
            with (
                patch(
                    "modules.tvrs_consensus_portal._activity_storage.get_meta",
                    return_value="500",
                ),
                patch(
                    "modules.tvrs_consensus_portal._activity_storage.set_meta_value"
                ) as store_message_id,
            ):
                result = await ensure_public_consensus_card(
                    bot,
                    guild,  # type: ignore[arg-type]
                    current,
                )

            self.assertIs(result, message)
            message.edit.assert_awaited_once()
            channel.send.assert_not_awaited()
            edited_embed = message.edit.await_args.kwargs["embed"]
            self.assertIsInstance(
                message.edit.await_args.kwargs["view"],
                TVRSConsensusEntryView,
            )
            self.assertIn(current.session_key, edited_embed.footer.text)
            store_message_id.assert_called_once_with(
                f"tvrs_consensus_public_status_message_id:{current.guild_id}",
                "500",
            )

        asyncio.run(inspect())

    def test_terminal_public_card_removes_the_entry_button(self) -> None:
        async def inspect() -> None:
            current = session()
            current.finished = True
            message = SimpleNamespace(id=502, edit=AsyncMock())
            channel = SimpleNamespace(
                fetch_message=AsyncMock(return_value=message),
                send=AsyncMock(),
            )
            guild = SimpleNamespace(
                id=current.guild_id,
                get_channel=lambda _channel_id: channel,
            )
            bot = SimpleNamespace(get_channel=lambda _channel_id: None)
            with (
                patch(
                    "modules.tvrs_consensus_portal._activity_storage.get_meta",
                    return_value="502",
                ),
                patch(
                    "modules.tvrs_consensus_portal._activity_storage.set_meta_value",
                ),
            ):
                await ensure_public_consensus_card(
                    bot,
                    guild,  # type: ignore[arg-type]
                    current,
                    terminal=True,
                )

            self.assertIsNone(message.edit.await_args.kwargs["view"])

        asyncio.run(inspect())

    def test_replaced_session_fences_delayed_terminal_public_write(self) -> None:
        async def inspect() -> None:
            old = session()
            old.stage = "finished"
            old.finished = True
            registry.add(old)
            fetch_started = asyncio.Event()
            release_fetch = asyncio.Event()
            message = SimpleNamespace(id=503, edit=AsyncMock())

            async def fetch_message(message_id: int):
                fetch_started.set()
                await release_fetch.wait()
                return message

            channel = SimpleNamespace(
                fetch_message=AsyncMock(side_effect=fetch_message),
                send=AsyncMock(),
            )
            guild = SimpleNamespace(
                id=old.guild_id,
                get_channel=lambda _channel_id: channel,
            )
            bot = SimpleNamespace(get_channel=lambda _channel_id: None)
            with (
                patch(
                    "modules.tvrs_consensus_portal._activity_storage.get_meta",
                    return_value="503",
                ),
                patch(
                    "modules.tvrs_consensus_portal._activity_storage.set_meta_value",
                ) as store_message_id,
            ):
                delayed = asyncio.create_task(
                    ensure_public_consensus_card(
                        bot,
                        guild,  # type: ignore[arg-type]
                        old,
                        terminal=True,
                    )
                )
                await fetch_started.wait()
                current = session()
                current.session_key = "77:v3:replacement"
                registry.add(current)
                release_fetch.set()
                result = await delayed

            self.assertIsNone(result)
            message.edit.assert_not_awaited()
            channel.send.assert_not_awaited()
            store_message_id.assert_not_called()

        asyncio.run(inspect())


class ConsensusV3MigrationTests(unittest.TestCase):
    def test_existing_session_table_gets_legacy_engine_default(self) -> None:
        old_data_dir = storage.DATA_DIR
        old_database_file = storage.DATABASE_FILE
        old_activity_file = storage.LEGACY_ACTIVITY_FILE
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            try:
                storage.DATA_DIR = Path(directory)
                storage.DATABASE_FILE = storage.DATA_DIR / "v3-migration.db"
                storage.LEGACY_ACTIVITY_FILE = storage.DATA_DIR / "activity.json"
                with sqlite3.connect(storage.DATABASE_FILE) as con:
                    con.executescript(
                        """
                        CREATE TABLE meta (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );
                        INSERT INTO meta(key, value, updated_at)
                        VALUES(
                            'migration:consensus-reset:2026-07-16-clean-consensus-v2',
                            '{}',
                            '2026-07-18T00:00:00+00:00'
                        );
                        CREATE TABLE tvrs_bills (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            guild_id INTEGER NOT NULL,
                            bill_number INTEGER NOT NULL,
                            channel_id INTEGER,
                            message_id INTEGER,
                            author_id INTEGER NOT NULL,
                            author_display TEXT,
                            title TEXT NOT NULL,
                            summary TEXT NOT NULL,
                            materials TEXT,
                            status TEXT NOT NULL DEFAULT 'draft',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE(guild_id, bill_number)
                        );
                        INSERT INTO tvrs_bills(
                            guild_id, bill_number, channel_id, author_id,
                            author_display, title, summary, status,
                            created_at, updated_at
                        ) VALUES(
                            77, 14, 100, 1, 'Автор', 'Сохранить меня',
                            'Существующий проект', 'draft',
                            '2026-07-18T00:00:00+00:00',
                            '2026-07-18T00:00:00+00:00'
                        );
                        """
                    )
                    con.execute(
                        """
                        CREATE TABLE tvrs_consensus_sessions (
                            session_key TEXT PRIMARY KEY,
                            guild_id INTEGER NOT NULL,
                            plenary_number INTEGER NOT NULL,
                            stage TEXT NOT NULL,
                            leader_id INTEGER NOT NULL,
                            current_bill_id INTEGER,
                            snapshot_json TEXT NOT NULL,
                            revision INTEGER NOT NULL DEFAULT 1,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            finished_at TEXT
                        )
                        """
                    )
                    con.execute(
                        """
                        INSERT INTO tvrs_consensus_sessions(
                            session_key, guild_id, plenary_number, stage,
                            leader_id, snapshot_json, revision,
                            created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "77:legacy:test",
                            77,
                            3,
                            "registration",
                            1,
                            "{}",
                            1,
                            "2026-07-18T00:00:00+00:00",
                            "2026-07-18T00:00:00+00:00",
                        ),
                    )
                    con.commit()
                storage.init_db()
                with sqlite3.connect(storage.DATABASE_FILE) as con:
                    columns = {
                        str(row[1]): str(row[4])
                        for row in con.execute("PRAGMA table_info(tvrs_consensus_sessions)")
                    }
                    session_row = con.execute(
                        "SELECT engine_version FROM tvrs_consensus_sessions WHERE session_key = ?",
                        ("77:legacy:test",),
                    ).fetchone()
                    bill_row = con.execute(
                        "SELECT title, status FROM tvrs_bills WHERE guild_id = 77 AND bill_number = 14"
                    ).fetchone()
                self.assertIn("engine_version", columns)
                self.assertEqual(columns["engine_version"], "2")
                self.assertEqual(session_row, (2,))
                self.assertEqual(bill_row, ("Сохранить меня", "draft"))
            finally:
                storage.DATA_DIR = old_data_dir
                storage.DATABASE_FILE = old_database_file
                storage.LEGACY_ACTIVITY_FILE = old_activity_file


if __name__ == "__main__":
    unittest.main()
