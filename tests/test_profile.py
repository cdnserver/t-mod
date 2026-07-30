import asyncio
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord
from discord.ext import commands

import storage
from modules.profile import (
    CharacterDetailView,
    CharacterModal,
    ProfileHomeView,
    ProfileNotificationsView,
    ProfilePrivacyView,
    ProfileQuietHoursView,
    ProfileSettingsView,
    ProfileStatusView,
    profile_embed,
    profile_settings_embed,
    parse_profile_clock,
    setup_profile,
)
from modules.profile_notifications import evaluate_profile_notification


class ProfileStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "profile-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    def test_character_limit_is_transactional_and_static_is_unique_per_guild(self) -> None:
        first = storage.add_profile_character(10, 100, "Robert Williams", "00123")
        storage.add_profile_character(10, 100, "John Doe", "456")
        storage.add_profile_character(10, 100, "Jane Doe", "789")

        self.assertEqual(first.static_id, "123")
        with self.assertRaisesRegex(ValueError, "profile_character_limit"):
            storage.add_profile_character(10, 100, "Fourth Hero", "999")
        with self.assertRaisesRegex(ValueError, "profile_static_taken"):
            storage.add_profile_character(10, 200, "Static Thief", "123")

        other_guild = storage.add_profile_character(11, 200, "Other Server", "00123")
        self.assertEqual(other_guild.static_id, "123")

    def test_update_delete_and_position_compaction(self) -> None:
        first = storage.add_profile_character(10, 100, "First Hero", "100")
        second = storage.add_profile_character(10, 100, "Second Hero", "200")
        third = storage.add_profile_character(10, 100, "Third Hero", "300")

        updated = storage.update_profile_character(10, 100, second.id, "Renamed Hero", "250")
        self.assertEqual((updated.nickname, updated.static_id), ("Renamed Hero", "250"))
        storage.delete_profile_character(10, 100, first.id)

        remaining = storage.list_profile_characters(10, 100)
        self.assertEqual([item.id for item in remaining], [second.id, third.id])
        self.assertEqual([item.position for item in remaining], [1, 2])
        with self.assertRaisesRegex(ValueError, "profile_character_not_found"):
            storage.update_profile_character(10, 200, second.id, "Wrong Owner", "777")

    def test_each_character_has_independent_public_visibility(self) -> None:
        first = storage.add_profile_character(10, 100, "First Hero", "100")
        second = storage.add_profile_character(10, 100, "Second Hero", "200")
        third = storage.add_profile_character(10, 100, "Private Hero", "300")

        hidden = storage.set_profile_character_visibility(
            10,
            100,
            third.id,
            is_public=False,
        )
        self.assertFalse(hidden.is_public)
        characters = storage.list_profile_characters(10, 100)
        self.assertEqual(
            [character.id for character in characters if character.is_public],
            [first.id, second.id],
        )
        with self.assertRaisesRegex(ValueError, "profile_character_not_found"):
            storage.set_profile_character_visibility(10, 200, third.id, is_public=True)

    def test_status_and_note_are_normalized(self) -> None:
        profile = storage.set_member_profile_status(
            10,
            100,
            "busy",
            note="  вернусь   вечером  ",
        )
        self.assertEqual(profile.status, "busy")
        self.assertEqual(profile.status_note, "вернусь вечером")
        with self.assertRaisesRegex(ValueError, "profile_status_invalid"):
            storage.set_member_profile_status(10, 100, "invisible")
        with self.assertRaisesRegex(ValueError, "profile_static_invalid"):
            storage.add_profile_character(10, 100, "Test Hero", "RU-15")

    def test_personal_preferences_and_primary_character_lifecycle(self) -> None:
        first = storage.add_profile_character(10, 100, "First Hero", "100")
        second = storage.add_profile_character(10, 100, "Second Hero", "200")
        initial = storage.get_member_profile(10, 100)
        self.assertEqual(initial.primary_character_id, first.id)
        self.assertEqual(initial.visibility, "members")
        self.assertTrue(initial.show_activity)
        self.assertEqual(initial.theme, "indigo")
        self.assertTrue(initial.dm_notifications)
        self.assertFalse(initial.quiet_hours_enabled)

        configured = storage.update_member_profile_preferences(
            10,
            100,
            visibility="private",
            show_activity=False,
            theme="rose",
            primary_character_id=second.id,
        )
        self.assertEqual(configured.visibility, "private")
        self.assertFalse(configured.show_activity)
        self.assertEqual(configured.theme, "rose")
        self.assertEqual(configured.primary_character_id, second.id)

        foreign = storage.add_profile_character(10, 200, "Foreign Hero", "300")
        with self.assertRaisesRegex(ValueError, "profile_primary_character_invalid"):
            storage.update_member_profile_preferences(
                10,
                100,
                primary_character_id=foreign.id,
            )
        storage.delete_profile_character(10, 100, second.id)
        self.assertEqual(storage.get_member_profile(10, 100).primary_character_id, first.id)

    def test_concurrent_additions_never_exceed_three_characters(self) -> None:
        def add(index: int) -> str:
            try:
                storage.add_profile_character(10, 100, f"Hero {index}", str(1000 + index))
            except ValueError as exc:
                return str(exc)
            return "saved"

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(add, range(6)))

        self.assertEqual(results.count("saved"), 3)
        self.assertEqual(results.count("profile_character_limit"), 3)
        self.assertEqual(len(storage.list_profile_characters(10, 100)), 3)

    def test_existing_profile_table_receives_preference_columns_without_data_loss(self) -> None:
        current_database = storage.DATABASE_FILE
        legacy_database = storage.DATA_DIR / "legacy-profile.db"
        try:
            storage.DATABASE_FILE = legacy_database
            with sqlite3.connect(legacy_database) as con:
                con.execute(
                    """
                    CREATE TABLE member_profiles (
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        status_note TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (guild_id, user_id)
                    )
                    """
                )
                con.execute(
                    """
                    INSERT INTO member_profiles(
                        guild_id, user_id, status, status_note, created_at, updated_at
                    ) VALUES(10, 100, 'busy', 'Вернусь вечером', 'old', 'old')
                    """
                )
                con.execute(
                    """
                    CREATE TABLE profile_characters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        nickname TEXT NOT NULL,
                        static_id TEXT NOT NULL,
                        position INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (guild_id, static_id),
                        UNIQUE (guild_id, user_id, position)
                    )
                    """
                )
                con.execute(
                    """
                    INSERT INTO profile_characters(
                        guild_id, user_id, nickname, static_id, position, created_at, updated_at
                    ) VALUES(10, 100, 'Legacy Hero', '777', 1, 'old', 'old')
                    """
                )
                con.commit()

            storage.init_db()
            migrated = storage.get_member_profile(10, 100)
            self.assertEqual(migrated.status, "busy")
            self.assertEqual(migrated.status_note, "Вернусь вечером")
            self.assertEqual(migrated.visibility, "members")
            self.assertTrue(migrated.show_activity)
            self.assertEqual(migrated.theme, "indigo")
            self.assertIsNone(migrated.primary_character_id)
            self.assertTrue(migrated.show_availability)
            self.assertTrue(migrated.show_position)
            self.assertTrue(migrated.show_characters)
            self.assertTrue(migrated.show_join_date)
            self.assertTrue(migrated.dm_notifications)
            self.assertTrue(migrated.dm_market)
            self.assertFalse(migrated.quiet_hours_enabled)
            self.assertEqual(migrated.quiet_end_minute, 480)
            legacy_character = storage.list_profile_characters(10, 100)[0]
            self.assertEqual(legacy_character.nickname, "Legacy Hero")
            self.assertTrue(legacy_character.is_public)
        finally:
            storage.DATABASE_FILE = current_database

    def test_dm_policy_honors_module_switch_and_cross_midnight_quiet_hours(self) -> None:
        storage.update_member_profile_preferences(10, 100, dm_market=False)
        disabled = evaluate_profile_notification(10, 100, "market")
        self.assertFalse(disabled.allowed)
        self.assertEqual(disabled.reason, "market_dm_disabled")

        storage.update_member_profile_preferences(
            10,
            100,
            dm_market=True,
            quiet_hours_enabled=True,
            quiet_start_minute=23 * 60,
            quiet_end_minute=7 * 60,
        )
        with patch("modules.profile_notifications.PROFILE_TIMEZONE", timezone.utc):
            quiet = evaluate_profile_notification(
                10,
                100,
                "market",
                now=datetime(2026, 7, 18, 23, 30, tzinfo=timezone.utc),
            )
        self.assertFalse(quiet.allowed)
        self.assertEqual(quiet.reason, "quiet_hours")
        self.assertEqual(
            quiet.resume_at,
            datetime(2026, 7, 19, 7, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(
            evaluate_profile_notification(10, 100, "consensus", critical=True).allowed
        )


class ProfileUiTests(unittest.TestCase):
    @staticmethod
    def member(
        user_id: int = 100,
        role_ids: tuple[int, ...] = (1526194626531299378, 1500563715622174881),
    ) -> SimpleNamespace:
        everyone = SimpleNamespace(id=10, name="@everyone")
        member_roles = [SimpleNamespace(id=role_id, name=f"Role {role_id}") for role_id in role_ids]
        return SimpleNamespace(
            id=user_id,
            display_name="Robert Williams",
            guild=SimpleNamespace(id=10),
            joined_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            roles=[everyone, *member_roles],
            display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
        )

    def test_profile_card_prioritizes_status_and_characters(self) -> None:
        member = self.member()
        profile = SimpleNamespace(status="busy", status_note="Вернусь после крафта")
        characters = [
            SimpleNamespace(id=1, nickname="Robert Williams", static_id="00123", position=1),
            SimpleNamespace(id=2, nickname="John Doe", static_id="456", position=2),
        ]
        activity = SimpleNamespace(
            last_activity_at="2026-07-17T10:00:00+00:00",
            total_events=125,
        )

        embed = profile_embed(member, profile, characters, activity, editable=True)
        rendered = "\n".join([embed.description or "", *(str(field.value) for field in embed.fields)])
        self.assertIn("Занят", rendered)
        self.assertIn("Вернусь после крафта", rendered)
        self.assertIn("`00123`", rendered)
        self.assertIn("Основное: 📜 **Сенатор Товарищества**", rendered)
        self.assertIn("🛠️ Старший мастер крафта", rendered)
        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.fields), 25)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))

    def test_own_profile_is_editable_and_foreign_profile_is_read_only(self) -> None:
        async def inspect() -> None:
            member = self.member()
            own = ProfileHomeView(100, member, [], editable=True)
            own_labels = {item.label for item in own.children if isinstance(item, discord.ui.Button)}
            self.assertEqual(own.timeout, 900)
            self.assertIn("Добавить", own_labels)
            self.assertIn("Доступность", own_labels)
            self.assertIn("Настройки", own_labels)

            foreign = ProfileHomeView(200, member, [], editable=False)
            foreign_labels = {
                item.label for item in foreign.children if isinstance(item, discord.ui.Button)
            }
            self.assertEqual(foreign_labels, {"Обновить"})

            modal = CharacterModal(100, member)
            self.assertEqual(len(modal.children), 2)
            self.assertEqual(modal.children[0].max_length, 48)
            status_view = ProfileStatusView(100, member, None)
            select = next(item for item in status_view.children if isinstance(item, discord.ui.Select))
            self.assertEqual({option.value for option in select.options}, {"active", "busy", "away", "vacation"})

            settings = ProfileSettingsView(100, member, None, [])
            settings_labels = {
                item.label for item in settings.children if isinstance(item, discord.ui.Button)
            }
            self.assertIn("Приватность", settings_labels)
            self.assertIn("Уведомления", settings_labels)
            self.assertIn("Тихие часы", settings_labels)
            self.assertIn("По умолчанию", settings_labels)
            self.assertEqual(
                len([item for item in settings.children if isinstance(item, discord.ui.Select)]),
                1,
            )

            privacy = ProfilePrivacyView(100, member, None)
            self.assertEqual(len(privacy.children), 8)
            notifications = ProfileNotificationsView(100, member, None)
            self.assertEqual(len(notifications.children), 7)
            quiet_hours = ProfileQuietHoursView(100, member, None)
            self.assertEqual(len(quiet_hours.children), 3)

            characters = [
                SimpleNamespace(
                    id=index,
                    nickname=f"Hero {index}",
                    static_id=str(100 + index),
                    position=index,
                )
                for index in range(1, 4)
            ]
            configured = SimpleNamespace(
                visibility="members",
                show_activity=False,
                theme="gold",
                primary_character_id=2,
            )
            full_settings = ProfileSettingsView(100, member, configured, characters)
            self.assertEqual(len(full_settings.children), 9)
            self.assertIn(
                "Микрофон",
                {
                    item.label
                    for item in full_settings.children
                    if isinstance(item, discord.ui.Button)
                },
            )
            self.assertIn(
                "Веб-доступ",
                {
                    item.label
                    for item in full_settings.children
                    if isinstance(item, discord.ui.Button)
                },
            )
            self.assertEqual(
                len([item for item in full_settings.children if isinstance(item, discord.ui.Select)]),
                2,
            )
            self.assertTrue(all(0 <= int(item.row) <= 4 for item in full_settings.children))

        asyncio.run(inspect())

    def test_member_without_recognized_roles_is_a_parishioner(self) -> None:
        embed = profile_embed(self.member(role_ids=()), None, [], None, editable=False)
        position = next(
            field for field in embed.fields if field.name == "Положение в Товариществе"
        )
        self.assertIn("🕯️ **Прихожанин**", position.value)

    def test_privacy_activity_theme_and_primary_character_affect_card(self) -> None:
        member = self.member()
        characters = [
            SimpleNamespace(id=1, nickname="First Hero", static_id="100", position=1),
            SimpleNamespace(id=2, nickname="Main Hero", static_id="200", position=2),
        ]
        profile = SimpleNamespace(
            status="active",
            status_note=None,
            visibility="members",
            show_activity=False,
            show_availability=False,
            show_position=False,
            show_characters=False,
            show_join_date=False,
            theme="rose",
            primary_character_id=2,
        )
        activity = SimpleNamespace(
            last_activity_at="2026-07-18T10:00:00+00:00",
            total_events=500,
        )
        own = profile_embed(member, profile, characters, activity, editable=True)
        own_rendered = "\n".join(str(field.value) for field in own.fields)
        self.assertEqual(own.color.value, 0xEB459E)
        self.assertIn("Последняя активность", own_rendered)
        self.assertIn("500", own_rendered)
        self.assertTrue(any(field.name.startswith("⭐ ②") for field in own.fields))

        foreign = profile_embed(member, profile, characters, activity, editable=False)
        foreign_rendered = "\n".join(str(field.value) for field in foreign.fields)
        self.assertEqual(len(foreign.fields), 0)
        self.assertNotIn("Последняя активность", foreign_rendered)
        self.assertNotIn("500", foreign_rendered)

        profile.visibility = "private"
        foreign = profile_embed(member, profile, characters, activity, editable=False)
        self.assertIn("Владелец сделал профиль личным", foreign.description)
        self.assertEqual(len(foreign.fields), 0)

        settings = profile_settings_embed(member, profile, characters)
        self.assertEqual(settings.color.value, 0xEB459E)
        self.assertIn("Main Hero", "\n".join(str(field.value) for field in settings.fields))

    def test_foreign_profile_only_renders_public_characters(self) -> None:
        member = self.member()
        profile = SimpleNamespace(
            visibility="members",
            show_characters=True,
            primary_character_id=1,
        )
        characters = [
            SimpleNamespace(
                id=1,
                nickname="Public One",
                static_id="100",
                position=1,
                is_public=True,
            ),
            SimpleNamespace(
                id=2,
                nickname="Hidden Two",
                static_id="200",
                position=2,
                is_public=False,
            ),
            SimpleNamespace(
                id=3,
                nickname="Public Three",
                static_id="300",
                position=3,
                is_public=True,
            ),
        ]
        owner = profile_embed(member, profile, characters, None, editable=True)
        owner_text = "\n".join(
            f"{field.name}\n{field.value}" for field in owner.fields
        )
        self.assertIn("Hidden Two", owner_text)
        self.assertIn("Скрыт от других участников", owner_text)

        foreign = profile_embed(member, profile, characters, None, editable=False)
        foreign_text = "\n".join(
            f"{field.name}\n{field.value}" for field in foreign.fields
        )
        self.assertIn("Public One", foreign_text)
        self.assertIn("Public Three", foreign_text)
        self.assertNotIn("Hidden Two", foreign_text)
        self.assertNotIn("`200`", foreign_text)
        self.assertTrue(any("② Public Three" in field.name for field in foreign.fields))
        self.assertFalse(any("③" in field.name for field in foreign.fields))

        async def inspect_detail() -> None:
            detail = CharacterDetailView(100, member, characters[1])
            visibility = next(
                item for item in detail.children if getattr(item, "label", None) == "Показывать"
            )
            self.assertEqual(str(visibility.emoji), "🌐")

        asyncio.run(inspect_detail())

    def test_quiet_clock_parser_is_strict(self) -> None:
        self.assertEqual(parse_profile_clock("02:30"), 150)
        self.assertEqual(parse_profile_clock("2:05"), 125)
        for invalid in ("24:00", "23:60", "2", "ночь", "02.30"):
            with self.assertRaisesRegex(ValueError, "profile_quiet_hours_invalid"):
                parse_profile_clock(invalid)

    def test_profile_command_has_optional_member_argument(self) -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        setup_profile(bot)
        command = bot.tree.get_command("profile")
        self.assertIsNotNone(command)
        self.assertEqual([parameter.name for parameter in command.parameters], ["user"])
        self.assertFalse(command.parameters[0].required)


if __name__ == "__main__":
    unittest.main()
